"""Per-acquisition scenes product writer (Zarr + COG + preview + records).

Extracted move-only from workflow_scenes.py (which re-exports every name
here). The writer is the bounded-memory blockwise path: per-block
mosaic/dB/Ratio/RVI/clip/Zarr writes, halo-blockwise GLCM, and COG/preview
streamed back from the store. Test seams are preserved via facade dispatch:
patching workflow_scenes._mosaic_align / ._write_scene_stac_item keeps
covering this writer.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import zarr
from rasterio.transform import Affine
import pandas as pd
import pyproj
from rasterio.features import rasterize
from rich.console import Console
from shapely import wkt as shapely_wkt
from shapely.geometry import mapping
from shapely.ops import transform as shp_transform

from s1grits.asf_output_writing import (
    _check_tile_integrity,
    _get_mgrs_tile_geometry_wkt,
)
from s1grits.canonical_catalog_schema import normalize_catalog_record
from s1grits.logger_config import get_logger
from s1grits.product_instance import (
    make_processing_signature,
    make_product_variant,
)
from s1grits.resampling import rasterio_resampling
from s1grits.scenes.blocks import (
    GLCM_BLOCK_HALO,
    _apply_block_clip,
    _begin_zarr_timestep_blockwise,
    _finalize_zarr_timestep_blockwise,
    _iter_prefetched,
    _iter_spatial_blocks,
    _linear_to_db,
    _prepare_block_clip_geom,
    _rollback_zarr_timestep_blockwise,
    _run_blocks,
    _write_smonthly_block_bands,
)
from s1grits.scenes.cog import (
    _export_scene_cog_preview_from_zarr,
)
from s1grits.scenes.mosaic import (
    _compute_scene_dst_bounds,
    _mosaic_align_scene_window,
    _prealign_scenes_to_master_grid,
    _ws_mosaic_align,
)
from s1grits.scenes.qc import (
    _interior_hole_fraction,
    _missing_interior_bursts,
)
from s1grits.scenes.stac_items import _ws_write_scene_stac_item
from s1grits.scenes.store import _init_zarr_2band
from s1grits.scenes.average import average_pair_window
from s1grits.scenes.center import burst_centers, center_pair_window

logger = get_logger(__name__)
console = Console(legacy_windows=True, no_color=False)

def _write_scene_timestep_blockwise(
    g: zarr.Group,
    dt_ns: np.datetime64,
    band_names: list[str],
    glcm_band_names: list[str],
    read_window,
    height: int,
    width: int,
    transform: Affine,
    chunk_y: int,
    chunk_x: int,
    copol_name: str,
    crosspol_name: str,
    features_ratio: bool,
    features_rvi: bool,
    ratio_name: str,
    rvi_name: str,
    texture_cfg: dict | None,
    clip_geom,
    num_threads: int = 1,
    halo: int = GLCM_BLOCK_HALO,
    read_average_window=None,
    read_center_window=None,
) -> int:
    """Write one scene acquisition into a reserved Zarr timestep, blockwise.

    ``read_window(y_slice, x_slice) -> (vv_lin, vh_lin)`` supplies the linear
    source for any window (a mosaic window when despeckle is off; slices of
    the full despeckled arrays when it is on — despeckle's global coupling
    makes that array the one unavoidable >block allocation, see
    docs/scenes_blockwise_architecture.md). Pass 1 derives and writes the
    dB/Ratio/RVI bands per chunk-aligned block; the optional GLCM pass reuses
    the smonthly halo machinery (halo >= support radius => bit-exact).
    Support-before-clip is preserved: every window is drawn from the
    burst-union grid and clipped only after derivation.

    The interior-hole QC decision is made by the caller BEFORE this reserves a
    timestep, so this only runs for acquisitions that will be kept. Any
    exception rolls the reserved slot back so the store never keeps a partial
    timestep. Returns the committed index.
    """
    _all_band_names = list(band_names) + list(glcm_band_names or [])
    blocks = list(_iter_spatial_blocks(height, width, chunk_y, chunk_x))
    time_index, new_key = _begin_zarr_timestep_blockwise(g, dt_ns, _all_band_names)

    def _pass1_block(block_num, y_slice, x_slice):
        if read_center_window is not None:
            vv_lin, vh_lin, candidates, source_index = read_center_window(y_slice, x_slice)
        elif read_average_window is None:
            vv_lin, vh_lin = read_window(y_slice, x_slice)
        else:
            vv_lin, vh_lin, contributors = read_average_window(y_slice, x_slice)
        if vv_lin is None and vh_lin is None:
            return  # nothing intersects this block; reserved slot stays NaN
        bh = int((y_slice.stop or 0) - (y_slice.start or 0))
        bw = int((x_slice.stop or 0) - (x_slice.start or 0))
        if vv_lin is None:
            vv_lin = np.full((bh, bw), np.nan, dtype=np.float32)
        if vh_lin is None:
            vh_lin = np.full((bh, bw), np.nan, dtype=np.float32)
        block_bands = {
            copol_name: _linear_to_db(vv_lin),
            crosspol_name: _linear_to_db(vh_lin),
        }
        if read_average_window is not None:
            block_bands['n_contributors'] = contributors.astype(np.float64)
        if read_center_window is not None:
            block_bands['n_candidates'] = candidates.astype(np.float64)
            block_bands['source_index'] = source_index.astype(np.float64)
        # Per-pixel band expressions (dB / Ratio / RVI): a block sees exactly
        # the same inputs as any other, so results are position-independent.
        if features_ratio:
            with np.errstate(divide="ignore", invalid="ignore"):
                _denom_r = np.where(vv_lin > 0, vv_lin, np.nan)
                block_bands[ratio_name] = (vh_lin / _denom_r).astype(np.float32)
        if features_rvi:
            with np.errstate(divide="ignore", invalid="ignore"):
                _denom_rvi = vv_lin + vh_lin
                block_bands[rvi_name] = np.where(
                    _denom_rvi > 0, 4.0 * vh_lin / _denom_rvi, np.nan
                ).astype(np.float32)
        _apply_block_clip(block_bands, clip_geom, transform, y_slice, x_slice)
        _write_smonthly_block_bands(
            g, time_index, y_slice, x_slice, band_names, block_bands
        )

    def _glcm_block(block_num, y_slice, x_slice):
        y0 = int(y_slice.start or 0); y1 = int(y_slice.stop or 0)
        x0 = int(x_slice.start or 0); x1 = int(x_slice.stop or 0)
        ey0 = max(0, y0 - halo); ey1 = min(int(height), y1 + halo)
        ex0 = max(0, x0 - halo); ex1 = min(int(width), x1 + halo)
        vv_lin, vh_lin = read_window(slice(ey0, ey1), slice(ex0, ex1))
        if vv_lin is None and vh_lin is None:
            return  # GLCM stays NaN, matching full-frame GLCM over NaN input
        if vv_lin is None:
            vv_lin = np.full((ey1 - ey0, ex1 - ex0), np.nan, dtype=np.float32)
        if vh_lin is None:
            vh_lin = np.full((ey1 - ey0, ex1 - ex0), np.nan, dtype=np.float32)
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

    try:
        _run_blocks(_pass1_block, blocks, num_threads)
        if glcm_band_names:
            _run_blocks(_glcm_block, blocks, num_threads)
        _finalize_zarr_timestep_blockwise(g, time_index, new_key)
    except Exception:
        _rollback_zarr_timestep_blockwise(g, time_index, _all_band_names)
        raise
    return time_index

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
    despeckle_pipeline: bool = True,
    despeckle_window: bool = True,
    despeckle_window_margin: int = 64,
    group_mode: str = "acq_group",  # always acq_group; kept for call-site compat
    require_complete_bursts: bool = False,
    track_footprint: dict | None = None,
    track_footprint_ids: dict | None = None,
    incomplete_policy: str = "skip",
    incomplete_sink: list | None = None,
    interior_hole_max_frac: float = 0.005,
    rebuild_on_mismatch: bool = False,
    blockwise_threads: int = 1,
    resampling_method: str = "nearest",
    intra_pass_method: str = "first_valid_pixel",
    clean_source_rows: list[int] | None = None,
) -> list[dict]:
    """
    Write per-acquisition-group scene products.

    Always operates in acq_group mode — each relative orbit track gets
    its own Zarr store, COGs, and previews. No time-floored grouping.

    Each acquisition is routed through the bounded-memory blockwise path:
    per-block mosaic/dB/Ratio/RVI/clip/Zarr writes, halo-blockwise GLCM, and
    COG/preview streamed back from the store (locked by
    tests/test_scenes_blockwise_writer.py).

    Returns catalog records with output_type='scenes'.
    """
    average = intra_pass_method == 'average'
    center = intra_pass_method == 'bursts_center'
    strategy_enabled = average or center
    if intra_pass_method not in ('first_valid_pixel', 'average', 'bursts_center'):
        raise ValueError('Unknown intra-pass mosaic method')
    if strategy_enabled:
        if do_despeckle or float(target_res) != 30 or resampling_method != 'nearest':
            raise ValueError(f'{intra_pass_method} requires unfiltered 30 m nearest-neighbor inputs')
        if (copol_name, crosspol_name) != ('VV_dB', 'VH_dB'):
            raise ValueError(f'{intra_pass_method} currently supports VV/VH only')
        if (clean_source_rows is None or '_source_row' not in df_rtc_ts
                or len(set(clean_source_rows)) != len(clean_dates)
                or df_rtc_ts['_source_row'].duplicated().any()
                or not (len(final_vv) == len(prof_vv) == len(final_vh)
                        == len(prof_vh) == len(clean_dates) == len(clean_source_rows))):
            raise ValueError(f'{intra_pass_method} requires exact paired source-row indices')
        rows_by_source = df_rtc_ts.set_index('_source_row')
        if not set(clean_source_rows) <= set(rows_by_source.index):
            raise ValueError(f'{intra_pass_method} source rows are missing from acquisition metadata')
        if rows_by_source.loc[clean_source_rows, 'opera_id'].duplicated().any():
            raise ValueError(f'{intra_pass_method} requires unique OPERA source assets')
        source_to_index = {row: i for i, row in enumerate(clean_source_rows)}
        for array, profile in zip(final_vv + final_vh, prof_vv + prof_vh):
            if (array is None or not isinstance(profile, Mapping)
                    or profile.get('crs') is None or profile.get('transform') is None):
                raise ValueError(f'{intra_pass_method} requires decoded arrays with georeferenced profiles')

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

    # ---- Build band list ----
    _band_names = [copol_name, crosspol_name]
    if features_ratio:
        _band_names.append(ratio_name)
    if features_rvi:
        _band_names.append(rvi_name)
    if strategy_enabled:
        _band_names.extend(['n_candidates', 'source_index'] if center else ['n_contributors'])
    _glcm_band_names: list[str] = []
    if features_glcm:
        from s1grits.asf_array_processing import _get_texture_band_names
        _tex_cfg = {
            "enabled": True, "inputs": [copol_name, crosspol_name],
            "metrics": ["contrast", "homogeneity", "entropy", "correlation"],
            "window_size": 5, "distance": 1, "angles": [0, 90],
            "average_angles": True, "levels": 16,
        }
        _glcm_band_names = list(_get_texture_band_names(_tex_cfg))
        _band_names.extend(_glcm_band_names)
    # Non-GLCM bands, written by blockwise pass 1 (GLCM has its own halo pass)
    _base_band_names = [b for b in _band_names if b not in set(_glcm_band_names)]

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
        "processing.target_resolution": float(target_res),
        "processing.resampling_method": str(resampling_method),
    }
    if strategy_enabled:
        _scenes_variant_vals.update({
            'contract_version': 'intra_pass_average_v1',
            'processing.intra_pass_mosaic.method': 'average',
            'processing.mosaic_domain': 'linear_power',
            'processing.mosaic_validity': 'paired_finite_positive_vv_vh',
            'processing.mosaic_weights': 'equal',
            'processing.source_order': 'opera_id_ascending',
            'processing.accumulator_dtype': 'float64',
            'processing.result_dtype': 'float32',
            'processing.transform_tolerance_pixels': 0.0,
            'processing.tile_clip': bool(tile_clip),
            'processing.operator_order': ['exact_nearest', 'paired_average', 'features_db', 'clip'],
            'processing.glcm_preset': 'scene_5x5_d1_a0_90_l16_vv-25_5_vh-32_-5' if features_glcm else None,
            'output.bands': _scenes_bands,
            'provenance.source_semantics': 'eligible_loaded_assets; per_pixel_membership_by_validity_rule',
        })
    if center:
        _scenes_variant_vals.update({
            'contract_version': 'intra_pass_bursts_center_v1',
            'processing.intra_pass_mosaic.method': 'bursts_center',
            'processing.mosaic_weights': None,
            'processing.accumulator_dtype': None,
            'processing.operator_order': ['exact_nearest', 'paired_center_selection', 'features_db', 'clip'],
            'processing.center_definition': 'full_native_paired_valid_pixel_centroid_transformed_to_target',
            'processing.center_distance': 'target_crs_euclidean_metres_float64',
            'processing.center_tie_rule': 'first_in_opera_id_order',
            'provenance.source_semantics': 'source_index_1based_into_acquisition_opera_ids; zero_is_nodata',
        })
    # Compute from full native sources before target clipping or prealignment.
    _centers = burst_centers(final_vv, prof_vv, final_vh, prof_vh, target_crs) if center else None
    _scenes_sig = make_processing_signature(_scenes_variant_vals)
    _scenes_variant = make_product_variant('scenes', _scenes_variant_vals, _scenes_bands)
    if strategy_enabled:
        product_label += f'_{intra_pass_method}_{_scenes_sig}'
    scenes_zarr_dir = tile_dir / product_label / 'zarr'
    scenes_cog_dir = tile_dir / product_label / 'cog'
    scenes_png_dir = tile_dir / product_label / 'preview'
    if not strategy_enabled:
        _check_tile_integrity(str(tile_dir), str(scenes_zarr_dir))

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

    # ---- Blockwise write path setup ----
    _bounds_vv = _bounds_vh = None
    _clip_geom_blocks = None
    _scene_tex_cfg = None
    _resampling = rasterio_resampling(resampling_method)
    if not do_despeckle:
        # One-time warp of cross-grid bursts onto the master lattice so
        # per-block reads are slice copies instead of O(bursts x blocks)
        # GDAL warps (same destination lattice => same values).
        final_vv, prof_vv = _prealign_scenes_to_master_grid(
            final_vv, prof_vv, transform, target_crs, height, width,
            resampling=_resampling,
        )
        final_vh, prof_vh = _prealign_scenes_to_master_grid(
            final_vh, prof_vh, transform, target_crs, height, width,
            resampling=_resampling,
        )
        _bounds_vv = _compute_scene_dst_bounds(
            final_vv, prof_vv, transform, target_crs, height, width
        )
        _bounds_vh = _compute_scene_dst_bounds(
            final_vh, prof_vh, transform, target_crs, height, width
        )
    _clip_geom_blocks = _prepare_block_clip_geom(
        tile_clip, mgrs_tile_id, target_crs
    )
    if features_glcm:
        # Explicit ranges equal compute_glcm_texture_bands' defaults.
        _scene_tex_cfg = {
            "enabled": True, "inputs": [copol_name, crosspol_name],
            "metrics": ["contrast", "homogeneity", "entropy", "correlation"],
            "window_size": 5, "distance": 1, "angles": [0, 90],
            "average_angles": True, "levels": 16,
            "vv_db_range": [-25, 5], "vh_db_range": [-32, -5],
        }
    logger.info(
        "[Blockwise] Scenes writer: bounded-memory per-block path "
        "(chunk %dx%d, threads=%d)", chunk_y, chunk_x, int(blockwise_threads)
    )

    # ---- Iterate over acquisition groups ----
    _acq_iter = sorted(_acq_group_to_rows.items(), key=lambda kv: min(pd.Timestamp(r['acq_dt']).tz_convert('UTC') for r in kv[1]))
    _n_acq = len(_acq_iter)

    def _prep_acquisition(_i: int, item) -> tuple:
        """Mosaic (and despeckle) one acquisition — the CPU-heavy prep.

        Runs on a background thread when the despeckle pipeline is active
        (one-slot lookahead via _iter_prefetched, mirroring the download
        prefetch), overlapping acquisition N+1's TV/NLM run with N's band
        derivation and Zarr/COG writes. The SAME function runs inline when
        the pipeline is off, so both modes execute identical code on
        identical inputs in identical order — bit-identical by construction
        (locked by tests/test_scenes_despeckle_pipeline.py).

        Despeckle is computed here even for acquisitions the interior-hole
        QC later skips (the skip decision needs the raw mosaic, which is
        also returned): a wasted filter run on the rare skipped scene, in
        exchange for hiding the dominant per-scene cost on every kept one.
        """
        _rows = item[1]
        _idx: list[int] = []
        if strategy_enabled:
            _idx = [source_to_index[int(r['_source_row'])]
                    for r in sorted(_rows, key=lambda r: str(r['opera_id']))
                    if int(r['_source_row']) in source_to_index]
        else:
            for _r in _rows:
                _r_str = pd.Timestamp(_r['acq_dt']).tz_convert('UTC').strftime('%Y%m%dT%H%M%S')
                _idx.extend(_dt_str_to_clean_idx.get(_r_str, []))
        if not do_despeckle:
            # Blockwise despeckle-off reads each destination block lazily in the
            # writer (_mosaic_align_scene_window); building the full-frame mosaic
            # here would defeat the O(block) memory bound. QC runs on the finite
            # mask accumulated during pass 1 instead of a full raw mosaic.
            return _idx, None, None, None, None
        _vv = _ws_mosaic_align(
            _idx, final_vv, prof_vv, height, width, transform, target_crs,
            resampling=_resampling,
        )
        _vh = _ws_mosaic_align(
            _idx, final_vh, prof_vh, height, width, transform, target_crs,
            resampling=_resampling,
        )
        _vv_d = _vh_d = None
        if do_despeckle and _vv is not None and _vh is not None:
            from s1grits.asf_array_processing import (
                despeckle_2d, despeckle_2d_windowed,
            )
            _tv_kw = despeckle_kwargs if despeckle_method == "tv_bregman" else None
            _nlm_kw = despeckle_kwargs if despeckle_method == "nlm" else None
            if despeckle_window:
                # Bounded-memory footprint window (Phase 1 of the blockwise
                # migration): O(valid bbox + margin) instead of O(grid);
                # output-equivalent within the footprint per the window-
                # equivalence contract.
                _vv_d = despeckle_2d_windowed(
                    _vv, method=despeckle_method, tv_kwargs=_tv_kw,
                    nlm_kwargs=_nlm_kw, margin=despeckle_window_margin)
                _vh_d = despeckle_2d_windowed(
                    _vh, method=despeckle_method, tv_kwargs=_tv_kw,
                    nlm_kwargs=_nlm_kw, margin=despeckle_window_margin)
            else:
                _vv_d = despeckle_2d(_vv, method=despeckle_method,
                                     tv_kwargs=_tv_kw, nlm_kwargs=_nlm_kw)
                _vh_d = despeckle_2d(_vh, method=despeckle_method,
                                     tv_kwargs=_tv_kw, nlm_kwargs=_nlm_kw)
        return _idx, _vv, _vh, _vv_d, _vh_d

    # Pipeline only pays when despeckle is the bottleneck; it holds ONE extra
    # acquisition's arrays resident (raw + despeckled VV/VH of item N+1).
    _use_pipeline = bool(do_despeckle and despeckle_pipeline and _n_acq > 1)
    if _use_pipeline:
        logger.info(
            "[Pipeline] Despeckle pipeline active: acquisition N+1 is "
            "mosaicked+despeckled on a background thread while N is written."
        )

    for _acq_i, _prep in _iter_prefetched(_acq_iter, _prep_acquisition, _use_pipeline):
        (_pass_id_key, _acq_grp_key), rows = _acq_iter[_acq_i - 1]
        indices, arr_vv_lin, arr_vh_lin, _vv_despeckled, _vh_despeckled = _prep
        _rep_ts = min(pd.Timestamp(r['acq_dt']).tz_convert('UTC') for r in rows)
        acq_ts = _rep_ts
        dt_str = acq_ts.strftime('%Y%m%dT%H%M%S')
        _date_label = acq_ts.strftime('%Y-%m-%d')

        # Per-group Zarr path. Store identity keys on the track ONLY: n_bursts
        # here is len(rows) for THIS acquisition, so it varies date-to-date
        # (edge truncation, ASF gaps). Embedding it in the store name would
        # split one track's time series across fragmented stores (the smonthly
        # _TK18_N09/_N10 bug); n_bursts stays per-scene catalog provenance.
        _track_tok_raw = str(rows[0]['track_token']) if rows else 'UNK'
        _track_tok = _track_tok_raw.replace('_', '-')
        n_bursts = len(rows)
        zarr_name = f"s1grits_scenes_{mgrs_tile_id}_{direction_label}_TK{_track_tok}.zarr"
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
        _item_id = f"{mgrs_tile_id}_{direction_label}_{dt_str}"
        if strategy_enabled:
            _item_id += f'_TK{_track_tok}_{_scenes_sig}'
            loaded_rows = [rows_by_source.loc[clean_source_rows[i]] for i in indices]
            opera_ids = [str(r['opera_id']) for r in loaded_rows]
            jpl_burst_ids = [str(r['jpl_burst_id']) for r in loaded_rows]

        # Interior-gap QC decision.
        # Edge truncation (bursts missing at the along-track ENDS) is normal and
        # kept. We act on a real interior gap, detected two ways (either
        # triggers):
        #   1. burst-index: a footprint burst is missing while present bursts
        #      exist both before AND after it along-track in its sub-swath. This
        #      catches full-width segment gaps and edge sub-swath gaps the raster
        #      test cannot (they connect to the swath border).
        #   2. raster fill-holes: NoData fully enclosed by valid data (catches
        #      download drops that leave an enclosed hole).
        _footprint_ids = (
            (track_footprint_ids or {}).get(_track_tok_raw) or set(jpl_burst_ids)
        )
        _interior_missing = _missing_interior_bursts(_footprint_ids, jpl_burst_ids)

        def _qc_decision(_raw_vv_finite) -> str:
            """Return 'keep' or 'skip' from a raw-VV finite mask; raise on the
            abort policy. Closes over the per-acquisition accounting locals.
            Despeckle-on passes the full raw mosaic mask up front; despeckle-off
            passes the mask accumulated during pass 1 — same logic, same
            decision."""
            if _raw_vv_finite is None or not _raw_vv_finite.any():
                logger.warning("Scene %s mosaic returned None/empty, skipping", dt_str)
                return "skip"
            _hole_frac = _interior_hole_fraction(_raw_vv_finite, _tile_mask)
            if not (_interior_missing or _hole_frac > interior_hole_max_frac):
                return "keep"
            # classify cause: missing-from-metadata = ASF; in-metadata-but-gap = NETWORK
            if _interior_missing:
                if _age_days is not None and _age_days <= 30:
                    _cause, _recover = "ASF_MISSING_RECENT", "ASF latency — re-run in a few days"
                else:
                    _cause, _recover = "ASF_MISSING_STALE", "likely permanent (ASF has no data here)"
            elif _network_missing:
                _cause, _recover = "NETWORK", "re-run should fill it"
            else:
                _cause, _recover = (
                    "RASTER_INTERIOR_NODATA",
                    "source raster or geometry gap - inspect source/footprint",
                )
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
                return "skip"
            logger.warning(_msg + " Writing with the gap (incomplete_acquisition=write).")
            console.print(
                f"[yellow]      WARNING {_date_label}: {_detail} "
                f"({_cause}) — writing[/yellow]"
            )
            return "keep"

        def _emit_scene_records(_zarr_rel, _cog_rel, _prev_rel) -> None:
            """Write the scene STAC item + catalog record."""
            _ws_write_scene_stac_item(
                mgrs_tile_id, direction_label, acq_ts, tile_dir,
                transform, width, height, target_crs,
                cog_relpath=_cog_rel,
                zarr_relpath=str(_zarr_rel),
                preview_relpath=_prev_rel,
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
                **({'item_id_override': _item_id} if strategy_enabled else {}),
            )
            # Catalog timestamps are stored tz-naive (UTC wall-time) so they stay
            # comparable with the tz-naive records written by the monthly workflow
            # into the same catalog.parquet (avoids tz-aware/tz-naive sort crashes).
            _acq_naive = acq_ts.tz_localize(None) if getattr(acq_ts, 'tz', None) is not None else acq_ts
            _rec = normalize_catalog_record({
                'item_id':      _item_id,
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
                'geometry_group_id': f"{mgrs_tile_id}_{direction_label}_TK{_track_tok}",
                'track':         track_number,
                'n_bursts':      len(indices) if strategy_enabled else n_bursts,
                'n_scenes':      None,
                'jpl_burst_ids': jpl_burst_ids,
                'opera_ids':     opera_ids,
                'pass_id':       pass_id,
                'zarr_path':     str(_zarr_rel),
                'cog_path':      _cog_rel,
                'preview_path':  _prev_rel,
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

        # ---- Bounded-memory blockwise write ----
        zarr_relpath = zarr_path_group.relative_to(tile_dir)

        def _read_pair(_ys, _xs):
            _vvw = _mosaic_align_scene_window(
                indices, final_vv, prof_vv, height, width,
                transform, target_crs, _ys, _xs, _bounds_vv,
                resampling=_resampling,
            )
            _vhw = _mosaic_align_scene_window(
                indices, final_vh, prof_vh, height, width,
                transform, target_crs, _ys, _xs, _bounds_vh,
                resampling=_resampling,
            )
            return _vvw, _vhw

        _read_average = None
        if average:
            def _read_average(_ys, _xs):
                return average_pair_window(
                    indices, final_vv, prof_vv, final_vh, prof_vh,
                    height, width, transform, target_crs, _ys, _xs,
                    _bounds_vv, _bounds_vh)

            def _read_pair(_ys, _xs):
                return _read_average(_ys, _xs)[:2]

        _read_center = None
        if center:
            _read_average = None

            def _read_center(_ys, _xs):
                return center_pair_window(
                    indices, _centers, final_vv, prof_vv, final_vh, prof_vh,
                    height, width, transform, target_crs, _ys, _xs,
                    _bounds_vv, _bounds_vh)

            def _read_pair(_ys, _xs):
                return _read_center(_ys, _xs)[:2]

        if do_despeckle:
            # Raw mosaic exists (from _prep_acquisition); the despeckled
            # full arrays are the block source (despeckle's global coupling
            # makes that the one >block allocation, per the architecture doc).
            if arr_vv_lin is None or arr_vh_lin is None:
                logger.warning("Scene %s mosaic returned None, skipping", dt_str)
                continue
            _raw_vv_finite = np.isfinite(arr_vv_lin)

            def _read_pair(_ys, _xs, _v=_vv_despeckled, _h=_vh_despeckled):
                return (
                    np.ascontiguousarray(_v[_ys, _xs]),
                    np.ascontiguousarray(_h[_ys, _xs]),
                )

        # Resume: reuse an existing timestep for COG/preview without
        # re-appending. Only OPEN the store if it already exists on disk, so a
        # QC skip never leaves an empty store behind (the store is created only
        # on the first kept acquisition).
        _g_group = None
        _time_index = None
        if zarr_path_group.exists():
            if strategy_enabled:
                existing = zarr.open_group(str(zarr_path_group), mode='r', zarr_format=3)
                if (existing.attrs.get('processing_signature') != _scenes_sig
                        or existing.attrs.get('transform') != list(transform)[:6]
                        or existing.attrs.get('crs') != str(target_crs)
                        or any(b not in existing for b in _band_names)
                        or not np.array_equal(existing['x'][:], x_coords)
                        or not np.array_equal(existing['y'][:], y_coords)):
                    raise ValueError(f'{intra_pass_method} target identity/grid mismatch; use a separate output root')
                old_sources = existing.attrs.get('acquisitions_by_time', {}).get(dt_str, {}).get('opera_ids')
                existing_times = pd.to_datetime(existing['time'][:]).strftime('%Y%m%dT%H%M%S')
                if dt_str in existing_times and old_sources != opera_ids:
                    raise ValueError(f'{intra_pass_method} acquisition sources changed; use a separate output root')
                if center and dt_str in existing_times:
                    old_centers = existing.attrs['acquisitions_by_time'][dt_str].get('centers_target_xy')
                    if old_centers != [_centers[i] for i in indices]:
                        raise ValueError('bursts_center acquisition centres changed; use a separate output root')
            _g_group = _init_zarr_2band(
                zarr_path_group, x_coords, y_coords, target_crs,
                transform, chunk_y, chunk_x,
                processing_level=f"scenes_{despeckle_method if do_despeckle else 'ARDC'}",
                band_names=_band_names,
                rebuild_on_mismatch=rebuild_on_mismatch,
            )
            _g_group.attrs['processing_signature'] = _scenes_sig
            _g_group.attrs['product_variant'] = _scenes_variant
            _g_group.attrs['processing_variant_json'] = json.dumps(_scenes_variant_vals)
            _g_group.attrs['target_resolution_m'] = float(target_res)
            _g_group.attrs['resampling_method'] = str(resampling_method)
            _g_group.attrs['product_label'] = product_label
            _g_group.attrs['geometry_group_id'] = (
                f"{mgrs_tile_id}_{direction_label}_TK{_track_tok}"
            )
            _time_keys = (
                np.asarray(pd.to_datetime(_g_group['time'][:]).strftime('%Y%m%dT%H%M%S'))
                if _g_group['time'].shape[0] > 0 else np.asarray([], dtype=object)
            )
            if dt_str in set(_time_keys.tolist()):
                _time_index = int(np.where(_time_keys == dt_str)[0][0])

        if _time_index is None:
            # Interior-hole QC BEFORE reserving a timestep.
            # Despeckle-off holds no full mosaic, so build the raw VV finite
            # mask block-by-block (O(grid bytes) boolean, per the S9 budget)
            # instead of an O(grid float) mosaic.
            if not do_despeckle:
                _raw_vv_finite = np.zeros((int(height), int(width)), dtype=bool)
                for _qy, _qx in _iter_spatial_blocks(height, width, chunk_y, chunk_x):
                    if strategy_enabled:
                        _vvw = _read_pair(_qy, _qx)[0]
                    else:
                        _vvw = _mosaic_align_scene_window(
                            indices, final_vv, prof_vv, height, width,
                            transform, target_crs, _qy, _qx, _bounds_vv,
                            resampling=_resampling,
                        )
                    if _vvw is not None:
                        _raw_vv_finite[_qy, _qx] = np.isfinite(_vvw)
            if _qc_decision(_raw_vv_finite) == "skip":
                continue

            # Kept: create the store now (first kept acquisition) if needed.
            if _g_group is None:
                _g_group = _init_zarr_2band(
                    zarr_path_group, x_coords, y_coords, target_crs,
                    transform, chunk_y, chunk_x,
                    processing_level=f"scenes_{despeckle_method if do_despeckle else 'ARDC'}",
                    band_names=_band_names,
                    rebuild_on_mismatch=rebuild_on_mismatch,
                )
                _g_group.attrs['processing_signature'] = _scenes_sig
                _g_group.attrs['product_variant'] = _scenes_variant
                _g_group.attrs['processing_variant_json'] = json.dumps(_scenes_variant_vals)
                _g_group.attrs['target_resolution_m'] = float(target_res)
                _g_group.attrs['resampling_method'] = str(resampling_method)
                _g_group.attrs['product_label'] = product_label
                _g_group.attrs['geometry_group_id'] = (
                    f"{mgrs_tile_id}_{direction_label}_TK{_track_tok}"
                )

            _dt_ns = np.datetime64(acq_ts.to_datetime64(), 'ns')
            _time_index = _write_scene_timestep_blockwise(
                _g_group, _dt_ns, _base_band_names, _glcm_band_names,
                _read_pair, int(height), int(width), transform,
                chunk_y, chunk_x, copol_name, crosspol_name,
                features_ratio, features_rvi, ratio_name, rvi_name,
                _scene_tex_cfg, _clip_geom_blocks,
                num_threads=int(blockwise_threads),
                **({'read_average_window': _read_average} if strategy_enabled else {}),
                **({'read_center_window': _read_center} if center else {}),
            )

        if strategy_enabled:
            acquisition_sources = dict(_g_group.attrs.get('acquisitions_by_time', {}))
            acquisition_sources[dt_str] = {
                'opera_ids': opera_ids, 'jpl_burst_ids': jpl_burst_ids, 'pass_id': pass_id,
            }
            if center:
                acquisition_sources[dt_str]['centers_target_xy'] = [_centers[i] for i in indices]
            _g_group.attrs['acquisitions_by_time'] = acquisition_sources
            if center:
                _g_group.attrs['numeric_processing'] = {
                    'contract_version': 'intra_pass_bursts_center_v1',
                    'mapping': {'resampling': 'nearest', 'transform_tolerance_pixels': 0.0},
                    'mosaic': {'method': 'bursts_center', 'domain': 'linear_power',
                               'validity': 'paired_finite_positive_vv_vh',
                               'center': _scenes_variant_vals['processing.center_definition'],
                               'distance': _scenes_variant_vals['processing.center_distance'],
                               'ties': 'first_in_opera_id_order'},
                    'output': {'backscatter_domain': 'dB', 'clip_after_features': bool(tile_clip)},
                }
                _g_group['n_candidates'].attrs['description'] = 'Number of jointly valid positive VV/VH candidates; not a temporal count.'
                _g_group['source_index'].attrs['description'] = 'One-based position in acquisitions_by_time[time].opera_ids; zero is NoData.'
            else:
                _g_group.attrs['numeric_processing'] = {
                    'contract_version': 'intra_pass_average_v1',
                    'mapping': {'resampling': 'nearest', 'transform_tolerance_pixels': 0.0},
                    'mosaic': {'method': 'average', 'domain': 'linear_power',
                               'validity': 'paired_finite_positive_vv_vh',
                               'source_order': 'opera_id_ascending', 'weights': 'equal',
                               'accumulator_dtype': 'float64', 'result_dtype': 'float32'},
                    'output': {'backscatter_domain': 'dB', 'count_band': 'n_contributors',
                               'clip_after_features': bool(tile_clip)},
                }
                _g_group['n_contributors'].attrs['description'] = (
                    'Number of jointly valid positive VV/VH burst pairs; equal linear-power weights. '
                    'Zero means no contribution. This is not a temporal observation count.')

        _cog_name = (
            f"s1grits_scenes_{mgrs_tile_id}_{direction_label}_"
            f"TK{_track_tok}_{dt_str}.tif"
        )
        _png_name = (
            f"s1grits_scenes_{mgrs_tile_id}_{direction_label}_"
            f"TK{_track_tok}_{dt_str}.png"
        )
        cog_relpath, preview_relpath = _export_scene_cog_preview_from_zarr(
            _g_group, _time_index, [b for b in _band_names if b not in ('n_contributors', 'n_candidates', 'source_index')], transform,
            int(height), int(width), target_crs, tile_clip, mgrs_tile_id,
            generate_cog, generate_preview,
            scenes_cog_dir / _cog_name, scenes_png_dir / _png_name,
            cog_block, chunk_y, copol_name, crosspol_name, tile_dir, dt_str,
        )
        _emit_scene_records(zarr_relpath, cog_relpath, preview_relpath)

    logger.info(
        "scenes/ written: %d scenes -> %s", len(catalog_records), scenes_zarr_dir
    )
    return catalog_records
