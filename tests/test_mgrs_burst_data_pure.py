"""Smoke/regression tests for the pure-Python mgrs_burst_data module.

Confirms the restored source (replacing the cp312-only compiled extension)
exposes the same public API and returns schema-valid data from the bundled
parquet tables for the active-workflow entry points.  These functions are
called at the very start of every scenes/monthly/static run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

mbd = pytest.importorskip("s1grits.mgrs_burst_data")

REAL_TILES = ["17MPU", "17MPV", "17MQV", "17NPA"]


def test_module_is_pure_python():
    assert mbd.__file__.endswith(".py"), "expected pure-Python module, got compiled"


def test_public_api_present():
    for name in [
        "get_mgrs_burst_lut_path", "get_mgrs_data_path", "get_burst_data_path",
        "get_burst_table", "get_mgrs_burst_lut", "get_lut_by_mgrs_tile_ids",
        "get_mgrs_table", "get_mgrs_tile_table_by_ids",
        "get_mgrs_tiles_overlapping_geometry", "get_burst_ids_in_mgrs_tiles",
        "get_burst_table_from_mgrs_tiles",
    ]:
        assert hasattr(mbd, name), f"missing public function {name}"


def test_lut_and_tile_lookups_for_real_tiles():
    lut = mbd.get_lut_by_mgrs_tile_ids(REAL_TILES)
    assert not lut.empty
    assert set(lut["mgrs_tile_id"]).issubset(set(REAL_TILES))

    tbl = mbd.get_mgrs_tile_table_by_ids(REAL_TILES)
    assert len(tbl) == len(REAL_TILES)


def test_burst_ids_nonempty_and_unique():
    ids = mbd.get_burst_ids_in_mgrs_tiles(REAL_TILES)
    assert ids and len(ids) == len(set(ids))


def test_burst_table_from_mgrs_tiles_returns_rows():
    # This function crashed in the shipped compiled .so ("Expected list, got
    # GeoDataFrame"); the restored pure source returns a valid table.
    df = mbd.get_burst_table_from_mgrs_tiles(["17MPU"])
    assert not df.empty
    assert "jpl_burst_id" in df.columns
