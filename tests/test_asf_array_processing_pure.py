"""Smoke/regression tests for the pure-Python asf_array_processing module.

The GLCM and despeckle numerics are validated in depth by
``test_glcm_halo_equivalence.py`` and ``test_despeckle_window_equivalence.py``.
This module only guards the restoration itself: the module is pure Python,
the public API is intact, and the core entry points run and preserve NaN
masks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

ap = pytest.importorskip("s1grits.asf_array_processing")

CFG = {
    "enabled": True, "inputs": ["VV_dB", "VH_dB"],
    "metrics": ["contrast", "homogeneity", "entropy", "correlation"],
    "window_size": 5, "distance": 1, "angles": [0, 90],
    "average_angles": True, "levels": 16,
    "vv_db_range": [-25, 5], "vh_db_range": [-32, -5],
}


def test_module_is_pure_python():
    assert ap.__file__.endswith(".py")


def test_public_api_present():
    for name in [
        "interpolate_arr", "despeckle_2d", "compute_glcm_texture_bands",
        "_get_texture_band_names", "_glcm_quantize", "_glcm_metrics_one_angle",
        "_glcm_band_name", "_despeckle_tv_bregman_linear", "_despeckle_nlm_linear",
        "DB_SCALE_FACTOR",
    ]:
        assert hasattr(ap, name), f"missing {name}"


def test_texture_band_names_order():
    names = ap._get_texture_band_names(CFG)
    assert names == [
        "VV_glcm_CONTR", "VV_glcm_IDM", "VV_glcm_ENT", "VV_glcm_CORR",
        "VH_glcm_CONTR", "VH_glcm_IDM", "VH_glcm_ENT", "VH_glcm_CORR",
    ]


def test_glcm_runs_and_preserves_nodata():
    rng = np.random.default_rng(0)
    vv = (rng.random((48, 48)) * 30 - 25).astype(np.float32)
    vh = (rng.random((48, 48)) * 27 - 32).astype(np.float32)
    vv[:6, :] = np.nan
    vh[:6, :] = np.nan
    arrs, names = ap.compute_glcm_texture_bands(vv, vh, CFG)
    assert names == ap._get_texture_band_names(CFG)
    # NaN in input is NaN in every output band
    for a in arrs:
        assert np.isnan(a[:6, :]).all()


def test_tv_despeckle_preserves_nan_and_returns_none():
    assert ap.despeckle_2d(None) is None
    rng = np.random.default_rng(1)
    lin = (10 ** ((rng.random((32, 32)) * 20 - 15) / 10)).astype(np.float32)
    lin[:4, :] = np.nan
    out = ap.despeckle_2d(lin, method="tv_bregman", tv_kwargs={"reg_param": 5.0})
    assert out.shape == lin.shape
    assert np.isnan(out[:4, :]).all()


def test_unknown_method_raises():
    with pytest.raises(ValueError):
        ap.despeckle_2d(np.ones((4, 4), dtype=np.float32), method="bogus")
