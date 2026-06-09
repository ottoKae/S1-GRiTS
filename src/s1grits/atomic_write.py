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
from pathlib import Path
from typing import Any

import pandas as pd


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
