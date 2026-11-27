"""Fixed-burst regional OPERA series, independent of MGRS production."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import time
import uuid

import numpy as np
import pandas as pd
from pyproj import Transformer
import requests
from shapely.geometry import shape
from shapely.ops import transform as transform_geometry
import zarr

from s1grits.atomic_write import atomic_write_json, atomic_write_parquet
from s1grits.file_lock import output_lock
from s1grits.provenance import _io_path
from s1grits.regions import _owner_chunk
from s1grits.spatial_inputs import region_mask
from s1grits.zarr_cf import add_cf_grid_mapping
from s1grits.zarr_encoding import create_zarr_array, record_zarr_compression
from .inventory import digest
from .reader import read_asset_window
from .state import (check_budget, group_key, job_budget, latest_products, persist,
                    physical_sample, prepare_state)


BANDS = {"VV": ("float32", np.nan), "VH": ("float32", np.nan),
         "source_index": ("uint32", 0), "quality": ("uint8", 1)}
QUALITY = {"0": "valid", "1": "no_source", "2": "invalid",
           "3": "read_failed", "4": "outside_roi", "5": "identity_unknown"}


def run_regions(config, features, products, output, *, repair=False, static_products=()):
    """Append new acquisitions or explicitly repair affected annual partitions."""
    output = Path(output)
    started = time.monotonic()
    result = {"status": "complete", "appended": 0, "repaired": 0, "skipped": 0,
              "repair_required": [], "failures": [], "downloaded_bytes": 0,
              "http_requests": 0, "cached_windows": 0}
    # Validate the destination before output_lock is allowed to create its file.
    checked = prepare_state(config, features, products, output, static_products=static_products)
    checked_products = {p["product_id"]: p for p in checked.get("products", [])}
    checked_products.update({p["product_id"]: p for p in products})
    budget = job_budget(checked, list(checked_products.values()),
                        dict(config, operation="repair" if repair else "update"))
    check_budget(budget, output, confirmed=config.get("confirm_large_job", False))
    requested_ids = {(group_key(p), p["acquisition_id"]) for p in products}
    start = pd.to_datetime(config["start"], utc=True) if config.get("start") else None
    end = pd.to_datetime(config["end"], utc=True) if config.get("end") else None
    with output_lock(_io_path(output)):
        state = prepare_state(config, features, products, output, static_products=static_products)
        output = _io_path(output)
        stores = state.setdefault("region_stores", {})
        accepted = {p["product_id"]: p for p in state.get("products", [])}
        accepted.update({p["product_id"]: p for p in products})
        all_products = list(accepted.values())
        grouped = defaultdict(list)
        for product in latest_products(all_products):
            grouped[group_key(product)].append(product)
        state["products"] = all_products
        # Persist the frozen geometry contract before any raster work can be interrupted.
        persist(state, output)
        with requests.Session() as session:
            for unit in state["units"]:
                for geometry_group, group in state["groups"].items():
                    available = grouped.get(geometry_group, [])
                    if not available:
                        continue
                    base = Path("regions") / unit.get("region_internal_id", unit["unit_id"]) / geometry_group
                    geometry = _geometry(output / base / "geometry.zarr", unit, group, config,
                                         state, session, output, result, repair=repair)
                    prefix = f"{unit['unit_id']}:{geometry_group}:"
                    previous = {record["acquisition_id"]: record for key, entry in stores.items()
                                if key.startswith(prefix) for record in entry["records"]}
                    latest_time = max((r["time_utc"] for r in previous.values()), default="")
                    events = defaultdict(list)
                    for product in available:
                        events[product["acquisition_id"]].append(product)
                    requested = defaultdict(list)
                    for acquisition_id, candidates in events.items():
                        prior = previous.get(acquisition_id)
                        utc = prior["time_utc"] if prior else min(p["acquisition_utc"] for p in candidates)
                        if repair and ((geometry_group, acquisition_id) not in requested_ids
                                       or start is not None and pd.to_datetime(utc, utc=True) < start
                                       or end is not None and pd.to_datetime(utc, utc=True) >= end):
                            continue
                        product_ids = sorted(p["product_id"] for p in candidates)
                        changed = not prior or prior["product_ids"] != product_ids or prior["status"] != "complete"
                        if not changed:
                            result["skipped"] += 1
                            continue
                        historical = prior is not None or bool(latest_time and utc <= latest_time)
                        if historical and not repair:
                            result["repair_required"].append({"unit_id": unit["unit_id"],
                                "geometry_group": geometry_group, "acquisition_id": acquisition_id})
                            continue
                        record = {"acquisition_id": acquisition_id, "time_utc": utc,
                                  "product_ids": product_ids, "status": "pending"}
                        requested[utc[:4]].append((record, candidates, historical))
                    for year, jobs in sorted(requested.items()):
                        key = prefix + year
                        entry = stores.get(key)
                        old_records = entry["records"] if entry else []
                        replacing = any(job[2] for job in jobs)
                        path = (base / "observations" / f"{year}-repair-{uuid.uuid4().hex[:10]}.zarr"
                                if replacing else Path(entry["path"]) if entry else base / "observations" / f"{year}.zarr")
                        target = _open_store(output / path, unit["grid"], geometry_group,
                                             config.get("source_strategy", "S0"))
                        changed_ids = {record["acquisition_id"] for record, _, _ in jobs}
                        if replacing:
                            originals = {r["acquisition_id"]: (i, r) for i, r in enumerate(old_records)}
                            todo = [(r, None, False) for r in old_records if r["acquisition_id"] not in changed_ids] + jobs
                            completed = []
                            source = zarr.open_group(str(output / entry["path"]), mode="r") if entry else None
                        else:
                            _truncate_tail(target, len(old_records))
                            completed = deepcopy(old_records)
                            originals, source, todo = {}, None, jobs
                        failed_repair = False
                        for record, candidates, _ in sorted(todo, key=lambda job: (job[0]["time_utc"], job[0]["acquisition_id"])):
                            index = len(completed)
                            if candidates is None:
                                old_index, record = originals[record["acquisition_id"]]
                                _copy_frame(source, target, old_index, index, unit["grid"])
                            else:
                                record = _write_frame(target, index, record, candidates, unit, group, geometry,
                                                      config, state, session, output, result)
                            if replacing and candidates is not None and record["status"] != "complete":
                                failed_repair = True
                                break
                            completed.append(record)
                            if not replacing:
                                entry = _commit_entry(state, stores, key, path, completed, output)
                                result["appended"] += 1
                        if replacing and not failed_repair:
                            _commit_entry(state, stores, key, path, completed, output)
                            result["repaired"] += len(jobs)
                        elif failed_repair:
                            result["failures"].append({"unit_id": unit["unit_id"], "year": year,
                                                       "error": "Repair left the prior committed partition active"})
                    state.setdefault("region_geometry", {})[prefix] = {
                        "path": str(base / "geometry.zarr"), "unit_id": unit["unit_id"],
                        "geometry_group": geometry_group, "grid": unit["grid"]}
        persist(state, output)
        _write_provenance_tables(state, output)
        rows = [_metrics(state, key, output) for key in state.get("region_geometry", {})]
        if rows:
            atomic_write_parquet(pd.DataFrame(rows), output / "region_catalog.parquet")
        for name in ("lia_failed_source_count", "lia_missing_source_count", "lia_missing_pixel_count"):
            result[name] = sum(row[name] for row in rows)
        result["lia_status"] = ("partial" if result["lia_failed_source_count"] else
                                "unavailable" if result["lia_missing_source_count"] else
                                "partial" if result["lia_missing_pixel_count"] else "complete")
        if result["failures"] or any(record["status"] != "complete" for entry in stores.values() for record in entry["records"]):
            result["status"] = "partial"
        elif result["repair_required"]:
            result["status"] = "repair_required"
        result["elapsed_seconds"] = time.monotonic() - started
        update_path = output / "updates" / (uuid.uuid4().hex + ".json")
        update_path.parent.mkdir(exist_ok=True)
        atomic_write_json(dict(result, operation="repair" if repair else "update"), update_path)
    return result


def _windows(grid):
    for row in range(0, grid["height"], 512):
        for col in range(0, grid["width"], 512):
            yield row, col, min(512, grid["height"] - row), min(512, grid["width"] - col)


def _slices(window):
    row, col, height, width = window
    return slice(row, row + height), slice(col, col + width)


def _open_store(path, grid, geometry_group, strategy, *, geometry=False):
    group = zarr.open_group(str(path), mode="a", zarr_format=3)
    if group.attrs.get("initialized"):
        return group
    h, w = grid["height"], grid["width"]
    a, _, x0, _, e, y0 = grid["transform"]
    for name, values, dim in (("x", x0 + (np.arange(w) + .5) * a, "x"),
                              ("y", y0 + (np.arange(h) + .5) * e, "y")):
        create_zarr_array(group, name, data=values, dimension_names=[dim], overwrite=True)
    if geometry:
        definitions = {"owner_index": ("uint32", 0), "roi_mask": ("bool", False),
                       "owner_nominal_lia": ("float32", np.nan)}
    else:
        definitions = BANDS
        arr = create_zarr_array(group, "time", shape=(0,), chunks=(1,), dtype="int64",
                                dimension_names=["time"], overwrite=True)
        arr.attrs.update(units="nanoseconds since 1970-01-01", calendar="proleptic_gregorian")
    for name, (dtype, fill) in definitions.items():
        arr = create_zarr_array(group, name, shape=(h, w) if geometry else (0, h, w),
            chunks=(min(h, 512), min(w, 512)) if geometry else (1, min(h, 512), min(w, 512)),
            dtype=dtype, fill_value=fill, dimension_names=["y", "x"] if geometry else ["time", "y", "x"],
            overwrite=True)
        if name in {"VV", "VH"}:
            arr.attrs.update(units="1", radiometry="gamma0", data_domain="linear_power")
    group.attrs.update(crs=grid["crs"], transform=grid["transform"], grid_id=grid["grid_id"],
                       geometry_group=geometry_group, owner_level="burst", source_strategy=strategy,
                       resampling_method="nearest", quality_codes=QUALITY,
                       fallback_definition="source_index > 0 and source_index != owner_index",
                       static_lia_scope="nominal burst geometry, not per-acquisition measured angle")
    add_cf_grid_mapping(group, grid["crs"])
    record_zarr_compression(group.attrs)
    group.attrs["initialized"] = True
    return group


def _geometry(path, unit, source_group, config, state, session, output, result, *, repair=False):
    geometry = _open_store(path, unit["grid"], "static", config.get("source_strategy", "S0"), geometry=True)
    complete = bool(geometry.attrs.get("owner_complete"))
    retry = {s["source_index"] for s in source_group["sources"]
             if f"lia_error_{s['source_index']}" in geometry.attrs}
    if complete and (not repair or not retry):
        return geometry
    transformer = Transformer.from_crs(4326, unit["grid"]["crs"], always_xy=True)
    polygons = [transform_geometry(transformer.transform, shape(s["geometry"])) for s in source_group["sources"]]
    row = dict(unit, geometry=shape(unit["geometry"]))
    errors = {}
    for window in _windows(unit["grid"]):
        slices = _slices(window)
        mask = np.asarray(geometry["roi_mask"][slices]) if complete else region_mask(row, unit["grid"], window)
        planned_owner, coverage = _owner_chunk(unit["grid"], polygons, window, mask)
        owner = np.asarray(geometry["owner_index"][slices]) if complete else planned_owner
        if not complete:
            geometry["owner_index"][slices] = owner
            geometry["roi_mask"][slices] = mask
        owner_lia = (np.asarray(geometry["owner_nominal_lia"][slices]) if complete else
                     np.full(owner.shape, np.nan, dtype=np.float32))
        for source, covered in zip(source_group["sources"], coverage):
            index = source["source_index"]
            if complete and index not in retry:
                continue
            name = f"nominal_lia_{index}"
            if name not in geometry:
                arr = create_zarr_array(geometry, name, shape=(unit["grid"]["height"], unit["grid"]["width"]),
                    chunks=(min(512, unit["grid"]["height"]), min(512, unit["grid"]["width"])), dtype="float32",
                    fill_value=np.nan, dimension_names=["y", "x"])
                arr.attrs.update(units="degree", source_index=index,
                                 unit_basis="official_OPERA_RTC_STATIC_catalog")
            if not source.get("static_asset") or not covered.any():
                continue
            asset = source["static_asset"]
            # The nominal STATIC reference can use a different platform from dynamic RTC.
            product = {"burst_id": source["burst_id"], "product_id": asset["product_id"]}
            try:
                sample = _read_window(session, asset, product, "lia", unit["grid"], window,
                                      config, state, output, result)
                values = np.where(covered, sample["values"], np.nan)
                geometry[name][slices] = values
                owner_lia[owner == index] = values[owner == index]
            except (ValueError, RuntimeError, OSError) as exc:
                errors[index] = str(exc)
        geometry["owner_nominal_lia"][slices] = owner_lia
    for source in source_group["sources"]:
        index = source["source_index"]
        if complete and index not in retry:
            continue
        name = f"lia_error_{index}"
        if index in errors:
            geometry.attrs[name] = errors[index]
        elif name in geometry.attrs:
            del geometry.attrs[name]
    geometry.attrs["lia_missing_source_count"] = sum(not s.get("static_asset") for s in source_group["sources"])
    geometry["owner_nominal_lia"].attrs.update(units="degree", scope="fixed owner nominal LIA")
    geometry.attrs["owner_complete"] = True
    return geometry


def _read_window(session, asset, product, role, grid, window, config, state, output, result):
    signature = digest({"asset_id": asset["asset_id"], "expected_bytes": asset["expected_bytes"],
                        "grid": grid, "window": window, "sampling": "nearest_raw_window_v1"})
    cache = output / "assets" / "windows"
    cache.mkdir(parents=True, exist_ok=True)
    array_path, meta_path = cache / (signature + ".npy"), cache / (signature + ".json")
    if meta_path.exists() and array_path.exists():
        sample = json.loads(meta_path.read_text(encoding="utf-8"))
        sample["values"] = np.load(array_path, allow_pickle=False)
        result["cached_windows"] += 1
    else:
        try:
            sample = read_asset_window(session, asset, grid, window, cache_dir=output / "assets" / "files",
                                       read_mode=config.get("read_mode", "range"))
        except Exception as exc:
            result["downloaded_bytes"] += getattr(exc, "downloaded_bytes", 0)
            result["http_requests"] += getattr(exc, "http_requests", 0)
            raise
        result["downloaded_bytes"] += sample.get("downloaded_bytes", 0)
        result["http_requests"] += sample.get("http_requests", 0)
        np.save(array_path, sample["values"], allow_pickle=False)
        metadata = {k: v for k, v in sample.items() if k != "values"}
        atomic_write_json(metadata, meta_path)
    native = {k: sample.get(k) for k in ("crs", "transform", "width", "height")}
    grid_key = (f"static:{asset['asset_id']}:lia" if role == "lia" else
                f"{group_key(product)}:{product['burst_id']}:{role}")
    old = state.setdefault("region_source_grids", {}).setdefault(grid_key, native)
    if old != native:
        raise ValueError("Native source grid changed; create a new assembly version")
    return physical_sample(sample, role, product)


def _write_frame(target, index, record, candidates, unit, source_group, geometry, config, state,
                 session, output, result):
    grid = unit["grid"]
    for name in BANDS:
        target[name].resize((index + 1, grid["height"], grid["width"]))
    products = {p["burst_id"]: p for p in candidates}
    source_rows = {s["source_index"]: {"acquisition_id": record["acquisition_id"],
        "source_index": s["source_index"], "burst_id": s["burst_id"],
        "product_id": products.get(s["burst_id"], {}).get("product_id"),
        "source_time_utc": products.get(s["burst_id"], {}).get("acquisition_utc"),
        "static_asset_id": (s.get("static_asset") or {}).get("asset_id"),
        "asset_ids_json": json.dumps({role: a["asset_id"] for role, a in products.get(s["burst_id"], {}).get("assets", {}).items()}, sort_keys=True),
        "source_status": "no_product" if s["burst_id"] not in products else "not_needed"}
        for s in source_group["sources"]}
    failures = []
    transformer = Transformer.from_crs(4326, grid["crs"], always_xy=True)
    polygons = [transform_geometry(transformer.transform, shape(s["geometry"])) for s in source_group["sources"]]
    for window in _windows(grid):
        slices = _slices(window)
        owner = np.asarray(geometry["owner_index"][slices])
        mask = np.asarray(geometry["roi_mask"][slices])
        selected = np.zeros(owner.shape, dtype="uint32")
        values = {role: np.full(owner.shape, np.nan, dtype="float32") for role in ("vv", "vh")}
        quality = np.where(mask, 1, 4).astype("uint8")
        samples = {}
        _, coverage = _owner_chunk(grid, polygons, window, mask)
        for source, covered in zip(source_group["sources"], coverage):
            number, burst = source["source_index"], source["burst_id"]
            product = products.get(burst)
            if not product or not covered.any() or (config.get("source_strategy", "S0") == "S0" and not (owner == number).any()):
                continue
            if product.get("acquisition_identity") != "verified":
                samples[number] = {"error": 5, "covered": covered}
                source_rows[number]["source_status"] = "identity_unknown"
                failures.append({"product_id": product["product_id"], "error": "Unverified acquisition identity"})
                continue
            try:
                read = {role: _read_window(session, product["assets"][role], product, role, grid,
                                           window, config, state, output, result) for role in ("vv", "vh", "mask")}
                valid = (np.isfinite(read["vv"]["values"]) & (read["vv"]["values"] > 0)
                         & np.isfinite(read["vh"]["values"]) & (read["vh"]["values"] > 0)
                         & (read["mask"]["values"] == 0) & covered)
                samples[number] = {"valid": valid, "vv": read["vv"]["values"], "vh": read["vh"]["values"]}
                if source_rows[number]["source_status"] != "read_failed":
                    source_rows[number]["source_status"] = "read"
            except (KeyError, ValueError, RuntimeError, OSError) as exc:
                samples[number] = {"error": 3, "covered": covered}
                source_rows[number]["source_status"] = "read_failed"
                source_rows[number]["error"] = str(exc)
                failures.append({"product_id": product["product_id"], "error": str(exc)})
        for number, sample in samples.items():
            own = owner == number
            if "error" in sample:
                quality[own & mask] = sample["error"]
            else:
                quality[own & mask] = 2
                take = own & sample["valid"]
                selected[take], quality[take] = number, 0
                for role in values:
                    values[role][take] = sample[role][take]
        if config.get("source_strategy", "S0") == "S1":
            for number in sorted(samples):
                sample = samples[number]
                eligible = mask & (owner > 0) & (selected == 0) & ~np.isin(quality, [3, 5])
                if "error" in sample:
                    # Unknown earlier candidates cannot be treated as confirmed invalid.
                    quality[eligible & sample["covered"]] = sample["error"]
                    continue
                take = eligible & sample["valid"]
                selected[take], quality[take] = number, 0
                for role in values:
                    values[role][take] = sample[role][take]
        for name, data in (("VV", values["vv"]), ("VH", values["vh"]),
                           ("source_index", selected), ("quality", quality)):
            target[name][(index, *slices)] = data
    target["time"].resize((index + 1,))
    target["time"][index] = pd.Timestamp(record["time_utc"]).value
    record = dict(record, status="partial" if failures else "complete", source_map=list(source_rows.values()))
    result["failures"].extend(failures)
    return record


def _truncate_tail(group, committed):
    for name in [*BANDS, "time"]:
        arr = group[name]
        if arr.shape[0] < committed:
            raise ValueError(f"Committed regional data are missing: {name}")
        if arr.shape[0] != committed:
            arr.resize((committed, *arr.shape[1:]))


def _copy_frame(source, target, old_index, index, grid):
    for name in BANDS:
        target[name].resize((index + 1, grid["height"], grid["width"]))
        for window in _windows(grid):
            slices = _slices(window)
            target[name][(index, *slices)] = source[name][(old_index, *slices)]
    target["time"].resize((index + 1,))
    target["time"][index] = source["time"][old_index]


def _commit_entry(state, stores, key, path, records, output):
    rows = [dict(row, time_index=i) for i, record in enumerate(records) for row in record["source_map"]]
    map_path = path.parent / (path.stem + ".sources-" + digest(rows)[:12] + ".parquet")
    atomic_write_parquet(pd.DataFrame(rows), output / map_path)
    stores[key] = {"path": str(path), "records": deepcopy(records), "committed_length": len(records),
                   "source_map_path": str(map_path)}
    persist(state, output)
    return stores[key]


def _write_provenance_tables(state, output):
    sources = [dict(source, geometry_group=key) for key, group in state["groups"].items()
               for source in group["sources"]]
    for name, records in (("sources", sources), ("products", state["products"])):
        rows = [{key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
                 for key, value in record.items()} for record in records]
        if rows:
            atomic_write_parquet(pd.DataFrame(rows), output / f"{name}.parquet")


def _metrics(state, prefix, output):
    definition = state["region_geometry"][prefix]
    geometry = zarr.open_group(str(output / definition["path"]), mode="r")
    entries = sorted((entry for key, entry in state["region_stores"].items() if key.startswith(prefix)),
                     key=lambda entry: entry["records"][0]["time_utc"] if entry["records"] else "")
    totals = dict(roi_pixel_count=0, source_switch_pixel_count=0, switch_eligible_pixel_count=0,
        source_switch_event_count=0, comparable_pair_count=0, gap_pair_count=0, gap_switch_count=0,
        fallback_observation_count=0, valid_observation_count=0, source_unavailable_count=0,
        source_identity_unknown_count=0, lia_missing_pixel_count=0)
    for window in _windows(definition["grid"]):
        slices = _slices(window)
        owner = np.asarray(geometry["owner_index"][slices])
        mask = np.asarray(geometry["roi_mask"][slices])
        totals["roi_pixel_count"] += int(mask.sum())
        totals["lia_missing_pixel_count"] += int((mask & (owner > 0)
            & ~np.isfinite(geometry["owner_nominal_lia"][slices])).sum())
        last = np.zeros(owner.shape, dtype="uint32")
        last_slot = np.full(owner.shape, -1, dtype="int64")
        count = np.zeros(owner.shape, dtype="uint32")
        switched = np.zeros(owner.shape, dtype=bool)
        slot = 0
        for entry in entries:
            group = zarr.open_group(str(output / entry["path"]), mode="r")
            for index in range(entry["committed_length"]):
                current = np.asarray(group["source_index"][(index, *slices)])
                quality = np.asarray(group["quality"][(index, *slices)])
                valid = mask & (current > 0)
                comparable = valid & (last > 0)
                change = comparable & (current != last)
                gap = comparable & (last_slot < slot - 1)
                totals["valid_observation_count"] += int(valid.sum())
                totals["fallback_observation_count"] += int((valid & (current != owner)).sum())
                totals["comparable_pair_count"] += int(comparable.sum())
                totals["source_switch_event_count"] += int(change.sum())
                totals["gap_pair_count"] += int(gap.sum())
                totals["gap_switch_count"] += int((gap & change).sum())
                totals["source_unavailable_count"] += int((mask & (quality == 3)).sum())
                totals["source_identity_unknown_count"] += int((mask & (quality == 5)).sum())
                switched |= change
                last[valid], last_slot[valid] = current[valid], slot
                count += valid
                slot += 1
        totals["source_switch_pixel_count"] += int(switched.sum())
        totals["switch_eligible_pixel_count"] += int((count >= 2).sum())
    for name, numerator, denominator in (
        ("source_switch_pixel_fraction", "source_switch_pixel_count", "switch_eligible_pixel_count"),
        ("switch_eligible_fraction_roi", "switch_eligible_pixel_count", "roi_pixel_count"),
        ("fallback_fraction", "fallback_observation_count", "valid_observation_count")):
        totals[name] = totals[numerator] / totals[denominator] if totals[denominator] else None
    return dict(unit_id=definition["unit_id"], geometry_group=definition["geometry_group"],
                owner_level="burst", source_strategy=geometry.attrs["source_strategy"],
                lia_failed_source_count=sum(key.startswith("lia_error_") for key in geometry.attrs),
                lia_missing_source_count=geometry.attrs.get("lia_missing_source_count", 0),
                burst_source_switch_pixel_count=totals["source_switch_pixel_count"],
                burst_source_switch_pixel_fraction=totals["source_switch_pixel_fraction"], **totals)
