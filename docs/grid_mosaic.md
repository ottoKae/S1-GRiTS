# Fixed-grid scientific mosaics

Use `mosaic_grid` to combine explicitly selected **existing COGs** without
downloading bursts or rebuilding their Cubes. The older `mosaic` and
`mosaic_scenes` commands keep their existing behavior. This new entry point
uses the existing catalog reader and GDAL VRT/COG infrastructure with an
explicit metric grid, exact nearest-neighbor mapping and a source receipt.

GDAL Python bindings (`osgeo`) are required, as for the existing mosaic tools.
Use the repository's Conda environment when they are unavailable; Rasterio's
bundled GDAL does not supply the Python bindings.

## Select inputs and the grid

Create `items.json` containing a JSON array of catalog `item_id` strings:

```json
["first_tile_item_id", "second_tile_item_id"]
```

List order determines **per-band first-valid priority** in overlaps. A NoData
pixel in the first tile is filled by the next eligible tile. This does not
average backscatter, select a joint VV/VH winner or fuse different passes.
When validity differs by band, different bands can come from different tiles;
the receipt records this rule. For paired burst selection or averaging, build
the appropriate intra-pass product before spatial mosaicking.

```bash
s1grits mosaic_grid --catalog-root /data/cube \
  --items-file items.json --output /data/derived/region.vrt \
  --format VRT --crs EPSG:32717 --resolution 30 \
  --bounds 692310 9842310 707670 9857670
```

Use `--format COG --output /data/derived/region.tif` for a materialized COG.
The Python equivalent is:

```python
from s1grits.analysis import create_grid_mosaic

create_grid_mosaic(
    "/data/cube", ["first_tile_item_id", "second_tile_item_id"],
    "/data/derived/region.vrt", target_crs="EPSG:32717", resolution=30,
    bounds=[692310, 9842310, 707670, 9857670], output_format="VRT",
)
```

The target CRS must be projected in metres. Bounds are `(xmin, ymin, xmax,
ymax)` in that CRS and must span integer numbers of pixels at the requested
resolution. The supplied upper-left bound fixes the lattice origin; bounds
are not silently expanded or snapped to zero. Degrees are not accepted as
metres. Select a projection suitable for the actual region; a single UTM zone
is not an automatic choice for a whole continent.

## Scientific input checks

Inputs must have one processing signature, product type, direction and band
order, and exactly one item per tile. Missing/incomplete/ambiguous catalog
items, missing COGs, incompatible bands and out-of-bounds inputs fail before
creating output dependencies. There is no silent tile omission. COGs must be
georeferenced float32 scientific rasters with named bands and declared NoData.
Their radiometric/feature bands must match the catalog; Zarr-only auxiliary
counts are not expected in the COG.

Monthly inputs must refer to the same month. Scenes must have exactly the
same acquisition timestamp and relative orbit track; the command does not
floor timestamps or infer a shared pass from a date. Adjacent scenes whose
catalog timestamps differ are deliberately unsupported by this first version.
Explicit acquisition association or temporal fusion requires its own contract.
Display-enhanced products are not accepted as a separate scientific product type.

Only selected catalog entries are interpreted as requested coverage. The tool
cannot infer an intended continent's missing tiles from a shorter item list.
Use a complete, reviewed item manifest for that purpose. The receipt records
source footprints and requested bounds, **not** a measured valid-pixel coverage
fraction; VRT construction does not scan all regional pixels.

## Mapping and durable outputs

Each source is mapped to a local extent on the target lattice with nearest
sampling, transform error threshold zero and source overviews disabled.
Source NoData is respected independently by band; uncovered output is NaN.
No display stretching or interpolation averaging is applied. COG overview
generation also uses nearest sampling.

The output bundle contains:

- `region.vrt` or `region.tif`: the requested mosaic.
- `region.vrt.json` or `region.tif.json`: target grid, processing signature,
  ordered catalog records, resolved sources, file sizes/mtimes and GDAL version.
- `region.vrt.sources/` or `region.tif.sources/`: persistent warped VRTs;
  the COG case also retains its build VRT for inspection.

VRT dependencies are part of the result, not disposable temporary files.
Keep both the dependencies and original COG paths available. The mosaic can
be opened from another working directory, but relocating original files is
not automatically supported. A materialized COG is independently readable.
Source bytes are not fully hashed; catalog identity and size/mtime are
provenance evidence, not an immutable content guarantee.

Outputs must be outside the source Cube root. Existing output paths, receipts
or dependency directories are never overwritten. If GDAL fails after writing
starts, partial outputs remain for diagnosis; use a new output name for a retry.
The receipt is written with `status=complete` only after output header checks.

## Small checks and scale limits

`tests/test_grid_mosaic.py` writes real small COGs and tests same-grid values,
NoData filling, source priority, cross-UTM mapping, VRT dependency reopening in
a fresh process, COG export, CLI selection, rejection cases and a large virtual
extent. These tests require `osgeo`; they are skipped when it is absent.

VRT construction keeps each warped source footprint local and avoids loading
the regional raster into a NumPy array. GDAL's per-warp working-memory setting
is 64 MB, which is not a bound on total process memory or GDAL's separate cache.
COG materialization incurs actual disk and raster-processing costs. Large
source counts, full-continent coverage and COG peak memory still require
separate real-scale measurement; tiny sources on a large virtual grid do not
constitute that validation.

See GDAL's [warp options](https://gdal.org/en/stable/programs/gdalwarp.html)
and [VRT source-priority rules](https://gdal.org/en/stable/programs/gdalbuildvrt.html).
