"""Fixed-region exports from existing, read-only S1-GRiTS stores."""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import shapely
import zarr
from affine import Affine
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, bounds, from_bounds
from shapely.geometry import shape
from shapely.ops import transform as transform_geometry

from .atomic_write import atomic_path, atomic_write_parquet
from .provenance import _io_path
from .zarr_cf import add_cf_grid_mapping
from .zarr_encoding import create_zarr_array, record_zarr_compression


def plan_region_outputs(config, regions_gdf, sources) -> list[dict]:
    """Resolve independent region groups, fixed grids and acquisition slots."""
    from .spatial_inputs import build_region_grid

    plans = []
    strategy = config.get("source_strategy", "S0")
    if strategy not in {"S0", "S1"}:
        raise ValueError("source_strategy must be S0 or S1")
    for _, row in regions_gdf.iterrows():
        grid = build_region_grid(row, config)
        groups = defaultdict(list)
        for source in sources:
            if source.get("nominal_geometry") is None:
                raise ValueError(f"Verified nominal source geometry is unavailable: {source['path']}")
            nominal = shape(source["nominal_geometry"])
            if not row.geometry.intersection(nominal).area:
                continue
            if len(source.get("track_members", [])) != 1 or source.get("track") is None:
                raise ValueError("Region export requires verified single-track sources")
            if source.get("direction") not in {"ASCENDING", "DESCENDING"}:
                raise ValueError(f"Region source flight direction is unknown: {source['path']}")
            if source.get("metadata_time_conflict"):
                raise ValueError(f"Region source semantics differ across selected times: {source['path']}")
            semantic = source.get("semantic_signature")
            if not semantic:
                raise ValueError("Region processing compatibility is unknown")
            key = (source["product_type"], source["direction"], source["track"], semantic)
            groups[key].append(source)
        if not groups:
            plans.append({"region_id": str(row.region_id),
                          "region_internal_id": str(row.region_internal_id),
                          "grid": grid, "status": "no_source", "sources": [], "times": [],
                          "logical_bytes": 0})
        for (product, direction, track, semantic), members in sorted(groups.items()):
            members.sort(key=lambda source: source["source_unit_id"])
            if len({s["source_unit_id"] for s in members}) != len(members):
                raise ValueError("Duplicate region source_unit_id")
            bands = {s["source_unit_id"]: _source_bands(s) for s in members}
            for source in members:
                for name in bands[source["source_unit_id"]]:
                    metadata = source["variables"][name]
                    if metadata.get("declared_scale", 1) != 1 or metadata.get("declared_offset", 0) != 0:
                        raise ValueError(f"Region export requires unpacked values; scale/offset declared for {source['path']}:{name}")
            domains = {_band_domain(s, bands[s["source_unit_id"]][0]) for s in members}
            if len(domains) != 1 or "unknown" in domains:
                raise ValueError("Region source units are unknown or incompatible; supply verified provenance metadata: "
                                 + ", ".join(s["path"] for s in members))
            domain = domains.pop()
            if any(_band_domain(s, bands[s["source_unit_id"]][1]) != domain for s in members):
                raise ValueError("VV/VH domains differ")
            aggregation_method, aggregation_domain = None, None
            if product == "smonthly":
                methods = {s.get("aggregation_method") for s in members}
                if len(methods) != 1 or not next(iter(methods)):
                    raise ValueError("Monthly aggregation compatibility is unknown")
                aggregation_method = next(iter(methods))
                aggregation_domains = {s.get("aggregation_domain") or "unknown" for s in members}
                if len(aggregation_domains) != 1 or (len(members) > 1 and "unknown" in aggregation_domains):
                    raise ValueError("Cross-source monthly aggregation domains must be known and compatible")
                aggregation_domain = next(iter(aggregation_domains))
            elif product != "scenes":
                raise ValueError("Region export supports scenes and smonthly")
            counts = ["n_obs" in s["variables"] for s in members]
            if any(counts) and not all(counts):
                raise ValueError("Selected monthly sources have inconsistent n_obs availability")
            times = _time_slots(members, product)
            dtype = np.result_type(*[np.dtype(s["variables"][name]["dtype"])
                                     for s in members for name in bands[s["source_unit_id"]]], np.float32)
            n_dtype = np.result_type(*[s["variables"]["n_obs"]["dtype"] for s in members]) if all(counts) else None
            if n_dtype is not None and n_dtype.kind not in "iu":
                raise ValueError("n_obs must retain an integer storage type")
            method = config.get("grid", {}).get("resampling_method", "auto")
            if method == "auto":
                method = "bilinear" if abs(grid["transform"][0]) == 10 else "nearest"
            if method not in {"nearest", "bilinear"}:
                raise ValueError("Region resampling must be nearest or bilinear")
            processing_signature = hashlib.sha256(json.dumps(
                {"source_semantic_signature": semantic, "source_strategy": strategy,
                 "resampling_method": method, "domain": domain, "owner_level": "source_product",
                 "aggregation_method": aggregation_method, "aggregation_domain": aggregation_domain,
                 "workflow": "region_export_v1"}, sort_keys=True).encode()).hexdigest()
            variant = processing_signature[:12]
            group_id = f"{row.region_internal_id}:{direction}:TK{track:03d}:{variant}"
            spatial_mode = row.get("spatial_mode", config.get("spatial", {}).get("mode"))
            relative = Path(str(spatial_mode)) / str(row.region_internal_id) / f"{direction}_TK{track:03d}_{variant}"
            pixels = grid["width"] * grid["height"]
            dynamic_bytes = pixels * len(times) * (2 * dtype.itemsize + 4 + (n_dtype.itemsize if n_dtype is not None else 0))
            plan = {"region_id": str(row.region_id), "region_internal_id": str(row.region_internal_id),
                    "spatial_mode": spatial_mode,
                    "region_group_id": group_id, "grid": grid, "product_type": product,
                    "direction": direction, "track": track, "processing_signature": processing_signature,
                    "source_semantic_signature": str(semantic),
                    "grid_mode": config.get("grid", {}).get("mode"),
                    "grid_origin": config.get("grid", {}).get("origin", [0, 0]),
                    "source_strategy": strategy, "resampling_method": method, "data_domain": domain,
                    "aggregation_method": aggregation_method, "aggregation_domain": aggregation_domain,
                    "sources": members, "bands": bands, "times": times, "dtype": str(dtype),
                    "n_obs_dtype": str(n_dtype) if n_dtype is not None else None,
                    "output_variables": ["VV_dB", "VH_dB"] if domain == "db" else ["VV", "VH"],
                    "zarr_path": (relative / f"{product}.zarr").as_posix(),
                    "status": "planned", "logical_bytes": int(dynamic_bytes + pixels * 5 +
                                                                 (grid["width"] + grid["height"] + len(times)) * 8)}
            if config.get("include_static"):
                static_names = sorted({name for s in members for name, meta in (s.get("static_source") or {}).get("variables", {}).items()
                                       if list(meta.get("dims", [])) == ["y", "x"]})
                plan["static_variables"] = static_names
                plan["static_dtypes"] = {name: str(np.result_type(*[
                    s["static_source"]["variables"][name]["dtype"] for s in members
                    if s.get("static_source") and name in s["static_source"]["variables"]], np.float32))
                    for name in static_names}
                plan["logical_bytes"] += pixels * (4 + sum(np.dtype(value).itemsize for value in plan["static_dtypes"].values()))
            plans.append(plan)
    return plans


