"""Contract checks use real Parquet files, without a production writer."""
from copy import deepcopy

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from s1grits.provenance_contract import (
    SCHEMA_VERSION, canonical_json, data_revision, logical_output_id,
    read_tables, sanitize_asset_url, validate_numeric_processing, validate_records, write_tables,
)


@pytest.mark.parametrize("change", [
    "version", "source_claim", "missing_source_domain", "no_reason", "formula", "aggregation_domain",
    "method", "backend", "storage_dtype", "zero_calls", "bool_calls", "missing_step", "order",
    "missing_output", "nonfinite_parameter", "count_physics", "filter_domain",
    "count_domain", "count_method", "count_variable", "fill_domain", "clip_domain",
])
def test_numeric_processing_rejects_incomplete_or_inconsistent_records(change):
    from test_provenance_production import _numeric_payload
    record = _numeric_payload()
    if change == "version":
        record["schema_version"] = True
    elif change == "source_claim":
        record["input"]["actual_domain"] = "linear_power"
    elif change == "missing_source_domain":
        record["input"].pop("actual_domain")
    elif change == "no_reason":
        record["input"]["unknown_reason"] = ""
    elif change == "formula":
        record["steps"][2]["parameters"]["formula"] = "20*log10(x)"
    elif change == "aggregation_domain":
        record["steps"][1]["input_domain"] = "db"
    elif change == "method":
        record["steps"][1]["parameters"]["method"] = "median_without_nan_handling"
    elif change == "backend":
        record["steps"][1]["parameters"]["backend"] = "unverified_backend"
    elif change == "storage_dtype":
        record["steps"][3]["parameters"]["dtype"] = "uint8"
    elif change in {"zero_calls", "bool_calls"}:
        record["steps"][0]["calls"] = 0 if change == "zero_calls" else True
    elif change == "missing_step":
        record["steps"].pop(2)
    elif change == "order":
        record["steps"][1], record["steps"][2] = record["steps"][2], record["steps"][1]
    elif change == "missing_output":
        record["output"].pop("VH_dB")
    elif change == "nonfinite_parameter":
        record["steps"][0]["parameters"]["invented"] = float("nan")
    elif change == "count_physics":
        record["output"]["n_obs"]["physical_quantity"] = "unique_acquisition_dates"
    elif change == "filter_domain":
        record["steps"][0]["output_domain"] = "db"
    elif change in {"count_domain", "count_method", "count_variable"}:
        step = next(s for s in record["steps"] if s["operation"] == "observation_count")
        if change == "count_domain":
            step["output_domain"] = "db"
        elif change == "count_method":
            step["parameters"]["method"] = "nanmedian"
        else:
            step["variable"] = "VV_dB"
    elif change in {"fill_domain", "clip_domain"}:
        record["steps"].append(dict(
            operation="fill_missing" if change == "fill_domain" else "spatial_clip",
            variable="n_obs", input_domain="db", output_domain="db",
            parameters={"value": 0, "outside": "NaN", "all_touched": False},
            status="executed", calls=1))
    with pytest.raises(ValueError):
        validate_numeric_processing(record)


def test_numeric_processing_accepts_missing_vh_then_observed_vh_in_later_blocks():
    from test_provenance_production import _numeric_payload
    record = _numeric_payload()
    fill = dict(operation="fill_missing", variable="VH_dB", input_domain="linear_power",
                output_domain="linear_power", parameters={"value": "NaN"}, status="executed", calls=1)
    vh_filter, vh_composite, vh_log, vh_write = record["steps"][4:8]
    record["steps"][4:8] = [fill, vh_log, vh_write, vh_filter, vh_composite]
    validate_numeric_processing(record)


