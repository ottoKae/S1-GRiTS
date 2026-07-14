"""Phase 3 — disk-backed batch sources (s1grits.batch_spill).

Locks the contract of the spill layer: byte-identical values through the
.npy round trip, ndarray semantics of the returned memmap (slicing works
exactly like the in-RAM array it replaces), inert-when-disabled passthrough,
graceful fallback on filesystem errors, per-batch cleanup, and — the one
that matters — END-TO-END writer parity: the smonthly composite produced
from spilled (memmap) sources is byte-identical to the one produced from
in-RAM arrays.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest
from rasterio.transform import Affine

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

pytest.importorskip("rasterio")
zarr = pytest.importorskip("zarr")

from s1grits import batch_spill  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_spill():
    """Every test starts and ends with spilling disabled."""
    batch_spill.configure(None)
    yield
    batch_spill._remove_dir_quiet()
    batch_spill.configure(None)


def _arr():
    rng = np.random.default_rng(11)
    a = rng.normal(-12, 3, (40, 50)).astype(np.float32)
    a[:5, :] = np.nan
    return a


# ---------------------------------------------------------------------------
# Core semantics
# ---------------------------------------------------------------------------

def test_disabled_is_pure_passthrough():
    a = _arr()
    assert batch_spill.maybe_spill(a) is a
    assert batch_spill.maybe_spill(None) is None
    assert not batch_spill.is_enabled()


def test_spill_roundtrip_is_byte_identical(tmp_path):
    batch_spill.configure(tmp_path)
    a = _arr()
    m = batch_spill.maybe_spill(a)
    assert isinstance(m, np.memmap)
    assert m.dtype == a.dtype and m.shape == a.shape
    np.testing.assert_array_equal(np.asarray(m), a)  # NaNs included
    # ndarray semantics: window slicing behaves exactly like the source
    np.testing.assert_array_equal(m[10:20, 5:15], a[10:20, 5:15])
    assert float(np.nansum(m)) == pytest.approx(float(np.nansum(a)))


def test_spill_none_and_filesystem_fallback(tmp_path):
    batch_spill.configure(tmp_path)
    assert batch_spill.maybe_spill(None) is None
    a = _arr()
    with mock.patch("numpy.save", side_effect=OSError("disk full")):
        out = batch_spill.maybe_spill(a)
    assert out is a  # graceful in-RAM fallback, never a failure


def test_cleanup_batch_removes_files(tmp_path):
    batch_spill.configure(tmp_path)
    refs = [batch_spill.maybe_spill(_arr()) for _ in range(3)]
    spill_dir = tmp_path / f"pid-{__import__('os').getpid()}"
    assert len(list(spill_dir.glob("burst-*.npy"))) == 3
    del refs
    removed = batch_spill.cleanup_batch()
    assert removed == 3
    assert list(spill_dir.glob("burst-*.npy")) == []


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX unlink-while-mapped semantics")
def test_posix_unlink_keeps_live_memmaps_readable(tmp_path):
    """Prefetch interaction: cleanup may unlink files whose memmaps are
    still referenced (batch N+1's slot); reads must keep working."""
    batch_spill.configure(tmp_path)
    a = _arr()
    m = batch_spill.maybe_spill(a)
    batch_spill.cleanup_batch()  # unlinks while m is alive
    np.testing.assert_array_equal(np.asarray(m), a)


# ---------------------------------------------------------------------------
# End-to-end: smonthly writer parity with spilled sources
# ---------------------------------------------------------------------------

def test_smonthly_writer_parity_spilled_vs_ram(tmp_path):
    from s1grits import workflow_scenes as ws
    from s1grits.zarr_cf import band_data_vars

    dates = [pd.Timestamp(f"2020-01-{d:02d}T00:00:00Z") for d in (5, 17)]
    scenes = {}
    rng = np.random.default_rng(4)
    for i in range(len(dates)):
        a = rng.lognormal(np.log(10 ** (-12 / 10)), 0.5, (30, 40)).astype(np.float32)
        a[:3, :] = np.nan
        scenes[i] = a

    def run(tag: str, spill: bool):
        if spill:
            batch_spill.configure(tmp_path / f"spill_{tag}")
        else:
            batch_spill.configure(None)
        # Simulate the decode path: sources arrive through maybe_spill
        final_vv = [batch_spill.maybe_spill(scenes[i].copy()) for i in range(len(dates))]
        final_vh = [batch_spill.maybe_spill((scenes[i] * 0.25).astype(np.float32))
                    for i in range(len(dates))]
        df = pd.DataFrame({
            "acq_dt": dates, "track_number": [18] * 2, "track_token": ["18"] * 2,
            "pass_id": [1, 2], "acq_group_id_within_mgrs_tile": [1, 1],
            "jpl_burst_id": ["B0", "B1"], "opera_id": ["O0", "O1"],
        })
        tile_dir = tmp_path / f"ws_{tag}"
        with mock.patch.object(ws, "_mosaic_align",
                               lambda idx, fa, *a, **k: np.asarray(fa[int(idx[0])]).copy()), \
             mock.patch.object(ws, "_write_monthly_stac_item", lambda *a, **k: "x"):
            ws._write_smonthly_one_track(
                "17MPU", "ASCENDING", tile_dir,
                final_vv=final_vv, prof_vv=[], final_vh=final_vh, prof_vh=[],
                clean_dates=dates, target_crs="EPSG:32717", target_res=30.0,
                generate_cog=False, generate_preview=False,
                chunk_y=16, chunk_x=16, cog_block=16, on_time_conflict="skip",
                monthly_cfg={"composite_method": "nanmedian"},
                processing_level="ARDC", transform=Affine.identity(),
                width=40, height=30,
                x_coords=np.arange(40), y_coords=np.arange(30),
                df_batch=df, tile_clip=False, track_token="18",
                n_bursts_track=2, restrict_to_group=True,
                valid_clean_indices={0, 1},
            )
        store = list(tile_dir.glob("smonthly_*/zarr/*.zarr"))[0]
        g = zarr.open_group(str(store), mode="r", zarr_format=3)
        return {b: np.asarray(g[b][:]) for b in band_data_vars(g)}

    ram = run("ram", spill=False)
    spl = run("spill", spill=True)
    assert set(ram) == set(spl)
    for band in ram:
        np.testing.assert_array_equal(ram[band], spl[band], err_msg=band)
