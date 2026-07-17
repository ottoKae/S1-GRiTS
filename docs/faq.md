[← Back to README](../README.md)

## FAQ

### Workflow Selection

**Q: When should I use the scenes workflow vs the monthly workflow?**

A: Choose based on your temporal resolution needs:
- **Monthly workflow:** Long-term trend analysis, seasonal monitoring, multi-year climate studies. Provides monthly temporal resolution with temporal median compositing for speckle reduction.
- **Scenes workflow:** Event detection (floods, landslides), rapid change monitoring (6-12 day revisit), disaster response, scene-level QA. Provides per-acquisition outputs at higher temporal resolution.

You can also use scenes workflow with `processing.monthly.enabled: true` to generate both outputs in one run.

---

**Q: What is the static layers workflow for?**

A: Static layers provide **time-invariant reference data** for:
- Geometric interpretation (incidence angle maps)
- Data filtering (layover/shadow masks)
- Uncertainty quantification (number of looks)
- Terrain correction validation (RTC area normalization factors)

Static layers complement the time-series workflows by providing context for geometric distortions and observation quality.

---

### Architecture & Data Products

**Q: Why are ASCENDING and DESCENDING orbits processed separately?**

A: Different orbit directions have different:
- **Incidence angles:** ASCENDING and DESCENDING observe terrain from opposite sides
- **Observation geometries:** Mixing introduces systematic bias in backscatter values
- **Scattering mechanisms:** Structural features (buildings, vegetation) scatter differently based on look direction

**Solution:** Run the workflow twice with `flight_direction: "ASCENDING"` and `flight_direction: "DESCENDING"` to produce both orbits. Merge at the analysis stage using `s1grits mosaic --direction ALL` if needed.

---

**Q: What is the difference between Zarr and COG?**

A: **Zarr** is the **primary product** — a time-series data cube supporting:
- Multi-dimensional slicing (time, space)
- Incremental append (add new time steps without reprocessing)
- Dask-parallel computation for large datasets
- Cloud-optimized chunked storage

**COG** is a **secondary product** — single-timestep GeoTIFF files:
- One file per month/scene
- GIS-tool compatible (QGIS, ArcGIS)
- Suitable for visualization and QC
- Can be regenerated from Zarr

**Key point:** COGs can be regenerated from Zarr, but **Zarr cannot be recovered from COGs**. Always preserve Zarr stores.

---

**Q: What is the acquisition group strategy?**

A: S1-GRiTS groups bursts by **(orbit_pass, track_number, frame_number)** to ensure geometric consistency within time-series cubes. Each acquisition group produces **one Zarr store** where all time steps share identical spatial grid and CRS.

**Benefits:**
- Perfect pixel-to-pixel alignment across time
- No geometric reprojection artifacts
- Temporal coherence for interferometric applications
- Efficient append-only updates

**Example:** Tile 17MQV DESCENDING has two acquisition groups:
- Track 142, Frame N07 → one Zarr store with 5 time steps
- Track 40, Frame N13 → one Zarr store with 4 time steps

---

### Configuration & Processing

**Q: Can I add GLCM texture bands to an existing 4-band Zarr?**

A: **No.** The Zarr band dimension is **fixed at creation time** and cannot be expanded in-place.

`existing_month: "overwrite"` re-processes existing months **within the existing schema** — it does not change band count.

**Solution:** To add GLCM bands, use a **separate output directory**:
```yaml
output:
  base_dir: "./output_glcm"   # New directory
processing:
  features_glcm: true
```

You will then have two separate datasets:
- `./output/` — 4 bands (VV_dB, VH_dB, Ratio, RVI)
- `./output_glcm/` — 12 bands (4 core + 8 GLCM texture)

---

**Q: What does `max_failed_ratio: 0.0` mean?**

A: Zero-tolerance mode — **any scene download or processing failure aborts the run** with an error.

Set to `0.1` to allow up to 10% failure rate for more lenient behavior:
```yaml
memory:
  max_failed_ratio: 0.1   # Allow 10% of scenes to fail
```

**Use case:** Useful when ASF has incomplete data for some bursts, allowing partial processing to continue.

---

**Q: How many bands does GLCM add?**

A: Default GLCM configuration adds **8 texture bands**:
```yaml
processing:
  texture_features:
    enabled: true
    inputs: ["VV_dB", "VH_dB"]            # 2 input bands
    metrics: ["contrast", "homogeneity", "entropy", "correlation"]  # 4 metrics
```

**Output:** 2 inputs × 4 metrics = **8 GLCM bands**

