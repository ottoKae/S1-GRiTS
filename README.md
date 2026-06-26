<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/logo-dark.png">
    <img src="assets/logo/logo.png" width="200" alt="S1-GRiTS logo">
  </picture>
</p>

<!-- <h1 align="center">S1-GRiTS: Sentinel-1 Gridded RTC Time Series Data Cube</h1> -->

<p align="center">
  <em>Each pixel knows where it came from. Geometry is not erased.</em>
</p>
<p align="center">
  <a href="https://www.python.org/downloads/">
    <img src="https://img.shields.io/badge/python-3.12+-blue.svg" />
  </a>
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" />
  </a>
  <a href="https://github.com/ottoKae/S1-GRiTS">
    <img src="https://img.shields.io/badge/version-2.2.0-orange.svg" />
  </a>
</p>

---

<p align="center">
  S1-GRiTS (Sentinel-1 Gridded RTC Time Series Data Cube) is a Python package for generating analysis-ready Sentinel-1 SAR time series data cubes from ASF OPERA RTC-S1 products.
  It converts burst-level observations into MGRS-aligned, temporally consistent Zarr/COG data cubes.
</p>

## Table of Contents

- [For Reviewers — Paper ↔ Code](#for-reviewers--paper--code)
- [Features](#features)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Workflows](#workflows)
- [Output Structure](#output-structure)
- [Configuration Reference](#configuration-reference)
- [CLI Reference](#cli-reference)
- [Python API](#python-api)
- [Usage Examples](#usage-examples)
- [Jupyter Notebooks](#jupyter-notebooks)
- [FAQ](#faq)
- [License & Citation](#license--citation)
- [Acknowledgements](#acknowledgements)

---

## For Reviewers — Paper ↔ Code

> This repository is the **open-source implementation of the manuscript** below. This section lets reviewers (1) confirm that every key technique described in the paper is actually implemented, and (2) reproduce the reported figures and tables.

**Manuscript:** *Sentinel-1 Gridded Time Series (S1-GRiTS): Geometry-traceable SAR Data Cubes for decadal vegetation monitoring in cloud-prone regions* — Rao et al., 2026 (under review).

**Software:** `s1grits` — [GitHub](https://github.com/ottoKae/S1-GRiTS) · [PyPI](https://pypi.org/project/s1grits/) (`pip install s1grits`).

**Validation testbed:** mainland Ecuador, MGRS tile **17MPU**, 2017–2025.

### Key technique → implementation map

Each row links a core methodological claim in the paper to the module/function and the CLI entry point that runs it.

| Paper technique (section) | Implemented in | Run via |
|---|---|---|
| **Burst-first deterministic acquisition grouping** by `(mgrs_tile_id, acq_group_id_within_mgrs_tile, pass_id)` + `track_token`, `pass_id` 6-day cycle (§3.1, Table 2) | `asf_tiles.py` (`extract_pass_id`, group/`track_token` build), `dist_enum.py` | `s1grits process_scenes` |
| **First-valid-pixel mosaicking** (source control, *not* radiometric fusion) (§3.1) | `asf_output_writing.py` → `_mosaic_align()` ("first burst covering each pixel wins") | all `process*` commands |
| **Orbit-direction separation + one Zarr per acquisition group** (§3.2) | `workflow_scenes.py`, `asf_output_writing.py` → `merge_acq_group_zarrs()` | `s1grits process_scenes` |
| **Cloud-native S3 streaming, zero-disk, in-memory virtual file → rasterio → float32** (§3.3) | `asf_io.py` (`rasterio.io.MemoryFile`), `rtc_s1_io.py` (streaming HTTP session) | all `process*` commands |
| **Adaptive temporal batching + memory-bounded parallelism** (Eq. 1–2, §3.3) | `memory_manager.py` (`detect_system_memory` via psutil, `select_batch_strategy`/`chunk_time_by_strategy`: yearly/quarterly/monthly) | `parallel` / `memory` config blocks |
| **Temporal median compositing + TV-Bregman despeckle before tile-clip** (§3.2) | `asf_io.py` → `load_and_despeckle_rtc_strict` (`tv_bregman`), `asf_array_processing.despeckle_2d` | `s1grits process` |
| **Incremental, appendable Zarr cube + STAC 1.1.0 + Parquet catalogs** (§3.2) | `stac_builder.py` (`STAC_VERSION = "1.1.0"`, datacube ext v2.3.0), `catalog_sync.py`, `canonical_catalog_schema.py` | `s1grits catalog inspect` |
| **Static acquisition-geometry layers** (LIA, inc. angle, layover/shadow, looks, ANF β0/σ0) (§2.2, §4.1) | `workflow_static.py` (`local_inc_angle`, `inc_angle`, `ls_map`, `number_of_looks`, `rtc_anf_beta0`, `rtc_anf_sigma0`) | `s1grits process_static` |
| **Cross-orbit (ASC–DESC) backscatter offset quantification vs. LIA/ANF** (§3.4, §4.3, Table 3) | `manuscript_analysis_scripts/c05_t03_*`, `c05_f07_*` | run scripts (see below) |

### Reproducing the paper's figures & tables

The `manuscript_analysis_scripts/` directory contains the exact scripts used to produce the published results:

| Script | Reproduces |
|---|---|
| `c01_f5a_gridded_composites_mosaics_ECU.py`, `c01_f5a_gridded_composites_17MPU.py` | **Fig. 5a** — Ecuador mosaic & tile 17MPU composite |
| `c04_f08_gridded_composites_mosaics_DEU.py` | **Fig. 8** — multi-region scalability (Bayern / Sahel / GBA / New Britain) |
| `c05_f07_evaluation_cross_orbit_offsets_LIA_ANF.py` | **Fig. 7** — cross-orbit offset spatial maps & LIA/ANF heatmaps |
| `c05_t03_evaluation_orbit_paris_offsets.py` | **Table 3** — ASC–DESC vs. within-orbit offset statistics |

**Published data products (Zenodo, no login/embargo):**
- Ecuador monthly DESC composites & mosaic (Jan 2026) — [10.5281/zenodo.20607389](https://doi.org/10.5281/zenodo.20607389)
- Tile 17MPU ASC gridded time series 2017–2025 — [10.5281/zenodo.20589543](https://doi.org/10.5281/zenodo.20589543)
- Tile 17MPU DESC gridded time series 2017–2025 — [10.5281/zenodo.20607919](https://doi.org/10.5281/zenodo.20607919)
- Multi-region scalability composites — [10.5281/zenodo.20604391](https://doi.org/10.5281/zenodo.20604391)
- Pixel-level orbit-pair statistics & static geometry — [10.5281/zenodo.20607604](https://doi.org/10.5281/zenodo.20607604)

### Reproduce the headline result in 3 commands

```bash
# 1. Generate the geometry-consistent gridded time series for the paper's study tile
s1grits process_scenes --config config/s1grits_scenes.yaml      # edit: manual_mgrs_tiles: ["17MPU"]

# 2. Generate the static acquisition-geometry layers (LIA, ANF β0) used in the offset analysis
s1grits process_static  --config config/s1grits_static.yaml     # edit: manual_mgrs_tiles: ["17MPU"]

# 3. Quantify cross-orbit ASC–DESC offsets (paper Table 3 / Fig. 7)
python manuscript_analysis_scripts/c01_f5a_gridded_composites_17MPU.py
```

---

## Features

S1-GRiTS is designed for researchers and practitioners who need **large-scale, long-term SAR time series analysis** without the complexity of raw data processing.

**Three Processing Workflows:**
- **Monthly Composites** — Multi-year time series at monthly temporal resolution
- **Per-Scene Processing** — High-temporal-resolution outputs for event detection
- **Static Layers** — Time-invariant reference layers (DEM, incidence angles, masks)

**Core Capabilities:**
1. **Zarr-First Data Cube Architecture** — Zarr stores are the primary time-series product; COG/preview are optional derivative exports
2. **Cloud-Native S3 Streaming** — Zero-disk download; data streamed directly from ASF S3
3. **MGRS Grid Alignment** — Products aligned to 100km MGRS tiles in native UTM projections
4. **Orbit-Direction Separation** — ASCENDING and DESCENDING processed independently for geometric consistency
5. **Acquisition Group Strategy** — Bursts grouped by (orbit, track, frame) for temporal coherence
6. **Standardized Gamma0 Radiometry** — Built on OPERA RTC-S1 with radiometric terrain correction
7. **Dual Speckle Suppression** — Temporal median compositing + optional spatial TV-Bregman filtering
8. **Incremental Time-Series Updates** — Zarr supports append-only updates without reprocessing
9. **STAC 1.1.0 Metadata** — Full STAC compliance with Parquet catalogs for fast queries
10. **Rich Analysis API** — 8 analysis submodules for data loading, time series extraction, visualization, validation

**Typical Use Cases:**
- Long-term deformation monitoring
- Agricultural crop classification
- Forest change detection
- Flood disaster assessment
- Land use / land cover mapping

![MGRS Mosaic Example](notebooks/S1-GRiTS-f1-exmaple.png)
**Figure 1.**  Burst-first MGRS-grid mosaic over Wuhan, China.

![Tile Composite](notebooks/S1-GRiTS-f12-tile.jpg)
**Figure 2.**  Burst-first MGRS-grid mosaic over mainland Ecuador and its 17MPU title, demonstrating spatial consistency and tile-edge-free seamless stitching after despeckling (paper Fig. 5a).

![Time Series](notebooks/S1-GRiTS-f2-TS-exmaple.jpg)
**Figure 3.** Near-decadal (2017–2025) Sentinel-1 backscatter time series for representative objects.

---

## Quick Start

Get your first S1-GRiTS output in **5 minutes**.

### Prerequisites

- **Python:** >= 3.12
- **RAM:** >= 16 GB (64 GB+ recommended for large regions)
- **OS:** Windows or Linux
- **Network:** Access to ASF (asf.alaska.edu) and AWS S3

### Step 1: Installation

> **Important:** Do not install in conda base environment. S1-GRiTS requires Python 3.12 and compiled extensions. Always create a dedicated environment first.

#### Option A: pip install from PyPI (recommended)

```bash
# 1. Create dedicated Python 3.12 environment
conda create -n s1grits python=3.12
conda activate s1grits

# 2. Install geospatial core libraries via conda-forge (optional but recommended on Windows)
conda install -c conda-forge rasterio geopandas rioxarray pyproj shapely

# 3. Install s1grits from PyPI
pip install s1grits
```

#### Option B: Install from source (developers)

```bash
# Clone repository
git clone https://github.com/ottoKae/S1-GRiTS.git
cd S1-GRiTS

# Create conda environment
conda install -n base conda-libmamba-solver
conda env create -f environment.yml --solver=libmamba
conda activate py312_s1grits_v100

# Install package
pip install .
```

#### Optional: Jupyter notebook support

```bash
pip install "s1grits[notebook]"
python -m ipykernel install --user --name py312_s1grits --display-name "Python (s1grits)"
jupyter lab
```

#### Optional: Streamlit GUI (under test)

```bash
pip install "s1grits[gui]"
s1grits-gui  # Launch at http://localhost:8501
```

#### Install all extras

```bash
pip install "s1grits[all]"
```

### Step 2: Earthdata Authentication

All ASF downloads require NASA Earthdata credentials. Set up `.netrc` authentication once.

**2.1 Register Account**

1. Register at https://urs.earthdata.nasa.gov
2. Authorize ASF DAAC at https://urs.earthdata.nasa.gov/profile (Applications > Alaska Satellite Facility Data Access)

**2.2 Create `.netrc` File**

**Linux/macOS:** `~/.netrc`
**Windows:** `%USERPROFILE%\.netrc`

```
machine urs.earthdata.nasa.gov
  login YOUR_USERNAME
  password YOUR_PASSWORD
```

**2.3 Enable in Python**

```python
import asf_search as asf
session = asf.ASFSession()
session.trust_env = True  # Allow reading .netrc
```

### Step 3: Configure

Choose a workflow and edit the corresponding config file:

**Monthly Composites:** `config/s1grits_monthly.yaml`
**Per-Scene Processing:** `config/s1grits_scenes.yaml`
**Static Layers:** `config/s1grits_static.yaml`

**Minimal config example** (monthly workflow):

```yaml
roi:
  # Option A: WKT polygon (auto-detect tiles)
  wkt: "POLYGON((113.587 30.0001,114.8881 30.0001,114.8881 30.9441,113.587 30.9441,113.587 30.0001))"
  
  # Option B: Manual tile list (faster)
  # manual_mgrs_tiles:
  #   - "50RKV"
  #   - "50RLV"
  
  flight_direction: "ASCENDING"   # ASCENDING | DESCENDING
  polarization: "VV+VH"           # VV+VH | HH+HV

time:
  years: [2024]
  months: [1, 2, 3]   # optional; omit for full year

output:
  base_dir: "./output"
```

### Step 4: Run

```bash
# Monthly composites workflow
s1grits process --config config/s1grits_monthly.yaml

# Per-scene workflow
s1grits process_scenes --config config/s1grits_scenes.yaml

# Static layers workflow
s1grits process_static --config config/s1grits_static.yaml
```

**What happens:**
- ASF metadata query for ROI and time range
- S3 streaming download (no local zip files)
- Multi-burst mosaic per MGRS tile
- Speckle reduction (temporal + optional spatial)
- Feature extraction (Ratio, RVI, optional GLCM)
- Zarr data cube + COG + preview generation
- STAC catalog + Parquet index creation

---
## Architecture

S1-GRiTS is built on a **Zarr-first data cube architecture** with three specialized processing workflows for different temporal analysis needs.

### Zarr-First Philosophy

**Zarr is the authoritative primary product** — a time-series data cube that accumulates all acquisitions incrementally. COG and preview files are **optional derivative exports** generated from Zarr.

**Why Zarr-first matters:**
- **Temporal alignment:** All time steps share the same spatial grid and CRS, ensuring perfect temporal alignment across acquisitions
- **Incremental updates:** New acquisitions append to existing Zarr stores without reprocessing historical data
- **Cloud-optimized:** Chunked storage (512×512 pixels) enables efficient parallel reads via Dask/xarray
- **Multi-dimensional:** Direct access to time-series slicing, spatial subsetting, and statistical aggregation
- **Future-proof:** COG files can be regenerated from Zarr, but Zarr cannot be recovered from COGs

**Product Hierarchy:**
```
Zarr Data Cube (PRIMARY)
    ├── Time-series analysis
    ├── Temporal statistics
    └── Multi-year compositing
         ↓
COG Files (SECONDARY - optional)
    ├── GIS visualization (QGIS, ArcGIS)
    ├── Single-timestep snapshots
    └── Web map services
         ↓
Preview PNG (TERTIARY - optional)
    └── Quick browse thumbnails
```

### Acquisition Group Strategy

S1-GRiTS uses an **acquisition group strategy** to ensure geometric consistency within time-series data cubes.

**How it works:**
1. **Burst enumeration:** Query ASF for all RTC-S1 bursts intersecting the MGRS tile
2. **Group by acquisition geometry:** Bursts are grouped by `(orbit_pass, track_number, frame_number)` — this is the **acquisition group**
3. **One Zarr store per group:** Each acquisition group produces one Zarr data cube, ensuring all time steps share identical geometry

**Example:**
```
MGRS Tile 17MQV, DESCENDING orbit:
  ├── Acquisition Group 1: Track 142, Frame N07
  │   └── Zarr: s1grits_scenes_17MQV_DESCENDING_TK142_N07.zarr
  │       ├── 2026-01-03 acquisition
  │       ├── 2026-01-09 acquisition
  │       └── 2026-01-15 acquisition
  │
  └── Acquisition Group 2: Track 40, Frame N13
      └── Zarr: s1grits_scenes_17MQV_DESCENDING_TK40_N13.zarr
          ├── 2026-01-02 acquisition
          ├── 2026-01-08 acquisition
          └── 2026-01-14 acquisition
```

**Benefits:**
- Perfect spatial alignment across all time steps within a group
- No geometric reprojection artifacts
- Temporal coherence for interferometric applications
- Efficient append-only updates

### Three Workflow Comparison

S1-GRiTS provides three specialized workflows for different analysis needs:

| Aspect | **Monthly Composites** | **Per-Scene Processing** | **Static Layers** |
|--------|------------------------|--------------------------|-------------------|
| **Purpose** | Long-term time series | Event detection | Terrain reference |
| **Temporal Resolution** | Monthly (median composite) | Per-acquisition (6-12 day revisit) | Timeless |
| **Primary Use Case** | Seasonal trends, multi-year analysis | Rapid change, disaster response | Incidence angle correction, masking |
| **Output Zarr** | One per tile-direction | One per acquisition group | One per acquisition group |
| **CLI Command** | `s1grits process` | `s1grits process_scenes` | `s1grits process_static` |
| **Config Template** | `s1grits_monthly.yaml` | `s1grits_scenes.yaml` | `s1grits_static.yaml` |
| **STAC Collection** | `s1grits-monthly` | `s1grits-scenes` | `s1grits-static` |
| **Typical Data Volume** | ~500 MB/tile/year (Zarr) | ~2-3 GB/tile/year (Zarr) | ~50 MB/tile (one-time) |
| **Processing Time** | Fast (monthly aggregation) | Moderate (per-scene outputs) | Fast (static, no time series) |

---

## Workflows

### Workflow 1: Monthly Composites

**Generate multi-year time series at monthly temporal resolution.**

#### Purpose

Create temporally consistent monthly composite time series suitable for:
- Long-term trend analysis (deforestation, urbanization)
- Seasonal vegetation monitoring
- Multi-year climate impact studies
- Large-scale land cover classification

#### When to Use

- Analysis requires **monthly or coarser** temporal resolution
- Focus on **long-term trends** rather than individual events
- Storage efficiency is important (monthly aggregation reduces volume)
- Temporal speckle reduction through median compositing is desired

#### Output Structure

```
{base_dir}/
  catalog.json                              # STAC root catalog
  catalog.parquet                           # Global Parquet index
  collections/
    s1grits-monthly/collection.json         # STAC Collection
  {TILE}_{DIR}/                            # e.g., 17MPV_ASCENDING/
    catalog.parquet                         # Tile-level index
    zarr/
      S1_monthly.zarr/                      # PRIMARY: Monthly time-series cube
        ├── VV_dB/      (time, y, x)
        ├── VH_dB/      (time, y, x)
        ├── Ratio/      (time, y, x)
        ├── RVI/        (time, y, x)
        ├── time/       [2024-01, 2024-02, ...]
        ├── y/          [pixel coordinates]
        └── x/          [pixel coordinates]
    cog/
      {TILE}_S1_Monthly_{DIR}_{YYYY-MM}.tif   # COG per month
    preview/
      {TILE}_S1_Monthly_{DIR}_{YYYY-MM}.png   # PNG per month
    {TILE}_{DIR}_{YYYY-MM}.json                # STAC Item per month
```

#### CLI Command

```bash
s1grits process --config config/s1grits_monthly.yaml
```

#### Key Configuration

```yaml
workflow: "monthly"   # Not explicitly set; default behavior

processing:
  post_processing: true        # Enable TV-Bregman spatial despeckle
  despeckle:
    monthly_despeckle: true
    method: "tv_bregman"
    kwargs:
      reg_param: 5.0           # Regularization strength

  texture_features:
    enabled: false             # Optional GLCM texture bands
```

#### Zarr Schema

- **Dimensions:** `(time, y, x)` — time is unlimited, spatial dims are fixed per tile
- **Chunk size:** 512×512 pixels (cloud-optimized for parallel access)
- **Variables:** VV_dB, VH_dB, Ratio, RVI (+ optional GLCM bands if enabled)
- **Coordinates:** time (datetime64), y (float), x (float)
- **CRS:** Native UTM zone derived from MGRS tile

---

### Workflow 2: Per-Scene Processing

**Generate high-temporal-resolution outputs for each acquisition pass.**

#### Purpose

Produce per-acquisition scene outputs suitable for:
- Event detection (floods, landslides, rapid deforestation)
- Rapid change monitoring (6-12 day revisit)
- Scene-level quality assurance
- High-frequency time series analysis

#### When to Use

- Analysis requires **sub-monthly** temporal resolution
- Focus on **individual events** rather than seasonal trends
- Scene-level metadata and provenance tracking is needed
- Optional: Generate monthly composites from scenes in one pass

#### Output Structure

```
{base_dir}/
  catalog.json
  catalog.parquet
  collections/
    s1grits-scenes/collection.json
    s1grits-smonthly/collection.json       # If monthly.enabled: true
  {TILE}/                                 # e.g., 17MQV/
    catalog.parquet
    scenes_{DIR}_{despeckle}_{bands}/     # e.g., scenes_DESCENDING_Ratio/
      zarr/
        s1grits_scenes_{TILE}_{DIR}_TK{track}_N{bursts}.zarr/   # Per-track cube
          ├── Ratio/     (time, y, x)      # All acquisitions for this track
          ├── VV_dB/     (time, y, x)
          ├── VH_dB/     (time, y, x)
          ├── RVI/       (time, y, x)
          └── time/      [2026-01-03, 2026-01-09, 2026-01-15, ...]
      cog/
        s1grits_scenes_{TILE}_{DIR}_TK{track}_N{bursts}_{DATE}.tif
      preview/
        s1grits_scenes_{TILE}_{DIR}_TK{track}_N{bursts}_{DATE}.png
    smonthly_{DIR}_{bands}/               # If monthly.enabled: true
      zarr/
        s1grits_smonthly_{TILE}_{DIR}_monthly.zarr/
      cog/
        s1grits_smonthly_{TILE}_{DIR}_{YYYY-MM}.tif
      preview/
        s1grits_smonthly_{TILE}_{DIR}_{YYYY-MM}.png
    items/
      scenes_{DIR}_{bands}/
        {TILE}_{DIR}_{DATE}.json          # STAC Item per scene
      smonthly_{DIR}_{bands}/
        {TILE}_{DIR}_{YYYY-MM}.json       # STAC Item per month
```

#### CLI Command

```bash
s1grits process_scenes --config config/s1grits_scenes.yaml
```

#### Key Configuration

```yaml
workflow: "scenes"

processing:
  spatial_despeckle: false    # Per-scene spatial filtering (optional)
  features_ratio: true        # Generate Ratio = VH/VV
  features_rvi: false         # Generate RVI index
  features_glcm: false        # Generate GLCM texture bands
  
  # Optional: Generate monthly composites from scenes
  monthly:
    enabled: false            # Set true to produce both scenes + monthlies
    composite_method: "nanmedian"
    generate_cog: true
    generate_preview: true
```

#### Acquisition Group Output

Each acquisition group (track + frame combination) produces **one Zarr data cube** containing all time steps:

**Example for Track 142, Frame N07:**
- Zarr: `s1grits_scenes_17MQV_DESCENDING_TK142_N07.zarr`
- Time dimension: 5 acquisitions in January 2026
- Perfect spatial alignment across all time steps

**Example for Track 40, Frame N13:**
- Zarr: `s1grits_scenes_17MQV_DESCENDING_TK40_N13.zarr`
- Time dimension: 4 acquisitions in January 2026
- Different spatial footprint (non-overlapping with TK142)

---

### Workflow 3: Static Layers

**Generate time-invariant reference layers from RTC-STATIC products.**

#### Purpose

Produce static reference layers suitable for:
- Terrain correction validation
- Incidence angle analysis
- Layover/shadow masking
- Number-of-looks weighting
- RTC area normalization factor reference

#### When to Use

- Need **incidence angle maps** for geometric interpretation
- Require **layover/shadow masks** for data filtering
- Want **number of looks** for uncertainty quantification
- Analyzing **terrain-induced geometric distortions**

#### Output Structure

```
{base_dir}_{DIR}_static/                  # e.g., output_DESCENDING_static/
  {TILE}_{DIR}/                          # e.g., 17MQV_DESCENDING/
    static/
      zarr/
        s1grits_static_{TILE}_{DIR}_TK{track}_N{bursts}.zarr/
          ├── local_inc_angle/   (y, x)   # Local incidence angle
          ├── inc_angle/         (y, x)   # Incidence angle
          ├── ls_map/            (y, x)   # Layover/shadow mask
          ├── number_of_looks/   (y, x)   # Number of looks
          ├── rtc_anf_beta0/     (y, x)   # RTC ANF (beta0)
          └── rtc_anf_sigma0/    (y, x)   # RTC ANF (sigma0)
      cog/
        {TILE}_{DIR}_TK{track}_N{bursts}_local_inc_angle.tif
        {TILE}_{DIR}_TK{track}_N{bursts}_inc_angle.tif
        {TILE}_{DIR}_TK{track}_N{bursts}_ls_map.tif
        {TILE}_{DIR}_TK{track}_N{bursts}_number_of_looks.tif
        {TILE}_{DIR}_TK{track}_N{bursts}_rtc_anf_beta0.tif
        {TILE}_{DIR}_TK{track}_N{bursts}_rtc_anf_sigma0.tif
    items/
      static_{DIR}/
        {TILE}_{DIR}_TK{track}_N{bursts}_static.json   # STAC Item (no datetime)
```

#### CLI Command

```bash
s1grits process_static --config config/s1grits_static.yaml
```

#### Key Configuration

```yaml
workflow: "static"

# Static layers are always enabled; no temporal processing options
output:
  base_dir: "./output_static"   # Separate directory recommended
  overwrite: false              # Skip if outputs exist
```

#### Available Static Layers

| Layer | Variable Name | Description | Units |
|-------|---------------|-------------|-------|
| **Local Incidence Angle** | `local_inc_angle` | Local terrain incidence angle | degrees |
| **Incidence Angle** | `inc_angle` | SAR incidence angle | degrees |
| **Layover/Shadow Map** | `ls_map` | Geometric distortion mask | 0=valid, 1=layover, 2=shadow |
| **Number of Looks** | `number_of_looks` | Multi-looking factor | count |
| **RTC ANF (beta0)** | `rtc_anf_beta0` | Area normalization factor (beta0) | unitless |
| **RTC ANF (sigma0)** | `rtc_anf_sigma0` | Area normalization factor (sigma0) | unitless |

#### Notes

- Static layers are **time-invariant** — no temporal dimension
- One set of outputs per acquisition group (same as scenes workflow)
- Processing is **skipped if outputs exist** (unless `overwrite: true`)
- Zarr stores have **no time dimension** — only spatial (y, x)

---
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

**Path:** `{base_dir}/{TILE}/scenes_{DIR}_{bands}/zarr/s1grits_scenes_{TILE}_{DIR}_TK{track}_N{bursts}.zarr`

**Per-Track Organization:**
- Each acquisition group (track + frame) produces **one Zarr store**
- Time dimension accumulates all acquisitions for that track
- Perfect spatial alignment within each track

**Example:**
```
17MQV/scenes_DESCENDING_Ratio/zarr/
  ├── s1grits_scenes_17MQV_DESCENDING_TK142_N07.zarr/   # Track 142
  │   ├── Dimensions: (time: 5, y: 3660, x: 3660)
  │   └── Acquisitions: 2026-01-03, 01-09, 01-15, 01-21, 01-27
  └── s1grits_scenes_17MQV_DESCENDING_TK40_N13.zarr/    # Track 40
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
s1grits_scenes_{TILE}_{DIR}_TK{track}_N{bursts}_{DATE}.tif
Example: s1grits_scenes_17MQV_DESCENDING_TK142_N07_20260103.tif
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
│   │   │   ├── s1grits_scenes_17MQV_DESCENDING_TK142_N07.zarr/
│   │   │   │   ├── Ratio/ (time, y, x)      # All acquisitions for track 142
│   │   │   │   ├── VV_dB/ (time, y, x)
│   │   │   │   ├── VH_dB/ (time, y, x)
│   │   │   │   ├── RVI/ (time, y, x)
│   │   │   │   └── time/ [2026-01-03, 2026-01-09, ...]
│   │   │   └── s1grits_scenes_17MQV_DESCENDING_TK40_N13.zarr/
│   │   │       └── ...                       # All acquisitions for track 40
│   │   ├── cog/
│   │   │   ├── s1grits_scenes_17MQV_DESCENDING_TK142_N07_20260103.tif
│   │   │   ├── s1grits_scenes_17MQV_DESCENDING_TK142_N07_20260109.tif
│   │   │   ├── s1grits_scenes_17MQV_DESCENDING_TK40_N13_20260102.tif
│   │   │   └── ...
│   │   └── preview/
│   │       └── ...
│   ├── smonthly_DESCENDING_Ratio/            # If monthly.enabled: true
│   │   ├── zarr/
│   │   │   └── s1grits_smonthly_17MQV_DESCENDING_monthly.zarr/
│   │   ├── cog/
│   │   │   ├── s1grits_smonthly_17MQV_DESCENDING_2026-01.tif
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
## Configuration Reference

S1-GRiTS workflows are configured via YAML files in the `config/` directory.

### Configuration Templates

| File | Workflow | Description |
|------|----------|-------------|
| `s1grits_monthly.yaml` | Monthly composites | Long-term time series at monthly resolution |
| `s1grits_scenes.yaml` | Per-scene processing | High-temporal-resolution outputs |
| `s1grits_static.yaml` | Static layers | Time-invariant reference layers |

### ROI Configuration

Two modes supported — choose one in your YAML:

#### Mode A: WKT Polygon (Auto-Detect Tiles)

System calculates all MGRS tiles intersecting the polygon.

**Coordinates:** EPSG:4326 (WGS84 lat/lon)

**Sources:** [ASF Vertex](https://search.asf.alaska.edu), [geojson.io](https://geojson.io)

```yaml
roi:
  wkt: "POLYGON((113.587 30.0001,114.8881 30.0001,114.8881 30.9441,113.587 30.9441,113.587 30.0001))"
  flight_direction: "ASCENDING"   # ASCENDING | DESCENDING
  polarization: "VV+VH"           # VV+VH | HH+HV
```

#### Mode B: Manual MGRS Tile List (Faster Startup)

Explicit tile list — no geometry processing needed.

```yaml
roi:
  manual_mgrs_tiles:
    - "50RKV"
    - "50RLV"
    - "17MPV"
  flight_direction: "DESCENDING"
  polarization: "VV+VH"
```

**Orbit Direction Note:**

ASCENDING and DESCENDING are processed **separately** and archived in independent directories (e.g., `17MPV_ASCENDING/`, `17MPV_DESCENDING/`). To produce both orbits, run the workflow twice with different `flight_direction` values.

### Time Range Configuration

Two modes supported:

#### Mode A: Specific Years (Recommended)

```yaml
time:
  years: [2024, 2025]    # Single or multiple years
  months: [6, 7, 8]      # Optional; omit for all 12 months
```

#### Mode B: Full Archive (Auto-Detect Start Date)

```yaml
time:
  full: 2026    # Process from earliest available (~2014) through end of 2026
```

#### Future-Month Guard

System automatically skips future/incomplete months with WARNING messages:

| Month Type | Condition | Behavior |
|------------|-----------|----------|
| Past month | `(year, month) < (today.year, today.month)` | Processed normally |
| Current month (incomplete) | `year == today.year AND month == today.month` | Skipped + WARNING |
| Future month | `(year, month) > (today.year, today.month)` | Skipped + WARNING |

**Example** (today = 2026-04-14, config `years: [2026], months: [3, 4, 5]`):

```text
WARNING  Skipping 2026-04: month is current (incomplete) or future (today is 2026-04-14)
WARNING  Skipping 2026-05: month is current (incomplete) or future (today is 2026-04-14)
```

In `full` mode, end date auto-clips to last completed month:

```text
WARNING  Clipping end date from 2026-12-31 to 2026-03-31 (last fully completed month; today is 2026-04-14)
```

### Output Configuration

```yaml
output:
  base_dir: "./output"   # Output root (subdirectory structure auto-created)
  overwrite: false       # false: skip existing outputs (incremental-safe)
                         # true:  re-process and replace existing products
  on_time_conflict: "skip"   # "skip" or "overwrite" (scenes workflow only)
  
  formats:
    cog: true            # Generate COG files (optional)
    preview: true        # Generate preview PNGs (optional)
                         # Note: Zarr is ALWAYS generated (cannot be disabled)
```

> **Important — Zarr Band Schema is Fixed at Creation**
>
> Zarr data cube has fixed `(bands, time, y, x)` shape. The **band dimension is set when the Zarr store is first created and cannot be changed**:
>
> - Zarr created with `features_ratio: false, features_rvi: false, features_glcm: false` → **2 bands** (VV_dB, VH_dB)
> - Zarr created with `features_ratio: true, features_rvi: true` → **4 bands** (VV_dB, VH_dB, Ratio, RVI)
> - Zarr created with `features_glcm: true` → **12 bands** (4 core + 8 GLCM texture)
>
> `overwrite: true` re-processes existing months **within existing schema** — it does NOT change band count.
>
> **To add GLCM bands to existing 4-band Zarr:**
> You must use a **separate output directory**:
>
> ```yaml
> output:
>   base_dir: "./output_glcm"   # New directory — do not reuse existing output
> processing:
>   features_glcm: true
> ```

### Parallel and Memory Configuration

```yaml
parallel:
  enabled: true
  max_workers: 4         # Concurrent MGRS tiles
                         # Recommended: ≤16GB RAM: 2 | 32GB: 4 | ≥64GB: 6-8

memory:
  max_memory_gb: 'auto'  # 'auto' = auto-detect | number = manual override (GB)
  batch_strategy: 'auto' # auto | yearly | quarterly | monthly
  max_download_workers: 4
  scene_retry_timeout_seconds: 600   # Per-scene retry budget
  batch_max_retries: 2               # Batch-level retry count
  max_failed_ratio: 0.0              # Max allowed failed scene fraction (0 = zero tolerance)
  clear_cache_per_batch: true
  scene_max_retries: 3
```

### Processing Configuration

#### Common Processing Options (All Workflows)

```yaml
processing:
  target_resolution: 30.0          # Meters
  target_crs: null                 # null = auto-derive UTM zone from MGRS tile
  tile_clip: true                  # Clip outputs to MGRS tile boundary
  
  zarr_chunks:
    y: 512                         # Chunk size in pixels (cloud-optimized)
    x: 512
  
  cog_block_size: 256              # COG internal tile size
```

#### Monthly Workflow Processing Options

```yaml
processing:
  post_processing: true            # true  = hARDCp: composite + TV despeckle + features
                                   # false = ARDC:   composite only, no despeckle
  use_roi_mask: false
  mosaic_strategy: "mean"
  trim_fraction: 0.15              # Trimmed-mean clip fraction

  despeckle:
    monthly_despeckle: true
    method: "tv_bregman"
    kwargs:
      reg_param: 5.0               # TV regularization strength (higher = smoother)

  # Optional GLCM texture features (disabled by default)
  texture_features:
    enabled: false                 # Set true to enable texture band generation
    inputs: ["VV_dB", "VH_dB"]
    metrics: ["contrast", "homogeneity", "entropy", "correlation"]
    window_size: 5                 # Sliding window size (odd number)
    distance: 1                    # GLCM pixel-pair distance
    angles: [0, 90]                # Directions (results averaged)
    average_angles: true
    levels: 16                     # Quantization levels (16 or 32)
    vv_db_range: [-25, 5]
    vh_db_range: [-32, -5]

  zarr_time_fix:
    enabled: true                  # Auto-fix time-dimension ordering after processing
    create_backup: true
    backup_dir: null               # null = timestamped backup next to original
```

#### Scenes Workflow Processing Options

```yaml
processing:
  spatial_despeckle: false         # Per-scene spatial TV-Bregman filtering
  
  # Feature toggles (independent)
  features_ratio: true             # Generate Ratio = VH/VV
  features_rvi: false              # Generate RVI = 4*VH/(VV+VH)
  features_glcm: false             # Generate GLCM texture bands
  
  despeckle:
    method: "tv_bregman"
    kwargs:
      reg_param: 5.0

  # Optional: Generate monthly composites from scenes
  monthly:
    enabled: false                 # Set true to produce both scenes + monthlies in one run
    composite_method: "nanmedian"
    generate_cog: true
    generate_preview: true
  
  on_time_conflict: "skip"         # "skip" or "overwrite"
```

**Scenes Workflow Feature Bands:**

Based on `features_*` toggles, output bands vary:

| Configuration | Bands | Total Count |
|---------------|-------|-------------|
| All features disabled | VV_dB, VH_dB | 2 |
| `features_ratio: true` | VV_dB, VH_dB, Ratio | 3 |
| `features_ratio: true, features_rvi: true` | VV_dB, VH_dB, Ratio, RVI | 4 |
| `features_glcm: true` | VV_dB, VH_dB, Ratio, RVI + 8 GLCM | 12 |

#### Static Workflow Processing Options

```yaml
# Static workflow has minimal processing configuration
# All static layers are always generated

output:
  base_dir: "./output_static"      # Separate directory recommended
  overwrite: false                 # Skip if outputs already exist
```

Static layers generated:
- `local_inc_angle` — Local incidence angle
- `inc_angle` — Incidence angle
- `ls_map` — Layover/shadow mask
- `number_of_looks` — Multi-looking factor
- `rtc_anf_beta0` — RTC area normalization factor (beta0)
- `rtc_anf_sigma0` — RTC area normalization factor (sigma0)

### Logging Configuration

```yaml
logging:
  file_level: 'DEBUG'              # DEBUG | INFO | WARNING | ERROR
  console_level: 'WARNING'
  suppress_third_party: true
  log_file: './logs/s1grits_{workflow}_{timestamp}.log'
```

### Complete Configuration Examples

#### Minimal Monthly Config

```yaml
workflow: "monthly"

roi:
  manual_mgrs_tiles: ["50RKV"]
  flight_direction: "ASCENDING"
  polarization: "VV+VH"

time:
  years: [2024]
  months: [1, 2, 3]

output:
  base_dir: "./output"
```

#### Minimal Scenes Config

```yaml
workflow: "scenes"

roi:
  manual_mgrs_tiles: ["17MQV"]
  flight_direction: "DESCENDING"
  polarization: "VV+VH"

time:
  years: [2026]
  months: [1]

output:
  base_dir: "./output"
  
processing:
  features_ratio: true
  features_rvi: false
  features_glcm: false
  spatial_despeckle: false
  
  monthly:
    enabled: false               # Set true to also generate monthly composites
```

#### Minimal Static Config

```yaml
workflow: "static"

roi:
  manual_mgrs_tiles: ["46SEG"]
  flight_direction: "DESCENDING"
  polarization: "VV+VH"

output:
  base_dir: "./output_static"
  overwrite: false
```

---
## CLI Reference

S1-GRiTS provides **10+ commands** covering the full workflow: processing, catalog management, analysis, and mosaicking.

### Command Overview

| Command | Purpose | Workflow |
|---------|---------|----------|
| `s1grits process` (alias `s1grits process_monthly`) | Monthly composite time series | Monthly |
| `s1grits process_scenes` | Per-acquisition scene outputs | Scenes |
| `s1grits process_static` | Time-invariant static layers | Static |
| `s1grits catalog resync` | Rebuild catalog + STAC from filesystem (no re-processing) | All |
| `s1grits catalog doctor` | Check catalog/STAC/Zarr consistency (`--strict` to fail on warnings) | All |
| `s1grits catalog validate` | Validate catalog schema & STAC Item alignment | All |
| `s1grits catalog inspect` | Global coverage summary | All |
| `s1grits tile inspect` | Single-tile temporal completeness | All |
| `s1grits mosaic` | Multi-tile monthly mosaic | Monthly |
| `s1grits mosaic_scenes` | Multi-tile per-scene mosaic | Scenes |

### Help

```bash
s1grits --help
s1grits process --help
s1grits catalog --help
s1grits mosaic --help
```

---

### Processing Workflows

#### Monthly Composites

Generate monthly composite time series.

```bash
s1grits process --config config/s1grits_monthly.yaml
```

**What it does:**
1. Query ASF for RTC-S1 bursts in ROI and time range
2. Download and process bursts per MGRS tile
3. Create monthly median composites
4. Apply TV-Bregman spatial despeckle (if enabled)
5. Compute derived features (Ratio, RVI, GLCM)
6. Write Zarr data cube + COG + preview
7. Generate STAC Items and update catalogs

**Output:** `{base_dir}/{TILE}_{DIR}/zarr/S1_monthly.zarr`

---

#### Per-Scene Processing

Generate per-acquisition scene outputs with optional monthly compositing.

```bash
s1grits process_scenes --config config/s1grits_scenes.yaml
```

**What it does:**
1. Query ASF for RTC-S1 bursts
2. Group bursts by acquisition geometry (orbit, track, frame)
3. Process each acquisition group independently
4. Write per-track Zarr stores (accumulate time steps)
5. Optionally generate monthly composites from scenes
6. Write COG + preview per scene
7. Generate STAC Items per scene

**Output:** `{base_dir}/{TILE}/scenes_{DIR}_{bands}/zarr/s1grits_scenes_{TILE}_{DIR}_TK{track}_N{bursts}.zarr`

**Key difference from monthly workflow:**
- One Zarr store **per acquisition group** (not per tile)
- Higher temporal resolution (6-12 day revisit)
- Optional monthly compositing via `processing.monthly.enabled: true`

---

#### Static Layers

Generate time-invariant reference layers.

```bash
s1grits process_static --config config/s1grits_static.yaml
```

**What it does:**
1. Query ASF for RTC-STATIC products
2. Group by acquisition geometry (same as scenes workflow)
3. Download static layers per burst
4. Mosaic to MGRS tile grid per acquisition group
5. Write Zarr store (no time dimension) + COG per layer

**Output:** `{base_dir}_{DIR}_static/{TILE}_{DIR}/static/zarr/s1grits_static_{TILE}_{DIR}_TK{track}_N{bursts}.zarr`

**Static layers:**
- `local_inc_angle` — Local incidence angle
- `inc_angle` — Incidence angle
- `ls_map` — Layover/shadow mask (0=valid, 1=layover, 2=shadow)
- `number_of_looks` — Multi-looking factor
- `rtc_anf_beta0` — RTC area normalization factor (beta0)
- `rtc_anf_sigma0` — RTC area normalization factor (sigma0)

---

### Catalog Management

#### Resync Catalog

Rebuild `catalog.parquet` and STAC Items from the filesystem (no re-processing).

```bash
s1grits catalog resync --output-dir ./output
```

**When to use:**
- After manual file edits or deletions
- After interrupted workflow runs
- To regenerate STAC Items from existing COGs
- To fix catalog inconsistencies

**What it does:**
1. Scan all COG files in output directory
2. Extract metadata (datetime, tile, direction, bands, CRS, bounds)
3. Rebuild global `catalog.parquet`
4. Rebuild tile-level `catalog.parquet` files
5. Regenerate all STAC Item JSON files
6. Update STAC Collection JSON with new extent/counts

---

#### Validate Catalog

Check catalog schema and STAC Item alignment.

```bash
s1grits catalog validate --output-dir ./output
```

**Checks performed:**
- Parquet schema matches canonical 30-column schema
- All required columns present
- STAC Item JSON files exist for all catalog records
- STAC Item datetimes match catalog datetimes
- Asset hrefs in STAC Items are valid paths

**Exit codes:**
- `0`: All checks passed
- `1`: Validation errors found

---

#### Inspect Global Coverage

Show coverage summary for all tiles.

```bash
s1grits catalog inspect --output-dir ./output
```

**Example output:**

```text
Tile       Direction    Months  Expected  Missing  Complete  Range
50RKV      ASCENDING        24        24        0   100.0%   2024-01 ~ 2025-12
50RKU      ASCENDING        22        24        2    91.7%   2024-01 ~ 2025-12
17MPV      DESCENDING       18        24        6    75.0%   2024-01 ~ 2025-12
```

**Columns:**
- **Tile:** MGRS tile identifier
- **Direction:** ASCENDING or DESCENDING
- **Months:** Number of months with data
- **Expected:** Expected months based on date range
- **Missing:** Count of missing months
- **Complete:** Completeness percentage
- **Range:** Temporal extent

---

### Single-Tile Inspection

Show temporal completeness and missing months for a single MGRS tile.

```bash
# Show all directions for tile
s1grits tile inspect --tile 50RKV --output-dir ./output

# Filter by orbit direction (recommended)
s1grits tile inspect --tile 50RKV --direction ASCENDING --output-dir ./output
s1grits tile inspect --tile 50RKV --direction DESCENDING --output-dir ./output
```

**Example output (with `--direction ASCENDING`):**

```text
------------------------------------------------------------ Tile: 50RKV  |  ASCENDING ------------------------------------------------------------

ASCENDING
  Present months:  22
  Expected months: 24
  Date range:      2024-01 ~ 2025-12
  Completeness:    91.7%

  Missing months (2):
    - 2024-03  (no source data)
    - 2025-08  (COG exists but missing from catalog -- run resync)
```

**Interpretation:**
- **no source data:** ASF has no RTC-S1 data for this month
- **COG exists but missing from catalog:** Run `catalog resync` to fix
- **Zarr exists but no COG:** COG generation was disabled or failed

---

### Multi-Tile Mosaicking

#### Monthly Mosaic

Create multi-tile mosaic for a specific month.

```bash
# Default: EPSG:4326, VRT format
s1grits mosaic --month 2024-01 --direction ASCENDING --output-dir ./output

# Specify projection
s1grits mosaic --month 2024-01 --direction ASCENDING --crs EPSG:3857

# Keep native UTM projection (precise measurements)
s1grits mosaic --month 2024-01 --direction ASCENDING --keep-utm

# Output as physical COG file (for distribution)
s1grits mosaic --month 2024-01 --direction ASCENDING --format COG

# Merge both directions (ASCENDING primary, DESCENDING fills gaps)
s1grits mosaic --month 2024-01 --direction ALL

# Filter tiles by MGRS prefix (e.g., 50R zone only)
s1grits mosaic --month 2024-01 --direction ASCENDING --mgrs-prefix 50R

# Specify output directory
s1grits mosaic --month 2024-01 --direction ASCENDING --output ./results/mosaic/
```

**Format and projection options:**

| Parameter | Description |
|-----------|-------------|
| `--format VRT` | Virtual mosaic, no extra disk usage (default) |
| `--format COG` | Physical mosaic GeoTIFF (suitable for distribution) |
| `--crs EPSG:4326` | Reproject to WGS84 (default; wide-area visualization) |
| `--crs EPSG:3857` | Reproject to Web Mercator (web map services) |
| `--keep-utm` | Preserve native UTM projection, skip reprojection |

**Output naming:**
```
VRT: mosaic_2024-01_ASCENDING_EPSG4326.vrt
COG: mosaic_2024-01_ASCENDING_EPSG4326.tif
```

---

#### Scenes Mosaic

Create multi-tile mosaic for a specific acquisition date.

```bash
# Basic usage (all tiles for given date)
s1grits mosaic_scenes --date 2024-01-15 --direction ASCENDING --output-dir ./output

# Filter by MGRS prefix
s1grits mosaic_scenes --date 2024-01-15 --direction ASCENDING --mgrs-prefix 50R

# Date range (all scenes in range)
s1grits mosaic_scenes --start-date 2024-01-01 --end-date 2024-01-31 --direction ASCENDING

# Output format
s1grits mosaic_scenes --date 2024-01-15 --direction ASCENDING --format COG

# Specify output directory
s1grits mosaic_scenes --date 2024-01-15 --direction ASCENDING --output ./results/scenes_mosaic/
```

**Output naming:**
```
mosaic_scenes_20240115_ASCENDING.vrt
mosaic_scenes_20240115_ASCENDING.tif
```

**Key difference from monthly mosaic:**
- Works with per-scene COG files (not monthly composites)
- Date filtering instead of month filtering
- Supports date ranges (multiple scenes)

---

### GUI

Launch Streamlit web interface.

```bash
s1grits-gui

# Custom host/port
s1grits-gui --host 0.0.0.0 --port 8080 --no-browser
```

**Default:** http://127.0.0.1:8501

**GUI features:**
- Interactive data discovery and browsing
- Coverage reports and gap analysis
- Tile/scene inspection
- Mosaic creation interface
- Time series visualization

---

### Common Command Patterns

#### Process All Tiles for 2024

```bash
# Monthly workflow
s1grits process --config config/2024_monthly.yaml

# Scenes workflow  
s1grits process_scenes --config config/2024_scenes.yaml
```

#### Process Both ASCENDING and DESCENDING

```bash
# Edit config to set flight_direction: "ASCENDING"
s1grits process_scenes --config config/scenes_ascending.yaml

# Edit config to set flight_direction: "DESCENDING"
s1grits process_scenes --config config/scenes_descending.yaml
```

#### Resync Catalog After Interrupted Run

```bash
s1grits catalog resync --output-dir ./output
s1grits catalog validate --output-dir ./output
s1grits catalog inspect --output-dir ./output
```

#### Create Regional Mosaic

```bash
# Monthly mosaic for all 50R zone tiles
s1grits mosaic --month 2024-06 --direction ASCENDING --mgrs-prefix 50R --format COG --output ./mosaics/

# Merge both orbits
s1grits mosaic --month 2024-06 --direction ALL --mgrs-prefix 50R --format COG --output ./mosaics/
```

#### Check Coverage for Specific Tile

```bash
s1grits tile inspect --tile 17MPV --direction ASCENDING --output-dir ./output
s1grits tile inspect --tile 17MPV --direction DESCENDING --output-dir ./output
```

---
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
conda activate py312_s1grits_v100

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

`overwrite: true` re-processes existing months **within the existing schema** — it does not change band count.

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

See [Python API](#python-api) section for complete documentation.

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

## License & Citation

### License

Copyright 2026 KaeRao

Licensed under the **Apache License, Version 2.0**. See [LICENSE](LICENSE) for full text.

### Citation

If you use S1-GRiTS in your research, please cite both the paper and the software.

**Paper (under review):**

```text
Rao, K., Lei, L., Dong, S., Alvarez, C. I., Zou, L., Hu, Z., & Wu, Z. (2026).
Sentinel-1 Gridded Time Series (S1-GRiTS): Geometry-traceable SAR Data Cubes
for decadal vegetation monitoring in cloud-prone regions. (under review).
```

**Software:**

```text
KaeRao. (2026). S1-GRiTS: Sentinel-1 Gridded RTC Time Series Data Cube (Version 2.1.0).
GitHub: https://github.com/ottoKae/S1-GRiTS
```

**BibTeX:**
```bibtex
@article{rao2026s1grits,
  author  = {Rao, Keyi and Lei, Lei and Dong, Shixin and Alvarez, Cesar Ivan
             and Zou, Linxin and Hu, Zhongwen and Wu, Zhaocong},
  title   = {Sentinel-1 Gridded Time Series (S1-GRiTS): Geometry-traceable SAR
             Data Cubes for decadal vegetation monitoring in cloud-prone regions},
  year    = {2026},
  note    = {under review}
}

@software{s1grits2026,
  author       = {KaeRao},
  title        = {S1-GRiTS: Sentinel-1 Gridded RTC Time Series Data Cube},
  year         = {2026},
  version      = {2.1.0},
  url          = {https://github.com/ottoKae/S1-GRiTS},
  note         = {A companion paper is under review}
}
```

---

## Acknowledgements

**[@ottoKae](https://github.com/ottoKae)** designed and planned the entire S1-GRiTS project, conducted all real-world testing and validation, ensured end-user usability, and performed quality assurance of all deliverables.

The burst-to-MGRS-tile enumeration and spatial speckle filtering approaches draw heavily from the [dist-s1-enumerator](https://github.com/opera-adt/dist-s1-enumerator) project by **OPERA/JPL**. We gratefully acknowledge their foundational work.

**OPERA RTC-S1 Products:** S1-GRiTS is built on NASA's OPERA (Observational Products for End-Users from Remote Sensing Analysis) RTC-S1 (Radiometric Terrain Corrected Sentinel-1) products. We acknowledge the OPERA team at JPL for providing analysis-ready SAR data.

Code optimization and production-ready implementation were carried out with assistance from **[@claude](https://claude.ai)** (Anthropic).

---

## Contributing

S1-GRiTS is currently under active development. Contributions, bug reports, and feature requests are welcome via GitHub Issues.

### Development Setup

```bash
# Clone repository
git clone https://github.com/ottoKae/S1-GRiTS.git
cd S1-GRiTS

# Create development environment
conda env create -f environment.yml
conda activate py312_s1grits_v100

# Install in editable mode
pip install -e .

```
**Questions? Issues? Feature Requests?**

Open an issue on GitHub: https://github.com/ottoKae/S1-GRiTS/issues

---

*README last updated: 2026-06-17 | Version 2.1.0*
