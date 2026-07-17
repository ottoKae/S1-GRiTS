[← Back to README](../README.md)

## Python API

S1-GRiTS provides a rich Python API for programmatic access to outputs. The `s1grits.analysis` module contains 8 submodules for data loading, time series extraction, visualization, validation, and more.

### API Overview

| Module | Purpose | Key Functions |
|--------|---------|---------------|
| `s1grits.analysis.io` | Data loading | `load_zarr_dataset`, `list_available_tiles` |
| `s1grits.analysis.timeseries` | Time series extraction | `extract_pixel_timeseries`, `lonlat_to_pixel` |
| `s1grits.analysis.plotting` | Visualization | `plot_timeseries_figure`, `plot_orbit_comparison` |
| `s1grits.analysis.catalog` | STAC/Parquet queries | `rebuild_global_catalog`, `validate_catalog` |
| `s1grits.analysis.validation` | Data validation | `validate_cog_file`, `validate_zarr_structure` |
| `s1grits.analysis.reporting` | Coverage reports | `generate_coverage_report`, `analyze_temporal_gaps` |
| `s1grits.analysis.mosaic` | Mosaic creation | `create_mosaic_vrt`, `find_cog_files_for_mosaic` |
| `s1grits.analysis.display_mosaic` | Display enhancement | `create_display_vrt` (per-tile normalization) |

---

### Data Loading (`s1grits.analysis.io`)

Load Zarr data cubes and discover available tiles.

#### `load_zarr_dataset(tile_id, direction, output_dir)`

Load complete Zarr time-series cube for a tile.

**Parameters:**
- `tile_id` (str): MGRS tile identifier (e.g., "17MPV")
- `direction` (str): "ASCENDING" or "DESCENDING"
- `output_dir` (str): Path to output directory

**Returns:** `xarray.Dataset` with dimensions `(time, y, x)`

**Example:**
```python
from s1grits.analysis import load_zarr_dataset

# Load monthly composite Zarr
ds = load_zarr_dataset("17MPV", "DESCENDING", output_dir="./output")

# Explore structure
print(ds)
# Dimensions:  (time: 24, y: 3660, x: 3660)
# Variables:   VV_dB, VH_dB, Ratio, RVI

# Access data
vv_data = ds['VV_dB'].values  # numpy array (time, y, x)
timestamps = ds['time'].values  # datetime64 array
```

#### `list_available_tiles(output_dir)`

Enumerate all available tile/direction combinations.

**Returns:** List of dicts with keys: `tile_id`, `direction`, `zarr_path`

**Example:**
```python
from s1grits.analysis import list_available_tiles

tiles = list_available_tiles("./output")
for tile in tiles:
    print(f"{tile['tile_id']} - {tile['direction']}")
    print(f"  Zarr: {tile['zarr_path']}")
```

#### `get_zarr_info(zarr_path)`

Get metadata without loading full data.

**Returns:** Dict with keys: `dimensions`, `variables`, `chunks`, `size_mb`

**Example:**
```python
from s1grits.analysis import get_zarr_info

info = get_zarr_info("output/17MPV_ASCENDING/zarr/S1_monthly.zarr")
print(f"Dimensions: {info['dimensions']}")
print(f"Size: {info['size_mb']} MB")
```

#### `find_tile_by_lonlat(lon, lat)`

Find which MGRS tile contains a coordinate.

**Parameters:**
- `lon` (float): Longitude (EPSG:4326)
- `lat` (float): Latitude (EPSG:4326)

**Returns:** MGRS tile ID (str) or None

**Example:**
```python
from s1grits.analysis import find_tile_by_lonlat

tile_id = find_tile_by_lonlat(-122.5, 37.8)
print(f"Coordinate is in tile: {tile_id}")
```

---

### Time Series Extraction (`s1grits.analysis.timeseries`)

Extract and analyze time series from Zarr data cubes.

#### `extract_pixel_timeseries(dataset, row, col)`

Extract time series for a single pixel.

**Parameters:**
- `dataset` (xarray.Dataset): Loaded Zarr dataset
- `row` (int): Pixel row index
- `col` (int): Pixel column index

**Returns:** Dict with keys: `vv_ts`, `vh_ts`, `ratio_ts`, `rvi_ts`, `dates`, `valid_count`, `total_count`, `row`, `col`