def output(**changes):
    row = dict(
        output_id="out-A", generation_id=None, store_path="18NUF/monthly.zarr",
        zarr_group="", variable_scope=["VV", "VH"], tile_id="18NUF",
        geometry_group_id="TK120", grid_id="grid-1", product_type="smonthly",
        record_kind="monthly", time_index=0, time_value_utc="2025-01-18T00:00:00Z",
        period_start="2025-01-01T00:00:00Z", period_end="2025-02-01T00:00:00Z",
        source_item_id=None, run_id=None, processing_id=None,
        evidence_status="artifact_observed", evidence_ref="evidence/snapshot.json",
        unknown_reason="Historical execution and write generation are not recorded.",
    )
    row.update(changes)
    return row


def source(**changes):
    row = dict(
        source_id="source-1", collection="sentinel-2-c1-l2a", product_id="scene-1",
        asset_key="B02", href="https://example.org/asset.tif?versionId=public-v1",
        source_sha256=None, declared_scale=None, declared_offset=None,
        evidence_status="source_declared", evidence_ref="evidence/item.json",
    )
    row.update(changes)
    return row


def link(**changes):
    row = dict(output_id="out-A", generation_id=None, source_id="source-1",
               role="candidate", evidence_ref="evidence/item.json")
    row.update(changes)
    return row


def test_canonical_identity_has_stable_key_order_and_real_utf8():
    assert SCHEMA_VERSION == 1
    assert canonical_json({"b": 2, "a": "\u4e2d"}) == b'{"a":"\xe4\xb8\xad","b":2}'
    identity = {"dataset": "ecuador", "month": "2025-01", "grid": "g1"}
    assert logical_output_id(identity) == logical_output_id(dict(reversed(list(identity.items()))))
    assert logical_output_id(identity) != logical_output_id({**identity, "month": "2025-02"})
    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})


def test_revision_separates_data_from_evidence_quality_and_pending():
    row = output()
    baseline = data_revision([row], {"catalog.parquet": "abc"})
    revised = {**row, "evidence_status": "execution_recorded", "evidence_ref": "new.json",
               "run_id": "run-1", "processing_id": "known", "unknown_reason": None,
               "metadata_release_id": "r2", "quality_available": True,
               "quality_release_id": "q2", "pending": "released"}
    assert data_revision([revised], {"catalog.parquet": "abc"}) == baseline
    assert data_revision([output(generation_id="write-2")], {"catalog.parquet": "abc"}) != baseline
    assert data_revision([output(time_index=1)], {"catalog.parquet": "abc"}) != baseline
    assert data_revision([row], {"catalog.parquet": "changed"}) != baseline
    extra = output(output_id="out-B", time_index=1, period_start="2025-02-01T00:00:00Z")
    assert data_revision([row, extra], {}) == data_revision([extra, row], {})
    assert data_revision([output(variable_scope=["VH", "VV"])], {}) == data_revision([row], {})


def test_nullable_historical_records_roundtrip_without_losing_types(tmp_path):
    rows = ([output()], [source()], [link()])
    write_tables(tmp_path, *rows)
    assert read_tables(tmp_path) == rows
    schema = pq.read_schema(tmp_path / "sources.parquet")
    assert schema.field("declared_offset").type == pa.float64()
    assert schema.field("source_sha256").type == pa.string()
    assert pq.read_schema(tmp_path / "outputs.parquet").field("time_index").type == pa.int64()
    assert read_tables(tmp_path)[0][0]["generation_id"] is None


def test_empty_tables_keep_identical_schemas(tmp_path):
    filled, empty = tmp_path / "filled", tmp_path / "empty"
    write_tables(filled, [output()], [source()], [link()])
    write_tables(empty, [], [], [])
    assert read_tables(empty) == ([], [], [])
    for name in ("outputs", "sources", "source_links"):
        assert pq.read_schema(filled / f"{name}.parquet") == pq.read_schema(empty / f"{name}.parquet")


def test_missing_optional_fields_read_back_as_null(tmp_path):
    row = output()
    del row["source_item_id"]
    write_tables(tmp_path, [row], [], [])
    assert read_tables(tmp_path)[0][0]["source_item_id"] is None


