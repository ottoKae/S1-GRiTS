"""Incremental-resume grid compatibility: pilot month -> full year.

The burst-union master grid depends on the processing window (and on which
scenes actually downloaded in a run's first batch), so a rerun over a larger
window derives a different H x W than the grid locked into a store written by
a pilot run. These tests lock in the resume semantics that make incremental
updating safe:

* ``_adopt_existing_master_grid`` — a rerun with ``output.overwrite=false``
  adopts the grid already locked into an existing store (instead of failing
  the grid-lock check with a freshly derived, different grid);
* ``_init_zarr_2band(rebuild_on_mismatch=False)`` — an incompatible store
  raises an actionable error naming the recovery options;
* ``_init_zarr_2band(rebuild_on_mismatch=True)`` — ``output.overwrite=true``
  rebuilds the incompatible store on the new grid instead of failing;
* a compatible store is reopened in-place with its time steps intact.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from rasterio.transform import Affine

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

zarr = pytest.importorskip("zarr")

from s1grits.workflow_scenes import (  # noqa: E402
    _adopt_existing_master_grid,
    _append_zarr_timestep,
    _init_zarr_2band,
)

CRS = "EPSG:32717"
RES = 30.0
BANDS = ["VV_dB", "VH_dB"]


def _grid(minx: float, maxy: float, width: int, height: int):
    """Res-aligned grid tuple mirroring _build_grid_from_bursts output."""
    transform = Affine(RES, 0.0, minx, 0.0, -RES, maxy)
    x = (minx + (np.arange(width) + 0.5) * RES).astype("float64")
    y = (maxy - (np.arange(height) + 0.5) * RES).astype("float64")
    return transform, width, height, x, y


def _make_store(tile_dir: Path, grid, name="s1grits_smonthly_17MPU_ASCENDING_TK18_N3.zarr"):
    transform, w, h, x, y = grid
    zp = tile_dir / "smonthly_VV_dB_VH_dB" / "zarr" / name
    g = _init_zarr_2band(
        zp, x, y, CRS, transform, chunk_y=32, chunk_x=32,
        processing_level="monthly_ARDC", band_names=BANDS,
    )
    return zp, g


def _append_month(g, when: str):
    h = g["y"].shape[0]
    w = g["x"].shape[0]
    dt = np.datetime64(pd.Timestamp(when).to_datetime64(), "ns")
    _append_zarr_timestep(
        g, dt, [(b, np.full((h, w), 1.0, np.float32)) for b in BANDS]
    )


# ---------------------------------------------------------------------------
# Resume-grid adoption
# ---------------------------------------------------------------------------
def test_adopts_locked_grid_from_existing_store(tmp_path):
    tile_dir = tmp_path / "17MPU"
    pilot = _grid(500000.0, 8500000.0, width=64, height=48)  # pilot-month grid
    _make_store(tile_dir, pilot)

    adopted = _adopt_existing_master_grid(tile_dir, CRS, RES)
    assert adopted is not None
    a_tr, a_w, a_h, a_x, a_y = adopted
    p_tr, p_w, p_h, p_x, p_y = pilot
    assert (a_w, a_h) == (p_w, p_h)
    assert tuple(a_tr)[:6] == pytest.approx(tuple(p_tr)[:6])
    np.testing.assert_allclose(a_x, p_x)
    np.testing.assert_allclose(a_y, p_y)


def test_adoption_enables_pilot_to_full_year_resume(tmp_path):
    """Pilot store + adopted grid -> full-year rerun reopens cleanly.

    A full-year rerun would derive a LARGER burst-union grid; adopting the
    pilot store's grid instead must let the store open for append (the actual
    failure mode this guards: RuntimeError('Cannot resume: ... grid mismatch')).
    """
    tile_dir = tmp_path / "17MPU"
    pilot = _grid(500000.0, 8500000.0, width=64, height=48)
    zp, g = _make_store(tile_dir, pilot)
    _append_month(g, "2026-01-15")  # January written by the pilot run

    # Full-year rerun: adopt the locked grid rather than the (different)
    # window-derived one, then reopen the same store with it.
    a_tr, a_w, a_h, a_x, a_y = _adopt_existing_master_grid(tile_dir, CRS, RES)
    g2 = _init_zarr_2band(
        zp, a_x, a_y, CRS, a_tr, chunk_y=32, chunk_x=32,
        processing_level="monthly_ARDC", band_names=BANDS,
    )
    assert g2["time"].shape[0] == 1  # January preserved
    _append_month(g2, "2026-02-14")  # February appends incrementally
    assert g2["time"].shape[0] == 2


def test_mixed_grid_siblings_adopt_data_richest_grid(tmp_path, caplog):
    """A tile whose sibling stores sit on DIFFERENT grids (the state left by
    an interrupted pre-adoption run) adopts the grid backing the most time
    steps, and warns naming the disagreeing stores (the 17MQV canary case)."""
    import logging
    tile_dir = tmp_path / "17MQV"
    pilot = _grid(500000.0, 8500000.0, width=86, height=61)     # populated
    stray = _grid(502000.0, 8498000.0, width=41, height=53)     # interrupted run

    zp_pilot, g_pilot = _make_store(
        tile_dir, pilot, name="s1grits_smonthly_17MQV_ASCENDING_TK18_N3.zarr")
    _append_month(g_pilot, "2026-01-15")
    _append_month(g_pilot, "2026-02-14")
    zp_stray, g_stray = _make_store(
        tile_dir, stray, name="s1grits_smonthly_17MQV_ASCENDING_TK91_N2.zarr")
    # stray store: zero or fewer steps than the pilot

    with caplog.at_level(logging.WARNING):
        adopted = _adopt_existing_master_grid(tile_dir, CRS, RES)
    a_tr, a_w, a_h, _, _ = adopted
    assert (a_w, a_h) == (pilot[1], pilot[2])  # most data wins
    warn = "\n".join(r.message for r in caplog.records)
    assert "DIFFERENT grids" in warn
    assert "TK91" in warn  # disagreeing store is named

    # Self-heal path: rebuild-incompatible rebuilds the stray store onto the
    # adopted grid while the pilot store resumes untouched.
    del g_stray
    g2 = _init_zarr_2band(
        zp_stray, adopted[3], adopted[4], CRS, a_tr, chunk_y=32, chunk_x=32,
        processing_level="monthly_ARDC", band_names=BANDS,
        rebuild_on_mismatch=True,
    )
    assert (g2["x"].shape[0], g2["y"].shape[0]) == (a_w, a_h)
    g3 = _init_zarr_2band(
        zp_pilot, adopted[3], adopted[4], CRS, a_tr, chunk_y=32, chunk_x=32,
        processing_level="monthly_ARDC", band_names=BANDS,
        rebuild_on_mismatch=True,
    )
    assert g3["time"].shape[0] == 2  # pilot data preserved


def test_adoption_skips_mismatched_crs_and_resolution(tmp_path):
    tile_dir = tmp_path / "17MPU"
    _make_store(tile_dir, _grid(500000.0, 8500000.0, width=16, height=16))

    assert _adopt_existing_master_grid(tile_dir, "EPSG:32718", RES) is None
    assert _adopt_existing_master_grid(tile_dir, CRS, 10.0) is None
    # And an empty tile dir has nothing to adopt.
    assert _adopt_existing_master_grid(tmp_path / "17MQV", CRS, RES) is None


# ---------------------------------------------------------------------------
# Grid-lock check vs output.overwrite
# ---------------------------------------------------------------------------
def test_grid_mismatch_without_overwrite_raises_actionable_error(tmp_path):
    tile_dir = tmp_path / "17MPU"
    zp, g = _make_store(tile_dir, _grid(500000.0, 8500000.0, width=64, height=48))
    _append_month(g, "2026-01-15")

    bigger = _grid(499000.0, 8501000.0, width=96, height=80)
    tr, w, h, x, y = bigger
    with pytest.raises(RuntimeError) as exc:
        _init_zarr_2band(
            zp, x, y, CRS, tr, chunk_y=32, chunk_x=32,
            processing_level="monthly_ARDC", band_names=BANDS,
            rebuild_on_mismatch=False,
        )
    msg = str(exc.value)
    # The error must tell the operator exactly how to recover, teaching the
    # v3 policy key while still naming the legacy alias.
    assert "grid mismatch" in msg
    assert "output.existing_store" in msg and "rebuild-incompatible" in msg
    assert "output.overwrite=true" in msg
    assert "output.base_dir" in msg
    # And must not have touched the store.
    g_check = zarr.open_group(str(zp), mode="r", zarr_format=3)
    assert g_check["time"].shape[0] == 1


def test_overwrite_true_rebuilds_incompatible_store(tmp_path):
    tile_dir = tmp_path / "17MPU"
    zp, g = _make_store(tile_dir, _grid(500000.0, 8500000.0, width=64, height=48))
    _append_month(g, "2026-01-15")
    del g

    bigger = _grid(499000.0, 8501000.0, width=96, height=80)
    tr, w, h, x, y = bigger
    g2 = _init_zarr_2band(
        zp, x, y, CRS, tr, chunk_y=32, chunk_x=32,
        processing_level="monthly_ARDC", band_names=BANDS,
        rebuild_on_mismatch=True,
    )
    # Fresh store on the new grid: old time steps discarded, new dims live.
    assert g2["time"].shape[0] == 0
    assert g2["x"].shape[0] == w and g2["y"].shape[0] == h
    _append_month(g2, "2026-01-15")
    assert g2["time"].shape[0] == 1


def test_compatible_store_resumes_in_place_regardless_of_flag(tmp_path):
    tile_dir = tmp_path / "17MPU"
    grid = _grid(500000.0, 8500000.0, width=64, height=48)
    zp, g = _make_store(tile_dir, grid)
    _append_month(g, "2026-01-15")
    del g

    tr, w, h, x, y = grid
    for flag in (False, True):
        g2 = _init_zarr_2band(
            zp, x, y, CRS, tr, chunk_y=32, chunk_x=32,
            processing_level="monthly_ARDC", band_names=BANDS,
            rebuild_on_mismatch=flag,
        )
        # overwrite only authorizes rebuilding INCOMPATIBLE stores; a
        # compatible one is always resumed with its contents intact.
        assert g2["time"].shape[0] == 1
