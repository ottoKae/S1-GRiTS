"""Preflight disk policy (warn/fail/off) and the `s1grits doctor` command.

The disk check must run BEFORE downloads and honour a three-mode policy;
doctor must exit 0 exactly when there is no FAIL-level finding. All tests are
network-free (doctor's network checks are opt-in and not exercised here).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from s1grits.preflight import (  # noqa: E402
    PreflightError,
    check_dir_writable,
    check_disk_space,
    resolve_disk_policy,
)


def _cfg(mode=None, min_free=None, legacy_warn=None):
    cfg: dict = {}
    if mode is not None or min_free is not None:
        cfg["preflight"] = {"disk": {}}
        if mode is not None:
            cfg["preflight"]["disk"]["mode"] = mode
        if min_free is not None:
            cfg["preflight"]["disk"]["min_free_gb"] = min_free
    if legacy_warn is not None:
        cfg["output"] = {"disk_warn_gb": legacy_warn}
    return cfg


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------
def test_default_policy_is_warn_50gb():
    mode, min_free, dep = resolve_disk_policy({})
    assert (mode, min_free, dep) == ("warn", 50.0, [])


def test_legacy_disk_warn_gb_maps_with_deprecation():
    mode, min_free, dep = resolve_disk_policy(_cfg(legacy_warn=120))
    assert mode == "warn" and min_free == 120.0
    assert len(dep) == 1 and "deprecated" in dep[0]


def test_invalid_mode_fails_fast():
    with pytest.raises(ValueError, match="preflight.disk.mode"):
        resolve_disk_policy(_cfg(mode="explode"))


# ---------------------------------------------------------------------------
# The three modes (threshold pinned far above/below real free space)
# ---------------------------------------------------------------------------
def test_warn_mode_logs_and_continues_when_below_threshold(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        res = check_disk_space(_cfg(mode="warn", min_free=10**9), tmp_path)
    assert res.ok is False and res.mode == "warn"
    assert any("Low disk space" in r.message for r in caplog.records)
    # Message names path, threshold, and actual free space.
    assert str(tmp_path) in res.message
    assert "min_free_gb" in res.message and "GB free" in res.message


def test_fail_mode_raises_before_run_when_below_threshold(tmp_path):
    with pytest.raises(PreflightError) as exc:
        check_disk_space(_cfg(mode="fail", min_free=10**9), tmp_path)
    msg = str(exc.value)
    assert "Aborting before downloads" in msg
    assert str(tmp_path) in msg


def test_off_mode_never_checks(tmp_path):
    res = check_disk_space(_cfg(mode="off", min_free=10**9), tmp_path)
    assert res.ok is True and res.free_gb is None


def test_passes_when_above_threshold(tmp_path):
    res = check_disk_space(_cfg(mode="fail", min_free=0.001), tmp_path)
    assert res.ok is True and res.free_gb > 0


def test_nonexistent_output_dir_probes_nearest_ancestor(tmp_path):
    res = check_disk_space(
        _cfg(mode="warn", min_free=0.001), tmp_path / "not" / "yet" / "made"
    )
    assert res.ok is True


# ---------------------------------------------------------------------------
# Writability probe
# ---------------------------------------------------------------------------
def test_writable_dir_probe(tmp_path):
    ok, detail = check_dir_writable(tmp_path / "new_subdir")
    assert ok and "writable" in detail
    assert not any((tmp_path / "new_subdir").iterdir())  # probe cleaned up


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def test_doctor_no_config_passes_in_this_env():
    from s1grits.doctor import run_doctor, OK, FAIL
    code, results = run_doctor(config_path=None, network=False)
    assert code == 0
    assert not [r for r in results if r.level == FAIL]
    names = {r.name for r in results}
    assert "python" in names and "import:zarr" in names


def test_doctor_with_template_config(tmp_path):
    # Template config but with a writable tmp output dir and disk check off.
    import yaml
    from s1grits.doctor import run_doctor
    cfg = yaml.safe_load((_ROOT / "config" / "s1grits_scenes.yaml").read_text())
    cfg["output"]["base_dir"] = str(tmp_path / "out")
    cfg["preflight"] = {"disk": {"mode": "off"}}
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(cfg))

    code, results = run_doctor(config_path=p, network=False)
    assert code == 0, "\n".join(f"{r.level} {r.name}: {r.detail}" for r in results)
    names = {r.name for r in results}
    assert {"config:keys", "config:output-policies", "fs:output-writable",
            "res:max_workers"} <= names


def test_doctor_fails_on_invalid_policy(tmp_path):
    import yaml
    from s1grits.doctor import run_doctor, FAIL
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump({
        "output": {"base_dir": str(tmp_path), "existing_month": "skpi"},
    }))
    code, results = run_doctor(config_path=p, network=False)
    assert code == 1
    fails = [r for r in results if r.level == FAIL]
    assert any("existing_month" in r.detail for r in fails)


def test_doctor_warns_on_deprecated_v2_keys(tmp_path):
    import yaml
    from s1grits.doctor import run_doctor, WARN
    p = tmp_path / "v2.yaml"
    p.write_text(yaml.safe_dump({
        "output": {"base_dir": str(tmp_path), "overwrite": True,
                   "on_time_conflict": "skip"},
    }))
    code, results = run_doctor(config_path=p, network=False)
    assert code == 0  # deprecations are warnings, not failures
    warns = [r for r in results if r.level == WARN and r.name == "config:deprecated"]
    assert len(warns) == 2


def test_doctor_unreadable_config_fails(tmp_path):
    from s1grits.doctor import run_doctor
    code, results = run_doctor(config_path=tmp_path / "missing.yaml")
    assert code == 1


def test_format_results_summarises():
    from s1grits.doctor import CheckResult, format_results, OK, WARN
    text = format_results([
        CheckResult("a", OK, "fine"), CheckResult("b", WARN, "meh"),
    ])
    assert "no failures, 1 warning(s)" in text
