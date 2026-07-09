# ==============================================================================
#  asf_output_writing.py
#  Output Writing: COG, Zarr, Preview PNG, STAC, and Catalog management.
#
#  Refactored from original asf_io.py for Cython compatibility.
#  Changes vs. original:
#    - Nested normalize_band -> top-level _normalize_band_to_uint8
#    - Nested prep / _prep -> top-level _prep_array
#    - Nested _read_band -> top-level _read_zarr_band_as_linear
#    - Nested _flush_month x3 (nonlocal) -> top-level _flush_month_crossUTM,
#      _flush_month_tileUTM, _flush_month_median_cube with explicit parameters
# ==============================================================================

import cv2
import gc
import json
import logging
import os
import re
import shutil
import time
import traceback
import warnings

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyproj
import rasterio
import rasterio.crs as rcrs
import zarr

from rasterio.features import rasterize


def _zarr_append(g: "zarr.Group", var_name: str, data: "np.ndarray") -> None:
    """Append one slice along axis-0 to a zarr v3 array (v3 removed Array.append())."""
    arr = g[var_name]
    t = arr.shape[0]
    arr.resize((t + 1,) + arr.shape[1:])
    arr[t, ...] = data
from rasterio.transform import Affine, array_bounds
from rasterio.warp import reproject, Resampling, transform_bounds
from rasterio.windows import from_bounds, Window
from rasterio.windows import transform as window_transform
from shapely import wkt as shapely_wkt
from shapely.geometry import mapping
from shapely.ops import transform as shp_transform
from tqdm.auto import tqdm

from s1grits.asf_io import (
    get_band_names,
    _mgrs_to_utm_epsg,
    NODATA_SENTINEL,
    DB_SCALE_FACTOR,
    METERS_PER_DEGREE_LAT,
    UTM_NORTH_BASE,
    UTM_SOUTH_BASE,
    MAX_BACKOFF_EXPONENT,
    DEFAULT_RETRY_TIMEOUT_S,
    DEFAULT_CHUNK_SIZE,
    COG_BLOCK_SIZE,
    DEFAULT_RES_M,
    PREVIEW_RES_M,
    DOWNLOAD_CHUNK_BYTES,
)
from s1grits.asf_array_processing import (
    despeckle_2d,
    _get_texture_band_names,
    compute_glcm_texture_bands,
)
from s1grits.mgrs_burst_data import get_mgrs_data_path
from s1grits.atomic_write import atomic_path
from s1grits.stac_builder import write_stac_item, write_stac_collection
from s1grits.canonical_catalog_schema import normalize_catalog_record, rebase_catalog_paths

# Optional dependency: sar_texture is not part of the core package.
# Import at module level so Cython can compile this file without a function-body import.
# If the module is absent, _compute_glcm_texture is set to None and the feature is skipped at runtime.
try:
    from s1grits.sar_texture import compute_glcm_texture as _compute_glcm_texture
except ImportError:
    _compute_glcm_texture = None

# =========================================================
#  Module-Level Constants — imported from asf_io (single source of truth)
# =========================================================
# NODATA_SENTINEL, DB_SCALE_FACTOR, METERS_PER_DEGREE_LAT, UTM_NORTH_BASE,
# UTM_SOUTH_BASE, MAX_BACKOFF_EXPONENT, DEFAULT_RETRY_TIMEOUT_S,
# DEFAULT_CHUNK_SIZE, COG_BLOCK_SIZE, DEFAULT_RES_M, PREVIEW_RES_M,
# DOWNLOAD_CHUNK_BYTES  — all re-exported via the import block above.


# =========================================================
#  Shared Array Helpers (extracted from nested functions)
# =========================================================

def _normalize_band_to_uint8(arr, vmin, vmax):
    """Normalize float array to uint8 [0-255], mapping NaN to 0."""
    nan_mask = np.isnan(arr)
    arr_norm = np.clip((arr - vmin) / (vmax - vmin), 0, 1)
    arr_norm = np.where(nan_mask, 0, arr_norm)
    return (arr_norm * 255).astype(np.uint8)


def _prep_array(a, nodata_value):
    """Replace NaN with nodata_value and cast to float32 for file writing."""
    return np.where(np.isnan(a), nodata_value, a).astype("float32")


def _read_zarr_band_as_linear(grp, name):
    """
    Read a named band from a Zarr month group and return as linear float32.

    Tries the linear band first; if absent, looks for a dB band and converts.
    Returns None if neither is found.
    Extracted from merge_acq_group_zarrs for Cython compatibility.
    """
    if name in grp:
        return grp[name][:].astype(np.float32)
    db_name = name + "_dB"
    if db_name in grp:
        arr_db = grp[db_name][:].astype(np.float32)
        lin = np.power(10.0, arr_db / 10.0)
        lin[~np.isfinite(lin)] = np.nan
        return lin
    return None


def _list_months_from_zarr(store) -> list:
    """Return sorted list of month-key strings present in a Zarr group store.

    Month groups follow the naming convention 'YYYY-MM' (length 7, dash at
    index 4).  Extracted from merge_acq_group_zarrs for Cython compatibility.
    """
    keys = []
    for k in store.group_keys():
        if len(k) == 7 and k[4] == "-":
            keys.append(k)
    return sorted(keys)


def _write_zarr_dataset(grp, name, arr, chunk_y, chunk_x, on_time_conflict):
    """Write a 2-D float32 array as a chunked Zarr dataset within *grp*.

    If a dataset named *name* already exists:
      - ``on_time_conflict == 'overwrite'``: delete and re-create it.
      - anything else: return without writing.

    Extracted from merge_acq_group_zarrs for Cython compatibility
    (the original nested ``_write_arr`` closed over ``chunk_y``,
    ``chunk_x``, and ``on_time_conflict`` via a closure, which Cython
    cannot compile reliably).
    """
    if name in grp:
        if on_time_conflict == "overwrite":
            del grp[name]
        else:
            return
    grp.create_array(
        name, data=arr,
        chunks=(chunk_y, chunk_x),
        fill_value=float("nan"),
        overwrite=True,
    )


# =========================================================
#  Part 1: Grid Builders
# =========================================================

def _build_grid_from_bursts(profiles, target_crs, target_res, align_pixels=True):
    glob_minx, glob_miny = float('inf'), float('inf')
    glob_maxx, glob_maxy = float('-inf'), float('-inf')
    logging.info("[Grid] Union of %d bursts in %s...", len(profiles), target_crs)

    valid = False
    for p in profiles:
        if p['height'] == 0:
            continue
        src_b = array_bounds(p['height'], p['width'], p['transform'])
        try:
            dst_b = transform_bounds(p['crs'], target_crs, *src_b)
            glob_minx = min(glob_minx, dst_b[0]); glob_miny = min(glob_miny, dst_b[1])
            glob_maxx = max(glob_maxx, dst_b[2]); glob_maxy = max(glob_maxy, dst_b[3])
            valid = True
        except Exception as _grid_exc:
            logging.debug("Skipping burst during grid union: %s", _grid_exc)
            continue

    if not valid:
        raise ValueError("Grid generation failed.")

    res = float(target_res)
    if align_pixels:
        glob_minx = np.floor(glob_minx / res) * res
        glob_miny = np.floor(glob_miny / res) * res
        glob_maxx = np.ceil(glob_maxx / res) * res
        glob_maxy = np.ceil(glob_maxy / res) * res

    width = int(round((glob_maxx - glob_minx) / res))
    height = int(round((glob_maxy - glob_miny) / res))
    transform = Affine(res, 0.0, glob_minx, 0.0, -res, glob_maxy)

    x_coords = (glob_minx + (np.arange(width) + 0.5) * res).astype("float64")
    y_coords = (glob_maxy - (np.arange(height) + 0.5) * res).astype("float64")
    return transform, width, height, x_coords, y_coords


def _build_grid_from_geoms(geoms, target_crs, target_res, align_pixels=True):
    """Grid from the union of EPSG:4326 footprint geometries — the FULL-query
    variant of ``_build_grid_from_bursts``.

    ``_build_grid_from_bursts`` unions the raster profiles of the first
    downloaded batch, so the master grid depended on which era (and which
    download luck) that batch happened to cover: an early, sparser era (e.g.
    2016-Q4 in a full-archive run) could lock a grid that crops bursts only
    present in later, stabler eras. This builder instead unions the CMR
    metadata footprints of EVERY burst in the whole query window — available
    before any download — so the grid is era-independent and deterministic
    for a given query. Snapping is identical (floor/ceil to the resolution
    lattice), so grids from either builder stay mutually aligned.
    """
    glob_minx, glob_miny = float('inf'), float('inf')
    glob_maxx, glob_maxy = float('-inf'), float('-inf')
    valid = False
    for g in geoms:
        if g is None or getattr(g, 'is_empty', False):
            continue
        try:
            dst_b = transform_bounds("EPSG:4326", target_crs, *g.bounds)
            glob_minx = min(glob_minx, dst_b[0]); glob_miny = min(glob_miny, dst_b[1])
            glob_maxx = max(glob_maxx, dst_b[2]); glob_maxy = max(glob_maxy, dst_b[3])
            valid = True
        except Exception as _exc:
            logging.debug("Skipping geometry during grid union: %s", _exc)
            continue
    if not valid:
        raise ValueError("Grid generation failed (no usable geometries).")

    res = float(target_res)
    if align_pixels:
        glob_minx = np.floor(glob_minx / res) * res
        glob_miny = np.floor(glob_miny / res) * res
        glob_maxx = np.ceil(glob_maxx / res) * res
        glob_maxy = np.ceil(glob_maxy / res) * res

    width = int(round((glob_maxx - glob_minx) / res))
    height = int(round((glob_maxy - glob_miny) / res))
    transform = Affine(res, 0.0, glob_minx, 0.0, -res, glob_maxy)

    x_coords = (glob_minx + (np.arange(width) + 0.5) * res).astype("float64")
    y_coords = (glob_maxy - (np.arange(height) + 0.5) * res).astype("float64")
    return transform, width, height, x_coords, y_coords


def _build_grid_from_mgrs_tile(
    mgrs_tile_id: str,
    target_crs: str,
    target_res: float,
    align_pixels: bool = True,
):
    """
    Build grid from MGRS tile bounds (ensures standard tile size).

    Args:
        mgrs_tile_id: MGRS tile identifier (e.g., '17MPV')
        target_crs: Target coordinate reference system
        target_res: Target resolution in meters
        align_pixels: Whether to align pixels to resolution grid

    Returns:
        tuple: (transform, width, height, x_coords, y_coords)
    """
    logging.info("[Grid] Building from MGRS tile %s bounds in %s...", mgrs_tile_id, target_crs)

    mgrs_wkt = _get_mgrs_tile_geometry_wkt(mgrs_tile_id)

    crs_ll = pyproj.CRS.from_epsg(4326)
    crs_t = pyproj.CRS.from_user_input(target_crs)
    proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
    geom_proj = shp_transform(proj, shapely_wkt.loads(mgrs_wkt))

    minx, miny, maxx, maxy = geom_proj.bounds

    res = float(target_res)
    if align_pixels:
        minx = np.floor(minx / res) * res
        miny = np.floor(miny / res) * res
        maxx = np.ceil(maxx / res) * res
        maxy = np.ceil(maxy / res) * res

    width = int(round((maxx - minx) / res))
    height = int(round((maxy - miny) / res))
    transform = Affine(res, 0.0, minx, 0.0, -res, maxy)

    x_coords = (minx + (np.arange(width) + 0.5) * res).astype("float64")
    y_coords = (maxy - (np.arange(height) + 0.5) * res).astype("float64")

    logging.info(
        "[Grid] MGRS tile grid: %dx%d (extent: %.1fkm x %.1fkm)",
        width, height, (maxx - minx) / 1000, (maxy - miny) / 1000,
    )
    return transform, width, height, x_coords, y_coords


# =========================================================
#  Part 2: Statistical & Geometry Helpers
# =========================================================



def _rasterize_roi_mask(roi_geom_proj, out_shape, out_transform, all_touched=False):
    mask = rasterize(
        [(mapping(roi_geom_proj), 1)],
        out_shape=out_shape,
        transform=out_transform,
        fill=0,
        dtype="uint8",
        all_touched=all_touched,
    )
    return mask.astype(bool)


def _get_mgrs_tile_geometry_wkt(mgrs_tile_id: str) -> str:
    """Look up the WKT geometry for *mgrs_tile_id* from the bundled parquet.

    The parquet file is read once per call.  Call sites that invoke this
    function repeatedly for the same tile (e.g. once per month inside a flush
    loop) should call it once before the loop and cache the result, rather than
    calling it on every iteration.
    """
    gdf = gpd.read_parquet(get_mgrs_data_path())
    row = gdf[gdf["mgrs_tile_id"] == mgrs_tile_id]
    if row.empty:
        raise ValueError(f"MGRS tile not found in mgrs.parquet: {mgrs_tile_id}")
    geom = row.iloc[0].geometry
    return geom.wkt


def _clip_arrays_to_wkt_4326(
    arrays: list,
    wkt_4326: str,
    target_crs: str,
    master_transform,
    height: int,
    width: int,
    all_touched: bool = False,
):
    """
    Clip arrays (H,W) in target_crs/master_transform to polygon WKT in EPSG:4326.
    Returns clipped_arrays, new_transform.
    """
    crs_ll = pyproj.CRS.from_epsg(4326)
    crs_t = pyproj.CRS.from_user_input(target_crs)
    proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
    geom_proj = shp_transform(proj, shapely_wkt.loads(wkt_4326))

    minx, miny, maxx, maxy = geom_proj.bounds
    win = from_bounds(minx, miny, maxx, maxy, transform=master_transform)

    r0 = max(0, int(np.floor(win.row_off)))
    c0 = max(0, int(np.floor(win.col_off)))
    r1 = min(height, int(np.ceil(win.row_off + win.height)))
    c1 = min(width, int(np.ceil(win.col_off + win.width)))

    if r1 <= r0 or c1 <= c0:
        raise ValueError("Clip window is empty; check WKT/CRS/transform.")

    clipped = [a[r0:r1, c0:c1] for a in arrays]

    clip_window = Window(col_off=c0, row_off=r0, width=c1 - c0, height=r1 - r0)
    new_transform = window_transform(clip_window, master_transform)

    mask = rasterize(
        [(mapping(geom_proj), 1)],
        out_shape=(r1 - r0, c1 - c0),
        transform=new_transform,
        fill=0,
        dtype="uint8",
        all_touched=all_touched,
    ).astype(bool)

    inv = ~mask
    for a in clipped:
        a[inv] = np.nan

    return clipped, new_transform


# =========================================================
#  Part 3: Preview Generation
# =========================================================

def _generate_preview_png(
    vv_db: np.ndarray,
    vh_db: np.ndarray,
    ratio: np.ndarray | None,
    src_transform,
    src_crs: str,
    output_path: str,
    preview_res: float = 300.0,
    preview_crs: str = "EPSG:4326",
    vv_min: float = -25.0,
    vv_max: float = 5.0,
    vh_min: float = -30.0,
    vh_max: float = 0.0,
    ratio_min: float = 0.0,
    ratio_max: float = 3.0,
):
    """
    Generate preview PNG with VV and VH bands, optionally including Ratio.
    Reproject to EPSG:4326 at coarse resolution (default 300m).

    Output format:
    - When ratio is None: 2-band image [VH, VV]
    - When ratio is present: 3-band RGB image [B=Ratio, G=VV, R=VH]

    Args:
        vv_db: VV (copolarized) backscatter in dB.
        vh_db: VH (crosspolarized) backscatter in dB.
        ratio: Optional VH/VV ratio (None if features_ratio disabled).
        src_transform: Affine transform of source array.
        src_crs: Source coordinate reference system.
        output_path: Output file path for PNG.
        preview_res: Resolution in meters (converted to degrees if preview_crs is geographic).
        preview_crs: Target CRS for preview (default EPSG:4326).
        vv_min/vv_max: Normalization range for VV band.
        vh_min/vh_max: Normalization range for VH band.
        ratio_min/ratio_max: Normalization range for Ratio band (ignored if ratio is None).
    """
    h, w = vv_db.shape
    src_bounds = array_bounds(h, w, src_transform)

    dst_bounds = transform_bounds(src_crs, preview_crs, *src_bounds)
    minx, miny, maxx, maxy = dst_bounds

    if preview_crs == "EPSG:4326":
        center_lat = (miny + maxy) / 2.0
        meters_per_degree_lat = METERS_PER_DEGREE_LAT
        meters_per_degree_lon = METERS_PER_DEGREE_LAT * np.cos(np.radians(center_lat))
        preview_res_deg = preview_res / ((meters_per_degree_lat + meters_per_degree_lon) / 2.0)
    else:
        preview_res_deg = preview_res

    out_width = int(np.ceil((maxx - minx) / preview_res_deg))
    out_height = int(np.ceil((maxy - miny) / preview_res_deg))
    out_transform = Affine(preview_res_deg, 0.0, minx, 0.0, -preview_res_deg, maxy)

    # Build list of bands to process, handling None values for optional features.
    bands_to_process = [
        ('VV_dB', vv_db, vv_min, vv_max),
        ('VH_dB', vh_db, vh_min, vh_max),
    ]
    if ratio is not None:
        bands_to_process.append(('Ratio', ratio, ratio_min, ratio_max))

    # Allocate reprojection arrays only for non-None bands.
    reproj_dict = {}
    for band_name, src_arr, _, _ in bands_to_process:
        reproj_dict[band_name] = np.full((out_height, out_width), np.nan, dtype=np.float32)

    # Reproject each band.
    for band_name, src_arr, _, _ in bands_to_process:
        if src_arr is None:
            continue
        reproject(
            source=src_arr,
            destination=reproj_dict[band_name],
            src_transform=src_transform,
            src_crs=src_crs,
            dst_transform=out_transform,
            dst_crs=preview_crs,
            resampling=Resampling.average,
            src_nodata=np.nan,
            dst_nodata=np.nan,
        )

    # Normalize bands and build RGB stack.
    normalized_bands = []
    for band_name, _, band_min, band_max in bands_to_process:
        norm_band = _normalize_band_to_uint8(reproj_dict[band_name], band_min, band_max)
        normalized_bands.append(norm_band)

    # RGB output: stack in order [VV_dB, VH_dB, Ratio] if all present.
    # OpenCV's imwrite only accepts 1, 3 or 4 channel images, so a bare
    # 2-channel (VV, VH) stack must be promoted to 3 channels.
    if len(normalized_bands) == 2:
        # Only VV and VH (Ratio disabled): synthesise the missing Ratio
        # (blue) channel as zeros to keep the documented B=Ratio, G=VV, R=VH
        # ordering and a valid 3-channel image.
        blue = np.zeros_like(normalized_bands[0])
        rgb = np.stack([blue, normalized_bands[0], normalized_bands[1]], axis=-1)
    elif len(normalized_bands) == 3:
        # VV, VH, and Ratio: create 3-band RGB (B=Ratio, G=VV, R=VH).
        rgb = np.stack([normalized_bands[2], normalized_bands[0], normalized_bands[1]], axis=-1)
    elif len(normalized_bands) == 1:
        # Single band: write as grayscale (1 channel is valid for imwrite).
        rgb = normalized_bands[0]
    else:
        # Unexpected case; default to stacking whatever we have.
        rgb = np.stack(normalized_bands, axis=-1)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    cv2.imwrite(output_path, rgb)

    return {
        "path": output_path,
        "width": out_width,
        "height": out_height,
        "crs": preview_crs,
        "resolution": preview_res_deg,
        "bounds": dst_bounds,
    }


