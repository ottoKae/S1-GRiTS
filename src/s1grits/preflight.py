"""Pre-run environment checks (disk space policy, writability).

The disk check runs before long downloads begin, under a configurable policy:

.. code-block:: yaml

    preflight:
      disk:
        mode: "warn"       # warn | fail | off
        min_free_gb: 100

``warn`` (default) logs and continues; ``fail`` raises ``PreflightError``
so a doomed multi-hour run stops in seconds; ``off`` skips the check.
The legacy ``output.disk_warn_gb`` threshold is still honoured (as warn
mode) with a deprecation notice when the ``preflight`` section is absent.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DISK_MODES = ("warn", "fail", "off")
DEFAULT_MIN_FREE_GB = 50.0


class PreflightError(RuntimeError):
    """A hard preflight failure — the run must not start."""


@dataclass
class DiskCheck:
    mode: str
    path: str
    free_gb: float | None
    min_free_gb: float
    ok: bool
    message: str


def resolve_disk_policy(config: dict) -> tuple[str, float, list[str]]:
    """Resolve (mode, min_free_gb, deprecations) from config."""
    deprecations: list[str] = []
    disk_cfg = ((config or {}).get("preflight") or {}).get("disk") or {}
    mode = str(disk_cfg.get("mode", "warn")).lower()
    if mode not in DISK_MODES:
        raise ValueError(
            f"preflight.disk.mode={mode!r} is invalid; expected one of {DISK_MODES}"
        )
    min_free = disk_cfg.get("min_free_gb")
    if min_free is None:
        legacy = ((config or {}).get("output") or {}).get("disk_warn_gb")
        if legacy is not None:
            min_free = legacy
            deprecations.append(
                "output.disk_warn_gb is deprecated; use "
                "preflight.disk.min_free_gb"
            )
        else:
            min_free = DEFAULT_MIN_FREE_GB
    return mode, float(min_free), deprecations


def check_disk_space(
    config: dict,
    output_root: str | Path,
    log: logging.Logger | None = None,
) -> DiskCheck:
    """Run the disk-space preflight for ``output_root`` under the configured
    policy. Raises :class:`PreflightError` in ``fail`` mode when free space is
    below the threshold; otherwise logs and returns the check result."""
    log = log or logger
    mode, min_free_gb, deprecations = resolve_disk_policy(config)
    for d in deprecations:
        log.warning("[Preflight] %s", d)

    root = Path(output_root)
    if mode == "off":
        return DiskCheck(mode, str(root), None, min_free_gb, True,
                         "disk check disabled (preflight.disk.mode=off)")

    # Walk up to the nearest existing ancestor so disk_usage works even when
    # the output directory has not been created yet.
    probe = root
    while probe != probe.parent and not probe.exists():
        probe = probe.parent
    try:
        free_gb = shutil.disk_usage(str(probe)).free / (1024 ** 3)
    except OSError as exc:
        msg = f"disk check could not stat {probe}: {exc}"
        log.warning("[Preflight] %s", msg)
        return DiskCheck(mode, str(root), None, min_free_gb, True, msg)

    if free_gb >= min_free_gb:
        msg = (f"disk OK: {free_gb:.1f} GB free on {probe} "
               f"(threshold {min_free_gb:.0f} GB)")
        log.info("[Preflight] %s", msg)
        return DiskCheck(mode, str(root), free_gb, min_free_gb, True, msg)

    msg = (
        f"Low disk space on output volume: {free_gb:.1f} GB free at {probe} "
        f"(output.base_dir={root}), below preflight.disk.min_free_gb="
        f"{min_free_gb:.0f} GB."
    )
    if mode == "fail":
        raise PreflightError(
            msg + " Aborting before downloads (preflight.disk.mode=fail). "
            "Free space, lower the threshold, or set mode: warn/off."
        )
    log.warning("[Preflight] %s Continuing (preflight.disk.mode=warn).", msg)
    return DiskCheck(mode, str(root), free_gb, min_free_gb, False, msg)


def check_dir_writable(path: str | Path) -> tuple[bool, str]:
    """Check that ``path`` exists (or can be created) and is writable, by
    creating and removing a probe file. Returns (ok, detail)."""
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".s1grits_write_probe"
        probe.write_text("ok")
        probe.unlink()
        return True, f"{p} is writable"
    except OSError as exc:
        return False, f"{p} is not writable: {exc}"
