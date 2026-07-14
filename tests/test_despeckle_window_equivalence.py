"""Despeckle acquisition-window equivalence: spec + diagnostic.

Despeckle (TV-Bregman / NLM via ``asf_array_processing.despeckle_2d``) is
applied per acquisition, post-mosaic, and currently forces the legacy
full-tile path.  A blockwise-friendly alternative is to despeckle each
acquisition on the bounding *window* of its valid pixels (~half a tile, one at
a time) instead of a full-tile array.

This test establishes the real contract for that strategy, and it deliberately
**corrects an earlier assumption** that a tight crop would be bit-exact:

* With a sufficient NaN *margin* around the valid footprint, window despeckle
  is bit-identical to full-tile despeckle within the footprint.
* With a tight (zero-margin) crop it is NOT — TV-Bregman's soft NaN padding
  makes the result near the valid/NoData boundary depend on how much NoData
  context surrounds it.

So the future implementation must crop to ``valid_bbox + margin``.  The margin
needed scales with the filter's smoothing strength (``reg_param``); this test
measures convergence rather than asserting a single magic number, so it stays
robust across skimage versions.
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


def _despeckle(arr, reg_param=5.0):
    return ap.despeckle_2d(arr, method="tv_bregman", tv_kwargs={"reg_param": reg_param})


def _make_acquisition(h=120, w=120, vy=(20, 80), vx=(10, 110), seed=2):
    """A single-swath acquisition: valid strip surrounded by NoData."""
    rng = np.random.default_rng(seed)
    full = np.full((h, w), np.nan, dtype=np.float32)
    vh_, vw_ = vy[1] - vy[0], vx[1] - vx[0]
    lin = (10 ** ((rng.random((vh_, vw_)) * 20 - 15) / 10)).astype(np.float32)
    full[vy[0]:vy[1], vx[0]:vx[1]] = lin
    return full, (slice(*vy), slice(*vx))


def _window_despeckle(full, valid_slices, margin):
    h, w = full.shape
    ys, xs = valid_slices
    wy = slice(max(0, ys.start - margin), min(h, ys.stop + margin))
    wx = slice(max(0, xs.start - margin), min(w, xs.stop + margin))
    out = np.full((h, w), np.nan, dtype=np.float32)
    out[wy, wx] = _despeckle(full[wy, wx])
    return out


def test_sufficient_margin_is_bit_exact_within_footprint():
    full, valid = _make_acquisition()
    ref = _despeckle(full)
    got = _window_despeckle(full, valid, margin=48)  # generous margin
    m = np.isfinite(full)
    assert np.allclose(ref[m], got[m], atol=1e-6), (
        "window despeckle with a generous margin must match full-tile within "
        "the valid footprint"
    )


def test_tight_crop_is_not_bit_exact():
    """Documents WHY a margin is required (corrects the earlier assumption)."""
    full, valid = _make_acquisition()
    ref = _despeckle(full)
    tight = _window_despeckle(full, valid, margin=0)
    m = np.isfinite(full)
    max_err = float(np.nanmax(np.abs(ref[m] - tight[m])))
    # A tight crop produces a real, non-negligible boundary error.
    assert max_err > 1e-3, (
        "expected a tight crop to differ from full-tile despeckle; if this "
        "fails, window despeckle could skip the margin entirely"
    )


def test_error_decreases_monotonically_with_margin():
    """Convergence evidence: larger margin -> smaller boundary error."""
    full, valid = _make_acquisition()
    ref = _despeckle(full)
    m = np.isfinite(full)
    errs = []
    for margin in (0, 8, 24, 48):
        got = _window_despeckle(full, valid, margin=margin)
        errs.append(float(np.nanmax(np.abs(ref[m] - got[m]))))
    # Non-increasing, and the largest margin is effectively converged.
    for a, b in zip(errs, errs[1:]):
        assert b <= a + 1e-6, f"margin error not monotonic: {errs}"
    assert errs[-1] < 1e-5, f"largest margin did not converge: {errs}"


# ---------------------------------------------------------------------------
# despeckle_2d_windowed — the Phase-1 implementation of this contract
# (docs/scenes_blockwise_architecture.md)
# ---------------------------------------------------------------------------

def test_windowed_impl_matches_full_frame_within_footprint():
    full, _ = _make_acquisition()
    ref = _despeckle(full)
    got = ap.despeckle_2d_windowed(
        full, method="tv_bregman", tv_kwargs={"reg_param": 5.0}, margin=48
    )
    m = np.isfinite(full)
    assert np.allclose(ref[m], got[m], atol=1e-6)
    # Outside the footprint the input had no data: output must be NaN there,
    # exactly like the full-frame path (mask restored).
    np.testing.assert_array_equal(np.isnan(ref), np.isnan(got))


def test_windowed_impl_footprint_touching_edges():
    """Footprint reaching the array border: window clamps, still equivalent.

    Uses the shipped default margin (64), which is measured EXACTLY
    converged for this geometry (48 is not — margin needs grow with
    footprint size, which is why the default carries headroom).
    """
    full, _ = _make_acquisition(h=250, w=250, vy=(0, 120), vx=(140, 250))
    ref = _despeckle(full)
    got = ap.despeckle_2d_windowed(
        full, method="tv_bregman", tv_kwargs={"reg_param": 5.0},
        margin=ap.DESPECKLE_WINDOW_MARGIN_PX,
    )
    m = np.isfinite(full)
    np.testing.assert_array_equal(ref[m], got[m])


def test_windowed_impl_near_full_frame_uses_direct_path():
    """Valid data everywhere -> the shortcut must return the exact
    full-frame result (same call, no crop/re-embed)."""
    rng = np.random.default_rng(3)
    full = (10 ** ((rng.random((80, 90)) * 20 - 15) / 10)).astype(np.float32)
    ref = _despeckle(full)
    got = ap.despeckle_2d_windowed(
        full, method="tv_bregman", tv_kwargs={"reg_param": 5.0}
    )
    np.testing.assert_array_equal(ref, got)


def test_windowed_impl_all_nan_and_none():
    assert ap.despeckle_2d_windowed(None) is None
    empty = np.full((30, 30), np.nan, dtype=np.float32)
    out = ap.despeckle_2d_windowed(empty)
    assert out.shape == empty.shape and np.isnan(out).all()