def export_regions(config, regions_gdf, sources, output_dir: Path, manifest: dict,
                   checkpoint, planned=None) -> dict:
    """Write bounded time/chunk units; only committed time units are resumed."""
    from .spatial_inputs import region_mask

    logical_output = Path(output_dir)
    output_dir = _io_path(logical_output)
    output_dir.mkdir(parents=True, exist_ok=True)
    plans = planned if planned is not None else plan_region_outputs(config, regions_gdf, sources)
    completed = manifest.setdefault("completed", [])
    errors_by_unit = manifest.setdefault("region_errors", {})
    region_rows = {str(row.region_id): row for _, row in regions_gdf.iterrows()}
    records, source_records = [], []
    for plan in plans:
        if plan["status"] == "no_source":
            records.append({"region_id": plan["region_id"], "region_internal_id": plan["region_internal_id"],
                            "status": "no_source", "zarr_path": None})
            continue
        row = region_rows[plan["region_id"]]
        grid = plan["grid"]
        group = _open_region_store(output_dir / plan["zarr_path"], plan)
        projected = _projected_sources(plan)
        source_map = []
        for index, source in enumerate(plan["sources"], 1):
            source_records.append({"region_group_id": plan["region_group_id"], "source_unit_index": index,
                                   "source_unit_id": source["source_unit_id"], "path": str(source["path"]),
                                   "geometry_group_id": source.get("geometry_group_id"), "owner_level": "source_product",
                                   "metadata_evidence_json": json.dumps(source.get("metadata_evidence", {}), sort_keys=True)})
        for window in _windows(grid):
            slices = _slices(window)
            mask = region_mask(row, grid, window)
            owner, _ = _owner_chunk(grid, projected, window, mask)
            group["roi_mask"][slices] = mask
            group["owner_index"][slices] = owner
        for output_index, slot in enumerate(plan["times"]):
            unit = f"region:{plan['region_id']}:{plan['region_group_id']}:{slot['key']}"
            for index, source in enumerate(plan["sources"], 1):
                member = slot["members"].get(source["source_unit_id"])
                if member is not None:
                    source_map.append({"time_index": output_index, "source_index": index,
                                       "source_unit_id": source["source_unit_id"], "source_path": str(source["path"]),
                                       "source_time_index": member["time_index"], "acquisition_id": member.get("acquisition_id"),
                                       "time_utc": member["time_utc"], "period_start": member.get("period_start"),
                                       "period_end": member.get("period_end"), "output_id": member.get("output_id"),
                                       "generation_id": member.get("generation_id"),
                                       "variable_semantics_json": json.dumps(member.get("variable_semantics", {}), sort_keys=True)})
            if unit in completed:
                continue
            checkpoint()
            failures = {}
            for window in _windows(grid):
                slices = _slices(window)
                mask = np.asarray(group["roi_mask"][slices])
                owner = np.asarray(group["owner_index"][slices])
                _, coverage = _owner_chunk(grid, projected, window, mask)
                values, selected, counts, chunk_errors = _select_chunk(plan, slot, window, owner, coverage)
                for failure in chunk_errors:
                    failures[(failure["source_unit_id"], failure["variable"])] = failure
                for name, array in zip(plan["output_variables"], values):
                    group[name][(output_index, *slices)] = array
                group["source_index"][(output_index, *slices)] = selected
                if counts is not None:
                    group["n_obs"][(output_index, *slices)] = counts
            if failures:
                errors_by_unit[unit] = list(failures.values())
            else:
                errors_by_unit.pop(unit, None)
                completed.append(unit)
            checkpoint()
        parent = (output_dir / plan["zarr_path"]).parent
        atomic_write_parquet(pd.DataFrame(source_map), parent / "source_map.parquet")
        if config.get("include_static"):
            static_key = f"region:{plan['region_id']}:{plan['region_group_id']}:static"
            if static_key not in completed:
                checkpoint()
                failures = _write_static(plan, group, parent / "static.zarr")
                if failures:
                    errors_by_unit[static_key] = failures
                else:
                    errors_by_unit.pop(static_key, None)
                    completed.append(static_key)
                checkpoint()
        prefix = f"region:{plan['region_id']}:{plan['region_group_id']}:"
        failure_count = sum(len(value) for key, value in errors_by_unit.items() if key.startswith(prefix))
        for record in source_records:
            if record["region_group_id"] != plan["region_group_id"]:
                continue
            source_errors = [error for key, errors in errors_by_unit.items() if key.startswith(prefix)
                             for error in errors if error.get("source_unit_id") == record["source_unit_id"]]
            record["source_status"] = "read_failed" if source_errors else "available"
            record["errors_json"] = json.dumps(source_errors, ensure_ascii=False)
        metrics = _source_metrics(group, grid, len(plan["times"]))
        status = "partial" if failure_count and metrics["valid_observation_count"] else "failed" if failure_count else "complete"
        group.attrs.update({"status": status, **metrics})
        records.append({"region_id": plan["region_id"], "region_internal_id": plan["region_internal_id"],
                        "region_group_id": plan["region_group_id"], "spatial_mode": plan["spatial_mode"],
                        "product_type": plan["product_type"], "direction": plan["direction"], "track": plan["track"],
                        "processing_signature": plan["processing_signature"], "grid_id": grid["grid_id"],
                        "grid_mode": config.get("grid", {}).get("mode"), "grid_origin": config.get("grid", {}).get("origin", [0, 0]),
                        "crs": grid["crs"], "transform": list(grid["transform"]), "width": grid["width"], "height": grid["height"],
                        "time_start": plan["times"][0]["time_utc"] if plan["times"] else None,
                        "time_end": plan["times"][-1]["time_utc"] if plan["times"] else None,
                        "zarr_path": plan["zarr_path"], "status": status, "source_strategy": plan["source_strategy"],
                        "owner_level": "source_product", "source_unavailable_count": failure_count,
                        "source_identity_unknown_count": 0, "burst_source_switch_pixel_count": None,
                        "burst_source_switch_pixel_fraction": None, **metrics})
    with atomic_path(output_dir / "regions.parquet") as temporary:
        regions_gdf.to_parquet(temporary, index=False)
    atomic_write_parquet(pd.DataFrame(source_records), output_dir / "sources.parquet")
    atomic_write_parquet(pd.DataFrame(records), output_dir / "region_catalog.parquet")
    failed = sum(record["status"] in {"partial", "failed"} for record in records)
    status = "no_source" if all(record["status"] == "no_source" for record in records) else "partial" if failed else "complete"
    return {"status": status, "regions": len(regions_gdf),
            "outputs": len(records), "failed_outputs": failed, "records": records,
            "catalog": str(logical_output / "region_catalog.parquet")}


