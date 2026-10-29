"""Ordinary workflow writes must respect interrupted provenance operations."""
from __future__ import annotations

from copy import deepcopy
import concurrent.futures
import json

import pytest

from s1grits import file_lock, preflight, workflow
from s1grits import workflow_scenes as scenes


@pytest.fixture
def workflow_case(tmp_path, monkeypatch):
    root = tmp_path / "dataset"
    root.mkdir()
    imagery = root / "existing-imagery.bin"
    imagery.write_bytes(b"existing imagery")
    config = {
        "output": {"base_dir": str(root)},
        "processing": {"target_resolution": 30},
        "runtime": {"enabled": False},
        "parallel": {"enabled": False, "max_workers": 1},
    }
    events, handles = [], []
    monkeypatch.setattr(scenes, "load_config", lambda path: deepcopy(config))
    monkeypatch.setattr(workflow, "apply_output_overrides_and_stac", lambda value, overrides: value)
    monkeypatch.setattr(scenes, "enumerate_mgrs_tiles", lambda value: ["18NUF"])
    monkeypatch.setattr(scenes, "parse_time_range_config", lambda *args, **kwargs: [])
    monkeypatch.setattr(preflight, "check_disk_space", lambda *args, **kwargs: None)

    def acquire(path):
        handle = file_lock.acquire_lock(path, timeout=0.1)
        handles.append(handle)
        events.append("acquire")
        return handle

    def release(handle):
        events.append("release")
        file_lock.release_lock(handle)

    def process(tile, ranges, value, output_root):
        events.append("pipeline")
        imagery.write_bytes(b"pipeline output")
        return {"status": "success", "written_scenes": 1}

    def no_process_pool(*args, **kwargs):
        raise AssertionError("Pending guard must run before creating production workers")

    monkeypatch.setattr(scenes, "acquire_lock", acquire)
    monkeypatch.setattr(scenes, "release_lock", release)
    monkeypatch.setattr(scenes, "process_single_scenes_tile", process)
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", no_process_pool)
    return root, imagery, config, events, handles


@pytest.mark.parametrize("parallel", [False, True])
def test_pending_blocks_legacy_workers_and_releases_real_root_lock(workflow_case, parallel):
    root, imagery, config, events, handles = workflow_case
    config["parallel"]["enabled"] = parallel
    pending = root / "provenance/pending/run-interrupted.json"
    pending.parent.mkdir(parents=True)
    pending.write_text(json.dumps({"run_id": "run-interrupted", "phase": "writing"}),
                       encoding="utf-8")
    before = pending.read_bytes()
    with pytest.raises(ValueError, match="pending"):
        scenes.run_scenes_workflow("fixture.yaml")
    assert events == ["acquire", "release"]
    assert handles[0][1].closed
    assert imagery.read_bytes() == b"existing imagery"
    assert pending.read_bytes() == before
    assert not (root / "catalog.parquet").exists()
    # A fresh acquisition proves rejection did not strand the actual OS lock.
    handle = file_lock.acquire_lock(root, timeout=0.1)
    file_lock.release_lock(handle)


@pytest.mark.parametrize("empty_pending_directory", [False, True])
def test_legacy_workflow_without_pending_still_runs(workflow_case, empty_pending_directory):
    root, imagery, _, events, handles = workflow_case
    if empty_pending_directory:
        (root / "provenance/pending").mkdir(parents=True)
    result = scenes.run_scenes_workflow("fixture.yaml")
    assert result == {"18NUF": {"status": "success", "written_scenes": 1}}
    assert events == ["acquire", "pipeline", "release"]
    assert handles[0][1].closed
    assert imagery.read_bytes() == b"pipeline output"