# =========================================================
#  Part 4: Mosaic Helper
# =========================================================

def _mosaic_align(
    indices: list,
    arr_list: list,
    prof_list: list,
    height: int,
    width: int,
    master_transform,
    target_crs,
):
    """
    Reproject and mosaic a list of burst arrays onto the master grid.

    Uses a first-valid pixel strategy: the first burst covering each pixel wins,
    avoiding radiometric gradients in burst overlap regions.

    Args:
        indices: List of integer indices into arr_list / prof_list to include.
        arr_list: List of 2-D numpy arrays (one per burst); None entries are skipped.
        prof_list: List of rasterio profile dicts with 'transform', 'crs', 'nodata'.
        height: Output grid height in pixels.
        width: Output grid width in pixels.
        master_transform: Affine transform of the output grid.
        target_crs: CRS of the output grid.

    Returns:
        Float32 numpy array of shape (height, width), or None if no valid bursts.
    """
    valid_idx = [i for i in indices if arr_list[i] is not None]
    if not valid_idx:
        return None

    NODATA_TMP = NODATA_SENTINEL

    out = np.full((height, width), np.nan, dtype="float32")

    for i in valid_idx:
        tmp = np.full((height, width), NODATA_TMP, dtype="float32")

        src_nd = prof_list[i].get("nodata", None)
        if src_nd is None or np.isnan(src_nd):
            src_nd = np.nan

        reproject(
            source=arr_list[i].astype("float32", copy=False),
            destination=tmp,
            src_transform=prof_list[i]["transform"],
            src_crs=prof_list[i]["crs"],
            dst_transform=master_transform,
            dst_crs=target_crs,
            resampling=Resampling.nearest,
            src_nodata=float(src_nd),
            dst_nodata=NODATA_TMP,
            init_dest_nodata=True,
        )

        valid_mask = (tmp != NODATA_TMP) & np.isfinite(tmp)
        unfilled = np.isnan(out)
        out[unfilled & valid_mask] = tmp[unfilled & valid_mask]

    return out


# =========================================================
#  Part 5: Monthly Flush Helpers (extracted from nested functions)
# =========================================================

def _flush_month_crossUTM(
    m_str: str,
    month_buf: dict,
    master_transform,
    height: int,
    width: int,
    catalog_records: list,
    existing_months: set,
    on_time_conflict: str,
    monthly_despeckle: bool,
    despeckle_method: str,
    despeckle_kwargs: dict,
    min_valid_lin: float,
    eps_lin: float,
    roi_mask,
    mgrs_tile_id: str,
    target_crs: str,
    mgrs_wkt: str,
    texture_cfg,
    generate_cog: bool,
    overwrite_cog: bool,
    out_cog_dir: str,
    flight_suffix: str,
    cog_block: int,
    copol_name: str,
    crosspol_name: str,
    ratio_name: str,
    rvi_name: str,
    generate_preview: bool,
    preview_dir: str,
    preview_res: float,
    zarr_path: str,
    output_root: str,
    processing_level: str,
    g,
    processed_log: list,
    polarization: str,
    features_ratio: bool = True,
    features_rvi: bool = True,
    features_glcm: bool = False,
    tile_clip: bool = True,
    direction_label: str = "",
    product_label: str = "monthly",
    feature_suffix: str = "",
    layout_mode: str = "stac",
):
    """
    Flush one month's buffered linear arrays to COG, Zarr, Preview, and Catalog.
    Used by build_s1_monthly_cog_and_zarr_crossUTM.

    Args:
        mgrs_wkt: Pre-computed WKT geometry string for mgrs_tile_id.
            Callers must resolve this once before the flush loop using
            _get_mgrs_tile_geometry_wkt() to avoid repeated parquet reads.

    Mutates: month_buf (cleared), catalog_records (appended), processed_log (appended).
    """
    if not month_buf['vv']:
        return
    if m_str in existing_months:
        if on_time_conflict == "skip":
            logging.debug("Skipping already-written month: %s", m_str)
            month_buf['vv'].clear()
            month_buf['vh'].clear()
            return
        # overwrite: remove existing timestep before re-appending to avoid duplicates
        _zarr_delete_timestep(g, m_str)

    logging.debug("Flushing month %s (N=%d)", m_str, len(month_buf['vv']))

    # A) Aggregation
    # Suppress all-NaN slice warnings: expected when a pixel has no valid
    # observations in a given month (e.g. edge tiles, radar shadow).
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning, message="All-NaN slice encountered")
        vv_lin = np.nanmin(np.stack(month_buf['vv']), axis=0).astype("float32")
        vh_lin = np.nanmin(np.stack(month_buf['vh']), axis=0).astype("float32")

    if vv_lin.shape != (height, width):
        logging.warning("[WARNING] vv_lin shape %s != grid %dx%d", vv_lin.shape, height, width)
    if vh_lin.shape != (height, width):
        logging.warning("[WARNING] vh_lin shape %s != grid %dx%d", vh_lin.shape, height, width)

    # B) Despeckle
    if monthly_despeckle:
        _method_key = 'tv_kwargs' if despeckle_method == 'tv_bregman' else 'nlm_kwargs'
        vv_lin = despeckle_2d(vv_lin, method=despeckle_method, min_valid_lin=min_valid_lin, interp_method='nearest', **{_method_key: despeckle_kwargs})
        vh_lin = despeckle_2d(vh_lin, method=despeckle_method, min_valid_lin=min_valid_lin, interp_method='nearest', **{_method_key: despeckle_kwargs})

    vv_lin_final = vv_lin
    vh_lin_final = vh_lin

    # C) Valid mask
    is_valid = (
        np.isfinite(vv_lin_final) & (vv_lin_final > min_valid_lin) &
        np.isfinite(vh_lin_final) & (vh_lin_final > min_valid_lin)
    )

    # D) SAR indices (optional, gated by features_ratio / features_rvi)
    eps = float(eps_lin)
    ratio = rvi = None
    if features_ratio:
        ratio = np.full_like(vh_lin_final, np.nan)
        mask_r = vh_lin_final > eps
        ratio[mask_r] = vh_lin_final[mask_r] / vv_lin_final[mask_r]
    if features_rvi:
        rvi = np.full_like(vv_lin_final, np.nan)
        denom = vv_lin_final + vh_lin_final
        mask_rv = denom > eps
        rvi[mask_rv] = (4.0 * vh_lin_final[mask_rv]) / denom[mask_rv]

    # E) dB conversion
    vv_db = np.full_like(vv_lin_final, np.nan, dtype=np.float32)
    vh_db = np.full_like(vh_lin_final, np.nan, dtype=np.float32)
    tmp = np.empty_like(vv_lin_final, dtype=np.float32)
    np.log10(vv_lin_final, out=tmp, where=is_valid)
    vv_db[is_valid] = DB_SCALE_FACTOR * tmp[is_valid]
    np.log10(vh_lin_final, out=tmp, where=is_valid)
    vh_db[is_valid] = DB_SCALE_FACTOR * tmp[is_valid]

    # F) Apply valid and ROI masks
    final_mask = ~is_valid
    if roi_mask is not None:
        final_mask |= (~roi_mask)
    _mask_arrs = [vv_db, vh_db]
    if ratio is not None:
        _mask_arrs.append(ratio)
    if rvi is not None:
        _mask_arrs.append(rvi)
    for arr in _mask_arrs:
        arr[final_mask] = np.nan

    # Apply MGRS tile polygon mask (mgrs_wkt is pre-computed by the caller)
    crs_ll = pyproj.CRS.from_epsg(4326)
    crs_t = pyproj.CRS.from_user_input(target_crs)
    proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
    geom_proj = shp_transform(proj, shapely_wkt.loads(mgrs_wkt))
    mask = rasterize(
        [(mapping(geom_proj), 1)],
        out_shape=(height, width),
        transform=master_transform,
        fill=0, dtype="uint8", all_touched=False,
    ).astype(bool)
    inv_mask = ~mask

    coverage_frac = float(np.count_nonzero(is_valid & mask) / max(1, int(mask.sum())))
    track_source_summary = {}

    # G) GLCM texture bands — computed BEFORE MGRS tile mask to avoid edge artifacts
    tex_arrays, tex_names = [], []
    if features_glcm or (texture_cfg and texture_cfg.get("enabled")):
        logging.debug("[GLCM] Computing texture bands for %s %s...", mgrs_tile_id, m_str)
        tex_arrays, tex_names = compute_glcm_texture_bands(vv_db, vh_db, texture_cfg or {})

    # Apply MGRS tile polygon mask to all bands (after GLCM computation)
    _clip_arrs = [vv_db, vh_db]
    if ratio is not None:
        _clip_arrs.append(ratio)
    if rvi is not None:
        _clip_arrs.append(rvi)
    for arr in _clip_arrs:
        arr[inv_mask] = np.nan
    for arr in tex_arrays:
        arr[inv_mask] = np.nan

    # G.5) Spatial crop for COG/preview output (Zarr keeps full grid).
    # When tile_clip=True, crop arrays to MGRS tile bounding box so that
    # COG and preview cover only the tile extent, not the full burst mosaic.
    _cog_transform = master_transform
    _cog_width, _cog_height = width, height
    _vv_cog, _vh_cog = vv_db, vh_db
    _ratio_cog, _rvi_cog = ratio, rvi
    _tex_cog = list(tex_arrays)

    if tile_clip:
        _all_cog = [vv_db, vh_db]
        if ratio is not None:
            _all_cog.append(ratio)
        if rvi is not None:
            _all_cog.append(rvi)
        _all_cog.extend(tex_arrays)
        try:
            _clipped, _cog_transform = _clip_arrays_to_wkt_4326(
                _all_cog, mgrs_wkt, target_crs, master_transform, height, width
            )
            _vv_cog, _vh_cog = _clipped[0], _clipped[1]
            _cidx = 2
            if ratio is not None:
                _ratio_cog = _clipped[_cidx]; _cidx += 1
            if rvi is not None:
                _rvi_cog = _clipped[_cidx]; _cidx += 1
            _tex_cog = _clipped[_cidx:]
            _cog_height, _cog_width = _vv_cog.shape
        except Exception as _clip_e:
            logging.warning(
                "tile_clip spatial crop failed for %s %s: %s",
                mgrs_tile_id, m_str, _clip_e
            )

    # H) Write COG
    # Layout-aware COG filename: STAC mode includes direction_label and feature_suffix in filename
    if layout_mode == 'legacy':
        cog_filename = f"s1grits_monthly_{mgrs_tile_id}{flight_suffix}_{m_str}.tif"
    else:
        # STAC mode: s1grits_{TILE}_monthly_{DIR}{_FEATURES}_{YYYY-MM}.tif
        direction_part = f"_{direction_label}" if direction_label else ""
        cog_filename = f"s1grits_{mgrs_tile_id}_monthly{direction_part}{feature_suffix}_{m_str}.tif"
    cog_p = os.path.join(out_cog_dir, cog_filename) if generate_cog else None
    if generate_cog and (overwrite_cog or not os.path.exists(cog_p)):
        NODATA_OUT = NODATA_SENTINEL
        master_profile = {
            "driver": "GTiff", "width": _cog_width, "height": _cog_height,
            "count": (2 + (1 if _ratio_cog is not None else 0) + (1 if _rvi_cog is not None else 0)) + len(_tex_cog), "dtype": "float32",
            "nodata": NODATA_OUT, "tiled": True,
            "blockxsize": int(cog_block), "blockysize": int(cog_block),
            "compress": "lzw", "predictor": 3, "interleave": "pixel",
            "transform": _cog_transform, "crs": target_crs,
        }
        with atomic_path(cog_p) as _cog_tmp, rasterio.open(_cog_tmp, "w", **master_profile) as dst:
            _bi = 1
            dst.write(_prep_array(_vv_cog, NODATA_OUT), _bi); dst.set_band_description(_bi, copol_name); _bi += 1
            dst.write(_prep_array(_vh_cog, NODATA_OUT), _bi); dst.set_band_description(_bi, crosspol_name); _bi += 1
            if _ratio_cog is not None:
                dst.write(_prep_array(_ratio_cog, NODATA_OUT), _bi); dst.set_band_description(_bi, ratio_name); _bi += 1
            if _rvi_cog is not None:
                dst.write(_prep_array(_rvi_cog, NODATA_OUT), _bi); dst.set_band_description(_bi, rvi_name); _bi += 1
            for i, (arr, name) in enumerate(zip(_tex_cog, tex_names), start=_bi):
                dst.write(_prep_array(arr, NODATA_OUT), i); dst.set_band_description(i, name)
            dst.update_tags(time=m_str)

    # I) Write Zarr (always full grid — do not use cropped arrays here)
    _t_int = np.datetime64(f"{m_str}-01", "ns").astype("int64")
    _zarr_pairs = [(copol_name, vv_db), (crosspol_name, vh_db)]
    if ratio is not None:
        _zarr_pairs.append((ratio_name, ratio))
    if rvi is not None:
        _zarr_pairs.append((rvi_name, rvi))
    for nm, arr in _zarr_pairs:
        _zarr_append(g, nm, arr)
    for nm, arr in zip(tex_names, tex_arrays):
        _zarr_append(g, nm, arr)
    # Append the time coordinate last so an interrupted band write cannot
    # orphan a time step (time length > data length).
    _zarr_append(g, "time", np.array([_t_int], dtype="int64"))

    # J) Generate Preview PNG (uses spatially cropped arrays when tile_clip=True)
    preview_metadata = None
    if generate_preview:
        try:
            if layout_mode == 'legacy':
                preview_path = os.path.join(preview_dir, f"s1grits_monthly_{mgrs_tile_id}{flight_suffix}_{m_str}.png")
            else:
                preview_path = os.path.join(preview_dir, f"s1grits_{mgrs_tile_id}_monthly_{direction_label}{feature_suffix}_{m_str}.png" if direction_label else f"s1grits_{mgrs_tile_id}_monthly{feature_suffix}_{m_str}.png")
            preview_metadata = _generate_preview_png(
                vv_db=_vv_cog, vh_db=_vh_cog, ratio=_ratio_cog,
                src_transform=_cog_transform, src_crs=target_crs,
                output_path=preview_path, preview_res=preview_res,
                preview_crs="EPSG:4326",
            )
            logging.debug("Preview written: %s", preview_path)
        except Exception as e:
            logging.warning("Preview generation failed for %s: %s", m_str, e)
            logging.debug("Preview traceback: %s", traceback.format_exc())

    # K) Update catalog
    zarr_rel_path = os.path.relpath(zarr_path, output_root)
    cog_rel_path = os.path.relpath(cog_p, output_root) if generate_cog and cog_p else None
    preview_rel_path = os.path.relpath(preview_metadata["path"], output_root) if preview_metadata else None
    catalog_records.append(normalize_catalog_record({
        "tile_id": mgrs_tile_id,
        "product_label": product_label,
        "flight_direction": flight_suffix.lstrip("_") if flight_suffix else None,
        "month": m_str,
        "datetime": np.datetime64(f"{m_str}-01", "ns"),
        "cog_path": cog_rel_path,
        "zarr_path": zarr_rel_path,
        "preview_path": preview_rel_path,
        "item_path": os.path.join("items", product_label, f"{mgrs_tile_id}_{direction_label}_{m_str}.json") if direction_label else os.path.join("items", product_label, f"{mgrs_tile_id}_{m_str}.json"),
        "crs": str(target_crs),
        "width": width,
        "height": height,
        "transform": list(master_transform),
        "processing_level": processing_level,
        "output_type": "monthly",
        "collection_id": "s1grits-monthly",
        "product_type": "monthly",
        "preview_crs": preview_metadata["crs"] if preview_metadata else None,
        "preview_width": preview_metadata["width"] if preview_metadata else None,
        "preview_height": preview_metadata["height"] if preview_metadata else None,
        "preview_bounds": preview_metadata["bounds"] if preview_metadata else None,
        "coverage_fraction": round(coverage_frac, 4),
        "track_source_summary": str(track_source_summary),
    }))

    try:
        write_stac_item(catalog_records[-1], output_root, polarization)
    except Exception as _stac_e:
        logging.warning("STAC item write failed for %s: %s", m_str, _stac_e)

    processed_log.append(m_str)
    month_buf['vv'].clear()
    month_buf['vh'].clear()
    gc.collect()


