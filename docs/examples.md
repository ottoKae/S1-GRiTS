[← Back to README](../README.md)

## Usage Examples

Practical code examples for common S1-GRiTS workflows.

### Example 1: Load and Visualize Monthly Time Series

```python
import xarray as xr
import matplotlib.pyplot as plt

# Load monthly composite Zarr
ds = xr.open_zarr("output/17MPV_DESCENDING/zarr/S1_monthly.zarr")

# Compute temporal mean
ratio_mean = ds['Ratio'].mean(dim='time')

# Visualize
plt.figure(figsize=(10, 8))
ratio_mean.plot(cmap='RdYlGn', vmin=0.1, vmax=0.3)
plt.title("Mean VH/VV Ratio (2024)")
plt.xlabel("X (pixels)")
plt.ylabel("Y (pixels)")
plt.savefig("ratio_mean_2024.png", dpi=300, bbox_inches='tight')
```

---

### Example 2: Extract and Plot Pixel Time Series

```python
from s1grits.analysis import (
    load_zarr_dataset,
    extract_pixel_timeseries,
    lonlat_to_pixel,
    plot_timeseries_figure
)

# Load data
ds = load_zarr_dataset("17MPV", "DESCENDING", output_dir="./output")

# Convert geographic coordinate to pixel
lon, lat = -122.5, 37.8
row, col = lonlat_to_pixel(lon, lat, ds)

# Extract time series
ts = extract_pixel_timeseries(ds, row, col)

# Plot 4-panel figure (VV, VH, Ratio, RVI)
plot_timeseries_figure(
    ts,
    title=f"Time Series at ({lon:.2f}, {lat:.2f})",
    output_path="timeseries_pixel.png"
)

# Print statistics
from s1grits.analysis import compute_time_series_statistics
stats = compute_time_series_statistics(ts)
print(f"VV mean: {stats['vv']['mean']:.2f} dB (σ={stats['vv']['std']:.2f})")
print(f"VH mean: {stats['vh']['mean']:.2f} dB (σ={stats['vh']['std']:.2f})")
```

---

### Example 3: Query Catalog for Specific Date Range

```python
import pandas as pd

# Load global catalog
df = pd.read_parquet("output/catalog.parquet")

# Query: All data from January 2024
jan_2024 = df[(df['datetime'] >= '2024-01-01') & (df['datetime'] < '2024-02-01')]

print(f"Found {len(jan_2024)} records for January 2024")
print(f"Tiles: {jan_2024['mgrs_tile_id'].unique()}")
print(f"Directions: {jan_2024['direction'].unique()}")

# Query: ASCENDING data for tile 17MPV
tile_data = df[(df['mgrs_tile_id'] == '17MPV') & (df['direction'] == 'ASCENDING')]

print(f"\nTile 17MPV ASCENDING:")
print(f"  Records: {len(tile_data)}")
print(f"  Date range: {tile_data['datetime'].min()} to {tile_data['datetime'].max()}")
print(f"  Unique months: {tile_data['datetime'].nunique()}")

# Save filtered results
jan_2024.to_csv("january_2024_inventory.csv", index=False)
```

---

### Example 4: Create False-Color Composite from Zarr

```python
import xarray as xr
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize

# Load Zarr
ds = xr.open_zarr("output/17MPV_DESCENDING/zarr/S1_monthly.zarr")

# Select specific month
month_data = ds.sel(time='2024-06-01')

# Extract bands
vv = month_data['VV_dB'].values
vh = month_data['VH_dB'].values
ratio = month_data['Ratio'].values

# Histogram stretch (2-98 percentile)
def percentile_stretch(data, pmin=2, pmax=98):
    vmin, vmax = np.nanpercentile(data, [pmin, pmax])
    return np.clip((data - vmin) / (vmax - vmin), 0, 1)

vv_norm = percentile_stretch(vv)
vh_norm = percentile_stretch(vh)
ratio_norm = percentile_stretch(ratio)

# Create RGB composite (R=VV, G=VH, B=Ratio)
rgb = np.dstack([vv_norm, vh_norm, ratio_norm])

# Visualize
plt.figure(figsize=(12, 10))
plt.imshow(rgb)
plt.title("False-Color Composite (R=VV, G=VH, B=Ratio) - June 2024")
plt.axis('off')
plt.savefig("false_color_composite_202406.png", dpi=300, bbox_inches='tight')
```

---

### Example 5: Multi-Tile Regional Mosaic

