"""Small real stores for CRS pairing, Web missing values and batch identity."""
import numpy as np
import pandas as pd
import pytest
from pyproj import CRS, Transformer
import zarr

from s1grits.analysis import analyze_segments_batch
from s1grits.resolver import CubeResolver
from test_cube_derivatives import source, hashes
from test_cube_segmentation import parent
from test_resolver_selection import product, resolver, TILE
from test_segmentation_batch import OPTIONS


@pytest.mark.parametrize('entry', ['stack', 'training'])
@pytest.mark.parametrize('same_crs', [False, True])
def test_pairing_checks_crs_before_equal_coordinates(tmp_path, entry, same_crs):
    root = tmp_path/'in'
    dynamic = product(root, 'dynamic')
    static = product(root, 'static', kind='static', band='angle', value=42)
    group = zarr.open_group(str(root/TILE/'static.zarr'), mode='r+')
    group.attrs['crs'] = CRS.from_epsg(32717).to_wkt() if same_crs else 'EPSG:32718'
    r = resolver(root, [dynamic, static])
    before = hashes(root)
    out = tmp_path/'training.zarr'
    if not same_crs:
        with pytest.raises(ValueError, match='CRS'):
            r.open_stack(TILE) if entry == 'stack' else r.materialize_training_cube(TILE, out)
        assert not out.exists()
    else:
        ds = r.open_stack(TILE) if entry == 'stack' else CubeResolver.open_training_cube(r.materialize_training_cube(TILE, out))
        np.testing.assert_array_equal(ds.angle, np.full((2, 2), 42))
    assert hashes(root) == before


def test_manual_read_keeps_crs_and_scalar_metadata(tmp_path):
    row = product(tmp_path, 'static', kind='static', band='angle', value=42)
    path = tmp_path/TILE/'static.zarr'
    group = zarr.open_group(str(path), mode='r+')
    group.attrs.update(crs='EPSG:32717', generation_id='test-generation')
    group.create_array('spatial_ref', data=np.array(0), dimension_names=[])
    before = hashes(tmp_path)
    ds = CubeResolver(tmp_path)._open_manual(str(path), row)
    assert ds.attrs['crs'] == 'EPSG:32717'
    assert ds.attrs['generation_id'] == 'test-generation'
    np.testing.assert_array_equal(ds.angle, np.full((2, 2), 42))
    assert ds.spatial_ref.item() == 0
    assert hashes(tmp_path) == before


@pytest.mark.parametrize('layout', ['tyx', 'yxt'])
def test_web_named_axes_nodata_and_valid_zero(tmp_path, layout):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient
    from s1grits.webapp.server import create_app
    group, _, _ = source(tmp_path, layout)
    values = np.array([0, -12345, -9999, np.nan], dtype='f4')
    if layout == 'tyx':
        group['VV_dB'][:, 5, 5] = values
    else:
        group['VV_dB'][5, 5, :] = values
    lon, lat = Transformer.from_crs(group.attrs['crs'], 4326, always_xy=True).transform(group['x'][5], group['y'][5])
    row = CubeResolver(tmp_path).catalog.iloc[0]
    before = hashes(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get('/api/timeseries', params=dict(tile=TILE, zarr_path=row.zarr_path,
                              lon=lon, lat=lat, bands='VV_dB'))
    assert response.status_code == 200, response.text
    assert response.json()['bands']['VV_dB'] == [0., None, None, None]
    # The app may create its own job metadata; the source store must stay intact.
    after = hashes(tmp_path)
    assert all(after[name] == value for name, value in before.items())


def test_batch_identity_ignores_location_but_tracks_values_and_parameters(tmp_path):
    pytest.importorskip('landtrendr')
    root = tmp_path/'in'
    group, _ = parent(root)
    def run(label, **options):
        return analyze_segments_batch(root, 'parent', tmp_path/label, window=[0, 0, 1, 1], **(OPTIONS | options))
    a = run('a')
    catalog = pd.read_parquet(root/'catalog.parquet')
    catalog['zarr_path'] = str(CubeResolver(root)._asset_path(catalog.iloc[0]).resolve())
    catalog['created_at'] = '2030-01-01'
    catalog.to_parquet(root/'catalog.parquet', index=False)
    before = hashes(root)
    b = run('b')
    assert a['processing_signature'] == b['processing_signature']
    assert b['parent_reference_item']['zarr_path'] == catalog.iloc[0].zarr_path
    assert b['contract']['contract_version'] == 'local_lt_batch_v2'
    assert hashes(root) == before
    group['VV_dB'][0, 0, 0] += 3
    c = run('c')
    d = run('d', annual_method='mean')
    assert len({a['processing_signature'], c['processing_signature'], d['processing_signature']}) == 3
