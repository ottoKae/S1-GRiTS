"""Versioned sidecar tables; never read or modify production imagery.

A data revision is a structural/commit binding, not a full image checksum.
Callers supply only original data witnesses, never quality publications or
provenance state. Production write generations are supplied, not inferred here.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA_VERSION = 1
_EVIDENCE = {
    "execution_recorded", "artifact_observed", "source_declared",
    "historical_reported", "current_implementation_reference", "unknown",
}
_ROLES = {"candidate", "selected", "read", "used", "excluded", "unknown"}
_CREDENTIAL_KEYS = {
    "access_token", "token", "id_token", "refresh_token", "auth", "authorization",
    "signature", "sig", "password", "secret", "client_secret", "api_key", "apikey",
    "awsaccesskeyid", "googleaccessid", "credential", "credentials",
}
_AZURE_SAS_KEYS = {"sv", "ss", "srt", "sp", "se", "st", "spr", "sr", "sip",
                   "skoid", "sktid", "skt", "ske", "sks", "skv", "saoid", "suoid", "scid"}
_OUTPUT_FIELDS = [
    "output_id", "generation_id", "store_path", "zarr_group", "variable_scope",
    "tile_id", "geometry_group_id", "grid_id", "product_type", "record_kind",
    "time_index", "time_value_utc", "period_start", "period_end", "source_item_id",
    "run_id", "processing_id", "evidence_status", "evidence_ref", "unknown_reason",
]
_SOURCE_FIELDS = [
    "source_id", "collection", "product_id", "asset_key", "href", "source_sha256",
    "declared_scale", "declared_offset", "evidence_status", "evidence_ref",
]
_LINK_FIELDS = ["output_id", "generation_id", "source_id", "role", "evidence_ref"]
_DATA_FIELDS = [
    "output_id", "generation_id", "store_path", "zarr_group", "variable_scope",
    "tile_id", "geometry_group_id", "grid_id", "product_type", "record_kind",
    "time_index", "time_value_utc", "period_start", "period_end", "source_item_id",
]


def _schema(name: str, fields: list[str]) -> pa.Schema:
    types = {"time_index": pa.int64(), "variable_scope": pa.list_(pa.field("element", pa.string())),
             "declared_scale": pa.float64(), "declared_offset": pa.float64()}
    return pa.schema([(field, types.get(field, pa.string())) for field in fields],
                     metadata={b"grits:provenance_schema_version": b"1",
                               b"grits:table": name.encode("ascii")})


TABLE_SCHEMAS = {
    "outputs": _schema("outputs", _OUTPUT_FIELDS),
    "sources": _schema("sources", _SOURCE_FIELDS),
    "source_links": _schema("source_links", _LINK_FIELDS),
}


def canonical_json(value) -> bytes:
    """Finite JSON with stable object ordering, preserving UTF-8 text."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def validate_historical_annotations(outputs, variables, limitations, evidence, *, required=False):
    """Validate active scoped annotations, not archived snapshots inside receipts.

    The release envelope may be v2; tables and data_revision remain v1.
    Return whether active annotations require the v2 envelope.
    """
    entries = [(r, "historical_output_generations", r, True) for r in variables]
    entries += [(r, "historical_output_generations", r, False) for r in limitations]
    for name, record in evidence.items():
        if name.startswith("stores/") and "historical_context" in record:
            context = record["historical_context"]
            if not isinstance(context, dict):
                raise ValueError("historical context must be an object")
            entries.append((context, "output_generations", record, False))
    scoped = any(key in record for record, key, _, _ in entries)
    if not (required or scoped):
        return False
    by_id = {row["output_id"]: row for row in outputs}
    for record, key, location, variable in entries:
        if (key not in record and "historical_processing_ref" not in record
                and key != "output_generations"):
            continue
        scope = record.get(key)
        if not isinstance(scope, dict) or not scope:
            raise ValueError("historical annotation requires a nonempty output scope")
        if not isinstance(location.get("store_path"), str) or not location["store_path"]:
            raise ValueError("historical annotation requires a store location")
        if variable and ("zarr_group" not in location or not location.get("variable")):
            raise ValueError("historical variable annotation requires group and variable")
        for identifier, generation in scope.items():
            row = by_id.get(identifier)
            if (not isinstance(identifier, str) or not row
                    or (generation is not None and not isinstance(generation, str))
                    or row.get("generation_id") != generation):
                raise ValueError("historical annotation output/generation is not current")
            if (row["store_path"] != location["store_path"]
                    or ("zarr_group" in location and row["zarr_group"] != location["zarr_group"])
                    or (variable and location["variable"] not in row["variable_scope"])):
                raise ValueError("historical annotation crosses its store/group/variable")
        for field, ref in record.items():
            if field.endswith("_ref"):
                if not isinstance(ref, str) or not ref.startswith("evidence/"):
                    raise ValueError("historical annotation reference must name packaged evidence")
                _relative_path(ref)
                if ref.removeprefix("evidence/") not in evidence:
                    raise ValueError("historical annotation evidence is missing")
    return scoped


