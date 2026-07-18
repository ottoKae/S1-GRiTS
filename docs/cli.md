[← Back to README](../README.md)

## CLI Reference

S1-GRiTS provides **10+ commands** covering the full workflow: processing, catalog management, analysis, and mosaicking.

### Command Overview

| Command | Purpose | Workflow |
|---------|---------|----------|
| `s1grits process` (alias `s1grits process_monthly`) | Monthly composite time series | Monthly |
| `s1grits process_scenes` | Per-acquisition scene outputs | Scenes |
| `s1grits process_static` | Time-invariant static layers | Static |
| `s1grits catalog resync` | Rebuild catalog + STAC from filesystem (no re-processing) | All |
| `s1grits doctor` | Preflight: environment, config, disk, store-grid consistency, resource plan (`--config`, `--network`) | All |
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
5. Optionally generate monthly composites from QC-passing acquisitions
6. Write COG + preview per scene
7. Generate STAC Items per scene

**Output:** `{base_dir}/{TILE}/scenes_{DIR}_{bands}/zarr/s1grits_scenes_{TILE}_{DIR}_TK{track}.zarr`

**Key difference from monthly workflow:**
- One Zarr store **per acquisition group** (not per tile)
- Higher temporal resolution (6-12 day revisit)
- Optional monthly compositing via `processing.monthly.enabled: true`

**Monthly-only mode:**

Set `processing.monthly.enabled: true` and `processing.monthly.only: true` to
run acquisition QC and write only `smonthly_*` products. This skips `scenes_*`
Zarr/COG/preview outputs while keeping the same interior-hole and incomplete
acquisition filtering before monthly compositing.

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

### Web interface

Launch the browser UI — interactive data discovery, coverage reports, tile/scene
inspection, mosaic creation, and time-series visualization. See `docs/webapp.md`.

```bash
s1grits serve --root <workspace>

# Custom host/port
s1grits serve --root <workspace> --host 0.0.0.0 --port 8080
```

> The legacy Streamlit GUI (`s1grits-gui`) was removed in v3.0.0; `s1grits serve`
> is its replacement.

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