def _source_bands(source):
    result = []
    for polarization in ("VV", "VH"):
        candidates = [name for name in source["variables"] if name.upper() in {polarization, f"{polarization}_DB"}]
        if len(candidates) != 1:
            raise ValueError(f"Source must have exactly one {polarization} band: {source['path']}")
        result.append(candidates[0])
    return result


def _band_domain(source, name):
    units = str(source["variables"][name].get("units") or "").lower()
    domain = str(source["variables"][name].get("data_domain") or source.get("data_domain") or "").lower()
    if (units in {"db", "decibel", "decibels"} and domain in {"linear", "linear_power", "power"}
            or units in {"1", "linear", "linear_power"} and domain in {"db", "decibel", "decibels"}):
        raise ValueError(f"Conflicting units and data domain: {source['path']}:{name}")
    if units in {"db", "decibel", "decibels"} or domain in {"db", "decibel", "decibels"}:
        return "db"
    if units in {"linear", "linear_power"} or domain in {"linear", "linear_power", "power"}:
        return "linear"
    return "unknown"


def _time_slots(sources, product):
    slots = {}
    for source in sources:
        for record in source["times"]:
            if product == "smonthly":
                start, end = record.get("period_start"), record.get("period_end")
                if not start or not end:
                    raise ValueError("Monthly region sources require verified UTC period boundaries")
                key = f"month:{pd.Timestamp(start).isoformat()}:{pd.Timestamp(end).isoformat()}"
            elif record.get("acquisition_id"):
                key = f"acquisition:{record.get('platform') or ''}:{record['acquisition_id']}"
            elif len(sources) == 1:
                key = f"source:{source['source_unit_id']}:{record['time_index']}"
            else:
                raise ValueError("Cross-source scenes require verified acquisition identity")
            time = record["period_start"] if product == "smonthly" else record["time_utc"]
            slot = slots.setdefault(key, {"key": key, "time_utc": str(time),
                                         "period_start": record.get("period_start"), "period_end": record.get("period_end"),
                                         "members": {}})
            if source["source_unit_id"] in slot["members"]:
                raise ValueError("A source has multiple records for one region acquisition/period")
            slot["members"][source["source_unit_id"]] = record
            if pd.Timestamp(time) < pd.Timestamp(slot["time_utc"]):
                slot["time_utc"] = str(time)
    return sorted(slots.values(), key=lambda slot: (pd.Timestamp(slot["time_utc"]), slot["key"]))


