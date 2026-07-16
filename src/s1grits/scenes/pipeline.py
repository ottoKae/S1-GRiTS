"""Per-tile scenes pipeline: the batch loop and worker plumbing.

Extracted move-only from workflow_scenes.py (which re-exports every name
here): metadata query + QC prefilter, date batching, download (+ prefetch),
grid build/adoption, writer calls, catalog/STAC assembly for one MGRS tile,
plus the worker-process initialiser and memory-budget wrapper used by the
parallel tile pool.
"""

import gc
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from collections.abc import Mapping

import numpy as np
import pandas as pd
import pyproj
from rasterio.features import rasterize

try:  # Fast C nanmedian; NumPy's masked-array path is ~50x slower on blocks
    import bottleneck as _bn
except ImportError:  # pragma: no cover - optional accelerator
    _bn = None
from rich.console import Console
from shapely import wkt as shapely_wkt
from shapely.geometry import mapping
from shapely.ops import transform as shp_transform

from s1grits.logger_config import get_logger
from s1grits.workflow import (
    query_rtc_metadata_for_tile,
)
from s1grits.adapters import (
    adapt_enumerator_to_distmetrics,
    filter_by_flight_direction,
    validate_url_pairs,
)
from s1grits.asf_io import (
    get_band_names,
    load_and_despeckle_rtc_strict,
    load_rtc_band_strict,
    _mgrs_to_utm_epsg,
)
from s1grits.memory_manager import (
    get_memory_strategy_from_config,
    chunk_time_by_strategy,
)
from s1grits.asf_output_writing import (  # noqa: F401  (_zarr_delete_timestep + _check_tile_integrity stay on the ws facade surface)
    _build_grid_from_bursts,
    _build_grid_from_geoms,
    _check_tile_integrity,
    _get_mgrs_tile_geometry_wkt,
    _mosaic_align,
    _zarr_delete_timestep,
)

# ---------------------------------------------------------------------------
# Extracted writer machinery (move-only split). workflow_scenes remains the
# facade: every moved name is re-exported here, so existing imports and test
# monkeypatch seams (ws._write_scene_stac_item, ws._mosaic_align via the
# windowed readers' facade dispatch) keep working unchanged.
# ---------------------------------------------------------------------------
from s1grits.scenes.blocks import (  # noqa: F401
    N_OBS_BAND,
    GLCM_BLOCK_HALO,
    _apply_block_clip,
    _begin_zarr_timestep_blockwise,
    _block_transform,
    _fill_value_is_nan,
    _finalize_zarr_timestep_blockwise,
    _iter_spatial_blocks,
    _prepare_block_clip_geom,
    _rollback_zarr_timestep_blockwise,
    _run_blocks,
    _write_smonthly_block_bands,
)
from s1grits.scenes.mosaic import (  # noqa: F401
    _ws_mosaic_align,
    _as_affine,
    _bounds_intersect_block,
    _can_window_reproject,
    _compute_scene_dst_bounds,
    _crs_equal,
    _direct_copy_offsets,
    _mosaic_align_scene_window,
    _mosaic_align_window,
    _mosaic_align_window_direct_copy,
    _prealign_scenes_to_master_grid,
    _scene_dst_bounds,
)
from s1grits.scenes.cog import (  # noqa: F401
    _COG_STRIP_BUDGET_BYTES,
    _export_scene_cog_preview_from_zarr,
    _free_disk_bytes,
    _tile_clip_crop_window,
    _write_multiband_cog,
    _write_multiband_cog_streamed,
    _write_multiband_cog_windowed,
)
from s1grits.scenes.store import (  # noqa: F401
    _adopt_existing_master_grid,
    _append_zarr_timestep,
    _expand_grid_to_tile_bounds,
    _init_zarr_2band,
    _zarr_append,
)
from s1grits.scenes.qc import (  # noqa: F401
    _burst_coverage_status,
    _burst_subswath_index,
    _estimate_interior_hole_frac,
    _interior_hole_fraction,
    _missing_interior_bursts,
)
from s1grits.scenes.blocks import (  # noqa: F401
    BLOCKWISE_AUTO_MAX_THREADS,
    _iter_prefetched,
    _linear_to_db,
    _resolve_blockwise_threads,
)
from s1grits.scenes.scene_writer import (  # noqa: F401
    _write_scene_timestep_blockwise,
    _write_scenes_output,
)
from s1grits.scenes.smonthly_writer import (  # noqa: F401
    _generate_cog_preview_from_zarr,
    _group_indices_by_period,
    _make_smonthly_block_bands,
    _monthly_composite_block,
    _priority_mosaic_lin_window,
    _smonthly_texture_cfg,
    _track_composite_block,
    _track_valid_mask_block,
    _write_glcm_blocks,
    _write_monthly_output_scenes,
    _write_smonthly_month_zarr_blockwise,
    _write_smonthly_month_zarr_blockwise_single_track,
    _write_smonthly_one_track,
)
from s1grits.scenes.stac_items import (  # noqa: F401
    _build_stac_item,
    _resolve_item_bands,
    _stac_extensions,
    _write_monthly_stac_item,
    _write_scene_stac_item,
)

from s1grits.scenes.support import (  # noqa: F401
    DOWNLOAD_GLOBAL_CONNECTION_BUDGET,
    _cap_batch_strategy_for_spatial_filters,
    _clean_date_key,
    _config_flag_enabled,
    _fmt_mb,
    _get_despeckle_token,
    _load_footprint_cache,
    _phase_timer,
    _resolve_download_workers,
    _resolve_max_workers,
    _rss_mb,
    _sanitize_band_arrays,
    _sanitize_prof_arrays,
    _save_footprint_cache,
    _spatial_filters_enabled,
)

logger = get_logger(__name__)
console = Console(legacy_windows=True, no_color=False)


def _valid_clean_indices_from_scene_records(
    clean_dates: list,
    scene_records: list[dict],
) -> set[int]:
    """Map scene catalog records that passed QC back to clean_dates indices."""
    valid_keys: set[str] = set()
    for rec in scene_records:
        dt = rec.get('datetime')
        if dt is not None:
            valid_keys.add(_clean_date_key(dt))

    valid_indices: set[int] = set()
    for idx, clean_dt in enumerate(clean_dates):
        if _clean_date_key(clean_dt) in valid_keys:
            valid_indices.add(idx)
    return valid_indices

