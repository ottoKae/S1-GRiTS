"""``s1grits doctor`` — find obvious problems in seconds, not hours.

Checks the runtime environment (interpreter, geospatial stack), the config
(schema, output policies, deprecated keys), the filesystem (output/cache
writability, disk space policy), and the resource plan (RAM/CPU vs resolved
worker counts). Network reachability is opt-in (``--network``) so the command
stays fast and CI-safe.

Exit code 0 when no FAIL-level findings; 1 otherwise. WARN findings do not
affect the exit code.
"""
from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass
class CheckResult:
    name: str
    level: str   # OK | WARN | FAIL
    detail: str


def _check(name: str, level: str, detail: str) -> CheckResult:
    return CheckResult(name, level, detail)


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------
def check_python() -> CheckResult:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) == (3, 12):
        return _check("python", OK, f"Python {ver}")
    return _check(
        "python", WARN,
        f"Python {ver}: the package pins >=3.12,<3.13 (geoscience wheels); "
        f"other versions are untested",
    )


def check_package_version() -> CheckResult:
    try:
        from s1grits.__version__ import __version__
        return _check("s1grits", OK, f"s1grits {__version__}")
    except Exception as exc:
        return _check("s1grits", FAIL, f"cannot import s1grits: {exc}")


# (module, required, note-if-missing)
_STACK = [
    ("numpy", True, ""),
    ("pandas", True, ""),
    ("rasterio", True, "core raster I/O"),
    ("pyproj", True, "CRS transforms"),
    ("shapely", True, "geometries"),
    ("geopandas", True, "tile enumeration"),
    ("xarray", True, "cube access"),
    ("zarr", True, "primary output format"),
    ("numcodecs", True, "zarr compression codecs"),
    ("cv2", True, "array warping/filtering (opencv-python-headless)"),
    ("asf_search", True, "ASF metadata queries"),
    ("psutil", False, "memory autodetection falls back to 8 GB"),
    ("osgeo", False, "mosaic/mosaic_scenes unavailable; install GDAL "
                     "python bindings via conda-forge (pip has no wheels)"),
]


def check_imports() -> list[CheckResult]:
    results = []
    for mod, required, note in _STACK:
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "")
            extra = ""
            if mod == "rasterio":
                gdal_ver = getattr(m, "__gdal_version__", "?")
                extra = f" (GDAL {gdal_ver})"
            results.append(_check(f"import:{mod}", OK, f"{mod} {ver}{extra}".strip()))
        except Exception as exc:
            level = FAIL if required else WARN
            msg = f"{mod} not importable: {exc}"
            if note:
                msg += f" — {note}"
            results.append(_check(f"import:{mod}", level, msg))
    return results


# ---------------------------------------------------------------------------
# Config checks
# ---------------------------------------------------------------------------
def check_config(config: dict) -> list[CheckResult]:
    from s1grits.config_schema import (
        find_unknown_config_keys, resolve_output_policies,
    )
    results = []

    unknown = find_unknown_config_keys(config)
    if unknown:
        for u in unknown:
            results.append(_check("config:keys", WARN, u))
    else:
        results.append(_check("config:keys", OK, "no unknown/misplaced keys"))

    try:
        pol = resolve_output_policies(config)
        results.append(_check(
            "config:output-policies", OK,
            f"existing_store={pol.existing_store}, "
            f"existing_month={pol.existing_month}",
        ))
        for d in pol.deprecations:
            results.append(_check("config:deprecated", WARN, d))
    except ValueError as exc:
        results.append(_check("config:output-policies", FAIL, str(exc)))

    try:
        from s1grits.preflight import resolve_disk_policy
        mode, min_free, deprecations = resolve_disk_policy(config)
        results.append(_check(
            "config:preflight-disk", OK,
            f"mode={mode}, min_free_gb={min_free:.0f}",
        ))
        for d in deprecations:
            results.append(_check("config:deprecated", WARN, d))
    except ValueError as exc:
        results.append(_check("config:preflight-disk", FAIL, str(exc)))

    return results


