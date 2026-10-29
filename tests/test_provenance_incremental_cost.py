"""Bounded incremental acceptance: portable history, untouched stores and metadata-only reads."""
from __future__ import annotations

import json
from shutil import copytree

import pytest
import zarr

from s1grits import provenance as publisher
from s1grits.file_lock import output_lock
from s1grits.provenance_production import ProductionRun
from test_provenance_production import (
    DATASET_ID, STORE, _complete, _current, _snapshot, _tables, dataset as production_dataset,
    _write_month, _new_run, _forbid_array_mutations,
)


def test_incremental_cost_and_unchanged_store(tmp_path, monkeypatch, record_property):
    dataset = production_dataset.__wrapped__(tmp_path)
    other = "18NUF/other-track.zarr"
    copytree(dataset / STORE, dataset / other)
    zarr.open_group(str(dataset / other), mode="a", use_consolidated=False).attrs[
        "geometry_group_id"
    ] = "18NUF_ASCENDING_TK47"
    inventory = publisher.collect_inventory(dataset, store_paths=[STORE, other])
    with output_lock(dataset):
        publisher._publish_records_locked(
            dataset, dataset_id=DATASET_ID, inventory=inventory,
            selection={"store_paths": [STORE, other], "start": None, "end": None},
        )
    _complete(dataset, "seed-february", "2025-02", 2)
    other_before = _snapshot(dataset / other)
    observed = []
    native_observe = ProductionRun._observe
    native_getitem = zarr.Array.__getitem__

    def metadata_only_getitem(array, selection):
        if array.path.rsplit("/", 1)[-1] in {"VV_dB", "VH_dB", "n_obs"}:
            raise AssertionError("metadata observation read image pixels")
        return native_getitem(array, selection)

    def counted_observe(run, paths):
        observed.append(tuple(paths))
        with monkeypatch.context() as guard:
            guard.setattr(zarr.Array, "__getitem__", metadata_only_getitem)
            return native_observe(run, paths)

    monkeypatch.setattr(ProductionRun, "_observe", counted_observe)
    metrics = []
    for month, value in (("2025-03", 3), ("2025-04", 4)):
        previous = dataset / "provenance/releases" / _current(dataset)["metadata_release_id"]
        old_release = _snapshot(previous)
        old_outputs = {row["output_id"]: row["generation_id"] for row in _tables(dataset)[0]}
        existing_evidence = {
            path.relative_to(previous).as_posix(): path.read_bytes()
            for path in (previous / "evidence/runs").rglob("*.json")
        }
        observed.clear()
        pointer = _complete(dataset, "append-" + month, month, value)
        release = dataset / "provenance/releases" / pointer["metadata_release_id"]
        outputs = _tables(dataset)[0]
        assert len(outputs) == len(old_outputs) + 1
        for row in outputs:
            if row["output_id"] in old_outputs:
                assert row["generation_id"] == old_outputs[row["output_id"]]
        assert _snapshot(previous) == old_release
        assert _snapshot(dataset / other) == other_before
        copied = [len(data) for name, data in existing_evidence.items()
                  if (release / name).is_file() and (release / name).read_bytes() == data]
        metrics.append({
            "month": month,
            "observe_calls": len(observed),
            "unchanged_store_observations": sum(other in paths for paths in observed),
            "copied_historical_evidence_files": len(copied),
            "copied_historical_evidence_bytes": sum(copied),
            "new_release_bytes": sum(path.stat().st_size for path in release.rglob("*") if path.is_file()),
        })
        assert not list((dataset / "provenance/pending").glob("*.json"))
    record_property("incremental_cost", json.dumps(metrics, sort_keys=True))


HISTORY_REF = "evidence/history/producer-confirmation.json"
OTHER_STORE = "18NUF/unchanged-track.zarr"


@pytest.fixture
def annotated_history(tmp_path):
    root = production_dataset.__wrapped__(tmp_path)
    copytree(root / STORE, root / OTHER_STORE)
    zarr.open_group(str(root / OTHER_STORE), mode="a", use_consolidated=False).attrs[
        "geometry_group_id"
    ] = "18NUF_ASCENDING_TK47"
    _write_month(root, "2025-03", 3)
    inventory = publisher.collect_inventory(root, store_paths=[STORE, OTHER_STORE])
    inventory["evidence"][HISTORY_REF.removeprefix("evidence/")] = {
        "basis": "producer_confirmation", "input_domain": "linear_power",
        "monthly_operator": "nanmedian", "output_units": "dB",
        "statement_extends_to_future_batches": False,
    }
    for value in inventory["evidence"].values():
        if value.get("store_path") in {STORE, OTHER_STORE}:
            value["historical_context"] = {"historical_processing_ref": HISTORY_REF}
    for variable in inventory["variables"]:
        if variable["variable"] in {"VV_dB", "VH_dB"}:
            variable.update(units="dB", historical_processing_ref=HISTORY_REF,
                            historical_processing_basis="producer_confirmation_and_retained_code")
    for limitation in inventory["limitations"]:
        limitation["historical_processing_ref"] = HISTORY_REF
    with output_lock(root):
        first = publisher._publish_records_locked(
            root, dataset_id=DATASET_ID, inventory=inventory,
            selection={"store_paths": [STORE, OTHER_STORE], "start": None, "end": None},
        )
    return root, first


