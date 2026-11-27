"""Regional increment and repair operate on actual TIFF, Zarr and Parquet files."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from pyproj import Transformer
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box, mapping
from shapely.ops import transform as transform_geometry
import zarr

from s1grits.opera_spatial.regions import run_regions
from s1grits.provenance import _io_path
from s1grits.spatial_inputs import read_spatial_input


def product(root, burst, date, value, *, version="a", failed=False, shift=0, platform="Sentinel-1A"):
    orbit = int(date[8:10]) + int(date[:4]) * 100
    pid = f"{burst}_{date}_{version}"
    transform = from_origin(-960 + shift, 960, 30, 30)
    polygon = transform_geometry(Transformer.from_crs(3857, 4326, always_xy=True).transform,
                                 box(-960, -960, 960, 960))
    record = {"product_id": pid, "burst_id": burst, "acquisition_id": f"S1A:{orbit}",
              "acquisition_identity": "verified", "acquisition_utc": date + "T00:00:00Z",
              "platform": platform, "direction": "ASCENDING", "relative_orbit": 84,
              "absolute_orbit": orbit, "processing_version": "1", "product_version": "1",
              "processing_date": f"2026-01-0{1 if version == 'a' else 2}T00:00:00Z",
              "geometry": mapping(polygon), "assets": {}}
    for role, amount in (("vv", value), ("vh", value / 2), ("mask", 0)):
        path = root / (pid + "_" + role + ".tif")
        data = np.full((64, 64), amount, dtype="uint8" if role == "mask" else "float32")
        with rasterio.open(path, "w", driver="GTiff", width=64, height=64, count=1,
                           dtype=data.dtype, crs="EPSG:3857", transform=transform,
                           tiled=True, blockxsize=32, blockysize=32, compress="deflate",
                           nodata=255 if role == "mask" else -9999) as dataset:
            dataset.write(data, 1)
            dataset.update_tags(BURST_ID=burst, TRACK_NUMBER="84", ABSOLUTE_ORBIT_NUMBER=str(orbit),
                PLATFORM=platform, ORBIT_PASS_DIRECTION="ASCENDING",
                PROCESSING_INFORMATION_OUTPUT_BACKSCATTER_NORMALIZATION_CONVENTION="gamma0",
                PROCESSING_INFORMATION_OUTPUT_BACKSCATTER_EXPRESSION_CONVENTION="linear backscatter intensity")
        record["assets"][role] = {"asset_id": hashlib.sha256((pid + role).encode()).hexdigest(),
            "product_id": pid, "role": role, "url": "https://fixture.invalid/" + path.name,
            "local_path": str(path.with_suffix(".absent") if failed else path),
            "expected_bytes": path.stat().st_size}
    return record


def inputs(root, *, strategy="S0", two=False, hole=False):
    polygon = box(-.001, -.001, .001, .001)
    if hole:
        polygon = polygon.difference(box(-.0003, -.0003, .0003, .0003))
    shapes = [polygon, box(.002, -.001, .004, .001)] if two else [polygon]
    path = root / "units.geojson"
    gpd.GeoDataFrame({"id": [f"unit{i}" for i in range(len(shapes))]}, geometry=shapes,
                     crs=4326).to_file(path, driver="GeoJSON")
    config = {"input": {"path": str(path), "id_column": "id"}, "spatial": {"mode": "vector_grid"},
              "grid": {"mode": "target_crs", "target_crs": "EPSG:3857", "target_resolution": 30},
              "source_strategy": strategy, "read_mode": "local_cache"}
    return config, read_spatial_input(config)


def manifest(output):
    return json.loads((output / "manifest.json").read_text(encoding="utf-8"))


def first_store(output):
    state = manifest(output)
    entry = next(iter(state["region_stores"].values()))
    return zarr.open_group(str(output / entry["path"]), mode="r"), entry


def test_append_idempotence_owner_and_full_replay(tmp_path):
    config, features = inputs(tmp_path, two=True, hole=True)
    a = product(tmp_path, "T084-A-IW1", "2024-01-01", 1)
    b = product(tmp_path, "T084-B-IW1", "2024-01-01", 9)
    later = product(tmp_path, "T084-A-IW1", "2024-02-02", 2)
    output = tmp_path / "increment"
    first = run_regions(config, features, [a, b], output)
    assert first["status"] == "complete" and first["appended"] == 2
    old = manifest(output)
    owners = {key: zarr.open_group(str(output / info["path"]), mode="r")["owner_index"][:]
              for key, info in old["region_geometry"].items()}
    second = run_regions(config, features, [later], output)
    assert second["appended"] == 2 and second["status"] == "complete"
    repeat = run_regions(config, features, [later], output)
    assert repeat["appended"] == 0 and repeat["http_requests"] == 0 and repeat["cached_windows"] == 0
    full = tmp_path / "full"
    run_regions(config, features, [a, b, later], full)
    new, reference = manifest(output), manifest(full)
    for key, entry in new["region_stores"].items():
        actual = zarr.open_group(str(output / entry["path"]), mode="r")
        expected = zarr.open_group(str(full / reference["region_stores"][key]["path"]), mode="r")
        for name in ("VV", "VH", "source_index", "quality", "time"):
            np.testing.assert_equal(actual[name][:], expected[name][:])
        assert actual["VV"].chunks[0] == 1
    for key, before in owners.items():
        geometry = zarr.open_group(str(output / new["region_geometry"][key]["path"]), mode="r")
        np.testing.assert_equal(before, geometry["owner_index"][:])
        if new["region_geometry"][key]["unit_id"] == "unit0":
            assert (~geometry["roi_mask"][:]).any()
    assert new["groups"] == old["groups"]
    catalog = pd.read_parquet(output / "region_catalog.parquet")
    assert (catalog.source_switch_event_count == 0).all()
    assert (catalog.owner_level == "burst").all()


def test_s1_fallback_restored_by_owner_repair_and_version_not_new_acquisition(tmp_path):
    config, features = inputs(tmp_path, strategy="S1")
    a = product(tmp_path, "T084-A-IW1", "2024-01-01", 1)
    b = product(tmp_path, "T084-B-IW1", "2024-01-01", 9)
    fallback = product(tmp_path, "T084-B-IW1", "2024-02-02", 8)
    output = tmp_path / "s1"
    run_regions(config, features, [a, b], output)
    run_regions(config, features, [fallback], output)
    group, old_entry = first_store(output)
    assert 2 in np.unique(group["source_index"][1])
    owner_late = product(tmp_path, "T084-A-IW1", "2024-02-02", 2)
    update = run_regions(config, features, [owner_late], output)
    assert update["status"] == "repair_required"
    repaired = run_regions(config, features, [owner_late], output, repair=True)
    assert repaired["repaired"] == 1 and repaired["status"] == "complete"
    group, entry = first_store(output)
    assert entry["path"] != old_entry["path"]
    assert (output / old_entry["path"]).is_dir()
    assert set(np.unique(group["source_index"][1])) <= {0, 1}
    new_version = product(tmp_path, "T084-A-IW1", "2024-02-02", 3, version="b")
    run_regions(config, features, [new_version], output, repair=True)
    group, entry = first_store(output)
    assert len(entry["records"]) == 2 and group["time"].shape == (2,)
    assert np.nanmax(group["VV"][1]) == 3
    source_map = pd.read_parquet(output / entry["source_map_path"])
    assert new_version["product_id"] in source_map.product_id.tolist()
    assert pd.read_parquet(output / "region_catalog.parquet").fallback_observation_count.iloc[0] == 0


def test_failure_is_not_fallback_and_repair_retries_only_failed_windows(tmp_path):
    config, features = inputs(tmp_path, strategy="S1")
    a = product(tmp_path, "T084-A-IW1", "2024-01-01", 1)
    b = product(tmp_path, "T084-B-IW1", "2024-01-01", 9)
    output = tmp_path / "failure"
    run_regions(config, features, [a, b], output)
    bad = product(tmp_path, "T084-A-IW1", "2024-02-02", 2, failed=True)
    alternative = product(tmp_path, "T084-B-IW1", "2024-02-02", 8)
    result = run_regions(config, features, [bad, alternative], output)
    assert result["status"] == "partial"
    group, _ = first_store(output)
    assert np.nanmax(group["quality"][1]) >= 3
    assert not group["source_index"][1].any()
    good = deepcopy(bad)
    for asset in good["assets"].values():
        asset["local_path"] = str(Path(asset["local_path"]).with_suffix(".tif"))
    repair = run_regions(config, features, [good], output, repair=True)
    assert repair["status"] == "complete" and repair["cached_windows"] >= 3
    group, _ = first_store(output)
    assert np.nanmax(group["VV"][1]) == 2


def test_late_insert_cross_year_and_uncommitted_tail_recovery(tmp_path):
    config, features = inputs(tmp_path)
    first = product(tmp_path, "T084-A-IW1", "2024-01-01", 1)
    later = product(tmp_path, "T084-A-IW1", "2024-03-03", 3)
    output = tmp_path / "recover"
    run_regions(config, features, [first], output)
    _, entry = first_store(output)
    partial = zarr.open_group(str(output / entry["path"]), mode="a")
    for name in ("VV", "VH", "source_index", "quality"):
        partial[name].resize((2, *partial[name].shape[1:]))
        partial[name][1] = 17
    partial["time"].resize((2,))
    partial["time"][1] = 0
    run_regions(config, features, [later], output)
    group, entry = first_store(output)
    assert len(entry["records"]) == 2 and np.nanmax(group["VV"][1]) == 3
    historical = product(tmp_path, "T084-A-IW1", "2024-02-02", 2)
    assert run_regions(config, features, [historical], output)["status"] == "repair_required"
    run_regions(config, features, [historical], output, repair=True)
    group, entry = first_store(output)
    assert np.all(np.diff(group["time"][:]) > 0)
    assert [np.nanmax(group["VV"][i]) for i in range(3)] == [1, 2, 3]
    year = product(tmp_path, "T084-A-IW1", "2025-01-01", 4)
    run_regions(config, features, [year], output)
    assert len(manifest(output)["region_stores"]) == 2


def test_changed_source_grid_rejected_as_failed_source(tmp_path):
    config, features = inputs(tmp_path)
    first = product(tmp_path, "T084-A-IW1", "2024-01-01", 1)
    shifted = product(tmp_path, "T084-A-IW1", "2024-02-02", 2, shift=30)
    output = tmp_path / "grid"
    run_regions(config, features, [first], output)
    result = run_regions(config, features, [shifted], output)
    assert result["status"] == "partial"
    assert any("Native source grid changed" in item["error"] for item in result["failures"])


def test_static_lia_and_failed_revision_preserve_committed_partition(tmp_path):
    config, features = inputs(tmp_path, strategy="S1")
    a = product(tmp_path, "T084-A-IW1", "2024-01-01", 1, platform="Sentinel-1D")
    b = product(tmp_path, "T084-B-IW1", "2024-01-01", 9, platform="Sentinel-1D")
    statics = []
    for original, angle in ((a, 50), (b, 53)):
        source = product(tmp_path, original["burst_id"], "2024-01-01", angle, version="static")
        statics.append({"product_id": source["product_id"], "burst_id": source["burst_id"],
                        "validity_start": "2016-01-01T00:00:00Z", "assets": {"lia": source["assets"]["vv"]}})
    output = tmp_path / "static"
    run_regions(config, features, [a, b], output, static_products=statics)
    state = manifest(output)
    definition = next(iter(state["region_geometry"].values()))
    geometry = zarr.open_group(str(output / definition["path"]), mode="r")
    mask = geometry["roi_mask"][:]
    assert np.all(geometry["owner_nominal_lia"][:][mask] == 50)
    assert np.all(geometry["nominal_lia_2"][:][mask] == 53)
    _, old_entry = first_store(output)
    broken_new_version = product(tmp_path, "T084-A-IW1", "2024-01-01", 7, version="b", failed=True, platform="Sentinel-1D")
    repair = run_regions(config, features, [broken_new_version], output, repair=True)
    assert repair["status"] == "partial"
    group, entry = first_store(output)
    assert entry["path"] == old_entry["path"] and np.nanmax(group["VV"][0]) == 1


@pytest.mark.parametrize("limit_dates", [False, True])
def test_repair_does_not_retry_failed_history_outside_requested_scope(tmp_path, limit_dates):
    config, features = inputs(tmp_path)
    first = product(tmp_path, "T084-A-IW1", "2024-01-01", 1)
    old_bad = product(tmp_path, "T084-A-IW1", "2024-02-02", 2, failed=True)
    new_bad = product(tmp_path, "T084-A-IW1", "2024-03-03", 3, failed=True)
    output = tmp_path / "scoped"
    run_regions(config, features, [first, old_bad, new_bad], output)
    fixed = deepcopy(new_bad)
    for asset in fixed["assets"].values():
        asset["local_path"] = str(Path(asset["local_path"]).with_suffix(".tif"))
    requested = [old_bad, fixed] if limit_dates else [fixed]
    if limit_dates:
        config.update(start="2024-03-01", end="2024-04-01")
    result = run_regions(config, features, requested, output, repair=True)
    assert result["repaired"] == 1 and not result["failures"]
    group, entry = first_store(output)
    assert entry["records"][1]["status"] == "partial"
    assert entry["records"][2]["status"] == "complete"
    assert not group["source_index"][1].any()
    assert np.nanmax(group["VV"][2]) == 3


def test_direct_region_api_checks_budget_before_creating_outputs(tmp_path):
    config, features = inputs(tmp_path)
    candidate = product(tmp_path, "T084-A-IW1", "2024-01-01", 1)
    config["read_mode"] = "burst_fetch"
    for asset in candidate["assets"].values():
        asset["expected_bytes"] = 20_000_000_000
    output = tmp_path / "over_budget"
    with pytest.raises(ValueError, match="50 GB"):
        run_regions(config, features, [candidate], output)
    assert not output.exists()


def test_real_zarr_repair_under_long_output_path(tmp_path):
    config, features = inputs(tmp_path)
    a = product(tmp_path, "T084-A-IW1", "2024-01-01", 1)
    b = product(tmp_path, "T084-A-IW1", "2024-03-03", 3)
    late = product(tmp_path, "T084-A-IW1", "2024-02-02", 2)
    output = tmp_path / ("long_archive_root_" * 4) / "regional_history"
    run_regions(config, features, [a, b], output)
    result = run_regions(config, features, [late], output, repair=True)
    assert result["status"] == "complete"
    state = manifest(output)
    entry = next(iter(state["region_stores"].values()))
    chunk = output / entry["path"] / "source_index/c/0/0/0"
    assert len(str(chunk)) > 250
    group = zarr.open_group(str(_io_path(output / entry["path"])), mode="r")
    assert [np.nanmax(group["VV"][i]) for i in range(3)] == [1, 2, 3]


def test_static_failure_retried_without_recomputing_owner_or_dynamic_data(tmp_path):
    config, features = inputs(tmp_path)
    dynamic = product(tmp_path, "T084-A-IW1", "2024-01-01", 1, platform="Sentinel-1D")
    static_source = product(tmp_path, "T084-A-IW1", "2024-01-01", 51, version="static")
    asset = deepcopy(static_source["assets"]["vv"])
    original = Path(asset["local_path"])
    absent = tmp_path / "static_initially_absent.tif"
    asset["local_path"] = str(absent)
    statics = [{"product_id": static_source["product_id"], "burst_id": dynamic["burst_id"],
                "validity_start": "2016-01-01T00:00:00Z", "assets": {"lia": asset}}]
    output = tmp_path / "static_retry"
    initial = run_regions(config, features, [dynamic], output, static_products=statics)
    assert initial["status"] == "complete" and initial["lia_status"] == "partial"
    assert initial["lia_failed_source_count"] == 1
    state = manifest(output)
    info = next(iter(state["region_geometry"].values()))
    geometry = zarr.open_group(str(_io_path(output / info["path"])), mode="r")
    owners = geometry["owner_index"][:].copy()
    source, entry = first_store(output)
    before_values = source["VV"][:].copy()
    absent.write_bytes(original.read_bytes())
    repaired = run_regions(config, features, [dynamic], output, static_products=statics, repair=True)
    assert repaired["lia_status"] == "complete" and repaired["lia_failed_source_count"] == 0
    assert repaired["repaired"] == 0
    geometry = zarr.open_group(str(_io_path(output / info["path"])), mode="r")
    np.testing.assert_equal(geometry["owner_index"][:], owners)
    assert "lia_error_1" not in geometry.attrs
    assert np.nanmax(geometry["owner_nominal_lia"][:]) == 51
    after, after_entry = first_store(output)
    assert after_entry["path"] == entry["path"]
    np.testing.assert_equal(after["VV"][:], before_values)