# ---------------------------------------------------------------------------
# Filesystem checks
# ---------------------------------------------------------------------------
def check_filesystem(config: dict) -> list[CheckResult]:
    from s1grits.preflight import (
        PreflightError, check_dir_writable, check_disk_space,
    )
    results = []

    base_dir = ((config.get("output") or {}).get("base_dir")) or "./output"
    ok, detail = check_dir_writable(base_dir)
    results.append(_check(
        "fs:output-writable", OK if ok else FAIL, f"output.base_dir: {detail}"
    ))

    cache_dir = (config.get("memory") or {}).get("burst_cache_dir")
    if cache_dir:
        ok, detail = check_dir_writable(cache_dir)
        results.append(_check(
            "fs:burst-cache-writable", OK if ok else FAIL,
            f"memory.burst_cache_dir: {detail}",
        ))
    else:
        results.append(_check(
            "fs:burst-cache-writable", OK, "burst cache disabled (null)"
        ))

    try:
        dc = check_disk_space(config, base_dir)
        results.append(_check(
            "fs:disk-space", OK if dc.ok else WARN, dc.message
        ))
    except PreflightError as exc:
        results.append(_check("fs:disk-space", FAIL, str(exc)))
    except ValueError as exc:
        results.append(_check("fs:disk-space", FAIL, str(exc)))

    return results


def check_scratch_hygiene(config: dict) -> list[CheckResult]:
    """Report reclaimable scratch: orphaned spill dirs and burst-cache size.

    - ``memory.batch_spill`` writes per-PID dirs under ``memory.spill_dir``
      (default ``{output.base_dir}/.spill``); a crashed/killed run leaves its
      dir behind. Orphans are detected by PID liveness, so a spill dir owned
      by a still-running worker is never flagged.
    - The burst cache grows without bound; its size is surfaced with the
      ``s1grits cache prune`` remediation attached.
    """
    import os

    results: list[CheckResult] = []
    base_dir = Path(((config.get("output") or {}).get("base_dir")) or "./output")
    mem = config.get("memory") or {}

    spill_root = Path(mem.get("spill_dir") or (base_dir / ".spill"))
    if spill_root.is_dir():
        orphans, orphan_bytes = [], 0
        for d in sorted(spill_root.glob("pid-*")):
            try:
                pid = int(d.name.split("-", 1)[1])
            except (IndexError, ValueError):
                continue
            try:
                os.kill(pid, 0)   # signal 0: liveness probe only
                continue          # process alive -> in use, not an orphan
            except ProcessLookupError:
                pass              # dead PID -> orphan
            except PermissionError:
                continue          # alive under another uid -> in use
            orphans.append(d)
            orphan_bytes += sum(
                f.stat().st_size for f in d.rglob("*") if f.is_file()
            )
        if orphans:
            results.append(_check(
                "fs:spill-orphans", WARN,
                f"{len(orphans)} orphaned spill dir(s), "
                f"{orphan_bytes / 1e9:.2f} GB under {spill_root} "
                f"(left by interrupted runs) — safe to delete: "
                f"rm -rf {' '.join(str(d) for d in orphans[:3])}"
                + (" ..." if len(orphans) > 3 else "")
            ))
        else:
            results.append(_check(
                "fs:spill-orphans", OK, f"no orphaned spill dirs in {spill_root}"
            ))

    cache_dir = mem.get("burst_cache_dir")
    if cache_dir and Path(cache_dir).is_dir():
        from s1grits.burst_cache import usage
        n, size = usage(cache_dir)
        results.append(_check(
            "fs:burst-cache-size", OK,
            f"{n} entrie(s), {size / 1e9:.2f} GB in {cache_dir} "
            f"(cap it with: s1grits cache prune --cache-dir {cache_dir} "
            f"--max-gb N)"
        ))
    return results