def _assert_scoped_history(root, expected_historical_ids):
    resolver = pytest.importorskip("grits_resolver")
    view = resolver.ProvenanceView(root, required=True)
    for row in view.snapshot["outputs"]:
        result = view.explain(output_id=row["output_id"])
        variables = [v for v in result["variables"] if v["store_path"] == row["store_path"]
                     and v["variable"] in {"VV_dB", "VH_dB"}]
        contexts = [v["historical_context"] for v in result["evidence"].values()
                    if isinstance(v, dict) and "historical_context" in v]
        limits = [v for v in result["limitations"] if v.get("store_path") == row["store_path"]]
        if row["output_id"] in expected_historical_ids:
            assert contexts and all(c["historical_processing_ref"] == HISTORY_REF for c in contexts)
            assert all(c["output_generations"] == {row["output_id"]: row["generation_id"]} for c in contexts)
            assert all(v["units"] == "dB" and v["historical_processing_ref"] == HISTORY_REF for v in variables)
            assert all(v["historical_output_generations"] == {row["output_id"]: row["generation_id"]}
                       for v in variables)
            assert limits and all(v["historical_processing_ref"] == HISTORY_REF for v in limits)
        else:
            assert not contexts
            assert all("historical_processing_ref" not in v and "units" not in v for v in variables)
            assert all("historical_processing_ref" not in v for v in limits)
            assert row["evidence_status"] == "execution_recorded"
            assert row["evidence_ref"].removeprefix("evidence/") in result["evidence"]
        array = zarr.open_group(str(root / row["store_path"]), mode="r", use_consolidated=False)["VV_dB"]
        assert all(v["shape"] == list(array.shape) for v in variables)
    return view


@pytest.mark.parametrize("month", ["2025-02", "2025-01"])
def test_historical_annotations_survive_append_replace_without_applying_to_new_generation(annotated_history, month):
    root, first = annotated_history
    original_rows = _tables(root)[0]
    expected = {r["output_id"] for r in original_rows
                if not (r["store_path"] == STORE and r["period_start"][:7] == month)}
    old_release = root / "provenance/releases" / first["metadata_release_id"]
    original_bytes, other_bytes = _snapshot(old_release), _snapshot(root / OTHER_STORE)
    _complete(root, "annotation-update", month, 9)
    view = _assert_scoped_history(root, expected)
    assert view.pointer["schema_version"] == 2
    assert _snapshot(old_release) == original_bytes and _snapshot(root / OTHER_STORE) == other_bytes
    for name, value in view.snapshot["evidence"].items():
        if name.startswith("stores/") and value.get("store_path") == STORE:
            assert len(value["groups"][0]["times"]) == (3 if month == "2025-02" else 2)


def test_historical_annotation_scope_only_shrinks_across_incremental_updates(annotated_history):
    root, _ = annotated_history
    original = _tables(root)[0]
    expected = {r["output_id"] for r in original}
    for run_id, month in (("append-feb", "2025-02"), ("replace-jan", "2025-01"),
                          ("append-apr", "2025-04"), ("replace-mar", "2025-03")):
        _complete(root, run_id, month, 9)
        expected -= {r["output_id"] for r in original
                     if r["store_path"] == STORE and r["period_start"][:7] == month}
        view = _assert_scoped_history(root, expected)
    assert all("historical_processing_ref" not in v for v in view.snapshot["variables"] if v["store_path"] == STORE)
    assert any(v.get("historical_processing_ref") == HISTORY_REF for v in view.snapshot["variables"]
               if v["store_path"] == OTHER_STORE)


def test_historical_annotations_recover_and_travel_with_dataset(annotated_history, tmp_path, monkeypatch):
    root, first = annotated_history
    expected = {r["output_id"] for r in _tables(root)[0]}
    original = publisher._atomic_json

    def interrupt_current(path, value):
        if path.name == "current.json":
            raise OSError("annotation publication interrupted")
        return original(path, value)

    with monkeypatch.context() as fault:
        fault.setattr(publisher, "_atomic_json", interrupt_current)
        with pytest.raises(OSError, match="annotation publication"):
            _complete(root, "annotation-recovery", "2025-02", 9)
    assert _current(root)["metadata_release_id"] == first["metadata_release_id"]
    original_pixels = _snapshot(root / STORE)
    with monkeypatch.context() as guard:
        _forbid_array_mutations(guard)
        with output_lock(root):
            run = _new_run(root, "annotation-recovery", "2025-02")
            run.recover()
            run.recover()
    assert _snapshot(root / STORE) == original_pixels
    _assert_scoped_history(root, expected)
    moved = tmp_path / "delivered"
    copytree(root, moved)
    root.rename(tmp_path / "original-unavailable")
    _assert_scoped_history(moved, expected)