def _valid_numeric_nodata(value):
    return (value is None or type(value) is int or (type(value) is float and math.isfinite(value))
            or (isinstance(value, dict) and set(value) == {"nonfinite"}
                and value["nonfinite"] in ("nan", "inf", "-inf")))


def validate_numeric_sources(records: list[dict]) -> None:
    """Validate optional asset declarations at both read and commit boundaries."""
    if not isinstance(records, list):
        raise ValueError("numeric source declarations must be a list")
    canonical_json(records)
    tag_keys = {"units", "unit", "data_domain", "radiometric_domain", "measurement_domain",
                "physical_quantity", "product_version", "processing_version", "algorithm_version",
                "processing_software_version"}
    domain_keys = {"units", "unit", "data_domain", "radiometric_domain", "measurement_domain", "physical_quantity"}
    seen = set()
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("source_id"), str)
                or not record["source_id"].strip() or record.get("variable") not in {"VV_dB", "VH_dB"}
                or type(record.get("input_index")) is not int or record["input_index"] < 0
                or type(record.get("array_present")) is not bool
                or record.get("evidence_status") != "source_declared"):
            raise ValueError("invalid numeric source declaration identity")
        if set(record) - {"source_id", "variable", "input_index", "array_present", "evidence_status",
                          "profile_nodata", "asset", "read"}:
            raise ValueError("unrecognized numeric source declaration fields")
        key = (record["input_index"], record["variable"])
        if key in seen:
            raise ValueError("duplicate numeric source declaration input")
        seen.add(key)
        if not _valid_numeric_nodata(record.get("profile_nodata")):
            raise ValueError("invalid numeric source declaration nodata")
        if "asset" not in record and "read" not in record:
            continue
        asset, read = record.get("asset"), record.get("read")
        if (not isinstance(asset, dict) or type(asset.get("band")) is not int or asset["band"] != 1
                or not isinstance(asset.get("dtype"), str) or not asset["dtype"]
                or not (asset.get("unit") is None or isinstance(asset["unit"], str))
                or any(type(asset.get(k)) not in {int, float} or not math.isfinite(asset[k]) for k in ("scale", "offset"))
                or asset.get("scale_offset_basis") != "rasterio_metadata_or_default"):
            raise ValueError("invalid numeric source declaration asset metadata")
        if (not isinstance(read, dict) or read.get("method") != "rasterio.read"
                or type(read.get("band")) is not int or read["band"] != 1
                or read.get("masked") is not False or read.get("output_dtype") != "float32"
                or read.get("scale_offset_applied") is not False):
            raise ValueError("invalid numeric source declaration read operation")
        if (set(asset) - {"band", "dtype", "unit", "scale", "offset", "scale_offset_basis", "tags"}
                or set(read) - {"method", "band", "masked", "output_dtype", "scale_offset_applied"}):
            raise ValueError("unrecognized numeric source declaration asset/read fields")
        tags = asset.get("tags")
        if not isinstance(tags, dict) or set(tags) != {"dataset", "band"}:
            raise ValueError("invalid numeric source declaration tag groups")
        declarations = [asset.get("unit")]
        for group in tags.values():
            if (not isinstance(group, dict) or set(group) - tag_keys
                    or any(not isinstance(value, str) for value in group.values())):
                raise ValueError("invalid numeric source declaration tags")
            declarations.extend(value for key, value in group.items() if key in domain_keys)
        conflicts = {"db", "decibel", "decibels", "amplitude", "linear_amplitude"}
        if record["array_present"] and any(
                value and value.strip().casefold().replace("-", "_").replace(" ", "_") in conflicts
                for value in declarations):
            raise ValueError("source declaration conflicts with the linear_power monthly input requirement")


