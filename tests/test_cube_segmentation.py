"""Local input/output checks and explicitly enabled real Earth Engine calls."""
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from test_cube_derivatives import source, hashes
from s1grits.analysis import analyze_segments
from s1grits.analysis.segmentation import _annual_series, _read_point
from s1grits.zarr_encoding import create_zarr_array


def parent(root, layout='tyx'):
    group, _, _ = source(root, layout, count=120)
    times = pd.date_range('2010-01-01', periods=120, freq='MS')+pd.Timedelta(days=14)
    group['time'][:] = times.values
    values = np.broadcast_to((-12+.2*np.sin(np.arange(120)*np.pi/6))[:, None, None], (120, 2, 2)).astype('f4').copy()
    values[66:, 0, 0] -= 4
    values[:, 1, 1] = np.nan
    values[0, 1, 1] = -9999
    values[1, 1, 1] = -12345
    for band in ('VV_dB', 'VH_dB'):
        group[band][:] = values if layout == 'tyx' else values.transpose(1, 2, 0)
    return group, values


def run(root, dest, algorithm='ccdc', **kwargs):
    options = dict(annual_method='median', annual_domain='db', disturbance_direction='decrease') if algorithm == 'landtrendr' else {}
    options.update(kwargs)
    return analyze_segments(root, 'parent', dest, algorithm=algorithm, backend='earthengine',
        band='VV_dB', pixel=options.pop('pixel', [0, 0]), start='2010-01-01', end='2019-12-31T23:59:59Z', **options)


@pytest.mark.parametrize('domain,expected', [('db', 5), ('linear_power', 10*np.log10(5.5))])
@pytest.mark.parametrize('method', ['mean', 'median'])
def test_annual_support(domain, expected, method):
    frame = pd.DataFrame({'time': pd.to_datetime(['2019-01-01', '2019-12-31', '2021-06-01'], utc=True),
                          'value_db': [0., 10., 3.]})
    out = _annual_series(frame, '2019', '2021-12-31', method, domain, 2)
    np.testing.assert_allclose(out.value_db, [expected, np.nan, np.nan])
    assert out.observations.tolist() == [2, 0, 1]
    assert out.year.tolist() == [2019, 2020, 2021]


@pytest.mark.parametrize('layout', ['tyx', 'yxt'])
def test_input_masks_and_axes(tmp_path, layout):
    root = tmp_path/'parent'
    group, values = parent(root, layout)
    mask = np.ones(values.shape, 'u1')
    mask[66:, 0, 0] = 0
    create_zarr_array(group, 'VV_observed_mask', data=mask, dimension_names=['time', 'y', 'x'])
    before = hashes(root)
    frame, _, lineage, _ = _read_point(root, 'parent', tmp_path/'out', 'VV_dB', [0, 0], '2010', '2019-12-31')
    assert frame.value_db.notna().sum() == 66
    np.testing.assert_array_equal(frame.value_db[:66], values[:66, 0, 0])
    assert lineage['observed_mask'] == 'VV_observed_mask'
    assert hashes(root) == before and not (tmp_path/'out').exists()


@pytest.mark.parametrize('algorithm', ['ccdc', 'landtrendr'])
def test_insufficient_output(tmp_path, algorithm):
    pytest.importorskip('ee')
    root = tmp_path/'parent'
    parent(root)
    before = hashes(root)
    result = run(root, tmp_path/'out', algorithm, pixel=[1, 1])
    assert result['status'] == 'complete' and result['analysis_state'] == 'insufficient_observations'
    assert result['model_observations'] == 0
    assert pd.read_parquet(tmp_path/'out/segments.parquet').empty
    assert json.loads((tmp_path/'out/raw_response.json').read_text()) == {}
    assert hashes(root) == before


@pytest.mark.parametrize('options', [dict(pixel=[2, 0]), dict(annual_method='median'), dict(pixel=[-1, 0])])
def test_invalid_before_output(tmp_path, options):
    root = tmp_path/'parent'
    parent(root)
    with pytest.raises(ValueError):
        run(root, tmp_path/'out', **options)
    assert not (tmp_path/'out').exists()


