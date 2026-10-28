"""Real tiny-store checks for the restricted P3 monthly adapter."""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pytest
from rasterio.transform import Affine

zarr = pytest.importorskip("zarr")

from s1grits import provenance_monthly as pm
from s1grits.provenance_contract import read_tables


def _inputs(root, *, month="2025-01"):
    transform = Affine(30, 0, 500000, 0, -30, 8500000)
    dates = [pd.Timestamp(month + day + "T00:00:00Z") for day in ("-05", "-15", "-25")]
    profile = {"crs": "EPSG:32717", "transform": transform, "height": 3,
               "width": 4, "dtype": "float32", "nodata": np.nan}
    arrays = [np.arange(1, 13, dtype=np.float32).reshape(3, 4) + index for index in range(3)]
    kwargs = dict(
        mgrs_tile_id="17MPU", direction_label="ASCENDING", tile_dir=root / "17MPU",
        final_vv=arrays, prof_vv=[dict(profile) for _ in dates],
        final_vh=[array * np.float32(0.5) for array in arrays],
        prof_vh=[dict(profile) for _ in dates], clean_dates=dates,
        target_crs="EPSG:32717", target_res=30.0,
        generate_cog=False, generate_preview=False, chunk_y=2, chunk_x=2, cog_block=16,
        on_time_conflict="skip", monthly_cfg={"composite_method": "median"},
        processing_level="ARDC", transform=transform, width=4, height=3,
        x_coords=500000 + (np.arange(4) + 0.5) * 30,
        y_coords=8500000 - (np.arange(3) + 0.5) * 30,
        tile_clip=False, track_token="18", valid_clean_indices={0, 1},
    )
    sources = [
        {"source_id": f"asset-{month}-{i}-{band}", "collection": "OPERA_L2_RTC-S1",
         "product_id": f"declared-product-{month}-{i}", "asset_key": band,
         "href": f"https://example.org/rtc/{month}/{i}_{band}.tif",
         "source_sha256": None, "declared_scale": None, "declared_offset": None,
         "evidence_status": "source_declared", "evidence_ref": None}
        for i in range(3) for band in ("VV", "VH")
    ]
    bindings = [{band: f"asset-{month}-{i}-{band}" for band in ("VV", "VH")} for i in range(3)]
    return kwargs, sources, bindings


def _write(root, run="run-one", *, inputs=None):
    kwargs, sources, bindings = inputs or _inputs(root)
    return pm.write_tracked_monthly(root, "pilot-s1", run, writer_kwargs=kwargs,
                                   sources=sources, source_indices=bindings)


def _store(root):
    return next(root.glob("17MPU/smonthly_ASCENDING/zarr/*.zarr"))


def _tables(root):
    pointer = json.loads((root / "provenance/current.json").read_text(encoding="utf-8"))
    return read_tables(root / "provenance/releases" / pointer["metadata_release_id"])


def _receipt(root, run="run-one"):
    return json.loads((root / "provenance/runs" / run / "receipt.json").read_text(encoding="utf-8"))


def _snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and path.name != ".s1grits.lock"}


def test_real_writer_values_match_and_qc_excluded_is_not_used(tmp_path):
    tracked_root, native_root = tmp_path / "tracked", tmp_path / "native"
    kwargs, sources, bindings = _inputs(tracked_root)
    result = _write(tracked_root, inputs=(kwargs, sources, bindings))
    native_kwargs = {**kwargs, "tile_dir": native_root / "17MPU"}
    pm._write_smonthly_one_track(**native_kwargs)
    tracked = zarr.open_group(str(_store(tracked_root)), mode="r")
    native = zarr.open_group(str(_store(native_root)), mode="r")
    for name in ("VV_dB", "VH_dB", "n_obs", "time", "x", "y"):
        np.testing.assert_array_equal(tracked[name][:], native[name][:])
    outputs, declared, links = _tables(tracked_root)
    used = {link["source_id"] for link in links if link["role"] == "used"}
    assert used == set(bindings[0].values()) | set(bindings[1].values())
    assert not used & set(bindings[2].values())
    assert len(result["catalog_records"]) == len(outputs) == 1
    assert outputs[0]["generation_id"]
    assert not (tracked_root / "catalog.parquet").exists()
    assert not (tracked_root / "17MPU/catalog.parquet").exists()


