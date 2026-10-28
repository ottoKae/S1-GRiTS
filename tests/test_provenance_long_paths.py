"""Real long-path publication, verification, and orphan recovery on Windows."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np
import pytest
import zarr

from s1grits import provenance as p
from s1grits.file_lock import output_lock
from s1grits.provenance_production import ProductionRun


def _physical(path):
    """Independent test access to files whose normal path exceeds MAX_PATH."""
    path = Path(path).absolute()
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(path))
    return path


def _snapshot(root):
    root = _physical(root)
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def long_paths(tmp_path):
    source = tmp_path / "source"
    store = "18NUF/monthly.zarr"
    group = zarr.open_group(str(source / store), mode="w", zarr_format=3)
    group.attrs.update({"product_type": "smonthly", "grid_id": "grid-30",
                        "geometry_group_id": "18NUF_ASCENDING_TK120"})
    group.create_array("time", data=np.array(["2025-01-15"], dtype="datetime64[ns]"))
    group.create_array("VV", data=np.arange(12, dtype="f4").reshape(1, 3, 4), chunks=(1, 3, 4))
    output = tmp_path / "companion"
    # The root fits ordinary Windows paths, but the real immutable release and
    # run evidence paths do not. Do not shorten the production identifiers.
    while len(str(output)) < 180:
        output /= "nested-" + "x" * 25
    assert len(str(output / "provenance/releases" / ("meta-" + "a" * 64)
                   / "evidence/data_witnesses.json")) > 260
    return source, output, store


def test_long_detached_export_verify_and_repeat_leave_source_unchanged(long_paths):
    source, output, store = long_paths
    before = _snapshot(source)
    kwargs = dict(dataset_root=source, output_root=output, dataset_id="long-historical",
                  store_paths=[store], quiescent=True)
    first = p.publish_inventory(**kwargs)
    assert p.verify_bundle(output, dataset_root=source)["valid"]
    published = _snapshot(output)
    repeat = p.publish_inventory(**kwargs, expected_release_id=first["metadata_release_id"])
    assert repeat["unchanged"]
    assert _snapshot(source) == before and _snapshot(output) == published
    release = output / "provenance/releases" / first["metadata_release_id"]
    # Both public callers using ordinary paths and inherited extended paths
    # must see every descendant, not just the files nearest the root.
    assert p._manifest(release) == p._manifest(_physical(release))
    assert "evidence/data_witnesses.json" in p._manifest(release)
    assert p._verify_release(release, first)["delivery_kind"] == "metadata_only"


def test_long_run_evidence_survives_orphan_recovery_and_previous_loading(long_paths, monkeypatch):
    source, output, store = long_paths
    before = _snapshot(source)
    selection = {"store_paths": [store], "start": None, "end": None}
    inventory = p.collect_inventory(source, **selection)
    run_id = "op-" + "b" * 40
    key = f"runs/{run_id}/receipt.json"
    run_evidence = {
        key: {"run_id": run_id, "status": "committed"},
        f"runs/{run_id}/catalog_plan.json": {"run_id": run_id, "targets": {}},
        f"runs/{run_id}/catalog_receipt.json": {"run_id": run_id, "catalog_hashes": {}},
    }
    inventory["evidence"].update(run_evidence)
    for row in inventory["outputs"]:
        row.update(generation_id="generation-first", run_id=run_id, processing_id="processing-fixed",
                   evidence_status="execution_recorded", evidence_ref="evidence/" + key,
                   unknown_reason=None)
    kwargs = dict(dataset_id="long-production", inventory=inventory, selection=selection,
                  execution_kind="production", extra_dataset={"committed_run_id": run_id})
    atomic = p._atomic_json

    def fail_before_pointer(*args, **kw):
        raise OSError("interrupted before current pointer")

    monkeypatch.setattr(p, "_atomic_json", fail_before_pointer)
    with output_lock(output), pytest.raises(OSError, match="interrupted"):
        p._publish_records_locked(output, **kwargs)
    assert not _physical(output / "provenance/current.json").exists()
    orphan = next(_physical(output / "provenance/releases").iterdir())
    original_files = _snapshot(orphan)
    assert set("evidence/" + name for name in run_evidence) <= set(original_files)
    assert len(str(output / "provenance/releases" / orphan.name / "evidence" / key)) > 260
    monkeypatch.setattr(p, "_atomic_json", atomic)
    with output_lock(output):
        recovered = p._publish_records_locked(output, **kwargs)
        repeated = p._publish_records_locked(output, **kwargs)
        # This is the real production reader; its recursive traversal must carry
        # all old run and Catalog evidence into the next operation's release.
        previous = ProductionRun(output, "long-production", "read-next", {})._previous(recovered)
    assert recovered["metadata_release_id"] == orphan.name and repeated["unchanged"]
    assert all(previous["evidence"][name] == value for name, value in run_evidence.items())
    assert previous["outputs"] == inventory["outputs"]
    assert _snapshot(orphan) == original_files and _snapshot(source) == before
    assert not list(_physical(output / "provenance/staging").iterdir())
    assert p.verify_bundle(output)["valid"]
    assert not any("\\\\?\\" in name or "\\" in name for name in p._manifest(orphan))


@pytest.mark.parametrize("name", ["../outside.json", "nested/../../outside.json", "/outside.json",
                                 "C:/outside.json", "nested\\outside.json"])
def test_long_relative_paths_keep_escape_checks(long_paths, name):
    _, output, _ = long_paths
    with pytest.raises(ValueError, match="escapes|invalid relative"):
        p._relative(output, name)


@pytest.mark.skipif(os.name != "nt", reason="Windows namespace aliases")
def test_extended_namespace_preserves_overlap_and_relative_path_checks(long_paths):
    source, output, store = long_paths
    with pytest.raises(ValueError, match="detached"):
        p.publish_inventory(source, _physical(source / "companion"), dataset_id="overlap",
                            store_paths=[store], quiescent=True)
    relative = "provenance/runs/op-safe/receipt.json"
    path = p._relative(output, relative)
    assert str(path).startswith("\\\\?\\")
    assert p._relative(_physical(output), relative) == path
    assert path.relative_to(_physical(output)).as_posix() == relative
