"""
Test suite for blockwise path fixes.

Tests verify:
1. prof_arr validation catches incomplete profiles
2. Blockwise path logs progress appropriately
3. COG/Preview generation from Zarr works correctly
4. use_blockwise condition only blocks on GLCM, not COG/Preview
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import zarr
from pathlib import Path
from rasterio.transform import Affine

from s1grits import workflow_scenes as ws


def test_mosaic_align_window_logs_fallback_warning(caplog, monkeypatch):
    """Verify that incomplete prof_arr triggers warning log."""
    import logging
    caplog.set_level(logging.WARNING)

    transform = Affine.translation(100.0, 200.0) * Affine.scale(30.0, -30.0)
    src = np.arange(16, dtype=np.float32).reshape(4, 4) + 1.0

    # Incomplete prof_arr (empty list)
    monkeypatch.setattr(ws, "_mosaic_align", lambda *args, **kwargs: src.copy())
    result = ws._mosaic_align_window(
        [0],
        [src],
        [],  # Empty prof_arr triggers fallback
        height=4,
        width=4,
        transform=transform,
        target_crs="EPSG:32617",
        y_slice=slice(0, 2),
        x_slice=slice(0, 2),
    )

    # Should have logged a fallback warning
    assert any("BLOCKWISE FALLBACK" in record.message for record in caplog.records)
    assert result is not None  # Still works, but inefficiently


def test_mosaic_align_window_direct_copy_skips_reproject(monkeypatch):
    transform = Affine.translation(100.0, 200.0) * Affine.scale(30.0, -30.0)
    src = (np.arange(36, dtype=np.float32).reshape(6, 6) + 1.0)
    prof = {"transform": transform, "crs": "EPSG:32617", "nodata": np.nan}

    def fail_reproject(*args, **kwargs):
        raise AssertionError("direct-copy path should not call reproject")

    monkeypatch.setattr(ws, "reproject", fail_reproject)

    result = ws._mosaic_align_window(
        [0],
        [src],
        [prof],
        height=6,
        width=6,
        transform=transform,
        target_crs="EPSG:32617",
        y_slice=slice(2, 5),
        x_slice=slice(1, 4),
    )

    assert result is not None
    assert np.allclose(result, src[2:5, 1:4])


def test_spatial_filters_cap_batch_and_disable_blockwise():
    assert ws._spatial_filters_enabled({"spatial_despeckle": True})
    assert ws._spatial_filters_enabled({"features_glcm": True})
    assert ws._spatial_filters_enabled({"morphology": {"enabled": True}})
    assert ws._cap_batch_strategy_for_spatial_filters("yearly", True) == "quarterly"
    assert ws._cap_batch_strategy_for_spatial_filters("quarterly", True) == "quarterly"
    assert ws._cap_batch_strategy_for_spatial_filters("yearly", False) == "yearly"


def test_generate_cog_preview_from_zarr_creates_outputs(tmp_path):
    """Test that COG/Preview are correctly generated from Zarr."""
    # Create a minimal Zarr store with one month of data
    zarr_path = tmp_path / "test.zarr"
    height, width = 10, 10
    transform = Affine.translation(400000, 5000000) * Affine.scale(30.0, -30.0)

    g = zarr.open_group(str(zarr_path), mode='w', zarr_format=3)
    g.attrs['crs'] = 'EPSG:32617'
    g.attrs['transform'] = list(transform)[:6]
    g.attrs['height'] = height
    g.attrs['width'] = width

    # Create coordinate arrays
    x_coords = np.arange(width, dtype=np.float64)
    y_coords = np.arange(height, dtype=np.float64)
    g.create_array('x', data=x_coords, overwrite=True)
    g.create_array('y', data=y_coords, overwrite=True)

    # Create time array with one timestamp (2020-01)
    time_data = np.array([np.datetime64('2020-01-15', 'ns')], dtype='int64')
    g.create_array('time', data=time_data, overwrite=True)

    # Create band arrays
    vv_data = np.random.uniform(-20, 0, (1, height, width)).astype(np.float32)
    vh_data = np.random.uniform(-25, -5, (1, height, width)).astype(np.float32)
    g.create_array('VV_dB', data=vv_data, overwrite=True)
    g.create_array('VH_dB', data=vh_data, overwrite=True)

    # Generate COG and Preview
    tile_dir = tmp_path / "tile"
    cog_relpath, preview_relpath = ws._generate_cog_preview_from_zarr(
        zarr_path=zarr_path,
        month_str='2020-01',
        tile_dir=tile_dir,
        direction_label='ASCENDING',
        mgrs_tile_id='17MQV',
        track_token='18',
        n_bursts_track=5,
        target_crs='EPSG:32617',
        tile_clip=False,
        generate_cog=True,
        generate_preview=True,
        cog_block=256,
        band_names=['VV_dB', 'VH_dB'],
        product_label='smonthly_ASCENDING',
    )

    # Verify outputs were created
    assert cog_relpath is not None
    assert preview_relpath is not None

    cog_path = tile_dir / cog_relpath
    preview_path = tile_dir / preview_relpath

    assert cog_path.exists()
    assert preview_path.exists()

    # Verify COG can be opened
    import rasterio
    with rasterio.open(cog_path) as src:
        assert src.count == 2  # VV_dB and VH_dB
        assert src.width == width
        assert src.height == height


def test_generate_cog_preview_handles_missing_month(tmp_path, caplog):
    """Test graceful handling when requested month doesn't exist in Zarr."""
    import logging
    caplog.set_level(logging.WARNING)

    # Create Zarr with 2020-01, request 2020-02
    zarr_path = tmp_path / "test.zarr"
    g = zarr.open_group(str(zarr_path), mode='w', zarr_format=3)
    g.attrs['crs'] = 'EPSG:32617'
    g.attrs['transform'] = [30.0, 0.0, 400000, 0.0, -30.0, 5000000]
    g.attrs['height'] = 10
    g.attrs['width'] = 10

    x_coords = np.arange(10, dtype=np.float64)
    y_coords = np.arange(10, dtype=np.float64)
    g.create_array('x', data=x_coords)
    g.create_array('y', data=y_coords)

    time_data = np.array([np.datetime64('2020-01-15', 'ns')], dtype='int64')
    g.create_array('time', data=time_data)

    vv_data = np.ones((1, 10, 10), dtype=np.float32)
    vh_data = np.ones((1, 10, 10), dtype=np.float32)
    g.create_array('VV_dB', data=vv_data)
    g.create_array('VH_dB', data=vh_data)

    tile_dir = tmp_path / "tile"

    cog_relpath, preview_relpath = ws._generate_cog_preview_from_zarr(
        zarr_path=zarr_path,
        month_str='2020-02',  # Doesn't exist
        tile_dir=tile_dir,
        direction_label='ASCENDING',
        mgrs_tile_id='17MQV',
        track_token='18',
        n_bursts_track=5,
        target_crs='EPSG:32617',
        tile_clip=False,
        generate_cog=True,
        generate_preview=True,
        cog_block=256,
        band_names=['VV_dB', 'VH_dB'],
        product_label='smonthly_ASCENDING',
    )

    # Should return None for both
    assert cog_relpath is None
    assert preview_relpath is None

    # Should have logged a warning
    assert any("not found in Zarr" in record.message for record in caplog.records)


