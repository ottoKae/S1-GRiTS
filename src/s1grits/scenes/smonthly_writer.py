"""Monthly composite (smonthly) writer family.

Extracted move-only from workflow_scenes.py (which re-exports every name
here): per-track monthly composites, the blockwise month writers (single- and
multi-track), halo GLCM pass, the COG/preview exporter, and the
monthly-of-scenes orchestrator. Test seams are preserved via facade dispatch
(_ws_mosaic_align / _ws_write_monthly_stac_item).
"""
from __future__ import annotations

import gc
import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pyproj
import zarr
from rasterio.features import rasterize
from rasterio.transform import Affine
from shapely import wkt as shapely_wkt
from shapely.geometry import mapping
from shapely.ops import transform as shp_transform

try:  # Fast C nanmedian; NumPy's masked-array path is ~50x slower on blocks
    import bottleneck as _bn
except ImportError:  # pragma: no cover - optional accelerator
    _bn = None

from s1grits.asf_output_writing import (
    _clip_arrays_to_wkt_4326,
    _get_mgrs_tile_geometry_wkt,
    _generate_preview_png,
    _zarr_delete_timestep,
)
from s1grits.canonical_catalog_schema import normalize_catalog_record
from s1grits.logger_config import get_logger
from s1grits.product_instance import (
    make_processing_signature,
    make_product_variant,
)
from s1grits.scenes.blocks import (
    GLCM_BLOCK_HALO,
    N_OBS_BAND,
    _apply_block_clip,
    _begin_zarr_timestep_blockwise,
    _finalize_zarr_timestep_blockwise,
    _iter_spatial_blocks,
    _linear_to_db,
    _prepare_block_clip_geom,
    _resolve_blockwise_threads,
    _rollback_zarr_timestep_blockwise,
    _run_blocks,
    _write_smonthly_block_bands,
)
from s1grits.scenes.cog import _write_multiband_cog
from s1grits.scenes.mosaic import (
    _compute_scene_dst_bounds,
    _mosaic_align_window,
    _prealign_scenes_to_master_grid,
    _ws_mosaic_align,
)
from s1grits.scenes.stac_items import _ws_write_monthly_stac_item
from s1grits.scenes.store import (
    _append_zarr_timestep,
    _init_zarr_2band,
)

logger = get_logger(__name__)

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

def _monthly_composite_block(
    stack: list[np.ndarray],
    method: str,
    trim_fraction: float,
) -> np.ndarray | None:
    """Composite a stack for one spatial block."""
    if not stack:
        return None
    arr = np.stack(stack, axis=0)
    method_norm = str(method or "median").lower()
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', 'All-NaN slice')
        warnings.filterwarnings('ignore', 'Mean of empty slice')
        if method_norm in {"median", "nanmedian"}:
            if _bn is not None:
                # bottleneck partitions ``arr`` in place, which is safe here
                # because ``arr`` is a fresh np.stack copy.  ~50x faster than
                # np.nanmedian's masked-array path for stack depths < 600.
                out = _bn.nanmedian(arr, axis=0)
            else:
                out = np.nanmedian(arr, axis=0)
        elif method_norm in {"min", "nanmin"}:
            out = np.nanmin(arr, axis=0)
        elif method_norm in {"mean", "nanmean"}:
            out = np.nanmean(arr, axis=0)
        elif method_norm == "trimmed_mean":
            from scipy.stats import trim_mean
            out = trim_mean(arr, trim_fraction, axis=0)
        else:
            out = np.nanmedian(arr, axis=0)
    return out.astype(np.float32, copy=False)

def _track_composite_block(
    idxs: list[int],
    final_arr: list,
    prof_arr: list,
    height: int,
    width: int,
    transform: Affine,
    target_crs: str,
    y_slice: slice,
    x_slice: slice,
    composite_method: str,
    trim_fraction: float,
    scene_bounds: list | None = None,
    with_count: bool = False,
):
    """Composite one track's scenes for a spatial block.

    Returns the composite array (or ``None`` when no scene contributes).
    With ``with_count=True`` returns ``(composite, count)`` instead, where
    ``count`` is the per-pixel number of finite scene observations in the
    stack (float32; the caller writes it into the uint8 ``n_obs`` band) —
    computed from the already-aligned stack so it adds no extra warps.
    """
    stack = []
    for idx in idxs:
        arr = _mosaic_align_window(
            [idx], final_arr, prof_arr, height, width,
            transform, target_crs, y_slice, x_slice,
            scene_bounds=scene_bounds,
        )
        if arr is not None:
            stack.append(arr)
    composite = _monthly_composite_block(stack, composite_method, trim_fraction)
    if not with_count:
        return composite
    if composite is None:
        return None, None
    count = np.zeros(composite.shape, dtype=np.float32)
    for arr in stack:
        count += np.isfinite(arr)
    return composite, count

def _track_valid_mask_block(
    idxs: list[int],
    final_arr: list,
    prof_arr: list,
    height: int,
    width: int,
    transform: Affine,
    target_crs: str,
    y_slice: slice,
    x_slice: slice,
    scene_bounds: list | None = None,
) -> np.ndarray | None:
    """Union of finite pixels across a track's aligned scenes for one block.

    A pixel of a median/mean/min composite is finite iff at least one aligned
    scene is finite there, so this mask equals the composite's finite mask
    without computing any composite.  (``trimmed_mean`` can propagate NaN, so
    for that method this slightly overestimates coverage; the result is only
    used for track priority ordering.)  Returns ``None`` when no scene
    contributes to the block.
    """
    mask: np.ndarray | None = None
    for idx in idxs:
        arr = _mosaic_align_window(
            [idx], final_arr, prof_arr, height, width,
            transform, target_crs, y_slice, x_slice,
            scene_bounds=scene_bounds,
        )
        if arr is None:
            continue
        finite = np.isfinite(arr)
        mask = finite if mask is None else np.logical_or(mask, finite, out=mask)
    return mask

def _make_smonthly_block_bands(
    composite_vv_lin: np.ndarray,
    composite_vh_lin: np.ndarray,
    copol_name: str,
    crosspol_name: str,
    features_ratio: bool,
    features_rvi: bool,
    ratio_name: str,
    rvi_name: str,
) -> dict[str, np.ndarray]:
    arr_vv_db = _linear_to_db(composite_vv_lin)
    arr_vh_db = _linear_to_db(composite_vh_lin)
    block_bands: dict[str, np.ndarray] = {
        copol_name: arr_vv_db,
        crosspol_name: arr_vh_db,
    }

    if features_ratio:
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(
                composite_vv_lin > 0,
                composite_vh_lin / composite_vv_lin,
                np.nan,
            ).astype(np.float32)
        block_bands[ratio_name] = ratio

    if features_rvi:
        denom = composite_vv_lin + composite_vh_lin
        with np.errstate(divide="ignore", invalid="ignore"):
            rvi = np.where(
                denom > 0,
                4.0 * composite_vh_lin / denom,
                np.nan,
            ).astype(np.float32)
        block_bands[rvi_name] = rvi

    return block_bands