```python
from s1grits.analysis import find_cog_files_for_mosaic, create_mosaic_vrt

# Define region of interest (multiple tiles)
tile_ids = ["17MPV", "17MQV", "17MPT", "17MQU"]
month = "2024-06"
direction = "ASCENDING"

# Find COG files
cog_files = find_cog_files_for_mosaic(
    tile_ids=tile_ids,
    month=month,
    direction=direction,
    output_dir="./output"
)

print(f"Found {len(cog_files)} COG files for mosaic")

# Create virtual mosaic
create_mosaic_vrt(
    cog_list=cog_files,
    output_path=f"mosaic_{month}_{direction}.vrt",
    direction=direction
)

print(f"Mosaic created: mosaic_{month}_{direction}.vrt")

# Load and visualize with rasterio
import rasterio
from rasterio.plot import show

with rasterio.open(f"mosaic_{month}_{direction}.vrt") as src:
    # Read VH band (band 2)
    vh_data = src.read(2)
    
    # Visualize
    import matplotlib.pyplot as plt
    plt.figure(figsize=(15, 12))
    show(src, band=2, cmap='gray', title=f"Regional Mosaic - VH ({month})")
    plt.savefig(f"regional_mosaic_{month}.png", dpi=300, bbox_inches='tight')
```

---

### Example 6: Generate Coverage Report

```python
from s1grits.analysis import generate_coverage_report

# Generate comprehensive coverage report
report = generate_coverage_report("./output")

# Overall statistics
print("=== OVERALL COVERAGE ===")
print(f"Total tiles: {report['overall']['tile_count']}")
print(f"Total records: {report['overall']['total_records']}")
print(f"Date range: {report['overall']['date_range']}")
print(f"Average completeness: {report['overall']['avg_completeness']:.1f}%")

# Per-tile details
print("\n=== PER-TILE COVERAGE ===")
for tile in report['tiles']:
    status = "✓ COMPLETE" if tile['completeness'] == 100.0 else "⚠ GAPS"
    print(f"{tile['tile_id']} ({tile['direction']}): {tile['completeness']:.1f}% {status}")
    print(f"  Records: {tile['record_count']}")
    print(f"  Range: {tile['date_range']}")
    if tile['completeness'] < 100.0:
        print(f"  Missing: {tile['missing_count']} months")

# Save report to JSON
import json
with open("coverage_report.json", 'w') as f:
    json.dump(report, f, indent=2, default=str)
```

---

### Example 7: Compare ASCENDING vs DESCENDING Orbits

```python
from s1grits.analysis import (
    load_zarr_dataset,
    extract_pixel_timeseries,
    plot_orbit_comparison
)
import matplotlib.pyplot as plt
import numpy as np

# Load both orbit directions
ds_asc = load_zarr_dataset("17MPV", "ASCENDING", "./output")
ds_desc = load_zarr_dataset("17MPV", "DESCENDING", "./output")

# Extract time series at same location
row, col = 1843, 1831
ts_asc = extract_pixel_timeseries(ds_asc, row, col)
ts_desc = extract_pixel_timeseries(ds_desc, row, col)

# Plot comparison
plot_orbit_comparison(ts_asc, ts_desc, output_path="orbit_comparison.png")

# Statistical comparison
from s1grits.analysis import compute_time_series_statistics

stats_asc = compute_time_series_statistics(ts_asc)
stats_desc = compute_time_series_statistics(ts_desc)

print("=== ORBIT COMPARISON ===")
print(f"ASCENDING VV: {stats_asc['vv']['mean']:.2f} ± {stats_asc['vv']['std']:.2f} dB")
print(f"DESCENDING VV: {stats_desc['vv']['mean']:.2f} ± {stats_desc['vv']['std']:.2f} dB")
print(f"Difference: {stats_asc['vv']['mean'] - stats_desc['vv']['mean']:.2f} dB")
```

---

### Example 8: Temporal Gap Analysis

```python
from s1grits.analysis import analyze_temporal_gaps
import pandas as pd

# Load catalog
df = pd.read_parquet("output/catalog.parquet")

# Analyze gaps for specific tile
tile_id = "17MPV"
direction = "ASCENDING"

gaps = analyze_temporal_gaps(df, tile_id=tile_id, direction=direction)

print(f"=== TEMPORAL GAP ANALYSIS: {tile_id} {direction} ===")
print(f"Completeness: {gaps['completeness']:.1f}%")
print(f"Expected months: {gaps['expected_count']}")
print(f"Actual months: {gaps['actual_count']}")

if gaps['has_gaps']:
    print(f"\nMissing months ({len(gaps['missing_list'])}):")
    for month in gaps['missing_list']:
        print(f"  - {month}")
else:
    print("\n✓ No gaps found - complete time series")

# Export gap report
gap_report = {
    'tile_id': tile_id,
    'direction': direction,
    'completeness': gaps['completeness'],
    'missing_months': gaps['missing_list']
}

import json
with open(f"gap_report_{tile_id}_{direction}.json", 'w') as f:
    json.dump(gap_report, f, indent=2)
```

