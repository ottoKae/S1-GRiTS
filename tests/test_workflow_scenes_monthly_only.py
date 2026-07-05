from __future__ import annotations

import json

import numpy as np
import pandas as pd
import zarr
from rasterio.transform import Affine

from s1grits import workflow_scenes as ws


def test_valid_clean_indices_from_scene_records_maps_utc_times():
    clean_dates = [
        pd.Timestamp("2020-01-01T00:00:00Z"),
        pd.Timestamp("2020-01-13T00:00:00Z"),
        pd.Timestamp("2020-01-25T00:00:00Z"),
    ]
    scene_records = [
        {"datetime": pd.Timestamp("2020-01-13T00:00:00")},
        {"datetime": pd.Timestamp("2020-01-25T08:00:00+08:00")},
    ]

    assert ws._valid_clean_indices_from_scene_records(clean_dates, scene_records) == {1, 2}


def test_mosaic_align_window_reprojects_only_requested_block():
    transform = Affine.translation(100.0, 200.0) * Affine.scale(30.0, -30.0)
    src = (np.arange(16, dtype=np.float32).reshape(4, 4) + 1.0)
    prof = {"transform": transform, "crs": "EPSG:32617", "nodata": np.nan}

    block = ws._mosaic_align_window(
        [0],
        [src],
        [prof],
        height=4,
        width=4,
        transform=transform,
        target_crs="EPSG:32617",
        y_slice=slice(1, 3),
        x_slice=slice(1, 4),
    )

    assert np.allclose(block, src[1:3, 1:4])


def test_monthly_only_qc_filters_incomplete_acquisitions(monkeypatch):
    clean_dates = [
        pd.Timestamp("2020-01-01T00:00:00Z"),
        pd.Timestamp("2020-01-13T00:00:00Z"),
    ]
    df_batch = pd.DataFrame(
        {
            "pass_id": [1, 1],
            "acq_group_id_within_mgrs_tile": [1, 2],
            "acq_dt": clean_dates,
            "track_token": ["18", "18"],
            "jpl_burst_id": ["B001", "B002"],
        }
    )

    monkeypatch.setattr(
        ws,
        "_mosaic_align",
        lambda indices, *args, **kwargs: np.ones((2, 2), dtype=np.float32),
    )
    monkeypatch.setattr(ws, "_missing_interior_bursts", lambda *args, **kwargs: set())
    hole_fracs = iter([1.0, 0.0])
    monkeypatch.setattr(ws, "_interior_hole_fraction", lambda *args, **kwargs: next(hole_fracs))

    incomplete = []
    valid = ws._prepare_valid_clean_indices_for_monthly(
        "17MNU",
        "ASCENDING",
        final_vv=[object(), object()],
        prof_vv=[],
        final_vh=[object(), object()],
        prof_vh=[],
        clean_dates=clean_dates,
        df_rtc_ts=df_batch,
        target_crs="EPSG:32617",
        transform=Affine.identity(),
        width=2,
        height=2,
        tile_clip=False,
        track_footprint={"18": 1},
        track_footprint_ids={"18": {"B001", "B002"}},
        incomplete_policy="skip",
        incomplete_sink=incomplete,
        interior_hole_max_frac=0.5,
    )

    assert valid == {1}
    assert len(incomplete) == 1
    assert incomplete[0]["datetime"] == "20200101T000000"
    assert incomplete[0]["cause"] == "RASTER_INTERIOR_NODATA"
    assert "source raster or geometry gap" in incomplete[0]["recoverable"]


def test_monthly_only_qc_uses_vv_without_crosspol(monkeypatch):
    clean_dates = [pd.Timestamp("2020-01-01T00:00:00Z")]
    df_batch = pd.DataFrame(
        {
            "pass_id": [1],
            "acq_group_id_within_mgrs_tile": [1],
            "acq_dt": clean_dates,
            "track_token": ["18"],
            "jpl_burst_id": ["B001"],
        }
    )
    final_vv = [object()]

    def fake_mosaic(indices, final_arr, *args, **kwargs):
        assert final_arr is final_vv
        return np.ones((2, 2), dtype=np.float32)

    monkeypatch.setattr(ws, "_mosaic_align", fake_mosaic)
    monkeypatch.setattr(ws, "_missing_interior_bursts", lambda *args, **kwargs: set())
    monkeypatch.setattr(ws, "_interior_hole_fraction", lambda *args, **kwargs: 0.0)

    valid = ws._prepare_valid_clean_indices_for_monthly(
        "17MNU",
        "ASCENDING",
        final_vv=final_vv,
        prof_vv=[],
        final_vh=None,
        prof_vh=None,
        clean_dates=clean_dates,
        df_rtc_ts=df_batch,
        target_crs="EPSG:32617",
        transform=Affine.identity(),
        width=2,
        height=2,
        tile_clip=False,
        track_footprint={"18": 1},
        track_footprint_ids={"18": {"B001"}},
        incomplete_policy="skip",
    )

    assert valid == {0}


