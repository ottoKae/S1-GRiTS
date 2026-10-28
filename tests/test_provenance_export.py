"""Integration checks for detached metadata publication and failure recovery."""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest
import zarr

from s1grits import provenance as p


def _snapshot(root):
    return {f.relative_to(root).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in root.rglob("*") if f.is_file()}


@pytest.fixture
def fixture(tmp_path):
    source = tmp_path / "source"
    relative = "18NUF/monthly.zarr"
    group = zarr.open_group(str(source / relative), mode="w", zarr_format=3)
    group.attrs.update({"product_type": "smonthly", "grid_id": "grid-30",
                        "geometry_group_id": "18NUF_ASCENDING_TK120"})
    group.create_array("time", data=np.array(["2025-01-15", "2025-02-17"], dtype="datetime64[ns]"))
    group.create_array("VV", data=np.arange(24, dtype="f4").reshape(2, 3, 4), chunks=(1, 3, 4))
    output = tmp_path / "companion"
    kwargs = dict(dataset_root=source, output_root=output, dataset_id="ecuador-s1",
                  store_paths=[relative], quiescent=True)
    return source, output, group, kwargs


def _release(output):
    pointer = json.loads((output / "provenance/current.json").read_text())
    return output / "provenance/releases" / pointer["metadata_release_id"]


def test_real_export_roundtrip_nonmutation_and_idempotency(fixture):
    source, output, _, kwargs = fixture
    before = _snapshot(source)
    first = p.publish_inventory(**kwargs)
    report = p.verify_bundle(output, dataset_root=source)
    assert report["counts"] == {"outputs": 2, "sources": 0, "source_links": 0}
    assert report["source_checked"] and not report["pixel_chunks_checked"]
    sidecar = _snapshot(output)
    repeat = p.publish_inventory(**kwargs, expected_release_id=first["metadata_release_id"])
    assert repeat["unchanged"]
    assert _snapshot(source) == before and _snapshot(output) == sidecar
    text = (_release(output) / "dataset.json").read_text()
    assert str(source) not in text and "metadata_only" in text


def test_notes_change_metadata_only_and_old_release_survives(fixture):
    source, output, _, kwargs = fixture
    first = p.publish_inventory(**kwargs)
    old = _snapshot(_release(output))
    second = p.publish_inventory(**kwargs, expected_release_id=first["metadata_release_id"],
                                 note="Clarified historical uncertainty.")
    assert second["data_revision"] == first["data_revision"]
    assert second["metadata_release_id"] != first["metadata_release_id"]
    assert _snapshot(output / "provenance/releases" / first["metadata_release_id"]) == old
    assert p.verify_bundle(output, dataset_root=source)["valid"]


def test_expanded_selection_is_a_complete_snapshot_without_implicit_merge(fixture):
    source, output, _, kwargs = fixture
    second_store = "18NUH/monthly.zarr"
    group = zarr.open_group(str(source / second_store), mode="w", zarr_format=3)
    group.attrs.update({"product_type": "smonthly", "grid_id": "grid-30",
                        "geometry_group_id": "18NUH_ASCENDING_TK120"})
    group.create_array("time", data=np.array(["2024-03-15", "2025-03-17"], dtype="datetime64[ns]"))
    group.create_array("VV", data=np.ones((2, 3, 4), dtype="f4"), chunks=(1, 3, 4))
    source_before = _snapshot(source)
    first = p.publish_inventory(**kwargs, start="2025-01-01", end="2025-01-31")
    first_release = _release(output)
    first_bytes = _snapshot(first_release)
    first_rows, _, _ = p.read_tables(first_release)
    assert len(first_rows) == 1

    union = {**kwargs, "store_paths": [*kwargs["store_paths"], second_store],
             "start": "2024-01-01", "end": "2025-12-31"}
    expanded = p.publish_inventory(**union, expected_release_id=first["metadata_release_id"])
    expanded_release = _release(output)
    expanded_bytes = _snapshot(expanded_release)
    expanded_rows, _, _ = p.read_tables(expanded_release)
    assert len(expanded_rows) == 4
    assert {row["output_id"] for row in first_rows} <= {row["output_id"] for row in expanded_rows}
    assert p.verify_bundle(output, dataset_root=source)["counts"]["outputs"] == 4

    p.publish_inventory(**{**kwargs, "store_paths": [second_store]},
                        start="2025-01-01", end="2025-12-31",
                        expected_release_id=expanded["metadata_release_id"])
    narrowed_rows, _, _ = p.read_tables(_release(output))
    assert len(narrowed_rows) == 1
    assert narrowed_rows[0]["store_path"] == second_store
    assert narrowed_rows[0]["period_start"] == "2025-03-01"
    assert narrowed_rows[0]["output_id"] in {row["output_id"] for row in expanded_rows}
    assert p.verify_bundle(output, dataset_root=source)["counts"]["outputs"] == 1
    assert _snapshot(first_release) == first_bytes
    assert _snapshot(expanded_release) == expanded_bytes
    assert _snapshot(source) == source_before