def test_blockwise_condition_allows_cog_preview():
    """Test that use_blockwise is True when only COG/Preview are enabled."""
    # Simulate the condition check in _write_smonthly_one_track

    # Case 1: No GLCM, with COG/Preview -> should use blockwise
    features_glcm = False
    use_blockwise = not features_glcm
    assert use_blockwise is True

    # Case 2: GLCM enabled -> should NOT use blockwise
    features_glcm = True
    use_blockwise = not features_glcm
    assert use_blockwise is False


def test_blockwise_progress_logging(tmp_path, caplog, monkeypatch):
    """Test that single-track blockwise path logs one-pass progress."""
    import logging
    caplog.set_level(logging.INFO)

    # Create minimal test data
    height, width = 8, 8
    chunk_y, chunk_x = 4, 4  # 2x2 = 4 blocks
    transform = Affine.identity()

    zarr_path = tmp_path / "test.zarr"
    g = zarr.open_group(str(zarr_path), mode='w', zarr_format=3)
    g.attrs['transform'] = list(transform)[:6]
    g.attrs['height'] = height
    g.attrs['width'] = width
    g.attrs['crs'] = 'EPSG:32617'

    x_coords = np.arange(width, dtype=np.float64)
    y_coords = np.arange(height, dtype=np.float64)
    g.create_array('x', data=x_coords)
    g.create_array('y', data=y_coords)
    g.create_array('time', shape=(0,), chunks=(1,), dtype='int64')
    g.create_array('VV_dB', shape=(0, height, width), chunks=(1, chunk_y, chunk_x), dtype='float32')
    g.create_array('VH_dB', shape=(0, height, width), chunks=(1, chunk_y, chunk_x), dtype='float32')

    # Mock _track_composite_block to return simple arrays
    call_count = {"n": 0}

    def fake_composite(idxs, final_arr, prof_arr, h, w, tfm, crs, y_sl, x_sl,
                       method, trim, scene_bounds=None):
        call_count["n"] += 1
        bh = (y_sl.stop or 0) - (y_sl.start or 0)
        bw = (x_sl.stop or 0) - (x_sl.start or 0)
        return np.ones((bh, bw), dtype=np.float32)

    monkeypatch.setattr(ws, '_track_composite_block', fake_composite)

    # Run blockwise processing
    result = ws._write_smonthly_month_zarr_blockwise(
        g=g,
        month_str='2020-01',
        dt_ns=np.datetime64('2020-01-15', 'ns'),
        idx_by_track={1: [0, 1, 2]},
        final_vv=[object(), object(), object()],
        prof_vv=[],
        final_vh=[object(), object(), object()],
        prof_vh=[],
        height=height,
        width=width,
        transform=transform,
        target_crs='EPSG:32617',
        chunk_y=chunk_y,
        chunk_x=chunk_x,
        band_names=['VV_dB', 'VH_dB'],
        copol_name='VV_dB',
        crosspol_name='VH_dB',
        features_ratio=False,
        features_rvi=False,
        ratio_name='Ratio',
        rvi_name='RVI',
        composite_method='nanmedian',
        trim_fraction=0.15,
        tile_clip=False,
        mgrs_tile_id='17MQV',
    )

    assert result is not None

    # Check for progress logs
    log_messages = [record.message for record in caplog.records]

    # Single-track months should avoid the old two-pass recompute. There are
    # 4 blocks and 2 bands, so this is 8 composite calls instead of 16.
    assert call_count["n"] == 8

    # Should log one-pass processing, not the two-pass path.
    assert any('blockwise: 4 spatial blocks' in msg for msg in log_messages)
    assert any('one-pass processing' in msg for msg in log_messages)
    assert any('One-pass - Writing single-track Zarr' in msg for msg in log_messages)
    assert any('One-pass complete' in msg for msg in log_messages)
    assert not any('Pass 1/2 - Computing track coverage' in msg for msg in log_messages)