def test_metadata_prefilter_skips_interior_missing_before_download(monkeypatch):
    df_batch = pd.DataFrame(
        {
            "pass_id": [1, 1, 1],
            "acq_group_id_within_mgrs_tile": [1, 1, 2],
            "acq_dt": pd.to_datetime(
                [
                    "2020-01-01T00:00:00Z",
                    "2020-01-01T00:00:01Z",
                    "2020-01-13T00:00:00Z",
                ],
                utc=True,
            ),
            "track_token": ["18", "18", "18"],
            "jpl_burst_id": ["B001", "B003", "B001"],
        }
    )
    monkeypatch.setattr(
        ws,
        "_missing_interior_bursts",
        lambda footprint, present: {"B002"} if "B003" in present else set(),
    )

    incomplete = []
    filtered = ws._prefilter_metadata_incomplete_acquisitions(
        "17MNU",
        "ASCENDING",
        df_batch,
        track_footprint={"18": 3},
        track_footprint_ids={"18": {"B001", "B002", "B003"}},
        incomplete_policy="skip",
        incomplete_sink=incomplete,
    )

    assert len(filtered) == 1
    assert filtered["acq_group_id_within_mgrs_tile"].tolist() == [2]
    assert len(incomplete) == 1
    assert incomplete[0]["qc_stage"] == "metadata_prefilter"
    assert incomplete[0]["cause"] == "ASF_MISSING_STALE"


def test_smonthly_writer_uses_only_qc_passing_indices(tmp_path, monkeypatch):
    clean_dates = [
        pd.Timestamp("2020-01-01T00:00:00Z"),
        pd.Timestamp("2020-01-13T00:00:00Z"),
    ]
    df_batch = pd.DataFrame(
        {
            "acq_dt": clean_dates,
            "track_number": [18, 18],
            "jpl_burst_id": ["B001", "B002"],
            "opera_id": ["O001", "O002"],
            "pass_id": [1, 1],
        }
    )
    final_vv = [object(), object()]
    final_vh = [object(), object()]
    used_indices: list[int] = []

    def fake_mosaic_align(indices, final_arr, *args, **kwargs):
        idx = int(indices[0])
        used_indices.append(idx)
        value = float(idx + 1)
        if final_arr is final_vh:
            value *= 10.0
        return np.full((2, 2), value, dtype=np.float32)

    monkeypatch.setattr(ws, "_mosaic_align", fake_mosaic_align)
    monkeypatch.setattr(ws, "_write_monthly_stac_item", lambda *args, **kwargs: "noop")

    records = ws._write_smonthly_one_track(
        "17MNU",
        "ASCENDING",
        tmp_path,
        final_vv=final_vv,
        prof_vv=[],
        final_vh=final_vh,
        prof_vh=[],
        clean_dates=clean_dates,
        target_crs="EPSG:32617",
        target_res=30.0,
        generate_cog=False,
        generate_preview=False,
        chunk_y=2,
        chunk_x=2,
        cog_block=2,
        on_time_conflict="skip",
        monthly_cfg={"composite_method": "nanmean"},
        processing_level="ARDC",
        transform=Affine.identity(),
        width=2,
        height=2,
        x_coords=np.arange(2),
        y_coords=np.arange(2),
        group_mode="acq_group",
        df_batch=df_batch,
        tile_clip=False,
        track_token="18",
        n_bursts_track=2,
        restrict_to_group=True,
        valid_clean_indices={1},
    )

    assert used_indices
    assert set(used_indices) == {1}
    assert len(records) == 1
    assert records[0]["product_type"] == "smonthly"
    assert records[0]["month"] == "2020-01"
    assert records[0]["n_scenes"] == 1
    assert (tmp_path / "smonthly_ASCENDING" / "zarr").exists()


