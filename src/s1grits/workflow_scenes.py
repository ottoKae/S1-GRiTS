"""
workflow_scenes.py
==================
Scenes-first SAR workflow for S1-GRiTS.

Produces per-track-acquisition-group scene Zarr stores (always acq_group mode).
Optional per-track monthly composites controlled by processing.monthly.enabled.
Writes directly to config.output.base_dir (no suffix appended).

Output structure:
    {base_dir}/
      catalog.json               # STAC Catalog (root, links to collections)
      catalog.parquet            # Global index (canonical 30-column schema)
      collections/               # One STAC Collection per product type
        s1grits-scenes/collection.json
        s1grits-smonthly/collection.json
      {TILE}/
        catalog.parquet
        scenes_{DIR}_{despeckle}_{bands}/
          zarr/s1grits_scenes_{TILE}_{DIR}_TK{tk}_N{nn}.zarr
          cog/s1grits_scenes_{TILE}_{DIR}_TK{tk}_N{nn}_{DT}.tif
          preview/s1grits_scenes_{TILE}_{DIR}_TK{tk}_N{nn}_{DT}.png
        smonthly_{DIR}_{bands}/
          zarr/s1grits_smonthly_{TILE}_{DIR}_monthly.zarr
          cog/s1grits_smonthly_{TILE}_{DIR}_{YYYY-MM}.tif
          preview/s1grits_smonthly_{TILE}_{DIR}_{YYYY-MM}.png
        items/{product_label}/{id}.json

CLI entry point: s1grits process_scenes --config config.yaml
"""

import gc
import warnings
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyproj
import rasterio
import zarr
import yaml
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import Affine
from rich.console import Console
from shapely import wkt as shapely_wkt
from shapely.geometry import mapping
from shapely.ops import transform as shp_transform

from s1grits.logger_config import get_logger
from s1grits.workflow import (
    load_config,
    enumerate_mgrs_tiles,
    query_rtc_metadata_for_tile,
)
from s1grits.time_utils import parse_time_range_config
from s1grits.adapters import (
    adapt_enumerator_to_distmetrics,
    filter_by_flight_direction,
    validate_url_pairs,
)
from s1grits.asf_io import get_band_names, load_and_despeckle_rtc_strict, _mgrs_to_utm_epsg
from s1grits.memory_manager import get_memory_strategy_from_config, chunk_time_by_strategy
from s1grits.asf_output_writing import _build_grid_from_bursts, _mosaic_align, _generate_preview_png, _get_mgrs_tile_geometry_wkt, _clip_arrays_to_wkt_4326, _check_tile_integrity, _zarr_delete_timestep
from s1grits.stac_builder import (
    _utm_extent_to_wgs84,
    _epsg_int,
    _resolve_bands,
    STAC_VERSION,
    ITEM_STAC_EXTENSION_URIS,
)
from s1grits.canonical_catalog_schema import (
    normalize_catalog_record,
    validate_collection_mapping,
)
from s1grits.product_instance import (
    resolve_variant_values, make_processing_signature,
    make_product_variant, derive_actual_bands,
)
from s1grits.product_registry import load_product_registry
from s1grits.file_lock import output_lock, acquire_lock, release_lock

logger = get_logger(__name__)
console = Console(legacy_windows=True, no_color=False)


def _burst_coverage_status(loaded: int, expected: int, date_label: str, dt_str: str):
    """Assess per-acquisition burst coverage.

    ``expected`` is the burst count from ASF metadata; ``loaded`` is how many
    bursts actually came through and will be mosaicked. A shortfall leaves a
    NoData gap in the COG/Zarr (commonly a 404 — not yet published on ASF for
    very recent acquisitions, or transient S3 — or a network drop).

    Returns ``(missing_count, message)`` when coverage is incomplete, else
    ``(0, None)``. Pure / side-effect-free so it can be unit-tested.
    """
    if loaded >= expected:
        return 0, None
    missing = expected - loaded
    msg = (
        f"[Coverage] Scene {date_label} ({dt_str}): only {loaded}/{expected} "
        f"expected burst(s) mosaicked — {missing} missing (likely 404/not-yet-"
        f"published on ASF or a network drop); this acquisition's COG/Zarr will "
        f"have a NoData gap."
    )
    return missing, msg


def _interior_hole_fraction(valid_mask: np.ndarray, tile_mask: np.ndarray | None = None) -> float:
    """Fraction of the tile that is an *interior* NoData hole — NoData fully
    enclosed by valid data, as opposed to NoData at the swath/tile edge.

    Edge truncation (a data-take ending a burst or two short along-track) leaves
    NoData connected to the array/tile border and is NORMAL; an interior hole is
    a missing burst surrounded by data and is the real defect. binary_fill_holes
    fills only fully-enclosed NoData regions, so the filled-minus-valid set is
    exactly the interior holes.

    ``valid_mask``: True where data is finite. ``tile_mask``: True inside the
    MGRS tile (restricts the measure to the tile interior). Pure / testable.
    """
    if valid_mask is None or not valid_mask.any():
        return 0.0
    try:
        from scipy.ndimage import binary_fill_holes
    except Exception:
        return 0.0
    filled = binary_fill_holes(valid_mask)
    interior = filled & (~valid_mask)
    if tile_mask is not None:
        interior = interior & tile_mask
        denom = int(tile_mask.sum())
    else:
        denom = int(valid_mask.size)
    return float(int(interior.sum()) / denom) if denom else 0.0


def _burst_subswath_index(bid: str) -> tuple[str, int]:
    """Parse an OPERA burst id like 'T011-021605-IW1' (or 'T011_021605_IW1')
    into ('IW1', 21605) — the sub-swath and its along-track burst index."""
    s = str(bid).replace('_', '-').upper()
    parts = s.split('-')
    iw = next((p for p in parts if p.startswith('IW')), '')
    nums = [int(p) for p in parts if p.isdigit()]
    return iw, (nums[-1] if nums else -1)


def _missing_interior_bursts(footprint_ids, present_ids) -> list[str]:
    """Burst ids that are in the track footprint but missing from this
    acquisition AND fall BETWEEN present bursts along-track within their
    sub-swath — i.e. a real interior gap (a missing burst with neighbours on
    both along-track sides), as opposed to missing at the along-track ends
    (normal edge truncation, which is kept).

    This is metadata-only and catches gaps that the raster fill-holes test
    misses (a full-width along-track segment, or an edge sub-swath, both connect
    to the swath border and are not "enclosed"). Pure / testable.
    """
    from collections import defaultdict
    present = {str(b) for b in present_ids}
    missing = {str(b) for b in footprint_ids} - present
    if not missing:
        return []
    pres_idx: dict[str, list[int]] = defaultdict(list)
    for b in present:
        iw, idx = _burst_subswath_index(b)
        if idx >= 0:
            pres_idx[iw].append(idx)
    interior = []
    for b in missing:
        iw, idx = _burst_subswath_index(b)
        ps = pres_idx.get(iw)
        if ps and min(ps) < idx < max(ps):
            interior.append(b)
    return sorted(interior)


def _load_footprint_cache(cache_path, key: dict, ttl_days: float):
    """Return the cached per-track footprint ({token: [burst_ids]}) if the cache
    file exists, matches ``key`` exactly, and is younger than ``ttl_days`` —
    otherwise None. Never raises; a corrupt/missing cache simply misses. Pure
    apart from reading the file's mtime, so it is unit-testable."""
    try:
        if not cache_path.exists():
            return None
        import time as _time
        age_days = (_time.time() - cache_path.stat().st_mtime) / 86400.0
        if ttl_days > 0 and age_days > ttl_days:
            return None
        data = json.loads(cache_path.read_text())
        if data.get('key') != key:
            return None
        return data.get('footprint', {})
    except Exception:
        return None


