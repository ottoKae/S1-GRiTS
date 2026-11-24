"""Frozen spatial/source contracts shared by point and regional archives."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
from shapely.geometry import mapping, shape

from s1grits.atomic_write import atomic_write_json
from s1grits.spatial_inputs import build_region_grid


STATIC_LAYER_NAMES = {
    "lia": ("local_incidence_angle", "Local Incidence Angle"),
    "number_of_looks": ("number_of_looks", "Number of Looks"),
    "rtc_anf_beta0": ("rtc_anf_gamma0_to_beta0", "RTC Area Normalization Factor Gamma0 to Beta0"),
    "rtc_anf_sigma0": ("rtc_anf_gamma0_to_sigma0", "RTC Area Normalization Factor Gamma0 to Sigma0"),
}


def requested_static_layers(config):
    """Validate the opt-in point static settings at the configuration boundary."""
    static = config.get("static", {})
    if not isinstance(static, dict):
        raise ValueError("static must be a mapping")
    layers = static.get("layers", ["lia"])
    if (not isinstance(layers, list) or not all(isinstance(role, str) for role in layers)
            or "lia" not in layers or len(layers) != len(set(layers))
            or set(layers) - STATIC_LAYER_NAMES.keys()):
        raise ValueError("static.layers must contain lia and unique supported static roles")
    if len(layers) > 1 and config.get("spatial", {}).get("mode") != "points":
        raise ValueError("Additional static layers currently support points only")
    references = static.get("reference_products", {})
    if not isinstance(references, dict) or any(
        not isinstance(burst, str) or not burst.strip() or not isinstance(pid, str) or not pid.strip()
        for burst, pid in references.items()
    ):
        raise ValueError("static.reference_products must map burst IDs to nonempty product IDs")
    return layers


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=_json_value, allow_nan=False).encode()).hexdigest()


def group_key(product):
    fields = {key: product.get(key) for key in (
        "direction", "relative_orbit", "platform", "processing_version", "product_version")}
    return "g_" + digest(fields)[:20]


def latest_products(products):
    selected = {}
    for product in products:
        key = (group_key(product), product["burst_id"], product["acquisition_id"])
        rank = (product.get("processing_date") or "", product["product_id"])
        if key not in selected or rank > (selected[key].get("processing_date") or "", selected[key]["product_id"]):
            selected[key] = product
    return sorted(selected.values(), key=lambda p: (p["acquisition_utc"], p["burst_id"], p["product_id"]))


def prepare_state(config, features, products, output, static_products=()):
    """Load or initialize frozen groups without modifying the persisted manifest."""
    output = Path(output).resolve()
    _validate_output(output, config)
    strategy = config.get("source_strategy", "S0")
    if strategy not in {"S0", "S1"}:
        raise ValueError("source_strategy must be S0 or S1")
    if config.get("read_mode", "range") not in {"range", "burst_fetch", "local_cache"}:
        raise ValueError("read_mode must be range, burst_fetch or local_cache")
    workers = config.get("workers", 4)
    if not isinstance(workers, int) or not 1 <= workers <= 16:
        raise ValueError("workers must be an integer between 1 and 16")
    units = []
    for _, row in features.iterrows():
        unit = {"geometry": mapping(row.geometry), "spatial_mode": row["spatial_mode"]}
        if row["spatial_mode"] == "points":
            unit.update(unit_id=str(row.point_id), point_id=str(row.point_id),
                        lon=float(row.geometry.x), lat=float(row.geometry.y))
        else:
            unit.update(unit_id=str(row.region_id), region_id=str(row.region_id),
                        region_internal_id=str(row.region_internal_id), grid=build_region_grid(row, config))
            for key in ("ix", "iy", "center_lon", "center_lat", "radius_m"):
                if key in row and row[key] is not None:
                    unit[key] = _json_value(row[key]) if isinstance(row[key], np.generic) else row[key]
        units.append(unit)
    units.sort(key=lambda item: item["unit_id"])
    contract = {"schema_version": 1, "units": units, "source_strategy": strategy,
                "platform_policy": "separate", "resampling": "nearest", "radiometry": "gamma0",
                "data_domain": "linear_power", "mask_accept_codes": [0]}
    contract_id = digest(contract)
    path = output / "manifest.json"
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("archive_kind") != "opera_spatial" or state.get("contract_id") != contract_id:
            raise ValueError("Frozen spatial/sampling contract differs; use a new archive")
    else:
        entries = list(output.iterdir()) if output.exists() else []
        if any(p.name != ".s1grits.lock" for p in entries):
            raise ValueError("New OPERA output must be empty or contain its own matching manifest")
        state = {"archive_kind": "opera_spatial", **contract, "contract_id": contract_id,
                 "groups": {}, "products": [], "committed": {}, "status": "initialized"}
    by_group = {}
    for product in sorted(products, key=lambda p: (p["acquisition_utc"], p["product_id"])):
        by_group.setdefault(group_key(product), []).append(product)
    for key, members in by_group.items():
        nominal = {}
        for product in members:
            nominal.setdefault(product["burst_id"], product)
        if key in state["groups"]:
            unknown = set(nominal) - {s["burst_id"] for s in state["groups"][key]["sources"]}
            if unknown:
                raise ValueError("New bursts change the frozen candidate set; use a new assembly: " + ", ".join(sorted(unknown)))
            continue
        sources = []
        for index, (burst, product) in enumerate(sorted(nominal.items()), 1):
            geometry = shape(product["geometry"])
            if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                raise ValueError("Nominal burst geometry must be a valid nonempty polygon: " + burst)
            static = [p for p in static_products if p["burst_id"] == burst and p.get("assets", {}).get("lia")]
            reference = max(static, key=lambda p: (p.get("validity_start") or "", p["product_id"])) if static else None
            sources.append({"source_index": index, "burst_id": burst, "geometry": mapping(geometry),
                            "nominal_reference_product_id": product["product_id"],
                            "static_asset": reference["assets"]["lia"] if reference else None,
                            "static_product_id": reference["product_id"] if reference else None})
        group = {name: members[0].get(name) for name in (
            "direction", "relative_orbit", "platform", "processing_version", "product_version")}
        group.update(sources=sources, owner_level="burst")
        group["assembly_id"] = digest({"contract": contract_id, "group": group})
        state["groups"][key] = group
    return json.loads(json.dumps(state, default=_json_value, allow_nan=False))


def persist(state, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    clean = json.loads(json.dumps(state, default=_json_value, allow_nan=False))
    atomic_write_json(clean, output / "manifest.json")


def physical_sample(sample, role, product):
    """Validate source evidence and apply declared scaling exactly once."""
    result = dict(sample)
    if result.get("status") == "read_failed":
        return result
    tags = result.get("tags", {})
    tags = tags.get("dataset", tags)
    for tag, field in (("BURST_ID", "burst_id"), ("TRACK_NUMBER", "relative_orbit"),
                       ("ABSOLUTE_ORBIT_NUMBER", "absolute_orbit"),
                       ("ORBIT_PASS_DIRECTION", "direction"), ("PLATFORM", "platform")):
        if tag in tags and product.get(field) is not None:
            actual, expected = str(tags[tag]).upper().replace("_", "-"), str(product[field]).upper().replace("_", "-")
            if field in {"relative_orbit", "absolute_orbit"}:
                actual, expected = str(int(float(actual))), str(int(float(expected)))
            if actual != expected:
                raise ValueError("Provider and TIFF identity differ: " + tag)
    if role in {"vv", "vh"}:
        if (str(tags.get("PROCESSING_INFORMATION_OUTPUT_BACKSCATTER_NORMALIZATION_CONVENTION", "")).lower() != "gamma0"
                or str(tags.get("PROCESSING_INFORMATION_OUTPUT_BACKSCATTER_EXPRESSION_CONVENTION", "")).lower() != "linear backscatter intensity"):
            raise ValueError("COG tags must establish gamma0 linear-power radiometry")
        result.update(radiometry="gamma0", data_domain="linear_power", units="1",
                      radiometry_evidence="source_COG_processing_tags")
    if role == "lia":
        result.update(units="deg", lia_scope="static_nominal_burst_not_per_acquisition",
                      lia_unit_basis="https://developers.google.com/earth-engine/datasets/catalog/OPERA_RTC_L2_V1_S1_STATIC")
    if role in STATIC_LAYER_NAMES:
        quantity, layer_name = STATIC_LAYER_NAMES[role]
        if "LAYER_NAME" in tags and str(tags["LAYER_NAME"]).strip().lower() not in {quantity, layer_name.lower()}:
            raise ValueError("COG LAYER_NAME differs from requested static role: " + role)
        result.update(quantity=quantity, scope="static_nominal_burst_reference")
        if role != "lia":
            result.update(units="1", data_domain="sample_contribution_count" if role == "number_of_looks" else "linear_ratio")
        if role in {"rtc_anf_beta0", "rtc_anf_sigma0"}:
            result.update(conversion_from="gamma0", conversion_to=role.removeprefix("rtc_anf_"))
    scale, offset = float(result.get("scale", 1)), float(result.get("offset", 0))
    if not np.isfinite([scale, offset]).all():
        raise ValueError("Nonfinite raster scale or offset")
    if role == "mask" and (scale != 1 or offset != 0):
        raise ValueError("Categorical OPERA mask must use identity scale and offset")
    if not result.get("scale_offset_applied", False):
        if "values" in result:
            values = np.asarray(result["values"], dtype=np.float32) * scale + offset
            values[~np.isfinite(values)] = np.nan
            if role in {"vv", "vh"}:
                values[values <= 0] = np.nan
            elif role in STATIC_LAYER_NAMES and role != "lia":
                values[values < 0] = np.nan
            result["values"] = values
        elif result.get("status") == "valid" and result.get("value") is not None:
            value = float(result["value"]) * scale + offset
            if not np.isfinite(value) or role in {"vv", "vh"} and value <= 0:
                result.update(value=None, status="nonfinite" if not np.isfinite(value) else "nonpositive")
            elif role in STATIC_LAYER_NAMES and role != "lia" and value < 0:
                result.update(value=None, status="negative")
            else:
                result["value"] = value
        result["scale_offset_applied"] = True
    return result


def job_budget(state, products, config):
    products = list({p["product_id"]: p for p in products}.values())
    latest = latest_products(products)
    pixels = sum(unit.get("grid", {}).get("width", 1) * unit.get("grid", {}).get("height", 1)
                 for unit in state["units"])
    times = len({(group_key(p), p["acquisition_id"]) for p in latest})
    source_count = sum(len(group["sources"]) for group in state["groups"].values())
    if state["units"] and state["units"][0]["spatial_mode"] == "points":
        # Include row identities and quality fields, not just two numeric samples.
        stored = pixels * (len(products) * 3 + times + source_count) * 1024
        window_cache = 0
    else:
        stored = pixels * (times * 13 + len(state["groups"]) * 9 + source_count * 4)
        window_cache = pixels * (len(products) * 12 + source_count * 4)
    assets = {a["asset_id"]: a for p in products for a in p["assets"].values()}
    for group in state["groups"].values():
        for source in group["sources"]:
            asset = source.get("static_asset")
            if asset:
                assets[asset["asset_id"]] = asset
    transfer_upper = sum(a.get("expected_bytes", 0) for a in assets.values())
    temporary = window_cache + (transfer_upper if config.get("read_mode", "range") == "burst_fetch" else 0)
    repair_bytes = stored if config.get("operation") == "repair" else 0
    return {"pixel_count": pixels, "acquisition_group_count": times,
            "uncompressed_output_bytes": stored, "full_asset_bytes_upper": transfer_upper,
            "temporary_bytes_upper": temporary, "repair_staging_bytes_upper": repair_bytes,
            "required_bytes_upper": stored + temporary + repair_bytes,
            "confirmation_required": stored + temporary + repair_bytes > 50_000_000_000}


def check_budget(budget, output, confirmed=False):
    if budget["confirmation_required"] and not confirmed:
        raise ValueError("Job exceeds 50 GB; review plan and pass --confirm-large-job")
    probe = Path(output).resolve()
    while not probe.exists():
        probe = probe.parent
    if shutil.disk_usage(probe).free < budget["required_bytes_upper"]:
        raise ValueError("Insufficient disk space for output and staging")


def _validate_output(output, config):
    for protected in config.get("protected_roots", []):
        protected = Path(protected).resolve()
        if output == protected or output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ValueError("Output overlaps a protected production root")
    for ancestor in [output, *output.parents]:
        if (ancestor / "manifest.json").exists():
            try:
                own = json.loads((ancestor / "manifest.json").read_text(encoding="utf-8")).get("archive_kind") == "opera_spatial"
            except (ValueError, OSError):
                own = False
            if ancestor == output and own:
                continue
        if any((ancestor / name).exists() for name in ("zarr.json", ".zgroup", "catalog.parquet")):
            raise ValueError("Output is inside an existing raster store or production catalog")


def _json_value(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"Cannot serialize {type(value).__name__}")
