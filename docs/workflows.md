[← Back to README](../README.md)

## Workflows

### Workflow 1: Monthly Composites (removed — see Per-Scene Processing)

> The standalone monthly-composite workflow (`s1grits process`) was **removed
> in v3.0.0**. Monthly composites are now produced by the scenes workflow: run
> **Workflow 2: Per-Scene Processing** and enable `processing.monthly` to emit
> the per-track `smonthly` composite cube alongside (or instead of, via
> `processing.monthly.only`) the per-scene product. This shares the
> bounded-memory blockwise pipeline and avoids a second full pass over the
> bursts.
>
> Monthly products written by earlier releases remain fully readable — the
> `monthly` STAC/catalog product type is retained, and `s1grits mosaic`
> continues to mosaic them.

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
        s1grits_scenes_{TILE}_{DIR}_TK{track}.zarr/   # Per-track cube
          ├── Ratio/     (time, y, x)      # All acquisitions for this track
          ├── VV_dB/     (time, y, x)
          ├── VH_dB/     (time, y, x)
          ├── RVI/       (time, y, x)
          └── time/      [2026-01-03, 2026-01-09, 2026-01-15, ...]
      cog/
        s1grits_scenes_{TILE}_{DIR}_TK{track}_{DATE}.tif
      preview/
        s1grits_scenes_{TILE}_{DIR}_TK{track}_{DATE}.png
    smonthly_{DIR}_{bands}/               # If monthly.enabled: true
      zarr/
        s1grits_smonthly_{TILE}_{DIR}_TK{track}.zarr/   # Per-track cube
      cog/
        s1grits_smonthly_{TILE}_{DIR}_TK{track}_{YYYY-MM}.tif
      preview/
        s1grits_smonthly_{TILE}_{DIR}_TK{track}_{YYYY-MM}.png
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

Each acquisition group (track) produces **one Zarr data cube** containing all
time steps. Store names key on the track only — the per-acquisition burst
count is time-varying (edge truncation, ASF gaps) and is recorded as
`n_bursts` provenance in the catalog, never in the filename:

**Example for Track 142:**
- Zarr: `s1grits_scenes_17MQV_DESCENDING_TK142.zarr`
- Time dimension: 5 acquisitions in January 2026
- Perfect spatial alignment across all time steps

**Example for Track 40:**
- Zarr: `s1grits_scenes_17MQV_DESCENDING_TK40.zarr`
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
s1grits static ensure --output-dir ./output --product-label smonthly_ASCENDING
```

For pixel-exact static/scenes cubes, run `process_scenes` first, use the same
`output.base_dir`, and set `static_layers.grid_reference: required`. Static
then adopts the corresponding scenes Zarr grid per direction and track. See
[Static and dynamic scenes: pixel-exact workflow](static_scenes_alignment.md).

#### Key Configuration

```yaml
workflow: "static"

# Static layers are always enabled; no temporal processing options
output:
  base_dir: "./output_static"   # Separate directory recommended
  overwrite: false              # Skip if outputs exist
                                # (legacy workflow: uses the v2 key natively)
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
