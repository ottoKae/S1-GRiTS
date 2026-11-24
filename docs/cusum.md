# Optional public CUSUM analysis (local experimental adapter)

`analyze_cusum` reads a single existing scenes or monthly Cube instance and writes independent analysis maps. It never downloads bursts or modifies the parent. The default Cube pipeline and installation dependencies are unchanged. This adapter uses the separately installed [ec-jrc/nrt CuSum](https://nrt.readthedocs.io/en/stable/api_reference.html), not the unpublished Windowed CuSum method.

The verified backend is `nrt==0.3.0`, with NumPy 2.2.6 and Numba 0.61.2 in an isolated Python 3.12 test environment. nrt is EUPL-1.2 licensed. Its source and dependencies are not bundled here or added to an installation extra. **Distribution and licensing review remains pending**; the local experiment does not establish compatibility with this project's licensing or authorize a public release. See [the upstream license](https://github.com/ec-jrc/nrt/blob/master/LICENSE).

```bash
s1grits analyze_cusum --catalog-root /data/cube --item-id PARENT_ITEM_ID \
  --output /data/analysis/cusum-run --band VV_dB \
  --history-start 2018-01-01 --history-end 2020-12-31T23:59:59 \
  --monitor-start 2021-01-01 --monitor-end 2023-12-31T23:59:59 \
  --window 100 200 16 16 --chunk-size 64 \
  --harmonic-order 1 --sensitivity 0.05 --fit-alpha 0.05
```

The catalog item selects its referenced whole Zarr store, not just its catalog date. History and monitoring intervals are inclusive UTC timestamps, must not overlap, and must both contain timestamps. A date without a time means midnight. Window order is x offset, y offset, width, height in the native grid. Only one of `VV_dB` and `VH_dB` is analyzed, in stored dB units. This does not undo prior filtering or compositing.

ROC selects a stable history suffix and fits the trend/harmonic model. `--no-trend` removes the trend; `--harmonic-order 0` removes seasonal terms. Defaults follow the backend (trend enabled, two harmonics, sensitivity and historical-fit alpha 0.05); these are not calibrated SAR recommendations. Monitoring proceeds in date order, uses no automatic interpolation, and stops for a pixel after its first confirmed change. It does not identify all subsequent events or estimate the physical event date between acquisitions.

Finite NoData sentinels, nonfinite values, and scientific `_FillValue` declarations are excluded. Zero dB is valid. If the selected band has a per-band observed mask, only values with mask 1 participate: filled values cannot increase observation support. This mask describes validity in the interpolation parent, not necessarily raw burst measurements. Select an appropriate parent; tools that discarded a mask cannot reconstruct that provenance here.

Each spatial block reads its complete selected time series. nrt computes statistics on float32 residuals; changing spatial block layout can change last-bit diagnostics, and values near a detection threshold may be sensitive to these differences. Therefore **block size is part of this adapter's result signature**, alongside backend/runtime versions, model settings, intervals, grid, parent identity/generation and a fingerprint of effective selected values. The parent must remain unchanged during the run.

The output contains `cusum.zarr` (2D y/x maps with x/y coordinates, CRS and affine attributes) and `analysis.json` (contract, parent reference and status counts). It is an analysis result, not a replacement VV/VH observation Cube or an observation catalog entry. Read it with `xarray.open_zarr(path, consolidated=False)`.

| Field | Meaning |
| --- | --- |
| `backend_mask` | 0: not monitored; 1: active; 2: unstable history; 3: confirmed change; 4: insufficient observations |
| `fit_usable` | 1 only for a fitted, finite model with positive residual standard deviation; inspect this alongside the mask |
| `detection_date` | First confirmation date, integer days since 1970-01-01; zero means no confirmation |
| `fit_start` | Start of the stable fitted suffix, integer Unix days; zero if unavailable |
| `process`, `boundary`, `sigma` | Final CUSUM statistic, detection boundary and residual standard deviation; not probabilities |
| `history_used`, `observations_used` | Fitted suffix support; then that support plus monitoring updates consumed before stopping |
| `history_available`, `monitoring_available` | Valid selected input counts, including later observations not consumed after a confirmed change |

**Mask 1 alone is not a verified no-change result.** Zero-variance/unusable fits retain their backend mask but have `fit_usable=0`, no monitoring updates and cleared diagnostics. Fewer than `max(regressors+2, ceil(1.5*regressors))` history observations are marked 4 before fitting: the additional recursive-fit requirement avoids a verified short-series division by zero in nrt 0.3.0. Entirely empty pixels are 0. Unusable support/date fields are zero and floating diagnostics are NaN. An exception leaves the new output marked `building`; it is not a successful empty result.

Small on-disk tests compare the adapter with direct public-backend calls at matching block layouts, plus stable/change inputs, unstable/short/constant history, masks, NoData, named axes, cropped output, signatures, CLI and source immutability. These verify implementation, not SAR detection accuracy or continental throughput. Real SAR calibration and distribution review remain separate work.
