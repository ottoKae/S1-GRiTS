[← Back to README](../README.md)

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
- Perfect spatial alignment across all time steps within a group
- No geometric reprojection artifacts
- Temporal coherence for interferometric applications
- Efficient append-only updates

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
| **CLI Command** | `s1grits process_scenes` | `s1grits process_static` |
| **Config Template** | `s1grits_scenes.yaml` | `s1grits_static.yaml` |
| **STAC Collection** | `s1grits-scenes` (+ `s1grits-smonthly`) | `s1grits-static` |
| **Typical Data Volume** | ~2-3 GB/tile/year (Zarr) | ~50 MB/tile (one-time) |
| **Processing Time** | Moderate (per-scene outputs) | Fast (static, no time series) |

---