def test_service_failure_is_not_zero_events(tmp_path, monkeypatch):
    ee = pytest.importorskip('ee')
    root = tmp_path/'parent'
    parent(root)

    def unavailable(**kwargs):
        raise ConnectionError('Deliberate transport failure')

    monkeypatch.setattr(ee, 'Initialize', unavailable)
    with pytest.raises(ConnectionError):
        run(root, tmp_path/'out')
    receipt = json.loads((tmp_path/'out/analysis.json').read_text())
    assert receipt['status'] == 'failed' and 'confirmed_breaks' not in receipt
    assert not (tmp_path/'out/segments.parquet').exists()


def test_lazy_import_and_cli(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')+os.pathsep+os.environ.get('PYTHONPATH', ''))
    result = subprocess.run([sys.executable, '-c',
        "import sys; from s1grits.analysis import analyze_segments; assert 'ee' not in sys.modules"],
        capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout+result.stderr
    pytest.importorskip('ee')
    root = tmp_path/'parent'
    parent(root)
    result = subprocess.run([sys.executable, '-m', 's1grits.cli', 'analyze_segments',
        '--catalog-root', str(root), '--item-id', 'parent', '--output', str(tmp_path/'out'),
        '--backend', 'earthengine', '--algorithm', 'ccdc', '--band', 'VV_dB', '--pixel', '1', '1',
        '--start', '2010', '--end', '2019-12-31'], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout+result.stderr
    assert json.loads((tmp_path/'out/analysis.json').read_text())['analysis_state'] == 'insufficient_observations'


@pytest.mark.skipif(os.environ.get('S1GRITS_EE_TEST') != '1', reason='Explicit authorized Earth Engine service test')
@pytest.mark.parametrize('short_span', [False, True])
def test_live_stable_and_short_span(tmp_path, short_span):
    root = tmp_path/'parent'
    group, _ = parent(root)
    if short_span:
        group['time'][:] = pd.date_range('2010-01-01', periods=120, freq='D').values
    receipt = run(root, tmp_path/'out', pixel=[1, 0])
    # CCDC can return a tail fit even for less than one year; preserve its actual response.
    assert receipt['analysis_state'] == 'fitted'
    raw = json.loads((tmp_path/'out/raw_response.json').read_text())
    assert receipt['segments'] == len(raw['tStart'])
    if not short_span:
        assert receipt['confirmed_breaks'] == 0


@pytest.mark.skipif(os.environ.get('S1GRITS_EE_TEST') != '1', reason='Explicit authorized Earth Engine service test')
@pytest.mark.parametrize('algorithm', ['ccdc', 'landtrendr'])
def test_live_backend_parity(tmp_path, algorithm):
    import ee

    root = tmp_path/'parent'
    parent(root)
    before = hashes(root)
    receipt = run(root, tmp_path/'out', algorithm)
    assert receipt['analysis_state'] == 'fitted'
    rows = pd.read_parquet(tmp_path/'out/model_input.parquet').dropna(subset=['signal'])
    images = [ee.Image.constant(float(row.signal)).toDouble().rename('signal')
              .set('system:time_start', int(row.time.value//1_000_000)) for row in rows.itertuples()]
    collection = ee.ImageCollection.fromImages(images)
    parameters = receipt['contract']['parameters']
    result = (ee.Algorithms.TemporalSegmentation.Ccdc(collection=collection, **parameters) if algorithm == 'ccdc'
              else ee.Algorithms.TemporalSegmentation.LandTrendr(timeSeries=collection, **parameters))
    direct = result.reduceRegion(ee.Reducer.first(), ee.Geometry.Point([15, -15], 'EPSG:3857'),
                                 crs='EPSG:3857', scale=30, maxPixels=16).getInfo()
    raw = json.loads((tmp_path/'out/raw_response.json').read_text())
    assert raw == direct and hashes(root) == before
    segments = pd.read_parquet(tmp_path/'out/segments.parquet')
    if algorithm == 'ccdc':
        assert segments.confirmed.any() and receipt['confirmed_breaks'] > 0
        np.testing.assert_array_equal(segments.t_break_ms, raw['tBreak'])
        np.testing.assert_array_equal(np.stack(segments.coefficients), raw['signal_coefs'])
    else:
        assert (segments.delta_disturbance_db > 0).any()
        fitted = pd.read_parquet(tmp_path/'out/annual_fitted.parquet')
        np.testing.assert_array_equal(fitted.fitted_db, -np.array(raw['LandTrendr'][2]))
        assert fitted.year.between(2010, 2019).all()