def _windows(grid):
    for row in range(0, grid["height"], 512):
        for col in range(0, grid["width"], 512):
            yield row, col, min(512, grid["height"] - row), min(512, grid["width"] - col)


def _slices(window):
    row, col, height, width = window
    return slice(row, row + height), slice(col, col + width)


def _projected_sources(plan):
    transformer = Transformer.from_crs("EPSG:4326", plan["grid"]["crs"], always_xy=True)
    return [transform_geometry(transformer.transform, shape(s["nominal_geometry"])) for s in plan["sources"]]


def _owner_chunk(grid, polygons, window, mask):
    row, col, height, width = window
    affine = Affine(*grid["transform"])
    x, y = np.meshgrid(affine.c + (np.arange(col, col + width) + 0.5) * affine.a,
                       affine.f + (np.arange(row, row + height) + 0.5) * affine.e)
    points = shapely.points(x, y)
    owner = np.zeros((height, width), dtype=np.uint32)
    distance = np.full((height, width), -1.0)
    coverage = []
    for index, polygon in enumerate(polygons, 1):
        covered = shapely.covers(polygon, points) & mask
        coverage.append(covered)
        inside_distance = shapely.distance(polygon.boundary, points)
        # Projection round trips perturb mathematically equal edges below a micrometre.
        tied = np.isclose(inside_distance, distance, rtol=0, atol=1e-7)
        chosen = covered & (inside_distance > distance) & ~tied
        owner[chosen] = index
        distance[chosen] = inside_distance[chosen]
    return owner, coverage


