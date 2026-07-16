"""Pipeline support helpers for the scenes workflow.

Extracted move-only from workflow_scenes.py (which re-exports every name
here): phase timing/telemetry, config-flag interpretation, worker/download
resolvers and their budgets, the footprint cache, despeckle product tokens,
and the profile/band sanitizers.
"""

import json
import os
import time
from contextlib import contextmanager
from typing import Optional, Tuple, List
from collections.abc import Mapping

import pandas as pd

try:  # Fast C nanmedian; NumPy's masked-array path is ~50x slower on blocks
    import bottleneck as _bn
except ImportError:  # pragma: no cover - optional accelerator
    _bn = None
from rich.console import Console

from s1grits.logger_config import get_logger
from s1grits.memory_manager import (
    detect_system_memory,
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

logger = get_logger(__name__)
console = Console(legacy_windows=True, no_color=False)


def _phase_fields(**fields) -> str:
    parts = [f"{k}={v}" for k, v in fields.items() if v is not None]
    return " " + " ".join(parts) if parts else ""

def _rss_mb() -> float | None:
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / (1024 ** 2)
    except Exception:
        return None

@contextmanager
def _phase_timer(name: str, **fields):
    """Log wall time and RSS around expensive workflow phases."""
    start = time.perf_counter()
    rss_start = _rss_mb()
    logger.info("[PHASE] %s START%s rss_mb=%s", name, _phase_fields(**fields), _fmt_mb(rss_start))
    try:
        yield
    except Exception:
        elapsed = time.perf_counter() - start
        rss_end = _rss_mb()
        logger.exception(
            "[PHASE] %s FAILED elapsed_s=%.2f rss_mb=%s%s",
            name, elapsed, _fmt_mb(rss_end), _phase_fields(**fields),
        )
        raise
    else:
        elapsed = time.perf_counter() - start
        rss_end = _rss_mb()
        delta = None if rss_start is None or rss_end is None else rss_end - rss_start
        logger.info(
            "[PHASE] %s END elapsed_s=%.2f rss_mb=%s delta_mb=%s%s",
            name, elapsed, _fmt_mb(rss_end), _fmt_mb(delta), _phase_fields(**fields),
        )

def _fmt_mb(value: float | None) -> str:
    return "na" if value is None else f"{value:.1f}"

def _config_flag_enabled(value) -> bool:
    if isinstance(value, Mapping):
        return bool(value.get("enabled", False))
    return bool(value)

def _spatial_filters_enabled(
    processing_config: Mapping | None,
    *,
    do_despeckle: bool | None = None,
    features_glcm: bool | None = None,
) -> bool:
    """Return True when processing needs full-array spatial neighborhoods."""
    cfg = processing_config or {}
    if do_despeckle is None:
        do_despeckle = bool(cfg.get("spatial_despeckle", False))
    if features_glcm is None:
        features_glcm = bool(cfg.get("features_glcm", False))
    if do_despeckle or features_glcm:
        return True

    # Future-proof common spatial filter flags that may appear in YAML.
    spatial_keys = (
        "features_texture",
        "features_local_texture",
        "local_texture",
        "texture",
        "convolution",
        "features_convolution",
        "morphology",
        "features_morphology",
        "spatial_filter",
        "spatial_filters",
    )
    return any(_config_flag_enabled(cfg.get(key, False)) for key in spatial_keys)

def _cap_batch_strategy_for_spatial_filters(strategy: str, enabled: bool) -> str:
    """Spatial filters use legacy full-array path, so cap batches at 3 months."""
    if not enabled:
        return strategy
    if str(strategy).lower() == "yearly":
        logger.warning(
            "Spatial filters enabled; downgrading batch strategy yearly -> "
            "quarterly to keep each tile batch at <=3 months."
        )
        return "quarterly"
    return strategy

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

# Global ceiling on simultaneous ASF download connections across the whole
# run (tile_workers x download_workers), chosen to stay polite to ASF's CDN
# while keeping the pipe full. "auto" divides this budget across the tile-level
# process pool. Downloads are I/O-bound, so this is a connection budget, not a
# CPU budget.
DOWNLOAD_GLOBAL_CONNECTION_BUDGET: int = 16

DOWNLOAD_AUTO_MIN_WORKERS: int = 4

DOWNLOAD_AUTO_MAX_WORKERS: int = 8

# Auto-mode ceiling on tile-level worker processes. Tile parallelism is
# memory-bound (each worker holds one tile's blockwise working set), so more
# than this rarely helps and risks oversubscribing cores against the block
# thread pool.
MAX_WORKERS_AUTO_CAP: int = 8

# Representative blockwise per-tile working set (GB) for the auto RAM budget.
# Derived from the blockwise estimate for a busy tile-month (~120 scenes on a
# standard tile); matches the ~11 GB peak seen in real runs.
_AUTO_PER_TILE_GB: float = 12.0

def _resolve_max_workers(value, *, available_gb: float | None = None,
                         cpu: int | None = None, per_tile_gb: float = _AUTO_PER_TILE_GB) -> int:
    """Resolve ``parallel.max_workers`` (int or ``"auto"``) to a worker count.

    A positive integer is used as-is (floored at 1).  ``"auto"`` picks the
    smaller of a CPU bound (one worker per core) and a RAM bound (available
    memory / the blockwise per-tile working set), floored at 1 and capped at
    ``MAX_WORKERS_AUTO_CAP``.  This deliberately sizes for the blockwise path,
    whose per-tile footprint is far smaller than the legacy full-tile stack.
    """
    if isinstance(value, str) and value.strip().lower() == "auto":
        n_cpu = cpu if cpu is not None else (os.cpu_count() or 1)
        ram = available_gb if available_gb is not None else detect_system_memory()
        by_cpu = max(1, int(n_cpu))
        by_ram = max(1, int(ram // max(1.0, per_tile_gb)))
        return max(1, min(by_cpu, by_ram, MAX_WORKERS_AUTO_CAP))
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 2

def _resolve_download_workers(value, tile_workers: int = 1) -> int:
    """Resolve ``memory.max_download_workers`` (int or ``"auto"``) to a count.

    A positive integer is used as-is (floored at 1).  ``"auto"`` divides the
    global connection budget across the tile-level process pool so the total
    concurrent ASF connections stay near ``DOWNLOAD_GLOBAL_CONNECTION_BUDGET``,
    floored at ``DOWNLOAD_AUTO_MIN_WORKERS`` and capped at
    ``DOWNLOAD_AUTO_MAX_WORKERS``.  Unparseable values fall back to 4 (the
    historical default).
    """
    if isinstance(value, str) and value.strip().lower() == "auto":
        per_tile = DOWNLOAD_GLOBAL_CONNECTION_BUDGET // max(1, int(tile_workers))
        return max(DOWNLOAD_AUTO_MIN_WORKERS, min(per_tile, DOWNLOAD_AUTO_MAX_WORKERS))
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 4

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

def _clean_date_key(dt) -> str:
    """Return the UTC acquisition timestamp key used for clean_dates matching."""
    ts = pd.Timestamp(dt)
    ts = ts.tz_localize('UTC') if ts.tz is None else ts.tz_convert('UTC')
    return ts.strftime('%Y%m%dT%H%M%S')

def _sanitize_prof_arrays(
    final_vv: list,
    prof_vv: list,
    final_vh: list,
    prof_vh: list,
    clean_dates: list,
    df_batch: Optional[pd.DataFrame] = None,
) -> Tuple[List, List, List, List, List, Optional[pd.DataFrame]]:
    """Remove entries with None or invalid profiles to prevent blockwise fallback.

    This function ensures prof_arr is always clean for blockwise processing.
    Invalid entries are filtered out, keeping indices aligned across all arrays.

    Parameters
    ----------
    final_vv, final_vh : list
        Loaded scene arrays (may contain None)
    prof_vv, prof_vh : list
        Rasterio profiles (may contain None or non-dict)
    clean_dates : list
        Acquisition timestamps
    df_batch : pd.DataFrame, optional
        Metadata dataframe to sync

    Returns
    -------
    tuple
        Cleaned (final_vv, prof_vv, final_vh, prof_vh, clean_dates, df_batch)
    """
    n_original = len(final_vv)

    # Identify valid indices
    valid_indices = []
    for i in range(n_original):
        # Check array bounds
        if i >= len(prof_vv) or i >= len(prof_vh):
            logger.warning(f"[SANITIZE] Removing index {i}: out of bounds")
            continue

        # Check arrays exist
        if final_vv[i] is None or final_vh[i] is None:
            logger.warning(
                f"[SANITIZE] Removing index {i}: array is None "
                f"(vv={final_vv[i] is not None}, vh={final_vh[i] is not None})"
            )
            continue

        # Check prof_vv validity
        prof_vv_item = prof_vv[i]
        if prof_vv_item is None:
            logger.warning(f"[SANITIZE] Removing index {i}: prof_vv is None")
            continue
        if not isinstance(prof_vv_item, Mapping):
            logger.warning(
                f"[SANITIZE] Removing index {i}: prof_vv is {type(prof_vv_item).__name__}, "
                f"not a Mapping (dict-like object)"
            )
            continue
        if prof_vv_item.get("transform") is None:
            logger.warning(f"[SANITIZE] Removing index {i}: prof_vv missing 'transform'")
            continue
        if prof_vv_item.get("crs") is None:
            logger.warning(f"[SANITIZE] Removing index {i}: prof_vv missing 'crs'")
            continue

        # Check prof_vh validity
        prof_vh_item = prof_vh[i]
        if prof_vh_item is None:
            logger.warning(f"[SANITIZE] Removing index {i}: prof_vh is None")
            continue
        if not isinstance(prof_vh_item, Mapping):
            logger.warning(
                f"[SANITIZE] Removing index {i}: prof_vh is {type(prof_vh_item).__name__}, "
                f"not a Mapping (dict-like object)"
            )
            continue
        if prof_vh_item.get("transform") is None:
            logger.warning(f"[SANITIZE] Removing index {i}: prof_vh missing 'transform'")
            continue
        if prof_vh_item.get("crs") is None:
            logger.warning(f"[SANITIZE] Removing index {i}: prof_vh missing 'crs'")
            continue

        # All checks passed
        valid_indices.append(i)

    # Report filtering
    n_removed = n_original - len(valid_indices)
    if n_removed > 0:
        logger.warning(
            "[SANITIZE] Filtered out %d/%d scenes with invalid profiles (%.1f%%)",
            n_removed, n_original, 100.0 * n_removed / n_original if n_original else 0
        )
        logger.warning(
            "[SANITIZE] This prevents blockwise fallback and restores performance"
        )
    else:
        logger.info("[SANITIZE] All %d scenes have valid profiles", n_original)

    # Rebuild arrays with valid indices only
    final_vv_clean = [final_vv[i] for i in valid_indices]
    prof_vv_clean = [prof_vv[i] for i in valid_indices]
    final_vh_clean = [final_vh[i] for i in valid_indices]
    prof_vh_clean = [prof_vh[i] for i in valid_indices]
    clean_dates_clean = [clean_dates[i] for i in valid_indices]

    # Sync df_batch if provided
    df_batch_clean = None
    if df_batch is not None and len(df_batch) > 0:
        # Match df_batch rows by clean_dates
        try:
            # Build date->index mapping for original clean_dates
            date_to_orig_idx = {_clean_date_key(dt): i for i, dt in enumerate(clean_dates)}

            # Find df_batch rows matching valid_indices
            valid_df_indices = []
            for _, row in df_batch.iterrows():
                row_date_key = _clean_date_key(row['acq_dt'])
                orig_idx = date_to_orig_idx.get(row_date_key)
                if orig_idx in valid_indices:
                    valid_df_indices.append(row.name)

            if valid_df_indices:
                df_batch_clean = df_batch.loc[valid_df_indices].copy()
                logger.info(
                    "[SANITIZE] Synced df_batch: %d → %d rows",
                    len(df_batch), len(df_batch_clean)
                )
            else:
                logger.warning("[SANITIZE] No matching df_batch rows after filtering")
        except Exception as e:
            logger.warning("[SANITIZE] Failed to sync df_batch: %s", e)

    return final_vv_clean, prof_vv_clean, final_vh_clean, prof_vh_clean, clean_dates_clean, df_batch_clean

def _sanitize_band_arrays(
    final_arr: list,
    prof_arr: list,
    clean_dates: list,
    source_indices: list[int] | None = None,
) -> tuple[list, list, list, list[int]]:
    """Remove invalid single-band array/profile entries while preserving order."""
    n_original = len(final_arr)
    valid_indices: list[int] = []

    for i in range(n_original):
        if i >= len(prof_arr):
            logger.warning("[SANITIZE] Removing band index %d: profile missing", i)
            continue
        if final_arr[i] is None:
            logger.warning("[SANITIZE] Removing band index %d: array is None", i)
            continue
        prof_item = prof_arr[i]
        if prof_item is None:
            logger.warning("[SANITIZE] Removing band index %d: profile is None", i)
            continue
        if not isinstance(prof_item, Mapping):
            logger.warning(
                "[SANITIZE] Removing band index %d: profile is %s, not Mapping",
                i, type(prof_item).__name__,
            )
            continue
        if prof_item.get("transform") is None:
            logger.warning("[SANITIZE] Removing band index %d: profile missing transform", i)
            continue
        if prof_item.get("crs") is None:
            logger.warning("[SANITIZE] Removing band index %d: profile missing crs", i)
            continue
        valid_indices.append(i)

    n_removed = n_original - len(valid_indices)
    if n_removed:
        logger.warning(
            "[SANITIZE] Filtered out %d/%d single-band entries with invalid profiles",
            n_removed, n_original,
        )
    else:
        logger.info("[SANITIZE] All %d single-band entries have valid profiles", n_original)

    src = source_indices if source_indices is not None else list(range(n_original))
    return (
        [final_arr[i] for i in valid_indices],
        [prof_arr[i] for i in valid_indices],
        [clean_dates[i] for i in valid_indices],
        [int(src[i]) for i in valid_indices],
    )