def _prefilter_metadata_incomplete_acquisitions(
    mgrs_tile_id: str,
    direction_label: str,
    df_rtc_ts: pd.DataFrame,
    require_complete_bursts: bool = False,
    track_footprint: dict | None = None,
    track_footprint_ids: dict | None = None,
    incomplete_policy: str = "skip",
    incomplete_sink: list | None = None,
    console_obj: Console | None = None,
    interior_hole_max_frac: float = 0.0,
    burst_geoms: dict | None = None,
    tile_geom=None,
) -> pd.DataFrame:
    """Drop acquisitions that fail metadata-only interior-burst QC.

    This runs before raster download in smonthly-only mode. It can only catch
    footprint/interior-burst omissions; raster interior NoData still requires
    the later VV-only mosaic QC.

    When ``burst_geoms``/``tile_geom`` are provided, the interior gap's tile
    area is ESTIMATED from burst footprints and the acquisition is dropped
    only when the estimate exceeds ``interior_hole_max_frac`` — honouring the
    same threshold the raster QC enforces. Previously any interior-missing
    burst dropped the acquisition regardless of hole size, which discarded
    entire multi-year eras of a track over a single permanently-missing burst
    covering a few percent of the tile (the 17MPV/17MPT canary finding).
    Without geometry, the legacy conservative drop is kept.
    """
    if df_rtc_ts.empty or not track_footprint_ids:
        return df_rtc_ts
    if incomplete_policy == "write" and not require_complete_bursts:
        return df_rtc_ts

    keep = pd.Series(True, index=df_rtc_ts.index)
    grouped: dict[tuple, list] = {}
    for idx, row in df_rtc_ts.iterrows():
        key = (int(row['pass_id']), int(row['acq_group_id_within_mgrs_tile']))
        grouped.setdefault(key, []).append((idx, row))

    for (_pass_id_key, _acq_grp_key), indexed_rows in grouped.items():
        rows = [r for _, r in indexed_rows]
        if not rows:
            continue

        acq_ts = min(pd.Timestamp(r['acq_dt']).tz_convert('UTC') for r in rows)
        dt_str = acq_ts.strftime('%Y%m%dT%H%M%S')
        date_label = acq_ts.strftime('%Y-%m-%d')
        track_tok_raw = str(rows[0]['track_token'])
        jpl_burst_ids = [str(r['jpl_burst_id']) for r in rows]
        n_bursts = len(rows)
        expected_full = (
            int(track_footprint.get(track_tok_raw, n_bursts))
            if track_footprint else n_bursts
        )
        footprint_ids = (track_footprint_ids or {}).get(track_tok_raw) or set(jpl_burst_ids)
        interior_missing = _missing_interior_bursts(footprint_ids, jpl_burst_ids)
        if not interior_missing:
            continue

        # Threshold test: estimate the gap's tile-area share from burst
        # footprints; keep the acquisition when it is within the configured
        # tolerance (the later raster QC re-measures the real NoData).
        _est_frac, _est_basis = _estimate_interior_hole_frac(
            interior_missing, jpl_burst_ids,
            burst_geoms or {}, tile_geom, expected_full,
        )
        if _est_frac is not None and _est_frac <= interior_hole_max_frac:
            logger.info(
                "[Hole] Scene %s TK%s: %d interior burst(s) missing but "
                "estimated hole %.2f%% of tile <= threshold %.2f%% (%s); "
                "keeping for download (raster QC re-checks).",
                date_label, track_tok_raw, len(interior_missing),
                _est_frac * 100, interior_hole_max_frac * 100, _est_basis,
            )
            continue

        try:
            age_days = int((pd.Timestamp.now(tz='UTC') - acq_ts).days)
        except Exception:
            age_days = None
        if age_days is not None and age_days <= 30:
            cause, recover = "ASF_MISSING_RECENT", "ASF latency - re-run in a few days"
        else:
            cause, recover = "ASF_MISSING_STALE", "likely permanent (ASF has no data here)"

        detail = f"{len(interior_missing)} interior burst(s) missing"
        msg = (
            f"[Hole] Scene {date_label} ({dt_str}) TK{track_tok_raw}: {detail}; "
            f"metadata {n_bursts}/{expected_full} bursts ({cause}: {recover})."
        )
        rec = {
            "tile_id": mgrs_tile_id,
            "flight_direction": direction_label,
            "track_token": track_tok_raw,
            "date": date_label,
            "datetime": dt_str,
            "loaded_bursts": n_bursts,
            "metadata_bursts": n_bursts,
            "footprint_bursts": expected_full,
            "interior_missing_bursts": len(interior_missing),
            # Estimated from burst footprints at the metadata stage (no raster
            # yet); 0.0 only when no geometry was available to estimate from.
            "interior_hole_pct": round((_est_frac or 0.0) * 100, 3),
            "hole_estimate_basis": _est_basis or "none",
            "network_missing": 0,
            "asf_missing": max(0, expected_full - n_bursts),
            "age_days": age_days,
            "cause": cause,
            "recoverable": recover,
            "qc_stage": "metadata_prefilter",
        }

        if incomplete_policy == "abort" or require_complete_bursts:
            raise RuntimeError(msg + " Aborting (incomplete_acquisition=abort).")
        if incomplete_sink is not None:
            incomplete_sink.append(rec)
        logger.warning(msg + " Skipping before download.")
        if console_obj is not None:
            console_obj.print(
                f"[yellow]      SKIP {date_label}: {detail} "
                f"({cause}) - not downloaded[/yellow]"
            )
        for idx, _row in indexed_rows:
            keep.loc[idx] = False

    filtered = df_rtc_ts.loc[keep].copy().reset_index(drop=True)
    dropped = len(df_rtc_ts) - len(filtered)
    if dropped:
        logger.info(
            "Metadata-first QC filtered %d/%d rows before download",
            dropped, len(df_rtc_ts),
        )
    return filtered

