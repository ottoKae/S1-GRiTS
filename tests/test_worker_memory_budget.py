"""Parallel-mode per-worker config: explicit user settings are never silently
overridden.

The parallel tile pool gives each worker a RAM budget. Previously that path
also forced ``memory.batch_strategy`` to 'auto', silently discarding an
explicit user choice like 'quarterly'. The contract now: the budget applies
to the 'auto' estimator only; an explicit strategy passes through unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from s1grits.workflow_scenes import _apply_worker_memory_budget  # noqa: E402


def test_explicit_strategy_is_honored():
    cfg = {"memory": {"batch_strategy": "quarterly", "max_memory_gb": "auto"}}
    out = _apply_worker_memory_budget(cfg, 7.5)
    assert out["memory"]["batch_strategy"] == "quarterly"
    assert out["memory"]["max_memory_gb"] == 7.5
    # Original config untouched (workers get a copy).
    assert cfg["memory"]["max_memory_gb"] == "auto"


def test_auto_strategy_gets_worker_budget():
    out = _apply_worker_memory_budget({"memory": {"batch_strategy": "auto"}}, 12.0)
    assert out["memory"]["batch_strategy"] == "auto"
    assert out["memory"]["max_memory_gb"] == 12.0


def test_missing_memory_section_defaults_to_auto():
    out = _apply_worker_memory_budget({}, 4.0)
    assert out["memory"]["max_memory_gb"] == 4.0
    assert out["memory"].get("batch_strategy", "auto") == "auto"
