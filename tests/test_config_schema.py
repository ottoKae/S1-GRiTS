"""Config known-key validation (warn-only).

Workflows read YAML with dict.get(default), so a misspelled or misplaced key
silently loses to the default. These tests lock in the validator that makes
such keys loud, and pin the shipped template configs to a clean validation —
so a template edit that introduces a key the code never reads fails CI
instead of shipping documentation for dead behaviour.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from s1grits.config_schema import (  # noqa: E402
    find_unknown_config_keys,
    warn_unknown_config_keys,
)


def _load(name: str) -> dict:
    return yaml.safe_load((_ROOT / "config" / name).read_text())


# ---------------------------------------------------------------------------
# Shipped templates must be clean: every documented key is actually read.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name", ["s1grits_scenes.yaml", "s1grits_monthly.yaml"]
)
def test_shipped_template_has_no_unknown_keys(name):
    problems = find_unknown_config_keys(_load(name))
    assert problems == [], (
        f"{name} documents keys the code never reads: {problems}"
    )


# ---------------------------------------------------------------------------
# Misplaced keys get a targeted "code reads X instead" hint.
# ---------------------------------------------------------------------------
def test_misplaced_on_time_conflict_gets_moved_hint():
    cfg = {"processing": {"on_time_conflict": "overwrite"}}
    problems = find_unknown_config_keys(cfg)
    assert len(problems) == 1
    assert "processing.on_time_conflict" in problems[0]
    assert "output.on_time_conflict" in problems[0]
    assert "IGNORED" in problems[0]


def test_misplaced_overwrite_gets_moved_hint():
    problems = find_unknown_config_keys({"processing": {"overwrite": True}})
    assert len(problems) == 1 and "output.overwrite" in problems[0]


# ---------------------------------------------------------------------------
# Unknown keys warn; known keys and free-form subtrees don't.
# ---------------------------------------------------------------------------
def test_unknown_top_level_and_nested_keys_warn():
    cfg = {
        "outputs": {"base_dir": "x"},                # typo'd section
        "memory": {"max_memory": 8},                 # typo'd leaf
        "processing": {"monthly": {"enable": True}}, # typo'd nested leaf
    }
    problems = find_unknown_config_keys(cfg)
    joined = "\n".join(problems)
    assert "'outputs'" in joined
    assert "'memory.max_memory'" in joined
    assert "'processing.monthly.enable'" in joined
    assert len(problems) == 3


def test_free_form_subtrees_are_not_flagged():
    # despeckle.kwargs is passed through verbatim; arbitrary kwargs are fine.
    cfg = {
        "processing": {
            "despeckle": {"method": "tv_bregman", "kwargs": {"reg_param": 5.0}}
        }
    }
    assert find_unknown_config_keys(cfg) == []


def test_warn_helper_logs_and_never_raises(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        out = warn_unknown_config_keys({"bogus_section": 1})
    assert len(out) == 1
    assert any("bogus_section" in r.message for r in caplog.records)
    # Not a dict at all -> no crash, no findings.
    assert warn_unknown_config_keys(None) == []  # type: ignore[arg-type]
