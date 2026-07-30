from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

import s1grits.workflow_scenes as scenes  # noqa: E402
import s1grits.workflow_static as static  # noqa: E402


def test_poststage_processes_only_successful_scene_tiles(monkeypatch):
    calls = []

    def fake_static(config_path, overrides=None, tile_ids=None):
        calls.append((config_path, overrides, tile_ids))
        return {tile: {"status": "success", "groups_written": [{}]} for tile in tile_ids}

    monkeypatch.setattr(static, "run_static_layer_workflow", fake_static)
    results = {
        "17MPU": {"status": "success"},
        "17MPV": {"status": "failed", "error": "no scenes"},
    }
    out = scenes._run_static_poststage(
        "scenes.yaml",
        {"static_layers": {"run_after_scenes": True}},
        {"overwrite": True},
        results,
    )
    assert calls == [("scenes.yaml", {"overwrite": True}, ["17MPU"])]
    assert out["17MPU"]["static_status"] == "success"
    assert "static" not in out["17MPV"]


def test_poststage_failure_policy(monkeypatch):
    monkeypatch.setattr(
        static, "run_static_layer_workflow",
        lambda *args, **kwargs: {"17MPU": {"status": "failed", "error": "download"}},
    )
    results = {"17MPU": {"status": "success"}}
    with pytest.raises(RuntimeError, match="Static post-stage failed"):
        scenes._run_static_poststage(
            "scenes.yaml",
            {"static_layers": {"run_after_scenes": True, "on_failure": "fail"}},
            None,
            results,
        )
