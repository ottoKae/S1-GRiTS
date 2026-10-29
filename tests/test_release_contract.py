"""Release and canonical-frontend contracts that must survive packaging."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from s1grits.templates import DEFAULT_SCENES_CONFIG
from s1grits.webapp.server import CONFIG_TEMPLATE


ROOT = Path(__file__).resolve().parents[1]


def test_cli_and_web_share_one_valid_template():
    assert CONFIG_TEMPLATE == DEFAULT_SCENES_CONFIG
    parsed = yaml.safe_load(DEFAULT_SCENES_CONFIG)
    assert parsed["workflow"] == "scenes"
    assert parsed["processing"]["target_resolution"] == 30.0
    assert parsed["output"]["existing_store"] == "resume"


def test_bundled_frontend_is_canonical_chinese_ui():
    index = (ROOT / "src/s1grits/webapp/static/index.html").read_text(encoding="utf-8")
    assert 'lang="zh-CN"' in index
    tracked = subprocess.run(
        ["git", "ls-files", "src"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.replace("\\", "/").casefold()
    assert "src/gui/" not in tracked
    assert "/webapp_en/" not in tracked


def test_offline_release_source_gate_passes():
    completed = subprocess.run(
        [sys.executable, "tools/release_check.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "[PASS] citation version parity" in completed.stdout
    assert "[PASS] privacy: email addresses" in completed.stdout
    assert "[PASS] privacy: user-home paths" in completed.stdout
    assert "[PASS] privacy: high-confidence secrets" in completed.stdout
