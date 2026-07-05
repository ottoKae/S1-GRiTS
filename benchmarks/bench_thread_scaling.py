"""Thread-scaling benchmark for the blockwise smonthly writer.

Runs ``_write_smonthly_month_zarr_blockwise`` on a synthetic representative
tile-month at ``blockwise_threads`` in {1, 2, 4, 8} and reports wall-clock time
plus an output checksum for each.

Two distinct outputs:

* **Diagnostic** (performance): the wall-clock table — timing varies by machine
  and load, so it is reported, never asserted.
* **Deterministic** (correctness): every thread count must produce the *same*
  checksum.  The function returns nonzero and the companion unit test
  (``tests/test_zarr_concurrency_stress.py``) asserts this bit-identity.

No network / ASF access.  Run::

    python -m benchmarks.bench_thread_scaling
    python -m benchmarks.bench_thread_scaling --threads 1,4 --scenes 100
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# repo-root + src on path
_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_ROOT), str(_ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from benchmarks import _synthetic as syn  # noqa: E402


def run(thread_counts=(1, 2, 4, 8), *, height=2048, width=2048, chunk=1024,
        n_scenes=60, n_tracks=2, tmpdir=None) -> tuple[list[dict], bool]:
    """Run the writer at each thread count. Returns (rows, checksums_consistent)."""
    import tempfile
    from s1grits import workflow_scenes as ws

    tmpdir = Path(tmpdir or tempfile.mkdtemp(prefix="bench_threads_"))
    fv, pv, fh, ph, idx_by_track, master = syn.build_month(
        height=height, width=width, n_scenes=n_scenes, n_tracks=n_tracks,
    )
    rows: list[dict] = []
    checksums: list[float] = []
    for nt in thread_counts:
        store = tmpdir / f"threads_{nt}.zarr"
        g = syn.init_store(store, height, width, chunk)
        t0 = time.perf_counter()
        res = ws._write_smonthly_month_zarr_blockwise(
            g=g, month_str="2026-01", dt_ns=__import__("numpy").datetime64("2026-01-15", "ns"),
            idx_by_track=idx_by_track, final_vv=fv, prof_vv=pv, final_vh=fh, prof_vh=ph,
            height=height, width=width, transform=master, target_crs="EPSG:32717",
            chunk_y=chunk, chunk_x=chunk,
            band_names=["VV_dB", "VH_dB"], copol_name="VV_dB", crosspol_name="VH_dB",
            features_ratio=False, features_rvi=False, ratio_name="Ratio", rvi_name="RVI",
            composite_method="median", trim_fraction=0.15,
            tile_clip=False, mgrs_tile_id="17MPU", num_threads=nt,
        )
        dt = time.perf_counter() - t0
        assert res is not None
        cs = syn.band_checksum(g, "VV_dB")
        checksums.append(cs)
        rows.append({"threads": nt, "seconds": dt, "checksum": cs})
    consistent = len(set(f"{c:.6f}" for c in checksums)) == 1
    return rows, consistent


def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threads", default="1,2,4,8")
    ap.add_argument("--scenes", type=int, default=60)
    ap.add_argument("--tracks", type=int, default=2)
    args = ap.parse_args(argv)
    tcs = tuple(int(x) for x in args.threads.split(","))
    rows, consistent = run(tcs, n_scenes=args.scenes, n_tracks=args.tracks)

    base = next(r["seconds"] for r in rows if r["threads"] == rows[0]["threads"])
    print(f"# thread-scaling benchmark (scenes={args.scenes}, tracks={args.tracks})")
    print(f"{'threads':>8}  {'seconds':>9}  {'speedup':>8}  checksum")
    for r in rows:
        print(f"{r['threads']:>8}  {r['seconds']:>9.2f}  {base / r['seconds']:>7.2f}x  {r['checksum']:.3f}")
    print()
    print("PERFORMANCE numbers above are machine-dependent diagnostics.")
    print(f"CHECKSUM CONSISTENCY (deterministic): {'PASS' if consistent else 'FAIL'}")
    return 0 if consistent else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
