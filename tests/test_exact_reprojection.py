"""Nearest-neighbour mapping must not depend on GDAL's warp window size."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import zarr
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import Affine

from s1grits.asf_output_writing import _mosaic_align, _reproject_band_to_master
from s1grits.scenes import mosaic
from s1grits.scenes.scene_writer import _write_scenes_output
from s1grits.scenes.smonthly_writer import _write_smonthly_one_track


@pytest.fixture(scope="module")
def cross_zone_grid():
    source = (1 + np.arange(768 * 1280, dtype=np.float32).reshape(768, 1280)) / 1000
    source[:12] = np.nan
    source[220:260, 400:460] = np.nan
    st = Affine(30, 0, 102810, 0, -30, 9901800)
    profile = dict(transform=st, crs="EPSG:32718", nodata=np.nan, height=768, width=1280)
    cx, cy = Transformer.from_crs(32718, 32717, always_xy=True).transform(*(st * (640, 384)))
    target = Affine(30, 0, cx - 512 * 30 + 11, 0, -30, cy + 256 * 30 + 7)
    height, width = 512, 1024
    x, y = np.meshgrid(target.c + (np.arange(width) + .5) * 30,
                       target.f - (np.arange(height) + .5) * 30)
    sx, sy = Transformer.from_crs(32717, 32718, always_xy=True).transform(x, y)
    rows = np.floor((sy - st.f) / st.e).astype(int)
    cols = np.floor((sx - st.c) / st.a).astype(int)
    assert ((rows >= 0) & (rows < 768) & (cols >= 0) & (cols < 1280)).all()
    expected = source[rows, cols]
    return source, profile, target, expected


@pytest.mark.parametrize("path", ["prealigned", "scenes", "monthly", "full", "band"])
def test_cross_zone_nearest_matches_independent_pixel_lookup(cross_zone_grid, path):
    source, profile, target, expected = cross_zone_grid
    height, width = expected.shape
    if path == "band":
        actual = _reproject_band_to_master(source, profile["transform"], profile["crs"],
                                           target, "EPSG:32717", height, width, "nearest")
    elif path == "full":
        actual = _mosaic_align([0], [source], [profile], height, width, target, "EPSG:32717")
    else:
        arrays, profiles = [source], [profile]
        if path == "prealigned":
            arrays, profiles = mosaic._prealign_scenes_to_master_grid(
                arrays, profiles, target, "EPSG:32717", height, width)
        window = mosaic._mosaic_align_window if path == "monthly" else mosaic._mosaic_align_scene_window
        actual = np.full_like(expected, np.nan)
        for y in range(0, height, 128):
            for x in range(0, width, 256):
                ys, xs = slice(y, y + 128), slice(x, x + 256)
                block = window([0], arrays, profiles, height, width, target, "EPSG:32717", ys, xs)
                if block is not None:
                    actual[ys, xs] = block
    np.testing.assert_array_equal(actual, expected)


def test_aligned_nearest_keeps_source_array(cross_zone_grid):
    source, profile, _, _ = cross_zone_grid
    arrays, profiles = mosaic._prealign_scenes_to_master_grid(
        [source], [profile], profile["transform"], profile["crs"], *source.shape,
        resampling=Resampling.nearest)
    assert arrays[0] is source
    assert profiles[0] is profile


@pytest.mark.parametrize("product", ["scenes", "smonthly"])
def test_cross_zone_written_products_match_reference(tmp_path, cross_zone_grid, product):
    source, profile, target, expected = cross_zone_grid
    height, width = expected.shape
    date = pd.Timestamp("2026-01-07T23:29:10Z")
    frame = pd.DataFrame([dict(acq_dt=date, track_number=120, track_token="120", pass_id=1,
                               acq_group_id_within_mgrs_tile=1, jpl_burst_id="T120-257740-IW1",
                               opera_id="synthetic-cross-zone")])
    kwargs = dict(mgrs_tile_id="17MQV", direction_label="ASCENDING", tile_dir=tmp_path,
                  final_vv=[source], prof_vv=[profile], final_vh=[source * np.float32(.25)],
                  prof_vh=[profile], clean_dates=[date], target_crs="EPSG:32717", target_res=30,
                  generate_cog=False, generate_preview=False, chunk_y=128, chunk_x=256, cog_block=256,
                  processing_level="ARDC", transform=target, width=width, height=height,
                  x_coords=target.c+(np.arange(width)+.5)*30, y_coords=target.f-(np.arange(height)+.5)*30,
                  tile_clip=False)
    if product == "scenes":
        records = _write_scenes_output(**kwargs, df_rtc_ts=frame)
    else:
        records = _write_smonthly_one_track(**kwargs, df_batch=frame, track_token="120",
                                            on_time_conflict="skip", monthly_cfg={"composite_method":"median"})
    assert len(records) == 1
    store = next(tmp_path.glob(f"{product}_*/zarr/*.zarr"))
    output = zarr.open_group(str(store), mode="r")
    for band, factor in [("VV_dB", 1), ("VH_dB", .25)]:
        reference = (10 * np.log10(expected.astype(np.float64) * factor)).astype(np.float32)
        actual = np.asarray(output[band][0])
        np.testing.assert_array_equal(np.isfinite(actual), np.isfinite(reference))
        np.testing.assert_allclose(actual, reference, rtol=0, atol=1e-5, equal_nan=True)
    if product == "smonthly":
        np.testing.assert_array_equal(output["n_obs"][0], np.isfinite(expected).astype(np.uint8))
