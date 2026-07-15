"""Phase 2 — blockwise scenes writer parity.

The bounded-memory per-acquisition scenes path (per-block mosaic / dB / Ratio /
RVI / clip / Zarr write, halo-blockwise GLCM, and COG/preview streamed back
from the store) must be VALUE-IDENTICAL to the legacy full-frame writer. These
tests drive the REAL ``_write_scenes_output`` with real burst arrays + rasterio
profiles on the master grid (so the true windowed-mosaic path runs, not the
full-frame fallback) and compare the two writers band-for-band across the
feature matrix: despeckle off/on x GLCM off/on x tile_clip off/on, plus the
COG assets and the interior-hole QC skip decision.
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
    direct-copyable — the legacy reproject and the blockwise direct-copy then
    agree exactly. Covers master rows [row0, row1); interior NaNs excluded."""
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


def _run(tmp_path, *, dates, blockwise, do_despeckle, features_glcm,
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
            scenes_blockwise=blockwise,
        )
    store = list(tile_dir.glob("scenes_*/zarr/*.zarr"))[0]
    g = zarr.open_group(str(store), mode="r", zarr_format=3)
    out = {b: np.asarray(g[b][:]) for b in band_data_vars(g)}
    out["_time"] = np.asarray(g["time"][:])
    return out, tile_dir


ONE = [pd.Timestamp("2020-01-05T00:00:00Z")]
THREE = [pd.Timestamp(f"2020-01-{d:02d}T00:00:00Z") for d in (5, 17, 29)]


@pytest.mark.parametrize("do_despeckle", [False, True])
@pytest.mark.parametrize("features_glcm", [False, True])
@pytest.mark.parametrize("tile_clip", [False, True])
def test_blockwise_matches_legacy_bands(tmp_path, do_despeckle, features_glcm, tile_clip):
    tg = f"{int(do_despeckle)}{int(features_glcm)}{int(tile_clip)}"
    legacy, _ = _run(tmp_path, dates=ONE, blockwise=False, do_despeckle=do_despeckle,
                     features_glcm=features_glcm, tile_clip=tile_clip, tag=f"L{tg}")
    block, _ = _run(tmp_path, dates=ONE, blockwise=True, do_despeckle=do_despeckle,
                    features_glcm=features_glcm, tile_clip=tile_clip, tag=f"B{tg}")
    assert set(legacy) == set(block)
    for band in legacy:
        np.testing.assert_array_equal(legacy[band], block[band], err_msg=band)


def test_blockwise_matches_legacy_multi_timestep(tmp_path):
    legacy, _ = _run(tmp_path, dates=THREE, blockwise=False, do_despeckle=True,
                     features_glcm=True, tile_clip=True, tag="Lm")
    block, _ = _run(tmp_path, dates=THREE, blockwise=True, do_despeckle=True,
                    features_glcm=True, tile_clip=True, tag="Bm")
    assert legacy["_time"].tolist() == block["_time"].tolist()
    for band in legacy:
        np.testing.assert_array_equal(legacy[band], block[band], err_msg=band)


def test_blockwise_cog_matches_legacy(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    _, ldir = _run(tmp_path, dates=ONE, blockwise=False, do_despeckle=False,
                   features_glcm=True, tile_clip=True, generate_cog=True, tag="Lc")
    _, bdir = _run(tmp_path, dates=ONE, blockwise=True, do_despeckle=False,
                   features_glcm=True, tile_clip=True, generate_cog=True, tag="Bc")
    lcog = list(ldir.glob("scenes_*/cog/*.tif"))[0]
    bcog = list(bdir.glob("scenes_*/cog/*.tif"))[0]
    with rasterio.open(lcog) as lr, rasterio.open(bcog) as br:
        assert (lr.width, lr.height, lr.count) == (br.width, br.height, br.count)
        assert lr.transform == br.transform
        assert lr.descriptions == br.descriptions
        for b in range(1, lr.count + 1):
            np.testing.assert_array_equal(
                lr.read(b), br.read(b), err_msg=f"COG band {b}"
            )


def test_blockwise_skips_interior_hole_like_legacy(tmp_path):
    """A genuine interior NoData hole must be skipped by BOTH writers (no
    timestep, no catalog record)."""
    dates = ONE
    final_vv, prof_vv, final_vh, prof_vh = _batch(1)
    # Punch an enclosed hole into every VV burst (interior, away from edges).
    for a in final_vv:
        a[10:16, 30:40] = np.nan
    for a in final_vh:
        a[10:16, 30:40] = np.nan
    df = _df(dates)

    def _go(blockwise):
        tile_dir = tmp_path / f"hole_{int(blockwise)}"
        with mock.patch.object(ws, "_write_scene_stac_item", lambda *a, **k: "x"):
            recs = ws._write_scenes_output(
                "17MPU", "ASCENDING", tile_dir,
                [a.copy() for a in final_vv], prof_vv,
                [a.copy() for a in final_vh], prof_vh, dates, df,
                CRS, RES, False, False, 16, 16, 16, "ARDC",
                transform=MASTER_T, width=W, height=H,
                x_coords=np.arange(W), y_coords=np.arange(H),
                tile_clip=False, features_ratio=True, features_rvi=False,
                features_glcm=False, do_despeckle=False,
                incomplete_policy="skip", interior_hole_max_frac=0.001,
                scenes_blockwise=blockwise,
            )
        stores = list(tile_dir.glob("scenes_*/zarr/*.zarr"))
        if not stores:
            return len(recs), 0  # all scenes skipped -> no store created
        g = zarr.open_group(str(stores[0]), mode="r", zarr_format=3)
        return len(recs), int(g["time"].shape[0])

    # Both writers must make the SAME skip decision (parity), and here the
    # enclosed hole (~2% of the grid) exceeds the 0.1% threshold -> skip. A
    # skipped-only track leaves NO store on disk in both paths.
    assert _go(False) == _go(True) == (0, 0)
