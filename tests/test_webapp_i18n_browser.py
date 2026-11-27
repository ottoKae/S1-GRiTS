"""Opt-in real Edge smoke; no frontend build or browser framework dependency.

Set S1GRITS_BROWSER_TESTS=1 and optionally S1GRITS_EDGE. The local diagnostic
environment supplies websocket-client; this is not a runtime package dependency.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest
from test_zarr_group_access import grouped as grouped

pytestmark = pytest.mark.skipif(os.environ.get('S1GRITS_BROWSER_TESTS') != '1', reason='opt-in real browser')


@pytest.fixture
def browser(tmp_path, request):
    websocket = pytest.importorskip('websocket')
    edge = Path(os.environ.get('S1GRITS_EDGE', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'))
    if not edge.is_file():
        pytest.skip('Edge executable unavailable')
    workspace = tmp_path / 'cube'
    workspace.mkdir()
    (workspace / '中文子目录').mkdir()
    import pandas as pd
    from s1grits.canonical_catalog_schema import normalize_catalog_record

    pd.DataFrame([normalize_catalog_record({
        'item_id': '17MPU_ASCENDING_TK18_2025-01', 'collection_id': 's1grits-smonthly',
        'product_type': 'smonthly', 'product_label': 'smonthly_ASCENDING',
        'tile_id': '17MPU', 'flight_direction': 'ASCENDING',
        'datetime': pd.Timestamp('2025-01-15'), 'month': '2025-01', 'grid_id': 'test-grid',
        'zarr_path': '17MPU/smonthly_ASCENDING/zarr/cube.zarr', 'status': 'complete',
    })]).to_parquet(workspace / 'catalog.parquet')
    with socket.socket() as port_socket:
        port_socket.bind(('127.0.0.1', 0))
        port = port_socket.getsockname()[1]
    server_code = (
        "import sys, uvicorn; from s1grits.webapp.server import create_app; "
        "app=create_app(sys.argv[1], job_cmd_prefix=[sys.executable,'-c',"
        "\"import time; print('原始日志 🌍', flush=True); time.sleep(0.5)\",'--ignored']); "
        "uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[2]),log_level='warning')"
    )
    if getattr(request, 'param', '') == 'real':
        server_code = (
            "import sys, uvicorn; from s1grits.webapp.server import create_app; "
            "app=create_app(sys.argv[1], job_cmd_prefix=[sys.executable,'-m','s1grits']); "
            "uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[2]),log_level='warning')"
        )
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    processes = []
    connection = None
    with (tmp_path / 'server.log').open('wb') as server_log, (tmp_path / 'edge.log').open('wb') as edge_log:
        try:
            processes.append(subprocess.Popen([sys.executable, '-X', 'utf8', '-c', server_code, str(workspace), str(port)], stdout=server_log, stderr=server_log, creationflags=flags))
            root = f'http://127.0.0.1:{port}'
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    with urlopen(root + '/api/health', timeout=1) as response:
                        assert response.status == 200
                    break
                except OSError:
                    time.sleep(.1)
            else:
                pytest.fail('Local Web service did not start')
            profile = tmp_path / 'edge-profile'
            processes.append(subprocess.Popen([
                str(edge), '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
                '--remote-debugging-port=0', '--lang=en-US', f'--user-data-dir={profile}', 'about:blank',
            ], stdout=edge_log, stderr=edge_log, creationflags=flags))
            active = profile / 'DevToolsActivePort'
            deadline = time.monotonic() + 30
            while not active.is_file() and time.monotonic() < deadline:
                time.sleep(.1)
            assert active.is_file(), 'Headless Edge did not expose DevTools'
            debug_port = active.read_text().splitlines()[0]
            with urlopen(f'http://127.0.0.1:{debug_port}/json', timeout=5) as response:
                target = next(item for item in json.load(response) if item['type'] == 'page')
            connection = websocket.create_connection(target['webSocketDebuggerUrl'], timeout=30, suppress_origin=True)
            serial = 0
            errors = []

            def call(method, **params):
                nonlocal serial
                serial += 1
                request_id = serial
                connection.send(json.dumps({'id': request_id, 'method': method, 'params': params}))
                while True:
                    result = json.loads(connection.recv())
                    if result.get('method') == 'Runtime.exceptionThrown':
                        errors.append(result['params'])
                    if result.get('id') == request_id:
                        assert 'error' not in result, result
                        return result.get('result', {})

            def evaluate(expression):
                result = call('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True)
                assert 'exceptionDetails' not in result, result
                return result.get('result', {}).get('value')

            call('Page.enable')
            call('Runtime.enable')
            call('Network.enable')
            call('Network.setBlockedURLs', urls=['https://*'])
            call('Page.addScriptToEvaluateOnNewDocument', source="Object.defineProperty(navigator,'languages',{get:()=>['en-US']});")
            call('Emulation.setDeviceMetricsOverride', width=1440, height=1000, deviceScaleFactor=1, mobile=False)
            call('Page.navigate', url=root)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if evaluate("typeof state!=='undefined' && state.capabilities && typeof map!=='undefined' && map && document.querySelector('#years input')!==null"):
                    break
                time.sleep(.1)
            else:
                pytest.fail(f'Console did not initialize: {errors}')
            yield evaluate, call, workspace, errors
        finally:
            if connection:
                connection.close()
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def test_language_switch_keeps_plan_map_tasks_and_logs(browser):
    evaluate, call, workspace, errors = browser
    assert evaluate('document.documentElement.lang') == 'en'
    assert evaluate('document.title') == 'S1-GRiTS Data Cube Console'
    evaluate("window.switchLanguage=value=>{const select=document.querySelector('dialog[open] [data-language]')||$('language');select.value=value;select.dispatchEvent(new Event('change'))}")
    evaluate("window.originalMap=map; window.originalTiles=$('tiles'); window.originalYear=document.querySelector('#years input');")
    evaluate("state.selectedTiles=new Set(['17MPU']);syncTileSelection();$('output').value='中文-output';$('resolution').value='10';$('resolution').dispatchEvent(new Event('change'));document.querySelectorAll('#years input').forEach(x=>x.checked=x.value==='2025');")
    before = evaluate('JSON.stringify(payload())')
    evaluate("switchLanguage('zh-CN')")
    assert evaluate('document.title') == 'S1-GRiTS 数据立方体控制台'
    assert evaluate('JSON.stringify(payload())') == before
    assert evaluate("map===originalMap && $('tiles')===originalTiles && document.querySelector('#years input')===originalYear")
    evaluate('preflight()')
    assert evaluate("$('confirm-dialog').open && state.plan.confirmation_required")
    plan_id = evaluate('state.plan.plan_id')
    phrase = evaluate('state.plan.confirmation_phrase')
    evaluate("$('phrase').value=state.plan.confirmation_phrase;updateConfirmation()")
    evaluate("switchLanguage('en')")
    assert evaluate("$('confirm-dialog').open && !$('confirm-run').disabled")
    assert evaluate('state.plan.plan_id') == plan_id
    assert evaluate("$('phrase').value") == phrase
    assert evaluate('JSON.stringify(payload())') == before
    assert 'Estimated size' in evaluate("$('plan-summary').textContent")
    assert 'months' in evaluate("$('plan-summary').textContent")
    assert 'Enter the exact' in evaluate("$('phrase-reason').textContent")
    evaluate('confirmTask({preventDefault(){}})')
    assert evaluate("['queued','running'].includes(state.tasks[0].status)")
    running_id = evaluate('state.tasks[0].run_id')
    evaluate("switchLanguage('zh-CN');switchLanguage('en')")
    assert evaluate('state.tasks[0].run_id') == running_id
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        evaluate('loadTasks()')
        if evaluate("state.tasks[0]?.status==='done'"):
            break
        time.sleep(.1)
    assert evaluate("state.tasks[0].status") == 'done'
    evaluate("openLog(state.tasks[0].run_id)")
    deadline = time.monotonic() + 10
    while not evaluate("$('task-log-output').textContent.includes('原始日志')") and time.monotonic() < deadline:
        time.sleep(.1)
    original_log = evaluate("$('task-log-output').textContent")
    cursor = evaluate('JSON.stringify([activeLog.id,activeLog.logOffset,activeLog.eventOffset])')
    evaluate("switchLanguage('zh-CN')")
    assert evaluate("$('task-log-output').textContent") == original_log
    assert evaluate('JSON.stringify([activeLog.id,activeLog.logOffset,activeLog.eventOffset])') == cursor
    assert '完成' in evaluate("$('task-stage-track').textContent")
    evaluate("switchLanguage('en')")
    assert 'Done' in evaluate("$('task-stage-track').textContent")
    assert 'Completed' in evaluate("$('tasks').textContent")
    assert original_log.count('原始日志') == 5
    evaluate('closeTaskLog()')
    evaluate("openCatalogFolderDialog()")
    evaluate('loadCatalogFolders(' + json.dumps(str(workspace)) + ')')
    assert 'Open' in evaluate("$('catalog-folder-list').textContent")
    evaluate("window.folderButton=document.querySelector('.catalog-folder-entry');switchLanguage('zh-CN')")
    assert evaluate("document.querySelector('.catalog-folder-entry')===folderButton && typeof folderButton.onclick==='function'")
    assert '进入' in evaluate("$('catalog-folder-list').textContent")
    evaluate("switchLanguage('en')")
    assert 'Open' in evaluate("$('catalog-folder-list').textContent")
    evaluate('closeCatalogFolderDialog()')
    assert evaluate("openCatalogSelection(catalogRoot('workspace'),'',{autoQuery:true})")
    assert evaluate("getComputedStyle($('catalog-empty')).display") == 'none'
    assert evaluate("$('catalog-selection-name').textContent") == 'Server output'
    assert evaluate("getComputedStyle($('catalog-discovery')).display") == 'none'
    evaluate('generateCatalogReport()')
    assert 'Available months' in evaluate("$('report-summary').textContent")
    evaluate("currentCatalogLayers.get('17MPU').openPopup();void 0")
    assert 'View records' in evaluate("document.querySelector('.catalog-popup').textContent")
    evaluate("switchLanguage('zh-CN')")
    assert '查看详细记录' in evaluate("document.querySelector('.catalog-popup').textContent")
    assert '有效月份' in evaluate("$('report-summary').textContent")
    assert evaluate("getComputedStyle($('catalog-empty')).display") == 'none'
    evaluate("switchLanguage('en');openCatalogTileDetails('17MPU')")
    assert 'Local data details' in evaluate("$('catalog-detail-title').textContent")
    assert '17MPU_ASCENDING_TK18' in evaluate("$('catalog-detail-body').textContent")
    evaluate("$('catalog-detail-dialog').close();map.closePopup();void 0")
    error = evaluate("api('/api/catalog-roots',{method:'POST',body:JSON.stringify({path:'relative'})}).catch(e=>e.message)")
    assert 'Request failed' in error and 'Server details' in error
    assert not errors, errors
    evidence = os.environ.get('S1GRITS_BROWSER_EVIDENCE')
    if evidence:
        target = Path(evidence)
        target.mkdir(parents=True, exist_ok=True)
        for lang in ('en', 'zh-CN'):
            evaluate(f"S1I18n.setLanguage('{lang}')")
            png = call('Page.captureScreenshot', format='png')['data']
            (target / f'console-{lang}.png').write_bytes(base64.b64decode(png))
        (target / 'browser.json').write_text(json.dumps({'plan_id': plan_id, 'request': json.loads(before), 'log': original_log, 'exceptions': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
    evaluate("S1I18n.setLanguage('zh-CN')")
    call('Page.reload')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if evaluate("typeof S1I18n!=='undefined' && document.title==='S1-GRiTS 数据立方体控制台'"):
            break
        time.sleep(.1)
    assert evaluate("localStorage.getItem('s1grits_language')") == 'zh-CN'
    assert evaluate('document.documentElement.lang') == 'zh-CN'


def test_product_selection_and_point_read_in_both_languages(browser, grouped):
    import shutil
    import numpy as np
    import zarr
    from s1grits.analysis import define_view, register_view
    from s1grits.zarr_encoding import create_zarr_array
    from test_cube_derivatives import hashes
    evaluate, call, workspace, errors = browser
    root, _, _ = grouped
    shutil.copytree(root/'bundle.zarr', workspace/'bundle.zarr')
    group = zarr.open_group(str(workspace/'bundle.zarr'), path='products/b', mode='r+')
    create_zarr_array(group, 'angle', data=np.full((24, 30), 45, dtype='f4'), dimension_names=['y', 'x'])
    shutil.copyfile(root/'catalog.parquet', workspace/'catalog.parquet')
    view = register_view(workspace, define_view(workspace, 'b', start='2026-01-01', end='2026-03-01', window=[0, 0, 2, 2], bands=['VV_dB']))
    before = hashes(workspace/'bundle.zarr')
    assert evaluate("openCatalogSelection(catalogRoot('workspace'),'',{autoQuery:true})")
    # Exercise actual button bindings, including the map-query generation argument.
    evaluate("$('cat-signature').value='p_b';$('query-catalog').click();void 0")
    deadline = time.monotonic()+10
    while time.monotonic()<deadline and not evaluate("state.catalogCoverage?.total_records===1 && !$('query-catalog').disabled"):
        time.sleep(.05)
    assert evaluate('state.catalogCoverage.total_records') == 1
    evaluate("$('list-catalog').click();void 0")
    deadline = time.monotonic()+10
    while time.monotonic()<deadline and not evaluate("!!document.querySelector('[data-product-id=\"b\"]')"):
        time.sleep(.05)
    assert evaluate("document.querySelectorAll('[data-product-id]').length") == 1
    evaluate("document.querySelector('[data-product-id=\"b\"]').click();void 0")
    deadline = time.monotonic()+10
    while time.monotonic()<deadline and not evaluate("selectedProduct?.data.record.item_id==='b'"):
        time.sleep(.05)
    assert evaluate('selectedProduct.data.attributes.generation_id') == 'gen-b'
    evaluate("$('sample-band').value='angle';$('sample-band').dispatchEvent(new Event('change'));readProductSample()")
    assert evaluate("$('sample-start').disabled && $('sample-end').disabled")
    assert evaluate("[...document.querySelectorAll('#product-sample tr td:last-child')].map(n=>n.textContent)") == ['45']
    evaluate("$('sample-band').value='VV_dB';$('sample-band').dispatchEvent(new Event('change'))")
    assert not evaluate("$('sample-start').disabled")
    evaluate("$('sample-band').value='VV_dB';$('sample-row').value='1';$('sample-col').value='1';$('sample-start').value='2026-01-13';$('sample-end').value='2026-01-25';window.originalBand=$('sample-band');S1I18n.setLanguage('zh-CN')")
    assert evaluate("$('sample-band')===originalBand && $('sample-row').value==='1' && selectedProduct.data.record.item_id==='b'")
    evaluate('readProductSample()')
    assert evaluate("[...document.querySelectorAll('#product-sample tr td:last-child')].map(n=>n.textContent)") == ['9', '9']
    evaluate("S1I18n.setLanguage('en')")
    assert '2 values' in evaluate("$('product-message').textContent")
    assert evaluate("$('sample-start').value") == '2026-01-13'
    evaluate("$('catalog-detail-dialog').close();void 0")
    # Wait for the native close event before opening another selection.
    evaluate('new Promise(resolve=>setTimeout(resolve,50))')
    evaluate("$('cat-signature').value='';$('cat-representation').value='virtual';openCatalogTileDetails(null)")
    assert evaluate("document.querySelectorAll('[data-product-id]').length") == 1
    evaluate('selectCatalogProduct('+json.dumps(view['item_id'])+')')
    assert evaluate('selectedProduct.data.record.representation') == 'virtual'
    assert evaluate('selectedProduct.data.sizes.x') == 2
    evaluate("$('sample-band').value='VV_dB';readProductSample()")
    assert evaluate("[...document.querySelectorAll('#product-sample tr td:last-child')].map(n=>n.textContent)") == ['9']*4
    assert evaluate("$('catalog-detail-dialog').scrollWidth <= $('catalog-detail-dialog').clientWidth+1")
    assert evaluate("$('sample-band').getBoundingClientRect().left >= $('catalog-detail-dialog').getBoundingClientRect().left")
    evidence = os.environ.get('S1GRITS_BROWSER_EVIDENCE')
    if evidence:
        target = Path(evidence)
        target.mkdir(parents=True, exist_ok=True)
        for lang in ('en', 'zh-CN'):
            evaluate(f"S1I18n.setLanguage('{lang}')")
            (target/f'product-{lang}.png').write_bytes(base64.b64decode(call('Page.captureScreenshot', format='png')['data']))
        (target/'products.json').write_text(json.dumps({'item_id':view['item_id'],'values':[9]*4,'exceptions':errors},indent=2),encoding='utf-8')
    assert hashes(workspace/'bundle.zarr') == before
    assert not errors, errors


@pytest.mark.parametrize('browser', ['real'], indirect=True)
def test_derive_preflight_execute_and_inspect_in_both_languages(browser, grouped):
    import shutil
    from test_cube_derivatives import hashes
    from s1grits.resolver import CubeResolver
    evaluate, call, workspace, errors = browser
    root, _, _ = grouped
    shutil.copytree(root/'bundle.zarr', workspace/'bundle.zarr')
    shutil.copyfile(root/'catalog.parquet', workspace/'catalog.parquet')
    before = hashes(workspace/'bundle.zarr')
    assert evaluate("openCatalogSelection(catalogRoot('workspace'),'',{autoQuery:true})")
    evaluate("openCatalogTileDetails(null)")
    evaluate("selectCatalogProduct('b')")
    assert not evaluate("$('derive-panel').hidden")
    evaluate("$('derive-panel').open=true;$('derive-output').value='derived/browser';$('derive-width').value='8';$('derive-height').value='8'")
    evaluate("S1I18n.setLanguage('zh-CN');preflightDerive({preventDefault(){}})")
    assert evaluate("$('confirm-dialog').open"), evaluate("$('derive-message').textContent")
    planned = evaluate('JSON.stringify(state.plan.request)')
    evaluate("S1I18n.setLanguage('en')")
    assert evaluate('JSON.stringify(state.plan.request)') == planned
    assert 'Confirm' in evaluate("$('confirm-title').textContent")
    assert evaluate("$('confirm-dialog').scrollWidth <= $('confirm-dialog').clientWidth+1")
    evidence = os.environ.get('S1GRITS_BROWSER_EVIDENCE')
    if evidence:
        target = Path(evidence)
        target.mkdir(parents=True, exist_ok=True)
        for lang in ('en','zh-CN'):
            evaluate(f"S1I18n.setLanguage('{lang}')")
            (target/f'derive-{lang}.png').write_bytes(base64.b64decode(call('Page.captureScreenshot',format='png')['data']))
    evaluate('confirmTask({preventDefault(){}})')
    deadline = time.monotonic()+60
    while time.monotonic()<deadline:
        evaluate('loadTasks()')
        if evaluate("['done','failed'].includes(state.tasks[0]?.status)"):
            break
        time.sleep(.1)
    assert evaluate('state.tasks[0].status') == 'done', evaluate('JSON.stringify(state.tasks[0])')
    assert evaluate('state.tasks[0].result_subdir') == 'derived/browser'
    assert evaluate("openCatalogSelection(catalogRoot('workspace'),'derived/browser',{autoQuery:true})")
    evaluate('openCatalogTileDetails(null)')
    item_id = CubeResolver(workspace/'derived/browser').catalog.iloc[0].item_id
    evaluate('selectCatalogProduct('+json.dumps(item_id)+')')
    evaluate("$('sample-band').value='Ratio';readProductSample()")
    assert evaluate("document.querySelectorAll('#product-sample tr').length") == 4
    assert hashes(workspace/'bundle.zarr') == before
    assert not errors, errors
    if evidence:
        (target/'derive.json').write_text(json.dumps({'request':json.loads(planned),'result_item_id':item_id,
            'sample':evaluate("$('product-sample').textContent"),'exceptions':errors},indent=2),encoding='utf-8')


def run_view_form(browser, name):
    evaluate, call, _, _ = browser
    evaluate("$('view-panel').open=true;$('view-form').requestSubmit();void 0")
    deadline=time.monotonic()+15
    while time.monotonic()<deadline and not evaluate("$('confirm-dialog').open"):
        time.sleep(.05)
    assert evaluate("$('confirm-dialog').open"), evaluate("$('view-message').textContent")
    request=evaluate('JSON.stringify(state.plan.request)')
    item=evaluate('state.plan.result_item_id')
    evidence=os.environ.get('S1GRITS_BROWSER_EVIDENCE')
    for lang in ('zh-CN','en'):
        evaluate(f"S1I18n.setLanguage('{lang}')")
        assert evaluate('JSON.stringify(state.plan.request)')==request
        assert evaluate("$('confirm-dialog').scrollWidth <= $('confirm-dialog').clientWidth+1")
        if evidence:
            target=Path(evidence);target.mkdir(parents=True,exist_ok=True)
            (target/f'{name}-{lang}.png').write_bytes(base64.b64decode(call('Page.captureScreenshot',format='png')['data']))
    evaluate('confirmTask({preventDefault(){}})')
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        evaluate('loadTasks()')
        if evaluate("['done','failed'].includes(state.tasks[0]?.status)"):
            break
        time.sleep(.1)
    assert evaluate('state.tasks[0].status')=='done',evaluate('JSON.stringify(state.tasks[0])')
    assert evaluate('state.tasks[0].result_item_id')==item
    # Exercise the actual result button and its explicit item-id locator.
    evaluate("document.querySelector('#tasks .task-card .task-actions button:last-child').click();void 0")
    deadline=time.monotonic()+15
    while time.monotonic()<deadline and evaluate('selectedProduct?.data.record.item_id')!=item:
        time.sleep(.05)
    assert evaluate('selectedProduct.data.record.item_id')==item
    if evidence:
        (target/f'{name}.json').write_text(json.dumps({'request':json.loads(request),'result_item_id':item},indent=2),encoding='utf-8')
    return item


@pytest.mark.parametrize('browser',['real'],indirect=True)
def test_virtual_group_view_and_explicit_materialization(browser,grouped):
    import shutil
    from test_cube_derivatives import hashes
    evaluate,_,workspace,errors=browser
    root,_,_=grouped
    shutil.copytree(root/'bundle.zarr',workspace/'bundle.zarr')
    shutil.copyfile(root/'catalog.parquet',workspace/'catalog.parquet')
    before=hashes(workspace/'bundle.zarr')
    assert evaluate("openCatalogSelection(catalogRoot('workspace'),'',{autoQuery:true})")
    evaluate("openCatalogTileDetails(null);" )
    evaluate("selectCatalogProduct('b')")
    evaluate("$('view-output').value='derived/selection';$('view-width').value='2';$('view-height').value='2'")
    run_view_form(browser,'selection-view')
    assert evaluate('selectedProduct.data.record.representation')=='virtual'
    assert not list((workspace/'derived/selection').rglob('*.zarr'))
    evaluate("$('sample-band').value='Ratio';readProductSample()")
    expected=evaluate("$('product-sample').textContent")
    assert evaluate("$('view-operation').value")=='view_materialize'
    evaluate("$('view-output').value='derived/materialized'")
    run_view_form(browser,'materialized-view')
    assert evaluate('selectedProduct.data.record.representation')=='physical'
    evaluate("$('sample-band').value='Ratio';readProductSample()")
    assert evaluate("$('product-sample').textContent")==expected
    assert hashes(workspace/'bundle.zarr')==before
    assert not errors,errors


@pytest.mark.parametrize('browser',['real'],indirect=True)
def test_multitrack_order_selection_and_result(browser):
    from test_multitrack_views import monthly_parents
    from test_cube_derivatives import hashes
    evaluate,_,workspace,errors=browser
    rows,stores=monthly_parents(workspace/'parents')
    before=[hashes(p) for p in stores]
    assert evaluate("openCatalogSelection(catalogRoot('workspace'),'parents/catalog',{autoQuery:true})")
    evaluate('openCatalogTileDetails(null)')
    for row in rows[::-1]:
        evaluate('selectCatalogProduct('+json.dumps(row['item_id'])+')')
        evaluate("$('view-operation').value='view_multitrack';$('view-operation').dispatchEvent(new Event('change'));$('view-add-parent').click();void 0")
    assert evaluate("$('view-parents').value.split('\\n')")==[r['item_id'] for r in rows[::-1]]
    evaluate("$('view-output').value='derived/multitrack';$('view-start-month').value='2026-01';$('view-end-month').value='2026-03';$('view-method').value='mean'")
    run_view_form(browser,'multitrack-view')
    assert evaluate('selectedProduct.data.record.track') is None
    assert evaluate('selectedProduct.data.record.representation')=='virtual'
    evaluate("$('sample-band').value='n_contributing_tracks';readProductSample()")
    assert evaluate("[...document.querySelectorAll('#product-sample tr td:last-child')].map(n=>n.textContent)")==['2','0','1']
    assert [hashes(p) for p in stores]==before
    assert not errors,errors