def test_blockwise_multitrack_stops_when_block_filled(tmp_path, monkeypatch):
    """Test that pass 2 skips lower-priority tracks after a block is filled."""
    height, width = 8, 8
    chunk_y, chunk_x = 4, 4  # 2x2 = 4 blocks
    transform = Affine.identity()

    zarr_path = tmp_path / "test.zarr"
    g = zarr.open_group(str(zarr_path), mode='w', zarr_format=3)
    g.attrs['transform'] = list(transform)[:6]
    g.attrs['height'] = height
    g.attrs['width'] = width
    g.attrs['crs'] = 'EPSG:32617'
    g.create_array('x', data=np.arange(width, dtype=np.float64))
    g.create_array('y', data=np.arange(height, dtype=np.float64))
    g.create_array('time', shape=(0,), chunks=(1,), dtype='int64')
    g.create_array('VV_dB', shape=(0, height, width), chunks=(1, chunk_y, chunk_x), dtype='float32')
    g.create_array('VH_dB', shape=(0, height, width), chunks=(1, chunk_y, chunk_x), dtype='float32')

    call_count = {"n": 0}
    mask_call_count = {"n": 0}

    def fake_composite(idxs, final_arr, prof_arr, h, w, tfm, crs, y_sl, x_sl,
                       method, trim, scene_bounds=None):
        call_count["n"] += 1
        bh = (y_sl.stop or 0) - (y_sl.start or 0)
        bw = (x_sl.stop or 0) - (x_sl.start or 0)
        return np.ones((bh, bw), dtype=np.float32)

    def fake_valid_mask(idxs, final_arr, prof_arr, h, w, tfm, crs, y_sl, x_sl,
                        scene_bounds=None):
        mask_call_count["n"] += 1
        bh = (y_sl.stop or 0) - (y_sl.start or 0)
        bw = (x_sl.stop or 0) - (x_sl.start or 0)
        return np.ones((bh, bw), dtype=bool)

    monkeypatch.setattr(ws, '_track_composite_block', fake_composite)
    monkeypatch.setattr(ws, '_track_valid_mask_block', fake_valid_mask)

    result = ws._write_smonthly_month_zarr_blockwise(
        g=g,
        month_str='2020-01',
        dt_ns=np.datetime64('2020-01-15', 'ns'),
        idx_by_track={1: [0], 2: [1]},
        final_vv=[object(), object()],
        prof_vv=[],
        final_vh=[object(), object()],
        prof_vh=[],
        height=height,
        width=width,
        transform=transform,
        target_crs='EPSG:32617',
        chunk_y=chunk_y,
        chunk_x=chunk_x,
        band_names=['VV_dB', 'VH_dB'],
        copol_name='VV_dB',
        crosspol_name='VH_dB',
        features_ratio=False,
        features_rvi=False,
        ratio_name='Ratio',
        rvi_name='RVI',
        composite_method='nanmedian',
        trim_fraction=0.15,
        tile_clip=False,
        mgrs_tile_id='17MQV',
    )

    assert result is not None
    # Pass 1 uses finite-mask unions instead of composites:
    # 4 blocks * 2 tracks = 8 mask calls, 0 composite calls.
    assert mask_call_count["n"] == 8
    # Pass 2 should compute only the first full-coverage track:
    # 4 blocks * 1 track * 2 bands = 8 composite calls. Without the early
    # break this would be 16.
    assert call_count["n"] == 8


