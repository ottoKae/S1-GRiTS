[← Back to README](../README.md)

## Output Structure

S1-GRiTS generates **analysis-ready data products** in three formats: Zarr (primary), COG (optional), and Preview PNG (optional).

### Product Format Overview

| Format | Purpose | Key Features | Typical Size |
|--------|---------|--------------|--------------|
| **Zarr** | Core scientific product | Multi-dim time cube, incremental append, Dask-parallel, cloud-optimized chunks | ~500 MB/tile/year (monthly)<br>~2 GB/tile/year (scenes) |
| **COG** | GIS visualization / QC | Cloud-optimized GeoTIFF, one file per timestep, internal tiling, overviews | ~50-80 MB/tile/month |
| **Preview** | Quick browse | 300m RGB composite PNG, histogram-stretched | ~1-2 MB/tile/month |

### Band Composition

#### Core Bands (VV+VH Polarization)

| Band | Name | Description | Typical Range | Units |
|------|------|-------------|---------------|-------|
| 1 | `VV_dB` | Co-polarization gamma0 backscatter | -25 to +5 | dB |
| 2 | `VH_dB` | Cross-polarization gamma0 backscatter | -32 to -5 | dB |
| 3 | `Ratio` | Cross-polarization ratio VH/VV | 0.1 to 0.3 (vegetation) | linear |
| 4 | `RVI` | Radar Vegetation Index = 4×VH / (VV+VH) | 0 to 4 (theoretical) | unitless |

> **Note for HH+HV polarization:** Bands 1/2 map to `HH_dB` / `HV_dB`; Ratio and RVI definitions remain unchanged.

#### Optional GLCM Texture Bands

When `processing.texture_features.enabled: true`, additional texture metrics are computed:

**Metrics:** `contrast`, `homogeneity`, `entropy`, `correlation`

**Naming convention:** `{VV|VH}_glcm_{metric}`

**Example bands:**
- `VV_glcm_contrast`
- `VV_glcm_homogeneity`
- `VV_glcm_entropy`
- `VV_glcm_correlation`
- `VH_glcm_contrast`
- `VH_glcm_homogeneity`
- `VH_glcm_entropy`
- `VH_glcm_correlation`

**Total band count with GLCM:** 4 (core) + 8 (GLCM) = **12 bands**

> **Important:** Zarr band dimension is **fixed at creation time**. You cannot add GLCM bands to an existing 4-band Zarr. Use a separate output directory for GLCM-enabled datasets.

### Zarr Data Cube Specifications

#### Monthly Workflow Zarr

**Path:** `{base_dir}/{TILE}_{DIR}/zarr/S1_monthly.zarr`

**Structure:**
```
S1_monthly.zarr/
  ├── .zgroup                    # Zarr group metadata
  ├── .zattrs                    # Dataset attributes (CRS, transform, etc.)
  ├── VV_dB/
  │   ├── .zarray                # Array metadata
  │   ├── .zattrs                # Variable attributes
  │   └── [chunk files]          # Compressed binary chunks
  ├── VH_dB/
  ├── Ratio/
  ├── RVI/
  ├── time/                      # Coordinate variable
  ├── y/                         # Spatial coordinate
  └── x/                         # Spatial coordinate
```

**Dimensions:**
- `time`: Unlimited (appends new months)
- `y`: Fixed (derived from MGRS tile bounds)
- `x`: Fixed (derived from MGRS tile bounds)

**Chunk Configuration:**
- Spatial: 512 × 512 pixels
- Temporal: 1 time step per chunk
- Compression: Blosc (default)

**Typical Dimensions for 100km MGRS Tile:**
- `y`: 3660 (at 30m resolution)
- `x`: 3660
- `time`: Variable (grows with each month)

