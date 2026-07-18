"""Phase 3.2 — windowed burst reads (s1grits.lazy_burst).

Locks the LazyBurstArray contract: metadata mirrors a float32 band-1 decode;
windowed slicing is byte-identical to slicing the full decode (the value the
block readers rely on); full reads (__array__ / astype) match the whole band;
the toggle + cache gating keep it inert unless explicitly enabled with an
on-disk cache; and — the one that matters — END-TO-END writer parity: the
blockwise scenes store built from lazy GeoTIFF-backed bursts is byte-identical
to the one built from eager in-RAM arrays.
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

rasterio = pytest.importorskip("rasterio")
pytest.importorskip("cv2")
zarr = pytest.importorskip("zarr")

from s1grits import lazy_burst, burst_cache  # noqa: E402
from s1grits import workflow_scenes as ws  # noqa: E402
from s1grits.zarr_cf import band_data_vars  # noqa: E402

CRS = "EPSG:32717"
RES = 30.0
MASTER_T = Affine(RES, 0.0, 499980.0, 0.0, -RES, 8_500_000.0)


@pytest.fixture(autouse=True)
def _reset():
    lazy_burst.configure(False)
    burst_cache.configure(None)
    yield
    lazy_burst.configure(False)
    burst_cache.configure(None)


def _write_tif(path, arr, transform=MASTER_T, nodata=np.nan):
    h, w = arr.shape
    prof = {
        "driver": "GTiff", "height": h, "width": w, "count": 1,
        "dtype": "float32", "crs": CRS, "transform": transform, "nodata": nodata,
    }
    with rasterio.open(path, "w", **prof) as ds:
        ds.write(arr.astype(np.float32), 1)
    return prof


def _arr(seed, h=40, w=50):
    rng = np.random.default_rng(seed)
    a = rng.lognormal(np.log(10 ** (-12 / 10)), 0.5, (h, w)).astype(np.float32)
    a[3:7, 10:20] = 6.0
    a[:2, :] = np.nan          # NaN margin
    return a


# ---------------------------------------------------------------------------
# Core semantics
# ---------------------------------------------------------------------------

def test_metadata_and_full_read(tmp_path):
    a = _arr(1)
    p = tmp_path / "b.tif"
    _write_tif(p, a)
    lz = lazy_burst.LazyBurstArray(p, 1, a.shape)
    assert lz.shape == a.shape and lz.ndim == 2 and lz.dtype == np.float32
    np.testing.assert_array_equal(np.asarray(lz), a)      # NaN included
    np.testing.assert_array_equal(lz.astype(np.float32), a)


def test_windowed_slice_matches_full_slice(tmp_path):
    a = _arr(2)
    p = tmp_path / "b.tif"
    _write_tif(p, a)
    lz = lazy_burst.LazyBurstArray(p, 1, a.shape)
    for (r0, r1, c0, c1) in [(0, 10, 0, 12), (8, 24, 15, 40), (30, 40, 0, 50)]:
        np.testing.assert_array_equal(lz[r0:r1, c0:c1], a[r0:r1, c0:c1])
    # out-of-range stops clamp exactly like ndarray slicing
    np.testing.assert_array_equal(lz[35:100, 40:100], a[35:100, 40:100])
    # scalar row index collapses the axis, matching ndarray
    np.testing.assert_array_equal(lz[5, 0:10], a[5, 0:10])


def test_disabled_or_missing_cache_returns_none(tmp_path):
    a = _arr(3)
    p = tmp_path / "b.tif"
    prof = _write_tif(p, a)
    # toggle off -> None even with a cache
    burst_cache.configure(tmp_path / "cache")
    assert lazy_burst.maybe_lazy("http://x/burst.tif", prof) is None
    # toggle on but URL not in cache -> None (caller keeps eager path)
    lazy_burst.configure(True)
    assert lazy_burst.maybe_lazy("http://x/not-cached.tif", prof) is None


def test_maybe_lazy_reads_from_cached_geotiff(tmp_path):
    a = _arr(4)
    cache_dir = tmp_path / "cache"
    burst_cache.configure(cache_dir)
    lazy_burst.configure(True)
    url = "http://asf/OPERA_L2_RTC-S1_T018_VV.tif"
    # Put the GeoTIFF bytes into the cache the way the download path would.
    tif = tmp_path / "src.tif"
    prof = _write_tif(tif, a)
    burst_cache.put(url, tif.read_bytes())
    lz = lazy_burst.maybe_lazy(url, prof)
    assert isinstance(lz, lazy_burst.LazyBurstArray)
    np.testing.assert_array_equal(np.asarray(lz), a)
    np.testing.assert_array_equal(lz[5:15, 8:20], a[5:15, 8:20])


# ---------------------------------------------------------------------------
# End-to-end: blockwise scenes writer parity, lazy vs eager sources
# ---------------------------------------------------------------------------

def test_scenes_writer_parity_lazy_vs_eager(tmp_path):
    H, W = 48, 64
    dates = [pd.Timestamp("2020-01-05T00:00:00Z"),
             pd.Timestamp("2020-01-17T00:00:00Z")]

    # Two overlapping bursts per date on the master grid, VV and VH.
    def burst(r0, r1, seed, scale):
        rng = np.random.default_rng(seed)
        h = r1 - r0
        a = rng.lognormal(np.log(10 ** (-12 / 10)), 0.5, (h, W)).astype(np.float32)
        a[1:4, 6:16] = 5.0 * scale
        a[:, :3] = np.nan
        a *= scale
        prof = {
            "transform": MASTER_T * Affine.translation(0, r0), "crs": CRS,
            "nodata": np.nan, "width": W, "height": h,
        }
        return a, prof

    specs = []  # (arr, prof, pol) in burst order
    for d in range(len(dates)):
        for (r0, r1) in ((0, 28), (24, H)):
            specs.append(("vv", d, r0, r1))
            specs.append(("vh", d, r0, r1))

    def build_eager():
        final_vv, prof_vv, final_vh, prof_vh = [], [], [], []
        for d in range(len(dates)):
            for (r0, r1) in ((0, 28), (24, H)):
                avv, pvv = burst(r0, r1, 100 + d * 10 + r0, 1.0)
                avh, pvh = burst(r0, r1, 100 + d * 10 + r0, 0.25)
                final_vv.append(avv); prof_vv.append(pvv)
                final_vh.append(avh); prof_vh.append(pvh)
        return final_vv, prof_vv, final_vh, prof_vh

    def build_lazy(cache_dir):
        burst_cache.configure(cache_dir)
        lazy_burst.configure(True)
        final_vv, prof_vv, final_vh, prof_vh = [], [], [], []
        k = 0
        for d in range(len(dates)):
            for (r0, r1) in ((0, 28), (24, H)):
                avv, pvv = burst(r0, r1, 100 + d * 10 + r0, 1.0)
                avh, pvh = burst(r0, r1, 100 + d * 10 + r0, 0.25)
                for tag, a, prof, out, pout in (
                    ("vv", avv, pvv, final_vv, prof_vv),
                    ("vh", avh, pvh, final_vh, prof_vh),
                ):
                    url = f"http://asf/b{k}_{tag}.tif"; k += 1
                    tif = cache_dir / f"src_{k}_{tag}.tif"
                    _write_tif(tif, a, transform=prof["transform"])
                    burst_cache.put(url, tif.read_bytes())
                    lz = lazy_burst.maybe_lazy(url, prof)
                    assert isinstance(lz, lazy_burst.LazyBurstArray)
                    out.append(lz); pout.append(prof)
        return final_vv, prof_vv, final_vh, prof_vh

    df = pd.DataFrame([
        {"acq_dt": dates[d], "track_number": 18, "track_token": "18",
         "pass_id": d + 1, "acq_group_id_within_mgrs_tile": 1,
         "jpl_burst_id": f"T018-{i:06d}-IW1", "opera_id": f"O{i:04d}"}
        for d in range(len(dates)) for i in range(2)
    ])

    def run(final_vv, prof_vv, final_vh, prof_vh, tag):
        tile_dir = tmp_path / f"ws_{tag}"
        with mock.patch.object(ws, "_write_scene_stac_item", lambda *a, **k: "x"):
            ws._write_scenes_output(
                "17MPU", "ASCENDING", tile_dir,
                final_vv, prof_vv, final_vh, prof_vh, dates, df,
                CRS, RES, False, False, 16, 16, 16, "ARDC",
                transform=MASTER_T, width=W, height=H,
                x_coords=np.arange(W), y_coords=np.arange(H),
                tile_clip=True, features_ratio=True, features_rvi=True,
                features_glcm=True, do_despeckle=False,
            )
        store = list(tile_dir.glob("scenes_*/zarr/*.zarr"))[0]
        g = zarr.open_group(str(store), mode="r", zarr_format=3)
        return {b: np.asarray(g[b][:]) for b in band_data_vars(g)}

    eager = run(*build_eager(), tag="eager")
    lazy_burst.configure(False); burst_cache.configure(None)
    lazy = run(*build_lazy(tmp_path / "cache"), tag="lazy")

    assert set(eager) == set(lazy)
    for band in eager:
        np.testing.assert_array_equal(eager[band], lazy[band], err_msg=band)
