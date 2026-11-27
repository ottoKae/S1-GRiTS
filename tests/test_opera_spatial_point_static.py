"""Static enrichment uses real HTTP COGs without revising dynamic observations."""
import hashlib
import json

import pandas as pd
import pytest

from test_opera_spatial_points import cog, inputs, product, selected
from test_opera_spatial_points import endpoint as endpoint
from s1grits.opera_spatial.inventory import digest
from s1grits.opera_spatial import point_static
from s1grits.opera_spatial.points import run_points
from s1grits.opera_spatial.point_static import (
    plan_static, read_point_static_context, run_static,
)


ROLES = ["lia", "number_of_looks", "rtc_anf_beta0", "rtc_anf_sigma0"]


def static_product(endpoint, burst="A", *, looks=12.5, beta=0, missing=(), shift=0,
                   revision=1):
    pid = f"STATIC_{burst}_v{revision}"
    assets = {}
    for role, value in (("lia", 45), ("number_of_looks", looks),
                        ("rtc_anf_beta0", beta), ("rtc_anf_sigma0", .75)):
        if role in missing:
            continue
        name = f"/{pid}_{role}.tif"
        body = cog(value, burst=burst, shift=shift)
        endpoint["bodies"][name] = body
        assets[role] = {"asset_id": digest(pid + role), "product_id": pid,
                        "role": role, "url": endpoint["url"] + name,
                        "filename": name[1:], "expected_bytes": len(body)}
    return {"product_id": pid, "burst_id": burst, "is_static": True,
            "acquisition_id": None, "acquisition_utc": None,
            "platform": "S1A", "direction": "ASCENDING", "relative_orbit": 84,
            "processing_version": "2.1.0", "product_version": None,
            "validity_start": "2014-04-03", "processing_date": "2023-12-04",
            "assets": assets}


def dynamic_hashes(output):
    paths = list((output / "points").rglob("*.parquet"))
    paths += [output / name for name in (
        "manifest.json", "products.parquet", "sources.parquet", "point_catalog.parquet")]
    return {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.exists()}


def samples(output):
    manifest = json.loads((output / "static/manifest.json").read_text(encoding="utf-8"))
    return pd.read_parquet(output / manifest["samples_path"])


def test_static_only_preserves_dynamic_files_and_replay_uses_cache(tmp_path, endpoint):
    config, features = inputs()
    dynamic = product(endpoint, "A", "e1", "2025-01-01")
    run_points(config, features, [dynamic], tmp_path)
    baseline = dynamic_hashes(tmp_path)
    config["static"] = {"layers": ROLES}
    static = [static_product(endpoint)]
    calls = len(endpoint["calls"])
    assert plan_static(config, features, tmp_path, static_products=static)["status"] == "planned"
    assert len(endpoint["calls"]) == calls

    result = run_static(config, features, tmp_path, static_products=static, repair=True)
    assert result["status"] == "complete" and result["http_requests"] > 0
    assert all(path.startswith("/STATIC_") for path in endpoint["calls"][calls:])
    assert dynamic_hashes(tmp_path) == baseline
    frame = read_point_static_context(tmp_path)
    row = frame.iloc[0]
    assert row.owner_static_product_id == row.selected_static_product_id == "STATIC_A_v1"
    assert row.selected_number_of_looks == pytest.approx(12.5)
    assert row.selected_rtc_anf_beta0 == 0
    assert row.selected_rtc_anf_beta0_status == "valid"
    assert row.vv == pytest.approx(.1)
    assert len(samples(tmp_path)) == 4

    calls = len(endpoint["calls"])
    again = run_static(config, features, tmp_path, static_products=static, repair=True)
    assert again["http_requests"] == 0 and again["downloaded_bytes"] == 0
    assert len(endpoint["calls"]) == calls and len(samples(tmp_path)) == 4
    pd.testing.assert_frame_equal(frame, read_point_static_context(tmp_path))
    assert dynamic_hashes(tmp_path) == baseline


def test_missing_static_role_does_not_invalidate_dynamic_value(tmp_path, endpoint):
    config, features = inputs()
    run_points(config, features, [product(endpoint, "A", "e1", "2025-01-01")], tmp_path)
    baseline = dynamic_hashes(tmp_path)
    config["static"] = {"layers": ROLES}
    result = run_static(config, features, tmp_path,
                        static_products=[static_product(endpoint, missing={"rtc_anf_beta0"})],
                        repair=True)
    assert result["status"] == "complete_with_gaps"
    row = read_point_static_context(tmp_path).iloc[0]
    assert row.status == "valid" and row.source_index == row.owner_index
    assert pd.isna(row.selected_rtc_anf_beta0)
    assert row.selected_rtc_anf_beta0_status == "missing_asset"
    assert row.selected_number_of_looks == pytest.approx(12.5)
    assert dynamic_hashes(tmp_path) == baseline


