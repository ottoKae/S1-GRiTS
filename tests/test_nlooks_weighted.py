"""Small real GeoTIFF weights through the scene writer, COG and catalog."""
import json
import shutil

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

from s1grits.scenes.average import validate_intra_pass_config
from s1grits.scenes.looks import load_looks_assets
from test_intra_pass_average import fingerprint, inputs, write


def rtc_id(burst):
    return f'OPERA_L2_RTC-S1_{burst}_20260107T000000Z_20260107T030000Z_S1A_30_v1.0'


def weight_file(folder, burst, profile, values, date='20140403'):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/f'OPERA_L2_RTC-S1-STATIC_{burst}_{date}_S1A_30_v1.0_number_of_looks.tif'
    with rasterio.open(path, 'w', **{**profile, 'driver': 'GTiff', 'count': 1, 'dtype': 'float32'}) as dst:
        dst.write(np.asarray(values, dtype='f4'), 1)
    return path


def weighted_inputs(root, chunk=2):
    options = inputs(root/'out', 'nlooks_weighted', chunk)
    frame = options['df_rtc_ts']
    frame['jpl_burst_id'] = [f'T018-03865{i}-IW2' for i in range(3)]
    frame['opera_id'] = frame['jpl_burst_id'].map(rtc_id)
    folder = root/'weights'
    for row, values in [(0, [[1, 2, 0], [2, 2, 2]]), (2, [[3, 0, 2], [-9999, .5, 2]])]:
        weight_file(folder, frame.iloc[row]['jpl_burst_id'], options['prof_vv'][0], values)
    options['looks_assets'] = load_looks_assets(folder)
    return options


@pytest.mark.parametrize('chunk,threads', [(1, 1), (2, 2)])
def test_values(tmp_path, chunk, threads):
    options = weighted_inputs(tmp_path, chunk)
    options['blockwise_threads'] = threads
    record, group = write(options)
    expected = np.array([[2.5, np.nan, np.nan], [np.nan, 7, np.nan]])
    for band in ('VV_dB', 'VH_dB'):
        np.testing.assert_allclose(group[band][0], 10*np.log10(expected), atol=1e-6)
    np.testing.assert_allclose(group['Ratio'][0], expected/expected)
    np.testing.assert_allclose(group['RVI'][0], 2*expected/expected)
    np.testing.assert_array_equal(group['n_contributors'][0], [[2, 0, 0], [0, 1, 0]])
    np.testing.assert_array_equal(group['weight_sum'][0], [[4, 0, 0], [0, .5, 0]])
    assert group['weight_sum'].dtype == np.dtype('float64')
    assert group.attrs['numeric_processing']['mosaic']['method'] == 'nlooks_weighted'
    sources = group.attrs['acquisitions_by_time']['20260107T000000']
    assert len(sources['looks_assets']) == len(sources['opera_ids']) == 2
    item = json.loads((options['tile_dir']/record['item_path']).read_text())
    assert 'weight_sum' in item['properties']['cube:dimensions']['spectral']['values']


def test_identity(tmp_path):
    options = weighted_inputs(tmp_path)
    record, group = write(options)
    expected = group['VV_dB'][:]
    original = fingerprint(options['tile_dir'])
    shutil.copytree(tmp_path/'weights', tmp_path/'copy')
    options['looks_assets'] = load_looks_assets(tmp_path/'copy')
    options['tile_dir'] = tmp_path/'other'
    for key in ('final_vv', 'final_vh', 'prof_vv', 'prof_vh', 'clean_dates', 'clean_source_rows'):
        options[key] = options[key][::-1]
    reordered, group = write(options)
    assert reordered['processing_signature'] == record['processing_signature']
    np.testing.assert_array_equal(group['VV_dB'][:], expected)
    # A different weight asset creates a distinct product, preserving the first store.
    path = next((tmp_path/'weights').glob('*038650*'))
    with rasterio.open(path, 'r+') as dst:
        dst.write(np.full((2, 3), 16777216, dtype='f4'), 1)
    options['looks_assets'] = load_looks_assets(tmp_path/'weights')
    options['tile_dir'] = tmp_path/'out'
    changed, group = write(options)
    assert changed['processing_signature'] != record['processing_signature']
    assert group['weight_sum'][0, 0, 0] == 16777219  # Detect a float32 write truncation.
    assert all(fingerprint(options['tile_dir'])[key] == value for key, value in original.items())
    again, group = write(options)
    assert again['item_id'] == changed['item_id']
    assert group['time'].shape == (1,)