def test_static_without_time_and_stable_data_without_evidence_are_allowed():
    validate_records([output(record_kind="static", time_index=None, time_value_utc=None,
                             period_start=None, period_end=None)], [], [])
    validate_records([output(evidence_status="unknown", evidence_ref=None)], [], [])


@pytest.mark.parametrize("patch", [
    {"generation_id": None, "unknown_reason": None},
    {"generation_id": ""}, {"time_index": -1}, {"time_index": True}, {"variable_scope": []},
    {"variable_scope": ["VV", "VV"]}, {"evidence_status": "guessed"},
    {"record_kind": "static"}, {"unknown_column": "must not disappear"},
])
def test_invalid_output_records_are_rejected(patch):
    with pytest.raises(ValueError):
        validate_records([output(**patch)], [], [])


@pytest.mark.parametrize("path", ["../outside.zarr", "/outside.zarr", "D:/outside.zarr",
                                   "tile/../../outside.zarr", "tile\\outside.zarr"])
def test_output_paths_cannot_escape_delivery(path):
    with pytest.raises(ValueError):
        validate_records([output(store_path=path)], [], [])


def test_duplicate_current_output_or_physical_variable_location_is_rejected():
    with pytest.raises(ValueError, match="output_id"):
        validate_records([output(), output(generation_id="new-write")], [], [])
    with pytest.raises(ValueError, match="location"):
        validate_records([output(), output(output_id="out-B", variable_scope=["VV"])], [], [])
    validate_records([output(variable_scope=["VV"]),
                      output(output_id="out-B", variable_scope=["VH"])], [], [])


@pytest.mark.parametrize("change", [{"output_id": "missing"}, {"source_id": "missing"},
                                    {"generation_id": "old-write"}, {"role": "guessed"}])
def test_source_links_require_existing_exact_generation(change):
    with pytest.raises(ValueError):
        validate_records([output()], [source()], [link(**change)])


def test_sources_and_links_cannot_be_silently_duplicated():
    with pytest.raises(ValueError, match="source_id"):
        validate_records([output()], [source(), source()], [link()])
    with pytest.raises(ValueError, match="duplicate"):
        validate_records([output()], [source()], [link(), link()])


def test_url_redaction_preserves_public_identity_and_original_encoding():
    signed = "https://example.org/a?versionId=v%2B1&X-Amz-Signature=secret&partNumber=2&token=secret"
    assert sanitize_asset_url(signed) == "https://example.org/a?versionId=v%2B1&partNumber=2"
    azure = "https://example.org/a?versionId=v1&sv=2025&sp=r&sig=secret"
    assert sanitize_asset_url(azure) == "https://example.org/a?versionId=v1"
    with pytest.raises(ValueError, match="userinfo"):
        sanitize_asset_url("https://user:secret@127.0.0.1/a")
    with pytest.raises(ValueError, match="credential"):
        validate_records([output()], [source(href=signed)], [])
    validate_records([output()], [source(href=sanitize_asset_url(signed))], [])


def test_evidence_paths_and_nonfinite_declared_values_are_rejected():
    with pytest.raises(ValueError):
        validate_records([output(evidence_ref="../private.json")], [], [])
    with pytest.raises(ValueError):
        validate_records([output()], [source(declared_offset=float("nan"))], [])


def test_read_rejects_retyped_or_extra_parquet_columns(tmp_path):
    write_tables(tmp_path, [output()], [], [])
    table = pq.read_table(tmp_path / "outputs.parquet")
    pq.write_table(table.append_column("undocumented", pa.array([1])), tmp_path / "outputs.parquet")
    with pytest.raises(ValueError, match="schema"):
        read_tables(tmp_path)


def test_records_are_not_mutated():
    records = ([output()], [source()], [link()])
    before = deepcopy(records)
    validate_records(*records)
    data_revision(records[0], {})
    assert records == before


def test_public_numeric_declarations_roundtrip_without_becoming_applied(tmp_path):
    row = source(declared_scale=0.0001, declared_offset=-0.1)
    write_tables(tmp_path, [output()], [row], [link()])
    assert read_tables(tmp_path)[1] == [row]
    assert "applied_offset" not in read_tables(tmp_path)[1][0]


