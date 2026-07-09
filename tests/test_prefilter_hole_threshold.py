"""Metadata prefilter must honour interior_hole_max_frac.

Canary finding (17MPV/17MPT): the metadata prefilter dropped an acquisition
on ANY topologically-interior missing burst, ignoring the configured
interior_hole_max_frac — one permanently-missing burst (~5% of the tile)
silently discarded entire multi-year eras of a track. These tests lock in
the fix: the gap's tile-area share is ESTIMATED from burst footprints
(exact when the missing burst's geometry is known; a per-burst track-share
proxy when it was never seen in the query), and the acquisition is dropped
only when the estimate exceeds the threshold. The later raster QC still
re-measures real NoData for everything kept.

Also covers the full-window master grid builder (_build_grid_from_geoms):
the grid is era-independent (union of ALL query footprints, not batch 1)
and stays on the same snapped lattice as the profile-based builder.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

shapely = pytest.importorskip("shapely")
from shapely.geometry import box  # noqa: E402

from s1grits.asf_output_writing import _build_grid_from_geoms  # noqa: E402
from s1grits.workflow_scenes import (  # noqa: E402
    _estimate_interior_hole_frac,
    _prefilter_metadata_incomplete_acquisitions,
)

# A synthetic track: 5 along-track bursts (IW1 indices 100..104), each a
# horizontal strip 1.0 wide x 0.24 tall with 0.02 overlap, stacked to cover
# the unit tile exactly. Missing burst k leaves a strip ~= its exclusive area.
TILE = box(0.0, 0.0, 1.0, 1.0)
BIDS = [f"T018-00010{i}-IW1" for i in range(5)]


def _strip(i):
    y0 = i * 0.22
    return box(0.0, y0, 1.0, y0 + 0.24)


GEOMS = {b: _strip(i) for i, b in enumerate(BIDS)}


# ---------------------------------------------------------------------------
# _estimate_interior_hole_frac
# ---------------------------------------------------------------------------
def test_known_geometry_hole_is_exclusive_area():
    # Burst 2 missing; neighbours 1 and 3 overlap it by 0.02 on each side ->
    # exclusive hole = 1.0 x (0.24 - 2*0.02) = 0.20 of the tile.
    present = [b for b in BIDS if b != BIDS[2]]
    frac, basis = _estimate_interior_hole_frac(
        [BIDS[2]], present, GEOMS, TILE, n_footprint_bursts=5)
    assert basis == "geometry"
    assert frac == pytest.approx(0.20, abs=0.01)


def test_unknown_geometry_uses_track_share_proxy():
    # Missing burst never seen in the query -> per-burst share of the track's
    # tile coverage: full coverage / 5 bursts = 0.2.
    present = [b for b in BIDS if b != BIDS[2]]
    geoms = {b: g for b, g in GEOMS.items() if b != BIDS[2]}
    frac, basis = _estimate_interior_hole_frac(
        ["T018-000199-IW1"], present, geoms, TILE, n_footprint_bursts=5)
    assert basis == "proxy"
    # present-union coverage is slightly under 1.0 (the gap), /5
    assert frac == pytest.approx(0.16, abs=0.02)


def test_no_geometry_at_all_returns_none():
    assert _estimate_interior_hole_frac(["x"], ["y"], {}, TILE, 5) == (None, None)
    assert _estimate_interior_hole_frac(["x"], ["y"], GEOMS, None, 5) == (None, None)


# ---------------------------------------------------------------------------
# Prefilter decision
# ---------------------------------------------------------------------------
def _df(present_bids):
    return pd.DataFrame({
        "pass_id": [1] * len(present_bids),
        "acq_group_id_within_mgrs_tile": [0] * len(present_bids),
        "acq_dt": [pd.Timestamp("2017-03-30T23:36:00", tz="UTC")] * len(present_bids),
        "track_token": ["18"] * len(present_bids),
        "jpl_burst_id": present_bids,
    })


def _run_prefilter(present_bids, footprint, thr, geoms, tile):
    sink: list = []
    out = _prefilter_metadata_incomplete_acquisitions(
        mgrs_tile_id="17MPV", direction_label="ASCENDING",
        df_rtc_ts=_df(present_bids),
        track_footprint={"18": len(footprint)},
        track_footprint_ids={"18": set(footprint)},
        incomplete_policy="skip", incomplete_sink=sink,
        interior_hole_max_frac=thr, burst_geoms=geoms, tile_geom=tile,
    )
    return out, sink


def test_small_interior_gap_is_kept_under_threshold():
    """The 17MPV case: one interior burst missing, hole ~20% < thr 25% -> KEEP."""
    present = [b for b in BIDS if b != BIDS[2]]
    out, sink = _run_prefilter(present, BIDS, thr=0.25, geoms=GEOMS, tile=TILE)
    assert len(out) == len(present), "acquisition must be kept for download"
    assert sink == []


def test_large_interior_gap_is_dropped_with_estimated_pct():
    present = [b for b in BIDS if b != BIDS[2]]
    out, sink = _run_prefilter(present, BIDS, thr=0.10, geoms=GEOMS, tile=TILE)
    assert len(out) == 0, "hole ~20% > thr 10% -> dropped"
    assert len(sink) == 1
    rec = sink[0]
    assert rec["interior_hole_pct"] == pytest.approx(20.0, abs=1.0)
    assert rec["hole_estimate_basis"] == "geometry"


def test_no_geometry_keeps_legacy_conservative_drop():
    present = [b for b in BIDS if b != BIDS[2]]
    out, sink = _run_prefilter(present, BIDS, thr=0.25, geoms={}, tile=None)
    assert len(out) == 0, "without geometry the legacy drop is preserved"
    assert sink and sink[0]["hole_estimate_basis"] == "none"


def test_edge_truncation_still_always_kept():
    # Missing burst at the along-track END is edge truncation, never dropped.
    present = BIDS[:-1]
    out, sink = _run_prefilter(present, BIDS, thr=0.0, geoms=GEOMS, tile=TILE)
    assert len(out) == len(present) and sink == []


# ---------------------------------------------------------------------------
# Full-window master grid builder
# ---------------------------------------------------------------------------
def test_grid_from_geoms_covers_union_and_snaps():
    # Two footprints near the 17M UTM zone; union bounds must be covered and
    # origins snapped to the 30 m lattice.
    g1 = box(-79.6, -13.6, -79.2, -13.2)
    g2 = box(-79.4, -13.4, -78.9, -12.9)
    tr, w, h, x, y = _build_grid_from_geoms([g1, g2], "EPSG:32717", 30.0)
    assert w > 0 and h > 0
    assert tr.c % 30.0 == pytest.approx(0.0, abs=1e-6)
    assert tr.f % 30.0 == pytest.approx(0.0, abs=1e-6)
    # Order-independent (deterministic for a given query, any batch order).
    tr2, w2, h2, _, _ = _build_grid_from_geoms([g2, g1], "EPSG:32717", 30.0)
    assert (w, h, tuple(tr)[:6]) == (w2, h2, tuple(tr2)[:6])
    # Strictly larger than either footprint alone (era-independence: the
    # union covers ALL eras' bursts, not just the first batch's).
    tr1, w1, h1, _, _ = _build_grid_from_geoms([g1], "EPSG:32717", 30.0)
    assert w > w1 or h > h1


def test_grid_from_geoms_rejects_empty():
    with pytest.raises(ValueError):
        _build_grid_from_geoms([], "EPSG:32717", 30.0)