def _prepare_valid_clean_indices_for_monthly(
    mgrs_tile_id: str,
    direction_label: str,
    final_vv: list,
    prof_vv: list,
    final_vh: list | None,
    prof_vh: list | None,
    clean_dates: list,
    df_rtc_ts: pd.DataFrame,
    target_crs: str,
    transform,
    width: int,
    height: int,
    tile_clip: bool = True,
    require_complete_bursts: bool = False,
    track_footprint: dict | None = None,
    track_footprint_ids: dict | None = None,
    incomplete_policy: str = "skip",
    incomplete_sink: list | None = None,
    interior_hole_max_frac: float = 0.005,
    console_obj: Console | None = None,
) -> set[int]:
    """
    Run acquisition-level QC without writing scenes.

    This mirrors the hole/incomplete-acquisition checks in the scenes writer so
    smonthly-only mode can exclude bad acquisitions before compositing.
    """
    _acq_group_to_rows: dict[tuple, list] = {}
    for _, row in df_rtc_ts.iterrows():
        _key = (int(row['pass_id']), int(row['acq_group_id_within_mgrs_tile']))
        _acq_group_to_rows.setdefault(_key, []).append(row)

    _dt_str_to_clean_idx: dict[str, list[int]] = {}
    for _ci, _cd in enumerate(clean_dates):
        _dt_str_to_clean_idx.setdefault(_clean_date_key(_cd), []).append(_ci)

    _tile_mask = None
    if tile_clip:
        mgrs_wkt = _get_mgrs_tile_geometry_wkt(mgrs_tile_id)
        crs_ll = pyproj.CRS.from_epsg(4326)
        crs_t = pyproj.CRS.from_user_input(target_crs)
        proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
        geom_proj = shp_transform(proj, shapely_wkt.loads(mgrs_wkt))
        _tile_mask = rasterize(
            [(mapping(geom_proj), 1)],
            out_shape=(height, width),
            transform=transform,
            fill=0, dtype="uint8", all_touched=False,
        ).astype(bool)

    valid_indices: set[int] = set()
    # Per-scene master-grid footprints for windowed hole QC; the denominator
    # stays the full tile so windowed fractions match the full-tile measure.
    _scene_bounds_qc = _compute_scene_dst_bounds(
        final_vv, prof_vv, transform, target_crs, height, width
    )
    _qc_denom = (
        int(_tile_mask.sum()) if _tile_mask is not None else int(height) * int(width)
    )
    _acq_iter = sorted(
        _acq_group_to_rows.items(),
        key=lambda kv: min(pd.Timestamp(r['acq_dt']).tz_convert('UTC') for r in kv[1])
    )

    for _acq_i, ((_pass_id_key, _acq_grp_key), rows) in enumerate(_acq_iter, 1):
        acq_ts = min(pd.Timestamp(r['acq_dt']).tz_convert('UTC') for r in rows)
        dt_str = acq_ts.strftime('%Y%m%dT%H%M%S')
        _date_label = acq_ts.strftime('%Y-%m-%d')

        indices: list[int] = []
        for r in rows:
            indices.extend(_dt_str_to_clean_idx.get(_clean_date_key(r['acq_dt']), []))

        _track_tok_raw = str(rows[0]['track_token']) if rows else 'UNK'
        n_bursts = len(rows)
        _loaded = len(indices)
        _expected_full = (
            int(track_footprint.get(_track_tok_raw, n_bursts))
            if track_footprint else n_bursts
        )
        _network_missing = max(0, n_bursts - _loaded)
        _asf_missing = max(0, _expected_full - n_bursts)
        try:
            _age_days = int((pd.Timestamp.now(tz='UTC') - acq_ts).days)
        except Exception:
            _age_days = None

        jpl_burst_ids = [str(r['jpl_burst_id']) for r in rows] if rows else []
        _footprint_ids = (
            (track_footprint_ids or {}).get(_track_tok_raw) or set(jpl_burst_ids)
        )
        _interior_missing = _missing_interior_bursts(_footprint_ids, jpl_burst_ids)

        # Metadata-first QC: if the footprint says an interior burst is missing
        # and policy is skip/abort, avoid the expensive raster mosaic entirely.
        if _interior_missing:
            if _interior_missing:
                if _age_days is not None and _age_days <= 30:
                    _cause, _recover = "ASF_MISSING_RECENT", "ASF latency - re-run in a few days"
                else:
                    _cause, _recover = "ASF_MISSING_STALE", "likely permanent (ASF has no data here)"
            _hole_frac = 0.0
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
                f"[Hole] Scene {_date_label} ({dt_str}) TK{_track_tok_raw}: {_detail}; "
                f"loaded {_loaded}/{_expected_full} bursts ({_cause}: {_recover})."
            )
            if incomplete_policy == "abort" or require_complete_bursts:
                raise RuntimeError(_msg + " Aborting (incomplete_acquisition=abort).")
            if incomplete_sink is not None:
                incomplete_sink.append(_inc_rec)
            if incomplete_policy == "skip":
                logger.warning(_msg + " Skipping this acquisition.")
                if console_obj is not None:
                    console_obj.print(
                        f"[yellow]      SKIP {_date_label}: {_detail} "
                        f"({_cause}) - not used for smonthly[/yellow]"
                )
                continue
            logger.warning(_msg + " Writing with the gap (incomplete_acquisition=write).")

        # Hole QC needs only the finite mask of the acquisition mosaic, and
        # interior holes are invariant under cropping to a window containing
        # all the acquisition's valid pixels — so build the mask on the union
        # bounding window instead of mosaicking the full tile per acquisition.
        _acq_bounds = [_scene_bounds_qc[i] for i in indices]
        if indices and all(b is not None for b in _acq_bounds):
            _r0 = min(b[0] for b in _acq_bounds)
            _r1 = max(b[1] for b in _acq_bounds)
            _c0 = min(b[2] for b in _acq_bounds)
            _c1 = max(b[3] for b in _acq_bounds)
            if _r1 <= _r0 or _c1 <= _c0:
                logger.warning("Scene %s footprint outside grid, skipping", dt_str)
                continue
            _y_sl, _x_sl = slice(_r0, _r1), slice(_c0, _c1)
            _valid_mask = _track_valid_mask_block(
                indices, final_vv, prof_vv, height, width,
                transform, target_crs, _y_sl, _x_sl,
                scene_bounds=_scene_bounds_qc,
            )
            if _valid_mask is None:
                logger.warning("Scene %s VV mosaic returned None, skipping", dt_str)
                continue
            _win_tile_mask = (
                _tile_mask[_y_sl, _x_sl] if _tile_mask is not None else None
            )
            _hole_frac = _interior_hole_fraction(
                _valid_mask, _win_tile_mask, denom=_qc_denom
            )
        else:
            # Unknown footprint(s): fall back to the full-tile mosaic
            arr_vv_lin = _ws_mosaic_align(
                indices, final_vv, prof_vv, height, width, transform, target_crs
            )
            if arr_vv_lin is None:
                logger.warning("Scene %s VV mosaic returned None, skipping", dt_str)
                continue
            _hole_frac = _interior_hole_fraction(np.isfinite(arr_vv_lin), _tile_mask)
            del arr_vv_lin
        if _hole_frac > interior_hole_max_frac:
            if _network_missing:
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
                "interior_missing_bursts": 0,
                "interior_hole_pct": round(_hole_frac * 100, 3),
                "network_missing": _network_missing, "asf_missing": _asf_missing,
                "age_days": _age_days, "cause": _cause, "recoverable": _recover,
            }
            _detail = f"interior NoData {_hole_frac*100:.2f}% of tile"
            _msg = (
                f"[Hole] Scene {_date_label} ({dt_str}) TK{_track_tok_raw}: {_detail}; "
                f"loaded {_loaded}/{_expected_full} bursts ({_cause}: {_recover})."
            )
            if incomplete_policy == "abort" or require_complete_bursts:
                raise RuntimeError(_msg + " Aborting (incomplete_acquisition=abort).")
            if incomplete_sink is not None:
                incomplete_sink.append(_inc_rec)
            if incomplete_policy == "skip":
                logger.warning(_msg + " Skipping this acquisition.")
                if console_obj is not None:
                    console_obj.print(
                        f"[yellow]      SKIP {_date_label}: {_detail} "
                        f"({_cause}) - not used for smonthly[/yellow]"
                    )
                continue
            logger.warning(_msg + " Writing with the gap (incomplete_acquisition=write).")

        valid_indices.update(indices)

    return valid_indices

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

    # Opt-in cross-tile burst cache (default off => unchanged download path).
    # Configured per worker process from memory.burst_cache_dir so shared bursts
    # between adjacent tiles are downloaded once and reused from disk.
    from s1grits import burst_cache
    burst_cache.configure(config.get('memory', {}).get('burst_cache_dir'))

    # Phase 3 (opt-in, memory.batch_spill): decoded batch bursts spill to
    # per-process .npy files and come back as read-only memmaps — the batch's
    # dominant memory term becomes file-backed/reclaimable instead of
    # anonymous RSS (docs/scenes_blockwise_architecture.md, S1/Phase 3).
    from s1grits import batch_spill
    if bool(config.get('memory', {}).get('batch_spill', False)):
        _spill_root = config.get('memory', {}).get('spill_dir') or (
            Path(output_root) / '.spill'
        )
        batch_spill.configure(_spill_root)
    else:
        batch_spill.configure(None)

    # Phase 3.2 (opt-in, memory.windowed_burst_reads): when the burst cache
    # holds the GeoTIFF, bursts enter the batch as lazy window readers — no
    # whole-array decode, no .npy copy; block reads fault in only their rows.
    # Requires memory.burst_cache_dir (needs on-disk files); inert otherwise.
    from s1grits import lazy_burst
    _windowed = bool(config.get('memory', {}).get('windowed_burst_reads', False))
    if _windowed and not burst_cache.is_enabled():
        logger.warning(
            "[LazyBurst] memory.windowed_burst_reads is set but "
            "memory.burst_cache_dir is not — windowed reads need the on-disk "
            "burst cache; falling back to the eager decode path."
        )
        _windowed = False
    lazy_burst.configure(_windowed)

    _console = Console(legacy_windows=True, no_color=False, quiet=quiet)
    _console.print(f"\n[bold cyan]Tile: {mgrs_tile_id}[/bold cyan]")

    try:
        # 1. Query metadata (spinner while ASF responds; skipped when quiet so
        # parallel workers don't open competing live displays)
        _metadata_t0 = time.perf_counter()
        _metadata_rss0 = _rss_mb()
        logger.info(
            "[PHASE] metadata.query START tile=%s rss_mb=%s",
            mgrs_tile_id, _fmt_mb(_metadata_rss0),
        )
        if quiet:
            df_rtc_ts = query_rtc_metadata_for_tile(
                mgrs_tile_id, time_ranges, config
            )
        else:
            with _console.status("[dim]  [1/4] Querying ASF metadata…[/dim]", spinner="dots"):
                df_rtc_ts = query_rtc_metadata_for_tile(
                    mgrs_tile_id, time_ranges, config
                )
        _metadata_rss1 = _rss_mb()
        logger.info(
            "[PHASE] metadata.query END elapsed_s=%.2f rss_mb=%s delta_mb=%s tile=%s rows=%d",
            time.perf_counter() - _metadata_t0,
            _fmt_mb(_metadata_rss1),
            _fmt_mb(None if _metadata_rss0 is None or _metadata_rss1 is None else _metadata_rss1 - _metadata_rss0),
            mgrs_tile_id,
            len(df_rtc_ts),
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
        _processing_for_strategy = config.get('processing') or {}
        _spatial_filters_for_strategy = _spatial_filters_enabled(
            _processing_for_strategy
        )
        # The blockwise smonthly writer is active when no spatial-neighbourhood
        # filter forces the legacy full-tile path; its peak memory scales with
        # the burst footprint, not a full-tile stack, so use the blockwise-aware
        # estimate for the 'auto' batch strategy. Passing the real acquisition
        # dates makes 'auto' demand-aware: the strategy is chosen from the PEAK
        # batch each candidate strategy would hold, not the total scene count.
        # Download prefetch keeps a second batch resident, so it doubles the
        # demand the estimator must fit.
        _blockwise_active = not _spatial_filters_for_strategy
        _prefetch_cfg = bool(
            (config.get('memory', {}) or {}).get('download_prefetch', False)
        )
        batch_strategy = get_memory_strategy_from_config(
            config, n_scenes, blockwise=_blockwise_active,
            acq_dates=df_rtc_ts['acq_dt'],
            resident_batches=2 if _prefetch_cfg else 1,
        )
        batch_strategy = _cap_batch_strategy_for_spatial_filters(
            batch_strategy, _spatial_filters_for_strategy
        )
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

        # Store-level + month-level output policies (v3 keys, v2 accepted
        # with deprecation). Resolved silently here — run_scenes_workflow
        # already validated and logged deprecations once.
        from s1grits.config_schema import resolve_output_policies
        _out_pol = resolve_output_policies(config)
        overwrite = _out_pol.rebuild_on_mismatch

        # Incremental update: when Zarr already exists and overwrite=false,
        # we still process the tile normally — the existing store's locked grid
        # is adopted as this run's master grid (see _adopt_existing_master_grid),
        # the store is opened in r+ mode, and existing time-step dedup prevents
        # re-writing old data. Only new acquisitions/months are appended;
        # on_time_conflict decides skip-vs-replace for a month that already
        # exists. overwrite=true instead derives a fresh burst-union grid from
        # the current window and REBUILDS any store that is incompatible with
        # it (grid mismatch / missing bands), discarding that store's contents.
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
        on_time_conflict = _out_pol.existing_month
        target_crs = _mgrs_to_utm_epsg(mgrs_tile_id)

        tile_clip = processing_config.get("tile_clip", True)
        # Phase 2: route the per-acquisition scenes writer through the
        # bounded-memory blockwise path (per-block bands, halo GLCM, streamed
        # COG). Default on; set processing.scenes_blockwise: false to force the
        # byte-strict legacy full-frame writer. blockwise_threads reuses the
        # smonthly monthly.blockwise_threads knob ('auto' or an int).
        scenes_blockwise = bool(processing_config.get("scenes_blockwise", True))
        scenes_blockwise_threads = _resolve_blockwise_threads(
            (processing_config.get("monthly") or {}).get("blockwise_threads", 1)
        )
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
                if 'product_type' in _cat.columns:
                    _cat = _cat[_cat['product_type'].isin(['scenes', 'smonthly'])]
                import re as _re
                for _, _row in _cat.iterrows():
                    _ggid = str(_row.get('geometry_group_id') or '')
                    # geometry_group_id is {TILE}_{DIR}_TK{tok} (track-only,
                    # current) or {TILE}_{DIR}_TK{tok}_N{nn} (legacy records).
                    _m = _re.search(r'_TK([\d\-]+)(?:_N\d+)?$', _ggid)
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
        # Burst-footprint geometries (fixed per OPERA burst id) + tile geometry,
        # built once per tile so the metadata prefilter can ESTIMATE an interior
        # gap's tile-area share instead of dropping on any interior-missing
        # burst. The geometry column exists when df_rtc_ts is the CMR
        # GeoDataFrame; without it the prefilter keeps its conservative drop.
        _burst_geoms: dict = {}
        _tile_geom_4326 = None
        try:
            if hasattr(df_rtc_ts, 'geometry') and 'jpl_burst_id' in df_rtc_ts.columns:
                for _bid, _geom in zip(df_rtc_ts['jpl_burst_id'], df_rtc_ts.geometry):
                    _b = str(_bid)
                    if _b not in _burst_geoms and _geom is not None:
                        _burst_geoms[_b] = _geom
            from shapely import wkt as _shp_wkt
            _tile_geom_4326 = _shp_wkt.loads(
                _get_mgrs_tile_geometry_wkt(mgrs_tile_id)
            )
        except Exception as _ge:
            logger.debug("Hole-estimate geometry unavailable: %s", _ge)
            _burst_geoms, _tile_geom_4326 = {}, None
        _incomplete_acqs: list[dict] = []
        group_mode = 'acq_group'  # always acq_group mode
        features_ratio = processing_config.get('features_ratio', False)
        features_rvi = processing_config.get('features_rvi', False)
        features_glcm = processing_config.get('features_glcm', False)
        polarization = config.get('roi', {}).get('polarization', 'VV+VH')
        copol_name, crosspol_name, ratio_name, rvi_name = get_band_names(polarization)

        # Monthly config
        monthly_cfg = processing_config.get('monthly', {})
        monthly_enabled = bool(monthly_cfg.get('enabled', False))
        monthly_only = bool(monthly_cfg.get('only', False))
        if monthly_only and not monthly_enabled:
            raise ValueError(
                "processing.monthly.only=true requires "
                "processing.monthly.enabled=true"
            )
        write_scenes = not monthly_only
        logger.info(
            "Scenes workflow products: scenes=%s, smonthly=%s, monthly_only=%s",
            "enabled" if write_scenes else "disabled",
            "enabled" if monthly_enabled else "disabled",
            monthly_only,
        )
        _console.print(
            f"[dim]  Products: scenes={'enabled' if write_scenes else 'disabled'}, "
            f"smonthly={'enabled' if monthly_enabled else 'disabled'}[/dim]"
        )

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
        # Resolve max_download_workers (int or "auto"). "auto" divides the
        # global ASF connection budget across the tile-level worker pool.
        _parallel_cfg = config.get('parallel', {})
        _tile_workers = (
            int(_parallel_cfg.get('max_workers', 2))
            if _parallel_cfg.get('enabled', False) else 1
        )
        _max_workers = _resolve_download_workers(
            config.get('memory', {}).get('max_download_workers', 4), _tile_workers
        )
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
        # GLCM no longer forces the legacy full-array path: the blockwise writer
        # computes it exactly via a halo-composite-before-clip pass. Despeckle
        # and other spatial-neighbourhood filters still require the legacy path
        # (handled separately), so exclude only GLCM from this gate.
        spatial_filter_legacy = _spatial_filters_enabled(
            processing_config,
            do_despeckle=do_despeckle,
            features_glcm=False,
        )
        # The smonthly writer composites RAW scenes: despeckle is applied only in
        # the per-acquisition scenes writer (_write_scenes_output), never to the
        # monthly composite. So despeckle alone must NOT force smonthly onto the
        # legacy full-array path — its blockwise output is bit-identical to legacy
        # regardless of spatial_despeckle. Exclude both despeckle AND GLCM here;
        # only a genuine composite-time neighbourhood filter would flip this True.
        smonthly_spatial_legacy = _spatial_filters_enabled(
            processing_config,
            do_despeckle=False,
            features_glcm=False,
        )
        if spatial_filter_legacy:
            logger.info(
                "Spatial-neighborhood processing enabled; scenes writer uses the "
                "legacy full-array despeckle path and batch strategy is capped at "
                "quarterly."
            )
        if smonthly_spatial_legacy:
            logger.info(
                "Composite-time spatial-neighborhood filter enabled; smonthly will "
                "use the legacy full-array path."
            )

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
        # Resume-grid adoption: when this tile already has product stores
        # from a previous run, reuse their locked grid (the one backing the
        # most data, see _adopt_existing_master_grid) as this run's master
        # grid instead of re-deriving it from the current window's burst
        # union. This is what makes a pilot-month store extensible to a
        # full-year rerun: the burst-union grid varies with the window, and
        # a mismatched grid would otherwise fail the store's grid-lock check.
        # Adoption applies under BOTH store policies: with existing_store=
        # rebuild-incompatible the adopted grid protects the populated
        # store(s) while any store on a disagreeing grid is rebuilt onto it;
        # only a fresh tile (or a CRS/resolution change) derives a new
        # burst-union grid.
        _adopted = _adopt_existing_master_grid(
            tile_dir, target_crs, target_res
        )
        if _adopted is not None:
            (_master_transform, _master_width, _master_height,
             _master_x_coords, _master_y_coords) = _adopted
            _grid_built = True
            logger.info(
                "[Grid] Resuming on existing store grid %dx%d for %s "
                "(grid locked); new acquisitions are aligned onto it",
                _master_width, _master_height, mgrs_tile_id,
            )
        _metadata_prefilter = monthly_only and monthly_enabled and not write_scenes
        # Default path downloads VV/VH concurrently. The old VV-first branch is
        # intentionally off by default because clean tiles rarely save VH
        # downloads, and sequential VV then VH doubles wall-clock network time.
        _vv_first_qc = False

        def _fetch_batch(batch_idx: int, batch_dates: list) -> dict | None:
            """Prepare and download ONE batch; returns its arrays or None.

            This is the producer half of the batch loop, extracted so a
            one-batch download prefetch can run it on a background thread
            while the main thread composites the previous batch (see
            _iter_prefetched). It is prefetch-safe: it only reads the
            enclosing scope and appends to _incomplete_acqs (list.append is
            atomic under the GIL). Returns None when the batch has no valid
            URL pairs or its download failed — the historical 'continue'
            cases.
            """
            logger.info("--- Batch %d/%d ---", batch_idx, len(date_batches))

            with _phase_timer(
                "batch.prepare",
                tile=mgrs_tile_id,
                batch=f"{batch_idx}/{len(date_batches)}",
            ):
                df_batch = df_rtc_ts[
                    df_rtc_ts['acq_dt'].isin(batch_dates)
                ].copy()
                df_batch = df_batch.sort_values('acq_dt').reset_index(drop=True)
                if _metadata_prefilter:
                    df_batch = _prefilter_metadata_incomplete_acquisitions(
                        mgrs_tile_id=mgrs_tile_id,
                        direction_label=direction_label,
                        df_rtc_ts=df_batch,
                        require_complete_bursts=require_complete_bursts,
                        track_footprint=_track_footprint,
                        track_footprint_ids=_fp_ids,
                        incomplete_policy=incomplete_policy,
                        incomplete_sink=_incomplete_acqs,
                        console_obj=_console,
                        interior_hole_max_frac=interior_hole_max_frac,
                        burst_geoms=_burst_geoms,
                        tile_geom=_tile_geom_4326,
                    )
                df_batch['_source_row'] = np.arange(len(df_batch), dtype=np.int64)
            df_input = adapt_enumerator_to_distmetrics(df_batch)
            if '_source_row' not in df_input.columns:
                df_input['_source_row'] = np.arange(len(df_input), dtype=np.int64)
            df_input = validate_url_pairs(df_input)

            if df_input.empty:
                logger.warning(
                    "Batch %d has no valid URL pairs, skipping", batch_idx
                )
                return None

            # Download with batch-level retry
            _batch_success = False
            final_vv = final_vh = clean_dates = None
            prof_vv = prof_vh = None
            _copol_source_indices: list[int] | None = None
            for _attempt in range(_batch_max_retries + 1):
                try:
                    logger.info(
                        "[DOWNLOAD] Batch %d/%d: Starting download of %d scenes (attempt %d/%d)",
                        batch_idx, len(date_batches), len(df_input), _attempt + 1, _batch_max_retries + 1
                    )

                    if _vv_first_qc:
                        with _phase_timer(
                            "download.copol",
                            tile=mgrs_tile_id,
                            batch=f"{batch_idx}/{len(date_batches)}",
                            scenes=len(df_input),
                        ):
                            (
                                final_vv, prof_vv, clean_dates, _copol_source_indices
                            ) = load_rtc_band_strict(
                                df_input,
                                band="copol",
                                max_workers=_max_workers,
                                do_despeckle=False,
                                despeckle_method=_despeckle_method,
                                despeckle_kwargs=_despeckle_kwargs,
                                scene_max_retries=_scene_max_retries,
                                max_failed_ratio=_max_failed_ratio,
                                retry_timeout_seconds=_scene_retry_timeout,
                            )
                        final_vh, prof_vh = [], []
                    else:
                        with _phase_timer(
                            "download.strict_vv_vh",
                            tile=mgrs_tile_id,
                            batch=f"{batch_idx}/{len(date_batches)}",
                            scenes=len(df_input),
                        ):
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

                    logger.info(
                        "[DOWNLOAD] Batch %d/%d complete: final_vv=%d, prof_vv=%d, "
                        "final_vh=%d, prof_vh=%d, clean_dates=%d",
                        batch_idx, len(date_batches),
                        len(final_vv), len(prof_vv), len(final_vh), len(prof_vh), len(clean_dates)
                    )

                    # Critical check: Detect empty download results
                    if len(final_vv) == 0 or len(prof_vv) == 0:
                        logger.error(
                            "[DOWNLOAD] *** CRITICAL: Batch %d/%d returned EMPTY arrays! ***",
                            batch_idx, len(date_batches)
                        )
                        logger.error(
                            "[DOWNLOAD] Expected %d scenes, got final_vv=%d, prof_vv=%d",
                            len(df_input), len(final_vv), len(prof_vv)
                        )
                        logger.error(
                            "[DOWNLOAD] Possible causes: "
                            "(1) All scenes failed download (network/ASF issue), "
                            "(2) max_failed_ratio=%.2f exceeded, "
                            "(3) ASF rate limiting. "
                            "Check worker logs for details.",
                            _max_failed_ratio
                        )

                        # Treat as batch failure, will retry if attempts remain
                        raise ValueError(
                            f"Batch {batch_idx}: No scenes downloaded. "
                            f"Cannot proceed with grid generation."
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
                return None

            _console.print(
                f"[dim]    Batch {batch_idx}/{len(date_batches)}: "
                f"{len(clean_dates)} scene(s) downloaded[/dim]"
            )

            if _vv_first_qc:
                with _phase_timer(
                    "profiles.sanitize.copol",
                    tile=mgrs_tile_id,
                    batch=f"{batch_idx}/{len(date_batches)}",
                ):
                    final_vv, prof_vv, clean_dates, _copol_source_indices = _sanitize_band_arrays(
                        final_vv, prof_vv, clean_dates, _copol_source_indices
                    )
                # Keep the legacy paired-profile diagnostic path satisfied until
                # QC has selected acquisitions and real crosspol is downloaded.
                final_vh = [np.empty((0, 0), dtype=np.float32) for _ in final_vv]
                prof_vh = list(prof_vv)

            # ================================================================
            # DIAGNOSTIC: Check prof_arr structure for contamination
            # ================================================================
            logger.info(
                "[DIAG] Checking profile arrays: final_vv=%d, prof_vv=%d, "
                "final_vh=%d, prof_vh=%d, clean_dates=%d",
                len(final_vv), len(prof_vv), len(final_vh), len(prof_vh), len(clean_dates)
            )

            # Check for None values and invalid types. Rasterio Profile objects
            # are Mapping-like and valid even when they are not plain dicts.
            none_count_vv = sum(1 for p in prof_vv if p is None)
            none_count_vh = sum(1 for p in prof_vh if p is None)
            non_mapping_vv = sum(1 for p in prof_vv if p is not None and not isinstance(p, Mapping))
            non_mapping_vh = sum(1 for p in prof_vh if p is not None and not isinstance(p, Mapping))

            logger.info(
                "[DIAG] prof_vv: %d None, %d non-Mapping", none_count_vv, non_mapping_vv
            )
            logger.info(
                "[DIAG] prof_vh: %d None, %d non-Mapping", none_count_vh, non_mapping_vh
            )

            # Detailed inspection of problematic indices
            problematic_indices = []
            for i in range(max(len(final_vv), len(prof_vv), len(final_vh), len(prof_vh))):
                issues = []

                # Check VV
                if i >= len(final_vv):
                    issues.append("final_vv missing")
                elif final_vv[i] is None:
                    issues.append("final_vv=None")

                if i >= len(prof_vv):
                    issues.append("prof_vv missing")
                elif prof_vv[i] is None:
                    issues.append("prof_vv=None")
                elif not isinstance(prof_vv[i], Mapping):
                    issues.append(f"prof_vv={type(prof_vv[i]).__name__}")
                elif prof_vv[i].get("transform") is None:
                    issues.append("prof_vv.transform=None")
                elif prof_vv[i].get("crs") is None:
                    issues.append("prof_vv.crs=None")

                # Check VH
                if i >= len(final_vh):
                    issues.append("final_vh missing")
                elif final_vh[i] is None:
                    issues.append("final_vh=None")

                if i >= len(prof_vh):
                    issues.append("prof_vh missing")
                elif prof_vh[i] is None:
                    issues.append("prof_vh=None")
                elif not isinstance(prof_vh[i], Mapping):
                    issues.append(f"prof_vh={type(prof_vh[i]).__name__}")
                elif prof_vh[i].get("transform") is None:
                    issues.append("prof_vh.transform=None")
                elif prof_vh[i].get("crs") is None:
                    issues.append("prof_vh.crs=None")

                if issues:
                    problematic_indices.append(i)
                    date_str = clean_dates[i] if i < len(clean_dates) else "N/A"
                    logger.error(
                        "[DIAG] Index %d (date=%s): %s", i, date_str, ", ".join(issues)
                    )

            if problematic_indices:
                logger.error(
                    "[DIAG] *** CONTAMINATION DETECTED *** "
                    "%d/%d indices have issues: %s",
                    len(problematic_indices), len(prof_vv),
                    problematic_indices[:10]  # Show first 10
                )
                logger.error(
                    "[DIAG] This will cause FALLBACK on every block, "
                    "resulting in 30-40x performance degradation!"
                )
                logger.warning("[DIAG] Applying automatic sanitization...")

                # Apply automatic fix
                final_vv, prof_vv, final_vh, prof_vh, clean_dates, df_batch = _sanitize_prof_arrays(
                    final_vv, prof_vv, final_vh, prof_vh, clean_dates, df_batch
                )

                logger.info(
                    "[DIAG] Sanitization complete: %d → %d valid scenes",
                    len(prof_vv) + len(problematic_indices), len(prof_vv)
                )
            else:
                logger.info("[DIAG] ✓ All profile arrays are clean")
            # ================================================================

            # Validate profile arrays for blockwise path compatibility
            if not prof_vv or len(prof_vv) != len(final_vv):
                logger.error(
                    "Profile array incomplete: prof_vv has %d entries but "
                    "final_vv has %d. Blockwise monthly path will fall back to "
                    "full-tile mosaics (memory-intensive).",
                    len(prof_vv) if prof_vv else 0, len(final_vv)
                )
            if not prof_vh or len(prof_vh) != len(final_vh):
                logger.error(
                    "Profile array incomplete: prof_vh has %d entries but "
                    "final_vh has %d. Blockwise monthly path will fall back to "
                    "full-tile mosaics (memory-intensive).",
                    len(prof_vh) if prof_vh else 0, len(final_vh)
                )

            return {
                'df_batch': df_batch,
                'df_input': df_input,
                'final_vv': final_vv, 'prof_vv': prof_vv,
                'final_vh': final_vh, 'prof_vh': prof_vh,
                'clean_dates': clean_dates,
                'copol_source_indices': _copol_source_indices,
            }

        # One-batch download prefetch (opt-in): while the main thread runs
        # QC/composite/write for batch N, a background thread prepares and
        # downloads batch N+1. Peak memory grows by ONE extra batch, so keep
        # batch strategy sizing in mind (the demand-aware 'auto' strategy
        # accounts for this automatically via resident_batches=2). Disabled
        # for the sequential VV-first QC path, which re-enters the download
        # pool mid-consume.
        _prefetch_enabled = bool(
            (config.get('memory', {}) or {}).get('download_prefetch', False)
        ) and not _vv_first_qc
        if _prefetch_enabled:
            logger.info(
                "[Prefetch] Download prefetch enabled: batch N+1 downloads on "
                "a background thread while batch N is composited (one extra "
                "batch resident in RAM)."
            )

        for batch_idx, _fetched in _iter_prefetched(
            date_batches, _fetch_batch, _prefetch_enabled
        ):
            if _fetched is None:
                continue
            df_batch = _fetched['df_batch']
            df_input = _fetched['df_input']
            final_vv = _fetched['final_vv']
            prof_vv = _fetched['prof_vv']
            final_vh = _fetched['final_vh']
            prof_vh = _fetched['prof_vh']
            clean_dates = _fetched['clean_dates']
            _copol_source_indices = _fetched['copol_source_indices']
            del _fetched

            # Build master grid from the burst-footprint UNION (not the MGRS
            # tile bounds). This is the "support-before-clip" invariant: the
            # union grid is strictly larger than the tile, so every spatial-
            # window operation downstream (GLCM texture, despeckle) sees real
            # beyond-tile neighbours near the tile edge. The MGRS tile clip is
            # applied only AFTER those operations, which is what prevents
            # artificial tile-boundary artifacts. Do NOT switch this to
            # _build_grid_from_mgrs_tile without moving the clip and re-deriving
            # the halo (see tests/test_spatial_support_before_clip.py).
            if not _grid_built:
                with _phase_timer(
                    "grid.build",
                    tile=mgrs_tile_id,
                    batch=f"{batch_idx}/{len(date_batches)}",
                    profiles=len(prof_vv),
                ):
                    # Prefer the FULL-WINDOW footprint union (every burst in
                    # the whole query, from CMR metadata) over the first
                    # batch's downloaded profiles: the first batch may be an
                    # early, sparser era (e.g. 2016-Q4) whose union would
                    # crop bursts that only exist in later eras. Both
                    # builders snap to the same lattice, so resume-grid
                    # adoption keeps working across the change.
                    if _burst_geoms:
                        _master_transform, _master_width, _master_height, \
                            _master_x_coords, _master_y_coords = (
                                _build_grid_from_geoms(
                                    list(_burst_geoms.values()),
                                    target_crs, target_res,
                                )
                            )
                        logger.info(
                            "Master grid from FULL-WINDOW footprint union "
                            "(%d bursts, all eras): %dx%d",
                            len(_burst_geoms), _master_width, _master_height,
                        )
                    else:
                        _master_transform, _master_width, _master_height, \
                            _master_x_coords, _master_y_coords = (
                                _build_grid_from_bursts(
                                    prof_vv, target_crs, target_res
                                )
                            )
                        logger.info(
                            "Master grid from %d burst footprints (batch 1 "
                            "fallback; no metadata geometry): %dx%d",
                            len(prof_vv), _master_width, _master_height,
                        )
                    # Era-independence guard: batch 1 is the earliest era,
                    # whose data-take framing may miss tile-edge bursts that
                    # later eras have. Grow the fresh grid to at least the
                    # MGRS tile bounds so no later-era data inside the tile
                    # can ever be cropped. (Adopted store-locked grids above
                    # are never expanded — they must match their stores.)
                    _master_transform, _master_width, _master_height, \
                        _master_x_coords, _master_y_coords = (
                            _expand_grid_to_tile_bounds(
                                _master_transform, _master_width,
                                _master_height, _master_x_coords,
                                _master_y_coords, mgrs_tile_id, target_crs,
                                target_res,
                            )
                        )
                _grid_built = True

            valid_clean_indices: set[int] | None = None

            if write_scenes:
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
                    despeckle_pipeline=bool(
                        _despeckle_cfg.get('pipeline', True)
                    ),
                    despeckle_window=bool(
                        _despeckle_cfg.get('window', True)
                    ),
                    despeckle_window_margin=int(
                        _despeckle_cfg.get('window_margin', 64)
                    ),
                    group_mode=group_mode,
                    require_complete_bursts=require_complete_bursts,
                    track_footprint=_track_footprint,
                    track_footprint_ids=_fp_ids,
                    incomplete_policy=incomplete_policy,
                    incomplete_sink=_incomplete_acqs,
                    interior_hole_max_frac=interior_hole_max_frac,
                    rebuild_on_mismatch=overwrite,
                    scenes_blockwise=scenes_blockwise,
                    blockwise_threads=scenes_blockwise_threads,
                )
                all_scenes_records.extend(scenes_recs)
                if monthly_enabled:
                    valid_clean_indices = _valid_clean_indices_from_scene_records(
                        clean_dates, scenes_recs
                    )
            elif monthly_enabled:
                with _phase_timer(
                    "qc.acquisitions",
                    tile=mgrs_tile_id,
                    batch=f"{batch_idx}/{len(date_batches)}",
                    vv_only=_vv_first_qc,
                ):
                    valid_clean_indices = _prepare_valid_clean_indices_for_monthly(
                        mgrs_tile_id, direction_label,
                        final_vv, prof_vv, final_vh, prof_vh, clean_dates, df_batch,
                        target_crs,
                        transform=_master_transform,
                        width=_master_width,
                        height=_master_height,
                        tile_clip=tile_clip,
                        require_complete_bursts=require_complete_bursts,
                        track_footprint=_track_footprint,
                        track_footprint_ids=_fp_ids,
                        incomplete_policy=incomplete_policy,
                        incomplete_sink=_incomplete_acqs,
                        interior_hole_max_frac=interior_hole_max_frac,
                        console_obj=_console,
                    )

                if _vv_first_qc:
                    if not valid_clean_indices:
                        logger.warning(
                            "Batch %d/%d has no QC-passing acquisitions after VV-only QC",
                            batch_idx, len(date_batches),
                        )
                        if _clear_cache:
                            del final_vv, final_vh, df_batch, df_input
                            gc.collect()
                        continue

                    qc_indices = sorted(valid_clean_indices)
                    if _copol_source_indices is None:
                        raise RuntimeError("VV-first QC lost source index mapping")

                    final_vv_qc = [final_vv[i] for i in qc_indices]
                    prof_vv_qc = [prof_vv[i] for i in qc_indices]
                    source_positions = [int(_copol_source_indices[i]) for i in qc_indices]
                    df_input_cross = df_input.iloc[source_positions].copy().reset_index(drop=True)

                    with _phase_timer(
                        "download.crosspol_after_qc",
                        tile=mgrs_tile_id,
                        batch=f"{batch_idx}/{len(date_batches)}",
                        scenes=len(df_input_cross),
                    ):
                        final_vh, prof_vh, vh_dates, vh_source_indices = load_rtc_band_strict(
                            df_input_cross,
                            band="crosspol",
                            max_workers=_max_workers,
                            do_despeckle=False,
                            despeckle_method=_despeckle_method,
                            despeckle_kwargs=_despeckle_kwargs,
                            scene_max_retries=_scene_max_retries,
                            max_failed_ratio=_max_failed_ratio,
                            retry_timeout_seconds=_scene_retry_timeout,
                        )
                        final_vh, prof_vh, vh_dates, vh_source_indices = _sanitize_band_arrays(
                            final_vh, prof_vh, vh_dates, vh_source_indices
                        )

                    if not vh_source_indices:
                        logger.warning(
                            "Batch %d/%d has no crosspol data after VV QC",
                            batch_idx, len(date_batches),
                        )
                        if _clear_cache:
                            del final_vv, final_vh, df_batch, df_input
                            gc.collect()
                        continue

                    final_vv = [final_vv_qc[i] for i in vh_source_indices]
                    prof_vv = [prof_vv_qc[i] for i in vh_source_indices]
                    clean_dates = list(vh_dates)
                    source_positions = [source_positions[i] for i in vh_source_indices]
                    source_rows = df_input.iloc[source_positions]['_source_row'].astype(int).tolist()
                    df_batch = df_batch.iloc[source_rows].copy().reset_index(drop=True)
                    valid_clean_indices = set(range(len(clean_dates)))
                    logger.info(
                        "VV-first QC retained %d acquisition rows; crosspol retained %d rows",
                        len(qc_indices), len(clean_dates),
                    )

            # Write smonthly/ output (conditional)
            if monthly_enabled:
                with _phase_timer(
                    "write.smonthly",
                    tile=mgrs_tile_id,
                    batch=f"{batch_idx}/{len(date_batches)}",
                    scenes=len(clean_dates),
                ):
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
                        valid_clean_indices=valid_clean_indices,
                        spatial_filter_legacy=smonthly_spatial_legacy,
                        rebuild_on_mismatch=overwrite,
                    )
                all_monthly_records.extend(monthly_recs)

            if _clear_cache:
                del final_vv, final_vh, df_batch, df_input
                gc.collect()
                # References dropped above: reclaim this batch's spill files
                # so disk stays bounded at ~one batch (+ prefetch slot). With
                # download prefetch, batch N+1's files are already spilled and
                # still referenced — POSIX unlink keeps them readable through
                # their live memmaps and frees the space when those close.
                batch_spill.cleanup_batch()

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

        dfs_new = []
        if all_scenes_records:
            dfs_new.append(pd.DataFrame(all_scenes_records))
        if all_monthly_records:
            dfs_new.append(pd.DataFrame(all_monthly_records))

        tile_catalog_path = tile_dir / 'catalog.parquet'
        if not dfs_new:
            logger.warning(
                "No scene or smonthly records were produced for %s; "
                "leaving any existing catalog unchanged",
                mgrs_tile_id,
            )
            return {
                'status':         'success',
                'error':          None,
                'tile_dir':       str(tile_dir),
                'catalog_path':   str(tile_catalog_path) if tile_catalog_path.exists() else None,
                'written_scenes': len(all_scenes_records),
                'written_months': [],
                'dropped_tracks': _dropped_tracks,
                'incomplete_acquisitions': _incomplete_acqs,
            }

        df_new = pd.concat(dfs_new, ignore_index=True)

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

        with _phase_timer("catalog.write_tile", tile=mgrs_tile_id, rows=len(df_catalog)):
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

