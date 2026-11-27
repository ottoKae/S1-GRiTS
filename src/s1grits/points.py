"""Extract original Zarr values into resumable, per-source point tables."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from affine import Affine
from pyproj import Transformer

from s1grits.atomic_write import atomic_path, atomic_write_parquet
from s1grits.spatial_sources import read_source_window


_OBS_COLUMNS = [
    "point_id", "source_id", "time_index", "time_utc", "period_start", "period_end",
    "variable", "value", "status", "row", "col", "sample_x", "sample_y",
]
_STATIC_COLUMNS = ["point_id", "source_id", "variable", "value", "status", "row", "col"]
_SOURCE_COLUMNS = [
    "source_id", "source_kind", "store_path", "product_id", "time_index", "product_type",
    "variable", "burst_ids", "burst_provenance_scope", "opera_ids", "opera_provenance_scope",
    "direction", "relative_orbit", "track_members", "track_mode", "provenance_level",
    "platform", "absolute_orbit", "acquisition_id", "geometry_group_id", "crs", "units",
    "data_domain", "source_dtype", "declared_scale", "declared_offset", "scale_offset_applied",
    "product_version", "production_time", "processing_signature", "read_mode",
    "source_status", "http_status", "error_message",
    "output_id", "generation_id", "aggregation_method", "aggregation_domain", "n_obs_definition", "metadata_evidence_json",
]


def extract_points(config, points_gdf, sources, output_dir: Path, manifest, checkpoint) -> dict:
    """Sample normalized points from the frozen local source selection.

    ``checkpoint`` persists the shared manifest and applies the caller's disk
    check. A completed unit is one store/time-index/variable, never one date.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    observations_dir = output_dir / "observations"
    observations_dir.mkdir(exist_ok=True)
    checkpoint()
    with atomic_path(output_dir / "points.parquet") as path:
        points_gdf.to_parquet(path, index=False)

    completed = set(manifest.setdefault("completed", []))
    point_ids = points_gdf["point_id"].astype(str).tolist()
    counts = {pid: {"observation_count": 0, "valid_count": 0, "failed_count": 0,
                    "candidate_sources": set()} for pid in point_ids}
    source_records, static_parts, dynamic_parts = [], [], []
    seen_units = set()
    failed_sources = set()
    missing_static = []

    for dynamic in sources:
        selected = [(dynamic, False)]
        if config.get("include_static", False):
            if dynamic.get("static_source") is None:
                missing_static.append(dynamic["source_unit_id"])
            else:
                selected.append((dynamic["static_source"], True))
        for source, is_static in selected:
            locations = _point_locations(points_gdf, source)
            for index, inside in enumerate(locations["inside"]):
                if inside:
                    counts[point_ids[index]]["candidate_sources"].add(source["source_unit_id"])
                    if not is_static and config.get("include_static", False) and source.get("static_source") is None:
                        counts[point_ids[index]]["failed_count"] += 1
            variables = sorted(source["variables"]) if is_static else (config.get("variables") or sorted(source["variables"]))
            times = [{}] if is_static else source["times"]
            for time in times:
                for variable in variables:
                    time_index = None if is_static else time["time_index"]
                    source_id = _source_id(source, time_index, variable)
                    if source_id in seen_units:
                        continue
                    seen_units.add(source_id)
                    unit_key = f"points:{source_id}"
                    directory = output_dir / ".state" / "static_parts" if is_static else observations_dir
                    directory.mkdir(parents=True, exist_ok=True)
                    part = directory / f"part-{source_id}.parquet"
                    if unit_key in completed:
                        frame = pd.read_parquet(part)
                        errors = []
                    else:
                        checkpoint()
                        frame, errors = _sample_source(source, variable, time, source_id, point_ids, locations)
                        atomic_write_parquet(frame, part)
                        if not errors:
                            manifest["completed"].append(unit_key)
                            completed.add(unit_key)
                        checkpoint()
                    source_records.append(_source_record(source, variable, time, source_id, errors))
                    if errors:
                        failed_sources.add(source_id)
                    if is_static:
                        static_parts.append(part)
                        for pid in frame.loc[frame.status == "read_failed", "point_id"]:
                            counts[pid]["failed_count"] += 1
                    else:
                        dynamic_parts.append(part)
                        for record in frame.itertuples(index=False):
                            tally = counts[record.point_id]
                            tally["observation_count"] += 1
                            tally["valid_count"] += int(record.status == "valid")
                            tally["failed_count"] += int(record.status == "read_failed")

    checkpoint()
    source_frame = pd.DataFrame(source_records, columns=_SOURCE_COLUMNS)
    atomic_write_parquet(source_frame, output_dir / "sources.parquet")
    if config.get("include_static", False):
        with atomic_path(output_dir / "static.parquet") as path:
            if static_parts:
                with pq.ParquetWriter(path, pq.read_schema(static_parts[0]).empty_table().select(_STATIC_COLUMNS).schema) as writer:
                    for part in static_parts:
                        writer.write_table(pq.read_table(part, columns=_STATIC_COLUMNS))
            else:
                _typed_observations([])[_STATIC_COLUMNS].to_parquet(path, index=False)

    summaries = []
    for pid, tally in counts.items():
        candidates = len(tally["candidate_sources"])
        if tally["failed_count"]:
            status = "partial" if tally["valid_count"] else "failed"
        elif candidates == 0:
            status = "no_source"
        elif tally["valid_count"] == 0:
            status = "no_valid_observations"
        else:
            status = "complete"
        summaries.append({"point_id": pid, "candidate_sources": candidates,
                          "observation_count": tally["observation_count"],
                          "valid_count": tally["valid_count"], "status": status})
    summary = pd.DataFrame(summaries, columns=["point_id", "candidate_sources", "observation_count", "valid_count", "status"])
    atomic_write_parquet(summary, output_dir / "point_summary.parquet")
    if config.get("output", {}).get("export_csv", False):
        with atomic_path(output_dir / "export.csv") as path:
            with path.open("w", newline="", encoding="utf-8") as stream:
                if dynamic_parts:
                    for index, part in enumerate(dynamic_parts):
                        pd.read_parquet(part).to_csv(stream, index=False, header=index == 0)
                else:
                    _typed_observations([]).to_csv(stream, index=False)
    valid_count = sum(tally["valid_count"] for tally in counts.values())
    has_failures = bool(failed_sources or missing_static)
    status = ("partial" if valid_count else "failed") if has_failures else ("complete" if sources else "no_source")
    result = {"status": status, "point_count": len(point_ids),
              "observation_count": sum(tally["observation_count"] for tally in counts.values()),
              "valid_count": valid_count, "failed_source_count": len(failed_sources),
              "missing_static_sources": sorted(set(missing_static)),
              "source_count": len(source_records), "output_dir": str(output_dir)}
    manifest["point_result"] = result
    checkpoint()
    return result


