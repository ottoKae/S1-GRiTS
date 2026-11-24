# Optional CCDC and LandTrendr point analysis

`analyze_segments` reads one pixel from an existing Cube and writes an independent analysis directory. The catalog item selects its referenced whole Zarr store. It does not change the parent, download bursts or add an algorithm to the default production pipeline.

This first version uses the official [Earth Engine CCDC](https://developers.google.com/earth-engine/apidocs/ee-algorithms-temporalsegmentation-ccdc) and [LandTrendr](https://developers.google.com/earth-engine/apidocs/ee-algorithms-temporalsegmentation-landtrendr) APIs. Install `s1grits[earthengine]` to get the verified Python client (1.7.22), and configure your own Earth Engine authentication and authorized project. The optional cloud dependency is separate from the default and `all` extras. Imports and help do not authenticate or contact the service.

Every call explicitly selects `--backend earthengine`. Valid values and UTC dates of the selected pixel are sent to Google's Earth Engine service as a constant-image series evaluated at a virtual point. Actual coordinates, source paths and the Cube itself are not sent; no persistent cloud asset/export is created. This remains remote computation of the selected values. `--project` selects and records an authorized project; otherwise the receipt records `credential_default` with no inferred project identifier.

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

LandTrendr assumes increasing model values indicate disturbance. `decrease` therefore multiplies dB by -1; `increase` uses +1. Original and directed fitted values are both retained. The preset is maxSegments=6, spikeThreshold=0.9, vertexCountOvershoot=3, preventOneYearRecovery=false, recoveryThreshold=0.25, pvalThreshold=0.1, bestModelProportion=0.75, minObservationsNeeded=6. These controls are recorded; arbitrary parameter tuning is not exposed in this initial point adapter.

`annual_fitted.parquet` stores the returned years, observed/fitted values and vertex indicators. `segments.parquet` stores adjacent vertices, duration in years, original-dB change, disturbance-oriented change and RMSE. It does not label every vertex as a disturbance or apply an undocumented event threshold. Resolution is annual, with no inferred monthly event dates.

## Results and verification

The new output directory contains:

- `input.parquet`: selected source timestamps and effective valid values.
- `model_input.parquet`: the actual series prepared for the backend, including annual support when applicable.
- `raw_response.json`: unchanged service response (empty for locally insufficient input).
- `segments.parquet` and, for LandTrendr, `annual_fitted.parquet`.
- `analysis.json`: status, parent identity/grid/generation, effective-input hash, configuration, project selection and runtime timestamps.

Fewer than six valid timestamps (CCDC) or supported years (LandTrendr) produce `analysis_state=insufficient_observations` without a service call. A successful but empty fit is `no_fit`; a nonempty fit is `fitted`. Service/parse failures raise an error and leave `status=failed`, not an empty successful detection result. Cloud success does not establish physical accuracy.

CCDC may return a tail fit even for a series shorter than one year. The adapter preserves that response rather than inventing a minimum-duration failure; a fitted result alone does not establish adequate seasonal coverage.

The scientific signature includes the client version, preset, input identity/hash, dates, pixel and preparation choices. Earth Engine is a managed backend whose server algorithm version cannot be pinned by this client; `service_version=managed_unpinned` records that limit. Keep the raw response and execution timestamp. The parent must remain unchanged while its series is read.

The supported scale is one pixel per call. Small tests cover local preparation and real service parity; this does not establish block batching or continental performance. The implementation uses official APIs and does not reuse unpublished research algorithms or private benchmark presets. Run live tests only explicitly with `S1GRITS_EE_TEST=1`; ordinary tests never require cloud credentials.
