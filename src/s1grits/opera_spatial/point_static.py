"""Attach frozen burst static samples without rewriting dynamic point archives."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import uuid

from affine import Affine
import numpy as np
import pandas as pd
from pyproj import CRS, Transformer
from shapely.geometry import Point, shape

from s1grits.atomic_write import atomic_write_json, atomic_write_parquet
from s1grits.file_lock import output_lock
from .inventory import digest
from .points import _acquire, _read_tables
from .state import check_budget, group_key, physical_sample, prepare_state, requested_static_layers


def plan_static(config, features, output, static_products=()):
    _, _, bindings, jobs, layers = _prepare(config, features, output, static_products)
    return _budget(config, bindings, jobs, layers)


def _budget(config, bindings, jobs, layers):
    assets = {job[4]["asset_id"]: job[4] for job in jobs if job[4]}
    stored = len(jobs) * 4096
    temporary = sum(a["expected_bytes"] for a in assets.values()) if config.get("read_mode") == "burst_fetch" else 0
    required = stored * 2 + temporary
    return {"status": "planned", "mode": "point_static", "layers": layers,
            "source_count": len(bindings), "sample_jobs": len(jobs),
            "unique_assets": len(assets),
            "full_asset_bytes_reference": sum(a["expected_bytes"] for a in assets.values()),
            "missing_asset_jobs": sum(job[4] is None for job in jobs),
            "required_bytes_upper": required, "confirmation_required": required > 50_000_000_000}


def run_static(config, features, output, static_products=(), repair=False):
    output = Path(output)
    with output_lock(output):
        state, previous, bindings, jobs, layers = _prepare(
            config, features, output, static_products)
        budget = _budget(config, bindings, jobs, layers)
        check_budget(budget, output, confirmed=config.get("confirm_large_job", False))
        if previous and previous["layers"] != layers and not repair:
            raise ValueError("Changing static layers requires repair --static-only")
        prior = (pd.read_parquet(output / previous["samples_path"])
                 if previous else pd.DataFrame())
        existing = {(r.point_id, r.sample_id): r._asdict()
                    for r in prior.itertuples(index=False)}

        def acquire(job):
            unit, product, source, role, asset = job
            if asset is None:
                return {"status": "missing_asset", "value": None,
                        "sampling_signature": digest({"point": unit, "role": role}),
                        "run_http_requests": 0, "run_downloaded_bytes": 0}
            raw = _acquire(job, output, config, repair)
            try:
                result = physical_sample(raw, role, product)
                version = re.search(r"_v(\d+(?:\.\d+)*)_", asset.get("filename", ""))
                filename_version = version.group(1) if version else None
                tags = raw.get("tags", {}).get("dataset", raw.get("tags", {}))
                cog_version = tags.get("PRODUCT_VERSION")
                if filename_version and cog_version and filename_version != str(cog_version).lstrip("v"):
                    raise ValueError("STATIC filename and COG product versions differ")
                result.update(product_version_filename=filename_version,
                              product_version_cog=cog_version, software_version_cog=tags.get("SOFTWARE_VERSION"))
            except ValueError as error:
                result = dict(raw, status="read_failed", value=None, error=str(error))
            return dict(result, raw_value=raw.get("value"))

        with ThreadPoolExecutor(max_workers=int(config.get("workers", 4))) as pool:
            samples = list(pool.map(acquire, jobs))
        retained = []
        for job, sample in zip(jobs, samples):
            unit, product, source, role, asset = job
            asset_id = asset["asset_id"] if asset else product["product_id"] + ":missing:" + role
            sample_id = digest({"asset_id": asset_id, "sampling": sample["sampling_signature"]})
            row = dict(point_id=unit["point_id"], burst_id=source["burst_id"], role=role,
                       static_product_id=product["product_id"], asset_id=asset_id,
                       sample_id=sample_id, sampling_signature=sample["sampling_signature"],
                       value=sample.get("value") if sample["status"] == "valid" else None,
                       raw_value=sample.get("raw_value"), status=sample["status"],
                       error=sample.get("error"), units=sample.get("units"),
                       data_domain=sample.get("data_domain"), scope="static_nominal_burst_reference")
            for field in ("crs", "transform", "width", "height", "row", "col", "dtype",
                          "nodata", "nodata_is_nan", "scale", "offset", "scale_offset_applied",
                          "centre_x", "centre_y", "read_mode", "quantity", "conversion_from", "conversion_to",
                          "product_version_filename", "product_version_cog", "software_version_cog"):
                row[field] = sample.get(field)
            row["tags_json"] = json.dumps(sample.get("tags", {}), sort_keys=True)
            key = (unit["point_id"], sample_id)
            if sample["status"] == "read_failed" and existing.get(key, {}).get("status") == "valid":
                retained.append({"sample_id": sample_id, "error": sample.get("error")})
            else:
                existing[key] = row
        frame = pd.DataFrame(existing.values())
        failures = sum(s["status"] == "read_failed" for s in samples)
        gaps = sum(s["status"] not in {"valid", "read_failed"} for s in samples)
        result = {"status": "partial" if failures else "complete_with_gaps" if gaps else "complete",
                  "mode": "point_static", "layers": layers, "sample_jobs": len(jobs),
                  "sample_rows": len(frame), "failed_samples": failures, "gap_samples": gaps,
                  "http_requests": sum(s.get("run_http_requests", 0) for s in samples),
                  "downloaded_bytes": sum(s.get("run_downloaded_bytes", 0) for s in samples),
                  "dynamic_http_requests": 0, "retained_after_failed_repair": retained}
        folder = output / "static"
        folder.mkdir(exist_ok=True)
        revision = uuid.uuid4().hex
        sample_path = folder / ("samples_" + revision + ".parquet")
        atomic_write_parquet(frame, sample_path)
        manifest = {"schema_version": 1, "layers": layers, "bindings": bindings,
                    "dynamic_contract_id": state["contract_id"],
                    "samples_path": sample_path.relative_to(output).as_posix(),
                    "created_utc": datetime.now(timezone.utc).isoformat(), "last_result": result}
        atomic_write_json(manifest, folder / "manifest.json")
        return result


def read_point_static_context(output):
    """Join against current selected identities, so dynamic repair cannot leave stale joins."""
    output = Path(output)
    with output_lock(output):
        state = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if state.get("pending_points"):
            raise ValueError("Point update is unpublished; complete its replay first")
        meta = json.loads((output / "static/manifest.json").read_text(encoding="utf-8"))
        if meta["dynamic_contract_id"] != state["contract_id"]:
            raise ValueError("Static and dynamic contracts differ")
        selected = _read_tables(output, "selected")
        samples = pd.read_parquet(output / meta["samples_path"])
    lookup = {(r.point_id, r.static_product_id, r.role): r._asdict()
              for r in samples.itertuples(index=False)}
    units = {u["point_id"]: u for u in state["units"]}
    rows = []
    for record in selected.to_dict("records"):
        for prefix, index_field in (("owner", "owner_index"), ("selected", "source_index")):
            key = record["geometry_group"] + ":" + str(int(record[index_field]))
            binding = meta["bindings"].get(key)
            product = binding.get("product") if binding else None
            pid = product["product_id"] if product else None
            record[prefix + "_static_product_id"] = pid
            for role in meta["layers"]:
                sample = lookup.get((record["point_id"], pid, role)) if pid else None
                status = sample["status"] if sample else "unavailable" if record[index_field] else "no_source"
                grid = state.get("point_grids", {}).get(record["geometry_group"] + ":" + binding["burst_id"]) if binding else None
                matched = _grid_status(sample, grid, units[record["point_id"]])
                stem = prefix + "_" + role
                record[stem] = sample["value"] if sample and status == "valid" and matched == "matched" else None
                record[stem + "_status"] = status
                record[stem + "_grid_status"] = matched
                record[stem + "_sample_id"] = sample["sample_id"] if sample else None
        rows.append(record)
    return pd.DataFrame(rows)


def _prepare(config, features, output, static_products):
    output = Path(output)
    if not (output / "manifest.json").exists():
        raise ValueError("Static attachment requires an existing point archive")
    layers = requested_static_layers(config)
    if config["spatial"]["mode"] != "points":
        raise ValueError("Static attachment supports points only")
    state = prepare_state(config, features, [], output)
    if state.get("pending_points"):
        raise ValueError("Point update is unpublished; complete its replay first")
    path = output / "static/manifest.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    bindings = dict(previous["bindings"]) if previous else {}
    if previous and previous["dynamic_contract_id"] != state["contract_id"]:
        raise ValueError("Static and dynamic contracts differ")
    products = {p["product_id"]: p for p in static_products}
    pins = config.get("static", {}).get("reference_products", {})
    selection = config.get("source", {})
    lower, upper = pd.Timestamp(config.get("start", "1900-01-01")), pd.Timestamp(config.get("end", "2200-01-01"))
    lower = lower.tz_localize("UTC") if lower.tzinfo is None else lower.tz_convert("UTC")
    upper = upper.tz_localize("UTC") if upper.tzinfo is None else upper.tz_convert("UTC")
    active = [p for p in state["products"] if lower <= pd.Timestamp(p["acquisition_utc"]) < upper
              and (not selection.get("relative_orbits") or p["relative_orbit"] in selection["relative_orbits"])]
    active_groups = {group_key(p) for p in active}
    wanted_bursts = {str(b).upper().replace("_", "-") for b in selection.get("burst_ids", [])}
    jobs, seen = [], set()
    for group in sorted(active_groups):
        for source in state["groups"][group]["sources"]:
            burst = source["burst_id"]
            if wanted_bursts and burst not in wanted_bursts:
                continue
            key = group + ":" + str(source["source_index"])
            old = bindings.get(key, {}).get("product")
            if old and old.get("missing_reference"):
                old = None
            pid = old["product_id"] if old else source.get("static_product_id")
            if pins.get(burst) and pid and pins[burst] != pid:
                raise ValueError("Cannot replace a frozen STATIC product: " + burst)
            reference = old or products.get(pid or pins.get(burst))
            if reference is None and not pid and not pins.get(burst):
                candidates = [p for p in static_products if p["burst_id"] == burst]
                if len(candidates) > 1:
                    raise ValueError("Multiple STATIC products; set static.reference_products for " + burst)
                reference = candidates[0] if candidates else None
            if pins.get(burst) and reference is None:
                raise ValueError("Pinned STATIC product absent from inventory: " + pins[burst])
            if reference is not None and reference["burst_id"] != burst:
                raise ValueError("STATIC product burst differs from source")
            if reference is None:
                reference = {"product_id": pid or "missing:" + burst, "burst_id": burst,
                             "is_static": True, "missing_reference": not bool(pid), "assets": {}}
                if source.get("static_asset"):
                    reference["assets"]["lia"] = source["static_asset"]
            # Extend the same frozen product, never fill roles from a different revision.
            reference = dict(reference, assets=dict(reference["assets"]))
            incoming = products.get(reference["product_id"], {})
            for role, asset in incoming.get("assets", {}).items():
                reference["assets"].setdefault(role, asset)
            bindings[key] = {"burst_id": burst, "source_index": source["source_index"],
                             "geometry_group": group, "product": reference,
                             "binding_scope": "same_burst_nominal_static_reference"}
            for unit in state["units"]:
                owner = state.get("point_owners", {}).get(group, {}).get(unit["point_id"])
                if owner != source["source_index"] and not shape(source["geometry"]).covers(Point(unit["lon"], unit["lat"])):
                    continue
                for role in layers:
                    asset = reference["assets"].get(role)
                    identity = (unit["point_id"], reference["product_id"], role)
                    if identity not in seen:
                        seen.add(identity)
                        jobs.append((unit, reference, source, role, asset))
    return state, previous, bindings, jobs, layers


def _grid_status(sample, grid, unit):
    if sample is None or grid is None or sample.get("crs") is None or sample.get("transform") is None:
        return "unknown"
    crs = CRS.from_user_input(grid["crs"])
    if crs != CRS.from_user_input(sample["crs"]):
        return "grid_mismatch"
    source = Affine(*grid["transform"])
    other = Affine(*sample["transform"])
    tolerance = 1e-10 if crs.is_geographic else 1e-6
    if not np.allclose([source.a, source.b, source.d, source.e],
                       [other.a, other.b, other.d, other.e], rtol=0, atol=tolerance):
        return "grid_mismatch"
    x, y = Transformer.from_crs(4326, crs, always_xy=True).transform(unit["lon"], unit["lat"])
    col, row = (~source) * (x, y)
    centre = source * (math.floor(col) + .5, math.floor(row) + .5)
    return "matched" if np.allclose(centre, [sample["centre_x"], sample["centre_y"]],
                                    rtol=0, atol=tolerance) else "grid_mismatch"