def _flush_month_tileUTM(
    m_str: str,
    month_buf: dict,
    master_transform,
    height: int,
    width: int,
    catalog_records: list,
    existing_months: set,
    on_time_conflict: str,
    monthly_despeckle: bool,
    despeckle_method: str,
    despeckle_kwargs: dict,
    min_valid_lin: float,
    eps_lin: float,
    roi_mask,
    mgrs_tile_id: str,
    target_crs: str,
    mgrs_wkt: str,
    texture_cfg,
    generate_cog: bool,
    overwrite_cog: bool,
    out_cog_dir: str,
    flight_suffix: str,
    cog_block: int,
    copol_name: str,
    crosspol_name: str,
    ratio_name: str,
    rvi_name: str,
    generate_preview: bool,
    preview_dir: str,
    preview_res: float,
    zarr_path: str,
    output_root: str,
    processing_level: str,
    g,
    processed_log: list,
    polarization: str,
    features_ratio: bool = True,
    features_rvi: bool = True,
    features_glcm: bool = False,
    tile_clip: bool = True,
    direction_label: str = "",
    product_label: str = "monthly",
    feature_suffix: str = "",
    layout_mode: str = "stac",
):
    """
    Flush one month's buffered linear arrays to COG, Zarr, Preview, and Catalog.
    Used by build_s1_monthly_cog_and_zarr_tileUTM.

    Args:
        mgrs_wkt: Pre-computed WKT geometry string for mgrs_tile_id.
            Callers must resolve this once before the flush loop using
            _get_mgrs_tile_geometry_wkt() to avoid repeated parquet reads.

    Mutates: month_buf (cleared), catalog_records (appended), processed_log (appended).
    """
    if not month_buf['vv']:
        return
    if m_str in existing_months:
        if on_time_conflict == "skip":
            logging.debug("Skipping already-written month: %s", m_str)
            month_buf['vv'].clear()
            month_buf['vh'].clear()
            return
        # overwrite: remove existing timestep before re-appending to avoid duplicates
        _zarr_delete_timestep(g, m_str)

    logging.debug("Flushing month %s (N=%d)", m_str, len(month_buf['vv']))

    # A) Aggregation
    # Suppress all-NaN slice warnings: expected when a pixel has no valid
    # observations in a given month (e.g. edge tiles, radar shadow).
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning, message="All-NaN slice encountered")
        vv_lin = np.nanmin(np.stack(month_buf['vv']), axis=0).astype("float32")
        vh_lin = np.nanmin(np.stack(month_buf['vh']), axis=0).astype("float32")

    if vv_lin.shape != (height, width):
        logging.warning("[WARNING] vv_lin shape %s != grid %dx%d", vv_lin.shape, height, width)
    if vh_lin.shape != (height, width):
        logging.warning("[WARNING] vh_lin shape %s != grid %dx%d", vh_lin.shape, height, width)

    # B) Despeckle
    if monthly_despeckle:
        _method_key = 'tv_kwargs' if despeckle_method == 'tv_bregman' else 'nlm_kwargs'
        vv_lin = despeckle_2d(vv_lin, method=despeckle_method, min_valid_lin=min_valid_lin, interp_method='nearest', **{_method_key: despeckle_kwargs})
        vh_lin = despeckle_2d(vh_lin, method=despeckle_method, min_valid_lin=min_valid_lin, interp_method='nearest', **{_method_key: despeckle_kwargs})

    vv_lin_final = vv_lin
    vh_lin_final = vh_lin

    # C) Valid mask
    is_valid = (
        np.isfinite(vv_lin_final) & (vv_lin_final > min_valid_lin) &
        np.isfinite(vh_lin_final) & (vh_lin_final > min_valid_lin)
    )

    # D) SAR indices (optional, gated by features_ratio / features_rvi)
    eps = float(eps_lin)
    ratio = rvi = None
    if features_ratio:
        ratio = np.full_like(vh_lin_final, np.nan)
        mask_r = vh_lin_final > eps
        ratio[mask_r] = vh_lin_final[mask_r] / vv_lin_final[mask_r]
    if features_rvi:
        rvi = np.full_like(vv_lin_final, np.nan)
        denom = vv_lin_final + vh_lin_final
        mask_rv = denom > eps
        rvi[mask_rv] = (4.0 * vh_lin_final[mask_rv]) / denom[mask_rv]

    # E) dB conversion
    vv_db = np.full_like(vv_lin_final, np.nan, dtype=np.float32)
    vh_db = np.full_like(vh_lin_final, np.nan, dtype=np.float32)
    tmp = np.empty_like(vv_lin_final, dtype=np.float32)
    np.log10(vv_lin_final, out=tmp, where=is_valid)
    vv_db[is_valid] = DB_SCALE_FACTOR * tmp[is_valid]
    np.log10(vh_lin_final, out=tmp, where=is_valid)
    vh_db[is_valid] = DB_SCALE_FACTOR * tmp[is_valid]

    # F) Apply valid and ROI masks
    final_mask = ~is_valid
    if roi_mask is not None:
        final_mask |= (~roi_mask)
    _mask_arrs = [vv_db, vh_db]
    if ratio is not None:
        _mask_arrs.append(ratio)
    if rvi is not None:
        _mask_arrs.append(rvi)
    for arr in _mask_arrs:
        arr[final_mask] = np.nan

    # Apply MGRS tile polygon mask (mgrs_wkt is pre-computed by the caller)
    crs_ll = pyproj.CRS.from_epsg(4326)
    crs_t = pyproj.CRS.from_user_input(target_crs)
    proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
    geom_proj = shp_transform(proj, shapely_wkt.loads(mgrs_wkt))
    mask = rasterize(
        [(mapping(geom_proj), 1)],
        out_shape=(height, width),
        transform=master_transform,
        fill=0, dtype="uint8", all_touched=False,
    ).astype(bool)
    inv_mask = ~mask

    # G) GLCM texture bands — computed BEFORE MGRS tile mask to avoid edge artifacts
    tex_arrays, tex_names = [], []
    if features_glcm or (texture_cfg and texture_cfg.get("enabled")):
        logging.debug("[GLCM] Computing texture bands for %s %s...", mgrs_tile_id, m_str)
        tex_arrays, tex_names = compute_glcm_texture_bands(vv_db, vh_db, texture_cfg or {})

    # Apply MGRS tile polygon mask to all bands (after GLCM computation)
    _clip_arrs = [vv_db, vh_db]
    if ratio is not None:
        _clip_arrs.append(ratio)
    if rvi is not None:
        _clip_arrs.append(rvi)
    for arr in _clip_arrs:
        arr[inv_mask] = np.nan
    for arr in tex_arrays:
        arr[inv_mask] = np.nan

    # G.5) Spatial crop for COG/preview output (Zarr keeps full grid).
    # When tile_clip=True, crop arrays to MGRS tile bounding box so that
    # COG and preview cover only the tile extent, not the full burst mosaic.
    _cog_transform = master_transform
    _cog_width, _cog_height = width, height
    _vv_cog, _vh_cog = vv_db, vh_db
    _ratio_cog, _rvi_cog = ratio, rvi
    _tex_cog = list(tex_arrays)

    if tile_clip:
        _all_cog = [vv_db, vh_db]
        if ratio is not None:
            _all_cog.append(ratio)
        if rvi is not None:
            _all_cog.append(rvi)
        _all_cog.extend(tex_arrays)
        try:
            _clipped, _cog_transform = _clip_arrays_to_wkt_4326(
                _all_cog, mgrs_wkt, target_crs, master_transform, height, width
            )
            _vv_cog, _vh_cog = _clipped[0], _clipped[1]
            _cidx = 2
            if ratio is not None:
                _ratio_cog = _clipped[_cidx]; _cidx += 1
            if rvi is not None:
                _rvi_cog = _clipped[_cidx]; _cidx += 1
            _tex_cog = _clipped[_cidx:]
            _cog_height, _cog_width = _vv_cog.shape
        except Exception as _clip_e:
            logging.warning(
                "tile_clip spatial crop failed for %s %s: %s",
                mgrs_tile_id, m_str, _clip_e
            )

    # H) Write COG
    # Layout-aware COG filename: STAC mode includes direction_label and feature_suffix in filename
    if layout_mode == 'legacy':
        cog_filename = f"s1grits_monthly_{mgrs_tile_id}{flight_suffix}_{m_str}.tif"
    else:
        # STAC mode: s1grits_{TILE}_monthly_{DIR}{_FEATURES}_{YYYY-MM}.tif
        direction_part = f"_{direction_label}" if direction_label else ""
        cog_filename = f"s1grits_{mgrs_tile_id}_monthly{direction_part}{feature_suffix}_{m_str}.tif"
    cog_p = os.path.join(out_cog_dir, cog_filename) if generate_cog else None
    if generate_cog and (overwrite_cog or not os.path.exists(cog_p)):
        NODATA_OUT = NODATA_SENTINEL
        master_profile = {
            "driver": "GTiff", "width": _cog_width, "height": _cog_height,
            "count": (2 + (1 if _ratio_cog is not None else 0) + (1 if _rvi_cog is not None else 0)) + len(_tex_cog), "dtype": "float32",
            "nodata": NODATA_OUT, "tiled": True,
            "blockxsize": int(cog_block), "blockysize": int(cog_block),
            "compress": "lzw", "predictor": 3, "interleave": "pixel",
            "transform": _cog_transform, "crs": target_crs,
        }
        with atomic_path(cog_p) as _cog_tmp, rasterio.open(_cog_tmp, "w", **master_profile) as dst:
            _bi = 1
            dst.write(_prep_array(_vv_cog, NODATA_OUT), _bi); dst.set_band_description(_bi, copol_name); _bi += 1
            dst.write(_prep_array(_vh_cog, NODATA_OUT), _bi); dst.set_band_description(_bi, crosspol_name); _bi += 1
            if _ratio_cog is not None:
                dst.write(_prep_array(_ratio_cog, NODATA_OUT), _bi); dst.set_band_description(_bi, ratio_name); _bi += 1
            if _rvi_cog is not None:
                dst.write(_prep_array(_rvi_cog, NODATA_OUT), _bi); dst.set_band_description(_bi, rvi_name); _bi += 1
            for i, (arr, name) in enumerate(zip(_tex_cog, tex_names), start=_bi):
                dst.write(_prep_array(arr, NODATA_OUT), i); dst.set_band_description(i, name)
            dst.update_tags(time=m_str)

    # I) Write Zarr (always full grid — do not use cropped arrays here)
    _t_int = np.datetime64(f"{m_str}-01", "ns").astype("int64")
    _zarr_pairs = [(copol_name, vv_db), (crosspol_name, vh_db)]
    if ratio is not None:
        _zarr_pairs.append((ratio_name, ratio))
    if rvi is not None:
        _zarr_pairs.append((rvi_name, rvi))
    for nm, arr in _zarr_pairs:
        _zarr_append(g, nm, arr)
    for nm, arr in zip(tex_names, tex_arrays):
        _zarr_append(g, nm, arr)
    # Append the time coordinate last so an interrupted band write cannot
    # orphan a time step (time length > data length).
    _zarr_append(g, "time", np.array([_t_int], dtype="int64"))

    # J) Generate Preview PNG (uses spatially cropped arrays when tile_clip=True)
    preview_metadata = None
    if generate_preview:
        try:
            if layout_mode == 'legacy':
                preview_path = os.path.join(preview_dir, f"s1grits_monthly_{mgrs_tile_id}{flight_suffix}_{m_str}.png")
            else:
                preview_path = os.path.join(preview_dir, f"s1grits_{mgrs_tile_id}_monthly_{direction_label}{feature_suffix}_{m_str}.png" if direction_label else f"s1grits_{mgrs_tile_id}_monthly{feature_suffix}_{m_str}.png")
            preview_metadata = _generate_preview_png(
                vv_db=_vv_cog, vh_db=_vh_cog, ratio=_ratio_cog,
                src_transform=_cog_transform, src_crs=target_crs,
                output_path=preview_path, preview_res=preview_res,
                preview_crs="EPSG:4326",
            )
            logging.debug("Preview written: %s", preview_path)
        except Exception as e:
            logging.warning("Preview generation failed for %s: %s", m_str, e)
            logging.debug("Preview traceback: %s", traceback.format_exc())

    # K) Update catalog
    zarr_rel_path = os.path.relpath(zarr_path, output_root)
    cog_rel_path = os.path.relpath(cog_p, output_root) if generate_cog and cog_p else None
    preview_rel_path = os.path.relpath(preview_metadata["path"], output_root) if preview_metadata else None
    catalog_records.append(normalize_catalog_record({
        "tile_id": mgrs_tile_id,
        "product_label": product_label,
        "flight_direction": flight_suffix.lstrip("_") if flight_suffix else None,
        "month": m_str,
        "datetime": np.datetime64(f"{m_str}-01", "ns"),
        "cog_path": cog_rel_path,
        "zarr_path": zarr_rel_path,
        "preview_path": preview_rel_path,
        "item_path": os.path.join("items", product_label, f"{mgrs_tile_id}_{direction_label}_{m_str}.json") if direction_label else os.path.join("items", product_label, f"{mgrs_tile_id}_{m_str}.json"),
        "crs": str(target_crs),
        "width": width,
        "height": height,
        "transform": list(master_transform),
        "processing_level": processing_level,
        "output_type": "monthly",
        "collection_id": "s1grits-monthly",
        "product_type": "monthly",
        "preview_crs": preview_metadata["crs"] if preview_metadata else None,
        "preview_width": preview_metadata["width"] if preview_metadata else None,
        "preview_height": preview_metadata["height"] if preview_metadata else None,
        "preview_bounds": preview_metadata["bounds"] if preview_metadata else None,
    }))

    try:
        write_stac_item(catalog_records[-1], output_root, polarization)
    except Exception as _stac_e:
        logging.warning("STAC item write failed for %s: %s", m_str, _stac_e)

    processed_log.append(m_str)
    month_buf['vv'].clear()
    month_buf['vh'].clear()
    gc.collect()


