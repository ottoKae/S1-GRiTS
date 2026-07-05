"""Unit tests for the blockwise-aware memory estimator (roadmap item 2).

The legacy estimate modelled every scene as a full-tile plane
(n_scenes x full_tile x 2 pol), which the blockwise smonthly writer never
builds — it holds burst-sized arrays plus a bounded block working set.  These
tests pin the estimate for both paths and confirm the blockwise path can
sustain a coarser batch strategy at the same RAM, while the legacy path (and
the scene/RAM threshold rules) are unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from s1grits import memory_manager as mm  # noqa: E402

TILE = (7180, 6166)  # a real 17MPU-sized tile


def test_blockwise_estimate_is_fraction_of_legacy():
    full = mm.estimate_memory_demand_gb(80, TILE, blockwise=False)
    block = mm.estimate_memory_demand_gb(80, TILE, blockwise=True)
    assert block == pytest.approx(full * mm.BLOCKWISE_SCENE_FRACTION)
    # 80 scenes on a 7180x6166 tile: legacy ~40 GB, blockwise ~10 GB (matches
    # the ~11 GB peak observed in real runs).
    assert 30 < full < 55
    assert 7 < block < 14


def test_legacy_estimate_unchanged():
    # Reproduces the original formula exactly (fraction 1.0, safety 1.5).
    h, w = TILE
    expected = (h * w * 4 * 2) / (1024 ** 2) * 80 * 1.5 / 1024
    assert mm.estimate_memory_demand_gb(80, TILE, blockwise=False) == pytest.approx(expected)


def test_blockwise_sustains_yearly_where_legacy_downgrades():
    # A machine with 64 GB RAM, 120 scenes on a big tile.
    avail = 64.0
    n = 120
    legacy = mm.select_batch_strategy(avail, n, TILE, blockwise=False)
    block = mm.select_batch_strategy(avail, n, TILE, blockwise=True)
    # Legacy estimate (~60 GB) exceeds 80% of 64 GB -> downgraded from yearly.
    # Blockwise estimate (~15 GB) stays under -> keeps yearly.
    assert legacy in ("quarterly", "monthly")
    assert block == "yearly"


def test_threshold_rules_unchanged_by_flag():
    # With ample RAM and few scenes, both paths pick yearly (no downgrade).
    assert mm.select_batch_strategy(256.0, 50, TILE, blockwise=False) == "yearly"
    assert mm.select_batch_strategy(256.0, 50, TILE, blockwise=True) == "yearly"
    # With tiny RAM, both fall to monthly.
    assert mm.select_batch_strategy(8.0, 300, TILE, blockwise=False) == "monthly"
    assert mm.select_batch_strategy(8.0, 300, TILE, blockwise=True) == "monthly"


def test_get_memory_strategy_from_config_passes_blockwise():
    cfg = {"memory": {"batch_strategy": "auto", "max_memory_gb": 64.0}}
    block = mm.get_memory_strategy_from_config(cfg, 120, blockwise=True)
    legacy = mm.get_memory_strategy_from_config(cfg, 120, blockwise=False)
    assert block == "yearly"
    assert legacy in ("quarterly", "monthly")
    # Manual strategy overrides the estimate regardless of the flag.
    cfg_manual = {"memory": {"batch_strategy": "monthly"}}
    assert mm.get_memory_strategy_from_config(cfg_manual, 120, blockwise=True) == "monthly"
