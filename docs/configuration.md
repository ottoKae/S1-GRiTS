[← Back to README](../README.md)

## Configuration Reference

S1-GRiTS workflows are configured via YAML files in the `config/` directory.

### Configuration Templates

| File | Workflow | Description |
|------|----------|-------------|
| `s1grits_scenes.yaml` | Per-scene processing (+ monthly composites) | High-temporal-resolution outputs; enable `processing.monthly` for composites |
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

  # v3 output policies (scenes workflow) — store level vs month level:
  existing_store: "resume"   # "resume": adopt an existing store's locked grid
                             #           and append (incremental, default)
                             # "rebuild-incompatible": rebuild any store whose
                             #           grid/bands are incompatible; compatible
                             #           stores are still resumed, never wiped
  existing_month: "skip"     # "skip": keep an already-written month
                             # "overwrite": delete + recompute that month

  formats:
    cog: true            # Generate COG files (optional)
    preview: true        # Generate preview PNGs (optional)
                         # Note: Zarr is ALWAYS generated (cannot be disabled)

preflight:
  disk:
    mode: "warn"         # warn | fail | off — checked BEFORE downloads start
    min_free_gb: 50
```

> **Deprecated v2 keys** — `output.overwrite` (== `existing_store:
> rebuild-incompatible` when true) and `output.on_time_conflict` (==
> `existing_month`) are still accepted with a deprecation warning; explicit
> v3 keys win. The legacy monthly/static workflows use `overwrite` natively.

> **Important — Zarr Band Schema is Fixed at Creation**
>
> Zarr data cube has fixed `(bands, time, y, x)` shape. The **band dimension is set when the Zarr store is first created and cannot be changed**:
>
> - Zarr created with `features_ratio: false, features_rvi: false, features_glcm: false` → **2 bands** (VV_dB, VH_dB)
> - Zarr created with `features_ratio: true, features_rvi: true` → **4 bands** (VV_dB, VH_dB, Ratio, RVI)
> - Zarr created with `features_glcm: true` → **12 bands** (4 core + 8 GLCM texture)
>
> `existing_month: "overwrite"` re-processes existing months **within the existing schema** — it does NOT change band count.
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
  max_workers: 4         # Concurrent MGRS tiles; "auto" sizes from CPU cores
                         # and RAM / the ~12 GB blockwise per-tile working set.
                         # Manual guide: ≤16GB RAM: 2 | 32GB: 4 | ≥64GB: 6-8

memory:
  max_memory_gb: 'auto'  # RAM assumed by the 'auto' batch estimator ('auto' =
                         # detect via psutil). NOT a hard cap; ignored when
                         # batch_strategy is set explicitly.
  batch_strategy: 'auto' # auto | yearly | quarterly | monthly. An explicit
                         # value is honored as-is, including in parallel mode
                         # (the per-worker RAM budget only tunes 'auto').
  burst_cache_dir: null  # Optional on-disk burst cache shared across tiles/runs
  max_download_workers: 4
  scene_retry_timeout_seconds: 600   # Per-scene retry budget
  batch_max_retries: 2               # Batch-level retry count
  max_failed_ratio: 0.0              # Max allowed failed scene fraction (0 = zero tolerance)
  clear_cache_per_batch: true
  scene_max_retries: 3
```

The scenes workflow also supports per-worker runtime limits. These are applied
in the parent process before the process pool is created, then re-applied by
each worker initializer before task work begins.

```yaml
runtime:
  enabled: true
  gdal_cachemax_mb: 512       # Per-process GDAL cache cap
  gdal_num_threads: 1         # GDAL internal threads per worker
  omp_num_threads: 1          # OpenMP-backed kernels
  openblas_num_threads: 1     # OpenBLAS-backed NumPy/SciPy kernels
  mkl_num_threads: 1          # MKL-backed NumPy/SciPy kernels
  blis_num_threads: 1
  veclib_maximum_threads: 1
  numexpr_num_threads: 1
```

### Processing Configuration

### Static workflow configuration

Run static against the same output root after scenes so every static track can
adopt its dynamic grid:

```yaml
roi:
  track_numbers: [40]             # Optional relative-orbit filter

output:
  formats:
    zarr: true                    # Canonical catalogued static asset

static_layers:
  enabled: true
  grid_reference: required        # required | auto | tile
  reference_product_label: null   # Pin a scenes_* variant if grids differ
  layers:
    local_inc_angle: true
    inc_angle: true
    ls_map: true
    number_of_looks: true
    rtc_anf_beta0: true
    rtc_anf_sigma0: true
  target_resolution: 30.0
  zarr_chunks: {y: 512, x: 512}
  cog_block_size: 256

  # ASF RTC-STATIC query controls
  query_batch_size: 50
  query_max_results: 5000
  query_max_retries: 5
  query_retry_base_delay: 2.0
```

