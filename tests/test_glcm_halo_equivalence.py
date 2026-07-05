"""GLCM halo-equivalence: spec test for a future blockwise GLCM path.

GLCM texture (``asf_array_processing.compute_glcm_texture_bands``) currently
forces the legacy full-tile path.  This test establishes — with executable
evidence — the contract a blockwise+halo implementation must meet: a block
computed with a sufficient halo of surrounding context, then cropped back to
the block, is **bit-identical** to the corresponding region of the full-tile
result.

Support radius is ``window_size // 2 + distance`` because quantization uses
*fixed* config dB ranges (per-pixel, not image statistics), the co-occurrence
shift is ``distance`` pixels, and accumulation is a ``window_size`` box filter.
For the default config (window_size=5, distance=1) that is 3 px; this test
proves halo=3 is exact and halo<3 is not, and that halo=8 is safely exact even
across NaN-edged blocks.

No workflow changes are made here — this is the spec the future code will match.
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
SUPPORT_RADIUS = CFG["window_size"] // 2 + CFG["distance"]  # = 3


def _make_tile(h=96, w=96, seed=1, with_nan=False):
    rng = np.random.default_rng(seed)
    vv = (rng.random((h, w)) * 30 - 25).astype(np.float32)
    vh = (rng.random((h, w)) * 27 - 32).astype(np.float32)
    if with_nan:
        vv[:8, :] = np.nan   # swath-edge NoData
        vh[:8, :] = np.nan
        vv[:, :6] = np.nan
        vh[:, :6] = np.nan
    return vv, vh


def _blockwise_with_halo(vv, vh, cfg, block, halo):
    """Reference blockwise+halo reconstruction of every GLCM band."""
    h, w = vv.shape
    full_ref, names = ap.compute_glcm_texture_bands(vv, vh, cfg)
    out = [np.full((h, w), np.nan, dtype=np.float32) for _ in names]
    for y0 in range(0, h, block):
        for x0 in range(0, w, block):
            y1, x1 = min(h, y0 + block), min(w, x0 + block)
            hy0, hx0 = max(0, y0 - halo), max(0, x0 - halo)
            hy1, hx1 = min(h, y1 + halo), min(w, x1 + halo)
            sub, _ = ap.compute_glcm_texture_bands(
                vv[hy0:hy1, hx0:hx1], vh[hy0:hy1, hx0:hx1], cfg
            )
            oy, ox = y0 - hy0, x0 - hx0
            for bi in range(len(names)):
                out[bi][y0:y1, x0:x1] = sub[bi][oy:oy + (y1 - y0), ox:ox + (x1 - x0)]
    return out, full_ref, names


@pytest.mark.parametrize("with_nan", [False, True])
def test_halo_8_is_bit_exact(with_nan):
    vv, vh = _make_tile(with_nan=with_nan)
    got, ref, names = _blockwise_with_halo(vv, vh, CFG, block=32, halo=8)
    for bi, name in enumerate(names):
        assert np.array_equal(
            np.nan_to_num(got[bi], nan=-9e9), np.nan_to_num(ref[bi], nan=-9e9)
        ), f"halo=8 not bit-exact for band {name}"


def test_minimal_support_radius_is_exact():
    vv, vh = _make_tile(with_nan=True)
    got, ref, names = _blockwise_with_halo(vv, vh, CFG, block=32, halo=SUPPORT_RADIUS)
    for bi, name in enumerate(names):
        a = np.nan_to_num(got[bi], nan=-9e9)
        b = np.nan_to_num(ref[bi], nan=-9e9)
        assert np.allclose(a, b, atol=1e-4), f"halo={SUPPORT_RADIUS} not exact for {name}"


def test_zero_halo_is_not_exact():
    """A halo is genuinely required: block-local GLCM differs at block seams."""
    vv, vh = _make_tile()
    got, ref, names = _blockwise_with_halo(vv, vh, CFG, block=32, halo=0)
    diffs = [
        np.nanmax(np.abs(got[bi] - ref[bi]))
        for bi in range(len(names))
    ]
    assert max(diffs) > 1e-2, (
        "expected block-seam error without a halo; if this fails the support "
        "assumption is wrong and the halo width must be re-derived"
    )
