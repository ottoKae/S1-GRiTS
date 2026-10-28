"""Opt-in serial S1 monthly workflow records; caller owns the output root lock.

Each month/store publishes an accepted prefix. A batch is not an all-or-nothing
pixel transaction. Catalog metadata is recovered before any further download.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .config_schema import resolve_output_policies
from .provenance import _atomic_json, _current, _digest, _json, _relative
from .provenance_catalog import snapshot_catalogs, verify_run_catalogs
from .provenance_contract import canonical_json, sanitize_asset_url
from .provenance_monthly import _json_value, _write_tracked_monthly_locked
from .provenance_production import ProductionRun


def validate_tracking_config(config):
    tracking = (config.get("metadata") or {}).get("provenance") or {}
    if not isinstance(tracking, dict) or type(tracking.get("enabled", False)) is not bool:
        raise ValueError("metadata.provenance.enabled must be a boolean")
    if not tracking.get("enabled", False):
        return False
    for key in ("dataset_id", "run_id"):
        if not isinstance(tracking.get(key), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tracking[key]):
            raise ValueError("provenance " + key + " must be a safe stable identifier")
    time_config = config.get("time") if "time" in config else config.get("time_range")
    if isinstance(time_config, dict) and ("full" in time_config or time_config.get("mode") == "full"):
        raise ValueError("provenance requires explicit years/months; full archive discovery queries before recovery")
    proc = config.get("processing") or {}
    monthly = proc.get("monthly") or {}
    memory = config.get("memory") or {}
    output = config.get("output") or {}
    if config.get("parallel", {}).get("enabled", False):
        raise ValueError("provenance currently requires serial tile processing")
    if not monthly.get("enabled") or not monthly.get("only"):
        raise ValueError("provenance requires processing.monthly.enabled and only=true")
    if monthly.get("blockwise_threads", 1) != 1:
        raise ValueError("provenance requires monthly.blockwise_threads=1")
    if any(proc.get(key, False) for key in ("spatial_despeckle", "features_ratio", "features_rvi", "features_glcm")):
        raise ValueError("provenance currently supports ARDC VV/VH without derived features")
    if config.get("static_layers", {}).get("enabled", False) or config.get("static_layers", {}).get("run_after_scenes", False):
        raise ValueError("provenance currently excludes static post-processing")
    if resolve_output_policies(config).rebuild_on_mismatch:
        raise ValueError("provenance cannot rebuild an existing store")
    if any(output.get("formats", {}).get(key, False) or monthly.get("generate_" + key, False)
           for key in ("cog", "preview")):
        raise ValueError("provenance currently excludes COG and preview exports")
    if any(memory.get(key, False) for key in ("download_prefetch", "windowed_burst_reads", "batch_spill")):
        raise ValueError("provenance currently excludes prefetch, lazy reads and batch spill")
    if memory.get("batch_strategy") != "monthly":
        raise ValueError("provenance requires memory.batch_strategy=monthly to avoid partial-month writes")
    if config.get("roi", {}).get("polarization", "VV+VH") != "VV+VH":
        raise ValueError("provenance currently requires VV+VH")
    if config.get("roi", {}).get("flight_direction") not in {"ASCENDING", "DESCENDING"}:
        raise ValueError("provenance requires one explicit flight_direction")
    return True


def _row_sources(row):
    result = []
    for band, column in (("VV", "url_copol"), ("VH", "url_crosspol")):
        value = row.get(column)
        href = sanitize_asset_url(str(value)) if isinstance(value, str) and value else None
        product = str(row["opera_id"]) if pd.notna(row.get("opera_id")) else None
        identity = {"collection": "OPERA_L2_RTC-S1", "product_id": product,
                    "asset_key": band, "href": href}
        result.append({"source_id": "source-" + _digest(canonical_json(identity)), **identity,
                       "source_sha256": None, "declared_scale": None, "declared_offset": None,
                       "evidence_status": "source_declared", "evidence_ref": None})
    return result


class WorkflowTracking:
    def __init__(self, root, config, tiles):
        self.root = Path(root).resolve(strict=True)
        self.config = config
        self.options = config["metadata"]["provenance"]
        self.dataset_id, self.batch_id = self.options["dataset_id"], self.options["run_id"]
        self.tiles = list(tiles)
        self.completed = set()
        self.active_tile = self.active_direction = None
        # Only experiment inputs enter the public recipe; credentials and local paths do not.
        self.recipe = {key: config.get(key) for key in ("roi", "time", "time_range", "processing")}
        self.recipe["output_policy"] = {key: (config.get("output") or {}).get(key)
                                         for key in ("existing_store", "existing_month", "overwrite", "on_time_conflict")}
        self.recipe["query"] = config.get("query")
        self.recipe["memory_sha256"] = _digest(canonical_json(_json_value(config.get("memory"))))
        self.recipe["metadata_products_sha256"] = _digest(canonical_json(_json_value(
            {key: config.get("metadata", {}).get(key) for key in ("products", "product_config")})))
        self.recipe_hash = _digest(canonical_json(_json_value(self.recipe)))
        self.path = _relative(self.root, f"provenance/batches/{self.batch_id}.json")
        if self.path.exists():
            self.state = _json(self.path)
            if (self.state["dataset_id"], self.state["recipe_sha256"], self.state["requested_tiles"]) != (
                    self.dataset_id, self.recipe_hash, self.tiles):
                raise ValueError("workflow run_id cannot be reused with changed configuration")
        else:
            self.state = {"schema_version": 1, "dataset_id": self.dataset_id,
                          "run_id": self.batch_id, "recipe_sha256": self.recipe_hash,
                          "recipe": _json_value(self.recipe), "requested_tiles": self.tiles,
                          "plans": {}, "skipped": {}, "results": None}
        pending = list(_relative(self.root, "provenance/pending").glob("*.json"))
        if len(pending) > 1:
            raise ValueError("multiple pending operations require explicit recovery")
        for path in pending:
            run_id = path.stem
            intent = _json(_relative(self.root, f"provenance/runs/{run_id}/intent.json"))
            context = intent["request"]
            if (context.get("workflow_batch_id"), context.get("workflow_recipe_sha256"), intent["dataset_id"]) != (
                    self.batch_id, self.recipe_hash, self.dataset_id):
                raise ValueError("pending operation belongs to a different workflow run")
            ProductionRun(self.root, self.dataset_id, run_id, context).recover()
        # Refuse corrupt Catalogs before metadata queries or downloads.
        for tile in self.tiles:
            snapshot_catalogs(self.root, f"{tile}/smonthly-placeholder.zarr")
        for plan in self.state["plans"].values():
            for run_id in plan["operations"]:
                self._is_completed(run_id)
        self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(self.path, self.state)

    def _operation(self, tile, direction, token, month):
        token = str(token).replace("_", "-")
        store = f"{tile}/smonthly_{direction}/zarr/s1grits_smonthly_{tile}_{direction}_TK{token}.zarr"
        identity = [self.dataset_id, self.batch_id, store, month]
        return "op-" + _digest(canonical_json(identity))[:40], store

    def _is_completed(self, run_id):
        if run_id in self.completed:
            return True
        folder = _relative(self.root, "provenance/runs/" + run_id)
        if not (folder / "completed.json").exists():
            return False
        intent = _json(folder / "intent.json")
        request = intent["request"]
        if request.get("workflow_batch_id") != self.batch_id or request.get("workflow_recipe_sha256") != self.recipe_hash:
            raise ValueError("completed operation has a conflicting workflow identity")
        ProductionRun(self.root, self.dataset_id, run_id, request).recover()
        self.completed.add(run_id)
        return True

    def register_candidates(self, tile, direction, frame):
        self.active_tile, self.active_direction = tile, direction
        operations, declarations = {}, []
        if not frame.empty:
            needed = {"track_token", "acq_dt", "_source_row", "url_copol", "url_crosspol"}
            if not needed <= set(frame.columns) or not frame["_source_row"].is_unique:
                raise ValueError("candidate rows require explicit unique source-row identities")
            for _, row in frame.iterrows():
                month = pd.Timestamp(row["acq_dt"]).strftime("%Y-%m")
                run_id, store = self._operation(tile, direction, row["track_token"], month)
                operations[run_id] = {"store_path": store, "month": month}
                declarations.append({"operation": run_id, "source_row": int(row["_source_row"]),
                                     "time": pd.Timestamp(row["acq_dt"]).isoformat(),
                                     "grouping": {key: _json_value(row.get(key)) for key in
                                         ("track_number", "jpl_burst_id", "pass_id", "acq_group_id_within_mgrs_tile")},
                                     "sources": _row_sources(row)})
        plan = {"direction": direction, "operations": operations,
                "source_declarations_sha256": _digest(canonical_json(declarations))}
        old = self.state["plans"].get(tile)
        if old is not None and old != plan:
            raise ValueError("queried source membership changed for the same workflow run")
        self.state["plans"][tile] = plan
        self._save()

    def filter_download_rows(self, frame):
        keep = []
        for _, row in frame.iterrows():
            op, _ = self._operation(self.active_tile, self.active_direction, row["track_token"],
                                    pd.Timestamp(row["acq_dt"]).strftime("%Y-%m"))
            keep.append(not self._is_completed(op) and op not in self.state["skipped"])
        return frame.loc[keep].copy()

    def write_monthly(self, *, writer_kwargs, candidate_frame, clean_source_rows):
        if len(clean_source_rows) != len(writer_kwargs["clean_dates"]) or len(set(clean_source_rows)) != len(clean_source_rows):
            raise ValueError("successful input arrays require unique explicit source rows")
        candidate = candidate_frame.copy()
        if not candidate["_source_row"].is_unique:
            raise ValueError("candidate source rows are not unique")
        by_row = candidate.set_index("_source_row", drop=False)
        if not set(clean_source_rows) <= set(by_row.index):
            raise ValueError("loader returned an unknown source row")
        candidate["_month"] = pd.to_datetime(candidate["acq_dt"], utc=True).dt.strftime("%Y-%m")
        records = []
        valid = writer_kwargs.get("valid_clean_indices")
        if valid is None:
            valid = set(range(len(clean_source_rows)))
        for (token, month), rows in candidate.groupby(["track_token", "_month"], sort=True):
            tile, direction = writer_kwargs["mgrs_tile_id"], writer_kwargs["direction_label"]
            run_id, _ = self._operation(tile, direction, token, month)
            if self._is_completed(run_id):
                continue
            row_set = set(rows["_source_row"])
            indices = [i for i, row in enumerate(clean_source_rows) if row in row_set]
            valid_local = {j for j, i in enumerate(indices) if i in valid}
            if not valid_local:
                self.state["skipped"][run_id] = "no usable QC-passing paired input; no output claimed"
                self._save()
                continue
            kwargs = dict(writer_kwargs)
            for key in ("final_vv", "prof_vv", "final_vh", "prof_vh", "clean_dates"):
                kwargs[key] = [writer_kwargs[key][i] for i in indices]
            source_rows = [clean_source_rows[i] for i in indices]
            kwargs["df_batch"] = by_row.loc[source_rows].drop(columns=["_month"], errors="ignore").reset_index(drop=True)
            kwargs["clean_source_rows"] = source_rows
            kwargs["valid_clean_indices"] = valid_local
            kwargs["track_token"] = str(token).replace("_", "-")
            kwargs["n_bursts_track"] = int(rows["jpl_burst_id"].nunique())
            kwargs["restrict_to_group"] = True
            kwargs["monthly_cfg"] = {key: value for key, value in (kwargs.get("monthly_cfg") or {}).items()
                                      if key not in {"enabled", "only"}}
            sources, bindings_by_row = {}, {}
            for _, row in rows.iterrows():
                assets = _row_sources(row)
                bindings_by_row[int(row["_source_row"])] = {asset["asset_key"]: asset["source_id"] for asset in assets}
                for asset in assets:
                    sources[asset["source_id"]] = asset
            context = {"catalog_mode": "tile_global_v1", "workflow_batch_id": self.batch_id,
                       "workflow_recipe_sha256": self.recipe_hash,
                       "workflow_recipe": _json_value(self.recipe),
                       "source_binding_evidence": "paired_loader_input_position_and_candidate_source_row",
                       "catalog_scope": "tile_and_global_coordinated",
                       "source_stage": {"candidate_rows": len(rows), "paired_profile_valid_rows": len(indices),
                                        "qc_passing_rows": len(valid_local)},
                       "upstream_calibration": "unverified provider declaration"}
            result = _write_tracked_monthly_locked(
                self.root, self.dataset_id, run_id, writer_kwargs=kwargs,
                sources=list(sources.values()), source_indices=[bindings_by_row[row] for row in source_rows],
                request_context=context,
            )
            if result.get("status") == "skipped":
                self.state["skipped"][run_id] = "existing month skipped by output policy; historical generation unchanged"
            else:
                self.completed.add(run_id)
                records.extend(result.get("catalog_records", []))
            self._save()
        return records

    def _validate_current(self):
        pointer = _current(self.root)
        if not pointer:
            return
        dataset = _json(_relative(self.root, "provenance/releases/" + pointer["metadata_release_id"] + "/dataset.json"))
        run_id = dataset.get("production_run_id")
        if run_id:
            intent = _json(_relative(self.root, f"provenance/runs/{run_id}/intent.json"))
            run = ProductionRun(self.root, self.dataset_id, run_id, intent["request"])
            _, receipt, _ = run._checked_receipt()
            run.recover()
            run._verify_source(intent, receipt)
            if run.managed_catalogs:
                verify_run_catalogs(self.root, run_id)

    def finished_results(self):
        if set(self.state["plans"]) != set(self.tiles):
            return None
        if any(not self._is_completed(op) and op not in self.state["skipped"]
               for plan in self.state["plans"].values() for op in plan["operations"]):
            return None
        self._validate_current()
        if self.state["results"] is None:
            self.state["results"] = {tile: {"status": "success", "error": None,
                "tile_dir": str(self.root / tile), "catalog_path": str(self.root / tile / "catalog.parquet"),
                "written_scenes": 0, "recovered": True} for tile in self.tiles}
            self._save()
        return self.state["results"]

    def finish(self, results):
        if any(row.get("status") != "success" for row in results.values()):
            raise ValueError("failed workflow cannot be published as a complete batch")
        for plan in self.state["plans"].values():
            for op in plan["operations"]:
                if not self._is_completed(op) and op not in self.state["skipped"]:
                    if _relative(self.root, f"provenance/runs/{op}/intent.json").exists():
                        raise ValueError("unresolved prepared/writing operation blocks batch completion")
                    self.state["skipped"][op] = "no output produced after input screening; no negative or success label inferred"
        self.state["results"] = results
        self._save()
        self._validate_current()
        return results


def start_tracking_locked(output_root, config, tiles):
    if not validate_tracking_config(config):
        return None
    return WorkflowTracking(output_root, config, tiles)
