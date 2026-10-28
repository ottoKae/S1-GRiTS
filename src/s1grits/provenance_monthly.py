"""Restricted one-month native writer with durable companion provenance.

This pilot API owns an isolated store and sidecar only. It returns native Catalog
records but does not update Catalogs or download inputs. Array-to-asset bindings
are caller declarations; only consumption at the composite stack is observed.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pandas as pd
import pyproj
import zarr
from rasterio.transform import Affine

from .file_lock import output_lock
from .provenance import _relative
from .provenance_contract import (
    canonical_json, validate_numeric_processing, validate_numeric_sources, validate_records,
)
from .provenance_production import ProductionRun
from .resampling import rasterio_resampling, validate_target_resolution
from .scenes.smonthly_writer import _monthly_output_months, _write_smonthly_one_track


def _json_value(value):
    """Canonicalize inputs for hashing; arbitrary paths stay inside the digest."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else {"nonfinite": str(value)}
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("input metadata keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    # ASF returns GeoDataFrames; bind geometry values without a mutable repr.
    from shapely.geometry.base import BaseGeometry
    if isinstance(value, BaseGeometry):
        return {"geometry_wkb": value.wkb_hex}
    if hasattr(value, "to_wkt"):
        return value.to_wkt()
    raise ValueError("unsupported input metadata type: " + type(value).__name__)


def _array_identity(value) -> dict | None:
    if value is None:
        return None
    array = np.asarray(value)
    if array.dtype.kind not in "fiu":
        raise ValueError("numeric arrays are required")
    digest = hashlib.sha256()
    if array.flags.c_contiguous:
        digest.update(memoryview(array).cast("B"))
    else:
        # Buffer at most 1 MiB for strided input; never serialize image pixels.
        for chunk in np.nditer(
            array, flags=["external_loop", "buffered", "zerosize_ok"],
            order="C", buffersize=max(1, 1048576 // array.dtype.itemsize),
        ):
            digest.update(memoryview(np.ascontiguousarray(chunk)).cast("B"))
    return {"shape": list(array.shape), "dtype": array.dtype.str,
            "sha256": digest.hexdigest()}


def _input_source_profiles(kwargs, bindings):
    records = []
    for index in sorted(kwargs["valid_clean_indices"]):
        for band in ("VV", "VH"):
            profile = kwargs["prof_" + band.lower()][index]
            record = {"source_id": bindings[index][band], "variable": band + "_dB",
                      "input_index": index, "array_present": kwargs["final_" + band.lower()][index] is not None,
                      "evidence_status": "source_declared"}
            if isinstance(profile, Mapping):
                if "nodata" in profile:
                    record["profile_nodata"] = _json_value(profile["nodata"])
                metadata = profile.get("grits_source")
                if metadata is not None:
                    if not isinstance(metadata, Mapping) or set(metadata) != {"asset", "read"}:
                        raise ValueError("source declaration requires asset metadata and observed read steps")
                    record.update(_json_value(metadata))
            records.append(record)
    validate_numeric_sources(records)
    return records


class _BoundSources(list):
    def __init__(self, arrays, source_ids, tracker, variable):
        super().__init__(arrays)
        self.source_ids = source_ids
        self.numeric_tracker = tracker
        self.variable = variable

    def _record_used(self, index):
        self.numeric_tracker.used.add(self.source_ids[index])

    def record_numeric(self, operation, input_domain, output_domain, parameters):
        self.numeric_tracker.record_numeric(
            operation, self.variable, input_domain, output_domain, parameters)


class _Tracker:
    def __init__(self, run, bindings, source_profiles):
        self.run = run
        self.bindings = bindings
        self.source_profiles = source_profiles
        self.used = set()
        self.steps = {}
        self.output = {}

    def bind(self, arrays, polarization):
        return _BoundSources(
            arrays, [binding[polarization] for binding in self.bindings], self, polarization + "_dB"
        )

    def record_numeric(self, operation, variable, input_domain, output_domain, parameters,
                       *, status="executed"):
        step = dict(operation=operation, variable=variable, input_domain=input_domain,
                    output_domain=output_domain, parameters=_json_value(parameters), status=status)
        key = canonical_json(step)
        if key not in self.steps:
            self.steps[key] = {**step, "calls": 0}
        self.steps[key]["calls"] += 1

    def record_output(self, variable, dtype):
        count = variable == "n_obs"
        self.output[variable] = {
            "domain": "count" if count else "db", "dtype": str(dtype),
            "unit": "aligned_scene_observations" if count else "dB",
            "physical_quantity": "finite_VV_stack_entries_from_selected_track" if count else None,
            "nodata": 0 if count else "NaN", "applied_scale": 1.0, "applied_offset": 0.0,
        }

    def record_clip(self, bands, applied):
        for variable in bands:
            domain = "count" if variable == "n_obs" else "db"
            self.record_numeric("spatial_clip", variable, domain, domain,
                                {"outside": "NaN", "all_touched": False},
                                status="executed" if applied else "not_applied")

    def begin_write(self, indices):
        selected = sorted({source for i in indices for source in self.bindings[i].values()})
        self.run.begin_write(selected)

    def commit(self, catalog_record=None):
        records = None
        if catalog_record is not None:
            records = json.loads(pd.DataFrame([catalog_record]).to_json(
                orient="records", date_format="iso", date_unit="ns", double_precision=15))
        numeric = {
            "schema_version": 1, "scope": "monthly_writer_input_to_zarr",
            "step_order": "first_observed_per_signature; repeated block calls are counted",
            "input": {"actual_domain": None, "operator_expected_domain": "linear_power",
                      "physical_quantity": None, "evidence_status": "unknown",
                      "unknown_reason": "Upstream input physics is not independently verified; "
                                        "recorded domains describe the local operator's interpretation.",
                      "sources": self.source_profiles},
            "steps": list(self.steps.values()), "output": self.output,
        }
        validate_numeric_processing(numeric)
        self.run.commit(sorted(self.used), catalog_records=records, numeric_processing=numeric)


def _validated_kwargs(root: Path, writer_kwargs: dict, source_indices: list[dict], *, allow_catalogs=False):
    if "_provenance_tracker" in writer_kwargs:
        raise ValueError("the provenance tracker is owned by this API")
    bound = inspect.signature(_write_smonthly_one_track).bind(**writer_kwargs)
    bound.apply_defaults()
    kwargs = dict(bound.arguments)
    kwargs.pop("_provenance_tracker", None)
    for field in ("mgrs_tile_id", "direction_label", "track_token"):
        value = kwargs[field]
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError(field + " must be a plain identifier")
    tile = Path(kwargs["tile_dir"]).absolute()
    if tile != root / kwargs["mgrs_tile_id"]:
        raise ValueError("tile_dir must be dataset_root / mgrs_tile_id")
    _relative(root, kwargs["mgrs_tile_id"])
    kwargs["tile_dir"] = tile
    if not allow_catalogs and ((root / "catalog.parquet").exists() or (tile / "catalog.parquet").exists()):
        raise ValueError("the pilot API requires an isolated root without Catalogs")

    forbidden = ("generate_cog", "generate_preview", "features_ratio", "features_rvi",
                 "features_glcm", "spatial_filter_legacy", "rebuild_on_mismatch")
    if any(kwargs[key] for key in forbidden) or kwargs["processing_level"] != "ARDC":
        raise ValueError("the pilot supports blockwise ARDC without features, exports or rebuild")
    if kwargs["copol_name"] != "VV_dB" or kwargs["crosspol_name"] != "VH_dB":
        raise ValueError("the pilot requires VV_dB and VH_dB")
    if kwargs["on_time_conflict"] not in {"skip", "overwrite"}:
        raise ValueError("on_time_conflict must be skip or overwrite")
    monthly = dict(kwargs["monthly_cfg"] or {})
    if set(monthly) - {"composite_method", "trim_fraction", "blockwise_threads",
                       "generate_cog", "generate_preview"}:
        raise ValueError("unsupported monthly configuration in the pilot API")
    if monthly.get("blockwise_threads", 1) != 1:
        raise ValueError("the pilot requires blockwise_threads=1")
    if monthly.get("generate_cog") or monthly.get("generate_preview"):
        raise ValueError("derived exports are not supported by the pilot API")
    method = str(monthly.get("composite_method", "median")).lower()
    if method not in {"median", "nanmedian", "min", "nanmin", "mean", "nanmean"}:
        raise ValueError("unsupported composite method in the pilot API")
    kwargs["monthly_cfg"] = monthly
    kwargs["target_res"] = validate_target_resolution(kwargs["target_res"])
    rasterio_resampling(kwargs["resampling_method"])
    pyproj.CRS(kwargs["target_crs"])
    try:
        transform = Affine(*kwargs["transform"])
    except (TypeError, ValueError) as exc:
        raise ValueError("target transform is invalid") from exc
    if not all(math.isfinite(value) for value in transform) or transform.determinant == 0:
        raise ValueError("target transform must be finite and invertible")
    kwargs["transform"] = transform

    dates = [pd.Timestamp(value) for value in kwargs["clean_dates"]]
    if not dates or any(pd.isna(value) or value.tz is None for value in dates):
        raise ValueError("clean_dates must contain UTC-aware acquisitions")
    dates = [value.tz_convert("UTC") for value in dates]
    months = {value.strftime("%Y-%m") for value in dates}
    if len(months) != 1:
        raise ValueError("one call must contain exactly one calendar month")
    kwargs["clean_dates"] = dates
    count = len(dates)
    if len(source_indices) != count:
        raise ValueError("source_indices must bind every clean_dates position")
    for key in ("final_vv", "prof_vv", "final_vh", "prof_vh"):
        if len(kwargs[key]) != count:
            raise ValueError(key + " must align with clean_dates")
    for band in ("vv", "vh"):
        for array, profile in zip(kwargs["final_" + band], kwargs["prof_" + band]):
            if array is None:
                continue
            if np.asarray(array).ndim != 2:
                raise ValueError("input scenes must be two-dimensional")
            if not isinstance(profile, Mapping) or profile.get("crs") is None or profile.get("transform") is None:
                raise ValueError("each input array needs an explicit CRS and transform")
            try:
                pyproj.CRS(profile["crs"])
                transform = Affine(*profile["transform"])
            except (TypeError, ValueError, pyproj.exceptions.CRSError) as exc:
                raise ValueError("input profile CRS or transform is invalid") from exc
            if not all(math.isfinite(value) for value in transform) or transform.determinant == 0:
                raise ValueError("input profile transform must be finite and invertible")
    valid = kwargs["valid_clean_indices"]
    if valid is None:
        valid = set(range(count))
    elif any(type(index) is not int or index < 0 or index >= count for index in valid):
        raise ValueError("invalid QC index")
    if not valid:
        raise ValueError("no QC-passing acquisitions; no write intent created")
    if not any(kwargs["final_vv"][index] is not None for index in valid):
        raise ValueError("no QC-passing VV arrays; no write intent created")
    kwargs["valid_clean_indices"] = set(valid)

    frame = kwargs["df_batch"]
    if frame is not None:
        required = {"acq_dt", "track_number", "jpl_burst_id", "opera_id", "pass_id"}
        if not required.issubset(frame.columns) or len(frame) != count:
            raise ValueError("df_batch must explicitly align one row per input")
        frame_dates = [pd.Timestamp(value).tz_convert("UTC") for value in frame["acq_dt"]]
        if frame_dates != dates or frame["track_number"].isna().any():
            raise ValueError("df_batch acquisition rows do not match explicit inputs")
        rows = kwargs.get("clean_source_rows")
        if rows is not None and ("_source_row" not in frame or len(rows) != count
                or len(set(rows)) != len(rows) or list(frame["_source_row"]) != list(rows)):
            raise ValueError("clean_source_rows must align exactly with df_batch source rows")
        by_date = frame.groupby("acq_dt")["track_number"].nunique()
        if kwargs.get("clean_source_rows") is None and (by_date > 1).any():
            raise ValueError("same-time multiple tracks are ambiguous in the native writer")
    elif kwargs["restrict_to_group"]:
        raise ValueError("restrict_to_group requires explicit aligned track metadata")

    for key in ("height", "width", "chunk_y", "chunk_x"):
        if type(kwargs[key]) is not int or kwargs[key] <= 0:
            raise ValueError(key + " must be a positive integer")
    for key, size in (("x_coords", kwargs["width"]), ("y_coords", kwargs["height"])):
        coordinate = np.asarray(kwargs[key])
        if coordinate.shape != (size,) or not np.isfinite(coordinate).all():
            raise ValueError("grid coordinate length or values are invalid")
    month = next(iter(months))
    store = (Path(kwargs["mgrs_tile_id"]) / ("smonthly_" + kwargs["direction_label"]) /
             "zarr" / ("s1grits_smonthly_" + kwargs["mgrs_tile_id"] + "_" +
                       kwargs["direction_label"] + "_TK" + kwargs["track_token"] + ".zarr"))
    return kwargs, month, store.as_posix()


def _existing_month(root: Path, store: str, kwargs: dict, month: str) -> bool:
    path = _relative(root, store)
    if not path.exists():
        return False
    group = zarr.open_group(str(path), mode="r", zarr_format=3)
    expected_arrays = {"VV_dB", "VH_dB", "n_obs", "time", "x", "y", "spatial_ref"}
    if set(group.array_keys()) != expected_arrays or list(group.group_keys()):
        raise ValueError("existing store layout must contain only the supported root arrays")
    if any(str(group[name].dtype) != dtype for name, dtype in
           (("VV_dB", "float32"), ("VH_dB", "float32"), ("n_obs", "uint8"))):
        raise ValueError("existing store dtype differs from the tracked numeric contract")
    identity = {
        "product_type": "smonthly",
        "product_label": "smonthly_" + kwargs["direction_label"],
        "geometry_group_id": (kwargs["mgrs_tile_id"] + "_" + kwargs["direction_label"]
                              + "_TK" + kwargs["track_token"]),
        "processing_level": "monthly_ARDC",
        "time_varying": True,
        "array_dims": ["time", "y", "x"],
    }
    if any(group.attrs.get(key) != value for key, value in identity.items()):
        raise ValueError("existing store identity differs from the native monthly writer")
    if group.attrs.get("tile_id", kwargs["mgrs_tile_id"]) != kwargs["mgrs_tile_id"]:
        raise ValueError("existing store tile identity differs")
    if (not np.array_equal(group["x"][:], kwargs["x_coords"])
            or not np.array_equal(group["y"][:], kwargs["y_coords"])
            or pyproj.CRS(group.attrs["crs"]) != pyproj.CRS(kwargs["target_crs"])
            or list(group.attrs["transform"])[:6] != list(kwargs["transform"])[:6]):
        raise ValueError("existing store grid differs; rebuild is not supported")
    times = pd.to_datetime(group["time"][:], utc=True)
    if len(set(times.strftime("%Y-%m"))) != len(times):
        raise ValueError("existing store contains duplicate calendar months")
    for name in ("VV_dB", "VH_dB", "n_obs"):
        dtype = np.dtype("uint8" if name == "n_obs" else "float32")
        if (group[name].shape != (len(times), kwargs["height"], kwargs["width"])
                or group[name].dtype != dtype):
            raise ValueError("existing store band layout is incomplete or incompatible")
    return month in set(times.strftime("%Y-%m"))


def _write_tracked_monthly_locked(dataset_root, dataset_id, run_id, *, writer_kwargs: dict,
                                  sources: list[dict], source_indices: list[dict[str, str]],
                                  request_context: dict | None = None) -> dict:
    """Write or recover one month under the normal output-root lock.

    Supplied source records and array bindings are declarations, not downloaded
    asset verification. Used links attest only that the bound arrays entered
    the actual monthly composite stack. Catalogs require the workflow coordinator;
    the public standalone pilot continues to reject existing Catalogs.
    """
    root = Path(dataset_root).absolute()
    kwargs, month, store = _validated_kwargs(root, writer_kwargs, source_indices,
        allow_catalogs=bool(request_context and request_context.get("catalog_mode") == "tile_global_v1"))
    validate_records([], sources, [])
    if any(row["evidence_status"] not in {"source_declared", "unknown"} for row in sources):
        raise ValueError("pilot source evidence must be source_declared or unknown")
    source_ids = {row["source_id"] for row in sources}
    for binding in source_indices:
        if (not isinstance(binding, dict) or set(binding) != {"VV", "VH"}
                or any(not isinstance(value, str) or value not in source_ids
                       for value in binding.values())):
            raise ValueError("each input binding must map VV and VH to known source IDs")
    source_profiles = _input_source_profiles(kwargs, source_indices)
    inputs = {key: [_array_identity(array) for array in kwargs[key]]
              for key in ("final_vv", "final_vh")}
    profiles = {key: hashlib.sha256(canonical_json(_json_value(kwargs[key]))).hexdigest()
                for key in ("prof_vv", "prof_vh")}
    excluded = {"final_vv", "final_vh", "prof_vv", "prof_vh", "df_batch",
                "tile_dir", "x_coords", "y_coords", "valid_clean_indices"}
    parameters = {key: _json_value(value) for key, value in kwargs.items() if key not in excluded}
    parameters["valid_clean_indices"] = sorted(kwargs["valid_clean_indices"])
    parameters["tile_dir"] = kwargs["mgrs_tile_id"]
    parameters["x_coords"] = _array_identity(kwargs["x_coords"])
    parameters["y_coords"] = _array_identity(kwargs["y_coords"])
    frame = kwargs["df_batch"]
    parameters["track_metadata_sha256"] = (hashlib.sha256(canonical_json(_json_value(
        frame.to_dict(orient="records")))).hexdigest() if frame is not None else None)
    request = {"writer": "smonthly_blockwise_pilot_v1", "store_path": store, "month": month,
               "numeric_processing_version": 1,
               "parameters": parameters, "input_arrays": inputs, "input_profiles_sha256": profiles,
               "sources": sources, "source_indices": source_indices,
               "source_binding_evidence": "caller_declared_array_to_asset_mapping",
               "used_evidence": "actual_array_admitted_to_composite_stack",
               "catalog_scope": "records_returned_only_no_catalogs_owned"}
    if request_context:
        request.update(request_context)
    # Caller holds the root lock for validation, mutation and publication.
    exists = _existing_month(root, store, kwargs, month)
    run = ProductionRun(root, dataset_id, run_id, request=request)
    if run.completed:
        return run.recover()
    if exists and kwargs["on_time_conflict"] == "skip":
        if run.pending_path.exists():
            raise ValueError("pending write without a durable receipt cannot be skipped")
        return {"status": "skipped", "store_path": store, "month": month,
                "catalog_records": [], "unchanged": True}
    if month not in _monthly_output_months(kwargs):
        raise ValueError("no valid output; no write intent created")
    run.prepare(store, month, parameters, sources)
    records = _write_smonthly_one_track(
        **kwargs, _provenance_tracker=_Tracker(run, source_indices, source_profiles))
    if len(records) != 1:
        raise RuntimeError("tracked monthly writer did not produce exactly one output")
    result = run.publish()
    return {**result, "catalog_records": records}


def write_tracked_monthly(dataset_root, dataset_id, run_id, *, writer_kwargs: dict,
                          sources: list[dict], source_indices: list[dict[str, str]]) -> dict:
    """Standalone pilot API; workflow uses the internal caller-held lock path."""
    root = Path(dataset_root).absolute()
    _validated_kwargs(root, writer_kwargs, source_indices)
    validate_records([], sources, [])
    if any(row["evidence_status"] not in {"source_declared", "unknown"} for row in sources):
        raise ValueError("pilot source evidence must be source_declared or unknown")
    with output_lock(root):
        return _write_tracked_monthly_locked(root, dataset_id, run_id, writer_kwargs=writer_kwargs,
                                            sources=sources, source_indices=source_indices)