def _smonthly_texture_cfg(copol_name: str, crosspol_name: str) -> dict:
    """The fixed GLCM texture config used by the smonthly writers.

    Kept identical to the legacy full-tile path so blockwise GLCM is bit-exact:
    no vv_db_range/vh_db_range keys, so compute_glcm_texture_bands uses its
    defaults ([-25, 5] / [-32, -5]).
    """
    return {
        "enabled": True, "inputs": [copol_name, crosspol_name],
        "metrics": ["contrast", "homogeneity", "entropy", "correlation"],
        "window_size": 5, "distance": 1, "angles": [0, 90],
        "average_angles": True, "levels": 16,
    }

def _priority_mosaic_lin_window(
    idx_by_track: dict,
    track_order: list,
    final_vv: list,
    prof_vv: list,
    final_vh: list,
    prof_vh: list,
    height: int,
    width: int,
    transform: Affine,
    target_crs: str,
    y_slice: slice,
    x_slice: slice,
    composite_method: str,
    trim_fraction: float,
    bounds_vv: list | None,
    bounds_vh: list | None,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Composite the unclipped VV/VH linear priority mosaic for one window.

    Mirrors the pass-2 first-valid-pixel priority fill (VV drives the per-pixel
    source so VV/VH stay co-sourced), producing exactly the composite the main
    blockwise write produces — but returned as linear arrays for the GLCM pass
    to convert to dB before clipping.  Returns ``(None, None)`` when no track
    contributes to the window.
    """
    bh = int((y_slice.stop or 0) - (y_slice.start or 0))
    bw = int((x_slice.stop or 0) - (x_slice.start or 0))
    vv = np.full((bh, bw), np.nan, dtype=np.float32)
    vh = np.full((bh, bw), np.nan, dtype=np.float32)
    filled = np.zeros((bh, bw), dtype=bool)
    any_data = False
    for tk in track_order:
        tk_idxs = idx_by_track.get(tk, [])
        cvv = _track_composite_block(
            tk_idxs, final_vv, prof_vv, height, width,
            transform, target_crs, y_slice, x_slice,
            composite_method, trim_fraction, scene_bounds=bounds_vv,
        )
        if cvv is None:
            continue
        cvh = _track_composite_block(
            tk_idxs, final_vh, prof_vh, height, width,
            transform, target_crs, y_slice, x_slice,
            composite_method, trim_fraction, scene_bounds=bounds_vh,
        )
        if cvh is None:
            cvh = np.full_like(cvv, np.nan, dtype=np.float32)
        take = ~filled & np.isfinite(cvv)
        if take.any():
            vv[take] = cvv[take]
            vh[take] = cvh[take]
            filled |= take
            any_data = True
        if filled.all():
            break
    return (vv, vh) if any_data else (None, None)

def _write_glcm_blocks(
    g: zarr.Group,
    time_index: int,
    idx_by_track: dict,
    track_order: list,
    final_vv: list,
    prof_vv: list,
    final_vh: list,
    prof_vh: list,
    height: int,
    width: int,
    transform: Affine,
    target_crs: str,
    chunk_y: int,
    chunk_x: int,
    bounds_vv: list | None,
    bounds_vh: list | None,
    composite_method: str,
    trim_fraction: float,
    texture_cfg: dict,
    glcm_band_names: list[str],
    clip_geom,
    num_threads: int,
    halo: int = GLCM_BLOCK_HALO,
) -> None:
    """Fill the GLCM band arrays for a reserved timestep, block by block.

    Two levels of spatial support cooperate here:

    * The composite window is drawn from the *burst-union* master grid
      (``transform``/``height``/``width`` come from ``_build_grid_from_bursts``),
      which is larger than the MGRS tile. This removes *tile*-boundary artifacts
      because the tile edge is interior to the support region.
    * ``halo`` (``GLCM_BLOCK_HALO``) expands each block by >= the GLCM support
      radius (``window//2 + distance``) before compositing, so the box filter and
      the co-occurrence shift see the true neighbourhood. This removes *block*-
      boundary artifacts within the tile.

    For each block, the unclipped VV/VH dB is composited on the halo-expanded
    window, GLCM is computed on that window, cropped back to the block, and only
    THEN tile-clipped (``_apply_block_clip``) and written. Clip-last is
    deliberate. The result is bit-identical to computing GLCM on the full
    unclipped dB mosaic and then clipping (the legacy path): the composite is
    per-pixel so the windowed composite equals the full-mosaic composite, and a
    halo >= the support radius makes the cropped GLCM equal the full-mosaic GLCM
    (see tests/test_glcm_halo_equivalence.py and
    tests/test_spatial_support_before_clip.py).
    """
    blocks = list(_iter_spatial_blocks(height, width, chunk_y, chunk_x))

    def _do_block(block_num: int, y_slice: slice, x_slice: slice):
        y0 = int(y_slice.start or 0); y1 = int(y_slice.stop or 0)
        x0 = int(x_slice.start or 0); x1 = int(x_slice.stop or 0)
        ey0 = max(0, y0 - halo); ey1 = min(int(height), y1 + halo)
        ex0 = max(0, x0 - halo); ex1 = min(int(width), x1 + halo)
        ys, xs = slice(ey0, ey1), slice(ex0, ex1)

        vv_lin, vh_lin = _priority_mosaic_lin_window(
            idx_by_track, track_order, final_vv, prof_vv, final_vh, prof_vh,
            height, width, transform, target_crs, ys, xs,
            composite_method, trim_fraction, bounds_vv, bounds_vh,
        )
        if vv_lin is None:
            return  # no data in this window -> GLCM stays NaN (reserved value)

        vv_db = _linear_to_db(vv_lin)
        vh_db = _linear_to_db(vh_lin)
        from s1grits.asf_array_processing import compute_glcm_texture_bands
        tex_arrays, tex_names = compute_glcm_texture_bands(vv_db, vh_db, texture_cfg)

        oy = y0 - ey0; ox = x0 - ex0
        bh = y1 - y0; bw = x1 - x0
        block_bands = {
            name: np.ascontiguousarray(arr[oy:oy + bh, ox:ox + bw])
            for name, arr in zip(tex_names, tex_arrays)
        }
        _apply_block_clip(block_bands, clip_geom, transform, y_slice, x_slice)
        _write_smonthly_block_bands(
            g, time_index, y_slice, x_slice, list(block_bands.keys()), block_bands
        )

    _run_blocks(_do_block, blocks, num_threads)

def _write_smonthly_month_zarr_blockwise_single_track(
    g: zarr.Group,
    month_str: str,
    dt_ns: np.datetime64,
    track_id: int,
    track_indices: list[int],
    final_vv: list,
    prof_vv: list,
    final_vh: list,
    prof_vh: list,
    height: int,
    width: int,
    transform: Affine,
    target_crs: str,
    chunk_y: int,
    chunk_x: int,
    band_names: list[str],
    copol_name: str,
    crosspol_name: str,
    features_ratio: bool,
    features_rvi: bool,
    ratio_name: str,
    rvi_name: str,
    composite_method: str,
    trim_fraction: float,
    tile_clip: bool,
    mgrs_tile_id: str,
    num_threads: int = 1,
    glcm_band_names: list[str] | None = None,
    texture_cfg: dict | None = None,
) -> tuple[list[int], dict[int, int]] | None:
    """Write one smonthly timestep for a single track in one blockwise pass."""
    blocks = list(_iter_spatial_blocks(height, width, chunk_y, chunk_x))
    total_blocks = len(blocks)
    logger.info(
        "Month %s blockwise: %d spatial blocks, 1 track, one-pass processing",
        month_str, total_blocks,
    )

    clip_geom = _prepare_block_clip_geom(tile_clip, mgrs_tile_id, target_crs)
    track_cov: dict[int, int] = {int(track_id): 0}
    _all_band_names = list(band_names) + list(glcm_band_names or [])

    # Per-scene master-grid footprints let each block skip the scenes that do
    # not intersect it, instead of stacking one all-NaN window per scene.
    bounds_vv = _compute_scene_dst_bounds(
        final_vv, prof_vv, transform, target_crs, height, width
    )
    bounds_vh = _compute_scene_dst_bounds(
        final_vh, prof_vh, transform, target_crs, height, width
    )

    time_index, new_key = _begin_zarr_timestep_blockwise(g, dt_ns, _all_band_names)
    logger.info("Month %s: One-pass - Writing single-track Zarr...", month_str)

    def _do_block(block_num, y_slice, x_slice):
        if block_num % 4 == 0 or block_num == total_blocks:
            logger.debug(
                "  One-pass: Block %d/%d writing to Zarr",
                block_num, total_blocks,
            )

        cvv, cnt = _track_composite_block(
            track_indices, final_vv, prof_vv, height, width,
            transform, target_crs, y_slice, x_slice,
            composite_method, trim_fraction,
            scene_bounds=bounds_vv,
            with_count=True,
        )
        if cvv is None:
            return 0

        cvh = _track_composite_block(
            track_indices, final_vh, prof_vh, height, width,
            transform, target_crs, y_slice, x_slice,
            composite_method, trim_fraction,
            scene_bounds=bounds_vh,
        )
        if cvh is None:
            cvh = np.full_like(cvv, np.nan, dtype=np.float32)

        block_bands = _make_smonthly_block_bands(
            cvv, cvh,
            copol_name, crosspol_name,
            features_ratio, features_rvi,
            ratio_name, rvi_name,
        )
        if N_OBS_BAND in band_names:
            block_bands[N_OBS_BAND] = cnt
        _apply_block_clip(block_bands, clip_geom, transform, y_slice, x_slice)
        _write_smonthly_block_bands(
            g, time_index, y_slice, x_slice, band_names, block_bands
        )
        return int(np.isfinite(cvv).sum())

    try:
        coverages = _run_blocks(_do_block, blocks, num_threads)
        track_cov[int(track_id)] += sum(coverages)

        if track_cov[int(track_id)] <= 0:
            _rollback_zarr_timestep_blockwise(g, time_index, _all_band_names)
            logger.warning("Month %s: no valid acquisitions, skipping", month_str)
            return None

        if glcm_band_names:
            _write_glcm_blocks(
                g, time_index, {int(track_id): list(track_indices)},
                [int(track_id)], final_vv, prof_vv, final_vh, prof_vh,
                height, width, transform, target_crs, chunk_y, chunk_x,
                bounds_vv, bounds_vh, composite_method, trim_fraction,
                texture_cfg, glcm_band_names, clip_geom, num_threads,
            )

        _finalize_zarr_timestep_blockwise(g, time_index, new_key)
        logger.info(
            "Month %s: One-pass complete. Zarr timestep written successfully.",
            month_str,
        )
    except Exception:
        _rollback_zarr_timestep_blockwise(g, time_index, _all_band_names)
        raise

    return [int(track_id)], track_cov

def _write_smonthly_month_zarr_blockwise(
    g: zarr.Group,
    month_str: str,
    dt_ns: np.datetime64,
    idx_by_track: dict[int, list[int]],
    final_vv: list,
    prof_vv: list,
    final_vh: list,
    prof_vh: list,
    height: int,
    width: int,
    transform: Affine,
    target_crs: str,
    chunk_y: int,
    chunk_x: int,
    band_names: list[str],
    copol_name: str,
    crosspol_name: str,
    features_ratio: bool,
    features_rvi: bool,
    ratio_name: str,
    rvi_name: str,
    composite_method: str,
    trim_fraction: float,
    tile_clip: bool,
    mgrs_tile_id: str,
    num_threads: int = 1,
    glcm_band_names: list[str] | None = None,
    texture_cfg: dict | None = None,
) -> tuple[list[int], dict[int, int]] | None:
    """Write one smonthly timestep block-by-block.

    The first pass computes full-tile track coverage by summing each block.
    The second pass writes the monthly arrays using that one global track order.
    This preserves the existing priority-mosaic semantics while avoiding
    full-tile per-track composites in memory.
    """
    track_items = [
        (int(tk), list(tk_idxs))
        for tk, tk_idxs in idx_by_track.items()
        if tk_idxs
    ]
    if not track_items:
        logger.warning("Month %s: no valid acquisitions, skipping", month_str)
        return None

    if len(track_items) == 1:
        return _write_smonthly_month_zarr_blockwise_single_track(
            g=g,
            month_str=month_str,
            dt_ns=dt_ns,
            track_id=track_items[0][0],
            track_indices=track_items[0][1],
            final_vv=final_vv,
            prof_vv=prof_vv,
            final_vh=final_vh,
            prof_vh=prof_vh,
            height=height,
            width=width,
            transform=transform,
            target_crs=target_crs,
            chunk_y=chunk_y,
            chunk_x=chunk_x,
            band_names=band_names,
            copol_name=copol_name,
            crosspol_name=crosspol_name,
            features_ratio=features_ratio,
            features_rvi=features_rvi,
            ratio_name=ratio_name,
            rvi_name=rvi_name,
            composite_method=composite_method,
            trim_fraction=trim_fraction,
            tile_clip=tile_clip,
            mgrs_tile_id=mgrs_tile_id,
            num_threads=num_threads,
            glcm_band_names=glcm_band_names,
            texture_cfg=texture_cfg,
        )

    # Calculate total blocks for progress reporting
    blocks = list(_iter_spatial_blocks(height, width, chunk_y, chunk_x))
    total_blocks = len(blocks)
    n_tracks = len(track_items)

    logger.info(
        "Month %s blockwise: %d spatial blocks, %d tracks, 2-pass processing",
        month_str, total_blocks, n_tracks,
    )

    idx_by_track = {tk: tk_idxs for tk, tk_idxs in track_items}
    track_cov: dict[int, int] = {int(tk): 0 for tk in idx_by_track}
    track_seen: set[int] = set()

    # Per-scene master-grid footprints let each block skip the scenes that do
    # not intersect it, instead of stacking one all-NaN window per scene.
    bounds_vv = _compute_scene_dst_bounds(
        final_vv, prof_vv, transform, target_crs, height, width
    )
    bounds_vh = _compute_scene_dst_bounds(
        final_vh, prof_vh, transform, target_crs, height, width
    )

    # Pass 1: Compute track coverage.  Coverage only needs the count of finite
    # composite pixels, and that finite mask is just the union of the aligned
    # scenes' finite masks — no composites (and no VH work) are required here.
    # Each block returns its own per-track counts (rather than mutating the
    # shared dict/set directly) so the pass is safe to run on worker threads.
    logger.info("Month %s: Pass 1/2 - Computing track coverage...", month_str)

    def _pass1_block(block_num, y_slice, x_slice):
        if block_num % 4 == 0 or block_num == total_blocks:
            logger.debug(
                "  Pass 1: Block %d/%d [y=%d:%d, x=%d:%d]",
                block_num, total_blocks,
                y_slice.start or 0, y_slice.stop or 0,
                x_slice.start or 0, x_slice.stop or 0,
            )
        block_cov: dict[int, int] = {}
        for tk, tk_idxs in idx_by_track.items():
            vmask = _track_valid_mask_block(
                tk_idxs, final_vv, prof_vv, height, width,
                transform, target_crs, y_slice, x_slice,
                scene_bounds=bounds_vv,
            )
            if vmask is None:
                continue
            block_cov[int(tk)] = int(vmask.sum())
        return block_cov

    for block_cov in _run_blocks(_pass1_block, blocks, num_threads):
        for tk, px in block_cov.items():
            track_seen.add(tk)
            track_cov[tk] += px

    if not track_seen:
        logger.warning("Month %s: no valid acquisitions, skipping", month_str)
        return None

    track_order = sorted(
        track_seen, key=lambda t: (track_cov.get(t, 0), -t), reverse=True
    )
    logger.info(
        "Month %s: Pass 1/2 complete. Priority order (by VV coverage): %s",
        month_str,
        ", ".join(f"TK{tk}={track_cov.get(tk, 0)}px" for tk in track_order),
    )

    clip_geom = _prepare_block_clip_geom(tile_clip, mgrs_tile_id, target_crs)
    _all_band_names = list(band_names) + list(glcm_band_names or [])

    time_index, new_key = _begin_zarr_timestep_blockwise(g, dt_ns, _all_band_names)
    logger.info("Month %s: Pass 2/2 - Writing to Zarr...", month_str)

    def _pass2_block(block_num, y_slice, x_slice):
        if block_num % 4 == 0 or block_num == total_blocks:
            logger.debug(
                "  Pass 2: Block %d/%d writing to Zarr",
                block_num, total_blocks,
            )
        bh = int((y_slice.stop or 0) - (y_slice.start or 0))
        bw = int((x_slice.stop or 0) - (x_slice.start or 0))
        composite_vv_lin = np.full((bh, bw), np.nan, dtype=np.float32)
        composite_vh_lin = np.full((bh, bw), np.nan, dtype=np.float32)
        # n_obs mirrors the priority fill: each pixel records the observation
        # count of the track that supplied its composite value (0 = no data).
        composite_nobs = np.zeros((bh, bw), dtype=np.float32)
        filled = np.zeros((bh, bw), dtype=bool)

        for tk in track_order:
            tk_idxs = idx_by_track.get(tk, [])
            cvv, cnt = _track_composite_block(
                tk_idxs, final_vv, prof_vv, height, width,
                transform, target_crs, y_slice, x_slice,
                composite_method, trim_fraction,
                scene_bounds=bounds_vv,
                with_count=True,
            )
            if cvv is None:
                continue
            cvh = _track_composite_block(
                tk_idxs, final_vh, prof_vh, height, width,
                transform, target_crs, y_slice, x_slice,
                composite_method, trim_fraction,
                scene_bounds=bounds_vh,
            )
            if cvh is None:
                cvh = np.full_like(cvv, np.nan, dtype=np.float32)

            take = ~filled & np.isfinite(cvv)
            if take.any():
                composite_vv_lin[take] = cvv[take]
                composite_vh_lin[take] = cvh[take]
                composite_nobs[take] = cnt[take]
                filled |= take
            del cvv, cvh, cnt
            if filled.all():
                break

        block_bands = _make_smonthly_block_bands(
            composite_vv_lin, composite_vh_lin,
            copol_name, crosspol_name,
            features_ratio, features_rvi,
            ratio_name, rvi_name,
        )
        if N_OBS_BAND in band_names:
            block_bands[N_OBS_BAND] = composite_nobs
        _apply_block_clip(block_bands, clip_geom, transform, y_slice, x_slice)
        _write_smonthly_block_bands(
            g, time_index, y_slice, x_slice, band_names, block_bands
        )

    try:
        _run_blocks(_pass2_block, blocks, num_threads)
        if glcm_band_names:
            _write_glcm_blocks(
                g, time_index, idx_by_track, track_order,
                final_vv, prof_vv, final_vh, prof_vh,
                height, width, transform, target_crs, chunk_y, chunk_x,
                bounds_vv, bounds_vh, composite_method, trim_fraction,
                texture_cfg, glcm_band_names, clip_geom, num_threads,
            )
        _finalize_zarr_timestep_blockwise(g, time_index, new_key)
        logger.info(
            "Month %s: Pass 2/2 complete. Zarr timestep written successfully.",
            month_str,
        )
    except Exception:
        _rollback_zarr_timestep_blockwise(g, time_index, _all_band_names)
        raise

    return track_order, track_cov

def _generate_cog_preview_from_zarr(
    zarr_path: Path,
    month_str: str,
    tile_dir: Path,
    direction_label: str,
    mgrs_tile_id: str,
    track_token: str,
    n_bursts_track: int,
    target_crs: str,
    tile_clip: bool,
    generate_cog: bool,
    generate_preview: bool,
    cog_block: int,
    band_names: list[str],
    product_label: str,
    skip_if_exists: bool = False,
) -> tuple[str | None, str | None]:
    """Generate COG and Preview from already-written Zarr data.

    This decouples Zarr writing (blockwise, memory-efficient) from
    COG/Preview export (one-time read-back from Zarr).

    ``skip_if_exists`` (used by the resume/backfill path): when True, an asset
    that is already on disk is left untouched and its path returned, and only
    genuinely missing assets are regenerated. This makes the exporter
    idempotent so a resume run can fill in COG/preview for a month whose Zarr
    already exists, without recomputing anything already present.

    Parameters
    ----------
    zarr_path : Path
        Path to the Zarr store containing the monthly data
    month_str : str
        Month identifier (YYYY-MM)
    tile_dir : Path
        Tile output directory
    direction_label : str
        Flight direction (ASCENDING/DESCENDING)
    mgrs_tile_id : str
        MGRS tile identifier
    track_token : str
        Track token for file naming
    n_bursts_track : int
        Number of bursts for this track. Time-varying provenance only; it is
        NOT part of the asset name (assets key on the track alone).
    target_crs : str
        Target CRS (e.g., EPSG:32617)
    tile_clip : bool
        Whether to apply spatial crop to MGRS tile bounds
    generate_cog : bool
        Whether to generate COG output
    generate_preview : bool
        Whether to generate PNG preview
    cog_block : int
        COG internal tile block size
    band_names : list[str]
        List of band names to export
    product_label : str
        Product label for directory naming

    Returns
    -------
    tuple[str | None, str | None]
        (cog_relpath, preview_relpath) relative to tile_dir
    """
    if not generate_cog and not generate_preview:
        return None, None

    # n_obs is a Zarr-only analysis band (uint8 count): keep it out of the
    # float32 COG/preview exports so their band sets stay radiometric.
    band_names = [b for b in band_names if b != N_OBS_BAND]

    # Deterministic asset paths (naming depends only on the args, not on Zarr
    # contents), computed once so the generate blocks and the skip-if-exists
    # check below cannot drift apart.
    _cog_dir = tile_dir / product_label / 'cog'
    _png_dir = tile_dir / product_label / 'preview'
    # Asset names key on the track only, matching the store identity (see
    # _write_smonthly_one_track). n_bursts is time-varying provenance, not part
    # of the filename, so a single track's assets never fragment across batches.
    _asset_stem = (
        f"s1grits_smonthly_{mgrs_tile_id}_{direction_label}_"
        f"TK{track_token}_{month_str}"
    )
    cog_path = _cog_dir / f"{_asset_stem}.tif"
    png_path = _png_dir / f"{_asset_stem}.png"

    # Backfill fast path: skip assets already on disk; regenerate only missing
    # ones. If nothing is missing, return existing relpaths without reading Zarr.
    if skip_if_exists:
        _cog_have = generate_cog and cog_path.exists()
        _png_have = generate_preview and png_path.exists()
        generate_cog = generate_cog and not cog_path.exists()
        generate_preview = generate_preview and not png_path.exists()
        if not generate_cog and not generate_preview:
            return (
                str(cog_path.relative_to(tile_dir)) if _cog_have else None,
                str(png_path.relative_to(tile_dir)) if _png_have else None,
            )

    logger.info(
        "Generating COG/Preview from Zarr for month %s (cog=%s, preview=%s)",
        month_str, generate_cog, generate_preview,
    )

    try:
        g = zarr.open_group(str(zarr_path), mode='r', zarr_format=3)
    except Exception as e:
        logger.error("Failed to open Zarr store %s: %s", zarr_path, e)
        return None, None

    # Find the time index for this month
    try:
        times = pd.to_datetime(g['time'][:])
        month_mask = times.strftime('%Y-%m') == month_str
        if not month_mask.any():
            logger.warning(
                "Month %s not found in Zarr store %s", month_str, zarr_path
            )
            return None, None

        time_idx = np.where(month_mask)[0][0]
    except Exception as e:
        logger.error("Failed to locate month %s in Zarr: %s", month_str, e)
        return None, None

    # Read grid metadata from Zarr
    transform = Affine(*g.attrs['transform'][:6])
    height = int(g.attrs['height'])
    width = int(g.attrs['width'])

    # Read all bands for this time step from Zarr (full grid)
    band_arrays = {}
    try:
        for band_name in band_names:
            if band_name not in g:
                logger.warning(
                    "Band '%s' not found in Zarr, skipping", band_name
                )
                continue
            band_arrays[band_name] = g[band_name][time_idx, :, :].astype(
                np.float32, copy=True
            )
    except Exception as e:
        logger.error("Failed to read bands from Zarr: %s", e)
        return None, None

    if not band_arrays:
        logger.error("No bands read from Zarr for month %s", month_str)
        return None, None

    # Spatial crop to MGRS tile bounds (optional)
    cog_transform = transform
    cog_height, cog_width = height, width

    if tile_clip:
        try:
            from s1grits.asf_output_writing import (
                _get_mgrs_tile_geometry_wkt,
                _clip_arrays_to_wkt_4326,
            )
            mgrs_wkt = _get_mgrs_tile_geometry_wkt(mgrs_tile_id)
            all_arrays = [band_arrays[bn] for bn in band_names if bn in band_arrays]

            if all_arrays:
                clipped, cog_transform = _clip_arrays_to_wkt_4326(
                    all_arrays, mgrs_wkt, target_crs, transform, height, width
                )
                for i, bn in enumerate([bn for bn in band_names if bn in band_arrays]):
                    band_arrays[bn] = clipped[i]
                cog_height, cog_width = clipped[0].shape
                logger.debug(
                    "Spatial crop: %dx%d -> %dx%d",
                    width, height, cog_width, cog_height,
                )
        except Exception as e:
            logger.warning(
                "tile_clip spatial crop failed for %s %s: %s. "
                "Using full grid.",
                mgrs_tile_id, month_str, e
            )

    # Seed with any assets already on disk (skip_if_exists path) so the return
    # value reflects on-disk truth, not only what THIS call generated. The
    # generate blocks below overwrite these when they (re)write an asset.
    cog_relpath = (
        str(cog_path.relative_to(tile_dir))
        if skip_if_exists and not generate_cog and cog_path.exists() else None
    )
    preview_relpath = (
        str(png_path.relative_to(tile_dir))
        if skip_if_exists and not generate_preview and png_path.exists() else None
    )

    # Generate COG
    if generate_cog:
        try:
            _cog_dir.mkdir(parents=True, exist_ok=True)

            band_list = [
                (bn, band_arrays[bn]) for bn in band_names if bn in band_arrays
            ]
            prof = {
                'driver': 'GTiff',
                'dtype': 'float32',
                'nodata': float('nan'),
                'width': cog_width,
                'height': cog_height,
                'count': len(band_list),
                'crs': target_crs,
                'transform': cog_transform,
                'compress': 'deflate',
                'tiled': True,
                'blockxsize': cog_block,
                'blockysize': cog_block,
            }
            _write_multiband_cog(cog_path, band_list, prof)
            cog_relpath = str(cog_path.relative_to(tile_dir))
            logger.info("COG written: %s", cog_relpath)
        except Exception as e:
            logger.error("Failed to generate COG for month %s: %s", month_str, e)

    # Generate Preview PNG
    if generate_preview:
        try:
            _png_dir.mkdir(parents=True, exist_ok=True)

            # Ratio calculation: VH/VV in linear power domain
            # band_arrays contains dB values, convert back to linear for ratio
            if 'VV_dB' in band_arrays and 'VH_dB' in band_arrays:
                vv_db = band_arrays['VV_dB']
                vh_db = band_arrays['VH_dB']

                # Convert dB to linear: linear = 10^(dB/10)
                vv_lin = np.power(10.0, vv_db / 10.0)
                vh_lin = np.power(10.0, vh_db / 10.0)

                valid_mask = (
                    np.isfinite(vv_lin) & np.isfinite(vh_lin) & (vv_lin > 0)
                )
                ratio_arr = np.full_like(vv_db, np.nan, dtype=np.float32)
                ratio_arr[valid_mask] = (
                    vh_lin[valid_mask] / vv_lin[valid_mask]
                ).astype(np.float32)

                _generate_preview_png(
                    vv_db=vv_db,
                    vh_db=vh_db,
                    ratio=ratio_arr,
                    src_transform=cog_transform,
                    src_crs=target_crs,
                    output_path=str(png_path),
                )
                preview_relpath = str(png_path.relative_to(tile_dir))
                logger.info("Preview written: %s", preview_relpath)
            else:
                logger.warning(
                    "Cannot generate preview: VV_dB or VH_dB not found in bands"
                )
        except Exception as e:
            logger.error(
                "Failed to generate preview for month %s: %s", month_str, e
            )

    return cog_relpath, preview_relpath

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
    valid_clean_indices: set[int] | None = None,
    spatial_filter_legacy: bool = False,
    rebuild_on_mismatch: bool = False,
) -> list[dict]:
    """
    Compute monthly composites for ONE acquisition-group track and write
    smonthly/zarr/s1grits_smonthly_{TILE}_{DIR}_TK{tok}.zarr (plus optional
    cog/preview). Writes one STAC Item per month.

    The caller passes ``df_batch`` already filtered to a single ``track_token``
    (acq group) and sets ``restrict_to_group=True``; the month loop then keeps
    only the acquisitions belonging to that group, so each track_token gets its
    own per-track product files (mirroring the scenes/static per-track layout).
    ``track_token`` drives the file naming; ``n_bursts_track`` is recorded as
    per-month catalog provenance only (never embedded in the store name). When
    ``restrict_to_group`` is False all acquisitions are used (single-product
    degrade, e.g. when no track metadata is available).
    The master grid is passed in from the caller and shared with the scenes
    writer to ensure both products use the identical grid.

    Returns catalog records with product_type='smonthly'.
    """
    # Store identity keys on the track ONLY. n_bursts is time-varying: bursts
    # drop in/out of a track's acquisitions between batches, so embedding it in
    # the store name splits ONE track across multiple fragmented .zarr stores
    # (e.g. _TK18_N09 vs _TK18_N10), each holding a disjoint slice of the time
    # series instead of appending to a single store. n_bursts is retained as
    # provenance on each per-month catalog record (see _add_smonthly_record).
    _tk_suffix = f"_TK{track_token}"
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
    # Core per-pixel bands (written by the main blockwise pass) vs GLCM texture
    # bands (filled by a dedicated blockwise halo pass). _band_names remains the
    # FULL advertised list (core + GLCM) for Zarr creation, product metadata and
    # COG export; _core_band_names / _glcm_band_names drive the two-pass write.
    _core_band_names = [copol_name, crosspol_name]
    if features_ratio:
        _core_band_names.append(ratio_name)
    if features_rvi:
        _core_band_names.append(rvi_name)
    _band_names = list(_core_band_names)
    _glcm_band_names: list[str] = []
    _blockwise_tex_cfg = None
    if features_glcm:
        from s1grits.asf_array_processing import _get_texture_band_names
        _blockwise_tex_cfg = _smonthly_texture_cfg(copol_name, crosspol_name)
        _glcm_band_names = _get_texture_band_names(_blockwise_tex_cfg)
        _band_names.extend(_glcm_band_names)
    # Per-pixel valid-observation count (uint8), always written alongside the
    # composite so downstream time-series work can confidence-weight each
    # month. Kept out of _core_band_names: the writers handle it explicitly
    # (it is a count, not a composited radiometric band).
    _band_names.append(N_OBS_BAND)

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
        rebuild_on_mismatch=rebuild_on_mismatch,
    )
    g.attrs['product_type'] = 'smonthly'
    g.attrs['time_varying'] = True
    g.attrs['array_dims'] = ['time', 'y', 'x']
    # Write variant metadata to Zarr root attrs for resync self-sufficiency
    g.attrs['processing_signature'] = _smonthly_sig
    g.attrs['product_variant'] = _smonthly_variant
    g.attrs['processing_variant_json'] = json.dumps(_smonthly_variant_vals)
    g.attrs['product_label'] = product_label
    # n_bursts is no longer in the store name (it is time-varying and would
    # fragment the track); record it as store-level provenance so a filesystem
    # rescan can still recover a burst count for the track.
    if n_bursts_track:
        g.attrs['n_bursts'] = int(n_bursts_track)

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
    # Spatial blocks are chunk-aligned, so concurrent block writes touch
    # disjoint Zarr chunks; the per-block work (bottleneck nanmedian, NumPy
    # copies, GDAL warps) releases the GIL. Defaults to 1 (serial, unchanged
    # behaviour) since this adds CPU contention across the tile-level worker
    # pool unless the operator opts in via monthly.blockwise_threads. The main
    # workflow resolves "auto" to a concrete count before dispatch; this
    # defensive resolve also handles direct/test callers passing "auto".
    blockwise_threads = (
        _resolve_blockwise_threads(monthly_cfg.get('blockwise_threads', 1))
        if monthly_cfg else 1
    )

    # Blockwise path is safe for per-pixel compositing. Spatial-neighborhood
    # operations (despeckle, GLCM/texture, convolution, morphology) need halo or
    # full arrays to avoid block-edge artifacts, so they use the legacy path.
    use_blockwise = not spatial_filter_legacy

    if not use_blockwise:
        logger.info(
            "smonthly blockwise Zarr path disabled for %s: spatial_filter_legacy=%s "
            "(spatial_despeckle=%s, features_glcm=%s). Using legacy full-array path.",
            zarr_path, spatial_filter_legacy, processing_level != "ARDC", features_glcm,
        )
    else:
        logger.info(
            "smonthly blockwise Zarr path enabled for %s. "
            "COG/Preview will be generated from Zarr if requested.",
            zarr_path,
        )
        # Warp cross-grid scenes (other UTM zone / misaligned grid) onto the
        # master grid once, so the block loop never calls GDAL reproject.
        # Local rebinding only: the caller's lists stay untouched for the
        # per-scene writers.
        final_vv, prof_vv = _prealign_scenes_to_master_grid(
            final_vv, prof_vv, transform, target_crs, height, width
        )
        final_vh, prof_vh = _prealign_scenes_to_master_grid(
            final_vh, prof_vh, transform, target_crs, height, width
        )
    # Helper: unified monthly compositing
    def _monthly_composite(stack, method):
        arr = np.stack(stack, axis=0)
        method_norm = str(method or 'median').lower()
        if method_norm in {'median', 'nanmedian'}:
            # bottleneck partitions the fresh np.stack copy in place; safe.
            if _bn is not None:
                return _bn.nanmedian(arr, axis=0)
            return np.nanmedian(arr, axis=0)
        elif method_norm in {'min', 'nanmin'}:
            return np.nanmin(arr, axis=0)
        elif method_norm in {'mean', 'nanmean'}:
            return np.nanmean(arr, axis=0)
        elif method_norm == 'trimmed_mean':
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

    def _add_smonthly_record(
        month_str: str,
        rep_dt: pd.Timestamp,
        burst_indices: list[int],
        primary_track: int,
        track_coverage: list[dict],
        cog_relpath: str | None,
        preview_relpath: str | None,
    ) -> None:
        # Per-track identifiers (mirror the scenes per-track layout)
        geom_group_id = f"{mgrs_tile_id}_{direction_label}{_tk_suffix}"
        item_id = f"{geom_group_id}_{month_str}"
        rec_track = _month_track.get(month_str)

        _ws_write_monthly_stac_item(
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
            track=rec_track,
            primary_track=primary_track,
            track_coverage=track_coverage,
            item_id_override=item_id,
        )

        rec = normalize_catalog_record({
            'item_id':      item_id,
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
            'geometry_group_id': geom_group_id,
            'track':         rec_track,
            'n_bursts':      n_bursts_track or None,
            'n_scenes':      len(burst_indices),
            'jpl_burst_ids': _month_burst_ids.get(month_str),
            'opera_ids':     _month_opera_ids.get(month_str),
            'pass_id':       _month_pass.get(month_str),
            'primary_track': primary_track,
            'track_coverage_json': json.dumps(track_coverage),
            'zarr_path':     str(zarr_relpath),
            'cog_path':      cog_relpath,
            'preview_path':  preview_relpath,
            'bands':         json.dumps(_smonthly_bands),
            'processing_level': processing_level,
            'product_variant': _smonthly_variant,
            'processing_signature': _smonthly_sig,
            'processing_variant_json': json.dumps(_smonthly_variant_vals),
        })
        catalog_records.append(rec)

    for month_str, burst_indices in sorted(month_groups.items()):
        if valid_clean_indices is not None:
            burst_indices = [i for i in burst_indices if i in valid_clean_indices]
            if not burst_indices:
                logger.info(
                    "Month %s: no QC-passing acquisitions, skipping", month_str
                )
                continue

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
                # The monthly Zarr timestep already exists and is authoritative
                # — do NOT recompute the composite. But derived COG/preview
                # assets may be missing (e.g. the first run had
                # generate_cog/preview=false and they were enabled later, or an
                # export was interrupted). Backfill only the missing assets from
                # the existing Zarr (read-back, no recompute); assets already on
                # disk are left untouched. Catalog/STAC pick these up on the
                # next `catalog resync`.
                if use_blockwise and (monthly_gen_cog or monthly_gen_png):
                    _bf_cog, _bf_png = _generate_cog_preview_from_zarr(
                        zarr_path=zarr_path, month_str=month_str,
                        tile_dir=tile_dir, direction_label=direction_label,
                        mgrs_tile_id=mgrs_tile_id, track_token=track_token,
                        n_bursts_track=n_bursts_track, target_crs=target_crs,
                        tile_clip=tile_clip, generate_cog=monthly_gen_cog,
                        generate_preview=monthly_gen_png, cog_block=cog_block,
                        band_names=_band_names, product_label=product_label,
                        skip_if_exists=True,
                    )
                    if _bf_cog or _bf_png:
                        logger.info(
                            "Month %s exists; backfilled missing derived "
                            "asset(s) from Zarr (cog=%s, preview=%s)",
                            month_str, bool(_bf_cog), bool(_bf_png),
                        )
                        continue
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

        # Representative datetime: median acquisition timestamp for this month
        acq_timestamps = sorted(pd.Timestamp(clean_dates[i]) for i in burst_indices)
        rep_dt = acq_timestamps[len(acq_timestamps) // 2]
        dt_ns = np.datetime64(rep_dt.to_datetime64(), 'ns')

        if use_blockwise:
            if _overwrite_pending:
                _zarr_delete_timestep(g, month_str)
                existing_months.discard(month_str)

            blockwise_result = _write_smonthly_month_zarr_blockwise(
                g=g,
                month_str=month_str,
                dt_ns=dt_ns,
                idx_by_track=_idx_by_track,
                final_vv=final_vv,
                prof_vv=prof_vv,
                final_vh=final_vh,
                prof_vh=prof_vh,
                height=height,
                width=width,
                transform=transform,
                target_crs=target_crs,
                chunk_y=chunk_y,
                chunk_x=chunk_x,
                band_names=_core_band_names + [N_OBS_BAND],
                copol_name=copol_name,
                crosspol_name=crosspol_name,
                features_ratio=features_ratio,
                features_rvi=features_rvi,
                ratio_name=ratio_name,
                rvi_name=rvi_name,
                composite_method=composite_method,
                trim_fraction=trim_fraction,
                tile_clip=tile_clip,
                mgrs_tile_id=mgrs_tile_id,
                num_threads=blockwise_threads,
                glcm_band_names=_glcm_band_names or None,
                texture_cfg=_blockwise_tex_cfg,
            )
            if blockwise_result is None:
                continue

            _track_order, _track_cov = blockwise_result
            existing_months.add(month_str)
            _grid_px = int(height * width)
            _primary_track = int(_track_order[0])
            _track_coverage = [
                {
                    "track": int(_tk),
                    "valid_px": int(_track_cov.get(_tk, 0)),
                    "fraction": round(_track_cov.get(_tk, 0) / _grid_px, 6)
                    if _grid_px else None,
                }
                for _tk in _track_order
            ]

            # Generate COG/Preview from Zarr (if requested)
            cog_relpath, preview_relpath = None, None
            if monthly_gen_cog or monthly_gen_png:
                cog_relpath, preview_relpath = _generate_cog_preview_from_zarr(
                    zarr_path=zarr_path,
                    month_str=month_str,
                    tile_dir=tile_dir,
                    direction_label=direction_label,
                    mgrs_tile_id=mgrs_tile_id,
                    track_token=track_token,
                    n_bursts_track=n_bursts_track,
                    target_crs=target_crs,
                    tile_clip=tile_clip,
                    generate_cog=monthly_gen_cog,
                    generate_preview=monthly_gen_png,
                    cog_block=cog_block,
                    band_names=_band_names,
                    product_label=product_label,
                )

            _add_smonthly_record(
                month_str, rep_dt, burst_indices,
                _primary_track, _track_coverage,
                cog_relpath=cog_relpath,
                preview_relpath=preview_relpath,
            )
            continue

        def _track_composite(idxs, final_arr, prof_arr, with_count=False):
            _stack = []
            for _i in idxs:
                _arr = _ws_mosaic_align(
                    [_i], final_arr, prof_arr, height, width,
                    transform, target_crs,
                )
                if _arr is not None:
                    _stack.append(_arr)
            if not _stack:
                return (None, None) if with_count else None
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', 'All-NaN slice')
                warnings.filterwarnings('ignore', 'Mean of empty slice')
                _comp = _monthly_composite(_stack, composite_method).astype(np.float32)
            if not with_count:
                return _comp
            # Per-pixel finite-observation count from the already-aligned
            # stack (no extra warps); feeds the uint8 n_obs band.
            _cnt = np.zeros(_comp.shape, dtype=np.uint8)
            for _arr in _stack:
                _cnt += np.isfinite(_arr)
            return _comp, _cnt

        _per_track_vv: dict[int, np.ndarray] = {}
        _per_track_vh: dict[int, np.ndarray] = {}
        _per_track_cnt: dict[int, np.ndarray] = {}
        for _tk, _tk_idxs in _idx_by_track.items():
            _cvv, _ccnt = _track_composite(_tk_idxs, final_vv, prof_vv, with_count=True)
            if _cvv is None:
                continue
            _cvh = _track_composite(_tk_idxs, final_vh, prof_vh)
            if _cvh is None:
                continue
            _per_track_vv[_tk] = _cvv
            _per_track_vh[_tk] = _cvh
            _per_track_cnt[_tk] = _ccnt

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
            _m_nobs = _per_track_cnt[_track_order[0]].copy()
        else:
            composite_vv_lin = np.full((height, width), np.nan, dtype=np.float32)
            composite_vh_lin = np.full((height, width), np.nan, dtype=np.float32)
            _m_nobs = np.zeros((height, width), dtype=np.uint8)
            _filled = np.zeros((height, width), dtype=bool)
            for _tk in _track_order:
                # VV drives the per-pixel source choice so VV and VH stay co-sourced
                _take = ~_filled & np.isfinite(_per_track_vv[_tk])
                composite_vv_lin[_take] = _per_track_vv[_tk][_take]
                composite_vh_lin[_take] = _per_track_vh[_tk][_take]
                # n_obs records the winning track's observation depth per pixel
                _m_nobs[_take] = _per_track_cnt[_tk][_take]
                _filled |= _take
            logger.info(
                "Month %s: priority mosaic of %d tracks (VV coverage) %s",
                month_str, len(_track_order),
                ", ".join(f"TK{_tk}={_track_cov[_tk]}" for _tk in _track_order),
            )
        del _per_track_vv, _per_track_vh, _per_track_cnt
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
            _m_nobs[_m_inv_mask] = 0
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
        _m_band_arrays.append((N_OBS_BAND, _m_nobs))

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

        _add_smonthly_record(
            month_str, rep_dt, burst_indices,
            _primary_track, _track_coverage,
            cog_relpath=cog_relpath,
            preview_relpath=preview_relpath,
        )

    try:
        _empty_zarr = zarr_path.exists() and int(g["time"].shape[0]) == 0
    except Exception:
        _empty_zarr = False
    if _empty_zarr:
        del g
        gc.collect()
        import shutil
        try:
            shutil.rmtree(zarr_path)
            logger.info(
                "Removed empty smonthly Zarr store with no time steps: %s",
                zarr_path,
            )
        except OSError as exc:
            logger.warning(
                "Could not remove empty smonthly Zarr store %s: %s",
                zarr_path,
                exc,
            )

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
    valid_clean_indices: set[int] | None = None,
    spatial_filter_legacy: bool = False,
    rebuild_on_mismatch: bool = False,
) -> list[dict]:
    """
    Write per-track smonthly composites for one tile/direction batch.

    Enumerates the acquisition-group tracks (``track_token``) present in
    ``df_batch`` and writes one independent smonthly product per group (its own
    ``..._TK{tok}.zarr``/cog/preview/catalog records), mirroring the
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
            valid_clean_indices=valid_clean_indices,
            spatial_filter_legacy=spatial_filter_legacy,
            rebuild_on_mismatch=rebuild_on_mismatch,
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