def _warp_window(source, variable, time_index, grid, window, method, domain=None):
    from .spatial_sources import read_source_window

    row, col, height, width = window
    target_transform = Affine(*grid["transform"]) * Affine.translation(col, row)
    source_transform = Affine(*source["transform"])
    target_bounds = bounds(Window(0, 0, width, height), target_transform)
    source_bounds = transform_bounds(grid["crs"], source["crs"], *target_bounds, densify_pts=21)
    source_window = from_bounds(*source_bounds, transform=source_transform)
    padding = 1 if method == "bilinear" else 0
    r0 = max(0, math.floor(source_window.row_off) - padding)
    c0 = max(0, math.floor(source_window.col_off) - padding)
    r1 = min(source["height"], math.ceil(source_window.row_off + source_window.height) + padding)
    c1 = min(source["width"], math.ceil(source_window.col_off + source_window.width) + padding)
    output = np.full((height, width), np.nan, dtype=np.float64)
    if r1 <= r0 or c1 <= c0:
        return output
    values = np.asarray(read_source_window(source, variable, time_index, (r0, c0, r1 - r0, c1 - c0)), dtype=np.float64)
    if domain == "linear":
        values[values <= 0] = np.nan
    if method == "bilinear" and domain == "db":
        values = np.power(10.0, values / 10.0)
    reproject(values, output, src_transform=source_transform * Affine.translation(c0, r0),
              src_crs=source["crs"], src_nodata=np.nan, dst_transform=target_transform,
              dst_crs=grid["crs"], dst_nodata=np.nan, resampling=Resampling[method])
    if method == "bilinear" and domain == "db":
        good = np.isfinite(output) & (output > 0)
        converted = np.full_like(output, np.nan)
        converted[good] = 10 * np.log10(output[good])
        output = converted
    return output


def _select_chunk(plan, slot, window, owner, coverage):
    shape_ = owner.shape
    values = [np.full(shape_, np.nan, dtype=plan["dtype"]) for _ in range(2)]
    selected = np.zeros(shape_, dtype=np.uint32)
    counts = np.zeros(shape_, dtype=plan["n_obs_dtype"]) if plan["n_obs_dtype"] else None
    blocked = np.zeros(shape_, dtype=bool)
    cache, failures = {}, []

    def read(index):
        if index in cache:
            return cache[index]
        source = plan["sources"][index - 1]
        unit_id = source["source_unit_id"]
        member = slot["members"].get(unit_id)
        if member is None:
            cache[index] = (None, False)
            return cache[index]
        arrays = []
        names = list(plan["bands"][unit_id]) + (["n_obs"] if counts is not None else [])
        for name in names:
            try:
                arrays.append(_warp_window(source, name, member["time_index"], plan["grid"], window,
                                           "nearest" if name == "n_obs" else plan["resampling_method"],
                                           None if name == "n_obs" else plan["data_domain"]))
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                failures.append({"source_unit_id": unit_id, "variable": name,
                                 "time_index": member["time_index"], "source_status": "read_failed", "error_message": str(exc)})
                cache[index] = (None, True)
                return cache[index]
        cache[index] = (arrays, False)
        return cache[index]

    def choose(index, requested):
        if not requested.any():
            return
        arrays, failed = read(index)
        if failed:
            blocked[requested] = True
        elif arrays is not None:
            valid = requested & np.isfinite(arrays[0]) & np.isfinite(arrays[1])
            for destination, array in zip(values, arrays):
                destination[valid] = array[valid]
            selected[valid] = index
            if counts is not None:
                count_valid = valid & np.isfinite(arrays[2])
                counts[count_valid] = arrays[2][count_valid].astype(plan["n_obs_dtype"])

    for index in range(1, len(plan["sources"]) + 1):
        choose(index, owner == index)
    if plan["source_strategy"] == "S1":
        for index in range(1, len(plan["sources"]) + 1):
            choose(index, (selected == 0) & ~blocked & coverage[index - 1])
    return values, selected, counts, failures


