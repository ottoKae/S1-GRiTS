"""Historical metadata export and shared immutable sidecar publication helpers."""
from __future__ import annotations

import argparse
from collections.abc import Callable
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from datetime import datetime, timezone
from uuid import uuid4
from tempfile import TemporaryDirectory

from .file_lock import output_lock
from .provenance_contract import (
    canonical_json, data_revision, read_tables, write_tables, validate_historical_annotations,
)
from .provenance_inventory import _assert_no_pending, collect_inventory

README = """# GRiTS metadata-only companion

This folder contains metadata, not satellite imagery.
Read provenance/current.json, then its immutable release dataset.json,
variables.json, outputs.parquet, sources.parquet and source_links.parquet.
Paths in outputs and source witnesses are relative to the original dataset root.
Keep this companion with the matching imagery; verify using the explicit
dataset root after moving both folders. Do not merge or replace old Catalogs.

Historical evidence gaps remain unknown. Declared scale/offset do not prove
an applied correction. This exporter records observed metadata and coordinates;
it does not hash image chunks or recover undocumented pixel-only replacements.
Source data must remain quiescent during export and verification.
"""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _io_path(path: Path) -> Path:
    """Use the Windows extended namespace for physical companion I/O only.

    A short release directory can contain evidence beyond MAX_PATH: ordinary
    traversal may silently omit those descendants. Keep the prefix on the
    directory so children inherit it. References stay relative POSIX paths.
    """
    path = Path(path)
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def _json(path: Path):
    return json.loads(_io_path(path).read_text(encoding="utf-8"))


