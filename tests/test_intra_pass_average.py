"""Real writers, source-row mapping, product isolation and paired averages."""
import hashlib
import json

import numpy as np
import pandas as pd
import pytest
import zarr
from rasterio.transform import Affine

from s1grits.scenes.average import validate_intra_pass_config
from s1grits.scenes.scene_writer import _write_scenes_output
from s1grits.resolver import CubeResolver
from s1grits.stac_builder import build_stac_item_dict, set_stac_output_enabled, stac_output_enabled


def inputs(root, method='average', chunk=2):
    date = pd.Timestamp('2026-01-07T00:00:00Z')
    transform = Affine(30, 0, 600000, 0, -30, 9900000)
    profile = dict(crs='EPSG:32717', transform=transform, nodata=-9999., height=2, width=3)
    # Metadata row 1 failed loading, and all rows share a timestamp.
    frame = pd.DataFrame(dict(
        acq_dt=[date]*3, track_number=[18]*3, track_token=['18']*3,
        pass_id=[1]*3, acq_group_id_within_mgrs_tile=[1]*3,
        jpl_burst_id=['B0', 'B1', 'B2'], opera_id=['O0', 'O1', 'O2'], _source_row=[0, 1, 2]))
    return dict(mgrs_tile_id='17MPU', direction_label='ASCENDING', tile_dir=root,
                final_vv=[np.array([[1, -9999, 3], [0, 5, np.nan]], dtype='f4'),
                          np.array([[3, 8, 9], [2, 7, np.nan]], dtype='f4')],
                final_vh=[np.array([[1, 2, 1], [1, np.nan, np.nan]], dtype='f4'),
                          np.array([[3, 4, -9999], [2, 7, np.nan]], dtype='f4')],
                prof_vv=[profile]*2, prof_vh=[profile]*2, clean_dates=[date]*2,
                df_rtc_ts=frame, target_crs='EPSG:32717', target_res=30,
                generate_cog=False, generate_preview=False, chunk_y=chunk, chunk_x=chunk,
                cog_block=16, processing_level='ARDC', transform=transform, width=3, height=2,
                x_coords=600015+30*np.arange(3), y_coords=9899985-30*np.arange(2),
                tile_clip=False, intra_pass_method=method, clean_source_rows=[0, 2],
                features_ratio=True, features_rvi=True, incomplete_policy='write')


def write(options):
    previous = stac_output_enabled()
    set_stac_output_enabled(True)
    try:
        record, = _write_scenes_output(**options)
    finally:
        set_stac_output_enabled(previous)
    group = zarr.open_group(str(options['tile_dir'] / record['zarr_path']), mode='r')
    return record, group


@pytest.mark.parametrize('chunk,threads', [(1, 1), (2, 2), (3, 1)])
def test_mean_count_sources_and_stac(tmp_path, chunk, threads):
    options = inputs(tmp_path / '17MPU', chunk=chunk)
    options['blockwise_threads'] = threads
    record, group = write(options)
    vv = np.array([[2, 8, 3], [2, 7, np.nan]], dtype='f4')
    vh = np.array([[2, 4, 1], [2, 7, np.nan]], dtype='f4')
    np.testing.assert_allclose(group['VV_dB'][0], 10*np.log10(vv), atol=1e-6)
    np.testing.assert_allclose(group['VH_dB'][0], 10*np.log10(vh), atol=1e-6)
    np.testing.assert_allclose(group['Ratio'][0], vh/vv, atol=1e-6)
    np.testing.assert_allclose(group['RVI'][0], 4*vh/(vv+vh), atol=1e-6)
    np.testing.assert_array_equal(group['n_contributors'][0], [[2, 1, 1], [1, 1, 0]])
    assert group['n_contributors'].dtype == np.dtype('uint32')
    assert record['opera_ids'] == ['O0', 'O2']
    assert record['n_bursts'] == 2
    assert group.attrs['acquisitions_by_time']['20260107T000000']['opera_ids'] == ['O0', 'O2']
    item = json.loads((options['tile_dir']/record['item_path']).read_text())
    resynced, _ = build_stac_item_dict(record, str(tmp_path))
    assert item['id'] == resynced['id'] == record['item_id']
    assert item['properties']['s1grits:processing_signature'] == record['processing_signature']
    assert resynced['properties']['s1grits:processing_variant']['processing.mosaic_domain'] == 'linear_power'
    assert 'n_contributors' in item['properties']['cube:dimensions']['spectral']['values']
    # Resume does not append the same acquisition twice.
    again, group = write(options)
    assert group['time'].shape == (1,)
    assert again['processing_signature'] == record['processing_signature']


