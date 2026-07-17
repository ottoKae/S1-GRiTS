"""
Analysis subpackage for S1 monthly mosaics

Provides tools for analyzing Zarr/COG outputs from s1grits processing workflow.

Modules:
- io: Load and read datasets
- pu_learning: Positive-Unlabeled classification (Elkan-Noto)
- timeseries: Extract and analyze time series
- plotting: Visualization tools
- mosaic: Mosaic creation
- catalog: Catalog management
- validation: Data validation
- reporting: Coverage reporting

Example:
    >>> from s1grits.analysis import load_zarr_dataset, extract_pixel_timeseries
    >>> from s1grits.analysis import plot_timeseries_figure
    >>> from s1grits.analysis import rebuild_global_catalog, generate_coverage_report
    >>>
    >>> ds = load_zarr_dataset("17MPV", "DESCENDING")
    >>> ts = extract_pixel_timeseries(ds, 1843, 1831)
    >>> plot_timeseries_figure(ts, output_path="ts.png")
"""

from .io import (
    load_zarr_dataset,
    load_catalog,
    list_available_tiles,
)

from .timeseries import (
    extract_pixel_timeseries,
    extract_region_timeseries,
    lonlat_to_pixel,
    compute_time_series_statistics,
)

from .plotting import (
    plot_timeseries_figure,
    plot_orbit_comparison,
    plot_monthly_preview,
)

from .mosaic import (
    create_mosaic_vrt,
    find_cog_files_for_mosaic,
    validate_mosaic_inputs,
    generate_mosaic_filename,
    fix_vrt_paths,
)

from .catalog import (
    rebuild_global_catalog,
    validate_catalog,
    get_catalog_statistics,
)
from s1grits.canonical_catalog_schema import (
    CANONICAL_CATALOG_COLUMNS,
    SCHEMA_VERSION,
    normalize_catalog_record,
    filter_complete_records,
)

from .validation import (
    validate_cog_file,
    validate_zarr_structure,
    check_data_integrity,
)

from .reporting import (
    generate_coverage_report,
    analyze_temporal_gaps,
    get_tile_statistics,
)

__all__ = [
    # IO
    "load_zarr_dataset",
    "load_catalog",
    "list_available_tiles",

    # Time series
    "extract_pixel_timeseries",
    "extract_region_timeseries",
    "lonlat_to_pixel",
    "compute_time_series_statistics",

    # Plotting
    "plot_timeseries_figure",
    "plot_orbit_comparison",
    "plot_monthly_preview",

    # Mosaic
    "create_mosaic_vrt",
    "find_cog_files_for_mosaic",
    "validate_mosaic_inputs",
    "generate_mosaic_filename",
    "fix_vrt_paths",

    # Catalog
    "rebuild_global_catalog",
    "validate_catalog",
    "get_catalog_statistics",

    # Validation
    "validate_cog_file",
    "validate_zarr_structure",
    "check_data_integrity",

    # Reporting
    "generate_coverage_report",
    "analyze_temporal_gaps",
    "get_tile_statistics",
]

from .pu_learning import (
    PUClassifier,
    pu_training_set,
    predict_proba_map,
)