**Example:**
```python
import xarray as xr
ds = xr.open_zarr("output/17MPV_ASCENDING/zarr/S1_monthly.zarr")
print(ds)
# Dimensions:  (time: 24, y: 3660, x: 3660)
# Coordinates:
#   * time     (time) datetime64[ns] 2024-01-01 ... 2025-12-01
#   * y        (y) float64 ...
#   * x        (x) float64 ...
# Data variables:
#   VV_dB    (time, y, x) float32 dask.array<chunksize=(1, 512, 512)>
#   VH_dB    (time, y, x) float32 dask.array<chunksize=(1, 512, 512)>
#   Ratio    (time, y, x) float32 dask.array<chunksize=(1, 512, 512)>
#   RVI      (time, y, x) float32 dask.array<chunksize=(1, 512, 512)>
```

#### Scenes Workflow Zarr

**Path:** `{base_dir}/{TILE}/scenes_{DIR}_{bands}/zarr/s1grits_scenes_{TILE}_{DIR}_TK{track}.zarr`

**Per-Track Organization:**
- Each acquisition group (track) produces **one Zarr store**
- Time dimension accumulates all acquisitions for that track
- Perfect spatial alignment within each track

**Example:**
```
17MQV/scenes_DESCENDING_Ratio/zarr/
  ├── s1grits_scenes_17MQV_DESCENDING_TK142.zarr/   # Track 142
  │   ├── Dimensions: (time: 5, y: 3660, x: 3660)
  │   └── Acquisitions: 2026-01-03, 01-09, 01-15, 01-21, 01-27
  └── s1grits_scenes_17MQV_DESCENDING_TK40.zarr/    # Track 40
      ├── Dimensions: (time: 4, y: 3660, x: 3660)
      └── Acquisitions: 2026-01-02, 01-08, 01-14, 01-20
```

#### Static Workflow Zarr

**Path:** `{base_dir}_{DIR}_static/{TILE}_{DIR}/static/zarr/s1grits_static_{TILE}_{DIR}_TK{track}_N{bursts}.zarr`

**Structure:**
- **No time dimension** (static layers are time-invariant)
- One Zarr store per acquisition group
- Variables: `local_inc_angle`, `inc_angle`, `ls_map`, `number_of_looks`, `rtc_anf_beta0`, `rtc_anf_sigma0`

**Dimensions:**
- `y`: 3660 (for 100km tile at 30m)
- `x`: 3660

### COG Specifications

**Format:** Cloud-Optimized GeoTIFF (COG)

**Compression:** LZW (lossless)

**Internal Tiling:** 256 × 256 pixels

**Overviews:** 4 levels (2×, 4×, 8×, 16× downsampling)

**CRS:** Native UTM zone (EPSG:326XX for northern hemisphere)

**Spatial Resolution:** 30 m

**Bands:** 4 core bands (+ 8 GLCM bands if enabled)

**Naming Conventions:**

**Monthly workflow:**
```
{TILE}_S1_Monthly_{DIR}_{YYYY-MM}.tif
Example: 17MPV_S1_Monthly_ASCENDING_2024-01.tif
```

**Scenes workflow:**
```
s1grits_scenes_{TILE}_{DIR}_TK{track}_{DATE}.tif
Example: s1grits_scenes_17MQV_DESCENDING_TK142_20260103.tif
```

**Static workflow:**
```
{TILE}_{DIR}_TK{track}_N{bursts}_{layer}.tif
Example: 17MQV_DESCENDING_TK142_N07_local_inc_angle.tif
```

### Preview PNG Specifications

**Format:** PNG (RGB composite)

**Resolution:** 300 m (10× downsampled from 30 m)

**RGB Composite:**
- R = VV_dB (histogram-stretched to 2-98 percentile)
- G = VH_dB (histogram-stretched to 2-98 percentile)
- B = Ratio (histogram-stretched to 2-98 percentile)

**Purpose:** Quick browse visualization for quality control

**File Size:** ~1-2 MB per tile per month

### STAC Metadata

S1-GRiTS auto-generates **STAC 1.1.0**-compliant metadata kept in sync with Parquet catalogs.

**Standard:** STAC 1.1.0 + DataCube extension v2.3.0

#### Root Catalog

**Path:** `{base_dir}/catalog.json`

