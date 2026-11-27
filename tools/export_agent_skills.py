"""Export the two canonical Agent skills without installing them into an Agent."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("s1grits-cube", "s1grits-analysis")


def export_skills(source_root: Path, output_root: Path, version: str) -> dict:
    payloads = {name: (source_root / name / "SKILL.md").read_bytes() for name in SKILLS}
    manifest = {
        "generated": True,
        "s1grits_version": version,
        "canonical_source": ".agents/skills",
        "files": {},
    }
    for name, payload in payloads.items():
        relative = f"{name}/SKILL.md"
        target = output_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        manifest["files"][relative] = {"sha256": hashlib.sha256(payload).hexdigest()}
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True,
                        help="Canonical directory containing the two skill folders")
    parser.add_argument("--output-root", type=Path, default=ROOT / "agents/skills",
                        help="Export directory; defaults to this checkout's agents/skills")
    args = parser.parse_args()
    with (ROOT / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    manifest = export_skills(args.source_root, args.output_root, version)
    print(f"Exported {len(manifest['files'])} skills for S1-GRiTS {version}")


if __name__ == "__main__":
    main()