**Total band count:** 4 (core) + 8 (GLCM) = **12 bands**

---

**Q: What SAR index conventions does S1-GRiTS use?**

A: S1-GRiTS follows standard SAR remote sensing conventions:

**Ratio = VH / VV** (linear, not dB)
- Typical range for vegetation: 0.1 to 0.3
- Higher values indicate stronger cross-polarized scattering

**RVI = 4 × VH / (VV + VH)**
- Theoretical range: [0, 4]
- Typical range for vegetation: 0.4 to 2.0
- Higher values indicate more volume scattering (dense vegetation)

**Relationship:** RVI = 4 × Ratio / (1 + Ratio)

Both indices are monotonically related and show similar temporal patterns, but have different value ranges.

---

### Data Access & Analysis

**Q: How do I access Zarr data cubes programmatically?**

A: Three methods:

**Method 1: Direct xarray (recommended)**
```python
import xarray as xr
ds = xr.open_zarr("output/17MPV_ASCENDING/zarr/S1_monthly.zarr")
ratio_mean = ds['Ratio'].mean(dim='time')
```

**Method 2: s1grits.analysis API (with logging)**
```python
from s1grits.analysis import load_zarr_dataset
ds = load_zarr_dataset("17MPV", "ASCENDING", output_dir="./output")
```

**Method 3: STAC + rioxarray**
```python
import pystac
import rioxarray

cat = pystac.read_dict_to_object("output/catalog.json")
item = cat.get_item("17MPV_ASCENDING_2024-01")
zarr_asset = item.assets['zarr']
ds = xr.open_zarr(zarr_asset.href)
```

See [Python API](python_api.md) section for complete documentation.

---

**Q: Can I merge ASCENDING and DESCENDING data?**

A: Yes, but approach depends on use case:

**Option 1: Mosaic-level merge (recommended for visualization)**
```bash
s1grits mosaic --month 2024-01 --direction ALL --output ./mosaics/
```
ASCENDING is primary, DESCENDING fills NoData gaps.

**Option 2: Analysis-level merge (recommended for research)**
```python
import xarray as xr

ds_asc = xr.open_zarr("output/17MPV_ASCENDING/zarr/S1_monthly.zarr")
ds_desc = xr.open_zarr("output/17MPV_DESCENDING/zarr/S1_monthly.zarr")

# Combine in your analysis code
combined_mean = (ds_asc['VV_dB'].mean(dim='time') + ds_desc['VV_dB'].mean(dim='time')) / 2
```

**Note:** Merging should be done carefully due to different incidence angles. Consider your research question before merging.

---

**Q: How do I query the catalog for specific months?**

A: Use Parquet for fast queries:

```python
import pandas as pd

df = pd.read_parquet("output/catalog.parquet")

# Query by date range
jan_2024 = df[(df['datetime'] >= '2024-01-01') & (df['datetime'] < '2024-02-01')]

# Query by tile and direction
tile_data = df[(df['mgrs_tile_id'] == '17MPV') & (df['direction'] == 'ASCENDING')]

# Query by product type
scenes = df[df['product_type'] == 'scenes']
monthly = df[df['product_type'] == 'monthly']
```

Parquet queries are **much faster** than iterating through STAC JSON files.

---

### Troubleshooting

**Q: What if the workflow fails due to missing bursts?**

A: Check `max_failed_ratio` setting:
```yaml
memory:
  max_failed_ratio: 0.0   # Zero tolerance (default)
```

If ASF has incomplete burst coverage, increase tolerance:
```yaml
memory:
  max_failed_ratio: 0.1   # Allow 10% missing bursts
```

Also check logs for specific burst IDs that failed, and verify ASF has data for your ROI and time range.

---

**Q: How do I rebuild the catalog after an interrupted run?**

A: Run catalog resync command:
```bash
s1grits catalog resync --output-dir ./output
s1grits catalog validate --output-dir ./output
s1grits catalog inspect --output-dir ./output
```

This will:
1. Scan all COG files
2. Rebuild `catalog.parquet` from metadata
3. Regenerate STAC Item JSON files
4. Update STAC Collection extent

---

**Q: Can I generate monthly composites from existing scenes workflow outputs?**

A: Yes, if you ran scenes workflow with `processing.monthly.enabled: true`, monthly composites are already generated in the `smonthly_{DIR}_{bands}/` directory.

If you ran scenes without monthly enabled, you can:
1. Re-run scenes workflow with `monthly.enabled: true` (it will skip existing scenes and only generate monthlies)
2. Or use the Python API to create custom monthly aggregates from scenes Zarr stores

---