@pytest.mark.parametrize("scope", [["output-0"], {"output-0": 42}])
def test_publisher_rejects_malformed_historical_annotation_scope(annotated_history, scope):
    root, first = annotated_history
    pointer_before = _current(root)
    run = _new_run(root, "invalid-history", "2025-02")
    previous = run._previous(first)
    variable = next(v for v in previous["variables"] if "historical_processing_ref" in v)
    variable["historical_output_generations"] = scope
    inventory = publisher.collect_inventory(root, store_paths=[STORE, OTHER_STORE])
    with pytest.raises(ValueError, match="historical annotation"):
        run._carry_annotations(inventory, previous)
    assert _current(root) == pointer_before


def test_metadata_only_v2_preserves_data_revision_and_never_downgrades(annotated_history):
    root, first = annotated_history
    run = _new_run(root, "upgrade-only", "2025-02")
    previous = run._previous(first)
    inventory = publisher.collect_inventory(root, store_paths=[STORE, OTHER_STORE])
    inventory["evidence"] = {**previous["evidence"], **inventory["evidence"]}
    run._carry_annotations(inventory, previous)
    kwargs = dict(dataset_id=DATASET_ID, inventory=inventory,
                  selection={"store_paths": [STORE, OTHER_STORE], "start": None, "end": None})
    pixels = _snapshot(root / STORE)
    old_release = root / "provenance/releases" / first["metadata_release_id"]
    old_bytes = _snapshot(old_release)
    with output_lock(root):
        upgraded = publisher._publish_records_locked(root, **kwargs)
        assert upgraded["schema_version"] == 2
        assert upgraded["data_revision"] == first["data_revision"]
        assert publisher._publish_records_locked(root, **kwargs)["unchanged"]
        plain = publisher.collect_inventory(root, store_paths=[STORE, OTHER_STORE])
        later = publisher._publish_records_locked(root, **{**kwargs, "inventory": plain})
    assert later["schema_version"] == 2
    assert later["data_revision"] == first["data_revision"]
    assert _snapshot(root / STORE) == pixels and _snapshot(old_release) == old_bytes


@pytest.mark.parametrize("stage", ["receipt", "tile_catalog", "current"])
def test_v2_managed_publication_recovers_each_boundary(tmp_path, monkeypatch, stage):
    from s1grits import provenance_catalog as catalog
    from test_provenance_production import _historical_current_with_catalog, _prepare

    root = production_dataset.__wrapped__(tmp_path)
    first, record = _historical_current_with_catalog(root)
    inventory = publisher.collect_inventory(root, store_paths=[STORE])
    variable = next(v for v in inventory["variables"] if v["variable"] == "VV_dB")
    variable["historical_output_generations"] = {r["output_id"]: None for r in inventory["outputs"]}
    with output_lock(root):
        publisher._publish_records_locked(root, dataset_id=DATASET_ID, inventory=inventory,
                                          selection={"store_paths": [STORE], "start": None, "end": None})
        run = _new_run(root, "fault-v2", "2025-02", catalog_mode="tile_global_v1")
        _prepare(run, "2025-02")
        run.begin_write(selected_source_ids=["source-a"])
        _write_month(root, "2025-02", 8)
        record.update(item_id="18NUF_ASCENDING_TK120_2025-02", month="2025-02", datetime="2025-02-15T00:00:00Z")
        run.commit(used_source_ids=["source-a"], catalog_records=[record])
    pixels = _snapshot(root / STORE)
    if stage != "receipt":
        with monkeypatch.context() as fault:
            if stage == "tile_catalog":
                original = catalog._install_catalog

                def interrupt(root, target, entry):
                    if target == "catalog.parquet":
                        raise OSError("injected v2 Catalog boundary")
                    return original(root, target, entry)

                fault.setattr(catalog, "_install_catalog", interrupt)
            else:
                original = publisher._atomic_json

                def interrupt(path, value):
                    if path.name == "completed.json":
                        raise OSError("injected v2 current boundary")
                    return original(path, value)

                fault.setattr("s1grits.provenance_production._atomic_json", interrupt)
            with output_lock(root), pytest.raises(OSError, match="injected v2"):
                run.publish()
    with monkeypatch.context() as guard:
        _forbid_array_mutations(guard)
        with output_lock(root):
            recovered = _new_run(root, "fault-v2", "2025-02", catalog_mode="tile_global_v1")
            recovered.recover()
            recovered.recover()
    assert _snapshot(root / STORE) == pixels
    assert _current(root)["schema_version"] == 2
    assert not list((root / "provenance/pending").glob("*.json"))
    assert (root / "provenance/releases" / first["metadata_release_id"]).exists()
