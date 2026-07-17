"""Ops hardening: burst-cache prune (LRU size cap) and doctor scratch checks.

Locks the prune contract — LRU order, pair-wise (.bin+.sha256) eviction,
budget respected, dry-run inertness, stale .part cleanup — and the doctor
scratch-hygiene checks: orphaned spill dirs are detected by PID liveness (a
live process's dir is never flagged) and the burst-cache size is surfaced
with the prune remediation attached.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from s1grits import burst_cache  # noqa: E402
from s1grits.doctor import check_scratch_hygiene, WARN, OK  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    burst_cache.configure(None)
    yield
    burst_cache.configure(None)


def _fill(cache_dir, n=5, mb=1):
    """n committed entries of ~mb MB each, oldest first (staggered mtimes)."""
    burst_cache.configure(cache_dir)
    urls = []
    for i in range(n):
        url = f"http://asf/burst-{i:02d}.tif"
        burst_cache.put(url, bytes(mb * 1_000_000))
        urls.append(url)
    # stagger mtimes so LRU order == insertion order regardless of clock res
    for i, url in enumerate(urls):
        data_p, _ = burst_cache._CACHE._paths(url)
        t = time.time() - (n - i) * 3600
        os.utime(data_p, (t, t))
    return urls


def test_prune_evicts_lru_pairs_to_budget(tmp_path):
    urls = _fill(tmp_path, n=5, mb=1)
    s = burst_cache.prune(tmp_path, max_gb=0.003)  # keep ~3 of 5 MB
    assert s["evicted"] == 2 and s["bytes"] <= 3_000_000
    # Oldest two evicted as pairs; newest three intact and readable
    for url in urls[:2]:
        assert burst_cache.get(url) is None
        data_p, meta_p = burst_cache._CACHE._paths(url)
        assert not data_p.exists() and not meta_p.exists()
    for url in urls[2:]:
        assert burst_cache.get(url) is not None


def test_prune_dry_run_deletes_nothing(tmp_path):
    urls = _fill(tmp_path, n=4, mb=1)
    s = burst_cache.prune(tmp_path, max_gb=0.001, dry_run=True)
    assert s["evicted"] == 3 and s["dry_run"]
    for url in urls:
        assert burst_cache.get(url) is not None  # nothing actually removed


def test_prune_under_budget_is_noop_and_removes_stale_parts(tmp_path):
    _fill(tmp_path, n=2, mb=1)
    stale = tmp_path / "deadbeef__x.bin.part.12345"
    stale.write_bytes(b"partial")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = tmp_path / "cafef00d__y.bin.part.99999"
    fresh.write_bytes(b"in-flight")
    s = burst_cache.prune(tmp_path, max_gb=1.0)
    assert s["evicted"] == 0
    assert s["stale_parts"] == 1
    assert not stale.exists() and fresh.exists()  # in-flight writer untouched


def test_usage_counts_committed_entries(tmp_path):
    _fill(tmp_path, n=3, mb=1)
    n, size = burst_cache.usage(tmp_path)
    assert n == 3 and 2_900_000 < size < 3_300_000


# ---------------------------------------------------------------------------
# doctor scratch hygiene
# ---------------------------------------------------------------------------

def _cfg(base_dir, cache_dir=None):
    mem = {"burst_cache_dir": str(cache_dir)} if cache_dir else {}
    return {"output": {"base_dir": str(base_dir)}, "memory": mem}


def test_doctor_flags_orphan_spill_dir(tmp_path):
    spill = tmp_path / ".spill" / "pid-999999999"  # PID far above pid_max
    spill.mkdir(parents=True)
    (spill / "burst-0.npy").write_bytes(bytes(1000))
    results = {r.name: r for r in check_scratch_hygiene(_cfg(tmp_path))}
    r = results["fs:spill-orphans"]
    assert r.level == WARN
    assert "1 orphaned spill dir" in r.detail and "rm -rf" in r.detail


def test_doctor_ignores_live_pid_spill_dir(tmp_path):
    spill = tmp_path / ".spill" / f"pid-{os.getpid()}"  # this test process
    spill.mkdir(parents=True)
    (spill / "burst-0.npy").write_bytes(bytes(1000))
    results = {r.name: r for r in check_scratch_hygiene(_cfg(tmp_path))}
    r = results["fs:spill-orphans"]
    assert r.level == OK, r.detail  # live process's dir is never an orphan


def test_doctor_reports_cache_size_with_remediation(tmp_path):
    cache = tmp_path / "cache"
    _fill(cache, n=2, mb=1)
    results = {r.name: r for r in check_scratch_hygiene(_cfg(tmp_path, cache))}
    r = results["fs:burst-cache-size"]
    assert r.level == OK
    assert "2 entrie(s)" in r.detail and "s1grits cache prune" in r.detail