def validate_numeric_processing(record: dict) -> None:
    """Validate the optional, output-generation-scoped monthly execution record."""
    if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
            or record["schema_version"] != 1
            or record.get("scope") != "monthly_writer_input_to_zarr"):
        raise ValueError("unsupported numeric processing record")
    canonical_json(record)
    source = record.get("input", {})
    if (not isinstance(source, dict)
            or not {"actual_domain", "physical_quantity", "operator_expected_domain", "evidence_status", "unknown_reason"} <= source.keys()
            or source.get("actual_domain") is not None
            or source.get("physical_quantity") is not None
            or source.get("operator_expected_domain") != "linear_power"
            or source.get("evidence_status") != "unknown"
            or not isinstance(source.get("unknown_reason"), str) or not source["unknown_reason"].strip()):
        raise ValueError("numeric input must distinguish operator assumptions from unknown source physics")
    if "sources" in source:
        validate_numeric_sources(source["sources"])
    output = record.get("output")
    if not isinstance(output, dict) or set(output) != {"VV_dB", "VH_dB", "n_obs"}:
        raise ValueError("numeric output must describe VV_dB, VH_dB and n_obs")
    for variable, info in output.items():
        count = variable == "n_obs"
        if (not isinstance(info, dict)
                or info.get("domain") != ("count" if count else "db")
                or info.get("dtype") != ("uint8" if count else "float32")
                or info.get("nodata") != (0 if count else "NaN")
                or info.get("unit") != ("aligned_scene_observations" if count else "dB")
                or info.get("applied_scale") != 1 or info.get("applied_offset") != 0
                or "physical_quantity" not in info
                or info["physical_quantity"] != ("finite_VV_stack_entries_from_selected_track" if count else None)):
            raise ValueError("numeric output dtype, domain or storage semantics mismatch")
    steps = record.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("numeric processing requires executed steps")
    operations = {"grid_alignment", "input_filter", "monthly_composite", "observation_count",
                  "track_priority_mosaic", "fill_missing", "linear_to_db", "spatial_clip",
                  "storage_cast_and_write"}
    for step in steps:
        if (not isinstance(step, dict) or step.get("operation") not in operations
                or step.get("variable") not in output
                or step.get("status") not in {"executed", "not_applied"}
                or type(step.get("calls")) is not int or step["calls"] < 1
                or step.get("input_domain") not in {"linear_power", "db", "count"}
                or step.get("output_domain") not in {"linear_power", "db", "count"}
                or not isinstance(step.get("parameters"), dict)):
            raise ValueError("invalid numeric execution step")
        op, params = step["operation"], step["parameters"]
        count = step["variable"] == "n_obs"
        before = "count" if count else "linear_power"
        after = "count" if count else "db"
        domains = (("linear_power", "count") if op == "observation_count" else
                   (after, after) if op in {"spatial_clip", "storage_cast_and_write"} else
                   ("linear_power", "db") if op == "linear_to_db" else (before, before))
        if ((step["input_domain"], step["output_domain"]) != domains
                or (op == "observation_count" and not count)
                or (count and op not in {"observation_count", "fill_missing", "spatial_clip", "storage_cast_and_write"})):
            raise ValueError("numeric operation variable/domain mismatch")
        if op == "grid_alignment" and (
                params.get("mode") not in {"direct_copy", "window_reproject", "prealign_reproject", "legacy_full_mosaic_fallback"}
                or (params["mode"] == "direct_copy" and params.get("resampling") is not None)
                or (params["mode"] != "direct_copy" and not isinstance(params.get("resampling"), str))):
            raise ValueError("numeric alignment parameters mismatch")
        if op == "grid_alignment":
            policies = {"direct_copy": "mask_source_nodata", "window_reproject": "rasterio_src_nodata",
                        "prealign_reproject": "rasterio_src_nodata"}
            if (("input_index" in params and (type(params["input_index"]) is not int or params["input_index"] < 0))
                    or ("src_nodata" in params and not _valid_numeric_nodata(params["src_nodata"]))
                    or ("nodata_policy" in params and (
                        not isinstance(params["nodata_policy"], str)
                        or params["nodata_policy"] != policies.get(params["mode"])))
                    or ("validity_policy" in params and (
                        params["mode"] != "legacy_full_mosaic_fallback"
                        or params["validity_policy"] != "legacy_helper_not_instrumented"))):
                raise ValueError("numeric alignment source/nodata parameters mismatch")
        if op == "input_filter" and (params.get("nonpositive") != "NaN"
                                     or params.get("nonfinite", "NaN") != "NaN"):
            raise ValueError("numeric filter parameters mismatch")
        if op == "observation_count" and (params.get("method", "sum_isfinite") != "sum_isfinite"
                                           or params.get("count_unit", "aligned_scene_stack_entry") != "aligned_scene_stack_entry"):
            raise ValueError("numeric observation count parameters mismatch")
        if op == "fill_missing" and params.get("value") != (0 if count else "NaN"):
            raise ValueError("numeric fill parameters mismatch")
        if op == "track_priority_mosaic" and (
                not isinstance(params.get("order"), list) or not params["order"]
                or any(type(value) is not int for value in params["order"])
                or len(set(params["order"])) != len(params["order"])
                or params.get("selection") != "first_finite_VV"):
            raise ValueError("numeric track priority parameters mismatch")
        if op == "spatial_clip" and (params.get("outside") != "NaN" or params.get("all_touched") is not False):
            raise ValueError("numeric clip parameters mismatch")
        if step["status"] == "not_applied" and op != "spatial_clip":
            raise ValueError("only a skipped spatial clip may be not_applied")
        if op == "monthly_composite" and (
                params.get("method") not in {"nanmedian", "nanmean", "nanmin"}
                or params.get("backend") not in {"numpy", "bottleneck"}
                or params.get("axis") != 0 or params.get("nan_policy") != "ignore_nan"
                or step["input_domain"] != "linear_power" or step["output_domain"] != "linear_power"):
            raise ValueError("unsupported numeric aggregation")
        if op == "linear_to_db" and (
                params.get("formula") != "10*log10(x)" or params.get("nonpositive") != "NaN"
                or step["input_domain"] != "linear_power" or step["output_domain"] != "db"):
            raise ValueError("numeric logarithm formula/domain mismatch")
        if op == "storage_cast_and_write" and (
                params.get("dtype") != output[step["variable"]]["dtype"]
                or step["input_domain"] != output[step["variable"]]["domain"]
                or step["output_domain"] != step["input_domain"]):
            raise ValueError("numeric storage step does not match output")
    for variable in output:
        sequence = [step["operation"] for step in steps
                    if step["variable"] == variable and step["status"] == "executed"]
        required = (["observation_count", "storage_cast_and_write"] if variable == "n_obs"
                    else ["linear_to_db", "storage_cast_and_write"])
        if any(op not in sequence for op in required):
            raise ValueError("numeric execution is missing a required step")
        before_write = [sequence.index(required[0])]
        if variable == "n_obs" and "fill_missing" in sequence:
            before_write.append(sequence.index("fill_missing"))
        if min(before_write) >= sequence.index(required[1]):
            raise ValueError("numeric execution steps are out of order")
        if variable != "n_obs":
            predecessors = [sequence.index(op) for op in ("monthly_composite", "fill_missing") if op in sequence]
            if (not predecessors or (variable == "VV_dB" and "monthly_composite" not in sequence)
                    or min(predecessors) >= sequence.index("linear_to_db")):
                raise ValueError("numeric composite must precede logarithm and storage")


