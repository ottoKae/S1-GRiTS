# Optional static-looks weighted mosaicking

`nlooks_weighted` is an opt-in intra-pass strategy for unfiltered 30 m VV/VH
scenes. It uses explicit OPERA RTC-S1-STATIC v1.0 `number_of_looks` rasters.
It shares the [average strategy's restrictions](intra_pass_average.md): exact
nearest-neighbor mapping, one orbit direction, no monthly composition and no
automatic static post-processing. Ratio, RVI and the scene GLCM preset can
follow the weighted mosaic. The default first-valid route is unchanged.

```yaml
roi:
  flight_direction: ASCENDING
  polarization: VV+VH
processing:
  target_resolution: 30
  resampling_method: nearest
  spatial_despeckle: false
  intra_pass_mosaic:
    method: nlooks_weighted
    looks_directory: /absolute/path/to/pinned-static-looks
  monthly:
    enabled: false
    only: false
static_layers:
  run_after_scenes: false
```

Run `s1grits process_scenes --config <config.yaml>`. Outputs are isolated in
`<output.base_dir>/derived/intra_pass_nlooks_weighted`. Product paths,
processing signatures and catalog/STAC IDs distinguish this strategy from
first-valid, equal average and burst-centre selection.

## Preparing and pinning weights

Place the original single-band GeoTIFFs directly in `looks_directory`, keeping
their OPERA names, for example
`OPERA_L2_RTC-S1-STATIC_T018-038650-IW2_20140403_S1A_30_v1.0_number_of_looks.tif`.
Use a dedicated collection with exactly one selected static asset per burst.
An absolute directory path avoids dependence on the command's working directory.
The existing `s1grits.asf_tiles.query_rtc_static_metadata_by_burst_ids` helper
returns the `url_number_of_looks` URLs for downloading the original assets;
the weighted workflow itself reads local files and does not choose newer
static products silently. A tile-mosaicked static product is not a substitute
for these individual burst assets.

The current implementation accepts RTC-S1 and RTC-S1-STATIC product v1.0,
30 m, S1A/S1B. Each signal pair must match the selected weight's burst ID,
CRS, affine transform and full native dimensions. Missing, ambiguous or
incompatible weights fail before the scene writer creates products. Invalid
weight pixels are excluded; they do not trigger an equal-weight fallback.
Other product versions require explicit compatibility validation before support.

Every file's product ID and SHA-256 enter the processing contract, sorted by
burst ID. The entire pinned collection defines identity across date batches;
adding or replacing a weight creates a new processing signature. Copying the
same files to another directory preserves identity. Freeze this collection
for incremental updates. Source paths and selected weight hashes are also
recorded per acquisition in Zarr `acquisitions_by_time[time].looks_assets`,
in the same order as that acquisition's `opera_ids`.

## Numerical meaning

For each target pixel, map signal and weight with exact nearest-neighbor
coordinates (`tolerance=0`). A contributor must have finite, positive VV,
VH and weight, excluding each raster's NoData. Both polarizations use this
same contributor set. Compute `sum(weight * power) / sum(weight)` in stable
OPERA ID order with float64 products and accumulation, then cast power to
float32 before dB and feature derivation. No contributors means NoData.

Zarr retains two auxiliary variables:

- `n_contributors` (uint32): contributing burst pairs, zero when none.
- `weight_sum` (float64): the actual denominator, zero when none and NaN
  outside the output clip. This is not a measured effective number of
  independent looks in the fused product, nor a temporal observation count.

COG/PNG outputs contain radiometric and feature bands; the auxiliary variables
remain in Zarr. Static weights are read by window, not expanded into a full
time-by-burst cube. Their file hashes and native-grid matches are prepared
before block processing.

These are nominal **static** weights, not a claim that the exact number of
contributing radar samples was measured separately for every acquisition.
OPERA describes these geometry layers as varying slightly with time and
therefore being distributed separately as static products. See the
[OPERA RTC-S1-STATIC dataset description](https://registry.opendata.aws/nasa-operal2rtc-s1-staticv1/)
and [product specification](https://d2pn8kiwq2w21t.cloudfront.net/documents/ProductSpec_RTC-S1-STATIC.pdf).
The weighted-average concept also appears in
[OPERA's mosaic implementation](https://github.com/opera-adt/RTC/blob/c5cc7403af16bb67fd14f43bf2aec15c6a422297/src/rtc/mosaic_geobursts.py).
S1-GRiTS specifies its own paired validity, source order and exact mapping;
this is not a claim of reproducing the complete OPERA pipeline.

## Quick output check

Build one acquisition on a small target window and reopen its Zarr and COG.
Compare power-domain values with independently mapped source and weight
rasters, check the denominator and contributor count, and repeat using a
different chunk size. Tests in `tests/test_nlooks_weighted.py` use real small
GeoTIFF weights and exercise the actual writer, COG export and catalog resync.
