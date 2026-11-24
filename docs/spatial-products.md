# Spatial units and local products

The local spatial commands select independent spatial units and read existing
S1-GRiTS Zarr stores. Existing MGRS production commands and layouts are unchanged.

| Spatial mode | Input | Output |
|---|---|---|
| `geographic_0p1` | Polygon AOI, optional candidate cell IDs | One Zarr per complete 0.1-degree cell and direction/track/variant |
| `vector_grid` | Shapefile, GeoJSON or explicitly selected GPKG layer | One Zarr per original polygon feature and direction/track/variant |
| `points` | CSV or point vector | Parquet observations keyed by point and source; optional CSV |
| `point_buffer` | Point input, radius in metres | One Zarr per fixed circle and direction/track/variant |

```powershell
s1grits regions export --config config/spatial_regions.example.yaml --dry-run
s1grits regions export --config config/spatial_regions.example.yaml
s1grits points extract --config config/spatial_points.example.yaml --dry-run
s1grits points extract --config config/spatial_points.example.yaml
```

Paths in a YAML file are relative to that file. Set the example paths before use.
`source.root` can be a Zarr store or an existing directory containing
`catalog.parquet`. Source imagery and metadata are read-only. Outputs must use
a separate directory. No new remote `fetch` command is delivered in P1/P2.

## Selection and grids

`geographic_0p1` reuses the existing `deg10_x{ix}_y{iy}` cell identity. Positive-area
AOI intersections select whole cells; AOI boundaries do not crop the selected
cells. `spatial.unit_ids` optionally selects a subset of these candidates. The
preflight reports a readable error before materializing more than 2,000,000 cells.
Cell masks use west/south-inclusive, east/north-exclusive pixel-centre membership.

`vector_grid` preserves feature IDs, holes and multipart polygons. A Shapefile
requires its `.shp`, `.shx`, `.dbf` and `.prj` components. Use an explicit unique
`input.id_column` for identifiers that remain stable when the input file is rebuilt.
Input hashes bind each job to its original selection.

For both fixed-grid modes, specify `grid.mode: target_crs`, a suitable common
metre-based CRS, resolution (10 or 30 metres), and `origin`. This is the pixel grid
inside each spatial unit; 0.1 degrees is the unit boundary, not the pixel size.
The arrays use outward-snapped bounding boxes and a separate `roi_mask`.

For circles, set `spatial.mode: point_buffer` and `spatial.radius_m: 100000`.
The default circle grid is local AEQD. To compare different circles pixel by pixel,
use a common target CRS, resolution and origin. Radius is geodesic distance on WGS84.

## Provenance and scientific limits

Regions keep direction and verified relative orbit separate. Sources must have
known compatible processing semantics, VV/VH units and nominal coverage geometry.
Monthly products are spatial mosaics of existing monthly values, never newly
computed monthly aggregates. Cross-store scene mosaics require reliable acquisition
identity; equal dates or six-day `pass_id` buckets are insufficient.

The default `source_strategy: S0` fixes one owner using nominal boundary distance
and stable source-ID ties. `S1` permits deterministic fallback only after confirmed
owner absence/invalidity. VV/VH always come from the same source. A read failure
does not authorize fallback. Local product owners cannot establish native burst
ownership: burst switch metrics remain null.

Each regional store contains `roi_mask`, `owner_index`, `source_index`, VV/VH and,
where available, the inherited integer `n_obs`. Its original definition is retained;
unknown historical definitions remain unknown. `n_obs` must not be interpreted as
an independent acquisition count. Optional static layers follow the fixed owner.

Historical stores without declared units remain usable for original-value point
extraction. Region preflight rejects unknown units; variable names alone are not
evidence of the numeric domain. A verified provenance companion can supply missing
semantics without changing the source store:

```yaml
source:
  root: /path/to/catalog-or-store
  dataset_root: /path/to/original-dataset
  metadata_root: /path/to/detached-metadata-only-companion
  product_type: smonthly
```

`dataset_root` is required when the selection catalog or individual store is not
the original dataset root. If `source.root` is already that root, omit it.
`metadata_root` is optional for datasets with attached `provenance/current.json`.
The reader uses GRiTS-Resolver's `ProvenanceView`; it never attaches or edits
metadata in a frozen MGRS dataset. The compatible candidate is `0.3.1rc1`,
available from the following pinned public source commit:

