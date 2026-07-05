"""Download micro-benchmark harness for ASF RTC GeoTIFFs (network-gated).

Measures effective throughput as a function of two tunables the roadmap wants
to change:

* HTTP streaming chunk size (``asf_io.DOWNLOAD_CHUNK_BYTES`` is 16 KiB today),
* number of concurrent download workers (``memory.max_download_workers``).

This harness performs REAL network I/O.  It never fabricates numbers: if no
reachable URL is available it prints ``NOT EXECUTED`` and exits 0.  Supply URLs
explicitly to run it::

    S1GRITS_BENCH_URLS="https://host/a.tif,https://host/b.tif" \\
        python -m benchmarks.bench_download

    python -m benchmarks.bench_download --urls https://host/a.tif https://host/b.tif

Results are performance diagnostics only — no assertions, no unit-test coverage
(there is nothing deterministic to assert about network throughput).
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor


def _download_one(url: str, chunk_bytes: int, timeout: float = 60.0) -> int:
    """Stream a URL to /dev/null at the given chunk size; return bytes read."""
    import urllib.request
    total = 0
    req = urllib.request.Request(url, headers={"User-Agent": "s1grits-bench"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            buf = resp.read(chunk_bytes)
            if not buf:
                break
            total += len(buf)
    return total


def _reachable(url: str, timeout: float = 10.0) -> bool:
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "s1grits-bench"})
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False


def run(urls: list[str], chunk_sizes=(16384, 262144, 1048576),
        worker_counts=(4, 8, 16)) -> list[dict]:
    """Sweep chunk sizes x worker counts over the given URLs. Real I/O."""
    rows: list[dict] = []
    for chunk in chunk_sizes:
        for workers in worker_counts:
            jobs = urls * max(1, (workers // max(1, len(urls))) + 1)
            jobs = jobs[:max(workers, len(urls))]
            t0 = time.perf_counter()
            total = 0
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for n in ex.map(lambda u: _download_one(u, chunk), jobs):
                    total += n
            dt = time.perf_counter() - t0
            mbps = (total / (1024 * 1024)) / dt if dt > 0 else 0.0
            rows.append({
                "chunk_bytes": chunk, "workers": workers,
                "files": len(jobs), "mb": total / (1024 * 1024),
                "seconds": dt, "MB_per_s": mbps,
            })
    return rows


def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--urls", nargs="*", default=None)
    args = ap.parse_args(argv)

    urls = args.urls
    if not urls:
        env = os.environ.get("S1GRITS_BENCH_URLS", "").strip()
        urls = [u for u in env.split(",") if u] if env else []

    if not urls:
        print("NOT EXECUTED: no benchmark URLs provided.")
        print("  Set S1GRITS_BENCH_URLS=<comma-separated ASF GeoTIFF URLs> or "
              "pass --urls to run.")
        return 0
    if not any(_reachable(u) for u in urls):
        print("NOT EXECUTED: provided URL(s) not reachable from this "
              "environment (no ASF/network access).")
        for u in urls:
            print(f"  unreachable: {u}")
        return 0

    rows = run(urls)
    print(f"# download micro-benchmark ({len(urls)} source URL(s)) — REAL network I/O")
    print(f"{'chunk_KiB':>9}  {'workers':>7}  {'files':>5}  {'MB':>8}  "
          f"{'sec':>7}  {'MB/s':>8}")
    for r in rows:
        print(f"{r['chunk_bytes'] // 1024:>9}  {r['workers']:>7}  {r['files']:>5}  "
              f"{r['mb']:>8.1f}  {r['seconds']:>7.2f}  {r['MB_per_s']:>8.1f}")
    print("\nPERFORMANCE diagnostics only (network-dependent).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