**Example:**
```python
from s1grits.analysis import load_zarr_dataset, extract_pixel_timeseries

ds = load_zarr_dataset("17MPV", "DESCENDING", "./output")
ts = extract_pixel_timeseries(ds, row=1843, col=1831)

print(f"VV time series: {ts['vv_ts']}")
print(f"Valid observations: {ts['valid_count']}/{ts['total_count']}")
print(f"Dates: {ts['dates']}")
```

#### `extract_region_timeseries(dataset, row_slice, col_slice, aggregation='mean')`

Extract aggregated time series for a region.

**Parameters:**
- `dataset` (xarray.Dataset): Loaded Zarr dataset
- `row_slice` (slice): Row range (e.g., `slice(1800, 1900)`)
- `col_slice` (slice): Column range
- `aggregation` (str): 'mean', 'median', 'std', 'min', 'max'

**Returns:** Dict with aggregated time series

**Example:**
```python
from s1grits.analysis import extract_region_timeseries

# Extract median time series for 100x100 pixel region
ts_region = extract_region_timeseries(
    ds,
    row_slice=slice(1800, 1900),
    col_slice=slice(1800, 1900),
    aggregation='median'
)
```

#### `lonlat_to_pixel(lon, lat, dataset)`

Convert geographic coordinates to pixel indices.

**Parameters:**
- `lon` (float): Longitude (EPSG:4326)
- `lat` (float): Latitude (EPSG:4326)
- `dataset` (xarray.Dataset): Loaded Zarr dataset

**Returns:** Tuple `(row, col)`

**Example:**
```python
from s1grits.analysis import lonlat_to_pixel, extract_pixel_timeseries

# Geographic lookup
row, col = lonlat_to_pixel(-122.5, 37.8, ds)
print(f"Lon/Lat ({-122.5}, {37.8}) -> Pixel ({row}, {col})")

# Extract time series at this location
ts = extract_pixel_timeseries(ds, row, col)
```

#### `compute_time_series_statistics(ts_dict)`

Calculate statistics for extracted time series.

**Parameters:**
- `ts_dict` (dict): Output from `extract_pixel_timeseries()`

**Returns:** Dict with keys: `vv`, `vh`, `ratio`, `rvi` (each containing `mean`, `std`, `min`, `max`, `median`)

**Example:**
```python
from s1grits.analysis import compute_time_series_statistics

stats = compute_time_series_statistics(ts)
print(f"VV mean: {stats['vv']['mean']:.2f} dB")
print(f"VV std: {stats['vv']['std']:.2f} dB")
print(f"VH median: {stats['vh']['median']:.2f} dB")
```

#### `detect_outliers(ts_dict, method='iqr', threshold=1.5)`

Identify anomalous observations.

**Parameters:**
- `ts_dict` (dict): Output from `extract_pixel_timeseries()`
- `method` (str): 'iqr' (interquartile range) or 'zscore'
- `threshold` (float): IQR multiplier (1.5 = mild, 3.0 = extreme) or z-score threshold

**Returns:** Dict with keys: `vv_outlier_mask`, `vh_outlier_mask`, `vv_outlier_count`, `vh_outlier_count`, `outlier_dates`

**Example:**
```python
from s1grits.analysis import detect_outliers

outliers = detect_outliers(ts, method='iqr', threshold=1.5)
print(f"VV outliers: {outliers['vv_outlier_count']}")
print(f"Outlier dates: {outliers['outlier_dates']}")
```

---

### Visualization (`s1grits.analysis.plotting`)

Generate plots and visualizations.

#### `plot_timeseries_figure(ts_dict, title='', output_path=None, figsize=(12,8), show_outliers=True)`

Create 4-panel time series plot (VV, VH, Ratio, RVI).

**Parameters:**
- `ts_dict` (dict): Output from `extract_pixel_timeseries()`
- `title` (str): Figure title
- `output_path` (str): Save path (None = display only)
- `figsize` (tuple): Figure size in inches
- `show_outliers` (bool): Highlight outliers in red

**Example:**
```python
from s1grits.analysis import plot_timeseries_figure

plot_timeseries_figure(
    ts,
    title="Pixel (1843, 1831) Time Series",
    output_path="timeseries.png"
)
```

