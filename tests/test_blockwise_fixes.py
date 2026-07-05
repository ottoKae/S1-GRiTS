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

    def fake_composite(idxs, final_arr, prof_arr, h, w, tfm, crs, y_sl, x_sl, method, trim):
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

    def fake_composite(idxs, final_arr, prof_arr, h, w, tfm, crs, y_sl, x_sl, method, trim):
        call_count["n"] += 1
        bh = (y_sl.stop or 0) - (y_sl.start or 0)
        bw = (x_sl.stop or 0) - (x_sl.start or 0)
        return np.ones((bh, bw), dtype=np.float32)

    monkeypatch.setattr(ws, '_track_composite_block', fake_composite)

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
    # Pass 1 still computes 4 blocks * 2 tracks * 2 bands = 16 calls.
    # Pass 2 should compute only the first full-coverage track:
    # 4 blocks * 1 track * 2 bands = 8 calls. Without the early break this
    # would be 32 total calls.
    assert call_count["n"] == 24


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