def _flush_month_median_cube(
    m_str: str,
    month_buf: dict,
    master_transform,
    height: int,
    width: int,
    catalog_records: list,
    existing_months: set,
    on_time_conflict: str,
    min_valid_lin: float,
    eps_lin: float,
    roi_mask,
    mgrs_mask,
    mgrs_tile_id: str,
    target_crs: str,
    texture_cfg,
    generate_cog: bool,
    overwrite_cog: bool,
    out_cog_dir: str,
    flight_suffix: str,
    cog_block: int,
    copol_name: str,
    crosspol_name: str,
    ratio_name: str,
    rvi_name: str,
    generate_preview: bool,
    preview_dir: str,
    preview_res: float,
    zarr_path: str,
    output_root: str,
    processing_level: str,
    g,
    processed_log: list,
    polarization: str,
    features_ratio: bool = True,
    features_rvi: bool = True,
    features_glcm: bool = False,
    layout_mode: str = 'modern',
    direction_label: str = None,
    feature_suffix: str = '',
    product_label: str = 'monthly',
):
    """
    Flush one month for the ARDC median cube builder (no despeckle).
    Used by build_s1_monthly_median_cube.

    Mutates: month_buf (cleared), catalog_records (appended), processed_log (appended).
    """
    if not month_buf['vv']:
        return
    if m_str in existing_months:
        if on_time_conflict == "skip":
            logging.debug("Skipping already-written month: %s", m_str)
            month_buf['vv'].clear()
            month_buf['vh'].clear()
            return
        # overwrite: remove existing timestep before re-appending to avoid duplicates
        _zarr_delete_timestep(g, m_str)

    logging.debug("Median composite %s (N=%d passes)", m_str, len(month_buf['vv']))

    # ARDC: pixel-wise temporal median, no despeckle
    # Suppress all-NaN slice warnings: expected when a pixel has no valid
    # observations in a given month (e.g. edge tiles, radar shadow).
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning, message="All-NaN slice encountered")
        vv_lin_final = np.nanmin(np.stack(month_buf['vv']), axis=0).astype("float32")
        vh_lin_final = np.nanmin(np.stack(month_buf['vh']), axis=0).astype("float32")

    # Valid mask
    is_valid = (
        np.isfinite(vv_lin_final) & (vv_lin_final > min_valid_lin) &
        np.isfinite(vh_lin_final) & (vh_lin_final > min_valid_lin)
    )

    # SAR indices
    eps = float(eps_lin)
    ratio = np.full_like(vh_lin_final, np.nan)
    mask_r = vh_lin_final > eps
    ratio[mask_r] = vh_lin_final[mask_r] / vv_lin_final[mask_r]

    rvi = np.full_like(vv_lin_final, np.nan)
    denom = vv_lin_final + vh_lin_final
    mask_rv = denom > eps
    rvi[mask_rv] = (4.0 * vh_lin_final[mask_rv]) / denom[mask_rv]

    # Linear -> dB
    vv_db = np.full_like(vv_lin_final, np.nan, dtype=np.float32)
    vh_db = np.full_like(vh_lin_final, np.nan, dtype=np.float32)
    tmp = np.empty_like(vv_lin_final, dtype=np.float32)
    np.log10(vv_lin_final, out=tmp, where=is_valid)
    vv_db[is_valid] = DB_SCALE_FACTOR * tmp[is_valid]
    np.log10(vh_lin_final, out=tmp, where=is_valid)
    vh_db[is_valid] = DB_SCALE_FACTOR * tmp[is_valid]

    # Apply valid mask and MGRS polygon mask
    inv_valid = ~is_valid
    inv_tile = ~mgrs_mask
    if roi_mask is not None:
        inv_tile = inv_tile | (~roi_mask)
    for arr in [vv_db, vh_db, ratio, rvi]:
        arr[inv_valid] = np.nan
        arr[inv_tile] = np.nan

    # GLCM texture bands
    tex_arrays, tex_names = [], []
    if features_glcm or (texture_cfg and texture_cfg.get("enabled")):
        logging.debug("[GLCM] Computing texture bands for %s %s...", mgrs_tile_id, m_str)
        tex_arrays, tex_names = compute_glcm_texture_bands(vv_db, vh_db, texture_cfg or {})
        for arr in tex_arrays:
            arr[inv_valid] = np.nan
            arr[inv_tile] = np.nan

    # Write COG
    cog_p = os.path.join(out_cog_dir, f"s1grits_monthly_{mgrs_tile_id}{flight_suffix}_{m_str}.tif") if generate_cog else None
    if generate_cog and (overwrite_cog or not os.path.exists(cog_p)):
        NODATA_OUT = NODATA_SENTINEL
        master_profile = {
            "driver": "GTiff", "width": width, "height": height,
            "count": (2 + (1 if ratio is not None else 0) + (1 if rvi is not None else 0)) + len(tex_arrays), "dtype": "float32",
            "nodata": NODATA_OUT, "tiled": True,
            "blockxsize": int(cog_block), "blockysize": int(cog_block),
            "compress": "lzw", "predictor": 3, "interleave": "pixel",
            "transform": master_transform, "crs": target_crs,
        }
        with atomic_path(cog_p) as _cog_tmp, rasterio.open(_cog_tmp, "w", **master_profile) as dst:
            _bi = 1
            dst.write(_prep_array(vv_db, NODATA_OUT), _bi); dst.set_band_description(_bi, copol_name); _bi += 1
            dst.write(_prep_array(vh_db, NODATA_OUT), _bi); dst.set_band_description(_bi, crosspol_name); _bi += 1
            if ratio is not None:
                dst.write(_prep_array(ratio, NODATA_OUT), _bi); dst.set_band_description(_bi, ratio_name); _bi += 1
            if rvi is not None:
                dst.write(_prep_array(rvi, NODATA_OUT), _bi); dst.set_band_description(_bi, rvi_name); _bi += 1
            for i, (arr, name) in enumerate(zip(tex_arrays, tex_names), start=_bi):
                dst.write(_prep_array(arr, NODATA_OUT), i); dst.set_band_description(i, name)
            dst.update_tags(time=m_str, processing_level=processing_level)

    # Write Zarr
    _t_int = np.datetime64(f"{m_str}-01", "ns").astype("int64")
    _zarr_pairs = [(copol_name, vv_db), (crosspol_name, vh_db)]
    if ratio is not None:
        _zarr_pairs.append((ratio_name, ratio))
    if rvi is not None:
        _zarr_pairs.append((rvi_name, rvi))
    for nm, arr in _zarr_pairs:
        _zarr_append(g, nm, arr)
    for nm, arr in zip(tex_names, tex_arrays):
        _zarr_append(g, nm, arr)
    # Append the time coordinate last so an interrupted band write cannot
    # orphan a time step (time length > data length).
    _zarr_append(g, "time", np.array([_t_int], dtype="int64"))

    # Generate preview PNG
    preview_metadata = None
    if generate_preview:
        try:
            if layout_mode == 'legacy':
                preview_path = os.path.join(preview_dir, f"s1grits_monthly_{mgrs_tile_id}{flight_suffix}_{m_str}.png")
            else:
                preview_path = os.path.join(preview_dir, f"s1grits_{mgrs_tile_id}_monthly_{direction_label}{feature_suffix}_{m_str}.png" if direction_label else f"s1grits_{mgrs_tile_id}_monthly{feature_suffix}_{m_str}.png")
            preview_metadata = _generate_preview_png(
                vv_db=vv_db, vh_db=vh_db, ratio=ratio,
                src_transform=master_transform, src_crs=target_crs,
                output_path=preview_path, preview_res=preview_res,
                preview_crs="EPSG:4326",
            )
            logging.debug("Preview written: %s", preview_path)
        except Exception as e:
            logging.warning("Preview generation failed for %s: %s", m_str, e)

    # Update catalog
    zarr_rel_path = os.path.relpath(zarr_path, output_root)
    cog_rel_path = os.path.relpath(cog_p, output_root) if generate_cog and cog_p else None
    preview_rel_path = os.path.relpath(preview_metadata["path"], output_root) if preview_metadata else None
    catalog_records.append(normalize_catalog_record({
        "tile_id": mgrs_tile_id,
        "product_label": product_label,
        "flight_direction": flight_suffix.lstrip("_") if flight_suffix else None,
        "month": m_str,
        "datetime": np.datetime64(f"{m_str}-01", "ns"),
        "cog_path": cog_rel_path,
        "zarr_path": zarr_rel_path,
        "preview_path": preview_rel_path,
        "item_path": os.path.join("items", product_label, f"{mgrs_tile_id}_{direction_label}_{m_str}.json") if direction_label else os.path.join("items", product_label, f"{mgrs_tile_id}_{m_str}.json"),
        "crs": str(target_crs),
        "width": width,
        "height": height,
        "transform": list(master_transform),
        "processing_level": processing_level,
        "output_type": "monthly",
        "collection_id": "s1grits-monthly",
        "product_type": "monthly",
        "preview_crs": preview_metadata["crs"] if preview_metadata else None,
        "preview_width": preview_metadata["width"] if preview_metadata else None,
        "preview_height": preview_metadata["height"] if preview_metadata else None,
        "preview_bounds": preview_metadata["bounds"] if preview_metadata else None,
    }))
    try:
        write_stac_item(catalog_records[-1], output_root, polarization)
    except Exception as _stac_e:
        logging.warning("STAC item write failed for %s: %s", m_str, _stac_e)
    processed_log.append(m_str)
    month_buf['vv'].clear()
    month_buf['vh'].clear()
    gc.collect()


# =========================================================
#  Part 4: Main Builders
# =========================================================

def _rmtree_windows_safe(path: str, max_retries: int = 3, delay: float = 1.0) -> None:
    """
    Windows-safe recursive directory removal.

    Retries on PermissionError (file locks) with short delays.
    Raises RuntimeError with actionable message if all retries fail.

    Args:
        path: Directory path to remove
        max_retries: Maximum number of removal attempts (default: 3)
        delay: Delay in seconds between retries (default: 1.0)

    Raises:
        RuntimeError: If unable to remove directory after all retries
    """
    for attempt in range(max_retries):
        try:
            shutil.rmtree(path)
            return
        except PermissionError as e:
            if attempt < max_retries - 1:
                logging.warning(
                    "[Integrity] File lock detected, retry %d/%d in %.1fs: %s",
                    attempt + 1, max_retries, delay, e
                )
                time.sleep(delay)
            else:
                raise RuntimeError(
                    f"Cannot remove inconsistent tile directory {path} after "
                    f"{max_retries} retries. Close all programs that may have "
                    f"these files open (Windows Explorer, QGIS, ArcGIS, etc.) "
                    f"and retry, or manually delete: {path}"
                ) from e


def _check_tile_integrity(
    tile_dir: str,
    zarr_path: str,
    product_label: str = "monthly",
    layout_mode: str = "stac",
) -> bool:
    """
    Check tile directory integrity before processing.

    Zarr is the authoritative Data Cube product. If COG/catalog/preview exist
    but the Zarr store is missing, the tile is in an inconsistent state —
    delete everything and start fresh to ensure temporal alignment.

    zarr_path may be either:
    - A path to a single .zarr store (file or directory)
    - A directory containing multiple .zarr stores (acq_group mode)

    Returns True if tile is consistent (safe to proceed).
    Returns False if tile was inconsistent and has been cleaned up.
    Raises RuntimeError if cleanup fails (e.g. Windows file locks).
    """
    # Determine if Zarr exists: for a directory, check if it contains any .zarr entries
    if os.path.isdir(zarr_path):
        zarr_exists = bool([
            e for e in os.listdir(zarr_path)
            if e.endswith('.zarr') or os.path.isdir(os.path.join(zarr_path, e))
        ])
    else:
        zarr_exists = os.path.exists(zarr_path)

    catalog_exists = os.path.isfile(os.path.join(tile_dir, "catalog.parquet"))

    # COG directory location depends on layout mode
    if layout_mode == 'stac':
        cog_dir = os.path.join(tile_dir, product_label, "cog")
    else:
        cog_dir = os.path.join(tile_dir, "cog")

    cog_exists = os.path.isdir(cog_dir) and bool(os.listdir(cog_dir))

    if zarr_exists:
        return True  # Zarr present — normal operation

    if not catalog_exists and not cog_exists:
        return True  # Nothing exists — fresh start, consistent

    # Zarr missing but other artifacts present — inconsistent state.
    # COG files without a Zarr cube are useless for Data Cube operations
    # and represent a corrupted state. Must rebuild from scratch.
    logging.warning(
        "[Integrity] Zarr Data Cube missing but stale COG/catalog artifacts found "
        "in %s. This indicates a previous run that did not generate Zarr, or "
        "an incomplete run. Cleaning only product-specific subdirectories to preserve "
        "other workflows' outputs.", tile_dir
    )

    # Selective cleanup: only remove product-specific directories, not the entire tile_dir
    # This allows multiple workflows (monthly, scenes, static) to coexist in the same tile
    try:
        tile_path = Path(tile_dir)

        # Remove product-specific subdirectory (e.g., monthly_DESCENDING_tvbregman5/)
        product_dir = tile_path / product_label
        if product_dir.exists():
            logging.info("[Integrity] Removing stale product directory: %s", product_dir)
            _rmtree_windows_safe(str(product_dir))

        # Also clean up items subdirectory for this product if it exists
        items_product_dir = tile_path / "items" / product_label
        if items_product_dir.exists():
            logging.info("[Integrity] Removing stale items directory: %s", items_product_dir)
            _rmtree_windows_safe(str(items_product_dir))

        # For scenes workflow, also clean up smonthly variants
        if "scenes" in product_label or "monthly" in product_label:
            for pattern in ["scenes_*", "smonthly_*"]:
                for subdir in tile_path.glob(pattern):
                    if subdir.is_dir():
                        logging.info("[Integrity] Removing stale directory: %s", subdir)
                        _rmtree_windows_safe(str(subdir))

                # Clean items subdirectories
                items_dir = tile_path / "items"
                if items_dir.exists():
                    for subdir in items_dir.glob(pattern):
                        if subdir.is_dir():
                            logging.info("[Integrity] Removing stale items directory: %s", subdir)
                            _rmtree_windows_safe(str(subdir))

        # NOTE: catalog.parquet is NOT deleted because other workflows may depend on it
        logging.info("[Integrity] Selective cleanup completed. Tile directory preserved.")

    except Exception as e:
        logging.warning("[Integrity] Selective cleanup encountered error: %s. Attempting full removal.", e)
        # Fallback to full removal if selective cleanup fails
        _expected_prefix = os.path.realpath(os.path.dirname(os.path.abspath(tile_dir)))
        _actual_path = os.path.realpath(os.path.abspath(tile_dir))
        if not _actual_path.startswith(_expected_prefix):
            raise ValueError(f"Refusing to remove path outside output_root: {tile_dir}")
        _rmtree_windows_safe(tile_dir)

    return False


def _zarr_delete_timestep(g, m_str: str) -> None:
    """
    Remove a specific month's timestep from an open Zarr group in-place.

    Reads all data, drops the matching time index, and rewrites the store.
    Required because chunked Zarr arrays do not support in-place deletion
    of a single timestep; this workaround re-writes the entire time axis.
    timestep — only append and full-array overwrite are supported.

    Args:
        g: Open zarr group (mode='r+')
        m_str: Month string 'YYYY-MM' to remove
    """
    times = pd.to_datetime(g["time"][:])
    keep_idx = [i for i, t in enumerate(times) if t.strftime("%Y-%m") != m_str]

    if len(keep_idx) == len(times):
        return  # Nothing to remove

    n_before = len(times)
    from s1grits.zarr_cf import band_data_vars
    band_vars = band_data_vars(g)
    kept_times = np.array([g["time"][i] for i in keep_idx], dtype="int64")
    # Read each full band then NumPy-fancy-index the kept timesteps. zarr>=3.2
    # basic indexing rejects a Python list on the time axis
    # ("unsupported selection item for basic indexing"); NumPy fancy indexing
    # on the materialised array is version-agnostic and memory-equivalent to
    # the previous per-slice read.
    kept_bands = {v: np.asarray(g[v][:])[keep_idx] for v in band_vars}

    g["time"].resize((len(keep_idx),))
    if keep_idx:
        g["time"][:] = kept_times

    for v, data in kept_bands.items():
        g[v].resize((len(keep_idx), g[v].shape[1], g[v].shape[2]))
        if keep_idx:
            g[v][:] = data

    logging.debug(
        "Removed timestep %s from Zarr (%d -> %d steps)", m_str, n_before, len(keep_idx)
    )