def test_historical_rebuild_after_append_preserves_ids_without_inventing_generations(fixture):
    source, output, group, kwargs = fixture
    first = p.publish_inventory(**kwargs)
    first_release = _release(output)
    first_bytes = _snapshot(first_release)
    first_rows, _, _ = p.read_tables(first_release)
    group["time"].resize((3,))
    group["time"][2] = np.datetime64("2025-03-19", "ns")
    group["VV"].resize((3, 3, 4))
    group["VV"][2] = np.full((3, 4), 42, dtype="f4")
    appended_bytes = _snapshot(source)
    with pytest.raises(ValueError, match="source revision changed"):
        p.verify_bundle(output, dataset_root=source)

    rebuilt = p.publish_inventory(**kwargs, expected_release_id=first["metadata_release_id"])
    rows, sources, links = p.read_tables(_release(output))
    assert len(rows) == 3 and not sources and not links
    assert [row["output_id"] for row in rows[:2]] == [row["output_id"] for row in first_rows]
    assert rows[2]["output_id"] not in {row["output_id"] for row in first_rows}
    assert rows[2]["period_start"] == "2025-03-01"
    for row in rows:
        assert row["generation_id"] is None and row["run_id"] is None and row["processing_id"] is None
        assert row["evidence_status"] == "artifact_observed" and row["unknown_reason"]
    assert rebuilt["data_revision"] != first["data_revision"]
    assert p.verify_bundle(output, dataset_root=source)["counts"]["outputs"] == 3
    assert p.publish_inventory(**kwargs, expected_release_id=rebuilt["metadata_release_id"])["unchanged"]
    assert _snapshot(first_release) == first_bytes
    assert _snapshot(source) == appended_bytes


def test_rejects_stale_expected_release(fixture):
    _, output, _, kwargs = fixture
    p.publish_inventory(**kwargs)
    before = _snapshot(output)
    with pytest.raises(ValueError, match="stale"):
        p.publish_inventory(**kwargs, note="outdated publisher")
    assert _snapshot(output) == before


@pytest.mark.parametrize("part", ["variables.json", "outputs.parquet", "FILES.sha256"])
def test_detects_corrupted_release(fixture, part):
    _, output, _, kwargs = fixture
    p.publish_inventory(**kwargs)
    path = _release(output) / part
    path.write_bytes(path.read_bytes() + b"corruption")
    with pytest.raises(ValueError, match="hash|manifest"):
        p.verify_bundle(output)


def test_changed_source_invalidates_companion(fixture):
    source, output, group, kwargs = fixture
    p.publish_inventory(**kwargs)
    group["time"][0] = np.datetime64("2025-01-16", "ns")
    with pytest.raises(ValueError, match="source revision changed"):
        p.verify_bundle(output, dataset_root=source)


def test_source_changes_mid_export_do_not_publish(fixture, monkeypatch):
    _, output, group, kwargs = fixture
    collect = p.collect_inventory
    calls = 0
    def changed(*args, **kw):
        nonlocal calls
        calls += 1
        if calls == 2:
            group["time"][0] = np.datetime64("2025-01-16", "ns")
        return collect(*args, **kw)
    monkeypatch.setattr(p, "collect_inventory", changed)
    with pytest.raises(ValueError, match="source changed"):
        p.publish_inventory(**kwargs)
    assert not (output / "provenance/current.json").exists()


