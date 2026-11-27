---
name: s1grits-analysis
description: Select and read existing S1-GRiTS products, create traceable derivatives or virtual views, combine comparable monthly tracks, and run supported temporal analysis through the existing CLI or Python API. Use without downloading bursts or rebuilding the parent Cube.
---

# Analyze an existing S1-GRiTS Cube

Locate the user's interpreter, Cube/collection root and installed import path. Check `python -m s1grits <command> --help` before choosing an operation; a release version alone does not prove candidate commands exist. Read only the relevant matching-version docs below. If only a package is installed, use its CLI/help and matching documentation, not an unrelated checkout.

Inspect `CubeResolver(root).catalog` and select a complete item by product, direction, track, grid and processing identity. Do not silently choose the first row of a multi-instance catalog. The item references a store/group or virtual definition; time selection happens inside it. Use `resolver.open(record)` and inspect the requested point/window, bands, units and NoData before calculating. Multi-parent operations require an explicit ordered list of inputs.

## Choose a supported entry point

| Intent | Entry and matching source documentation |
| --- | --- |
| Features, filters or scene-to-month aggregation | `derive_cube`; `docs/cube_derivatives.md` |
| Selection or Ratio/RVI without immediately saving arrays | `view create` / `define_view`; `docs/cube_views.md` |
| Save a registered virtual result | `view materialize` / `materialize_view`; `docs/cube_views.md` |
| Combine comparable single-track monthly products | `view multitrack` / `define_multitrack_view`; `docs/multitrack_monthly.md` |
| Temporal interpolation | `derive_cube` interpolation options; `docs/temporal_interpolation.md` |
| Public CUSUM or temporal segmentation | `analyze_cusum`, `analyze_segments`, `analyze_segments_batch`; `docs/cusum.md`, `docs/segmentation.md` |

Use `docs/getting_started_existing_cube_ZH.md` for a runnable synthetic tutorial. Production YAML configures construction; these analysis entries use CLI/API arguments and saved contracts. Do not invent a generic analysis YAML engine or another algorithm implementation.

Monthly derivation requires a declared single-track scenes parent. Already-monthly inputs can supply features but cannot be recomposited as scenes. R4c combinations currently require entity monthly `derive_cube` outputs with comparable recorded statistical ranges and source stages; legacy monthly labels alone are insufficient. Select `first_valid_pixel` or linear-power equal-track `mean` explicitly. Preserve track order and paired VV/VH validity. `n_contributing_tracks` and `contributes_TK*` are spatial track contributions, not `n_obs`; the parent counts remain at their referenced parent months. Do not silently mix directions or reproject parents.

Virtual registration saves a definition, not pixel arrays. Resolve it using the same collection API; materialize only when requested or required by a downstream entity-only tool. Do not create nested views where the implementation requires entity parents. Missing parent generation evidence means `metadata_only`, not an immutable snapshot. A changed/stale parent must be reported, not silently rebound. Keep source data quiescent during computation.

Choose a new output destination separate from source stores, including referenced external/group assets. Supported sibling output roots may reside within the collection; follow the installed entry's rules. Do not overwrite a completed result to reuse a command. For a replay, choose a new independent destination; the scientific identity should remain the same when only the locator changes. Start large operations with a small explicit window within the selected native grid.

## Temporal backends

Filtering, interpolation and cloud computation are not implicit preprocessing. Public CUSUM uses its separately installed experimental backend. Local LandTrendr uses the verified LT-rs adapter; local CCDC is not implemented. Both segmentation methods can use explicit `earthengine` when supported. Check the actual environment and do not fall back to GEE when a local dependency is missing. Local analysis authorization does not authorize sending observations to a cloud service.

For local LandTrendr batches use explicit point IDs/col/row or a native-grid window. Inspect `batch.json`, `point_summary.parquet` and each point's state; preserve the ID-to-result-path mapping. Partial/failed batches remain incomplete; a manual retry selects failed IDs and a new output directory. The batch entry does not support GEE.

## Verify and replay

Open produced data or analysis result tables and inspect times, coordinates, finite/NoData values and count semantics. Check the saved contract/receipt, parent item(s), parameters, processing signature and completion state. Distinguish missing support from a valid no-change result. Synthetic expectations apply only to the tutorial, not real data.

Save the exact executed command or a short replay script with interpreter, import location, input item IDs, explicit parameters and destination. Preserve native records such as `derivation.json`, registered view JSON and analysis receipts. For a no-Agent replay, compare arrays/coordinates, scientific signature and parent references; creation times, output paths and new physical generation IDs may differ. Deliver paths, command and measured results, including any incomplete steps.

This skill is generic: no forestry workflow, private detector or unpublished Windowed CuSum. Its canonical source is the project-scoped skill tree; installing the S1-GRiTS wheel does not install the skill. Generate any public export from this source and record its hash instead of maintaining another editable copy.
