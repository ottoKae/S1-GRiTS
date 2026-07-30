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
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Keys the scenes/monthly/static workflows actually read, mirrored from the
# config.get(...) call sites. A dict value means "recurse"; ``None`` means
# "leaf or free-form subtree" (despeckle.kwargs etc. are passed through).
KNOWN_KEYS: dict = {
    "workflow": None,
    "metadata": {
        "product_config": None,  # optional registry-overlay YAML path
        "products": None,        # inline product overlay (free-form subtree)
    },
    "roi": {
        "wkt": None,
        "manual_mgrs_tiles": None,
        "flight_direction": None,
        "polarization": None,
        "min_tile_coverage_frac": None,
        "track_numbers": None,
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
        # v3 policy keys (scenes workflow)
        "existing_store": None,   # resume | rebuild-incompatible
        "existing_month": None,   # skip | overwrite
        # v2 policy keys (deprecated in the scenes workflow; still native to
        # the legacy monthly/static workflows)
        "overwrite": None,
        "on_time_conflict": None,
        "disk_warn_gb": None,     # deprecated in favor of preflight.disk
        "formats": {"cog": None, "preview": None, "zarr": None},
        "layout_mode": None,  # legacy monthly workflow only
    },
    "preflight": {
        "disk": {"mode": None, "min_free_gb": None},
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
        "download_prefetch": None,
        "batch_spill": None,
        "spill_dir": None,
        "windowed_burst_reads": None,
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
        # Static workflow + legacy read-compat keys (workflow_static.py):
        "texture_features": None,  # free-form GLCM config subtree
        "target_crs": None,        # static workflow; scenes auto-derives from MGRS
        "use_roi_mask": None,
        "trim_fraction": None,
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
    "static_layers": {
        "enabled": None,
        "layers": None,
        "grid_reference": None,
        "reference_product_label": None,
        "target_resolution": None,
        "zarr_chunks": {"y": None, "x": None},
        "cog_block_size": None,
        "query_batch_size": None,
        "query_max_results": None,
        "query_max_retries": None,
        "query_retry_base_delay": None,
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


# ---------------------------------------------------------------------------
# Output policies (v3): store-level vs month-level, explicitly separated.
#
# v2 exposed `output.overwrite` (bool) and `output.on_time_conflict`, which
# read as overlapping/contradictory ("overwrite: true" + "skip"?!). v3 names
# the two independent levels:
#
#   existing_store: what to do with a WHOLE existing Zarr store
#       "resume"               (default) adopt its locked grid and append;
#                              fail with recovery options if incompatible
#       "rebuild-incompatible" delete + re-create a store whose grid/bands
#                              are incompatible with this run (compatible
#                              stores are still resumed)
#   existing_month: what to do when a month ALREADY EXISTS in a
#                   (compatible) store
#       "skip"      (default) keep the existing composite
#       "overwrite" delete that month's time step and rewrite it
#
# v2 keys are still accepted with a deprecation warning; explicit v3 keys win.
# ---------------------------------------------------------------------------
EXISTING_STORE_VALUES = ("resume", "rebuild-incompatible")
EXISTING_MONTH_VALUES = ("skip", "overwrite")


@dataclass
class OutputPolicies:
    """Resolved store-level + month-level output policies."""
    existing_store: str = "resume"
    existing_month: str = "skip"
    deprecations: list[str] = field(default_factory=list)

    @property
    def rebuild_on_mismatch(self) -> bool:
        return self.existing_store == "rebuild-incompatible"


def resolve_output_policies(
    config: dict, log: logging.Logger | None = None
) -> OutputPolicies:
    """Resolve v3 (preferred) or v2 (deprecated) output policy keys.

    Raises ``ValueError`` on an invalid value — a typo like ``"Skip"`` must
    fail at startup, not silently select the other branch mid-run.
    """
    out = (config or {}).get("output", {}) or {}
    pol = OutputPolicies()

    if "existing_store" in out:
        pol.existing_store = str(out["existing_store"]).lower()
        if "overwrite" in out:
            pol.deprecations.append(
                "output.overwrite is ignored because output.existing_store "
                "is set (v3 key wins)"
            )
    elif "overwrite" in out:
        pol.existing_store = (
            "rebuild-incompatible" if bool(out["overwrite"]) else "resume"
        )
        pol.deprecations.append(
            "output.overwrite is deprecated; use "
            f"output.existing_store: {pol.existing_store!r}"
        )

    if "existing_month" in out:
        pol.existing_month = str(out["existing_month"]).lower()
        if "on_time_conflict" in out:
            pol.deprecations.append(
                "output.on_time_conflict is ignored because "
                "output.existing_month is set (v3 key wins)"
            )
    elif "on_time_conflict" in out:
        pol.existing_month = str(out["on_time_conflict"]).lower()
        pol.deprecations.append(
            "output.on_time_conflict is deprecated; use "
            f"output.existing_month: {pol.existing_month!r}"
        )

    if pol.existing_store not in EXISTING_STORE_VALUES:
        raise ValueError(
            f"output.existing_store={pol.existing_store!r} is invalid; "
            f"expected one of {EXISTING_STORE_VALUES}"
        )
    if pol.existing_month not in EXISTING_MONTH_VALUES:
        raise ValueError(
            f"output.existing_month={pol.existing_month!r} is invalid; "
            f"expected one of {EXISTING_MONTH_VALUES}"
        )

    if log is not None:
        for d in pol.deprecations:
            log.warning("[Config] %s", d)
    return pol
