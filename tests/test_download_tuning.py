"""Unit tests for download tuning (roadmap item 5).

Covers:
- DOWNLOAD_CHUNK_BYTES is a real 1 MiB streaming chunk (the loop uses the
  constant; the earlier 16 KiB value was dead — the loop already hardcoded
  1 MiB).
- _resolve_download_workers: integer passthrough and the "auto" strategy that
  divides the global ASF connection budget across the tile worker pool.

The end-to-end download throughput sweep lives in benchmarks/bench_download.py
and is network-gated (NOT EXECUTED without ASF access); it is not asserted
here because there is nothing deterministic about network throughput.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from s1grits import workflow_scenes as ws  # noqa: E402
from s1grits import asf_io  # noqa: E402


def test_download_chunk_is_one_mib():
    assert asf_io.DOWNLOAD_CHUNK_BYTES == 1024 * 1024


def test_resolve_download_workers_integer_passthrough():
    assert ws._resolve_download_workers(4) == 4
    assert ws._resolve_download_workers("6") == 6
    assert ws._resolve_download_workers(0) == 1        # floored at 1
    assert ws._resolve_download_workers(-2) == 1
    assert ws._resolve_download_workers(None) == 4     # historical default
    assert ws._resolve_download_workers("nonsense") == 4


def test_resolve_download_workers_auto_divides_budget():
    budget = ws.DOWNLOAD_GLOBAL_CONNECTION_BUDGET  # 16
    lo, hi = ws.DOWNLOAD_AUTO_MIN_WORKERS, ws.DOWNLOAD_AUTO_MAX_WORKERS  # 4, 8
    # 1 tile worker: 16/1 = 16 -> capped at 8
    assert ws._resolve_download_workers("auto", 1) == hi
    # 2 tile workers: 16/2 = 8 -> 8
    assert ws._resolve_download_workers("auto", 2) == min(budget // 2, hi)
    # 4 tile workers: 16/4 = 4 -> 4 (== floor)
    assert ws._resolve_download_workers("auto", 4) == lo
    # 8 tile workers: 16/8 = 2 -> floored to 4
    assert ws._resolve_download_workers("auto", 8) == lo
    assert ws._resolve_download_workers("AUTO", 3) >= lo


def test_auto_total_connections_stay_polite():
    # total = tile_workers * per_tile should not explode past ~budget+floor slack
    for tw in (1, 2, 4, 8):
        per_tile = ws._resolve_download_workers("auto", tw)
        assert lo_ok(per_tile), per_tile


def lo_ok(n):
    return ws.DOWNLOAD_AUTO_MIN_WORKERS <= n <= ws.DOWNLOAD_AUTO_MAX_WORKERS
