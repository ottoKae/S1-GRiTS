# Retrospective temporal interpolation

The optional interpolation step in `derive_cube` reads one existing Cube instance and writes an independent derived Cube. The default build pipeline and existing derivations are unchanged unless interpolation is explicitly selected.

```bash
s1grits derive_cube --catalog-root /data/cube --item-id PARENT_ITEM_ID \
  --output /data/filled --start 2026-01-01 --end 2026-12-31T23:59:59Z \
  --window 100 200 64 64 --chunk-size 64 --features \
  --interpolation linear --interpolation-domain linear_power --max-gap-days 65
```

Use `--features` with no values to disable the default Ratio/RVI calculation. Interpolation is a separate step: do not combine it in one call with monthly compositing, spatial filtering or feature calculation. To interpolate a monthly product, first create/select that product, then use it as the parent.

## Calculation

- `linear` fills only missing values that have a valid earlier and later value in the **selected interval**, separately for VV and VH. Weights use actual UTC elapsed time, including irregular dates and leap days.
- `--max-gap-days` is required and positive. It bounds the **entire interval between the two bracketing valid values**, not the number of missing timestamps or the distance to the nearest observation. A span equal to the limit is allowed; a longer span remains missing.
- `--interpolation-domain db` interpolates stored dB values. `linear_power` converts the parent dB to linear power, interpolates, then converts the estimates back to dB. For endpoints 0 and 10 dB, the midpoint is 5 dB in the first domain and approximately 7.404 dB in the second. The domain must be selected explicitly.
- Existing valid values are copied exactly. NoData is normalized to NaN. All-missing pixels, pixels with fewer than two valid anchors, and missing endpoints stay missing. There is no extrapolation or use of observations outside the requested interval.
- `--interpolation none --interpolation-domain db` writes an identity control with masks. Omit `--max-gap-days` for this mode.

The output time axis is the parent's selected time axis. This command **does not add dates**, regularize a calendar, or synthesize a missing month's timestamp. Timeline alignment and numerical gap filling are separate operations. For example, an empty month already represented by NaNs in a monthly Cube can be filled; a month absent from the time coordinate is not inserted.

Linear interpolation uses future bracketing values and is explicitly recorded as **retrospective**. It must not be presented as an online monitoring input available at the earlier date. Converting a processed parent from dB to linear power does not recover original burst measurements or undo its previous filtering/compositing.

## Masks and provenance

The output has six bands: `VV_dB`, `VH_dB`, `VV_observed_mask`, `VH_observed_mask`, `VV_interpolated_mask`, and `VH_interpolated_mask`. Masks are uint8, with zero a valid mask value.

An observed mask is 1 wherever that band had a valid value in the selected **parent** before this operation. It is not a raw burst count or proof that the parent was unprocessed. An interpolated mask is 1 only at a value newly filled by this run. The two masks never overlap; zero in both means the value remains missing. Existing monthly `n_obs` is not recalculated or emitted by this operation; use the original parent's observation counts. Interpolation never creates a new real observation.

Use these masks when deciding which values an analysis may consume. This step produces its own masks and does not use previously interpolated values as fresh anchors: select the pre-interpolation parent when changing the gap limit or recomputing. Subsequent copy/crop and Ratio/RVI derivations preserve the per-band VV/VH masks, so filled values remain distinguishable from observations. Selected mask contents participate in the derived signature. Filtering, GLCM and monthly compositing of this masked output are currently rejected; run those operations on the pre-interpolation parent. Arbitrary auxiliary bands are not copied automatically.

The existing lineage receipt records the input fingerprint, selected interval/window, exact timestamps and parent instance. Its `temporal_interpolation` contract adds the method, domain, maximum gap, unchanged-axis policy, no-extrapolation rule and retrospective/identity mode. Input fingerprints are read before processing; the parent must remain unchanged during the run. Changing selected parent data or interpolation parameters creates another signature. Changing spatial chunk size does not change the scientific result or signature. Interpolation anchors differ per pixel and band: the contract records nearest-valid bracketing, and the observed masks identify those candidates. `output_time_sources` is null for temporal operations because a single timestamp-level source list cannot describe these pixel-dependent anchors.

## Memory and Python use

Each spatial block holds the complete selected time series for VV/VH and its masks. Memory scales with selected time count × spatial block area; it does not require the full spatial Cube in memory. `chunk_size` controls spatial block size, not a time cutoff. Long series can use smaller spatial chunks. No parallel backend or optional dependency is added.

```python
from s1grits.analysis import derive_cube

record = derive_cube(
    '/data/cube', 'PARENT_ITEM_ID', '/data/filled',
    start='2026-01-01', end='2026-12-31T23:59:59Z',
    window=[100, 200, 64, 64], chunk_size=64, features=[],
    interpolation='linear', interpolation_domain='linear_power', max_gap_days=65,
)
```

`tests/test_cube_interpolation.py` writes and reopens small Zarr products to check irregular dates, leap days, gaps at/above the limit, per-band masks, finite NoData, endpoints, identity mode, named axes, chunk equivalence, signatures, CLI and catalog rebuilding. These are implementation checks, not a scientific accuracy assessment of filling actual environmental changes.
