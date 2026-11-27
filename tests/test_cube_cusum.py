"""On-disk public-backend comparisons; no detector implementation in these tests."""
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest
import xarray as xr
import zarr

from test_cube_derivatives import source, hashes
from s1grits.analysis import analyze_cusum
from s1grits.zarr_encoding import create_zarr_array

pytest.importorskip('nrt')
from nrt.monitor.cusum import CuSum


def parent(root, layout='tyx'):
    group, _, _ = source(root, layout, count=90)
    times = pd.date_range('2020-01-01', periods=90, freq='12D')
    group['time'][:] = times.values
    values = np.broadcast_to((-12+np.tile([-.2, .2], 45))[:, None, None], (90, 2, 2)).astype('f4').copy()
    values[65:, 0, 1] -= 4
    values[:-2, 1, 0] = np.nan
    values[:, 1, 1] = np.nan
    group['VV_dB'][:] = values if layout == 'tyx' else values.transpose(1, 2, 0)
    return group, values, times


def run(root, dest, **kwargs):
    receipt = analyze_cusum(root, 'parent', dest, band='VV_dB',
        history_start='2020-01-01', history_end='2021-12-09T23:59:59Z',
        monitor_start=kwargs.pop('monitor_start', '2021-12-10'), monitor_end='2023-01-01',
        trend=kwargs.pop('trend', False), harmonic_order=kwargs.pop('harmonic_order', 0), **kwargs)
    return receipt, zarr.open_group(str(dest/'cusum.zarr'), mode='r')


@pytest.mark.parametrize('layout', ['tyx', 'yxt'])
def test_public_backend_and_chunks(tmp_path, layout):
    root = tmp_path/'parent'
    _, values, times = parent(root, layout)
    before = hashes(root)
    split = 60
    model = CuSum(trend=False, harmonic_order=0, mask=np.array([[1, 1], [4, 0]], 'u1'), save_fit_start=True)
    model.fit(xr.DataArray(values[:split], dims=['time', 'y', 'x'],
        coords={'time': times[:split], 'y': [1, 0], 'x': [0, 1]}), method='ROC')
    for value, date in zip(values[split:], times[split:]):
        model.monitor(value, date.to_pydatetime())
    first, a = run(root, tmp_path/'a', chunk_size=1)
    second, b = run(root, tmp_path/'b', chunk_size=8)
    # nrt reduces float32 residuals: spatial layout can change last-bit diagnostics.
    assert first['processing_signature'] != second['processing_signature']
    for name in ('backend_mask', 'fit_usable', 'detection_date', 'history_used', 'observations_used'):
        np.testing.assert_array_equal(a[name][:], b[name][:])
    np.testing.assert_array_equal(a['backend_mask'][:], [[1, 3], [4, 0]])
    np.testing.assert_array_equal(a['backend_mask'][:], model.mask)
    for name in ('process', 'boundary', 'sigma', 'detection_date', 'fit_start'):
        np.testing.assert_array_equal(b[name][0], getattr(model, name)[0])
    np.testing.assert_array_equal(a['history_used'][0], model.histsize[0])
    np.testing.assert_array_equal(a['observations_used'][0], model.n[0])
    assert a['observations_used'][0, 1] < 90  # First event stops subsequent updates.
    assert not a['fit_usable'][1].any()
    assert np.isnan(a['process'][1]).all() and not a['history_used'][1].any()
    assert a.attrs['status'] == 'complete' and hashes(root) == before
    with xr.open_zarr(str(tmp_path/'a/cusum.zarr'), consolidated=False) as ds:
        assert ds.backend_mask.dims == ('y', 'x') and ds.backend_mask.values[1, 1] == 0
    receipt = json.loads((tmp_path/'a/analysis.json').read_text())
    assert receipt['contract']['backend_version'] == '0.3.0'
    for col in (0, 1):
        single = CuSum(trend=False, harmonic_order=0, save_fit_start=True)
        single.fit(xr.DataArray(values[:split, :1, col:col+1], dims=['time', 'y', 'x'],
            coords={'time': times[:split], 'y': [1], 'x': [col]}), method='ROC')
        for value, date in zip(values[split:, :1, col:col+1], times[split:]):
            single.monitor(value, date.to_pydatetime())
        for name in ('process', 'boundary', 'sigma', 'detection_date'):
            np.testing.assert_array_equal(a[name][:1, col:col+1], getattr(single, name))