def test_numeric_record_matches_even_median_and_preaggregation_validity_mask(tmp_path):
    tracked_root, native_root = tmp_path / "tracked", tmp_path / "native"
    kwargs, sources, bindings = _inputs(tracked_root)
    first = np.array([[1, 0, -1, np.nan], [0, -2, np.nan, 4], [0, -5, np.nan, 2]], dtype="f4")
    second = np.array([[9, 9, 9, 9], [0, -8, np.nan, 16], [np.nan, 5, np.nan, 8]], dtype="f4")
    kwargs["final_vv"][:2] = [first, second]
    kwargs["final_vh"][:2] = [first * np.float32(0.5), second * np.float32(0.5)]
    kwargs["monthly_cfg"]["composite_method"] = "nanmedian"
    _write(tracked_root, inputs=(kwargs, sources, bindings))
    pm._write_smonthly_one_track(**{**kwargs, "tile_dir": native_root / "17MPU"})
    tracked = zarr.open_group(str(_store(tracked_root)), mode="r")
    native = zarr.open_group(str(_store(native_root)), mode="r")
    expected_linear = np.array([[5, 9, 9, 9], [np.nan, np.nan, np.nan, 10],
                                [np.nan, 5, np.nan, 5]], dtype="f4")
    for variable, multiplier in (("VV_dB", 1), ("VH_dB", 0.5)):
        expected = 10 * np.log10(expected_linear * multiplier)
        np.testing.assert_allclose(tracked[variable][0], expected, rtol=1e-6, equal_nan=True)
        np.testing.assert_array_equal(tracked[variable][:], native[variable][:])
    np.testing.assert_array_equal(tracked["n_obs"][0], [[2, 1, 1, 1], [0, 0, 0, 2], [0, 1, 0, 2]])
    np.testing.assert_array_equal(tracked["n_obs"][:], native["n_obs"][:])
    assert not np.isclose(tracked["VV_dB"][0, 0, 0], (10 * np.log10(1) + 10 * np.log10(9)) / 2)
    receipt = _receipt(tracked_root)
    numeric = receipt["numeric_processing"]
    assert numeric["schema_version"] == 1
    assert numeric["scope"] == "monthly_writer_input_to_zarr"
    assert numeric["input"]["actual_domain"] is None
    assert numeric["input"]["operator_expected_domain"] == "linear_power"
    assert numeric["input"]["unknown_reason"]
    for name in ("VV_dB", "VH_dB"):
        output = numeric["output"][name]
        assert output["domain"] == "db" and output["unit"] == "dB"
        assert output["physical_quantity"] is None
        assert output["dtype"] == "float32" and output["nodata"] == "NaN"
        assert output["applied_scale"] == 1 and output["applied_offset"] == 0
    steps = numeric["steps"]
    assert steps
    assert all(step["status"] in {"executed", "not_applied"} for step in steps)
    assert all(step["calls"] > 0 for step in steps if step["status"] == "executed")
    vv = [step for step in steps if step["variable"] == "VV_dB"]
    operations = [step["operation"] for step in vv]
    expected_order = ["input_filter", "monthly_composite", "linear_to_db", "storage_cast_and_write"]
    assert [operations.index(operation) for operation in expected_order] == sorted(
        operations.index(operation) for operation in expected_order)
    filtering = next(step for step in vv if step["operation"] == "input_filter")
    assert filtering["parameters"]["nonfinite"] == filtering["parameters"]["nonpositive"] == "NaN"
    composite = next(step for step in vv if step["operation"] == "monthly_composite")
    assert composite["parameters"]["method"] == "nanmedian"
    assert composite["parameters"]["backend"] in {"numpy", "bottleneck"}
    assert composite["parameters"]["axis"] == 0
    assert composite["parameters"]["nan_policy"] == "ignore_nan"
    logarithm = next(step for step in vv if step["operation"] == "linear_to_db")
    assert logarithm["parameters"]["formula"] == "10*log10(x)"
    assert logarithm["parameters"]["nonpositive"] == "NaN"
    assert numeric["output"]["n_obs"]["domain"] == "count"
    assert numeric["output"]["n_obs"]["dtype"] == "uint8"
    assert numeric["output"]["n_obs"]["nodata"] == 0


