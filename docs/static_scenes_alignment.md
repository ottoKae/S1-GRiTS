# Static and dynamic scenes: pixel-exact workflow

This workflow writes RTC-STATIC geometry layers directly on the locked grid of
the matching `scenes` or monthly-only `smonthly` Zarr store. The join key is
`tile + flight direction + track` (`geometry_group_id`), and no spatial
resampling is performed by the resolver.

## Recommended execution order

Use the scenes YAML integrated post-stage for new data:

```bash
s1grits process_scenes --config config/s1grits_scenes.yaml
s1grits catalog resync --output-dir ../outputs
```

Enable it by adding this block to the scenes YAML:

```yaml
static_layers:
  run_after_scenes: true
  grid_reference: required
  on_failure: fail
```

All six raw RTC-STATIC layers are downloaded. The
post-stage runs after the scenes output lock is released and only for tiles and
tracks whose scenes Zarr stores were successfully created.

For dynamic products that already exist, use the catalog-driven second entry:

```bash
s1grits static ensure --output-dir ../outputs --product-label smonthly_ASCENDING
```

For each static acquisition group, the workflow finds the matching dynamic
store. Monthly-only archives use
`smonthly_{direction}*/zarr/s1grits_smonthly_{tile}_{direction}_TK{track}.zarr`;
per-acquisition archives use the equivalent `scenes_*` path.
It copies that store's CRS, affine transform, shape, `x`, `y`, and `grid_id`,
then mosaics downloaded RTC-STATIC rasters directly onto that grid.

If several scenes variants exist and their grids differ, set
`static_layers.reference_product_label` to the desired directory name. In
`required` mode a missing or ambiguous reference stops the run.
`grid_reference: auto` falls back to the legacy MGRS-tile grid; `tile` always
uses that legacy grid. Use `required` for production machine-learning cubes.

## Alignment and metadata contract

Matching scenes and static stores have:

- identical CRS, affine transform, width, height, `x`, and `y` coordinates;
- identical canonical `grid_id`;
- identical track-level `geometry_group_id`;
- static arrays shaped `(y, x)` and dynamic arrays shaped `(time, y, x)`;
- static provenance attributes `grid_source=workflow_scenes`,
  `reference_grid_id`, `reference_product_label`, and `reference_zarr_path`.

Opening an existing static Zarr validates shape, CRS, transform, coordinates,
and required bands. `catalog resync` reconstructs the grid and reference fields
from Zarr attributes, so rebuilding Parquet/STAC metadata preserves the link.

## Resolver and machine learning

Select a track whenever a tile contains multiple acquisition geometries:

```python
from s1grits.resolver import CubeResolver

r = CubeResolver("../outputs")
ds = r.open_stack(
    "17MPU", ["scenes", "static"],
    direction="DESCENDING", track=40,
    chunks={"time": 1, "y": 512, "x": 512},
)
```

Static bands remain 2-D by default and broadcast only in downstream patch or
model-batch operations. `open_stack(..., broadcast_static=True)` provides an
explicit lazy compatibility view. New stores take the identical-grid fast
path. Legacy tile-grid stores are accepted only when their resolution and
origin prove an integer-pixel lattice match.

Create a machine-learning store with dynamic arrays in the root and 2-D static
arrays in the `static/` subgroup:

```python
r.materialize_training_cube(
    "17MPU", "training/17MPU_DESCENDING_TK40.zarr",
    direction="DESCENDING", track=40,
)
```

If `track` or `geometry_group_id` is omitted while several groups match, the
resolver raises an error instead of pairing unrelated records.
