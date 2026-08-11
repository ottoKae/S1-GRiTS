"""
STAC Item and Collection builder for S1-GRiTS outputs.

Generates STAC 1.1.0 Item JSON files alongside data products and maintains
a STAC Collection JSON at the output root directory. Implements the
STAC datacube extension v2.3.0.

No external STAC library is required — outputs are plain JSON.
"""

import json
import os

import pyproj
import pandas as pd

from s1grits.logger_config import get_logger

logger = get_logger(__name__)

STAC_VERSION = "1.1.0"
DATACUBE_EXTENSION_URI = "https://stac-extensions.github.io/datacube/v2.3.0/schema.json"
PROJECTION_EXTENSION_URI = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"
SAT_EXTENSION_URI = "https://stac-extensions.github.io/sat/v1.0.0/schema.json"
EO_EXTENSION_URI = "https://stac-extensions.github.io/eo/v1.1.0/schema.json"

# Extension set shared by every data Item this project emits (single source of
# truth so scenes/monthly/static never drift on extension URIs or versions).
ITEM_STAC_EXTENSION_URIS = [
    DATACUBE_EXTENSION_URI,
    PROJECTION_EXTENSION_URI,
    SAT_EXTENSION_URI,
    EO_EXTENSION_URI,
]

# Process-level switch for STAC *file* output. Workflows ALWAYS leave this OFF
# (they are catalog-only — see config_overrides.apply_output_overrides_and_stac
# and the per-tile processors); STAC is produced solely by `catalog resync`,
# which turns it on via set_stac_output_enabled(write_stac). This gates only the
# file writers (write_stac_item / write_stac_collection / update_root_catalog and
# the per-item writers in the workflows); build_stac_item_dict stays pure so
# resync can always project items to geoparquet regardless of this flag.
_STAC_OUTPUT_ENABLED = True


def set_stac_output_enabled(enabled: bool) -> None:
    """Enable/disable STAC file output for the current process."""
    global _STAC_OUTPUT_ENABLED
    _STAC_OUTPUT_ENABLED = bool(enabled)


def stac_output_enabled() -> bool:
    """True if STAC file writers should emit output."""
    return _STAC_OUTPUT_ENABLED


# Core 4 bands always present
_CORE_BANDS_VV = ["VV_dB", "VH_dB", "Ratio", "RVI"]
_CORE_BANDS_HH = ["HH_dB", "HV_dB", "Ratio", "RVI"]

# Canonical band ordering. The two polarization channels lead (VV/VH or HH/HV),
# then the derived Ratio/RVI, then everything else (GLCM textures, etc.) sorted
# by name. This guarantees a STABLE, deterministic band order regardless of the
# source (COG band order, or Zarr key-iteration order which is NOT insertion
# order) — the first two bands are always the polarization pair. ML should still
# select bands BY NAME (eo:bands / cube spectral values carry the authoritative
# names); the stable order is a convenience, not a contract to index positionally.
_BAND_PRIORITY = ["VV_dB", "VH_dB", "HH_dB", "HV_dB", "Ratio", "RVI"]

# Variables that live in a Zarr group but are NOT data bands: spatial/temporal
# coordinates and CF grid_mapping (CRS) variables (rioxarray writes 'spatial_ref').
# These must never leak into the band list when bands are reconstructed from
# Zarr keys.
_NON_BAND_VARS = {
    "x", "y", "time", "lat", "lon", "latitude", "longitude",
    "spatial_ref", "crs", "grid_mapping", "band",
}


def _canonical_band_order(bands) -> list[str]:
    """Order a band list canonically and drop non-band variables.

    The polarization pair leads (VV/VH or HH/HV), then Ratio/RVI, then the rest
    alphabetically. Coordinate / CF grid_mapping variables (x, y, time,
    spatial_ref, …) are filtered out. Stable and idempotent.
    """
    if not bands:
        return []
    filtered = [b for b in bands if b not in _NON_BAND_VARS]

    def _key(b):
        try:
            return (0, _BAND_PRIORITY.index(b))
        except ValueError:
            return (1, str(b))
    return sorted(filtered, key=_key)



def _epsg_int(crs_str: str) -> int:
    """Extract EPSG integer from an 'EPSG:XXXXX' string."""
    return int(crs_str.split(":")[-1])


