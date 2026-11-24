"""Opt-in, paired linear-power averaging within one acquisition."""
from __future__ import annotations

import numpy as np

from s1grits.resampling import resolve_resampling_method
from s1grits.scenes.mosaic import _mosaic_align_scene_window


def validate_intra_pass_config(config: dict) -> str:
    """Validate the optional strategy before querying or creating outputs."""
    processing = config.get("processing", {})
    options = processing.get("intra_pass_mosaic", {})
    if not isinstance(options, dict) or set(options) - {"method"}:
        raise ValueError("processing.intra_pass_mosaic accepts only method")
    method = options.get("method", "first_valid_pixel")
    if method not in ("first_valid_pixel", "average"):
        raise ValueError("intra_pass_mosaic.method must be first_valid_pixel or average")
    if method == "first_valid_pixel":
        return method
    resolution = float(processing.get("target_resolution", 30))
    resampling = resolve_resampling_method(resolution, processing.get("resampling_method", "auto"))
    if resolution != 30 or resampling != "nearest":
        raise ValueError("average requires 30 m and nearest resampling")
    if config.get("roi", {}).get("polarization", "VV+VH") != "VV+VH":
        raise ValueError("average requires paired VV+VH")
    if config.get("roi", {}).get("flight_direction") not in ('ASCENDING', 'DESCENDING'):
        raise ValueError("average requires one explicit roi.flight_direction: ASCENDING or DESCENDING")
    if processing.get("spatial_despeckle", False):
        raise ValueError("average currently requires spatial_despeckle=false")
    monthly = processing.get("monthly", {})
    if monthly.get("enabled", False) or monthly.get("only", False):
        raise ValueError("average monthly composites are not implemented; disable processing.monthly")
    static = config.get("static_layers", {})
    if static.get("run_after_scenes", False):
        raise ValueError("average currently excludes static post-processing")
    return method


def average_pair_window(indices, vv, vv_profiles, vh, vh_profiles,
                        height, width, transform, crs, ys, xs,
                        vv_bounds=None, vh_bounds=None):
    """Equal-weight mean of paired valid bursts, in the caller's stable order.

    A contributor has finite positive power in both polarizations. Mapping
    and finite NoData exclusion use the existing exact nearest-neighbor reader.
    """
    shape = (ys.stop - ys.start, xs.stop - xs.start)
    sums_vv = np.zeros(shape, dtype=np.float64)
    sums_vh = np.zeros(shape, dtype=np.float64)
    count = np.zeros(shape, dtype=np.uint32)
    for index in indices:
        a = _mosaic_align_scene_window(
            [index], vv, vv_profiles, height, width, transform, crs, ys, xs, vv_bounds)
        b = _mosaic_align_scene_window(
            [index], vh, vh_profiles, height, width, transform, crs, ys, xs, vh_bounds)
        if a is None or b is None:
            continue
        valid = np.isfinite(a) & np.isfinite(b) & (a > 0) & (b > 0)
        sums_vv[valid] += a[valid]
        sums_vh[valid] += b[valid]
        count[valid] += 1
    out_vv = np.full(shape, np.nan, dtype=np.float32)
    out_vh = np.full(shape, np.nan, dtype=np.float32)
    np.divide(sums_vv, count, out=out_vv, where=count > 0)
    np.divide(sums_vh, count, out=out_vh, where=count > 0)
    return out_vv, out_vh, count
