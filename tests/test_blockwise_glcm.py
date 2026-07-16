"""Bit-exactness tests for blockwise GLCM (roadmap item 4).

The blockwise smonthly writer now fills GLCM texture bands via a
halo-composite-before-clip pass instead of forcing the legacy full-tile path.
These tests assert the GLCM bands written by the blockwise writer are
bit-identical to the legacy contract: compute GLCM on the full-tile UNCLIPPED
dB composite (fixed smonthly texture cfg), then apply the tile clip — for both
single-track and multi-track months, and unchanged (no GLCM) behaviour.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import zarr

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from s1grits import workflow_scenes as ws  # noqa: E402
from benchmarks import _synthetic as syn  # noqa: E402

pytest.importorskip("s1grits.asf_array_processing")

COPOL, CROSSPOL = "VV_dB", "VH_dB"


def _glcm_names():
    from s1grits.asf_array_processing import _get_texture_band_names
    return _get_texture_band_names(ws._smonthly_texture_cfg(COPOL, CROSSPOL))


def _legacy_reference_glcm(fv, pv, fh, ph, idx_by_track, master, height, width, crs):
    """Full-tile composite -> unclipped dB -> GLCM (legacy contract, no clip)."""
    from s1grits.asf_array_processing import compute_glcm_texture_bands
    # Per-track full-tile composite, VV-priority mosaic (== legacy)
    order = sorted(
        idx_by_track,
        key=lambda tk: sum(
            int(np.isfinite(ws._track_composite_block(
                idx_by_track[tk], fv, pv, height, width, master, crs,
                slice(0, height), slice(0, width), "median", 0.15)).sum())
            for _ in [0]
        ),
        reverse=True,
    )
    vv = np.full((height, width), np.nan, np.float32)
    vh = np.full((height, width), np.nan, np.float32)
    filled = np.zeros((height, width), bool)
    for tk in order:
        cvv = ws._track_composite_block(idx_by_track[tk], fv, pv, height, width,
                                        master, crs, slice(0, height), slice(0, width),
                                        "median", 0.15)
        cvh = ws._track_composite_block(idx_by_track[tk], fh, ph, height, width,
                                        master, crs, slice(0, height), slice(0, width),
                                        "median", 0.15)
        take = ~filled & np.isfinite(cvv)
        vv[take] = cvv[take]
        vh[take] = cvh[take]
        filled |= take
    vv_db = ws._linear_to_db(vv)
    vh_db = ws._linear_to_db(vh)
    tex_cfg = ws._smonthly_texture_cfg(COPOL, CROSSPOL)
    arrs, names = compute_glcm_texture_bands(vv_db, vh_db, tex_cfg)
    return dict(zip(names, arrs))


def _run_blockwise_with_glcm(tmp_path, tag, n_tracks, num_threads=1):
    height = width = 512
    chunk = 256
    fv, pv, fh, ph, idx_by_track, master = syn.build_month(
        height=height, width=width, n_scenes=30, n_tracks=n_tracks,
        scene_h=200, scene_w=400, seed=5,
    )
    crs = "EPSG:32717"
    glcm_names = _glcm_names()
    g = zarr.open_group(str(tmp_path / f"{tag}.zarr"), mode="w", zarr_format=3)
    g.create_array("x", data=np.arange(width, dtype=np.float64))
    g.create_array("y", data=np.arange(height, dtype=np.float64))
    g.create_array("time", shape=(0,), chunks=(64,), dtype="int64")
    for b in [COPOL, CROSSPOL] + glcm_names:
        g.create_array(b, shape=(0, height, width), chunks=(1, chunk, chunk),
                       dtype="float32", fill_value=np.nan)
    res = ws._write_smonthly_month_zarr_blockwise(
        g=g, month_str="2026-01", dt_ns=np.datetime64("2026-01-15", "ns"),
        idx_by_track=idx_by_track, final_vv=fv, prof_vv=pv, final_vh=fh, prof_vh=ph,
        height=height, width=width, transform=master, target_crs=crs,
        chunk_y=chunk, chunk_x=chunk,
        band_names=[COPOL, CROSSPOL], copol_name=COPOL, crosspol_name=CROSSPOL,
        features_ratio=False, features_rvi=False, ratio_name="Ratio", rvi_name="RVI",
        composite_method="median", trim_fraction=0.15,
        tile_clip=False, mgrs_tile_id="17MPU", num_threads=num_threads,
        glcm_band_names=glcm_names,
        texture_cfg=ws._smonthly_texture_cfg(COPOL, CROSSPOL),
    )
    assert res is not None
    ref = _legacy_reference_glcm(fv, pv, fh, ph, idx_by_track, master, height, width, crs)
    return g, glcm_names, ref


@pytest.mark.parametrize("n_tracks", [1, 2])
def test_blockwise_glcm_matches_full_tile(tmp_path, n_tracks):
    g, glcm_names, ref = _run_blockwise_with_glcm(tmp_path, f"t{n_tracks}", n_tracks)
    assert glcm_names, "no GLCM band names"
    for name in glcm_names:
        got = np.asarray(g[name][0])
        exp = ref[name]
        assert np.array_equal(
            np.nan_to_num(got, nan=-9e9), np.nan_to_num(exp, nan=-9e9)
        ), f"blockwise GLCM band {name} differs from full-tile reference"


def test_blockwise_glcm_thread_invariant(tmp_path):
    g1, names, _ = _run_blockwise_with_glcm(tmp_path, "th1", 2, num_threads=1)
    g4, _, _ = _run_blockwise_with_glcm(tmp_path, "th4", 2, num_threads=4)
    for name in names:
        a = np.nan_to_num(np.asarray(g1[name][0]), nan=-9e9)
        b = np.nan_to_num(np.asarray(g4[name][0]), nan=-9e9)
        assert np.array_equal(a, b), f"GLCM band {name} not thread-invariant"


def test_no_glcm_names_leaves_core_only(tmp_path):
    # Sanity: without glcm_band_names the writer behaves exactly as before.
    height = width = 384
    chunk = 192
    fv, pv, fh, ph, idx_by_track, master = syn.build_month(
        height=height, width=width, n_scenes=20, n_tracks=2,
        scene_h=160, scene_w=300, seed=6,
    )
    g = zarr.open_group(str(tmp_path / "core.zarr"), mode="w", zarr_format=3)
    g.create_array("x", data=np.arange(width, dtype=np.float64))
    g.create_array("y", data=np.arange(height, dtype=np.float64))
    g.create_array("time", shape=(0,), chunks=(64,), dtype="int64")
    for b in (COPOL, CROSSPOL):
        g.create_array(b, shape=(0, height, width), chunks=(1, chunk, chunk),
                       dtype="float32", fill_value=np.nan)
    res = ws._write_smonthly_month_zarr_blockwise(
        g=g, month_str="2026-01", dt_ns=np.datetime64("2026-01-15", "ns"),
        idx_by_track=idx_by_track, final_vv=fv, prof_vv=pv, final_vh=fh, prof_vh=ph,
        height=height, width=width, transform=master, target_crs="EPSG:32717",
        chunk_y=chunk, chunk_x=chunk,
        band_names=[COPOL, CROSSPOL], copol_name=COPOL, crosspol_name=CROSSPOL,
        features_ratio=False, features_rvi=False, ratio_name="Ratio", rvi_name="RVI",
        composite_method="median", trim_fraction=0.15,
        tile_clip=False, mgrs_tile_id="17MPU", num_threads=1,
    )
    assert res is not None
    assert g[COPOL].shape[0] == 1
