"""Exercise the Cube skill's existing config/CLI/restart route with tiny COGs."""
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest
import yaml

from s1grits import cli
from s1grits.resolver import CubeResolver
from test_provenance_workflow import Case, _pixel_snapshot, _tables, TILE


def test_config_cli_and_no_agent_resume(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    environment = dict(os.environ, PYTHONIOENCODING='utf-8')
    command = [sys.executable, '-m', 's1grits']
    config_path = tmp_path/'bounded.yaml'

    def invoke(*args):
        result = subprocess.run(command+list(map(str, args)), cwd=repo, env=environment,
                                capture_output=True, encoding='utf-8', timeout=120)
        assert result.returncode == 0, result.stdout+result.stderr
        return result

    invoke('init', config_path)
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    assert 'full' in config['time']
    # Only query/download boundaries are fixtures; YAML, decoder and writers run normally.
    case = Case(tmp_path/'case', monkeypatch)
    config.update(case.config)
    config['time'] = {'years': [2025], 'months': [1]}
    config['processing']['monthly'].update(generate_cog=False, generate_preview=False)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
    assert 'full' not in config['time']
    doctor = invoke('doctor', '--config', config_path)
    (tmp_path/'doctor.log').write_text(doctor.stdout+doctor.stderr, encoding='utf-8')

    monkeypatch.setattr(sys, 'argv', ['s1grits', 'process_scenes', '--config', str(config_path)])
    with pytest.raises(SystemExit) as completed:
        cli.main()
    assert completed.value.code == 0
    assert case.calls and case.queries
    pointer, (outputs, sources, links) = _tables(case.root)
    assert len([row for row in outputs if row['generation_id']]) == 2
    assert sources and links
    before = _pixel_snapshot(case.root)
    provenance_before = {p.relative_to(case.root).as_posix(): p.read_bytes()
                         for p in (case.root/'provenance/runs').glob('*/receipt.json')}
    restarted = invoke('process_scenes', '--config', config_path)
    (tmp_path/'resume.log').write_text(restarted.stdout+restarted.stderr, encoding='utf-8')
    assert _pixel_snapshot(case.root) == before
    assert all((case.root/p).read_bytes() == value for p, value in provenance_before.items())
    assert _tables(case.root)[0] == pointer
    assert not list((case.root/'provenance/pending').glob('*.json'))

    invoke('catalog', 'inspect', '--output-dir', case.root)
    resolver = CubeResolver(case.root)
    record = resolver.query(tile_id=TILE, product_type='smonthly', track=18,
                            time_start='2025-01-01', time_end='2025-01-31').iloc[0]
    with resolver.open(record) as dataset:
        times = pd.to_datetime(dataset.time.values).strftime('%Y-%m')
        index = list(times).index('2025-01')
        assert dataset.sizes['x'] == 4 and dataset.sizes['y'] == 3
        assert np.isfinite(dataset.VV_dB.isel(time=index)).any()
        assert (dataset.n_obs.isel(time=index) > 0).any()
    (tmp_path/'skill-replay.json').write_text(json.dumps({
        'scope': 'synthetic offline COGs; query and byte download fixtures only',
        'config': str(config_path), 'initial_command': command+['process_scenes', '--config', str(config_path)],
        'resume_command': command+['process_scenes', '--config', str(config_path)],
        'resume_without_agent_or_fixtures': True, 'pixels_coordinates_and_receipts_unchanged': True,
        'selected_item': record.item_id, 'selected_track': int(record.track),
        'completed_generations': 2, 'pending': 0,
    }, indent=2), encoding='utf-8')