#### `plot_orbit_comparison(ts_asc, ts_desc, output_path=None)`

Compare ASCENDING vs DESCENDING orbit time series.

**Parameters:**
- `ts_asc` (dict): Time series from ASCENDING orbit
- `ts_desc` (dict): Time series from DESCENDING orbit
- `output_path` (str): Save path

**Example:**
```python
from s1grits.analysis import (
    load_zarr_dataset,
    extract_pixel_timeseries,
    plot_orbit_comparison
)

# Load both orbits
ds_asc = load_zarr_dataset("17MPV", "ASCENDING", "./output")
ds_desc = load_zarr_dataset("17MPV", "DESCENDING", "./output")

# Extract time series at same location
ts_asc = extract_pixel_timeseries(ds_asc, 1843, 1831)
ts_desc = extract_pixel_timeseries(ds_desc, 1843, 1831)

# Compare
plot_orbit_comparison(ts_asc, ts_desc, output_path="orbit_compare.png")
```

#### `plot_monthly_preview(dataset, month, tile_id, direction, variable='Ratio', output_path=None, cmap='viridis', vmin=None, vmax=None)`

Create false-color RGB composite for a specific month.

**Parameters:**
- `dataset` (xarray.Dataset): Loaded Zarr dataset
- `month` (str): Month in 'YYYY-MM' format
- `tile_id` (str): MGRS tile ID
- `direction` (str): "ASCENDING" or "DESCENDING"
- `variable` (str): Variable to visualize
- `output_path` (str): Save path
- `cmap` (str): Matplotlib colormap
- `vmin`, `vmax` (float): Value range (None = auto)

**Example:**
```python
from s1grits.analysis import plot_monthly_preview

plot_monthly_preview(
    ds,
    month="2024-01",
    tile_id="17MPV",
    direction="DESCENDING",
    variable='Ratio',
    output_path="preview_202401.png"
)
```

#### `plot_time_series_heatmap(dataset, variable='VV_dB', row_slice=None, col_slice=None, output_path=None)`

Create space-time heatmap visualization.

**Parameters:**
- `dataset` (xarray.Dataset): Loaded Zarr dataset
- `variable` (str): Variable to visualize
- `row_slice`, `col_slice` (slice): Spatial subset
- `output_path` (str): Save path

**Example:**
```python
from s1grits.analysis import plot_time_series_heatmap

# Visualize VV_dB evolution for a region
plot_time_series_heatmap(
    ds,
    variable='VV_dB',
    row_slice=slice(1800, 1900),
    col_slice=slice(1800, 1900),
    output_path="heatmap.png"
)
```

---

### Catalog & STAC Management (`s1grits.analysis.catalog`)

Query and manage catalogs.

#### STAC Discovery

```python
import pystac

# Load root catalog
cat = pystac.read_dict_to_object("output/catalog.json")

# Browse collections
for collection in cat.get_children():
    print(f"Collection: {collection.id}")
    for item in collection.get_items():
        print(f"  Item: {item.id}")
        # Access assets
        for asset_key, asset in item.assets.items():
            print(f"    {asset_key}: {asset.href}")
```

#### Parquet Fast Queries

```python
import pandas as pd

# Load global catalog
df = pd.read_parquet("output/catalog.parquet")

# Find records by date range
recent = df[(df['datetime'] >= '2026-01-15') & (df['datetime'] <= '2026-01-31')]
print(f"Found {len(recent)} records")

# Filter by tile
tile_17mpv = df[df['mgrs_tile_id'] == '17MPV']
print(f"Tile 17MPV: {tile_17mpv['datetime'].nunique()} unique dates")

# Group by direction
by_direction = df.groupby('direction').size()
print(by_direction)
```

#### `rebuild_global_catalog(output_dir)`

Rebuild catalog from COG metadata.

**Example:**
```python
from s1grits.analysis import rebuild_global_catalog

rebuild_global_catalog("./output")
```

#### `validate_catalog(catalog_df)`

Check catalog integrity.

**Returns:** Dict with validation results

**Example:**
```python
from s1grits.analysis import validate_catalog
import pandas as pd

df = pd.read_parquet("output/catalog.parquet")
results = validate_catalog(df)

if results['valid']:
    print("Catalog is valid")
else:
    print(f"Errors: {results['errors']}")
```