def _utm_extent_to_wgs84(transform: list, width: int, height: int, crs_str: str):
    """
    Project raster UTM bounding box corners to WGS84.

    Returns:
        bbox: [west, south, east, north] in WGS84 degrees
        geometry: GeoJSON Polygon dict in WGS84
    """
    x_min = transform[2]
    x_max = transform[2] + transform[0] * width
    y_max = transform[5]
    y_min = transform[5] + transform[4] * height  # transform[4] is negative

    src_crs = pyproj.CRS.from_user_input(crs_str)
    dst_crs = pyproj.CRS.from_epsg(4326)
    tr = pyproj.Transformer.from_crs(src_crs, dst_crs, always_xy=True)

    # Five points: four corners + closing point
    xs = [x_min, x_max, x_max, x_min, x_min]
    ys = [y_min, y_min, y_max, y_max, y_min]
    lons, lats = tr.transform(xs, ys)

    bbox = [
        round(min(lons[:4]), 6),
        round(min(lats[:4]), 6),
        round(max(lons[:4]), 6),
        round(max(lats[:4]), 6),
    ]
    geometry = {
        "type": "Polygon",
        "coordinates": [[[round(lon, 6), round(lat, 6)] for lon, lat in zip(lons, lats)]],
    }
    return bbox, geometry


def _read_cog_bands(cog_path: str) -> list[str] | None:
    """
    Read band names from a COG file using rasterio band descriptions.

    Returns a list of band name strings, or None if the file cannot be read.
    Falls back to description index ('Band_N') for bands without a description.
    """
    try:
        import rasterio
        with rasterio.open(cog_path) as src:
            bands = []
            for i, desc in enumerate(src.descriptions, start=1):
                bands.append(desc if desc else f"Band_{i}")
            return bands
    except Exception as _e:
        logger.debug("Could not read band descriptions from COG: %s", _e)
        return None


def _band_values(polarization: str) -> list[str]:
    """
    Return the default 4-band core list for the given polarization mode.

    Args:
        polarization: SAR polarization mode ('VV+VH' or 'HH+HV').

    Returns:
        List of band name strings (e.g. ['VV_dB', 'VH_dB', 'Ratio', 'RVI']).
    """
    if polarization == "HH+HV":
        return _CORE_BANDS_HH
    return _CORE_BANDS_VV


def _resolve_bands(record: dict, output_root: str, polarization: str) -> list[str]:
    """
    Resolve the actual band list for a catalog record.

    Priority:
      1. The catalog-recorded band list (record['bands']) — authoritative, it is
         exactly what the Zarr holds. Critical for static products, which are
         stored as one single-band COG PER layer plus a multi-variable Zarr:
         reading a single COG would wrongly yield just one band ("Band_1"). Also
         what makes Zarr-only outputs (no COG) self-describing.
      2. Band descriptions read from the COG on disk (for callers that didn't
         record a band list, e.g. some direct workflow writes); GLCM texture
         bands are picked up here when present.
      3. The default core-band list for the given polarization.

    The result is always returned in canonical order (VV/VH or HH/HV first).
    """
    # 1) catalog-recorded bands (authoritative; matches the Zarr variables)
    rec_bands = record.get("bands")
    if rec_bands:
        if isinstance(rec_bands, str):
            try:
                rec_bands = json.loads(rec_bands)
            except (ValueError, TypeError):
                rec_bands = None
        if rec_bands:
            return _canonical_band_order(rec_bands)

    # 2) fall back to reading band descriptions from the COG on disk
    cog_rel = record.get("cog_path")
    if cog_rel:
        # cog_path may be absolute, output_root-relative (starts with the tile
        # id), or tile-relative (resync stores it without the tile prefix).
        if os.path.isabs(cog_rel):
            cog_abs = cog_rel
        else:
            _p = str(cog_rel).replace("\\", "/").lstrip("/")
            _tile = record.get("tile_id") or record.get("mgrs_tile_id")
            if _tile and not (_p == _tile or _p.startswith(_tile + "/")):
                _p = f"{_tile}/{_p}"
            cog_abs = os.path.join(output_root, _p)
        bands = _read_cog_bands(cog_abs)
        if bands:
            return _canonical_band_order(bands)

    # 3) default core-band list for the polarization
    return _band_values(polarization)


