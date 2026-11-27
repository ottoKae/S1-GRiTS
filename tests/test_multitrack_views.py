"""Small on-disk monthly products: hand values, contribution flags and reuse."""
import json
import os
import shutil
import subprocess
import sys

from dask.callbacks import Callback
import numpy as np
import pandas as pd
import pytest
import zarr

from s1grits.analysis import define_multitrack_view, derive_cube, register_product, register_view, materialize_view
from s1grits.analysis.catalog import resync_catalog_from_filesystem
from s1grits.catalog_validator import validate_catalog_integrity
from s1grits.product_instance import make_processing_signature
from s1grits.resolver import CubeResolver
from test_cube_derivatives import source, hashes


def monthly_parents(root, layout='tyx'):
    records, stores = [], []
    for track, count, power in [(18, 3, 1), (120, 6, 9)]:
        scene_root = root/f's{track}'
        group, _, _ = source(scene_root, layout, count=count)
        dates = (['2026-01-01', '2026-01-13', '2026-03-01'] if track == 18
                 else [f'2026-01-{day:02}' for day in range(1, 7)])
        group['time'][:] = np.array(dates, dtype='datetime64[ns]')
        for band, scale in [('VV_dB', 1), ('VH_dB', .25)]:
            data = np.full((count, 2, 2), 10*np.log10(power*scale), 'f4')
            if track == 18:
                data[-1] = 10*np.log10(4*scale)
                if band == 'VH_dB':
                    data[:, 0, 1] = -12345
                    data[:, 1, 1] = np.nan
            elif band == 'VV_dB':
                data[:, 1, 0] = -9999
                data[:, 1, 1] = np.nan
            group[band][:] = data if layout == 'tyx' else data.transpose(1, 2, 0)
        frame = pd.read_parquet(scene_root/'catalog.parquet')
        frame['track'] = track
        frame['item_id'] = f'scene{track}'
        frame['geometry_group_id'] = f'17MPU_ASCENDING_TK{track}'
        frame.to_parquet(scene_root/'catalog.parquet')
        out = root/f'm{track}'
        record = derive_cube(scene_root, f'scene{track}', out, features=[], monthly='mean',
                             start='2026-01-01', end='2026-03-31T23:59:59.999999999', chunk_size=2)
        store = out/record['tile_id']/record['zarr_path']
        monthly = zarr.open_group(str(store), mode='r+')
        monthly.attrs['generation_id'] = f'g{track}'
        # Missing February in both stores, and missing March in the second.
        keep = [0, 2] if track == 18 else [0]
        for name in monthly.array_keys():
            array = monthly[name]
            if name == 'time' or array.ndim == 3:
                values = array[:][keep]
                array.resize(values.shape)
                array[:] = values
        records.append(register_product(root/'catalog', record, source_root=out))
        stores.append(store)
    return records, stores


def define(root, records, method='mean', **kwargs):
    return define_multitrack_view(root/'catalog', [r['item_id'] for r in records],
        method=method, start_month='2026-01', end_month='2026-03', **kwargs)


@pytest.mark.parametrize('layout', ['tyx', 'yxt'])
@pytest.mark.parametrize('method', ['mean', 'first_valid_pixel'])
def test_values_provenance_lazy_and_materialized(tmp_path, layout, method):
    records, stores = monthly_parents(tmp_path, layout)
    before = [hashes(p) for p in stores]
    executed = []
    with Callback(posttask=lambda *args: executed.append(args[0])):
        definition = define(tmp_path, records, method)
        row = register_view(tmp_path/'v', definition)
        assert not list((tmp_path/'v').rglob('*.zarr'))
        with CubeResolver(tmp_path/'v').open(row) as lazy:
            assert not executed
            actual = lazy.compute()
    assert executed
    power = 5 if method == 'mean' else 1
    np.testing.assert_allclose(actual.VV_dB[0], 10*np.log10([[power, 9], [1, np.nan]]), atol=2e-6)
    np.testing.assert_allclose(actual.VH_dB[0], 10*np.log10(np.array([[power, 9], [1, np.nan]])*.25), atol=2e-6)
    np.testing.assert_array_equal(actual.n_contributing_tracks[0], [[2 if method == 'mean' else 1, 1], [1, 0]])
    np.testing.assert_array_equal(actual.contributes_TK18[0], [[1, 0], [1, 0]])
    np.testing.assert_array_equal(actual.contributes_TK120[0], [[int(method == 'mean'), 1], [0, 0]])
    assert np.isnan(actual.VV_dB[1]).all() and not actual.n_contributing_tracks[1].any()
    np.testing.assert_allclose(actual.VV_dB[2, 0, 0], 10*np.log10(4), atol=1e-6)
    assert actual.contributes_TK120[2].sum() == 0
    np.testing.assert_array_equal(actual.time.values, np.array(['2026-01-01', '2026-02-01', '2026-03-01'], 'datetime64[ns]'))
    assert row['track'] is None and row['geometry_group_id'] is None
    assert 'n_obs' not in actual
    assert zarr.open_group(str(stores[0]))['n_obs'][0, 0, 0] == 2
    assert zarr.open_group(str(stores[1]))['n_obs'][0, 0, 0] == 6
    assert definition['contract']['parents'][1]['parent_n_obs']['variable'] == 'n_obs'
    for chunk in [1, 2]:
        destination = tmp_path/'v'/'m1' if chunk == 1 else tmp_path/'m2'
        saved = materialize_view(tmp_path/'v', row['item_id'], destination, chunk_size=chunk)
        with CubeResolver(destination).open(saved) as physical:
            for name in list(actual.data_vars)+['time', 'x', 'y']:
                np.testing.assert_array_equal(actual[name], physical[name])
            assert physical.n_contributing_tracks.dtype == 'uint32'
            assert physical.contributes_TK18.dtype == 'uint8'
            assert physical.attrs['processing_signature'] == row['processing_signature']
    assert [hashes(p) for p in stores] == before


