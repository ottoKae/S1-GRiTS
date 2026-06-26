"""
Lightweight config-override helpers shared by every workflow entry point.

Kept free of the heavy processing imports (cv2, asf_io, …) so the override /
STAC-switch logic can be imported and unit-tested on its own.
"""
from __future__ import annotations

from typing import Any


def deep_merge_config(base: dict, overrides: dict | None) -> dict:
    """Recursively merge ``overrides`` into ``base`` (overrides win). Returns a
    new dict; ``base`` is not mutated. Used to apply CLI overrides (e.g.
    --zarr-only, --no-stac) onto the loaded YAML config."""
    if not overrides:
        return base
    out = dict(base)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge_config(out[k], v)
        else:
            out[k] = v
    return out


def apply_output_overrides_and_stac(config: dict, overrides: dict | None) -> dict[str, Any]:
    """Merge CLI output overrides into the config and disable STAC output.

    Workflows NEVER write STAC: data production (scenes / monthly / static) only
    writes catalog.parquet + data (Zarr/COG/Preview). STAC is produced solely by
    ``catalog resync`` (stac-geoparquet by default). This keeps a single,
    consistent STAC representation and avoids per-worker inconsistencies.

    Returns the merged config. Every workflow entry point calls this so that
    --zarr-only behaves identically across workflows.
    """
    from s1grits.stac_builder import set_stac_output_enabled
    config = deep_merge_config(config, overrides)
    set_stac_output_enabled(False)
    return config