def _save_footprint_cache(cache_path, key: dict, footprint: dict) -> bool:
    """Persist the look-back footprint keyed by ``key``. Returns True on success.
    Never raises (a cache is best-effort)."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({'key': key, 'footprint': footprint}))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helpers (adapted from workflow_ablation.py lines 77-179)
# ---------------------------------------------------------------------------

def _linear_to_db(arr: np.ndarray) -> np.ndarray:
    """Convert linear power to dB (10*log10). OPERA RTC-S1 stores power values. Values <= 0 become NaN."""
    out = np.full_like(arr, np.nan, dtype=np.float32)
    valid = arr > 0
    out[valid] = (10.0 * np.log10(arr[valid])).astype(np.float32)
    return out


def _group_indices_by_period(dates: list, composite_mode: str) -> dict:
    """
    Group flat burst indices by composite period.
    composite_mode: 'monthly' -> key = 'YYYY-MM'
    Returns {period_key: [burst_index, ...]}
    """
    groups: dict[str, list[int]] = {}
    for i, dt in enumerate(dates):
        ts = pd.Timestamp(dt).tz_convert('UTC')
        key = ts.strftime('%Y-%m')
        groups.setdefault(key, []).append(i)
    return groups


def _init_zarr_2band(
    zarr_path: Path,
    x_coords: np.ndarray,
    y_coords: np.ndarray,
    target_crs: str,
    transform: Affine,
    chunk_y: int,
    chunk_x: int,
    processing_level: str,
    band_names: list[str] = None,
) -> zarr.Group:
    """
    Create (or open) a Zarr store with variable band support.
    Dimensions: time(0,), y(H,), x(W,).

    Band names default to ["VV_dB", "VH_dB"] for backward compatibility.
    Additional bands (Ratio, RVI, GLCM) are created when specified.
    """
    if band_names is None:
        band_names = ["VV_dB", "VH_dB"]
    zarr_path.parent.mkdir(parents=True, exist_ok=True)

    if zarr_path.exists():
        try:
            g_check = zarr.open_group(str(zarr_path), mode='r', zarr_format=3)
            z_h = g_check['y'].shape[0]
            z_w = g_check['x'].shape[0]

            H, W = len(y_coords), len(x_coords)
            if H != z_h or W != z_w:
                raise ValueError(
                    f"Grid mismatch for {zarr_path}: "
                    f"existing=({z_h},{z_w}) new=({H},{W}). "
                    f"Delete the Zarr store or use a matching grid to re-run."
                )
            g = zarr.open_group(str(zarr_path), mode='r+', zarr_format=3)
            logger.info("[Zarr] Opened existing store (%d time steps), grid locked %dx%d",
                        g['time'].shape[0], z_w, z_h)
            # Validate that all expected band datasets exist
            if band_names:
                _missing = [b for b in band_names if b not in g]
                if _missing:
                    raise RuntimeError(
                        f"Cannot resume: existing Zarr at {zarr_path} is missing "
                        f"band(s): {_missing}. Delete the store or use a different "
                        f"output directory to add new bands."
                    )
            return g
        except (KeyError, ValueError) as e:
            raise RuntimeError(
                f"Cannot resume: existing Zarr at {zarr_path} is incompatible. "
                f"Delete it or use a new output directory. Detail: {e}"
            ) from e

    # Fresh store
    g = zarr.open_group(str(zarr_path), mode='w', zarr_format=3)
    g.attrs['crs'] = str(target_crs)
    g.attrs['transform'] = list(transform)[:6]
    g.attrs['processing_level'] = processing_level
    # Grid identity and metadata for cross-product alignment
    from s1grits.canonical_catalog_schema import make_grid_id, _format_grid_name
    _tile_from_path = zarr_path.parent.parent.parent.name if zarr_path.parent.parent.parent.name else 'UNKNOWN'
    _tfm = list(transform)[:6]
    _w, _h = len(x_coords), len(y_coords)
    _gid = make_grid_id(_tile_from_path, str(target_crs), _tfm, _w, _h)
    g.attrs['grid_id'] = _gid
    g.attrs['grid_name'] = _format_grid_name(_tile_from_path, _tfm, str(target_crs))
    g.attrs['grid_version'] = 1
    g.attrs['width'] = _w
    g.attrs['height'] = _h
    g.attrs['resolution'] = [float(abs(_tfm[0])), float(abs(_tfm[4]))]
    g.attrs['product_type'] = 'scenes'  # overridden by smonthly variant
    g.attrs['geometry_group_id'] = None  # set by caller
    g.attrs['time_varying'] = True
    g.attrs['array_dims'] = ['time', 'y', 'x']
    _a = g.create_array('x', data=x_coords, overwrite=True, dimension_names=['x'])
    _a.attrs['_ARRAY_DIMENSIONS'] = ['x']
    _a = g.create_array('y', data=y_coords, overwrite=True, dimension_names=['y'])
    _a.attrs['_ARRAY_DIMENSIONS'] = ['y']
    _a = g.create_array('time', shape=(0,), chunks=(1,), dtype='datetime64[ns]', overwrite=True, dimension_names=['time'])
    _a.attrs['_ARRAY_DIMENSIONS'] = ['time']
    for var in band_names:
        _a = g.create_array(var, shape=(0, _h, _w), chunks=(1, chunk_y, chunk_x), dtype='float32', overwrite=True, dimension_names=['time', 'y', 'x'])
        _a.attrs['_ARRAY_DIMENSIONS'] = ['time', 'y', 'x']

    # CF-compliant grid_mapping so generic readers (GDAL/QGIS/rioxarray) can
    # auto-detect the CRS, not just our own code via g.attrs['crs'].
    from s1grits.zarr_cf import add_cf_grid_mapping
    add_cf_grid_mapping(g, str(target_crs))

    return g


def _zarr_append(g: "zarr.Group", var_name: str, data: "np.ndarray") -> None:
    """Append one slice along axis-0 to a zarr v3 array (v3 has no .append())."""
    arr = g[var_name]
    t = arr.shape[0]
    arr.resize((t + 1,) + arr.shape[1:])
    arr[t, ...] = data


def _append_zarr_timestep(
    g: zarr.Group,
    dt_ns: np.datetime64,
    band_arrays: list[tuple[str, np.ndarray]],
) -> None:
    """Append one time step to an open Zarr group with variable bands.

    Raises ValueError if dt_ns already exists (duplicate time step)
    or if any band array shape does not match the existing dataset.
    """
    # ---- Time dedup (int64 ns comparison for cross-platform stability) ----
    _existing = set(g['time'][:].tolist()) if g['time'].shape[0] > 0 else set()
    _new_key = np.datetime64(dt_ns, 'ns').astype('int64')
    if _new_key in _existing:
        _ts = pd.Timestamp(dt_ns).strftime('%Y-%m-%dT%H:%M:%S')
        raise ValueError(
            f"Duplicate time step {_ts} — already exists in Zarr store. "
            f"Use overwrite mode or delete the store to re-process."
        )

    # ---- Validate every band shape BEFORE mutating the store, so a mismatch
    # cannot leave an orphaned time step (time length > data length). ----
    for var, arr in band_arrays:
        _dset = g[var]
        _expected = (_dset.shape[1], _dset.shape[2]) if _dset.ndim == 3 else _dset.shape
        if len(_dset.shape) >= 2 and arr.shape != _expected:
            raise ValueError(
                f"Shape mismatch for '{var}': got {arr.shape}, "
                f"expected {_expected}. Grid may have changed between runs."
            )

    # Append band data first, then the time coordinate last: an interrupted
    # band write then cannot orphan a time step.
    for var, arr in band_arrays:
        _zarr_append(g, var, arr.astype(np.float32))
    _zarr_append(g, 'time', np.array([_new_key], dtype='int64'))


# ---------------------------------------------------------------------------
# Processing level
# ---------------------------------------------------------------------------

def _get_despeckle_token(config: dict) -> str:
    """Return despeckle suffix token from config.

    Returns empty string if spatial_despeckle is False.
    Returns the method+reg_param token (e.g. '_tvbregman5') if True.

    The token is derived from processing.despeckle.method and
    processing.despeckle.kwargs.reg_param.
    """
    proc = config.get('processing', {})
    if not proc.get('spatial_despeckle', False):
        return ""

    despeckle_cfg = proc.get('despeckle', {})
    method = despeckle_cfg.get('method', 'tv_bregman')
    kwargs = despeckle_cfg.get('kwargs', {})
    reg_param = kwargs.get('reg_param', 5.0)

    # Sanitise method name: lowercase, remove underscores
    method_clean = method.replace('_', '').lower()
    # Format reg_param: replace '.' with '_' for filesystem safety
    # (5.0 → '5', 3.5 → '3_5')
    if isinstance(reg_param, float) and reg_param == int(reg_param):
        reg_param_str = str(int(reg_param))
    else:
        reg_param_str = str(reg_param).replace('.', '_')

    return f"_{method_clean}{reg_param_str}"


# ---------------------------------------------------------------------------
# COG writer
# ---------------------------------------------------------------------------

def _write_multiband_cog(
    cog_path: Path,
    bands: list[tuple[str, np.ndarray]],
    profile: dict,
    *,
    overview_resampling: str = "average",
) -> None:
    """
    Write a tiled, internally-overviewed multi-band GeoTIFF.

    The STAC items advertise these assets as
    ``profile=cloud-optimized``; that claim only holds if the file is both
    internally tiled (already in ``profile``) and carries internal overviews.
    This helper writes the bands, their descriptions, and a pyramid of
    decimated overviews so the advertised profile is accurate.

    Overview factors are chosen so the smallest level stays >= 128 px on its
    shorter side; tiny rasters simply get no overviews.

    The file is written atomically (temp + rename) so an interrupted write never
    leaves a partial/corrupt COG that the catalog might point at.
    """
    from s1grits.atomic_write import atomic_path
    with atomic_path(cog_path) as _cog_tmp:
        with rasterio.open(_cog_tmp, "w", **profile) as dst:
            for _i, (_name, _arr) in enumerate(bands, 1):
                dst.write(_arr, _i)
                dst.set_band_description(_i, _name)

            _short = min(int(profile["width"]), int(profile["height"]))
            _factors = [f for f in (2, 4, 8, 16, 32) if _short // f >= 128]
            if _factors:
                dst.build_overviews(_factors, Resampling[overview_resampling])
                dst.update_tags(ns="rio_overview", resampling=overview_resampling)


# ---------------------------------------------------------------------------
# STAC Item writers (scene and monthly variants)
# ---------------------------------------------------------------------------

def _stac_extensions() -> list[str]:
    """STAC extension URIs shared by every item this module emits."""
    return list(ITEM_STAC_EXTENSION_URIS)


def _resolve_item_bands(
    cog_relpath: str | None, tile_dir: Path, polarization: str
) -> list[str]:
    """Band names for an item: read from the COG when present, else the default pair."""
    bands = ["VV_dB", "VH_dB"]
    if cog_relpath:
        try:
            cog_abs = (
                str(tile_dir / cog_relpath)
                if not os.path.isabs(cog_relpath)
                else cog_relpath
            )
            if os.path.exists(cog_abs):
                bands = _resolve_bands(
                    {"cog_path": cog_relpath}, str(tile_dir), polarization
                )
        except Exception:
            pass
    return bands


def _build_stac_item(
    *,
    mgrs_tile_id: str,
    direction_label: str,
    item_id: str,
    datetime_str: str,
    time_step: str,
    collection_id: str,
    cog_title: str,
    tile_dir: Path,
    transform: Affine,
    width: int,
    height: int,
    target_crs: str,
    processing_level: str,
    pass_id: int | None,
    track: int | None,
    jpl_burst_ids: list[str] | None,
    opera_ids: list[str] | None,
    variant_properties: dict,
    cog_relpath: str | None,
    zarr_relpath: str | None,
    preview_relpath: str | None,
    product_label: str,
    polarization: str,
) -> str:
    """Build and atomically write one STAC Item JSON; return the item ID.

    Shared by the scene and monthly writers. Everything that differs between the
    two variants is passed in: identity (``item_id``/``datetime_str``), the cube
    ``time_step`` (P1D vs P1M), the ``collection_id``, the COG asset ``cog_title``
    and the ``variant_properties`` merged into ``properties``.
    """
    bbox, geometry = _utm_extent_to_wgs84(
        list(transform)[:6], width, height, target_crs
    )
    x_min = transform[2]
    x_max = transform[2] + transform[0] * width
    y_max = transform[5]
    y_min = transform[5] + transform[4] * height
    pixel_size = abs(transform[0])
    epsg = _epsg_int(target_crs)
    tform_9 = list(transform)[:9]
    bands = _resolve_item_bands(cog_relpath, tile_dir, polarization)

    item = {
        "stac_version": STAC_VERSION,
        "stac_extensions": _stac_extensions(),
        "type": "Feature",
        "id": item_id,
        "geometry": geometry,
        "bbox": bbox,
        "properties": {
            "datetime": datetime_str,
            "platform": "sentinel-1",
            "instruments": ["c-sar"],
            "mgrs:tile_id": mgrs_tile_id,
            "s1:orbit_direction": direction_label.lower(),
            "s1:processing_level": processing_level,
            "sat:orbit_state": direction_label.lower(),
            "sat:absolute_orbit": pass_id,
            "sat:relative_orbit": track,
            "s1grits:jpl_burst_ids": jpl_burst_ids,
            "s1grits:opera_ids": opera_ids,
            **variant_properties,
            "proj:epsg": epsg,
            "proj:shape": [height, width],
            "proj:transform": tform_9,
            "proj:geometry": geometry,
            "proj:bbox": bbox,
            "cube:dimensions": {
                "x": {
                    "type": "spatial", "axis": "x",
                    "extent": [round(x_min, 3), round(x_max, 3)],
                    "step": pixel_size, "reference_system": epsg,
                },
                "y": {
                    "type": "spatial", "axis": "y",
                    "extent": [round(y_min, 3), round(y_max, 3)],
                    "step": pixel_size, "reference_system": epsg,
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
        "collection": collection_id,
        "assets": {},
        "links": [
            {"rel": "self", "href": f"./{item_id}.json"},
            {"rel": "collection", "href": f"../../../../collections/{collection_id}/collection.json"},
            {"rel": "root", "href": "../../../../catalog.json"},
        ],
    }

    # Asset hrefs are computed relative to the item JSON location.
    items_dir = tile_dir / "items" / product_label
    _rel = lambda p: os.path.relpath(str(tile_dir / p), str(items_dir)).replace("\\", "/") if p else None

    if cog_relpath:
        item["assets"]["cog"] = {
            "href": _rel(cog_relpath),
            "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            "roles": ["data"],
            "title": cog_title,
            "eo:bands": [{"name": _b} for _b in bands],
        }
    if zarr_relpath:
        item["assets"]["zarr"] = {
            "href": _rel(zarr_relpath),
            "type": "application/vnd.zarr; version=3",
            "roles": ["data"],
            "title": "Full Time Series Zarr Store",
        }
    if preview_relpath:
        item["assets"]["preview"] = {
            "href": _rel(preview_relpath),
            "type": "image/png",
            "roles": ["overview"],
            "title": "RGB Preview (VH / VV / Ratio)",
        }

    items_dir.mkdir(parents=True, exist_ok=True)
    item_path = items_dir / f"{item_id}.json"
    from s1grits.atomic_write import atomic_write_json
    atomic_write_json(item, item_path)
    return item_id


def _write_scene_stac_item(
    mgrs_tile_id: str,
    direction_label: str,
    acq_ts: pd.Timestamp,
    tile_dir: Path,
    transform: Affine,
    width: int,
    height: int,
    target_crs: str,
    cog_relpath: str | None,
    zarr_relpath: str,
    preview_relpath: str | None = None,
    processing_level: str = "hARDCp",
    polarization: str = "VV+VH",
    product_label: str = "scenes",
    product_variant: str | None = None,
    processing_signature: str | None = None,
    processing_variant_json: str | None = None,
    actual_bands: list[str] | None = None,
    jpl_burst_ids: list[str] | None = None,
    opera_ids: list[str] | None = None,
    pass_id: int | None = None,
    track: int | None = None,
) -> str:
    """
    Write a STAC Item JSON for a single acquisition (per-pass scene).

    Returns the item ID. No-op (returns None) when STAC output is disabled
    (catalog-only build).
    """
    from s1grits.stac_builder import stac_output_enabled
    if not stac_output_enabled():
        return None
    dt_str = acq_ts.strftime('%Y%m%dT%H%M%S')
    item_id = f"{mgrs_tile_id}_{direction_label}_{dt_str}"
    datetime_str = acq_ts.strftime('%Y-%m-%dT%H:%M:%SZ')

    variant_properties = {
        "s1grits:product_variant": product_variant,
        "s1grits:processing_signature": processing_signature,
        "s1grits:processing_variant": (
            json.loads(processing_variant_json)
            if processing_variant_json and isinstance(processing_variant_json, str)
            else None
        ),
        "s1grits:bands": actual_bands,
    }

    item_id = _build_stac_item(
        mgrs_tile_id=mgrs_tile_id,
        direction_label=direction_label,
        item_id=item_id,
        datetime_str=datetime_str,
        time_step="P1D",
        collection_id="s1grits-scenes",
        cog_title="Per-Acquisition Scene COG",
        tile_dir=tile_dir,
        transform=transform,
        width=width,
        height=height,
        target_crs=target_crs,
        processing_level=processing_level,
        pass_id=pass_id,
        track=track,
        jpl_burst_ids=jpl_burst_ids,
        opera_ids=opera_ids,
        variant_properties=variant_properties,
        cog_relpath=cog_relpath,
        zarr_relpath=zarr_relpath,
        preview_relpath=preview_relpath,
        product_label=product_label,
        polarization=polarization,
    )

    logger.info(
        "Scene STAC Item: %s",
        tile_dir / "items" / product_label / f"{item_id}.json",
    )
    return item_id



def _write_monthly_stac_item(
    mgrs_tile_id: str,
    direction_label: str,
    month_str: str,
    rep_dt: pd.Timestamp,
    tile_dir: Path,
    transform: Affine,
    width: int,
    height: int,
    target_crs: str,
    cog_relpath: str | None,
    zarr_relpath: str,
    preview_relpath: str | None = None,
    processing_level: str = "hARDCp",
    polarization: str = "VV+VH",
    product_label: str = "smonthly",
    product_variant: str | None = None,
    processing_signature: str | None = None,
    processing_variant_json: str | None = None,
    actual_bands: list[str] | None = None,
    jpl_burst_ids: list[str] | None = None,
    opera_ids: list[str] | None = None,
    pass_id: int | None = None,
    track: int | None = None,
    primary_track: int | None = None,
    track_coverage: list[dict] | None = None,
    item_id_override: str | None = None,
) -> str:
    """
    Write a STAC Item JSON for one monthly composite.

    Returns the item ID. No-op (returns None) when STAC output is disabled
    (catalog-only build).
    """
    from s1grits.stac_builder import stac_output_enabled
    if not stac_output_enabled():
        return None
    item_id = item_id_override or f"{mgrs_tile_id}_{direction_label}_{month_str}"
    datetime_str = f"{month_str}-01T00:00:00Z"

    variant_properties = {
        "s1:monthly_composite": "median",
        "s1grits:primary_track": primary_track,
        "s1grits:track_coverage": track_coverage,
        **({"s1grits:product_variant": product_variant} if product_variant else {}),
        **({"s1grits:processing_signature": processing_signature} if processing_signature else {}),
        **({"s1grits:processing_variant": processing_variant_json} if processing_variant_json else {}),
        **({"s1grits:bands": actual_bands} if actual_bands else {}),
    }

    item_id = _build_stac_item(
        mgrs_tile_id=mgrs_tile_id,
        direction_label=direction_label,
        item_id=item_id,
        datetime_str=datetime_str,
        time_step="P1M",
        collection_id="s1grits-smonthly",
        cog_title="Monthly Composite COG",
        tile_dir=tile_dir,
        transform=transform,
        width=width,
        height=height,
        target_crs=target_crs,
        processing_level=processing_level,
        pass_id=pass_id,
        track=track,
        jpl_burst_ids=jpl_burst_ids,
        opera_ids=opera_ids,
        variant_properties=variant_properties,
        cog_relpath=cog_relpath,
        zarr_relpath=zarr_relpath,
        preview_relpath=preview_relpath,
        product_label=product_label,
        polarization=polarization,
    )

    logger.info(
        "Monthly STAC Item: %s",
        tile_dir / "items" / product_label / f"{item_id}.json",
    )
    return item_id


# ---------------------------------------------------------------------------
# scenes/ output writer
# ---------------------------------------------------------------------------

def _write_scenes_output(
    mgrs_tile_id: str,
    direction_label: str,
    tile_dir: Path,
    final_vv: list,
    prof_vv: list,
    final_vh: list,
    prof_vh: list,
    clean_dates: list,
    df_rtc_ts: pd.DataFrame,
    target_crs: str,
    target_res: float,
    generate_cog: bool,
    generate_preview: bool,
    chunk_y: int,
    chunk_x: int,
    cog_block: int,
    processing_level: str,
    transform,
    width: int,
    height: int,
    x_coords,
    y_coords,
    tile_clip: bool = True,
    features_ratio: bool = False,
    features_rvi: bool = False,
    features_glcm: bool = False,
    copol_name: str = "VV_dB",
    crosspol_name: str = "VH_dB",
    ratio_name: str = "Ratio",
    rvi_name: str = "RVI",
    do_despeckle: bool = False,
    despeckle_method: str = "tv_bregman",
    despeckle_kwargs: dict | None = None,
    group_mode: str = "acq_group",  # always acq_group; kept for call-site compat
    require_complete_bursts: bool = False,
    track_footprint: dict | None = None,
    track_footprint_ids: dict | None = None,
    incomplete_policy: str = "skip",
    incomplete_sink: list | None = None,
    interior_hole_max_frac: float = 0.005,
) -> list[dict]:
    """
    Write per-acquisition-group scene products.

    Always operates in acq_group mode — each relative orbit track gets
    its own Zarr store, COGs, and previews. No time-floored grouping.

    Returns catalog records with output_type='scenes'.
    """
    # ---- Build product label and directory structure ----
    _desp_label = ""
    if do_despeckle:
        _m = despeckle_kwargs.get('reg_param', 5.0) if despeckle_kwargs else 5.0
        _m_str = str(int(_m)) if isinstance(_m, float) and _m == int(_m) else str(_m).replace('.', '_')
        _desp_label = f"_{despeckle_method.replace('_', '').lower()}{_m_str}"
    _band_label = ""
    _bp = []
    if features_ratio: _bp.append('Ratio')
    if features_rvi:   _bp.append('RVI')
    if features_glcm:  _bp.append('GLCM')
    if _bp: _band_label = '_' + '_'.join(_bp)

    product_label = f"scenes_{direction_label}{_desp_label}{_band_label}"
    scenes_zarr_dir = tile_dir / product_label / 'zarr'
    scenes_cog_dir  = tile_dir / product_label / 'cog'
    scenes_png_dir  = tile_dir / product_label / 'preview'

    # Integrity check
    _check_tile_integrity(str(tile_dir), str(scenes_zarr_dir))

    # ---- Build band list ----
    _band_names = [copol_name, crosspol_name]
    if features_ratio:
        _band_names.append(ratio_name)
    if features_rvi:
        _band_names.append(rvi_name)
    if features_glcm:
        from s1grits.asf_array_processing import _get_texture_band_names
        _tex_cfg = {
            "enabled": True, "inputs": [copol_name, crosspol_name],
            "metrics": ["contrast", "homogeneity", "entropy", "correlation"],
            "window_size": 5, "distance": 1, "angles": [0, 90],
            "average_angles": True, "levels": 16,
        }
        _band_names.extend(_get_texture_band_names(_tex_cfg))

    # ---- Compute product instance metadata (once per call) ----
    _scenes_bands = list(_band_names)
    _scenes_variant_vals = {
        "processing.spatial_despeckle": do_despeckle,
        "processing.despeckle_method": despeckle_method if do_despeckle else None,
        "processing.despeckle_strength": (
            despeckle_kwargs.get('reg_param', 5.0) if despeckle_kwargs and do_despeckle else None
        ),
        "processing.features_ratio": features_ratio,
        "processing.features_rvi": features_rvi,
        "processing.features_glcm": features_glcm,
    }
    _scenes_sig = make_processing_signature(_scenes_variant_vals)
    _scenes_variant = make_product_variant('scenes', _scenes_variant_vals, _scenes_bands)

    # ---- Pre-compute acquisition groups (always acq_group mode) ----
    _acq_group_to_rows: dict[tuple, list] = {}
    for _, row in df_rtc_ts.iterrows():
        _key = (int(row['pass_id']), int(row['acq_group_id_within_mgrs_tile']))
        _acq_group_to_rows.setdefault(_key, []).append(row)
    _dt_str_to_clean_idx: dict[str, list[int]] = {}
    for _ci, _cd in enumerate(clean_dates):
        _cd_str = pd.Timestamp(_cd).tz_convert('UTC').strftime('%Y%m%dT%H%M%S')
        _dt_str_to_clean_idx.setdefault(_cd_str, []).append(_ci)

    if generate_cog:
        scenes_cog_dir.mkdir(parents=True, exist_ok=True)
    if generate_preview:
        scenes_png_dir.mkdir(parents=True, exist_ok=True)
    scenes_zarr_dir.mkdir(parents=True, exist_ok=True)  # Zarr always generated

    # ---- Pre-compute MGRS tile clip mask ----
    _clip_inv_mask = None
    _tile_mask = None  # bool, True inside the MGRS tile (for interior-hole check)
    if tile_clip:
        mgrs_wkt = _get_mgrs_tile_geometry_wkt(mgrs_tile_id)
        crs_ll = pyproj.CRS.from_epsg(4326)
        crs_t = pyproj.CRS.from_user_input(target_crs)
        proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
        geom_proj = shp_transform(proj, shapely_wkt.loads(mgrs_wkt))
        _clip_mask = rasterize(
            [(mapping(geom_proj), 1)],
            out_shape=(height, width),
            transform=transform,
            fill=0, dtype="uint8", all_touched=False,
        ).astype(bool)
        _clip_inv_mask = ~_clip_mask
        _tile_mask = _clip_mask

    catalog_records: list[dict] = []

    # ---- Iterate over acquisition groups ----
    _acq_iter = sorted(_acq_group_to_rows.items(), key=lambda kv: min(pd.Timestamp(r['acq_dt']).tz_convert('UTC') for r in kv[1]))
    _n_acq = len(_acq_iter)

    for _acq_i, ((_pass_id_key, _acq_grp_key), rows) in enumerate(_acq_iter, 1):
        _rep_ts = min(pd.Timestamp(r['acq_dt']).tz_convert('UTC') for r in rows)
        acq_ts = _rep_ts
        dt_str = acq_ts.strftime('%Y%m%dT%H%M%S')
        _date_label = acq_ts.strftime('%Y-%m-%d')

        # Collect clean_dates indices for this group's bursts
        indices = []
        for r in rows:
            _r_str = pd.Timestamp(r['acq_dt']).tz_convert('UTC').strftime('%Y%m%dT%H%M%S')
            indices.extend(_dt_str_to_clean_idx.get(_r_str, []))

        # Per-group Zarr path
        _track_tok_raw = str(rows[0]['track_token']) if rows else 'UNK'
        _track_tok = _track_tok_raw.replace('_', '-')
        n_bursts = len(rows)
        zarr_name = f"s1grits_scenes_{mgrs_tile_id}_{direction_label}_TK{_track_tok}_N{n_bursts:02d}.zarr"
        zarr_path_group = scenes_zarr_dir / zarr_name

        console.print(
            f"[dim]      Scene {_acq_i}/{_n_acq}: {_date_label} "
            f"({len(indices)} burst(s))[/dim]"
        )

        # Burst accounting for this acquisition. The full footprint (distinct
        # bursts for this track across the whole query + catalog history) is the
        # reference. We do NOT skip on a raw count shortfall: a data-take ending
        # a burst or two short along-track is normal "edge truncation" and is
        # kept. The skip decision is made below on the mosaicked raster — only an
        # INTERIOR NoData hole (a genuinely missing burst, enclosed by data)
        # triggers a skip. These counts are kept to classify the hole's cause.
        _loaded = len(indices)
        _expected_full = (
            int(track_footprint.get(_track_tok_raw, n_bursts))
            if track_footprint else n_bursts
        )
        _network_missing = max(0, n_bursts - _loaded)     # in metadata, not loaded
        _asf_missing = max(0, _expected_full - n_bursts)  # not in metadata for this date
        try:
            _age_days = int((pd.Timestamp.now(tz='UTC') - acq_ts).days)
        except Exception:
            _age_days = None

        jpl_burst_ids = [str(r['jpl_burst_id']) for r in rows] if rows else []
        opera_ids     = [str(r['opera_id'])      for r in rows] if rows else []
        track_number  = int(rows[0]['track_number']) if rows else -1
        pass_id       = int(rows[0]['pass_id'])       if rows else -1

        # Mosaic all bursts from this acquisition onto master grid
        arr_vv_lin = _mosaic_align(
            indices, final_vv, prof_vv, height, width, transform, target_crs
        )
        arr_vh_lin = _mosaic_align(
            indices, final_vh, prof_vh, height, width, transform, target_crs
        )

        if arr_vv_lin is None or arr_vh_lin is None:
            logger.warning("Scene %s mosaic returned None, skipping", dt_str)
            continue

        # Interior-gap check. Edge truncation (bursts missing at the along-track
        # ENDS) is normal and kept. We act on a real interior gap, detected two
        # ways (either triggers):
        #   1. burst-index: a footprint burst is missing while present bursts
        #      exist both before AND after it along-track in its sub-swath. This
        #      catches full-width segment gaps and edge sub-swath gaps that the
        #      raster test below cannot (they connect to the swath border).
        #   2. raster fill-holes: NoData fully enclosed by valid data (catches
        #      download drops that leave an enclosed hole).
        _footprint_ids = (
            (track_footprint_ids or {}).get(_track_tok_raw) or set(jpl_burst_ids)
        )
        _interior_missing = _missing_interior_bursts(_footprint_ids, jpl_burst_ids)
        _hole_frac = _interior_hole_fraction(np.isfinite(arr_vv_lin), _tile_mask)
        if _interior_missing or _hole_frac > interior_hole_max_frac:
            # classify cause: missing-from-metadata = ASF; in-metadata-but-gap = NETWORK
            if _interior_missing:
                if _age_days is not None and _age_days <= 30:
                    _cause, _recover = "ASF_MISSING_RECENT", "ASF latency — re-run in a few days"
                else:
                    _cause, _recover = "ASF_MISSING_STALE", "likely permanent (ASF has no data here)"
            elif _network_missing:
                _cause, _recover = "NETWORK", "re-run should fill it"
            else:
                _cause, _recover = "UNKNOWN", "inspect the scene"
            _inc_rec = {
                "tile_id": mgrs_tile_id, "flight_direction": direction_label,
                "track_token": _track_tok_raw, "date": _date_label, "datetime": dt_str,
                "loaded_bursts": _loaded, "metadata_bursts": n_bursts,
                "footprint_bursts": _expected_full,
                "interior_missing_bursts": len(_interior_missing),
                "interior_hole_pct": round(_hole_frac * 100, 3),
                "network_missing": _network_missing, "asf_missing": _asf_missing,
                "age_days": _age_days, "cause": _cause, "recoverable": _recover,
            }
            _detail = (
                f"{len(_interior_missing)} interior burst(s) missing"
                if _interior_missing else f"interior NoData {_hole_frac*100:.2f}% of tile"
            )
            _msg = (
                f"[Hole] Scene {_date_label} ({dt_str}) TK{_track_tok}: {_detail}; "
                f"loaded {_loaded}/{_expected_full} bursts ({_cause}: {_recover})."
            )
            if incomplete_policy == "abort" or require_complete_bursts:
                raise RuntimeError(_msg + " Aborting (incomplete_acquisition=abort).")
            if incomplete_sink is not None:
                incomplete_sink.append(_inc_rec)
            if incomplete_policy == "skip":
                logger.warning(_msg + " Skipping this acquisition.")
                console.print(
                    f"[yellow]      SKIP {_date_label}: {_detail} "
                    f"({_cause}) — not written[/yellow]"
                )
                continue
            logger.warning(_msg + " Writing with the gap (incomplete_acquisition=write).")
            console.print(
                f"[yellow]      WARNING {_date_label}: {_detail} "
                f"({_cause}) — writing[/yellow]"
            )

        # Apply spatial despeckle to full mosaicked image (post-mosaic, not per-burst)
        if do_despeckle:
            from s1grits.asf_array_processing import despeckle_2d
            _tv_kw = despeckle_kwargs if despeckle_method == "tv_bregman" else None
            _nlm_kw = despeckle_kwargs if despeckle_method == "nlm" else None
            arr_vv_lin = despeckle_2d(arr_vv_lin, method=despeckle_method,
                                      tv_kwargs=_tv_kw, nlm_kwargs=_nlm_kw)
            arr_vh_lin = despeckle_2d(arr_vh_lin, method=despeckle_method,
                                      tv_kwargs=_tv_kw, nlm_kwargs=_nlm_kw)

        arr_vv_db = _linear_to_db(arr_vv_lin)
        arr_vh_db = _linear_to_db(arr_vh_lin)

        # Optional derived bands
        _extra_bands = []
        if features_ratio:
            # Ratio = VH/VV in linear domain (standard SAR convention)
            _denom_r = np.where(arr_vv_lin > 0, arr_vv_lin, np.nan)
            _ratio_lin = arr_vh_lin / _denom_r
            _extra_bands.append((ratio_name, _ratio_lin.astype(np.float32)))
        if features_rvi:
            # RVI = 4*VH / (VV+VH) in linear domain; range [0, 4]
            _denom_rvi = arr_vv_lin + arr_vh_lin
            _rvi_lin = np.where(_denom_rvi > 0, 4.0 * arr_vh_lin / _denom_rvi, np.nan)
            _extra_bands.append((rvi_name, _rvi_lin.astype(np.float32)))

        # GLCM texture bands
        _glcm_bands = []
        if features_glcm:
            from s1grits.asf_array_processing import compute_glcm_texture_bands
            _tex_cfg = {
                "enabled": True, "inputs": [copol_name, crosspol_name],
                "metrics": ["contrast", "homogeneity", "entropy", "correlation"],
                "window_size": 5, "distance": 1, "angles": [0, 90],
                "average_angles": True, "levels": 16,
                "vv_db_range": [-25, 5], "vh_db_range": [-32, -5],
            }
            _tex_arrays, _tex_names = compute_glcm_texture_bands(
                arr_vv_db, arr_vh_db, _tex_cfg
            )
            for _name, _arr in zip(_tex_names, _tex_arrays):
                _glcm_bands.append((_name, _arr.astype(np.float32)))

        # Apply MGRS tile clip mask (final step after all processing)
        if _clip_inv_mask is not None:
            arr_vv_db[_clip_inv_mask] = np.nan
            arr_vh_db[_clip_inv_mask] = np.nan
            for _, _arr in _extra_bands + _glcm_bands:
                _arr[_clip_inv_mask] = np.nan

        # Build band arrays for Zarr append (always full grid)
        _band_arrays = [(copol_name, arr_vv_db), (crosspol_name, arr_vh_db)]
        _band_arrays.extend(_extra_bands)
        _band_arrays.extend(_glcm_bands)

        # Append to per-group Zarr (always generated, primary Data Cube product)
        _g_group = _init_zarr_2band(
            zarr_path_group, x_coords, y_coords, target_crs,
            transform, chunk_y, chunk_x,
            processing_level=f"scenes_{despeckle_method if do_despeckle else 'ARDC'}",
            band_names=_band_names,
        )
        # Write variant metadata to Zarr root attrs (idempotent)
        _g_group.attrs['processing_signature'] = _scenes_sig
        _g_group.attrs['product_variant'] = _scenes_variant
        _g_group.attrs['processing_variant_json'] = json.dumps(_scenes_variant_vals)
        _g_group.attrs['product_label'] = product_label
        _existing_group = set()
        if _g_group['time'].shape[0] > 0:
            _existing_group = set(
                pd.to_datetime(_g_group['time'][:]).strftime('%Y%m%dT%H%M%S')
            )
        if dt_str not in _existing_group:
            dt_ns = np.datetime64(acq_ts.to_datetime64(), 'ns')
            _append_zarr_timestep(_g_group, dt_ns, _band_arrays)
        zarr_relpath = zarr_path_group.relative_to(tile_dir)

        # Spatial crop for COG and preview output (Zarr keeps full grid)
        # When tile_clip=True, crop arrays to MGRS tile bounding box so that
        # COG and preview cover only the tile extent, not the full burst mosaic.
        _cog_transform = transform
        _cog_width, _cog_height = width, height
        _arr_vv_cog, _arr_vh_cog = arr_vv_db, arr_vh_db
        _extra_bands_cog = list(_extra_bands)
        _glcm_bands_cog = list(_glcm_bands)

        if tile_clip and _clip_inv_mask is not None:
            _all_cog = (
                [arr_vv_db, arr_vh_db]
                + [a for _, a in _extra_bands]
                + [a for _, a in _glcm_bands]
            )
            try:
                _clipped, _cog_transform = _clip_arrays_to_wkt_4326(
                    _all_cog, mgrs_wkt, target_crs, transform, height, width
                )
                _arr_vv_cog = _clipped[0]
                _arr_vh_cog = _clipped[1]
                _idx = 2
                _extra_bands_cog = [
                    (n, _clipped[_idx + i]) for i, (n, _) in enumerate(_extra_bands)
                ]
                _idx += len(_extra_bands)
                _glcm_bands_cog = [
                    (n, _clipped[_idx + i]) for i, (n, _) in enumerate(_glcm_bands)
                ]
                _cog_height, _cog_width = _arr_vv_cog.shape
            except Exception as _clip_e:
                logger.warning(
                    "tile_clip spatial crop failed for %s %s: %s",
                    mgrs_tile_id, dt_str, _clip_e
                )

        # Optional COG (multi-band) — uses spatially cropped arrays when tile_clip=True
        cog_relpath = None
        if generate_cog:
            _all_bands_cog = (
                [(copol_name, _arr_vv_cog), (crosspol_name, _arr_vh_cog)]
                + _extra_bands_cog
                + _glcm_bands_cog
            )
            fname = f"s1grits_scenes_{mgrs_tile_id}_{direction_label}_TK{_track_tok}_N{n_bursts:02d}_{dt_str}.tif"
            cog_path = scenes_cog_dir / fname
            prof = {
                'driver': 'GTiff', 'dtype': 'float32', 'nodata': float('nan'),
                'width': _cog_width, 'height': _cog_height,
                'count': len(_all_bands_cog),
                'crs': target_crs, 'transform': _cog_transform,
                'compress': 'deflate', 'tiled': True,
                'blockxsize': cog_block, 'blockysize': cog_block,
            }
            _write_multiband_cog(cog_path, _all_bands_cog, prof)
            cog_relpath = str(cog_path.relative_to(tile_dir))

        # Optional preview PNG — uses spatially cropped arrays when tile_clip=True
        preview_relpath = None
        if generate_preview:
            # Ratio = VH/VV in linear power domain (per CLAUDE.md standard).
            # _arr_*_cog are power dB (10*log10), so the linear ratio is
            # 10**((VH_dB - VV_dB)/10).  Validity is "both finite" — a >0 mask
            # on dB would wrongly discard the (typically negative) backscatter.
            _valid_cog = np.isfinite(_arr_vv_cog) & np.isfinite(_arr_vh_cog)
            ratio_arr = np.full_like(_arr_vv_cog, np.nan, dtype=np.float32)
            ratio_arr[_valid_cog] = np.power(
                10.0, (_arr_vh_cog[_valid_cog] - _arr_vv_cog[_valid_cog]) / 10.0
            ).astype(np.float32)

            png_name = f"s1grits_scenes_{mgrs_tile_id}_{direction_label}_TK{_track_tok}_N{n_bursts:02d}_{dt_str}.png"
            png_path = scenes_png_dir / png_name
            _generate_preview_png(
                vv_db=_arr_vv_cog,
                vh_db=_arr_vh_cog,
                ratio=ratio_arr,
                src_transform=_cog_transform,
                src_crs=target_crs,
                output_path=str(png_path),
            )
            preview_relpath = str(png_path.relative_to(tile_dir))

        # Write STAC Item
        _write_scene_stac_item(
            mgrs_tile_id, direction_label, acq_ts, tile_dir,
            transform, width, height, target_crs,
            cog_relpath=cog_relpath,
            zarr_relpath=str(zarr_relpath),
            preview_relpath=preview_relpath,
            processing_level=processing_level,
            product_label=product_label,
            product_variant=_scenes_variant,
            processing_signature=_scenes_sig,
            processing_variant_json=json.dumps(_scenes_variant_vals),
            actual_bands=_scenes_bands,
            jpl_burst_ids=jpl_burst_ids,
            opera_ids=opera_ids,
            pass_id=pass_id,
            track=track_number,
        )

        # Catalog timestamps are stored tz-naive (UTC wall-time) so they stay
        # comparable with the tz-naive records written by the monthly workflow
        # into the same catalog.parquet (avoids tz-aware/tz-naive sort crashes).
        _acq_naive = acq_ts.tz_localize(None) if getattr(acq_ts, 'tz', None) is not None else acq_ts
        _rec = normalize_catalog_record({
            'item_id':      f"{mgrs_tile_id}_{direction_label}_{dt_str}",
            'collection_id': 's1grits-scenes',
            'product_type': 'scenes',
            'product_label': product_label,
            'tile_id':       mgrs_tile_id,
            'flight_direction': direction_label,
            'crs':           target_crs,
            'transform':     list(transform)[:6],
            'width':         width,
            'height':        height,
            'datetime':      _acq_naive,
            'start_datetime': _acq_naive,
            'end_datetime':   _acq_naive,
            'month':         acq_ts.strftime('%Y-%m') if hasattr(acq_ts, 'strftime') else str(acq_ts)[:7],
            'geometry_group_id': f"{mgrs_tile_id}_{direction_label}_TK{_track_tok}_N{n_bursts:02d}",
            'track':         track_number,
            'n_bursts':      n_bursts,
            'n_scenes':      None,
            'jpl_burst_ids': jpl_burst_ids,
            'opera_ids':     opera_ids,
            'pass_id':       pass_id,
            'zarr_path':     str(zarr_relpath),
            'cog_path':      cog_relpath,
            'preview_path':  preview_relpath,
            'bands':         json.dumps(_scenes_bands),
            'processing_level': processing_level,
            'product_variant': _scenes_variant,
            'processing_signature': _scenes_sig,
            'processing_variant_json': json.dumps(_scenes_variant_vals),
            # item_path auto-computed by normalize_catalog_record from item_id
            # as items/{product_label}/{item_id}.json (matches the file written
            # by _write_scene_stac_item).
        })
        catalog_records.append(_rec)

    logger.info(
        "scenes/ written: %d scenes -> %s", len(catalog_records), scenes_zarr_dir
    )
    return catalog_records


# ---------------------------------------------------------------------------
# smonthly/ output writer
# ---------------------------------------------------------------------------

def _write_smonthly_one_track(
    mgrs_tile_id: str,
    direction_label: str,
    tile_dir: Path,
    final_vv: list,
    prof_vv: list,
    final_vh: list,
    prof_vh: list,
    clean_dates: list,
    target_crs: str,
    target_res: float,
    generate_cog: bool,
    generate_preview: bool,
    chunk_y: int,
    chunk_x: int,
    cog_block: int,
    on_time_conflict: str,
    monthly_cfg: dict,
    processing_level: str,
    transform,
    width: int,
    height: int,
    x_coords,
    y_coords,
    # group_mode kept for compat — always per-acq-group processing
    group_mode: str = 'acq_group',
    df_batch=None,
    tile_clip: bool = True,
    features_ratio: bool = False,
    features_rvi: bool = False,
    features_glcm: bool = False,
    copol_name: str = "VV_dB",
    crosspol_name: str = "VH_dB",
    ratio_name: str = "Ratio",
    rvi_name: str = "RVI",
    track_token: str = "UNK",
    n_bursts_track: int = 0,
    restrict_to_group: bool = False,
) -> list[dict]:
    """
    Compute monthly composites for ONE acquisition-group track and write
    smonthly/zarr/s1grits_smonthly_{TILE}_{DIR}_TK{tok}_N{nn}.zarr (plus optional
    cog/preview). Writes one STAC Item per month.

    The caller passes ``df_batch`` already filtered to a single ``track_token``
    (acq group) and sets ``restrict_to_group=True``; the month loop then keeps
    only the acquisitions belonging to that group, so each track_token gets its
    own per-track product files (mirroring the scenes/static per-track layout).
    ``track_token``/``n_bursts_track`` drive the file naming. When
    ``restrict_to_group`` is False all acquisitions are used (single-product
    degrade, e.g. when no track metadata is available).
    The master grid is passed in from the caller and shared with the scenes
    writer to ensure both products use the identical grid.

    Returns catalog records with product_type='smonthly'.
    """
    _tk_suffix = f"_TK{track_token}_N{n_bursts_track:02d}"
    # Build product subdirectory name: smonthly_{DIR}_{bands}
    _mbp = []
    if features_ratio: _mbp.append('Ratio')
    if features_rvi:   _mbp.append('RVI')
    if features_glcm:  _mbp.append('GLCM')
    _m_band_label = '_' + '_'.join(_mbp) if _mbp else ''
    product_label = f"smonthly_{direction_label}{_m_band_label}"

    monthly_zarr_dir = tile_dir / product_label / 'zarr'
    monthly_cog_dir  = tile_dir / product_label / 'cog'
    monthly_png_dir  = tile_dir / product_label / 'preview'
    zarr_path = monthly_zarr_dir / f"s1grits_smonthly_{mgrs_tile_id}_{direction_label}{_tk_suffix}.zarr"

    # Build band list
    _band_names = [copol_name, crosspol_name]
    if features_ratio:
        _band_names.append(ratio_name)
    if features_rvi:
        _band_names.append(rvi_name)
    if features_glcm:
        from s1grits.asf_array_processing import _get_texture_band_names
        _tex_cfg = {
            "enabled": True, "inputs": [copol_name, crosspol_name],
            "metrics": ["contrast", "homogeneity", "entropy", "correlation"],
            "window_size": 5, "distance": 1, "angles": [0, 90],
            "average_angles": True, "levels": 16,
        }
        _band_names.extend(_get_texture_band_names(_tex_cfg))

    # ---- Compute smonthly product instance metadata ----
    _composite_method = monthly_cfg.get('composite_method', 'median') if monthly_cfg else 'median'
    _smonthly_bands = list(_band_names)
    _smonthly_variant_vals = {
        "processing.spatial_despeckle": processing_level != "ARDC",
        "processing.despeckle_method": None,
        "processing.despeckle_strength": None,
        "processing.features_ratio": features_ratio,
        "processing.features_rvi": features_rvi,
        "processing.features_glcm": features_glcm,
        "processing.monthly.composite_method": _composite_method,
    }
    _smonthly_sig = make_processing_signature(_smonthly_variant_vals)
    _smonthly_variant = make_product_variant('smonthly', _smonthly_variant_vals, _smonthly_bands)

    # Open / create Zarr (always enabled — Zarr is the primary Data Cube product)
    g = _init_zarr_2band(
        zarr_path, x_coords, y_coords, target_crs,
        transform, chunk_y, chunk_x,
        processing_level=f"monthly_{processing_level}",
        band_names=_band_names,
    )
    g.attrs['product_type'] = 'smonthly'
    g.attrs['time_varying'] = True
    g.attrs['array_dims'] = ['time', 'y', 'x']
    # Write variant metadata to Zarr root attrs for resync self-sufficiency
    g.attrs['processing_signature'] = _smonthly_sig
    g.attrs['product_variant'] = _smonthly_variant
    g.attrs['processing_variant_json'] = json.dumps(_smonthly_variant_vals)
    g.attrs['product_label'] = product_label

    existing_months: set[str] = set()
    if g['time'].shape[0] > 0:
        existing_months = set(
            pd.to_datetime(g['time'][:]).strftime('%Y-%m')
        )

    # Override from monthly_cfg if present
    monthly_gen_cog = monthly_cfg.get('generate_cog', generate_cog)
    monthly_gen_png = monthly_cfg.get('generate_preview', generate_preview)
    composite_method = monthly_cfg.get('composite_method', 'median')
    trim_fraction = monthly_cfg.get('trim_fraction', 0.15)
    # Helper: unified monthly compositing
    def _monthly_composite(stack, method):
        arr = np.stack(stack, axis=0)
        if method == 'median':
            return np.nanmedian(arr, axis=0)
        elif method == 'min':
            return np.nanmin(arr, axis=0)
        elif method == 'mean':
            return np.nanmean(arr, axis=0)
        elif method == 'trimmed_mean':
            from scipy.stats import trim_mean
            return trim_mean(arr, trim_fraction, axis=0)
        else:
            return np.nanmedian(arr, axis=0)

    if monthly_gen_cog:
        monthly_cog_dir.mkdir(parents=True, exist_ok=True)
    if monthly_gen_png:
        monthly_png_dir.mkdir(parents=True, exist_ok=True)
    monthly_zarr_dir.mkdir(parents=True, exist_ok=True)

    # Group scene indices by month
    month_groups = _group_indices_by_period(clean_dates, 'monthly')

    # Per-month burst provenance (union of the bursts whose acquisitions fall in
    # the month) aggregated from the source burst dataframe. Composites lose the
    # per-acquisition identity, so this records the burst footprint of the month.
    _month_burst_ids: dict[str, list[str]] = {}
    _month_opera_ids: dict[str, list[str]] = {}
    _month_pass: dict[str, int | None] = {}
    _month_track: dict[str, int | None] = {}
    if df_batch is not None and len(df_batch):
        _months = pd.to_datetime(df_batch['acq_dt'], utc=True).dt.strftime('%Y-%m')
        for _m, _idx in df_batch.groupby(_months.values).groups.items():
            _g = df_batch.loc[_idx]
            _month_burst_ids[_m] = sorted({str(x) for x in _g['jpl_burst_id'].dropna()})
            _month_opera_ids[_m] = sorted({str(x) for x in _g['opera_id'].dropna()})
            _pp = _g['pass_id'].dropna().unique()
            _month_pass[_m] = int(_pp[0]) if len(_pp) else None
            _tt = _g['track_number'].dropna().unique()
            _month_track[_m] = int(_tt[0]) if len(_tt) else None

    # Map each clean_dates acquisition index -> relative orbit (track_number),
    # so the monthly composite can be built per-track and then mosaicked.
    # Acquisitions with no match (or no df_batch) fall into a single group (-1),
    # which degrades to the original single-median behaviour.
    def _ts_key(_x):
        _t = pd.Timestamp(_x)
        _t = _t.tz_convert('UTC') if _t.tz is not None else _t.tz_localize('UTC')
        return _t.strftime('%Y%m%dT%H%M%S')

    _index_track: dict[int, int] = {}
    if df_batch is not None and len(df_batch):
        _ts_to_track: dict[str, int] = {}
        for _, _r in df_batch.iterrows():
            _ts_to_track[_ts_key(_r['acq_dt'])] = int(_r['track_number'])
        for _ci, _cd in enumerate(clean_dates):
            _index_track[_ci] = _ts_to_track.get(_ts_key(_cd), -1)

    zarr_relpath = zarr_path.relative_to(tile_dir)

    catalog_records: list[dict] = []

    for month_str, burst_indices in sorted(month_groups.items()):
        # Restrict to this acq-group's acquisitions (per-track product files).
        # df_batch is already filtered to one track_token, so any index that
        # maps to a real track (!= -1) belongs to this group — this is correct
        # even for multi-relative-orbit acq groups (e.g. track_token "69_172").
        if restrict_to_group:
            burst_indices = [
                i for i in burst_indices if _index_track.get(i, -1) != -1
            ]
            if not burst_indices:
                continue

        _overwrite_pending = False
        if month_str in existing_months:
            if on_time_conflict == 'skip':
                logger.info("Month %s already exists, skipping", month_str)
                continue
            # 'overwrite': mark for deletion after new composite is validated
            _overwrite_pending = True
            # falls through to rebuild composite

        # Split this month's acquisitions by relative orbit (track), composite
        # each track separately with the configured method, then mosaic with the
        # largest-coverage track as the base ("first") and lower-coverage tracks
        # filling only the remaining gaps. Single-track months degrade to the
        # original single-median behaviour.
        _idx_by_track: dict[int, list[int]] = {}
        for i in burst_indices:
            _idx_by_track.setdefault(_index_track.get(i, -1), []).append(i)

        def _track_composite(idxs, final_arr, prof_arr):
            _stack = []
            for _i in idxs:
                _arr = _mosaic_align(
                    [_i], final_arr, prof_arr, height, width,
                    transform, target_crs,
                )
                if _arr is not None:
                    _stack.append(_arr)
            if not _stack:
                return None
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', 'All-NaN slice')
                warnings.filterwarnings('ignore', 'Mean of empty slice')
                return _monthly_composite(_stack, composite_method).astype(np.float32)

        _per_track_vv: dict[int, np.ndarray] = {}
        _per_track_vh: dict[int, np.ndarray] = {}
        for _tk, _tk_idxs in _idx_by_track.items():
            _cvv = _track_composite(_tk_idxs, final_vv, prof_vv)
            if _cvv is None:
                continue
            _cvh = _track_composite(_tk_idxs, final_vh, prof_vh)
            if _cvh is None:
                continue
            _per_track_vv[_tk] = _cvv
            _per_track_vh[_tk] = _cvh

        if not _per_track_vv:
            logger.warning(
                "Month %s: no valid acquisitions, skipping", month_str
            )
            continue

        # Order tracks by VV coverage (valid-pixel count), largest first;
        # tie-break on track id for determinism.
        _track_cov = {
            _tk: int(np.isfinite(_arr).sum())
            for _tk, _arr in _per_track_vv.items()
        }
        _track_order = sorted(
            _per_track_vv, key=lambda t: (_track_cov[t], -t), reverse=True
        )

        if len(_track_order) == 1:
            composite_vv_lin = _per_track_vv[_track_order[0]]
            composite_vh_lin = _per_track_vh[_track_order[0]]
        else:
            composite_vv_lin = np.full((height, width), np.nan, dtype=np.float32)
            composite_vh_lin = np.full((height, width), np.nan, dtype=np.float32)
            _filled = np.zeros((height, width), dtype=bool)
            for _tk in _track_order:
                # VV drives the per-pixel source choice so VV and VH stay co-sourced
                _take = ~_filled & np.isfinite(_per_track_vv[_tk])
                composite_vv_lin[_take] = _per_track_vv[_tk][_take]
                composite_vh_lin[_take] = _per_track_vh[_tk][_take]
                _filled |= _take
            logger.info(
                "Month %s: priority mosaic of %d tracks (VV coverage) %s",
                month_str, len(_track_order),
                ", ".join(f"TK{_tk}={_track_cov[_tk]}" for _tk in _track_order),
            )
        del _per_track_vv, _per_track_vh
        gc.collect()

        # Track-coverage provenance for this month's priority mosaic: the base
        # ("primary") track and each contributing track's grid coverage fraction,
        # ordered as used in the fill (largest coverage first).
        _grid_px = int(height * width)
        _primary_track = int(_track_order[0])
        _track_coverage = [
            {
                "track": int(_tk),
                "valid_px": int(_track_cov[_tk]),
                "fraction": round(_track_cov[_tk] / _grid_px, 6) if _grid_px else None,
            }
            for _tk in _track_order
        ]

        arr_vv_db = _linear_to_db(composite_vv_lin)
        arr_vh_db = _linear_to_db(composite_vh_lin)

        # Optional derived bands — computed from linear-domain composites,
        # equivalent to asf_output_writing.py (workflow.py monthly path).
        # Ratio = VH/VV in linear domain (per CLAUDE.md standard)
        _m_extra_bands = []
        if features_ratio:
            _m_ratio_lin = np.where(
                composite_vv_lin > 0,
                composite_vh_lin / composite_vv_lin,
                np.nan,
            ).astype(np.float32)
            _m_extra_bands.append((ratio_name, _m_ratio_lin))
        if features_rvi:
            _denom = composite_vv_lin + composite_vh_lin
            _m_rvi_lin = np.where(
                _denom > 0,
                4.0 * composite_vh_lin / _denom,
                np.nan,
            ).astype(np.float32)
            _m_extra_bands.append((rvi_name, _m_rvi_lin))

        del composite_vv_lin, composite_vh_lin

        # GLCM texture bands — computed BEFORE masking to avoid edge artifacts
        _m_glcm_bands = []
        if features_glcm:
            from s1grits.asf_array_processing import compute_glcm_texture_bands
            _tex_cfg = {
                "enabled": True, "inputs": [copol_name, crosspol_name],
                "metrics": ["contrast", "homogeneity", "entropy", "correlation"],
                "window_size": 5, "distance": 1, "angles": [0, 90],
                "average_angles": True, "levels": 16,
            }
            _tex_arrays, _tex_names = compute_glcm_texture_bands(
                arr_vv_db, arr_vh_db, _tex_cfg
            )
            for _name, _arr in zip(_tex_names, _tex_arrays):
                _m_glcm_bands.append((_name, _arr.astype(np.float32)))

        # Apply MGRS tile clip mask — final step after all preprocessing
        # (aggregation, dB conversion, band computation, GLCM all done above)
        _m_clip_mask = None
        if tile_clip:
            mgrs_wkt = _get_mgrs_tile_geometry_wkt(mgrs_tile_id)
            crs_ll = pyproj.CRS.from_epsg(4326)
            crs_t = pyproj.CRS.from_user_input(target_crs)
            proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
            geom_proj = shp_transform(proj, shapely_wkt.loads(mgrs_wkt))
            _m_clip_mask = rasterize(
                [(mapping(geom_proj), 1)],
                out_shape=(height, width),
                transform=transform,
                fill=0, dtype="uint8", all_touched=False,
            ).astype(bool)
            _m_inv_mask = ~_m_clip_mask
            arr_vv_db[_m_inv_mask] = np.nan
            arr_vh_db[_m_inv_mask] = np.nan
            for _, _arr in _m_extra_bands:
                _arr[_m_inv_mask] = np.nan
            for _, _arr in _m_glcm_bands:
                _arr[_m_inv_mask] = np.nan

        # Representative datetime: median acquisition timestamp for this month
        acq_timestamps = sorted(pd.Timestamp(clean_dates[i]) for i in burst_indices)
        rep_dt = acq_timestamps[len(acq_timestamps) // 2]
        dt_ns = np.datetime64(rep_dt.to_datetime64(), 'ns')

        # Build band arrays for Zarr (always full grid)
        _m_band_arrays = [(copol_name, arr_vv_db), (crosspol_name, arr_vh_db)]
        _m_band_arrays.extend(_m_extra_bands)
        _m_band_arrays.extend(_m_glcm_bands)

        # Write to Zarr (always enabled — Zarr is the primary Data Cube product)
        # Validate new arrays against Zarr grid before any mutation
        for _nm, _arr in _m_band_arrays:
            _dset = g[_nm]
            _exp = (_dset.shape[1], _dset.shape[2]) if _dset.ndim == 3 else _dset.shape
            if _arr.shape != _exp:
                raise ValueError(
                    f"Monthly composite shape mismatch for '{_nm}': "
                    f"got {_arr.shape}, expected {_exp}"
                )
        # Mutate: delete old step if overwriting, then append new
        if _overwrite_pending:
            _zarr_delete_timestep(g, month_str)
            existing_months.discard(month_str)
        _append_zarr_timestep(g, dt_ns, _m_band_arrays)
        existing_months.add(month_str)

        # Spatial crop for COG and preview output (Zarr keeps full grid)
        _m_cog_transform = transform
        _m_cog_width, _m_cog_height = width, height
        _m_vv_cog, _m_vh_cog = arr_vv_db, arr_vh_db
        _m_extra_cog = list(_m_extra_bands)
        _m_glcm_cog = list(_m_glcm_bands)

        if tile_clip and _m_clip_mask is not None:
            _all_m_cog = (
                [arr_vv_db, arr_vh_db]
                + [a for _, a in _m_extra_bands]
                + [a for _, a in _m_glcm_bands]
            )
            try:
                _m_clipped, _m_cog_transform = _clip_arrays_to_wkt_4326(
                    _all_m_cog, mgrs_wkt, target_crs, transform, height, width
                )
                _m_vv_cog = _m_clipped[0]
                _m_vh_cog = _m_clipped[1]
                _midx = 2
                _m_extra_cog = [
                    (n, _m_clipped[_midx + i]) for i, (n, _) in enumerate(_m_extra_bands)
                ]
                _midx += len(_m_extra_bands)
                _m_glcm_cog = [
                    (n, _m_clipped[_midx + i]) for i, (n, _) in enumerate(_m_glcm_bands)
                ]
                _m_cog_height, _m_cog_width = _m_vv_cog.shape
            except Exception as _clip_e:
                logger.warning(
                    "tile_clip spatial crop failed for %s %s: %s",
                    mgrs_tile_id, month_str, _clip_e
                )

        # Optional COG (multi-band) — uses spatially cropped arrays when tile_clip=True
        cog_relpath = None
        if monthly_gen_cog:
            _m_all_cog = (
                [(copol_name, _m_vv_cog), (crosspol_name, _m_vh_cog)]
                + _m_extra_cog
                + _m_glcm_cog
            )
            fname = f"s1grits_smonthly_{mgrs_tile_id}_{direction_label}{_tk_suffix}_{month_str}.tif"
            cog_path = monthly_cog_dir / fname
            prof = {
                'driver': 'GTiff', 'dtype': 'float32', 'nodata': float('nan'),
                'width': _m_cog_width, 'height': _m_cog_height,
                'count': len(_m_all_cog),
                'crs': target_crs, 'transform': _m_cog_transform,
                'compress': 'deflate', 'tiled': True,
                'blockxsize': cog_block, 'blockysize': cog_block,
            }
            _write_multiband_cog(cog_path, _m_all_cog, prof)
            cog_relpath = str(cog_path.relative_to(tile_dir))

        # Optional preview PNG — uses spatially cropped arrays when tile_clip=True
        preview_relpath = None
        if monthly_gen_png:
            # Ratio = VH/VV in linear power domain (per CLAUDE.md), from cropped
            # arrays.  _m_*_cog are power dB (10*log10), so invert with /10.0
            # (not /20.0, which is the amplitude convention).
            vv_lin_for_ratio = np.power(10.0, _m_vv_cog / 10.0)
            vh_lin_for_ratio = np.power(10.0, _m_vh_cog / 10.0)
            valid_mask = np.isfinite(vv_lin_for_ratio) & np.isfinite(vh_lin_for_ratio) & (vv_lin_for_ratio > 0)
            ratio_arr = np.full_like(vv_lin_for_ratio, np.nan, dtype=np.float32)
            ratio_arr[valid_mask] = (
                vh_lin_for_ratio[valid_mask] / vv_lin_for_ratio[valid_mask]
            ).astype(np.float32)

            png_name = f"s1grits_smonthly_{mgrs_tile_id}_{direction_label}{_tk_suffix}_{month_str}.png"
            png_path = monthly_png_dir / png_name
            _generate_preview_png(
                vv_db=_m_vv_cog,
                vh_db=_m_vh_cog,
                ratio=ratio_arr,
                src_transform=_m_cog_transform,
                src_crs=target_crs,
                output_path=str(png_path),
            )
            preview_relpath = str(png_path.relative_to(tile_dir))

        # Per-track identifiers (mirror the scenes per-track layout)
        _geom_group_id = f"{mgrs_tile_id}_{direction_label}{_tk_suffix}"
        _item_id = f"{_geom_group_id}_{month_str}"
        _rec_track = _month_track.get(month_str)

        # Write STAC Item
        _write_monthly_stac_item(
            mgrs_tile_id, direction_label, month_str, rep_dt, tile_dir,
            transform, width, height, target_crs,
            cog_relpath=cog_relpath,
            zarr_relpath=str(zarr_relpath),
            preview_relpath=preview_relpath,
            processing_level=processing_level,
            product_label=product_label,
            product_variant=_smonthly_variant,
            processing_signature=_smonthly_sig,
            processing_variant_json=json.dumps(_smonthly_variant_vals),
            actual_bands=_smonthly_bands,
            jpl_burst_ids=_month_burst_ids.get(month_str),
            opera_ids=_month_opera_ids.get(month_str),
            pass_id=_month_pass.get(month_str),
            track=_rec_track,
            primary_track=_primary_track,
            track_coverage=_track_coverage,
            item_id_override=_item_id,
        )

        _rec = normalize_catalog_record({
            'item_id':      _item_id,
            'collection_id': 's1grits-smonthly',
            'product_type': 'smonthly',
            'product_label': product_label,
            'tile_id':       mgrs_tile_id,
            'flight_direction': direction_label,
            'crs':           target_crs,
            'transform':     list(transform)[:6],
            'width':         width,
            'height':        height,
            'datetime':      rep_dt.tz_localize(None) if getattr(rep_dt, 'tz', None) is not None else rep_dt,
            'start_datetime': pd.Timestamp(f"{month_str}-01"),
            'end_datetime':   pd.Timestamp(f"{month_str}-01") + pd.offsets.MonthEnd(1),
            'month':          month_str,
            'geometry_group_id': _geom_group_id,
            'track':         _rec_track,
            'n_bursts':      n_bursts_track or None,
            'n_scenes':      len(burst_indices),
            'jpl_burst_ids': _month_burst_ids.get(month_str),
            'opera_ids':     _month_opera_ids.get(month_str),
            'pass_id':       _month_pass.get(month_str),
            'primary_track': _primary_track,
            'track_coverage_json': json.dumps(_track_coverage),
            'zarr_path':     str(zarr_relpath),
            'cog_path':      cog_relpath,
            'preview_path':  preview_relpath,
            'bands':         json.dumps(_smonthly_bands),
            'processing_level': processing_level,
            'product_variant': _smonthly_variant,
            'processing_signature': _smonthly_sig,
            'processing_variant_json': json.dumps(_smonthly_variant_vals),
        })
        catalog_records.append(_rec)

    logger.info(
        "smonthly/ written: %d months -> %s", len(catalog_records), zarr_path
    )
    return catalog_records


def _write_monthly_output_scenes(
    mgrs_tile_id: str,
    direction_label: str,
    tile_dir: Path,
    final_vv: list,
    prof_vv: list,
    final_vh: list,
    prof_vh: list,
    clean_dates: list,
    target_crs: str,
    target_res: float,
    generate_cog: bool,
    generate_preview: bool,
    chunk_y: int,
    chunk_x: int,
    cog_block: int,
    on_time_conflict: str,
    monthly_cfg: dict,
    processing_level: str,
    transform,
    width: int,
    height: int,
    x_coords,
    y_coords,
    group_mode: str = 'acq_group',
    df_batch=None,
    tile_clip: bool = True,
    features_ratio: bool = False,
    features_rvi: bool = False,
    features_glcm: bool = False,
    copol_name: str = "VV_dB",
    crosspol_name: str = "VH_dB",
    ratio_name: str = "Ratio",
    rvi_name: str = "RVI",
) -> list[dict]:
    """
    Write per-track smonthly composites for one tile/direction batch.

    Enumerates the acquisition-group tracks (``track_token``) present in
    ``df_batch`` and writes one independent smonthly product per group (its own
    ``..._TK{tok}_N{nn}.zarr``/cog/preview/catalog records), mirroring the
    scenes/static per-track layout. Grouping by ``track_token`` (not
    ``track_number``) is what keeps the file naming and the grouping key
    identical — so multi-relative-orbit acq groups (e.g. "69_172") map to a
    single, collision-free product — and lets ``catalog resync`` rebuild
    per-track STAC straight from the on-disk Zarr names.

    Falls back to a single (untracked) product when no track metadata is
    available in ``df_batch``.
    """
    def _call(restrict_to_group, track_token, n_bursts_track, df_track):
        return _write_smonthly_one_track(
            mgrs_tile_id, direction_label, tile_dir,
            final_vv, prof_vv, final_vh, prof_vh, clean_dates,
            target_crs, target_res, generate_cog, generate_preview,
            chunk_y, chunk_x, cog_block,
            on_time_conflict=on_time_conflict,
            monthly_cfg=monthly_cfg,
            processing_level=processing_level,
            transform=transform, width=width, height=height,
            x_coords=x_coords, y_coords=y_coords,
            group_mode=group_mode,
            df_batch=df_track,
            tile_clip=tile_clip,
            features_ratio=features_ratio,
            features_rvi=features_rvi,
            features_glcm=features_glcm,
            copol_name=copol_name,
            crosspol_name=crosspol_name,
            ratio_name=ratio_name,
            rvi_name=rvi_name,
            track_token=track_token,
            n_bursts_track=n_bursts_track,
            restrict_to_group=restrict_to_group,
        )

    # No track metadata -> single untracked product (df_track=None so the inner
    # provenance/track-mapping code is skipped instead of raising KeyError).
    if df_batch is None or not len(df_batch) or 'track_token' not in df_batch.columns:
        return _call(False, "UNK", 0, None)

    records: list[dict] = []
    for _tok_val, _g in df_batch.groupby('track_token'):
        _tok = str(_tok_val).replace('_', '-')
        _nb = int(_g['jpl_burst_id'].nunique()) if 'jpl_burst_id' in _g.columns else 0
        records.extend(_call(True, _tok, _nb, _g))
    return records


# ---------------------------------------------------------------------------
# Per-tile processor
# ---------------------------------------------------------------------------

def process_single_scenes_tile(
    mgrs_tile_id: str,
    time_ranges: list[tuple[str, str]],
    config: dict,
    output_root: Path,
    quiet: bool = False,
) -> dict[str, Any]:
    """
    Download raw bursts for one MGRS tile and write scenes/ outputs.
    Optionally writes smonthly/ composites if processing.monthly.enabled=true.

    ``quiet=True`` suppresses all console output (used in parallel mode, where
    many worker processes share one terminal and only the parent progress bar
    should render — per-tile detail still goes to the log file).
    """
    # Workflows NEVER write STAC — STAC is produced only by `catalog resync`.
    # This must be set per-tile because ProcessPoolExecutor workers (spawn)
    # re-import stac_builder fresh with the default ON and never call the entry
    # point, so without this they would write the legacy items/ JSON tree.
    from s1grits.stac_builder import set_stac_output_enabled
    set_stac_output_enabled(False)

    _console = Console(legacy_windows=True, no_color=False, quiet=quiet)
    _console.print(f"\n[bold cyan]Tile: {mgrs_tile_id}[/bold cyan]")

    try:
        # 1. Query metadata (spinner while ASF responds; skipped when quiet so
        # parallel workers don't open competing live displays)
        if quiet:
            df_rtc_ts = query_rtc_metadata_for_tile(
                mgrs_tile_id, time_ranges, config
            )
        else:
            with _console.status("[dim]  [1/4] Querying ASF metadata…[/dim]", spinner="dots"):
                df_rtc_ts = query_rtc_metadata_for_tile(
                    mgrs_tile_id, time_ranges, config
                )

        # Tracks dropped by the query-stage tile-coverage filter (recorded on the
        # frame so we can report them even though this may be a quiet worker).
        _dropped_tracks = list(getattr(df_rtc_ts, 'attrs', {}).get('coverage_dropped', []))

        if df_rtc_ts.empty:
            return {
                'status': 'failed', 'error': 'No data available',
                'tile_dir': None, 'catalog_path': None,
            }

        _console.print(
            f"[dim]  [1/4] Found [bold]{len(df_rtc_ts)}[/bold] scenes[/dim]"
        )

        # 2. Filter by flight direction
        flight_direction = config.get('roi', {}).get('flight_direction')
        if flight_direction:
            df_rtc_ts = filter_by_flight_direction(df_rtc_ts, flight_direction)
            if df_rtc_ts.empty:
                return {
                    'status': 'failed',
                    'error': f'No data for flight direction {flight_direction}',
                    'tile_dir': None, 'catalog_path': None,
                }

        direction_label = (
            df_rtc_ts['orbit_pass'].iloc[0].upper()
            if 'orbit_pass' in df_rtc_ts.columns
            else (flight_direction.upper() if flight_direction else 'UNKNOWN')
        )

        # Warn if the tile mixes ascending/descending passes but no filter is
        # set: a single direction_label is applied to every scene/path below,
        # which mislabels the minority direction. Set roi.flight_direction to fix.
        if not flight_direction and 'orbit_pass' in df_rtc_ts.columns:
            _dirs = sorted(df_rtc_ts['orbit_pass'].dropna().str.upper().unique())
            if len(_dirs) > 1:
                logger.warning(
                    "Tile %s contains mixed orbit directions %s but "
                    "roi.flight_direction is not set; all scenes will be "
                    "labelled '%s'. Set roi.flight_direction to avoid "
                    "mislabelling outputs.",
                    mgrs_tile_id, _dirs, direction_label,
                )

        # 3. Batch strategy
        n_scenes = len(df_rtc_ts)
        batch_strategy = get_memory_strategy_from_config(config, n_scenes)
        _console.print(
            f"[dim]  [2/4] Batch strategy: [bold]{batch_strategy}[/bold] "
            f"({n_scenes} scenes)[/dim]"
        )

        # 4. Output directories
        processing_level = _get_despeckle_token(config) or "ARDC"
        tile_dir = output_root / mgrs_tile_id
        tile_dir.mkdir(parents=True, exist_ok=True)

        # Forward consistency check: Zarr exists but catalog missing → auto-rebuild
        _tile_cat = tile_dir / 'catalog.parquet'
        _any_zarr = list(tile_dir.glob('scenes_*/zarr/*.zarr')) + list(tile_dir.glob('smonthly_*/zarr/*.zarr'))
        if _any_zarr and not _tile_cat.exists():
            logger.warning("[Recovery] Zarr found but catalog missing for %s", mgrs_tile_id)
            logger.warning(
                "[Recovery] Deferring catalog resync until after the workflow "
                "exits to avoid recursive output-root lock acquisition"
            )

        overwrite = config.get('output', {}).get('overwrite', False)

        # Incremental update: when Zarr already exists and overwrite=false,
        # we still process the tile normally — existing Zarr is opened in r+
        # mode and existing_times dedup prevents re-writing old data.
        # Only new acquisitions are appended; COG/STAC items are re-written
        # (deterministic, same data → same output).
        # The per-tile catalog reads existing records before writing (see below).

        # Processing config
        processing_config = config.get('processing') or {}
        target_res = processing_config.get('target_resolution', 30.0)
        chunk_y = processing_config.get('zarr_chunks', {}).get('y', 512)
        chunk_x = processing_config.get('zarr_chunks', {}).get('x', 512)
        cog_block = processing_config.get('cog_block_size', 256)
        generate_cog = config.get('output', {}).get('formats', {}).get(
            'cog', False
        )
        generate_preview = config.get('output', {}).get('formats', {}).get(
            'preview', False
        )
        on_time_conflict = config.get('output', {}).get(
            'on_time_conflict', 'skip'
        )
        target_crs = _mgrs_to_utm_epsg(mgrs_tile_id)

        tile_clip = processing_config.get("tile_clip", True)
        require_complete_bursts = processing_config.get("require_complete_bursts", False)
        # Policy for acquisitions missing bursts vs the track's full footprint:
        # "skip" (default — don't write a partial tile, retried next run),
        # "write" (write with a NoData gap), or "abort". require_complete_bursts
        # is kept as a back-compat alias for "abort".
        incomplete_policy = str(
            processing_config.get("incomplete_acquisition", "skip")
        ).lower()
        # Full footprint per track = distinct burst count, keyed by track_token.
        # It is the union of (a) this query's bursts and (b) bursts already
        # recorded in the tile catalog (jpl_burst_ids). The catalog is the
        # persisted footprint store: it grows monotonically as runs accumulate
        # and as ASF latency fills in, so we don't re-query history every run.
        interior_hole_max_frac = float(
            processing_config.get("interior_hole_max_frac", 0.005)
        )
        _fp_ids: dict[str, set] = {}
        if {'track_token', 'jpl_burst_id'}.issubset(df_rtc_ts.columns):
            for _tok, _g in df_rtc_ts.groupby('track_token'):
                _fp_ids.setdefault(str(_tok), set()).update(
                    str(b) for b in _g['jpl_burst_id']
                )
        # Merge in burst ids already persisted in the tile catalog.
        try:
            _cat_path = tile_dir / 'catalog.parquet'
            if _cat_path.exists():
                _cat = pd.read_parquet(_cat_path)
                _cat = _cat[_cat.get('product_type') == 'scenes'] if 'product_type' in _cat.columns else _cat
                import re as _re
                for _, _row in _cat.iterrows():
                    _ggid = str(_row.get('geometry_group_id') or '')
                    _m = _re.search(r'_TK(.+?)_N\d+', _ggid)
                    if not _m:
                        continue
                    _tok = _m.group(1).replace('-', '_')  # dashed in name -> underscore token
                    _bids = _row.get('jpl_burst_ids')
                    try:
                        _bids = json.loads(_bids) if isinstance(_bids, str) else (_bids or [])
                    except Exception:
                        _bids = []
                    if len(_bids):
                        _fp_ids.setdefault(_tok, set()).update(str(b) for b in _bids)
        except Exception as _fe:
            logger.debug("Footprint merge from catalog failed: %s", _fe)
        # Optional history look-back to establish the TRUE footprint when the
        # processed time window is short. Without it, a single-month run whose
        # only acquisitions are themselves incomplete would under-estimate the
        # footprint and miss interior gaps. Queries metadata only (no download).
        # The look-back is cached on disk per tile (.footprint_lookback.json),
        # keyed by the exact look-back window, with a TTL. A re-run over the
        # same processing window within the TTL reuses the cache and skips the
        # ASF query entirely — one fewer metadata round-trip per tile per run
        # (matters most under VPN/high-latency conditions and multi-tile runs).
        _lookback_months = int((config.get('query', {}) or {}).get('footprint_lookback_months', 6))
        _fp_cache_ttl_days = float((config.get('query', {}) or {}).get('footprint_cache_ttl_days', 7))
        if _lookback_months > 0 and time_ranges:
            _earliest = min(pd.Timestamp(s) for s, _ in time_ranges)
            _lb_start = (_earliest - pd.DateOffset(months=_lookback_months)).strftime('%Y-%m-%d')
            _lb_stop = _earliest.strftime('%Y-%m-%d')
            _cache_key = {
                'lookback_months': _lookback_months,
                'lb_start': _lb_start,
                'lb_stop': _lb_stop,
            }
            _cache_path = tile_dir / '.footprint_lookback.json'
            _cached_fp = _load_footprint_cache(_cache_path, _cache_key, _fp_cache_ttl_days)
            if _cached_fp is not None:
                for _tok, _bids in _cached_fp.items():
                    _fp_ids.setdefault(str(_tok), set()).update(str(b) for b in _bids)
                logger.info(
                    "Footprint look-back cache hit for %s (%d tracks)",
                    mgrs_tile_id, len(_cached_fp),
                )
            else:
                try:
                    from s1grits.asf_tiles import get_rtc_s1_ts_metadata_from_mgrs_tiles
                    _pol = config.get('roi', {}).get('polarization', 'VV+VH')
                    _lb = get_rtc_s1_ts_metadata_from_mgrs_tiles(
                        [mgrs_tile_id], track_numbers=None,
                        start_acq_dt=_lb_start, stop_acq_dt=_lb_stop,
                        polarizations=_pol,
                    )
                    _lb_fp: dict[str, list] = {}
                    if _lb is not None and not _lb.empty and \
                            {'track_token', 'jpl_burst_id'}.issubset(_lb.columns):
                        for _tok, _g in _lb.groupby('track_token'):
                            _bset = sorted({str(b) for b in _g['jpl_burst_id']})
                            _lb_fp[str(_tok)] = _bset
                            _fp_ids.setdefault(str(_tok), set()).update(_bset)
                        logger.info(
                            "Footprint look-back (%d mo) for %s: +%d bursts",
                            _lookback_months, mgrs_tile_id, len(_lb),
                        )
                    # Persist the cache (even when empty, so a genuinely empty
                    # window is not re-queried on every run within the TTL).
                    _save_footprint_cache(_cache_path, _cache_key, _lb_fp)
                except Exception as _le:
                    logger.warning("Footprint look-back query failed for %s: %s", mgrs_tile_id, _le)
        _track_footprint = {k: len(v) for k, v in _fp_ids.items()}
        _incomplete_acqs: list[dict] = []
        group_mode = 'acq_group'  # always acq_group mode
        features_ratio = processing_config.get('features_ratio', False)
        features_rvi = processing_config.get('features_rvi', False)
        features_glcm = processing_config.get('features_glcm', False)
        polarization = config.get('roi', {}).get('polarization', 'VV+VH')
        copol_name, crosspol_name, ratio_name, rvi_name = get_band_names(polarization)

        # Monthly config
        monthly_cfg = processing_config.get('monthly', {})
        monthly_enabled = monthly_cfg.get('enabled', False)

        # Memory / retry config
        _batch_max_retries = config.get('memory', {}).get(
            'batch_max_retries', 2
        )
        _scene_max_retries = config.get('memory', {}).get(
            'scene_max_retries', 3
        )
        _max_failed_ratio = config.get('memory', {}).get(
            'max_failed_ratio', 0.0
        )
        _scene_retry_timeout = config.get('memory', {}).get(
            'scene_retry_timeout_seconds', 600.0
        )
        _max_workers = config.get('memory', {}).get('max_download_workers', 4)
        _clear_cache = config.get('memory', {}).get(
            'clear_cache_per_batch', True
        )

        # Despeckle config from processing.despeckle
        _despeckle_cfg = config.get('processing', {}).get('despeckle', {})
        _despeckle_method = _despeckle_cfg.get('method', 'tv_bregman')
        if _despeckle_method == 'tv_bregman':
            _tv_kw = dict(_despeckle_cfg.get('tv_kwargs', {}) or {})
            _tv_kw['reg_param'] = float(
                _tv_kw.pop('tv_reg_param', _tv_kw.get('reg_param', 5.0))
            )
            _despeckle_kwargs = _tv_kw
        elif _despeckle_method == 'nlm':
            _nlm_kw = dict(_despeckle_cfg.get('nlm_kwargs', {}) or {})
            _h_raw = _nlm_kw.get('h', None)
            _nlm_kw['h'] = (
                None
                if (str(_h_raw).lower() == 'adaptive' or _h_raw is None)
                else float(_h_raw)
            )
            _nlm_kw.setdefault('patch_size', 3)
            _nlm_kw.setdefault('patch_distance', 7)
            _despeckle_kwargs = _nlm_kw
        else:
            _despeckle_kwargs = {}

        do_despeckle = bool(processing_config.get('spatial_despeckle', False))

        # 5. Batch loop
        dates = pd.to_datetime(df_rtc_ts['acq_dt']).unique()
        date_batches = chunk_time_by_strategy(dates.tolist(), batch_strategy)
        _console.print(
            f"[dim]  [3/4] Downloading [bold]{len(date_batches)}[/bold] "
            f"batch(es)...[/dim]"
        )

        all_scenes_records: list[dict] = []
        all_monthly_records: list[dict] = []
        _grid_built = False
        _master_transform = _master_width = _master_height = None
        _master_x_coords = _master_y_coords = None

        for batch_idx, batch_dates in enumerate(date_batches, 1):
            logger.info("--- Batch %d/%d ---", batch_idx, len(date_batches))

            df_batch = df_rtc_ts[
                df_rtc_ts['acq_dt'].isin(batch_dates)
            ].copy()
            df_input = adapt_enumerator_to_distmetrics(df_batch)
            df_input = validate_url_pairs(df_input)

            if df_input.empty:
                logger.warning(
                    "Batch %d has no valid URL pairs, skipping", batch_idx
                )
                continue

            # Download with batch-level retry
            _batch_success = False
            final_vv = final_vh = clean_dates = None
            for _attempt in range(_batch_max_retries + 1):
                try:
                    (
                        final_vv, prof_vv, final_vh, prof_vh, clean_dates
                    ) = load_and_despeckle_rtc_strict(
                        df_input,
                        max_workers=_max_workers,
                        do_despeckle=False,  # despeckle after mosaic, not per-burst
                        despeckle_method=_despeckle_method,
                        despeckle_kwargs=_despeckle_kwargs,
                        scene_max_retries=_scene_max_retries,
                        max_failed_ratio=_max_failed_ratio,
                        retry_timeout_seconds=_scene_retry_timeout,
                    )
                    _batch_success = True
                    break
                except RuntimeError as _err:
                    if _attempt < _batch_max_retries:
                        _wait = 30 * (2 ** _attempt)
                        logger.warning(
                            "Batch %d attempt %d/%d failed: %s. "
                            "Retrying in %ds...",
                            batch_idx, _attempt + 1,
                            _batch_max_retries + 1, _err, _wait,
                        )
                        time.sleep(_wait)
                    else:
                        logger.error(
                            "Batch %d failed after %d attempts: %s. Skipping.",
                            batch_idx, _batch_max_retries + 1, _err,
                        )

            if not _batch_success or not clean_dates:
                logger.warning(
                    "Batch %d skipped (download failed)", batch_idx
                )
                continue

            _console.print(
                f"[dim]    Batch {batch_idx}/{len(date_batches)}: "
                f"{len(clean_dates)} scene(s) downloaded[/dim]"
            )

            # Build master grid from burst footprint union on first success
            if not _grid_built:
                _master_transform, _master_width, _master_height, \
                    _master_x_coords, _master_y_coords = (
                        _build_grid_from_bursts(
                            prof_vv, target_crs, target_res
                        )
                    )
                _grid_built = True
                logger.info(
                    "Master grid from %d burst footprints: %dx%d",
                    len(prof_vv), _master_width, _master_height,
                )

            # Write scenes/ output (always)
            scenes_recs = _write_scenes_output(
                mgrs_tile_id, direction_label, tile_dir,
                final_vv, prof_vv, final_vh, prof_vh, clean_dates, df_batch,
                target_crs, target_res, generate_cog, generate_preview,
                chunk_y, chunk_x, cog_block, processing_level,
                transform=_master_transform,
                width=_master_width,
                height=_master_height,
                x_coords=_master_x_coords,
                y_coords=_master_y_coords,
                tile_clip=tile_clip,
                features_ratio=features_ratio,
                features_rvi=features_rvi,
                features_glcm=features_glcm,
                copol_name=copol_name,
                crosspol_name=crosspol_name,
                ratio_name=ratio_name,
                rvi_name=rvi_name,
                do_despeckle=do_despeckle,
                despeckle_method=_despeckle_method,
                despeckle_kwargs=_despeckle_kwargs,
                group_mode=group_mode,
                require_complete_bursts=require_complete_bursts,
                track_footprint=_track_footprint,
                track_footprint_ids=_fp_ids,
                incomplete_policy=incomplete_policy,
                incomplete_sink=_incomplete_acqs,
                interior_hole_max_frac=interior_hole_max_frac,
            )
            all_scenes_records.extend(scenes_recs)

            # Write smonthly/ output (conditional)
            if monthly_enabled:
                monthly_recs = _write_monthly_output_scenes(
                    mgrs_tile_id, direction_label, tile_dir,
                    final_vv, prof_vv, final_vh, prof_vh, clean_dates,
                    target_crs, target_res,
                    generate_cog, generate_preview,
                    chunk_y, chunk_x, cog_block,
                    on_time_conflict=on_time_conflict,
                    monthly_cfg=monthly_cfg,
                    processing_level=processing_level,
                    group_mode=group_mode,
                    df_batch=df_batch,
                    transform=_master_transform,
                    width=_master_width,
                    height=_master_height,
                    x_coords=_master_x_coords,
                    y_coords=_master_y_coords,
                    tile_clip=tile_clip,
                    features_ratio=features_ratio,
                    features_rvi=features_rvi,
                    features_glcm=features_glcm,
                    copol_name=copol_name,
                    crosspol_name=crosspol_name,
                    ratio_name=ratio_name,
                    rvi_name=rvi_name,
                )
                all_monthly_records.extend(monthly_recs)

            if _clear_cache:
                del final_vv, final_vh, df_batch, df_input
                gc.collect()

        # Write a per-tile processing report (coverage-filtered tracks +
        # incomplete acquisitions) to disk. This is the reliable record in
        # parallel mode, where worker console/log output is suppressed; the
        # parent also prints a consolidated summary from the returned dict.
        if _dropped_tracks or _incomplete_acqs:
            try:
                tile_dir.mkdir(parents=True, exist_ok=True)
                (tile_dir / 'processing_report.json').write_text(json.dumps({
                    "tile_id": mgrs_tile_id,
                    "coverage_threshold_frac": float(
                        config.get('roi', {}).get('min_tile_coverage_frac', 0.0) or 0.0
                    ),
                    "dropped_tracks": _dropped_tracks,
                    "incomplete_policy": incomplete_policy,
                    "incomplete_acquisitions": _incomplete_acqs,
                }, indent=2))
            except Exception as _me:
                logger.warning("Could not write processing_report.json: %s", _me)
            if _incomplete_acqs:
                _verb = "skipped" if incomplete_policy == "skip" else "flagged"
                _console.print(
                    f"[yellow]  {len(_incomplete_acqs)} incomplete acquisition(s) "
                    f"{_verb} — see {mgrs_tile_id}/processing_report.json[/yellow]"
                )

        # 6. Write per-tile catalog.parquet (scenes + monthly combined)
        monthly_info = (
            f", {len(all_monthly_records)} months" if monthly_enabled else ""
        )
        _console.print(
            f"[dim]  [4/4] Writing catalog ({len(all_scenes_records)} scenes"
            f"{monthly_info})...[/dim]"
        )

        df_new = pd.DataFrame(all_scenes_records)
        dfs_new = [df_new]

        if all_monthly_records:
            df_m = pd.DataFrame(all_monthly_records)
            dfs_new.append(df_m)

        df_new = pd.concat(dfs_new, ignore_index=True)
        tile_catalog_path = tile_dir / 'catalog.parquet'

        # ---- Read-merge-dedup with existing catalog ----
        # Always merge with any existing catalog (even when overwrite=True): the
        # per-tile catalog is shared across product types / directions, so the
        # newly regenerated records must win on conflicting keys (keep='last')
        # without discarding sibling products. overwrite governs raster outputs,
        # not the catalog index.
        df_existing = None
        if tile_catalog_path.exists():
            try:
                df_existing = pd.read_parquet(tile_catalog_path)
            except Exception as _e:
                logger.warning(
                    "Could not read existing tile catalog %s; writing new "
                    "records only: %s", tile_catalog_path, _e,
                )
                df_existing = None

        if df_existing is not None:
            try:
                df_merged = pd.concat([df_existing, df_new], ignore_index=True)
                # Normalise temporal columns to tz-naive so tz-aware records
                # left by older builds and tz-naive new records sort together.
                for _tcol in ('datetime', 'start_datetime', 'end_datetime'):
                    if _tcol in df_merged.columns:
                        df_merged[_tcol] = pd.to_datetime(
                            df_merged[_tcol], utc=True, errors='coerce'
                        ).dt.tz_localize(None)
                # Dedup: keep new records over old on key columns
                dedup_keys = [
                    k for k in ['tile_id', 'flight_direction',
                                'datetime', 'product_type', 'month']
                    if k in df_merged.columns
                ]
                if dedup_keys:
                    before = len(df_merged)
                    df_merged = df_merged.drop_duplicates(
                        subset=dedup_keys, keep='last',
                    )
                    added = len(df_merged) - len(df_existing)
                    dropped = before - len(df_merged)
                    logger.info(
                        "Tile catalog: merged %d existing + %d new → %d rows "
                        "(+%d added, %d dedup dropped)",
                        len(df_existing), len(df_new), len(df_merged),
                        added, dropped,
                    )
                df_catalog = df_merged.sort_values(
                    [k for k in dedup_keys if k in df_merged.columns]
                ).reset_index(drop=True)
            except Exception as _e:
                # Never silently drop existing data: keep the union (new wins
                # via order) even if dedup/sort fails for some reason.
                logger.error(
                    "Tile catalog merge failed; preserving existing+new union "
                    "without dedup/sort: %s", _e, exc_info=True,
                )
                df_catalog = pd.concat([df_existing, df_new], ignore_index=True)
        else:
            df_catalog = df_new

        from s1grits.atomic_write import atomic_write_parquet
        atomic_write_parquet(df_catalog, tile_catalog_path)
        logger.info(
            "Tile catalog written: %s (%d rows)", tile_catalog_path, len(df_catalog)
        )

        _console.print(f"[dim]  Done: {tile_dir}[/dim]")

        return {
            'status':         'success',
            'error':          None,
            'tile_dir':       str(tile_dir),
            'catalog_path':   str(tile_catalog_path),
            'written_scenes': len(all_scenes_records),
            'written_months': sorted(
                {r['month'] for r in all_monthly_records}
            ),
            'dropped_tracks': _dropped_tracks,
            'incomplete_acquisitions': _incomplete_acqs,
        }

    except Exception as e:
        logger.error(
            "Tile %s failed: %s", mgrs_tile_id, e, exc_info=True
        )
        return {
            'status':       'failed',
            'error':        str(e),
            'tile_dir':     None,
            'catalog_path': None,
        }


# ---------------------------------------------------------------------------
# Memory-budget wrapper (for parallel processing)
# ---------------------------------------------------------------------------

def _process_scenes_tile_with_memory_budget(
    mgrs_tile_id: str,
    time_ranges: list[tuple[str, str]],
    config: dict,
    output_root: Path,
    memory_budget_gb: float,
) -> dict[str, Any]:
    """
    Wrapper: force memory budget for each worker in ProcessPoolExecutor.

    Runs quietly: many workers share one terminal, so per-tile prints, status
    spinners and tqdm bars would garble each other and the parent progress bar.
    We silence them here (only this worker process is affected) and let the
    parent's progress bar be the single live display; per-tile detail still
    lands in the log file.
    """
    import copy
    import os
    os.environ['TQDM_DISABLE'] = '1'   # silence download tqdm bars in this worker
    console.quiet = True               # silence the module-global console
    config_copy = copy.deepcopy(config)

    if 'memory' not in config_copy:
        config_copy['memory'] = {}
    config_copy['memory']['max_memory_gb'] = memory_budget_gb
    config_copy['memory']['batch_strategy'] = 'auto'

    return process_single_scenes_tile(
        mgrs_tile_id, time_ranges, config_copy, output_root, quiet=True
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_scenes_workflow(config_path: str | Path, overrides: dict | None = None) -> dict[str, dict]:
    """
    Main scenes workflow: YAML config -> scenes/ + optional smonthly/
    per MGRS tile.

    Parameters
    ----------
    config_path : str or Path
        Path to YAML configuration file.

    Returns
    -------
    dict
        {mgrs_tile_id: {'tile_dir', 'catalog_path', 'written_scenes',
         'status', 'error'}}
    """
    from s1grits.workflow import apply_output_overrides_and_stac
    config = load_config(config_path)
    config = apply_output_overrides_and_stac(config, overrides)

    despeckle_token = _get_despeckle_token(config)
    dir_token = {"ASCENDING": "ASCENDING", "DESCENDING": "DESCENDING"}.get(
        config.get('roi', {}).get('flight_direction'), ""
    )

    # Build band token from feature flags (e.g. '_Ratio_RVI')
    _proc = config.get('processing', {})
    _band_parts = []
    if _proc.get('features_ratio', False):
        _band_parts.append('Ratio')
    if _proc.get('features_rvi', False):
        _band_parts.append('RVI')
    if _proc.get('features_glcm', False):
        _band_parts.append('GLCM')
    _band_token = ('_' + '_'.join(_band_parts)) if _band_parts else ''

    # Composite token: appended when monthly compositing is enabled in scenes workflow
    _monthly_enabled = _proc.get('monthly', {}).get('enabled', False)
    _composite_token = '_composite' if _monthly_enabled else ''

    # Suffix: no suffix — multi-modal DataCube writes to base_dir directly
    mgrs_tile_ids = enumerate_mgrs_tiles(config)
    wkt = config.get('roi', {}).get('wkt')
    time_ranges = parse_time_range_config(config, wkt, allow_current_month=True)

    # Output root: base_dir directly, no suffix appended
    output_root = Path(config.get('output', {}).get('base_dir', './output'))
    output_root.mkdir(parents=True, exist_ok=True)

    polarization = config.get('roi', {}).get('polarization', 'VV+VH')

    logger.info(
        "Scenes output root: %s (despeckle=%s)",
        output_root, despeckle_token or "none",
    )

    # Parallel configuration
    parallel_cfg = config.get('parallel', {})
    parallel_enabled = parallel_cfg.get('enabled', False)
    max_workers = parallel_cfg.get('max_workers', 2)

    results: dict[str, dict] = {}
    all_catalogs: list[pd.DataFrame] = []

    # Acquire file lock — prevents concurrent writes to same base_dir
    _lock_info = acquire_lock(str(output_root))
    try:
        if parallel_enabled:
            try:
                import psutil
                total_mem_gb = psutil.virtual_memory().available / (1024**3)
                mem_per_worker = total_mem_gb / max_workers / 1.2
            except ImportError:
                total_mem_gb = 8.0
                mem_per_worker = total_mem_gb / max_workers

            logger.info(
                "Parallel processing: %d workers, %.1f GB/worker",
                max_workers, mem_per_worker,
            )

            from concurrent.futures import ProcessPoolExecutor, as_completed
            from rich.progress import (
                Progress, BarColumn, TextColumn, TimeElapsedColumn,
            )

            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                future_to_tile = {
                    executor.submit(
                        _process_scenes_tile_with_memory_budget,
                        tile, time_ranges, config, output_root, mem_per_worker,
                    ): tile
                    for tile in mgrs_tile_ids
                }

                with Progress(
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    "[progress.percentage]{task.percentage:>3.0f}%",
                    TimeElapsedColumn(),
                    console=console,
                ) as progress:
                    _n = len(future_to_tile)
                    task_id = progress.add_task(
                        f"Processing {_n} MGRS tiles", total=_n,
                    )

                    _done = _ok = _fail = 0
                    for future in as_completed(future_to_tile):
                        tile_id = future_to_tile[future]
                        try:
                            results[tile_id] = future.result()
                            status = results[tile_id]['status']
                            if status == 'success':
                                _ok += 1
                                logger.info("Completed: %s", tile_id)
                            else:
                                _fail += 1
                                logger.warning(
                                    "Failed: %s - %s",
                                    tile_id, results[tile_id]['error'],
                                )
                        except Exception as e:
                            _fail += 1
                            results[tile_id] = {
                                'status': 'failed', 'error': str(e),
                            }
                            logger.error(
                                "Exception in %s: %s", tile_id, e, exc_info=True,
                            )

                        _done += 1
                        progress.update(
                            task_id, advance=1,
                            description=(
                                f"Tiles {_done}/{_n}  "
                                f"[green]{_ok} ok[/green] "
                                f"[red]{_fail} failed[/red]  "
                                f"(last: {tile_id})"
                            ),
                        )
        else:
            # Serial processing
            logger.info("Serial processing mode")
            for mgrs_tile_id in mgrs_tile_ids:
                result = process_single_scenes_tile(
                    mgrs_tile_id, time_ranges, config, output_root
                )
                results[mgrs_tile_id] = result

        # Read existing global catalog (from other workflows) for merge safety
        _global_catalog_path = output_root / 'catalog.parquet'
        if _global_catalog_path.exists():
            try:
                all_catalogs.append(pd.read_parquet(_global_catalog_path))
                logger.info("Loaded existing global catalog: %d rows", len(all_catalogs[-1]))
            except Exception as _e:
                logger.warning("Could not read existing global catalog: %s", _e)

        # Collect tile catalogs for merge
        for mgrs_tile_id, result in results.items():
            if result['status'] == 'success' and result.get('catalog_path'):
                try:
                    all_catalogs.append(
                        pd.read_parquet(result['catalog_path'])
                    )
                except Exception as e:
                    logger.warning(
                        "Could not read tile catalog for merge: %s", e
                    )

        # Merge global catalog (read-merge-dedup across all tiles)
        if all_catalogs:
            df_global = pd.concat(all_catalogs, ignore_index=True)
            if 'datetime' in df_global.columns:
                df_global['datetime'] = pd.to_datetime(
                    df_global['datetime'], utc=True, errors='coerce'
                ).dt.tz_localize(None)
            # Dedup across tile catalogs (same keys as per-tile dedup)
            _g_dedup_keys = [
                k for k in ['tile_id', 'flight_direction',
                            'datetime', 'product_type', 'month']
                if k in df_global.columns
            ]
            if _g_dedup_keys:
                _g_before = len(df_global)
                df_global = df_global.drop_duplicates(
                    subset=_g_dedup_keys, keep='last',
                )
                _g_dropped = _g_before - len(df_global)
                if _g_dropped > 0:
                    logger.info(
                        "Global catalog dedup: %d → %d rows (%d dropped)",
                        _g_before, len(df_global), _g_dropped,
                    )
            # Validate collection_id mapping before writing
            validate_collection_mapping(df_global, raise_on_error=True)

            df_global = (
                df_global
                .sort_values(
                    ['tile_id', 'product_type', 'datetime'],
                    na_position='last',
                )
                .reset_index(drop=True)
            )
            from s1grits.atomic_write import atomic_write_parquet
            atomic_write_parquet(df_global, _global_catalog_path)
            logger.info(
                "Global catalog: %s (%d rows)",
                _global_catalog_path, len(df_global),
            )
        else:
            logger.warning(
                "No tile catalogs to merge - global catalog not written"
            )
            df_global = pd.DataFrame()

        # Write one collection.json per product_type found in the data
        if not df_global.empty:
            from s1grits.stac_builder import write_stac_collection
            _col_ids = df_global["collection_id"].dropna().unique() if "collection_id" in df_global.columns else []
            for _cid in _col_ids:
                _df_sub = df_global[df_global["collection_id"] == _cid]
                if _df_sub.empty:
                    continue
                try:
                    write_stac_collection(_df_sub, str(output_root), str(_cid), polarization)
                    logger.info("STAC collection written: %s (%d items)", _cid, len(_df_sub))
                except Exception as _stac_e:
                    logger.warning("STAC collection failed for %s: %s", _cid, _stac_e)
            from s1grits.catalog_sync import update_root_catalog
            update_root_catalog(output_root, df_global)

        n_ok = sum(
            1 for r in results.values() if r['status'] == 'success'
        )
        n_all = len(results)
        total_scenes = sum(
            r.get('written_scenes', 0) for r in results.values()
            if r['status'] == 'success'
        )
        logger.info(
            "Scenes workflow complete: %d/%d tiles succeeded, %d scenes written",
            n_ok, n_all, total_scenes,
        )

        return results
    finally:
        release_lock(_lock_info)
