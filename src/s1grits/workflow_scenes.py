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
          zarr/s1grits_scenes_{TILE}_{DIR}_TK{tk}.zarr
          cog/s1grits_scenes_{TILE}_{DIR}_TK{tk}_{DT}.tif
          preview/s1grits_scenes_{TILE}_{DIR}_TK{tk}_{DT}.png
        smonthly_{DIR}_{bands}/
          zarr/s1grits_smonthly_{TILE}_{DIR}_TK{tk}.zarr
          cog/s1grits_smonthly_{TILE}_{DIR}_TK{tk}_{YYYY-MM}.tif
          preview/s1grits_smonthly_{TILE}_{DIR}_TK{tk}_{YYYY-MM}.png
        items/{product_label}/{id}.json

CLI entry point: s1grits process_scenes --config config.yaml
"""

import os
from pathlib import Path

import pandas as pd

try:  # Fast C nanmedian; NumPy's masked-array path is ~50x slower on blocks
    import bottleneck as _bn
except ImportError:  # pragma: no cover - optional accelerator
    _bn = None
from rich.console import Console

from s1grits.logger_config import get_logger
from s1grits.workflow import (
    load_config,
    enumerate_mgrs_tiles,
)
from s1grits.time_utils import parse_time_range_config
from s1grits.asf_output_writing import (  # noqa: F401  (_zarr_delete_timestep + _check_tile_integrity stay on the ws facade surface)
    _build_grid_from_bursts,
    _build_grid_from_geoms,
    _check_tile_integrity,
    _get_mgrs_tile_geometry_wkt,
    _mosaic_align,
    _zarr_delete_timestep,
)
from s1grits.canonical_catalog_schema import (
    validate_collection_mapping,
)
from s1grits.file_lock import acquire_lock, release_lock

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
from s1grits.scenes.support import (  # noqa: F401
    DOWNLOAD_AUTO_MAX_WORKERS,
    DOWNLOAD_AUTO_MIN_WORKERS,
    DOWNLOAD_GLOBAL_CONNECTION_BUDGET,
    MAX_WORKERS_AUTO_CAP,
    _AUTO_PER_TILE_GB,
    _cap_batch_strategy_for_spatial_filters,
    _clean_date_key,
    _config_flag_enabled,
    _fmt_mb,
    _get_despeckle_token,
    _load_footprint_cache,
    _phase_fields,
    _phase_timer,
    _resolve_download_workers,
    _resolve_max_workers,
    _rss_mb,
    _sanitize_band_arrays,
    _sanitize_prof_arrays,
    _save_footprint_cache,
    _spatial_filters_enabled,
)
from s1grits.scenes.pipeline import (  # noqa: F401
    _apply_worker_memory_budget,
    _init_scenes_worker,
    _prefilter_metadata_incomplete_acquisitions,
    _prepare_valid_clean_indices_for_monthly,
    _process_scenes_tile_with_memory_budget,
    _valid_clean_indices_from_scene_records,
    process_single_scenes_tile,
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

logger = get_logger(__name__)
console = Console(legacy_windows=True, no_color=False)


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
    from s1grits.runtime_limits import (
        apply_runtime_limits,
        runtime_limits_from_config,
    )
    config = load_config(config_path)
    config = apply_output_overrides_and_stac(config, overrides)
    # Warn-only: surface misspelled/misplaced YAML keys that dict.get() would
    # otherwise silently ignore (e.g. processing.on_time_conflict).
    from s1grits.config_schema import (
        warn_unknown_config_keys,
        resolve_output_policies,
    )
    warn_unknown_config_keys(config, logger)
    # Fail fast on invalid output policies; log v2-key deprecations once.
    _policies = resolve_output_policies(config, logger)
    logger.info(
        "Output policies: existing_store=%s, existing_month=%s",
        _policies.existing_store, _policies.existing_month,
    )
    runtime_limits = runtime_limits_from_config(config)
    applied_runtime_env = apply_runtime_limits(runtime_limits)
    if applied_runtime_env:
        logger.info(
            "Runtime limits applied: %s",
            ", ".join(f"{k}={v}" for k, v in sorted(applied_runtime_env.items())),
        )
    else:
        logger.info("Runtime limits disabled")

    despeckle_token = _get_despeckle_token(config)

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
    # Resolve parallel.max_workers (int or "auto"). "auto" sizes the tile-level
    # process pool from CPU cores and available RAM / the blockwise per-tile
    # working set, so a big-RAM box uses more workers and a small one fewer.
    _mw_raw = parallel_cfg.get('max_workers', 2)
    max_workers = _resolve_max_workers(_mw_raw)
    if str(_mw_raw).lower() == 'auto':
        logger.info(
            "parallel.max_workers: auto -> %d tile workers "
            "(cpu=%s, per_tile~%.0f GB)",
            max_workers, os.cpu_count(), _AUTO_PER_TILE_GB,
        )

    # Resolve monthly.blockwise_threads (int or "auto") once, up front, and
    # write the concrete value back into the config so every tile worker —
    # parallel (pickled copy) or serial (same dict) — receives the same count.
    _monthly_cfg_ref = config.get('processing', {}).get('monthly')
    if isinstance(_monthly_cfg_ref, dict) and 'blockwise_threads' in _monthly_cfg_ref:
        _bw_raw = _monthly_cfg_ref['blockwise_threads']
        _bw_workers = max_workers if parallel_enabled else 1
        _monthly_cfg_ref['blockwise_threads'] = _resolve_blockwise_threads(
            _bw_raw, _bw_workers
        )
        logger.info(
            "smonthly blockwise threads: %s -> %d per tile worker "
            "(tile workers=%d)",
            _bw_raw, _monthly_cfg_ref['blockwise_threads'], _bw_workers,
        )

    results: dict[str, dict] = {}
    all_catalogs: list[pd.DataFrame] = []

    # Disk space preflight under the configurable policy (warn | fail | off):
    # a full-year multi-tile run writes tens of GB, and running out mid-run
    # wastes the whole download budget. mode=fail raises PreflightError here,
    # BEFORE any download starts.
    from s1grits.preflight import check_disk_space
    check_disk_space(config, output_root, logger)

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

            with ProcessPoolExecutor(
                max_workers=max_workers,
                initializer=_init_scenes_worker,
                initargs=(runtime_limits,),
            ) as executor:
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
