---
name: s1grits-cube
description: Configure, build, inspect and resume S1-GRiTS Data Cubes using the existing production YAML and CLI. Use for a new bounded production job, coverage checks, product registration or recovery of an existing build; use s1grits-analysis for calculations on existing products.
---

# Manage an S1-GRiTS Cube

Locate the user's interpreter, installed import path and matching source/docs. Check `python -m s1grits --help` and the relevant subcommand help; a version string does not establish that candidate features are installed. Keep the user's chosen environment. The package is the executor; this skill selects and records its existing inputs, not another processing engine.

## Choose the operation

- Existing data inspection: read `CubeResolver(root).catalog`, `catalog inspect` and a small selected data window. Inspect complete product type, item ID, direction, track, grid, processing signature and locator. Distinguish physical `zarr_path`/`zarr_group` from `view_path`; use Resolver rather than guessing filenames. Catalog row counts are not acquisition or per-pixel coverage counts.
- New or extended production: identify region/tile, explicit dates, direction, product and output root from the request. Use `init` to generate the installed package's starter YAML in a new file, then set only the requested scope and necessary resource controls. The starter's `time.full` means an archive-wide request: replace it with bounded time fields when a short job is intended. Remove mutually exclusive starter alternatives rather than leaving both active.
- Existing-Cube calculation: use the available `s1grits-analysis` skill or the matching CLI/docs; do not run production to make an analysis entry available.

## Build and resume

Use the matching `docs/configuration.md`, `docs/workflows.md` and `process_scenes --help` for current options. Production YAML remains the saved configuration used by CLI, Python and Web. Do not translate it into a second skill-specific schema or silently enable filters, features, alternate mosaicking or monthly output.

Run `doctor --config <file>` before a new build; inspect failures and warnings. The ordinary doctor is offline; `--network` is an explicit connectivity check. An environment pass is not proof that data exist for the requested region. Keep the final YAML, interpreter/import location and command with the results.

For an authorized build, invoke `process_scenes --config <file>` and inspect actual workflow status, logs and products. A small time interval does not imply a small spatial tile or small burst download; use the user's size/resource limits. Do not claim successful construction merely because the command returned zero or a catalog exists.

For continuation, inspect the existing identity, time coverage and any provenance pending/receipt state first. Preserve the original processing configuration, output root and compatible resume/skip policies. Rerun the existing entry point for a supported restart; do not delete a store, change identity or switch to overwrite/rebuild to suppress an incompatibility error. A request to repair missing catalog metadata differs from a request to replace scientific arrays.

Use `catalog register` for an explicit external/group product, or `catalog resync` for requested metadata reconstruction, following the matching workflow docs. These commands write metadata. `resync --no-stac` also removes STAC artifacts: it is not a read-only inspection. Retain the selected STAC representation and provenance workflow; do not replace provenance recovery with an unrelated filesystem rescan. A stale virtual definition needs deliberate replacement/retirement, not an automatic refresh of the parent data.

## Verify and hand off

Open the selected output through Resolver; check its actual time axis, dimensions, CRS, finite/NoData values and available counts. Read `n_obs` according to its recorded meaning; missing data are not zero backscatter. Inspect logs/receipts for incomplete tiles or batches. For restart checks, compare existing arrays, coordinates and source mappings before/after, not only filenames.

Deliver the final YAML, exact command and environment, output root, selected product identities and measured completion/coverage. Save commands with actual resolved arguments, or a short replay script, so the same job can be run without an Agent. Clearly label synthetic/offline exercises and substituted query/download inputs; they are not online production or real-SAR acceptance.

This generic skill has no forestry application or private detector dependency. Its canonical source is the project `.agents/skills/` tree, separate from the Python package. Source exports are generated from that canonical skill with recorded hashes; do not maintain a second editable copy in the package or user skill directory.