def test_group_aliases_cannot_hide_duplicate_locations():
    with pytest.raises(ValueError, match="location"):
        validate_records([output(zarr_group="r20m"),
                          output(output_id="out-B", zarr_group="r20m/.")], [], [])


def test_credential_query_is_not_a_valid_evidence_path():
    with pytest.raises(ValueError):
        validate_records([output(evidence_ref="evidence/item.json?token=secret")], [], [])


@pytest.mark.parametrize("change", [None, "unit_conflict", "applied_scale", "private_tag", "duplicate_input"])
def test_optional_source_declarations_preserve_raw_reads_and_reject_false_claims(change):
    from test_provenance_production import _numeric_payload
    source = {
        "source_id": "source-a", "variable": "VV_dB", "input_index": 0,
        "array_present": True, "evidence_status": "source_declared",
        "profile_nodata": {"nonfinite": "nan"},
        "asset": {"band": 1, "dtype": "float32", "unit": "1", "scale": 2.0, "offset": 3.0,
                  "scale_offset_basis": "rasterio_metadata_or_default", "tags": {"dataset": {}, "band": {}}},
        "read": {"method": "rasterio.read", "band": 1, "masked": False,
                 "output_dtype": "float32", "scale_offset_applied": False},
    }
    record = _numeric_payload()
    record["input"]["sources"] = [source]
    if change == "unit_conflict":
        source["asset"]["unit"] = "dB"
    elif change == "applied_scale":
        source["read"]["scale_offset_applied"] = True
    elif change == "private_tag":
        source["asset"]["tags"]["dataset"]["credential"] = "must-not-be-exported"
    elif change == "duplicate_input":
        record["input"]["sources"].append(deepcopy(source))
    if change:
        with pytest.raises(ValueError, match="source declaration"):
            validate_numeric_processing(record)
    else:
        validate_numeric_processing(record)
        assert record["input"]["actual_domain"] is None


@pytest.mark.parametrize("parameters, valid", [
    ({"mode": "direct_copy", "resampling": None}, True),
    ({"mode": "direct_copy", "resampling": None, "input_index": 0,
      "src_nodata": {"nonfinite": "nan"}, "nodata_policy": "mask_source_nodata"}, True),
    ({"mode": "prealign_reproject", "resampling": "nearest", "input_index": 1,
      "src_nodata": None, "nodata_policy": "rasterio_src_nodata"}, True),
    ({"mode": "legacy_full_mosaic_fallback", "resampling": "nearest",
      "validity_policy": "legacy_helper_not_instrumented"}, True),
    ({"mode": "direct_copy", "resampling": None, "input_index": True}, False),
    ({"mode": "direct_copy", "resampling": None, "input_index": -1}, False),
    ({"mode": "direct_copy", "resampling": None, "src_nodata": "9999"}, False),
    ({"mode": "direct_copy", "resampling": None, "src_nodata": {"nonfinite": "unknown"}}, False),
    ({"mode": "direct_copy", "resampling": None, "nodata_policy": "rasterio_src_nodata"}, False),
    ({"mode": "window_reproject", "resampling": "nearest", "nodata_policy": "mask_source_nodata"}, False),
    ({"mode": "legacy_full_mosaic_fallback", "resampling": "nearest",
      "nodata_policy": "mask_source_nodata"}, False),
])
def test_alignment_source_metadata_is_optional_but_validated_when_present(parameters, valid):
    from test_provenance_production import _numeric_payload
    record = _numeric_payload()
    record["steps"].insert(0, dict(operation="grid_alignment", variable="VV_dB",
                                  input_domain="linear_power", output_domain="linear_power",
                                  parameters=parameters, status="executed", calls=1))
    if valid:
        validate_numeric_processing(record)
    else:
        with pytest.raises(ValueError, match="alignment"):
            validate_numeric_processing(record)