def test_missing_vh_does_not_claim_executed_vh_composite(tmp_path):
    root = tmp_path / "tracked"
    kwargs, sources, bindings = _inputs(root)
    kwargs["final_vh"] = [None, None, None]
    _write(root, inputs=(kwargs, sources, bindings))
    group = zarr.open_group(str(_store(root)), mode="r")
    assert np.isnan(group["VH_dB"][:]).all()
    numeric = _receipt(root)["numeric_processing"]
    assert numeric["input"]["actual_domain"] is None
    assert numeric["steps"]
    vh = [step for step in numeric["steps"] if step["variable"] == "VH_dB"]
    assert not any(step["operation"] == "monthly_composite" and step["status"] == "executed" for step in vh)
    assert any(step["operation"] == "fill_missing" and step["status"] == "executed" for step in vh)
    assert any(step["operation"] == "storage_cast_and_write" and step["status"] == "executed" for step in vh)


def test_only_arrays_admitted_to_stack_are_used_and_alignment_preserves_binding(tmp_path):
    root = tmp_path / "tracked"
    kwargs, sources, bindings = _inputs(root)
    kwargs["final_vh"][1] = None
    kwargs["prof_vv"][0]["transform"] *= Affine.translation(0.25, 0.25)
    _write(root, inputs=(kwargs, sources, bindings))
    links = _tables(root)[2]
    used = {link["source_id"] for link in links if link["role"] == "used"}
    assert bindings[0]["VV"] in used
    assert bindings[1]["VV"] in used
    assert bindings[0]["VH"] in used
    assert bindings[1]["VH"] not in used


def test_completed_run_does_not_call_writer_again(tmp_path, monkeypatch):
    root = tmp_path / "tracked"
    _write(root)
    before = _snapshot(_store(root))
    receipt_before = _receipt(root)
    assert receipt_before["numeric_processing"]["steps"]
    from s1grits.scenes import smonthly_writer
    def forbidden(**kwargs):
        pytest.fail("completed recovery must not call the pixel writer")
    monkeypatch.setattr(smonthly_writer, "_write_smonthly_month_zarr_blockwise", forbidden)
    _write(root)
    assert _snapshot(_store(root)) == before
    assert _receipt(root) == receipt_before


def test_changed_request_cannot_reuse_run_id(tmp_path):
    root = tmp_path / "tracked"
    _write(root)
    before = _snapshot(root)
    kwargs, sources, bindings = _inputs(root)
    kwargs["monthly_cfg"]["composite_method"] = "mean"
    with pytest.raises(ValueError, match="request|fingerprint|different|changed"):
        _write(root, inputs=(kwargs, sources, bindings))
    assert _snapshot(root) == before


def test_existing_skip_is_read_only_and_does_not_create_generation(tmp_path):
    root = tmp_path / "tracked"
    _write(root)
    before = _snapshot(root)
    result = _write(root, run="skip-again")
    assert result["status"] == "skipped"
    assert _snapshot(root) == before


def test_append_and_same_time_replace_keep_current_locations_correct(tmp_path):
    root = tmp_path / "tracked"
    _write(root)
    old = _tables(root)[0][0]
    original_receipt = _receipt(root)
    _write(root, run="run-feb", inputs=_inputs(root, month="2025-02"))
    feb_receipt = _receipt(root, "run-feb")
    kwargs, sources, bindings = _inputs(root)
    kwargs["on_time_conflict"] = "overwrite"
    kwargs["monthly_cfg"]["composite_method"] = "mean"
    kwargs["final_vv"][0] = kwargs["final_vv"][0] + np.float32(2)
    _write(root, run="run-replace", inputs=(kwargs, sources, bindings))
    outputs = _tables(root)[0]
    current = next(row for row in outputs if row["output_id"] == old["output_id"])
    assert current["generation_id"] != old["generation_id"]
    assert current["time_value_utc"] == old["time_value_utc"]
    assert current["time_index"] == 1
    assert len(outputs) == 2
    assert _receipt(root) == original_receipt
    assert _receipt(root, "run-feb") == feb_receipt
    replacement = _receipt(root, "run-replace")
    assert replacement["generation_id"] == current["generation_id"]
    assert replacement["numeric_processing"] != original_receipt["numeric_processing"]
    pointer = json.loads((root / "provenance/current.json").read_text())
    release = root / "provenance/releases" / pointer["metadata_release_id"]
    for row in outputs:
        saved = json.loads((release / row["evidence_ref"]).read_text())
        assert saved["generation_id"] == row["generation_id"]
        assert saved["numeric_processing"] == _receipt(root, row["run_id"])["numeric_processing"]


