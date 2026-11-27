"""Real local CLI results through the bilingual browser controls."""
import base64
import json
import os
from pathlib import Path
import time

import pytest
from test_webapp_i18n_browser import browser as browser, pytestmark as pytestmark
from test_cube_derivatives import hashes


def run_form(browser, prefix):
    evaluate, call, _, errors = browser
    evaluate(f"$('{prefix}-panel').open=true;$('{prefix}-form').requestSubmit();void 0")
    deadline=time.monotonic()+20
    while time.monotonic()<deadline and not evaluate("$('confirm-dialog').open"):
        time.sleep(.05)
    assert evaluate("$('confirm-dialog').open"), evaluate(f"$('{prefix}-message').textContent")
    request=evaluate('JSON.stringify(state.plan.request)')
    for language in ('zh-CN','en'):
        evaluate(f"S1I18n.setLanguage('{language}')")
        assert evaluate('JSON.stringify(state.plan.request)')==request
        assert evaluate("$('confirm-dialog').scrollWidth <= $('confirm-dialog').clientWidth+1")
    evaluate('confirmTask({preventDefault(){}})')
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        evaluate('loadTasks()')
        if evaluate("['done','failed'].includes(state.tasks[0]?.status)"):
            break
        time.sleep(.1)
    assert evaluate('state.tasks[0].status')=='done',evaluate('JSON.stringify(state.tasks[0])')
    evaluate("document.querySelector('#tasks .task-card .task-actions button:last-child').click();void 0")
    deadline=time.monotonic()+10
    while time.monotonic()<deadline and not evaluate("$('tool-result-body').querySelector('details')!==null"):
        time.sleep(.05)
    assert evaluate("$('tool-result-dialog').open")
    assert evaluate("$('tool-result-body').textContent.includes('complete')")
    evidence=os.environ.get('S1GRITS_BROWSER_EVIDENCE')
    for language in ('zh-CN','en'):
        evaluate(f"S1I18n.setLanguage('{language}')")
        assert evaluate("$('tool-result-dialog').scrollWidth <= $('tool-result-dialog').clientWidth+1")
        if evidence:
            target=Path(evidence);target.mkdir(parents=True,exist_ok=True)
            (target/f'{prefix}-{language}.png').write_bytes(base64.b64decode(call('Page.captureScreenshot',format='png')['data']))
    if evidence:
        (target/f'{prefix}.json').write_text(json.dumps(dict(request=json.loads(request),
            task=evaluate('state.tasks[0]'),exceptions=errors),ensure_ascii=False,indent=2),encoding='utf-8')
    assert not errors, errors


@pytest.mark.parametrize('browser',['real'],indirect=True)
def test_landtrendr_window(browser):
    pytest.importorskip('landtrendr')
    from test_cube_segmentation import parent
    evaluate,_,workspace,_=browser
    parent(workspace/'parent')
    before=hashes(workspace/'parent')
    assert evaluate("openCatalogSelection(catalogRoot('workspace'),'parent',{autoQuery:true})")
    evaluate('openCatalogTileDetails(null)')
    evaluate("selectCatalogProduct('parent')")
    assert not evaluate("$('analysis-panel').hidden || $('plan-analysis').disabled")
    evaluate("$('analysis-mode').value='window';syncAnalysisMode();$('analysis-width').value=2;$('analysis-height').value=2;$('analysis-method').value='median';$('analysis-domain').value='db';$('analysis-direction').value='decrease';$('analysis-output').value='analysis/result'")
    run_form(browser,'analysis')
    assert evaluate("$('tool-result-body').textContent.includes('insufficient_observations')")
    assert hashes(workspace/'parent')==before


@pytest.mark.parametrize('browser',['real'],indirect=True)
def test_cog_mosaic(browser):
    from test_grid_mosaic import cube
    evaluate,_,workspace,_=browser
    cube(workspace/'parent')
    before=hashes(workspace/'parent')
    assert evaluate("openCatalogSelection(catalogRoot('workspace'),'parent',{autoQuery:true})")
    evaluate('openCatalogTileDetails(null)')
    evaluate("document.querySelector('[data-mosaic-id=i0]').click();document.querySelector('[data-mosaic-id=i1]').click();void 0")
    assert evaluate("$('mosaic-parents').value")== 'i0\ni1'
    evaluate("$('mosaic-crs').value='EPSG:32717';$('mosaic-bounds').value='600000 9899880 600210 9900000';$('mosaic-format').value='COG';$('mosaic-output').value='analysis/mosaic'")
    run_form(browser,'mosaic')
    assert evaluate("$('tool-result-body').textContent.includes('mosaic.tif')")
    assert hashes(workspace/'parent')==before
