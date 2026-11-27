"""Real small Zarr groups and virtual products through the public read API."""
import shutil

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from s1grits.analysis import define_view, register_view
from s1grits.webapp.server import create_app
from s1grits.zarr_encoding import create_zarr_array
from test_cube_derivatives import hashes, source
from test_cube_views import sample, OPTIONS
from test_zarr_group_access import grouped as grouped
from test_multitrack_views import monthly_parents, define


def test_group_identity_values_and_no_writes(grouped, tmp_path):
    root, rows, _ = grouped
    workspace = tmp_path/'web'
    workspace.mkdir()
    client = TestClient(create_app(workspace, catalog_roots=[str(root)]))
    root_id = next(k for k, v in client.app.state.console._catalog_roots.items() if v['path'] == root)
    before = hashes(root)
    for name, expected in [('a', 0), ('b', 9)]:
        params = dict(root_id=root_id, item_id=name)
        detail = client.get('/api/catalog/product', params=params)
        assert detail.status_code == 200, detail.text
        assert detail.json()['attributes']['generation_id'] == f'gen-{name}'
        assert detail.json()['record']['zarr_group'] == f'products/{name}'
        result = client.get('/api/catalog/sample', params={**params, 'band': 'VV_dB'}).json()
        assert result['sample']['values'][0] == expected
    missing = client.get('/api/catalog/sample', params=dict(root_id=root_id, item_id='a', band='VV_dB', col=1))
    assert missing.json()['sample']['values'] == [None]*4
    fill = client.get('/api/catalog/sample', params=dict(root_id=root_id, item_id='a', band='VV_dB', row=1, col=1))
    assert fill.json()['sample']['values'][1] is None
    assert hashes(root) == before


@pytest.mark.parametrize('layout', ['tyx', 'yxt'])
def test_view_read_masks_and_parent_generation(tmp_path, layout):
    parent = tmp_path/'parent'
    group = sample(parent, layout, masked=True)
    record = register_view(tmp_path/'view', define_view(parent, 'parent', bands=['VV_dB', 'RVI'], **OPTIONS))
    before = hashes(tmp_path)
    client = TestClient(create_app(tmp_path))
    params = dict(output='view', item_id=record['item_id'])
    detail = client.get('/api/catalog/product', params=params)
    assert detail.status_code == 200, detail.text
    assert detail.json()['record']['representation'] == 'virtual'
    assert detail.json()['sizes']['x'] == 2
    for band in ['VV_dB', 'RVI', 'VV_observed_mask', 'VV_interpolated_mask']:
        response = client.get('/api/catalog/sample', params={**params, 'band': band, 'start': '2026-01-13', 'end': '2026-01-25'})
        assert response.status_code == 200, response.text
        assert len(response.json()['sample']['values']) == 2
        if band == 'VV_interpolated_mask':
            assert response.json()['sample']['values'] == [0, 1]
    # Job-manager setup is workspace state, not an input modification.
    assert {k: v for k, v in hashes(tmp_path).items() if k in before} == before
    group.attrs['generation_id'] = 'g2'
    response = client.get('/api/catalog/product', params=params)
    assert response.status_code == 400
    assert 'changed' in response.text


def test_view_outside_registered_roots_is_rejected(tmp_path):
    parent, workspace = tmp_path/'parent', tmp_path/'web'
    source(parent)
    record = register_view(workspace/'view', define_view(parent, 'parent', **OPTIONS))
    params = dict(output='view', item_id=record['item_id'], band='VV_dB')
    client = TestClient(create_app(workspace))
    assert client.get('/api/catalog/sample', params=params).status_code == 403
    client = TestClient(create_app(workspace, catalog_roots=[str(parent)]))
    result = client.get('/api/catalog/sample', params=params)
    assert result.status_code == 200, result.text
    assert result.json()['warnings']
    assert result.json()['sample']['values'][0] == 0