def test_instances_are_separate_and_readable(tmp_path):
    records = []
    for method in ('first_valid_pixel', 'average'):
        record, _ = write(inputs(tmp_path / '17MPU', method))
        records.append(record)
    first, average = records
    assert first['grid_id'] == average['grid_id']
    for key in ('processing_signature', 'zarr_path', 'item_id', 'item_path'):
        assert first[key] != average[key]
    pd.DataFrame(records).to_parquet(tmp_path/'catalog.parquet')
    resolver = CubeResolver(tmp_path)
    selected = resolver.query(processing_signature=average['processing_signature'])
    assert len(selected) == 1
    with resolver.open(selected.iloc[0]) as dataset:
        assert 'n_contributors' in dataset
        assert dataset['VV_dB'].isel(time=0, y=0, x=0).item() == pytest.approx(10*np.log10(2))
    with resolver.open(resolver.query(processing_signature=first['processing_signature']).iloc[0]) as dataset:
        assert 'n_contributors' not in dataset
        assert dataset['VV_dB'].isel(time=0, y=0, x=0).item() == 0


def fingerprint(folder):
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob('*') if p.is_file()}


@pytest.mark.parametrize('mismatch', ['signature', 'grid', 'sources'])
def test_resume_mismatch_does_not_mutate_store(tmp_path, mismatch):
    options = inputs(tmp_path)
    record, _ = write(options)
    path = tmp_path / record['zarr_path']
    group = zarr.open_group(str(path), mode='r+')
    if mismatch == 'signature':
        group.attrs['processing_signature'] = 'other_instance'
    elif mismatch == 'grid':
        group.attrs['transform'] = [30, 0, 600030, 0, -30, 9900000]
    else:
        options['df_rtc_ts'].loc[2, 'opera_id'] = 'O9'
    before = fingerprint(tmp_path)
    with pytest.raises(ValueError, match='average'):
        _write_scenes_output(**options)
    assert fingerprint(tmp_path) == before


def test_source_order_is_stable_and_mapping_is_required(tmp_path):
    options = inputs(tmp_path/'a')
    rec, group = write(options)
    expected = group['VV_dB'][:]
    options['tile_dir'] = tmp_path/'b'
    options['df_rtc_ts'] = options['df_rtc_ts'].iloc[::-1]
    for name in ('final_vv', 'final_vh', 'prof_vv', 'prof_vh', 'clean_dates', 'clean_source_rows'):
        options[name] = options[name][::-1]
    reordered, group = write(options)
    np.testing.assert_array_equal(group['VV_dB'][:], expected)
    assert reordered['processing_signature'] == rec['processing_signature']
    options['tile_dir'] = tmp_path/'bad'
    options['clean_source_rows'] = None
    with pytest.raises(ValueError, match='source-row'):
        _write_scenes_output(**options)
    assert not options['tile_dir'].exists()


@pytest.mark.parametrize('section,value', [
    ('processing', {'monthly': {'enabled': True}}),
    ('processing', {'spatial_despeckle': True}),
    ('processing', {'target_resolution': 10}),
    ('processing', {'resampling_method': 'bilinear'}),
    ('roi', {'polarization': 'HH+HV'}),
    ('static_layers', {'run_after_scenes': True}),
])
def test_unsupported_combinations_fail_before_output(tmp_path, section, value):
    import yaml
    from s1grits.workflow_scenes import run_scenes_workflow
    config = {'processing': {'intra_pass_mosaic': {'method': 'average'}},
              'roi': {'flight_direction': 'ASCENDING'},
              'output': {'base_dir': str(tmp_path/'out')}}
    config.setdefault(section, {}).update(value)
    path = tmp_path/'config.yaml'
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match='average'):
        run_scenes_workflow(path)
    assert not (tmp_path/'out').exists()


def test_default_strategy_is_unchanged():
    assert validate_intra_pass_config({}) == 'first_valid_pixel'
    assert validate_intra_pass_config({'processing': {'intra_pass_mosaic': {'method': 'first_valid_pixel'}}}) == 'first_valid_pixel'
    with pytest.raises(ValueError, match='method'):
        validate_intra_pass_config({'processing': {'intra_pass_mosaic': {'method': 'avergae'}}})