---

### Data Validation (`s1grits.analysis.validation`)

Validate output products.

#### `validate_cog_file(cog_path, verbose=True)`

Validate single COG file.

**Checks:**
- File exists and is readable
- GeoTIFF format with proper tags
- Cloud-optimized structure (internal tiling, overviews)
- Valid CRS and geotransform
- Band count and data types
- Data value ranges (dB values in expected range)

**Returns:** Dict with validation results

**Example:**
```python
from s1grits.analysis import validate_cog_file

result = validate_cog_file(
    "output/17MPV_ASCENDING/cog/17MPV_S1_Monthly_ASCENDING_2024-01.tif",
    verbose=True
)

if result['valid']:
    print("COG is valid")
    print(f"  Bands: {result['band_count']}")
    print(f"  Internal tiling: {result['is_tiled']}")
    print(f"  Overviews: {result['has_overviews']}")
else:
    print(f"Validation failed: {result['errors']}")
```

#### `validate_zarr_structure(zarr_path)`

Validate Zarr dataset structure.

**Checks:**
- Zarr group metadata present
- Expected variables exist (VV_dB, VH_dB, etc.)
- Coordinate variables present (time, y, x)
- Chunk configuration is optimal
- Data types are correct

**Returns:** Dict with validation results

**Example:**
```python
from s1grits.analysis import validate_zarr_structure

result = validate_zarr_structure("output/17MPV_ASCENDING/zarr/S1_monthly.zarr")

if result['valid']:
    print("Zarr structure is valid")
else:
    print(f"Issues: {result['warnings']}")
```

#### `check_data_integrity(path)`

General integrity check for file or directory.

**Example:**
```python
from s1grits.analysis import check_data_integrity

result = check_data_integrity("output/17MPV_ASCENDING/")
print(f"Integrity: {result['status']}")
```

---

### Coverage Reports (`s1grits.analysis.reporting`)

Generate coverage statistics and gap analysis.

#### `generate_coverage_report(output_dir)`

Comprehensive coverage statistics.

**Returns:** Dict with keys: `overall`, `tiles`

**Example:**
```python
from s1grits.analysis import generate_coverage_report

report = generate_coverage_report("./output")

# Overall statistics
print(f"Total tiles: {report['overall']['tile_count']}")
print(f"Date range: {report['overall']['date_range']}")
print(f"Total records: {report['overall']['total_records']}")

# Per-tile statistics
for tile in report['tiles']:
    print(f"{tile['tile_id']} - {tile['completeness']:.1f}% complete")
    print(f"  Records: {tile['record_count']}")
    print(f"  Date range: {tile['date_range']}")
```

#### `analyze_temporal_gaps(catalog_df, tile_id, direction)`

Identify missing months.

**Returns:** Dict with keys: `has_gaps`, `missing_list`, `completeness`, `expected_count`, `actual_count`

**Example:**
```python
from s1grits.analysis import analyze_temporal_gaps, load_catalog

cat = load_catalog("./output")
gaps = analyze_temporal_gaps(cat, tile_id="17MPV", direction="DESCENDING")

if gaps['has_gaps']:
    print(f"Missing months: {gaps['missing_list']}")
    print(f"Completeness: {gaps['completeness']:.1f}%")
else:
    print("No gaps found")
```

#### `get_tile_statistics(catalog_df, tile_id)`

Per-tile statistics.

**Returns:** Dict with tile-level stats

**Example:**
```python
from s1grits.analysis import get_tile_statistics, load_catalog

cat = load_catalog("./output")
stats = get_tile_statistics(cat, "17MPV")

print(f"Total records: {stats['total_records']}")
for direction, dir_stats in stats['directions'].items():
    print(f"  {direction}: {dir_stats['records']} records")
    print(f"    Date range: {dir_stats['date_range']}")
```

---

### Mosaic Creation (`s1grits.analysis.mosaic`)

Create multi-tile mosaics programmatically.

#### `create_mosaic_vrt(cog_list, tile_bounds, output_path, direction='ASCENDING')`

Create virtual mosaic across tiles.

**Parameters:**
- `cog_list` (list): List of COG file paths
- `tile_bounds` (dict): Bounding boxes per tile
- `output_path` (str): Output VRT path
- `direction` (str): Orbit direction