def _open_region_store(path, plan, static=False):
    group = zarr.open_group(str(_io_path(path)), mode="a", zarr_format=3)
    if group.attrs.get("region_initialized"):
        return group
    grid = plan["grid"]
    height, width = grid["height"], grid["width"]
    affine = Affine(*grid["transform"])
    dimensions = ("y", "x") if static else ("time", "y", "x")
    array_shape = (height, width) if static else (len(plan["times"]), height, width)
    chunks = (min(512, height), min(512, width)) if static else (1, min(512, height), min(512, width))
    for name, data, dimension in (("x", affine.c + (np.arange(width) + .5) * affine.a, "x"),
                                  ("y", affine.f + (np.arange(height) + .5) * affine.e, "y")):
        create_zarr_array(group, name, data=data, dimension_names=[dimension], overwrite=True)
    if not static:
        times = np.asarray([pd.Timestamp(t["time_utc"]).value for t in plan["times"]], dtype=np.int64)
        time_array = create_zarr_array(group, "time", data=times, dimension_names=["time"], overwrite=True)
        time_array.attrs.update({"units": "nanoseconds since 1970-01-01", "calendar": "proleptic_gregorian"})
    names = plan.get("static_variables", []) if static else plan["output_variables"]
    for name in names:
        dtype = plan["static_dtypes"][name] if static else plan["dtype"]
        array = create_zarr_array(group, name, shape=array_shape, chunks=chunks, dtype=dtype,
                                  fill_value=np.nan, dimension_names=list(dimensions), overwrite=True)
        array.attrs["grid_mapping"] = "spatial_ref"
        if not static:
            array.attrs["units"] = "dB" if plan["data_domain"] == "db" else "1"
        else:
            units = {s["static_source"]["variables"][name].get("units") for s in plan["sources"]
                     if s.get("static_source") and name in s["static_source"]["variables"]}
            if len(units) == 1 and next(iter(units)) is not None:
                array.attrs["units"] = units.pop()
    create_zarr_array(group, "source_index", shape=array_shape, chunks=chunks, dtype="uint32", fill_value=0,
                      dimension_names=list(dimensions), overwrite=True)
    if not static:
        for name, dtype in (("roi_mask", "bool"), ("owner_index", "uint32")):
            create_zarr_array(group, name, shape=(height, width), chunks=chunks[-2:], dtype=dtype,
                              fill_value=0, dimension_names=["y", "x"], overwrite=True)
        if plan["n_obs_dtype"]:
            array = create_zarr_array(group, "n_obs", shape=array_shape, chunks=chunks, dtype=plan["n_obs_dtype"],
                                      fill_value=0, dimension_names=list(dimensions), overwrite=True)
            definitions = {s.get("n_obs_definition") or "unknown" for s in plan["sources"]}
            array.attrs.update({"n_obs_definition": next(iter(definitions)) if len(definitions) == 1 else "unknown",
                                "n_obs_unique_acquisitions": False, "outside_valid_output": 0})
    group.attrs.update({"crs": grid["crs"], "transform": list(grid["transform"]), "grid_id": grid["grid_id"],
                        "region_id": plan["region_id"], "region_group_id": plan["region_group_id"],
                        "owner_level": "source_product", "source_strategy": plan["source_strategy"],
                        "resampling_method": plan["resampling_method"], "processing_signature": plan["processing_signature"],
                        "source_semantic_signature": plan["source_semantic_signature"],
                        "spatial_mode": plan["spatial_mode"], "grid_mode": plan["grid_mode"], "grid_origin": plan["grid_origin"],
                        "product_type": "static" if static else plan["product_type"]})
    if not static:
        group.attrs.update({"data_domain": plan["data_domain"], "aggregation_method": plan["aggregation_method"],
                            "aggregation_domain": plan["aggregation_domain"]})
    add_cf_grid_mapping(group, grid["crs"])
    record_zarr_compression(group.attrs)
    group.attrs["region_initialized"] = True
    return group