def test_failed_pointer_publish_keeps_old_release_and_retry_reuses_orphan(fixture, monkeypatch):
    _, output, _, kwargs = fixture
    first = p.publish_inventory(**kwargs)
    old_pointer = (output / "provenance/current.json").read_bytes()
    atomic = p._atomic_json
    def fail(*args, **kw):
        raise OSError("simulated interruption before pointer")
    monkeypatch.setattr(p, "_atomic_json", fail)
    with pytest.raises(OSError, match="interruption"):
        p.publish_inventory(**kwargs, expected_release_id=first["metadata_release_id"], note="updated")
    assert (output / "provenance/current.json").read_bytes() == old_pointer
    assert len(list((output / "provenance/releases").iterdir())) == 2
    monkeypatch.setattr(p, "_atomic_json", atomic)
    recovered = p.publish_inventory(**kwargs, expected_release_id=first["metadata_release_id"], note="updated")
    assert recovered["metadata_release_id"] != first["metadata_release_id"]
    assert len(list((output / "provenance/releases").iterdir())) == 2
    assert not list((output / "provenance/staging").iterdir())
    assert p.verify_bundle(output)["valid"]


def test_fail_closed_missing_evidence(fixture, monkeypatch):
    _, output, _, kwargs = fixture
    collect = p.collect_inventory
    def missing(*args, **kw):
        result = collect(*args, **kw)
        result["evidence"] = {}
        return result
    monkeypatch.setattr(p, "collect_inventory", missing)
    with pytest.raises(ValueError, match="missing evidence"):
        p.publish_inventory(**kwargs)
    assert not (output / "provenance/current.json").exists()


def test_rejects_source_destination_overlap_and_no_quiescence(fixture):
    source, output, _, kwargs = fixture
    with pytest.raises(ValueError, match="quiescence"):
        p.publish_inventory(**{**kwargs, "quiescent": False})
    for destination in (source, source / "metadata", source.parent):
        with pytest.raises(ValueError, match="detached"):
            p.publish_inventory(**{**kwargs, "output_root": destination})
    assert not output.exists()


def test_manifest_path_traversal_is_rejected(fixture):
    _, output, _, kwargs = fixture
    p.publish_inventory(**kwargs)
    directory = _release(output)
    path = directory / "FILES.sha256"
    path.write_text("0" * 64 + "  ../outside.json\n")
    pointer_path = output / "provenance/current.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["manifest_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    pointer_path.write_text(json.dumps(pointer))
    with pytest.raises(ValueError, match="escapes"):
        p.verify_bundle(output)


def test_publication_reuses_existing_lock(fixture, monkeypatch):
    _, output, _, kwargs = fixture
    from contextlib import contextmanager
    @contextmanager
    def busy(path):
        assert path == output
        raise TimeoutError("publication locked")
        yield
    monkeypatch.setattr(p, "output_lock", busy)
    with pytest.raises(TimeoutError, match="locked"):
        p.publish_inventory(**kwargs)
    assert not output.exists()

def test_rejects_orphan_with_self_consistent_but_different_evidence(fixture, monkeypatch):
    _, output, _, kwargs = fixture
    atomic = p._atomic_json
    def fail(*args, **kw):
        raise OSError("before pointer")
    monkeypatch.setattr(p, "_atomic_json", fail)
    with pytest.raises(OSError):
        p.publish_inventory(**kwargs)
    release = next((output / "provenance/releases").iterdir())
    variables = release / "variables.json"
    variables.write_text('{"invented_processing":"applied"}\n')
    manifest = p._manifest(release)
    (release / "FILES.sha256").write_text(
        "".join(digest + "  " + name + "\n" for name, digest in manifest.items()))
    monkeypatch.setattr(p, "_atomic_json", atomic)
    with pytest.raises(ValueError, match="orphan release content disagrees"):
        p.publish_inventory(**kwargs)
    assert not (output / "provenance/current.json").exists()
    assert not list((output / "provenance/staging").iterdir())


def test_existing_real_lock_blocks_publisher(fixture, monkeypatch):
    _, output, _, kwargs = fixture
    from s1grits.file_lock import output_lock
    with output_lock(output):
        monkeypatch.setattr(p, "output_lock", lambda path: output_lock(path, timeout=0))
        with pytest.raises(TimeoutError):
            p.publish_inventory(**kwargs)
    assert not (output / "provenance/current.json").exists()


