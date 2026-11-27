[← Back to README](../README.md)

## Output Structure

S1-GRiTS generates **analysis-ready data products** in three formats: Zarr (primary), COG (optional), and Preview PNG (optional).

### Current production layout

The scenes workflow writes `scenes` and, when enabled, per-track `smonthly`
products. The standalone `monthly` workflow was removed in V3; its
`{TILE}_{DIR}/zarr/S1_monthly.zarr` layout below is retained only as a legacy reference.

```text
{base_dir}/
  catalog.parquet
  {TILE}/
    catalog.parquet
    {scenes_product_label}/zarr/s1grits_scenes_{TILE}_{DIR}_TK{track}.zarr/
    {smonthly_product_label}/zarr/s1grits_smonthly_{TILE}_{DIR}_TK{track}.zarr/
    static_{DIR}/zarr/<static-store>.zarr/
```

For the default mosaic strategy, the scene label is `scenes_{DIR}` plus optional
filter and feature suffixes; the monthly label is `smonthly_{DIR}` plus feature
suffixes. For example, Ratio enabled without spatial filtering gives
`scenes_DESCENDING_Ratio` and `smonthly_DESCENDING_Ratio`. Optional intra-pass
strategies add their method and processing signature to the scene label.
Use the Catalog's `product_label`, `zarr_path` and `item_id` to locate a selected
instance rather than constructing paths from a filename template.

Within each product directory, optional exports use sibling `cog/` and `preview/`
directories. `TK{track}` identifies the relative orbit track; changing burst counts
are provenance and do not split a scene/monthly time series into new stores.
Static outputs are present only when requested. Production writes Parquet catalogs
and product metadata; STAC files require the explicit export described below.

### Resuming existing Zarr products

Resume requires the stored CRS, affine transform and pixel coordinates to match
the requested grid, including when the array dimensions are unchanged. An
incompatible grid fails before modifying the store. The existing explicit
`output.existing_store: rebuild-incompatible` policy recreates an incompatible
store and discards its old timesteps; use a new output location to retain them.

A nonempty per-track monthly store must also have the same processing signature.
Changing median to mean, for example, requires a new `output.base_dir`; skipping
or replacing a month must not relabel other stored values. A historical store
without a processing signature remains readable, but this writer cannot verify
its processing identity for append. Use a new output location instead of filling
unknown historical metadata with the current settings. Same-method updates and
the existing provenance recovery flow remain supported.

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

#### Legacy standalone Monthly Workflow Zarr

This Zarr v2 layout and the following example describe historical `monthly`
products, not the current per-track `smonthly` writer. Keep existing archives
readable; use the current layout above for new production.

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
- Compression: lossless Zstandard level 7, without shuffle (new stores)

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

**Path:** `{base_dir}/{TILE}/{scenes_product_label}/zarr/s1grits_scenes_{TILE}_{DIR}_TK{track}.zarr`

**Per-Track Organization:**
- Each acquisition group (track) produces **one Zarr store**
- Time dimension accumulates all acquisitions for that track
- A shared CRS, affine transform and pixel grid within each store; this does not guarantee subpixel registration

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

#### Per-track Monthly (`smonthly`) Zarr

**Path:** `{base_dir}/{TILE}/{smonthly_product_label}/zarr/s1grits_smonthly_{TILE}_{DIR}_TK{track}.zarr`

Enable `processing.monthly.enabled` in the scenes configuration. Each store keeps
one track's monthly time series, with `(time, y, x)` science arrays and the
Zarr-only `n_obs` support band. `processing.monthly.only` can omit scene outputs.
Current scenes and smonthly writers create Zarr v3 stores (`zarr.json` metadata),
unlike the historical `.zgroup`/`.zarray` example above.

#### Static Workflow Zarr

**Path:** `{base_dir}/{TILE}/static_{DIR}/zarr/<static-store>.zarr`,
sharing the dynamic data root and Catalog.

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

**Current per-track monthly products:**
```
s1grits_smonthly_{TILE}_{DIR}_TK{track}_{YYYY-MM}.tif
Example: s1grits_smonthly_17MQV_DESCENDING_TK142_2026-01.tif
```

**Legacy standalone monthly workflow:**
```
{TILE}_S1_Monthly_{DIR}_{YYYY-MM}.tif
Example: 17MPV_S1_Monthly_ASCENDING_2024-01.tif
```