```powershell
pip install "grits-resolver @ git+https://github.com/ottoKae/GRiTS-Resolver.git@9a92523d8cdbf20ec0b3b377c2265875a6521d73"
python -m s1grits.cli regions export --config /path/to/spatial_config.json --dry-run
```

Install this optional reader in the environment used for spatial exports.
YAML and JSON configuration files use the same loader.

Evidence is bound to the selected store, time layer, variable and output generation.
Execution receipts and historical code/configuration evidence retain their distinct
basis. An annotation for two months does not establish units for another month.
The manifest and source tables preserve release IDs, output IDs and evidence refs;
source structure and metadata releases are checked at checkpoints. Conflicting
declarations fail preflight. Point tables retain time-specific units, while a
regional job requires consistent semantics across its selected times.

Cross-store monthly mosaics also require matching known aggregation domains;
the domain of stored dB values alone does not establish the domain of monthly
aggregation. Declared nonidentity scale/offset is rejected for regional exports;
original-value point extraction remains available. Historical physical quantities
and unique-acquisition counts remain unknown unless independently established.

Point long tables use nullable Float64 for the shared `value` column and record
the original `source_dtype` in `sources.parquet`. Current float32/float64
backscatter and uint8 counts retain their numeric values. Per-variable missing
values, repeated timestamps and original time indices are preserved. Point values
are sampled at native pixel centres without interpolation.

Each observation is one point, source store, original time index and variable.
Overlapping stores and different tracks remain separate rows; the extractor does
not average them. Join `observations/` to `sources.parquet` on `source_id` to inspect
`direction`, `relative_orbit`, `track_members`, `geometry_group_id` and `store_path`.
`track_mode` is `single_track`, `mixed_tracks` or `unknown`. For an already mixed
source product, `relative_orbit` remains null and `track_members` retains the full
declared list; those fields cannot undo the source product's mixing.

Declared product IDs, versions, production times, burst IDs and OPERA IDs are
preserved from source metadata, with time-layer declarations taking precedence.
`provenance_level=source_product` distinguishes this from pixel-level attribution.
`burst_provenance_scope` and `opera_provenance_scope` distinguish
`time_layer_candidates`, `store_candidates` and `unknown`. Even a nonempty candidate
list does not identify which burst supplied this point. Missing lists stay null;
a store-level `n_bursts` count is not an ID list. Monthly point records do not
reconstruct individual acquisitions that were discarded when the source was built.

## Storage, budgets and recovery

Regions are stored beneath `<output>/<spatial_mode>/<internal_region_id>/` with
direction/track/variant subdirectories. `regions.parquet` preserves the input units;
`region_catalog.parquet` locates products and records coverage/source switches;
`sources.parquet` and each `source_map.parquet` retain provenance and failures.
Points use `points.parquet`, `sources.parquet`, `observations/` and
`point_summary.parquet`. Internal static resume fragments live in `.state/`.

Dry-run reports the full selected time span and all units, including dynamic
source indices, masks and static arrays. Values are uncompressed planning
estimates; point rows are estimated at 200 bytes. Actual compressed filesystem
size is not guaranteed. The greater-than-50-GB threshold is decimal and requires
`--confirm-large-job`. Disk checks additionally preserve the existing configured
free-space reserve. Confirmation cannot override insufficient disk space.

Successful work units are recorded only after their writes finish. Resume reuses
the same manifest and rewrites incomplete units without appending duplicate
observations. Input, source metadata/time-index or config changes require a new
output directory. All sources must remain frozen during the task; structural
provenance revisions do not checksum image chunks. The implementation does not
hash entire historical imagery archives.

Read failures are recorded as failures, separately from scientific NoData.
Partial results remain readable and the CLI exits nonzero. These commands do not
run the MGRS Catalog validator on point/region products.

These local commands do not download remote imagery or integrate with the Web
console. For direct OPERA acquisition, see [OPERA spatial tools](opera-spatial.md).
Small-file regression tests are included in `tests/test_spatial*.py`,
`tests/test_points.py` and `tests/test_regions.py`; they do not establish
production-scale capacity or performance.
