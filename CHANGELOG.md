# Changelog

Notable changes to S1-GRiTS. Format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow the tags on this repository. Entries before this file was
introduced are summarised from the merged pull requests; see `git log` for
the full history.

## [Unreleased]

### Added
- **Layered product registry** — the Data Cube design layer is decoupled
  from the repository: built-in defaults ship inside the package
  (`s1grits.product_registry.DEFAULT_REGISTRY`), `metadata.product_config`
  overlays a registry file with per-product merge semantics (top-level
  `replace: true` keeps the legacy wholesale replacement), and the new
  inline `metadata.products` block lets a workflow YAML define a fully
  custom Data Cube self-contained, with no dependency on
  `config/s1grits_products.yaml`.
- `s1grits cache prune --max-gb N` — LRU size cap for the burst cache
  (evicts least-recently-used entries as `.bin`+`.sha256` pairs; also removes
  stale `.part` temp files). Safe to run alongside active workers (#25).
- `doctor` now reports **orphaned spill directories** (PID-liveness checked)
  with the exact `rm -rf` remediation, and the **burst-cache size** with the
  prune command attached (#25).
- Per-tile **peak-RSS watermark**: sampled at batch boundaries, logged at
  tile completion and returned as `peak_rss_mb` in the tile result — an
  auditable record of the bounded-memory model in production (#25).

### Changed
- `workflow_scenes.py` split into the `s1grits.scenes` package (ten
  single-responsibility modules: `blocks`, `mosaic`, `cog`, `stac_items`,
  `store`, `qc`, `scene_writer`, `smonthly_writer`, `pipeline`, `support`);
  `workflow_scenes` remains a full backward-compatible facade (7,152 → 499
  lines) (#22, #23, #24).
- The **Streamlit GUI is deprecated** (removal in **v3.0.0**, together
  with the legacy monthly/static workflows and the full-frame writer
  opt-out) in favour of the v2.3 web interface
  (`s1grits serve`, see `docs/webapp.md`); the duplicated GUI tree was
  consolidated to `src/gui` — fixing a bug where the packaged copy emitted
  the obsolete `post_processing` config key so the despeckle toggle was
  silently ignored (#21).
- CI hardened: required lint job (ruff + mypy), coverage reporting,
  macOS/Windows promoted to required checks, deduplicated triggers (#20).

### Fixed
- **COG export "dirty block" failure** on large multi-band products
  (`GDALRasterBand::IRasterIO` on e.g. 6608×5620×12): the writers now write
  all bands of each tile-row strip in one call, so no compressed tile is
  revisited after being flushed; plus a disk-space preflight warning and an
  actionable error with free-space context (#19).

## [2.3.3] - 2026-07

### Added
- **Bounded-memory scenes pipeline** — peak memory now scales with block
  size, halo, dtype, and worker count instead of scene dimensions:
  - Phase 1: footprint-window despeckle (`processing.despeckle.window`) (#15)
  - Phase 2: blockwise scenes writer — per-block mosaic/dB/Ratio/RVI/clip,
    halo GLCM, streamed COG (`processing.scenes_blockwise`, default on) (#17)
  - Phase 3: spill-to-memmap batch sources (`memory.batch_spill`) (#16)
  - Phase 3.2: windowed burst reads straight from the on-disk burst cache
    (`memory.windowed_burst_reads`, requires `memory.burst_cache_dir`) (#18)
  All byte-identical to the legacy paths (locked by parity tests).
- `n_obs` uint8 valid-observation-count band in smonthly stores (Zarr-only).
- v2.3 web interface: `s1grits serve` (FastAPI + zero-build SPA), job
  manager, catalog/timeseries APIs (see `docs/webapp.md`).
- Download performance: persistent download pool, one-batch download
  prefetch (`memory.download_prefetch`), demand-aware `auto` batch strategy.
- GLCM entropy zero-pair skipping (bit-identical, large speedup on sparse
  quantised imagery); despeckle pipelining (`processing.despeckle.pipeline`).

### Fixed
- Store-identity fragmentation: Zarr/COG/preview names key on the track only
  (`_TK{token}`); the time-varying `n_bursts` no longer splits one track's
  time series across stores.
- Despeckle value fidelity: no upper linear clip (bright scatterers above
  0 dB preserved) and nearest-valid NaN padding (the constant −23 dB padding
  darkened mosaic edges).
- Master grid: fresh grids grow to at least the MGRS tile bounds
  (era-independence guard), preventing later-era in-tile data from being
  cropped by an early sparse batch's footprint union.

## [2.3.2] and earlier

See the release tags and `git log` — this file starts at the 2.3.3 cycle.
