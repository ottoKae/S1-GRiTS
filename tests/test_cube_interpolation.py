"""Tiny on-disk time series: gap limits, true dates, masks and independent outputs."""
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest
import zarr

from test_cube_derivatives import source, hashes
from s1grits.analysis import derive_cube
from s1grits.analysis.catalog import resync_catalog_from_filesystem
from s1grits.resolver import CubeResolver


def parent(root, layout='tyx'):
    group, vv, vh = source(root, layout)
    group['time'][:] = np.array(['2026-01-01', '2026-01-02', '2026-01-04', '2026-01-08'], 'datetime64[ns]')
    vv[:, 0, 0] = [0, np.nan, 10, 20]
    vh[:, 0, 0] = [0, 10, np.nan, 20]
    vv[:, 0, 1] = np.nan
    vv[:, 0, 2] = [np.nan, 5, np.nan, np.nan]
    vv[:, 0, 3] = [np.nan, 3, 4, np.nan]
    for col, missing in [(4, -9999), (5, -12345), (6, np.inf)]:
        vv[:, 0, col] = [0, missing, 10, 20]
    vv[:, 1, 0] = [0, np.nan, np.nan, 20]
    for band, values in [('VV_dB', vv), ('VH_dB', vh)]:
        group[band][:] = values if layout == 'tyx' else values.transpose(1, 2, 0)
    return group, vv, vh


def run(root, dest, **kwargs):
    record = derive_cube(root, 'parent', dest, start=kwargs.pop('start', '2026-01-01'),
        end=kwargs.pop('end', '2026-01-08'), features=[], window=kwargs.pop('window', [0, 0, 8, 3]),
        interpolation=kwargs.pop('interpolation', 'linear'),
        interpolation_domain=kwargs.pop('interpolation_domain', 'db'),
        max_gap_days=kwargs.pop('max_gap_days', 6), **kwargs)
    return record, zarr.open_group(str(dest/record['tile_id']/record['zarr_path']), mode='r')


@pytest.mark.parametrize('domain', ['db', 'linear_power'])
@pytest.mark.parametrize('gap', [2.9, 3, 6])
def test_values(tmp_path, domain, gap):
    root = tmp_path/'in'
    group, vv, _ = parent(root)
    before = hashes(root)
    record, out = run(root, tmp_path/'out', interpolation_domain=domain, max_gap_days=gap)
    expected_vv = 10/3 if domain == 'db' else 10*np.log10(4)
    expected_vh = 40/3 if domain == 'db' else 10*np.log10(40)
    for col in (0, 4, 5, 6):
        if gap >= 3:
            np.testing.assert_allclose(out['VV_dB'][1, 0, col], expected_vv, atol=1e-6)
        else:
            assert np.isnan(out['VV_dB'][1, 0, col])
        assert out['VV_observed_mask'][1, 0, col] == 0
        assert out['VV_interpolated_mask'][1, 0, col] == int(gap >= 3)
    if gap >= 6:
        np.testing.assert_allclose(out['VH_dB'][2, 0, 0], expected_vh, atol=1e-6)
    else:
        assert np.isnan(out['VH_dB'][2, 0, 0])
    assert np.isnan(out['VV_dB'][:, 0, 1]).all()
    np.testing.assert_array_equal(out['VV_dB'][:, 0, 2], [np.nan, 5, np.nan, np.nan])
    np.testing.assert_array_equal(out['VV_dB'][:, 0, 3], [np.nan, 3, 4, np.nan])
    assert np.isnan(out['VV_dB'][1:3, 1, 0]).all()  # Seven-day span exceeds every tested limit.
    observed = np.isfinite(vv[:, :3, :8]) & ~np.isin(vv[:, :3, :8], [-9999, -12345])
    np.testing.assert_array_equal(out['VV_observed_mask'][:], observed.astype('u1'))
    np.testing.assert_array_equal(out['VV_dB'][:][observed], vv[:, :3, :8][observed])
    assert not (out['VV_observed_mask'][:].astype(bool) & out['VV_interpolated_mask'][:].astype(bool)).any()
    np.testing.assert_array_equal(out['time'][:], group['time'][:])
    assert hashes(root) == before and 'n_obs' not in out
    contract = json.loads(record['processing_variant_json'])
    assert contract['temporal_interpolation']['mode'] == 'retrospective'
    assert contract['temporal_interpolation']['domain'] == domain
    receipt = json.loads((tmp_path/'out/derivation.json').read_text())
    assert receipt['output_time_sources'] is None
    assert contract['temporal_interpolation']['anchor_selection'] == 'nearest_valid_before_and_after_per_band_pixel'
    with CubeResolver(tmp_path/'out').open(record) as ds:
        np.testing.assert_array_equal(ds.VV_observed_mask.values, observed.astype('u1'))
    assert resync_catalog_from_filesystem(tmp_path/'out', write_stac=False)['success']
    assert len(CubeResolver(tmp_path/'out').query(time_start='2026-01-01')) == 1


