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
    # Shipped YAML is UTF-8; Windows' locale codec may be GBK.
    return yaml.safe_load((_ROOT / "config" / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Shipped templates must be clean: every documented key is actually read.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name", ["s1grits_scenes.yaml"]
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


# ---------------------------------------------------------------------------
# v3 output policies: store level vs month level, v2 keys deprecated.
# ---------------------------------------------------------------------------
from s1grits.config_schema import resolve_output_policies  # noqa: E402


def test_v3_defaults_are_resume_and_skip():
    pol = resolve_output_policies({})
    assert pol.existing_store == "resume"
    assert pol.existing_month == "skip"
    assert pol.rebuild_on_mismatch is False
    assert pol.deprecations == []


def test_v3_keys_resolve_without_deprecations():
    cfg = {"output": {"existing_store": "rebuild-incompatible",
                      "existing_month": "overwrite"}}
    pol = resolve_output_policies(cfg)
    assert pol.rebuild_on_mismatch is True
    assert pol.existing_month == "overwrite"
    assert pol.deprecations == []


def test_v2_keys_map_with_deprecation_warnings():
    cfg = {"output": {"overwrite": True, "on_time_conflict": "overwrite"}}
    pol = resolve_output_policies(cfg)
    assert pol.existing_store == "rebuild-incompatible"
    assert pol.existing_month == "overwrite"
    assert len(pol.deprecations) == 2
    assert all("deprecated" in d for d in pol.deprecations)


def test_v3_wins_over_v2_when_both_present():
    cfg = {"output": {"existing_store": "resume", "overwrite": True,
                      "existing_month": "skip", "on_time_conflict": "overwrite"}}
    pol = resolve_output_policies(cfg)
    assert pol.existing_store == "resume"
    assert pol.existing_month == "skip"
    assert len(pol.deprecations) == 2
    assert all("ignored" in d for d in pol.deprecations)


def test_invalid_policy_values_fail_fast():
    # A typo must error at startup, not silently pick the other branch.
    with pytest.raises(ValueError, match="existing_store"):
        resolve_output_policies({"output": {"existing_store": "rebuild"}})
    with pytest.raises(ValueError, match="existing_month"):
        resolve_output_policies({"output": {"on_time_conflict": "skpi"}})


def test_shipped_scenes_template_uses_v3_keys():
    out = _load("s1grits_scenes.yaml")["output"]
    assert out.get("existing_store") == "resume"
    assert out.get("existing_month") == "skip"
    assert "overwrite" not in out and "on_time_conflict" not in out
    pol = resolve_output_policies({"output": out})
    assert pol.deprecations == []


def test_warn_helper_logs_and_never_raises(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        out = warn_unknown_config_keys({"bogus_section": 1})
    assert len(out) == 1
    assert any("bogus_section" in r.message for r in caplog.records)
    # Not a dict at all -> no crash, no findings.
    assert warn_unknown_config_keys(None) == []  # type: ignore[arg-type]