def check_store_grid_consistency(config: dict) -> list[CheckResult]:
    """Detect tiles whose existing Zarr stores sit on DIFFERENT locked grids.

    This is the state left behind by an interrupted pre-v2.3 run (one track's
    store on the pilot grid, a sibling on a window-derived grid) and the one
    condition that still fails a tile under existing_store=resume. Surfacing
    it in doctor turns a 3-hour runtime failure into a 2-second preflight
    warning with the remediation attached.
    """
    results: list[CheckResult] = []
    base_dir = Path(((config.get("output") or {}).get("base_dir")) or "./output")
    if not base_dir.is_dir():
        return results  # nothing produced yet — nothing to check
    try:
        import zarr
    except ImportError:
        return results

    checked = 0
    for tile_dir in sorted(p for p in base_dir.iterdir() if p.is_dir()):
        stores = sorted(tile_dir.glob("smonthly_*/zarr/*.zarr")) + \
            sorted(tile_dir.glob("scenes_*/zarr/*.zarr"))
        if not stores:
            continue
        checked += 1
        grids: dict[tuple, list[str]] = {}
        for zp in stores:
            try:
                g = zarr.open_group(str(zp), mode="r", zarr_format=3)
                key = (int(g["x"].shape[0]), int(g["y"].shape[0]))
                steps = int(g["time"].shape[0]) if "time" in g else 0
                grids.setdefault(key, []).append(f"{zp.name} ({steps} steps)")
            except Exception as exc:
                results.append(_check(
                    "store:readable", WARN,
                    f"{tile_dir.name}/{zp.name}: unreadable store ({exc})",
                ))
        if len(grids) > 1:
            detail = "; ".join(
                f"{w}x{h}: {', '.join(names)}" for (w, h), names in sorted(grids.items())
            )
            results.append(_check(
                "store:grid-consistency", WARN,
                f"tile {tile_dir.name} has stores on {len(grids)} different "
                f"grids ({detail}). Under existing_store=resume the minority "
                f"store(s) will fail; rerun with existing_store: "
                f"rebuild-incompatible to rebuild them onto the data-richest "
                f"grid, or delete the stray store(s).",
            ))
    if checked and not any(r.name == "store:grid-consistency" for r in results):
        results.append(_check(
            "store:grid-consistency", OK,
            f"{checked} tile(s) checked, all stores grid-consistent",
        ))
    return results


# ---------------------------------------------------------------------------
# Resource-plan checks
# ---------------------------------------------------------------------------
def check_resources(config: dict) -> list[CheckResult]:
    results = []
    cpu = os.cpu_count() or 1
    try:
        import psutil
        avail_gb = psutil.virtual_memory().available / (1024 ** 3)
        results.append(_check(
            "res:hardware", OK, f"{cpu} CPU cores, {avail_gb:.1f} GB RAM available"
        ))
    except Exception:
        avail_gb = None
        results.append(_check(
            "res:hardware", WARN,
            f"{cpu} CPU cores; RAM unknown (psutil unavailable)",
        ))

    # Resolve worker counts through the real production resolvers so doctor
    # reports exactly what a run would use. Lazy import: workflow_scenes pulls
    # in the full processing stack.
    try:
        from s1grits import workflow_scenes as ws
        par = config.get("parallel") or {}
        mw_raw = par.get("max_workers", 2)
        mw = ws._resolve_max_workers(mw_raw)
        results.append(_check(
            "res:max_workers", OK, f"parallel.max_workers: {mw_raw!r} -> {mw}"
        ))
        monthly = ((config.get("processing") or {}).get("monthly")) or {}
        bt_raw = monthly.get("blockwise_threads", 1)
        bt = ws._resolve_blockwise_threads(bt_raw, mw if par.get("enabled") else 1)
        results.append(_check(
            "res:blockwise_threads", OK,
            f"monthly.blockwise_threads: {bt_raw!r} -> {bt} per tile worker "
            f"(effective ~{mw * bt} threads)",
        ))
        dl = (config.get("memory") or {}).get("max_download_workers", 4)
        results.append(_check(
            "res:download_workers", OK,
            f"memory.max_download_workers: {dl!r} per tile worker",
        ))
        if avail_gb is not None and par.get("enabled"):
            per_worker = avail_gb / max(mw, 1)
            level = OK if per_worker >= 8 else WARN
            results.append(_check(
                "res:ram-per-worker", level,
                f"~{per_worker:.1f} GB RAM per tile worker "
                f"({'sufficient' if level == OK else 'tight: blockwise working set is ~12 GB; consider fewer workers'})",
            ))
    except Exception as exc:
        results.append(_check(
            "res:workers", WARN, f"could not resolve worker counts: {exc}"
        ))
    return results