def logical_output_id(identity: dict) -> str:
    """Hash explicitly supplied logical identity; do not infer it from an index."""
    if not isinstance(identity, dict) or not identity:
        raise ValueError("logical identity must be a nonempty dictionary")
    return "output-" + hashlib.sha256(canonical_json(identity)).hexdigest()


def data_revision(outputs: list[dict], witnesses: dict[str, str]) -> str:
    """Bind data locations/generations and original-file witnesses only.

    Evidence text, run descriptions, quality pairing and pending lifecycle never
    enter this digest. A caller must not supply those files as data witnesses.
    """
    records = []
    for row in outputs:
        record = {key: row.get(key) for key in _DATA_FIELDS}
        record["variable_scope"] = sorted(record["variable_scope"] or [])
        record["zarr_group"] = record["zarr_group"] or ""
        if record["zarr_group"] == "/":
            record["zarr_group"] = ""
        records.append(record)
    records.sort(key=canonical_json)
    if not isinstance(witnesses, dict):
        raise ValueError("witnesses must map relative data paths to digests")
    for path, digest in witnesses.items():
        _relative_path(path)
        if not isinstance(digest, str) or not digest:
            raise ValueError("witness digest must be a nonempty string")
    payload = {"schema_version": SCHEMA_VERSION, "outputs": records, "witnesses": witnesses}
    return "data-" + hashlib.sha256(canonical_json(payload)).hexdigest()


