"""Real queued derive CLI outputs, with the same parent reused by each method."""
import json
import sys
import time

import numpy as np
import pytest

pytest.importorskip('fastapi')
pytest.importorskip('httpx')
from fastapi.testclient import TestClient
from s1grits.webapp.server import create_app
from s1grits.analysis import derive_cube
from s1grits.resolver import CubeResolver
from test_cube_derivatives import source, hashes


@pytest.fixture
def tools_client(tmp_path):
    parent = tmp_path/'parent'
    source(parent)
    workspace = tmp_path/'web'
    workspace.mkdir()
    client = TestClient(create_app(workspace, catalog_roots=[str(parent)], job_cmd_prefix=[sys.executable, '-m', 's1grits']))
    root_id = next(k for k, v in client.app.state.console._catalog_roots.items() if v['path'] == parent)
    request = dict(root_id=root_id, item_id='parent', output_subdir='result', start='2026-01-01',
                   end='2026-03-01', features=[], window=[0, 0, 8, 8], chunk_size=8)
    return client, request, parent, workspace


def completed(client, job_id):
    deadline = time.monotonic()+90
    while time.monotonic() < deadline:
        job = client.get('/api/tasks/'+job_id).json()
        if job['status'] in ('done', 'failed', 'cancelled'):
            return job
        time.sleep(.05)
    pytest.fail('CLI job did not finish')


@pytest.mark.parametrize('options', [
    {}, {'features':['ratio']}, {'features':['rvi']}, {'features':['glcm']},
    {'spatial_filter':'tv_bregman'}, {'spatial_filter':'nlm'},
    {'monthly':'mean'}, {'monthly':'median'},
    {'interpolation':'none','interpolation_domain':'db'},
    {'interpolation':'none','interpolation_domain':'linear_power'},
    {'interpolation':'linear','interpolation_domain':'db','max_gap_days':60},
    {'interpolation':'linear','interpolation_domain':'linear_power','max_gap_days':60},
])
def test_real_web_cli_matches_direct_derivative(tools_client, tmp_path, options):
    client, request, parent, workspace = tools_client
    before = hashes(parent)
    response = client.post('/api/tools/plan', json={**request, **options})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert not (workspace/'result').exists()
    submitted = client.post('/api/tools/tasks', json={'plan_id':plan['plan_id']})
    assert submitted.status_code == 201, submitted.text
    job_id = submitted.json()['run_id']
    assert client.post('/api/tools/tasks', json={'plan_id':plan['plan_id']}).status_code == 400
    job = completed(client, job_id)
    assert job['status'] == 'done', client.app.state.jobs.get(job_id).job_dir.joinpath('log.txt').read_text(encoding='utf-8')
    expected = derive_cube(parent, 'parent', tmp_path/'reference', start=request['start'], end=request['end'],
                           window=request['window'], chunk_size=8, **{'features':[], **options})
    actual = CubeResolver(workspace/'result')
    record = actual.catalog.iloc[0]
    assert expected['processing_signature'] == record.processing_signature
    with actual.open(record) as ds, CubeResolver(tmp_path/'reference').open(expected) as reference:
        assert set(ds.data_vars) == set(reference.data_vars)
        for name in list(ds.data_vars)+['time','x','y']:
            np.testing.assert_array_equal(ds[name].values, reference[name].values)
    assert hashes(parent) == before
    assert json.loads((workspace/'result/derivation.json').read_text())['status'] == 'complete'
    assert job['result_type'] == 'physical' and job['validation']['records'] > 0


@pytest.mark.parametrize('options,status', [({'operation':'shell'},422),({'features':['unknown']},422),
    ({'interpolation':'linear'},422),({'window':[0,0,500,500]},400),({'output_subdir':'../parent'},400),
    ({'start':'bad'},400),({'monthly':'mean','interpolation':'none','interpolation_domain':'db'},422)])
def test_invalid_tool_requests_do_not_write(tools_client, options, status):
    client, request, parent, workspace = tools_client
    before = hashes(parent)
    response = client.post('/api/tools/plan', json={**request, **options})
    assert response.status_code == status, response.text
    assert not (workspace/'result').exists()
    assert hashes(parent) == before


def test_parent_change_and_existing_destination_rejected(tools_client):
    import zarr
    client, request, parent, workspace = tools_client
    plan = client.post('/api/tools/plan', json=request).json()
    (workspace/'result').mkdir()
    assert client.post('/api/tools/tasks', json={'plan_id':plan['plan_id']}).status_code == 400
    request['output_subdir']='other'
    plan=client.post('/api/tools/plan', json=request).json()
    resolver=CubeResolver(parent)
    zarr.open_group(str(resolver._asset_path(resolver.catalog.iloc[0])), mode='r+').attrs['generation_id']='changed'
    assert client.post('/api/tools/tasks', json={'plan_id':plan['plan_id']}).status_code == 400
    assert not (workspace/'other').exists()


def test_tool_restart_does_not_replay_persisted_command(tools_client):
    from s1grits.webapp.jobs import JobManager
    client, _, _, workspace = tools_client
    folder=workspace/'.webapp/jobs/interrupted-tool'
    folder.mkdir(parents=True)
    (folder/'job.json').write_text(json.dumps({'id':'interrupted-tool','type':'derive_cube','status':'queued',
         'commands':[[sys.executable,'-c',"raise RuntimeError('must not run')"]]}))
    restarted=JobManager(workspace)
    job=restarted.get('interrupted-tool')
    assert job.status=='interrupted' and not job.to_dict()['recoverable']
    assert not restarted.recovery_check(job.id)['recoverable']


def test_running_tool_can_cancel_without_touching_parent(tools_client):
    client, request, parent, _ = tools_client
    before = hashes(parent)
    plan = client.post('/api/tools/plan', json=request).json()
    task = client.post('/api/tools/tasks', json={'plan_id':plan['plan_id']}).json()
    job = client.app.state.jobs.get(task['run_id'])
    deadline = time.monotonic()+10
    while job._proc is None and time.monotonic()<deadline:
        time.sleep(.005)
    assert job._proc is not None
    assert client.post('/api/jobs/'+job.id+'/cancel').status_code == 200
    assert completed(client, job.id)['status'] == 'cancelled'
    assert hashes(parent) == before
    assert not client.app.state.jobs.recovery_check(job.id)['recoverable']


def test_bad_source_chunk_fails_without_rewriting_parent(tools_client):
    client, request, parent, workspace = tools_client
    store = parent/'17MPU/scenes_ASCENDING/zarr/parent.zarr'
    chunk = next(path for path in (store/'VV_dB/c').rglob('*') if path.is_file())
    chunk.write_bytes(b'invalid compressed chunk')
    before = hashes(parent)
    planned = client.post('/api/tools/plan', json=request)
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    assert client.post('/api/tasks', json={'plan_id':plan['plan_id']}).status_code == 400
    task = client.post('/api/tools/tasks', json={'plan_id':plan['plan_id']}).json()
    assert completed(client, task['run_id'])['status'] == 'failed'
    assert hashes(parent) == before
    assert not (workspace/'result').exists()
    job = client.app.state.jobs.get(task['run_id'])
    assert 'decompression error' in (job.job_dir/'log.txt').read_text(encoding='utf-8')
