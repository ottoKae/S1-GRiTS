# Smonthly–Static Integration Plan

## Objective

Make `smonthly` the grid authority for aligned RTC-STATIC products while
keeping both product families below one `output.base_dir`, indexed by one root
catalog, and paired strictly by `geometry_group_id` (`tile + direction +
track`). Static arrays remain `(y, x)` in storage and resolver results.

## Phase 1 — Generalize dynamic-reference discovery

1. Replace scenes-only filename assumptions in `workflow_static` with a
   product-aware dynamic reference lookup.
2. Add `static_layers.reference_product_type` with supported values
   `auto`, `smonthly`, and `scenes`.
3. In `auto` mode, accept either product type. If several candidates have
   different grids, fail and require `reference_product_type` and/or
   `reference_product_label`; never choose an arbitrary grid.
4. Generalize track discovery so an integrated monthly-only run discovers
   tracks represented by `smonthly_*` stores.
5. Preserve scenes-based behavior for backward compatibility.

## Phase 2 — Enforce the static output contract

1. Write static products as siblings at
   `{base_dir}/{tile}/static_{direction}/zarr/`.
2. Copy CRS, affine transform, width, height, `x`, `y`, and `grid_id` from the
   selected smonthly store without resampling.
3. Require the same `geometry_group_id` on both products.
4. Store `reference_product_type`, `reference_product_label`,
   `reference_grid_id`, and a tile-relative `reference_zarr_path` as
   provenance.
5. Fail before downloading static assets when a required reference is missing
   or ambiguous.

## Phase 3 — Remove full-time static materialization from the resolver

1. Change `CubeResolver.open_stack()` so dynamic variables remain
   `(time, y, x)` and static variables remain `(y, x)` in the merged Dataset.
2. Keep exact grid validation/windowing, but remove the current `np.tile`
   expansion over the complete time axis.
3. If explicit broadcasting is retained for compatibility, make it opt-in and
   lazy; default behavior must be 2-D static.
4. Broadcast only after selecting a spatial patch/model batch, where xarray or
   the ML framework can expand a small view without duplicating the archive.
5. Continue rejecting ambiguous multi-track requests unless `track` or
   `geometry_group_id` is supplied.

## Phase 4 — Catalog and STAC linkage

1. Rebuild one root `catalog.parquet` from both sibling product directories.
2. Deduplicate catalog records idempotently during `catalog resync`.
3. Link `smonthly` and `static` records with the same geometry group in both
   Parquet and STAC `related` links.
4. Validate that a static record never links across direction or track.
5. Preserve the six RTC-STATIC band names and auxiliary role metadata.

## Phase 5 — Compatibility and rollout

1. Keep existing scenes/static configurations working unchanged.
2. Document `reference_product_type: smonthly` for monthly-only archives.
3. Run focused unit tests, existing static/scenes regression tests, catalog
   resync tests, and one small end-to-end CLI fixture.
4. Pilot one Ecuador tile with multiple tracks before running all 27 tiles.
5. Verify byte-identical `x`/`y`, transform, shape, grid ID, and track pairing
   before enabling bulk static production.

## Test matrix

| Contract | Test expectation |
|---|---|
| Smonthly reference discovery | Finds the exact track store in `smonthly_{direction}` |
| Integrated track discovery | Includes tracks present only in smonthly stores |
| Single-root layout | Smonthly and static are sibling products below one tile |
| Catalog pairing | Both records share `geometry_group_id` and `grid_id` |
| Resolver dimensions | Dynamic is 3-D; static remains 2-D |
| Multi-track safety | Unqualified ambiguous requests raise an error |
| Grid ambiguity | Differing candidate grids fail rather than choosing silently |
| Backward compatibility | Existing scenes/static tests continue to pass |
| Resync idempotence | Repeated resync produces no duplicate records |
| Patch use | Static broadcasts only after a spatial/model-batch selection |

The initial executable contract is in
`tests/test_smonthly_static_contract.py`. Until production implementation is
updated, its three red tests intentionally identify the current gaps.
