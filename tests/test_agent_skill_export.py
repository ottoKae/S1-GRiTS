"""Verify real skill exports remain lossless, versioned and separate from handoff."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
EXPORTED = ROOT / "agents/skills"


def test_public_skill_export_round_trip(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/export_agent_skills.py"),
         "--source-root", str(EXPORTED), "--output-root", str(tmp_path)],
        capture_output=True, text=True, check=True,
    )
    assert "Exported 2 skills" in result.stdout
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == json.loads((EXPORTED / "manifest.json").read_text(encoding="utf-8"))
    with (ROOT / "pyproject.toml").open("rb") as stream:
        assert manifest["s1grits_version"] == tomllib.load(stream)["project"]["version"]
    assert set(manifest["files"]) == {"s1grits-cube/SKILL.md", "s1grits-analysis/SKILL.md"}
    for relative, record in manifest["files"].items():
        payload = (tmp_path / relative).read_bytes()
        assert payload == (EXPORTED / relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]


def test_regeneration_updates_hash_without_removing_other_skills(tmp_path):
    source, output = tmp_path / "source", tmp_path / "export"
    for name in ("s1grits-cube", "s1grits-analysis"):
        shutil.copytree(EXPORTED / name, source / name)
    changed = source / "s1grits-analysis/SKILL.md"
    changed.write_bytes(changed.read_bytes() + b"\nExport test revision.\n")
    handoff = output / "s1-data-handoff/SKILL.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_bytes((EXPORTED / "s1-data-handoff/SKILL.md").read_bytes())
    before = handoff.read_bytes()
    subprocess.run(
        [sys.executable, str(ROOT / "tools/export_agent_skills.py"),
         "--source-root", str(source), "--output-root", str(output)],
        capture_output=True, text=True, check=True,
    )
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert (output / "s1grits-analysis/SKILL.md").read_bytes() == changed.read_bytes()
    assert manifest["files"]["s1grits-analysis/SKILL.md"]["sha256"] == hashlib.sha256(changed.read_bytes()).hexdigest()
    assert handoff.read_bytes() == before