def test_rejects_empty_qc_and_existing_catalog_before_pending(tmp_path):
    root = tmp_path / "tracked"
    kwargs, sources, bindings = _inputs(root)
    kwargs["valid_clean_indices"] = set()
    with pytest.raises(ValueError, match="QC"):
        _write(root, inputs=(kwargs, sources, bindings))
    assert not root.exists()
    root.mkdir()
    (root / "catalog.parquet").write_bytes(b"preserve")
    with pytest.raises(ValueError, match="Catalog"):
        _write(root)
    assert _snapshot(root) == {"catalog.parquet": hashlib.sha256(b"preserve").hexdigest()}


def test_grid_mismatch_rejected_before_pending(tmp_path):
    root = tmp_path / "tracked"
    _write(root)
    before = _snapshot(root)
    kwargs, sources, bindings = _inputs(root, month="2025-02")
    kwargs["x_coords"] = kwargs["x_coords"] + 30
    with pytest.raises(ValueError, match="grid"):
        _write(root, run="bad-grid", inputs=(kwargs, sources, bindings))
    assert _snapshot(root) == before


def test_strided_input_fingerprint_is_content_based():
    array = np.arange(60, dtype=np.float32).reshape(5, 12)[:, ::2]
    assert pm._array_identity(array) == pm._array_identity(np.ascontiguousarray(array))


def test_pending_precedes_store_initialization(tmp_path, monkeypatch):
    from s1grits.scenes import smonthly_writer
    root = tmp_path / "tracked"
    original = smonthly_writer._init_zarr_2band
    def checked(*args, **kwargs):
        pending = json.loads((root / "provenance/pending/run-one.json").read_text())
        assert pending["phase"] == "prepared"
        assert not _store_if_present(root)
        return original(*args, **kwargs)
    monkeypatch.setattr(smonthly_writer, "_init_zarr_2band", checked)
    _write(root)
    assert not list((root / "provenance/pending").glob("*.json"))


def _store_if_present(root):
    return list(root.glob("17MPU/smonthly_ASCENDING/zarr/*.zarr"))


def test_all_none_qc_arrays_rejected_before_replace(tmp_path):
    root = tmp_path / "tracked"
    _write(root)
    before = _snapshot(root)
    kwargs, sources, bindings = _inputs(root)
    kwargs["final_vv"] = [None, None, None]
    kwargs["on_time_conflict"] = "overwrite"
    with pytest.raises(ValueError, match="no QC-passing VV"):
        _write(root, run="no-vv", inputs=(kwargs, sources, bindings))
    assert _snapshot(root) == before
    assert not (root / "provenance/pending/no-vv.json").exists()


@pytest.mark.parametrize("phase", ["writing", "prepared"])
def test_existing_month_without_receipt_cannot_skip_pending(tmp_path, monkeypatch, phase):
    from s1grits.scenes import smonthly_writer
    root = tmp_path / "tracked"
    kwargs, sources, bindings = _inputs(root)
    def interrupted(*args, **kwargs):
        raise RuntimeError("injected interruption")
    with monkeypatch.context() as patch:
        if phase == "writing":
            patch.setattr(pm.ProductionRun, "commit", interrupted)
        else:
            patch.setattr(smonthly_writer, "_init_zarr_2band", interrupted)
        with pytest.raises(RuntimeError, match="injected interruption"):
            _write(root, inputs=(kwargs, sources, bindings))
    if phase == "prepared":
        # A bypass writer changed the store while the earlier intent remained.
        smonthly_writer._write_smonthly_one_track(**kwargs)
    pending = root / "provenance/pending/run-one.json"
    assert json.loads(pending.read_text())["phase"] == phase
    assert not (root / "provenance/runs/run-one/receipt.json").exists()
    before = _snapshot(root)
    with pytest.raises(ValueError, match="pending.*cannot be skipped"):
        _write(root, inputs=(kwargs, sources, bindings))
    assert _snapshot(root) == before