def build_s1_monthly_cog_and_zarr_crossUTM(
    arrs_vv,
    prof_vv,
    arrs_vh,
    prof_vh,
    acq_datetimes,
    mgrs_tile_id,
    flight_direction=None,
    output_root="./output",
    target_crs="EPSG:3857",
    target_res=30.0,
    roi_wkt=None,
    use_roi_mask=False,
    trim_fraction=0.15,
    group_mode="hour",
    on_time_conflict="skip",
    overwrite_cog=False,
    chunk_y=None,
    chunk_x=None,
    cog_block=256,
    monthly_despeckle=True,
    despeckle_method="tv_bregman",
    despeckle_kwargs=None,
    min_valid_lin=1e-6,
    eps_lin=1e-7,
    generate_cog=True,
    generate_preview=True,
    preview_res=300.0,
    preview_crs=None,
    polarization="VV+VH",
    texture_cfg=None,
    processing_level="hARDCp",
    features_ratio=False,
    features_rvi=False,
    features_glcm=False,
    tile_clip=True,
    **kwargs,
):
    if despeckle_kwargs is None:
        despeckle_kwargs = {}
    copol_name, crosspol_name, ratio_name, rvi_name = get_band_names(polarization)

    if preview_crs is not None:
        logging.warning("[Warning] preview_crs parameter is deprecated and ignored. Preview always uses EPSG:4326.")

    if not (len(arrs_vv) == len(prof_vv) == len(arrs_vh) == len(prof_vh) == len(acq_datetimes)):
        raise ValueError("Input alignment error.")

    # Extract layout mode from kwargs, default to 'stac'
    layout_mode = kwargs.get('layout_mode', 'stac')

    # Build product label components
    direction_label = flight_direction if flight_direction else ""
    flight_suffix = f"_{direction_label}" if direction_label else ""

    _features = []
    if features_ratio:
        _features.append('Ratio')
    if features_rvi:
        _features.append('RVI')
    if features_glcm:
        _features.append('GLCM')
    feature_suffix = '_' + '_'.join(_features) if _features else ''

    product_label = f"monthly_{direction_label}{feature_suffix}"

    if not re.match(r'^[0-9]{2}[A-Z]{3}$', mgrs_tile_id):
        raise ValueError(f"Invalid MGRS tile ID format: '{mgrs_tile_id}' (expected e.g. '17MPV')")

    # Path construction based on layout mode
    if layout_mode == 'legacy':
        tile_dir_name = f"{mgrs_tile_id}{flight_suffix}"
        tile_dir = os.path.join(output_root, tile_dir_name)
        out_cog_dir = os.path.join(tile_dir, "cog")
        zarr_path = os.path.join(tile_dir, "zarr", "s1grits_monthly.zarr")
        preview_dir = os.path.join(tile_dir, "preview")
    else:  # 'stac' mode
        tile_dir = os.path.join(output_root, mgrs_tile_id)
        product_dir = os.path.join(tile_dir, product_label)
        out_cog_dir = os.path.join(product_dir, "cog")

        # Zarr filename includes tile ID and product label
        if direction_label:
            zarr_name = f"s1grits_{mgrs_tile_id}_monthly_{direction_label}{feature_suffix}.zarr"
        else:
            zarr_name = f"s1grits_{mgrs_tile_id}_monthly{feature_suffix}.zarr"
        zarr_path = os.path.join(product_dir, "zarr", zarr_name)
        preview_dir = os.path.join(product_dir, "preview")

    _check_tile_integrity(tile_dir, zarr_path, product_label, layout_mode)

    os.makedirs(tile_dir, exist_ok=True)
    if generate_cog:
        os.makedirs(out_cog_dir, exist_ok=True)
    os.makedirs(os.path.dirname(zarr_path), exist_ok=True)
    if generate_preview:
        os.makedirs(preview_dir, exist_ok=True)

    ts0 = pd.to_datetime(acq_datetimes, utc=True)
    order = np.argsort(ts0.values)
    ts = pd.Series(ts0).iloc[order].reset_index(drop=True)
    arrs_vv = [arrs_vv[i] for i in order]; prof_vv = [prof_vv[i] for i in order]
    arrs_vh = [arrs_vh[i] for i in order]; prof_vh = [prof_vh[i] for i in order]

    calc_new_grid = True
    if os.path.exists(zarr_path):
        try:
            _g_lock = zarr.open_group(zarr_path, mode="r", zarr_format=3)
            if "transform" in _g_lock.attrs:
                master_transform = Affine(*_g_lock.attrs["transform"])
                target_crs = _g_lock.attrs["crs"]
                x_coords = _g_lock["x"][:]; y_coords = _g_lock["y"][:]
                width = x_coords.shape[0]; height = _g_lock["y"][:].shape[0]
                calc_new_grid = False
            logging.info("[Grid] Locked to Zarr: %dx%d", width, height)
        except Exception as _lock_exc:
            logging.debug("Could not read grid from existing Zarr, will recompute: %s", _lock_exc)

    if calc_new_grid:
        master_transform, width, height, x_coords, y_coords = _build_grid_from_mgrs_tile(
            mgrs_tile_id, target_crs, target_res
        )
        logging.info("[Grid] New: %dx%d", width, height)

    zarr_transform = master_transform
    zarr_height = height; zarr_width = width
    zarr_x_coords = x_coords; zarr_y_coords = y_coords

    roi_mask = None
    if use_roi_mask and roi_wkt:
        try:
            crs_ll = pyproj.CRS.from_epsg(4326)
            crs_t = pyproj.CRS.from_user_input(target_crs)
            proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
            roi_geom_proj = shp_transform(proj, shapely_wkt.loads(roi_wkt))
            roi_mask = _rasterize_roi_mask(roi_geom_proj, (zarr_height, zarr_width), zarr_transform)
        except Exception as _roi_exc:
            logging.debug("ROI mask computation skipped: %s", _roi_exc)

    # Pre-compute MGRS WKT once here so _flush_month_crossUTM never re-reads parquet.
    mgrs_wkt_crossUTM = _get_mgrs_tile_geometry_wkt(mgrs_tile_id)

    mode = 'w'
    if os.path.exists(zarr_path) and not calc_new_grid:
        mode = 'r+'
    elif os.path.exists(zarr_path):
        _expected_prefix = os.path.realpath(output_root)
        _actual_path = os.path.realpath(zarr_path)
        if not _actual_path.startswith(_expected_prefix):
            raise ValueError(f"Refusing to remove path outside output_root: {zarr_path}")
        shutil.rmtree(zarr_path)

    g = zarr.open_group(zarr_path, mode=mode, zarr_format=3)
    try:
        if mode == 'w':
            g.attrs["crs"] = str(target_crs); g.attrs["transform"] = tuple(zarr_transform)
            g.attrs["processing_level"] = processing_level
            from s1grits.canonical_catalog_schema import make_grid_id
            _gid = make_grid_id(mgrs_tile_id, str(target_crs), list(zarr_transform), zarr_width, zarr_height)
            g.attrs["grid_id"] = _gid
            g.attrs["grid_name"] = f"{mgrs_tile_id}_native_{int(target_res)}m"
            g.attrs["grid_version"] = 1
            g.attrs["width"] = zarr_width
            g.attrs["height"] = zarr_height
            g.attrs["resolution"] = [float(abs(zarr_transform[0])), float(abs(zarr_transform[4]))]
            g.attrs["product_type"] = "monthly"
            g.attrs["time_varying"] = True
            g.attrs["array_dims"] = ["time", "y", "x"]
            # Write variant metadata to Zarr root attrs for resync self-sufficiency
            if kwargs.get('processing_signature'):
                g.attrs['processing_signature'] = kwargs['processing_signature']
            if kwargs.get('product_variant'):
                g.attrs['product_variant'] = kwargs['product_variant']
            if kwargs.get('processing_variant_json'):
                g.attrs['processing_variant_json'] = kwargs['processing_variant_json']
            _a = g.create_array("x", data=zarr_x_coords, overwrite=True, dimension_names=["x"])
            _a.attrs["_ARRAY_DIMENSIONS"] = ["x"]
            _a = g.create_array("y", data=zarr_y_coords, overwrite=True, dimension_names=["y"])
            _a.attrs["_ARRAY_DIMENSIONS"] = ["y"]
            _a = g.create_array("time", shape=(0,), chunks=(1,), dtype="datetime64[ns]", overwrite=True, dimension_names=["time"])
            _a.attrs["_ARRAY_DIMENSIONS"] = ["time"]
            _zarr_vars = [copol_name, crosspol_name]
            if features_ratio:
                _zarr_vars.append(ratio_name)
            if features_rvi:
                _zarr_vars.append(rvi_name)
            for var in _zarr_vars:
                _a = g.create_array(var, shape=(0, zarr_height, zarr_width), chunks=(1, chunk_y, chunk_x), dtype="float32", fill_value=np.nan, overwrite=True, dimension_names=["time", "y", "x"])
                _a.attrs["_ARRAY_DIMENSIONS"] = ["time", "y", "x"]
            if features_glcm or (texture_cfg and texture_cfg.get("enabled")):
                for var in _get_texture_band_names(texture_cfg or {}):
                    _a = g.create_array(var, shape=(0, zarr_height, zarr_width), chunks=(1, chunk_y, chunk_x), dtype="float32", fill_value=np.nan, overwrite=True, dimension_names=["time", "y", "x"])
                    _a.attrs["_ARRAY_DIMENSIONS"] = ["time", "y", "x"]

            # CF-compliant grid_mapping so generic readers (GDAL/QGIS/rioxarray)
            # detect the CRS, consistent with workflow_scenes' Zarr creator.
            from s1grits.zarr_cf import add_cf_grid_mapping
            add_cf_grid_mapping(g, str(target_crs))

        existing_months = set(pd.to_datetime(np.array(g["time"][:], dtype="int64").astype("datetime64[ns]")).strftime("%Y-%m")) if "time" in g and g["time"].shape[0] > 0 else set()

        month_buf = {'vv': [], 'vh': []}
        curr_month = None
        processed_log = []
        catalog_records = []

        if group_mode == "minute":
            group_keys = ts.dt.floor("min")
        elif group_mode == "hour":
            group_keys = ts.dt.floor("h")
        elif group_mode == "day":
            group_keys = ts.dt.floor("D")
        else:
            raise ValueError(f"Invalid group_mode={group_mode!r}. Expected 'minute', 'hour', or 'day'.")

        n = len(ts)
        df_idx = pd.DataFrame({"idx": np.arange(n), "grp": group_keys}).set_index("grp")
        unique_grps = df_idx.index.unique()

        for grp in tqdm(unique_grps, desc="Batches"):
            indices = df_idx.loc[grp, "idx"]
            if np.isscalar(indices): indices = [indices]
            else: indices = indices.values.tolist()

            m_str = pd.Timestamp(grp).strftime("%Y-%m")
            if curr_month and m_str != curr_month:
                _flush_month_crossUTM(
                    curr_month, month_buf, master_transform, height, width, catalog_records,
                    existing_months, on_time_conflict, monthly_despeckle, despeckle_method,
                    despeckle_kwargs, min_valid_lin, eps_lin, roi_mask, mgrs_tile_id, target_crs,
                    mgrs_wkt_crossUTM,
                    texture_cfg, generate_cog, overwrite_cog, out_cog_dir, flight_suffix, cog_block,
                    copol_name, crosspol_name, ratio_name, rvi_name, generate_preview, preview_dir,
                    preview_res, zarr_path, output_root, processing_level, g, processed_log, polarization,
                    features_ratio=features_ratio, features_rvi=features_rvi,
                    features_glcm=features_glcm, tile_clip=tile_clip,
                    direction_label=direction_label, product_label=product_label,
                    feature_suffix=feature_suffix, layout_mode=layout_mode,
                )
            curr_month = m_str

            vv_a = _mosaic_align(indices, arrs_vv, prof_vv, height, width, master_transform, target_crs)
            vh_a = _mosaic_align(indices, arrs_vh, prof_vh, height, width, master_transform, target_crs)
            if vv_a is not None:
                month_buf['vv'].append(vv_a); month_buf['vh'].append(vh_a)

        if curr_month:
            _flush_month_crossUTM(
                curr_month, month_buf, master_transform, height, width, catalog_records,
                existing_months, on_time_conflict, monthly_despeckle, despeckle_method,
                despeckle_kwargs, min_valid_lin, eps_lin, roi_mask, mgrs_tile_id, target_crs,
                mgrs_wkt_crossUTM,
                texture_cfg, generate_cog, overwrite_cog, out_cog_dir, flight_suffix, cog_block,
                copol_name, crosspol_name, ratio_name, rvi_name, generate_preview, preview_dir,
                preview_res, zarr_path, output_root, processing_level, g, processed_log, polarization,
                flight_suffix=flight_suffix, direction_label=direction_label, product_label=product_label,
                feature_suffix=feature_suffix, layout_mode=layout_mode,
                features_ratio=features_ratio, features_rvi=features_rvi,
                features_glcm=features_glcm, tile_clip=tile_clip,
            )
        try:
            zarr.consolidate_metadata(zarr_path)
        except Exception:
            pass
    except Exception as _zarr_exc:
        logging.error(
            "[ERROR] Zarr write failed mid-batch for %s. "
            "Months written before failure are intact; "
            "the current month (%s) may be partially written. "
            "Check the Zarr store before reuse. Error: %s",
            zarr_path, curr_month if curr_month else "unknown", _zarr_exc,
        )
        raise
    finally:
        pass

    catalog_path = os.path.join(tile_dir, "catalog.parquet")
    if catalog_records:
        total = _upsert_tile_catalog(catalog_path, catalog_records)
        logging.info("[Catalog] Updated: %s (%d new, %d total records)", catalog_path, len(catalog_records), total)

    return {
        "written_months": processed_log,
        "catalog_path": catalog_path,
        "tile_dir": tile_dir,
    }