def test_order_window_and_statistics_are_distinct(tmp_path):
    records, _ = monthly_parents(tmp_path)
    definitions = [define(tmp_path, order, method, window=[0, 0, 1, 1])
                   for method in ['first_valid_pixel', 'mean'] for order in [records, records[::-1]]]
    assert len({d['processing_signature'] for d in definitions}) == 4
    for index, (definition, expected) in enumerate(zip(definitions, [1, 9, 5, 5])):
        row = register_view(tmp_path/f'v{index}', definition)
        with CubeResolver(tmp_path/f'v{index}').open(row) as data:
            np.testing.assert_allclose(data.VV_dB[0, 0, 0].compute(), 10*np.log10(expected), atol=1e-6)
            assert data.sizes == {'time': 3, 'y': 1, 'x': 1}
    crop = define(tmp_path, records, window=[1, 0, 1, 1])
    row = register_view(tmp_path/'crop', crop)
    with CubeResolver(tmp_path/'crop').open(row) as ds:
        assert ds.x.item() == 600045
        np.testing.assert_allclose(ds.VV_dB[0].compute(), 10*np.log10(9), atol=1e-6)


@pytest.mark.parametrize('stac_format', ['json', 'geoparquet'])
def test_resync_and_stac(tmp_path, stac_format):
    records, _ = monthly_parents(tmp_path)
    row = register_view(tmp_path/'v', define(tmp_path, records))
    result = resync_catalog_from_filesystem(tmp_path/'v', write_stac=True, stac_format=stac_format)
    assert result['success'], result
    resolver = CubeResolver(tmp_path/'v')
    with resolver.open(resolver.catalog.iloc[0]) as ds:
        assert ds.n_contributing_tracks[0, 0, 0].compute().item() == 2
    if stac_format == 'json':
        check = validate_catalog_integrity(tmp_path/'v', strict=True)
        assert check.valid, check.report()
        item = json.loads((tmp_path/'v/17MPU'/row['item_path']).read_text())
        assert item['properties']['s1grits:representation'] == 'virtual'
        assert 's1:monthly_composite' not in item['properties']  # Do not label a cross-track mean as temporal mean.


@pytest.mark.parametrize('change', ['direction', 'track', 'duplicate_asset', 'grid', 'unverifiable', 'ranges', 'stage', 'duplicate_month', 'mixed', 'interpolated'])
def test_reject_incompatible_parents_before_writes(tmp_path, change):
    records, stores = monthly_parents(tmp_path)
    frame = pd.read_parquet(tmp_path/'catalog/catalog.parquet')
    group = zarr.open_group(str(stores[1]), mode='r+')
    contract = json.loads(group.attrs['processing_variant_json'])
    if change == 'direction':
        frame.loc[1, 'flight_direction'] = 'DESCENDING'
    elif change == 'track':
        frame.loc[1, 'track'] = 18
    elif change == 'duplicate_asset':
        frame.loc[1, 'zarr_path'] = frame.loc[0, 'zarr_path']
    elif change == 'grid':
        frame.loc[1, 'grid_id'] = 'wrong'
    elif change == 'unverifiable':
        contract = {'processing.monthly.composite_method': 'mean'}
    elif change == 'ranges':
        contract['start_utc'] = '2026-01-02T00:00:00+00:00'
    elif change == 'stage':
        contract['monthly'] = 'median'
    elif change == 'duplicate_month':
        group['time'].resize((2,))
        group['time'][:] = np.array(['2026-01-01', '2026-01-02'], 'datetime64[ns]')
        for name in ['VV_dB', 'VH_dB', 'n_obs']:
            group[name].resize((2, 2, 2))
    elif change == 'mixed':
        contract['parent_track'] = None
    elif change == 'interpolated':
        contract['temporal_interpolation'] = {'method': 'linear'}
    if change in ('unverifiable', 'ranges', 'stage', 'mixed', 'interpolated'):
        signature = make_processing_signature(contract)
        group.attrs.update(processing_variant_json=json.dumps(contract), processing_signature=signature)
        frame.loc[1, 'processing_signature'] = signature
    frame.to_parquet(tmp_path/'catalog/catalog.parquet')
    before = hashes(tmp_path)
    with pytest.raises(ValueError):
        define(tmp_path, records)
    assert hashes(tmp_path) == before