def _write_static(plan, dynamic, path):
    static = _open_region_store(path, plan, static=True)
    errors = {}
    for window in _windows(plan["grid"]):
        slices = _slices(window)
        owner = np.asarray(dynamic["owner_index"][slices])
        static["source_index"][slices] = owner
        for name in plan.get("static_variables", []):
            values = np.full(owner.shape, np.nan, dtype=plan["static_dtypes"][name])
            for index, source in enumerate(plan["sources"], 1):
                positions = owner == index
                if not positions.any():
                    continue
                reference = source.get("static_source")
                try:
                    if reference is None or name not in reference["variables"]:
                        raise ValueError("Matching owner static layer is unavailable")
                    array = _warp_window(reference, name, None, plan["grid"], window,
                                         "nearest" if name == "ls_map" else plan["resampling_method"])
                    values[positions] = array[positions]
                except (OSError, ValueError, KeyError, RuntimeError) as exc:
                    errors[(source["source_unit_id"], name)] = {"source_unit_id": source["source_unit_id"],
                                                              "variable": name, "source_status": "read_failed", "error_message": str(exc)}
            static[name][slices] = values
    if not plan.get("static_variables"):
        for source in plan["sources"]:
            errors[(source["source_unit_id"], "static")] = {"source_unit_id": source["source_unit_id"],
                                                          "variable": "static", "source_status": "read_failed",
                                                          "error_message": "No matching static variables"}
    return list(errors.values())


def _source_metrics(group, grid, time_count):
    totals = dict(source_switch_pixel_count=0, switch_eligible_pixel_count=0, source_switch_event_count=0,
                  comparable_pair_count=0, gap_pair_count=0, gap_switch_count=0,
                  fallback_observation_count=0, valid_observation_count=0, roi_pixel_count=0)
    for window in _windows(grid):
        slices = _slices(window)
        owner = np.asarray(group["owner_index"][slices])
        mask = np.asarray(group["roi_mask"][slices])
        previous = np.zeros(owner.shape, dtype=np.uint32)
        previous_time = np.full(owner.shape, -1, dtype=np.int64)
        switched = np.zeros(owner.shape, dtype=bool)
        eligible = np.zeros(owner.shape, dtype=bool)
        totals["roi_pixel_count"] += int(mask.sum())
        for index in range(time_count):
            current = np.asarray(group["source_index"][(index, *slices)])
            valid = (current > 0) & mask
            pair = valid & (previous > 0)
            change = pair & (current != previous)
            gap = pair & (previous_time != index - 1)
            eligible |= pair
            switched |= change
            totals["valid_observation_count"] += int(valid.sum())
            totals["fallback_observation_count"] += int((valid & (current != owner)).sum())
            totals["comparable_pair_count"] += int(pair.sum())
            totals["source_switch_event_count"] += int(change.sum())
            totals["gap_pair_count"] += int(gap.sum())
            totals["gap_switch_count"] += int((gap & change).sum())
            previous[valid] = current[valid]
            previous_time[valid] = index
        totals["source_switch_pixel_count"] += int(switched.sum())
        totals["switch_eligible_pixel_count"] += int(eligible.sum())
    for output, numerator, denominator in (
        ("source_switch_pixel_fraction", "source_switch_pixel_count", "switch_eligible_pixel_count"),
        ("switch_eligible_fraction_roi", "switch_eligible_pixel_count", "roi_pixel_count"),
        ("fallback_fraction", "fallback_observation_count", "valid_observation_count"),
    ):
        totals[output] = totals[numerator] / totals[denominator] if totals[denominator] else None
    return totals
