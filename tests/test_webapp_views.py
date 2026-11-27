"""Web view plans execute existing CLI against real parents, without downloads."""
import json
import sys

import numpy as np
import pytest

pytest.importorskip('fastapi')
pytest.importorskip('httpx')
from fastapi.testclient import TestClient
from s1grits.webapp.server import create_app
from s1grits.analysis import define_view, define_multitrack_view, register_view
from s1grits.resolver import CubeResolver
from test_cube_derivatives import source, hashes
from test_multitrack_views import monthly_parents
from test_webapp_tools import completed


def client_for(workspace, parent):
    workspace.mkdir()
    client = TestClient(create_app(workspace, catalog_roots=[str(parent)],
                                 job_cmd_prefix=[sys.executable, '-m', 's1grits']))
    root_id = next(k for k,v in client.app.state.console._catalog_roots.items() if v['path']==parent)
    return client, root_id


def execute(client, body):
    response = client.post('/api/tools/plan', json=body)
    assert response.status_code == 200, response.text
    plan = response.json()
    submitted = client.post('/api/tools/tasks', json={'plan_id':plan['plan_id']})
    assert submitted.status_code == 201, submitted.text
    task = completed(client, submitted.json()['run_id'])
    assert task['status']=='done', client.app.state.jobs.get(task['run_id']).job_dir.joinpath('log.txt').read_text(encoding='utf-8')
    assert task['result_item_id']==plan['result_item_id']
    assert task['result_type']==plan['result_type']
    assert task['validation']['records']==1
    return plan, task


@pytest.mark.parametrize('bands', [['VV_dB'], ['VH_dB'], ['Ratio'], ['RVI'], ['VV_dB','VH_dB','Ratio','RVI']])
def test_selection_view_and_materialization(tmp_path, bands):
    parent = tmp_path/'parent'
    if len(bands)==4:
        from test_cube_views import sample
        sample(parent, masked=True)
    else:
        source(parent)
    client, root_id = client_for(tmp_path/'w', parent)
    before = hashes(parent)
    body=dict(operation='view_create', root_id=root_id, item_id='parent', output_subdir='view',
              start='2026-01-13', end='2026-01-25', window=[1,1,2,2], bands=bands)
    response=client.post('/api/tools/plan',json=body)
    assert response.status_code==200, response.text
    assert not (tmp_path/'w/view').exists()
    plan, _ = execute(client,body)
    assert plan['raw_gib']==0 and plan['materialized_gib']>0
    assert not list((tmp_path/'w/view').rglob('*.zarr'))
    direct = register_view(tmp_path/'reference', define_view(parent,'parent',start=body['start'],end=body['end'],
                           window=body['window'],bands=bands))
    assert plan['result_item_id']==direct['item_id']
    material, _=execute(client,dict(operation='view_materialize',output='view',item_id=plan['result_item_id'],
                                  output_subdir='physical',chunk_size=2))
    resolver = CubeResolver(tmp_path/'w/physical')
    assert resolver.catalog.iloc[0].item_id == material['result_item_id']
    with CubeResolver(tmp_path/'reference').open(direct) as expected, \
         resolver.open(resolver.catalog.iloc[0]) as actual:
        for name in list(expected.data_vars)+['time','x','y']:
            np.testing.assert_array_equal(actual[name].values,expected[name].values)
    assert hashes(parent)==before


@pytest.mark.parametrize('method', ['mean','first_valid_pixel'])
@pytest.mark.parametrize('reverse',[False,True])
def test_multitrack_and_materialization(tmp_path, method, reverse):
    parent=tmp_path/'parent'
    records,_=monthly_parents(parent)
    ids=[r['item_id'] for r in records][::-1] if reverse else [r['item_id'] for r in records]
    client, root_id=client_for(tmp_path/'w',parent)
    before=hashes(parent)
    body=dict(operation='view_multitrack',root_id=root_id,output='catalog',item_ids=ids,
              output_subdir='view',start_month='2026-01',end_month='2026-03',method=method,window=[0,0,2,2])
    plan,task=execute(client,body)
    assert task['parent_item_ids']==ids
    direct=register_view(tmp_path/'reference',define_multitrack_view(parent/'catalog',ids,
        start_month=body['start_month'],end_month=body['end_month'],method=method,window=body['window']))
    assert plan['result_item_id']==direct['item_id']
    assert not list((tmp_path/'w/view').rglob('*.zarr'))
    material,_=execute(client,dict(operation='view_materialize',output='view',item_id=plan['result_item_id'],
                                 output_subdir='physical',chunk_size=2))
    resolver=CubeResolver(tmp_path/'w/physical')
    row=resolver.catalog.iloc[0]
    assert row['track'] is None or np.isnan(row['track'])
    with resolver.open(row) as actual, CubeResolver(tmp_path/'reference').open(direct) as expected:
        for name in list(expected.data_vars)+['time','x','y']:
            np.testing.assert_array_equal(actual[name].values,expected[name].values)
        power=5 if method=='mean' else (9 if reverse else 1)
        assert actual.VV_dB[0,0,0].item()==pytest.approx(10*np.log10(power),abs=2e-6)
        assert 'n_obs' not in actual
        assert not actual.n_contributing_tracks[1].values.any()
    assert hashes(parent)==before