def build_s1_monthly_cog_and_zarr_tileUTM(
    arrs_vv,
    prof_vv,
    arrs_vh,
    prof_vh,
    acq_datetimes,
    mgrs_tile_id,
    flight_direction=None,
    output_root="./output",
    target_crs=None,
    target_res=30.0,
    roi_wkt=None,
    use_roi_mask=False,
    trim_fraction=0.15,
    group_mode="hour",
    on_time_conflict="skip",
    overwrite_cog=False,
    chunk_y=None,
    chunk_x=None,
    cog_block=256,
    monthly_despeckle=True,
    despeckle_method="tv_bregman",
    despeckle_kwargs=None,
    min_valid_lin=1e-6,
    eps_lin=1e-7,
    generate_cog=True,
    generate_preview=True,
    preview_res=300.0,
    preview_crs=None,
    polarization="VV+VH",
    processing_level="hARDCp",
    texture_cfg=None,
    features_ratio=True,
    features_rvi=True,
    features_glcm=False,
    tile_clip=True,
    **kwargs,
):
    if despeckle_kwargs is None:
        despeckle_kwargs = {}
    copol_name, crosspol_name, ratio_name, rvi_name = get_band_names(polarization)

    if preview_crs is not None:
        logging.warning("[Warning] preview_crs parameter is deprecated and ignored. Preview always uses EPSG:4326.")

    if not (len(arrs_vv) == len(prof_vv) == len(arrs_vh) == len(prof_vh) == len(acq_datetimes)):
        raise ValueError("Input alignment error.")

    # Auto-derive UTM CRS from MGRS tile ID if not provided
    if target_crs is None:
        target_crs = _mgrs_to_utm_epsg(mgrs_tile_id)
        logging.info("[CRS] Auto-derived from MGRS tile %s: %s", mgrs_tile_id, target_crs)
    else:
        logging.info("[CRS] Using provided CRS: %s", target_crs)

    # Extract layout mode from kwargs, default to 'stac'
    layout_mode = kwargs.get('layout_mode', 'stac')

    # Build product label components
    direction_label = flight_direction if flight_direction else ""
    flight_suffix = f"_{direction_label}" if direction_label else ""

    _features = []
    if features_ratio:
        _features.append('Ratio')
    if features_rvi:
        _features.append('RVI')
    if features_glcm:
        _features.append('GLCM')
    feature_suffix = '_' + '_'.join(_features) if _features else ''

    product_label = f"monthly_{direction_label}{feature_suffix}"

    if not re.match(r'^[0-9]{2}[A-Z]{3}$', mgrs_tile_id):
        raise ValueError(f"Invalid MGRS tile ID format: '{mgrs_tile_id}' (expected e.g. '17MPV')")

    # Path construction based on layout mode
    if layout_mode == 'legacy':
        tile_dir_name = f"{mgrs_tile_id}{flight_suffix}"
        tile_dir = os.path.join(output_root, tile_dir_name)
        out_cog_dir = os.path.join(tile_dir, "cog")
        zarr_path = os.path.join(tile_dir, "zarr", "s1grits_monthly.zarr")
        preview_dir = os.path.join(tile_dir, "preview")
    else:  # 'stac' mode
        tile_dir = os.path.join(output_root, mgrs_tile_id)
        product_dir = os.path.join(tile_dir, product_label)
        out_cog_dir = os.path.join(product_dir, "cog")

        # Zarr filename includes tile ID and product label
        if direction_label:
            zarr_name = f"s1grits_{mgrs_tile_id}_monthly_{direction_label}{feature_suffix}.zarr"
        else:
            zarr_name = f"s1grits_{mgrs_tile_id}_monthly{feature_suffix}.zarr"
        zarr_path = os.path.join(product_dir, "zarr", zarr_name)
        preview_dir = os.path.join(product_dir, "preview")

    _check_tile_integrity(tile_dir, zarr_path, product_label, layout_mode)

    os.makedirs(tile_dir, exist_ok=True)
    if generate_cog:
        os.makedirs(out_cog_dir, exist_ok=True)
    os.makedirs(os.path.dirname(zarr_path), exist_ok=True)
    if generate_preview:
        os.makedirs(preview_dir, exist_ok=True)

    ts0 = pd.to_datetime(acq_datetimes, utc=True)
    order = np.argsort(ts0.values)
    ts = pd.Series(ts0).iloc[order].reset_index(drop=True)
    arrs_vv = [arrs_vv[i] for i in order]; prof_vv = [prof_vv[i] for i in order]
    arrs_vh = [arrs_vh[i] for i in order]; prof_vh = [prof_vh[i] for i in order]

    calc_new_grid = True
    if os.path.exists(zarr_path):
        try:
            _g_lock = zarr.open_group(zarr_path, mode="r", zarr_format=3)
            if "transform" in _g_lock.attrs:
                master_transform = Affine(*_g_lock.attrs["transform"])
                target_crs = _g_lock.attrs["crs"]
                x_coords = _g_lock["x"][:]; y_coords = _g_lock["y"][:]
                width = x_coords.shape[0]; height = _g_lock["y"][:].shape[0]
                calc_new_grid = False
            logging.info("[Grid] Locked to Zarr: %dx%d", width, height)
        except Exception as _lock_exc:
            logging.debug("Could not read grid from existing Zarr, will recompute: %s", _lock_exc)

    if calc_new_grid:
        master_transform, width, height, x_coords, y_coords = _build_grid_from_mgrs_tile(
            mgrs_tile_id, target_crs, target_res
        )
        logging.info("[Grid] New (burst union): %dx%d", width, height)

    zarr_transform = master_transform
    zarr_height = height; zarr_width = width
    zarr_x_coords = x_coords; zarr_y_coords = y_coords

    roi_mask = None
    if use_roi_mask and roi_wkt:
        try:
            crs_ll = pyproj.CRS.from_epsg(4326)
            crs_t = pyproj.CRS.from_user_input(target_crs)
            proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
            roi_geom_proj = shp_transform(proj, shapely_wkt.loads(roi_wkt))
            roi_mask = _rasterize_roi_mask(roi_geom_proj, (zarr_height, zarr_width), zarr_transform)
        except Exception as _roi_exc:
            logging.debug("ROI mask computation skipped: %s", _roi_exc)

    # Pre-compute MGRS WKT once here so _flush_month_tileUTM never re-reads parquet.
    mgrs_wkt_tileUTM = _get_mgrs_tile_geometry_wkt(mgrs_tile_id)

    mode = 'w'
    if os.path.exists(zarr_path) and not calc_new_grid:
        mode = 'r+'
    elif os.path.exists(zarr_path):
        _expected_prefix = os.path.realpath(output_root)
        _actual_path = os.path.realpath(zarr_path)
        if not _actual_path.startswith(_expected_prefix):
            raise ValueError(f"Refusing to remove path outside output_root: {zarr_path}")
        shutil.rmtree(zarr_path)

    g = zarr.open_group(zarr_path, mode=mode, zarr_format=3)
    try:
        if mode == 'w':
            g.attrs["crs"] = str(target_crs); g.attrs["transform"] = tuple(zarr_transform)
            g.attrs["processing_level"] = processing_level
            from s1grits.canonical_catalog_schema import make_grid_id
            _gid = make_grid_id(mgrs_tile_id, str(target_crs), list(zarr_transform), zarr_width, zarr_height)
            g.attrs["grid_id"] = _gid
            g.attrs["grid_name"] = f"{mgrs_tile_id}_native_{int(target_res)}m"
            g.attrs["grid_version"] = 1
            g.attrs["width"] = zarr_width
            g.attrs["height"] = zarr_height
            g.attrs["resolution"] = [float(abs(zarr_transform[0])), float(abs(zarr_transform[4]))]
            g.attrs["product_type"] = "monthly"
            g.attrs["time_varying"] = True
            g.attrs["array_dims"] = ["time", "y", "x"]
            # Write variant metadata to Zarr root attrs for resync self-sufficiency
            if kwargs.get('processing_signature'):
                g.attrs['processing_signature'] = kwargs['processing_signature']
            if kwargs.get('product_variant'):
                g.attrs['product_variant'] = kwargs['product_variant']
            if kwargs.get('processing_variant_json'):
                g.attrs['processing_variant_json'] = kwargs['processing_variant_json']
            _a = g.create_array("x", data=zarr_x_coords, overwrite=True, dimension_names=["x"])
            _a.attrs["_ARRAY_DIMENSIONS"] = ["x"]
            _a = g.create_array("y", data=zarr_y_coords, overwrite=True, dimension_names=["y"])
            _a.attrs["_ARRAY_DIMENSIONS"] = ["y"]
            _a = g.create_array("time", shape=(0,), chunks=(1,), dtype="datetime64[ns]", overwrite=True, dimension_names=["time"])
            _a.attrs["_ARRAY_DIMENSIONS"] = ["time"]
            _zarr_vars = [copol_name, crosspol_name]
            if features_ratio:
                _zarr_vars.append(ratio_name)
            if features_rvi:
                _zarr_vars.append(rvi_name)
            for var in _zarr_vars:
                _a = g.create_array(var, shape=(0, zarr_height, zarr_width), chunks=(1, chunk_y, chunk_x), dtype="float32", fill_value=np.nan, overwrite=True, dimension_names=["time", "y", "x"])
                _a.attrs["_ARRAY_DIMENSIONS"] = ["time", "y", "x"]
            if features_glcm or (texture_cfg and texture_cfg.get("enabled")):
                for var in _get_texture_band_names(texture_cfg or {}):
                    _a = g.create_array(var, shape=(0, zarr_height, zarr_width), chunks=(1, chunk_y, chunk_x), dtype="float32", fill_value=np.nan, overwrite=True, dimension_names=["time", "y", "x"])
                    _a.attrs["_ARRAY_DIMENSIONS"] = ["time", "y", "x"]

            # CF-compliant grid_mapping so generic readers (GDAL/QGIS/rioxarray)
            # detect the CRS, consistent with workflow_scenes' Zarr creator.
            from s1grits.zarr_cf import add_cf_grid_mapping
            add_cf_grid_mapping(g, str(target_crs))

        existing_months = set(pd.to_datetime(np.array(g["time"][:], dtype="int64").astype("datetime64[ns]")).strftime("%Y-%m")) if "time" in g and g["time"].shape[0] > 0 else set()

        month_buf = {'vv': [], 'vh': []}
        curr_month = None
        processed_log = []
        catalog_records = []

        if group_mode == "minute":
            group_keys = ts.dt.floor("min")
        elif group_mode == "hour":
            group_keys = ts.dt.floor("h")
        elif group_mode == "day":
            group_keys = ts.dt.floor("D")
        else:
            raise ValueError(f"Invalid group_mode={group_mode!r}. Expected 'minute', 'hour', or 'day'.")

        n = len(ts)
        df_idx = pd.DataFrame({"idx": np.arange(n), "grp": group_keys}).set_index("grp")
        unique_grps = df_idx.index.unique()

        for grp in tqdm(unique_grps, desc="Batches"):
            indices = df_idx.loc[grp, "idx"]
            if np.isscalar(indices): indices = [indices]
            else: indices = indices.values.tolist()

            m_str = pd.Timestamp(grp).strftime("%Y-%m")
            if curr_month and m_str != curr_month:
                _flush_month_tileUTM(
                    curr_month, month_buf, master_transform, height, width, catalog_records,
                    existing_months, on_time_conflict, monthly_despeckle, despeckle_method,
                    despeckle_kwargs, min_valid_lin, eps_lin, roi_mask, mgrs_tile_id, target_crs,
                    mgrs_wkt_tileUTM,
                    texture_cfg, generate_cog, overwrite_cog, out_cog_dir, flight_suffix, cog_block,
                    copol_name, crosspol_name, ratio_name, rvi_name, generate_preview, preview_dir,
                    preview_res, zarr_path, output_root, processing_level, g, processed_log, polarization,
                    features_ratio=features_ratio, features_rvi=features_rvi,
                    features_glcm=features_glcm, tile_clip=tile_clip,
                    layout_mode=layout_mode, direction_label=direction_label,
                    product_label=product_label, feature_suffix=feature_suffix,
                )
            curr_month = m_str

            vv_a = _mosaic_align(indices, arrs_vv, prof_vv, height, width, master_transform, target_crs)
            vh_a = _mosaic_align(indices, arrs_vh, prof_vh, height, width, master_transform, target_crs)
            if vv_a is not None:
                month_buf['vv'].append(vv_a); month_buf['vh'].append(vh_a)

        if curr_month:
            _flush_month_tileUTM(
                curr_month, month_buf, master_transform, height, width, catalog_records,
                existing_months, on_time_conflict, monthly_despeckle, despeckle_method,
                despeckle_kwargs, min_valid_lin, eps_lin, roi_mask, mgrs_tile_id, target_crs,
                mgrs_wkt_tileUTM,
                texture_cfg, generate_cog, overwrite_cog, out_cog_dir, flight_suffix, cog_block,
                copol_name, crosspol_name, ratio_name, rvi_name, generate_preview, preview_dir,
                preview_res, zarr_path, output_root, processing_level, g, processed_log, polarization,
                features_ratio=features_ratio, features_rvi=features_rvi,
                features_glcm=features_glcm, tile_clip=tile_clip,
                layout_mode=layout_mode, direction_label=direction_label,
                product_label=product_label, feature_suffix=feature_suffix,
            )
        try:
            zarr.consolidate_metadata(zarr_path)
        except Exception:
            pass
    except Exception as _zarr_exc:
        logging.error(
            "[ERROR] Zarr write failed mid-batch for %s. "
            "Months written before failure are intact; "
            "the current month (%s) may be partially written. "
            "Check the Zarr store before reuse. Error: %s",
            zarr_path, curr_month if curr_month else "unknown", _zarr_exc,
        )
        raise
    finally:
        pass

    catalog_path = os.path.join(tile_dir, "catalog.parquet")
    if catalog_records:
        total = _upsert_tile_catalog(catalog_path, catalog_records)
        logging.info("[Catalog] Updated: %s (%d new, %d total records)", catalog_path, len(catalog_records), total)

    return {
        "written_months": processed_log,
        "catalog_path": catalog_path,
        "tile_dir": tile_dir,
    }


def build_s1_monthly_median_cube(
    arrs_vv,
    prof_vv,
    arrs_vh,
    prof_vh,
    acq_datetimes,
    mgrs_tile_id,
    flight_direction=None,
    output_root="./output",
    target_crs=None,
    target_res=30.0,
    roi_wkt=None,
    use_roi_mask=False,
    group_mode="hour",
    on_time_conflict="skip",
    overwrite_cog=False,
    chunk_y=None,
    chunk_x=None,
    cog_block=256,
    min_valid_lin=1e-6,
    eps_lin=1e-7,
    generate_cog=True,
    generate_preview=True,
    preview_res=300.0,
    polarization="VV+VH",
    texture_cfg=None,
    features_ratio=False,
    features_rvi=False,
    features_glcm=False,
    **kwargs,
):
    """
    Build a monthly SAR median composite cube (ARDC processing level).

    Within each acquisition group, all burst passes in a month are spatially
    mosaiced per-pass using first-valid-pixel strategy, then pixel-wise temporal
    median is computed across all passes.  No despeckle is applied, preserving
    original backscatter statistics.  All four bands (VV_dB, VH_dB, Ratio, RVI)
    are written so that the Zarr schema is identical to the hARDCp output.

    This function is the ARDC counterpart of build_s1_monthly_cog_and_zarr_tileUTM.
    """
    PROCESSING_LEVEL = "ARDC"
    copol_name, crosspol_name, ratio_name, rvi_name = get_band_names(polarization)

    # Extract new parameters from kwargs (added for v2.1.0 compatibility)
    layout_mode = kwargs.get('layout_mode', 'modern')
    product_label = kwargs.get('product_label', f'monthly_{flight_direction}_{PROCESSING_LEVEL}' if flight_direction else f'monthly_{PROCESSING_LEVEL}')
    direction_label = kwargs.get('direction_label', flight_direction)

    # Build feature suffix based on enabled features
    feature_parts = []
    if features_ratio:
        feature_parts.append('ratio')
    if features_rvi:
        feature_parts.append('rvi')
    if features_glcm:
        feature_parts.append('glcm')
    feature_suffix = '_' + '_'.join(feature_parts) if feature_parts else ''

    if not (len(arrs_vv) == len(prof_vv) == len(arrs_vh) == len(prof_vh) == len(acq_datetimes)):
        raise ValueError("Input alignment error.")

    if target_crs is None:
        target_crs = _mgrs_to_utm_epsg(mgrs_tile_id)
        logging.info("[CRS] Auto-derived from MGRS tile %s: %s", mgrs_tile_id, target_crs)

    flight_suffix = f"_{flight_direction}" if flight_direction else ""
    tile_dir_name = f"{mgrs_tile_id}{flight_suffix}"
    tile_dir = os.path.join(output_root, tile_dir_name)
    out_cog_dir = os.path.join(tile_dir, "cog")
    zarr_path = os.path.join(tile_dir, "zarr", "s1grits_monthly.zarr")
    preview_dir = os.path.join(tile_dir, "preview")

    _check_tile_integrity(tile_dir, zarr_path, product_label, layout_mode)

    os.makedirs(tile_dir, exist_ok=True)
    if generate_cog:
        os.makedirs(out_cog_dir, exist_ok=True)
    os.makedirs(os.path.dirname(zarr_path), exist_ok=True)
    if generate_preview:
        os.makedirs(preview_dir, exist_ok=True)

    ts0 = pd.to_datetime(acq_datetimes, utc=True)
    order = np.argsort(ts0.values)
    ts = pd.Series(ts0).iloc[order].reset_index(drop=True)
    arrs_vv = [arrs_vv[i] for i in order]; prof_vv = [prof_vv[i] for i in order]
    arrs_vh = [arrs_vh[i] for i in order]; prof_vh = [prof_vh[i] for i in order]

    calc_new_grid = True
    if os.path.exists(zarr_path):
        try:
            _g_lock = zarr.open_group(zarr_path, mode="r", zarr_format=3)
            if "transform" in _g_lock.attrs:
                master_transform = Affine(*_g_lock.attrs["transform"])
                target_crs = _g_lock.attrs["crs"]
                x_coords = _g_lock["x"][:]; y_coords = _g_lock["y"][:]
                width = x_coords.shape[0]; height = y_coords.shape[0]
                calc_new_grid = False
                logging.info("[Grid] Locked to Zarr: %dx%d", width, height)
        except Exception as _lock_exc:
            logging.debug("Could not read grid from existing Zarr, will recompute: %s", _lock_exc)

    if calc_new_grid:
        master_transform, width, height, x_coords, y_coords = _build_grid_from_mgrs_tile(
            mgrs_tile_id, target_crs, target_res
        )
        logging.info("[Grid] New: %dx%d", width, height)

    zarr_transform = master_transform
    zarr_height = height; zarr_width = width
    zarr_x_coords = x_coords; zarr_y_coords = y_coords

    roi_mask = None
    if use_roi_mask and roi_wkt:
        try:
            crs_ll = pyproj.CRS.from_epsg(4326)
            crs_t = pyproj.CRS.from_user_input(target_crs)
            proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
            roi_geom_proj = shp_transform(proj, shapely_wkt.loads(roi_wkt))
            roi_mask = _rasterize_roi_mask(roi_geom_proj, (zarr_height, zarr_width), zarr_transform)
        except Exception as _roi_exc:
            logging.debug("ROI mask computation skipped: %s", _roi_exc)

    mode = 'w'
    if os.path.exists(zarr_path) and not calc_new_grid:
        mode = 'r+'
    elif os.path.exists(zarr_path):
        shutil.rmtree(zarr_path)

    g = zarr.open_group(zarr_path, mode=mode, zarr_format=3)
    try:
        if mode == 'w':
            g.attrs["crs"] = str(target_crs)
            g.attrs["transform"] = tuple(zarr_transform)
            g.attrs["processing_level"] = PROCESSING_LEVEL
            from s1grits.canonical_catalog_schema import make_grid_id
            _gid = make_grid_id(mgrs_tile_id, str(target_crs), list(zarr_transform), zarr_width, zarr_height)
            g.attrs["grid_id"] = _gid
            g.attrs["grid_name"] = f"{mgrs_tile_id}_native_{int(target_res)}m"
            g.attrs["grid_version"] = 1
            g.attrs["width"] = zarr_width
            g.attrs["height"] = zarr_height
            g.attrs["resolution"] = [float(abs(zarr_transform[0])), float(abs(zarr_transform[4]))]
            g.attrs["product_type"] = "monthly"
            g.attrs["time_varying"] = True
            g.attrs["array_dims"] = ["time", "y", "x"]
            # Write variant metadata to Zarr root attrs for resync self-sufficiency
            if kwargs.get('processing_signature'):
                g.attrs['processing_signature'] = kwargs['processing_signature']
            if kwargs.get('product_variant'):
                g.attrs['product_variant'] = kwargs['product_variant']
            if kwargs.get('processing_variant_json'):
                g.attrs['processing_variant_json'] = kwargs['processing_variant_json']
            _a = g.create_array("x", data=zarr_x_coords, overwrite=True, dimension_names=["x"])
            _a.attrs["_ARRAY_DIMENSIONS"] = ["x"]
            _a = g.create_array("y", data=zarr_y_coords, overwrite=True, dimension_names=["y"])
            _a.attrs["_ARRAY_DIMENSIONS"] = ["y"]
            _a = g.create_array("time", shape=(0,), chunks=(1,), dtype="datetime64[ns]", overwrite=True, dimension_names=["time"])
            _a.attrs["_ARRAY_DIMENSIONS"] = ["time"]
            _zarr_vars = [copol_name, crosspol_name]
            if features_ratio:
                _zarr_vars.append(ratio_name)
            if features_rvi:
                _zarr_vars.append(rvi_name)
            for var in _zarr_vars:
                _a = g.create_array(var, shape=(0, zarr_height, zarr_width), chunks=(1, chunk_y, chunk_x), dtype="float32", fill_value=np.nan, overwrite=True, dimension_names=["time", "y", "x"])
                _a.attrs["_ARRAY_DIMENSIONS"] = ["time", "y", "x"]
            if features_glcm or (texture_cfg and texture_cfg.get("enabled")):
                for var in _get_texture_band_names(texture_cfg or {}):
                    _a = g.create_array(var, shape=(0, zarr_height, zarr_width), chunks=(1, chunk_y, chunk_x), dtype="float32", fill_value=np.nan, overwrite=True, dimension_names=["time", "y", "x"])
                    _a.attrs["_ARRAY_DIMENSIONS"] = ["time", "y", "x"]

            # CF-compliant grid_mapping so generic readers (GDAL/QGIS/rioxarray)
            # detect the CRS, consistent with workflow_scenes' Zarr creator.
            from s1grits.zarr_cf import add_cf_grid_mapping
            add_cf_grid_mapping(g, str(target_crs))

        existing_months = set(pd.to_datetime(np.array(g["time"][:], dtype="int64").astype("datetime64[ns]")).strftime("%Y-%m")) if "time" in g and g["time"].shape[0] > 0 else set()

        # Pre-compute MGRS polygon mask once (avoids re-rasterizing each month)
        mgrs_wkt = _get_mgrs_tile_geometry_wkt(mgrs_tile_id)
        crs_ll = pyproj.CRS.from_epsg(4326)
        crs_t = pyproj.CRS.from_user_input(target_crs)
        proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
        geom_proj = shp_transform(proj, shapely_wkt.loads(mgrs_wkt))
        mgrs_mask = rasterize(
            [(mapping(geom_proj), 1)],
            out_shape=(height, width),
            transform=master_transform,
            fill=0, dtype="uint8", all_touched=False,
        ).astype(bool)

        month_buf = {'vv': [], 'vh': []}
        curr_month = None
        processed_log = []
        catalog_records = []

        if group_mode == "minute":
            group_keys = ts.dt.floor("min")
        elif group_mode == "hour":
            group_keys = ts.dt.floor("h")
        elif group_mode == "day":
            group_keys = ts.dt.floor("D")
        else:
            raise ValueError(f"Invalid group_mode={group_mode!r}. Expected 'minute', 'hour', or 'day'.")

        n = len(ts)
        df_idx = pd.DataFrame({"idx": np.arange(n), "grp": group_keys}).set_index("grp")
        unique_grps = df_idx.index.unique()

        for grp in tqdm(unique_grps, desc="Batches (ARDC)"):
            indices = df_idx.loc[grp, "idx"]
            if np.isscalar(indices):
                indices = [indices]
            else:
                indices = indices.values.tolist()

            m_str = pd.Timestamp(grp).strftime("%Y-%m")
            if curr_month and m_str != curr_month:
                _flush_month_median_cube(
                    curr_month, month_buf, master_transform, height, width, catalog_records,
                    existing_months, on_time_conflict, min_valid_lin, eps_lin, roi_mask, mgrs_mask,
                    mgrs_tile_id, target_crs, texture_cfg, generate_cog, overwrite_cog, out_cog_dir,
                    flight_suffix, cog_block, copol_name, crosspol_name, ratio_name, rvi_name,
                    generate_preview, preview_dir, preview_res, zarr_path, output_root,
                    PROCESSING_LEVEL, g, processed_log, polarization,
                    features_ratio=features_ratio, features_rvi=features_rvi,
                    features_glcm=features_glcm,
                    layout_mode=layout_mode, direction_label=direction_label,
                    feature_suffix=feature_suffix, product_label=product_label,
                )
            curr_month = m_str

            vv_a = _mosaic_align(indices, arrs_vv, prof_vv, height, width, master_transform, target_crs)
            vh_a = _mosaic_align(indices, arrs_vh, prof_vh, height, width, master_transform, target_crs)
            if vv_a is not None:
                month_buf['vv'].append(vv_a)
                month_buf['vh'].append(vh_a)

        if curr_month:
            _flush_month_median_cube(
                curr_month, month_buf, master_transform, height, width, catalog_records,
                existing_months, on_time_conflict, min_valid_lin, eps_lin, roi_mask, mgrs_mask,
                mgrs_tile_id, target_crs, texture_cfg, generate_cog, overwrite_cog, out_cog_dir,
                flight_suffix, cog_block, copol_name, crosspol_name, ratio_name, rvi_name,
                generate_preview, preview_dir, preview_res, zarr_path, output_root,
                PROCESSING_LEVEL, g, processed_log, polarization,
                features_ratio=features_ratio, features_rvi=features_rvi,
                features_glcm=features_glcm,
                layout_mode=layout_mode, direction_label=direction_label,
                feature_suffix=feature_suffix, product_label=product_label,
            )
        try:
            zarr.consolidate_metadata(zarr_path)
        except Exception:
            pass
    except Exception as _zarr_exc:
        logging.error(
            "[ERROR] Zarr write failed mid-batch for %s. "
            "Months written before failure are intact; "
            "the current month (%s) may be partially written. "
            "Check the Zarr store before reuse. Error: %s",
            zarr_path, curr_month if curr_month else "unknown", _zarr_exc,
        )
        raise
    finally:
        pass

    catalog_path = os.path.join(tile_dir, "catalog.parquet")
    if catalog_records:
        total = _upsert_tile_catalog(catalog_path, catalog_records)
        logging.info("[Catalog] Updated: %s (%d new, %d total records)", catalog_path, len(catalog_records), total)

    return {
        "written_months": processed_log,
        "catalog_path": catalog_path,
        "tile_dir": tile_dir,
    }


