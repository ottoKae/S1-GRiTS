# S1-GRiTS benchmarks & diagnostics

Read-only tooling to evaluate the scenes/blockwise workflow with **real runtime
evidence** before making optimization changes. Nothing here is imported by the
production workflow; these are diagnostics and reference fixtures.

Run everything with the project's Python (the compiled `.so`/`.pyd` modules are
cp312-only, so use a Python 3.12 environment).

## Harnesses

| File | What it measures | Deterministic? |
|------|------------------|----------------|
| `phase_timing.py` | Parses `[PHASE] … elapsed_s= rss_mb=` log lines into a per-tile/per-phase table (elapsed, peak RSS, delta). | Parser is deterministic (unit-tested in `tests/test_phase_timing.py`); the summary is a diagnostic. |
| `bench_thread_scaling.py` | Wall-clock + output checksum of the blockwise writer at `blockwise_threads ∈ {1,2,4,8}` on a synthetic tile-month. | Timing is a **diagnostic** (machine-dependent); checksum-consistency is **asserted**. |
| `bench_download.py` | Effective ASF download throughput vs HTTP chunk size and worker count. Performs **real network I/O**; prints `NOT EXECUTED` and exits 0 when no reachable URL is available. Never fabricates numbers. | Not deterministic; no assertions. |
| `_synthetic.py` | Shared builder for in-memory burst-like scenes + a smonthly-layout Zarr store. Used by the benchmarks and the memory/concurrency tests. | Deterministic given a seed. |

## Usage

```bash
# Parse a real run log into a phase table
python -m benchmarks.phase_timing logs/s1grits_scenes_*.log
python -m benchmarks.phase_timing --json logs/run.log > phases.json

# Thread scaling (synthetic; no network)
python -m benchmarks.bench_thread_scaling --threads 1,2,4,8 --scenes 60

# Download micro-benchmark (needs reachable URLs; otherwise NOT EXECUTED)
S1GRITS_BENCH_URLS="https://host/a.tif,https://host/b.tif" \
    python -m benchmarks.bench_download
```

## Deterministic tests vs performance diagnostics

- **Deterministic unit tests** live in `tests/` and gate CI: phase parsing,
  memory-ceiling structural invariants, GLCM halo equivalence, despeckle
  acquisition-window equivalence, Zarr threaded-vs-serial bit-identity, and the
  burst-cache correctness contract.
- **Performance diagnostics** (thread-scaling timings, download throughput) are
  reported for decision-making but never asserted, because wall-clock and
  network numbers are environment-dependent.