`grid_reference: required` is recommended for machine learning: a matching
scenes Zarr must exist for every direction/track, and static adopts its CRS,
affine transform, shape, coordinates, and `grid_id`. See
[the pixel-exact static/scenes workflow](static_scenes_alignment.md).

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
```

> Month skip-vs-recompute is controlled by `output.existing_month`, **not** a
> processing-level key — a `processing.on_time_conflict` entry is ignored and
> the workflow warns if it finds one.

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
                                   # (legacy workflow: uses the v2 key natively)
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


---

## Reference: keys added or not covered above (complete as of v2.3.3)

Every key below is read by the workflows (the whitelist in
`src/s1grits/config_schema.py` is the source of truth; `s1grits doctor` and
the startup warning both flag unknown keys). `tests/test_config_reference_docs.py`
keeps this document in lockstep with that whitelist.

### Metadata — layered product registry

Product definitions (STAC collection ids, dimension layouts, guaranteed
bands, variant fields) resolve through three layers; the effective registry
is a pure function of the package version + the workflow config, never of
the current working directory:

1. **Built-in defaults** ship inside the package
   (`s1grits.product_registry.DEFAULT_REGISTRY`) — official workflows need
   no registry file at all.
2. `metadata.product_config` — optional path to an overlay YAML whose
   `products:` entries are **merged per product** over the built-ins: an
   unknown product type is added (must define at least `collection_id`), a
   known one is field-merged (e.g. tweak `variant_fields` of `scenes`
   without restating the rest). A file declaring top-level `replace: true`
   replaces the registry wholesale instead (the legacy semantics).
3. `metadata.products` — the same `products:`-shaped mapping **inline** in
   the workflow YAML, merged last. This is the recommended way for external
   projects to define a custom Data Cube: one self-contained config file,
   no dependency on this repository's config tree.

```yaml
metadata:
  products:
    my_flood_cube:            # new product type
      collection_id: myproj-flood
      derived_from: ["VV_dB"]
    scenes:                   # tweak a built-in product
      variant_fields:
        - processing.spatial_despeckle
        - processing.features_ratio
```

> **Deprecated (removal in v3.0.0):** with neither override set, a
> `config/s1grits_products.yaml` found under the *current working directory*
> is still auto-loaded with legacy replace semantics, with a deprecation
> warning when it differs from the built-ins.

### ROI

- `roi.min_tile_coverage_frac` — minimum fraction of an MGRS tile the ROI must
  cover for the tile to be processed (filters slivers on ROI boundaries).

### Time (legacy forms)

- `time_range` — legacy alias for the `time` section, still accepted.
- `time.full_mode`, `time.years_mode` — legacy verbose forms of
  `time.full` / `time.years`.

### Output (legacy / deprecated)

- `output.disk_warn_gb` — deprecated; use `preflight.disk` (`mode`,
  `min_free_gb`).
- `output.layout_mode` — legacy monthly workflow only.

### Query (CMR/ASF metadata search)

- `query.cmr_timeout_seconds` — per-request CMR timeout.
- `query.max_retries` / `query.retry_backoff_seconds` — metadata-query retry
  policy (exponential backoff).
- `query.max_workers` — parallel metadata queries.
- `query.footprint_lookback_months` — how far back the track-footprint query
  looks when establishing each track's full burst set.
- `query.footprint_cache_ttl_days` — on-disk footprint cache lifetime.

### Memory (bounded-memory pipeline, v2.3.3)

- `memory.download_prefetch` — download batch N+1 on a background thread while
  batch N is processed. Holds ONE extra batch resident; the demand-aware
  `auto` batch strategy accounts for this automatically.
- `memory.batch_spill` — spill each batch's decoded burst arrays to
  per-process `.npy` files and use read-only memmaps: byte-identical values,
  and the batch's dominant memory term becomes file-backed/reclaimable
  instead of anonymous RSS. Needs ~one batch of disk in `memory.spill_dir`.
- `memory.spill_dir` — spill location (default `{output.base_dir}/.spill`;
  point it at fast local scratch). Interrupted runs may leave orphan dirs —
  `s1grits doctor` detects them.
- `memory.windowed_burst_reads` — bursts held in the on-disk burst cache are
  window-read straight from the cached GeoTIFF (no full decode, no `.npy`
  copy). Requires `memory.burst_cache_dir`; warns and no-ops otherwise.

### Processing

- `processing.incomplete_acquisition` — policy for acquisitions with a genuine
  interior gap: `skip` (default), `write` (keep with NoData gap), `abort`.
- `processing.interior_hole_max_frac` — interior-NoData fraction of the tile
  above which an acquisition counts as incomplete (default `0.005`).
- `processing.require_complete_bursts` — back-compat alias for the `abort`
  policy.
- `processing.monthly.only` — produce monthly composites without writing the
  per-scene product.
- `processing.monthly.blockwise_threads` — threads per tile worker for
  blockwise writes (`auto` divides cores across `parallel.max_workers`); the
  scenes writer reuses this knob.