def test_smonthly_writer_removes_empty_zarr_when_no_qc_passing_acquisitions(
    tmp_path, monkeypatch
):
    clean_dates = [pd.Timestamp("2020-01-01T00:00:00Z")]
    df_batch = pd.DataFrame(
        {
            "acq_dt": clean_dates,
            "track_number": [18],
            "jpl_burst_id": ["B001"],
            "opera_id": ["O001"],
            "pass_id": [1],
        }
    )

    monkeypatch.setattr(ws, "_write_monthly_stac_item", lambda *args, **kwargs: "noop")

    records = ws._write_smonthly_one_track(
        "17MNU",
        "ASCENDING",
        tmp_path,
        final_vv=[object()],
        prof_vv=[],
        final_vh=[object()],
        prof_vh=[],
        clean_dates=clean_dates,
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
        width=2,
        height=2,
        x_coords=np.arange(2),
        y_coords=np.arange(2),
        group_mode="acq_group",
        df_batch=df_batch,
        tile_clip=False,
        track_token="18",
        n_bursts_track=2,
        restrict_to_group=True,
        valid_clean_indices=set(),
    )

    zarr_path = (
        tmp_path
        / "smonthly_ASCENDING"
        / "zarr"
        / "s1grits_smonthly_17MNU_ASCENDING_TK18_N02.zarr"
    )

    assert records == []
    assert not zarr_path.exists()


def test_smonthly_blockwise_uses_global_track_coverage_order(tmp_path, monkeypatch):
    clean_dates = [
        pd.Timestamp("2020-01-01T00:00:00Z"),
        pd.Timestamp("2020-01-13T00:00:00Z"),
    ]
    df_batch = pd.DataFrame(
        {
            "acq_dt": clean_dates,
            "track_number": [1, 2],
            "jpl_burst_id": ["B001", "B002"],
            "opera_id": ["O001", "O002"],
            "pass_id": [1, 1],
        }
    )
    final_vv = [object(), object()]
    final_vh = [object(), object()]
    vv_by_idx = {
        0: np.array(
            [[1.0, 1.0, np.nan, np.nan],
             [1.0, 1.0, np.nan, np.nan]],
            dtype=np.float32,
        ),
        1: np.array(
            [[2.0, np.nan, 2.0, 2.0],
             [np.nan, np.nan, 2.0, 2.0]],
            dtype=np.float32,
        ),
    }

    def fake_mosaic_align(indices, final_arr, *args, **kwargs):
        idx = int(indices[0])
        arr = vv_by_idx[idx].copy()
        if final_arr is final_vh:
            arr = arr * 10.0
        return arr

    monkeypatch.setattr(ws, "_mosaic_align", fake_mosaic_align)
    monkeypatch.setattr(ws, "_write_monthly_stac_item", lambda *args, **kwargs: "noop")

    records = ws._write_smonthly_one_track(
        "17MNU",
        "ASCENDING",
        tmp_path,
        final_vv=final_vv,
        prof_vv=[],
        final_vh=final_vh,
        prof_vh=[],
        clean_dates=clean_dates,
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
        width=4,
        height=2,
        x_coords=np.arange(4),
        y_coords=np.arange(2),
        group_mode="acq_group",
        df_batch=df_batch,
        tile_clip=False,
        track_token="1_2",
        n_bursts_track=2,
        restrict_to_group=True,
        valid_clean_indices={0, 1},
    )

    zarr_path = (
        tmp_path
        / "smonthly_ASCENDING"
        / "zarr"
        / "s1grits_smonthly_17MNU_ASCENDING_TK1_2_N02.zarr"
    )
    g = zarr.open_group(str(zarr_path), mode="r", zarr_format=3)
    vv = g["VV_dB"][0]

    assert len(records) == 1
    assert records[0]["primary_track"] == 2
    coverage = json.loads(records[0]["track_coverage_json"])
    assert coverage[0]["track"] == 2
    assert coverage[0]["valid_px"] == 5
    assert coverage[1]["track"] == 1
    assert coverage[1]["valid_px"] == 4
    assert np.isclose(vv[0, 0], 10.0 * np.log10(2.0))
    assert np.isclose(vv[0, 1], 0.0)
    assert np.isclose(vv[0, 2], 10.0 * np.log10(2.0))