def test_identity(tmp_path):
    root = tmp_path/'in'
    _, vv, _ = parent(root)
    record, out = run(root, tmp_path/'out', interpolation='none', max_gap_days=None)
    vv[(vv == -9999) | (vv == -12345) | ~np.isfinite(vv)] = np.nan
    np.testing.assert_array_equal(out['VV_dB'][:], vv[:, :3, :8])
    assert not out['VV_interpolated_mask'][:].any()
    assert json.loads(record['processing_variant_json'])['temporal_interpolation']['mode'] == 'identity'


@pytest.mark.parametrize('layout', ['tyx', 'yxt'])
def test_chunks(tmp_path, layout):
    root = tmp_path/'in'
    parent(root, layout)
    a, first = run(root, tmp_path/'a', chunk_size=2)
    b, second = run(root, tmp_path/'b', chunk_size=32)
    assert a['processing_signature'] == b['processing_signature']
    for band in json.loads(a['bands']):
        np.testing.assert_array_equal(first[band][:], second[band][:])
    c, _ = run(root, tmp_path/'c', interpolation_domain='linear_power')
    d, _ = run(root, tmp_path/'d', max_gap_days=3)
    assert len({a['processing_signature'], c['processing_signature'], d['processing_signature']}) == 3


def test_interval(tmp_path):
    root = tmp_path/'in'
    group, _, _ = parent(root)
    group['time'][:] = np.array(['2024-02-28', '2024-02-29', '2024-03-01', '2024-03-05'], 'datetime64[ns]')
    _, out = run(root, tmp_path/'all', start='2024-02-28', end='2024-03-05')
    assert out['VV_dB'][1, 0, 0] == 5  # Leap day is halfway between anchors.
    _, selected = run(root, tmp_path/'selected', start='2024-02-29', end='2024-03-01')
    assert np.isnan(selected['VV_dB'][0, 0, 0])  # No anchor borrowed from outside the requested interval.
    _, single = run(root, tmp_path/'single', start='2024-03-01', end='2024-03-01')
    assert single['VV_dB'].shape[0] == 1 and not single['VV_interpolated_mask'][:].any()


def test_long_series(tmp_path):
    root = tmp_path/'in'
    group, _, _ = source(root, count=300)
    group['VV_dB'][50:70, 0, 0] = np.nan
    group['VV_dB'][100:180, 0, 0] = np.nan
    _, out = run(root, tmp_path/'out', end='2026-01-31', max_gap_days=1, window=[0, 0, 2, 2])
    assert out['VV_dB'].shape == (300, 2, 2)
    assert (out['VV_dB'][50:70, 0, 0] == 0).all()
    assert np.isnan(out['VV_dB'][100:180, 0, 0]).all()
    assert out['VV_interpolated_mask'][:].sum() == 20
    assert out['VV_observed_mask'][:].sum() == 1100


