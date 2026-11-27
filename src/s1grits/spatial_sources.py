"""Read-only local source discovery for spatial exports."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import zarr
from affine import Affine
from rasterio.crs import CRS
from rasterio.warp import transform_bounds
from shapely.geometry import box, mapping, shape


_OPEN_GROUPS: dict[str, zarr.Group] = {}


def source_group(path):
    key = str(Path(path).resolve())
    if key not in _OPEN_GROUPS:
        _OPEN_GROUPS[key] = zarr.open_group(key, mode="r")
    return _OPEN_GROUPS[key]


def close_sources():
    _OPEN_GROUPS.clear()


def read_source_window(source, variable, time_index, window):
    """Preserve stored values and precision; only express declared NoData as NaN."""
    row, col, height, width = map(int, window)
    array = source_group(source["path"])[variable]
    dims = source["variables"][variable]["dims"]
    slices = {"y": slice(row, row + height), "x": slice(col, col + width),
              "time": int(time_index or 0)}
    data = np.asarray(array[tuple(slices[d] for d in dims)])
    if [d for d in dims if d != "time"] == ["x", "y"]:
        data = data.T
    dtype = data.dtype if np.issubdtype(data.dtype, np.floating) else np.dtype("float64")
    data = data.astype(dtype, copy=True)
    nodata = source["variables"][variable].get("nodata")
    if nodata is not None:
        data[data == nodata] = np.nan
    data[~np.isfinite(data)] = np.nan
    return data


def discover_local_sources(config, geometry=None):
    root = Path(config["source"]["root"]).resolve()
    product_type = config["source"].get("product_type", "smonthly")
    if product_type not in ("scenes", "smonthly"):
        raise ValueError("source.product_type must be scenes or smonthly")
    rows = _source_rows(root)
    selection = config.get("selection", {})
    sources, static = [], []
    seen = set()
    for row in rows:
        row = _plain(row)
        kind = row.get("product_type")
        if kind and kind not in (product_type, "static"):
            continue
        if kind == "static" and not config.get("include_static", False):
            continue
        path = _resolve_asset(root, row["zarr_path"], row.get("tile_id"))
        if str(path) in seen:
            continue
        seen.add(str(path))
        source = describe_source(path, row, root)
        if source["product_type"] not in (product_type, "static"):
            continue
        if selection.get("flight_directions") and source["direction"] not in selection["flight_directions"]:
            continue
        tracks = selection.get("track_numbers", [])
        if tracks and not set(map(int, tracks)).intersection(source["track_members"]):
            continue
        if geometry is not None and not shape(source["coverage_geometry"]).intersects(geometry):
            continue
        if source["product_type"] == "static":
            static.append(source)
            continue
        signature = config["source"].get("processing_signature")
        if signature and source["processing_signature"] != signature:
            continue
        source["times"] = [t for t in source["times"] if _selected_time(t, config.get("time", {}))]
        if source["times"]:
            sources.append(source)
    variants = {s["semantic_signature"] or s["processing_signature"] for s in sources}
    if not config["source"].get("processing_signature") and len(variants) > 1:
        choices = sorted({str(s["processing_signature"]) for s in sources})
        raise ValueError("Multiple processing variants match; select source.processing_signature "
                         f"or a single source store. Available signatures: {choices}")
    for source in sources:
        matches = [s for s in static if source["geometry_group_id"]
                   and s["geometry_group_id"] == source["geometry_group_id"]
                   and s["direction"] == source["direction"]
                   and s["track_members"] == source["track_members"]
                   and _compatible_static_grid(source, s)]
        source["static_source"] = matches[0] if len(matches) == 1 else None
        source["static_status"] = "matched" if len(matches) == 1 else (
            "ambiguous" if matches else "missing")
    return sorted(sources, key=lambda s: s["source_unit_id"])


def describe_source(path, record=None, root=None):
    """Describe evidence available in the store/catalog, without guessing units."""
    path = Path(path).resolve()
    record = _plain(record or {})
    root = Path(root or path.parent)
    group = source_group(path)
    attrs = dict(group.attrs)
    merged = {**record, **{k: v for k, v in attrs.items() if v is not None}}
    crs = merged.get("crs")
    transform = merged.get("transform")
    if not crs or transform is None:
        raise ValueError(f"Source CRS/transform missing: {path}")
    transform = list(transform)[:6]
    affine = Affine(*transform)
    if affine.b or affine.d or affine.a <= 0 or affine.e >= 0:
        raise ValueError(f"Only north-up source grids are supported: {path}")
    width, height = int(group["x"].shape[0]), int(group["y"].shape[0])
    variables = {}
    for name, array in group.arrays():
        meta = dict(array.attrs)
        dims = list(getattr(array.metadata, "dimension_names", None) or meta.get("_ARRAY_DIMENSIONS", []))
        if set(dims) not in ({"time", "y", "x"}, {"y", "x"}):
            continue
        # A storage fill value alone does not declare scientific NoData.
        nodata = meta.get("_FillValue", meta.get("missing_value", meta.get("nodata")))
        units = meta.get("units", (attrs.get("variable_units") or {}).get(name))
        domain = meta.get("data_domain", attrs.get("data_domain"))
        if domain is None and units in ("dB", "db", "decibel"):
            domain = "db"
        variables[name] = {"dtype": str(array.dtype), "dims": dims,
                           "chunks": list(array.chunks), "nodata": _plain(nodata),
                           "units": units or "unknown", "data_domain": domain or "unknown",
                           "declared_scale": meta.get("scale_factor", 1.0),
                           "declared_offset": meta.get("add_offset", 0.0),
                           "scale_offset_applied": False}
    variant = merged.get("processing_variant_json")
    variant = json.loads(variant) if isinstance(variant, str) else variant
    semantic = merged.get("semantic_signature")
    if variant:
        semantic = _digest(variant)
    tracks = _track_members(merged)
    direction = merged.get("flight_direction") or merged.get("direction")
    if direction is None:
        label = str(merged.get("product_label", merged.get("geometry_group_id", "")))
        direction = next((d for d in ("ASCENDING", "DESCENDING") if d in label), None)
    kind = merged.get("product_type")
    if kind not in ("scenes", "smonthly", "static"):
        raise ValueError(f"Source product_type is missing or unsupported: {path}")
    nominal = merged.get("nominal_geometry")
    tile_id = merged.get("tile_id") or merged.get("mgrs_tile_id")
    if nominal is None and tile_id:
        nominal = _mgrs_geometry(str(tile_id))
    if isinstance(nominal, str):
        from shapely import wkt
        nominal = mapping(wkt.loads(nominal))
    west, north = affine * (0, 0)
    east, south = affine * (width, height)
    coverage = mapping(box(*transform_bounds(crs, "EPSG:4326", west, south, east, north)))
    times = _source_times(group, merged, root, path, kind)
    domain_values = {v["data_domain"] for k, v in variables.items() if k in ("VV", "VH", "VV_dB", "VH_dB")}
    domain = next(iter(domain_values)) if len(domain_values) == 1 else "unknown"
    method = merged.get("aggregation_method") or (variant or {}).get("processing.monthly.composite_method")
    source = {"source_unit_id": "store_" + _digest(str(path))[:20], "path": str(path),
              "product_type": kind, "direction": direction,
              "track": tracks[0] if len(tracks) == 1 else None, "track_members": tracks,
              "processing_signature": merged.get("processing_signature"),
              "semantic_signature": semantic, "aggregation_method": method,
              "aggregation_domain": merged.get("aggregation_domain", "unknown"),
              "data_domain": domain, "geometry_group_id": merged.get("geometry_group_id"),
              "crs": str(crs), "transform": transform, "width": width, "height": height,
              "grid_id": merged.get("grid_id"),
              "nominal_geometry": nominal, "coverage_geometry": coverage,
              "variables": variables, "times": times, "attrs": _plain(attrs),
              "data_revision": merged.get("data_revision"),
              "n_obs_definition": merged.get("n_obs_definition", "unknown"),
              "read_mode": "local_cache", "source_kind": "local_zarr",
              "metadata_evidence": {"catalog": str(root / "catalog.parquet") if (root / "catalog.parquet").exists() else None,
                                    "store": str(path)},
              "static_source": None}
    source.update(_declared_product_metadata(merged))
    return _plain(source)


def _source_rows(root):
    if (root / "zarr.json").is_file() or (root / ".zgroup").is_file():
        return [{"zarr_path": str(root)}]
    catalog = root / "catalog.parquet"
    if catalog.exists():
        df = pd.read_parquet(catalog)
        if "status" in df:
            df = df[df["status"] == "complete"]
        return df.to_dict("records")
    raise ValueError(f"source.root must be a Zarr store or a directory with catalog.parquet: {root}")


def _resolve_asset(root, value, tile=None):
    path = Path(value)
    if path.is_absolute():
        return path
    direct = root / path
    if direct.exists():
        return direct
    if tile is not None:
        tile_path = root / str(tile) / path
        if tile_path.exists():
            return tile_path
    raise FileNotFoundError(f"Source asset not found: {direct}")


def _track_members(metadata):
    members = metadata.get("track_members")
    if members:
        return sorted(set(map(int, members)))
    token = metadata.get("track_token")
    if token is None:
        match = re.search(r"_TK(\d+(?:_\d+)*)", str(metadata.get("geometry_group_id", "")))
        token = match.group(1) if match else None
    if token is not None:
        return sorted(set(int(t) for t in str(token).split("_")))
    track = metadata.get("track")
    return [int(track)] if track is not None else []


def _source_times(group, metadata, root, path, kind):
    if kind == "static":
        return []
    array = group["time"]
    raw = array[:]
    if np.issubdtype(raw.dtype, np.number):
        units = dict(array.attrs).get("units")
        if not units:
            raise ValueError(f"Numeric source time has no declared units: {path}")
        from xarray.coding.times import decode_cf_datetime
        raw = decode_cf_datetime(raw, units, dict(array.attrs).get("calendar", "standard"))
    explicit = metadata.get("time_records") or []
    by_index = {int(r["time_index"]): r for r in explicit}
    rows = []
    for index, value in enumerate(raw):
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp):
            raise ValueError(f"Invalid source time at {path}, index {index}")
        timestamp = timestamp.tz_localize("UTC") if timestamp.tz is None else timestamp.tz_convert("UTC")
        info = by_index.get(index, {})
        record = {"time_index": index, "time_utc": timestamp.isoformat(),
                  "period_start": info.get("period_start"), "period_end": info.get("period_end"),
                  "acquisition_id": info.get("acquisition_id"), "platform": info.get("platform"),
                  "absolute_orbit": info.get("absolute_orbit"), "datatake_id": info.get("datatake_id")}
        record.update(_declared_product_metadata(info))
        if record["acquisition_id"] is None and record["platform"] and (record["absolute_orbit"] or record["datatake_id"]):
            record["acquisition_id"] = f"{record['platform']}:{record['absolute_orbit'] or record['datatake_id']}"
        if kind == "smonthly":
            month = timestamp.tz_localize(None).to_period("M")
            record["period_start"] = record["period_start"] or month.start_time.tz_localize("UTC").isoformat()
            record["period_end"] = record["period_end"] or (month + 1).start_time.tz_localize("UTC").isoformat()
            record["period_evidence"] = "declared_time_records" if info.get("period_start") else "smonthly_UTC_calendar_month"
        rows.append(record)
    return rows


def _declared_product_metadata(metadata):
    fields = {name: metadata[name] for name in ("product_id", "product_version", "production_time", "opera_ids")
              if metadata.get(name) is not None}
    bursts = metadata.get("burst_ids", metadata.get("jpl_burst_ids"))
    if bursts is not None:
        fields["burst_ids"] = bursts
    return fields


def _selected_time(record, selection):
    timestamp = pd.Timestamp(record["period_start"] or record["time_utc"])
    if selection.get("years") and timestamp.year not in selection["years"]:
        return False
    if selection.get("months") and timestamp.month not in selection["months"]:
        return False
    return True


def _mgrs_geometry(tile):
    import geopandas as gpd
    df = gpd.read_parquet(Path(__file__).parent / "data" / "mgrs.parquet", filters=[("mgrs_tile_id", "=", tile)])
    if len(df) != 1:
        return None
    return mapping(df.to_crs(4326).geometry.iloc[0])


def _compatible_static_grid(first, second):
    a, b = Affine(*first["transform"]), Affine(*second["transform"])
    if CRS.from_user_input(first["crs"]) != CRS.from_user_input(second["crs"]) or (a.a, a.e) != (b.a, b.e):
        return False
    offsets = np.array([(b.c - a.c) / a.a, (b.f - a.f) / a.e])
    if not np.allclose(offsets, np.rint(offsets), rtol=0, atol=1e-8):
        return False
    col, row = np.rint(offsets).astype(int)
    return (max(0, col) < min(first["width"], col + second["width"])
            and max(0, row) < min(first["height"], row + second["height"]))


def _digest(value):
    return hashlib.sha256(json.dumps(_plain(value), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _plain(value):
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_plain(v) for v in value]
    if isinstance(value, np.generic):
        return _plain(value.item())
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, Path)):
        return str(value)
    return value