def _source_id(source, time_index, variable):
    key = [str(Path(source["path"]).resolve()), time_index, variable]
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]


def _point_locations(points, source):
    transformer = Transformer.from_crs(points.crs, source["crs"], always_xy=True)
    x, y = transformer.transform(points.geometry.x.to_numpy(), points.geometry.y.to_numpy())
    transform = Affine(*source["transform"])
    fractional_col, fractional_row = ~transform * (np.asarray(x), np.asarray(y))
    cols = np.floor(fractional_col).astype(np.int64)
    rows = np.floor(fractional_row).astype(np.int64)
    sample_x, sample_y = transform * (cols + 0.5, rows + 0.5)
    inside = (rows >= 0) & (rows < source["height"]) & (cols >= 0) & (cols < source["width"])
    return {"rows": rows, "cols": cols, "sample_x": sample_x, "sample_y": sample_y, "inside": inside}


def _sample_source(source, variable, time, source_id, point_ids, locations):
    metadata = source["variables"].get(variable)
    rows = []
    for index, pid in enumerate(point_ids):
        inside = bool(locations["inside"][index])
        rows.append({"point_id": pid, "source_id": source_id,
                     "time_index": time.get("time_index"), "time_utc": time.get("time_utc"),
                     "period_start": time.get("period_start"), "period_end": time.get("period_end"),
                     "variable": variable, "value": None, "status": "nodata" if inside else "outside",
                     "row": int(locations["rows"][index]), "col": int(locations["cols"][index]),
                     "sample_x": float(locations["sample_x"][index]) if inside else None,
                     "sample_y": float(locations["sample_y"][index]) if inside else None})
    inside_indices = np.flatnonzero(locations["inside"])
    if metadata is None:
        for index in inside_indices:
            rows[index]["status"] = "read_failed"
        return _typed_observations(rows), [f"Variable missing from source: {variable}"]

    chunk_y = metadata["chunks"][metadata["dims"].index("y")]
    chunk_x = metadata["chunks"][metadata["dims"].index("x")]
    grouped = {}
    for index in inside_indices:
        key = (int(locations["rows"][index]) // chunk_y, int(locations["cols"][index]) // chunk_x)
        grouped.setdefault(key, []).append(index)
    errors = []
    for (block_row, block_col), indices in grouped.items():
        rowoff, coloff = block_row * chunk_y, block_col * chunk_x
        height = min(chunk_y, source["height"] - rowoff)
        width = min(chunk_x, source["width"] - coloff)
        try:
            array = read_source_window(source, variable, time.get("time_index"), (rowoff, coloff, height, width))
            for index in indices:
                value = array[locations["rows"][index] - rowoff, locations["cols"][index] - coloff]
                if np.isfinite(value):
                    rows[index]["value"] = float(value)
                    rows[index]["status"] = "valid"
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            for index in indices:
                rows[index]["status"] = "read_failed"
    return _typed_observations(rows), errors


def _typed_observations(records):
    frame = pd.DataFrame(records, columns=_OBS_COLUMNS)
    for name in ("point_id", "source_id", "variable", "status"):
        frame[name] = frame[name].astype("string")
    for name in ("time_index", "row", "col"):
        frame[name] = frame[name].astype("Int64")
    for name in ("value", "sample_x", "sample_y"):
        frame[name] = frame[name].astype("Float64")
    for name in ("time_utc", "period_start", "period_end"):
        frame[name] = pd.to_datetime(frame[name], utc=True)
    return frame


def _source_record(source, variable, time, source_id, errors):
    metadata = {**source["variables"].get(variable, {}),
                **time.get("variable_semantics", {}).get(variable, {})}
    record = {name: None for name in _SOURCE_COLUMNS}
    record.update(source_id=source_id, source_kind="local_zarr", store_path=str(source["path"]),
                  time_index=time.get("time_index"), product_type=source["product_type"], variable=variable,
                  direction=source.get("direction"), relative_orbit=source.get("track"),
                  track_members=source.get("track_members"),
                  geometry_group_id=source.get("geometry_group_id"), crs=str(source["crs"]),
                  units=metadata.get("units", "unknown"), data_domain=metadata.get("data_domain", "unknown"),
                  source_dtype=metadata.get("dtype"),
                  declared_scale=metadata.get("declared_scale"), declared_offset=metadata.get("declared_offset"),
                  scale_offset_applied=False, processing_signature=source.get("processing_signature"),
                  read_mode="local_zarr", source_status="read_failed" if errors else "complete",
                  error_message="; ".join(dict.fromkeys(errors)) if errors else None)
    members = source.get("track_members") or []
    record.update(track_mode="mixed_tracks" if len(members) > 1 else "single_track" if members else "unknown",
                  provenance_level="source_product")
    for field, scope_field in (("burst_ids", "burst_provenance_scope"), ("opera_ids", "opera_provenance_scope")):
        declaration = time if field in time else source
        record[field] = declaration.get(field)
        record[scope_field] = ("time_layer_candidates" if declaration is time else "store_candidates") if record[field] is not None else "unknown"
    record.update(output_id=time.get("output_id"), generation_id=time.get("generation_id"),
                  aggregation_method=metadata.get("aggregation_method", source.get("aggregation_method")),
                  aggregation_domain=metadata.get("aggregation_domain", source.get("aggregation_domain")),
                  n_obs_definition=metadata.get("n_obs_definition") if variable == "n_obs" else None,
                  metadata_evidence_json=json.dumps(metadata.get("metadata_evidence", {}), sort_keys=True))
    for name in ("platform", "absolute_orbit", "acquisition_id"):
        record[name] = time.get(name)
    for name in ("product_id", "product_version", "production_time"):
        record[name] = time.get(name, source.get(name))
    return record
