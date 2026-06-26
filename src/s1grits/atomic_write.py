"""
atomic_write.py
===============
Atomic file write helpers for crash-safe S1-GRiTS outputs.

Usage:
    atomic_write_parquet(df, path)   # crash-safe catalog.parquet
    atomic_write_json(data, path)    # crash-safe STAC item.json
"""

import json as _json
import os as _os
import shutil as _shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd


@contextmanager
def atomic_path(path: str | Path):
    """Yield a temp path to write into; atomically move it onto ``path`` on success.

    Crash-safe binary writes (COG GeoTIFFs, preview PNGs): a crash or exception
    leaves any pre-existing file at ``path`` intact and removes the temp file.

    Usage::

        with atomic_path(cog_path) as tmp:
            with rasterio.open(tmp, "w", **profile) as dst:
                ...
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        yield tmp
        _os.replace(str(tmp), str(path))
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_parquet(df: pd.DataFrame, path: str | Path) -> None:
    """
    Write a DataFrame to parquet atomically.

    Uses write-to-temp-then-rename with a backup copy.  If the write
    crashes mid-way, the original file (if present) is preserved.
    """
    path = Path(path)
    tmp = path.with_suffix(".parquet.tmp")
    bak = path.with_suffix(".parquet.bak")

    # Create backup of existing file
    if path.exists():
        try:
            _shutil.copy2(str(path), str(bak))
        except OSError:
            pass

    # Write to temp, then atomic rename
    df.to_parquet(str(tmp), index=False)
    _os.replace(str(tmp), str(path))

    # Clean up backup on success
    if bak.exists():
        try:
            bak.unlink()
        except OSError:
            pass


def atomic_write_json(data: dict[str, Any], path: str | Path) -> None:
    """
    Write JSON data atomically via temp file + rename.

    A crash during write leaves the original file intact.
    """
    path = Path(path)
    tmp = path.with_suffix(".json.tmp")

    with open(str(tmp), "w", encoding="utf-8") as f:
        _json.dump(data, f, indent=2, ensure_ascii=False)

    _os.replace(str(tmp), str(path))
