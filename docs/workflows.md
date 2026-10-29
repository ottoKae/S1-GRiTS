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

<a id="static-alignment"></a>

### Workflow 3: Paired static layers

RTC-STATIC supplies `local_inc_angle`, `inc_angle`, `ls_map`,
`number_of_looks`, `rtc_anf_beta0` and `rtc_anf_sigma0`. These reference
arrays have spatial dimensions `(y, x)` and no time dimension.

#### Production order and one output root

For new data, use the scenes YAML and its integrated static post-stage:

```yaml
static_layers:
  run_after_scenes: true
  grid_reference: required
  on_failure: fail
```

```bash
s1grits process_scenes --config config/s1grits_scenes.yaml
s1grits catalog resync --output-dir ./output
```

The YAML's `output.base_dir` must match `--output-dir`. The static post-stage
runs after the dynamic output lock is released and only for successfully
created tiles/tracks. For dynamic products that already exist:

```bash
s1grits static ensure --output-dir ./output --product-label smonthly_ASCENDING
```

Keep dynamic and static products in the same data root and Catalog:

```text
{base_dir}/
  catalog.parquet
  {TILE}/
    catalog.parquet
    scenes_{DIR}_{label}/zarr/<dynamic-store>.zarr/
    smonthly_{DIR}_{label}/zarr/<monthly-store>.zarr/
    static_{DIR}/zarr/<static-store>.zarr/
```

Do not create a separate static data root or standalone static YAML. The
reference is selected by tile, direction, track and product label. If several
dynamic variants have different grids, set
`static_layers.reference_product_label` explicitly. With `grid_reference:
required`, a missing or ambiguous reference stops the run. `auto` can fall
back to the legacy tile grid; `tile` always uses it.

#### Alignment contract

Paired dynamic and static stores have identical CRS, affine transform,
width, height, `x`, `y` and `grid_id`, and share the track-level
`geometry_group_id`. Static provenance retains `grid_source`,
`reference_grid_id`, `reference_product_label` and `reference_zarr_path`.
Opening existing static stores validates the grid and required bands;
`catalog resync` reconstructs the pairing from Zarr attributes.

#### Reading and machine-learning inputs

Select a track when a tile contains multiple acquisition geometries:

```python
from s1grits.resolver import CubeResolver

r = CubeResolver("./output")
ds = r.open_stack(
    "17MPU", ["scenes", "static"],
    direction="DESCENDING", track=40,
    chunks={"time": 1, "y": 512, "x": 512},
)
```

Static bands remain 2-D by default; broadcast only in the downstream patch
or model batch. `open_stack(..., broadcast_static=True)` provides an explicit
lazy compatibility view. New stores use the identical-grid path. Legacy
tile-grid stores require an integer-pixel lattice match; the resolver does
not resample their values. Ambiguous tracks or geometry groups are rejected.

To write a training store with dynamic arrays at the root and 2-D static
arrays under `static/`:

```python
r.materialize_training_cube(
    "17MPU", "training/17MPU_DESCENDING_TK40.zarr",
    direction="DESCENDING", track=40,
)
```

Cross-sensor reading and versioned processing evidence use the separate
[GRiTS-Resolver package](https://github.com/ottoKae/GRiTS-Resolver).
Choose a release compatible with the dataset's metadata version; the old
v0.1.0 installer example is not a recommendation for new v2 metadata.
Installation and reader usage are maintained in that repository.