def build_stac_item_dict(
    record: dict,
    output_root: str,
    polarization: str = "VV+VH",
    tile_dir_override: str = None,
    relative_to: str = None,
    related_items: list[dict] | None = None,
) -> tuple[dict, str]:
    """
    Build a STAC Item dict for one tile x time slice (no disk write).

    This is the single source of truth for item construction, shared by the
    JSON serializer (``write_stac_item``) and the stac-geoparquet serializer.

    Args:
        record: Catalog record dict. Required keys: tile_id/mgrs_tile_id,
                month, crs, width, height, transform. Optional: cog_path,
                zarr_path, preview_path, flight_direction, product_type/
                output_type, processing_level, product_label, item_id, bands.
        output_root: Root output directory.
        polarization: SAR polarization mode ('VV+VH' or 'HH+HV').
        tile_dir_override: Explicit tile directory name.
        relative_to: Directory that asset/link hrefs are computed relative to.
            When None (JSON mode), hrefs are relative to the item JSON's own
            directory and a ``self`` link is included. When set (e.g. the
            collection dir for stac-geoparquet), hrefs are relative to that
            directory and the ``self`` link is omitted, since geoparquet items
            have no standalone file.

    Returns:
        (item_dict, item_abs_path) — the item, and the absolute path the JSON
        serializer would write it to (also used to derive the default href base).
    """
    # Validate that item_path has been computed (either manually or via normalize_catalog_record)
    item_path_field = record.get("item_path")
    if not item_path_field:
        item_id = record.get("item_id")
        logger.warning(
            "item_path not found in catalog record for item_id=%s. "
            "This should have been auto-computed by normalize_catalog_record().",
            item_id,
        )

    mgrs_tile_id = record.get("tile_id") or record.get("mgrs_tile_id", "")
    flight_direction = record.get("flight_direction")
    # output_type drives the STAC layout. It is NOT a canonical catalog column,
    # so normalize_catalog_record() strips it; fall back to product_type (which
    # is canonical and carries the same value: scenes/smonthly/static/monthly).
    output_type = record.get("output_type") or record.get("product_type")
    crs = record["crs"]
    width = int(record["width"])
    height = int(record["height"])
    transform = list(record["transform"])

    flight_suffix = f"_{flight_direction}" if flight_direction else ""
    if tile_dir_override:
        tile_dir_name = tile_dir_override
    else:
        tile_dir_name = f"{mgrs_tile_id}{flight_suffix}"

    # ---- Item identity ----
    # Normalise month: None / NaN / the literal "None"/"nan" must become "" so
    # the datetime fallback below triggers (str(None) == "None" is truthy and
    # would otherwise yield an invalid "None-01T00:00:00Z" — e.g. timeless
    # static products whose month is None).
    _month_raw = record.get("month")
    month = "" if _month_raw is None or str(_month_raw).strip().lower() in ("none", "nan", "nat", "") else str(_month_raw)
    _explicit_id = record.get("item_id")
    if _explicit_id and output_type not in ("scenes",):
        # Use explicit item_id from catalog record (resync or direct caller)
        item_id = _explicit_id
        datetime_str = f"{month}-01T00:00:00Z" if month else "1970-01-01T00:00:00Z"
    elif output_type == "scenes":
        # Datetime-based item id for per-acquisition scenes
        dt_val = record.get("datetime")
        if dt_val is not None:
            ts = pd.Timestamp(dt_val)
            item_id = f"{mgrs_tile_id}{flight_suffix}_{ts.strftime('%Y%m%dT%H%M%S')}"
            datetime_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            item_id = f"{mgrs_tile_id}{flight_suffix}_{month}"
            datetime_str = f"{month}-01T00:00:00Z"
    else:
        # Monthly composite (new or legacy)
        item_id = f"{mgrs_tile_id}{flight_suffix}_{month}"
        datetime_str = f"{month}-01T00:00:00Z"

    # ---- Item subdirectory (where the JSON lives) ----
    _product_label = record.get("product_label", "")
    if output_type == "scenes":
        item_subdir = f"items/{_product_label}" if _product_label else "items/scenes"
    elif output_type == "smonthly":
        item_subdir = f"items/{_product_label}" if _product_label else "items/smonthly"
    elif output_type == "static":
        item_subdir = f"items/{_product_label}" if _product_label else "items/static"
    elif output_type == "monthly":
        item_subdir = f"items/{_product_label}" if _product_label else "items/monthly"
    else:
        item_subdir = ""  # legacy: item at tile_dir root

    # Collection ID for item links (from catalog record or default)
    _cid = record.get("collection_id", "s1grits-monthly")

    # ---- Item JSON location (output_root-relative) ----
    # The item lives under the SAME tile directory as its data ({tile_id}/),
    # inside items/{product_label}/. Prefer the catalog's item_path (tile-dir
    # relative); else derive it; else fall back to the true legacy root layout.
    _item_path_rel = record.get("item_path")
    if not _item_path_rel and item_subdir and mgrs_tile_id:
        _item_path_rel = f"{item_subdir}/{item_id}.json"
    if _item_path_rel and mgrs_tile_id:
        _item_rel = os.path.join(mgrs_tile_id, str(_item_path_rel).replace("\\", "/"))
    elif item_subdir and mgrs_tile_id:
        _item_rel = os.path.join(mgrs_tile_id, item_subdir, f"{item_id}.json")
    else:
        _item_rel = os.path.join(tile_dir_name, f"{item_id}.json")  # legacy root
    item_abs = os.path.join(output_root, _item_rel)
    item_abs_dir = os.path.dirname(item_abs)

    # Directory that asset/link hrefs are resolved relative to. JSON mode uses
    # the item's own dir; geoparquet mode passes the parquet/collection dir so
    # the parquet + its relative hrefs are self-contained.
    _href_base = relative_to if relative_to else item_abs_dir

    # ---- Asset/link href resolution ----
    # Catalog asset paths are stored output_root-relative; resolve each to a
    # href relative to _href_base with os.path.relpath (robust to depth/naming).
    def _rel_to_item(path_from_record):
        """Catalog asset path -> href relative to ``_href_base``.

        Accepts both conventions seen across callers: output_root-relative
        (already starts with '{tile_id}/', e.g. the monthly/merge builds) and
        tile_dir-relative (e.g. resync via Path.relative_to(tile_dir)). The path
        is normalised to an absolute path under output_root before computing the
        relative href, so hrefs are correct either way.
        """
        if not path_from_record:
            return None
        _p = str(path_from_record).replace("\\", "/").lstrip("/")
        if _p.startswith("./"):
            _p = _p[2:]
        if mgrs_tile_id and not (_p == mgrs_tile_id or _p.startswith(mgrs_tile_id + "/")):
            _p = f"{mgrs_tile_id}/{_p}"
        _asset_abs = os.path.join(output_root, _p)
        return os.path.relpath(_asset_abs, _href_base).replace("\\", "/")

    # Compute WGS84 bbox and GeoJSON geometry
    bbox, geometry = _utm_extent_to_wgs84(transform, width, height, crs)

    # Compute spatial extents in tile CRS
    x_min = transform[2]
    x_max = transform[2] + transform[0] * width
    y_max = transform[5]
    y_min = transform[5] + transform[4] * height
    pixel_size = abs(transform[0])
    epsg = _epsg_int(crs)

    # Resolve actual band list from the COG file (includes GLCM bands if present)
    bands = _resolve_bands(record, output_root, polarization)

    # Temporal extent step differs for scenes vs monthly
    time_step = "P1D" if output_type == "scenes" else "P1M"

    item = {
        "stac_version": STAC_VERSION,
        "stac_extensions": list(ITEM_STAC_EXTENSION_URIS),
        "type": "Feature",
        "id": item_id,
        "collection": _cid,
        "geometry": geometry,
        "bbox": bbox,
        "properties": {
            "datetime": datetime_str,
            "platform": "sentinel-1",
            "instruments": ["c-sar"],
            "mgrs:tile_id": mgrs_tile_id,
            "s1:orbit_direction": flight_direction.lower() if flight_direction else None,
            "sat:orbit_state": flight_direction.lower() if flight_direction else None,
            "sat:relative_orbit": record.get("track"),
            "sat:absolute_orbit": record.get("pass_id"),
            "s1:processing_level": record.get("processing_level", "hARDCp"),
            "s1:monthly_composite": "median" if output_type in ("monthly", "smonthly") else None,
            "s1:despeckle": record.get("processing_level") != "ARDC",
            # Track-level join key: static geometry layers are auxiliary
            # variables of the scenes/smonthly cube sharing this geometry group.
            "s1grits:geometry_group_id": record.get("geometry_group_id") or None,
            "s1grits:role": "auxiliary" if output_type == "static" else None,
            "s1grits:product_variant": record.get("product_variant") or None,
            "s1grits:processing_signature": record.get("processing_signature") or None,
            "s1grits:grid_source": record.get("grid_source") or None,
            "s1grits:reference_grid_id": record.get("reference_grid_id") or None,
            "s1grits:reference_zarr_path": record.get("reference_zarr_path") or None,
            "s1grits:reference_product_type": record.get("reference_product_type") or None,
            "s1grits:reference_product_label": record.get("reference_product_label") or None,
            "s1grits:processing_variant": (
                json.loads(record["processing_variant_json"])
                if record.get("processing_variant_json") and isinstance(record.get("processing_variant_json"), str)
                else record.get("processing_variant_json") or None
            ),
            "s1grits:bands": (
                json.loads(record["bands"])
                if record.get("bands") and isinstance(record.get("bands"), str)
                else record.get("bands") or None
            ),
            "proj:epsg": epsg,
            "proj:shape": [height, width],
            "proj:transform": list(transform)[:9],
            "proj:geometry": geometry,
            "proj:bbox": bbox,
            "cube:dimensions": {
                "x": {
                    "type": "spatial",
                    "axis": "x",
                    "extent": [round(x_min, 3), round(x_max, 3)],
                    "step": pixel_size,
                    "reference_system": epsg,
                },
                "y": {
                    "type": "spatial",
                    "axis": "y",
                    "extent": [round(y_min, 3), round(y_max, 3)],
                    "step": pixel_size,
                    "reference_system": epsg,
                },
                "time": {
                    "type": "temporal",
                    "extent": [datetime_str, datetime_str],
                    "step": time_step,
                },
                "spectral": {
                    "type": "bands",
                    "values": bands,
                },
            },
        },
        "assets": {},
    }
    item["links"] = []
    if relative_to is None:
        item["links"].append({"rel": "self", "href": f"./{os.path.basename(item_abs)}"})
    item["links"].append({"rel": "collection", "href": os.path.relpath(os.path.join(output_root, "collections", _cid, "collection.json"), _href_base).replace("\\", "/")})
    item["links"].append({"rel": "root", "href": os.path.relpath(os.path.join(output_root, "catalog.json"), _href_base).replace("\\", "/")})

    # Cross-links to the other products of the same geometry group (one per
    # product_type). Lets a consumer navigate scenes <-> smonthly <-> static
    # auxiliary geometry without knowing the naming convention: static items
    # carry `related` links to the dynamic cube and vice versa.
    for _sib in (related_items or []):
        _sib_rel = _sib.get("item_rel")
        if not _sib_rel:
            continue
        _sib_pt = _sib.get("product_type")
        _sib_href = os.path.relpath(
            os.path.join(output_root, _sib_rel), _href_base
        ).replace("\\", "/")
        _link = {
            "rel": "related",
            "href": _sib_href,
            "type": "application/geo+json",
            "title": _sib.get("title") or (
                f"Auxiliary static geometry ({_sib_pt})"
                if _sib_pt == "static"
                else f"{_sib_pt} product (same geometry group)"
            ),
            "s1grits:product_type": _sib_pt,
            "s1grits:geometry_group_id": record.get("geometry_group_id"),
        }
        if _sib_pt == "static" and output_type != "static":
            _link["s1grits:role"] = "auxiliary"
        item["links"].append(_link)

    # Assets — hrefs are relative to the item JSON
    asset_title_prefix = {
        "scenes": "Scene",
        "monthly": "Monthly Composite",
        "smonthly": "Monthly Composite",
        "static": "Static Layer",
    }.get(output_type, "Product")

    cog_href = _rel_to_item(record.get("cog_path"))
    if cog_href:
        item["assets"]["cog"] = {
            "href": cog_href,
            "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            "roles": ["data"],
            "title": f"{asset_title_prefix} COG ({', '.join(bands)})",
            "eo:bands": [{"name": _b} for _b in bands],
        }

    zarr_path_rec = record.get("zarr_path") or record.get("vv_path")
    zarr_href = _rel_to_item(zarr_path_rec)
    if zarr_href:
        # The Zarr store is the primary asset for the time-series ML pipeline,
        # and the only data asset when COG/Preview are skipped (Zarr-only mode).
        # eo:bands here keeps the item self-describing without a COG.
        item["assets"]["zarr"] = {
            "href": zarr_href,
            "type": "application/vnd.zarr; version=3",
            "roles": ["data"],
            "title": "Full Time Series Zarr Store",
            "eo:bands": [{"name": _b} for _b in bands],
        }

    preview_href = _rel_to_item(record.get("preview_path"))
    if preview_href:
        item["assets"]["preview"] = {
            "href": preview_href,
            "type": "image/png",
            "roles": ["overview"],
            "title": "RGB Preview (VV / VH / Ratio)",
        }

    # Drop properties whose value is None. STAC extension schemas (e.g. the
    # `sat` extension) treat their fields as optional but type-constrained when
    # present, so a literal null (e.g. sat:absolute_orbit for a monthly
    # composite that aggregates multiple passes) fails validation. Omitting the
    # key is the STAC-conventional way to express "not applicable".
    item["properties"] = {
        _k: _v for _k, _v in item["properties"].items() if _v is not None
    }

    return item, item_abs