def test_view_boundaries_and_changed_parent(tmp_path):
    import zarr
    parent=tmp_path/'parent'
    source(parent)
    client, root_id=client_for(tmp_path/'w',parent)
    body=dict(operation='view_create',root_id=root_id,item_id='parent',output_subdir='view',
              start='2026-01-01',end='2026-03-01',bands=['Ratio'])
    for extra in ({'bands':['GLCM']},{'bands':[]},{'window':[0,0,999,999]},
                  {'output_subdir':'../parent'},{'start':'invalid'}):
        assert client.post('/api/tools/plan',json={**body,**extra}).status_code in (400,422)
    plan=client.post('/api/tools/plan',json=body).json()
    zarr.open_group(str(parent/'17MPU/scenes_ASCENDING/zarr/parent.zarr'),mode='r+').attrs['generation_id']='changed'
    assert client.post('/api/tools/tasks',json={'plan_id':plan['plan_id']}).status_code==400
    assert not (tmp_path/'w/view').exists()


@pytest.mark.parametrize('operation',['view_create','view_materialize','view_multitrack'])
def test_view_restart_never_replays_ledger(tmp_path,operation):
    from s1grits.webapp.jobs import JobManager
    folder=tmp_path/'.webapp/jobs/stopped'
    folder.mkdir(parents=True)
    (folder/'job.json').write_text(json.dumps({'id':'stopped','type':operation,'status':'queued',
        'commands':[[sys.executable,'-c',"raise RuntimeError('must not run')"]]}))
    manager=JobManager(tmp_path)
    assert manager.get('stopped').status=='interrupted'
    assert not manager.get('stopped').to_dict()['recoverable']
    assert not manager.recovery_check('stopped')['recoverable']


@pytest.mark.parametrize('problem',['duplicate','grid','direction','range','legacy','nested'])
def test_multitrack_rejects_incompatible_inputs(tmp_path,problem):
    import pandas as pd
    import zarr
    from s1grits.product_instance import make_processing_signature
    parent=tmp_path/'parent'
    records,stores=monthly_parents(parent)
    ids=[r['item_id'] for r in records]
    if problem=='duplicate':
        ids[1]=ids[0]
    elif problem=='nested':
        definition=define_multitrack_view(parent/'catalog',ids,start_month='2026-01',end_month='2026-03',method='mean')
        ids[1]=register_view(parent/'catalog',definition)['item_id']
    else:
        path=parent/'catalog/catalog.parquet'
        frame=pd.read_parquet(path)
        if problem in ('range','legacy'):
            group=zarr.open_group(str(stores[1]),mode='r+')
            variant=json.loads(group.attrs['processing_variant_json'])
            if problem=='range':
                variant['start_utc']='2026-01-15T00:00:00+00:00'
            else:
                variant={'processing.monthly.composite_method':'mean'}
            signature=make_processing_signature(variant)
            group.attrs.update(processing_variant_json=json.dumps(variant),processing_signature=signature)
            frame.loc[1,'processing_signature']=signature
        else:
            column={'grid':'grid_id','direction':'flight_direction'}[problem]
            frame.loc[1,column]={'grid':'other-grid','direction':'DESCENDING'}[problem]
        frame.to_parquet(path)
    client,root_id=client_for(tmp_path/'w',parent)
    response=client.post('/api/tools/plan',json=dict(operation='view_multitrack',root_id=root_id,output='catalog',
        item_ids=ids,output_subdir='view',start_month='2026-01',end_month='2026-03',method='mean'))
    assert response.status_code==400, response.text
    assert not (tmp_path/'w/view').exists()


def test_missing_view_dependency_is_explicit(tmp_path,monkeypatch):
    from s1grits.webapp import tool_api
    monkeypatch.setattr(tool_api,'find_spec',lambda name:None)
    client=TestClient(create_app(tmp_path))
    assert not client.get('/api/capabilities').json()['tools']['views']['available']
    response=client.post('/api/tools/plan',json=dict(operation='view_materialize',item_id='v',output_subdir='new'))
    assert response.status_code==400 and 's1grits[views]' in response.text
