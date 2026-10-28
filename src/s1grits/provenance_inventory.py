"""Read-only, explicitly scoped inventory of historical GRiTS stores.

This records observed structure and source declarations, not inferred production
history. Pixel-preserving replacement cannot be detected by metadata witnesses.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import zarr

from .provenance_contract import canonical_json, logical_output_id, sanitize_asset_url

_METADATA_FILES = ("zarr.json", ".zgroup", ".zattrs", ".zarray", ".zmetadata")
_COMMIT_FILES = ("s2grits_resume_state.json", "s2grits_metadata.json")
_STORE_ATTRS = (
    "tile_id", "collection", "crs", "transform", "grid_id", "width", "height",
    "product_type", "product_label", "product_variant", "geometry_group_id",
    "processing_signature", "processing_level", "resolution", "time_varying",
)
_ARRAY_ATTRS = (
    "_ARRAY_DIMENSIONS", "units", "nodata", "scale", "offset", "scale_factor",
    "add_offset", "source_asset", "source_band", "data_type", "spatial_resolution",
)
_UNKNOWN = (
    "Historical generation, execution run and applied numerical processing are not "
    "established by this structural inventory. Declared signatures and source "
    "scale/offset do not prove that transformations were executed."
)


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("Inventory path escapes dataset root")
    return resolved


def _assert_no_pending(root: Path, *, allowed_run_id: str | None = None) -> bytes | None:
    pending = root / "provenance" / "pending"
    if allowed_run_id is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", allowed_run_id):
        raise ValueError("invalid pending run_id")
    records = []
    if pending.exists():
        _inside(root, pending)
        records = sorted(pending.glob("*.json"))
    if allowed_run_id is None:
        if records:
            raise ValueError("Active pending data writes must be resolved before inventory")
        return None
    expected = pending / (allowed_run_id + ".json")
    if records != [expected]:
        raise ValueError("Run inventory requires exactly its own pending record")
    if expected.is_symlink() or expected.is_junction():
        raise ValueError("Pending record must not be a link")
    payload = _inside(root, expected).read_bytes()
    record = json.loads(payload)
    if not isinstance(record, dict) or record.get("run_id") != allowed_run_id:
        raise ValueError("Pending record run_id does not match requested run")
    return payload


def _relative_store(root: Path, value: str) -> tuple[str, Path]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("store_paths must be nonempty relative POSIX paths")
    parts = PurePosixPath(value)
    if parts.is_absolute() or any(p in {".", ".."} for p in value.split("/")) or ":" in value:
        raise ValueError("store_paths must stay inside the dataset root")
    path = _inside(root, root / value)
    if not path.is_dir():
        raise ValueError("Selected store must be a local directory")
    return parts.as_posix(), path


def _witness_file(root: Path, path: Path, witnesses: dict) -> None:
    if path.is_file():
        actual = _inside(root, path)
        with actual.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        witnesses[path.relative_to(root).as_posix()] = digest


def _time(value) -> str:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("Missing time cannot identify an output layer")
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return stamp.isoformat().replace("+00:00", "Z")


def _read_times(array) -> list[str]:
    values = np.asarray(array[:])
    if values.ndim != 1:
        raise ValueError("time must be a one-dimensional coordinate")
    if values.dtype.kind in "iu":
        units = str(array.attrs.get("units", ""))
        match = re.fullmatch(r"(nanoseconds|microseconds|milliseconds|seconds|minutes|hours|days) since (.+)", units)
        if match:
            unit = dict(nanoseconds="ns", microseconds="us", milliseconds="ms", seconds="s", minutes="m", hours="h", days="D")[match[1]]
            values = pd.Timestamp(match[2]) + pd.to_timedelta(values, unit=unit)
        elif units:
            raise ValueError("Unsupported time units; do not guess the coordinate encoding")
        else:
            # Legacy S1 writers store epoch nanoseconds without CF units.
            values = values.astype("datetime64[ns]")
    return [_time(value) for value in values]


def _attrs(attrs, allowed: tuple) -> dict:
    result = {}
    for key in allowed:
        if key in attrs:
            value = attrs[key]
            result[key] = str(value) if isinstance(value, float) and not math.isfinite(value) else value
    return result


def _catalog_rows(root: Path, paths: list[str], witnesses: dict) -> list[dict]:
    path = root / "catalog.parquet"
    if not path.exists():
        return []
    _witness_file(root, path, witnesses)
    table = pq.read_table(_inside(root, path))
    wanted = set(paths)
    rows = []
    for row in table.to_pylist():
        candidate = str(row.get("zarr_path") or "").replace("\\", "/")
        tile_relative = str(row["tile_id"]) + "/" + candidate if row.get("tile_id") else None
        matches = [p for p in wanted
                   if candidate == p or candidate.endswith("/" + p) or tile_relative == p]
        if len(matches) > 1:
            raise ValueError("Ambiguous Catalog store identity")
        if matches:
            row["_relative_store"] = matches[0]
            rows.append(row)
    return rows


def _groups(root_group):
    yield "", root_group
    for name, group in sorted(root_group.groups()):
        yield name, group
        for child_name, child in _groups(group):
            if child_name:
                yield name + "/" + child_name, child


def _commits(root: Path, store: Path, witnesses: dict) -> list[dict]:
    for name in _COMMIT_FILES:
        _witness_file(root, store / name, witnesses)
    selected = next((store / n for n in _COMMIT_FILES if (store / n).exists()), None)
    if selected is None:
        return []
    payload = json.loads(_inside(root, selected).read_text(encoding="utf-8"))
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("Invalid S2 commit record")
    identities = set()
    for item in items:
        if not isinstance(item, dict) or not item.get("item_id") or not item.get("datetime"):
            raise ValueError("Invalid committed scene identity")
        if item["item_id"] in identities:
            raise ValueError("Duplicate committed scene identity")
        identities.add(item["item_id"])
        _time(item["datetime"])
    return items


def _scene_at(group, index: int, stamp: str, commits: list[dict]) -> dict | None:
    identity = None
    if "item_id" in group:
        identity = str(group["item_id"][index])
    candidates = [i for i in commits if _time(i["datetime"]) == stamp]
    if identity is not None:
        candidates = [i for i in candidates if i["item_id"] == identity]
    if len(candidates) != 1:
        if commits:
            raise ValueError("Scene time/identity is absent or ambiguous in commit record")
        return None
    return candidates[0]


def _catalog_scene(rows: list[dict], group_name: str, stamp: str, item: dict | None,
                   grid_id: str | None) -> dict | None:
    candidates = [row for row in rows if str(row.get("zarr_group") or "") == group_name
                  and row.get("datetime") is not None and _time(row["datetime"]) == stamp]
    if item:
        if any(row.get("item_id") != item["item_id"] for row in candidates):
            raise ValueError("Catalog and commit disagree on scene identity")
        candidates = [row for row in candidates if row.get("item_id") == item["item_id"]]
    if len(candidates) > 1:
        raise ValueError("Ambiguous Catalog scene/group identity")
    if not candidates:
        if rows and item and any(row.get("item_id") == item["item_id"] for row in rows):
            raise ValueError("Catalog scene disagrees with group/time identity")
        return None
    row = candidates[0]
    if grid_id and row.get("grid_id") and row["grid_id"] != grid_id:
        raise ValueError("Catalog and Zarr grid identity disagree")
    return row


def _sources(item: dict | None, row: dict | None, collection: str | None,
             variables: dict, evidence_ref: str) -> list[dict]:
    assets = (item or {}).get("source_assets")
    if assets is None and row and row.get("source_assets_json"):
        assets = json.loads(row["source_assets_json"])
    if not assets:
        return []
    if not isinstance(assets, dict):
        raise ValueError("Source assets must be an object keyed by asset name")
    wanted = {v["declared_attributes"].get("source_asset", name) for name, v in variables.items()}
    result = []
    for key, asset in sorted(assets.items()):
        if key not in wanted:
            continue
        if not isinstance(asset, dict):
            raise ValueError("Malformed source asset declaration")
        href = sanitize_asset_url(asset["href"]) if asset.get("href") else None
        bands = asset.get("raster:bands") or []
        declared = bands[0] if bands else asset
        product_id = (item or {}).get("item_id") or (row or {}).get("item_id")
        identity = {"collection": collection, "product_id": product_id, "asset_key": key,
                    "href": href, "source_sha256": asset.get("sha256"),
                    "source_version": asset.get("version")}
        result.append({
            "source_id": "source-" + _hash(canonical_json(identity)),
            "collection": collection, "product_id": product_id, "asset_key": key,
            "href": href, "source_sha256": asset.get("sha256"),
            "declared_scale": declared.get("scale"), "declared_offset": declared.get("offset"),
            "evidence_status": "source_declared", "evidence_ref": evidence_ref,
        })
    return result


def collect_inventory(dataset_root: Path, *, store_paths: list[str],
                      start: str | None = None, end: str | None = None) -> dict:
    """Read explicitly selected historical stores; any active write blocks export.

    Bounds select inclusive UTC dates for scenes or calendar months for monthly
    outputs. Static layers remain included. Pixel chunks are never read.
    """
    return _collect_inventory(dataset_root, store_paths=store_paths, start=start, end=end)


def _collect_inventory_for_run(dataset_root: Path, *, store_paths: list[str],
                               run_id: str, include_catalogs: bool = True) -> dict:
    """Observe a producer's stores under its root lock and sole pending record.

    This does not establish commitment or infer generations. The producer must
    validate its durable receipt and merge previous generations before publishing.
    """
    return _collect_inventory(dataset_root, store_paths=store_paths, allowed_run_id=run_id, include_catalogs=include_catalogs)


def _collect_inventory(dataset_root: Path, *, store_paths: list[str],
                       start: str | None = None, end: str | None = None,
                       allowed_run_id: str | None = None, include_catalogs: bool = True) -> dict:
    root = Path(dataset_root).resolve(strict=True)
    if not root.is_dir() or not store_paths:
        raise ValueError("A dataset directory and explicit store_paths are required")
    pending_before = _assert_no_pending(root, allowed_run_id=allowed_run_id)
    stores = sorted(_relative_store(root, path) for path in store_paths)
    if len({path for path, _ in stores}) != len(stores):
        raise ValueError("Duplicate selected store")
    start_date = pd.Timestamp(start).date() if start is not None else None
    end_date = pd.Timestamp(end).date() if end is not None else None
    if start_date and end_date and start_date > end_date:
        raise ValueError("start must not be after end")
    witnesses = {}
    rows = _catalog_rows(root, [p for p, _ in stores], witnesses) if include_catalogs else []
    result = {"outputs": [], "sources": [], "source_links": [], "variables": [],
              "limitations": [], "witnesses": witnesses, "evidence": {}}
    source_by_id = {}
    for relative, store in stores:
        group_root = zarr.open_group(str(store), mode="r", use_consolidated=False)
        commits = _commits(root, store, witnesses)
        store_rows = [r for r in rows if r["_relative_store"] == relative]
        root_attrs = _attrs(group_root.attrs, _STORE_ATTRS)
        store_token = _hash(relative.encode())[:20]
        evidence_path = "stores/" + store_token + ".json"
        evidence_ref = "evidence/" + evidence_path
        evidence = {"store_path": relative, "declared_attributes": root_attrs,
                    "groups": [], "sources": [], "scene_mappings": []}
        result["evidence"][evidence_path] = evidence
        for name, group in _groups(group_root):
            location = store / name
            for metadata_name in _METADATA_FILES:
                _witness_file(root, location / metadata_name, witnesses)
            arrays = dict(group.arrays())
            for array_name in arrays:
                for metadata_name in _METADATA_FILES:
                    _witness_file(root, location / array_name / metadata_name, witnesses)
            for coordinate in ("x", "y"):
                if coordinate in arrays:
                    values = np.asarray(arrays[coordinate][:])
                    if values.ndim != 1 or not np.isfinite(values).all():
                        raise ValueError("Spatial coordinates must be finite one-dimensional arrays")
                    key = (location / coordinate).relative_to(root).as_posix() + "#values"
                    witnesses[key] = _hash(canonical_json(values.tolist()))
            times = _read_times(arrays["time"]) if "time" in arrays else []
            if "time" in arrays:
                witnesses[(location / "time").relative_to(root).as_posix() + "#values"] = _hash(canonical_json(times))
            if "item_id" in arrays:
                identities = [str(v) for v in arrays["item_id"][:]]
                witnesses[(location / "item_id").relative_to(root).as_posix() + "#values"] = _hash(canonical_json(identities))
                if len(identities) != len(times):
                    raise ValueError("item_id coordinate length differs from time")
            attrs = {**root_attrs, **_attrs(group.attrs, _STORE_ATTRS)}
            variables = {}
            for variable, array in sorted(arrays.items()):
                if array.ndim not in {2, 3}:
                    continue
                if array.ndim == 3 and ("time" not in arrays or array.shape[0] != len(times)):
                    raise ValueError("Image array length differs from its group time coordinate")
                dims = array.attrs.get("_ARRAY_DIMENSIONS")
                if dims and list(dims) != (["time", "y", "x"] if array.ndim == 3 else ["y", "x"]):
                    raise ValueError("Unsupported image dimension ordering")
                if "x" in arrays and array.shape[-1] != arrays["x"].shape[0]:
                    raise ValueError("Image width differs from x coordinate")
                if "y" in arrays and array.shape[-2] != arrays["y"].shape[0]:
                    raise ValueError("Image height differs from y coordinate")
                fill = array.fill_value
                if hasattr(fill, "item"):
                    fill = fill.item()
                if isinstance(fill, float) and not math.isfinite(fill):
                    fill = str(fill)
                variables[variable] = {"dtype": str(array.dtype), "shape": list(array.shape),
                    "chunks": list(array.chunks), "fill_value": fill,
                    "declared_attributes": _attrs(array.attrs, _ARRAY_ATTRS)}
                result["variables"].append({"store_path": relative, "zarr_group": name,
                    "variable": variable, **variables[variable], "stored_physical_quantity": None,
                    "applied_scale": None, "applied_offset": None,
                    "evidence_status": "artifact_observed", "evidence_ref": evidence_ref,
                    "unknown_reason": _UNKNOWN})
            evidence["groups"].append({"zarr_group": name, "declared_attributes": attrs,
                                       "times": times, "variables": variables})
            if not variables:
                continue
            dimensions = {len(v["shape"]) for v in variables.values()}
            if len(dimensions) != 1:
                raise ValueError("Mixed static and temporal variables require separate groups")
            static = dimensions == {2}
            monthly = attrs.get("product_type") == "smonthly"
            scene = not static and not monthly
            if not static and len(times) != len(set(times)) and "item_id" not in arrays:
                raise ValueError("Repeated time has no explicit item identity")
            if monthly and len({t[:7] for t in times}) != len(times):
                raise ValueError("Monthly store has multiple layers in one calendar month")
            tile = attrs.get("tile_id") or (relative.split("/")[0] if re.fullmatch(r"[0-9]{2}[A-Z]{3}", relative.split("/")[0]) else None)
            for index, stamp in ([(None, None)] if static else enumerate(times)):
                if stamp is not None:
                    date = pd.Timestamp(stamp).date()
                    if monthly:
                        if start_date and stamp[:7] < start_date.isoformat()[:7]: continue
                        if end_date and stamp[:7] > end_date.isoformat()[:7]: continue
                    else:
                        if start_date and date < start_date: continue
                        if end_date and date > end_date: continue
                item = _scene_at(group, index, stamp, commits) if scene else None
                row = _catalog_scene(store_rows, name, stamp, item, attrs.get("grid_id")) if scene else None
                item_id = (item or {}).get("item_id") or (row or {}).get("item_id")
                if scene and item_id is None:
                    raise ValueError("Scene identity requires a matching commit or Catalog record")
                if item and item.get("tile_id") not in {None, tile}:
                    raise ValueError("Committed scene tile differs from store tile")
                if item and item.get("resolution_groups") and name not in item["resolution_groups"]:
                    raise ValueError("Committed scene does not declare this resolution group")
                if row:
                    for key in ("crs", "transform", "width", "height"):
                        if attrs.get(key) is not None and row.get(key) is not None and attrs[key] != row[key]:
                            raise ValueError("Catalog and Zarr grid metadata disagree")
                if scene:
                    evidence["scene_mappings"].append({"zarr_group": name, "time_index": index,
                        "time_value_utc": stamp, "source_item_id": item_id,
                        "binding_source": "commit_and_catalog" if item and row else "commit" if item else "catalog"})
                if monthly:
                    period = pd.Period(stamp[:7], freq="M")
                    begin, finish = period.start_time.date().isoformat(), period.end_time.date().isoformat()
                else:
                    begin = finish = stamp
                identity = {"store_path": relative, "zarr_group": name,
                    "variable_scope": sorted(variables), "record_kind": "static" if static else "monthly" if monthly else "scene",
                    "period": stamp[:7] if monthly else item_id,
                    "grid_id": attrs.get("grid_id"), "geometry_group_id": attrs.get("geometry_group_id")}
                output = {"output_id": logical_output_id(identity), "generation_id": None,
                    "store_path": relative, "zarr_group": name, "variable_scope": sorted(variables),
                    "tile_id": tile, "geometry_group_id": attrs.get("geometry_group_id"),
                    "grid_id": attrs.get("grid_id"), "product_type": attrs.get("product_type") or ("static" if static else "scenes"),
                    "record_kind": identity["record_kind"], "time_index": index, "time_value_utc": stamp,
                    "period_start": begin, "period_end": finish, "source_item_id": item_id,
                    "run_id": None, "processing_id": None, "evidence_status": "artifact_observed",
                    "evidence_ref": evidence_ref, "unknown_reason": _UNKNOWN}
                result["outputs"].append(output)
                collection = (row or {}).get("source_collection") or attrs.get("collection")
                for source in _sources(item, row, collection, variables, evidence_ref):
                    old = source_by_id.get(source["source_id"])
                    if old and {k: v for k, v in old.items() if k != "evidence_ref"} != {k: v for k, v in source.items() if k != "evidence_ref"}:
                        raise ValueError("Conflicting declarations for the same source asset")
                    source_by_id.setdefault(source["source_id"], source)
                    result["source_links"].append({"output_id": output["output_id"], "generation_id": None,
                        "source_id": source["source_id"], "role": "selected", "evidence_ref": evidence_ref})
                    evidence["sources"].append(source)
        result["limitations"].append({"store_path": relative, "reason": _UNKNOWN,
            "source_usage": "Source declarations identify selected candidates; actual operator participation is unproven.",
            "revision_scope": "Metadata and coordinate witnesses cannot detect pixel-only replacement; no full pixel digest was computed."})
    if len({o["output_id"] for o in result["outputs"]}) != len(result["outputs"]):
        raise ValueError("Duplicate logical output identity")
    result["sources"] = sorted(source_by_id.values(), key=lambda source: source["source_id"])
    result["outputs"].sort(key=lambda output: (output["store_path"], output["zarr_group"], output["time_index"] if output["time_index"] is not None else -1))
    pending_after = _assert_no_pending(root, allowed_run_id=allowed_run_id)
    if pending_before != pending_after:
        raise ValueError("Pending record changed during run inventory")
    return result
