"""Known-key validation for workflow YAML configs (warn-only).

The workflows read config values with ``dict.get(..., default)``, so a
misspelled or misplaced key is silently ignored and the default silently
wins — the worst kind of config bug (e.g. ``processing.on_time_conflict``
looks plausible but the code only reads ``output.on_time_conflict``).

``warn_unknown_config_keys`` walks the loaded config against a whitelist of
keys the code actually reads and logs a warning for anything unrecognised,
with a "did you mean" hint for known relocations. It never raises and never
mutates the config, so it cannot change workflow behaviour — it only makes
silent typos loud.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Keys the scenes/monthly/static workflows actually read, mirrored from the
# config.get(...) call sites. A dict value means "recurse"; ``None`` means
# "leaf or free-form subtree" (despeckle.kwargs etc. are passed through).
KNOWN_KEYS: dict = {
    "workflow": None,
    "metadata": {"product_config": None},
    "roi": {
        "wkt": None,
        "manual_mgrs_tiles": None,
        "flight_direction": None,
        "polarization": None,
        "min_tile_coverage_frac": None,
    },
    "time": {
        "full": None,
        "years": None,
        "months": None,
        # legacy verbose forms still accepted by time_utils
        "mode": None,
        "full_mode": None,
        "years_mode": None,
    },
    "time_range": None,  # legacy alias for time (time_utils)
    "output": {
        "base_dir": None,
        "overwrite": None,
        "on_time_conflict": None,
        "disk_warn_gb": None,
        "formats": {"cog": None, "preview": None},
        "layout_mode": None,  # legacy monthly workflow only
    },
    "query": {
        "cmr_timeout_seconds": None,
        "max_retries": None,
        "retry_backoff_seconds": None,
        "max_workers": None,
        "footprint_lookback_months": None,
        "footprint_cache_ttl_days": None,
    },
    "parallel": {"enabled": None, "max_workers": None},
    "memory": {
        "max_memory_gb": None,
        "batch_strategy": None,
        "max_download_workers": None,
        "burst_cache_dir": None,
        "clear_cache_per_batch": None,
        "batch_max_retries": None,
        "scene_max_retries": None,
        "max_failed_ratio": None,
        "scene_retry_timeout_seconds": None,
    },
    "runtime": {
        "enabled": None,
        "gdal_cachemax_mb": None,
        "gdal_num_threads": None,
        "omp_num_threads": None,
        "openblas_num_threads": None,
        "mkl_num_threads": None,
        "blis_num_threads": None,
        "veclib_maximum_threads": None,
        "numexpr_num_threads": None,
    },
    "processing": {
        "spatial_despeckle": None,
        "features_ratio": None,
        "features_rvi": None,
        "features_glcm": None,
        "tile_clip": None,
        "target_resolution": None,
        "zarr_chunks": {"y": None, "x": None},
        "cog_block_size": None,
        "despeckle": None,  # method + free-form kwargs
        "incomplete_acquisition": None,
        "interior_hole_max_frac": None,
        "require_complete_bursts": None,
        # Legacy monthly/static workflows only (workflow.py / workflow_static.py):
        "texture_features": None,  # free-form GLCM config subtree
        "target_crs": None,        # static workflow; scenes auto-derives from MGRS
        "use_roi_mask": None,
        "group_mode": None,
        "trim_fraction": None,
        "min_valid_lin": None,
        "eps_lin": None,
        "monthly": {
            "enabled": None,
            "only": None,
            "composite_method": None,
            "generate_cog": None,
            "generate_preview": None,
            "blockwise_threads": None,
            "trim_fraction": None,
        },
    },
    "logging": {
        "file_level": None,
        "console_level": None,
        "suppress_third_party": None,
        "log_file": None,
    },
}

# Keys users plausibly place in the wrong section (or that moved), mapped to
# the location the code actually reads. Emitted as a targeted hint instead of
# a generic "unknown key" warning.
MOVED_KEYS: dict[str, str] = {
    "processing.on_time_conflict": "output.on_time_conflict",
    "processing.overwrite": "output.overwrite",
    "output.max_workers": "parallel.max_workers",
    "parallel.blockwise_threads": "processing.monthly.blockwise_threads",
    "memory.disk_warn_gb": "output.disk_warn_gb",
}


def _walk(cfg: dict, schema: dict, prefix: str, problems: list[str]) -> None:
    for key, val in cfg.items():
        path = f"{prefix}{key}"
        if key not in schema:
            hint = MOVED_KEYS.get(path)
            if hint:
                problems.append(
                    f"'{path}' is IGNORED by the workflow — the code reads "
                    f"'{hint}' instead; move the value there"
                )
            else:
                problems.append(
                    f"'{path}' is not a recognised option and will be ignored"
                )
            continue
        sub = schema[key]
        if isinstance(sub, dict) and isinstance(val, dict):
            _walk(val, sub, f"{path}.", problems)


def find_unknown_config_keys(config: dict) -> list[str]:
    """Return warning strings for keys the workflows never read."""
    problems: list[str] = []
    if isinstance(config, dict):
        _walk(config, KNOWN_KEYS, "", problems)
    return problems


def warn_unknown_config_keys(config: dict, log: logging.Logger | None = None) -> list[str]:
    """Log (never raise) a warning per unknown/misplaced config key."""
    log = log or logger
    problems = find_unknown_config_keys(config)
    for p in problems:
        log.warning("[Config] %s", p)
    return problems