def write_stac_item(
    record: dict,
    output_root: str,
    polarization: str = "VV+VH",
    tile_dir_override: str = None,
    related_items: list[dict] | None = None,
) -> str:
    """
    Write a STAC Item JSON for one tile x time slice.

    Thin wrapper over :func:`build_stac_item_dict` that serializes the item to
    its own JSON file (the classic static-catalog layout). For the
    stac-geoparquet output, the item dict is collected and written to parquet
    instead — see ``stac_geoparquet_io.write_items_geoparquet``.

    Returns:
        Path to the written Item JSON file, or None if STAC output is disabled.
    """
    if not _STAC_OUTPUT_ENABLED:
        return None
    item, item_abs = build_stac_item_dict(
        record, output_root, polarization, tile_dir_override=tile_dir_override,
        related_items=related_items,
    )
    os.makedirs(os.path.dirname(item_abs), exist_ok=True)
    from s1grits.atomic_write import atomic_write_json
    atomic_write_json(item, item_abs)
    return item_abs


def write_stac_collection(
    catalog_path_or_df,
    output_root: str,
    collection_id: str,
    polarization: str = "VV+VH",
    items_parquet: str | None = None,
) -> str | None:
    """
    Write or overwrite the STAC Collection JSON at {output_root}/collection.json.

    Computes the global bbox and temporal extent by aggregating all catalog records.
    The spectral band list is resolved from actual COG files on disk so that GLCM
    texture bands are correctly reflected when texture features were enabled.

    Args:
        catalog_path_or_df: Path to catalog.parquet file, or a pandas DataFrame.
        output_root: Root output directory.
        polarization: SAR polarization mode used during processing ('VV+VH' or 'HH+HV').
        items_parquet: When set, the collection's items live in this
            stac-geoparquet file (path relative to the collection.json) instead
            of as per-item JSON files. The collection then references the parquet
            via an ``item_assets``-style asset + an ``items`` link, and does NOT
            embed one ``rel=item`` link per row — which is what keeps
            collection.json tiny at 10k+ items. When None (default), the classic
            per-item JSON links are written.

    Returns:
        Path to the written collection.json, or None if no records / STAC disabled.
    """
    if not _STAC_OUTPUT_ENABLED:
        return None
    if isinstance(catalog_path_or_df, (str, os.PathLike)):
        df = pd.read_parquet(catalog_path_or_df)
    else:
        df = catalog_path_or_df

    if df.empty:
        logger.warning("No records — collection.json not written.")
        return None

    # Global WGS84 bbox from preview_bounds or UTM reprojection
    bboxes = []
    for _, row in df.iterrows():
        pb = row.get("preview_bounds")
        if isinstance(pb, dict) and all(k in pb for k in ("left", "bottom", "right", "top")):
            bboxes.append([pb["left"], pb["bottom"], pb["right"], pb["top"]])
        else:
            try:
                bbox, _ = _utm_extent_to_wgs84(
                    list(row["transform"]), int(row["width"]), int(row["height"]), row["crs"]
                )
                bboxes.append(bbox)
            except Exception as _e:
                logger.debug("Skipping bbox computation for row: %s", _e)

    global_bbox = (
        [
            round(min(b[0] for b in bboxes), 6),
            round(min(b[1] for b in bboxes), 6),
            round(max(b[2] for b in bboxes), 6),
            round(max(b[3] for b in bboxes), 6),
        ]
        if bboxes
        else [-180, -90, 180, 90]
    )

    # Temporal extent (handle None/NaT for static products)
    _dt_col = df["datetime"].dropna()
    if len(_dt_col) > 0:
        try:
            # Convert to datetime with UTC awareness to handle tz-aware datetimes
            datetimes = pd.to_datetime(_dt_col, utc=True)
            # Strip timezone for STAC (use naive UTC timestamps)
            datetimes = datetimes.dt.tz_localize(None)
            # Only the start feeds the collection extent below; the temporal
            # interval is deliberately open-ended ([t_start, None]).
            t_start = datetimes.min().strftime("%Y-%m-%dT00:00:00Z")
        except Exception as dt_err:
            logger.debug("Could not compute temporal extent: %s", dt_err)
            t_start = None
    else:
        t_start = None

    # Resolve the authoritative band list from COG files.
    # Scan up to 5 records to find one whose COG is readable; use the longest
    # band list found (most likely to include GLCM bands).
    all_bands: list[str] = _band_values(polarization)  # safe fallback
    for _, row in df.head(5).iterrows():
        bands = _resolve_bands(row.to_dict(), output_root, polarization)
        if len(bands) > len(all_bands):
            all_bands = bands

    # Build band description strings for the collection description
    core_band_desc = (
        "VV_dB (dB), VH_dB (dB), Ratio = VH/VV (dimensionless, linear), "
        "RVI = 4*VH/(VV+VH) (dimensionless, range 0-4)"
    ) if polarization != "HH+HV" else (
        "HH_dB (dB), HV_dB (dB), Ratio = HV/HH (dimensionless, linear), "
        "RVI = 4*HV/(HH+HV) (dimensionless, range 0-4)"
    )
    glcm_bands = [b for b in all_bands if "glcm" in b.lower()]
    glcm_desc = (
        f" GLCM texture bands: {', '.join(glcm_bands)}."
        if glcm_bands else ""
    )

    # Summaries
    _tid_col = "tile_id" if "tile_id" in df.columns else "mgrs_tile_id"
    tiles = sorted(df[_tid_col].dropna().unique().tolist())
    directions = (
        sorted(df["flight_direction"].dropna().unique().tolist())
        if "flight_direction" in df.columns
        else []
    )
    has_output_type = "product_type" in df.columns
    output_types = (
        sorted(df["product_type"].dropna().unique().tolist())
        if has_output_type
        else ["monthly"]
    )

    # Item links (one per row) — honour the output_type subdirectory.
    # Skipped entirely in stac-geoparquet mode: a single parquet asset/link
    # replaces tens of thousands of per-item links.
    item_links = []
    if items_parquet is None:
        for _, row in df.iterrows():
            tile_id = row.get("tile_id") or row.get("mgrs_tile_id", "")
            fd = row.get("flight_direction")
            fs = f"_{fd}" if fd else ""
            ot = row.get("product_type") if has_output_type else None
            pl = row.get("product_label") or ""
            month = str(row.get("month", ""))
            # Construct item_id from canonical fields
            _item_id = row.get("item_id")
            if not _item_id:
                if ot == "scenes":
                    dt_val = row.get("datetime")
                    _item_id = f"{tile_id}{fs}_{pd.Timestamp(dt_val).strftime('%Y%m%dT%H%M%S')}" if dt_val else f"{tile_id}{fs}_{month}"
                elif ot in ("smonthly", "monthly"):
                    _item_id = f"{tile_id}{fs}_{month}"
                elif ot == "static":
                    _item_id = f"{tile_id}{fs}_static"
                else:
                    _item_id = f"{tile_id}{fs}_{month}"
            # Build href from canonical product_label (uses bare tile_id, no direction suffix)
            _subdir = pl if pl else (f"static_{fd}" if ot == "static" and fd else (ot or "items"))
            href = f"../../{tile_id}/items/{_subdir}/{_item_id}.json"

            item_links.append(
                {
                    "rel": "item",
                    "href": href,
                    "type": "application/geo+json",
                    "title": _item_id,
                }
            )

    # Dynamic collection title & description
    if "scenes" in output_types and "monthly" in output_types:
        coll_title = "S1-GRiTS: Sentinel-1 Scenes + Monthly Composite Time Series"
        coll_desc_base = (
            "Sentinel-1 SAR backscatter time series (per-acquisition scenes and "
            "monthly composites) processed from OPERA RTC-S1 products and gridded "
            "to MGRS tiles."
        )
    elif "scenes" in output_types:
        coll_title = "S1-GRiTS: Sentinel-1 Per-Scene Time Series"
        coll_desc_base = (
            "Sentinel-1 SAR backscatter per-acquisition scenes processed from "
            "OPERA RTC-S1 products and gridded to MGRS tiles."
        )
    else:
        coll_title = "S1-GRiTS: Sentinel-1 Monthly Composite Time Series"
        coll_desc_base = (
            "Monthly Sentinel-1 SAR backscatter composites processed from OPERA "
            "RTC-S1 products and gridded to MGRS tiles."
        )

    # A STAC Collection conveys its footprint via `extent` and per-field
    # `summaries`; the datacube/projection extensions (and cube:dimensions) live
    # on the Items, where they validate. The datacube v2.3.0 schema rejects
    # Collections (requires type=Feature), so we do NOT declare it here.
    collection = {
        "stac_version": STAC_VERSION,
        "stac_extensions": [],
        "type": "Collection",
        "id": collection_id,
        "title": coll_title,
        "description": (
            f"{coll_desc_base} "
            f"Tiles: {', '.join(tiles)}. "
            f"Bands: {core_band_desc}.{glcm_desc}"
        ),
        "license": "proprietary",
        "extent": {
            "spatial": {"bbox": [global_bbox]},
            "temporal": {"interval": [[t_start, None]]},
        },
        "summaries": {
            "platform": ["sentinel-1a", "sentinel-1b"],
            "instruments": ["c-sar"],
            "mgrs:tile_id": tiles,
            "s1:orbit_direction": [d.lower() for d in directions],
            "s1:band_count": [len(all_bands)],
            "s1:glcm_enabled": [len(glcm_bands) > 0],
            "s1:output_types": output_types,
        },
        "links": [
            {"rel": "self", "href": "./collection.json"},
            *item_links,
        ],
    }

    # stac-geoparquet mode: reference the items parquet instead of per-item links.
    if items_parquet is not None:
        _pq_href = f"./{items_parquet}" if not str(items_parquet).startswith(".") else str(items_parquet)
        collection["assets"] = {
            "items": {
                "href": _pq_href,
                "type": "application/vnd.apache.parquet",
                "roles": ["stac-items"],
                "title": "STAC items (stac-geoparquet)",
            }
        }
        collection["links"].append({
            "rel": "items",
            "href": _pq_href,
            "type": "application/vnd.apache.parquet",
            "title": "STAC items (stac-geoparquet)",
        })

    collection_dir = os.path.join(output_root, "collections", collection_id)
    os.makedirs(collection_dir, exist_ok=True)
    collection_path = os.path.join(collection_dir, "collection.json")
    from s1grits.atomic_write import atomic_write_json
    atomic_write_json(collection, collection_path)

    logger.info("Collection written: %s (%d items, %d bands)", collection_path, len(df), len(all_bands))
    return collection_path