---

### Example 9: Batch Process Multiple Tiles

```python
from s1grits.analysis import load_zarr_dataset, extract_pixel_timeseries
import pandas as pd

# Define tiles and locations to process
tiles = [
    {"tile_id": "17MPV", "direction": "ASCENDING", "row": 1843, "col": 1831},
    {"tile_id": "17MQV", "direction": "DESCENDING", "row": 2000, "col": 1500},
    {"tile_id": "50RKV", "direction": "ASCENDING", "row": 1200, "col": 1800}
]

results = []

for tile in tiles:
    try:
        # Load data
        ds = load_zarr_dataset(
            tile["tile_id"],
            tile["direction"],
            output_dir="./output"
        )
        
        # Extract time series
        ts = extract_pixel_timeseries(ds, tile["row"], tile["col"])
        
        # Compute statistics
        from s1grits.analysis import compute_time_series_statistics
        stats = compute_time_series_statistics(ts)
        
        # Store results
        results.append({
            'tile_id': tile["tile_id"],
            'direction': tile["direction"],
            'row': tile["row"],
            'col': tile["col"],
            'vv_mean': stats['vv']['mean'],
            'vv_std': stats['vv']['std'],
            'vh_mean': stats['vh']['mean'],
            'vh_std': stats['vh']['std'],
            'valid_obs': ts['valid_count'],
            'total_obs': ts['total_count']
        })
        
        print(f"✓ Processed {tile['tile_id']} {tile['direction']}")
        
    except Exception as e:
        print(f"✗ Failed {tile['tile_id']} {tile['direction']}: {e}")

# Convert to DataFrame and save
df_results = pd.DataFrame(results)
df_results.to_csv("batch_timeseries_results.csv", index=False)

print(f"\n=== BATCH PROCESSING COMPLETE ===")
print(df_results)
```

---

### Example 10: Validate All Outputs

```python
from s1grits.analysis import validate_cog_file, validate_zarr_structure
import pandas as pd
from pathlib import Path

# Load catalog
df = pd.read_parquet("output/catalog.parquet")

validation_results = []

# Validate all COG files
print("Validating COG files...")
for idx, row in df.iterrows():
    cog_path = row['cog_path']
    if pd.notna(cog_path) and Path(cog_path).exists():
        result = validate_cog_file(cog_path, verbose=False)
        validation_results.append({
            'file': cog_path,
            'type': 'COG',
            'valid': result['valid'],
            'errors': result.get('errors', [])
        })

# Validate all Zarr stores
print("Validating Zarr stores...")
zarr_paths = df['zarr_path'].dropna().unique()
for zarr_path in zarr_paths:
    if Path(zarr_path).exists():
        result = validate_zarr_structure(zarr_path)
        validation_results.append({
            'file': zarr_path,
            'type': 'Zarr',
            'valid': result['valid'],
            'errors': result.get('errors', [])
        })

# Summary
df_validation = pd.DataFrame(validation_results)
valid_count = df_validation['valid'].sum()
total_count = len(df_validation)

print(f"\n=== VALIDATION SUMMARY ===")
print(f"Total files checked: {total_count}")
print(f"Valid: {valid_count}")
print(f"Invalid: {total_count - valid_count}")

# Show failures
if total_count > valid_count:
    print("\nFailed files:")
    failed = df_validation[~df_validation['valid']]
    for idx, row in failed.iterrows():
        print(f"  {row['type']}: {row['file']}")
        print(f"    Errors: {row['errors']}")

# Save validation report
df_validation.to_csv("validation_report.csv", index=False)
```

---

### Example 11: Export Time Series to CSV

