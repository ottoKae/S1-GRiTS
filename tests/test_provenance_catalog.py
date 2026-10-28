"""Real Parquet Catalogs exercise per-operation metadata publication recovery."""
from __future__ import annotations

from copy import deepcopy
import json

import pandas as pd
import pytest

from s1grits import provenance_catalog as catalogs
from s1grits.canonical_catalog_schema import normalize_catalog_record


def _record(*, tile="18NUF", track=18, month="2025-01", day=15):
    relative = f"smonthly_ASCENDING/zarr/s1grits_smonthly_{tile}_ASCENDING_TK{track}.zarr"
    return normalize_catalog_record({
        "item_id": f"{tile}_ASCENDING_TK{track}_{month}",
        "collection_id": "s1grits-smonthly", "product_type": "smonthly",
        "product_label": "smonthly_ASCENDING", "tile_id": tile,
        "flight_direction": "ASCENDING", "crs": "EPSG:32618",
        "transform": [30, 0, 400000, 0, -30, 1000000], "width": 3, "height": 2,
        "datetime": f"{month}-{day:02d}T10:00:00Z", "start_datetime": month + "-01",
        "end_datetime": month + "-28", "month": month,
        "geometry_group_id": f"{tile}_ASCENDING_TK{track}", "track": track,
        "zarr_path": relative, "bands": ["VV_dB", "VH_dB", "n_obs"],
        "processing_level": "ARDC", "created_at": "2025-08-01T00:00:00Z",
        "software_version": "fixture-original", "n_scenes": 2,
    })


def _operation(root, run_id="run-one", **changes):
    row = _record(**changes)
    store = row["tile_id"] + "/" + row["zarr_path"]
    intent = {"run_id": run_id, "store_path": store, "month": row["month"],
              "catalog_before": catalogs.snapshot_catalogs(root, store)}
    receipt = {"run_id": run_id, "store_path": store, "month": row["month"],
               "generation_id": "generation-" + run_id, "catalog_records": [row]}
    return intent, receipt


def _files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def test_empty_snapshot_is_read_only_and_catalog_publication_is_idempotent(tmp_path):
    intent, receipt = _operation(tmp_path)
    assert intent["catalog_before"] == {"18NUF/catalog.parquet": None, "catalog.parquet": None}
    assert not list(tmp_path.iterdir())
    pending = tmp_path / "provenance/pending/run-one.json"
    pending.parent.mkdir(parents=True)
    pending.write_text('{"phase":"committed_provenance_pending"}', encoding="utf-8")
    result = catalogs.publish_run_catalogs(tmp_path, intent, receipt)
    assert catalogs.verify_run_catalogs(tmp_path, "run-one") == result
    assert set(result["catalog_hashes"]) == set(intent["catalog_before"])
    assert pending.exists()
    before = _files(tmp_path)
    assert catalogs.publish_run_catalogs(tmp_path, intent, receipt) == result
    assert _files(tmp_path) == before
    assert not list(tmp_path.rglob("*.zarr"))


def test_same_time_tracks_and_months_survive_and_global_paths_are_rebased(tmp_path):
    operations = [dict(track=18), dict(track=19), dict(track=18, month="2025-02"),
                  dict(tile="18NUG", track=18)]
    for index, changes in enumerate(operations):
        catalogs.publish_run_catalogs(tmp_path, *_operation(tmp_path, f"run-{index}", **changes))
    tile = pd.read_parquet(tmp_path / "18NUF/catalog.parquet")
    global_frame = pd.read_parquet(tmp_path / "catalog.parquet")
    assert len(tile) == 3 and len(global_frame) == 4
    assert {(row.track, row.month) for row in tile.itertuples()} == {
        (18, "2025-01"), (19, "2025-01"), (18, "2025-02")}
    assert all(not value.startswith("18NUF/") for value in tile["zarr_path"])
    assert all(value.startswith(tile_id + "/")
               for value, tile_id in zip(global_frame["zarr_path"], global_frame["tile_id"]))
    assert all(value.startswith(tile_id + "/")
               for value, tile_id in zip(global_frame["item_path"], global_frame["tile_id"]))
    assert set(global_frame["software_version"]) == {"fixture-original"}


def test_representative_time_change_replaces_month_without_losing_other_track(tmp_path):
    catalogs.publish_run_catalogs(tmp_path, *_operation(tmp_path, "track-18"))
    catalogs.publish_run_catalogs(tmp_path, *_operation(tmp_path, "track-19", track=19))
    intent, receipt = _operation(tmp_path, "replace-18", day=23)
    catalogs.publish_run_catalogs(tmp_path, intent, receipt)
    for path in (tmp_path / "catalog.parquet", tmp_path / "18NUF/catalog.parquet"):
        frame = pd.read_parquet(path)
        assert len(frame) == 2
        assert frame.loc[frame["track"] == 18, "datetime"].iloc[0].day == 23
        assert frame.loc[frame["track"] == 19, "datetime"].iloc[0].day == 15
        assert set(frame["created_at"]) == {"2025-08-01T00:00:00Z"}


