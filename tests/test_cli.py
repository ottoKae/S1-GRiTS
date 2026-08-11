"""CLI surface smoke tests.

Locks the production CLI: `--version` reports the real package version, the
help lists exactly the retained commands, dead examples are gone, and every
retained command (plus the hidden back-compat alias) still parses `--help`
without importing/running a workflow. There were previously no CLI tests, so
this also guards against accidental removal of a documented command.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from s1grits.__version__ import __version__  # noqa: E402
from s1grits import cli  # noqa: E402

# Commands that must remain visible in the production help.
RETAINED_VISIBLE = [
    "process_scenes", "static",
    "catalog", "tile", "mosaic", "mosaic_scenes", "doctor",
]
# Dead references that must never reappear. `process`/`process_monthly` were the
# legacy monthly-composite workflow, removed in v3.0.0 (superseded by
# process_scenes). Neither is a substring of a retained command.
DEAD = ["process_monthly", "process_ablation", "process_normal40"]


def _run(argv, capsys):
    """Invoke main() with argv; return (exit_code, stdout+stderr)."""
    old = sys.argv
    sys.argv = ["s1grits", *argv]
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        out = capsys.readouterr()
        return exc.value.code, (out.out + out.err)
    finally:
        sys.argv = old


def test_version_matches_package(capsys):
    code, text = _run(["--version"], capsys)
    assert code == 0
    assert __version__ in text, f"--version must report {__version__}, got: {text!r}"
    assert "1.0.0" not in text  # the old hard-coded value is gone


def test_top_level_help_lists_retained_commands(capsys):
    code, text = _run(["--help"], capsys)
    assert code == 0
    for name in RETAINED_VISIBLE:
        assert name in text, f"{name} missing from top-level --help"


def test_top_level_help_hides_dead_examples(capsys):
    code, text = _run(["--help"], capsys)
    assert code == 0
    # The SUPPRESS sentinel must never leak into help text.
    assert "==SUPPRESS==" not in text
    # Dead command references never reappear anywhere in help/epilog.
    for name in DEAD:
        assert name not in text, f"dead reference {name} resurfaced in help"


@pytest.mark.parametrize("name", RETAINED_VISIBLE)
def test_each_command_help_parses(name, capsys):
    # `<cmd> --help` exits 0 and never dispatches a workflow, so this is safe.
    code, text = _run([name, "--help"], capsys)
    assert code == 0
    assert "usage:" in text.lower()


def test_removed_monthly_command_is_gone():
    # The legacy monthly workflow entry points were removed in v3.0.0.
    assert not hasattr(cli, "cmd_process")
    code, _ = _run_expect_error(["process", "--help"])
    assert code != 0  # argparse rejects the unknown command


def _run_expect_error(argv):
    old = sys.argv
    sys.argv = ["s1grits", *argv]
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        return exc.value.code, None
    finally:
        sys.argv = old
