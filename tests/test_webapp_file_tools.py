"""Small real CLI outputs through Web preflight, queue and result APIs."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import rasterio

from test_cube_derivatives import hashes
from test_cube_segmentation import parent
from test_webapp_views import client_for
from test_webapp_tools import completed


def analysis_request(root_id, **extra):
    return dict(operation='analyze_segments', root_id=root_id, item_id='parent', output_subdir='out',
        band='VV_dB', start='2010-01-01', end='2019-12-31T23:59:59Z',
        annual_method='median', annual_domain='db', disturbance_direction='decrease', pixel=[0, 0], **extra)


def execute(client, body):
    response = client.post('/api/tools/plan', json=body)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert not Path(plan['output_dir']).exists()
    submitted = client.post('/api/tools/tasks', json={'plan_id': plan['plan_id']})
    assert submitted.status_code == 201, submitted.text
    task = completed(client, submitted.json()['run_id'])
    assert task['status'] == 'done', client.app.state.jobs.get(task['run_id']).job_dir.joinpath('log.txt').read_text(encoding='utf-8')
    assert task['command_total'] == 1 and 'validation' not in task
    result = client.get('/api/tools/results/'+task['run_id'])
    assert result.status_code == 200, result.text
    assert client.post('/api/tools/tasks', json={'plan_id': plan['plan_id']}).status_code == 400
    return plan, task, result.json()


@pytest.mark.parametrize('method,domain,direction', [
    ('mean','db','increase'), ('median','db','decrease'),
    ('mean','linear_power','decrease'), ('median','linear_power','increase')])
def test_real_landtrendr(tmp_path, method, domain, direction):
    pytest.importorskip('landtrendr')
    from s1grits.analysis.segmentation import analyze_segments
    root = tmp_path/'p'
    parent(root)
    client, root_id = client_for(tmp_path/'w', root)
    before = hashes(root)
    body = analysis_request(root_id)
    body.update(annual_method=method, annual_domain=domain, disturbance_direction=direction)
    plan, task, result = execute(client, body)
    assert plan['samples'][0]['model_observations'] == 10
    assert result['receipt']['analysis_state'] == 'fitted'
    assert result['tables']['segments']['total'] > 0
    reference = tmp_path/'ref'
    options = {key:body[key] for key in ('band','start','end','pixel','annual_method','annual_domain','disturbance_direction')}
    expected = analyze_segments(root, 'parent', reference, algorithm='landtrendr', backend='local', **options)
    assert result['receipt']['processing_signature'] == expected['processing_signature']
    pd.testing.assert_frame_equal(pd.read_parquet(Path(plan['output_dir'])/'segments.parquet'),
                                  pd.read_parquet(reference/'segments.parquet'))
    assert hashes(root) == before
    assert not list(Path(plan['output_dir']).glob('catalog*'))


@pytest.mark.parametrize('mode', ['window','points'])
def test_real_batch_and_missing_data(tmp_path, mode):
    pytest.importorskip('landtrendr')
    root = tmp_path/'p'
    parent(root)
    client, root_id = client_for(tmp_path/'w', root)
    body = analysis_request(root_id)
    body.pop('pixel')
    body['operation'] = 'analyze_segments_batch'
    if mode == 'window':
        body['window'] = [0,0,2,2]
    else:
        body['points'] = [dict(point_id='event', col=0, row=0),dict(point_id='missing',col=1,row=1)]
    plan, _, result = execute(client, body)
    rows = result['tables']['point_summary']['rows']
    assert rows[0]['analysis_state'] == 'fitted'
    assert rows[-1]['analysis_state'] == 'insufficient_observations'
    assert rows[-1]['segments'] is None
    assert len(rows) == (4 if mode == 'window' else 2)
    assert result['receipt']['status'] == 'complete'


def mosaic_request(root_id, **extra):
    return dict(operation='mosaic_grid', root_id=root_id, item_ids=['i0','i1'], output_subdir='out',
                target_crs='EPSG:32717', resolution=30, bounds=[600000,9899880,600210,9900000],
                output_format='VRT', **extra)


@pytest.mark.parametrize('format', ['VRT','COG'])
def test_real_grid_outputs(tmp_path, format):
    from test_grid_mosaic import cube, call
    root = tmp_path/'p'
    cube(root)
    client, root_id = client_for(tmp_path/'w', root)
    before = hashes(root)
    body = mosaic_request(root_id)
    body['output_format'] = format
    plan, task, result = execute(client, body)
    assert task['parent_item_ids'] == ['i0','i1']
    assert result['receipt']['contract']['transform_tolerance_pixels'] == 0
    reference = tmp_path/('ref.vrt' if format=='VRT' else 'ref.tif')
    call(root, reference, output_format=format)
    with rasterio.open(result['receipt']['output']) as actual, rasterio.open(reference) as expected:
        np.testing.assert_array_equal(actual.read(),expected.read())
        assert actual.shape == (4,7)
        assert actual.read(1)[1,2] == 100
        assert np.isnan(actual.read()[:,:,-1]).all()
    assert hashes(root) == before
    assert result['tables'] == {} and result['result_type'] == 'raster'


def test_analysis_preflight_and_disabled_backends(tmp_path, monkeypatch):
    from s1grits.webapp import analysis_api
    root = tmp_path/'p'
    parent(root)
    client, root_id = client_for(tmp_path/'w', root)
    body = analysis_request(root_id)
    capabilities = client.get('/api/capabilities').json()['tools']['local']
    assert not capabilities['ccdc']['available'] and not capabilities['cusum']['available']
    assert not capabilities['earthengine']['available']
    for extra in ({'backend':'earthengine'}, {'operation':'ccdc'}, {'pixel':[999,0]},
                  {'output_subdir':'../p/out'}, {'start':'invalid'},
                  {'pixel':None,'operation':'analyze_segments_batch','window':[0,0,9,9]},
                  {'pixel':None,'operation':'analyze_segments_batch','window':[0,0,-1,-1]}):
        response = client.post('/api/tools/plan',json={**body,**extra})
        assert response.status_code in (400,422), response.text
    monkeypatch.setattr(analysis_api, 'local_tool_capabilities',lambda:dict(landtrendr={'available':False}))
    response=client.post('/api/tools/plan',json=body)
    assert response.status_code==400 and 'landtrendr==0.4.0' in response.text
    assert not (tmp_path/'w/out').exists()


def test_partial_batch_result_preserves_failure(tmp_path):
    pytest.importorskip('landtrendr')
    root = tmp_path/'p'
    group, _ = parent(root)
    client, root_id = client_for(tmp_path/'w',root)
    body = analysis_request(root_id)
    body.pop('pixel')
    body.update(operation='analyze_segments_batch', points=[
        dict(point_id='good',col=0,row=0), dict(point_id='changed',col=0,row=1)])
    response = client.post('/api/tools/plan',json=body)
    assert response.status_code == 200, response.text
    # A real parent coordinate change after preflight makes only the second point invalid.
    group['y'][1] += 1
    submitted=client.post('/api/tools/tasks',json={'plan_id':response.json()['plan_id']})
    assert submitted.status_code == 201, submitted.text
    task=completed(client,submitted.json()['run_id'])
    assert task['status']=='failed' and task['exit_code'] != 0
    result=client.get('/api/tools/results/'+task['run_id']).json()
    assert result['receipt']['status']=='partial'
    rows=result['tables']['point_summary']['rows']
    assert rows[0]['analysis_state']=='fitted'
    assert rows[1]['status']=='failed' and rows[1]['segments'] is None
    assert 'affine' in rows[1]['error']


@pytest.mark.parametrize('operation',['analyze_segments','analyze_segments_batch','mosaic_grid'])
def test_file_tool_restart_keeps_output_without_replay(tmp_path, operation):
    from test_webapp_views import test_view_restart_never_replays_ledger
    test_view_restart_never_replays_ledger(tmp_path, operation)


def test_mosaic_paths_changed_input_and_invalid_grid(tmp_path):
    from test_grid_mosaic import cube
    root = tmp_path/'p'
    _, paths = cube(root)
    client, root_id = client_for(tmp_path/'w',root)
    body = mosaic_request(root_id)
    for extra in ({'item_ids':['i0','i0']},{'bounds':[0,0,1,1]},{'target_crs':'EPSG:4326'},
                  {'output_subdir':'../p/out'},{'item_ids':['unknown']}):
        response = client.post('/api/tools/plan',json={**body,**extra})
        assert response.status_code in (400,422), response.text
    plan = client.post('/api/tools/plan',json=body).json()
    with rasterio.open(paths[0],'r+',IGNORE_COG_LAYOUT_BREAK='YES') as dst:
        dst.update_tags(changed='yes')
    assert client.post('/api/tools/tasks',json={'plan_id':plan['plan_id']}).status_code==400
    frame = pd.read_parquet(root/'catalog.parquet')
    frame.loc[0,'cog_path'] = str(tmp_path/'outside.tif')
    frame.to_parquet(root/'catalog.parquet')
    assert client.post('/api/tools/plan',json=body).status_code==403
    assert not (tmp_path/'w/out').exists()
