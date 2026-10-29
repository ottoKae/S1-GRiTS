"""Recoverable tile/global Catalog publication under the caller's root lock.

This module changes Catalog metadata only. Pixel receipts and pending cleanup
belong to the production coordinator; it never acquires another writer lock.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import shutil

import pandas as pd

from .atomic_write import atomic_path, atomic_write_parquet
from .canonical_catalog_schema import (
    CANONICAL_CATALOG_COLUMNS, CATALOG_PATH_COLUMNS, SCHEMA_VERSION,
    normalize_catalog_record, rebase_catalog_paths, validate_collection_mapping,
)
from .provenance import _atomic_json, _digest, _json, _relative
from .provenance_contract import canonical_json

_TIME_COLUMNS = ("datetime", "start_datetime", "end_datetime")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _posix(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Catalog paths must be nonempty relative paths")
    value = value.replace("\\", "/")
    if (":" in value or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(c) < 32 for c in value) or value.startswith("/")):
        raise ValueError("Catalog path must not escape the dataset")
    return value


def _targets(root: Path, store_path: str) -> tuple[str, list[str]]:
    store = _posix(store_path)
    if store != store_path or len(PurePosixPath(store).parts) < 2 or not store.endswith(".zarr"):
        raise ValueError("store_path must be a root-relative POSIX Zarr path")
    _relative(root, store)
    tile = PurePosixPath(store).parts[0]
    targets = [tile + "/catalog.parquet", "catalog.parquet"]
    for target in targets:
        _relative(root, target)
    return tile, targets


def _run_directory(root: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("invalid Catalog run_id")
    return _relative(root, "provenance/runs/" + run_id)


def _file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("Catalog artifact is not a regular file: " + str(path))
    return _digest(path.read_bytes())


def _canonical_frame(frame: pd.DataFrame, *, root: Path, tile: str | None) -> pd.DataFrame:
    """Validate current schema, preserving existing row facts and optional fields."""
    if not frame.columns.is_unique or set(frame.columns) != set(CANONICAL_CATALOG_COLUMNS):
        raise ValueError("Catalog columns do not match the canonical schema")
    frame = frame.loc[:, CANONICAL_CATALOG_COLUMNS].copy()
    if not frame.empty and not frame["schema_version"].eq(SCHEMA_VERSION).all():
        raise ValueError("Catalog schema version is unsupported; explicit migration required")
    for key in ("tile_id", "product_type", "collection_id"):
        if any(not isinstance(value, str) or not value for value in frame[key]):
            raise ValueError("Catalog identity field is missing: " + key)
    if tile is not None and not frame["tile_id"].eq(tile).all():
        raise ValueError("tile Catalog contains a different tile identity")
    validate_collection_mapping(frame)
    for key in _TIME_COLUMNS:
        try:
            frame[key] = pd.to_datetime(frame[key], utc=True, errors="raise").dt.tz_localize(None)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Catalog timestamp in " + key) from exc
    for index, row in frame.iterrows():
        row_tile = _posix(row["tile_id"])
        if "/" in row_tile:
            raise ValueError("Catalog tile_id must be a plain directory name")
        for key in CATALOG_PATH_COLUMNS:
            value = row[key]
            if value is None or pd.isna(value):
                continue
            path = _posix(value)
            if path.startswith(row_tile + "/"):
                path = path[len(row_tile) + 1:]
            # Native tile-relative paths and already rebased global paths are
            # accepted; every stored global path is rebased exactly once below.
            first = path.split("/", 1)[0]
            if re.fullmatch(r"[0-9]{2}[A-Z]{3}", first):
                raise ValueError("Catalog asset path has a conflicting tile prefix")
            _relative(root, row_tile + "/" + path)
            frame.at[index, key] = path
        if row["product_type"] == "smonthly":
            month = row["month"]
            if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
                raise ValueError("monthly Catalog row has invalid month")
            if not isinstance(row["zarr_path"], str) or pd.isna(row["datetime"]):
                raise ValueError("monthly Catalog row lacks its store or representative time")
            if row["datetime"].strftime("%Y-%m") != month:
                raise ValueError("Catalog representative time disagrees with month")
    if tile is None:
        parts = []
        for _, part in frame.groupby("tile_id", sort=False):
            parts.append(rebase_catalog_paths(part.copy(), str(part["tile_id"].iloc[0])))
        if parts:
            frame = pd.concat(parts).sort_index()
    return frame.reset_index(drop=True)


def _read_catalog(root: Path, target: str, tile: str | None) -> pd.DataFrame:
    path = _relative(root, target)
    if not path.exists():
        return pd.DataFrame(columns=CANONICAL_CATALOG_COLUMNS)
    try:
        frame = pd.read_parquet(path)
    except Exception as exc:
        raise ValueError("existing Catalog is unreadable: " + target) from exc
    return _canonical_frame(frame, root=root, tile=tile)


def snapshot_catalogs(root, store_path: str) -> dict[str, str | None]:
    """Validate tile/global Catalogs and capture their exact pre-write bytes."""
    root = Path(root).resolve(strict=True)
    tile, targets = _targets(root, store_path)
    result = {}
    for target in targets:
        before = _file_hash(_relative(root, target))
        _read_catalog(root, target, tile if target != "catalog.parquet" else None)
        after = _file_hash(_relative(root, target))
        if before != after:
            raise ValueError("Catalog changed while taking its snapshot")
        result[target] = before
    return result


def _incoming(root: Path, intent: dict, receipt: dict, tile: str) -> pd.DataFrame:
    records = receipt.get("catalog_records")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError("one committed operation requires one native Catalog record")
    record = records[0]
    if set(record) != set(CANONICAL_CATALOG_COLUMNS) or not record.get("created_at"):
        raise ValueError("receipt must preserve a complete normalized Catalog row and created_at")
    if record.get("schema_version") != SCHEMA_VERSION or record.get("status") != "complete":
        raise ValueError("receipt Catalog record must use the current complete schema")
    frame = _canonical_frame(pd.DataFrame([normalize_catalog_record(record)]), root=root, tile=tile)
    row = frame.iloc[0]
    if row["product_type"] != "smonthly" or tile + "/" + row["zarr_path"] != intent["store_path"]:
        raise ValueError("receipt Catalog record does not match the intended monthly store")
    month = intent.get("month", receipt.get("month"))
    if month is not None and row["month"] != month:
        raise ValueError("receipt Catalog record month differs from the intent")
    return frame


def _merge(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    row = incoming.iloc[0]
    replace = (existing["product_type"].eq("smonthly")
               & existing["zarr_path"].eq(row["zarr_path"])
               & existing["month"].eq(row["month"]))
    retained = existing.loc[~replace]
    if retained.empty:
        return incoming.copy().reset_index(drop=True)
    return pd.concat([retained, incoming], ignore_index=True)


def _load_plan(root: Path, run_id: str) -> tuple[dict, Path]:
    directory = _run_directory(root, run_id)
    path = _relative(directory, "catalog_plan.json")
    plan = _json(path)
    _, targets = _targets(root, plan["store_path"])
    if plan.get("schema_version") != 1 or plan.get("run_id") != run_id or set(plan.get("targets", {})) != set(targets):
        raise ValueError("Catalog plan identity or targets are invalid")
    for index, target in enumerate(targets):
        entry = plan["targets"][target]
        expected_stage = f"provenance/runs/{run_id}/catalogs/{index}.parquet"
        if entry.get("staged_path") != expected_stage:
            raise ValueError("Catalog staging path disagrees with run")
        for key in ("before_sha256", "desired_sha256"):
            value = entry.get(key)
            if value is None and key == "before_sha256":
                continue
            if not isinstance(value, str) or not _DIGEST.fullmatch(value):
                raise ValueError("Catalog plan contains an invalid digest")
        stage = _relative(root, entry["staged_path"])
        if _file_hash(stage) != entry["desired_sha256"]:
            raise ValueError("staged Catalog hash mismatch")
    return plan, path


def _install_catalog(root: Path, target: str, entry: dict) -> None:
    path = _relative(root, target)
    stage = _relative(root, entry["staged_path"])
    _relative(root, target + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_path(path) as temporary:
        shutil.copyfile(stage, temporary)
        with temporary.open("rb+") as stream:
            stream.flush()
            os.fsync(stream.fileno())


def publish_run_catalogs(root, intent: dict, receipt: dict) -> dict:
    """Finish one receipt's Catalog updates; no pixel writes or pending cleanup."""
    root = Path(root).resolve(strict=True)
    run_id, store = intent["run_id"], intent["store_path"]
    directory = _run_directory(root, run_id)
    tile, targets = _targets(root, store)
    if receipt.get("run_id") != run_id or receipt.get("store_path") != store:
        raise ValueError("source receipt does not match the Catalog operation")
    before = intent.get("catalog_before")
    if not isinstance(before, dict) or set(before) != set(targets):
        raise ValueError("intent lacks the exact tile/global Catalog baseline")
    for value in before.values():
        if value is not None and (not isinstance(value, str) or not _DIGEST.fullmatch(value)):
            raise ValueError("invalid Catalog baseline digest")
    intent_hash = _digest(canonical_json(intent))
    receipt_hash = _digest(canonical_json(receipt))
    plan_path = _relative(root, f"provenance/runs/{run_id}/catalog_plan.json")
    completed_path = _relative(root, f"provenance/runs/{run_id}/catalog_receipt.json")
    if not plan_path.exists():
        if completed_path.exists():
            raise ValueError("permanent Catalog receipt is missing its plan")
        incoming = _incoming(root, intent, receipt, tile)
        if snapshot_catalogs(root, store) != before:
            raise ValueError("Catalog baseline changed before publication")
        directory.mkdir(parents=True, exist_ok=True)
        entries = {}
        for index, target in enumerate(targets):
            frame = incoming.copy()
            if target == "catalog.parquet":
                rebase_catalog_paths(frame, tile)
            existing = _read_catalog(root, target, tile if target != "catalog.parquet" else None)
            merged = _merge(existing, frame)
            stage_name = f"provenance/runs/{run_id}/catalogs/{index}.parquet"
            stage = _relative(root, stage_name)
            stage.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_parquet(merged, stage)
            with stage.open("rb+") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            _read_catalog(root, stage_name, tile if target != "catalog.parquet" else None)
            entries[target] = {"before_sha256": before[target], "desired_sha256": _file_hash(stage),
                               "staged_path": stage_name}
        _atomic_json(plan_path, {"schema_version": 1, "run_id": run_id, "store_path": store,
                                "intent_canonical_sha256": intent_hash,
                                "source_receipt_canonical_sha256": receipt_hash, "targets": entries})
    plan, plan_path = _load_plan(root, run_id)
    if (plan["store_path"] != store or plan.get("intent_canonical_sha256") != intent_hash
            or plan.get("source_receipt_canonical_sha256") != receipt_hash
            or {key: value["before_sha256"] for key, value in plan["targets"].items()} != before):
        raise ValueError("Catalog plan conflicts with the immutable intent or source receipt")
    if completed_path.exists():
        return verify_run_catalogs(root, run_id)
    # Check BOTH targets before changing either one. A third state is a conflict,
    # not permission to overwrite edits made after this operation began.
    actual = {target: _file_hash(_relative(root, target)) for target in targets}
    for target in targets:
        entry = plan["targets"][target]
        if actual[target] not in {entry["before_sha256"], entry["desired_sha256"]}:
            raise ValueError("Catalog is in neither its before nor desired state: " + target)
    for target in targets:
        entry = plan["targets"][target]
        if actual[target] != entry["desired_sha256"]:
            _install_catalog(root, target, entry)
    result = {"schema_version": 1, "run_id": run_id, "store_path": store,
              "catalog_plan_sha256": _digest(plan_path.read_bytes()),
              "source_receipt_canonical_sha256": receipt_hash,
              "catalog_hashes": {target: entry["desired_sha256"] for target, entry in plan["targets"].items()},
              "scope": "tile/global Catalog metadata only; no pixel changes"}
    for target, desired in result["catalog_hashes"].items():
        if _file_hash(_relative(root, target)) != desired:
            raise ValueError("Catalog publication did not retain its desired bytes")
    _atomic_json(completed_path, result)
    return verify_run_catalogs(root, run_id)