def test_tile_published_global_interrupted_recovers_only_metadata(tmp_path, monkeypatch):
    intent, receipt = _operation(tmp_path)
    original = catalogs._install_catalog
    def fail_global(root, target, entry):
        if target == "catalog.parquet":
            raise OSError("interrupted before global Catalog")
        original(root, target, entry)
    monkeypatch.setattr(catalogs, "_install_catalog", fail_global)
    with pytest.raises(OSError, match="global Catalog"):
        catalogs.publish_run_catalogs(tmp_path, intent, receipt)
    tile_path = tmp_path / "18NUF/catalog.parquet"
    tile_bytes = tile_path.read_bytes()
    assert not (tmp_path / "catalog.parquet").exists()
    run = tmp_path / "provenance/runs/run-one"
    assert (run / "catalog_plan.json").exists()
    assert not (run / "catalog_receipt.json").exists()
    installed = []
    def resumed(root, target, entry):
        installed.append(target)
        original(root, target, entry)
    def no_rebuild(*args, **kwargs):
        raise AssertionError("Recovery must reuse immutable staged Parquet")
    monkeypatch.setattr(catalogs, "_install_catalog", resumed)
    monkeypatch.setattr(catalogs, "atomic_write_parquet", no_rebuild)
    result = catalogs.publish_run_catalogs(tmp_path, intent, receipt)
    assert installed == ["catalog.parquet"]
    assert tile_path.read_bytes() == tile_bytes
    assert catalogs.verify_run_catalogs(tmp_path, "run-one") == result


def test_third_catalog_state_is_rejected_before_either_target_changes(tmp_path, monkeypatch):
    intent, receipt = _operation(tmp_path)
    original = catalogs._install_catalog
    def interrupt(root, target, entry):
        raise OSError("before all Catalog publication")
    monkeypatch.setattr(catalogs, "_install_catalog", interrupt)
    with pytest.raises(OSError):
        catalogs.publish_run_catalogs(tmp_path, intent, receipt)
    # A separate edit made after preparation is neither the missing baseline nor
    # the staged output. It must not be replaced even though tile is still absent.
    (tmp_path / "catalog.parquet").write_bytes(b"unrelated edit")
    before = _files(tmp_path)
    monkeypatch.setattr(catalogs, "_install_catalog", original)
    with pytest.raises(ValueError, match="neither.*before.*desired"):
        catalogs.publish_run_catalogs(tmp_path, intent, receipt)
    assert _files(tmp_path) == before
    assert not (tmp_path / "18NUF/catalog.parquet").exists()


@pytest.mark.parametrize("problem", ["unreadable", "schema", "absolute", "traversal"])
def test_bad_existing_catalog_fails_snapshot_without_writes(tmp_path, problem):
    path = tmp_path / "catalog.parquet"
    if problem == "unreadable":
        path.write_bytes(b"broken parquet")
    elif problem == "schema":
        pd.DataFrame([{"unexpected": 1}]).to_parquet(path, index=False)
    else:
        row = _record()
        row["zarr_path"] = "C:/private/image.zarr" if problem == "absolute" else "../image.zarr"
        pd.DataFrame([row]).to_parquet(path, index=False)
    before = _files(tmp_path)
    with pytest.raises(ValueError):
        catalogs.snapshot_catalogs(tmp_path, "18NUF/smonthly_ASCENDING/zarr/monthly.zarr")
    assert _files(tmp_path) == before


def test_native_windows_paths_and_already_root_relative_paths_are_normalized(tmp_path):
    intent, receipt = _operation(tmp_path)
    receipt["catalog_records"][0]["zarr_path"] = receipt["catalog_records"][0]["zarr_path"].replace("/", "\\")
    catalogs.publish_run_catalogs(tmp_path, intent, receipt)
    intent, receipt = _operation(tmp_path, "replace-again", day=21)
    row = receipt["catalog_records"][0]
    row["zarr_path"] = "18NUF/" + row["zarr_path"]
    catalogs.publish_run_catalogs(tmp_path, intent, receipt)
    assert len(pd.read_parquet(tmp_path / "18NUF/catalog.parquet")) == 1
    global_path = pd.read_parquet(tmp_path / "catalog.parquet")["zarr_path"].iloc[0]
    assert global_path == intent["store_path"] and global_path.count("18NUF/") == 1


def test_changed_source_receipt_cannot_reuse_existing_catalog_plan(tmp_path):
    intent, receipt = _operation(tmp_path)
    catalogs.publish_run_catalogs(tmp_path, intent, receipt)
    before = _files(tmp_path)
    changed = deepcopy(receipt)
    changed["generation_id"] = "other-generation"
    with pytest.raises(ValueError, match="immutable intent or source receipt"):
        catalogs.publish_run_catalogs(tmp_path, intent, changed)
    assert _files(tmp_path) == before


@pytest.mark.parametrize("artifact", ["plan", "stage", "receipt", "catalog"])
def test_permanent_evidence_and_current_catalog_hashes_are_verified(tmp_path, artifact):
    intent, receipt = _operation(tmp_path)
    catalogs.publish_run_catalogs(tmp_path, intent, receipt)
    run = tmp_path / "provenance/runs/run-one"
    paths = {"plan": run / "catalog_plan.json", "stage": run / "catalogs/0.parquet",
             "receipt": run / "catalog_receipt.json", "catalog": tmp_path / "catalog.parquet"}
    path = paths[artifact]
    if artifact == "receipt":
        value = json.loads(path.read_text(encoding="utf-8"))
        value["catalog_hashes"]["catalog.parquet"] = "0" * 64
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        catalogs.verify_run_catalogs(tmp_path, "run-one")


def test_old_catalog_receipt_is_stale_after_later_accepted_operation(tmp_path):
    catalogs.publish_run_catalogs(tmp_path, *_operation(tmp_path, "first"))
    catalogs.publish_run_catalogs(tmp_path, *_operation(tmp_path, "second", month="2025-02"))
    assert catalogs.verify_run_catalogs(tmp_path, "second")["run_id"] == "second"
    with pytest.raises(ValueError, match="changed since"):
        catalogs.verify_run_catalogs(tmp_path, "first")
