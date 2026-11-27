"""Batch output is the actual point fitter, including missing support and failures."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from s1grits.analysis import analyze_segments, analyze_segments_batch
from test_cube_derivatives import hashes
from test_cube_segmentation import parent


OPTIONS = dict(band='VV_dB', start='2010', end='2019-12-31', annual_method='median',
               annual_domain='db', disturbance_direction='decrease')


@pytest.mark.parametrize('layout', ['tyx', 'yxt'])
def test_window_matches_each_single_point(tmp_path, layout):
    pytest.importorskip('landtrendr')
    root, output = tmp_path/'in', tmp_path/'out'
    group, values = parent(root, layout)
    values[24:, 0, 1] = np.nan
    group['VV_dB'][:] = values if layout == 'tyx' else values.transpose(1, 2, 0)
    before = hashes(root)
    result = analyze_segments_batch(root, 'parent', output, window=[0, 0, 2, 2], **OPTIONS)
    summary = pd.read_parquet(output/'point_summary.parquet')
    assert result['status'] == 'complete' and result['failed_points'] == 0
    assert summary.point_id.tolist() == ['r0_c0', 'r0_c1', 'r1_c0', 'r1_c1']
    assert summary.valid_observations.tolist() == [120, 24, 120, 0]
    assert summary.model_observations.tolist() == [10, 2, 10, 0]
    assert summary.analysis_state.tolist() == ['fitted', 'insufficient_observations', 'fitted', 'insufficient_observations']
    assert summary.loc[[1, 3], 'segments'].isna().all()
    for row in summary.itertuples():
        single = tmp_path/f'single-{row.point_id}'
        receipt = analyze_segments(root, 'parent', single, algorithm='landtrendr',
                                   pixel=[row.col, row.row], **OPTIONS)
        assert row.processing_signature == receipt['processing_signature']
        assert [row.sample_x, row.sample_y] == receipt['contract']['input']['pixel_center']
        for name in ('input.parquet', 'model_input.parquet', 'segments.parquet', 'annual_fitted.parquet'):
            pd.testing.assert_frame_equal(pd.read_parquet(output/row.result_path/name), pd.read_parquet(single/name))
    assert hashes(root) == before
    with pytest.raises(FileExistsError):
        analyze_segments_batch(root, 'parent', output, window=[0, 0, 2, 2], **OPTIONS)


def test_partial_failure_ids_order_and_manual_retry(tmp_path):
    pytest.importorskip('landtrendr')
    root = tmp_path/'in'
    parent(root)
    points = [dict(point_id='outside', col=100, row=0), dict(point_id='../sample/树', col=0, row=0)]
    before = hashes(root)
    result = analyze_segments_batch(root, 'parent', tmp_path/'out', points=points, **OPTIONS)
    assert result['status'] == 'partial' and result['failed_points'] == 1
    failed, good = result['points']
    assert failed['status'] == 'failed' and failed['error_type'] == 'ValueError'
    assert failed['segments'] is None and failed['valid_observations'] is None
    assert good['point_id'] == points[1]['point_id']
    assert (tmp_path/'out'/good['result_path']).resolve().is_relative_to((tmp_path/'out').resolve())
    reversed_result = analyze_segments_batch(root, 'parent', tmp_path/'reversed', points=points[::-1], **OPTIONS)
    assert reversed_result['points'][0]['result_path'] == good['result_path']
    assert reversed_result['points'][0]['processing_signature'] == good['processing_signature']
    retry = [dict(point_id=failed['point_id'], col=1, row=0)]
    retried = analyze_segments_batch(root, 'parent', tmp_path/'retry', points=retry, **OPTIONS)
    assert retried['status'] == 'complete' and retried['points'][0]['point_id'] == failed['point_id']
    assert retried['processing_signature'] != result['processing_signature']
    assert hashes(root) == before


@pytest.mark.parametrize('selection', [dict(points=[]), dict(points=[dict(point_id='a', col=0, row=0)]*2),
    dict(points=[dict(point_id='', col=0, row=0)]), dict(window=[0, 0, 0, 2]),
    dict(points=[dict(point_id='a', col=.5, row=0)]), dict(window=[0, 0, 2, 2], points=[])])
def test_selection_validation_before_write(tmp_path, selection):
    with pytest.raises(ValueError):
        analyze_segments_batch(tmp_path/'in', 'parent', tmp_path/'out', **selection, **OPTIONS)
    assert not (tmp_path/'out').exists()


def test_output_cannot_touch_source_or_referenced_store(tmp_path):
    root = tmp_path/'in'
    parent(root)
    before = hashes(root)
    with pytest.raises(ValueError, match='separate'):
        analyze_segments_batch(root, 'parent', root/'out', window=[0, 0, 1, 1], **OPTIONS)
    catalog = pd.read_parquet(root/'catalog.parquet')
    store = (root/catalog.iloc[0].zarr_path).resolve()
    catalog['zarr_path'] = str(store)
    external = tmp_path/'external-catalog'
    external.mkdir()
    catalog.to_parquet(external/'catalog.parquet')
    with pytest.raises(ValueError, match='referenced source store'):
        analyze_segments_batch(external, 'parent', store/'out', window=[0, 0, 1, 1], **OPTIONS)
    assert hashes(root) == before


def test_all_failed_is_not_zero_segments(tmp_path):
    parent(tmp_path/'in')
    result = analyze_segments_batch(tmp_path/'in', 'parent', tmp_path/'out',
        points=[dict(point_id='outside', col=20, row=20)], **OPTIONS)
    assert result['status'] == 'failed' and result['completed_points'] == 0
    summary = pd.read_parquet(tmp_path/'out/point_summary.parquet')
    assert summary.segments.isna().all() and summary.valid_observations.isna().all()


def test_missing_optional_backend_records_failure(tmp_path):
    if importlib.util.find_spec('landtrendr') is not None:
        pytest.skip('Run in the core environment without LT-rs')
    parent(tmp_path/'in')
    result = analyze_segments_batch(tmp_path/'in', 'parent', tmp_path/'out', window=[0, 0, 1, 1], **OPTIONS)
    assert result['status'] == 'failed'
    assert result['points'][0]['error_type'] == 'ImportError'
    assert 'landtrendr' in result['points'][0]['error']
    assert not (tmp_path/'out'/result['points'][0]['result_path']).exists()


def test_cli_partial_returns_failure_and_no_cloud(tmp_path):
    pytest.importorskip('landtrendr')
    parent(tmp_path/'in')
    points = tmp_path/'points.json'
    points.write_text(json.dumps([dict(point_id='ok', col=0, row=0), dict(point_id='outside', col=2, row=0)]))
    command = [sys.executable, '-c',
        "import sys; from s1grits.cli import main\ntry: main()\nfinally: assert 'ee' not in sys.modules",
        'analyze_segments_batch', '--catalog-root', str(tmp_path/'in'), '--item-id', 'parent',
        '--output', str(tmp_path/'out'), '--points-file', str(points)]
    for key, value in OPTIONS.items():
        command.extend(['--'+key.replace('_', '-'), value])
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')+
               os.pathsep+os.environ.get('PYTHONPATH', ''))
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    assert result.returncode == 1, result.stdout+result.stderr
    assert 'Batch partial' in result.stdout
    assert json.loads((tmp_path/'out/batch.json').read_text())['failed_points'] == 1