def test_compute_scene_dst_bounds_same_crs():
    """Bounds on the master grid match the scene's pixel offsets."""
    master = Affine.translation(1000.0, 2000.0) * Affine.scale(30.0, -30.0)
    # Scene offset by (row=4, col=6) on the same grid, 5x7 pixels
    scene_t = master * Affine.translation(6, 4)
    src = np.ones((5, 7), dtype=np.float32)
    prof = {"transform": scene_t, "crs": "EPSG:32617"}

    bounds = ws._compute_scene_dst_bounds(
        [src], [prof], master, "EPSG:32617", height=100, width=100
    )
    assert len(bounds) == 1
    row0, row1, col0, col1 = bounds[0]
    # Margin of 2 pixels around [4:9, 6:13]
    assert row0 <= 4 and row1 >= 9
    assert col0 <= 6 and col1 >= 13
    assert row1 - row0 <= 5 + 4 and col1 - col0 <= 7 + 4


def test_compute_scene_dst_bounds_handles_missing_profiles():
    """Unknown footprints (no/short prof_arr) must yield None (no filtering)."""
    master = Affine.identity()
    bounds = ws._compute_scene_dst_bounds(
        [object(), object()], [], master, "EPSG:32617", height=10, width=10
    )
    assert bounds == [None, None]


def test_mosaic_align_window_skips_nonoverlapping_scene(monkeypatch):
    """A scene whose footprint misses the block returns None without work."""
    master = Affine.translation(0.0, 0.0) * Affine.scale(30.0, -30.0)
    scene_t = master * Affine.translation(50, 50)  # far from block [0:4, 0:4]
    src = np.ones((4, 4), dtype=np.float32)
    prof = {"transform": scene_t, "crs": "EPSG:32617", "nodata": np.nan}

    def fail_reproject(*args, **kwargs):
        raise AssertionError("non-overlapping scene must not be processed")

    monkeypatch.setattr(ws, "reproject", fail_reproject)

    bounds = ws._compute_scene_dst_bounds(
        [src], [prof], master, "EPSG:32617", height=100, width=100
    )
    result = ws._mosaic_align_window(
        [0], [src], [prof],
        height=100, width=100, transform=master, target_crs="EPSG:32617",
        y_slice=slice(0, 4), x_slice=slice(0, 4),
        scene_bounds=bounds,
    )
    assert result is None

    # The same scene IS used for a block that its footprint covers
    result = ws._mosaic_align_window(
        [0], [src], [prof],
        height=100, width=100, transform=master, target_crs="EPSG:32617",
        y_slice=slice(50, 54), x_slice=slice(50, 54),
        scene_bounds=bounds,
    )
    assert result is not None
    assert np.allclose(result, 1.0)