**Structure:**
```json
{
  "stac_version": "1.1.0",
  "type": "Catalog",
  "id": "s1-grits-root",
  "title": "S1-GRiTS DataCube",
  "description": "Sentinel-1 analysis-ready data",
  "links": [
    {
      "rel": "child",
      "href": "./collections/s1grits-scenes/collection.json",
      "type": "application/json",
      "title": "S1-GRiTS Scenes (per-acquisition)"
    },
    {
      "rel": "child",
      "href": "./collections/s1grits-monthly/collection.json",
      "type": "application/json",
      "title": "S1-GRiTS Monthly Composites"
    },
    {
      "rel": "child",
      "href": "./collections/s1grits-static/collection.json",
      "type": "application/json",
      "title": "S1-GRiTS Static Layers"
    }
  ]
}
```

#### Collections

**Monthly:** `collections/s1grits-monthly/collection.json`
**Scenes:** `collections/s1grits-scenes/collection.json`
**Static:** `collections/s1grits-static/collection.json`

Each collection defines:
- Spatial extent (all tiles combined)
- Temporal extent (date range)
- License and providers
- Datacube dimensions and variables

#### STAC Items

**One Item per product:**
- Monthly: One Item per tile × direction × month
- Scenes: One Item per tile × direction × acquisition date × track
- Static: One Item per tile × direction × track (no temporal info)

**Item paths:**
```
{TILE}/items/scenes_{DIR}_{bands}/{TILE}_{DIR}_{DATE}.json
{TILE}/items/smonthly_{DIR}_{bands}/{TILE}_{DIR}_{YYYY-MM}.json
{TILE}/items/static_{DIR}/{TILE}_{DIR}_TK{track}_N{bursts}_static.json
```

**Assets in each Item:**
- `zarr`: Link to Zarr store (primary asset)
- `cog`: Link to COG file (if enabled)
- `preview`: Link to PNG file (if enabled)

### Parquet Catalogs

Fast metadata queries without loading full STAC JSON.

#### Global Catalog

**Path:** `{base_dir}/catalog.parquet`

**Schema:** 30 columns including:
- `tile_id`: MGRS tile identifier
- `direction`: ASCENDING or DESCENDING
- `datetime`: Acquisition or composite date
- `product_type`: scenes, monthly, static
- `zarr_path`: Path to Zarr store
- `cog_path`: Path to COG file
- `geometry`: Tile bounding box (WKT)
- `bands`: List of band names
- `spatial_resolution`: 30.0
- `processing_level`: ARDC or hARDCp

**Query example:**
```python
import pandas as pd
df = pd.read_parquet("output/catalog.parquet")

# Find all January 2024 data
jan_2024 = df[(df['datetime'] >= '2024-01-01') & (df['datetime'] < '2024-02-01')]

# Find all ASCENDING data for tile 17MPV
tile_data = df[(df['tile_id'] == '17MPV') & (df['direction'] == 'ASCENDING')]
```

#### Tile-Level Catalog

**Path:** `{base_dir}/{TILE}/catalog.parquet` or `{base_dir}/{TILE}_{DIR}/catalog.parquet`

Same schema as global catalog, but filtered to single tile/direction.

**Purpose:**
- Faster queries for single-tile analysis
- Tile-level completeness checking
- Independent tile archival

### Complete Directory Trees

#### Monthly Workflow

```
output/
├── catalog.json                              # STAC root catalog
├── catalog.parquet                           # Global Parquet index
├── collections/
│   └── s1grits-monthly/
│       └── collection.json                   # STAC Collection
├── 17MPV_ASCENDING/
│   ├── catalog.parquet                       # Tile-level index
│   ├── zarr/
│   │   └── S1_monthly.zarr/                  # PRIMARY: Time-series cube
│   │       ├── VV_dB/ (time, y, x)
│   │       ├── VH_dB/ (time, y, x)
│   │       ├── Ratio/ (time, y, x)
│   │       ├── RVI/ (time, y, x)
│   │       ├── time/
│   │       ├── y/
│   │       └── x/
│   ├── cog/                                  # Optional COG exports
│   │   ├── 17MPV_S1_Monthly_ASCENDING_2024-01.tif
│   │   ├── 17MPV_S1_Monthly_ASCENDING_2024-02.tif
│   │   └── ...
│   ├── preview/                              # Optional PNG previews
│   │   ├── 17MPV_S1_Monthly_ASCENDING_2024-01.png
│   │   └── ...
│   ├── 17MPV_ASCENDING_2024-01.json          # STAC Item per month
│   ├── 17MPV_ASCENDING_2024-02.json
│   └── ...
└── 17MPV_DESCENDING/
    └── ...                                   # Separate directory per orbit
```

