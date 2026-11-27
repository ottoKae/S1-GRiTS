"""Real LT-rs calls, independent backend parity and explicit cloud boundaries."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from test_cube_derivatives import hashes, source
from test_cube_segmentation import parent
from s1grits.analysis import analyze_segments


def run(root, output, **options):
    settings = dict(algorithm='landtrendr', band='VV_dB', pixel=[0, 0], start='2010',
                    end='2019-12-31', annual_method='median', annual_domain='db',
                    disturbance_direction='decrease')
    settings.update(options)
    return analyze_segments(root, 'parent', output, **settings)


@pytest.mark.parametrize('layout', ['tyx', 'yxt'])
@pytest.mark.parametrize('direction', ['decrease', 'increase'])
def test_local_parity_and_direction(tmp_path, layout, direction):
    lt = pytest.importorskip('landtrendr')
    root, output = tmp_path/'in', tmp_path/'out'
    group, values = parent(root, layout)
    if direction == 'increase':
        group['VV_dB'][:] = -values if layout == 'tyx' else -values.transpose(1, 2, 0)
    before = hashes(root)
    receipt = run(root, output, disturbance_direction=direction)
    assert receipt['status'] == 'complete' and receipt['analysis_state'] == 'fitted'
    assert receipt['contract']['backend'] == 'local'
    assert receipt['contract']['transmission'] == 'none'
    assert receipt['project_selection'] == 'not_applicable'
    model = pd.read_parquet(output/'model_input.parquet').dropna(subset=['signal'])
    direct, vertices, rmse = lt.pixel(np.ascontiguousarray(model.signal, dtype='f4'),
        np.ascontiguousarray(model.year, dtype='i4'), **receipt['contract']['parameters'])
    raw = json.loads((output/'raw_response.json').read_text())
    np.testing.assert_array_equal(raw['fitted'], direct)
    np.testing.assert_array_equal(raw['is_vertex'], vertices)
    assert raw['rmse'] == rmse
    fitted = pd.read_parquet(output/'annual_fitted.parquet')
    sign = 1 if direction == 'decrease' else -1
    np.testing.assert_array_equal(fitted.fitted_db, sign*direct)
    np.testing.assert_array_equal(fitted.fitted_directed_db, -direct)
    segments = pd.read_parquet(output/'segments.parquet')
    assert (segments.delta_disturbance_db > 0).any()
    np.testing.assert_allclose(segments.delta_db, segments.end_db-segments.start_db)
    assert hashes(root) == before
    with pytest.raises(FileExistsError):
        run(root, output)


def test_stable_missing_and_insufficient(tmp_path):
    pytest.importorskip('landtrendr')
    root = tmp_path/'in'
    group, values = parent(root)
    group['VV_dB'][:, 0, 1] = -12
    group['VV_dB'][24:36, 0, 1] = np.nan
    before = hashes(root)
    receipt = run(root, tmp_path/'stable', pixel=[1, 0])
    assert receipt['model_observations'] == 9
    model = pd.read_parquet(tmp_path/'stable/model_input.parquet')
    assert model.loc[model.year == 2012, 'signal'].isna().all()
    fitted = pd.read_parquet(tmp_path/'stable/annual_fitted.parquet')
    assert 2012 not in fitted.year.values
    np.testing.assert_array_equal(fitted.fitted_db, np.full(9, -12))
    assert (pd.read_parquet(tmp_path/'stable/segments.parquet').delta_db == 0).all()
    for label, options in [('empty', dict(pixel=[1, 1])), ('short', dict(end='2013-12-31'))]:
        receipt = run(root, tmp_path/label, **options)
        assert receipt['analysis_state'] == 'insufficient_observations'
        assert pd.read_parquet(tmp_path/label/'segments.parquet').empty
        assert json.loads((tmp_path/label/'raw_response.json').read_text()) == {}
    assert hashes(root) == before


def test_native_limit_before_output(tmp_path):
    pytest.importorskip('landtrendr')
    group, _, _ = source(tmp_path/'in', count=129)
    group['time'][:] = pd.date_range('2000', periods=129, freq='YS').values
    with pytest.raises(ValueError, match='128'):
        run(tmp_path/'in', tmp_path/'out', start='2000', end='2128-12-31')
    assert not (tmp_path/'out').exists()


def test_local_backend_does_not_fall_back(tmp_path):
    with pytest.raises(ValueError, match='Local CCDC is not implemented'):
        analyze_segments(tmp_path/'in', 'parent', tmp_path/'out', algorithm='ccdc',
                         band='VV_dB', pixel=[0, 0], start='2010', end='2020')
    with pytest.raises(ValueError, match='project applies only'):
        run(tmp_path/'in', tmp_path/'out', project='not-for-local')
    assert not (tmp_path/'out').exists()


def test_missing_local_backend(tmp_path):
    if importlib.util.find_spec('landtrendr') is not None:
        pytest.skip('Run in the core environment without optional LT-rs')
    parent(tmp_path/'in')
    with pytest.raises(ImportError, match='landtrendr'):
        run(tmp_path/'in', tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_cli_default_local_without_ee_import(tmp_path):
    pytest.importorskip('landtrendr')
    root = tmp_path/'in'
    parent(root)
    command = [sys.executable, '-c',
        "import sys; from s1grits.cli import main; main(); assert 'ee' not in sys.modules",
        'analyze_segments', '--catalog-root', str(root), '--item-id', 'parent',
        '--output', str(tmp_path/'out'), '--algorithm', 'landtrendr', '--band', 'VV_dB',
        '--pixel', '0', '0', '--start', '2010', '--end', '2019-12-31',
        '--annual-method', 'median', '--annual-domain', 'db', '--disturbance-direction', 'decrease']
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')+
               os.pathsep+os.environ.get('PYTHONPATH', ''))
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout+result.stderr
    receipt = json.loads((tmp_path/'out/analysis.json').read_text())
    assert receipt['contract']['backend'] == 'local' and receipt['analysis_state'] == 'fitted'