def test_cli_build_then_verify(fixture, capsys):
    source, output, _, kwargs = fixture
    assert p.main(["build", "--dataset-root", str(source), "--output-root", str(output),
                   "--dataset-id", "ecuador-s1", "--store", kwargs["store_paths"][0],
                   "--quiescent"]) == 0
    assert '"metadata_release_id"' in capsys.readouterr().out
    assert p.main(["verify", "--output-root", str(output), "--dataset-root", str(source)]) == 0
    assert '"source_checked": true' in capsys.readouterr().out


def test_dataset_identity_cannot_be_silently_reassigned(fixture):
    _, output, _, kwargs = fixture
    first = p.publish_inventory(**kwargs)
    before = _snapshot(output)
    with pytest.raises(ValueError, match="dataset_id cannot change"):
        p.publish_inventory(**{**kwargs, "dataset_id": "unrelated-dataset"},
                            expected_release_id=first["metadata_release_id"])
    assert _snapshot(output) == before


def _production_records(fixture):
    source, output, _, kwargs = fixture
    selection = {"store_paths": kwargs["store_paths"], "start": None, "end": None}
    inventory = p.collect_inventory(source, **selection)
    run_id = "run-helper"
    receipt_key = "runs/" + run_id + ".json"
    inventory["evidence"][receipt_key] = {"run_id": run_id, "status": "committed"}
    for row in inventory["outputs"]:
        row.update(generation_id="generation-" + row["period_start"], run_id=run_id,
                   processing_id="processing-fixed", evidence_status="execution_recorded",
                   evidence_ref="evidence/" + receipt_key, unknown_reason=None)
    return output, dict(dataset_id=kwargs["dataset_id"], inventory=inventory,
                       selection=selection, execution_kind="production",
                       extra_dataset={"committed_run_id": run_id})


def test_shared_publication_preserves_generations_without_nested_lock(fixture, monkeypatch):
    output, kwargs = _production_records(fixture)
    def unexpected_lock(*args, **kw):
        raise AssertionError("shared publisher must use caller's existing lock")
    monkeypatch.setattr(p, "output_lock", unexpected_lock)
    checks = []
    first = p._publish_records_locked(output, **kwargs, before_publish=lambda: checks.append(True))
    second = p._publish_records_locked(output, **kwargs, before_publish=lambda: checks.append(True))
    assert checks == [True, True] and second["unchanged"]
    assert first["metadata_release_id"] == second["metadata_release_id"]
    rows, _, _ = p.read_tables(_release(output))
    assert rows == kwargs["inventory"]["outputs"]
    dataset = json.loads((_release(output) / "dataset.json").read_text(encoding="utf-8"))
    assert dataset["execution_kind"] == "production"
    assert dataset["committed_run_id"] == "run-helper"
    assert dataset["delivery_kind"] == "metadata_only"
    assert p.verify_bundle(output)["valid"]
    with pytest.raises(ValueError, match="production recovery verifier"):
        p.verify_bundle(output, dataset_root=fixture[0])


def test_shared_publisher_validates_before_changing_current(fixture):
    output, kwargs = _production_records(fixture)
    p._publish_records_locked(output, **kwargs)
    old_pointer = (output / "provenance/current.json").read_bytes()
    def invalid_receipt():
        raise ValueError("producer receipt no longer matches")
    with pytest.raises(ValueError, match="receipt"):
        p._publish_records_locked(output, **kwargs, note="new evidence", before_publish=invalid_receipt)
    assert (output / "provenance/current.json").read_bytes() == old_pointer
    assert len(list((output / "provenance/releases").iterdir())) == 1
    assert not list((output / "provenance/staging").iterdir())


def test_shared_publisher_extension_fields_are_immutable_and_do_not_change_data_revision(fixture):
    output, kwargs = _production_records(fixture)
    first = p._publish_records_locked(output, **kwargs)
    old_release = _release(output)
    before = _snapshot(old_release)
    revised = {**kwargs, "extra_dataset": {"committed_run_id": "run-helper", "recovery_note": "verified"}}
    second = p._publish_records_locked(output, **revised)
    assert first["data_revision"] == second["data_revision"]
    assert first["metadata_release_id"] != second["metadata_release_id"]
    assert _snapshot(old_release) == before
    with pytest.raises(ValueError, match="cannot replace"):
        p._publish_records_locked(output, **{**kwargs, "extra_dataset": {"data_revision": "fabricated"}})
    assert p.verify_bundle(output)["metadata_release_id"] == second["metadata_release_id"]
