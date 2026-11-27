# Optional burst-centre selection

Set `processing.intra_pass_mosaic.method: bursts_center` in a scenes config
to select the paired VV/VH observation from the nearest eligible burst
centre. Use the same supported configuration as [average](intra_pass_average.md):
30 m, exact nearest-neighbor mapping, VV+VH, one explicit orbit direction,
no filtering, no monthly composition and no automatic static post-processing.
Ratio, RVI and the fixed scene GLCM preset remain available after selection.

```yaml
roi:
  flight_direction: ASCENDING
  polarization: VV+VH
processing:
  target_resolution: 30
  resampling_method: nearest
  spatial_despeckle: false
  intra_pass_mosaic:
    method: bursts_center
  monthly:
    enabled: false
    only: false
static_layers:
  run_after_scenes: false
```

Run `s1grits process_scenes --config <config.yaml>`. Outputs go to
`<output.base_dir>/derived/intra_pass_bursts_center`, with the method and
processing signature in each product directory and STAC/catalog identity.
The default `first_valid_pixel` and existing `average` identities are unchanged.

## Centre and selection rules

For each loaded burst, compute the centroid of all native pixel centres
where both VV and VH have finite, positive power and are not source NoData.
VV/VH must share the same native grid. Read the complete native footprint in
row strips once, before prealignment or target cropping. A cropped input is
therefore a different footprint: provide full native bursts when comparing
different target windows.

Transform the native centroid into the target projected CRS. Distances are
Euclidean distances in target metres, evaluated at target pixel centres in
float64. The target CRS must be projected in metres. Among sources with
jointly valid mapped VV/VH at a pixel, choose the nearest centre; exact ties
use ascending OPERA product ID order. Both polarizations use the same winner.
No eligible source means NoData. Changing output chunks or cropping the
target window does not recalculate the native centres.

This follows the valid-area-centre concept in
[OPERA RTC's mosaic implementation](https://github.com/opera-adt/RTC/blob/c5cc7403af16bb67fd14f43bf2aec15c6a422297/src/rtc/mosaic_geobursts.py).
S1-GRiTS explicitly uses a paired VV/VH native mask, a transformed native
centroid, target-metre distances and exact nearest-neighbor mapping. It is
not a claim of byte-for-byte equivalence with OPERA's complete mosaic workflow.

## Small output and provenance checks

Zarr includes two uint32 auxiliary variables:

- `source_index`: one-based index into this acquisition's ordered
  `acquisitions_by_time[time].opera_ids`; 0 means NoData. Index meanings may
  differ between acquisition times, so always use the corresponding list.
- `n_candidates`: number of jointly valid mapped sources at the pixel.
  This is neither an average's contributor count nor a temporal observation count.

The selected value comes from exactly one burst. Stored target centres are
available as `acquisitions_by_time[time].centers_target_xy`; the numerical
rules are in `numeric_processing` and the processing contract. A resume
with a changed source list or centre is rejected before writing the store.
Source bytes are not fully fingerprinted by this mechanism; retained input
asset versions remain part of reproducibility.

Auxiliary variables stay in Zarr; COGs and previews contain radiometric and
feature bands. Tile clipping zeros both auxiliary variables. Catalog resync
preserves each acquisition's identity and source list; the Zarr retains the
centre lookup. Use `CubeResolver` with this derived output root and an explicit
processing signature when selecting variants. No shared processing class or
migration of existing Cube stores is required.

For a quick check, build one acquisition on a small target window, reopen
its Zarr/COG, reconstruct a few pixels from `source_index`, and repeat with
another chunk size. Full-native centre calculation still reads each source
mask once even when the output window is small.
