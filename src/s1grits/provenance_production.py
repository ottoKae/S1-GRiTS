"""Single-month production journal for the explicit tracked S1 writer.

Callers must hold the existing dataset output_lock for this object's lifetime.
A missing receipt after writing is ambiguous; recovery never runs the writer.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.metadata
from pathlib import Path
import re
import sys
from uuid import uuid4

from .provenance import (
    _atomic_json, _current, _digest, _json, _relative, _verify_release,
    _publish_records_locked,
)
from .provenance_contract import (
    canonical_json, data_revision, read_tables, validate_numeric_processing, validate_records,
)
from .provenance_catalog import snapshot_catalogs
from .provenance_inventory import collect_inventory, _collect_inventory_for_run, _collect_inventory


def _now():
    return datetime.now(timezone.utc).isoformat()


def _empty():
    return dict(outputs=[], sources=[], source_links=[], variables=[],
                limitations=[], witnesses={}, evidence={})


def _optional_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _numeric_processing_required(request):
    if "numeric_processing_version" not in request:
        return False
    version = request["numeric_processing_version"]
    if type(version) is not int or version != 1:
        raise ValueError("unsupported numeric_processing_version")
    return True


def _check_numeric_processing(request, payload, *, present=False):
    if _numeric_processing_required(request) and payload is None:
        raise ValueError("numeric_processing is required by this production request")
    if payload is not None or present:
        validate_numeric_processing(payload)


def _check_numeric_output(payload, observed, output_id):
    if payload is None:
        return
    targets = [row for row in observed["outputs"] if row["output_id"] == output_id]
    if len(targets) != 1 or set(targets[0]["variable_scope"]) != set(payload["output"]):
        raise ValueError("numeric output variables do not match the observed monthly output")
    target = targets[0]
    variables = {row["variable"]: row for row in observed["variables"]
                 if row["store_path"] == target["store_path"] and row["zarr_group"] == target["zarr_group"]}
    for name, info in payload["output"].items():
        actual = variables.get(name)
        if (not actual or actual["dtype"] != info["dtype"] or len(actual["shape"]) != 3
                or not 0 <= target["time_index"] < actual["shape"][0]):
            raise ValueError("numeric output dtype or time index disagrees with observed arrays")


def _monthly_target(intent, observed):
    """Bind a write to its intended month and preserve unrelated output identities."""
    target = [row for row in observed["outputs"]
              if row["store_path"] == intent["store_path"]
              and (row.get("period_start") or "")[:7] == intent["month"]]
    if len(target) != 1:
        raise ValueError("written month is absent or ambiguous")
    if intent["operation"] == "replace":
        old_target = next(row for row in intent["before_inventory"]["outputs"]
                          if row["store_path"] == intent["store_path"]
                          and (row.get("period_start") or "")[:7] == intent["month"])
        if target[0]["output_id"] != old_target["output_id"]:
            raise ValueError("replacement changed the logical output identity")
    before_other = {row["output_id"]: row for row in intent["before_inventory"]["outputs"]
                    if not (row["store_path"] == intent["store_path"]
                            and (row.get("period_start") or "")[:7] == intent["month"])}
    after_other = {row["output_id"]: row for row in observed["outputs"]
                   if row["output_id"] != target[0]["output_id"]}
    if before_other.keys() != after_other.keys():
        raise ValueError("unrelated output identities changed during monthly write")
    for key, old in before_other.items():
        if {k: v for k, v in old.items() if k != "time_index"} != {
                k: v for k, v in after_other[key].items() if k != "time_index"}:
            raise ValueError("unrelated output mapping changed beyond index movement")
    return target[0]


def _producer():
    folder = Path(__file__).parent
    names = ["provenance_production.py", "provenance_monthly.py",
             "scenes/smonthly_writer.py", "scenes/blocks.py", "scenes/store.py",
             "scenes/mosaic.py", "scenes/pipeline.py", "asf_output_writing.py",
             "asf_io.py", "provenance_workflow.py", "provenance_catalog.py",
             "workflow_scenes.py", "resampling.py", "canonical_catalog_schema.py",
             "provenance.py", "provenance_inventory.py", "provenance_contract.py",
             "zarr_cf.py", "zarr_encoding.py", "product_instance.py"]
    return {
        "scope": "S1 paired loader, source mapping and monthly writer; not proof of upstream calibration",
        "python": sys.version.split()[0],
        "versions": {name: importlib.metadata.version(name)
                     for name in ("numpy", "pandas", "zarr", "rasterio", "pyarrow", "pyproj", "numcodecs")},
        "optional_bottleneck_version": _optional_version("bottleneck"),
        "code_sha256": {name: _digest((folder / name).read_bytes())
                        for name in names if (folder / name).is_file()},
    }


class ProductionRun:
    """One explicit month mutation under the caller's dataset-wide writer lock."""

    def __init__(self, root, dataset_id: str, run_id: str, request: dict):
        self.root = Path(root).resolve(strict=True)
        for value in (dataset_id, run_id):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
                raise ValueError("dataset_id and run_id must be safe public identifiers")
        self.dataset_id, self.run_id = dataset_id, run_id
        _numeric_processing_required(request)
        self.request = request
        self.managed_catalogs = request.get("catalog_mode") == "tile_global_v1"
        self.request_hash = _digest(canonical_json(request))
        self.pending_path = _relative(self.root, f"provenance/pending/{run_id}.json")
        self.intent_path = _relative(self.root, f"provenance/runs/{run_id}/intent.json")
        self.receipt_path = _relative(self.root, f"provenance/runs/{run_id}/receipt.json")
        self.completed_path = _relative(self.root, f"provenance/runs/{run_id}/completed.json")
        self._check_other_pending()
        if self.intent_path.exists():
            intent = _json(self.intent_path)
            legacy_request = intent["request"]
            if ("numeric_processing_version" not in legacy_request and self.receipt_path.exists()
                    and _numeric_processing_required(request)
                    and canonical_json({k: v for k, v in request.items() if k != "numeric_processing_version"})
                    == canonical_json(legacy_request)):
                # A durable legacy receipt can be recovered, never retroactively enriched.
                self.request = legacy_request
                self.request_hash = _digest(canonical_json(legacy_request))
            if intent["request_sha256"] != self.request_hash or intent["dataset_id"] != dataset_id:
                raise ValueError("run_id cannot be reused with changed input or parameters")
        elif self.receipt_path.exists() or self.completed_path.exists() or self.pending_path.exists():
            raise ValueError("run evidence is missing its immutable intent")
        self.completed = self.receipt_path.exists() or self.completed_path.exists()

    def _check_other_pending(self):
        directory = _relative(self.root, "provenance/pending")
        for path in directory.glob("*.json"):
            if path != self.pending_path:
                raise ValueError("another pending production run blocks this dataset")
            if _json(path).get("run_id") != self.run_id:
                raise ValueError("pending identity mismatch")

    def _pending(self):
        self._check_other_pending()
        if not self.pending_path.exists():
            raise ValueError("missing pending production intent")
        pending = _json(self.pending_path)
        if pending.get("intent_sha256") != _digest(self.intent_path.read_bytes()):
            raise ValueError("immutable intent hash mismatch")
        return pending

    def _observe(self, paths):
        paths = [p for p in paths if _relative(self.root, p).is_dir()]
        if not paths:
            self._check_other_pending()
            return _empty()
        if self.pending_path.exists():
            return _collect_inventory_for_run(self.root, store_paths=paths, run_id=self.run_id,
                                              include_catalogs=not self.managed_catalogs)
        if self.managed_catalogs:
            return _collect_inventory(self.root, store_paths=paths, include_catalogs=False)
        return collect_inventory(self.root, store_paths=paths)

    def _previous(self, pointer):
        if not pointer:
            return _empty()
        directory = _relative(self.root, "provenance/releases/" + pointer["metadata_release_id"])
        dataset = _verify_release(directory, pointer)
        if dataset["dataset_id"] != self.dataset_id:
            raise ValueError("dataset_id differs from existing published provenance")
        outputs, sources, links = read_tables(directory)
        evidence = {}
        # shortcut: copy prior evidence per release; bound batches until deduplicated publication is validated.
        for path in (directory / "evidence").rglob("*.json"):
            name = path.relative_to(directory / "evidence").as_posix()
            _relative(directory, "evidence/" + name)
            evidence[name] = _json(path)
        return dict(outputs=outputs, sources=sources, source_links=links,
                    variables=_json(directory / "variables.json"),
                    limitations=_json(directory / "limitations.json"),
                    evidence=evidence,
                    witnesses=_json(directory / "evidence/data_witnesses.json"))

    def _receipt_witnesses(self, previous):
        receipts = {}
        for key, expected in previous["witnesses"].items():
            if key.startswith("provenance/runs/") and key.endswith("/receipt.json"):
                path = _relative(self.root, key)
                if not path.is_file() or _digest(path.read_bytes()) != expected:
                    raise ValueError("permanent receipt evidence changed since publication")
                receipts[key] = expected
        return receipts

    @staticmethod
    def _carry_history(inventory, previous):
        previous_rows = {row["output_id"]: row for row in previous["outputs"]}
        fields = ("generation_id", "run_id", "processing_id", "evidence_status",
                  "evidence_ref", "unknown_reason")
        for row in inventory["outputs"]:
            old = previous_rows.get(row["output_id"])
            if old:
                row.update({key: old.get(key) for key in fields})
        return inventory


    @staticmethod
    def _carry_annotations(inventory, previous):
        """Keep explanatory annotations on surviving output generations only."""
        old_rows = {row["output_id"]: row for row in previous["outputs"]}
        surviving = {
            row["output_id"]: row["generation_id"] for row in inventory["outputs"]
            if row["output_id"] in old_rows
            and row["generation_id"] == old_rows[row["output_id"]]["generation_id"]
        }
        by_store, by_group = {}, {}
        for row in previous["outputs"]:
            identity = {row["output_id"]: row["generation_id"]}
            by_store.setdefault(row["store_path"], {}).update(identity)
            by_group.setdefault((row["store_path"], row["zarr_group"]), {}).update(identity)

        def retained(annotation, key, default):
            scope = annotation.get(key, default)
            if (not isinstance(scope, dict) or any(
                    not isinstance(identifier, str) or (generation is not None and not isinstance(generation, str))
                    for identifier, generation in scope.items())):
                raise ValueError("invalid historical annotation output generations")
            return {identifier: generation for identifier, generation in scope.items()
                    if identifier in surviving and surviving[identifier] == generation}

        for name, old in previous["evidence"].items():
            if not name.startswith("stores/") or "historical_context" not in old:
                continue
            observed = inventory["evidence"].get(name)
            if observed is None:
                continue
            context = old["historical_context"]
            scope = retained(context, "output_generations", by_store.get(old["store_path"], {}))
            if scope:
                observed["historical_context"] = {**context, "output_generations": scope}
            else:
                observed.pop("historical_context", None)

        for table, fields in (
            ("variables", ("units", "historical_processing_ref", "historical_processing_basis")),
            ("limitations", ("historical_processing_ref",)),
        ):
            def key(record):
                return (record.get("store_path"), record.get("zarr_group"), record.get("variable"))
            old_records = {key(record): record for record in previous[table]}
            for observed in inventory[table]:
                old = old_records.get(key(observed), {})
                if "historical_processing_ref" not in old:
                    continue
                default = (by_group.get((observed["store_path"], observed["zarr_group"]), {})
                           if table == "variables" else by_store.get(observed["store_path"], {}))
                scope = retained(old, "historical_output_generations", default)
                if scope:
                    observed.update({field: old[field] for field in fields if field in old})
                    observed["historical_output_generations"] = scope

    def prepare(self, store_path: str, month: str, parameters: dict, sources: list[dict]):
        """Persist intent before store initialization, attrs, delete or append."""
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
            raise ValueError("month must be YYYY-MM")
        _relative(self.root, store_path)
        validate_records([], sources, [])
        canonical_json(parameters)
        if self.intent_path.exists():
            intent = _json(self.intent_path)
            if (intent["store_path"], intent["month"], intent["parameters"], intent["sources"]) != (
                    store_path, month, parameters, sources):
                raise ValueError("prepared intent cannot change")
            pending = self._pending()
            if pending["phase"] != "prepared":
                raise ValueError("writing without a durable receipt is ambiguous; recovery required")
            if _producer() != intent["producer"]:
                raise ValueError("prepared producer code or dependencies changed; do not retry automatically")
            if _current(self.root) != intent["previous_pointer"]:
                raise ValueError("metadata base changed after preparation")
            self._receipt_witnesses(self._previous(intent["previous_pointer"]))
            observed = self._observe(intent["store_paths"])
            if observed != intent["before_inventory"]:
                raise ValueError("prepared source state changed; do not retry automatically")
            self.intent = intent
            return
        previous_pointer = _current(self.root)
        previous = self._previous(previous_pointer)
        prior_sources = {row["source_id"]: row for row in previous["sources"]}
        for source in sources:
            old = prior_sources.get(source["source_id"])
            if old and {k: v for k, v in old.items() if k != "evidence_ref"} != {
                    k: v for k, v in source.items() if k != "evidence_ref"}:
                raise ValueError("source identity reused with different declarations")
        paths = sorted({row["store_path"] for row in previous["outputs"]} | {store_path})
        before = self._observe(paths)
        if previous_pointer:
            bound = self._carry_history(self._observe(paths), previous)
            witnesses = {**bound["witnesses"], **self._receipt_witnesses(previous)}
            # Historical snapshots seal the Catalog; verify it before adopting managed publication.
            if self.managed_catalogs and "catalog.parquet" in previous["witnesses"]:
                witnesses["catalog.parquet"] = _digest(_relative(self.root, "catalog.parquet").read_bytes())
            if data_revision(bound["outputs"], witnesses) != previous_pointer["data_revision"]:
                raise ValueError("published source revision is stale; cannot start a production run")
        target = [row for row in before["outputs"]
                  if row["store_path"] == store_path and (row.get("period_start") or "")[:7] == month]
        if len(target) > 1 or any(row["record_kind"] != "monthly" for row in before["outputs"]
                                if row["store_path"] == store_path):
            raise ValueError("tracked API requires one monthly root group")
        previous_target = next((row for row in previous["outputs"]
                                if target and row["output_id"] == target[0]["output_id"]), None)
        self.intent = {
            "schema_version": 1, "dataset_id": self.dataset_id, "run_id": self.run_id,
            "request_sha256": self.request_hash, "request": self.request,
            "store_path": store_path, "store_paths": paths, "month": month,
            "operation": "replace" if target else "append",
            "generation_id": "gen-" + uuid4().hex,
            "previous_generation_id": previous_target["generation_id"] if previous_target else None,
            "previous_pointer": previous_pointer, "before_inventory": before,
            "parameters": parameters, "sources": sources, "producer": _producer(),
            "processing_id": "processing-" + _digest(canonical_json(parameters)),
            "created_at": _now(),
            "source_binding_evidence": self.request.get("source_binding_evidence",
                "caller-declared mapping to actual supplied input arrays"),
            "affected_scope": "entire store/group including shifted indices",
        }
        if self.managed_catalogs:
            self.intent["catalog_before"] = snapshot_catalogs(self.root, store_path)
        self.intent_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(self.intent_path, self.intent)
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(self.pending_path, {
            "run_id": self.run_id, "phase": "prepared",
            "intent_sha256": _digest(self.intent_path.read_bytes()),
            "store_path": store_path, "month": month,
        })

    def begin_write(self, selected_source_ids: list[str]):
        pending = self._pending()
        if pending["phase"] != "prepared" or self.receipt_path.exists():
            raise ValueError("writing may start only from a prepared intent")
        intent = _json(self.intent_path)
        if self.managed_catalogs and snapshot_catalogs(self.root, intent["store_path"]) != intent["catalog_before"]:
            raise ValueError("Catalog baseline changed before data write")
        selected = sorted(set(selected_source_ids))
        if not selected or not set(selected) <= {s["source_id"] for s in intent["sources"]}:
            raise ValueError("selected sources must reference declared input assets")
        pending.update(phase="writing", selected_source_ids=selected, started_at=_now())
        _atomic_json(self.pending_path, pending)

    def commit(self, used_source_ids: list[str], catalog_records: list[dict] | None = None, *,
               numeric_processing: dict | None = None):
        pending = self._pending()
        if pending["phase"] != "writing" or self.receipt_path.exists():
            raise ValueError("commit requires this run's writing state")
        intent = _json(self.intent_path)
        _check_numeric_processing(intent["request"], numeric_processing)
        used = sorted(set(used_source_ids))
        if not used or not set(used) <= set(pending["selected_source_ids"]):
            raise ValueError("used source set must be a nonempty subset of selected inputs")
        observed = self._observe(intent["store_paths"])
        target = _monthly_target(intent, observed)
        _check_numeric_output(numeric_processing, observed, target["output_id"])
        receipt = {
            "schema_version": 1, "run_id": self.run_id, "dataset_id": self.dataset_id,
            "intent_sha256": _digest(self.intent_path.read_bytes()),
            "output_id": target["output_id"], "generation_id": intent["generation_id"],
            "previous_generation_id": intent["previous_generation_id"],
            "operation": intent["operation"], "month": intent["month"],
            "store_path": intent["store_path"], "processing_id": intent["processing_id"],
            "selected_source_ids": pending["selected_source_ids"], "used_source_ids": used,
            "committed_at": _now(), "observed": observed,
            "verification": "native writer returned; complete array/time lengths and structural seal",
            "pixel_checksum": None,
            "limitation": "receipt is not a full pixel checksum or proof of upstream processing",
        }
        if numeric_processing is not None:
            receipt["numeric_processing"] = numeric_processing
        if self.managed_catalogs:
            if not catalog_records or len(catalog_records) != 1:
                raise ValueError("workflow commit requires its canonical Catalog row")
            receipt["catalog_records"] = catalog_records
        _atomic_json(self.receipt_path, receipt)
        pending.update(phase="committed_provenance_pending",
                       receipt_sha256=_digest(self.receipt_path.read_bytes()))
        _atomic_json(self.pending_path, pending)
        self.completed = True

    def _checked_receipt(self):
        if not self.receipt_path.exists():
            raise ValueError("writing without a durable receipt is ambiguous; no automatic rewrite")
        receipt = _json(self.receipt_path)
        if receipt.get("run_id") != self.run_id or receipt.get("intent_sha256") != _digest(self.intent_path.read_bytes()):
            raise ValueError("receipt identity or intent digest mismatch")
        intent = _json(self.intent_path)
        if receipt.get("generation_id") != intent["generation_id"]:
            raise ValueError("receipt generation mismatch")
        for key in ("dataset_id", "month", "store_path", "operation", "previous_generation_id", "processing_id"):
            if key not in receipt or receipt[key] != intent[key]:
                raise ValueError("receipt " + key + " differs from immutable intent")
        target = _monthly_target(intent, receipt["observed"])
        if receipt.get("output_id") != target["output_id"]:
            raise ValueError("receipt output differs from intended monthly output")
        declared = {source["source_id"] for source in intent["sources"]}
        for key in ("selected_source_ids", "used_source_ids"):
            values = receipt.get(key)
            if (not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError("receipt source sets must be nonempty unique string lists")
        if not set(receipt["used_source_ids"]) <= set(receipt["selected_source_ids"]) <= declared:
            raise ValueError("receipt used/selected sources differ from declared inputs")
        _check_numeric_processing(intent["request"], receipt.get("numeric_processing"),
                                  present="numeric_processing" in receipt)
        _check_numeric_output(receipt.get("numeric_processing"), receipt["observed"], receipt["output_id"])
        numeric_sources = (receipt.get("numeric_processing") or {}).get("input", {}).get("sources", [])
        if any(source["source_id"] not in declared for source in numeric_sources):
            raise ValueError("receipt numeric sources differ from declared inputs")
        digest = _digest(self.receipt_path.read_bytes())
        if self.pending_path.exists():
            pending = self._pending()
            if (pending.get("phase") not in {"writing", "committed_provenance_pending"}
                    or pending.get("selected_source_ids") != receipt["selected_source_ids"]):
                raise ValueError("receipt selected sources or write phase differ from pending intent")
            if pending.get("receipt_sha256") and pending["receipt_sha256"] != digest:
                raise ValueError("receipt hash mismatch")
        if self.completed_path.exists() and _json(self.completed_path)["receipt_sha256"] != digest:
            raise ValueError("completed receipt hash mismatch")
        return intent, receipt, digest

    def _verify_source(self, intent, receipt):
        observed = self._observe(intent["store_paths"])
        if canonical_json(observed) != canonical_json(receipt["observed"]):
            raise ValueError("committed source structure changed; recovery blocked")
        return observed

    def _check_published_receipt(self, pointer, intent, receipt, digest):
        if not pointer:
            raise ValueError("published current pointer is missing")
        directory = _relative(self.root, "provenance/releases/" + pointer["metadata_release_id"])
        dataset = _verify_release(directory, pointer)
        if dataset.get("dataset_id") != self.dataset_id or dataset.get("production_run_id") != self.run_id:
            raise ValueError("metadata base changed; refusing lost update")
        row = next((r for r in read_tables(directory)[0] if r["output_id"] == receipt["output_id"]), None)
        witnesses = _json(directory / "evidence/data_witnesses.json")
        if (not row or row["generation_id"] != intent["generation_id"]
                or witnesses.get(f"provenance/runs/{self.run_id}/receipt.json") != digest):
            raise ValueError("published generation or receipt binding disagrees")

    def publish(self):
        return self.recover()

    def recover(self):
        """Finish metadata only; never delete, append or invoke a writer."""
        intent, receipt, digest = self._checked_receipt()
        if self.completed_path.exists():
            completed = _json(self.completed_path)
            pointer = completed["pointer"]
            self._check_published_receipt(pointer, intent, receipt, digest)
            if self.pending_path.exists():
                if _current(self.root) != pointer:
                    raise ValueError("current pointer changed before pending cleanup")
                self._verify_source(intent, receipt)
                if self.managed_catalogs:
                    from .provenance_catalog import verify_run_catalogs
                    verify_run_catalogs(self.root, self.run_id)
                self.pending_path.unlink()
            return {**pointer, "unchanged": True, "run_id": self.run_id}
        if not self.pending_path.exists():
            raise ValueError("unpublished receipt has no pending protection")
        observed = self._verify_source(intent, receipt)
        previous = self._previous(intent["previous_pointer"])
        self._receipt_witnesses(previous)
        current = _current(self.root)
        if current != intent["previous_pointer"]:
            self._check_published_receipt(current, intent, receipt, digest)
        # Reject an unrelated metadata base before mutating either Catalog.
        catalog_receipt = None
        if self.managed_catalogs:
            from .provenance_catalog import publish_run_catalogs
            catalog_receipt = publish_run_catalogs(self.root, intent, receipt)
        if current != intent["previous_pointer"]:
            _atomic_json(self.completed_path, {"run_id": self.run_id, "pointer": current, "receipt_sha256": digest})
            self.pending_path.unlink()
            return {**current, "unchanged": True, "run_id": self.run_id}
        inventory = self._carry_history(observed, previous)
        target = next(row for row in inventory["outputs"] if row["output_id"] == receipt["output_id"])
        ref_base = f"evidence/runs/{self.run_id}"
        target.update(generation_id=intent["generation_id"], run_id=self.run_id,
                      processing_id=intent["processing_id"], evidence_status="execution_recorded",
                      evidence_ref=ref_base + "/receipt.json",
                      unknown_reason=("Upstream processing is not independently verified; source IDs are provider declarations."
                                      if self.managed_catalogs else
                                      "Upstream processing and caller-supplied asset binding are not independently verified."))
        kept_links = [row for row in previous["source_links"] if row["output_id"] != target["output_id"]]
        source_map = {row["source_id"]: row for row in previous["sources"]}
        for source in intent["sources"]:
            row = {**source, "evidence_ref": ref_base + "/intent.json"}
            old = source_map.get(row["source_id"])
            if old and {k: v for k, v in old.items() if k != "evidence_ref"} != {
                    k: v for k, v in row.items() if k != "evidence_ref"}:
                raise ValueError("source identity reused with different declarations")
            source_map.setdefault(row["source_id"], row)
            identifier = row["source_id"]
            role = "used" if identifier in receipt["used_source_ids"] else (
                "selected" if identifier in receipt["selected_source_ids"] else "excluded")
            kept_links.append(dict(output_id=target["output_id"], generation_id=intent["generation_id"],
                                   source_id=identifier, role=role, evidence_ref=ref_base + "/receipt.json"))
        referenced = {row["source_id"] for row in kept_links}
        inventory["sources"] = [source_map[key] for key in sorted(referenced)]
        inventory["source_links"] = kept_links
        inventory["evidence"] = {**previous["evidence"], **inventory["evidence"],
                                 f"runs/{self.run_id}/intent.json": intent,
                                 f"runs/{self.run_id}/receipt.json": receipt}
        self._carry_annotations(inventory, previous)
        if catalog_receipt is not None:
            inventory["evidence"][f"runs/{self.run_id}/catalog_receipt.json"] = catalog_receipt
            inventory["evidence"][f"runs/{self.run_id}/catalog_plan.json"] = _json(self.intent_path.parent / "catalog_plan.json")
        active_runs = {row["run_id"] for row in inventory["outputs"] if row["run_id"]}
        for run_id in sorted(active_runs):
            key = f"provenance/runs/{run_id}/receipt.json"
            path = _relative(self.root, key)
            if not path.is_file():
                raise ValueError("current generation is missing permanent receipt evidence")
            inventory["witnesses"][key] = _digest(path.read_bytes())
        inventory["limitations"].append({
            "scope": "one-month tracked writer",
            "upstream_source_binding": ("loader position bound to queried asset identity; upstream processing unverified"
                                        if self.managed_catalogs else
                                        "caller-declared; actual supplied input consumption recorded"),
            "catalog_scope": ("tile/global Catalogs coordinated under root lock" if self.managed_catalogs
                              else "native writer records returned separately; catalogs are not produced by this pilot"),
            "recovery": "receipt authorizes metadata recovery; absent receipt after mutation blocks",
        })
        def verify_before_publish():
            self._verify_source(intent, receipt)
            if self.managed_catalogs:
                from .provenance_catalog import verify_run_catalogs
                verify_run_catalogs(self.root, self.run_id)
        pointer = _publish_records_locked(
            self.root, dataset_id=self.dataset_id, inventory=inventory,
            selection={"store_paths": intent["store_paths"], "start": None, "end": None},
            execution_kind="production",
            extra_dataset={"production_run_id": self.run_id, "production_scope": "s1_monthly_workflow" if self.managed_catalogs else "single_month_writer_pilot"},
            before_publish=verify_before_publish,
        )
        _atomic_json(self.completed_path, {
            "run_id": self.run_id, "pointer": {k: v for k, v in pointer.items() if k != "unchanged"},
            "receipt_sha256": digest,
        })
        self.pending_path.unlink()
        return {**pointer, "run_id": self.run_id}