**Scenes workflow:**
```
s1grits_scenes_{TILE}_{DIR}_TK{track}_{YYYYMMDDTHHMMSS}.tif
Example: s1grits_scenes_17MQV_DESCENDING_TK142_20260103T120000.tif
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

### Optional STAC Export

Default production output is a hierarchical product directory with Parquet catalogs
and provenance metadata. It is not automatically a STAC directory. Export STAC from
the existing products explicitly:

```bash
s1grits catalog resync --output-dir ./output
```

The exporter declares **STAC 1.1.0**, with the DataCube v2.3.0 extension on Items.
The JSON paths below describe this explicit export, not files promised by every
production run. Check the export diagnostics and actual assets before distributing
a catalog. The optional GeoParquet export stores Items in
`collections/{collection_id}/items.parquet` instead of individual JSON files.

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
      "href": "./collections/s1grits-smonthly/collection.json",
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

**Per-track monthly:** `collections/s1grits-smonthly/collection.json`
**Legacy monthly:** `collections/s1grits-monthly/collection.json` (if present)
**Scenes:** `collections/s1grits-scenes/collection.json`
**Static:** `collections/s1grits-static/collection.json`

Each collection defines:
- Spatial extent (all tiles combined)
- Temporal extent (date range)
- License and providers
- Product summaries; datacube dimensions and variables are declared on Items

#### STAC Items

**One Item per product:**
- Per-track monthly: One Item per tile × direction × track × month × product variant
- Scenes: One Item per tile × direction × acquisition date × track
- Static: One Item per tile × direction × track (no temporal info)

**Item paths:**
```
{TILE}/items/{product_label}/{item_id}.json
```

Use the exported Catalog's `item_path` for the exact filename. Historical and
optional strategy products can have different identity suffixes.

**Assets in each Item:**
- `zarr`: Link to Zarr store (primary asset)
- `cog`: Link to COG file (if enabled)
- `preview`: Link to PNG file (if enabled)

### Parquet Catalogs

Fast metadata queries without loading full STAC JSON.

#### Global Catalog

**Path:** `{base_dir}/catalog.parquet`

**Schema:** The versioned canonical schema includes:
- `tile_id`: MGRS tile identifier
- `flight_direction`: ASCENDING or DESCENDING
- `datetime`: Acquisition or composite date
- `product_type`: scenes, smonthly, static (and legacy monthly)
- `zarr_path`: Path to Zarr store
- `cog_path`: Path to COG file
- `bands`: JSON-encoded list of band names
- `crs`, `transform`, `width`, `height`: Product grid
- `resolution_x`, `resolution_y`: Pixel size
- `processing_level`: ARDC or hARDCp

**Query example:**
```python
import pandas as pd
df = pd.read_parquet("output/catalog.parquet")

# Find all January 2024 data
jan_2024 = df[(df['datetime'] >= '2024-01-01') & (df['datetime'] < '2024-02-01')]

# Find all ASCENDING data for tile 17MPV
tile_data = df[(df['tile_id'] == '17MPV') & (df['flight_direction'] == 'ASCENDING')]
```

#### Tile-Level Catalog

**Current path:** `{base_dir}/{TILE}/catalog.parquet`; historical archives may use
`{base_dir}/{TILE}_{DIR}/catalog.parquet`.

Same schema as the global catalog, limited to the products in that tile directory.

**Purpose:**
- Faster queries for single-tile analysis
- Tile-level completeness checking
- Independent tile archival

### Complete Directory Trees

#### Legacy standalone Monthly Workflow (historical reference)

This complete tree illustrates an older archive with STAC files. It is not the
directory template for new scenes or smonthly production.

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

#### Current Scenes Workflow with Optional Monthly and STAC Outputs

This example enables Ratio without spatial filtering. `catalog.json`,
`collections/` and `items/` appear only after the explicit JSON STAC export;
`cog/`, `preview/` and monthly outputs depend on the requested configuration.

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
│   │   │   │   └── time/ [2026-01-03, 2026-01-09, ...]
│   │   │   └── s1grits_scenes_17MQV_DESCENDING_TK40.zarr/
│   │   │       └── ...                       # All acquisitions for track 40
│   │   ├── cog/
│   │   │   ├── s1grits_scenes_17MQV_DESCENDING_TK142_20260103T120000.tif
│   │   │   ├── s1grits_scenes_17MQV_DESCENDING_TK142_20260109T120000.tif
│   │   │   ├── s1grits_scenes_17MQV_DESCENDING_TK40_20260102T120000.tif
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
│       │   ├── <scene_item_id>.json
│       │   └── ...
│       └── smonthly_DESCENDING_Ratio/
│           ├── <monthly_item_id>.json
│           └── ...
└── ...
```

#### Static Workflow

Static products are siblings of the matching dynamic products below each
tile directory. Their six arrays use `(y, x)` and retain the matching grid
and geometry-group identifiers. The canonical directory layout, production
order and reader examples are in [paired static layers](workflows.md#static-alignment).

---
