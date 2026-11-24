"""The actual monthly reducer must agree with its stored metadata."""
import json

import numpy as np
import pandas as pd
import pytest
import zarr
from rasterio.transform import Affine

from s1grits.scenes.smonthly_writer import _write_smonthly_one_track
from s1grits.stac_builder import set_stac_output_enabled, stac_output_enabled


def monthly_inputs(tile_dir, method):
    dates = list(pd.to_datetime(['2026-01-01', '2026-01-13', '2026-01-25'], utc=True))
    transform = Affine(30, 0, 600000, 0, -30, 9900000)
    profile = dict(crs='EPSG:32717', transform=transform, nodata=np.nan, height=2, width=3)
    arrays = [np.full((2, 3), x, dtype=np.float32) for x in (1, 2, 9)]
    for array in arrays:
        array[0, 0] = np.nan
    frame = pd.DataFrame(dict(acq_dt=dates, track_number=[18]*3, pass_id=[1, 2, 3],
                              jpl_burst_id=['B1']*3, opera_id=['O1', 'O2', 'O3']))
    return dict(mgrs_tile_id='17MPU', direction_label='ASCENDING', tile_dir=tile_dir,
                final_vv=arrays, prof_vv=[profile]*3, final_vh=arrays, prof_vh=[profile]*3,
                clean_dates=dates, target_crs='EPSG:32717', target_res=30,
                generate_cog=False, generate_preview=False, chunk_y=2, chunk_x=3, cog_block=16,
                on_time_conflict='skip', monthly_cfg={'composite_method': method},
                processing_level='ARDC', transform=transform, width=3, height=2,
                x_coords=600015+30*np.arange(3), y_coords=9899985-30*np.arange(2),
                df_batch=frame, tile_clip=False, track_token='18')


@pytest.mark.parametrize('method,expected', [('mean', 4), ('nanmean', 4), ('median', 2), ('nanmedian', 2)])
@pytest.mark.parametrize('legacy', [False, True])
def test_written_method_matches_values_and_metadata(tmp_path, method, expected, legacy):
    previous = stac_output_enabled()
    set_stac_output_enabled(True)
    try:
        records = _write_smonthly_one_track(**monthly_inputs(tmp_path, method), spatial_filter_legacy=legacy)
    finally:
        set_stac_output_enabled(previous)
    group = zarr.open_group(str(next(tmp_path.glob('smonthly_*/zarr/*.zarr'))), mode='r')
    assert np.isnan(group['VV_dB'][0, 0, 0])
    np.testing.assert_allclose(group['VV_dB'][0, 1, :], 10*np.log10(expected), atol=1e-6)
    np.testing.assert_array_equal(group['n_obs'][0, 1, :], [3, 3, 3])
    item = json.loads(next(tmp_path.glob('items/*/*.json')).read_text())
    assert item['properties']['s1:monthly_composite'] == method.removeprefix('nan')
    assert item['properties']['s1grits:composite_domain'] == 'linear_power'
    for metadata in [records[0], group.attrs]:
        variant = json.loads(metadata['processing_variant_json'])
        assert variant['processing.monthly.composite_method'] == method


def test_bad_method_fails_before_creating_output(tmp_path):
    with pytest.raises(ValueError, match='Unknown monthly'):
        _write_smonthly_one_track(**monthly_inputs(tmp_path / 'out', 'medain'))
    assert not (tmp_path / 'out').exists()
