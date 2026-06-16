"""
canonical_catalog_schema.py
===========================
Shared canonical schema (34 columns) for all S1-GRiTS catalog records.

ALL workflows must normalise their catalog records through
``normalize_catalog_record()`` before writing to catalog.parquet.
This ensures every per-tile and global catalog has identical columns,
regardless of which workflow (scenes, static, monthly) produced the data.

Constants:
    SCHEMA_VERSION      — bump when columns are added, removed, or renamed
    GRID_ID_VERSION     — bump when make_grid_id hash algorithm changes
    _GRID_ROUND_PRECISION — decimal places for transform/resolution rounding

Usage:
    from s1grits.canonical_catalog_schema import (
        CANONICAL_CATALOG_COLUMNS, SCHEMA_VERSION, GRID_ID_VERSION,
        normalize_catalog_record, filter_complete_records, make_grid_id,
    )

    record = normalize_catalog_record(my_dict, status="complete")
    catalog_records.append(record)
"""

import hashlib as _hashlib
import json as _json
from pathlib import Path
from typing import Any

import pandas as pd

from s1grits.logger_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Schema version — bump when columns are added, removed, or renamed
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 3
GRID_ID_VERSION = 1       # bump when make_grid_id hash algorithm changes
_GRID_ROUND_PRECISION = 9  # decimal places for transform/resolution rounding

# Collection ↔ Product Type Mapping (P1 optimization)
COLLECTION_PRODUCT_MAPPING = {
    's1grits-monthly': 'monthly',
    's1grits-scenes': 'scenes',
    's1grits-smonthly': 'smonthly',
    's1grits-static': 'static'
}

