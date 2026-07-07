"""Per-pixel n_obs valid-observation-count band in the smonthly store.

Every smonthly composite month must carry an ``n_obs`` uint8 band recording,
for each pixel, how many finite scene observations (from the track whose
composite filled that pixel) went into the month's composite value — 0 means
no data. These tests lock in:

* exact per-pixel counts on the blockwise path (single-track and the
  multi-track priority mosaic) and on the legacy full-array path;
* blockwise/legacy parity;
* uint8 dtype with fill_value 0;
* exclusion from the float32 COG export (Zarr-only analysis band).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from rasterio.transform import Affine

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

pytest.importorskip("rasterio")
zarr = pytest.importorskip("zarr")

from s1grits import workflow_scenes as ws  # noqa: E402
from s1grits.workflow_scenes import N_OBS_BAND  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixture data: two tracks, two scenes each, one month (2020-01).
# Track 1 = indices 0,1; Track 2 = indices 2,3.
# ---------------------------------------------------------------------------
CLEAN_DATES = [
    pd.Timestamp("2020-01-01T00:00:00Z"),
    pd.Timestamp("2020-01-13T00:00:00Z"),
    pd.Timestamp("2020-01-25T00:00:00Z"),
    pd.Timestamp("2020-01-31T00:00:00Z"),
]

VV_BY_IDX = {
    0: np.array(
        [[1.0, 1.0, np.nan, 4.0],
         [1.0, np.nan, np.nan, 4.0],
         [np.nan, 1.0, 1.0, np.nan]],
        dtype=np.float32,
    ),
    1: np.array(
        [[3.0, 1.0, np.nan, 2.0],
         [1.0, 1.0, np.nan, np.nan],
         [1.0, 1.0, np.nan, np.nan]],
        dtype=np.float32,
    ),
    2: np.array(
        [[2.0, 2.0, 2.0, np.nan],
         [2.0, np.nan, 2.0, 2.0],
         [np.nan, np.nan, 2.0, 2.0]],
        dtype=np.float32,
    ),
    3: np.array(
        [[2.0, 4.0, 2.0, np.nan],
         [2.0, 2.0, 2.0, np.nan],
         [np.nan, np.nan, np.nan, 2.0]],
        dtype=np.float32,
    ),
}

# Per-track finite counts (VV):
#   track1: [[2,2,0,2],[2,1,0,1],[1,2,1,0]]   -> 9 finite composite px
#   track2: [[2,2,2,0],[2,1,2,1],[0,0,1,2]]   -> 9 finite composite px
# Coverage ties at 9 px, so the deterministic tie-break (lower track id
# first) makes track 1 the priority-mosaic base; track 2 fills only the
# pixels where track 1 has no data: (0,2), (1,2), (2,3).
EXPECTED_N_OBS = np.array(
    [[2, 2, 2, 2],   # (0,2): track1 empty -> track2 count 2
     [2, 1, 2, 1],   # (1,2): track1 empty -> track2 count 2
     [1, 2, 1, 2]],  # (2,3): track1 empty -> track2 count 2
    dtype=np.uint8,
)

HEIGHT, WIDTH = 3, 4


def _df_batch(track_numbers):
    return pd.DataFrame(
        {
            "acq_dt": CLEAN_DATES[: len(track_numbers)],
            "track_number": track_numbers,
            "jpl_burst_id": [f"B{i:03d}" for i in range(len(track_numbers))],
            "opera_id": [f"O{i:03d}" for i in range(len(track_numbers))],
            "pass_id": [1] * len(track_numbers),
        }
    )


def _run_writer(tile_dir, *, track_numbers, spatial_filter_legacy):
    n = len(track_numbers)
    final_vv = [object() for _ in range(n)]
    final_vh = [object() for _ in range(n)]

    def fake_mosaic_align(indices, final_arr, *args, **kwargs):
        idx = int(indices[0])
        arr = VV_BY_IDX[idx].copy()
        if final_arr is final_vh:
            arr = arr * np.float32(0.5)
        return arr

    import unittest.mock as mock
    with mock.patch.object(ws, "_mosaic_align", fake_mosaic_align), \
         mock.patch.object(ws, "_write_monthly_stac_item",
                           lambda *a, **k: "noop"):
        records = ws._write_smonthly_one_track(
            "17MNU",
            "ASCENDING",
            tile_dir,
            final_vv=final_vv,
            prof_vv=[],
            final_vh=final_vh,
            prof_vh=[],
            clean_dates=CLEAN_DATES[:n],
            target_crs="EPSG:32617",
            target_res=30.0,
            generate_cog=False,
            generate_preview=False,
            chunk_y=2,
            chunk_x=2,
            cog_block=2,
            on_time_conflict="skip",
            monthly_cfg={"composite_method": "nanmedian"},
            processing_level="ARDC",
            transform=Affine.identity(),
            width=WIDTH,
            height=HEIGHT,
            x_coords=np.arange(WIDTH),
            y_coords=np.arange(HEIGHT),
            group_mode="acq_group",
            df_batch=_df_batch(track_numbers),
            tile_clip=False,
            track_token="1_2",
            n_bursts_track=2,
            restrict_to_group=True,
            valid_clean_indices=set(range(n)),
            spatial_filter_legacy=spatial_filter_legacy,
        )
    zarr_path = (
        tile_dir / "smonthly_ASCENDING" / "zarr"
        / "s1grits_smonthly_17MNU_ASCENDING_TK1_2.zarr"
    )
    return records, zarr.open_group(str(zarr_path), mode="r", zarr_format=3)


def test_n_obs_multitrack_blockwise_exact_counts(tmp_path):
    """Priority mosaic: each pixel carries the WINNING track's obs count."""
    records, g = _run_writer(
        tmp_path, track_numbers=[1, 1, 2, 2], spatial_filter_legacy=False
    )
    assert len(records) == 1
    assert N_OBS_BAND in g
    assert g[N_OBS_BAND].dtype == np.uint8
    assert g[N_OBS_BAND].fill_value == 0
    np.testing.assert_array_equal(g[N_OBS_BAND][0], EXPECTED_N_OBS)
    # n_obs is advertised in the catalog band list
    import json
    assert N_OBS_BAND in json.loads(records[0]["bands"])


