# Derive products from an existing Cube

`s1grits derive_cube` reads an existing scenes or monthly Zarr Cube and writes a new, independent Cube root. It does not download bursts. This optional entry point does not change `process_scenes` or its defaults.

```bash
s1grits derive_cube --catalog-root /data/cube --item-id PARENT_ITEM_ID \
  --output /data/derived-features --start 2026-01-01 --end 2026-03-31T23:59:59Z \
  --window 100 200 128 128 --features ratio rvi glcm --filter none
```

`--item-id` selects the **Zarr store** referenced by one complete catalog row. The explicit UTC interval selects timestamps within that store, even when the row describes just one scene. Both endpoints are inclusive; a date without a time means midnight. `--window` is `(x offset, y offset, width, height)` in the parent's grid; omitting it selects the full grid. Source and destination roots must be separate, and the destination must not exist.

| Option | Output and calculation |
|---|---|
| `--features ratio rvi` (default) | Ratio = VH/VV; dual-pol RVI proxy = 4 VH/(VV+VH), using linear power recovered from parent dB |
| `--features glcm` | Eight VV/VH texture bands: contrast, homogeneity, entropy, correlation; 5×5 window, distance 1, 0°/90° average, 16 levels, fixed VV [−25,5] and VH [−32,−5] dB quantization ranges |
| `--features` with no values | Keep the resulting VV_dB/VH_dB only |
| `--filter none` (default) | Preserve selected parent VV/VH values, with declared NoData and nonfinite values mapped to NaN |
| `--filter tv_bregman` | Existing TV operator, regularization 5 |
| `--filter nlm` | Existing NLM operator, patch 3, search distance 7, adaptive strength |
| `--monthly mean` or `median` | Scenes from one declared track, grouped by UTC calendar month; composite paired finite positive VV/VH in linear power, then convert to dB |

RVI here is the existing dual-polarization proxy, not a full-polarimetric decomposition. Deriving from a filtered or composited parent does not recover the original burst samples. Parent dB values may already have rounding or other processing losses.

The order within one call is **parent VV/VH → optional monthly composite → optional spatial filter → requested features → crop**. To filter scenes before monthly compositing, first produce a filtered scenes Cube, then use that output as the next call's parent. This preserves both intermediate objects and their processing order. Existing auxiliary bands are not copied automatically.

Monthly outputs include every month touched by the requested interval, including empty months between observations. Empty months have NaN backscatter/features and `n_obs=0`. `n_obs` is uint32 and counts paired valid **parent scenes**, not bursts, and is calculated before any optional filter. Partial first/last months use only observations inside the requested interval. At least one parent observation must be selected. Already-monthly parents can be filtered or used for features, but cannot be composited again with `--monthly`.

## Output and lineage

Each output contains a Zarr store, global/per-tile `catalog.parquet`, and `derivation.json`. Open it with the existing `CubeResolver`; `catalog resync` preserves its derived identity. This entry point does not emit COGs automatically.

The processing contract records the parent item, grid, processing signature, optional generation ID, selected time/window/support, decoded-input SHA-256, operator parameters and order. The receipt maps each output timestamp to its parent timestamps. Changing selected parent values or the operator changes the derived signature; changing only the output chunk size does not. These are parent-specific derivative identities, so the fixed-grid mosaic command's same-signature gate is not a cross-parent derivative compatibility checker.

The parent must remain unchanged while the command runs. The content fingerprint involves a read pass over the selected VV/VH support; it is not a lock or an atomic snapshot of a concurrently updated production store. The output is an independent batch, with no automatic incremental refresh. Recompute into a fresh root after changing the parent selection.

## Memory and spatial support

VV/VH arithmetic, monthly composites without a spatial filter, and GLCM are evaluated in spatial chunks. GLCM reads an 8-pixel halo and crops it away, preserving the existing operator's full-grid result at block boundaries. The contract records the actual support window.

TV/NLM process the **entire selected support for one output timestamp** to avoid treating chunk boundaries as image edges. When combined with monthly compositing, the selected month's input stack is loaded for that support. Memory therefore grows with the chosen window and number of scenes in that month; `--chunk-size` does not bound filter memory. Start with an explicit small window for long series or large grids. Cropped-domain TV/NLM is not claimed to equal full-grid filtering. Adding GLCM extends the filter support by its halo, which is recorded in the contract.

## Python

```python
from s1grits.analysis import derive_cube
from s1grits.resolver import CubeResolver

record = derive_cube(
    '/data/cube', 'PARENT_ITEM_ID', '/data/derived-monthly',
    start='2026-01-01', end='2026-03-31T23:59:59Z',
    window=[100, 200, 128, 128], monthly='mean', features=['ratio', 'rvi'],
)
with CubeResolver('/data/derived-monthly').open(record) as dataset:
    print(dataset)
```

The small output tests in `tests/test_cube_derivatives.py` cover axis order, exact no-filter copies, valid 0 dB, finite NoData, mean/median, empty months, counts above 255, GLCM halo/chunk equivalence, TV/NLM, parent immutability, signatures, chaining, CLI and catalog rebuilding.
