# Optional CCDC and LandTrendr point analysis

`analyze_segments` reads one pixel from an existing Cube and writes an independent analysis directory. The catalog item selects its referenced whole Zarr store. It does not change the parent, download bursts or add an algorithm to the default production pipeline.

The default backend is `local`: LandTrendr uses the separately installed LT-rs implementation described below. Local CCDC is not implemented and fails explicitly before reading inputs or creating output. There is no automatic cloud fallback. This is an optional analysis entry point; the default Cube production route is unchanged.

The explicit `earthengine` backend uses the official [Earth Engine CCDC](https://developers.google.com/earth-engine/apidocs/ee-algorithms-temporalsegmentation-ccdc) and [LandTrendr](https://developers.google.com/earth-engine/apidocs/ee-algorithms-temporalsegmentation-landtrendr) APIs. Install `s1grits[earthengine]` to get the verified Python client (1.7.22), and configure your own Earth Engine authentication and authorized project. The optional cloud dependency is separate from the default and `all` extras. Imports and help do not authenticate or contact the service.

Cloud calls explicitly select `--backend earthengine`. Valid values and UTC dates of the selected pixel are sent to Google's Earth Engine service as a constant-image series evaluated at a virtual point. Actual coordinates, source paths and the Cube itself are not sent; no persistent cloud asset/export is created. This remains remote computation of the selected values. `--project` selects and records an authorized project; otherwise the receipt records `credential_default` with no inferred project identifier. Local calls reject `--project`.

## Local LandTrendr

The local **experimental** implementation is [nthh/LT-rs v0.4.0](https://github.com/nthh/LT-rs/releases/tag/v0.4.0), a Rust implementation with a Python `landtrendr.pixel` API. It is a distinct implementation, not a bit-exact substitute for GEE or an independently calibrated SAR detector. See the [backend assessment](local_backends.md) for selection and distribution limits. It remains an opt-in, separately installed analysis dependency; it is not added to the base or `all` extras.

For the tested Windows x64 / Python 3.12 environment, install the exact upstream wheel in your analysis environment:

```sh
python -m pip install "https://github.com/nthh/LT-rs/releases/download/v0.4.0/landtrendr-0.4.0-cp310-abi3-win_amd64.whl#sha256=0a9f996243471d12f73916e271f89cb46be334180cdecf0232ce1a98f44d86ab"
s1grits analyze_segments --catalog-root /data/cube --item-id PARENT_ITEM_ID --output /data/analysis/local-lt --algorithm landtrendr --band VV_dB --pixel 100 200 --start 2017-01-01 --end 2025-12-31T23:59:59 --annual-method median --annual-domain db --disturbance-direction decrease --min-annual-observations 6
```

Use the matching asset from that release for another platform; those platforms have not been tested here. Do not substitute an unrelated same-named package. An absent or different-version local backend raises an installation error and leaves the output uncreated.

The adapter reuses the annual preparation below. LT-rs expects disturbance **down**, so `decrease` passes dB unchanged and `increase` negates it. Result tables still use positive disturbance-oriented change, alongside original-dB values. `model_input.parquet` and the contract record the actual backend sign. Valid annual values are passed as contiguous float32 and years as int32; unsupported years remain in the preparation table but are omitted from the fit. Fewer than six valid years return `insufficient_observations`; more than 128 are rejected before entering LT-rs's fixed native workspace.

All eight controls are supplied explicitly, mapping the GEE preset below to LT-rs keyword names; upstream defaults are not silently substituted. Local `raw_response.json` contains years, actual float32 signals, returned fitted values, vertex flags and RMSE. The normalized output tables have the same meanings as below. Receipts record `backend=local`, implementation/version, NumPy version, dtype, input identity and `transmission=none`. No Earth Engine import, authentication or request is needed.

```bash
s1grits analyze_segments --catalog-root /data/cube --item-id PARENT_ITEM_ID \
  --output /data/analysis/ccdc-point --backend earthengine --algorithm ccdc \
  --band VV_dB --pixel 100 200 --start 2017-01-01 --end 2025-12-31T23:59:59 \
  --project YOUR_AUTHORIZED_PROJECT

s1grits analyze_segments --catalog-root /data/cube --item-id PARENT_ITEM_ID \
  --output /data/analysis/landtrendr-point --backend earthengine --algorithm landtrendr \
  --band VV_dB --pixel 100 200 --start 2017-01-01 --end 2025-12-31T23:59:59 \
  --annual-method median --annual-domain db --disturbance-direction decrease \
  --min-annual-observations 6 --project YOUR_AUTHORIZED_PROJECT
```

Pixel order is column, row in the native grid; intervals are inclusive UTC. Dates without a time mean midnight. Only VV_dB or VH_dB is supported in this version. Named time/y/x axes are required; time must be increasing and unique. No sorting, interpolation or mixing of tracks occurs implicitly. Source scientific NoData values and nonfinite values are excluded; zero dB is valid. If an observed mask exists, only mask 1 participates. An interpolated store without its observed mask is rejected. Masks denote validity in the selected parent; they do not certify raw-burst provenance.

## CCDC preset

CCDC consumes the selected valid timestamps directly. This single-band preset uses six observations to flag a change, chi-square threshold 0.99, minimum-year scaler 1.33, and no optical TMask. Dates/coefficients use Unix milliseconds (`dateFormat=2`). LASSO regularization is explicitly zero (OLS) and maxIterations is 25000; using OLS here avoids silently applying a default regularization scale to stored SAR dB. The full preset is recorded and is not a SAR calibration recommendation.

`segments.parquet` keeps each segment's start/end/break timestamps, observation support, changeProb, magnitude, RMSE and unmodified coefficient vector. The coefficient time basis is Unix milliseconds, not years. `confirmed` means the backend returned a positive tBreak with changeProb equal to 1. Tail segments remain present. Break dates are algorithm estimates, not independently verified event times. A fitted record with zero confirmed breaks differs from an unfitted or failed result.

## LandTrendr annual preparation

Specify mean or median and its domain explicitly. `db` aggregates stored dB; `linear_power` converts to power, aggregates, then returns to dB before fitting. The number of valid source observations is recorded per UTC calendar year. `--min-annual-observations` defaults to 1 and is a support threshold, not proof of seasonal completeness. Partial edge years use only the selected interval. Missing/insufficient years remain NaN in the local table and are omitted from the service collection; they are never filled with zero or interpolated. July 1 represents an annual summary, not the date of a detected event.

GEE LandTrendr assumes increasing model values indicate disturbance. For GEE, `decrease` therefore multiplies dB by -1; `increase` uses +1. The local backend uses the opposite input convention described above. Original and directed fitted values are both retained. The preset is maxSegments=6, spikeThreshold=0.9, vertexCountOvershoot=3, preventOneYearRecovery=false, recoveryThreshold=0.25, pvalThreshold=0.1, bestModelProportion=0.75, minObservationsNeeded=6. These controls are recorded; arbitrary parameter tuning is not exposed in this initial point adapter.

`annual_fitted.parquet` stores the returned years, observed/fitted values and vertex indicators. `segments.parquet` stores adjacent vertices, duration in years, original-dB change, disturbance-oriented change and RMSE. It does not label every vertex as a disturbance or apply an undocumented event threshold. Resolution is annual, with no inferred monthly event dates.

## Results and verification

The new output directory contains:

- `input.parquet`: selected source timestamps and effective valid values.
- `model_input.parquet`: the actual series prepared for the backend, including annual support when applicable.
- `raw_response.json`: unchanged service response or local backend arrays (empty for insufficient input).
- `segments.parquet` and, for LandTrendr, `annual_fitted.parquet`.
- `analysis.json`: status, parent identity/grid/generation, effective-input hash, configuration, project selection and runtime timestamps.

Fewer than six valid timestamps (CCDC) or supported years (LandTrendr) produce `analysis_state=insufficient_observations` without a service call. A successful but empty fit is `no_fit`; a nonempty fit is `fitted`. Service/parse failures raise an error and leave `status=failed`, not an empty successful detection result. Cloud success does not establish physical accuracy.

CCDC may return a tail fit even for a series shorter than one year. The adapter preserves that response rather than inventing a minimum-duration failure; a fitted result alone does not establish adequate seasonal coverage.

The scientific signature includes the selected backend/version, preset, input identity/hash, dates, pixel and preparation choices. Earth Engine is a managed backend whose server algorithm version cannot be pinned by this client; `service_version=managed_unpinned` records that limit. Keep the raw response and execution timestamp. The parent must remain unchanged while its series is read.

The single-point entry remains one pixel per call. Local LandTrendr also supports the small batches below; Earth Engine remains single-point only. The adapters call public implementations and do not reuse unpublished research algorithms or private benchmark presets. Run live tests only explicitly with `S1GRITS_EE_TEST=1`; ordinary tests never require cloud credentials.

## Small local point sets and windows

`analyze_segments_batch` runs the same experimental local LandTrendr fitter sequentially. It keeps the full time series of one pixel in memory at a time and reopens the catalog/store for each pixel. Use it for small samples; continental throughput has not been tested. There is no cloud batch path, automatic retry, interpolation or new spatial resampling.

For a native-grid 2×2 window:

```bash
python -m s1grits analyze_segments_batch --catalog-root ./cube --item-id YOUR_ITEM_ID --output ./lt-window --window 0 0 2 2 --band VV_dB --start 2017-01-01 --end 2026-01-31 --annual-method median --annual-domain db --disturbance-direction decrease --min-annual-observations 6
```

The window uses the same `XOFF YOFF WIDTH HEIGHT` convention as the derivative tools. Each pixel receives a stable `rROW_cCOL` ID. For selected points, replace `--window 0 0 2 2` with `--points-file points.json`:

```json
[
  {"point_id": "sample-A", "col": 0, "row": 0},
  {"point_id": "sample-B", "col": 1, "row": 1}
]
```

IDs must be unique nonempty strings; columns and rows are zero-based integers on the **selected parent grid**. These are the `point_id`, `col`, `row` fields used by existing point-extraction observations. If reusing those observations, select the matching source grid and deduplicate repeated dates/bands first. This entry does not interpret longitude/latitude or another grid's pixel indices. The Python API accepts the same list of records through `points=` or the four integers through `window=`; exactly one selection is required.

Outputs:

- `batch.json`: selected parent/grid, explicit selection, common parameters, overall status, processing signature and per-point result paths. Directory names hash the point ID, so the mapping stays stable when input order changes.
- `point_summary.parquet`: ID, column/row, sampled center, status, valid and annual observation counts, point signature and errors. Each successful point's `analysis.json` retains its CRS, affine transform, generation and effective-input hash.
- `points/<ID hash>/`: the normal single-point outputs described above. On identical input and parameters, its scientific signature and result tables match a standalone call.

Batch contract `local_lt_batch_v2` keeps asset paths and creation timestamps in
the receipt's `parent_reference_item`, outside the scientific signature.
The signature includes parent identity, selection, parameters and the point
signatures (which capture effective values and generation). Changing only an
asset's location does not create a new scientific batch identity. Existing v1
receipts are retained without rewriting.

`status=complete` means all selected calls finished; inspect `analysis_state` to distinguish a fitted series from insufficient observations or no fit. Segment count is null for unsupported/no-fit points, not a zero-event conclusion. A failed point has `status=failed`, a concrete error and null counts; subsequent points still run. Overall status is `partial` if some calls fail and `failed` if all fail; either returns CLI exit code 1. Invalid selection structure or an unsafe/existing output root fails before creating the batch.

To rerun failed points, select their IDs from the summary, correct their input if needed, and supply those records with a **new output directory**. Existing results are never overwritten or silently reused. An interrupted batch retains `status=building` and the point receipts already written; it is not a completed result.
