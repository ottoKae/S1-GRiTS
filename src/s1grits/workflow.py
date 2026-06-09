"""
Core workflow module

Integrates all functional modules to implement the complete workflow:
WKT → MGRS Tiles → Monthly compositing → Zarr/COG output

v1.0.0 - Supports parallel processing and catalog management
"""

import sys
import os
import gc
import json
import time
import logging
from pathlib import Path
from typing import Any
from warnings import warn
from concurrent.futures import ProcessPoolExecutor, as_completed

import yaml
import pandas as pd
import geopandas as gpd
from shapely import wkt as shapely_wkt
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn
from rich.console import Console

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    warn("psutil not installed. Memory management features limited.")

# Initialize console with legacy Windows mode for compatibility
console = Console(legacy_windows=True)

# Import project modules
from s1grits.mgrs_burst_data import get_mgrs_tiles_overlapping_geometry
from s1grits.asf_tiles import get_rtc_s1_ts_metadata_from_mgrs_tiles
from s1grits.asf_io import load_and_despeckle_rtc_strict
from s1grits.asf_output_writing import build_s1_monthly_cog_and_zarr_tileUTM
from s1grits.time_utils import parse_time_range_config
from s1grits.memory_manager import get_memory_strategy_from_config, chunk_time_by_strategy
from s1grits.adapters import (
    adapt_enumerator_to_distmetrics,
    filter_by_flight_direction,
    validate_url_pairs
)
from s1grits.zarr_time_fix import fix_zarr_order
from s1grits.logger_config import get_logger
from s1grits.file_lock import acquire_lock, release_lock
from s1grits.product_instance import (
    resolve_variant_values,
    make_processing_signature,
    make_product_variant,
    derive_actual_bands,
)
from s1grits.product_registry import load_product_registry

logger = get_logger(__name__)