def test_reject(tmp_path):
    root = tmp_path/'in'
    parent(root)
    for kwargs in [dict(max_gap_days=None), dict(max_gap_days=0), dict(max_gap_days=np.inf),
        dict(interpolation='cubic'), dict(interpolation_domain=None), dict(monthly='mean'),
        dict(spatial_filter='tv_bregman'), dict(interpolation='none'), dict(interpolation=None)]:
        with pytest.raises(ValueError):
            run(root, tmp_path/'bad', **kwargs)
        assert not (tmp_path/'bad').exists()
    rec, _ = run(root, tmp_path/'out')
    with pytest.raises(ValueError, match='pre-interpolation'):
        derive_cube(tmp_path/'out', rec['item_id'], tmp_path/'again', start='2026-01-01', end='2026-01-08',
            features=[], interpolation='linear', interpolation_domain='db', max_gap_days=6)
    assert not (tmp_path/'again').exists()


def test_cli(tmp_path):
    root = tmp_path/'in'
    parent(root)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]/'src'))
    proc = subprocess.run([sys.executable, '-m', 's1grits', 'derive_cube', '--catalog-root', str(root),
        '--item-id', 'parent', '--output', str(tmp_path/'out'), '--start', '2026-01-01', '--end', '2026-01-08',
        '--features', '--interpolation', 'linear', '--interpolation-domain', 'db', '--max-gap-days', '6',
        '--window', '0', '0', '8', '3'], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout+proc.stderr
    record = pd.read_parquet(tmp_path/'out/catalog.parquet').iloc[0]
    with CubeResolver(tmp_path/'out').open(record) as ds:
        np.testing.assert_allclose(ds.VV_dB.values[1, 0, 0], 10/3, atol=1e-6)
        assert ds.VV_interpolated_mask.values[1, 0, 0] == 1


@pytest.mark.parametrize('features', [[], ['ratio', 'rvi']])
@pytest.mark.parametrize('crop', [False, True])
def test_derivative_preserves_masks_and_analysis_observations(tmp_path, features, crop):
    from s1grits.analysis.segmentation import _read_point
    root, filled = tmp_path/'in', tmp_path/'filled'
    parent(root)
    rec, interpolated = run(root, filled)
    before = hashes(root), hashes(filled)
    start, end = ('2026-01-02', '2026-01-04') if crop else ('2026-01-01', '2026-01-08')
    window = [4, 0, 2, 1] if crop else [0, 0, 8, 3]
    dest = tmp_path/'derived'
    result = derive_cube(filled, rec['item_id'], dest, start=start, end=end,
                         features=features, window=window, chunk_size=1)
    output = zarr.open_group(str(dest/result['tile_id']/result['zarr_path']), mode='r')
    x, y, w, h = window
    time_slice = slice(1, 3) if crop else slice(None)
    bands = ['VV_dB', 'VH_dB'] + [f'{p}_{kind}_mask' for p in ('VV', 'VH')
                                 for kind in ('observed', 'interpolated')]
    for name in bands:
        np.testing.assert_array_equal(output[name][:], interpolated[name][time_slice, y:y+h, x:x+w])
        assert name in json.loads(result['bands'])
    frame, _, lineage, _ = _read_point(dest, result['item_id'], tmp_path/'analysis',
                                       'VV_dB', [0, 0], start, end)
    assert frame.value_db.notna().sum() == (1 if crop else 3)
    assert lineage['observed_mask'] == 'VV_observed_mask'
    assert (hashes(root), hashes(filled)) == before
    assert resync_catalog_from_filesystem(dest, write_stac=False)['success']
    with CubeResolver(dest).open(CubeResolver(dest).catalog.iloc[0]) as ds:
        np.testing.assert_array_equal(ds.VV_observed_mask, output['VV_observed_mask'][:])


@pytest.mark.parametrize('options', [dict(monthly='mean'), dict(monthly='median'),
    dict(spatial_filter='tv_bregman'), dict(spatial_filter='nlm'), dict(features=['glcm'])])
def test_masked_parent_rejects_undefined_mask_propagation(tmp_path, options):
    root, filled = tmp_path/'in', tmp_path/'filled'
    parent(root)
    rec, _ = run(root, filled)
    before = hashes(filled)
    with pytest.raises(ValueError, match='mask propagation'):
        derive_cube(filled, rec['item_id'], tmp_path/'bad', start='2026-01-01', end='2026-01-08',
                    **{'features': [], **options})
    assert not (tmp_path/'bad').exists()
    assert hashes(filled) == before


def test_mask_only_change_changes_identity_but_chunk_size_does_not(tmp_path):
    root, filled = tmp_path/'in', tmp_path/'filled'
    parent(root)
    rec, _ = run(root, filled)
    signatures = []
    for label, chunk in [('a', 1), ('b', 32), ('changed', 32)]:
        if label == 'changed':
            group = zarr.open_group(str(filled/rec['tile_id']/rec['zarr_path']), mode='r+')
            group['VV_observed_mask'][1, 0, 0] = 1
            group['VV_interpolated_mask'][1, 0, 0] = 0
        result = derive_cube(filled, rec['item_id'], tmp_path/label,
                             start='2026-01-01', end='2026-01-08', features=[], chunk_size=chunk)
        signatures.append(result['processing_signature'])
    assert signatures[0] == signatures[1] != signatures[2]


@pytest.mark.parametrize('problem', ['missing_observed', 'nonbinary', 'dimensions'])
def test_invalid_parent_masks_fail_before_output(tmp_path, problem):
    root, filled = tmp_path/'in', tmp_path/'filled'
    parent(root)
    rec, _ = run(root, filled)
    group = zarr.open_group(str(filled/rec['tile_id']/rec['zarr_path']), mode='r+')
    if problem == 'missing_observed':
        del group['VV_observed_mask']
    elif problem == 'nonbinary':
        group['VV_observed_mask'][0, 0, 0] = 2
    else:
        del group['VV_observed_mask']
        group.create_array('VV_observed_mask', data=np.ones((3, 8), dtype='u1'), dimension_names=['y', 'x'])
    before = hashes(filled)
    with pytest.raises(ValueError, match='mask'):
        derive_cube(filled, rec['item_id'], tmp_path/'bad', start='2026-01-01', end='2026-01-08', features=[])
    assert not (tmp_path/'bad').exists()
    assert hashes(filled) == before


def test_observed_only_yxt_mask_survives_repeated_derivation(tmp_path):
    from s1grits.analysis.segmentation import _read_point
    from s1grits.zarr_encoding import create_zarr_array
    root = tmp_path/'in'
    group, vv, _ = source(root, layout='yxt')
    observed = np.ones(vv.shape, dtype='u1')
    observed[1, 0, 0] = 0
    create_zarr_array(group, 'VV_observed_mask', data=observed.transpose(1, 2, 0),
                      dimension_names=['y', 'x', 'time'])
    item = 'parent'
    for label in ('first', 'second'):
        before = hashes(root)
        destination = tmp_path/label
        record = derive_cube(root, item, destination, start='2026-01-01', end='2026-03-01',
                             features=[], window=[0, 0, 2, 2])
        assert hashes(root) == before
        with CubeResolver(destination).open(record) as ds:
            np.testing.assert_array_equal(ds.VV_observed_mask, observed[:, :2, :2])
        root, item = destination, record['item_id']
    frame, _, _, _ = _read_point(root, item, tmp_path/'analysis', 'VV_dB', [0, 0],
                                 '2026-01-01', '2026-03-01')
    assert frame.value_db.notna().sum() == 3
    with pytest.raises(ValueError, match='pre-interpolation'):
        derive_cube(root, item, tmp_path/'refill', start='2026-01-01', end='2026-03-01',
                    features=[], interpolation='linear', interpolation_domain='db', max_gap_days=30)
    assert not (tmp_path/'refill').exists()
