"""Actual tiny Zarr writes exercise production provenance crash boundaries."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import zarr

from s1grits import provenance as publisher
from s1grits.file_lock import output_lock
from s1grits.provenance_contract import read_tables
from s1grits.provenance_production import ProductionRun

STORE = "18NUF/monthly.zarr"
DATASET_ID = "tiny-s1-production"


def _snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and p.name != ".s1grits.lock"}


def _forbid_array_mutations(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Recovery attempted to rewrite or resize image data")
    monkeypatch.setattr(zarr.Array, "__setitem__", blocked)
    monkeypatch.setattr(zarr.Array, "resize", blocked)


def _current(root):
    return json.loads((root / "provenance/current.json").read_text(encoding="utf-8"))


def _tables(root):
    pointer = _current(root)
    return read_tables(root / "provenance/releases" / pointer["metadata_release_id"])


def _sources():
    return [
        {"source_id": "source-" + name, "collection": "fixture-s1",
         "product_id": "scene-" + name, "asset_key": "VV",
         "href": "https://example.invalid/" + name + ".tif",
         "source_sha256": None, "declared_scale": None, "declared_offset": None,
         "evidence_status": "source_declared", "evidence_ref": None}
        for name in ("a", "b", "c")
    ]


def _new_run(root, run_id, month="2025-02", **request_changes):
    request = {"month": month, "purpose": "controlled-real-array-test", **request_changes}
    return ProductionRun(root, DATASET_ID, run_id, request)


def _prepare(run, month):
    run.prepare(store_path=STORE, month=month,
                parameters={"aggregation": "fixture-constant", "input_domain": "fixture"},
                sources=_sources())


def _write_month(root, month, value):
    """Delete any same-calendar-month row then append actual arrays at the tail."""
    group = zarr.open_group(str(root / STORE), mode="a", use_consolidated=False)
    times = np.asarray(group["time"][:])
    keep = np.flatnonzero(times.astype("datetime64[M]") != np.datetime64(month, "M"))
    old = {name: np.asarray(group[name][:])[keep] for name in ("VV_dB", "VH_dB", "n_obs")}
    times = np.concatenate((times[keep], np.array([month + "-15"], dtype="datetime64[ns]")))
    group["time"].resize((len(times),))
    group["time"][:] = times
    for name, values in old.items():
        array = group[name]
        array.resize((len(times), 2, 3))
        if len(keep):
            array[:-1] = values
        array[-1] = np.full((2, 3), value if name != "n_obs" else 1, dtype=array.dtype)


def _write_and_commit(root, run, month="2025-02", value=2):
    _prepare(run, month)
    run.begin_write(selected_source_ids=["source-a", "source-b"])
    _write_month(root, month, value)
    run.commit(used_source_ids=["source-a"])


def _complete(root, run_id, month, value):
    with output_lock(root):
        run = _new_run(root, run_id, month)
        _write_and_commit(root, run, month, value)
        pointer = run.publish()
        assert run.completed
    return pointer


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "dataset"
    group = zarr.open_group(str(root / STORE), mode="w", zarr_format=3)
    group.attrs.update({"product_type": "smonthly", "tile_id": "18NUF",
                        "geometry_group_id": "18NUF_ASCENDING_TK120",
                        "grid_id": "fixture-grid-30", "crs": "EPSG:32618",
                        "transform": [30, 0, 400000, 0, -30, 1000000]})
    group.create_array("time", data=np.array(["2025-01-15"], dtype="datetime64[ns]"))
    group.create_array("x", data=np.array([400015, 400045, 400075], dtype="f8"))
    group.create_array("y", data=np.array([999985, 999955], dtype="f8"))
    for name in ("VV_dB", "VH_dB", "n_obs"):
        group.create_array(name, data=np.ones((1, 2, 3), dtype="u1" if name == "n_obs" else "f4"),
                           chunks=(1, 2, 3))
    return root


def _historical_current_with_catalog(root):
    from s1grits.canonical_catalog_schema import normalize_catalog_record
    _write_month(root, "2025-03", 3)
    records = []
    for month in ("2025-01", "2025-03"):
        records.append(normalize_catalog_record({
            "item_id": "18NUF_ASCENDING_TK120_" + month,
            "collection_id": "s1grits-smonthly", "product_type": "smonthly",
            "product_label": "smonthly_ASCENDING", "tile_id": "18NUF",
            "flight_direction": "ASCENDING", "geometry_group_id": "18NUF_ASCENDING_TK120",
            "crs": "EPSG:32618", "transform": [30, 0, 400000, 0, -30, 1000000],
            "width": 3, "height": 2, "datetime": month + "-15T00:00:00Z",
            "month": month, "zarr_path": "monthly.zarr", "track": 120,
            "bands": ["VV_dB", "VH_dB", "n_obs"], "created_at": "2025-08-01T00:00:00Z",
            "software_version": "unknown-historical",
        }))
    frame = pd.DataFrame(records)
    frame.to_parquet(root / "catalog.parquet", index=False)
    frame.to_parquet(root / "18NUF/catalog.parquet", index=False)
    inventory = publisher.collect_inventory(root, store_paths=[STORE])
    assert "catalog.parquet" in inventory["witnesses"]
    inventory["evidence"]["historical/questions.json"] = {"input_domain": None, "reason": "unknown"}
    with output_lock(root):
        pointer = publisher._publish_records_locked(
            root, dataset_id=DATASET_ID, inventory=inventory,
            selection={"store_paths": [STORE], "start": None, "end": None},
        )
    return pointer, records[0]


@pytest.mark.parametrize("month", ["2025-02", "2025-01"])
def test_managed_append_or_replace_accepts_historical_catalog_current(dataset, month):
    pointer, record = _historical_current_with_catalog(dataset)
    old_release = dataset / "provenance/releases" / pointer["metadata_release_id"]
    old_bytes = _snapshot(old_release)
    old_outputs = {row["period_start"]: row for row in _tables(dataset)[0]}
    run_id = "managed-" + month
    with output_lock(dataset):
        run = _new_run(dataset, run_id, month, catalog_mode="tile_global_v1")
        _prepare(run, month)
        assert run.intent["previous_generation_id"] is None
        run.begin_write(selected_source_ids=["source-a"])
        _write_month(dataset, month, 8)
        record.update(item_id="18NUF_ASCENDING_TK120_" + month,
                      month=month, datetime=month + "-15T00:00:00Z")
        run.commit(used_source_ids=["source-a"], catalog_records=[record])
        result = run.publish()
    outputs = {row["period_start"]: row for row in _tables(dataset)[0]}
    assert len(outputs) == (3 if month == "2025-02" else 2)
    target = outputs[month + "-01"]
    assert target["generation_id"] and target["run_id"] == run_id
    assert target["evidence_status"] == "execution_recorded"
    for period, old in old_outputs.items():
        assert outputs[period]["output_id"] == old["output_id"]
        if period != month + "-01":
            assert outputs[period]["generation_id"] is None and outputs[period]["run_id"] is None
    release = dataset / "provenance/releases" / result["metadata_release_id"]
    assert json.loads((release / "evidence/historical/questions.json").read_text()) == {
        "input_domain": None, "reason": "unknown"}
    witnesses = json.loads((release / "evidence/data_witnesses.json").read_text())
    assert "catalog.parquet" not in witnesses
    assert _snapshot(old_release) == old_bytes
    assert publisher.verify_bundle(dataset)["valid"]
    with output_lock(dataset):
        next_run = _new_run(dataset, "managed-next", "2025-04", catalog_mode="tile_global_v1")
        _prepare(next_run, "2025-04")


def test_managed_historical_current_rejects_changed_catalog_before_preparing(dataset):
    _historical_current_with_catalog(dataset)
    path = dataset / "catalog.parquet"
    frame = pd.read_parquet(path)
    frame.loc[0, "software_version"] = "changed-outside-protocol"
    frame.to_parquet(path, index=False)
    before = _snapshot(dataset)
    with output_lock(dataset):
        run = _new_run(dataset, "managed-stale", catalog_mode="tile_global_v1")
        with pytest.raises(ValueError, match="published source revision is stale"):
            _prepare(run, "2025-02")
    assert _snapshot(dataset) == before
    assert not (dataset / "provenance/pending/managed-stale.json").exists()


@pytest.mark.parametrize("catalog_path", ["catalog.parquet", "18NUF/catalog.parquet"])
def test_prepared_managed_resume_rejects_catalog_change_before_data_write(dataset, catalog_path):
    _historical_current_with_catalog(dataset)
    with output_lock(dataset):
        run = _new_run(dataset, "managed-resume", catalog_mode="tile_global_v1")
        _prepare(run, "2025-02")
    path = dataset / catalog_path
    frame = pd.read_parquet(path)
    frame.loc[0, "software_version"] = "changed-between-sessions"
    frame.to_parquet(path, index=False)
    before = _snapshot(dataset)
    with output_lock(dataset):
        resumed = _new_run(dataset, "managed-resume", catalog_mode="tile_global_v1")
        _prepare(resumed, "2025-02")
        with pytest.raises(ValueError, match="Catalog baseline changed before data write"):
            resumed.begin_write(selected_source_ids=["source-a"])
    assert _snapshot(dataset) == before
    pending = json.loads((dataset / "provenance/pending/managed-resume.json").read_text())
    assert pending["phase"] == "prepared"


def test_append_replace_reindexes_without_reassigning_untouched_generation(dataset):
    first = _complete(dataset, "append-feb", "2025-02", 2)
    first_tables = _tables(dataset)
    first_release = dataset / "provenance/releases" / first["metadata_release_id"]
    first_bytes = _snapshot(first_release)
    second = _complete(dataset, "append-mar", "2025-03", 3)
    before = {row["period_start"]: row for row in _tables(dataset)[0]}
    third = _complete(dataset, "replace-jan", "2025-01", 9)
    after, sources, links = _tables(dataset)
    after = {row["period_start"]: row for row in after}
    assert len(after) == 3
    assert len({first["data_revision"], second["data_revision"], third["data_revision"]}) == 3
    assert _snapshot(first_release) == first_bytes
    assert first_tables[0][0]["generation_id"] is None
    assert after["2025-01-01"]["generation_id"]
    assert after["2025-01-01"]["output_id"] == before["2025-01-01"]["output_id"]
    assert after["2025-01-01"]["time_index"] == 2
    for month, index in (("2025-02-01", 0), ("2025-03-01", 1)):
        assert after[month]["generation_id"] == before[month]["generation_id"]
        assert after[month]["output_id"] == before[month]["output_id"]
        assert after[month]["time_index"] == index
    group = zarr.open_group(str(dataset / STORE), mode="r", use_consolidated=False)
    np.testing.assert_array_equal(group["VV_dB"][:, 0, 0], [2, 3, 9])
    assert {row["source_id"] for row in sources} == {"source-a", "source-b", "source-c"}
    assert any(link["source_id"] == "source-a" and link["role"] == "used" for link in links)
    assert not any(link["source_id"] == "source-b" and link["role"] == "used" for link in links)
    assert any(link["source_id"] == "source-b" and link["role"] == "selected" for link in links)
    assert any(link["source_id"] == "source-c" and link["role"] == "excluded" for link in links)
    current_generations = {row["output_id"]: row["generation_id"] for row in after.values()}
    assert all(link["generation_id"] == current_generations[link["output_id"]] for link in links)


def test_replacement_uses_new_generation_and_does_not_merge_old_used_sources(dataset):
    _complete(dataset, "append-feb", "2025-02", 2)
    old = next(row for row in _tables(dataset)[0] if row["period_start"] == "2025-02-01")
    with output_lock(dataset):
        run = _new_run(dataset, "replace-feb")
        _prepare(run, "2025-02")
        run.begin_write(selected_source_ids=["source-b"])
        _write_month(dataset, "2025-02", 8)
        run.commit(used_source_ids=["source-b"])
        run.publish()
    outputs, _, links = _tables(dataset)
    new = next(row for row in outputs if row["period_start"] == "2025-02-01")
    assert new["output_id"] == old["output_id"]
    assert new["generation_id"] != old["generation_id"]
    used = [link for link in links if link["output_id"] == new["output_id"] and link["role"] == "used"]
    assert {link["source_id"] for link in used} == {"source-b"}
    assert {link["generation_id"] for link in used} == {new["generation_id"]}


def test_completed_run_recovery_is_idempotent_and_changed_request_is_rejected(dataset, monkeypatch):
    _complete(dataset, "idempotent", "2025-02", 2)
    before = _snapshot(dataset)
    _forbid_array_mutations(monkeypatch)
    with output_lock(dataset):
        run = _new_run(dataset, "idempotent")
        run.recover()
        assert run.completed
    assert _snapshot(dataset) == before
    with output_lock(dataset), pytest.raises((ValueError, RuntimeError)):
        run = _new_run(dataset, "idempotent", purpose="different-input-selection")
        run.recover()
    assert _snapshot(dataset) == before


def test_failed_publication_recovers_from_durable_commit_without_data_write(dataset, monkeypatch):
    baseline = _complete(dataset, "baseline", "2025-02", 2)
    with output_lock(dataset):
        run = _new_run(dataset, "commit-before-publish", "2025-03")
        _write_and_commit(dataset, run, "2025-03", 3)
        pixels = _snapshot(dataset / STORE)
        original = publisher._atomic_json
        def fail_pointer(path, value):
            if path.name == "current.json":
                raise OSError("interrupted before current publication")
            return original(path, value)
        monkeypatch.setattr(publisher, "_atomic_json", fail_pointer)
        with pytest.raises(OSError, match="before current"):
            run.publish()
        assert _current(dataset)["metadata_release_id"] == baseline["metadata_release_id"]
        monkeypatch.setattr(publisher, "_atomic_json", original)
        _forbid_array_mutations(monkeypatch)
        recovered = _new_run(dataset, "commit-before-publish", "2025-03")
        recovered.recover()
        assert recovered.completed
    assert _snapshot(dataset / STORE) == pixels
    assert len(_tables(dataset)[0]) == 3
    assert not list((dataset / "provenance/pending").glob("*.json"))


def test_current_publish_then_pending_cleanup_interruption_only_finishes_cleanup(dataset, monkeypatch):
    with output_lock(dataset):
        run = _new_run(dataset, "cleanup-interruption")
        _write_and_commit(dataset, run)
        pixels = _snapshot(dataset / STORE)
        original = Path.unlink
        def fail_cleanup(path, *args, **kwargs):
            if path.parent.name == "pending" and path.name == "cleanup-interruption.json":
                raise OSError("interrupted pending cleanup")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", fail_cleanup)
        with pytest.raises(OSError, match="pending cleanup"):
            run.publish()
        pointer = _current(dataset)
        release_bytes = _snapshot(dataset / "provenance/releases")
        monkeypatch.setattr(Path, "unlink", original)
        _forbid_array_mutations(monkeypatch)
        resumed = _new_run(dataset, "cleanup-interruption")
        resumed.recover()
        assert resumed.completed
    assert _current(dataset) == pointer
    assert _snapshot(dataset / "provenance/releases") == release_bytes
    assert _snapshot(dataset / STORE) == pixels
    assert not list((dataset / "provenance/pending").glob("*.json"))


@pytest.mark.parametrize("write_finished", [False, True])
def test_writing_without_commit_receipt_is_ambiguous_even_when_time_exists(dataset, write_finished):
    with output_lock(dataset):
        run = _new_run(dataset, "uncommitted")
        _prepare(run, "2025-02")
        run.begin_write(selected_source_ids=["source-a"])
        if write_finished:
            _write_month(dataset, "2025-02", 2)
        before = _snapshot(dataset / STORE)
        resumed = _new_run(dataset, "uncommitted")
        with pytest.raises((ValueError, RuntimeError)):
            resumed.recover()
    assert _snapshot(dataset / STORE) == before
    assert (dataset / "provenance/pending/uncommitted.json").exists()
    assert not (dataset / "provenance/current.json").exists()


@pytest.mark.parametrize("change", ["time", "shape", "spatial_coordinate"])
def test_committed_structure_tamper_blocks_recovery(dataset, change):
    with output_lock(dataset):
        run = _new_run(dataset, "changed-structure")
        _write_and_commit(dataset, run)
        group = zarr.open_group(str(dataset / STORE), mode="a", use_consolidated=False)
        if change == "time":
            group["time"][1] = np.datetime64("2025-02-16", "ns")
        elif change == "shape":
            group["VV_dB"].resize((3, 2, 3))
        else:
            group["x"][:] = group["x"][:] + 1
        changed = _snapshot(dataset / STORE)
        resumed = _new_run(dataset, "changed-structure")
        with pytest.raises((ValueError, RuntimeError)):
            resumed.recover()
    assert _snapshot(dataset / STORE) == changed
    assert not (dataset / "provenance/current.json").exists()


def test_pending_exists_before_first_data_mutation(dataset):
    with output_lock(dataset):
        run = _new_run(dataset, "prepare-only")
        before = _snapshot(dataset / STORE)
        _prepare(run, "2025-02")
        path = dataset / "provenance/pending/prepare-only.json"
        assert path.exists()
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["run_id"] == "prepare-only"
        assert _snapshot(dataset / STORE) == before


def test_corrupted_durable_receipt_cannot_be_used_to_complete_publication(dataset):
    with output_lock(dataset):
        run = _new_run(dataset, "corrupted-receipt")
        _write_and_commit(dataset, run)
        receipt = dataset / "provenance/runs/corrupted-receipt/receipt.json"
        assert receipt.exists()
        receipt.write_bytes(receipt.read_bytes() + b" ")
        before = _snapshot(dataset / STORE)
        with pytest.raises((ValueError, RuntimeError)):
            _new_run(dataset, "corrupted-receipt").recover()
    assert _snapshot(dataset / STORE) == before
    assert (dataset / "provenance/pending/corrupted-receipt.json").exists()
    assert not (dataset / "provenance/current.json").exists()


def test_prepared_run_cannot_change_requested_inputs_under_same_run_id(dataset):
    with output_lock(dataset):
        _prepare(_new_run(dataset, "fixed-request"), "2025-02")
        before = _snapshot(dataset)
        with pytest.raises((ValueError, RuntimeError)):
            changed = _new_run(dataset, "fixed-request", "2025-03")
            _prepare(changed, "2025-03")
    assert _snapshot(dataset) == before


def test_new_run_rejects_conflicting_existing_source_before_new_pending(dataset, monkeypatch):
    _complete(dataset, "published-source", "2025-02", 2)
    changed_sources = _sources()
    changed_sources[0]["href"] = "https://example.invalid/different-source.tif"
    before = _snapshot(dataset)
    _forbid_array_mutations(monkeypatch)
    with output_lock(dataset), pytest.raises(ValueError, match="source identity reused"):
        run = _new_run(dataset, "source-conflict", "2025-03")
        run.prepare(store_path=STORE, month="2025-03",
                    parameters={"aggregation": "fixture-constant", "input_domain": "fixture"},
                    sources=changed_sources)
    assert _snapshot(dataset) == before
    assert not (dataset / "provenance/pending/source-conflict.json").exists()
    assert not (dataset / "provenance/runs/source-conflict/intent.json").exists()


def test_new_run_rejects_modified_previous_receipt_before_new_pending(dataset, monkeypatch):
    _complete(dataset, "published-receipt", "2025-02", 2)
    receipt = dataset / "provenance/runs/published-receipt/receipt.json"
    receipt.write_bytes(receipt.read_bytes() + b" ")
    before = _snapshot(dataset)
    _forbid_array_mutations(monkeypatch)
    with output_lock(dataset), pytest.raises(ValueError, match="receipt evidence changed"):
        _prepare(_new_run(dataset, "old-receipt-changed", "2025-03"), "2025-03")
    assert _snapshot(dataset) == before
    assert not (dataset / "provenance/pending/old-receipt-changed.json").exists()
    assert not (dataset / "provenance/runs/old-receipt-changed/intent.json").exists()


@pytest.mark.parametrize("producer_field", ["code_sha256", "versions"])
def test_prepared_resume_rejects_changed_producer_without_data_writes(dataset, monkeypatch,
                                                                     producer_field):
    from s1grits import provenance_production as production
    with output_lock(dataset):
        _prepare(_new_run(dataset, "frozen-producer"), "2025-02")
    producer = production._producer()
    key = next(iter(producer[producer_field]))
    producer[producer_field][key] = "changed-after-preparation"
    monkeypatch.setattr(production, "_producer", lambda: producer)
    _forbid_array_mutations(monkeypatch)
    before = _snapshot(dataset)
    with output_lock(dataset), pytest.raises(ValueError, match="producer code or dependencies changed"):
        _prepare(_new_run(dataset, "frozen-producer"), "2025-02")
    assert _snapshot(dataset) == before
    assert (dataset / "provenance/pending/frozen-producer.json").exists()


def test_replacement_geometry_change_cannot_be_committed_as_same_output(dataset):
    with output_lock(dataset):
        run = _new_run(dataset, "wrong-geometry", "2025-01")
        _prepare(run, "2025-01")
        run.begin_write(selected_source_ids=["source-a"])
        _write_month(dataset, "2025-01", 9)
        group = zarr.open_group(str(dataset / STORE), mode="a", use_consolidated=False)
        group.attrs["geometry_group_id"] = "18NUF_DESCENDING_TK999"
        changed_store = _snapshot(dataset / STORE)
        with pytest.raises(ValueError, match="replacement changed the logical output identity"):
            run.commit(used_source_ids=["source-a"])
        pending = json.loads(run.pending_path.read_text(encoding="utf-8"))
        assert pending["phase"] == "writing"
        assert not run.receipt_path.exists()
        assert not (dataset / "provenance/current.json").exists()
    assert _snapshot(dataset / STORE) == changed_store


@pytest.mark.parametrize("current_change", ["missing", "old_other_run"])
def test_cleanup_recovery_keeps_pending_when_current_no_longer_matches(dataset, monkeypatch,
                                                                      current_change):
    _complete(dataset, "prior-pointer", "2025-02", 2)
    current_path = dataset / "provenance/current.json"
    old_pointer = current_path.read_bytes()
    with output_lock(dataset):
        run = _new_run(dataset, "pending-with-changed-current", "2025-03")
        _write_and_commit(dataset, run, "2025-03", 3)
        original = Path.unlink
        def fail_cleanup(path, *args, **kwargs):
            if path == run.pending_path:
                raise OSError("interrupted pending cleanup")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", fail_cleanup)
        with pytest.raises(OSError, match="pending cleanup"):
            run.publish()
        monkeypatch.setattr(Path, "unlink", original)
        assert run.completed_path.exists()
        assert run.pending_path.exists()
        if current_change == "missing":
            current_path.unlink()
        else:
            current_path.write_bytes(old_pointer)
        before = _snapshot(dataset)
        _forbid_array_mutations(monkeypatch)
        with pytest.raises(ValueError, match="current pointer changed before pending cleanup"):
            _new_run(dataset, "pending-with-changed-current", "2025-03").recover()
        assert run.pending_path.exists()
    assert _snapshot(dataset) == before


@pytest.mark.parametrize("version", [None, True, 0, 2, "1"])
def test_numeric_request_rejects_unknown_version_before_any_run_record(dataset, version):
    before = _snapshot(dataset)
    with pytest.raises(ValueError, match="numeric_processing_version"):
        _new_run(dataset, "bad-numeric-version", numeric_processing_version=version)
    assert _snapshot(dataset) == before


@pytest.mark.parametrize("versioned", [True, False])
def test_numeric_commit_rejects_invalid_payload_without_receipt(dataset, versioned):
    request = {"numeric_processing_version": 1} if versioned else {}
    with output_lock(dataset):
        run = _new_run(dataset, "invalid-numeric", **request)
        _prepare(run, "2025-02")
        run.begin_write(selected_source_ids=["source-a"])
        _write_month(dataset, "2025-02", 2)
        pixels = _snapshot(dataset / STORE)
        with pytest.raises(ValueError, match="numeric|schema|version"):
            run.commit(used_source_ids=["source-a"], numeric_processing={})
        assert not run.receipt_path.exists()
        assert json.loads(run.pending_path.read_text())["phase"] == "writing"
        assert not (dataset / "provenance/current.json").exists()
    assert _snapshot(dataset / STORE) == pixels


def test_numeric_commit_requires_payload_when_requested(dataset):
    with output_lock(dataset):
        run = _new_run(dataset, "missing-numeric", numeric_processing_version=1)
        _prepare(run, "2025-02")
        run.begin_write(selected_source_ids=["source-a"])
        _write_month(dataset, "2025-02", 2)
        before = _snapshot(dataset)
        with pytest.raises(ValueError, match="numeric_processing is required"):
            run.commit(used_source_ids=["source-a"])
    assert _snapshot(dataset) == before
    assert not run.receipt_path.exists()


def test_numeric_commit_rejects_schema_valid_payload_for_wrong_actual_dtype(dataset):
    group = zarr.open_group(str(dataset / STORE), mode="a", use_consolidated=False)
    values = np.asarray(group["VV_dB"][:], dtype="float64")
    del group["VV_dB"]
    group.create_array("VV_dB", data=values, chunks=(1, 2, 3))
    with output_lock(dataset):
        run = _new_run(dataset, "wrong-stored-dtype", numeric_processing_version=1)
        _prepare(run, "2025-02")
        run.begin_write(selected_source_ids=["source-a"])
        _write_month(dataset, "2025-02", 2)
        before = _snapshot(dataset)
        with pytest.raises(ValueError, match="numeric output dtype"):
            run.commit(used_source_ids=["source-a"], numeric_processing=_numeric_payload())
    assert _snapshot(dataset) == before
    assert not run.receipt_path.exists()


def test_legacy_receipt_without_numeric_processing_remains_unknown_after_recovery(dataset, monkeypatch):
    _complete(dataset, "legacy-no-numeric", "2025-02", 2)
    receipt_path = dataset / "provenance/runs/legacy-no-numeric/receipt.json"
    receipt = json.loads(receipt_path.read_text())
    assert "numeric_processing" not in receipt
    before = _snapshot(dataset)
    _forbid_array_mutations(monkeypatch)
    with output_lock(dataset):
        _new_run(dataset, "legacy-no-numeric").recover()
    assert _snapshot(dataset) == before
    assert "numeric_processing" not in json.loads(receipt_path.read_text())


@pytest.mark.parametrize("published", [True, False])
def test_numeric_upgrade_recovers_legacy_durable_receipt_without_enriching_it(dataset, monkeypatch, published):
    with output_lock(dataset):
        run = _new_run(dataset, "legacy-upgrade")
        _write_and_commit(dataset, run, "2025-02", 2)
        if published:
            run.publish()
        intent_bytes, receipt_bytes = run.intent_path.read_bytes(), run.receipt_path.read_bytes()
        pixels = _snapshot(dataset / STORE)
        before = _snapshot(dataset)
        _forbid_array_mutations(monkeypatch)
        upgraded = _new_run(dataset, "legacy-upgrade", numeric_processing_version=1)
        assert upgraded.completed
        assert upgraded.request == run.request
        assert upgraded.request_hash == run.request_hash
        upgraded.recover()
    assert run.intent_path.read_bytes() == intent_bytes
    assert run.receipt_path.read_bytes() == receipt_bytes
    assert "numeric_processing" not in json.loads(receipt_bytes)
    assert _snapshot(dataset / STORE) == pixels
    if published:
        assert _snapshot(dataset) == before


@pytest.mark.parametrize("state", ["prepared", "writing", "completed_without_receipt"])
def test_numeric_upgrade_does_not_relax_legacy_run_without_receipt(dataset, state):
    with output_lock(dataset):
        run = _new_run(dataset, "legacy-no-receipt")
        _prepare(run, "2025-02")
        if state != "prepared":
            run.begin_write(selected_source_ids=["source-a"])
            _write_month(dataset, "2025-02", 2)
        if state == "completed_without_receipt":
            run.commit(used_source_ids=["source-a"])
            run.publish()
            run.receipt_path.unlink()
        before = _snapshot(dataset)
        with pytest.raises(ValueError, match="changed input or parameters"):
            _new_run(dataset, "legacy-no-receipt", numeric_processing_version=1)
    assert _snapshot(dataset) == before


@pytest.mark.parametrize("request_change", [{"purpose": "changed-input"}, {"operator_steps": []}])
def test_numeric_upgrade_still_rejects_other_request_changes(dataset, request_change):
    _complete(dataset, "legacy-changed-request", "2025-02", 2)
    before = _snapshot(dataset)
    with output_lock(dataset), pytest.raises(ValueError, match="changed input or parameters"):
        _new_run(dataset, "legacy-changed-request", numeric_processing_version=1, **request_change)
    assert _snapshot(dataset) == before


def test_legacy_receipt_with_explicit_null_numeric_processing_is_not_silent_unknown(dataset):
    with output_lock(dataset):
        run = _new_run(dataset, "legacy-null-numeric")
        _write_and_commit(dataset, run, "2025-02", 2)
        receipt = json.loads(run.receipt_path.read_text())
        receipt["numeric_processing"] = None
        run.receipt_path.write_text(json.dumps(receipt))
        before = _snapshot(dataset)
        with pytest.raises(ValueError, match="numeric"):
            run.recover()
    assert _snapshot(dataset) == before
    assert not (dataset / "provenance/current.json").exists()


def _numeric_payload(backend="numpy"):
    steps = []
    outputs = {}
    for variable in ("VV_dB", "VH_dB"):
        steps.extend([
            {"operation": "input_filter", "variable": variable,
             "input_domain": "linear_power", "output_domain": "linear_power",
             "parameters": {"nonpositive": "NaN"}, "status": "executed", "calls": 1},
            {"operation": "monthly_composite", "variable": variable,
             "input_domain": "linear_power", "output_domain": "linear_power",
             "parameters": {"method": "nanmedian", "backend": backend, "axis": 0, "nan_policy": "ignore_nan"},
             "status": "executed", "calls": 1},
            {"operation": "linear_to_db", "variable": variable,
             "input_domain": "linear_power", "output_domain": "db",
             "parameters": {"formula": "10*log10(x)", "nonpositive": "NaN"},
             "status": "executed", "calls": 1},
            {"operation": "storage_cast_and_write", "variable": variable,
             "input_domain": "db", "output_domain": "db", "parameters": {"dtype": "float32"},
             "status": "executed", "calls": 1},
        ])
        outputs[variable] = {"domain": "db", "unit": "dB", "dtype": "float32",
                                     "physical_quantity": None, "nodata": "NaN",
                                     "applied_scale": 1.0, "applied_offset": 0.0}
    steps.extend([
        {"operation": "observation_count", "variable": "n_obs",
         "input_domain": "linear_power", "output_domain": "count", "parameters": {},
         "status": "executed", "calls": 1},
        {"operation": "storage_cast_and_write", "variable": "n_obs",
         "input_domain": "count", "output_domain": "count", "parameters": {"dtype": "uint8"},
         "status": "executed", "calls": 1},
    ])
    outputs["n_obs"] = {"domain": "count", "unit": "aligned_scene_observations", "dtype": "uint8",
                        "physical_quantity": "finite_VV_stack_entries_from_selected_track", "nodata": 0,
                        "applied_scale": 1.0, "applied_offset": 0.0}
    return {"schema_version": 1, "scope": "monthly_writer_input_to_zarr",
            "input": {"actual_domain": None, "operator_expected_domain": "linear_power",
                      "physical_quantity": None, "evidence_status": "unknown",
                      "unknown_reason": "Upstream input physics is not independently verified."},
            "steps": steps, "output": outputs}


@pytest.mark.parametrize("versioned", [True, False])
def test_numeric_payload_is_stored_on_exact_output_receipt_without_upgrading_historical_rows(dataset, versioned):
    payload = _numeric_payload()
    with output_lock(dataset):
        run = _new_run(dataset, "numeric-receipt", **({"numeric_processing_version": 1} if versioned else {}))
        _prepare(run, "2025-02")
        run.begin_write(selected_source_ids=["source-a"])
        _write_month(dataset, "2025-02", 2)
        run.commit(used_source_ids=["source-a"], numeric_processing=payload)
        receipt_bytes = run.receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes)
        assert receipt["numeric_processing"] == payload
        result = run.publish()
    rows = _tables(dataset)[0]
    target = next(row for row in rows if row["period_start"] == "2025-02-01")
    historical = next(row for row in rows if row["period_start"] == "2025-01-01")
    assert receipt["output_id"] == target["output_id"]
    assert receipt["generation_id"] == target["generation_id"]
    assert historical["generation_id"] is None and historical["run_id"] is None
    copied = dataset / "provenance/releases" / result["metadata_release_id"] / "evidence/runs/numeric-receipt/receipt.json"
    assert json.loads(copied.read_text())["numeric_processing"] == payload
    assert run.receipt_path.read_bytes() == receipt_bytes
    variables = json.loads((copied.parents[3] / "variables.json").read_text())
    assert all("numeric_processing" not in variable for variable in variables)


@pytest.mark.parametrize("tamper", ["missing", "invalid", "dtype_mismatch", "unknown_request_version"])
def test_numeric_receipt_is_revalidated_on_recovery(dataset, tamper):
    with output_lock(dataset):
        run = _new_run(dataset, "numeric-revalidate", numeric_processing_version=1)
        _prepare(run, "2025-02")
        run.begin_write(selected_source_ids=["source-a"])
        _write_month(dataset, "2025-02", 2)
        run.commit(used_source_ids=["source-a"], numeric_processing=_numeric_payload())
        receipt = json.loads(run.receipt_path.read_text())
        if tamper == "missing":
            receipt.pop("numeric_processing")
        elif tamper == "invalid":
            receipt["numeric_processing"]["schema_version"] = 2
        elif tamper == "dtype_mismatch":
            observed = next(row for row in receipt["observed"]["variables"] if row["variable"] == "VV_dB")
            observed["dtype"] = "float64"
        else:
            intent = json.loads(run.intent_path.read_text())
            intent["request"]["numeric_processing_version"] = 2
            run.intent_path.write_text(json.dumps(intent))
            receipt["intent_sha256"] = hashlib.sha256(run.intent_path.read_bytes()).hexdigest()
        run.receipt_path.write_text(json.dumps(receipt))
        before = _snapshot(dataset)
        with pytest.raises(ValueError, match="numeric|schema|version"):
            run.recover()
    assert _snapshot(dataset) == before
    assert not (dataset / "provenance/current.json").exists()


def test_numeric_failed_publication_recovers_original_payload_without_recomputing(dataset, monkeypatch):
    payload = _numeric_payload()
    with output_lock(dataset):
        run = _new_run(dataset, "numeric-crash", numeric_processing_version=1)
        _prepare(run, "2025-02")
        run.begin_write(selected_source_ids=["source-a"])
        _write_month(dataset, "2025-02", 2)
        run.commit(used_source_ids=["source-a"], numeric_processing=payload)
        receipt_bytes = run.receipt_path.read_bytes()
        pixels = _snapshot(dataset / STORE)
        original = publisher._atomic_json
        def fail_pointer(path, value):
            if path.name == "current.json":
                raise OSError("interrupted numeric publication")
            return original(path, value)
        monkeypatch.setattr(publisher, "_atomic_json", fail_pointer)
        with pytest.raises(OSError, match="numeric publication"):
            run.publish()
        monkeypatch.setattr(publisher, "_atomic_json", original)
        _forbid_array_mutations(monkeypatch)
        resumed = _new_run(dataset, "numeric-crash", numeric_processing_version=1)
        result = resumed.recover()
        assert resumed.receipt_path.read_bytes() == receipt_bytes
        assert json.loads(resumed.receipt_path.read_text())["numeric_processing"] == payload
        assert resumed.completed
    assert _snapshot(dataset / STORE) == pixels
    current = dataset / "provenance/releases" / result["metadata_release_id"]
    records = json.loads((current / "evidence/runs/numeric-crash/receipt.json").read_text())
    assert records["numeric_processing"] == payload


def test_replacement_numeric_record_is_generation_scoped_and_old_receipt_survives(dataset):
    old_receipt = None
    generations = []
    for run_id, value, backend in (("numeric-first", 2, "numpy"), ("numeric-replace", 3, "bottleneck")):
        with output_lock(dataset):
            run = _new_run(dataset, run_id, numeric_processing_version=1)
            _prepare(run, "2025-02")
            run.begin_write(selected_source_ids=["source-a"])
            _write_month(dataset, "2025-02", value)
            run.commit(used_source_ids=["source-a"], numeric_processing=_numeric_payload(backend))
            run.publish()
        target = next(row for row in _tables(dataset)[0] if row["period_start"] == "2025-02-01")
        receipt = json.loads(run.receipt_path.read_text())
        generations.append((target["output_id"], target["generation_id"]))
        assert receipt["generation_id"] == target["generation_id"]
        assert receipt["numeric_processing"] == _numeric_payload(backend)
        if old_receipt is None:
            old_receipt = run.receipt_path.read_bytes()
    assert generations[0][0] == generations[1][0]
    assert generations[0][1] != generations[1][1]
    assert (dataset / "provenance/runs/numeric-first/receipt.json").read_bytes() == old_receipt


@pytest.mark.parametrize("bad_field", [
    None, "output_id", "dataset_id", "month", "store_path", "operation",
    "previous_generation_id", "processing_id", "selected_source_ids", "used_source_ids",
])
@pytest.mark.parametrize("managed", [False, True])
def test_unsealed_receipt_is_bound_to_intent_before_any_recovery_publication(dataset, monkeypatch, bad_field, managed):
    from s1grits import provenance_production as production
    _, catalog_record = _historical_current_with_catalog(dataset)
    catalog_record.update(item_id="18NUF_ASCENDING_TK120_2025-02", month="2025-02", datetime="2025-02-15T00:00:00Z")
    with output_lock(dataset):
        run = _new_run(dataset, "unsealed", **({"catalog_mode": "tile_global_v1"} if managed else {}))
        _prepare(run, "2025-02")
        run.begin_write(["source-a", "source-b"])
        _write_month(dataset, "2025-02", 2)
        original = production._atomic_json

        def stop_after_receipt(path, value):
            if path == run.pending_path and value.get("phase") == "committed_provenance_pending":
                raise OSError("receipt-to-pending crash gap")
            return original(path, value)

        monkeypatch.setattr(production, "_atomic_json", stop_after_receipt)
        with pytest.raises(OSError, match="crash gap"):
            run.commit(["source-a"], catalog_records=[catalog_record] if managed else None)
        monkeypatch.setattr(production, "_atomic_json", original)
        assert "receipt_sha256" not in json.loads(run.pending_path.read_text())
        receipt = json.loads(run.receipt_path.read_text())
        if bad_field == "output_id":
            receipt[bad_field] = next(row["output_id"] for row in receipt["observed"]["outputs"]
                                      if row["period_start"] == "2025-01-01")
        elif bad_field == "selected_source_ids":
            receipt[bad_field] = ["source-a", "source-c"]
        elif bad_field == "used_source_ids":
            receipt[bad_field] = ["source-c"]
        elif bad_field:
            receipt[bad_field] = "inconsistent"
        if bad_field:
            run.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        before = _snapshot(dataset)
        pixels = _snapshot(dataset / STORE)
        _forbid_array_mutations(monkeypatch)
        if bad_field:
            with pytest.raises(ValueError, match="receipt"):
                run.recover()
            assert _snapshot(dataset) == before
        else:
            run.recover()
            assert not run.pending_path.exists()
            target = next(row for row in _tables(dataset)[0] if row["period_start"] == "2025-02-01")
            assert target["generation_id"] == receipt["generation_id"]
        assert _snapshot(dataset / STORE) == pixels