@pytest.mark.parametrize("change", ["extra_time_band", "child_group", "geometry_identity", "product_identity"])
def test_unsupported_existing_layout_or_identity_is_rejected_without_mutation(tmp_path, change):
    root = tmp_path / "tracked"
    _write(root)
    group = zarr.open_group(str(_store(root)), mode="r+")
    if change == "extra_time_band":
        group.create_array("unexpected", data=np.zeros((1, 3, 4), dtype=np.float32))
    elif change == "child_group":
        group.create_group("unexpected")
    elif change == "geometry_identity":
        group.attrs["geometry_group_id"] = "17MPU_ASCENDING_TK999"
    else:
        group.attrs["product_type"] = "scenes"
    before = _snapshot(root)
    with pytest.raises(ValueError, match="layout|identity"):
        _write(root, run="rejected-layout", inputs=_inputs(root, month="2025-02"))
    assert _snapshot(root) == before
    assert not (root / "provenance/pending/rejected-layout.json").exists()


@pytest.mark.parametrize("field,value", [("crs", "invalid-crs"), ("transform", [0, 0, 0, 0, 0, 0])])
def test_invalid_profile_rejected_before_initialization(tmp_path, field, value):
    root = tmp_path / "tracked"
    kwargs, sources, bindings = _inputs(root)
    kwargs["prof_vv"][0][field] = value
    with pytest.raises(ValueError, match="CRS|transform"):
        _write(root, inputs=(kwargs, sources, bindings))
    assert not root.exists()


@pytest.mark.parametrize("status", ["execution_recorded", "artifact_observed"])
def test_source_evidence_cannot_claim_upstream_verification(tmp_path, status):
    root = tmp_path / "tracked"
    kwargs, sources, bindings = _inputs(root)
    sources[0]["evidence_status"] = status
    with pytest.raises(ValueError, match="source_declared or unknown"):
        _write(root, inputs=(kwargs, sources, bindings))
    assert not root.exists()


@pytest.mark.parametrize("operator", ["_monthly_composite_block", "_linear_to_db", "_write_smonthly_block_bands"])
def test_failed_numeric_operator_never_publishes_successful_receipt(tmp_path, monkeypatch, operator):
    from s1grits.scenes import smonthly_writer
    root = tmp_path / "failed"
    original = getattr(smonthly_writer, operator)
    def fail(*args, **kwargs):
        pending = root / "provenance/pending/run-one.json"
        if pending.exists() and json.loads(pending.read_text())["phase"] == "writing":
            raise RuntimeError("numeric operator failed")
        return original(*args, **kwargs)
    monkeypatch.setattr(smonthly_writer, operator, fail)
    with pytest.raises(RuntimeError, match="numeric operator failed"):
        _write(root)
    assert not (root / "provenance/runs/run-one/receipt.json").exists()
    assert not (root / "provenance/current.json").exists()
    pending = json.loads((root / "provenance/pending/run-one.json").read_text())
    assert pending["phase"] == "writing"


def test_numeric_record_follows_multitrack_priority_and_actual_clip(tmp_path, monkeypatch):
    from shapely.geometry import box
    from s1grits.scenes import smonthly_writer
    root = tmp_path / "tracks"
    kwargs, sources, bindings = _inputs(root)
    kwargs["track_token"] = "18-19"
    kwargs["df_batch"] = pd.DataFrame({
        "acq_dt": kwargs["clean_dates"], "track_number": [18, 19, 18],
        "jpl_burst_id": ["a", "b", "c"], "opera_id": ["a", "b", "c"], "pass_id": [1, 2, 3],
    })
    kwargs["tile_clip"] = True
    monkeypatch.setattr(smonthly_writer, "_prepare_block_clip_geom",
                        lambda *args: box(500000, 8499910, 500060, 8500000))
    _write(root, inputs=(kwargs, sources, bindings))
    store = zarr.open_group(str(_store(root)), mode="r")
    assert np.isnan(store["VV_dB"][0, :, 2:]).all()
    assert (store["n_obs"][0, :, 2:] == 0).all()
    np.testing.assert_allclose(store["VV_dB"][0, :, :2],
                               10 * np.log10(kwargs["final_vv"][0][:, :2]), rtol=1e-6)
    receipt = json.loads((root / "provenance/runs/run-one/receipt.json").read_text())
    steps = receipt["numeric_processing"]["steps"]
    priority = [s for s in steps if s["operation"] == "track_priority_mosaic"]
    assert priority and all(s["parameters"]["order"] == [18, 19] for s in priority)
    assert all(s["status"] == "executed" for s in steps if s["operation"] == "spatial_clip")


