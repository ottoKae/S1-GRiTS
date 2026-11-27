"""The default installation must remain usable without the optional detector."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_cusum_is_lazy():
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')+os.pathsep+os.environ.get('PYTHONPATH', ''))
    result = subprocess.run([sys.executable, '-c',
        "import sys; from s1grits.analysis import analyze_cusum; assert 'nrt' not in sys.modules"],
        capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout+result.stderr


def test_missing_backend_does_not_write(tmp_path):
    if importlib.util.find_spec('nrt') is not None:
        pytest.skip('Runs in the core environment without optional nrt')
    from s1grits.analysis import analyze_cusum

    with pytest.raises(ImportError, match='nrt==0.3.0'):
        analyze_cusum(tmp_path/'source', 'item', tmp_path/'out', band='VV_dB',
                      history_start='2020', history_end='2021', monitor_start='2022', monitor_end='2023')
    assert not (tmp_path/'out').exists()