**Example:**
```python
from s1grits.analysis import create_mosaic_vrt

cog_files = [
    "output/17MPV_ASCENDING/cog/17MPV_S1_Monthly_ASCENDING_2024-01.tif",
    "output/17MQV_ASCENDING/cog/17MQV_S1_Monthly_ASCENDING_2024-01.tif"
]

create_mosaic_vrt(
    cog_list=cog_files,
    output_path="mosaic_2024-01.vrt",
    direction="ASCENDING"
)
```

#### `find_cog_files_for_mosaic(tile_ids, month, direction, output_dir)`

Locate COG files for mosaicking.

**Parameters:**
- `tile_ids` (list): List of MGRS tile IDs
- `month` (str): Month in 'YYYY-MM' format
- `direction` (str): "ASCENDING" or "DESCENDING"
- `output_dir` (str): Base output directory

**Returns:** List of COG file paths

**Example:**
```python
from s1grits.analysis import find_cog_files_for_mosaic

cog_files = find_cog_files_for_mosaic(
    tile_ids=["17MPV", "17MQV", "17MPT"],
    month="2024-01",
    direction="ASCENDING",
    output_dir="./output"
)

print(f"Found {len(cog_files)} COG files")
```

#### `validate_mosaic_inputs(cog_list)`

Validate input COG files for mosaicking.

**Checks:**
- All files exist
- Same flight direction
- Same month
- Compatible CRS and resolution

**Returns:** Dict with validation results

---

### Display Enhancement (`s1grits.analysis.display_mosaic`)

Per-tile histogram normalization for visualization.

#### `create_display_vrt(data_mosaic_vrt, output_path, percentile_min=2, percentile_max=98)`

Create display-optimized VRT with per-tile percentile stretching.

**Purpose:** Normalize tiles individually for better visualization without modifying analysis data.

**Parameters:**
- `data_mosaic_vrt` (str): Path to data mosaic VRT
- `output_path` (str): Output display VRT path
- `percentile_min` (float): Lower percentile (default: 2)
- `percentile_max` (float): Upper percentile (default: 98)

**Example:**
```python
from s1grits.analysis import create_display_vrt

# Create data mosaic first
create_mosaic_vrt(cog_files, output_path="data_mosaic.vrt")

# Create display-enhanced version
create_display_vrt(
    "data_mosaic.vrt",
    "display_mosaic.vrt",
    percentile_min=2,
    percentile_max=98
)
```

---


---

## Positive-Unlabeled Learning (`s1grits.analysis.pu_learning`)

Train a binary classifier when you have reliable POSITIVE points (field-
verified deforestation, flood, crop parcels) but no trustworthy negatives —
only the unlabeled remainder of the scene. Implements Elkan & Noto (KDD
2008): a labeled-vs-unlabeled classifier `g(x)` estimates `c * p(y=1|x)`
under the SCAR assumption, and the label frequency `c` is recovered from a
positive hold-out, so calibrated probabilities follow. Requires the `ml`
extra (`pip install "s1grits[ml]"`).

```python
from s1grits.ml_loader import load_timeseries
from s1grits.analysis import PUClassifier, pu_training_set, predict_proba_map

cube = load_timeseries(root, collection="s1grits-smonthly", tile="17MPV")

# positive_mask: 2-D bool (y, x) — your verified positive pixels
X, s, meta = pu_training_set(
    cube, positive_mask,
    reducers=("mean", "std"),        # temporal features per band
    unlabeled_per_positive=25,       # subsample the unlabeled ocean of pixels
    random_state=0,
)

clf = PUClassifier(random_state=0).fit(X, s)
print(clf.c_)      # estimated label frequency p(s=1 | y=1)
print(clf.prior_)  # estimated class prior p(y=1)

prob = predict_proba_map(clf, cube, meta)   # (y, x) calibrated p(y=1), NaN outside
```

- `PUClassifier(method="weighted")` uses the paper's weighted refit
  (requires a base estimator accepting `sample_weight`); pass any
  scikit-learn-style classifier as `base_estimator`, or a known label
  frequency via `c=`.
- The e1 estimator assumes positives are confidently positive (near-1 true
  posterior); with heavy class overlap it underestimates `c` — supply a
  domain-derived `c=` in that regime.