def test_repair_retries_only_the_failed_static_asset(tmp_path, endpoint):
    config, features = inputs("S1")
    a = product(endpoint, "A", "e1", "2025-01-01")
    b = product(endpoint, "B", "e1", "2025-01-01", .4)
    run_points(config, features, [a, b], tmp_path)
    baseline = dynamic_hashes(tmp_path)
    config["static"] = {"layers": ROLES}
    static = [static_product(endpoint, "A"), static_product(endpoint, "B", looks=20.25)]
    bad = "/STATIC_A_v1_rtc_anf_beta0.tif"
    endpoint["blocked"].add(bad)
    failed = run_static(config, features, tmp_path, static_products=static, repair=True)
    assert failed["status"] == "partial"
    row = read_point_static_context(tmp_path).iloc[0]
    assert row.burst_id == "A" and not row.fallback and row.status == "valid"
    assert row.selected_rtc_anf_beta0_status == "read_failed"
    assert dynamic_hashes(tmp_path) == baseline

    endpoint["blocked"].clear()
    calls = len(endpoint["calls"])
    repaired = run_static(config, features, tmp_path, static_products=static, repair=True)
    assert repaired["status"] == "complete"
    assert set(endpoint["calls"][calls:]) == {bad}
    assert read_point_static_context(tmp_path).iloc[0].selected_rtc_anf_beta0_status == "valid"
    assert dynamic_hashes(tmp_path) == baseline


def test_context_uses_current_selected_burst_after_dynamic_repair(tmp_path, endpoint):
    config, features = inputs("S1")
    a1 = product(endpoint, "A", "e1", "2025-01-01")
    b1 = product(endpoint, "B", "e1", "2025-01-01", .4)
    b2 = product(endpoint, "B", "e2", "2025-02-01", .5)
    run_points(config, features, [a1, b1, b2], tmp_path)
    extended = {**config, "static": {"layers": ROLES}}
    static = [static_product(endpoint, "A", looks=12.5),
              static_product(endpoint, "B", looks=30.25)]
    run_static(extended, features, tmp_path, static_products=static, repair=True)
    frame = read_point_static_context(tmp_path).sort_values("acquisition_utc")
    assert frame.owner_number_of_looks.tolist() == [12.5, 12.5]
    assert frame.selected_number_of_looks.tolist() == [12.5, 30.25]
    assert frame.selected_static_product_id.tolist() == ["STATIC_A_v1", "STATIC_B_v1"]

    a2 = product(endpoint, "A", "e2", "2025-02-01", .2)
    run_points(config, features, [a2], tmp_path, repair=True)
    calls = len(endpoint["calls"])
    current = read_point_static_context(tmp_path).sort_values("acquisition_utc")
    assert current.burst_id.tolist() == ["A", "A"]
    assert current.selected_number_of_looks.tolist() == [12.5, 12.5]
    assert len(endpoint["calls"]) == calls


def test_s0_gap_keeps_selected_static_values_empty(tmp_path, endpoint):
    config, features = inputs("S0")
    products = [product(endpoint, "A", "e1", "2025-01-01"),
                product(endpoint, "B", "e1", "2025-01-01", .4),
                product(endpoint, "B", "e2", "2025-02-01", .5)]
    run_points(config, features, products, tmp_path)
    config["static"] = {"layers": ROLES}
    run_static(config, features, tmp_path, static_products=[static_product(endpoint, "A"),
               static_product(endpoint, "B", looks=30.25)], repair=True)
    frame = read_point_static_context(tmp_path).sort_values("acquisition_utc")
    assert frame.status.tolist() == ["valid", "no_source"]
    assert frame.owner_number_of_looks.tolist() == [12.5, 12.5]
    assert pd.isna(frame.selected_number_of_looks.iloc[1])
    assert selected(tmp_path).status.tolist() == ["valid", "no_source"]


def test_static_asset_is_shared_by_multiple_geometry_groups(tmp_path, endpoint):
    config, features = inputs()
    first = product(endpoint, "A", "e1", "2025-01-01")
    other = product(endpoint, "A", "e2", "2025-02-01")
    other["processing_version"] = "2"
    run_points(config, features, [first, other], tmp_path)
    config["static"] = {"layers": ROLES}
    calls = len(endpoint["calls"])
    run_static(config, features, tmp_path, static_products=[static_product(endpoint)], repair=True)
    assert len(samples(tmp_path)) == 4
    assert len(set(endpoint["calls"][calls:])) == 4
    frame = read_point_static_context(tmp_path)
    assert frame.geometry_group.nunique() == 2
    assert frame.selected_number_of_looks.tolist() == [12.5, 12.5]