def rebuild_stac_from_catalog(
    output_root: str,
    polarization: str = "VV+VH",
) -> None:
    """
    Rebuild all STAC Item JSONs and collection.json from the global catalog.parquet.

    Internal helper to rebuild STAC Items from catalog.parquet (used by the
    'catalog resync' path) after manual file changes.

    Args:
        output_root: Root output directory containing catalog.parquet.
        polarization: SAR polarization mode ('VV+VH' or 'HH+HV').
    """
    catalog_path = os.path.join(output_root, "catalog.parquet")
    if not os.path.exists(catalog_path):
        logger.warning("No catalog.parquet at: %s", catalog_path)
        return

    df = pd.read_parquet(catalog_path)
    logger.info("Rebuilding %d STAC Items...", len(df))

    ok = 0
    for _, row in df.iterrows():
        try:
            write_stac_item(row.to_dict(), output_root, polarization)
            ok += 1
        except Exception as e:
            logger.warning(
                "Item failed for %s %s: %s",
                row.get('mgrs_tile_id'), row.get('month'), e
            )

    logger.info("Items written: %d/%d", ok, len(df))
    _cid = df["collection_id"].iloc[0] if "collection_id" in df.columns and len(df) > 0 else "s1grits-monthly"
    write_stac_collection(df, output_root, _cid, polarization=polarization)