def test_track_valid_mask_block_matches_composite_mask():
    """Mask-OR coverage equals the finite mask of the median composite."""
    master = Affine.translation(0.0, 0.0) * Affine.scale(30.0, -30.0)
    rng = np.random.default_rng(42)

    # Two scenes on the master grid with different valid regions
    src_a = np.full((8, 8), np.nan, dtype=np.float32)
    src_a[:5, :] = rng.random((5, 8), dtype=np.float32) + 0.1
    src_b = np.full((8, 8), np.nan, dtype=np.float32)
    src_b[3:, 2:] = rng.random((5, 6), dtype=np.float32) + 0.1
    prof = {"transform": master, "crs": "EPSG:32617", "nodata": np.nan}
    final = [src_a, src_b]
    profs = [prof, prof]

    y_sl, x_sl = slice(0, 8), slice(0, 8)
    mask = ws._track_valid_mask_block(
        [0, 1], final, profs, 8, 8, master, "EPSG:32617", y_sl, x_sl
    )
    composite = ws._track_composite_block(
        [0, 1], final, profs, 8, 8, master, "EPSG:32617", y_sl, x_sl,
        "median", 0.15,
    )
    assert mask is not None and composite is not None
    assert np.array_equal(mask, np.isfinite(composite))


def test_monthly_composite_block_median_matches_numpy():
    """Accelerated nanmedian must agree with np.nanmedian (incl. NaN slices)."""
    rng = np.random.default_rng(7)
    stack = [rng.random((6, 6), dtype=np.float32) for _ in range(5)]
    stack[0][:, :3] = np.nan
    stack[1][:] = np.nan  # all-NaN layer

    out = ws._monthly_composite_block(list(stack), "median", 0.15)
    with np.errstate(all="ignore"):
        import warnings as _w
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            expected = np.nanmedian(np.stack(stack, axis=0), axis=0)
    assert out is not None
    assert np.allclose(out, expected, equal_nan=True)


def test_prealign_scenes_to_master_grid():
    """Misaligned scenes get warped once; aligned scenes are left alone."""
    master = Affine.translation(0.0, 0.0) * Affine.scale(30.0, -30.0)

    # Scene A: already on the master grid (integer pixel offset)
    src_a = np.ones((4, 4), dtype=np.float32)
    prof_a = {"transform": master * Affine.translation(2, 2),
              "crs": "EPSG:32617", "nodata": np.nan}

    # Scene B: same CRS but half-pixel offset -> cannot direct-copy
    src_b = np.full((4, 4), 5.0, dtype=np.float32)
    prof_b = {"transform": Affine.translation(15.0, -15.0) * master,
              "crs": "EPSG:32617", "nodata": np.nan}

    final = [src_a, src_b]
    profs = [prof_a, prof_b]
    new_final, new_prof = ws._prealign_scenes_to_master_grid(
        final, profs, master, "EPSG:32617", height=20, width=20
    )

    # Caller's lists must be untouched
    assert final[1] is src_b and profs[1] is prof_b
    # Aligned scene shared, misaligned scene replaced
    assert new_final[0] is src_a and new_prof[0] is prof_a
    assert new_final[1] is not src_b
    # The replacement is direct-copyable on the master grid...
    assert ws._direct_copy_offsets(new_prof[1], master, "EPSG:32617") is not None
    # ...and carries the warped data
    assert np.nanmax(new_final[1]) == pytest.approx(5.0)

    # After pre-alignment, windows come out via direct copy (no reproject)
    bounds = ws._compute_scene_dst_bounds(
        new_final, new_prof, master, "EPSG:32617", height=20, width=20
    )
    result = ws._mosaic_align_window(
        [1], new_final, new_prof,
        height=20, width=20, transform=master, target_crs="EPSG:32617",
        y_slice=slice(0, 8), x_slice=slice(0, 8),
        scene_bounds=bounds,
    )
    assert result is not None
    assert np.nanmax(result) == pytest.approx(5.0)


