"""Reject empty monthly outputs before mutation; respect source nodata."""
import numpy as np
import pandas as pd
import pytest
from rasterio.transform import Affine
from shapely.geometry import box

from s1grits.scenes import mosaic, smonthly_writer
from test_provenance_monthly import _inputs, _snapshot, _store


@pytest.mark.parametrize("multitrack", [False, True])
@pytest.mark.parametrize("invalid", ["nan", "zero", "outside", "clip"])
def test_invalid_native_month_preserves_old_store_and_next_valid_write(tmp_path, monkeypatch, multitrack, invalid):
    kwargs, _, _ = _inputs(tmp_path)
    smonthly_writer._write_smonthly_one_track(**kwargs)
    before = _snapshot(tmp_path)
    for month in ("2025-01", "2025-02"):
        bad, _, _ = _inputs(tmp_path, month=month)
        bad["on_time_conflict"] = "overwrite"
        if multitrack:
            bad["df_batch"] = pd.DataFrame({
                "acq_dt": bad["clean_dates"], "track_number": [18, 19, 18],
                "jpl_burst_id": ["a", "b", "c"], "opera_id": ["a", "b", "c"], "pass_id": [1, 2, 3],
            })
        if invalid in {"nan", "zero"}:
            bad["final_vv"] = [np.full_like(a, np.nan if invalid == "nan" else 0) for a in bad["final_vv"]]
        elif invalid == "outside":
            bad["prof_vv"] = [{**p, "transform": p["transform"] * Affine.translation(100, 100)} for p in bad["prof_vv"]]
        else:
            bad["tile_clip"] = True
            monkeypatch.setattr(smonthly_writer, "_prepare_block_clip_geom", lambda enabled, *a: box(0, 0, 1, 1) if enabled else None)
        assert smonthly_writer._write_smonthly_one_track(**bad) == []
        assert _snapshot(tmp_path) == before
    valid, _, _ = _inputs(tmp_path, month="2025-02")
    assert len(smonthly_writer._write_smonthly_one_track(**valid)) == 1
    import zarr
    assert zarr.open_group(str(_store(tmp_path)), mode="r")["time"].shape == (2,)


@pytest.mark.parametrize("nodata", [None, np.nan, 0.0, -9999.0, 9999.0])
def test_direct_copy_and_nearest_warp_have_same_nodata_values(nodata):
    transform = Affine(30, 0, 500000, 0, -30, 8500000)
    src = np.full((3, 4), 2, dtype="f4")
    src[1, 1] = 5 if nodata is None else nodata
    profile = {"crs": "EPSG:32717", "transform": transform, "nodata": nodata}
    aligned = []
    for shift in (0, 0.25):
        prof = {**profile, "transform": transform * Affine.translation(shift, 0)}
        aligned.append(mosaic._mosaic_align_window(
            [0], [src], [prof], 3, 4, transform, "EPSG:32717", slice(0, 3), slice(0, 4)))
    expected = src.copy()
    if nodata is not None:
        expected[1, 1] = np.nan
    np.testing.assert_array_equal(aligned[0], expected)
    np.testing.assert_array_equal(aligned[1], expected)
    assert src[0, 0] == 2


def test_scene_direct_copy_preserves_real_zero_and_excludes_positive_nodata():
    transform = Affine(30, 0, 500000, 0, -30, 8500000)
    src = np.array([[0, 9999], [2, 4]], dtype="f4")
    profile = {"crs": "EPSG:32717", "transform": transform, "nodata": 9999}
    out = mosaic._mosaic_align_scene_window(
        [0], [src], [profile], 2, 2, transform, "EPSG:32717", slice(0, 2), slice(0, 2))
    np.testing.assert_array_equal(out, [[0, np.nan], [2, 4]])


@pytest.mark.parametrize("shift", [0, 0.25])
def test_positive_nodata_is_excluded_from_stored_values_and_count(tmp_path, shift):
    kwargs, _, _ = _inputs(tmp_path)
    for band in ("vv", "vh"):
        for arr in kwargs["final_" + band]:
            arr[0, 0] = 9999
        for profile in kwargs["prof_" + band]:
            profile.update(nodata=9999, transform=profile["transform"] * Affine.translation(shift, 0))
    assert len(smonthly_writer._write_smonthly_one_track(**kwargs)) == 1
    import zarr
    group = zarr.open_group(str(_store(tmp_path)), mode="r")
    assert np.isnan(group["VV_dB"][0, 0, 0])
    assert np.isnan(group["VH_dB"][0, 0, 0])
    assert group["n_obs"][0, 0, 0] == 0
    assert group["n_obs"][0, 0, 1] == 2
    np.testing.assert_allclose(group["VV_dB"][0, 0, 1], 10 * np.log10(2.5), rtol=1e-6)


def test_later_valid_block_with_missing_vh_still_writes(tmp_path):
    kwargs, _, _ = _inputs(tmp_path)
    for arr in kwargs["final_vv"]:
        arr[:, :2] = np.nan
    kwargs["final_vh"] = [None] * 3
    kwargs["prof_vh"] = [None] * 3
    assert len(smonthly_writer._write_smonthly_one_track(**kwargs)) == 1
    import zarr
    group = zarr.open_group(str(_store(tmp_path)), mode="r")
    assert np.isnan(group["VV_dB"][0, :, :2]).all()
    assert np.isfinite(group["VV_dB"][0, :, 2:]).all()
    assert np.isnan(group["VH_dB"][0]).all()
    np.testing.assert_array_equal(group["n_obs"][0], [[0, 0, 2, 2]] * 3)


def test_skipped_existing_month_does_not_read_new_arrays(tmp_path):
    kwargs, _, _ = _inputs(tmp_path)
    smonthly_writer._write_smonthly_one_track(**kwargs)
    before = _snapshot(tmp_path)

    class Unreadable:
        def __array__(self, *args, **kwargs):
            raise AssertionError("skip must not inspect unused input arrays")

        @property
        def shape(self):
            raise AssertionError("skip must not inspect unused input arrays")

    kwargs["final_vv"] = [Unreadable()] * 3
    kwargs["final_vh"] = [Unreadable()] * 3
    assert smonthly_writer._write_smonthly_one_track(**kwargs) == []
    assert _snapshot(tmp_path) == before
