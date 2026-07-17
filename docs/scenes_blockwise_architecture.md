# Scenes Workflow: Bounded-Memory (Blockwise) Architecture Review

**Objective** (as specified): every computational stage of the `scenes`
pipeline should operate in bounded memory — peak RSS a function of *block
size, halo, dtype, and worker count*, not of scene dimensions or archive
density. Larger scenes should cost time, not memory.

This document is the code-level audit of every remaining full-frame /
full-batch allocation in the scenes path, a classification of what can be
migrated **without changing output values**, what can be *bounded* but not
block-tiled, and the phased migration design. Grid figures use the crash
case: 6,608 × 5,620 px ⇒ **G = 148 MB per float32 plane**.

---

## 1. Why the recent OOMs were NOT the per-scene full-frame ops

The audit must start with an honest accounting, because the intuitive
diagnosis ("full-scene despeckle/GLCM blew the memory") is wrong for the
two incidents we have logs for:

- The 4-worker crash (11-tile run) peaked at **15.4 GB during *download***
  of a 205-burst quarterly batch — before any raster processing ran.
- The NUMA crash died at `rss=8.2 GB` immediately after **downloading a
  112-burst batch** — despeckle and GLCM were *disabled*.

**The dominant memory term in the scenes workflow is stage S1 below: the
decoded burst arrays of the whole batch, held in RAM** (`final_vv/final_vh`
lists), sized by the batch strategy (B bursts × 2 pol × ~35 MB). All
per-acquisition raster stages together are ~3 GB even with every feature
on. A blockwise rewrite of the *processing* stages alone would therefore
NOT have prevented either crash — which is why the migration below has
three phases, and why Phase 3 (disk-backed batch sources) is the one that
actually removes scene/batch dimensions from the memory equation.

## 2. Complete allocation inventory (scenes path)

| # | Stage | Code | Peak allocation | Scales with | Bit-exact blockwise possible? |
|---|---|---|---|---|---|
| S1 | **Batch burst arrays** | `load_and_despeckle_rtc_strict` → `final_vv/vh` | B × 2 × ~35 MB (5–15 GB) | batch size B | n/a (data residency, not compute) → **Phase 3: disk-backed, window-read** |
| S2 | Download prefetch slot (opt-in) | `_iter_prefetched` | +1 × S1 | flag | by design (already bounded) |
| S3 | Acquisition mosaic ×2 pol | `_mosaic_align` | 2 × G | grid | **YES** — per-pixel first-valid overlay; windowed variant (`_mosaic_align_window`) already exists and is used by smonthly |
| S4 | Despeckle (TV-Bregman/NLM) ×2 | `_despeckle_tv_bregman_linear` | ~4–5 × G transient | grid | **NO — global iterative solver.** But *footprint-window + NaN margin* is output-equivalent (repo contract: `test_despeckle_window_equivalence`) → **boundable to valid-bbox+margin (Phase 1)** |
| S5 | Despeckle pipeline slot (opt-in) | `_prep_acquisition` | +4 × G | flag | by design; shrinks with S3/S4 |
| S6 | dB conversion ×2 | `_linear_to_db` | +2 × G | grid | YES (per-pixel) |
| S7 | Ratio / RVI | scenes writer | +2 × G | grid | YES (per-pixel) |
| S8 | GLCM (scenes product) | `compute_glcm_texture_bands` | 8 out-bands + ~4 × G transient ≈ 12 × G | grid | **YES** — halo-blockwise machinery already proven bit-exact for smonthly (`_write_glcm_blocks`, halo 8); port to scenes (Phase 2) |
| S9 | Interior-hole QC | `_interior_hole_fraction` / `binary_fill_holes` | 2–3 × G **bool** (~37 MB ea) | grid | global connectivity, but boolean: 1 byte/px. Keep full-frame; O(G bytes) documented as acceptable |
| S10 | Tile clip mask | `rasterize` | 1 × G uint8 | grid | per-block clip exists (`_apply_block_clip`) |
| S11 | Zarr append | `_append_zarr_timestep` | writes full planes (already resident) | — | YES — chunk-aligned block writes (`_begin/_write/_finalize_zarr_timestep_blockwise` exist) |
| S12 | COG export | `_clip_arrays_to_wkt_4326` + `_write_multiband_cog` | + clipped copies of ALL bands (≤12 × tile crop) | grid | YES — rasterio windowed block writes (Phase 2) |
| S13 | Preview PNG | `_generate_preview_png` | downsampled | — | already small |

Today's per-acquisition peak with every feature on ≈ **(2+2+2+2+8+~4) × G
≈ 3 GB** (+S5 if pipelined), *on top of* S1's 5–15 GB.

The smonthly product is already fully blockwise (composite, GLCM halo,
n_obs, per-block clip, chunk-aligned writes) — the review confirms no
full-frame stage remains there except the shared S1/S9 terms.

## 3. The hard constraint: despeckle cannot be block-tiled exactly

TV-Bregman (and NLM) are globally coupled: every output pixel depends,
through the solver, on the whole connected valid region. Cutting *through
valid data* and filtering tiles independently produces different values at
every cut — no halo makes it exact (unlike GLCM, whose support is a finite
window). The repo's own spec test encodes what IS safe:

- `test_sufficient_margin_is_bit_exact_within_footprint`: filtering on the
  **valid-footprint bounding box + a NaN margin** reproduces the full-frame
  result within the footprint (≤1e-6, i.e. converged float32 equality).
- `test_tight_crop_is_not_bit_exact`: dropping the margin visibly changes
  boundary values — the margin is load-bearing.