#### Scenes Workflow

```
output/
├── catalog.json
├── catalog.parquet
├── collections/
│   ├── s1grits-scenes/collection.json
│   └── s1grits-smonthly/collection.json      # If monthly.enabled: true
├── 17MQV/
│   ├── catalog.parquet
│   ├── scenes_DESCENDING_Ratio/
│   │   ├── zarr/
│   │   │   ├── s1grits_scenes_17MQV_DESCENDING_TK142.zarr/
│   │   │   │   ├── Ratio/ (time, y, x)      # All acquisitions for track 142
│   │   │   │   ├── VV_dB/ (time, y, x)
│   │   │   │   ├── VH_dB/ (time, y, x)
│   │   │   │   ├── RVI/ (time, y, x)
│   │   │   │   └── time/ [2026-01-03, 2026-01-09, ...]
│   │   │   └── s1grits_scenes_17MQV_DESCENDING_TK40.zarr/
│   │   │       └── ...                       # All acquisitions for track 40
│   │   ├── cog/
│   │   │   ├── s1grits_scenes_17MQV_DESCENDING_TK142_20260103.tif
│   │   │   ├── s1grits_scenes_17MQV_DESCENDING_TK142_20260109.tif
│   │   │   ├── s1grits_scenes_17MQV_DESCENDING_TK40_20260102.tif
│   │   │   └── ...
│   │   └── preview/
│   │       └── ...
│   ├── smonthly_DESCENDING_Ratio/            # If monthly.enabled: true
│   │   ├── zarr/
│   │   │   └── s1grits_smonthly_17MQV_DESCENDING_TK142.zarr/
│   │   ├── cog/
│   │   │   ├── s1grits_smonthly_17MQV_DESCENDING_TK142_2026-01.tif
│   │   │   └── ...
│   │   └── preview/
│   │       └── ...
│   └── items/
│       ├── scenes_DESCENDING_Ratio/
│       │   ├── 17MQV_DESCENDING_20260103.json
│       │   └── ...
│       └── smonthly_DESCENDING_Ratio/
│           ├── 17MQV_DESCENDING_2026-01.json
│           └── ...
└── ...
```

#### Static Workflow

```
output_DESCENDING_static/
├── 17MQV_DESCENDING/
│   ├── static/
│   │   ├── zarr/
│   │   │   ├── s1grits_static_17MQV_DESCENDING_TK142_N07.zarr/
│   │   │   │   ├── local_inc_angle/ (y, x)
│   │   │   │   ├── inc_angle/ (y, x)
│   │   │   │   ├── ls_map/ (y, x)
│   │   │   │   ├── number_of_looks/ (y, x)
│   │   │   │   ├── rtc_anf_beta0/ (y, x)
│   │   │   │   └── rtc_anf_sigma0/ (y, x)
│   │   │   └── s1grits_static_17MQV_DESCENDING_TK40_N13.zarr/
│   │   │       └── ...
│   │   └── cog/
│   │       ├── 17MQV_DESCENDING_TK142_N07_local_inc_angle.tif
│   │       ├── 17MQV_DESCENDING_TK142_N07_inc_angle.tif
│   │       ├── 17MQV_DESCENDING_TK142_N07_ls_map.tif
│   │       ├── 17MQV_DESCENDING_TK142_N07_number_of_looks.tif
│   │       ├── 17MQV_DESCENDING_TK142_N07_rtc_anf_beta0.tif
│   │       ├── 17MQV_DESCENDING_TK142_N07_rtc_anf_sigma0.tif
│   │       └── ...
│   └── items/
│       └── static_DESCENDING/
│           ├── 17MQV_DESCENDING_TK142_N07_static.json
│           └── ...
└── ...
```

---