def load_config(config_path: str | Path) -> dict[str, Any]:
    """
    Load YAML configuration file

    Args:
        config_path: Configuration file path

    Returns:
        dict[str, Any]: Configuration dictionary

    Raises:
        FileNotFoundError: If the config file does not exist.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file does not exist: {config_path}")

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    logger.info("Config file loaded: %s", config_path)
    return config


def enumerate_mgrs_tiles(config: dict) -> list[str]:
    """
    Get MGRS tiles list from config (auto-detect or manual specification)

    Args:
        config: Configuration dictionary

    Returns:
        list[str]: MGRS tile IDs

    Raises:
        ValueError: If no MGRS tiles intersect the ROI geometry.
    """
    roi_config = config.get('roi', {})

    # Check if MGRS tiles are manually specified
    manual_tiles = roi_config.get('manual_mgrs_tiles')
    if manual_tiles:
        logger.info("Using manually specified MGRS tiles: %s", manual_tiles)
        return manual_tiles

    # Auto-detect MGRS tiles
    wkt_str = roi_config.get('wkt', '')
    geom = shapely_wkt.loads(wkt_str)

    logger.info("Auto-detecting MGRS tiles from WKT...")
    df_mgrs = get_mgrs_tiles_overlapping_geometry(geom)

    if df_mgrs.empty:
        raise ValueError(f"No MGRS tiles intersecting ROI: {wkt_str[:100]}...")

    mgrs_tile_ids = df_mgrs['mgrs_tile_id'].tolist()
    logger.info("Detected %d MGRS tiles: %s", len(mgrs_tile_ids), mgrs_tile_ids)

    # Log UTM information for each tile
    for _, row in df_mgrs.iterrows():
        logger.debug("  %s: EPSG:%s", row['mgrs_tile_id'], row['utm_epsg'])

    return mgrs_tile_ids


def query_rtc_metadata_for_tile(
    mgrs_tile_id: str,
    time_ranges: list[tuple[str, str]],
    config: dict
) -> gpd.GeoDataFrame:
    """
    Query RTC-S1 time series metadata for a single MGRS tile

    Args:
        mgrs_tile_id: MGRS tile ID
        time_ranges: [(start_date, end_date), ...] list of time range tuples
        config: Configuration dictionary

    Returns:
        gpd.GeoDataFrame: Merged metadata
    """
    roi_config = config.get('roi', {})
    polarization = roi_config.get('polarization', 'VV+VH')

    all_metadata = []

    for start_date, end_date in time_ranges:
        logger.info("  Querying %s ~ %s...", start_date, end_date)

        try:
            df_chunk = get_rtc_s1_ts_metadata_from_mgrs_tiles(
                [mgrs_tile_id],
                track_numbers=None,  # Get all tracks
                start_acq_dt=start_date,
                stop_acq_dt=end_date,
                polarizations=polarization
            )

            if not df_chunk.empty:
                all_metadata.append(df_chunk)
                logger.info("    Found %d scenes", len(df_chunk))
            else:
                logger.warning("No data for %s (%s ~ %s)", mgrs_tile_id, start_date, end_date)

        except Exception as e:
            logger.warning("Query failed for %s (%s ~ %s): %s", mgrs_tile_id, start_date, end_date, e)
            continue

    if not all_metadata:
        warn(f"No RTC-S1 data found for tile: {mgrs_tile_id}")
        return gpd.GeoDataFrame()

    # Merge data from all time ranges
    df_rtc_ts = pd.concat(all_metadata, ignore_index=True)

    # Remove duplicates (by opera_id)
    if 'opera_id' in df_rtc_ts.columns:
        df_rtc_ts = df_rtc_ts.drop_duplicates(subset=['opera_id']).reset_index(drop=True)

    logger.info("Total: %d scenes for %s", len(df_rtc_ts), mgrs_tile_id)
    return df_rtc_ts


def process_single_mgrs_tile(
    mgrs_tile_id: str,
    time_ranges: list[tuple[str, str]],
    config: dict
) -> dict[str, Any]:
    """
    Process a single MGRS tile

    Args:
        mgrs_tile_id: MGRS tile ID
        time_ranges: Time range list
        config: Configuration dictionary

    Returns:
        dict: Processing result
            {
                'zarr_path': str,
                'cog_dir': str,
                'written_months': list[str],
                'status': 'success' | 'failed',
                'error': str | None
            }
    """
    logger.info("=" * 60)
    logger.info("Processing MGRS tile: %s", mgrs_tile_id)
    logger.info("=" * 60)
    _console = Console()
    _console.print(f"\n[bold cyan]Tile: {mgrs_tile_id}[/bold cyan]")

    try:
        # 1. Query metadata
        _console.print("[dim]  [1/4] Querying ASF metadata...[/dim]")
        df_rtc_ts = query_rtc_metadata_for_tile(mgrs_tile_id, time_ranges, config)

        if df_rtc_ts.empty:
            return {
                'tile_dir': None,
                'catalog_path': None,
                'written_months': [],
                'status': 'failed',
                'error': 'No data available'
            }

        _console.print(f"[dim]  [1/4] Found [bold]{len(df_rtc_ts)}[/bold] scenes[/dim]")

        # 2. Filter by flight direction
        flight_direction = config.get('roi', {}).get('flight_direction')
        if flight_direction:
            df_rtc_ts = filter_by_flight_direction(df_rtc_ts, flight_direction)

            if df_rtc_ts.empty:
                return {
                    'tile_dir': None,
                    'catalog_path': None,
                    'written_months': [],
                    'status': 'failed',
                    'error': f'Flight direction {flight_direction} No data'
                }

        # 3. Select batch strategy
        n_scenes = len(df_rtc_ts)
        batch_strategy = get_memory_strategy_from_config(config, n_scenes)
        _console.print(f"[dim]  [2/4] Batch strategy: [bold]{batch_strategy}[/bold] ({n_scenes} scenes)[/dim]")

        # 4. Prepare output paths (using new output_root architecture)
        output_config = config.get('output', {})
        # Defensive read: YAML parses "processing:\n\n  key: val" correctly, but an
        # empty "processing:" node returns None.  Guard against that here.
        processing_config = config.get('processing') or {}
        if not processing_config:
            raise KeyError(
                "Config section 'processing' is missing or empty. "
                "Check that 'processing:' in the YAML has no blank line immediately after it."
            )
        # Apply processing level suffix to output root directory
        # spatial_despeckle=true  → hARDCp (despeckle applied)
        # spatial_despeckle=false → ARDC   (no despeckle)
        _monthly_despeckle = processing_config.get('spatial_despeckle', False)
        _processing_level = "hARDCp" if _monthly_despeckle else "ARDC"
        features_ratio = processing_config.get('features_ratio', False)
        features_rvi = processing_config.get('features_rvi', False)
        features_glcm = processing_config.get('features_glcm', False)
        tile_clip = processing_config.get('tile_clip', True)
        _dir_token = {"ASCENDING": "ASCENDING", "DESCENDING": "DESCENDING"}.get(
            config.get('roi', {}).get('flight_direction'), ""
        )
        # Build band token from enabled feature flags (only non-default bands get a token)
        _band_parts = []
        if features_ratio:
            _band_parts.append('Ratio')
        if features_rvi:
            _band_parts.append('RVI')
        if features_glcm:
            _band_parts.append('GLCM')
        _band_token = ('_' + '_'.join(_band_parts)) if _band_parts else ''
        output_root = Path(output_config.get('base_dir', './output'))

        logger.info("Output root directory: %s", output_root)
        logger.info("Tile directory: %s", output_root / mgrs_tile_id)

        # Compute processing variant metadata for monthly product
        _registry = load_product_registry()
        _monthly_spec = _registry.get("monthly")
        _monthly_variant_fields = _monthly_spec.variant_fields if _monthly_spec else []
        _monthly_variant_vals = resolve_variant_values(config, _monthly_variant_fields)
        _monthly_bands = derive_actual_bands(config, "monthly")
        _monthly_sig = make_processing_signature(_monthly_variant_vals)
        _monthly_variant = make_product_variant("monthly", _monthly_variant_vals, _monthly_bands)
        logger.info(
            "Monthly variant: %s  signature: %s  bands: %s",
            _monthly_variant, _monthly_sig, _monthly_bands,
        )

        # 5. Process in batches
        dates = pd.to_datetime(df_rtc_ts['acq_dt']).unique()
        date_batches = chunk_time_by_strategy(dates.tolist(), batch_strategy)
        _console.print(f"[dim]  [3/4] Downloading & processing [bold]{len(date_batches)}[/bold] batch(es)...[/dim]")

        written_months = []

        for batch_idx, batch_dates in enumerate(date_batches, 1):
            logger.info("--- Batch %d/%d ---", batch_idx, len(date_batches))
            _console.print(
                f"[dim]    Batch {batch_idx}/{len(date_batches)}: "
                f"{len(batch_dates)} scene(s), "
                f"{min(batch_dates).strftime('%Y-%m')} ~ {max(batch_dates).strftime('%Y-%m')}[/dim]"
            )

            # Filter data for current batch
            df_batch = df_rtc_ts[df_rtc_ts['acq_dt'].isin(batch_dates)].copy()

            # Format adaptation
            df_input = adapt_enumerator_to_distmetrics(df_batch)
            df_input = validate_url_pairs(df_input)

            if df_input.empty:
                logger.warning("Batch %d has no valid data, skipping", batch_idx)
                continue

            # Download + despeckle (with batch-level retry)
            logger.info("  Downloading data...")
            _console.print("[dim]      Downloading...[/dim]", end="\r")
            _mem_cfg = config.get('memory', {})
            _batch_max_retries          = _mem_cfg.get('batch_max_retries', 2)
            _scene_max_retries          = _mem_cfg.get('scene_max_retries', 3)
            _max_failed_ratio           = _mem_cfg.get('max_failed_ratio', 0.0)
            _scene_retry_timeout        = _mem_cfg.get('scene_retry_timeout_seconds', 600.0)
            _batch_success = False
            final_vv = final_vh = clean_dates = None
            for _batch_attempt in range(_batch_max_retries + 1):
                try:
                    final_vv, prof_vv, final_vh, prof_vh, clean_dates = load_and_despeckle_rtc_strict(
                        df_input,
                        max_workers=config.get('memory', {}).get('max_download_workers', 4),
                        do_despeckle=False,
                        scene_max_retries=_scene_max_retries,
                        max_failed_ratio=_max_failed_ratio,
                        retry_timeout_seconds=_scene_retry_timeout,
                    )
                    _batch_success = True
                    break
                except Exception as _batch_err:
                    if _batch_attempt < _batch_max_retries:
                        _wait = 30 * (2 ** _batch_attempt)  # 30s, 60s
                        logger.warning(
                            "Batch %d attempt %d/%d failed: %s. Retrying in %ds...",
                            batch_idx, _batch_attempt + 1, _batch_max_retries + 1,
                            _batch_err, _wait
                        )
                        time.sleep(_wait)
                    else:
                        logger.error(
                            "Batch %d failed after %d attempts: %s. Skipping batch.",
                            batch_idx, _batch_max_retries + 1, _batch_err
                        )

            if not _batch_success or not clean_dates:
                logger.warning("Download failed after all batch retries, skipping batch")
                _console.print(f"[yellow]    [WARN] Batch {batch_idx} download failed, skipping[/yellow]")
                continue

            # Monthly compositing + output (using new API)
            logger.info("  Monthly compositing...")
            _console.print("[dim]      Compositing...[/dim]", end="\r")
            res = build_s1_monthly_cog_and_zarr_tileUTM(
                arrs_vv=final_vv,
                prof_vv=prof_vv,
                arrs_vh=final_vh,
                prof_vh=prof_vh,
                acq_datetimes=clean_dates,
                mgrs_tile_id=mgrs_tile_id,
                flight_direction=config.get('roi', {}).get('flight_direction'),
                output_root=str(output_root),
                target_res=processing_config.get('target_resolution', 30.0),
                roi_wkt=config.get('roi', {}).get('wkt'),
                use_roi_mask=processing_config.get('use_roi_mask', False),
                group_mode=processing_config.get('group_mode', 'hour'),
                trim_fraction=processing_config.get('trim_fraction', 0.15),
                on_time_conflict=output_config.get('on_time_conflict', 'skip'),
                monthly_despeckle=_monthly_despeckle,
                despeckle_method=processing_config.get('despeckle', {}).get('method', 'tv_bregman'),
                despeckle_kwargs=processing_config.get('despeckle', {}).get('kwargs', {'reg_param': 5.0}),
                min_valid_lin=processing_config.get('min_valid_lin', 1e-6),
                eps_lin=processing_config.get('eps_lin', 1e-8),
                chunk_y=processing_config.get('zarr_chunks', {}).get('y', 512),
                chunk_x=processing_config.get('zarr_chunks', {}).get('x', 512),
                cog_block=processing_config.get('cog_block_size', 256),
                overwrite_cog=output_config.get('overwrite', False),
                generate_cog=output_config.get('formats', {}).get('cog', True),
                generate_preview=output_config.get('formats', {}).get('preview', True),
                preview_res=300.0,
                processing_level=_processing_level,
                texture_cfg=processing_config.get('texture_features'),
                features_ratio=features_ratio,
                features_rvi=features_rvi,
                features_glcm=features_glcm,
                tile_clip=tile_clip,
                processing_signature=_monthly_sig,
                product_variant=_monthly_variant,
                processing_variant_json=json.dumps(_monthly_variant_vals),
            )

            batch_months = res.get('written_months', [])
            written_months.extend(batch_months)
            logger.info("Months written: %s", batch_months)
            _console.print(
                f"[green]      Batch {batch_idx}/{len(date_batches)} done — "
                f"wrote {len(batch_months)} month(s): {', '.join(batch_months)}[/green]"
            )

            # Patch catalog parquet with variant metadata for records written in this batch
            _catalog_path = res.get('catalog_path')
            if _catalog_path and batch_months:
                try:
                    import json as _json
                    import pyarrow.parquet as _pq
                    import pyarrow as _pa
                    _df_cat = _pq.read_table(_catalog_path).to_pandas()
                    _mask = _df_cat['month'].isin(batch_months) & (_df_cat['product_type'] == 'monthly')
                    if _mask.any():
                        _df_cat.loc[_mask, 'product_variant'] = _monthly_variant
                        _df_cat.loc[_mask, 'processing_signature'] = _monthly_sig
                        _df_cat.loc[_mask, 'processing_config_json'] = _json.dumps(
                            _monthly_variant_vals, sort_keys=True
                        )
                        _pq.write_table(_pa.Table.from_pandas(_df_cat), _catalog_path)
                        logger.info(
                            "[Catalog] Patched %d monthly records with variant=%s sig=%s",
                            _mask.sum(), _monthly_variant, _monthly_sig,
                        )
                except Exception as _patch_err:
                    logger.warning("Failed to patch catalog with variant metadata: %s", _patch_err)

            # Clean up memory
            if config.get('memory', {}).get('clear_cache_per_batch', True):
                del final_vv, final_vh, df_batch, df_input
                gc.collect()

        # Processing completed
        logger.info("=" * 60)
        logger.info("Completed processing: %s", mgrs_tile_id)
        logger.info("Total months: %d", len(set(written_months)))
        logger.info("=" * 60)
        _console.print(
            f"[bold green]  [4/4] Done: {len(set(written_months))} month(s) written[/bold green]"
        )

        # Apply Zarr time ordering fix if enabled
        zarr_fix_config = config.get('processing', {}).get('zarr_time_fix', {})
        if zarr_fix_config.get('enabled', False):
            zarr_path = Path(res['tile_dir']) / 'zarr' / 'S1_monthly.zarr'
            if zarr_path.exists():
                logger.info("Checking Zarr time ordering...")

                backup_dir = zarr_fix_config.get('backup_dir')
                if backup_dir:
                    backup_dir = Path(backup_dir)

                fix_success = fix_zarr_order(
                    zarr_path=zarr_path,
                    dry_run=False,
                    skip_backup=not zarr_fix_config.get('create_backup', True),
                    backup_dir=backup_dir
                )

                if not fix_success:
                    logger.warning("Zarr time ordering fix failed for %s", mgrs_tile_id)
            else:
                logger.warning("Zarr path not found: %s", zarr_path)

        # Static layer processing (one-shot, time-invariant)
        # Runs after monthly compositing so df_rtc_ts is already in scope.
        static_cfg = config.get('static_layers', {})
        if static_cfg.get('enabled', False):
            from s1grits.workflow_static import _get_enabled_layers, process_static_for_tile
            _static_layers = _get_enabled_layers(config)
            if _static_layers:
                _console.print("[dim]  [5/5] Processing static layers...[/dim]")
                _static_result = process_static_for_tile(
                    mgrs_tile_id=mgrs_tile_id,
                    config=config,
                    output_root=output_root,
                    layers=_static_layers,
                )
                if _static_result['status'] == 'failed':
                    logger.warning(
                        "Static layer processing failed for %s: %s",
                        mgrs_tile_id, _static_result['error'],
                    )

        return {
            'tile_dir': res['tile_dir'],
            'catalog_path': res['catalog_path'],
            'written_months': sorted(set(written_months)),
            'status': 'success',
            'error': None
        }

    except Exception as e:
        logger.error("Processing failed for %s: %s", mgrs_tile_id, e, exc_info=True)
        return {
            'tile_dir': None,
            'catalog_path': None,
            'written_months': [],
            'status': 'failed',
            'error': str(e)
        }


def _process_tile_with_memory_budget(
    mgrs_tile_id: str,
    time_ranges: list[tuple[str, str]],
    config: dict,
    memory_budget_gb: float
) -> dict[str, Any]:
    """
    Wrapper function: force memory budget for each worker

    Args:
        mgrs_tile_id: MGRS tile ID
        time_ranges: Time range list
        config: Configuration dictionary
        memory_budget_gb: Memory budget for this worker (GB)

    Returns:
        dict: Processing result
    """
    # Deep copy config and update memory configuration (preserve other fields)
    import copy
    config_copy = copy.deepcopy(config)

    # Update memory config, preserve original fields like max_download_workers
    if 'memory' not in config_copy:
        config_copy['memory'] = {}

    config_copy['memory']['max_memory_gb'] = memory_budget_gb
    config_copy['memory']['batch_strategy'] = 'auto'
    # Preserve other fields (max_download_workers, clear_cache_per_batch, etc.)

    return process_single_mgrs_tile(mgrs_tile_id, time_ranges, config_copy)


def run_multi_mgrs_monthly_workflow(config_path: str | Path) -> dict[str, dict]:
    """
    Main workflow: WKT → MGRS Tiles → Process by tile → Zarr/COG output

    Args:
        config_path: YAMLConfiguration file path

    Returns:
        {
            'mgrs_tile_id': {
                'zarr_path': str,
                'cog_dir': str,
                'written_months': list[str],
                'status': 'success' | 'failed',
                'error': str | None
            }
        }
    """
    # 1. Load configuration
    config = load_config(config_path)

    # 2. Enumerate MGRS tiles
    mgrs_tile_ids = enumerate_mgrs_tiles(config)

    # 3. Parse time ranges
    # wkt is only needed for mode='full' without manual_mgrs_tiles; use None for Mode B
    wkt = config.get('roi', {}).get('wkt')
    time_ranges = parse_time_range_config(config, wkt)

    # 4. Parallel configuration
    parallel_config = config.get('parallel', {})
    enabled = parallel_config.get('enabled', False)
    max_workers = parallel_config.get('max_workers', 4)

    results = {}
    _base_dir = config.get('output', {}).get('base_dir', './output')
    _lock_info = acquire_lock(str(_base_dir))

    # 4b. Disk space pre-check
    try:
        import shutil as _shutil
        from pathlib import Path as _Path
        output_root_check = _Path(_base_dir)
        # Walk up to the nearest existing ancestor so disk_usage works even
        # when the output directory has not been created yet.
        while output_root_check != output_root_check.parent and not output_root_check.exists():
            output_root_check = output_root_check.parent
        _total, _used, _free = _shutil.disk_usage(str(output_root_check))
        _free_gb = _free / (1024 ** 3)
        _warn_gb = config.get('output', {}).get('disk_warn_gb', 50.0)
        if _free_gb < _warn_gb:
            logger.warning(
                "Low disk space: %.1f GB free on output volume (threshold=%.0f GB). "
                "Consider freeing space before proceeding.",
                _free_gb, _warn_gb
            )
        else:
            logger.info("Disk space OK: %.1f GB free on output volume", _free_gb)
    except Exception as _disk_e:
        logger.debug("Disk space check failed: %s", _disk_e)

    # 5. Process tiles (parallel or serial)
    if enabled and len(mgrs_tile_ids) > 1:
        # Parallel processing
        if PSUTIL_AVAILABLE:
            total_mem_gb = psutil.virtual_memory().available / (1024**3)
            mem_per_worker = total_mem_gb / max_workers / 1.2
        else:
            total_mem_gb = 8.0  # Conservative estimate
            mem_per_worker = total_mem_gb / max_workers

        logger.info("Parallel processing enabled: %d workers", max_workers)
        logger.info("Available memory: %.1f GB", total_mem_gb)
        logger.info("Memory per worker: %.1f GB", mem_per_worker)

        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            # Submit all tasks
            future_to_tile = {
                executor.submit(
                    _process_tile_with_memory_budget,
                    tile, time_ranges, config, mem_per_worker
                ): tile
                for tile in mgrs_tile_ids
            }

            # Collect results (with progress bar)
            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                "[progress.percentage]{task.percentage:>3.0f}%",
                TimeElapsedColumn(),
                console=console
            ) as progress:
                task_id = progress.add_task(
                    f"Processing {len(mgrs_tile_ids)} MGRS tiles",
                    total=len(future_to_tile)
                )

                for future in as_completed(future_to_tile):
                    tile_id = future_to_tile[future]
                    try:
                        results[tile_id] = future.result()
                        status = results[tile_id]['status']
                        if status == 'success':
                            logger.info("Completed: %s", tile_id)
                        else:
                            logger.warning("Failed: %s - %s", tile_id, results[tile_id]['error'])
                    except Exception as e:
                        results[tile_id] = {
                            'status': 'failed',
                            'error': str(e)
                        }
                        logger.error("Exception in %s: %s", tile_id, e, exc_info=True)

                    progress.update(task_id, advance=1)
    else:
        # Serial processing (original logic)
        logger.info("Serial processing mode")

        for mgrs_tile_id in mgrs_tile_ids:
            result = process_single_mgrs_tile(mgrs_tile_id, time_ranges, config)
            results[mgrs_tile_id] = result

    # 6. Merge all tile catalogs
    from s1grits.asf_output_writing import merge_tile_catalogs

    output_root = str(config.get('output', {}).get('base_dir', '.'))
    try:
        catalog_path = merge_tile_catalogs(output_root)
        logger.info("Global catalog: %s", catalog_path)
        from s1grits.catalog_sync import update_root_catalog
        update_root_catalog(output_root)
    except Exception as e:
        logger.warning("Catalog merge failed: %s", e)

    # 7. Final coverage summary report
    n_total   = len(results)
    n_success = sum(1 for r in results.values() if r.get('status') == 'success')
    n_failed  = n_total - n_success
    total_months = sum(
        len(r.get('written_months', [])) for r in results.values()
        if r.get('status') == 'success'
    )

    logger.info("=" * 60)
    logger.info("WORKFLOW COMPLETE: %d/%d tiles succeeded", n_success, n_total)
    logger.info("Total months written: %d", total_months)

    if n_failed > 0:
        logger.warning("%d tile(s) failed:", n_failed)
        for tid, r in results.items():
            if r.get('status') != 'success':
                logger.warning("  %s: %s", tid, r.get('error', 'unknown error'))

    # Per-tile coverage summary (from catalog if available)
    try:
        import pandas as _pd
        _dir_cov = {"ASCENDING": "ASCENDING", "DESCENDING": "DESCENDING"}.get(
            config.get('roi', {}).get('flight_direction'), ""
        )
        _output_root = Path(config.get('output', {}).get('base_dir', './output'))
        _catalog_glob = list(_output_root.glob("*/catalog.parquet"))
        if _catalog_glob:
            _dfs = []
            for _cp in _catalog_glob:
                try:
                    _dfs.append(_pd.read_parquet(_cp))
                except Exception as _e:
                    logging.debug("Could not read parquet catalog %s: %s", _cp, _e)
            if _dfs:
                _cat = _pd.concat(_dfs, ignore_index=True)
                if "coverage_fraction" in _cat.columns:
                    _cov = _cat.groupby("mgrs_tile_id")["coverage_fraction"].mean()
                    logger.info("Per-tile mean coverage fraction:")
                    for _tid, _frac in _cov.items():
                        _flag = " [LOW]" if _frac < 0.5 else ""
                        logger.info("  %s: %.1f%%%s", _tid, _frac * 100, _flag)
    except Exception as _cov_e:
        logging.debug("Coverage summary failed: %s", _cov_e)

    logger.info("=" * 60)

    release_lock(_lock_info)
    return results
