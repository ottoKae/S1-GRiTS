"""Performance-architecture primitives: persistent download pool (B),
one-batch prefetch iterator (A), demand-aware auto batch strategy (C).

B — asf_io keeps ONE process-wide download ThreadPoolExecutor so per-thread
    requests.Sessions (TLS + Earthdata URS auth) survive across batches
    instead of being re-paid on every batch's pool teardown.
A — _iter_prefetched overlaps fetch(N+1) with the consumer's processing of
    item N, with a strict one-item lookahead and inline-identical semantics
    when disabled.
C — 'auto' batch strategy sizes from the PEAK batch each candidate strategy
    would hold (real date histogram), not the total scene count.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))


# ---------------------------------------------------------------------------
# B: persistent download pool
# ---------------------------------------------------------------------------

def test_download_executor_is_persistent_and_resizes():
    from s1grits import asf_io

    ex1 = asf_io._get_download_executor(2)
    ex2 = asf_io._get_download_executor(2)
    assert ex1 is ex2, "same worker count must return the same pool"

    ex3 = asf_io._get_download_executor(3)
    assert ex3 is not ex1, "size change must recreate the pool"
    assert asf_io._get_download_executor(3) is ex3


def test_download_threads_and_sessions_survive_across_batches():
    """Two 'batches' of work reuse the same threads => same cached Sessions."""
    import threading
    from s1grits import asf_io

    ex = asf_io._get_download_executor(2)

    # ThreadPoolExecutor spawns worker threads LAZILY: a round of instant
    # tasks may run entirely on one thread, leaving the second thread (and
    # its session) to be created in round 2 — which is not a persistence
    # violation. Warm BOTH workers deterministically with a barrier so every
    # pool thread's session exists before round 1 is measured.
    barrier = threading.Barrier(2)

    def _warm_session_id(_):
        barrier.wait(timeout=10)   # both threads must be alive at once
        return id(asf_io._get_session())

    def _session_id(_):
        # _get_session caches per-thread; reused thread => same object id.
        return id(asf_io._get_session())

    batch1 = {f.result() for f in [ex.submit(_warm_session_id, i) for i in range(2)]}
    batch2 = {f.result() for f in [ex.submit(_session_id, i) for i in range(8)]}
    assert len(batch1) == 2
    # Round 2 must create NO NEW sessions: every pool thread's session was
    # warmed in round 1 and persisted (no TLS/auth re-handshake per batch).
    assert batch2 <= batch1, (
        "sessions must persist across batches (no TLS/auth re-handshake)"
    )


# ---------------------------------------------------------------------------
# A: one-batch prefetch iterator
# ---------------------------------------------------------------------------

def _ws():
    from s1grits import workflow_scenes as ws
    return ws


@pytest.mark.parametrize("prefetch", [False, True])
def test_iter_prefetched_order_and_values(prefetch):
    ws = _ws()
    items = ["a", "b", "c", "d"]
    calls = []

    def fetch(idx, item):
        calls.append((idx, item))
        return item.upper()

    out = list(ws._iter_prefetched(items, fetch, prefetch))
    assert out == [(1, "A"), (2, "B"), (3, "C"), (4, "D")]
    assert sorted(calls) == [(1, "a"), (2, "b"), (3, "c"), (4, "d")]


def test_iter_prefetched_overlaps_next_fetch_with_consumption():
    ws = _ws()
    started = [threading.Event() for _ in range(3)]

    def fetch(idx, item):
        started[idx - 1].set()
        return item

    gen = ws._iter_prefetched([10, 20, 30], fetch, True)
    idx, val = next(gen)
    assert (idx, val) == (1, 10)
    # While item 1 is merely *held* by the consumer, fetch #2 must start on
    # the background thread — that is the whole point of the lookahead.
    assert started[1].wait(timeout=5.0), "fetch(2) did not start during consumption of item 1"
    assert list(gen) == [(2, 20), (3, 30)]


def test_iter_prefetched_lookahead_is_exactly_one():
    ws = _ws()
    in_flight = []
    lock = threading.Lock()
    max_alive = [0]

    def fetch(idx, item):
        with lock:
            in_flight.append(idx)
            max_alive[0] = max(max_alive[0], len(in_flight))
        time.sleep(0.05)
        with lock:
            in_flight.remove(idx)
        return item

    list(ws._iter_prefetched(list(range(6)), fetch, True))
    # A single prefetch thread can only run one fetch at a time.
    assert max_alive[0] == 1


def test_iter_prefetched_propagates_fetch_exception_at_right_index():
    ws = _ws()

    def fetch(idx, item):
        if idx == 2:
            raise RuntimeError("boom at 2")
        return item

    gen = ws._iter_prefetched(["x", "y", "z"], fetch, True)
    assert next(gen) == (1, "x")
    with pytest.raises(RuntimeError, match="boom at 2"):
        next(gen)


def test_iter_prefetched_close_cancels_pending_work():
    ws = _ws()
    fetched = []

    def fetch(idx, item):
        fetched.append(idx)
        return item

    gen = ws._iter_prefetched([1, 2, 3, 4, 5], fetch, True)
    next(gen)
    gen.close()  # consumer abandons the loop
    # fetch(1) ran, fetch(2) was the lookahead; 3..5 must never have started.
    time.sleep(0.1)
    assert max(fetched) <= 2


# ---------------------------------------------------------------------------
# C: demand-aware auto batch strategy
# ---------------------------------------------------------------------------

def _dates(rows_per_month: int, months: int = 24, start: str = "2020-01-01"):
    """rows_per_month scene rows in every month for `months` months."""
    out = []
    base = pd.Timestamp(start)
    for m in range(months):
        anchor = base + pd.DateOffset(months=m)
        out.extend([anchor + pd.Timedelta(days=d % 27) for d in range(rows_per_month)])
    return out

# estimate_memory_demand_gb(n, blockwise=True) ~= n * 0.1254 GB
# (full-tile 342.5 MB/scene * 0.25 blockwise * 1.5 safety).
# With 40 rows/month: monthly peak 40 (~5.0 GB), quarterly 120 (~15.0 GB),
# yearly 480 (~60.2 GB).


def test_peak_batch_scene_counts():
    from s1grits.memory_manager import peak_batch_scene_counts
    peaks = peak_batch_scene_counts(_dates(40))
    assert peaks == {'yearly': 480, 'quarterly': 120, 'monthly': 40}


@pytest.mark.parametrize("budget_gb, expected", [
    (100.0, 'yearly'),     # 60.2 <= 80
    (25.0, 'quarterly'),   # yearly 60.2 > 20; quarterly 15.0 <= 20
    (8.0, 'monthly'),      # quarterly 15.0 > 6.4; monthly 5.0 <= 6.4
])
def test_demand_aware_selects_coarsest_fitting_strategy(budget_gb, expected):
    from s1grits.memory_manager import select_batch_strategy_by_demand
    assert select_batch_strategy_by_demand(
        budget_gb, _dates(40), blockwise=True
    ) == expected


def test_demand_aware_accounts_for_prefetch_residency():
    from s1grits.memory_manager import select_batch_strategy_by_demand
    # 25 GB budget fits quarterly (15.0 <= 20) at residency 1, but with the
    # prefetch's second resident batch (30.0 > 20) it must drop to monthly.
    assert select_batch_strategy_by_demand(
        25.0, _dates(40), blockwise=True, resident_batches=1
    ) == 'quarterly'
    assert select_batch_strategy_by_demand(
        25.0, _dates(40), blockwise=True, resident_batches=2
    ) == 'monthly'


def test_config_auto_uses_demand_aware_when_dates_provided():
    from s1grits.memory_manager import get_memory_strategy_from_config
    cfg = {'memory': {'batch_strategy': 'auto', 'max_memory_gb': 25.0}}
    # Total scenes = 960 -> legacy thresholds would force 'monthly';
    # demand-aware sees a 120-row quarterly peak and allows 'quarterly'.
    strategy = get_memory_strategy_from_config(
        cfg, n_scenes=960, blockwise=True, acq_dates=_dates(40)
    )
    assert strategy == 'quarterly'


def test_config_manual_strategy_is_untouched():
    from s1grits.memory_manager import get_memory_strategy_from_config
    cfg = {'memory': {'batch_strategy': 'quarterly', 'max_memory_gb': 1.0}}
    assert get_memory_strategy_from_config(
        cfg, n_scenes=10_000, blockwise=True, acq_dates=_dates(40)
    ) == 'quarterly'
