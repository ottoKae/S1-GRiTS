"""Plan and execute independent spatial products without changing MGRS production."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import yaml

from .atomic_write import atomic_write_json
from .file_lock import output_lock
from .preflight import PreflightError, check_disk_space, resolve_disk_policy
from .spatial_sources import close_sources, discover_local_sources


LARGE_JOB_BYTES = 50_000_000_000


def plan_spatial(config):
    """Return a metadata-only, JSON-serializable local product plan."""
    try:
        return _prepare(config)[3]
    finally:
        close_sources()


def run_spatial(config, *, dry_run=False, confirm_large_job=False):
    """Execute the frozen local plan, replacing incomplete work units on resume."""
    try:
        config, features, sources, plan, region_plans, provenance = _prepare(config)
        if dry_run:
            return plan
        output = Path(config["output"]["base_dir"])
        existing_manifest = output / "manifest.json"
        previous = json.loads(existing_manifest.read_text(encoding="utf-8")) if existing_manifest.exists() else None
        _check_resume(previous, plan, config)
        accepted = bool(confirm_large_job or (previous or {}).get("large_job_confirmed"))
        if plan["budget"]["confirmation_required"] and not accepted:
            raise ValueError("Job exceeds 50 GB. Review --dry-run and pass --confirm-large-job.")
        _check_disk(config, output, _remaining_bytes(plan, region_plans, previous or {"completed": []}))
        if output.exists() and previous is None and any(output.iterdir()):
            raise ValueError("Use an empty output directory or resume its matching manifest")
        with output_lock(output):
            manifest = json.loads(existing_manifest.read_text(encoding="utf-8")) if existing_manifest.exists() else None
            _check_resume(manifest, plan, config)
            if manifest is None:
                manifest = {"schema_version": 1, "plan_id": plan["plan_id"], "config": config,
                            "input_snapshot": plan["input_snapshot"], "sources": sources,
                            "budget": plan["budget"], "completed": [], "status": "running",
                            "large_job_confirmed": accepted,
                            "source_freeze_requirement": "All sources must remain unchanged throughout the job; structural provenance revisions do not checksum image chunks"}
            manifest["large_job_confirmed"] = accepted
            manifest["status"] = "running"
            atomic_write_json(manifest, existing_manifest)
            atomic_write_json(plan, output / "plan.json")

            def checkpoint():
                if provenance is not None:
                    provenance.assert_current(stores=[s["path"] for s in sources])
                remaining = _remaining_bytes(plan, region_plans, manifest)
                _check_disk(config, output, remaining)
                atomic_write_json(manifest, existing_manifest)

            try:
                if config["spatial"]["mode"] == "points":
                    from .points import extract_points
                    result = extract_points(config, features, sources, output, manifest, checkpoint)
                else:
                    from .regions import export_regions
                    result = export_regions(config, features, sources, output, manifest, checkpoint,
                                            planned=region_plans)
                checkpoint()
                manifest["status"] = result["status"]
                manifest["result"] = result
                atomic_write_json(manifest, existing_manifest)
                return result
            except BaseException as exc:
                manifest["status"] = "partial" if manifest["completed"] else "failed"
                manifest["last_error"] = str(exc)
                atomic_write_json(manifest, existing_manifest)
                raise
    finally:
        close_sources()


def _prepare(config):
    from .spatial_inputs import read_spatial_input
    from .spatial_provenance import enrich_sources
    config = _normalize_config(config)
    features = read_spatial_input(config)
    geometry = features.geometry.union_all()
    sources = discover_local_sources(config, geometry=geometry)
    provenance = enrich_sources(config, sources)
    output = Path(config["output"]["base_dir"])
    readonly_sources = sources + [s["static_source"] for s in sources if s.get("static_source")]
    for source in readonly_sources:
        path = Path(source["path"])
        if output == path or output.is_relative_to(path) or path.is_relative_to(output):
            raise ValueError("Spatial output must be separate from every read-only source store")
    source_root = Path(config["source"]["root"])
    if output == source_root or output.is_relative_to(source_root):
        raise ValueError("Spatial output must be outside the source production root")
    for key in ("dataset_root", "metadata_root"):
        if config["source"].get(key):
            readonly_root = Path(config["source"][key])
            if output == readonly_root or output.is_relative_to(readonly_root) or readonly_root.is_relative_to(output):
                raise ValueError(f"Spatial output must be separate from source.{key}")
    regions = None
    rows = []
    if config["spatial"]["mode"] == "points":
        variables = config.get("variables")
        count = sum(len(source["times"]) * len(variables or source["variables"]) for source in sources) * len(features)
        static_count = sum(len((s.get("static_source") or {}).get("variables", {})) for s in sources) * len(features) if config.get("include_static") else 0
        # Point-table bytes are a planning estimate, not a compression guarantee.
        logical = (count + static_count) * 200
        temporary = len(features) * 200 * (2 if config.get("include_static") else 1)
        for row in features.itertuples():
            rows.append({"point_id": row.point_id, "lon": row.geometry.x, "lat": row.geometry.y,
                         "output_path": "observations/", "summary_path": "point_summary.parquet"})
    else:
        from .regions import plan_region_outputs
        regions = plan_region_outputs(config, features, sources)
        count = None
        logical = sum(item["logical_bytes"] for item in regions)
        temporary = max((min(p["grid"]["width"], 512) * min(p["grid"]["height"], 512) * 20
                         for p in regions), default=0)
        by_id = features.set_index("region_id")
        for item in regions:
            row = by_id.loc[item["region_id"]]
            rows.append({"region_id": item["region_id"], "geometry": row.geometry.__geo_interface__,
                         "grid": item["grid"], "time_count": len(item["times"]),
                         "time_slots": [{k: v for k, v in t.items() if k != "members"} for t in item["times"]],
                         "direction": item.get("direction"), "track": item.get("track"),
                         "status": item["status"], "output_path": item.get("zarr_path"),
                         "logical_bytes": item["logical_bytes"]})
    snapshot = _input_snapshot(config["input"]["path"])
    binding = {"config": config, "input_snapshot": snapshot, "sources": sources,
               "units": rows}
    identifier = hashlib.sha256(json.dumps(binding, sort_keys=True, default=_json_default).encode()).hexdigest()
    metadata_bytes = len(json.dumps(binding, default=_json_default).encode()) * 2 + len(features.to_json().encode())
    budget = capacity_budget(logical + metadata_bytes, temporary + metadata_bytes)
    budget.update({"observation_rows": count, "network_bytes": 0,
                   "estimate_basis": "uncompressed arrays; point rows estimated at 200 bytes; compression and filesystem overhead unknown"})
    plan = {"schema_version": 1, "plan_id": identifier, "spatial_mode": config["spatial"]["mode"],
            "source_kind": "local_zarr", "unit_count": len(features), "source_count": len(sources),
            "output_dir": str(output), "input_snapshot": snapshot, "units": rows, "budget": budget,
            "unversioned_sources": [s["path"] for s in sources if not s.get("data_revision")],
            "status": "planned" if sources else "no_source"}
    return config, features, sources, json.loads(json.dumps(plan, default=_json_default)), regions, provenance


def capacity_budget(logical_bytes, temporary_bytes=0):
    logical_bytes, temporary_bytes = int(logical_bytes), int(temporary_bytes)
    peak = logical_bytes + temporary_bytes
    return {"logical_bytes": logical_bytes, "temporary_bytes": temporary_bytes,
            "estimated_peak_bytes": peak, "logical_gb": logical_bytes / 1e9,
            "logical_gib": logical_bytes / 2**30, "estimated_peak_gb": peak / 1e9,
            "estimated_peak_gib": peak / 2**30, "confirmation_threshold_bytes": LARGE_JOB_BYTES,
            "confirmation_required": logical_bytes > LARGE_JOB_BYTES or peak > LARGE_JOB_BYTES}


def _normalize_config(config):
    if isinstance(config, (str, Path)):
        path = Path(config).resolve()
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        base = path.parent
    else:
        config = copy.deepcopy(config)
        base = Path.cwd()
    if not isinstance(config, dict):
        raise ValueError("Spatial config must be a YAML mapping")
    for section, key in (("input", "path"), ("source", "root"), ("output", "base_dir")):
        value = config.get(section, {}).get(key)
        if not value:
            raise ValueError(f"{section}.{key} is required")
        config[section][key] = str((base / value).resolve())
    for key in ("dataset_root", "metadata_root"):
        if config["source"].get(key):
            config["source"][key] = str((base / config["source"][key]).resolve())
    if config.get("spatial", {}).get("mode") == "mgrs":
        raise ValueError("MGRS production is unchanged; use its existing command")
    if config["source"].get("kind", "local_zarr") != "local_zarr":
        raise ValueError("P1/P2 support local Zarr; ASF fetch is a later phase")
    policy = config["output"].get("existing_store", "resume")
    if policy not in ("resume", "fail"):
        raise ValueError("Spatial output.existing_store must be resume or fail")
    for key, minimum, maximum in (("months", 1, 12), ("years", 1, 9999)):
        values = config.get("time", {}).get(key, [])
        if not isinstance(values, list) or any(type(v) is not int or not minimum <= v <= maximum for v in values):
            raise ValueError(f"time.{key} must be a list of integers in {minimum}..{maximum}")
    variables = config.get("variables")
    if variables is not None and (not isinstance(variables, list) or not variables or not all(isinstance(v, str) for v in variables)):
        raise ValueError("variables must be a nonempty list of names")
    return config


def _input_snapshot(path):
    path = Path(path)
    paths = [path]
    if path.suffix.lower() == ".shp":
        paths = sorted(p for p in path.parent.glob(path.stem + ".*")
                       if p.suffix.lower() in {".shp", ".shx", ".dbf", ".prj", ".cpg"})
    return [{"path": str(p), "sha256": _hash_file(p)} for p in paths]


def _hash_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _check_resume(manifest, plan, config):
    if manifest is None:
        return
    if config["output"].get("existing_store", "resume") == "fail":
        raise ValueError("Output already exists (existing_store=fail)")
    if manifest.get("plan_id") != plan["plan_id"]:
        raise ValueError("Input, source metadata/time indices or configuration changed; use a new output directory")


def _check_disk(config, output, needed):
    _, reserve_gib, _ = resolve_disk_policy(config)
    disk = check_disk_space({"preflight": {"disk": {"mode": "fail", "min_free_gb": reserve_gib + needed / 2**30}}}, output)
    if disk.free_gb is None:
        raise PreflightError("Cannot determine available space for the spatial output")


def _remaining_bytes(plan, regions, manifest):
    budget = plan["budget"]
    if regions is None:
        committed_rows = len([key for key in manifest["completed"] if key.startswith("points:")]) * plan["unit_count"]
        return max(budget["temporary_bytes"], budget["estimated_peak_bytes"] - committed_rows * 200)
    done = set(manifest["completed"])
    remaining = budget["estimated_peak_bytes"]
    for region in regions:
        if not region["times"]:
            continue
        pixels = region["grid"]["width"] * region["grid"]["height"]
        per_time = pixels * (np.dtype(region["dtype"]).itemsize * 2 + 4 +
                             (np.dtype(region["n_obs_dtype"]).itemsize if region["n_obs_dtype"] else 0))
        for slot in region["times"]:
            if f"region:{region['region_id']}:{region['region_group_id']}:{slot['key']}" in done:
                remaining -= per_time
    return max(budget["temporary_bytes"], remaining)


def _json_default(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def register_cli(subparsers):
    for noun, verb in (("points", "extract"), ("regions", "export")):
        parser = subparsers.add_parser(noun, help=f"Plan/export local {noun} without MGRS production")
        commands = parser.add_subparsers(dest=f"{noun}_command", required=True)
        command = commands.add_parser(verb, help=f"Read existing Zarr into independent {noun} products")
        command.add_argument("--config", required=True)
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--confirm-large-job", action="store_true")
        command.set_defaults(func=_command, spatial_noun=noun)


def _command(args):
    config = _normalize_config(args.config)
    point_mode = config.get("spatial", {}).get("mode") == "points"
    if point_mode != (args.spatial_noun == "points"):
        raise ValueError("The command does not match spatial.mode")
    result = run_spatial(config, dry_run=args.dry_run, confirm_large_job=args.confirm_large_job)
    print(json.dumps(result, ensure_ascii=True, indent=2, default=_json_default))
    if result["status"] in ("partial", "failed"):
        raise SystemExit(1)