# ---------------------------------------------------------------------------
# Canonical column list (order is the write-order in parquet)
# ---------------------------------------------------------------------------
CANONICAL_CATALOG_COLUMNS: list[str] = [
    # ---- Identity ----
    "item_id",              # str: {TILE}_{DIR}_TK{tk}_N{nn}_{product} | null
    "collection_id",        # str: s1grits-scenes / s1grits-smonthly / s1grits-static / s1grits-monthly
    "product_type",         # str: scenes | smonthly | static | monthly
    "product_label",        # str: e.g. scenes_DESCENDING_tvbregman5_Ratio
    "schema_version",       # int: always SCHEMA_VERSION

    # ---- Spatial ----
    "tile_id",              # str: MGRS tile (e.g. "50RKV")
    "flight_direction",     # str: ASCENDING | DESCENDING | null
    "crs",                  # str: EPSG:xxxxx or WKT
    "transform",            # list[float] x 6: affine geotransform
    "width",                # int: pixels
    "height",               # int: pixels
    "grid_id",              # str: SHA-256 hash (machine join key)
    "grid_name",            # str: {TILE}_native_{res}m (human-readable)
    "grid_version",         # int: hash algorithm version
    "resolution_x",         # float: pixel width in CRS units
    "resolution_y",         # float: pixel height in CRS units

    # ---- Temporal ----
    "datetime",             # timestamp or null (static = null)
    "start_datetime",       # timestamp or null (static = null)
    "end_datetime",         # timestamp or null (static = null)
    "month",                # str: YYYY-MM or null (scenes = null)

    # ---- Grouping ----
    "geometry_group_id",    # str: {TILE}_{DIR}_TK{tk}_N{nn} | null
    "track",                # int or null (monthly = null)
    "n_bursts",             # int or null (monthly = null)
    "n_scenes",             # int or null (only smonthly/monthly)

    # ---- Assets ----
    "zarr_path",            # str: relative path from tile_dir, or null
    "cog_path",             # str: relative path or null
    "preview_path",         # str: relative path or null
    "item_path",            # str: relative path to STAC item JSON, or null

    # ---- Processing ----
    "bands",                # str: JSON array e.g. '["VV_dB","VH_dB","Ratio"]' or null
    "processing_level",     # str: hARDCp / ARDC / tvbregman5 / null
    "product_variant",      # str: human-readable variant e.g. "scenes_tvbregman5_Ratio"
    "processing_signature", # str: "p" + sha256[:10] hash of variant values
    "processing_variant_json",  # str: JSON dict of resolved variant field values

    # ---- Status ----
    "status",               # str: "complete" | "partial" | "empty" | "failed" | "deprecated"
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_catalog_record(record: dict[str, Any], status: str = "complete") -> dict[str, Any]:
    """
    Normalise any catalog record dict to the canonical schema.

    Missing keys are filled with ``None``.  ``schema_version`` and
    ``status`` are always overwritten with the canonical defaults.

    Status values: ``"complete"`` (default), ``"partial"``, ``"empty"``,
    ``"failed"``, ``"deprecated"``.

    Args:
        record: Raw catalog record from any workflow.
        status: One of the five canonical status values.

    Returns:
        dict with exactly the keys in ``CANONICAL_CATALOG_COLUMNS``.
    """
    out: dict[str, Any] = {}
    for col in CANONICAL_CATALOG_COLUMNS:
        out[col] = record.get(col, None)
    out["schema_version"] = SCHEMA_VERSION
    out["status"] = status

    # Auto-compute grid_id from spatial params if available
    _tid = out["tile_id"] or record.get("mgrs_tile_id")
    _crs = out["crs"]
    _tfm = out["transform"]
    _w = out["width"]
    _h = out["height"]
    if _tid and _crs and _tfm and _w and _h:
        _rx = abs(float(_tfm[0]))
        _ry = abs(float(_tfm[4])) if len(_tfm) >= 5 else _rx
        _computed = make_grid_id(
            str(_tid), str(_crs), list(_tfm), int(_w), int(_h),
            resolution=[_rx, _ry], version=GRID_ID_VERSION,
        )
        _existing = record.get("grid_id")
        if _existing and _existing != _computed:
            # Legacy Zarr stores used hardcoded strings (e.g. "17MQV_native_30m");
            # new records use SHA-256 hashes.  DEBUG-level only to avoid noise.
            logger.debug(
                "grid_id updated for %s: legacy=%s -> computed=%s",
                _tid, _existing, _computed,
            )
        out["grid_id"] = _computed
        out["grid_version"] = GRID_ID_VERSION
        out["resolution_x"] = round(_rx, _GRID_ROUND_PRECISION)
        out["resolution_y"] = round(_ry, _GRID_ROUND_PRECISION)
        out["grid_name"] = record.get("grid_name") or _format_grid_name(str(_tid), list(_tfm), str(_crs))
    else:
        _legacy_grid = record.get("grid_id")
        if _legacy_grid and not out.get("grid_name"):
            out["grid_name"] = str(_legacy_grid)

    # Ensure bands is always a JSON string
    if out["bands"] is not None and not isinstance(out["bands"], str):
        out["bands"] = _json.dumps(out["bands"])

    # Ensure processing_variant_json is always a JSON string
    _pvj = out.get("processing_variant_json")
    if _pvj is not None and not isinstance(_pvj, str):
        out["processing_variant_json"] = _json.dumps(_pvj, sort_keys=True, ensure_ascii=False)

    # Auto-compute item_path if not provided (for monthly workflow)
    if not out.get("item_path"):
        _item_id = out.get("item_id")
        _product_label = out.get("product_label")
        _tile_id = out.get("tile_id")

        if _tile_id and _product_label and _item_id:
            out["item_path"] = f"items/{_product_label}/{_item_id}.json"
            logger.debug(
                "Auto-computed item_path for item_id=%s: %s",
                _item_id, out["item_path"],
            )
        elif not _item_id:
            logger.debug(
                "Cannot auto-compute item_path: item_id is None "
                "(product_type=%s, tile_id=%s). "
                "Workflows with product-type-specific item generation "
                "should compute this manually.",
                out.get("product_type"), _tile_id,
            )

    return out


# ---------------------------------------------------------------------------
# Grid name formatting
# ---------------------------------------------------------------------------

def _guess_unit(crs_str: str) -> str:
    """
    Guess the unit of measurement for a CRS.

    Projected CRS (UTM, Web Mercator, etc.) → "m"
    Geographic CRS (WGS84, NAD83, etc.)      → "deg"

    Args:
        crs_str: Any pyproj-recognised CRS string.

    Returns:
        ``"m"`` or ``"deg"``.
    """
    try:
        import pyproj
        _obj = pyproj.CRS.from_user_input(crs_str)
        return "deg" if _obj.is_geographic else "m"
    except Exception:
        return "m"


def _format_grid_name(tile_id: str, transform: list[float], crs_str: str) -> str:
    """
    Build a human-readable grid name with CRS-aware unit suffix.

    Examples:
        UTM 30m:   ``50RKV_native_30m``
        UTM 10m:   ``50RKV_native_10m``
        WGS84:     ``50RKV_native_0.00025deg``
        Non-square: ``50RKV_native_30x20m``
    """
    unit = _guess_unit(crs_str)
    rx = abs(float(transform[0]))
    ry = abs(float(transform[4])) if len(transform) >= 5 else rx
    if abs(rx - ry) < 1e-9:
        suffix = f"{rx:g}{unit}"
    else:
        suffix = f"{rx:g}x{ry:g}{unit}"
    return f"{tile_id}_native_{suffix}"


# ---------------------------------------------------------------------------
# Grid identity helper
# ---------------------------------------------------------------------------

def make_grid_id(
    tile_id: str,
    crs: str,
    transform: list[float],
    width: int,
    height: int,
    resolution: float | list[float] | None = None,
    version: int = 1,
) -> str:
    """
    Generate a stable, deterministic grid identity from spatial parameters.

    Normalises CRS to authority form (EPSG:xxxxx), rounds transform values
    to 9 decimal places, and produces a JSON-sorted SHA-256 payload.
    Different CRS representations (e.g. EPSG:32618, 32618, PROJ string)
    that refer to the same CRS will produce the same hash.

    The hash describes the SPATIAL grid only — not the product type.
    Same grid (tile, CRS, transform, width, height) = same grid_id,
    regardless of whether the data is scenes, static, or monthly.

    Args:
        tile_id: MGRS tile (e.g. "50RKV")
        crs: Any pyproj-recognised CRS string
        transform: Affine geotransform [a, b, c, d, e, f]
        width, height: Grid dimensions in pixels
        resolution: Optional explicit resolution(s). If None, derived from transform[0].
        version: Hash algorithm version (bump if rules change)

    Returns:
        12-character hex string (48 bits)
    """
    from pyproj import CRS as _CRS

    _crs_obj = _CRS.from_user_input(crs)
    _auth = _crs_obj.to_authority()
    if _auth:
        _crs_norm = f"{_auth[0]}:{_auth[1]}"
    else:
        # Normalise WKT: strip whitespace, uppercase
        _crs_norm = "".join(_crs_obj.to_wkt().split()).upper()

    # Force Python native float (not numpy.float64) for JSON serialisation
    _t_norm = [round(float(v), _GRID_ROUND_PRECISION) for v in transform]

    _payload: dict = {
        "v": version,
        "tile": str(tile_id).upper(),
        "crs": _crs_norm,
        "transform": [float(v) for v in _t_norm],
        "w": int(width),
        "h": int(height),
    }

    if resolution is not None:
        if isinstance(resolution, (list, tuple)):
            _payload["res"] = [round(float(r), _GRID_ROUND_PRECISION) for r in resolution]
        else:
            _payload["res"] = round(float(resolution), _GRID_ROUND_PRECISION)

    _text = _json.dumps(_payload, sort_keys=True, separators=(",", ":"))
    return _hashlib.sha256(_text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Resolver helpers
# ---------------------------------------------------------------------------

def filter_complete_records(
    catalog: str | Path | pd.DataFrame,
    schema_version: int = SCHEMA_VERSION,
    require_grid: bool = False,
) -> pd.DataFrame:
    """
    Return only records that are complete and schema-compatible.

    Filters for ``status == 'complete'`` and ``schema_version == schema_version``.
    When ``require_grid=True``, additionally excludes records with null ``grid_id``.

    Accepts a path to a parquet file or an existing DataFrame.
    """
    df = pd.read_parquet(catalog) if isinstance(catalog, (str, Path)) else catalog.copy()
    if "status" not in df.columns or "schema_version" not in df.columns:
        return df  # legacy catalog — return as-is
    mask = (df["status"] == "complete") & (df["schema_version"] == schema_version)
    if require_grid and "grid_id" in df.columns:
        mask &= df["grid_id"].notna()
    return df[mask].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Virtual index — cross-CRS product stacking
# ---------------------------------------------------------------------------

def find_virtual_stack(
    df: pd.DataFrame,
    tile_id: str,
    target_grid_id: str | None = None,
    require_same_processing: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Return products that can be directly stacked AND those that can be
    virtually indexed for a given tile.

    Direct stack:   same tile_id + same grid_id (no reprojection needed)
    Virtual index:  same tile_id + different grid_id (may need reprojection)

    When require_same_processing=True, raises ValueError if the direct-stack
    group contains multiple processing_signatures for the same product_type.

    Args:
        df: Catalog DataFrame (pre-filtered via ``filter_complete_records``).
        tile_id: Target MGRS tile (e.g. ``"50RKV"``).
        target_grid_id: Optional hash to filter the direct-stack group.
        require_same_processing: If True, enforce uniform processing_signature
            within each product_type in the direct-stack group.

    Returns:
        ``(same_grid, virtual)`` tuple of DataFrames.
        ``same_grid`` contains products that share ``target_grid_id``
        (or all non-null grid_id if ``target_grid_id`` is None).
        ``virtual`` contains products on the same tile but different grid_id.

    Raises:
        ValueError: If require_same_processing=True and multiple signatures found.
    """
    _same_tile = df[df["tile_id"] == tile_id]
    if _same_tile.empty:
        return (_same_tile, _same_tile)

    if target_grid_id:
        _same_grid = _same_tile[_same_tile["grid_id"] == target_grid_id]
        _virtual = _same_tile[_same_tile["grid_id"] != target_grid_id]
    else:
        _same_grid = _same_tile[_same_tile["grid_id"].notna()]
        _virtual = pd.DataFrame()

    # Enforce processing_signature consistency
    if require_same_processing and "processing_signature" in _same_grid.columns:
        for pt in _same_grid["product_type"].dropna().unique():
            _pt_df = _same_grid[_same_grid["product_type"] == pt]
            _sigs = _pt_df["processing_signature"].dropna().unique()
            if len(_sigs) > 1:
                raise ValueError(
                    f"require_same_processing=True but product_type '{pt}' "
                    f"on tile '{tile_id}' has {len(_sigs)} distinct "
                    f"processing_signatures: {list(_sigs)}. "
                    f"Filter by processing_signature before stacking."
                )

    return (_same_grid.reset_index(drop=True), _virtual.reset_index(drop=True))


# ---------------------------------------------------------------------------
# Collection ID validation
# ---------------------------------------------------------------------------

def validate_collection_mapping(
    df: pd.DataFrame,
    raise_on_error: bool = True,
) -> list[tuple[str, str, list[str]]]:
    """
    Validate product_type <-> collection_id mapping consistency.

    Checks that all records with a given product_type use the correct
    collection_id according to COLLECTION_PRODUCT_MAPPING.

    Args:
        df: Catalog DataFrame with 'product_type' and 'collection_id' columns.
        raise_on_error: If True, raise ValueError on first violation.
                       If False, collect all errors and return list.

    Returns:
        List of error tuples: [(product_type, expected_cid, [actual_cids]), ...]
        Empty list if no violations found.

    Raises:
        ValueError: If raise_on_error=True and violations are detected.
    """
    if "product_type" not in df.columns or "collection_id" not in df.columns:
        return []

    errors: list[tuple[str, str, list[str]]] = []

    # Build reverse mapping: product_type -> collection_id
    reverse_map = {v: k for k, v in COLLECTION_PRODUCT_MAPPING.items()}

    for product_type, expected_cid in reverse_map.items():
        subset = df[df["product_type"] == product_type]
        if subset.empty:
            continue

        # Find records with wrong collection_id
        wrong = subset[subset["collection_id"] != expected_cid]
        if not wrong.empty:
            bad_cids = sorted(wrong["collection_id"].dropna().unique().tolist())
            errors.append((product_type, expected_cid, bad_cids))

            if raise_on_error:
                raise ValueError(
                    f"Collection mapping violation detected:\n"
                    f"  product_type='{product_type}' expects collection_id='{expected_cid}'\n"
                    f"  but found: {bad_cids}\n"
                    f"  Affected rows: {len(wrong)}"
                )

    if not raise_on_error:
        for product_type, expected_cid, bad_cids in errors:
            logger.warning(
                "Collection mapping violation: product_type='%s' expected '%s' but found %s",
                product_type, expected_cid, bad_cids,
            )

    return errors
