"""Exercise the public tutorial against real files and the existing CLI/API."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from s1grits.analysis import derive_cube
from s1grits.resolver import CubeResolver


ROOT = Path(__file__).resolve().parents[1]


def invoke(*args):
    return subprocess.run([sys.executable, *map(str, args)], cwd=ROOT,
                          env=dict(os.environ, PYTHONPATH=str(ROOT / 'src'), PYTHONIOENCODING='utf-8'),
                          capture_output=True, encoding='utf-8')


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('method, expected_db', [('mean', 6.020599913), ('median', 3.010299957)])
def test_tutorial(tmp_path, method, expected_db):
    parent, output = tmp_path / 'input', tmp_path / 'result'
    created = invoke('tools/create_demo_cube.py', '--output', parent)
    assert created.returncode == 0, created.stdout + created.stderr
    before = hashes(parent)
    resolver = CubeResolver(parent)
    record = resolver.catalog.iloc[0]
    assert record.item_id == 'demo-scenes'
    with resolver.open(record) as ds:
        assert dict(ds.sizes) == {'time': 4, 'x': 5, 'y': 4}
        assert ds.VV_dB.isel(time=0, y=2, x=2).values.item() == 0
    inspected = invoke('-m', 's1grits', 'catalog', 'inspect', '--output-dir', parent)
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    command = ['-m', 's1grits', 'derive_cube', '--catalog-root', parent,
               '--item-id', 'demo-scenes', '--output', output, '--start', '2026-01-01',
               '--end', '2026-03-31T23:59:59Z', '--features', 'ratio', 'rvi',
               '--filter', 'none', '--monthly', method]
    proc = invoke(*command)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = CubeResolver(output)
    with result.open(result.catalog.iloc[0]) as ds:
        np.testing.assert_allclose(ds.VV_dB[:, 2, 2], [expected_db, np.nan, 6.020599913], atol=2e-6)
        np.testing.assert_allclose(ds.Ratio[:, 2, 2], [.25, np.nan, .25], atol=1e-6)
        np.testing.assert_allclose(ds.RVI[:, 2, 2], [.8, np.nan, .8], atol=1e-6)
        np.testing.assert_array_equal(ds.n_obs[:, 2, 2], [3, 0, 1])
        np.testing.assert_array_equal(ds.n_obs[:, 1, 1], [2, 0, 1])
        assert np.isnan(ds.VV_dB[:, 0, 0]).all()
        assert (ds.n_obs[:, 0, 0] == 0).all()
        np.testing.assert_array_equal(ds.x.values, 600000 + (np.arange(5) + .5) * 30)
        np.testing.assert_array_equal(ds.y.values, 9900000 - (np.arange(4) + .5) * 30)
    receipt = json.loads((output / 'derivation.json').read_text())
    assert receipt['status'] == 'complete'
    assert receipt['contract']['parent_item_id'] == 'demo-scenes'
    assert receipt['contract']['parent_generation'] == 'demo-v1'
    direct = derive_cube(parent, 'demo-scenes', tmp_path / 'api', start='2026-01-01',
                         end='2026-03-31T23:59:59Z', monthly=method)
    assert direct['processing_signature'] == result.catalog.iloc[0].processing_signature
    with CubeResolver(tmp_path / 'api').open(direct) as api, result.open(result.catalog.iloc[0]) as cli:
        for band in ('VV_dB', 'VH_dB', 'Ratio', 'RVI', 'n_obs'):
            np.testing.assert_array_equal(api[band].values, cli[band].values)
    completed = hashes(output)
    assert invoke(*command).returncode != 0
    assert hashes(output) == completed
    assert invoke('tools/create_demo_cube.py', '--output', parent).returncode != 0
    assert hashes(parent) == before