def _init_scenes_worker(runtime_limits) -> None:
    """Apply runtime limits before a process-pool worker starts task work.

    Also sets up per-worker logging to enable debugging of download failures
    and other worker-specific issues that don't appear in the main log.
    """
    from pathlib import Path
    from s1grits.runtime_limits import apply_runtime_limits

    worker_pid = os.getpid()

    # Set up per-worker log file
    try:
        log_dir = Path("./logs")
        log_dir.mkdir(exist_ok=True)

        worker_log = log_dir / f"worker_{worker_pid}.log"
        handler = logging.FileHandler(str(worker_log), mode='w', encoding='utf-8')
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)

        # Add to root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(handler)
        for noisy_logger in (
            "urllib3", "requests", "rasterio", "rasterio._env",
            "rasterio.env", "rasterio._warp", "rasterio._base",
            "fiona", "pyproj", "zarr", "zarr.group", "numcodecs",
        ):
            logging.getLogger(noisy_logger).setLevel(logging.WARNING)

        logger = logging.getLogger(__name__)
        logger.setLevel(logging.DEBUG)
        logger.info(f"[WORKER {worker_pid}] Worker process initialized, logging to {worker_log}")
    except Exception as e:
        # Don't fail worker initialization if logging setup fails
        print(f"Warning: Worker {worker_pid} failed to set up log file: {e}")

    # Apply runtime limits
    apply_runtime_limits(runtime_limits)

    logger = logging.getLogger(__name__)
    logger.info(f"[WORKER {worker_pid}] Runtime limits applied: {runtime_limits}")
    logger.info(f"[WORKER {worker_pid}] Worker ready")

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
    from s1grits.runtime_limits import apply_runtime_limits, runtime_limits_from_config
    apply_runtime_limits(runtime_limits_from_config(config))
    os.environ['TQDM_DISABLE'] = '1'   # silence download tqdm bars in this worker
    console.quiet = True               # silence the module-global console
    config_copy = _apply_worker_memory_budget(config, memory_budget_gb)

    return process_single_scenes_tile(
        mgrs_tile_id, time_ranges, config_copy, output_root, quiet=True
    )

def _apply_worker_memory_budget(config: dict, memory_budget_gb: float) -> dict:
    """Return a per-worker config copy with this worker's RAM budget applied.

    ``memory.max_memory_gb`` is set to the worker's share of system RAM so
    the 'auto' batch-strategy estimator sizes batches for one worker, not the
    whole machine. An EXPLICIT ``memory.batch_strategy`` (yearly/quarterly/
    monthly) is honoured unchanged — user configuration is never silently
    overridden; only 'auto' (or absent) resolves against the worker budget.
    """
    import copy
    config_copy = copy.deepcopy(config)
    mem = config_copy.setdefault('memory', {})
    mem['max_memory_gb'] = memory_budget_gb

    explicit = str(mem.get('batch_strategy', 'auto')).lower()
    if explicit == 'auto':
        mem['batch_strategy'] = 'auto'
    else:
        logger.info(
            "Honoring explicit memory.batch_strategy=%r in parallel mode "
            "(worker RAM budget %.1f GB is applied to 'auto' only)",
            explicit, memory_budget_gb,
        )
    return config_copy