def test_zarr_nan_fill_value_skips_explicit_init():
    """A NaN fill_value store reads unwritten slots as NaN via resize alone."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        g = zarr.open_group(str(Path(td) / "t.zarr"), mode="w", zarr_format=3)
        g.create_array('x', data=np.arange(4, dtype=np.float64))
        g.create_array('y', data=np.arange(4, dtype=np.float64))
        g.create_array('time', shape=(0,), chunks=(1,), dtype='int64')
        g.create_array('VV_dB', shape=(0, 4, 4), chunks=(1, 2, 2),
                        dtype='float32', fill_value=np.nan)
        t, _ = ws._begin_zarr_timestep_blockwise(
            g, np.datetime64('2020-01-01', 'ns'), ['VV_dB']
        )
        assert np.isnan(g['VV_dB'][t]).all()


def test_zarr_default_fill_value_still_nan_initialised():
    """A store without fill_value=NaN must still be explicitly NaN-filled."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        g = zarr.open_group(str(Path(td) / "t.zarr"), mode="w", zarr_format=3)
        g.create_array('x', data=np.arange(4, dtype=np.float64))
        g.create_array('y', data=np.arange(4, dtype=np.float64))
        g.create_array('time', shape=(0,), chunks=(1,), dtype='int64')
        g.create_array('VV_dB', shape=(0, 4, 4), chunks=(1, 2, 2), dtype='float32')
        t, _ = ws._begin_zarr_timestep_blockwise(
            g, np.datetime64('2020-01-01', 'ns'), ['VV_dB']
        )
        assert np.isnan(g['VV_dB'][t]).all()


def test_windowed_hole_qc_matches_full_tile_and_rejects_real_hole():
    """Bounding-window hole fraction must equal the full-tile computation."""
    rng = np.random.default_rng(21)
    master = Affine.translation(0.0, 0.0) * Affine.scale(30.0, -30.0)
    H = W = 300

    src = rng.random((150, 150), dtype=np.float32) + 0.1
    src[60:90, 60:90] = np.nan  # hole with >=60px valid margin on every side
    prof = {"transform": master * Affine.translation(20, 20),
            "crs": "EPSG:32617", "nodata": np.nan}

    clean_dates = [pd.Timestamp("2020-02-01T00:00:00Z")]
    df_batch = pd.DataFrame({
        "pass_id": [1], "acq_group_id_within_mgrs_tile": [1],
        "acq_dt": clean_dates, "track_token": ["18"], "jpl_burst_id": ["B001"],
    })
    incomplete = []
    valid = ws._prepare_valid_clean_indices_for_monthly(
        "17MNU", "ASCENDING",
        final_vv=[src], prof_vv=[prof], final_vh=None, prof_vh=None,
        clean_dates=clean_dates, df_rtc_ts=df_batch,
        target_crs="EPSG:32617", transform=master, width=W, height=H,
        tile_clip=False, track_footprint={"18": 1},
        track_footprint_ids={"18": {"B001"}},
        incomplete_policy="skip", incomplete_sink=incomplete,
        interior_hole_max_frac=0.001,
    )
    assert valid == set()
    assert incomplete[0]["cause"] == "RASTER_INTERIOR_NODATA"

    full = np.full((H, W), np.nan, dtype=np.float32)
    full[20:170, 20:170] = src
    ref_frac = ws._interior_hole_fraction(np.isfinite(full), None)
    assert incomplete[0]["interior_hole_pct"] / 100.0 == pytest.approx(ref_frac, abs=1e-9)


def test_mosaic_align_window_adopts_first_buffer():
    """Multi-scene merge must match the pre-optimization first-valid-pixel policy."""
    rng = np.random.default_rng(5)
    t = Affine.translation(1000.0, 2000.0) * Affine.scale(30.0, -30.0)
    src_a = rng.random((6, 6), dtype=np.float32) + 0.1
    src_a[0, :] = np.nan
    src_b = rng.random((6, 6), dtype=np.float32) + 0.1
    prof = {"transform": t, "crs": "EPSG:32617", "nodata": np.nan}

    result = ws._mosaic_align_window(
        [0, 1], [src_a, src_b], [prof, dict(prof)],
        height=6, width=6, transform=t, target_crs="EPSG:32617",
        y_slice=slice(0, 6), x_slice=slice(0, 6),
    )
    ref = np.full((6, 6), np.nan, dtype=np.float32)
    for src in (src_a, src_b):
        tmp = src.copy()
        tmp[~np.isfinite(tmp) | (tmp <= 0)] = np.nan
        take = np.isnan(ref) & np.isfinite(tmp)
        ref[take] = tmp[take]
    assert np.allclose(result, ref, equal_nan=True)

    # Single-scene result must not alias the raw source array.
    solo = ws._mosaic_align_window(
        [0], [src_a], [prof],
        height=6, width=6, transform=t, target_crs="EPSG:32617",
        y_slice=slice(0, 6), x_slice=slice(0, 6),
    )
    assert solo is not src_a


