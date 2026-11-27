[← Back to README](../README.md)

## Architecture

S1-GRiTS uses a **Zarr-first data cube architecture**, with the existing production workflow and explicit optional analysis tools. See the [V4 runtime call paths](call_flow.md) for CLI, Python and Web ownership and the distinction between production, views and derived outputs.

### Zarr-First Philosophy

**Zarr is the authoritative primary product** — a time-series data cube that accumulates all acquisitions incrementally. COG and preview files are **optional derivative exports** generated from Zarr.

**Why Zarr-first matters:**
- **Shared grid:** All time steps in a store share the same spatial grid and CRS, allowing pixel-wise time-series access
- **Incremental updates:** New acquisitions append to existing Zarr stores without reprocessing historical data
- **Cloud-optimized:** Chunked storage (512×512 pixels) enables efficient parallel reads via Dask/xarray
- **Multi-dimensional:** Direct access to time-series slicing, spatial subsetting, and statistical aggregation
- **Reproducible exports:** COG files can be regenerated from Zarr; COGs alone do not preserve the complete Cube metadata and support arrays

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

S1-GRiTS uses an **acquisition group strategy** to keep observations from different
relative orbit tracks separate while mapping each store to a fixed target grid.

**How it works:**
1. **Burst enumeration:** Query ASF for all RTC-S1 bursts intersecting the MGRS tile
2. **Group acquisitions:** Bursts from the same acquisition are mosaicked together, preserving their relative orbit track and flight direction
3. **One Zarr store per track:** Within a tile, direction and product variant, each relative orbit track has its own Zarr store; acquisitions append as time steps on the store's fixed grid

**Example:**
```
MGRS Tile 17MQV, DESCENDING orbit:
  ├── Acquisition Group 1: Track 142
  │   └── Zarr: s1grits_scenes_17MQV_DESCENDING_TK142.zarr
  │       ├── 2026-01-03 acquisition
  │       ├── 2026-01-09 acquisition
  │       └── 2026-01-15 acquisition
  │
  └── Acquisition Group 2: Track 40
      └── Zarr: s1grits_scenes_17MQV_DESCENDING_TK40.zarr
          ├── 2026-01-02 acquisition
          ├── 2026-01-08 acquisition
          └── 2026-01-14 acquisition
```

**Benefits:**
- A consistent output grid across time steps within a store
- Separate track identity and acquisition provenance
- Incremental updates when grid and processing identity remain compatible

Grid alignment does not guarantee subpixel registration or eliminate reprojection
effects. The default 30 m route uses exact coordinate transformation and nearest-neighbor
resampling where mapping is needed; see the [grid and resampling contract](target-grid-resampling-v1.md).
The input is RTC backscatter intensity, so this architecture does not provide the
complex phase data required for interferometric coherence analysis.

### Workflow Comparison

S1-GRiTS provides two specialized workflows for different analysis needs. Monthly
composites are produced by the scenes workflow (enable `processing.monthly`, which
emits the per-track `smonthly` product); the standalone monthly workflow was
removed in v3.0.0.

| Aspect | **Per-Scene Processing** | **Static Layers** |
|--------|--------------------------|-------------------|
| **Purpose** | Event detection + monthly composites | Terrain reference |
| **Temporal Resolution** | Per-acquisition (6-12 day revisit); optional monthly | Timeless |
| **Primary Use Case** | Rapid change, disaster response, time series | Incidence angle correction, masking |
| **Output Zarr** | One per acquisition group (+ `smonthly` per track) | One per acquisition group |
| **CLI Command** | `s1grits process_scenes` | integrated YAML / `s1grits static ensure` |
| **Config Template** | `s1grits_scenes.yaml` | `static_layers` in the scenes YAML |
| **STAC Collection** | `s1grits-scenes` (+ `s1grits-smonthly`) | `s1grits-static` |
| **Typical Data Volume** | ~2-3 GB/tile/year (Zarr) | ~50 MB/tile (one-time) |
| **Processing Time** | Moderate (per-scene outputs) | Fast (static, no time series) |

---