# ---------------------------------------------------------------------------
# Network (opt-in)
# ---------------------------------------------------------------------------
def check_network(timeout: float = 10.0) -> list[CheckResult]:
    results = []
    try:
        import requests
        r = requests.head(
            "https://cmr.earthdata.nasa.gov/search/", timeout=timeout,
            allow_redirects=True,
        )
        results.append(_check(
            "net:cmr", OK if r.status_code < 500 else WARN,
            f"CMR reachable (HTTP {r.status_code})",
        ))
    except Exception as exc:
        results.append(_check("net:cmr", FAIL, f"CMR unreachable: {exc}"))
    # OPERA RTC-S1 bursts are public on the ASF CDN — no Earthdata credential
    # is required for this workflow's downloads.
    results.append(_check(
        "net:credentials", OK,
        "no credentials required (OPERA RTC-S1 bursts are public)",
    ))
    return results


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_doctor(
    config_path: str | Path | None = None,
    network: bool = False,
) -> tuple[int, list[CheckResult]]:
    """Run all checks; return (exit_code, results)."""
    results: list[CheckResult] = [check_python(), check_package_version()]
    results.extend(check_imports())

    config: dict | None = None
    if config_path is not None:
        try:
            import yaml
            with open(config_path, encoding="utf-8") as f:
                config = yaml.safe_load(f)
            results.append(_check("config:load", OK, f"loaded {config_path}"))
        except Exception as exc:
            results.append(_check(
                "config:load", FAIL, f"cannot load {config_path}: {exc}"
            ))
            config = None

    if config:
        results.extend(check_config(config))
        results.extend(check_filesystem(config))
        results.extend(check_scratch_hygiene(config))
        results.extend(check_store_grid_consistency(config))
        results.extend(check_resources(config))

    if network:
        results.extend(check_network())

    exit_code = 1 if any(r.level == FAIL for r in results) else 0
    return exit_code, results


def _icons() -> dict:
    """Unicode icons when stdout can encode them, ASCII otherwise.

    Windows consoles (and CI shells) often use cp1252, where printing
    '✓' raises UnicodeEncodeError — doctor must never crash over
    cosmetics.
    """
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "✓✗".encode(enc)
        return {OK: "✓", WARN: "!", FAIL: "✗"}
    except (UnicodeEncodeError, LookupError):
        return {OK: "+", WARN: "!", FAIL: "x"}


def format_results(results: list[CheckResult]) -> str:
    icon = _icons()
    lines = [
        f" {icon[r.level]} [{r.level:>4}] {r.name}: {r.detail}"
        for r in results
    ]
    n_fail = sum(1 for r in results if r.level == FAIL)
    n_warn = sum(1 for r in results if r.level == WARN)
    lines.append("")
    if n_fail:
        lines.append(f"doctor: {n_fail} failure(s), {n_warn} warning(s) — fix failures before a long run")
    elif n_warn:
        lines.append(f"doctor: no failures, {n_warn} warning(s)")
    else:
        lines.append("doctor: all checks passed")
    return "\n".join(lines)
