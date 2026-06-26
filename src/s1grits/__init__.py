"""
S1-GRiTS: Sentinel-1 Monthly Grid Time Series Processor

Generate monthly COG/Zarr time series from ASF RTC products with
MGRS tile-based processing and intelligent memory management.

Subpackages:
- analysis: Analysis tools for Zarr/COG outputs (time series, plotting, statistics)
"""

from s1grits.__version__ import __version__, __version_info__

# Lazy imports to avoid circular dependencies
__all__ = [
    "__version__",
    "__version_info__",
    "analysis",  # Expose analysis subpackage
    "run_multi_mgrs_monthly_workflow",
]

# Main processing functions will be imported on demand
def __getattr__(name):
    """Lazy import for main processing functions"""
    if name == "run_multi_mgrs_monthly_workflow":
        from s1grits.workflow import run_multi_mgrs_monthly_workflow
        return run_multi_mgrs_monthly_workflow
    elif name == "build_s1_monthly_cog_and_zarr_crossUTM":
        from s1grits.asf_output_writing import build_s1_monthly_cog_and_zarr_crossUTM
        return build_s1_monthly_cog_and_zarr_crossUTM
    elif name == "get_rtc_s1_ts_metadata_from_mgrs_tiles":
        from s1grits.asf_tiles import get_rtc_s1_ts_metadata_from_mgrs_tiles
        return get_rtc_s1_ts_metadata_from_mgrs_tiles
    elif name == "get_mgrs_tiles_overlapping_geometry":
        from s1grits.mgrs_burst_data import get_mgrs_tiles_overlapping_geometry
        return get_mgrs_tiles_overlapping_geometry
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
