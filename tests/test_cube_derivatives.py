"""Small on-disk Cube outputs: named axes, values, provenance and independent reuse."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest
import zarr
from rasterio.transform import from_origin

from s1grits.analysis import derive_cube
from s1grits.analysis.catalog import resync_catalog_from_filesystem
from s1grits.asf_array_processing import compute_glcm_texture_bands, despeckle_2d
from s1grits.canonical_catalog_schema import normalize_catalog_record
from s1grits.resolver import CubeResolver
from s1grits.zarr_encoding import create_zarr_array


def source(root, layout='tyx', count=4):
    path = root/'17MPU/scenes_ASCENDING/zarr/parent.zarr'
    group = zarr.open_group(str(path), mode='w', zarr_format=3)
    transform = from_origin(600000, 9900000, 30, 30)
    group.attrs.update(crs='EPSG:32717', transform=list(transform)[:6])
    if count == 4:
        times = np.array(['2026-01-01', '2026-01-13', '2026-01-25', '2026-03-01'], dtype='datetime64[ns]')
        power = np.array([1, 2, 9, 4], dtype='f4')[:, None, None]
        power = power * np.random.default_rng(42).uniform(.5, 1.5, (24, 30)).astype('f4')
    else:
        times = pd.date_range('2026-01-01', periods=count, freq='h').values
        power = np.ones((count, 2, 2), dtype='f4')
    vv = (10*np.log10(power)).astype('f4')
    vh = (10*np.log10(power*.25)).astype('f4')
    if count == 4:
        vv[0, 0, 0] = 0  # Valid 0 dB with the legacy Zarr storage fill of zero.
        vv[:, 0, 1] = vh[:, 0, 1] = np.nan
        vv[1, 1, 1] = -12345
        vh[0, 2, 2] = -9999
    h, w = vv.shape[1:]
    for name, values in [('x', 600000+(np.arange(w)+.5)*30),
                         ('y', 9900000-(np.arange(h)+.5)*30), ('time', times)]:
        array = create_zarr_array(group, name, data=values, dimension_names=[name])
        array.attrs['_ARRAY_DIMENSIONS'] = [name]
    for name, values in [('VV_dB', vv), ('VH_dB', vh)]:
        dims = ['time', 'y', 'x'] if layout == 'tyx' else ['y', 'x', 'time']
        array = create_zarr_array(group, name, data=values if layout == 'tyx' else values.transpose(1, 2, 0),
                                  fill_value=0, dimension_names=dims)
        array.attrs.update({'_ARRAY_DIMENSIONS': dims,
                            '_FillValue': base64.b64encode(np.array(-12345, dtype='<f8').tobytes()).decode()})
    record = normalize_catalog_record(dict(item_id='parent', tile_id='17MPU', product_type='scenes',
        output_type='scenes', flight_direction='ASCENDING', track=18, geometry_group_id='17MPU_ASCENDING_TK18',
        processing_signature='p_parent', crs='EPSG:32717', transform=list(transform)[:6], width=w, height=h,
        datetime=str(times[0]), start_datetime=str(times[0]), end_datetime=str(times[-1]),
        bands=['VV_dB', 'VH_dB'], zarr_path=path.relative_to(root/'17MPU').as_posix()))
    pd.DataFrame([record]).to_parquet(root/'catalog.parquet')
    return group, vv, vh


def run(root, out, **kwargs):
    record = derive_cube(root, 'parent', out, start=kwargs.pop('start', '2026-01-01'),
                         end=kwargs.pop('end', '2026-03-31'), **kwargs)
    return record, zarr.open_group(str(out/record['tile_id']/record['zarr_path']), mode='r')


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('relative', [False, True])
def test_external_source_store_cannot_contain_derived_output(tmp_path, relative):
    assets, catalog = tmp_path/'assets', tmp_path/'catalog'
    group, _, _ = source(assets)
    store = assets/'17MPU/scenes_ASCENDING/zarr/parent.zarr'
    catalog.mkdir()
    frame = pd.read_parquet(assets/'catalog.parquet')
    frame['zarr_path'] = os.path.relpath(store, catalog) if relative else str(store)
    frame.to_parquet(catalog/'catalog.parquet')
    before = hashes(assets), hashes(catalog)
    with pytest.raises(ValueError, match='referenced source store'):
        run(catalog, store/'inside', features=[])
    assert (hashes(assets), hashes(catalog)) == before
    assert not (store/'inside').exists()
    _, output = run(catalog, tmp_path/'legal', features=[])
    assert output['VV_dB'][0, 0, 0] == group['VV_dB'][0, 0, 0] == 0
    assert (hashes(assets), hashes(catalog)) == before


@pytest.mark.parametrize('layout', ['tyx', 'yxt'])
def test_axes(tmp_path, layout):
    root = tmp_path/'in'
    group, vv, vh = source(root, layout)
    stamp = np.datetime64('2026-01-01T00:00:00.123456789', 'ns')
    group['time'][0] = stamp
    before = hashes(root)
    record, out = run(root, tmp_path/'out', window=[3, 4, 12, 13])
    np.testing.assert_array_equal(out['VV_dB'][:], vv[:, 4:17, 3:15])
    np.testing.assert_array_equal(out['VH_dB'][:], vh[:, 4:17, 3:15])
    np.testing.assert_allclose(out['Ratio'][:], .25, rtol=1e-6)
    np.testing.assert_allclose(out['RVI'][:], .8, rtol=1e-6)
    assert hashes(root) == before
    assert out.attrs['transform'] == [30, 0, 600090, 0, -30, 9899880]
    assert record['processing_signature'] != 'p_parent'
    assert record['datetime'] == pd.Timestamp(stamp)
    assert len(CubeResolver(tmp_path/'out').query(time_start='2026-01-01')) == 1
    with CubeResolver(tmp_path/'out').open(record) as ds:
        assert ds.VV_dB.dims == ('time', 'y', 'x') and ds.sizes['time'] == 4
    assert resync_catalog_from_filesystem(tmp_path/'out', write_stac=True)['success']
    after = pd.read_parquet(tmp_path/'out/catalog.parquet').iloc[0]
    assert after['item_id'] == record['item_id'] and after['track'] == 18
    assert after['processing_signature'] == record['processing_signature']
    assert after['datetime'] == pd.Timestamp(stamp)
    assert len(CubeResolver(tmp_path/'out').query(time_start='2026-01-01')) == 1


@pytest.mark.parametrize('method, expected', [('mean', 4), ('median', 2)])
def test_month(tmp_path, method, expected):
    root = tmp_path/'in'
    group, vv, _ = source(root)
    group['VV_dB'][:, 0, 3] = 10*np.log10(np.array([1, 2, 9, 4], dtype='f4'))
    group['VH_dB'][:, 0, 3] = 0
    record, out = run(root, tmp_path/'out', monthly=method, chunk_size=16)
    # January's linear powers are 1, 2, 9 times the same spatial factor.
    np.testing.assert_allclose(out['VV_dB'][0, 3:, 3:], vv[0, 3:, 3:]+10*np.log10(expected), atol=2e-6)
    np.testing.assert_allclose(out['Ratio'][0, 3:, 3:], .25, atol=1e-6)
    np.testing.assert_allclose(out['Ratio'][0, 0, 3], 1/expected, atol=1e-6)
    assert out['n_obs'].dtype == np.dtype('uint32')
    assert out['n_obs'][0, 3, 3] == 3
    assert out['n_obs'][0, 1, 1] == out['n_obs'][0, 2, 2] == 2
    assert np.isnan(out['VV_dB'][1]).all() and (out['n_obs'][1] == 0).all()
    np.testing.assert_array_equal(out['time'][:], np.array(['2026-01-01', '2026-02-01', '2026-03-01'], 'datetime64[ns]'))
    receipt = json.loads((tmp_path/'out/derivation.json').read_text())
    assert receipt['output_time_sources']['2026-02-01 00:00:00'] == []
    assert method in record['product_variant']
    with CubeResolver(tmp_path/'out').open(record) as ds:
        assert (ds.n_obs.isel(time=1).values == 0).all()


@pytest.mark.parametrize('method', ['none', 'tv_bregman', 'nlm'])
def test_filter(tmp_path, method):
    root = tmp_path/'in'
    _, vv, _ = source(root)
    record, out = run(root, tmp_path/'out', end='2026-01-01', spatial_filter=method, features=[])
    expected = vv[0]
    if method != 'none':
        expected = 10*np.log10(despeckle_2d(10**(expected/10), method=method))
        assert not np.allclose(expected, vv[0], equal_nan=True)
        assert method.replace('_', '') in record['product_variant']
    np.testing.assert_allclose(out['VV_dB'][0], expected, atol=2e-6, equal_nan=True)
    assert np.isnan(out['VV_dB'][0, 0, 1])
    if method == 'none':
        assert out['VV_dB'][0, 0, 0] == 0


def test_glcm(tmp_path):
    root = tmp_path/'in'
    _, vv, vh = source(root)
    results = []
    for chunk in (7, 32):
        record, out = run(root, tmp_path/f'o{chunk}', end='2026-01-01', features=['glcm'],
                           window=[9, 9, 15, 12], chunk_size=chunk)
        results.append((record, out))
    cfg = json.loads(results[0][0]['processing_variant_json'])['glcm_preset']
    vh[vh == -9999] = np.nan
    refs, names = compute_glcm_texture_bands(vv[0], vh[0], cfg)
    for name, ref in zip(names, refs):
        np.testing.assert_array_equal(results[0][1][name][0], ref[9:21, 9:24])
        np.testing.assert_array_equal(results[1][1][name][:], results[0][1][name][:])
    assert results[0][0]['processing_signature'] == results[1][0]['processing_signature']


def test_snapshot(tmp_path):
    root = tmp_path/'in'
    group, _, _ = source(root)
    a, _ = run(root, tmp_path/'a', end='2026-01-01', features=[])
    group['VV_dB'][3, 5, 5] = 99  # Outside the selected snapshot.
    b, _ = run(root, tmp_path/'b', end='2026-01-01', features=[])
    assert a['processing_signature'] == b['processing_signature']
    group['VV_dB'][0, 5, 5] = 99
    c, _ = run(root, tmp_path/'c', end='2026-01-01', features=[])
    assert a['processing_signature'] != c['processing_signature']
    before = hashes(tmp_path/'a')
    chained = derive_cube(tmp_path/'a', a['item_id'], tmp_path/'chain', start='2026-01-01',
                          end='2026-03-01', monthly='mean')
    assert chained['product_type'] == 'smonthly'
    assert hashes(tmp_path/'a') == before


@pytest.mark.parametrize('method', ['tv_bregman', 'nlm'])
def test_combined(tmp_path, method):
    root = tmp_path/'in'
    source(root)
    record, out = run(root, tmp_path/'out', monthly='mean', spatial_filter=method,
                       features=['ratio', 'rvi', 'glcm'], window=[9, 9, 12, 12], chunk_size=7)
    # Reuse the monthly-only product as the independent input to the existing full-support filter/GLCM operators.
    _, parent = run(root, tmp_path/'month', monthly='mean', features=[], chunk_size=32)
    refs = [10*np.log10(despeckle_2d(10**(parent[b][0, 1:24, 1:29]/10), method=method))
            for b in ('VV_dB', 'VH_dB')]
    cfg = json.loads(record['processing_variant_json'])['glcm_preset']
    textures, names = compute_glcm_texture_bands(*refs, cfg)
    for band, values in zip(['VV_dB', 'VH_dB']+names, refs+textures):
        np.testing.assert_allclose(out[band][0], values[8:20, 8:20], atol=2e-6, rtol=0)
        assert np.isnan(out[band][1]).all()
    np.testing.assert_allclose(out['Ratio'][0], 10**((out['VH_dB'][0]-out['VV_dB'][0])/10), atol=1e-6)
    assert (out['n_obs'][1] == 0).all()


def test_count(tmp_path):
    root = tmp_path/'in'
    source(root, count=300)
    _, out = run(root, tmp_path/'out', monthly='mean', features=[], end='2026-01-31')
    assert (out['n_obs'][:] == 300).all()


def test_cli(tmp_path):
    root = tmp_path/'in'
    source(root)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]/'src'))
    proc = subprocess.run([sys.executable, '-m', 's1grits', 'derive_cube', '--catalog-root', str(root),
        '--item-id', 'parent', '--output', str(tmp_path/'cli'), '--start', '2026-01-01', '--end', '2026-01-01',
        '--window', '3', '3', '4', '4', '--features', 'ratio'], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout+proc.stderr
    record = pd.read_parquet(tmp_path/'cli/catalog.parquet').iloc[0]
    with CubeResolver(tmp_path/'cli').open(record) as ds:
        np.testing.assert_allclose(ds.Ratio.values, .25, atol=1e-6)


def test_reject(tmp_path):
    root = tmp_path/'in'
    source(root)
    for kwargs in [dict(window=[0, 0, 31, 24]), dict(start='2027-01-01'), dict(monthly='sum'),
                   dict(start='2026-02-01', end='2026-02-28'),
                   dict(features=['unknown']), dict(spatial_filter='unknown')]:
        with pytest.raises(ValueError):
            run(root, tmp_path/'bad', **kwargs)
        assert not (tmp_path/'bad').exists()
    with pytest.raises(ValueError, match='separate'):
        run(root, root)
    run(root, tmp_path/'out', end='2026-01-01', features=[])
    before = hashes(tmp_path/'out')
    with pytest.raises(FileExistsError):
        run(root, tmp_path/'out')
    assert hashes(tmp_path/'out') == before


def test_partial(tmp_path):
    root = tmp_path/'out'
    group = zarr.open_group(str(root/'17MPU/scenes_ASCENDING_derived_p1/zarr/partial.zarr'), mode='w')
    group.attrs['status'] = 'building'
    assert not resync_catalog_from_filesystem(root, write_stac=False)['success']
    assert not (root/'catalog.parquet').exists()