def test_masks_nodata_and_signature(tmp_path):
    root = tmp_path/'parent'
    group, values, _ = parent(root)
    a, out_a = run(root, tmp_path/'a')
    mask = np.ones(values.shape, 'u1')
    mask[65:, 0, 1] = 0
    create_zarr_array(group, 'VV_observed_mask', data=mask, dimension_names=['time', 'y', 'x'])
    b, out_b = run(root, tmp_path/'b')
    assert out_b['backend_mask'][0, 1] == 1 and out_b['monitoring_available'][0, 1] == 5
    assert a['processing_signature'] != b['processing_signature']
    values[65:, 0, 1] = 100  # Filled values cannot affect the detector or effective-input fingerprint.
    group['VV_dB'][:] = values
    c, out_c = run(root, tmp_path/'c')
    assert b['processing_signature'] == c['processing_signature']
    np.testing.assert_array_equal(out_b['process'][:], out_c['process'][:])
    values[:, 1, 0] = -9999
    values[:, 1, 1] = -12345
    group['VV_dB'][:] = values
    _, out_d = run(root, tmp_path/'d')
    assert not out_d['history_available'][1].any()
    assert not out_d['monitoring_available'][1].any()
    d, _ = run(root, tmp_path/'changed', sensitivity=.01)
    assert d['processing_signature'] != c['processing_signature']
    assert out_a['backend_mask'][0, 1] == 3


def test_unstable_constant_and_short(tmp_path):
    root = tmp_path/'parent'
    group, values, _ = parent(root)
    values[:50, 0, 0] = np.nan  # Ten history samples spanning <183 days: ROC unstable.
    values[:, 0, 1] = -12  # Zero sigma is unusable, not a valid no-change result.
    values[:58, 1, 0] = np.nan
    values[58:, 1, 0] = [-12, -11]*16  # Two history samples formerly crashed upstream ROC.
    group['VV_dB'][:] = values
    _, out = run(root, tmp_path/'out')
    np.testing.assert_array_equal(out['backend_mask'][:], [[2, 1], [4, 0]])
    assert not out['fit_usable'][:].any() and not out['detection_date'][:].any()
    assert np.isnan(out['process'][:]).all()


def test_harmonic_and_window(tmp_path):
    import numba

    root = tmp_path/'parent'
    parent(root)
    threads = numba.get_num_threads()
    a, out = run(root, tmp_path/'a', trend=True, harmonic_order=1, window=[0, 0, 1, 1])
    assert out['backend_mask'].shape == (1, 1) and out['fit_usable'][0, 0] == 1
    b, _ = run(root, tmp_path/'b', window=[0, 0, 1, 1])
    assert a['processing_signature'] != b['processing_signature']
    assert numba.get_num_threads() == threads


def test_output_isolation(tmp_path):
    root = tmp_path/'parent'
    parent(root)
    before = hashes(root)
    with pytest.raises(ValueError, match='separate'):
        run(root, root/'output')
    existing = tmp_path/'existing'
    existing.mkdir()
    with pytest.raises(FileExistsError):
        run(root, existing)
    assert hashes(root) == before and not list(existing.iterdir())


@pytest.mark.parametrize('kwargs', [dict(monitor_start='2021-01-01'), dict(monitor_start='2022-12-31'),
                                   dict(sensitivity=0), dict(window=[0, 0, 3, 2])])
def test_invalid_inputs_do_not_write(tmp_path, kwargs):
    root = tmp_path/'parent'
    parent(root)
    with pytest.raises(ValueError):
        run(root, tmp_path/'out', **kwargs)
    assert not (tmp_path/'out').exists()


def test_cli(tmp_path):
    root = tmp_path/'parent'
    parent(root)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')+os.pathsep+os.environ.get('PYTHONPATH', ''))
    result = subprocess.run([sys.executable, '-m', 's1grits.cli', 'analyze_cusum',
        '--catalog-root', str(root), '--item-id', 'parent', '--output', str(tmp_path/'out'),
        '--band', 'VV_dB', '--history-start', '2020-01-01', '--history-end', '2021-12-09',
        '--monitor-start', '2021-12-10', '--monitor-end', '2023-01-01', '--no-trend', '--harmonic-order', '0'],
        capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout+result.stderr
    assert json.loads((tmp_path/'out/analysis.json').read_text())['status_counts'] == {'0': 1, '1': 1, '2': 0, '3': 1, '4': 1}