def test_n_obs_multitrack_legacy_matches_blockwise(tmp_path):
    """The legacy full-array path writes identical n_obs to blockwise."""
    _, legacy = _run_writer(
        tmp_path / "legacy", track_numbers=[1, 1, 2, 2],
        spatial_filter_legacy=True,
    )
    _, blockwise = _run_writer(
        tmp_path / "blockwise", track_numbers=[1, 1, 2, 2],
        spatial_filter_legacy=False,
    )
    assert legacy[N_OBS_BAND].dtype == np.uint8
    np.testing.assert_array_equal(legacy[N_OBS_BAND][0], EXPECTED_N_OBS)
    np.testing.assert_array_equal(
        legacy[N_OBS_BAND][:], blockwise[N_OBS_BAND][:]
    )


def test_n_obs_single_track_counts_and_zero_fill(tmp_path):
    """Single-track month: n_obs = per-pixel finite count; 0 where no data."""
    records, g = _run_writer(
        tmp_path, track_numbers=[1, 1], spatial_filter_legacy=False
    )
    assert len(records) == 1
    expected = np.array(
        [[2, 2, 0, 2],
         [2, 1, 0, 1],
         [1, 2, 1, 0]],
        dtype=np.uint8,
    )
    np.testing.assert_array_equal(g[N_OBS_BAND][0], expected)
    # Composite is finite exactly where n_obs > 0 (nanmedian semantics)
    vv = g["VV_dB"][0]
    np.testing.assert_array_equal(np.isfinite(vv), expected > 0)


def test_n_obs_excluded_from_cog_export(tmp_path):
    """The COG keeps its radiometric band set; n_obs stays Zarr-only."""
    from s1grits.workflow_scenes import (
        _append_zarr_timestep,
        _generate_cog_preview_from_zarr,
        _init_zarr_2band,
    )
    import rasterio

    tile_dir = tmp_path / "17MPU"
    res, minx, maxy = 30.0, 500000.0, 8500000.0
    w, h = 40, 32
    transform = Affine(res, 0.0, minx, 0.0, -res, maxy)
    x = (minx + (np.arange(w) + 0.5) * res).astype("float64")
    y = (maxy - (np.arange(h) + 0.5) * res).astype("float64")
    bands = ["VV_dB", "VH_dB", N_OBS_BAND]
    zp = (tile_dir / "smonthly_ASCENDING" / "zarr"
          / "s1grits_smonthly_17MPU_ASCENDING_TK18.zarr")
    g = _init_zarr_2band(zp, x, y, "EPSG:32717", transform, 16, 16,
                         processing_level="monthly_ARDC", band_names=bands)
    dt = np.datetime64(pd.Timestamp("2026-01-15").to_datetime64(), "ns")
    _append_zarr_timestep(g, dt, [
        ("VV_dB", np.full((h, w), -12.0, np.float32)),
        ("VH_dB", np.full((h, w), -18.0, np.float32)),
        (N_OBS_BAND, np.full((h, w), 3, np.uint8)),
    ])
    assert g[N_OBS_BAND].dtype == np.uint8
    np.testing.assert_array_equal(g[N_OBS_BAND][0], np.full((h, w), 3))

    cog_rel, _ = _generate_cog_preview_from_zarr(
        zarr_path=zp, month_str="2026-01", tile_dir=tile_dir,
        direction_label="ASCENDING", mgrs_tile_id="17MPU", track_token="18",
        n_bursts_track=16, target_crs="EPSG:32717", tile_clip=False,
        generate_cog=True, generate_preview=False, cog_block=16,
        band_names=bands, product_label="smonthly_ASCENDING",
    )
    assert cog_rel is not None
    with rasterio.open(tile_dir / cog_rel) as src:
        assert src.count == 2  # VV_dB + VH_dB only, no n_obs