def test_static_grid_mismatch_is_reported_without_discarding_raw_value(tmp_path, endpoint):
    config, features = inputs()
    run_points(config, features, [product(endpoint, "A", "e1", "2025-01-01")], tmp_path)
    baseline = dynamic_hashes(tmp_path)
    config["static"] = {"layers": ROLES}
    run_static(config, features, tmp_path,
               static_products=[static_product(endpoint, looks=25.5, shift=.00003)], repair=True)
    row = read_point_static_context(tmp_path).iloc[0]
    assert row.selected_number_of_looks_grid_status == "grid_mismatch"
    raw = samples(tmp_path)
    assert raw.loc[raw.role == "number_of_looks", "value"].iloc[0] == pytest.approx(25.5)
    assert dynamic_hashes(tmp_path) == baseline


def test_frozen_static_product_does_not_switch_or_mix_missing_role(tmp_path, endpoint):
    config, features = inputs()
    old = static_product(endpoint, missing={"rtc_anf_beta0"})
    newer = static_product(endpoint, looks=99, beta=8, revision=2)
    run_points(config, features, [product(endpoint, "A", "e1", "2025-01-01")],
               tmp_path, static_products=[old])
    baseline = dynamic_hashes(tmp_path)
    config["static"] = {"layers": ROLES}
    calls = len(endpoint["calls"])
    result = run_static(config, features, tmp_path, static_products=[newer], repair=True)
    assert result["status"] == "complete_with_gaps"
    row = read_point_static_context(tmp_path).iloc[0]
    assert row.selected_static_product_id == old["product_id"]
    assert pd.isna(row.selected_rtc_anf_beta0) and pd.isna(row.selected_number_of_looks)
    assert len(endpoint["calls"]) == calls

    result = run_static(config, features, tmp_path, static_products=[old, newer], repair=True)
    assert result["status"] == "complete_with_gaps"
    row = read_point_static_context(tmp_path).iloc[0]
    assert row.selected_static_product_id == old["product_id"]
    assert row.selected_number_of_looks == pytest.approx(12.5)
    assert row.selected_rtc_anf_beta0_status == "missing_asset"
    assert all("_v2_" not in path for path in endpoint["calls"][calls:])
    assert dynamic_hashes(tmp_path) == baseline


def test_missing_reference_can_be_bound_when_static_becomes_available(tmp_path, endpoint):
    config, features = inputs()
    run_points(config, features, [product(endpoint, "A", "e1", "2025-01-01")], tmp_path)
    baseline = dynamic_hashes(tmp_path)
    config["static"] = {"layers": ROLES}
    calls = len(endpoint["calls"])
    absent = run_static(config, features, tmp_path, repair=True)
    assert absent["status"] == "complete_with_gaps" and absent["http_requests"] == 0
    assert pd.isna(read_point_static_context(tmp_path).iloc[0].selected_number_of_looks)
    assert len(endpoint["calls"]) == calls

    recovered = run_static(config, features, tmp_path,
                           static_products=[static_product(endpoint)], repair=True)
    assert recovered["status"] == "complete" and recovered["http_requests"] > 0
    row = read_point_static_context(tmp_path).iloc[0]
    assert row.selected_static_product_id == "STATIC_A_v1"
    assert row.selected_number_of_looks == pytest.approx(12.5)
    assert row.selected_rtc_anf_beta0_status == "valid"
    assert dynamic_hashes(tmp_path) == baseline


def test_failed_manifest_publish_preserves_previous_static_and_recovers_cache(
        tmp_path, endpoint, monkeypatch):
    config, features = inputs()
    run_points(config, features, [product(endpoint, "A", "e1", "2025-01-01")], tmp_path)
    baseline = dynamic_hashes(tmp_path)
    static = [static_product(endpoint)]
    config["static"] = {"layers": ["lia"]}
    run_static(config, features, tmp_path, static_products=static, repair=True)
    manifest_path = tmp_path / "static/manifest.json"
    old_manifest = manifest_path.read_bytes()
    old_context = read_point_static_context(tmp_path)
    old_samples = samples(tmp_path)
    config["static"] = {"layers": ROLES}
    original = point_static.atomic_write_json
    fail_next = True

    def fail_manifest_once(value, path):
        nonlocal fail_next
        if path == manifest_path and fail_next:
            fail_next = False
            raise OSError("Injected static manifest publication failure")
        return original(value, path)

    monkeypatch.setattr(point_static, "atomic_write_json", fail_manifest_once)
    with pytest.raises(OSError, match="publication failure"):
        run_static(config, features, tmp_path, static_products=static, repair=True)
    assert manifest_path.read_bytes() == old_manifest
    pd.testing.assert_frame_equal(read_point_static_context(tmp_path), old_context)
    pd.testing.assert_frame_equal(samples(tmp_path), old_samples)
    assert dynamic_hashes(tmp_path) == baseline

    calls = len(endpoint["calls"])
    recovered = run_static(config, features, tmp_path, static_products=static, repair=True)
    assert recovered["status"] == "complete" and recovered["http_requests"] == 0
    assert len(endpoint["calls"]) == calls and len(samples(tmp_path)) == 4
    assert read_point_static_context(tmp_path).iloc[0].selected_number_of_looks == pytest.approx(12.5)
    assert dynamic_hashes(tmp_path) == baseline