def test_smonthly_blockwise_matches_legacy_small_array(tmp_path, monkeypatch):
    clean_dates = [
        pd.Timestamp("2020-01-01T00:00:00Z"),
        pd.Timestamp("2020-01-13T00:00:00Z"),
        pd.Timestamp("2020-01-25T00:00:00Z"),
        pd.Timestamp("2020-01-31T00:00:00Z"),
    ]
    df_batch = pd.DataFrame(
        {
            "acq_dt": clean_dates,
            "track_number": [1, 1, 2, 2],
            "jpl_burst_id": ["B001", "B002", "B003", "B004"],
            "opera_id": ["O001", "O002", "O003", "O004"],
            "pass_id": [1, 1, 1, 1],
        }
    )
    final_vv = [object() for _ in clean_dates]
    final_vh = [object() for _ in clean_dates]
    vv_by_idx = {
        0: np.array(
            [
                [1.0, 1.0, np.nan, 4.0, 4.0],
                [1.0, np.nan, np.nan, 4.0, 4.0],
                [1.0, 1.0, 1.0, np.nan, np.nan],
                [np.nan, 1.0, 1.0, np.nan, np.nan],
            ],
            dtype=np.float32,
        ),
        1: np.array(
            [
                [3.0, 1.0, np.nan, 2.0, 4.0],
                [1.0, 1.0, np.nan, 4.0, np.nan],
                [1.0, 2.0, 1.0, np.nan, np.nan],
                [1.0, 1.0, np.nan, np.nan, np.nan],
            ],
            dtype=np.float32,
        ),
        2: np.array(
            [
                [2.0, 2.0, 2.0, np.nan, np.nan],
                [2.0, np.nan, 2.0, 2.0, 2.0],
                [np.nan, np.nan, 2.0, 2.0, 2.0],
                [np.nan, np.nan, np.nan, 2.0, 2.0],
            ],
            dtype=np.float32,
        ),
        3: np.array(
            [
                [2.0, 4.0, 2.0, np.nan, np.nan],
                [2.0, 2.0, 2.0, 2.0, 2.0],
                [np.nan, np.nan, 3.0, 2.0, np.nan],
                [np.nan, np.nan, np.nan, 2.0, 2.0],
            ],
            dtype=np.float32,
        ),
    }
    vh_by_idx = {
        idx: (arr * np.float32(0.25 + idx * 0.05)).astype(np.float32)
        for idx, arr in vv_by_idx.items()
    }

    def fake_mosaic_align(indices, final_arr, *args, **kwargs):
        idx = int(indices[0])
        source = vh_by_idx if final_arr is final_vh else vv_by_idx
        return source[idx].copy()

    monkeypatch.setattr(ws, "_mosaic_align", fake_mosaic_align)
    monkeypatch.setattr(ws, "_write_monthly_stac_item", lambda *args, **kwargs: "noop")
    monkeypatch.setattr(ws, "_write_multiband_cog", lambda *args, **kwargs: None)

    def run_writer(tile_dir, *, generate_cog):
        records = ws._write_smonthly_one_track(
            "17MNU",
            "ASCENDING",
            tile_dir,
            final_vv=final_vv,
            prof_vv=[],
            final_vh=final_vh,
            prof_vh=[],
            clean_dates=clean_dates,
            target_crs="EPSG:32617",
            target_res=30.0,
            generate_cog=generate_cog,
            generate_preview=False,
            chunk_y=2,
            chunk_x=3,
            cog_block=2,
            on_time_conflict="skip",
            monthly_cfg={
                "composite_method": "nanmedian",
                "generate_cog": generate_cog,
                "generate_preview": False,
            },
            processing_level="ARDC",
            transform=Affine.identity(),
            width=5,
            height=4,
            x_coords=np.arange(5),
            y_coords=np.arange(4),
            group_mode="acq_group",
            df_batch=df_batch,
            tile_clip=False,
            features_ratio=True,
            features_rvi=True,
            track_token="1_2",
            n_bursts_track=2,
            restrict_to_group=True,
            valid_clean_indices=set(range(len(clean_dates))),
        )
        zarr_path = (
            tile_dir
            / "smonthly_ASCENDING_Ratio_RVI"
            / "zarr"
            / "s1grits_smonthly_17MNU_ASCENDING_TK1_2_N02.zarr"
        )
        return records, zarr.open_group(str(zarr_path), mode="r", zarr_format=3)

    legacy_records, legacy = run_writer(tmp_path / "legacy", generate_cog=True)
    blockwise_records, blockwise = run_writer(tmp_path / "blockwise", generate_cog=False)

    assert len(legacy_records) == 1
    assert len(blockwise_records) == 1
    assert legacy_records[0]["primary_track"] == blockwise_records[0]["primary_track"]
    assert legacy_records[0]["track_coverage_json"] == blockwise_records[0]["track_coverage_json"]
    np.testing.assert_array_equal(legacy["time"][:], blockwise["time"][:])
    for band_name in ["VV_dB", "VH_dB", "Ratio", "RVI"]:
        np.testing.assert_allclose(
            legacy[band_name][:],
            blockwise[band_name][:],
            rtol=1e-6,
            atol=1e-6,
            equal_nan=True,
        )
