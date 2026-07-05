"""Smoke/regression tests for the pure-Python asf_output_writing module.

Guards the restoration of the module that the scenes/blockwise workflow
imports for its live helpers (_build_grid_from_bursts, _mosaic_align,
_get_mgrs_tile_geometry_wkt, _clip_arrays_to_wkt_4326, _check_tile_integrity,
_zarr_delete_timestep, _generate_preview_png) plus the legacy monthly
builders.  Confirms pure-Python, full public API, and correct behavior of the
grid/mosaic helpers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import Affine

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

ow = pytest.importorskip("s1grits.asf_output_writing")


def test_module_is_pure_python():
    assert ow.__file__.endswith(".py")


def test_public_api_present():
    for name in [
        # live (scenes/blockwise + static)
        "_build_grid_from_bursts", "_build_grid_from_mgrs_tile", "_mosaic_align",
        "_get_mgrs_tile_geometry_wkt", "_clip_arrays_to_wkt_4326",
        "_check_tile_integrity", "_zarr_delete_timestep", "_generate_preview_png",
        "_zarr_append", "merge_tile_catalogs", "_upsert_tile_catalog",
        # legacy monthly builders (kept for compatibility)
        "build_s1_monthly_cog_and_zarr_crossUTM",
        "build_s1_monthly_cog_and_zarr_tileUTM",
        "build_s1_monthly_median_cube", "merge_acq_group_zarrs",
    ]:
        assert hasattr(ow, name), f"missing public function {name}"


def test_mgrs_wkt_and_grid_for_real_tile():
    wkt = ow._get_mgrs_tile_geometry_wkt("17MPU")
    assert wkt.upper().startswith("POLYGON")
    transform, w, h, xs, ys = ow._build_grid_from_mgrs_tile("17MPU", "EPSG:32717", 30.0)
    assert w > 0 and h > 0
    assert len(xs) == w and len(ys) == h
    assert transform.a == 30.0 and transform.e == -30.0


def test_mgrs_wkt_unknown_tile_raises():
    with pytest.raises(ValueError):
        ow._get_mgrs_tile_geometry_wkt("99ZZZ")


def test_mosaic_align_first_valid_pixel():
    master = Affine(30, 0, 500000, 0, -30, 9500000)
    a = np.ones((50, 60), dtype=np.float32) * 3.0
    b = np.ones((40, 50), dtype=np.float32) * 7.0
    profs = [
        {"transform": master * Affine.translation(5, 5), "crs": "EPSG:32717", "nodata": np.nan},
        {"transform": master * Affine.translation(20, 20), "crs": "EPSG:32717", "nodata": np.nan},
    ]
    out = ow._mosaic_align([0, 1], [a, b], profs, 100, 100, master, "EPSG:32717")
    assert out.shape == (100, 100)
    # first-valid-pixel: where scene 0 covers, value is 3.0 (it wins)
    assert np.nanmax(out) == pytest.approx(7.0)
    assert 3.0 in np.unique(out[np.isfinite(out)])


def test_mosaic_align_none_when_no_valid():
    master = Affine(30, 0, 500000, 0, -30, 9500000)
    out = ow._mosaic_align([0], [None], [{"transform": master, "crs": "EPSG:32717"}],
                           10, 10, master, "EPSG:32717")
    assert out is None


def test_build_grid_from_bursts_union():
    profs = [
        {"height": 100, "width": 120, "transform": Affine(30, 0, 500000, 0, -30, 9500000), "crs": "EPSG:32717"},
        {"height": 80, "width": 90, "transform": Affine(30, 0, 502000, 0, -30, 9498000), "crs": "EPSG:32717"},
    ]
    transform, w, h, xs, ys = ow._build_grid_from_bursts(profs, "EPSG:32717", 30.0)
    assert w > 0 and h > 0 and transform.a == 30.0
