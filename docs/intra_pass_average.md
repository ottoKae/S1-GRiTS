# Optional intra-pass averaging

The default scenes workflow retains first-valid-pixel mosaicking, its paths
and its processing signatures. To produce a separate equal-weight average
of overlapping bursts within each acquisition, add this to a scenes config:

```yaml
roi:
  flight_direction: ASCENDING
  polarization: VV+VH
processing:
  target_resolution: 30
  resampling_method: nearest
  spatial_despeckle: false
  intra_pass_mosaic:
    method: average
  monthly:
    enabled: false
    only: false
static_layers:
  run_after_scenes: false
```

Run the existing `s1grits process_scenes --config <config.yaml>` command.
The optional strategy currently supports 30 m VV/VH scenes, with optional
Ratio, RVI and the existing scene GLCM preset. Filtering, monthly composition,
other polarizations, other resolutions and automatic static post-processing
are rejected before querying/downloading. An unsupported method or method
option is also an error. Omitting the strategy, or explicitly choosing
`first_valid_pixel`, retains the existing route.

## Numerical meaning

Acquisitions retain the existing `(pass_id, acq_group_id_within_mgrs_tile)`
grouping and separate relative-orbit stores. A calendar batch boundary does
not split an average acquisition. This combines bursts within a pass; it
does not fuse different passes, tracks or acquisition times into one image.

Each burst is mapped using exact coordinate transformation and nearest
neighbor sampling. Aligned inputs use direct copies. The average operates
on linear power before dB conversion, feature derivation and tile clipping.
A burst contributes only where both its VV and VH are finite, positive and
not source NoData. The same contributing pairs determine both means; this
is deliberately stricter than the default route's independent polarization
selection. Eligible loaded assets are sorted by OPERA product ID, summed in
float64, divided by their count and cast to float32. Ratio and RVI are then
derived from the averaged powers; GLCM operates on the resulting dB bands.

The Zarr variable `n_contributors` is uint32: 0 means no contributing pair;
1 means a single source; larger values identify overlap. Clipped pixels have
count 0. It counts spatial contributors within one acquisition, not temporal
observations (`n_obs`). COG and preview exports contain radiometric/feature
bands; inspect the Zarr asset for contributor counts.

## Independent product identity and reading

Outputs are rooted at:

```text
<output.base_dir>/derived/intra_pass_average/
  catalog.parquet
  <tile>/catalog.parquet
  <tile>/scenes_<direction>[_features]_average_<signature>/zarr/...
```

Each signature includes the strategy version, mapping/validity/weight rules,
numerical types, operator order, feature preset and clipping choice. Threads
and chunk size do not change that identity. Catalog and STAC IDs also include
the track and signature. Different configurations coexist without replacing
each other's catalog rows. A different signature/grid or a changed source
list for an existing acquisition is rejected before opening its store for
writing; use a fresh output root to recompute that acquisition. Existing
default Cube stores are not migrated. On Windows, use a short output root
to leave room for Zarr's temporary filenames.

Use this derived root when running `s1grits catalog resync` or reading:

```python
from s1grits.resolver import CubeResolver

cube = CubeResolver("output/derived/intra_pass_average")
records = cube.query(tile_id="17MPU", product_type="scenes")
print(records[["processing_signature", "track", "datetime"]])
# Select the required signature explicitly when multiple variants exist.
selected = cube.query(processing_signature="<signature from the catalog>")
dataset = cube.open(selected.iloc[0])
```

The processing contract is recorded in Zarr, catalog and STAC. Zarr also
records `numeric_processing` and the ordered source lists in `acquisitions_by_time`;
catalog/STAC preserve the corresponding loaded OPERA and burst IDs. These
are eligible asset lists, not a saved per-pixel winner or contributor bitmap.
The validity rule and original assets are needed to reconstruct individual
contributors; count alone cannot identify them. This contract is separate
from the existing monthly provenance sidecars.

An existing first-valid-pixel Cube cannot supply the lost overlap values.
Average must read the original burst assets; the existing burst cache can
reuse retained inputs. Downstream analysis of an already built average Cube
uses the same read-only Resolver as other Cube products.