```python
from s1grits.analysis import load_zarr_dataset, extract_pixel_timeseries
import pandas as pd

# Load data
ds = load_zarr_dataset("17MPV", "DESCENDING", "./output")

# Define locations of interest
locations = [
    {"name": "Site A", "row": 1843, "col": 1831},
    {"name": "Site B", "row": 2000, "col": 1500},
    {"name": "Site C", "row": 1200, "col": 1800}
]

# Extract time series for all locations
all_data = []

for loc in locations:
    ts = extract_pixel_timeseries(ds, loc["row"], loc["col"])
    
    # Convert to DataFrame
    for i, date in enumerate(ts['dates']):
        all_data.append({
            'site': loc['name'],
            'row': loc['row'],
            'col': loc['col'],
            'date': date,
            'vv_db': ts['vv_ts'][i] if i < len(ts['vv_ts']) else None,
            'vh_db': ts['vh_ts'][i] if i < len(ts['vh_ts']) else None,
            'ratio': ts['ratio_ts'][i] if i < len(ts['ratio_ts']) else None,
            'rvi': ts['rvi_ts'][i] if i < len(ts['rvi_ts']) else None
        })

# Create DataFrame and save
df_export = pd.DataFrame(all_data)
df_export.to_csv("timeseries_export.csv", index=False)

print(f"Exported {len(all_data)} time series observations")
print(f"Sites: {len(locations)}")
print(f"Date range: {df_export['date'].min()} to {df_export['date'].max()}")
```

---

### Example 12: Interactive Visualization with Jupyter

```python
# Run in Jupyter Notebook with ipywidgets
import xarray as xr
import matplotlib.pyplot as plt
import ipywidgets as widgets
from IPython.display import display

# Load data
ds = xr.open_zarr("output/17MPV_DESCENDING/zarr/S1_monthly.zarr")

# Create interactive time slider
def plot_month(time_index):
    month_data = ds.isel(time=time_index)
    
    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    
    # VV
    month_data['VV_dB'].plot(ax=axes[0], cmap='gray', vmin=-25, vmax=5)
    axes[0].set_title(f"VV (dB) - {month_data['time'].values}")
    
    # VH
    month_data['VH_dB'].plot(ax=axes[1], cmap='gray', vmin=-32, vmax=-5)
    axes[1].set_title(f"VH (dB) - {month_data['time'].values}")
    
    # Ratio
    month_data['Ratio'].plot(ax=axes[2], cmap='RdYlGn', vmin=0.1, vmax=0.3)
    axes[2].set_title(f"Ratio (VH/VV) - {month_data['time'].values}")
    
    plt.tight_layout()
    plt.show()

# Create slider widget
time_slider = widgets.IntSlider(
    value=0,
    min=0,
    max=len(ds['time']) - 1,
    step=1,
    description='Month:',
    continuous_update=False
)

# Interactive plot
widgets.interact(plot_month, time_index=time_slider)
```

---
## Jupyter Notebooks

S1-GRiTS provides 5 tutorial notebooks covering data discovery, workflows, and analysis.

### Getting Started

```bash
# Activate environment
conda activate py312_s1grits

# Install notebook support (if not already installed)
pip install "s1grits[notebook]"

# Launch Jupyter
jupyter lab
# or
jupyter notebook
```

### Available Notebooks

| Notebook | Topic | Description |
|----------|-------|-------------|
| `Tutorial_A01_asf_search_basics.ipynb` | Data Discovery | ASF search basics, metadata queries, burst enumeration |
| `Tutorial_A02_S1-GRiTS_Guideline.ipynb` | Workflow Guide | Complete workflow walkthrough (config → run → outputs) |
| `Tutorial_A03_shapefile_wkt_query_en.ipynb` | ROI Setup | Convert shapefiles to WKT polygons for config |
| `Tutorial_B01_S1-GRiTS_mosaicVisual_byYear(Henan).ipynb` | Mosaic Visualization | Per-year multi-tile mosaic & false-color composites (additional demo region) |
| `Tutorial_B02_S1-GRiTS_timeseries_Fig5.ipynb` | Time Series | Pixel/region time-series extraction, plotting, statistics (reproduces paper Fig. 6) |

### Notebook Topics Overview

**A-Series: Getting Started**
- A01: Query ASF for available RTC-S1 data, understand burst geometry, filter by ROI
- A02: Configure and run S1-GRiTS workflows, interpret outputs, verify results
- A03: Create WKT polygons from shapefiles for ROI configuration

**B-Series: Analysis & Visualization**
- B01: Multi-tile mosaicking and false-color composites, per-year visualization (uses an additional demo region beyond the paper's Ecuador testbed)
- B02: Extract pixel/region time series, compute statistics, detect outliers — reproduces the near-decadal crop trajectories in the paper

---