def test_parent_update_definition_tamper_and_migration(tmp_path):
    records, stores = monthly_parents(tmp_path/'bundle')
    row = register_view(tmp_path/'bundle/v', define(tmp_path/'bundle', records))
    saved = materialize_view(tmp_path/'bundle/v', row['item_id'], tmp_path/'entity')
    shutil.move(str(tmp_path/'bundle'), str(tmp_path/'moved'))
    resolver = CubeResolver(tmp_path/'moved/v')
    with resolver.open(row) as ds:
        assert ds.n_contributing_tracks[0, 0, 0].compute().item() == 2
    path = resolver._asset_path(row)
    definition = json.loads(path.read_text())
    definition['contract']['method'] = 'first_valid_pixel'
    path.write_text(json.dumps(definition))
    with pytest.raises(ValueError, match='signature'):
        resolver.open(row)
    definition['contract']['method'] = 'mean'
    path.write_text(json.dumps(definition))
    store = tmp_path/'moved'/stores[1].relative_to(tmp_path/'bundle')
    zarr.open_group(str(store), mode='r+').attrs['generation_id'] = 'changed'
    with pytest.raises(ValueError, match='Parent version'):
        resolver.open(row)
    before = hashes(tmp_path/'moved/v')
    assert not resync_catalog_from_filesystem(tmp_path/'moved/v', write_stac=False)['success']
    assert hashes(tmp_path/'moved/v') == before
    with CubeResolver(tmp_path/'entity').open(saved) as ds:
        np.testing.assert_allclose(ds.VV_dB[0, 0, 0], 10*np.log10(5), atol=1e-6)


def test_cli(tmp_path):
    records, _ = monthly_parents(tmp_path)
    common = [sys.executable, '-m', 's1grits.cli', 'view']
    result = subprocess.run(common+['multitrack', '--catalog-root', str(tmp_path/'catalog'),
        '--item-ids', *[r['item_id'] for r in records], '--output', str(tmp_path/'v'),
        '--start-month', '2026-01', '--end-month', '2026-03', '--method', 'mean'],
        capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode == 0, result.stdout+result.stderr
    row = CubeResolver(tmp_path/'v').catalog.iloc[0]
    result = subprocess.run(common+['materialize', '--catalog-root', str(tmp_path/'v'),
        '--item-id', row.item_id, '--output', str(tmp_path/'m'), '--chunk-size', '1'],
        capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode == 0, result.stdout+result.stderr
    with CubeResolver(tmp_path/'m').open(CubeResolver(tmp_path/'m').catalog.iloc[0]) as ds:
        assert ds.n_contributing_tracks[0, 0, 0].item() == 2


def test_second_parent_is_protected_and_legacy_version_evidence_is_explicit(tmp_path):
    records, stores = monthly_parents(tmp_path)
    group = zarr.open_group(str(stores[1]), mode='r+')
    del group.attrs['generation_id']
    row = register_view(tmp_path/'v', define(tmp_path, records))
    with pytest.warns(UserWarning, match='no generation_id'):
        with CubeResolver(tmp_path/'v').open(row) as ds:
            assert ds.attrs['parent_version_evidence'] == 'metadata_only'
            assert ds.n_contributing_tracks[0, 0, 0].compute().item() == 2
    before = hashes(stores[1])
    with pytest.raises(ValueError, match='separate'):
        materialize_view(tmp_path/'v', row['item_id'], stores[1]/'nested')
    assert hashes(stores[1]) == before


@pytest.mark.parametrize('start,end', [('2026-03', '2026-01'), ('2026-01-01', '2026-03'), ('2025-12', '2026-03')])
def test_month_selection_is_explicit(tmp_path, start, end):
    records, _ = monthly_parents(tmp_path)
    with pytest.raises(ValueError):
        define_multitrack_view(tmp_path/'catalog', [r['item_id'] for r in records],
                               start_month=start, end_month=end, method='mean')
