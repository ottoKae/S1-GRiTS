"""In-place metadata attachment must preserve original data and publication safety."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
import zarr

from s1grits import provenance as p


def snapshot(root):
    root = p._io_path(root)
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def release(root):
    pointer = p._current(root)
    return p._relative(root, "provenance/releases/" + pointer["metadata_release_id"])


@pytest.fixture
def bundle(tmp_path):
    data = tmp_path / "data"
    store = "18NUF/monthly.zarr"
    group = zarr.open_group(str(data / store), mode="w", zarr_format=3)
    group.attrs.update(product_type="smonthly", grid_id="grid-30", geometry_group_id="track-120")
    group.create_array("time", data=np.array(["2025-01-16", "2025-02-17"], dtype="datetime64[ns]"))
    group.create_array("VV", data=np.arange(12, dtype="f4").reshape(2, 2, 3), chunks=(1, 2, 3))
    (data / ".s1grits.lock").write_text("")
    (data / "legacy.txt").write_text("Original data documentation")
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    readme = b"# Dataset with imagery and provenance\nRead provenance/current.json.\n"
    (metadata / "DATASET_README.md").write_bytes(readme)
    selection = {"store_paths": [store], "start": None, "end": None}
    inventory = p.collect_inventory(data, **selection)
    inventory["evidence"]["history/questions.json"] = {"answer": None, "reason": "No original execution binding."}
    with p.output_lock(metadata):
        p._publish_records_locked(
            metadata, dataset_id="ecuador-s1", inventory=inventory, selection=selection,
            extra_dataset={"attachment_readme_sha256": hashlib.sha256(readme).hexdigest()},
        )
    return data, metadata, group, p._current(metadata), inventory, selection


def attach(bundle, **kwargs):
    data, metadata, *_ = bundle
    return p.attach_inventory(metadata, data, quiescent=True, **kwargs)


def test_attach_original_bytes_unchanged_repeat_and_cli(bundle, capsys):
    data, metadata, _, pointer, *_ = bundle
    original, companion = snapshot(data), snapshot(metadata)
    assert p.main(["attach", "--metadata-root", str(metadata), "--dataset-root", str(data), "--quiescent"]) == 0
    assert '"unchanged": false' in capsys.readouterr().out
    assert p._current(data) == pointer
    assert snapshot(release(data)) == snapshot(release(metadata))
    assert all(snapshot(data)[name] == digest for name, digest in original.items())
    assert snapshot(metadata) == companion
    before = snapshot(data)
    repeated = attach(bundle, expected_release_id=pointer["metadata_release_id"])
    assert repeated["unchanged"] and repeated["readme_preserved"]
    assert snapshot(data) == before
    assert p.verify_bundle(data, dataset_root=data)["valid"]


def test_explicit_readme_required_no_generic_companion_text(bundle):
    data, metadata, _, pointer, inventory, selection = bundle
    with p.output_lock(metadata):
        p._publish_records_locked(metadata, dataset_id="ecuador-s1", inventory=inventory, selection=selection)
    original = snapshot(data)
    with pytest.raises(ValueError, match="explicit attachment README"):
        attach(bundle)
    assert snapshot(data) == original


def test_missing_or_changed_readme_is_rejected(bundle):
    data, metadata, *_ = bundle
    original = snapshot(data)
    (metadata / "DATASET_README.md").write_text("Unbound text")
    with pytest.raises(ValueError, match="explicit attachment README"):
        attach(bundle)
    assert snapshot(data) == original
    (metadata / "DATASET_README.md").unlink()
    with pytest.raises(FileNotFoundError):
        attach(bundle)


def test_existing_dataset_readme_is_preserved(bundle):
    data, *_ = bundle
    text = b"Existing dataset README"
    (data / "DATASET_README.md").write_bytes(text)
    result = attach(bundle)
    assert result["readme_preserved"]
    assert (data / "DATASET_README.md").read_bytes() == text


def test_stale_expected_release_refuses_before_publication(bundle):
    data, metadata, _, pointer, *_ = bundle
    attach(bundle)
    old = snapshot(data)
    with pytest.raises(ValueError, match="stale expected"):
        attach(bundle)
    assert snapshot(data) == old


@pytest.mark.parametrize("target", ["data", "metadata"])
def test_pending_rejected(bundle, target):
    data, metadata, *_ = bundle
    folder = (data if target == "data" else metadata) / "provenance/pending"
    folder.mkdir(parents=True)
    (folder / "unfinished.json").write_text("{}")
    old = snapshot(data)
    with pytest.raises(ValueError, match="pending"):
        attach(bundle)
    assert snapshot(data) == old


def test_source_array_coordinate_change_refuses(bundle):
    data, _, group, *_ = bundle
    group["time"][0] = np.datetime64("2025-01-18", "ns")
    old = snapshot(data)
    with pytest.raises(ValueError, match="source revision changed"):
        attach(bundle)
    assert snapshot(data) == old


def test_tampered_metadata_refuses(bundle):
    data, metadata, *_ = bundle
    (release(metadata) / "variables.json").write_text("[]")
    old = snapshot(data)
    with pytest.raises(ValueError, match="manifest"):
        attach(bundle)
    assert snapshot(data) == old


def test_update_preserves_old_release_and_data_revision(bundle):
    data, metadata, _, first, inventory, selection = bundle
    attach(bundle)
    old_release = snapshot(release(data))
    readme_digest = hashlib.sha256((metadata / "DATASET_README.md").read_bytes()).hexdigest()
    inventory["evidence"]["history/questions.json"]["scope"] = "Explicitly limited to this archive"
    with p.output_lock(metadata):
        second = p._publish_records_locked(
            metadata, dataset_id="ecuador-s1", inventory=inventory, selection=selection,
            extra_dataset={"attachment_readme_sha256": readme_digest},
        )
    assert first["data_revision"] == second["data_revision"]
    assert first["metadata_release_id"] != second["metadata_release_id"]
    result = attach(bundle, expected_release_id=first["metadata_release_id"])
    assert result["metadata_release_id"] == second["metadata_release_id"]
    assert snapshot(data / "provenance/releases" / first["metadata_release_id"]) == old_release


@pytest.mark.parametrize("change", ["production", "dataset_id", "data_revision"])
def test_rejects_replacing_production_or_changing_dataset_identity_or_revision(bundle, change):
    data, metadata, group, first, inventory, selection = bundle
    attach(bundle)
    readme_digest = hashlib.sha256((metadata / "DATASET_README.md").read_bytes()).hexdigest()
    if change == "production":
        with p.output_lock(data):
            existing = p._publish_records_locked(
                data, dataset_id="ecuador-s1", inventory=inventory, selection=selection,
                execution_kind="production", extra_dataset={"attachment_readme_sha256": readme_digest},
            )
        expected = existing["metadata_release_id"]
        message = "cannot replace production"
    elif change == "dataset_id":
        # A different independent companion may be valid, but cannot rename this dataset.
        other = metadata.parent / "other-metadata"
        other.mkdir()
        (other / "DATASET_README.md").write_bytes((metadata / "DATASET_README.md").read_bytes())
        with p.output_lock(other):
            p._publish_records_locked(
                other, dataset_id="different-dataset", inventory=inventory, selection=selection,
                extra_dataset={"attachment_readme_sha256": readme_digest},
            )
        metadata = other
        expected = first["metadata_release_id"]
        message = "dataset_id cannot change"
    else:
        group["time"][0] = np.datetime64("2025-01-18", "ns")
        refreshed = p.collect_inventory(data, **selection)
        with p.output_lock(metadata):
            p._publish_records_locked(
                metadata, dataset_id="ecuador-s1", inventory=refreshed, selection=selection,
                extra_dataset={"attachment_readme_sha256": readme_digest},
            )
        expected = first["metadata_release_id"]
        message = "cannot change the published data revision"
    before = snapshot(data)
    with pytest.raises(ValueError, match=message):
        p.attach_inventory(metadata, data, quiescent=True, expected_release_id=expected)
    assert snapshot(data) == before


def test_interrupted_pointer_recovers_existing_release_without_rewriting_data(bundle, monkeypatch):
    data, _, _, pointer, *_ = bundle
    original = snapshot(data)
    atomic = p._atomic_json
    def fail(*args, **kwargs):
        raise OSError("simulated pointer interruption")
    monkeypatch.setattr(p, "_atomic_json", fail)
    with pytest.raises(OSError, match="pointer interruption"):
        attach(bundle)
    assert not (data / "provenance/current.json").exists()
    orphan = data / "provenance/releases" / pointer["metadata_release_id"]
    frozen = snapshot(orphan)
    monkeypatch.setattr(p, "_atomic_json", atomic)
    result = attach(bundle)
    assert not result["unchanged"]
    assert snapshot(orphan) == frozen
    assert all(snapshot(data)[name] == digest for name, digest in original.items())
    assert not list((data / "provenance/staging").iterdir())


def test_source_pointer_changes_while_staging_are_rejected(bundle, monkeypatch):
    data, metadata, *_ = bundle
    verify = p._verify_release
    def changed(directory, pointer):
        result = verify(directory, pointer)
        if Path(directory).name.startswith("attach-"):
            (metadata / "provenance/current.json").write_text("{}")
        return result
    monkeypatch.setattr(p, "_verify_release", changed)
    with pytest.raises(ValueError, match="metadata source changed"):
        attach(bundle)
    assert not (data / "provenance/current.json").exists()
    assert not list((data / "provenance/staging").iterdir())


def test_companion_link_is_rejected(bundle, monkeypatch):
    data, metadata, *_ = bundle
    old = snapshot(data)
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path.name == metadata.name or original(path))
    with pytest.raises(ValueError, match="must not traverse links"):
        attach(bundle)
    assert snapshot(data) == old


def test_shared_writer_lock_blocks_attachment(bundle):
    data, *_ = bundle
    original = snapshot(data)
    with p.output_lock(data):
        with pytest.raises(TimeoutError):
            attach(bundle)
    assert snapshot(data) == original


def test_overlap_and_missing_quiescence_rejected(bundle):
    data, metadata, *_ = bundle
    with pytest.raises(ValueError, match="quiescence"):
        p.attach_inventory(metadata, data)
    with pytest.raises(ValueError, match="independent"):
        p.attach_inventory(data, data, quiescent=True)


def test_attachment_does_not_read_image_arrays(bundle, monkeypatch):
    original = zarr.Array.__getitem__
    def coordinates_only(array, key):
        assert array.ndim < 2, "Metadata attachment must not read image arrays"
        return original(array, key)
    monkeypatch.setattr(zarr.Array, "__getitem__", coordinates_only)
    assert not attach(bundle)["unchanged"]


def test_link_inside_source_release_is_rejected(bundle, monkeypatch):
    data, *_ = bundle
    before = snapshot(data)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "variables.json" or original(path))
    with pytest.raises(ValueError, match="links are not permitted"):
        attach(bundle)
    assert snapshot(data) == before


def test_attach_accepts_v2_and_refuses_downgrade(bundle, tmp_path):
    import json
    from shutil import copytree

    data, metadata, _, first, inventory, selection = bundle
    original_data = snapshot(data)
    row = inventory["outputs"][0]
    inventory["variables"][0]["historical_output_generations"] = {row["output_id"]: None}
    with p.output_lock(metadata):
        upgraded = p._publish_records_locked(
            metadata, dataset_id="ecuador-s1", inventory=inventory, selection=selection,
            extra_dataset={"attachment_readme_sha256": hashlib.sha256((metadata / "DATASET_README.md").read_bytes()).hexdigest()},
        )
    assert upgraded["schema_version"] == 2 and upgraded["data_revision"] == first["data_revision"]
    attach(bundle)
    assert all(snapshot(data)[name] == digest for name, digest in original_data.items())
    older = tmp_path / "older-companion"
    copytree(metadata, older)
    (older / "provenance/current.json").write_text(json.dumps({k: v for k, v in first.items() if k != "unchanged"}), encoding="utf-8")
    before = snapshot(data)
    with pytest.raises(ValueError, match="downgrade"):
        p.attach_inventory(older, data, quiescent=True, expected_release_id=upgraded["metadata_release_id"])
    assert snapshot(data) == before
