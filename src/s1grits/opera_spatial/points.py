"""Incremental native OPERA point observations with frozen burst ownership."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
import uuid

import pandas as pd
from pyproj import CRS, Transformer
import requests
from shapely.geometry import Point, shape
from shapely.ops import transform

from s1grits.atomic_write import atomic_write_json, atomic_write_parquet
from s1grits.file_lock import output_lock
from .inventory import digest
from .reader import read_point_asset
from .state import (
    _validate_output, check_budget, group_key, job_budget, latest_products,
    persist, physical_sample, prepare_state,
)


_LOCAL = threading.local()
_COMPLETE_SAMPLE = {"valid", "nodata", "outside", "nonfinite", "nonpositive"}


def run_points(config, features, products, output, *, repair=False, static_products=()):
    """Append new acquisitions or explicitly repair historical observations."""
    started = time.monotonic()
    output = Path(output).resolve()
    _validate_output(output, config)
    with output_lock(output):
        state = prepare_state(config, features, products, output, static_products=static_products)
        budget = job_budget(state, state["products"] + products,
                            dict(config, operation="repair" if repair else "update"))
        check_budget(budget, output, confirmed=config.get("confirm_large_job", False))
        state.setdefault("point_owners", {})
        state.setdefault("point_grids", {})
        state.setdefault("acquisition_times", {})
        old_products = {p["product_id"]: p for p in state["products"]}
        pending_run = state.get("pending_points")
        if pending_run:
            signatures = {p["product_id"]: _product_signature(p) for p in products}
            if (pending_run["repair"] != repair
                    or any(signatures.get(pid) != signature for pid, signature in pending_run["signatures"].items())):
                raise ValueError("An interrupted point update is unpublished; replay its products and operation first")
        previous = _read_tables(output, "observations")
        previous_selected = _read_tables(output, "selected")
        if not previous.empty:
            previous = previous[previous.product_id.isin(old_products)].copy()
        work, pending = _work_products(products, old_products, previous, repair)
        accepted = dict(old_products)
        accepted.update({p["product_id"]: p for p in work})
        units = state["units"]
        _freeze_owners(state, units)
        if work:
            state["pending_points"] = {
                "product_ids": [p["product_id"] for p in work], "repair": repair,
                "signatures": {p["product_id"]: _product_signature(p) for p in work},
                "years": sorted({_utc(p["acquisition_utc"])[:4] for p in work}),
            }
            state["status"] = "running"
        persist(state, output)
        jobs = []
        for product in work:
            group = group_key(product)
            source = next(s for s in state["groups"][group]["sources"] if s["burst_id"] == product["burst_id"])
            for unit in units:
                for role in ("vv", "vh", "mask"):
                    jobs.append((unit, product, source, role, product["assets"].get(role)))
        static_jobs = []
        static_keys = set()
        needed_static = set()
        for product in work:
            group = group_key(product)
            owners = state["point_owners"][group]
            for source in state["groups"][group]["sources"]:
                if source.get("static_asset"):
                    for unit in units:
                        if source["burst_id"] == product["burst_id"] or source["source_index"] == owners[unit["point_id"]]:
                            needed_static.add((unit["point_id"], source["static_asset"]["asset_id"]))
        for group, settings in state["groups"].items():
            for source in settings["sources"]:
                asset = source.get("static_asset")
                if asset:
                    product = {"product_id": asset["product_id"], "burst_id": source["burst_id"]}
                    for unit in units:
                        key = (unit["point_id"], asset["asset_id"])
                        if key not in static_keys:
                            static_jobs.append((unit, product, source, "lia", asset))
                            static_keys.add(key)

        def acquire(job):
            needed = job[3] != "lia" or (job[0]["point_id"], job[4]["asset_id"]) in needed_static
            return _acquire(job, output, config, repair and needed, allow_network=needed)

        with ThreadPoolExecutor(max_workers=int(config.get("workers", 4))) as pool:
            samples = list(pool.map(acquire, jobs + static_jobs))
        raw_rows = []
        static_values = {}
        failed_assets = 0
        for job, sample in zip(jobs + static_jobs, samples):
            unit, product, source, role, asset = job
            try:
                sample = physical_sample(sample, role, product)
                if role != "lia" and sample["status"] in _COMPLETE_SAMPLE and "transform" in sample:
                    key = group_key(product) + ":" + product["burst_id"]
                    grid = {k: sample[k] for k in ("crs", "transform", "width", "height")}
                    if key in state["point_grids"] and state["point_grids"][key] != grid:
                        raise ValueError("Native source grid changed; create a new assembly")
                    state["point_grids"].setdefault(key, grid)
            except ValueError as error:
                sample = dict(sample, status="read_failed", value=None, error=str(error))
            needed = role != "lia" or (unit["point_id"], asset["asset_id"]) in needed_static
            failed_assets += sample["status"] == "read_failed" and needed
            if role == "lia":
                static_values[(unit["point_id"], asset["asset_id"])] = sample
            else:
                raw_rows.append(_observation(unit, product, source, role, asset, sample))
        incoming = pd.DataFrame(raw_rows)
        raw = pd.concat([previous, incoming], ignore_index=True) if not previous.empty else incoming
        if not raw.empty:
            raw = raw.drop_duplicates(["point_id", "asset_id", "sampling_signature"], keep="last")
            raw = raw.sort_values(["geometry_group", "acquisition_utc", "point_id", "product_id", "variable"])
        state["products"] = sorted(accepted.values(), key=lambda p: p["product_id"])
        selected = _selected_rows(state, raw, static_values, config.get("source_strategy", "S0"))
        selected, retained = _preserve_committed_selection(previous_selected, selected)
        changed_years = {(group_key(p), _utc(p["acquisition_utc"])[:4]) for p in work}
        # A late burst can have a different timestamp from the frozen acquisition slot.
        for p in work:
            group = group_key(p)
            when = state["acquisition_times"].get(group, {}).get(p["acquisition_id"])
            if when:
                changed_years.add((group, when[:4]))
        for group, year in sorted(changed_years):
            _write_year(raw, output, group, year, "observations")
            _write_year(selected, output, group, year, "selected")
        if not selected.empty:
            summary = _summaries(selected)
            atomic_write_parquet(summary, output / "point_catalog.parquet")
        state["committed"]["points"] = {
            "rows": len(selected), "raw_rows": len(raw),
            "max_time": None if selected.empty else selected.acquisition_utc.max(),
        }
        failed_rows = 0 if selected.empty else int(selected.status.isin(["read_failed", "identity_unknown"]).sum())
        result = {
            "status": "repair_required" if pending else "partial" if failed_assets or failed_rows or retained else "complete",
            "mode": "points", "operation": "repair" if repair else "update",
            "products_processed": len(work), "products_total": len(accepted),
            "raw_rows": len(raw), "selected_rows": len(selected),
            "repair_required": pending, "failed_assets": int(failed_assets),
            "failed_rows": failed_rows,
            "retained_after_failed_repair": retained,
            "downloaded_bytes": sum(s.get("run_downloaded_bytes", 0) for s in samples),
            "http_requests": sum(s.get("run_http_requests", 0) for s in samples),
            "cached_assets": sum(bool(s.get("cached")) for s in samples),
            "elapsed_seconds": time.monotonic() - started,
        }
        state["status"] = result["status"]
        state["last_result"] = result
        state["point_repair_pending"] = retained
        state.pop("pending_points", None)
        _write_catalogs(state, output, raw)
        persist(state, output)
        receipt = dict(result, created_utc=datetime.now(timezone.utc).isoformat(),
                       product_ids=[p["product_id"] for p in products],
                       query_start=config.get("start"), query_end=config.get("end"))
        updates = output / "updates"
        updates.mkdir(exist_ok=True)
        atomic_write_json(receipt, updates / (uuid.uuid4().hex + ".json"))
        return result


def _work_products(products, old, raw, repair):
    latest_time = {}
    for p in old.values():
        group = group_key(p)
        latest_time[group] = max(latest_time.get(group, ""), _utc(p["acquisition_utc"]))
    failed = set() if raw.empty else set(raw.loc[raw.status == "read_failed", "product_id"])
    work, pending = [], []
    for p in products:
        prior = old.get(p["product_id"])
        if prior is not None:
            if (group_key(prior) != group_key(p)
                    or any(prior.get(key) != p.get(key) for key in ("burst_id", "acquisition_id", "acquisition_utc"))):
                raise ValueError("Existing product identity changed; create a new assembly")
            if _product_signature(prior) != _product_signature(p):
                if repair:
                    work.append(p)
                else:
                    pending.append(p["product_id"])
            elif repair:
                work.append(p)
            elif p["product_id"] in failed:
                pending.append(p["product_id"])
            continue
        if not repair and _utc(p["acquisition_utc"]) <= latest_time.get(group_key(p), ""):
            pending.append(p["product_id"])
        else:
            work.append(p)
    return work, sorted(set(pending))


def _product_signature(product):
    return digest({"product_id": product["product_id"], "burst_id": product["burst_id"],
                   "group": group_key(product), "acquisition_id": product["acquisition_id"],
                   "acquisition_utc": product["acquisition_utc"],
                   "assets": {k: (v["asset_id"], v["expected_bytes"]) for k, v in product["assets"].items()}})


def _freeze_owners(state, units):
    for group, settings in state["groups"].items():
        owners = state["point_owners"].setdefault(group, {})
        for unit in units:
            if unit["point_id"] in owners:
                continue
            point = Point(unit["lon"], unit["lat"])
            crs = CRS.from_proj4(f"+proj=aeqd +lat_0={unit['lat']} +lon_0={unit['lon']} +datum=WGS84 +units=m")
            project = Transformer.from_crs(4326, crs, always_xy=True).transform
            candidates = []
            for source in settings["sources"]:
                geometry = shape(source["geometry"])
                if geometry.covers(point):
                    distance = transform(project, geometry).boundary.distance(transform(project, point))
                    candidates.append((-round(distance, 7), source["burst_id"], source["source_index"]))
            owners[unit["point_id"]] = min(candidates)[2] if candidates else 0


def _acquire(job, output, config, repair, *, allow_network=True):
    unit, product, source, role, asset = job
    signature = digest({"point_id": unit["point_id"], "lon": unit["lon"], "lat": unit["lat"],
                        "sampling": "native_containing_pixel_v1"})
    if asset is None:
        return {"status": "read_failed", "value": None, "error": "Product has no listed " + role + " asset",
                "sampling_signature": signature, "run_downloaded_bytes": 0, "run_http_requests": 0}
    identity = digest({"sampling": signature, "asset_id": asset["asset_id"],
                       "expected_bytes": asset["expected_bytes"]})
    path = output / "assets" / (identity + ".json")
    if path.exists():
        prior = json.loads(path.read_text(encoding="utf-8"))
        if prior["status"] in _COMPLETE_SAMPLE or not repair:
            return dict(prior, cached=True, run_downloaded_bytes=0, run_http_requests=0)
    if not allow_network:
        return {"status": "unavailable", "value": None, "sampling_signature": signature,
                "run_downloaded_bytes": 0, "run_http_requests": 0}
    if not hasattr(_LOCAL, "session"):
        _LOCAL.session = requests.Session()
        _LOCAL.session.headers["User-Agent"] = "S1-GRiTS-opera-spatial/1"
    received = calls = 0
    for attempt in range(2):
        try:
            sample = read_point_asset(_LOCAL.session, asset, unit["lon"], unit["lat"],
                                      cache_dir=output / "burst_cache", read_mode=config.get("read_mode", "range"))
            received += sample.get("downloaded_bytes", 0)
            calls += sample.get("http_requests", 0)
            break
        except Exception as error:
            received += getattr(error, "downloaded_bytes", 0)
            calls += getattr(error, "http_requests", 0)
            sample = {"status": "read_failed", "value": None, "error": str(error)}
            if any(code in str(error) for code in ("HTTP_401", "HTTP_403", "HTTP_404", "HTTP_410")):
                break
    sample.update(sampling_signature=signature, cached=False, asset_id=asset["asset_id"],
                  run_downloaded_bytes=received, run_http_requests=calls)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(sample, path)
    return sample


def _observation(unit, product, source, role, asset, sample):
    return {
        "point_id": unit["point_id"], "lon": unit["lon"], "lat": unit["lat"],
        "geometry_group": group_key(product), "product_id": product["product_id"],
        "burst_id": product["burst_id"], "source_index": source["source_index"],
        "acquisition_id": product["acquisition_id"], "acquisition_utc": _utc(product["acquisition_utc"]),
        "direction": product["direction"], "relative_orbit": product["relative_orbit"],
        "platform": product["platform"], "variable": role,
        "asset_id": asset["asset_id"] if asset else product["product_id"] + ":missing:" + role,
        "sampling_signature": sample["sampling_signature"],
        "value": sample.get("value") if sample["status"] == "valid" else None,
        "status": sample["status"], "error": sample.get("error"),
        "row": sample.get("row"), "col": sample.get("col"), "crs": sample.get("crs"),
        "sample_x": sample.get("centre_x"), "sample_y": sample.get("centre_y"),
        "read_mode": sample.get("read_mode"),
        "units": "1" if role in {"vv", "vh"} else "class",
        "data_domain": "linear_power" if role in {"vv", "vh"} else "categorical",
        "scale_offset_applied": sample.get("scale_offset_applied", False),
    }


def _selected_rows(state, raw, static_values, strategy):
    if raw.empty:
        return pd.DataFrame()
    lookup = {(r.point_id, r.product_id, r.variable): r for r in raw.itertuples(index=False)}
    events = {}
    for product in latest_products(state["products"]):
        events.setdefault((group_key(product), product["acquisition_id"]), []).append(product)
    rows = []
    for (group, acquisition), products in sorted(events.items()):
        times = state["acquisition_times"].setdefault(group, {})
        when = times.setdefault(acquisition, min(_utc(p["acquisition_utc"]) for p in products))
        sources = state["groups"][group]["sources"]
        by_burst = {p["burst_id"]: p for p in products}
        for unit in state["units"]:
            point_id = unit["point_id"]
            owner = state["point_owners"][group][point_id]
            candidates = []
            for source in sources:
                product = by_burst.get(source["burst_id"])
                if product is None:
                    continue
                samples = {role: lookup.get((point_id, product["product_id"], role)) for role in ("vv", "vh", "mask")}
                if product.get("acquisition_identity") != "verified":
                    status = "identity_unknown"
                elif any(s is None or s.status == "read_failed" for s in samples.values()):
                    status = "read_failed"
                else:
                    status = "valid" if (all(s.status == "valid" for s in samples.values())
                                         and samples["vv"].value > 0 and samples["vh"].value > 0
                                         and samples["mask"].value == 0) else "invalid"
                candidates.append((source, product, samples, status))
            owner_candidate = next((c for c in candidates if c[0]["source_index"] == owner), None)
            chosen = owner_candidate if owner_candidate and owner_candidate[3] == "valid" else None
            status = "outside" if owner == 0 else owner_candidate[3] if owner_candidate else "no_source"
            if owner and chosen is None and strategy == "S1" and status not in {"read_failed", "identity_unknown"}:
                for candidate in sorted(candidates, key=lambda c: c[0]["source_index"]):
                    if candidate[3] in {"read_failed", "identity_unknown"}:
                        status = candidate[3]
                        break
                    if candidate[3] == "valid":
                        chosen = candidate
                        break
            source_index = chosen[0]["source_index"] if chosen else 0
            owner_source = next((s for s in sources if s["source_index"] == owner), None)
            owner_lia = _lia(point_id, owner_source, static_values)
            selected_lia = _lia(point_id, chosen[0], static_values) if chosen else None
            vv = float(chosen[2]["vv"].value) if chosen else None
            vh = float(chosen[2]["vh"].value) if chosen else None
            rows.append(dict(
                point_id=point_id, geometry_group=group, acquisition_id=acquisition, acquisition_utc=when,
                owner_index=owner, owner_burst_id=owner_source["burst_id"] if owner_source else None,
                source_index=source_index, burst_id=chosen[0]["burst_id"] if chosen else None,
                product_id=chosen[1]["product_id"] if chosen else None,
                direction=state["groups"][group]["direction"],
                relative_orbit=state["groups"][group]["relative_orbit"],
                platform=state["groups"][group]["platform"], source_strategy=strategy,
                fallback=bool(chosen and source_index != owner), status="valid" if chosen else status,
                vv=vv, vh=vh, vv_db=10 * math.log10(vv) if vv is not None else None,
                vh_db=10 * math.log10(vh) if vh is not None else None,
                owner_nominal_lia=owner_lia, selected_nominal_lia=selected_lia,
                delta_nominal_lia_to_owner=(selected_lia - owner_lia)
                if selected_lia is not None and owner_lia is not None else None,
            ))
    return pd.DataFrame(rows).sort_values(["geometry_group", "point_id", "acquisition_utc", "acquisition_id"])


def _lia(point_id, source, values):
    if not source or not source.get("static_asset"):
        return None
    sample = values.get((point_id, source["static_asset"]["asset_id"]))
    return sample.get("value") if sample and sample["status"] == "valid" else None


def _preserve_committed_selection(previous, selected):
    """A failed replacement is unpublished; it is not a new burst fallback."""
    if previous.empty or selected.empty:
        return selected, []
    keys = ["geometry_group", "point_id", "acquisition_id"]
    old = previous[~previous.status.isin(["read_failed", "identity_unknown"])].set_index(keys)
    retained = []
    for index, row in selected[selected.status.isin(["read_failed", "identity_unknown"])].iterrows():
        key = tuple(row[k] for k in keys)
        if key not in old.index:
            continue
        prior = old.loc[key]
        retained.append({**{k: row[k] for k in keys}, "attempted_status": row.status,
                         "retained_product_id": prior.product_id})
        for column in prior.index:
            selected.at[index, column] = prior[column]
    return selected, retained


def _summaries(selected):
    rows = []
    for (group, point), frame in selected.groupby(["geometry_group", "point_id"], sort=True):
        frame = frame.sort_values("acquisition_utc").reset_index(drop=True)
        valid = frame[frame.status == "valid"]
        switches = valid.source_index.ne(valid.source_index.shift()).iloc[1:]
        gaps = valid.index.to_series().diff().iloc[1:] > 1
        pairs = max(0, len(valid) - 1)
        count = int(switches.sum())
        rows.append(dict(geometry_group=group, point_id=point, owner_index=int(frame.owner_index.iloc[0]),
                         owner_level="burst", source_strategy=frame.source_strategy.iloc[0],
                         observation_count=len(frame), valid_observation_count=len(valid),
                         source_switch_event_count=count, comparable_pair_count=pairs,
                         source_switch_pixel_count=int(count > 0), switch_eligible_pixel_count=int(pairs > 0),
                         source_switch_pixel_fraction=float(count > 0) if pairs else None,
                         switch_eligible_fraction_roi=float(pairs > 0),
                         gap_pair_count=int(gaps.sum()), gap_switch_count=int((gaps & switches).sum()),
                         fallback_observation_count=int(valid.fallback.sum()),
                         fallback_fraction=float(valid.fallback.mean()) if len(valid) else None,
                         source_identity_unknown_count=int((frame.status == "identity_unknown").sum()),
                         source_unavailable_count=int((frame.status == "read_failed").sum())))
    return pd.DataFrame(rows)


def _read_tables(output, kind):
    paths = sorted((output / "points").glob("*/" + kind + "/year=*.parquet"))
    return pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True) if paths else pd.DataFrame()


def _write_catalogs(state, output, raw):
    sources = []
    for group, settings in state["groups"].items():
        for source in settings["sources"]:
            row = dict(source, geometry_group=group, direction=settings["direction"],
                       relative_orbit=settings["relative_orbit"], platform=settings["platform"], owner_level="burst")
            row["geometry_json"] = json.dumps(row.pop("geometry"), sort_keys=True)
            row["static_asset_json"] = json.dumps(row.pop("static_asset", None), sort_keys=True)
            sources.append(row)
    products = []
    for product in state["products"]:
        row = dict(product, geometry_group=group_key(product))
        samples = raw[raw.product_id == product["product_id"]] if not raw.empty else raw
        failed = int((samples.status == "read_failed").sum()) if not samples.empty else 0
        row.update(source_status="read_failed" if failed else "sampled", failed_asset_sample_count=failed)
        for key in list(row):
            if isinstance(row[key], (dict, list)):
                row[key + "_json"] = json.dumps(row.pop(key), sort_keys=True)
        products.append(row)
    if sources:
        atomic_write_parquet(pd.DataFrame(sources), output / "sources.parquet")
    if products:
        atomic_write_parquet(pd.DataFrame(products), output / "products.parquet")


def _write_year(frame, output, group, year, kind):
    if frame.empty:
        return
    rows = frame[(frame.geometry_group == group) & frame.acquisition_utc.str.startswith(year)]
    if rows.empty:
        return
    path = output / "points" / group / kind / ("year=" + year + ".parquet")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(rows.reset_index(drop=True), path)


def _utc(value):
    return pd.to_datetime(value, utc=True).isoformat().replace("+00:00", "Z")
