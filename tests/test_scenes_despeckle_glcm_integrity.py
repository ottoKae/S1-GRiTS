"""Scenes-writer data integrity: despeckle/GLCM interplay, pipeline parity,
entropy zero-pair skip, and boundary-continuity guarantees.

Locks in the findings of the VV/VH-distortion audit:

1. GLCM computation must NEVER change the written VV/VH bands (no in-place
   mutation of its dB inputs) — with or without despeckle.
2. The despeckle pipeline (background prep of acquisition N+1) must be
   BIT-IDENTICAL to the serial path, including append order.
3. Despeckle must not distort bright scatterers: the legacy [1e-7, 1.0]
   linear clip flattened >0 dB targets by >7 dB.
4. Despeckle must not darken valid pixels at NaN boundaries: the legacy
   constant -23 dB padding bled ~-1.8 dB into every mosaic edge.
5. The GLCM entropy zero-pair skip is bit-identical to the exhaustive
   levels^2 loop.
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
from s1grits import asf_array_processing as ap  # noqa: E402
from s1grits.zarr_cf import band_data_vars  # noqa: E402

H, W = 40, 50
_rng = np.random.default_rng(42)


def _scene(seed_shift: float = 0.0) -> np.ndarray:
    """Synthetic linear gamma0: speckle around -12 dB, +6 dB bright block,
    NaN exterior frame (burst-union margin)."""
    rng = np.random.default_rng(42 + int(seed_shift * 100))
    a = rng.lognormal(np.log(10 ** (-12 / 10)), 0.5, (H, W)).astype(np.float32)
    a[10:16, 10:18] = 4.0  # +6.02 dB, above the legacy 1.0 linear clip
    a[:4, :] = np.nan; a[-4:, :] = np.nan
    a[:, :4] = np.nan; a[:, -4:] = np.nan
    return a


def _run_writer(tmp_path, *, dates, do_despeckle, features_glcm,
                despeckle_pipeline=True, tag=""):
    """Drive the REAL _write_scenes_output with mocked mosaics; return bands."""
    tile_dir = tmp_path / f"ws_{tag}"
    scenes = {i: _scene(i) for i in range(len(dates))}
    final_vv, final_vh = [object()] * len(dates), [object()] * len(dates)

    def fake_mosaic(indices, final_arr, *a, **k):
        arr = scenes[int(indices[0])]
        return (arr * 0.25 if final_arr is final_vh else arr).copy()

    df = pd.DataFrame({
        "acq_dt": dates,
        "track_number": [18] * len(dates), "track_token": ["18"] * len(dates),
        "pass_id": list(range(1, len(dates) + 1)),
        "acq_group_id_within_mgrs_tile": [1] * len(dates),
        "jpl_burst_id": [f"B{i:03d}" for i in range(len(dates))],
        "opera_id": [f"O{i:03d}" for i in range(len(dates))],
    })
    with mock.patch.object(ws, "_mosaic_align", fake_mosaic), \
         mock.patch.object(ws, "_write_scene_stac_item", lambda *a, **k: "x"):
        ws._write_scenes_output(
            "17MPU", "ASCENDING", tile_dir,
            final_vv, [{}] * len(dates), final_vh, [{}] * len(dates),
            dates, df,
            "EPSG:32717", 30.0, False, False, 16, 16, 16, "ARDC",
            transform=Affine(30.0, 0, 499980.0, 0, -30.0, 8499990.0),
            width=W, height=H,
            x_coords=np.arange(W), y_coords=np.arange(H),
            tile_clip=False,
            features_ratio=True, features_rvi=True, features_glcm=features_glcm,
            do_despeckle=do_despeckle, despeckle_method="tv_bregman",
            despeckle_kwargs={"reg_param": 5.0},
            despeckle_pipeline=despeckle_pipeline,
        )
    store = list(tile_dir.glob("scenes_*/zarr/*.zarr"))[0]
    g = zarr.open_group(str(store), mode="r", zarr_format=3)
    out = {b: np.asarray(g[b][:]) for b in band_data_vars(g)}
    out["time"] = np.asarray(g["time"][:])
    return out


ONE_DATE = [pd.Timestamp("2020-01-05T00:00:00Z")]
THREE_DATES = [pd.Timestamp(f"2020-01-{d:02d}T00:00:00Z") for d in (5, 17, 29)]


# ---------------------------------------------------------------------------
# 1. GLCM never touches VV/VH
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("do_despeckle", [False, True])
def test_glcm_does_not_change_written_vv_vh(tmp_path, do_despeckle):
    a = _run_writer(tmp_path, dates=ONE_DATE, do_despeckle=do_despeckle,
                    features_glcm=False, tag=f"noglcm{do_despeckle}")
    b = _run_writer(tmp_path, dates=ONE_DATE, do_despeckle=do_despeckle,
                    features_glcm=True, tag=f"glcm{do_despeckle}")
    for band in ("VV_dB", "VH_dB", "Ratio", "RVI"):
        np.testing.assert_array_equal(a[band], b[band], err_msg=band)


# ---------------------------------------------------------------------------
# 2. Despeckle pipeline parity (opt #7)
# ---------------------------------------------------------------------------

def test_despeckle_pipeline_is_bit_identical_to_serial(tmp_path):
    serial = _run_writer(tmp_path, dates=THREE_DATES, do_despeckle=True,
                         features_glcm=True, despeckle_pipeline=False,
                         tag="serial")
    piped = _run_writer(tmp_path, dates=THREE_DATES, do_despeckle=True,
                        features_glcm=True, despeckle_pipeline=True,
                        tag="piped")
    np.testing.assert_array_equal(serial["time"], piped["time"])  # append order
    for band in serial:
        np.testing.assert_array_equal(serial[band], piped[band], err_msg=band)


# ---------------------------------------------------------------------------
# 3 + 4. Despeckle value fidelity (bright targets, edge continuity)
# ---------------------------------------------------------------------------

def test_despeckle_preserves_bright_targets(tmp_path):
    raw = _run_writer(tmp_path, dates=ONE_DATE, do_despeckle=False,
                      features_glcm=False, tag="raw")
    dsp = _run_writer(tmp_path, dates=ONE_DATE, do_despeckle=True,
                      features_glcm=False, tag="dsp")
    block = dsp["VV_dB"][0][11:15, 11:17]  # interior of the +6.02 dB block
    # Legacy clip flattened this to <= -1.4 dB; smoothing alone keeps it
    # well above 0 dB.
    assert np.nanmin(block) > 0.0, f"bright block crushed: min={np.nanmin(block):.2f} dB"
    assert np.nanmax(dsp["VV_dB"]) > 2.0
    # NaN footprint identical
    np.testing.assert_array_equal(
        np.isnan(raw["VV_dB"]), np.isnan(dsp["VV_dB"])
    )


def test_despeckle_has_no_nan_boundary_darkening(tmp_path):
    raw = _run_writer(tmp_path, dates=ONE_DATE, do_despeckle=False,
                      features_glcm=False, tag="raw2")
    dsp = _run_writer(tmp_path, dates=ONE_DATE, do_despeckle=True,
                      features_glcm=False, tag="dsp2")
    delta = dsp["VV_dB"][0] - raw["VV_dB"][0]
    edge = np.concatenate([delta[4, 4:-4], delta[-5, 4:-4],
                           delta[4:-4, 4], delta[4:-4, -5]])
    inner = delta[8:-8, 8:-8][np.isfinite(delta[8:-8, 8:-8])]
    edge = edge[np.isfinite(edge)]
    # Legacy -23 dB padding biased edges ~-1.8 dB vs +0.6 interior. With
    # nearest-valid padding the edge behaves like the interior.
    assert abs(edge.mean() - inner.mean()) < 0.75, (
        f"edge bias {edge.mean():+.2f} dB vs interior {inner.mean():+.2f} dB"
    )
    assert edge.mean() > -0.5, f"edges darkened by {edge.mean():+.2f} dB"


def test_legacy_noise_floor_padding_still_available():
    a = _scene()
    legacy = ap.despeckle_2d(a, tv_kwargs={"reg_param": 5.0},
                             nan_pad_mode="noise_floor")
    modern = ap.despeckle_2d(a, tv_kwargs={"reg_param": 5.0})
    assert legacy.shape == modern.shape
    assert not np.array_equal(legacy, modern, equal_nan=True)


# ---------------------------------------------------------------------------
# 5. Entropy zero-pair skip is bit-identical (opt #6)
# ---------------------------------------------------------------------------

def _entropy_reference(q_arr, angle, window, levels, sentinel, distance):
    """The pre-optimisation exhaustive levels^2 loop, verbatim."""
    import cv2
    h, w = q_arr.shape
    kernel = np.ones((window, window), dtype=np.float32)
    dx = distance * np.cos(np.deg2rad(angle))
    dy = distance * np.sin(np.deg2rad(-angle))
    mat = np.array([[1.0, 0.0, -dx], [0.0, 1.0, -dy]], dtype=np.float32)
    q_shifted = cv2.warpAffine(
        q_arr.astype(np.float32), mat, (w, h),
        flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_REPLICATE,
    ).astype(np.int32)
    valid = ((q_arr != sentinel) & (q_shifted != sentinel)).astype(np.float32)
    N = cv2.filter2D(valid, -1, kernel, borderType=cv2.BORDER_REPLICATE).astype(np.float64)
    safe_N = np.where(N > 0, N, 1.0)
    nan_mask = N == 0
    acc = np.zeros((h, w), dtype=np.float64)
    for i in range(levels):
        row_i = (q_arr == i)
        for j in range(levels):
            mask = (row_i & (q_shifted == j)).astype(np.float32)
            cnt = cv2.filter2D(mask, -1, kernel,
                               borderType=cv2.BORDER_REPLICATE).astype(np.float64)
            nz = cnt > 0
            acc[nz] += cnt[nz] * np.log(cnt[nz])
    r = (np.log(safe_N) - acc / safe_N).astype(np.float32)
    r[nan_mask] = np.nan
    return r


@pytest.mark.parametrize("case", ["sparse", "dense", "with_nan", "uniform"])
def test_entropy_zero_pair_skip_bit_identical(case):
    levels, sentinel, window, distance = 16, 16, 5, 1
    rng = np.random.default_rng(7)
    if case == "sparse":       # few levels used -> many skipped pairs
        q = rng.choice([2, 3, 11], size=(30, 34)).astype(np.int32)
    elif case == "dense":      # all levels used
        q = rng.integers(0, levels, size=(30, 34)).astype(np.int32)
    elif case == "with_nan":   # sentinel pixels present
        q = rng.integers(0, levels, size=(30, 34)).astype(np.int32)
        q[rng.random((30, 34)) < 0.2] = sentinel
    else:                      # single level everywhere
        q = np.full((30, 34), 5, dtype=np.int32)

    for angle in (0.0, 90.0):
        got = ap._glcm_metrics_one_angle(
            q, angle, window, levels, sentinel, distance, ["entropy"]
        )["entropy"]
        ref = _entropy_reference(q, angle, window, levels, sentinel, distance)
        np.testing.assert_array_equal(got, ref, err_msg=f"{case}/angle={angle}")
