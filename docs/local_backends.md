# Local temporal backend assessment

Assessment date: 2026-11-27. These are development-candidate capabilities, not a statement that the released V3.0.0 package already contains them. Default Cube production and package dependencies are unchanged.

| Algorithm / candidate | Decision | Evidence and limit |
| --- | --- | --- |
| CUSUM: ec-jrc/nrt 0.3.0 | Retain the separately installed local experimental adapter | Existing direct-library and real SAR tests pass. Distribution review is not replaced by those tests. |
| CCDC: USGS PyCCD | Not selected for direct VV/VH adaptation | Its public interface requires Landsat optical, thermal and QA series. Do not fabricate those bands from SAR. |
| CCDC: pyxccd | Do not relabel COLD / S-CCD as the existing GEE CCDC | These are distinct algorithm choices, requiring their own contract and validation. |
| CCDC / LandTrendr: Zeit | Not selected for direct SAR CCDC adaptation in this candidate | Reviewed 3.10.0 source: fixed LASSO strength and mandatory TMask path; changing the range alone does not meet our SAR input contract. Not installed or integrated. |
| LandTrendr: original IDL | Reference implementation, not selected for this Python environment | Requires its own runtime and has custom upstream usage terms. |
| LandTrendr: lt-gee-py | Not a local computational backend | It wraps Earth Engine calls. |
| LandTrendr: nthh/LT-rs 0.4.0 | Separately installed experimental local point adapter | Windows x64 / Python 3.12 tested against direct library calls; float32 implementation, maximum 128 valid annual samples. Scientific equivalence to GEE is not assumed. |

## CUSUM distribution decision

S1-GRiTS currently uses PolyForm Noncommercial 1.0.0 with a separate commercial licensing route. nrt is EUPL-1.2. The [EUPL text](https://github.com/ec-jrc/nrt/blob/master/LICENSE) distinguishes use, interoperability and redistribution of derivative works; PolyForm is not in its compatible-license appendix. That absence alone does **not** prove that an unmodified API dependency is an incompatible derivative.

The European Commission's [FAQ](https://interoperable-europe.ec.europa.eu/collection/eupl/faqs) distinguishes independent components linked through their public interfaces from incorporated or modified code, while noting that derivative-work classification depends on applicable law. Our adapter imports the separately installed CuSum API; it does not vendor or modify nrt. The engineering decision is to keep this separate installation route and the existing experimental status. No nrt extra, bundled copy, project relicensing or claim of universal legal compatibility is introduced. Public/commercial distribution clearance remains an explicit open item, not an automatic failure of local use.

For the already verified research environment, the tested versions are nrt 0.3.0, NumPy 2.2.6 and Numba 0.61.2. Use a separate environment so this combination does not replace a production stack. See [CUSUM behavior and outputs](cusum.md). This assessment is a dependency/distribution record, not legal clearance for every jurisdiction or business model.

The concrete candidate scope is the existing CLI/Agent adapter with a user-installed,
unmodified backend. Installation commands are now documented. Bundled nrt, an automatic
installation extra and public Web/SaaS access are not part of that decision; their
distribution review remains open. A new small-sample run passed all 18 CUSUM and
Zarr-group tests, including comparisons with direct backend calls. Subsequent R7
verification installed the candidate wheel and the same nrt/NumPy/Numba versions in
a fresh Windows Python 3.12 environment without inherited site packages: `pip check`
and direct-backend CUSUM tests passed. This establishes installation and adapter
behavior for that environment; it does not close the distribution review or establish
SAR detection accuracy.

## Local LandTrendr selection

[LT-rs v0.4.0](https://github.com/nthh/LT-rs/releases/tag/v0.4.0) exposes a small single-pixel API and only declares NumPy as a Python dependency. Its [license](https://github.com/nthh/LT-rs/blob/v0.4.0/LICENSE) is MIT. Upstream describes it as a port of the original IDL algorithm; retain that attribution and the [original project's usage information](https://github.com/KennedyResearch/LandTrendr-2012/blob/master/README.md) in any subsequent distribution review. No upstream code or wheel is copied into the S1-GRiTS package.

The adapter records this implementation's identity and explicitly maps its disturbance-down convention to the common disturbance-up result fields. Direct-library equality verifies the adapter; agreement with a historical GEE result is a comparison of implementations, not the acceptance reference. See [installation, parameters and output semantics](segmentation.md).

One retained real SAR pixel supplied 403 selected timestamps, 346 valid values and 9 supported annual summaries. Adapter output matched direct LT-rs exactly and left the parent unchanged. Against the saved GEE run on the same annual inputs, LT-rs returned 3 segments versus GEE's 1; mean absolute fitted-value difference was 0.467136 dB and vertex choices differed. This is not established to be rounding error and does not show which implementation is scientifically preferable. Results carry `implementation_status=experimental`; preserve backend identity when comparing analyses. No new GEE request was used for this comparison.

## CCDC follow-up

The [USGS PyCCD interface](https://pypi.org/project/lcmap-pyccd/2021.7.14/) is an optical/thermal Landsat interface. Its public example does not establish a supported single-band SAR mode. This pass therefore does not invent one or copy and rewrite its detection algorithm.

Zeit 3.10.0 was reviewed at commit `25c692d1231c61c1014e9d55d9aa377be44900ba`:

- Its [public API](https://github.com/sacridini/zeit-cdts/blob/25c692d1231c61c1014e9d55d9aa377be44900ba/zeit/_ccdc_api.py#L60) specifies reflectance ×10000. The default exclusive range (0, 10000) rejects negative dB inputs; the range is configurable, so this alone is not the rejection reason.
- Its [Rust fitting core](https://github.com/sacridini/zeit-cdts/blob/25c692d1231c61c1014e9d55d9aa377be44900ba/rust/zeit-core/src/ccdc.rs#L227) fixes LASSO strength at 20. The public Python and native parameter objects expose no control to select zero regularization, as our current single-band SAR preset does.
- In the [TMask configuration](https://github.com/sacridini/zeit-cdts/blob/25c692d1231c61c1014e9d55d9aa377be44900ba/rust/zeit-core/src/ccdc.rs#L311), an empty list selects default bands, including duplicated band 0 for a single-band input. It does not disable TMask; the core invokes that screen during fitting.

This is a source-level contract assessment, not a numerical benchmark or a claim
that CCDC cannot work on SAR. This candidate cannot implement our existing raw-dB,
zero-regularization, no-optical-TMask contract through a small API adapter. Changing
its core, inventing a signal scaling or fabricating optical bands is outside this
update. No Zeit runtime or dependencies were installed.

Local CCDC remains explicitly unsupported, with no automatic cloud fallback.
The existing explicit GEE single-point path remains available. A future local
candidate must expose the necessary controls, pass direct-backend tests on stable,
step-change and missing-data series plus an existing SAR pixel, and record its own
implementation and units. Such tests establish adapter correctness; they do not
establish detection accuracy or equality with GEE.