# =============================================================================
# Part 5: Multi-Track Acquisition Group Merge
# =============================================================================

def _reproject_band_to_master(
    src_array, src_transform, src_crs,
    dst_transform, dst_crs, dst_height, dst_width,
    resampling_name="bilinear",
) -> np.ndarray:
    """Reproject a 2-D float32 band to the master grid.

    Parameters
    ----------
    src_array : np.ndarray
        Source 2-D array (float32). NaN pixels are treated as nodata.
    src_transform : affine.Affine
        Affine transform of the source array.
    src_crs : rasterio.crs.CRS or str
        CRS of the source array.
    dst_transform : affine.Affine
        Affine transform of the destination (master) grid.
    dst_crs : rasterio.crs.CRS or str
        CRS of the destination grid.
    dst_height : int
        Height of the destination array in pixels.
    dst_width : int
        Width of the destination array in pixels.
    resampling_name : str, optional
        Name of the rasterio Resampling enum member (default "bilinear").

    Returns
    -------
    np.ndarray
        Reprojected float32 array with shape (dst_height, dst_width).
        Pixels outside the source extent are filled with np.nan.
    """
    resampling = getattr(Resampling, resampling_name, Resampling.bilinear)

    src = src_array.astype(np.float32)
    dst = np.full((dst_height, dst_width), np.nan, dtype=np.float32)

    reproject(
        source=src,
        destination=dst,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        resampling=resampling,
        src_nodata=np.nan,
        dst_nodata=np.nan,
    )
    return dst


def merge_acq_group_zarrs(
    group_zarr_paths: dict,
    priority_order: list,
    output_root: str,
    mgrs_tile_id: str,
    flight_direction=None,
    monthly_despeckle: bool = True,
    despeckle_method: str = "tv_bregman",
    despeckle_kwargs: dict = None,
    min_valid_lin: float = 1e-6,
    eps_lin: float = 1e-7,
    keep_group_zarrs: bool = False,
    generate_cog: bool = True,
    generate_preview: bool = True,
    preview_res: float = 300.0,
    cog_block: int = 256,
    chunk_y: int = 1024,
    chunk_x: int = 1024,
    overwrite_cog: bool = False,
    on_time_conflict: str = "skip",
    polarization: str = "VV+VH",
    processing_level: str = "hARDCp",
    texture_cfg=None,
) -> dict:
    """Merge multiple per-acquisition-group Zarr stores into a single merged tile.

    For each calendar month that appears in any group, this function builds a
    merged mosaic using a first-valid-pixel priority scheme applied in the
    linear backscatter domain.  The priority of groups is determined by
    ``priority_order`` (first entry = highest priority).

    After mosaicking, optional TV-Bregman despeckling is applied to VV and VH
    linear bands before converting to dB and deriving SAR indices.

    The function writes:
    - A merged Zarr v3 store (monthly time-series)
    - Cloud-Optimised GeoTIFFs  (one per month, optional)
    - PNG preview thumbnails    (optional)
    - Per-tile ``catalog.parquet``
    - STAC item JSONs

    Parameters
    ----------
    group_zarr_paths : dict
        Mapping of group label (str) -> absolute path to the group's Zarr store.
        Example: ``{"ASC_135": "/data/tile/ASC_135.zarr", "DSC_064": ...}``
    priority_order : list
        Ordered list of group labels. The first label has the highest priority
        (its valid pixels are kept; lower-priority groups fill gaps).
    output_root : str
        Root output directory.  Merged outputs land in
        ``<output_root>/<mgrs_tile_id>/merged/``.
    mgrs_tile_id : str
        MGRS tile identifier (e.g. ``"32TNT"``).
    flight_direction : str or None, optional
        Flight direction tag used in file naming (e.g. ``"MERGED"``).
        Defaults to ``"MERGED"`` when *None*.
    monthly_despeckle : bool, optional
        If *True*, apply despeckling to VV and VH linear mosaics before index
        computation (default ``True``).
    despeckle_method : str, optional
        Despeckling algorithm name passed to ``despeckle_2d`` (default
        ``"tv_bregman"``).
    despeckle_kwargs : dict or None, optional
        Extra keyword arguments forwarded to ``despeckle_2d``.
    min_valid_lin : float, optional
        Minimum linear backscatter value considered valid (default ``1e-6``).
    eps_lin : float, optional
        Small epsilon added before log conversion to avoid log(0) (default
        ``1e-7``).
    keep_group_zarrs : bool, optional
        If *False* (default), the per-group Zarr directories are removed after
        a successful merge.
    generate_cog : bool, optional
        Write Cloud-Optimised GeoTIFFs (default ``True``).
    generate_preview : bool, optional
        Write PNG preview thumbnails (default ``True``).
    preview_res : float, optional
        Target resolution (metres) of preview thumbnails (default ``300.0``).
    cog_block : int, optional
        COG internal tile size in pixels (default ``256``).
    chunk_y : int, optional
        Zarr chunk size along Y axis (default ``1024``).
    chunk_x : int, optional
        Zarr chunk size along X axis (default ``1024``).
    overwrite_cog : bool, optional
        Overwrite existing COG files (default ``False``).
    on_time_conflict : str, optional
        Strategy when a month already exists in the Zarr store: ``"skip"``
        (default) or ``"overwrite"``.
    polarization : str, optional
        Polarisation mode string (default ``"VV+VH"``).
    processing_level : str, optional
        Processing level tag used in STAC metadata (default ``"hARDCp"``).
    texture_cfg : dict or None, optional
        GLCM texture configuration forwarded to COG writers.

    Returns
    -------
    dict
        ``{"written_months": list, "catalog_path": str, "tile_dir": str}``
    """
    if flight_direction is None:
        flight_direction = "MERGED"

    # Use module-level zarr import (aliased to _zarr to avoid shadowing the
    # module name in case callers import zarr themselves)
    _zarr = zarr

    flight_suffix = flight_direction.upper()
    PROCESSING_LEVEL = processing_level

    # Band / index names
    copol_name  = "VV"
    crosspol_name = "VH"
    ratio_name  = "Ratio"
    rvi_name    = "RVI"

    if despeckle_kwargs is None:
        despeckle_kwargs = {}

    # ------------------------------------------------------------------
    # Resolve output paths
    # ------------------------------------------------------------------
    tile_dir   = os.path.join(output_root, mgrs_tile_id, "merged")
    os.makedirs(tile_dir, exist_ok=True)

    zarr_name  = f"s1grits_monthly_{mgrs_tile_id}{flight_suffix}_{PROCESSING_LEVEL}.zarr"
    zarr_path  = os.path.join(tile_dir, zarr_name)

    out_cog_dir  = os.path.join(tile_dir, "cog")
    preview_dir  = os.path.join(tile_dir, "preview")
    if generate_cog:
        os.makedirs(out_cog_dir, exist_ok=True)
    if generate_preview:
        os.makedirs(preview_dir, exist_ok=True)

    catalog_records: list = []
    processed_log:   list = []

    # ------------------------------------------------------------------
    # Open all group Zarr stores; collect metadata from the master group
    # ------------------------------------------------------------------
    # Determine master group: use the first entry in priority_order that
    # has a readable Zarr store.
    master_label = None
    master_store = None
    group_stores: dict = {}

    for label in priority_order:
        zpath = group_zarr_paths.get(label)
        if zpath is None or not os.path.isdir(zpath):
            logging.warning("[Merge] Group '%s' zarr not found at %s — skipping.", label, zpath)
            continue
        try:
            store = _zarr.open(zpath, mode="r")
            group_stores[label] = (store, zpath)
            if master_label is None:
                master_label = label
                master_store = store
        except Exception as exc:
            logging.warning("[Merge] Cannot open zarr for group '%s': %s", label, exc)

    if master_label is None:
        raise RuntimeError(
            f"[Merge] No readable group Zarr stores found for tile {mgrs_tile_id}. "
            f"Checked labels: {list(group_zarr_paths.keys())}"
        )

    logging.info("[Merge] Master group: '%s'  |  Additional groups: %s",
                 master_label, [l for l in group_stores if l != master_label])

    # Read spatial metadata from master store
    master_meta  = dict(master_store.attrs)
    master_crs_wkt = master_meta.get("crs_wkt") or master_meta.get("crs")
    master_transform_tuple = master_meta.get("transform")  # stored as list/tuple
    _first_arr = next(arr for name, arr in master_store.arrays() if name not in ('x', 'y', 'time', 'spatial_ref'))
    height = int(master_meta.get("height", _first_arr.shape[-2]))
    width  = int(master_meta.get("width",  _first_arr.shape[-1]))

    if master_transform_tuple is None:
        raise RuntimeError(f"[Merge] Master zarr '{master_label}' has no 'transform' attribute.")

    master_transform = Affine(*master_transform_tuple[:6])
    master_crs       = rcrs.CRS.from_user_input(master_crs_wkt)

    # ------------------------------------------------------------------
    # Collect union of available months across all groups.
    # Pre-build a months-set per group to avoid O(N*K) zarr key enumeration
    # inside the per-month loop (each _list_months_from_zarr call iterates
    # all group keys; doing it once per group is O(K), not O(N*K)).
    # ------------------------------------------------------------------
    all_months: set = set()
    group_months: dict = {}   # label -> frozenset of month strings
    for label, (store, _) in group_stores.items():
        months = frozenset(_list_months_from_zarr(store))
        group_months[label] = months
        all_months.update(months)

    if not all_months:
        logging.warning("[Merge] No monthly data found across any group for tile %s.", mgrs_tile_id)
        return {"written_months": [], "catalog_path": None, "tile_dir": tile_dir}

    logging.info("[Merge] Union of months to process: %s", sorted(all_months))

    # ------------------------------------------------------------------
    # Open / create merged Zarr store
    # ------------------------------------------------------------------
    band_names = [copol_name, crosspol_name, ratio_name, rvi_name,
                  f"{copol_name}_dB", f"{crosspol_name}_dB"]
    if texture_cfg:
        for feat in texture_cfg.get("features", []):
            band_names.append(f"GLCM_{feat}")

    n_bands = len(band_names)

    try:
        g_merged = _zarr.open_group(zarr_path, mode="a")
    except Exception as exc:
        raise RuntimeError(f"[Merge] Cannot open/create merged Zarr at {zarr_path}: {exc}") from exc

    # Store spatial metadata
    for k, v in {
        "tile_id":          mgrs_tile_id,
        "flight_direction": flight_direction,
        "processing_level": PROCESSING_LEVEL,
        "crs_wkt":          master_crs.to_wkt(),
        "transform":        list(master_transform)[:6],
        "height":           height,
        "width":            width,
        "band_names":       band_names,
        "polarization":     polarization,
        "source_groups":    list(group_stores.keys()),
        "priority_order":   priority_order,
    }.items():
        g_merged.attrs[k] = v

    existing_months = set(_list_months_from_zarr(g_merged))

    # ------------------------------------------------------------------
    # Process each month
    # ------------------------------------------------------------------
    for month in sorted(all_months):

        if month in existing_months and on_time_conflict == "skip":
            logging.info("[Merge] Month %s already exists in merged zarr — skipping.", month)
            continue

        logging.info("[Merge] Processing month %s ...", month)

        # Apply priority: iterate from lowest to highest (so highest overwrites).
        # Use pre-built group_months sets — no zarr key enumeration per iteration.
        # Mosaic arrays are allocated lazily on first group that contributes data.
        vv_mosaic = None
        vh_mosaic = None

        for label in reversed(priority_order):
            if label not in group_stores:
                continue
            store, _ = group_stores[label]
            if month not in group_months[label]:
                continue

            month_grp = store[month]
            grp_attrs = month_grp.attrs  # zarr attrs support .get() directly; no dict() needed

            # Read VV and VH linear bands from this group month
            vv_src = _read_zarr_band_as_linear(month_grp, copol_name)
            vh_src = _read_zarr_band_as_linear(month_grp, crosspol_name)

            if vv_src is None or vh_src is None:
                logging.warning("[Merge] Group '%s' month %s missing VV or VH band — skipping group.", label, month)
                continue

            # Reproject to master grid if needed
            grp_transform_list = grp_attrs.get("transform") or grp_attrs.get("geotransform")
            grp_crs_str = grp_attrs.get("crs_wkt") or grp_attrs.get("crs")

            if grp_transform_list is not None and grp_crs_str is not None:
                grp_transform = Affine(*grp_transform_list[:6])
                grp_crs = rcrs.CRS.from_user_input(grp_crs_str)

                # Check if reprojection is needed
                need_reproject = (
                    grp_crs != master_crs
                    or grp_transform != master_transform
                    or vv_src.shape != (height, width)
                )
                if need_reproject:
                    logging.debug("[Merge] Reprojecting group '%s' month %s to master grid.", label, month)
                    vv_src = _reproject_band_to_master(
                        vv_src, grp_transform, grp_crs,
                        master_transform, master_crs, height, width,
                    )
                    vh_src = _reproject_band_to_master(
                        vh_src, grp_transform, grp_crs,
                        master_transform, master_crs, height, width,
                    )

            # Validate shape
            if vv_src.shape != (height, width) or vh_src.shape != (height, width):
                logging.warning(
                    "[Merge] Group '%s' month %s shape mismatch after reproject "
                    "(%s vs master %s) — skipping group.",
                    label, month, vv_src.shape, (height, width),
                )
                continue

            # Allocate mosaic arrays lazily on first group that passes validation.
            if vv_mosaic is None:
                vv_mosaic = np.full((height, width), np.nan, dtype=np.float32)
                vh_mosaic = np.full((height, width), np.nan, dtype=np.float32)

            # First-valid-pixel layering (highest priority overwrites last)
            valid_mask = (
                np.isfinite(vv_src) & (vv_src > min_valid_lin)
                & np.isfinite(vh_src) & (vh_src > min_valid_lin)
            )
            vv_mosaic[valid_mask] = vv_src[valid_mask]
            vh_mosaic[valid_mask] = vh_src[valid_mask]

        # Check if anything was collected
        if vv_mosaic is None:
            logging.warning("[Merge] Month %s: no contributing groups found — skipping.", month)
            continue
        valid_total = np.sum(np.isfinite(vv_mosaic) & (vv_mosaic > min_valid_lin))
        if valid_total == 0:
            logging.warning("[Merge] Month %s: mosaic is entirely empty — skipping.", month)
            continue

        # ------------------------------------------------------------------
        # Optional despeckling on linear mosaics
        # ------------------------------------------------------------------
        if monthly_despeckle:
            try:
                vv_mosaic = despeckle_2d(vv_mosaic, method=despeckle_method, **despeckle_kwargs)
                vh_mosaic = despeckle_2d(vh_mosaic, method=despeckle_method, **despeckle_kwargs)
            except Exception as ds_exc:
                logging.warning("[Merge] Despeckling failed for month %s: %s — proceeding without.", month, ds_exc)

        # ------------------------------------------------------------------
        # Derive SAR indices
        # ------------------------------------------------------------------
        # Mask invalid pixels
        valid_mask = (
            np.isfinite(vv_mosaic) & (vv_mosaic > min_valid_lin)
            & np.isfinite(vh_mosaic) & (vh_mosaic > min_valid_lin)
        )

        # np.where() with float32 inputs produces float32 output; no .astype() needed.
        vv_lin = np.where(valid_mask, vv_mosaic, np.nan)
        vh_lin = np.where(valid_mask, vh_mosaic, np.nan)

        # dB conversion
        vv_db = np.where(valid_mask, 10.0 * np.log10(vv_lin + eps_lin), np.nan)
        vh_db = np.where(valid_mask, 10.0 * np.log10(vh_lin + eps_lin), np.nan)

        # Ratio: VH / VV  (standard SAR convention per CLAUDE.md)
        ratio = np.where(valid_mask, vh_lin / (vv_lin + eps_lin), np.nan)

        # RVI: 4*VH / (VV + VH)  — theoretical range [0, 4]; do NOT clip
        rvi = np.where(valid_mask, (4.0 * vh_lin) / (vv_lin + vh_lin + eps_lin), np.nan)

        # ------------------------------------------------------------------
        # Write month to merged Zarr
        # ------------------------------------------------------------------
        month_grp = g_merged.require_group(month)

        _write_zarr_dataset(month_grp, copol_name,            vv_lin, chunk_y, chunk_x, on_time_conflict)
        _write_zarr_dataset(month_grp, crosspol_name,         vh_lin, chunk_y, chunk_x, on_time_conflict)
        _write_zarr_dataset(month_grp, f"{copol_name}_dB",    vv_db,  chunk_y, chunk_x, on_time_conflict)
        _write_zarr_dataset(month_grp, f"{crosspol_name}_dB", vh_db,  chunk_y, chunk_x, on_time_conflict)
        _write_zarr_dataset(month_grp, ratio_name,             ratio,  chunk_y, chunk_x, on_time_conflict)
        _write_zarr_dataset(month_grp, rvi_name,               rvi,    chunk_y, chunk_x, on_time_conflict)

        for k, v in {
            "month":         month,
            "transform":     list(master_transform)[:6],
            "crs_wkt":       master_crs.to_wkt(),
            "height":        height,
            "width":         width,
            "source_groups": [l for l in priority_order if l in group_stores],
        }.items():
            month_grp.attrs[k] = v

        processed_log.append(month)

        # ------------------------------------------------------------------
        # Stack bands for COG / preview writing
        # ------------------------------------------------------------------
        band_stack = np.stack([vv_lin, vh_lin, ratio, rvi, vv_db, vh_db], axis=0)

        if texture_cfg and _compute_glcm_texture is not None:
            try:
                tex_bands = _compute_glcm_texture(vv_db, texture_cfg)
                band_stack = np.concatenate([band_stack, tex_bands], axis=0)
            except Exception as tex_exc:
                logging.warning("[Merge] GLCM texture failed for month %s: %s", month, tex_exc)

        # ------------------------------------------------------------------
        # COG: inline rasterio write (consistent with _flush_month_* functions)
        # ------------------------------------------------------------------
        cog_p = None
        if generate_cog:
            cog_filename = (
                f"{mgrs_tile_id}_{flight_suffix}_{PROCESSING_LEVEL}_{month}.tif"
            )
            cog_p = os.path.join(out_cog_dir, cog_filename)
            if overwrite_cog or not os.path.isfile(cog_p):
                try:
                    NODATA_OUT = NODATA_SENTINEL
                    n_bands_stack = band_stack.shape[0]
                    merge_profile = {
                        "driver": "GTiff", "width": width, "height": height,
                        "count": n_bands_stack, "dtype": "float32",
                        "nodata": NODATA_OUT, "tiled": True,
                        "blockxsize": int(cog_block), "blockysize": int(cog_block),
                        "compress": "lzw", "predictor": 3, "interleave": "pixel",
                        "transform": master_transform, "crs": master_crs,
                    }
                    with atomic_path(cog_p) as _cog_tmp, rasterio.open(_cog_tmp, "w", **merge_profile) as dst:
                        for band_i, band_arr in enumerate(band_stack, start=1):
                            dst.write(_prep_array(band_arr, NODATA_OUT), band_i)
                            if band_i - 1 < len(band_names):
                                dst.set_band_description(band_i, band_names[band_i - 1])
                        dst.update_tags(time=month, processing_level=PROCESSING_LEVEL)
                    logging.info("[Merge] COG written: %s", cog_p)
                except Exception as cog_exc:
                    logging.error("[Merge] COG write failed for month %s: %s", month, cog_exc)
                    cog_p = None
            else:
                logging.debug("[Merge] COG exists, skipping: %s", cog_p)

        # ------------------------------------------------------------------
        # Preview thumbnail: use _generate_preview_png (same as flush functions)
        # ------------------------------------------------------------------
        preview_metadata = None
        if generate_preview:
            try:
                preview_path = os.path.join(
                    preview_dir,
                    f"s1grits_monthly_{mgrs_tile_id}{flight_suffix}_{PROCESSING_LEVEL}_{month}.png",
                )
                preview_metadata = _generate_preview_png(
                    vv_db=vv_db, vh_db=vh_db, ratio=ratio,
                    src_transform=master_transform, src_crs=master_crs,
                    output_path=preview_path, preview_res=preview_res,
                    preview_crs="EPSG:4326",
                )
                logging.debug("[Merge] Preview written: %s", preview_path)
            except Exception as prev_exc:
                logging.warning("[Merge] Preview write failed for month %s: %s", month, prev_exc)

        # ------------------------------------------------------------------
        # Catalog record + STAC item (consistent with _flush_month_* functions)
        # ------------------------------------------------------------------
        zarr_rel_path    = os.path.relpath(zarr_path, output_root)
        cog_rel_path     = os.path.relpath(cog_p, output_root) if cog_p else None
        preview_rel_path = os.path.relpath(preview_metadata["path"], output_root) if preview_metadata else None

        catalog_records.append(normalize_catalog_record({
            "tile_id":     mgrs_tile_id,
            "flight_direction": flight_direction,
            "month":            month,
            "datetime":         np.datetime64(f"{month}-01", "ns"),
            "zarr_path":        zarr_rel_path,
            "cog_path":         cog_rel_path,
            "preview_path":     preview_rel_path,
            "processing_level": PROCESSING_LEVEL,
            "output_type":      "monthly",
            "collection_id":    "s1grits-monthly",
            "product_type":     "monthly",
            "product_label":    f"monthly_{flight_direction}_{PROCESSING_LEVEL}" if flight_direction else f"monthly_{PROCESSING_LEVEL}",
            "polarization":     polarization,
            "source_groups":    ",".join([l for l in priority_order if l in group_stores]),
            "crs":              str(master_crs),
            "width":            width,
            "height":           height,
            "transform":        list(master_transform),
            "preview_crs":      preview_metadata["crs"] if preview_metadata else None,
            "preview_width":    preview_metadata["width"] if preview_metadata else None,
            "preview_height":   preview_metadata["height"] if preview_metadata else None,
            "preview_bounds":   preview_metadata["bounds"] if preview_metadata else None,
            "bands":            json.dumps(band_names),
        }))

        try:
            write_stac_item(catalog_records[-1], output_root, polarization)
        except Exception as stac_exc:
            logging.warning("[Merge] STAC item write failed for month %s: %s", month, stac_exc)

    # ------------------------------------------------------------------
    # Consolidate Zarr metadata
    # ------------------------------------------------------------------
    try:
        _zarr.consolidate_metadata(zarr_path)
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Update per-tile catalog
    # ------------------------------------------------------------------
    catalog_path = os.path.join(tile_dir, "catalog.parquet")
    if catalog_records:
        total = _upsert_tile_catalog(catalog_path, catalog_records)
        logging.info(
            "[Merge][Catalog] Updated: %s (%d new, %d total records)",
            catalog_path, len(catalog_records), total,
        )

    # ------------------------------------------------------------------
    # Optionally remove per-group Zarr directories
    # ------------------------------------------------------------------
    if not keep_group_zarrs:
        for label, (_, zpath) in group_stores.items():
            try:
                shutil.rmtree(zpath)
                logging.info("[Merge] Removed group zarr: %s", zpath)
            except Exception as rm_exc:
                logging.warning("[Merge] Could not remove group zarr '%s': %s", zpath, rm_exc)

    return {
        "written_months": processed_log,
        "catalog_path":   catalog_path,
        "tile_dir":       tile_dir,
    }