So despeckle is **footprint-boundable, not block-tileable**: memory becomes
O(valid-bbox + margin) — typically 30–60 % of the burst-union grid for a
single-swath acquisition — rather than O(G), and exactness is preserved
*within the footprint*. Anyone requiring strict full-frame filtering
(e.g. reproducing historical bytes) keeps an opt-out.

## 4. Phased migration

### Phase 1 — footprint-window despeckle (implemented in this PR)
`despeckle_2d_windowed()`: crop to valid bbox + `window_margin` (config,
default 64 px ≫ the measured convergence margin at reg_param 5), filter,
re-embed into a NaN frame. Cuts S4 (the largest single-op transient) by
the footprint factor, and shrinks S5 with it. Opt-out:
`processing.despeckle.window: false` for byte-strict full-frame behaviour.

### Phase 2 — blockwise scenes writer  ✅ DELIVERED
**Implemented: `_write_scene_timestep_blockwise` + `_export_scene_cog_preview_from_zarr`**
(opt-out `processing.scenes_blockwise: false`). The per-acquisition write path
now runs on the proven smonthly machinery: reserve timestep → for each
chunk-aligned block: windowed mosaic (S3, via `_mosaic_align_scene_window` — a
scenes-exact replica of `_mosaic_align` that, unlike the smonthly reader, keeps
non-positive linear values so dB/Ratio/RVI decide validity), dB/Ratio/RVI
(S6/S7), per-block clip (S10), block write (S11); GLCM via the halo pass (S8,
bit-exact, `GLCM_BLOCK_HALO=8`); COG via rasterio windowed strip writes (S12,
`_write_multiband_cog_windowed`) streamed straight from the store, preview from
the two dB planes of the tile crop.

Interior-hole QC runs BEFORE a timestep is reserved (matching the legacy
order): despeckle-on reuses the raw mosaic `_prep_acquisition` already holds;
despeckle-off builds the raw-VV finite mask block-by-block as an O(grid
**bytes**) boolean (the S9 budget), so no full float mosaic is ever
materialised and a skipped-only track leaves no empty store.

Interaction with Phase 1: despeckled acquisitions keep the footprint-window
array as the block source (S4's output must exist before blocks are cut —
it becomes the *only* >block allocation, by mathematical necessity).
Result: without despeckle, per-acquisition memory = O(block × bands + halo);
with despeckle, + O(footprint window). Byte-identity vs the legacy full-frame
writer is locked across despeckle×GLCM×tile_clip (Zarr bands, COG assets, and
the QC skip decision) by `tests/test_scenes_blockwise_writer.py`.

### Phase 3 — disk-backed batch sources (removes S1)
**Delivered (first increment): `s1grits.batch_spill`** — decoded burst
arrays spill to per-process `.npy` files at decode time and return as
read-only `np.memmap` views (opt-in `memory.batch_spill`, spill dir
`memory.spill_dir`, default `{root}/.spill`). A memmap IS an ndarray, so
every downstream contract is unchanged and values are byte-identical
(writer parity locked by tests). The batch's dominant term becomes
FILE-BACKED memory: the kernel evicts it under pressure instead of
OOM-killing (the failure mode of both recorded incidents), resident size
tracks the pages actually touched, and `_mosaic_align_window` slicing
faults in only each block's rows. Files are reclaimed at every batch
boundary (POSIX unlink semantics keep the prefetch slot's live memmaps
readable).

**Delivered (second increment): `s1grits.lazy_burst`** — when the on-disk
`burst_cache` holds the GeoTIFF, a burst enters the batch as a
`LazyBurstArray`: an ndarray-like handle (`.shape`/`.dtype`/`.ndim`,
`__getitem__` windowed reads, `__array__`/`.astype` full reads) that reads
only the destination window it is asked for, straight from the cached
GeoTIFF via a rasterio windowed `read`. This removes BOTH the decode-time
full-array transient (`dataset.read(1)` no longer runs at download time) and
the `.npy` spill copy. The block readers were adjusted to pass the source to
`_mosaic_align_window_direct_copy` *before* materialising, so the slice
itself is the windowed read (the reproject fallback, hit only for cross-grid
scenes, still does a full `np.asarray`). Opt-in `memory.windowed_burst_reads`
(requires `memory.burst_cache_dir`); values are byte-identical and the
blockwise scenes store built from lazy sources is locked equal to the eager
one by `tests/test_lazy_burst.py`. GDAL's dataset-handle pool amortises the
per-window open, and the handle is never held across calls, so a batch of
hundreds of bursts cannot exhaust file descriptors.

### Post-migration memory model (per tile worker)
```
peak ≈ n_block_threads × block_y × block_x × (bands + halo_overhead) × 4 B   [processing]
     + footprint_window × 4–5 × 4 B                                          [only if despeckle on]
     + O(G bytes) boolean QC masks                                            [S9, ~37 MB]
     + O(1) I/O buffers
```
— i.e. bounded by block size, halo, dtype, worker/thread counts, plus one
explicitly-documented footprint term that only despeckle users pay.

With `memory.windowed_burst_reads` on (Phase 3.2), the batch residency term
S1 leaves RAM entirely: bursts live only as on-disk cached GeoTIFFs and are
window-read on demand, so the resident batch cost collapses to the block
windows actually being processed. Without it, `memory.batch_spill` (Phase 3)
keeps S1 file-backed and reclaimable instead of anonymous RSS.

## 5. Interim operational guidance (until Phases 2–3 land)
- S1 is the term to manage: monthly batches + honest `max_memory_gb`
  (cgroup/NUMA-bind aware — psutil reads the whole node) keep it 3–5 GB.
- `processing.despeckle.window: true` (Phase 1) is safe to leave on.
- The demand-aware `auto` strategy already sizes S1 correctly *when its
  budget number is truthful*; both recorded OOMs fed it an untruthful one.