def test_numeric_record_survives_real_reader_and_metadata_export(tmp_path):
    resolver = pytest.importorskip("grits_resolver.provenance")
    from shutil import copytree
    root = tmp_path / "tracked"
    _write(root)
    view = resolver.ProvenanceView(root, required=True)
    result = view.explain(time_index=0)["numeric_processing"]
    row = _tables(root)[0][0]
    receipt = json.loads((root / "provenance/runs/run-one/receipt.json").read_text())
    assert result == [{"output_id": row["output_id"], "generation_id": row["generation_id"],
                       "record": receipt["numeric_processing"], "unknown_reason": None}]
    exported = tmp_path / "exported"
    copytree(root / "provenance", exported / "provenance")
    exported_view = resolver.ProvenanceView(root, metadata_root=exported, required=True)
    assert exported_view.explain(time_index=0)["numeric_processing"] == result


def test_sparse_multitrack_records_empty_blocks_before_first_composite(tmp_path):
    root = tmp_path / "sparse"
    kwargs, sources, bindings = _inputs(root)
    kwargs.update(width=8, x_coords=500000 + (np.arange(8) + 0.5) * 30, track_token="18-19")
    for band in ("vv", "vh"):
        kwargs["final_" + band] = [array[:2, :2] for array in kwargs["final_" + band]]
        for profile in kwargs["prof_" + band]:
            profile.update(width=2, height=2, transform=kwargs["transform"] * Affine.translation(6, 1))
    kwargs["df_batch"] = pd.DataFrame({
        "acq_dt": kwargs["clean_dates"], "track_number": [18, 19, 18],
        "jpl_burst_id": ["a", "b", "c"], "opera_id": ["a", "b", "c"], "pass_id": [1, 2, 3],
    })
    _write(root, inputs=(kwargs, sources, bindings))
    store = zarr.open_group(str(_store(root)), mode="r")
    assert np.isnan(store["VV_dB"][0, :, :4]).all()
    assert np.isfinite(store["VV_dB"][0, 1:, 6:]).all()
    receipt = json.loads((root / "provenance/runs/run-one/receipt.json").read_text())
    operations = [s["operation"] for s in receipt["numeric_processing"]["steps"] if s["variable"] == "VV_dB"]
    assert operations.index("fill_missing") < operations.index("linear_to_db") < operations.index("monthly_composite")


def test_source_profiles_preserve_distinct_nodata_and_unknown_loader_state(tmp_path):
    root = tmp_path / "nodata"
    kwargs, sources, bindings = _inputs(root)
    kwargs["prof_vv"][0]["nodata"] = 9999
    kwargs["prof_vv"][1]["nodata"] = None
    kwargs["prof_vv"][2].pop("nodata")
    kwargs["valid_clean_indices"] = {0, 1, 2}
    _write(root, inputs=(kwargs, sources, bindings))
    records = _receipt(root)["numeric_processing"]["input"]["sources"]
    vv = [row for row in records if row["variable"] == "VV_dB"]
    assert [row["source_id"] for row in vv] == [binding["VV"] for binding in bindings]
    assert vv[0]["profile_nodata"] == 9999
    assert vv[1]["profile_nodata"] is None
    assert "profile_nodata" not in vv[2]
    assert all("read" not in row and "asset" not in row for row in records)
    vh = next(row for row in records if row["variable"] == "VH_dB")
    assert vh["profile_nodata"] == {"nonfinite": "nan"}
    alignment = [step["parameters"] for step in _receipt(root)["numeric_processing"]["steps"]
                 if step["operation"] == "grid_alignment" and step["variable"] == "VV_dB"]
    assert any(step["input_index"] == 0 and step["src_nodata"] == 9999
               and step["nodata_policy"] == "mask_source_nodata" for step in alignment)


@pytest.mark.parametrize("unit,domain", [("dB", None), (None, "amplitude"), (None, "db")])
def test_conflicting_source_declaration_rejected_before_intent(tmp_path, unit, domain):
    root = tmp_path / "conflict"
    kwargs, sources, bindings = _inputs(root)
    kwargs["prof_vv"][0]["grits_source"] = {
        "asset": {"band": 1, "dtype": "float32", "unit": unit, "scale": 1.0, "offset": 0.0,
                  "scale_offset_basis": "rasterio_metadata_or_default",
                  "tags": {"dataset": {}, "band": {"data_domain": domain} if domain else {}}},
        "read": {"method": "rasterio.read", "band": 1, "masked": False,
                 "output_dtype": "float32", "scale_offset_applied": False},
    }
    with pytest.raises(ValueError, match="source declaration.*linear_power"):
        _write(root, inputs=(kwargs, sources, bindings))
    assert not list(root.glob("provenance/pending/*.json"))
    assert not list(root.glob("provenance/runs/*/intent.json"))
    assert not list(root.glob("17MPU/smonthly_ASCENDING/zarr/*.zarr"))
    _write(root, run="valid-next")


