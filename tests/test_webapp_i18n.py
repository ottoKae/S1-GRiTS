"""Bilingual resource completeness and actual JavaScript locale selection."""
import json
from html.parser import HTMLParser
from importlib.resources import files
import re
import shutil
import subprocess

import pytest

STATIC = files('s1grits.webapp') / 'static'


def test_display_catalog_keys_and_placeholders_match():
    source = (STATIC / 'i18n.js').read_text(encoding='utf-8')
    catalog, _ = json.JSONDecoder().raw_decode(source.split('const S1_MESSAGES = ', 1)[1])
    used = set()

    class Keys(HTMLParser):
        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                if name.startswith('data-i18n'):
                    used.add(value)

    Keys().feed((STATIC / 'index.html').read_text(encoding='utf-8'))
    script = (STATIC / 'app.js').read_text(encoding='utf-8')
    for quoted in re.findall(r'\bt\(("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')', script):
        used.add(json.loads(quoted) if quoted.startswith('"') else quoted[1:-1])
    assert used <= catalog.keys(), used - catalog.keys()
    for key, pair in catalog.items():
        assert len(pair) == 2 and all(isinstance(value, str) and value for value in pair), key
        assert set(re.findall(r'\{p\d+\}', pair[0])) == set(re.findall(r'\{p\d+\}', pair[1])), key
        assert not re.search(r'[\u3400-\u9fff]', pair[1]), key


@pytest.mark.parametrize(('browser_language', 'saved', 'expected'), [
    ('zh-CN', None, 'zh-CN'), ('zh-TW', None, 'zh-CN'), ('en-US', None, 'en'),
    ('de-DE', None, 'en'), ('en-US', 'zh-CN', 'zh-CN'), ('zh-CN', 'en', 'en'),
    ('zh-CN', 'invalid', 'zh-CN'),
])
def test_js_language_preference_and_parameter_substitution(browser_language, saved, expected):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable; real-browser acceptance is recorded separately')
    source = (STATIC / 'i18n.js').read_text(encoding='utf-8')
    # DOM/storage are the boundary; execute the actual packaged language module.
    script = "const vm=require('node:vm');const context={navigator:{languages:[" + json.dumps(browser_language) + "]},localStorage:{getItem:()=>" + json.dumps(saved) + ",setItem(){}},document:{documentElement:{},querySelectorAll:()=>[],dispatchEvent(){}},Event:class{}};"
    script += 'vm.createContext(context);vm.runInContext(' + json.dumps(source) + ',context);'
    script += "process.stdout.write(JSON.stringify(vm.runInContext(`({initial:S1I18n.language,zh:(S1I18n.setLanguage('zh-CN'),S1I18n.t('任务 {p0} 已进入队列。',{p0:'中文-$&-{p1}'})),en:(S1I18n.setLanguage('en'),S1I18n.t('任务 {p0} 已进入队列。',{p0:'中文-$&-{p1}'}))})`,context)));"
    result = subprocess.run([node, '-'], input=script, text=True, encoding='utf-8', capture_output=True)
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values == {'initial': expected, 'zh': '任务 中文-$&-{p1} 已进入队列。', 'en': 'Task 中文-$&-{p1} queued.'}