def sanitize_asset_url(href: str) -> str:
    """Remove authorization query fields, retaining public version parameters.

    Userinfo is rejected rather than silently preserving a private account name.
    Non-credential query bytes keep their original encoding and ordering.
    """
    if not isinstance(href, str) or any(ord(char) < 32 or ord(char) == 127 for char in href):
        raise ValueError("asset URL must be text without control characters")
    parts = urlsplit(href)
    if parts.username is not None or parts.password is not None:
        raise ValueError("asset URL userinfo is not permitted")
    if parts.scheme not in {"https", "http", "s3", "gs", "az"} or not parts.netloc:
        raise ValueError("asset URL must have a public scheme and authority")
    if "\\" in href:
        raise ValueError("asset URL must not contain backslashes")
    segments = parts.query.split("&") if parts.query else []
    keys = [unquote_plus(segment.partition("=")[0]).casefold() for segment in segments]
    azure_signed = "sig" in keys
    kept = [segment for segment, key in zip(segments, keys)
            if key not in _CREDENTIAL_KEYS
            and not key.startswith(("x-amz-", "x-goog-"))
            and not (azure_signed and key in _AZURE_SAS_KEYS)]
    if any(unquote_plus(part.partition("=")[0]).casefold() in _CREDENTIAL_KEYS
           for part in parts.fragment.split("&")):
        raise ValueError("asset URL fragment contains credentials")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept), parts.fragment))


