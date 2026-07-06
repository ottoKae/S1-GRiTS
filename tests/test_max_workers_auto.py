"""Unit tests for parallel.max_workers auto-tuning (roadmap item 9).

"auto" sizes the tile-level process pool from CPU cores and available RAM
divided by the blockwise per-tile working set (built on item 2's estimator),
floored at 1 and capped at MAX_WORKERS_AUTO_CAP.  Integer values pass through.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from s1grits import workflow_scenes as ws  # noqa: E402


def test_integer_passthrough():
    assert ws._resolve_max_workers(2) == 2
    assert ws._resolve_max_workers("4") == 4
    assert ws._resolve_max_workers(0) == 1
    assert ws._resolve_max_workers(-1) == 1
    assert ws._resolve_max_workers(None) == 2  # historical default


def test_auto_is_ram_bounded_on_small_box():
    # 16 GB RAM, 32 cores, ~12 GB/tile -> RAM bound = 1 worker.
    assert ws._resolve_max_workers("auto", available_gb=16.0, cpu=32) == 1


def test_auto_is_cpu_bounded_on_big_ram_few_cores():
    # 256 GB RAM but only 3 cores -> CPU bound = 3.
    assert ws._resolve_max_workers("auto", available_gb=256.0, cpu=3) == 3


def test_auto_respects_cap():
    # Plenty of RAM and cores -> capped at MAX_WORKERS_AUTO_CAP.
    got = ws._resolve_max_workers("auto", available_gb=1024.0, cpu=64)
    assert got == ws.MAX_WORKERS_AUTO_CAP


def test_auto_scales_with_ram():
    # 12 GB/tile: 48 GB -> 4 workers (cpu ample).
    assert ws._resolve_max_workers("auto", available_gb=48.0, cpu=16) == 4
    # 24 GB -> 2 workers.
    assert ws._resolve_max_workers("auto", available_gb=24.0, cpu=16) == 2


def test_auto_never_zero():
    assert ws._resolve_max_workers("auto", available_gb=1.0, cpu=1) == 1
