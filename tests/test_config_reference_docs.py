"""docs/configuration.md must stay in lockstep with config_schema.KNOWN_KEYS.

The schema whitelist is the single source of truth for the keys the workflows
actually read; the reference document is only trustworthy if it covers all of
them and mentions nothing the code doesn't read. Two directions:

1. completeness — every leaf key path in KNOWN_KEYS appears verbatim in the
   document (as `section.key` or as the bare key inside its section's YAML
   examples);
2. validity — every code-formatted dotted token in the document that looks
   like a config key resolves against KNOWN_KEYS (small allowlist for prose
   that isn't a key).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from s1grits.config_schema import KNOWN_KEYS  # noqa: E402

DOC = _ROOT / "docs" / "configuration.md"


def _leaf_paths(schema: dict, prefix: str = "") -> list[str]:
    paths = []
    for key, sub in schema.items():
        path = f"{prefix}{key}"
        if isinstance(sub, dict):
            paths.extend(_leaf_paths(sub, f"{path}."))
        else:
            paths.append(path)
    return paths


def test_every_schema_key_is_documented():
    text = DOC.read_text(encoding="utf-8")
    missing = []
    for path in _leaf_paths(KNOWN_KEYS):
        leaf = path.rsplit(".", 1)[-1]
        # Documented either as the full dotted path or as the bare key
        # (YAML examples inside the section's code blocks use bare keys).
        if path not in text and not re.search(rf"(?<![\w.]){re.escape(leaf)}\s*:", text):
            missing.append(path)
    assert not missing, (
        "config keys the workflows read but docs/configuration.md does not "
        f"document: {missing}"
    )


# Dotted tokens that appear in the document but are not config keys
# (module paths, file names, decimal examples, CLI flags).
_ALLOWED_NON_KEYS = {
    # documented deliberately as "this key is IGNORED here" warnings
    "processing.on_time_conflict",
    "s1grits.analysis", "catalog.parquet", "catalog.json", "collection.json",
    "config.yaml", "my_run.yaml",
}


def test_documented_keys_exist_in_schema():
    text = DOC.read_text(encoding="utf-8")
    known = set(_leaf_paths(KNOWN_KEYS))
    # also accept intermediate (section) paths
    for p in list(known):
        parts = p.split(".")
        for i in range(1, len(parts)):
            known.add(".".join(parts[:i]))

    bogus = []
    for tok in re.findall(r"`([a-z_]+(?:\.[a-z_]+)+)`", text):
        if tok in known or tok in _ALLOWED_NON_KEYS:
            continue
        if any(tok.endswith(ext) for ext in (".yaml", ".yml", ".json", ".py", ".md")):
            continue
        bogus.append(tok)
    assert not bogus, (
        "docs/configuration.md mentions dotted tokens that are not keys in "
        f"config_schema.KNOWN_KEYS (typo or stale doc?): {sorted(set(bogus))}"
    )