@pytest.mark.parametrize("invalid", ["nan", "zero", "outside"])
@pytest.mark.parametrize("multitrack", [False, True])
def test_invalid_overwrite_has_no_pending_and_next_month_can_run(tmp_path, invalid, multitrack):
    root = tmp_path / "preflight"
    kwargs, sources, bindings = _inputs(root)
    if multitrack:
        kwargs["track_token"] = "18-19"
        kwargs["df_batch"] = pd.DataFrame({
            "acq_dt": kwargs["clean_dates"], "track_number": [18, 19, 18],
            "jpl_burst_id": ["a", "b", "c"], "opera_id": ["a", "b", "c"], "pass_id": [1, 2, 3],
        })
    _write(root, inputs=(kwargs, sources, bindings))
    before_pixels = _snapshot(_store(root))
    before_current = (root / "provenance/current.json").read_bytes()
    kwargs["on_time_conflict"] = "overwrite"
    if invalid == "outside":
        for profile in kwargs["prof_vv"]:
            profile["transform"] *= Affine.translation(1000, 1000)
    else:
        kwargs["final_vv"] = [np.full((3, 4), np.nan if invalid == "nan" else 0, dtype="f4") for _ in bindings]
    with pytest.raises(ValueError, match="no valid output"):
        _write(root, run="invalid", inputs=(kwargs, sources, bindings))
    assert _snapshot(_store(root)) == before_pixels
    assert (root / "provenance/current.json").read_bytes() == before_current
    assert not list(root.glob("provenance/pending/*.json"))
    assert not (root / "provenance/runs/invalid/intent.json").exists()
    next_kwargs, next_sources, next_bindings = _inputs(root, month="2025-02")
    if multitrack:
        next_kwargs["track_token"] = "18-19"
        next_kwargs["df_batch"] = kwargs["df_batch"].assign(acq_dt=next_kwargs["clean_dates"])
    _write(root, run="valid-next-month", inputs=(next_kwargs, next_sources, next_bindings))
    assert len(_tables(root)[0]) == 2


def test_numeric_preflight_error_does_not_leave_a_pending_write(tmp_path, monkeypatch):
    from s1grits.scenes import smonthly_writer
    root = tmp_path / "preflight-error"
    def fail(*args, **kwargs):
        raise RuntimeError("preflight calculation failed")
    monkeypatch.setattr(smonthly_writer, "_monthly_composite_block", fail)
    with pytest.raises(RuntimeError, match="preflight calculation failed"):
        _write(root)
    assert not list(root.glob("provenance/pending/*.json"))
    assert not list(root.glob("provenance/runs/*/intent.json"))
    assert not list(root.glob("17MPU/smonthly_ASCENDING/zarr/*.zarr"))


def test_original_and_reprojected_nodata_remain_distinguishable(tmp_path):
    root = tmp_path / "aligned-nodata"
    kwargs, sources, bindings = _inputs(root)
    kwargs["prof_vv"][0]["nodata"] = 9999
    kwargs["prof_vv"][0]["transform"] *= Affine.translation(0.25, 0.25)
    kwargs["final_vv"][0][0, 0] = 9999
    _write(root, inputs=(kwargs, sources, bindings))
    numeric = _receipt(root)["numeric_processing"]
    original = next(row for row in numeric["input"]["sources"]
                    if row["variable"] == "VV_dB" and row["input_index"] == 0)
    assert original["source_id"] == bindings[0]["VV"]
    assert original["profile_nodata"] == 9999
    alignment = [step["parameters"] for step in numeric["steps"]
                 if step["operation"] == "grid_alignment" and step["variable"] == "VV_dB"
                 and step["parameters"].get("input_index") == 0]
    assert any(step["nodata_policy"] == "rasterio_src_nodata" and step["src_nodata"] == 9999
               for step in alignment)
    assert any(step["nodata_policy"] == "mask_source_nodata"
               and step["src_nodata"] == {"nonfinite": "nan"} for step in alignment)