def _write_json(path: Path, value) -> None:
    path = _io_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def _atomic_json(path: Path, value) -> None:
    path = _io_path(path)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(canonical_json(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _relative(root: Path, name: str) -> Path:
    if (not isinstance(name, str) or not name or ":" in name or "\\" in name
            or any(ord(c) < 32 for c in name)):
        raise ValueError("invalid relative companion path")
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or name == ".":
        raise ValueError("companion path escapes root")
    # Compare paths in one namespace: pathlib treats extended and ordinary
    # Windows prefixes as different roots, even when they name the same file.
    root = _io_path(root)
    target = root.joinpath(*pure.parts)
    current = target
    while current != root:
        if current.is_symlink() or current.is_junction():
            raise ValueError("companion links are not permitted")
        current = current.parent
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("companion path escapes root")
    return target


def _current(root: Path):
    path = _relative(root, "provenance/current.json")
    return _json(path) if path.exists() else None


def _observer() -> dict:
    files = ("provenance.py", "provenance_contract.py", "provenance_inventory.py")
    return {
        "role": "metadata_observer_not_historical_producer",
        "code_sha256": {name: _digest(Path(__file__).with_name(name).read_bytes()) for name in files},
        "python": sys.version.split()[0],
        "dependencies": {name: importlib.metadata.version(name)
                         for name in ("zarr", "pyarrow", "pandas", "numpy", "numcodecs")},
    }


def _manifest(directory: Path) -> dict[str, str]:
    directory = _io_path(directory)
    result = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        _relative(directory, relative)
        if path.is_file() and relative != "FILES.sha256":
            result[relative] = _digest(path.read_bytes())
    return result


def _verify_release(directory: Path, pointer: dict) -> dict:
    directory = _io_path(directory)
    if type(pointer.get("schema_version")) is not int or pointer["schema_version"] not in (1, 2):
        raise ValueError("Unsupported provenance release version; supported: 1, 2. Upgrade S1-GRiTS.")
    manifest_path = _relative(directory, "FILES.sha256")
    if _digest(manifest_path.read_bytes()) != pointer["manifest_sha256"]:
        raise ValueError("manifest hash mismatch")
    listed = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest) or name in listed:
            raise ValueError("invalid or duplicate manifest record")
        _relative(directory, name)
        listed[name] = digest
    if listed != _manifest(directory):
        raise ValueError("release contents differ from manifest")
    dataset = _json(directory / "dataset.json")
    if _digest((directory / "dataset.json").read_bytes()) != pointer["dataset_sha256"]:
        raise ValueError("dataset manifest hash mismatch")
    if type(dataset.get("schema_version")) is not int:
        raise ValueError("invalid release version type")
    for field in ("data_revision", "metadata_release_id", "schema_version"):
        if dataset[field] != pointer[field]:
            raise ValueError("pointer and release disagree")
    if dataset["delivery_kind"] != "metadata_only":
        raise ValueError("unexpected delivery kind")
    outputs, sources, links = read_tables(directory)
    for row in outputs + sources + links:
        ref = row.get("evidence_ref")
        if ref and (ref not in listed or not _relative(directory, ref).is_file()):
            raise ValueError("missing evidence reference")
    witnesses = _json(directory / "evidence/data_witnesses.json")
    if data_revision(outputs, witnesses) != dataset["data_revision"]:
        raise ValueError("data revision disagrees with output mapping")
    counts = {"outputs": len(outputs), "sources": len(sources), "source_links": len(links)}
    if counts != dataset["counts"]:
        raise ValueError("table counts disagree")
    if pointer["schema_version"] == 2:
        evidence = {name.removeprefix("evidence/"): _json(_relative(directory, name))
                    for name in listed if name.startswith("evidence/") and name.endswith(".json")}
        validate_historical_annotations(outputs, _json(directory / "variables.json"),
                                        _json(directory / "limitations.json"), evidence, required=True)
    return dataset


def verify_bundle(output_root, *, dataset_root=None) -> dict:
    """Check published bytes/schema/refs; optionally re-observe the source data."""
    root = _io_path(output_root).resolve(strict=True)
    pointer = _current(root)
    if not pointer:
        raise ValueError("no published metadata release")
    release_id = pointer["metadata_release_id"]
    if not re.fullmatch(r"meta-[0-9a-f]{64}", release_id):
        raise ValueError("invalid metadata release id")
    directory = _relative(root, "provenance/releases/" + release_id)
    dataset = _verify_release(directory, pointer)
    result = {"valid": True, "metadata_release_id": release_id,
              "data_revision": dataset["data_revision"], "counts": dataset["counts"],
              "source_checked": dataset_root is not None, "pixel_chunks_checked": False}
    if dataset_root is not None:
        if dataset["execution_kind"] != "historical_import":
            raise ValueError("Production source verification requires the production recovery verifier")
        selection = dataset["selection"]
        observed = collect_inventory(Path(dataset_root), **selection)
        if data_revision(observed["outputs"], observed["witnesses"]) != dataset["data_revision"]:
            raise ValueError("source revision changed; rebuild metadata")
    return result


def publish_inventory(dataset_root, output_root, *, dataset_id: str,
                      store_paths: list[str], start: str | None = None,
                      end: str | None = None, quiescent: bool = False,
                      expected_release_id: str | None = None, note: str = "") -> dict:
    """Publish a detached sidecar while leaving the selected data root read-only.

    expected_release_id=None means the caller expects an unpublished output.
    This serializes companion publication, not a separate production writer.
    """
    if not quiescent:
        raise ValueError("confirm source quiescence before historical export")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", dataset_id):
        raise ValueError("dataset_id must be a stable public identifier")
    source = Path(dataset_root).resolve(strict=True)
    destination = Path(output_root).resolve()
    source_location, destination_location = _io_path(source), _io_path(destination)
    if (destination_location.is_relative_to(source_location)
            or source_location.is_relative_to(destination_location)):
        raise ValueError("P2 requires a detached destination outside the source dataset")
    # shortcut: detached P2 does not lock the producer; P3 must share its write lock.
    selection = {"store_paths": sorted(set(store_paths)), "start": start, "end": end}
    _relative(destination, ".s1grits.lock")
    with output_lock(destination):
        current = _current(destination)
        actual_id = current["metadata_release_id"] if current else None
        if actual_id != expected_release_id:
            raise ValueError("stale expected metadata release")
        if current:
            verify_bundle(destination)
            previous = _json(_relative(destination, "provenance/releases/" + actual_id + "/dataset.json"))
            if previous["dataset_id"] != dataset_id:
                raise ValueError("dataset_id cannot change within an existing companion")
        inventory = collect_inventory(source, **selection)
        def source_unchanged():
            after = collect_inventory(source, **selection)
            if canonical_json(after) != canonical_json(inventory):
                raise ValueError("source changed during export; release not published")
        return _publish_records_locked(
            destination, dataset_id=dataset_id, inventory=inventory,
            selection=selection, note=note, before_publish=source_unchanged,
        )


def _publish_records_locked(output_root: Path, *, dataset_id: str, inventory: dict,
                            selection: dict, execution_kind: str = "historical_import",
                            note: str = "", extra_dataset: dict | None = None,
                            before_publish: Callable[[], None] | None = None) -> dict:
    """Publish supplied records while the caller holds the root writer lock.

    The caller owns expected-release comparison and producer receipt validation.
    This helper never acquires another lock or writes imagery. Evidence keys are
    relative to the release's evidence directory, including nested run records.
    ``before_publish`` validates source/receipt state before any pointer update;
    it also runs when the requested release is already current.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", dataset_id):
        raise ValueError("dataset_id must be a stable public identifier")
    if execution_kind not in {"historical_import", "production", "repair", "metadata_recheck"}:
        raise ValueError("unsupported execution_kind")
    destination = _io_path(output_root).resolve()
    current = _current(destination)
    actual_id = current["metadata_release_id"] if current else None
    if current:
        previous = _verify_release(
            _relative(destination, "provenance/releases/" + actual_id), current,
        )
        if previous["dataset_id"] != dataset_id:
            raise ValueError("dataset_id cannot change within an existing companion")
    if not inventory["outputs"]:
        raise ValueError("selection has no output layers")
    revision = data_revision(inventory["outputs"], inventory["witnesses"])
    scoped = validate_historical_annotations(
        inventory["outputs"], inventory["variables"], inventory["limitations"], inventory["evidence"],
        required=bool(current and current["schema_version"] == 2))
    release_version = 2 if scoped or (current and current["schema_version"] == 2) else 1
    observer = _observer()
    fingerprint_input = {
        "schema_version": release_version, "dataset_id": dataset_id,
        "selection": selection, "inventory": inventory, "note": note, "observer": observer,
    }
    if execution_kind != "historical_import" or extra_dataset:
        fingerprint_input.update(execution_kind=execution_kind, extra_dataset=extra_dataset)
    fingerprint = _digest(canonical_json(fingerprint_input))
    release_id = "meta-" + fingerprint
    if current and actual_id == release_id:
        if before_publish is not None:
            before_publish()
        return {**current, "unchanged": True}
    provenance = _relative(destination, "provenance")
    release = _relative(destination, "provenance/releases/" + release_id)
    staging_parent = _relative(destination, "provenance/staging")
    staging_parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="export-", dir=staging_parent) as temporary:
        staging = Path(temporary)
        write_tables(staging, inventory["outputs"], inventory["sources"], inventory["source_links"])
        for name, value in inventory.get("evidence", {}).items():
            path = _relative(staging, "evidence/" + name)
            _write_json(path, value)
        _write_json(staging / "evidence/data_witnesses.json", inventory["witnesses"])
        _write_json(staging / "evidence/observer.json", observer)
        _write_json(staging / "variables.json", inventory["variables"])
        _write_json(staging / "limitations.json", inventory["limitations"])
        dataset = {
            "schema_version": release_version, "dataset_id": dataset_id,
            "data_revision": revision, "metadata_release_id": release_id,
            "delivery_kind": "metadata_only", "execution_kind": execution_kind,
            "evidence_status": "partial_evidence", "selection": selection, "note": note,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "counts": {key: len(inventory[key]) for key in ("outputs", "sources", "source_links")},
            "revision_scope": ("Original metadata and coordinate witnesses; not a pixel checksum."
                               if execution_kind == "historical_import"
                               else "Output generations, metadata, coordinates and supplied commit witnesses; not a pixel checksum."),
        }
        if extra_dataset:
            conflict = set(extra_dataset) & set(dataset)
            if conflict:
                raise ValueError(f"extra_dataset cannot replace publisher fields: {sorted(conflict)}")
            dataset.update(extra_dataset)
        _write_json(staging / "dataset.json", dataset)
        _write_json(staging / "verification.json", {
            "method": ("fixed_schema_roundtrip_manifest_and_before_after_inventory"
                       if execution_kind == "historical_import"
                       else "fixed_schema_roundtrip_manifest_and_caller_validation"),
            "quiescence": "operator_declared" if execution_kind == "historical_import" else "caller_writer_lock",
            "pixel_chunks_checked": False, "source_mutation": "none_by_exporter",
            "validation": "required_before_pointer_publish",
        })
        files = _manifest(staging)
        (staging / "FILES.sha256").write_text(
            "".join(digest + "  " + name + "\n" for name, digest in files.items()), encoding="utf-8",
        )
        pointer = {"schema_version": release_version, "metadata_release_id": release_id,
                   "data_revision": revision,
                   "dataset_sha256": _digest((staging / "dataset.json").read_bytes()),
                   "manifest_sha256": _digest((staging / "FILES.sha256").read_bytes())}
        _verify_release(staging, pointer)
        if before_publish is not None:
            before_publish()
        release.parent.mkdir(parents=True, exist_ok=True)
        if release.exists():
            # A crash after directory promotion may have left a valid orphan release.
            old_dataset = _json(_relative(release, "dataset.json"))
            old_pointer = {**pointer,
                "dataset_sha256": _digest((release / "dataset.json").read_bytes()),
                "manifest_sha256": _digest((release / "FILES.sha256").read_bytes())}
            _verify_release(release, old_pointer)
            old_files = _manifest(release)
            if {k: v for k, v in old_files.items() if k != "dataset.json"} != {
                    k: v for k, v in files.items() if k != "dataset.json"}:
                raise ValueError("orphan release content disagrees with inventory")
            if {k: v for k, v in old_dataset.items() if k != "observed_at"} != {
                    k: v for k, v in dataset.items() if k != "observed_at"}:
                raise ValueError("existing immutable release disagrees")
            pointer = old_pointer
        else:
            os.replace(staging, release)
        readme = _relative(destination, "DATASET_README.md")
        if not readme.exists():
            readme.write_text(README, encoding="utf-8")
        _atomic_json(provenance / "current.json", pointer)
        return {**pointer, "unchanged": False}


def attach_inventory(metadata_root, dataset_root, *, quiescent: bool = False,
                     expected_release_id: str | None = None) -> dict:
    """Attach a verified historical companion to its existing image-data root.

    The source release must bind an explicitly prepared DATASET_README.md using
    attachment_readme_sha256. Existing dataset documentation is never replaced.
    """
    if not quiescent:
        raise ValueError("confirm source quiescence before metadata attachment")
    roots = []
    for value in (metadata_root, dataset_root):
        path = Path(value).absolute()
        if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
            raise ValueError("attachment roots must not traverse links")
        path = _io_path(path).resolve(strict=True)
        if not path.is_dir():
            raise ValueError("attachment roots must be existing directories")
        roots.append(path)
    source, destination = roots
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError("attachment requires independent metadata and data roots")
    _relative(destination, ".s1grits.lock")
    with output_lock(destination, timeout=0):
        current = _current(destination)
        actual_id = current["metadata_release_id"] if current else None
        if actual_id != expected_release_id:
            raise ValueError("stale expected metadata release")
        _assert_no_pending(source)
        pointer = _current(source)
        if not pointer:
            raise ValueError("no published metadata release")
        verify_bundle(source, dataset_root=destination)
        release_name = "provenance/releases/" + pointer["metadata_release_id"]
        source_release = _relative(source, release_name)
        dataset = _verify_release(source_release, pointer)
        if dataset["execution_kind"] != "historical_import":
            raise ValueError("only historical metadata can be attached")
        source_readme = _relative(source, "DATASET_README.md")
        readme_bytes = source_readme.read_bytes()
        if (_digest(readme_bytes) != dataset.get("attachment_readme_sha256")
                or not readme_bytes.decode("utf-8").strip()):
            raise ValueError("release must bind an explicit attachment README")
        if current:
            verify_bundle(destination)
            if pointer["schema_version"] < current["schema_version"]:
                raise ValueError("attachment cannot downgrade the provenance release version")
            previous = _json(_relative(destination, "provenance/releases/" + actual_id + "/dataset.json"))
            if previous["execution_kind"] != "historical_import":
                raise ValueError("attachment cannot replace production provenance")
            if previous["dataset_id"] != dataset["dataset_id"]:
                raise ValueError("dataset_id cannot change during attachment")
            if current["data_revision"] != pointer["data_revision"]:
                raise ValueError("attachment cannot change the published data revision")

        def source_unchanged():
            _assert_no_pending(source)
            if _current(source) != pointer or source_readme.read_bytes() != readme_bytes:
                raise ValueError("metadata source changed during attachment")
            verify_bundle(source, dataset_root=destination)
            if _current(destination) != current:
                raise ValueError("destination metadata changed during attachment")

        readme = _relative(destination, "DATASET_README.md")
        if current == pointer:
            source_unchanged()
            if not readme.exists():
                raise ValueError("attached dataset README is missing")
            return {**pointer, "unchanged": True, "readme_preserved": True}
        release = _relative(destination, release_name)
        staging_parent = _relative(destination, "provenance/staging")
        staging_parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="attach-", dir=staging_parent) as temporary:
            staging = Path(temporary)
            for name in [*_manifest(source_release), "FILES.sha256"]:
                target = _relative(staging, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_relative(source_release, name).read_bytes())
            _verify_release(staging, pointer)
            source_unchanged()
            if release.exists():
                _verify_release(release, pointer)
            else:
                release.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, release)
            preserved = readme.exists()
            if not preserved:
                # An exclusive create cannot overwrite unrelated dataset documentation.
                with readme.open("xb") as stream:
                    stream.write(readme_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
            _atomic_json(_relative(destination, "provenance/current.json"), pointer)
        return {**pointer, "unchanged": False, "readme_preserved": preserved}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--dataset-root", required=True)
    build.add_argument("--output-root", required=True)
    build.add_argument("--dataset-id", required=True)
    build.add_argument("--store", action="append", required=True, dest="store_paths")
    build.add_argument("--start")
    build.add_argument("--end")
    build.add_argument("--quiescent", action="store_true")
    build.add_argument("--expected-release", dest="expected_release_id")
    build.add_argument("--note", default="")
    attach = commands.add_parser("attach")
    attach.add_argument("--metadata-root", required=True)
    attach.add_argument("--dataset-root", required=True)
    attach.add_argument("--quiescent", action="store_true")
    attach.add_argument("--expected-release", dest="expected_release_id")
    check = commands.add_parser("verify")
    check.add_argument("--output-root", required=True)
    check.add_argument("--dataset-root")
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    operation = {"build": publish_inventory, "verify": verify_bundle, "attach": attach_inventory}[command]
    result = operation(**args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