def test_exports(tmp_path):
    import rasterio
    from s1grits.analysis.catalog import resync_catalog_from_filesystem

    options = inputs(tmp_path/'17MPU', chunk=8)
    options.update(features_glcm=True, generate_cog=True, width=16, height=16,
                   x_coords=600015+30*np.arange(16), y_coords=9899985-30*np.arange(16))
    for arrays, profiles in [('final_vv', 'prof_vv'), ('final_vh', 'prof_vh')]:
        options[arrays] = [np.resize(a, (16, 16)) for a in options[arrays]]
        options[arrays].append(np.full((16, 16), 2, dtype='f4'))
        options[profiles] = [dict(options[profiles][0], height=16, width=16)]*3
    options['clean_source_rows'] = [0, 2, 1]
    options['clean_dates'].append(options['clean_dates'][0])
    records = []
    for days in [0, 12]:
        if days:
            options['clean_dates'] = [d+pd.Timedelta(days=days) for d in options['clean_dates']]
            options['df_rtc_ts']['acq_dt'] += pd.Timedelta(days=days)
            options['df_rtc_ts']['opera_id'] += '-next'
        record, group = write(options)
        records.append(record)
        assert group['n_contributors'][len(records)-1, 0, 0] == 3
        with rasterio.open(options['tile_dir']/record['cog_path']) as cog:
            assert 'n_contributors' not in cog.descriptions
            assert any('glcm' in b.lower() for b in cog.descriptions)
            np.testing.assert_allclose(cog.read(1), group['VV_dB'][len(records)-1], equal_nan=True)
        item, _ = build_stac_item_dict(record, str(tmp_path))
        assert 'n_contributors' not in [b['name'] for b in item['assets']['cog']['eo:bands']]
    previous = stac_output_enabled()
    try:
        result = resync_catalog_from_filesystem(tmp_path, write_stac=True, stac_format='json')
    finally:
        set_stac_output_enabled(previous)
    assert result['success']
    rebuilt = pd.read_parquet(tmp_path/'catalog.parquet')
    assert set(rebuilt['item_id']) == {r['item_id'] for r in records}
    assert len(rebuilt) == 2
    for record in rebuilt.to_dict('records'):
        item = json.loads((tmp_path/'17MPU'/record['item_path']).read_text())
        assert item['id'] == record['item_id']
        assert 'n_contributors' not in [b['name'] for b in item['assets']['cog']['eo:bands']]