def _relative_path(value: str, *, group: bool = False) -> None:
    if group and value in {"", "/"}:
        return
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(char in value for char in '?*<>|"')
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("path must be a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value == ".":
        raise ValueError("relative path must not escape the dataset")


def _check_row(row: dict, fields: list[str]) -> None:
    if not isinstance(row, dict):
        raise ValueError("records must be dictionaries")
    extra = row.keys() - set(fields)
    if extra:
        raise ValueError(f"unrecognized columns: {sorted(extra)}")
    for key, value in row.items():
        if value is None or key in {"variable_scope", "time_index", "declared_scale", "declared_offset"}:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string or null")
    if row.get("evidence_ref") is not None:
        _relative_path(row["evidence_ref"])
    if "evidence_status" in fields and row.get("evidence_status") not in _EVIDENCE:
        raise ValueError("unrecognized evidence_status")


def _required(row: dict, names: tuple[str, ...]) -> None:
    for name in names:
        if not isinstance(row.get(name), str) or not row[name].strip():
            raise ValueError(f"{name} must be a nonempty string")


def validate_records(outputs: list[dict], sources: list[dict], source_links: list[dict]) -> None:
    """Validate the current snapshot, including exact-generation source links.

    Unknown historical processing and absent quality do not imply unfinished
    imagery. Writer pending protections belong to publication/read integration.
    """
    current, occupied = {}, set()
    for row in outputs:
        _check_row(row, _OUTPUT_FIELDS)
        _required(row, ("output_id", "store_path", "product_type", "record_kind"))
        _relative_path(row["store_path"])
        _relative_path(row.get("zarr_group"), group=True)
        if row["output_id"] in current:
            raise ValueError("duplicate current output_id")
        current[row["output_id"]] = row.get("generation_id")
        for key in ("generation_id", "run_id", "processing_id"):
            if row.get(key) is not None and not row[key].strip():
                raise ValueError(f"{key} must be nonempty or explicitly null")
        if (any(row.get(key) is None for key in ("generation_id", "run_id", "processing_id"))
                or row["evidence_status"] == "unknown") and not row.get("unknown_reason", ""):
            raise ValueError("unknown historical values require unknown_reason")
        index = row.get("time_index")
        if index is not None and (type(index) is not int or index < 0):
            raise ValueError("time_index must be a nonnegative integer or null")
        if row["record_kind"] == "static":
            if any(row.get(key) is not None for key in ("time_index", "time_value_utc", "period_start", "period_end")):
                raise ValueError("static records must not invent a time axis")
        elif index is None or not row.get("time_value_utc"):
            raise ValueError("temporal outputs require a time_index and actual time")
        variables = row.get("variable_scope")
        if (not isinstance(variables, list) or not variables
                or any(not isinstance(item, str) or not item for item in variables)
                or len(set(variables)) != len(variables)):
            raise ValueError("variable_scope must contain distinct nonempty variable names")
        for variable in variables:
            group = "" if row["zarr_group"] in {"", "/"} else str(PurePosixPath(row["zarr_group"]))
            location = (str(PurePosixPath(row["store_path"])), group, index, variable)
            if location in occupied:
                raise ValueError("duplicate physical variable location")
            occupied.add(location)
    source_ids = set()
    for row in sources:
        _check_row(row, _SOURCE_FIELDS)
        _required(row, ("source_id",))
        if row["source_id"] in source_ids:
            raise ValueError("duplicate source_id")
        source_ids.add(row["source_id"])
        if row.get("href") is not None and sanitize_asset_url(row["href"]) != row["href"]:
            raise ValueError("source href contains credential query fields; sanitize before publication")
        for field in ("declared_scale", "declared_offset"):
            value = row.get(field)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError(f"{field} must be finite numeric metadata or null")
    seen_links = set()
    for row in source_links:
        _check_row(row, _LINK_FIELDS)
        _required(row, ("output_id", "source_id", "role"))
        if row["output_id"] not in current or row["source_id"] not in source_ids:
            raise ValueError("source link references an absent output/source")
        if row.get("generation_id") != current[row["output_id"]]:
            raise ValueError("source link references a different write generation")
        if row["role"] not in _ROLES:
            raise ValueError("unrecognized source participation role")
        key = (row["output_id"], row.get("generation_id"), row["source_id"], row["role"])
        if key in seen_links:
            raise ValueError("duplicate source link")
        seen_links.add(key)


def write_tables(directory, outputs: list[dict], sources: list[dict], source_links: list[dict]) -> None:
    """Write fixed-schema tables into the caller's unpublished release directory."""
    validate_records(outputs, sources, source_links)
    tables = [pa.Table.from_pylist(rows, schema=TABLE_SCHEMAS[name])
              for name, rows in zip(TABLE_SCHEMAS, (outputs, sources, source_links))]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, table in zip(TABLE_SCHEMAS, tables):
        pq.write_table(table, directory / f"{name}.parquet")


def read_tables(directory) -> tuple[list[dict], list[dict], list[dict]]:
    """Read and validate this schema version without inferring nullable types."""
    records = []
    for name, schema in TABLE_SCHEMAS.items():
        table = pq.read_table(Path(directory) / f"{name}.parquet")
        if not table.schema.equals(schema, check_metadata=True):
            raise ValueError(f"{name} schema does not match provenance v{SCHEMA_VERSION}")
        records.append(table.to_pylist())
    validate_records(*records)
    return tuple(records)
