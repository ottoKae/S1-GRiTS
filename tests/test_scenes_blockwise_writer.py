"""Blockwise scenes writer behaviour.

The per-acquisition scenes path is bounded-memory and blockwise: per-block
mosaic / dB / Ratio / RVI / clip / Zarr write, halo-blockwise GLCM, and
COG/preview streamed back from the store. These tests drive the REAL
``_write_scenes_output`` with real burst arrays + rasterio profiles on the
master grid (so the true windowed-mosaic path runs) and check the written
bands, multi-timestep ordering, COG assets, and the interior-hole QC skip
across the feature matrix: despeckle off/on x GLCM off/on x tile_clip off/on.
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
pytest.importorskip("cv2")
zarr = pytest.importorskip("zarr")

from s1grits import workflow_scenes as ws  # noqa: E402
from s1grits.zarr_cf import band_data_vars  # noqa: E402

# Master grid: 48x64 @ 30 m in a real UTM zone (EPSG:32717, tile 17MPU area).
H, W = 48, 64
RES = 30.0
CRS = "EPSG:32717"
MASTER_T = Affine(RES, 0.0, 499980.0, 0.0, -RES, 8_500_000.0)


def _burst(row0: int, row1: int, seed: int, *, scale: float = 1.0) -> tuple:
    """A burst array + profile ON the master grid (integer row offset), so it is
    direct-copyable — the blockwise direct-copy reproduces the input exactly.
    Covers master rows [row0, row1); interior NaNs excluded."""
    rng = np.random.default_rng(seed)
    h = row1 - row0
    a = rng.lognormal(np.log(10 ** (-12 / 10)), 0.5, (h, W)).astype(np.float32)
    a[2:6, 8:20] = 5.0 * scale  # bright block, > 1.0 linear
    a[:, :3] = np.nan           # left NaN margin (burst-union edge)
    a *= scale
    prof = {
        "transform": MASTER_T * Affine.translation(0, row0),
        "crs": CRS,
        "nodata": np.nan,
        "width": W,
        "height": h,
    }
    return a, prof


def _batch(n_dates: int):
    """Two overlapping bursts per date on the master grid, for VV and VH."""
    final_vv, prof_vv, final_vh, prof_vh = [], [], [], []
    for d in range(n_dates):
        for (r0, r1) in ((0, 28), (24, H)):  # overlap in rows 24:28
            avv, pvv = _burst(r0, r1, seed=100 + d * 10 + r0)
            avh, pvh = _burst(r0, r1, seed=100 + d * 10 + r0, scale=0.25)
            final_vv.append(avv); prof_vv.append(pvv)
            final_vh.append(avh); prof_vh.append(pvh)
    return final_vv, prof_vv, final_vh, prof_vh


def _df(dates):
    rows = []
    idx = 0
    for d, dt in enumerate(dates):
        for _ in range(2):  # two bursts share the acquisition datetime
            rows.append({
                "acq_dt": dt, "track_number": 18, "track_token": "18",
                "pass_id": d + 1, "acq_group_id_within_mgrs_tile": 1,
                "jpl_burst_id": f"T018-{idx:06d}-IW1", "opera_id": f"O{idx:04d}",
            })
            idx += 1
    return pd.DataFrame(rows)


def _run(tmp_path, *, dates, do_despeckle, features_glcm,
         tile_clip, generate_cog=False, tag=""):
    tile_dir = tmp_path / f"ws_{tag}"
    final_vv, prof_vv, final_vh, prof_vh = _batch(len(dates))
    df = _df(dates)
    with mock.patch.object(ws, "_write_scene_stac_item", lambda *a, **k: "x"):
        ws._write_scenes_output(
            "17MPU", "ASCENDING", tile_dir,
            final_vv, prof_vv, final_vh, prof_vh, dates, df,
            CRS, RES, generate_cog, False, 16, 16, 16, "ARDC",
            transform=MASTER_T, width=W, height=H,
            x_coords=np.arange(W), y_coords=np.arange(H),
            tile_clip=tile_clip,
            features_ratio=True, features_rvi=True, features_glcm=features_glcm,
            do_despeckle=do_despeckle, despeckle_method="tv_bregman",
            despeckle_kwargs={"reg_param": 5.0},
            despeckle_window=True, despeckle_pipeline=False,
        )
    store = list(tile_dir.glob("scenes_*/zarr/*.zarr"))[0]
    g = zarr.open_group(str(store), mode="r", zarr_format=3)
    out = {b: np.asarray(g[b][:]) for b in band_data_vars(g)}
    out["_time"] = np.asarray(g["time"][:])
    return out, tile_dir


ONE = [pd.Timestamp("2020-01-05T00:00:00Z")]
THREE = [pd.Timestamp(f"2020-01-{d:02d}T00:00:00Z") for d in (5, 17, 29)]

_BASE_BANDS = {"VV_dB", "VH_dB", "Ratio", "RVI"}


@pytest.mark.parametrize("do_despeckle", [False, True])
@pytest.mark.parametrize("features_glcm", [False, True])
@pytest.mark.parametrize("tile_clip", [False, True])
def test_writer_bands_and_invariants(tmp_path, do_despeckle, features_glcm, tile_clip):
    tg = f"{int(do_despeckle)}{int(features_glcm)}{int(tile_clip)}"
    out, _ = _run(tmp_path, dates=ONE, do_despeckle=do_despeckle,
                  features_glcm=features_glcm, tile_clip=tile_clip, tag=tg)
    bands = {b for b in out if b != "_time"}
    assert _BASE_BANDS <= bands
    assert (len(bands) > len(_BASE_BANDS)) == features_glcm  # GLCM bands only when on
    vv = out["VV_dB"]
    assert vv.shape == (1, H, W)
    # NaN margin (left 3 cols, burst-union edge) is preserved as NoData.
    assert np.isnan(vv[:, :, :3]).all()
    if tile_clip:
        # The synthetic master grid sits outside the real 17MPU MGRS polygon,
        # so the tile clip (applied last, support-before-clip) removes every
        # pixel — a NoData scene, written as all-NaN.
        assert np.isnan(vv).all()
    else:
        # Unclipped: real backscatter was written across the burst footprint.
        assert np.isfinite(vv).any()


def test_writer_multi_timestep_ordering(tmp_path):
    out, _ = _run(tmp_path, dates=THREE, do_despeckle=True,
                  features_glcm=True, tile_clip=False, tag="m")
    times = pd.to_datetime(out["_time"])
    assert len(times) == 3
    assert list(times) == sorted(times)  # ascending acquisition order
    assert out["VV_dB"].shape == (3, H, W)
    assert np.isfinite(out["VV_dB"]).any()


def test_writer_emits_cog(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    _, bdir = _run(tmp_path, dates=ONE, do_despeckle=False,
                   features_glcm=True, tile_clip=False, generate_cog=True, tag="c")
    cog = list(bdir.glob("scenes_*/cog/*.tif"))[0]
    with rasterio.open(cog) as r:
        # VV, VH, Ratio, RVI + the four GLCM metrics x two pols.
        assert r.count > len(_BASE_BANDS)
        assert r.crs.to_string() == CRS
        assert {"VV_dB", "VH_dB", "Ratio", "RVI"} <= set(r.descriptions)


def test_writer_skips_interior_hole(tmp_path):
    """A genuine interior NoData hole is skipped (no timestep, no record, and no
    empty store left on disk)."""
    dates = ONE
    final_vv, prof_vv, final_vh, prof_vh = _batch(1)
    # Punch an enclosed hole into every VV/VH burst (interior, away from edges).
    for a in final_vv:
        a[10:16, 30:40] = np.nan
    for a in final_vh:
        a[10:16, 30:40] = np.nan
    df = _df(dates)
    tile_dir = tmp_path / "hole"
    with mock.patch.object(ws, "_write_scene_stac_item", lambda *a, **k: "x"):
        recs = ws._write_scenes_output(
            "17MPU", "ASCENDING", tile_dir,
            final_vv, prof_vv, final_vh, prof_vh, dates, df,
            CRS, RES, False, False, 16, 16, 16, "ARDC",
            transform=MASTER_T, width=W, height=H,
            x_coords=np.arange(W), y_coords=np.arange(H),
            tile_clip=False, features_ratio=True, features_rvi=False,
            features_glcm=False, do_despeckle=False,
            incomplete_policy="skip", interior_hole_max_frac=0.001,
        )
    stores = list(tile_dir.glob("scenes_*/zarr/*.zarr"))
    # The enclosed hole (~2% of the grid) exceeds the 0.1% threshold -> skip;
    # a skipped-only track leaves NO store on disk and produces no records.
    assert len(recs) == 0
    assert stores == []