@pytest.mark.parametrize('method', ['average', 'bursts_center', 'nlooks_weighted'], ids=['a', 'c', 'w'])
def test_flow(tmp_path_factory, monkeypatch, method):
    tmp_path = tmp_path_factory.mktemp('f')
    import yaml
    from s1grits import workflow_scenes as workflow
    from s1grits.scenes import pipeline
    options = inputs(tmp_path)
    dates = pd.to_datetime(['2026-01-31T23:59:50Z', '2026-02-01T00:00:01Z',
                            '2026-02-01T00:00:02Z', '2026-02-01T00:00:03Z'])
    frame = pd.DataFrame(dict(
        acq_dt=dates, opera_id=['O0', 'O1', 'O2', 'O3'],
        jpl_burst_id=['B0', 'B1', 'B2', 'B3'], track_number=[18]*4,
        track_token=['18']*4, pass_id=[1]*4, acq_group_id_within_mgrs_tile=[1]*4,
        orbit_pass=['ASCENDING']*4,
        url_copol=[f'https://example.org/{i}_VV.tif' for i in range(4)],
        url_crosspol=[f'https://example.org/{i}_VH.tif' for i in range(4)]))
    monkeypatch.setattr(workflow, 'enumerate_mgrs_tiles', lambda _: ['17MPU'])
    monkeypatch.setattr(workflow, 'parse_time_range_config', lambda *a, **k: [('2026-01-01', '2026-02-28')])
    monkeypatch.setattr(pipeline, 'query_rtc_metadata_for_tile', lambda *a: frame.copy())
    monkeypatch.setattr(pipeline, '_adopt_existing_master_grid', lambda *a: (
        options['transform'], 3, 2, options['x_coords'], options['y_coords']))
    calls = []

    def download(rows, **kwargs):
        assert kwargs['return_source_indices']
        assert len(rows) == 4  # One acquisition, despite spanning two calendar months.
        calls.append(rows['_source_row'].tolist())
        arrays = [np.full((2, 3), v, dtype='f4') for v in (1, 99, 7)]
        profiles = [options['prof_vv'][0], None, options['prof_vv'][0]]
        positions = [0, 2, 3]  # row 1 failed; row 2 has an invalid decoded profile.
        return arrays, profiles, arrays, profiles, rows.iloc[positions]['acq_datetime'].tolist(), positions

    monkeypatch.setattr(pipeline, 'load_and_despeckle_rtc_strict', download)
    root = tmp_path/'out'
    root.mkdir()
    (root/'mainline.txt').write_text('preserve')
    config = {
        'roi': {'flight_direction': 'ASCENDING', 'polarization': 'VV+VH'},
        'output': {'base_dir': str(root), 'formats': {'cog': False, 'preview': False}},
        'processing': {'intra_pass_mosaic': {'method': method}, 'tile_clip': False,
                       'zarr_chunks': {'y': 2, 'x': 3}, 'incomplete_acquisition': 'write'},
        'memory': {'batch_strategy': 'monthly', 'batch_max_retries': 0, 'clear_cache_per_batch': False},
        'query': {'footprint_lookback_months': 0}, 'runtime': {'enabled': False},
    }
    if method == 'nlooks_weighted':
        from test_nlooks_weighted import rtc_id, weight_file
        frame['jpl_burst_id'] = [f'T018-03865{i}-IW2' for i in range(4)]
        frame['opera_id'] = frame['jpl_burst_id'].map(rtc_id)
        for i, weight in [(0, 1), (3, 3)]:
            weight_file(tmp_path/'weights', frame.iloc[i]['jpl_burst_id'], options['prof_vv'][0],
                        np.full((2, 3), weight))
        config['processing']['intra_pass_mosaic']['looks_directory'] = str(tmp_path/'weights')
    expected_ids = frame.iloc[[0, 3]]['opera_id'].tolist()
    path = tmp_path/'config.yaml'
    for ratio in (False, True, False):
        config['processing']['features_ratio'] = ratio
        path.write_text(yaml.safe_dump(config))
        result = workflow.run_scenes_workflow(path)
        assert result['17MPU']['status'] == 'success', result
    derived = root/'derived'/f'intra_pass_{method}'
    assert len(calls) == 3
    for catalog_path in (derived/'catalog.parquet', derived/'17MPU/catalog.parquet'):
        catalog = pd.read_parquet(catalog_path)
        assert len(catalog) == 2
        assert catalog['processing_signature'].nunique() == 2
        for record in catalog.to_dict('records'):
            assert list(record['opera_ids']) == expected_ids
            group = zarr.open_group(str(derived/'17MPU'/record['zarr_path']), mode='r')
            expected_value = {'average': 4, 'bursts_center': 1, 'nlooks_weighted': 5.5}[method]
            np.testing.assert_allclose(group['VV_dB'][:], 10*np.log10(expected_value), atol=1e-6)
            count_band = 'n_candidates' if method=='bursts_center' else 'n_contributors'
            np.testing.assert_array_equal(group[count_band][:], np.full((1, 2, 3), 2))
    assert not (root/'catalog.parquet').exists()
    assert (root/'mainline.txt').read_text() == 'preserve'
    from s1grits.analysis.catalog import resync_catalog_from_filesystem
    previous = stac_output_enabled()
    try:
        result = resync_catalog_from_filesystem(derived, write_stac=True, stac_format='json')
    finally:
        set_stac_output_enabled(previous)
    assert result['success'], result
    rebuilt = pd.read_parquet(derived/'catalog.parquet')
    assert set(rebuilt['item_id']) == set(catalog['item_id'])
    for record in rebuilt.to_dict('records'):
        assert list(record['opera_ids']) == expected_ids
        assert record['pass_id'] == 1
        item = json.loads((derived/'17MPU'/record['item_path']).read_text())
        assert item['id'] == record['item_id']
        assert set(item['properties']['cube:dimensions']['spectral']['values']) == set(json.loads(record['bands']))
        assert 'spatial_ref' not in json.loads(record['bands'])