def test_filters_pagination_and_legacy_fields(grouped):
    root, rows, _ = grouped
    rows = [dict(rows[i % 2], item_id=f'item-{i:04}') for i in range(503)]
    pd.DataFrame(rows).to_parquet(root/'catalog.parquet')
    tile = root/'17MPU'
    tile.mkdir()
    shutil.copyfile(root/'catalog.parquet', tile/'catalog.parquet')
    client = TestClient(create_app(root))
    first = client.get('/api/catalog').json()
    last = client.get('/api/catalog', params={'offset': 500}).json()
    assert (first['total'], first['returned'], last['returned']) == (503, 500, 3)
    assert not {x['item_id'] for x in first['records']} & {x['item_id'] for x in last['records']}
    params = dict(track=18, processing_signature='p_b', representation='physical', limit=2, offset=1)
    filtered = client.get('/api/catalog', params=params).json()
    assert filtered['total'] == 251
    assert all(row['zarr_group'] == 'products/b' for row in filtered['records'])
    assert client.get('/api/catalog/map', params=params).json()['total_records'] == 251
    legacy = client.get('/api/items', params=params).json()
    assert legacy['total'] == 251
    assert all(row['zarr_group'] == 'products/b' for row in legacy['items'])
    assert client.get('/api/catalog', params={'offset': -1}).status_code == 422
    assert client.get('/api/catalog', params={'representation': 'virtual'}).json()['total'] == 0


@pytest.mark.parametrize('params', [dict(item_id='absent'), dict(item_id='a', band='unknown'),
    dict(item_id='a', band='VV_dB', row=24), dict(item_id='a', band='VV_dB', start='bad'),
    dict(item_id='a', band='VV_dB', start='2026-03-01', end='2026-01-01')])
def test_invalid_selections(grouped, params):
    root, _, _ = grouped
    response = TestClient(create_app(root)).get('/api/catalog/sample', params=params)
    assert response.status_code == 400, response.text


def test_duplicate_identity_and_group_escape(grouped):
    root, rows, _ = grouped
    client = TestClient(create_app(root))
    pd.DataFrame([rows[0], rows[0]]).to_parquet(root/'catalog.parquet')
    assert client.get('/api/catalog/product', params={'item_id': 'a'}).status_code == 400
    rows[0]['zarr_group'] = '../outside'
    pd.DataFrame(rows).to_parquet(root/'catalog.parquet')
    assert client.get('/api/catalog/product', params={'item_id': 'a'}).status_code == 403


def test_static_band_and_nonfinite_values(tmp_path):
    group, _, _ = source(tmp_path)
    del group['time']
    del group['VV_dB']
    del group['VH_dB']
    values = np.zeros((24, 30), dtype='f4')
    values[0, 1] = np.inf
    values[0, 2] = -9999
    create_zarr_array(group, 'static_band', data=values, dimension_names=['y', 'x'])
    frame = pd.read_parquet(tmp_path/'catalog.parquet')
    frame['product_type'] = 'static'
    frame.to_parquet(tmp_path/'catalog.parquet')
    client = TestClient(create_app(tmp_path))
    params = dict(item_id='parent', band='static_band')
    result = client.get('/api/catalog/sample', params=params).json()
    assert result['sample']['times'] is None
    assert result['sample']['values'] == [0]
    for col in [1, 2]:
        assert client.get('/api/catalog/sample', params={**params, 'col': col}).json()['sample']['values'] == [None]
    assert client.get('/api/catalog/sample', params={**params, 'start': '2026-01-01'}).status_code == 400


@pytest.mark.parametrize('method,power,contributors', [('mean', 5, 2), ('first_valid_pixel', 1, 1)])
def test_multitrack_point_and_lazy_metadata(tmp_path, method, power, contributors):
    from dask.callbacks import Callback
    records, stores = monthly_parents(tmp_path)
    view = register_view(tmp_path/'view', define(tmp_path, records, method))
    client = TestClient(create_app(tmp_path))
    params = dict(output='view', item_id=view['item_id'])
    before = [hashes(p) for p in stores]
    executed = []
    with Callback(posttask=lambda *args: executed.append(args[0])):
        detail = client.app.state.console.catalog_product('view', view['item_id'])
    assert not executed
    assert detail['record']['track'] is None
    values = client.get('/api/catalog/sample', params={**params, 'band': 'VV_dB'}).json()['sample']['values']
    assert values[0] == pytest.approx(10*np.log10(power), abs=2e-6)
    assert values[1] is None
    counts = client.get('/api/catalog/sample', params={**params, 'band': 'n_contributing_tracks'}).json()['sample']['values']
    assert counts == [contributors, 0, 1]
    assert [hashes(p) for p in stores] == before


def test_whole_end_date_is_included(tmp_path):
    source(tmp_path, count=30)
    client = TestClient(create_app(tmp_path))
    result = client.get('/api/catalog/sample', params=dict(item_id='parent', band='VV_dB', start='2026-01-01', end='2026-01-01'))
    assert result.status_code == 200, result.text
    assert len(result.json()['sample']['values']) == 24