# =============================================================================
# Part 6: Global Catalog Management
# =============================================================================

def _coerce_catalog_datetimes(df):
    """Normalise temporal columns to a uniform tz-naive ``datetime64[ns]``.

    Different workflows (and older builds) may persist ``datetime`` as
    tz-aware, tz-naive, or string values.  When such a catalog is read back
    and concatenated with freshly built (tz-naive) records, the merged column
    becomes ``object`` dtype holding a mix of tz-aware and tz-naive
    ``Timestamp`` objects.  Sorting that column then raises
    ``TypeError: Cannot compare tz-naive and tz-aware timestamps``.

    Coercing every temporal column to tz-naive UTC up front makes the catalog
    internally consistent and safely sortable, regardless of how the existing
    records were written.
    """
    for col in ("datetime", "start_datetime", "end_datetime"):
        if col in df.columns:
            coerced = pd.to_datetime(df[col], utc=True, errors="coerce")
            try:
                coerced = coerced.dt.tz_localize(None)
            except (AttributeError, TypeError):
                pass
            df[col] = coerced
    return df


def _upsert_tile_catalog(catalog_path: str, new_records: list) -> int:
    """Incrementally update a per-tile catalog Parquet file.

    Reads the existing catalog (if any), appends *new_records*, deduplicates
    on the composite key ``(mgrs_tile_id, flight_direction, month)``, sorts
    by ``(mgrs_tile_id, datetime)``, writes back, and returns the total number
    of records in the updated catalog.

    Parameters
    ----------
    catalog_path : str
        Absolute path to the ``catalog.parquet`` file.  The parent directory
        must already exist.
    new_records : list of dict
        List of record dicts to add.  Each dict must contain at least the keys
        ``mgrs_tile_id``, ``flight_direction``, and ``month``.

    Returns
    -------
    int
        Total number of records in the catalog after the upsert.
    """
    new_df = pd.DataFrame(new_records)

    if os.path.isfile(catalog_path):
        try:
            existing_df = pd.read_parquet(catalog_path)
            combined = pd.concat([existing_df, new_df], ignore_index=True)
        except Exception as read_exc:
            logging.warning(
                "[Catalog] Could not read existing catalog at %s: %s — "
                "overwriting with new records only.",
                catalog_path, read_exc,
            )
            combined = new_df
    else:
        combined = new_df

    # Normalise temporal columns so existing (possibly tz-aware) and new
    # (tz-naive) records compare/dedup/sort consistently (no tz TypeError).
    # Done before dedup so datetime is a stable, uniform key.
    combined = _coerce_catalog_datetimes(combined)

    # Deduplicate: keep the last occurrence (new record wins on conflict).
    # ``datetime`` is part of the key so that per-acquisition scene records
    # (which share a month but differ by acquisition time) are not collapsed
    # when this monthly-workflow upsert rewrites the shared per-tile catalog.
    # Aligned with the scenes workflow's per-tile dedup keys.
    dedup_keys = ["tile_id", "flight_direction", "datetime", "product_type", "month"]
    available_keys = [k for k in dedup_keys if k in combined.columns]
    if available_keys:
        combined = combined.drop_duplicates(subset=available_keys, keep="last")

    # Sort for deterministic output
    sort_keys = [k for k in ["tile_id", "datetime"] if k in combined.columns]
    if sort_keys:
        combined = combined.sort_values(sort_keys).reset_index(drop=True)

    from s1grits.atomic_write import atomic_write_parquet
    atomic_write_parquet(combined, catalog_path)
    return len(combined)


def merge_tile_catalogs(output_root: str, output_catalog: str = None):
    """Walk *output_root* and merge all per-tile ``catalog.parquet`` files.

    Collects every ``catalog.parquet`` found under *output_root* (at any
    depth), concatenates them, deduplicates on ``(mgrs_tile_id,
    flight_direction, month)``, sorts by ``(mgrs_tile_id, datetime)``, writes
    a global catalog Parquet file, and calls ``write_stac_collection`` to
    produce a STAC collection JSON from the combined records.

    Parameters
    ----------
    output_root : str
        Root directory to search for per-tile ``catalog.parquet`` files.
    output_catalog : str or None, optional
        Destination path for the global catalog Parquet file.  Defaults to
        ``<output_root>/catalog.parquet``.

    Returns
    -------
    str
        Absolute path to the written global catalog Parquet file.

    Raises
    ------
    FileNotFoundError
        If no ``catalog.parquet`` files are found under *output_root*.
    """
    if output_catalog is None:
        output_catalog = os.path.join(output_root, "catalog.parquet")

    # Collect all per-tile catalog files
    tile_catalog_files = []
    for dirpath, _dirnames, filenames in os.walk(output_root):
        for fname in filenames:
            if fname == "catalog.parquet":
                tile_catalog_files.append(os.path.join(dirpath, fname))

    if not tile_catalog_files:
        raise FileNotFoundError(
            f"[GlobalCatalog] No 'catalog.parquet' files found under: {output_root}"
        )

    logging.info("[GlobalCatalog] Found %d per-tile catalog files.", len(tile_catalog_files))

    # Load and concatenate. Each per-tile catalog's asset/item paths are
    # rebased to be output-root-relative (prepending the tile directory's path
    # relative to output_root) so the global catalog resolves uniformly via the
    # resolver. The previously-written global catalog (if any) is skipped so it
    # is never merged into itself.
    _global_abs = os.path.abspath(output_catalog)
    dfs = []
    for fpath in tile_catalog_files:
        if os.path.abspath(fpath) == _global_abs:
            continue
        try:
            df = pd.read_parquet(fpath)
            tile_prefix = os.path.relpath(os.path.dirname(fpath), output_root)
            df = rebase_catalog_paths(df, tile_prefix)
            dfs.append(df)
        except Exception as read_exc:
            logging.warning("[GlobalCatalog] Could not read %s: %s — skipping.", fpath, read_exc)

    if not dfs:
        raise RuntimeError("[GlobalCatalog] All per-tile catalog files failed to load.")

    combined = pd.concat(dfs, ignore_index=True)
    logging.info("[GlobalCatalog] Combined %d raw records from %d files.", len(combined), len(dfs))

    # Normalise temporal columns across per-tile catalogs (which may have been
    # written with differing tz-awareness) before dedup/sort.
    combined = _coerce_catalog_datetimes(combined)

    # Deduplicate. ``datetime`` is part of the key so per-acquisition scene
    # records sharing a month are preserved (aligned with the per-tile and
    # scenes-workflow dedup keys).
    dedup_keys = ["tile_id", "flight_direction", "datetime", "product_type", "month"]
    available_keys = [k for k in dedup_keys if k in combined.columns]
    if available_keys:
        combined = combined.drop_duplicates(subset=available_keys, keep="last")
        logging.info("[GlobalCatalog] After dedup: %d records.", len(combined))

    # Sort
    sort_keys = [k for k in ["tile_id", "datetime"] if k in combined.columns]
    if sort_keys:
        combined = combined.sort_values(sort_keys).reset_index(drop=True)

    # Write global catalog
    os.makedirs(os.path.dirname(os.path.abspath(output_catalog)), exist_ok=True)
    from s1grits.atomic_write import atomic_write_parquet
    atomic_write_parquet(combined, output_catalog)
    logging.info("[GlobalCatalog] Written: %s (%d records)", output_catalog, len(combined))

    # Generate STAC collection from the global catalog records
    try:
        write_stac_collection(combined, output_root, "s1grits-monthly")
    except Exception as stac_exc:
        logging.warning("[GlobalCatalog] STAC collection write failed: %s", stac_exc)

    return output_catalog