def verify_run_catalogs(root, run_id: str) -> dict:
    """Verify permanent artifacts and both current Catalogs for this operation.

    A later accepted operation legitimately makes this older Catalog receipt
    stale. Historical release evidence can still retain that receipt.
    """
    root = Path(root).resolve(strict=True)
    plan, plan_path = _load_plan(root, run_id)
    receipt = _json(_relative(root, f"provenance/runs/{run_id}/catalog_receipt.json"))
    expected = {target: entry["desired_sha256"] for target, entry in plan["targets"].items()}
    if (receipt.get("schema_version") != 1 or receipt.get("run_id") != run_id
            or receipt.get("store_path") != plan["store_path"]
            or receipt.get("catalog_plan_sha256") != _digest(plan_path.read_bytes())
            or receipt.get("source_receipt_canonical_sha256") != plan["source_receipt_canonical_sha256"]
            or receipt.get("catalog_hashes") != expected):
        raise ValueError("permanent Catalog receipt does not match its plan")
    tile, _ = _targets(root, plan["store_path"])
    for target, digest in expected.items():
        if _file_hash(_relative(root, target)) != digest:
            raise ValueError("published Catalog changed since this operation: " + target)
        _read_catalog(root, target, tile if target != "catalog.parquet" else None)
    return receipt
