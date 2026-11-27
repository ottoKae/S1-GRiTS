# Local virtual products and explicit materialization

A view describes a selection or pointwise calculation on an existing local Cube.
It can select `VV_dB`/`VH_dB`, calculate `Ratio`/`RVI`, and restrict the UTC interval
and pixel window. It does not download data, reproject, filter or composite it.
The existing production configuration and defaults are unchanged.

This optional capability uses Dask, already used by the Cube analysis code. From
the candidate checkout, install the declared extra with:

```bash
python -m pip install -e ".[views]"
```

## Define, register, then read

```python
from s1grits.analysis import define_view, register_view, materialize_view
from s1grits.resolver import CubeResolver

definition = define_view(
    '/data/cube', 'PARENT_ITEM_ID',
    start='2026-01-01', end='2026-03-31T23:59:59Z',
    window=[100, 200, 16, 16], bands=['VV_dB', 'VH_dB', 'Ratio', 'RVI'],
)
# Only the in-memory definition exists at this point.
row = register_view('/data/collection', definition)

with CubeResolver('/data/collection').open(row) as dataset:
    print(dataset)             # Coordinates, metadata and lazy variables.
    values = dataset.compute()  # Explicitly calculate the selected pixels.
```

Definition and opening read coordinate arrays and metadata, not backscatter or
mask chunks. `.compute()`, `.values`, and plotting can trigger calculation.
The returned object is an ordinary xarray Dataset with Dask-backed variables.
The source storage chunk size still governs physical I/O; a pixel window is not
a promise to decompress individual pixels independently.

`window` is `[x_offset, y_offset, width, height]` in the parent grid. Both UTC time
bounds are inclusive; a date alone means midnight. A parent must be one complete,
unambiguous entity scenes/monthly product with named `time/y/x` VV/VH arrays,
known processing signature and grid identity. Existing Zarr groups are supported.
Nested virtual parents are not supported in this first version.

Registration writes `views/<processing_signature>.json` and updates the existing
registration manifest and catalog. A virtual record has `view_path` and no
`zarr_path`; its `complete` status means the definition is valid, not that pixel
results have been computed. `catalog resync` retains the reference. JSON and
GeoParquet STAC expose a definition asset with `roles: [metadata]` and
`s1grits:representation: virtual`, rather than claiming a saved Zarr exists.

## Explicitly save an entity product

```python
saved = materialize_view(
    '/data/collection', row['item_id'], '/data/collection/products/saved-view',
    chunk_size=128,
)
with CubeResolver('/data/collection/products/saved-view').open(saved) as dataset:
    print(dataset)
```

The destination must be new and separate from the input assets. An output inside
the collection is automatically registered alongside the view; a separate output
root remains independently readable. The returned record is relative to its own
output root, as with `derive_cube`. To select the saved representation through the
collection, query its catalog by the returned `item_id`.

The virtual and materialized items have different item IDs and locators but the
same scientific `processing_signature`. The signature describes the parent
identity/version evidence, selection, operator, grid, numeric domains, float32
output and mask rules. Saving does not change these rules. The saved store also
receives its own generation ID and retains the view definition for provenance.
Failed computation marks the partial store `failed` and publishes no successful
catalog record. Existing outputs are never overwritten or used as an automatic
cache. To use tools that require a Zarr group, including the existing training
Cube writer, first materialize the view.

## Values, masks and parent versions

- Scientific finite NoData, NaN/Inf and the existing -9999 sentinel become NaN;
  valid 0 dB remains valid. Zarr storage fill zero alone is not scientific NoData.
- `Ratio = VH/VV` and `RVI = 4*VH/(VV+VH)` use linear powers converted from dB,
  requiring both powers to be finite and positive. They share the existing entity
  derivative implementation. RVI remains the existing dual-polarization proxy.
- Existing VV/VH observed/interpolated masks are retained at the selected times
  and pixels as uint8, including when only features are selected. They describe
  the parent polarization values; no joint feature observation mask is invented.
- Opening checks the definition signature, catalog identity and available parent
  generation/metadata evidence. A changed generation, time axis, grid, dtype or
  relevant metadata requires a new definition. A saved entity remains independently
  readable even when its former parent changes.
- Legacy parents without `generation_id` are explicitly marked `metadata_only`.
  Opening emits a warning: in-place value changes cannot be detected from metadata.
  These views are not immutable snapshots and do not promise permanent numerical
  reproducibility. Parents must remain unchanged between opening and computing;
  version checks do not create a concurrent read snapshot.

As with other invalid explicit references, a stale registered view stops resync
and registration validation. Put its replacement in a new collection, or deliberately
retire the stale manifest entry before registering the replacement and resyncing the
existing collection. Definitions and saved entities are not automatically deleted.

Parent paths are outside the scientific signature. Moving the collection and its
relative references together preserves access. To repoint an unchanged parent to
a new location, call `define_view` there and `register_view` again: the same
scientific contract keeps the same identity while updating its locator. This still
requires matching parent identity and available version evidence.

For ordered multi-track monthly combinations, use the separate
[monthly track view entry point](multitrack_monthly.md). It shares registration,
Resolver access and explicit materialization with these single-parent views.

## CLI

```bash
s1grits view create --catalog-root /data/cube --item-id PARENT_ITEM_ID \
  --output /data/collection --start 2026-01-01 --end 2026-03-31T23:59:59Z \
  --window 100 200 16 16 --bands VV_dB Ratio RVI

# Use the virtual item ID printed above.
s1grits view materialize --catalog-root /data/collection --item-id VIEW_ITEM_ID \
  --output /data/collection/products/saved-view --chunk-size 128
```

Small real-file tests cover both axis orders, missing values, masks, exact entity
parity, delayed execution, source immutability, catalog/STAC rebuilds, relocation,
version changes, failed output and both CLI operations. This first version does
not add Web controls, automatic refreshing, remote execution, arbitrary operator
graphs, or writing new groups into shared Zarr stores.