@pytest.mark.parametrize('failure', ['missing', 'grid', 'version', 'duplicate', 'empty'])
def test_reject(tmp_path, failure):
    options = weighted_inputs(tmp_path)
    if failure == 'missing':
        options['looks_assets'].pop('T018-038650-IW2')
    elif failure == 'grid':
        options['prof_vh'] = [dict(p, transform=Affine(30, 0, 600015, 0, -30, 9900000)) for p in options['prof_vh']]
    elif failure == 'version':
        options['df_rtc_ts']['opera_id'] = options['df_rtc_ts']['opera_id'].str.replace('v1.0', 'v2.0', regex=False)
    else:
        folder = tmp_path/'weights'
        if failure == 'duplicate':
            weight_file(folder, 'T018-038650-IW2', options['prof_vv'][0], np.ones((2, 3)), '20150403')
        else:
            folder = tmp_path/'empty'
        with pytest.raises(ValueError):
            load_looks_assets(folder)
        return
    with pytest.raises(ValueError):
        write(options)
    assert not options['tile_dir'].exists()


def test_invalid_weights(tmp_path):
    options = weighted_inputs(tmp_path)
    for burst in ('T018-038650-IW2', 'T018-038652-IW2'):
        weight_file(tmp_path/'weights', burst, options['prof_vv'][0], [[np.nan, np.inf, -1], [0, 1, 1]])
    options['looks_assets'] = load_looks_assets(tmp_path/'weights')
    _, group = write(options)
    np.testing.assert_array_equal(group['n_contributors'][0], [[0, 0, 0], [0, 1, 0]])
    np.testing.assert_array_equal(group['weight_sum'][0], [[0, 0, 0], [0, 1, 0]])
    assert np.isfinite(group['VV_dB'][:]).sum() == 1


def test_cog(tmp_path):
    import pandas as pd
    from s1grits.analysis.catalog import resync_catalog_from_filesystem
    from s1grits.stac_builder import set_stac_output_enabled, stac_output_enabled

    options = weighted_inputs(tmp_path)
    options.update(tile_dir=tmp_path/'17MPU', generate_cog=True)
    record, group = write(options)
    with rasterio.open(options['tile_dir']/record['cog_path']) as src:
        assert src.descriptions == ('VV_dB', 'VH_dB', 'Ratio', 'RVI')
        actual = src.read(1, masked=True).filled(np.nan)
        np.testing.assert_allclose(actual, group['VV_dB'][0], equal_nan=True)
    previous = stac_output_enabled()
    try:
        result = resync_catalog_from_filesystem(tmp_path, write_stac=True, stac_format='json')
    finally:
        set_stac_output_enabled(previous)
    assert result['success'], result
    rebuilt, = pd.read_parquet(tmp_path/'catalog.parquet').to_dict('records')
    assert rebuilt['item_id'] == record['item_id']
    item = json.loads((options['tile_dir']/rebuilt['item_path']).read_text())
    assert 'weight_sum' not in [b['name'] for b in item['assets']['cog']['eo:bands']]
    assert list(rebuilt['opera_ids']) == record['opera_ids']


def test_config():
    with pytest.raises(ValueError, match='looks_directory'):
        validate_intra_pass_config({'processing': {'intra_pass_mosaic': {'method': 'nlooks_weighted'}}})
    with pytest.raises(ValueError, match='only used'):
        validate_intra_pass_config({'processing': {'intra_pass_mosaic': {'looks_directory': 'unused'}}})


def test_clip(tmp_path, monkeypatch):
    from pyproj import Transformer
    from shapely.geometry import box
    from shapely.ops import transform
    from s1grits.scenes import blocks, scene_writer

    options = weighted_inputs(tmp_path)
    geometry = transform(Transformer.from_crs(32717, 4326, always_xy=True).transform,
                         box(600000, 9899940, 600030, 9900000))
    monkeypatch.setattr(scene_writer, '_get_mgrs_tile_geometry_wkt', lambda _: geometry.wkt)
    monkeypatch.setattr(blocks, '_get_mgrs_tile_geometry_wkt', lambda _: geometry.wkt)
    options['tile_clip'] = True
    _, group = write(options)
    np.testing.assert_array_equal(group['n_contributors'][0], [[2, 0, 0], [0, 0, 0]])
    np.testing.assert_array_equal(group['weight_sum'][0], [[4, np.nan, np.nan], [0, np.nan, np.nan]])


def test_empty(tmp_path):
    from s1grits.scenes.scene_writer import _write_scenes_output

    options = weighted_inputs(tmp_path)
    for burst in ('T018-038650-IW2', 'T018-038652-IW2'):
        weight_file(tmp_path/'weights', burst, options['prof_vv'][0], np.zeros((2, 3)))
    options['looks_assets'] = load_looks_assets(tmp_path/'weights')
    assert _write_scenes_output(**options) == []
    assert not list(options['tile_dir'].rglob('*.zarr'))