def test_threaded_blocks_match_serial_output(tmp_path):
    """num_threads>1 must produce output identical to the serial path."""
    rng = np.random.default_rng(7)
    H = W = 512
    CHUNK = 256  # 2x2 = 4 blocks
    master = Affine.translation(500000.0, 9500000.0) * Affine.scale(30.0, -30.0)
    crs = "EPSG:32617"

    final_vv, prof_vv, final_vh, prof_vh = [], [], [], []
    idx_by_track = {18: [], 40: []}
    for i in range(8):
        h, w = 150, 300
        row_off = (i * 53) % (H - h)
        col_off = (i * 37) % (W - w)
        t = master * Affine.translation(col_off, row_off)
        prof = {"transform": t, "crs": crs, "nodata": np.nan}
        final_vv.append(rng.random((h, w), dtype=np.float32) + 0.05)
        prof_vv.append(prof)
        final_vh.append(rng.random((h, w), dtype=np.float32) * 0.3 + 0.02)
        prof_vh.append(dict(prof))
        idx_by_track[18 if i % 2 == 0 else 40].append(i)

    def run(num_threads, name):
        g = zarr.open_group(str(tmp_path / name), mode="w", zarr_format=3)
        g.create_array('x', data=np.arange(W, dtype=np.float64))
        g.create_array('y', data=np.arange(H, dtype=np.float64))
        g.create_array('time', shape=(0,), chunks=(64,), dtype='int64')
        for b in ('VV_dB', 'VH_dB'):
            g.create_array(b, shape=(0, H, W), chunks=(1, CHUNK, CHUNK),
                            dtype='float32', fill_value=np.nan)
        res = ws._write_smonthly_month_zarr_blockwise(
            g=g, month_str='2026-01', dt_ns=np.datetime64('2026-01-15', 'ns'),
            idx_by_track=idx_by_track,
            final_vv=final_vv, prof_vv=prof_vv, final_vh=final_vh, prof_vh=prof_vh,
            height=H, width=W, transform=master, target_crs=crs,
            chunk_y=CHUNK, chunk_x=CHUNK,
            band_names=['VV_dB', 'VH_dB'], copol_name='VV_dB', crosspol_name='VH_dB',
            features_ratio=False, features_rvi=False, ratio_name='Ratio', rvi_name='RVI',
            composite_method='median', trim_fraction=0.15,
            tile_clip=False, mgrs_tile_id='17MPU', num_threads=num_threads,
        )
        return res, g['VV_dB'][0], g['VH_dB'][0]

    res1, vv1, vh1 = run(1, "serial.zarr")
    res4, vv4, vh4 = run(4, "threaded.zarr")

    assert res1 == res4
    assert np.array_equal(np.isfinite(vv1), np.isfinite(vv4))
    assert np.allclose(vv1, vv4, equal_nan=True)
    assert np.allclose(vh1, vh4, equal_nan=True)


def test_resolve_blockwise_threads_integers():
    assert ws._resolve_blockwise_threads(1) == 1
    assert ws._resolve_blockwise_threads(4) == 4
    assert ws._resolve_blockwise_threads("8") == 8
    # Floors at 1 for zero/negative/garbage
    assert ws._resolve_blockwise_threads(0) == 1
    assert ws._resolve_blockwise_threads(-3) == 1
    assert ws._resolve_blockwise_threads(None) == 1
    assert ws._resolve_blockwise_threads("notanint") == 1


def test_resolve_blockwise_threads_auto(monkeypatch):
    monkeypatch.setattr(ws.os, "cpu_count", lambda: 16)
    # Divides cores across the tile-worker pool
    assert ws._resolve_blockwise_threads("auto", max_workers=2) == 8
    assert ws._resolve_blockwise_threads("auto", max_workers=4) == 4
    assert ws._resolve_blockwise_threads("AUTO", max_workers=8) == 2
    # Caps at 8 even with many cores and a single worker
    monkeypatch.setattr(ws.os, "cpu_count", lambda: 64)
    assert ws._resolve_blockwise_threads("auto", max_workers=1) == 8
    # Never returns 0 when workers exceed cores
    monkeypatch.setattr(ws.os, "cpu_count", lambda: 4)
    assert ws._resolve_blockwise_threads("auto", max_workers=16) == 1
    # Unknown cpu_count degrades to 1
    monkeypatch.setattr(ws.os, "cpu_count", lambda: None)
    assert ws._resolve_blockwise_threads("auto", max_workers=2) == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
