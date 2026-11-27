"""Exercise the public local planning/export path with actual files."""
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import xarray as xr
import zarr
from shapely.geometry import Point, box, mapping

from s1grits.spatial import capacity_budget, plan_spatial, run_spatial
from s1grits.zarr_encoding import create_zarr_array


def _config(tmp_path, mode="geographic_0p1"):
    source = tmp_path / "source.zarr"
    group = zarr.open_group(source, mode="w", zarr_format=3)
    group.attrs.update({"crs": "EPSG:3857", "transform": [30, 0, 0, 0, -30, 11160],
                        "product_type": "smonthly", "direction": "ASCENDING", "track_members": [18],
                        "geometry_group_id": "sample_ASCENDING_TK18", "processing_signature": "known",
                        "processing_variant_json": json.dumps({"processing.monthly.composite_method": "median"}),
                        "nominal_geometry": mapping(box(0, 0, .2, .1)), "data_domain": "db",
                        "n_obs_definition": "finite_VV_aligned_burst_stack_entries"})
    create_zarr_array(group, "x", data=np.arange(744) * 30 + 15, dimension_names=["x"])
    create_zarr_array(group, "y", data=11160 - np.arange(372) * 30 - 15, dimension_names=["y"])
    create_zarr_array(group, "time", data=np.array(["2024-01-10", "2024-02-10"], dtype="datetime64[ns]"), dimension_names=["time"])
    for name, value in (("VV_dB", -10), ("VH_dB", -15)):
        array = create_zarr_array(group, name, shape=(2, 372, 744), dtype="float32", chunks=(1, 128, 128),
                                  fill_value=np.nan, dimension_names=["time", "y", "x"])
        array[:] = value
        array.attrs["units"] = "dB"
    n_obs = create_zarr_array(group, "n_obs", shape=(2, 372, 744), dtype="uint8", chunks=(1, 128, 128), dimension_names=["time", "y", "x"])
    n_obs[:] = 3
    path = tmp_path / "input.geojson"
    if mode == "points":
        features = gpd.GeoDataFrame({"id": ["p1", "p2"]}, geometry=[Point(.05, .05), Point(.15, .05)], crs=4326)
    else:
        features = gpd.GeoDataFrame({"id": ["aoi"]}, geometry=[box(.01, .01, .19, .09)], crs=4326)
    features.to_file(path, driver="GeoJSON")
    return {"spatial": {"mode": mode}, "input": {"path": str(path), "id_column": "id"},
            "source": {"root": str(source), "product_type": "smonthly"},
            "time": {"years": [2024], "months": [1, 2]},
            "grid": {"mode": "target_crs", "target_crs": "EPSG:3857", "target_resolution": 30},
            "variables": ["VV_dB", "VH_dB", "n_obs"],
            "preflight": {"disk": {"min_free_gb": 0}},
            "output": {"base_dir": str(tmp_path / "output"), "existing_store": "resume"}}


def test_public_geographic_export_resume_and_source_freeze(tmp_path):
    config = _config(tmp_path)
    plan = plan_spatial(config)
    output = Path(config["output"]["base_dir"])
    assert not output.exists()
    assert plan["unit_count"] == 2
    assert {u["region_id"] for u in plan["units"]} == {"deg10_x0_y0", "deg10_x1_y0"}
    assert all(u["time_count"] == 2 for u in plan["units"])
    assert plan["budget"]["logical_bytes"] > 2 * 2 * 372 * 372 * 12
    result = run_spatial(config)
    assert result["status"] == "complete"
    catalog = pd.read_parquet(output / "region_catalog.parquet")
    assert len(catalog) == 2
    assert all(p.startswith("geographic_0p1/") for p in catalog.zarr_path)
    for relative in catalog.zarr_path:
        with xr.open_zarr(output / relative, consolidated=False) as ds:
            assert ds.sizes["time"] == 2
            assert set(np.unique(ds.VV_dB.values[np.isfinite(ds.VV_dB.values)])) == {-10}
    before = json.loads((output / "manifest.json").read_text())["completed"]
    assert run_spatial(config)["status"] == "complete"
    assert json.loads((output / "manifest.json").read_text())["completed"] == before
    group = zarr.open_group(config["source"]["root"], mode="r+")
    group.attrs["data_revision"] = "changed"
    with pytest.raises(ValueError, match="changed"):
        run_spatial(config)


def test_fixed_shapefile_units_and_points(tmp_path):
    config = _config(tmp_path, "vector_grid")
    path = tmp_path / "fixed.shp"
    gpd.GeoDataFrame({"id": ["left", "right"]}, geometry=[box(.01, .01, .04, .04), box(.05, .01, .08, .04)], crs=4326).to_file(path)
    config["input"]["path"] = str(path)
    assert run_spatial(config)["outputs"] == 2
    stored = gpd.read_parquet(Path(config["output"]["base_dir"]) / "regions.parquet")
    assert set(stored.region_id) == {"left", "right"}
    point_dir = tmp_path / "point_run"
    point_dir.mkdir()
    point_config = _config(point_dir, "points")
    point_config["variables"] = None
    assert run_spatial(point_config)["observation_count"] == 12
    plan = plan_spatial(point_config)
    assert plan["budget"]["observation_rows"] == 12


def test_budget_exact_decimal_threshold_and_peak():
    assert not capacity_budget(50_000_000_000)["confirmation_required"]
    assert capacity_budget(50_000_000_001)["confirmation_required"]
    assert capacity_budget(49_000_000_000, 1_000_000_001)["confirmation_required"]


def test_large_circle_is_planned_but_not_written_without_confirmation(tmp_path):
    config = _config(tmp_path, "points")
    config["spatial"] = {"mode": "point_buffer", "radius_m": 1_000_000}
    config["grid"] = {"mode": "aeqd", "target_resolution": 30}
    assert plan_spatial(config)["budget"]["confirmation_required"]
    with pytest.raises(ValueError, match="50 GB"):
        run_spatial(config)
    assert not Path(config["output"]["base_dir"]).exists()


def test_protect_source_and_changed_inputs(tmp_path):
    config = _config(tmp_path, "points")
    config["output"]["base_dir"] = config["source"]["root"]
    with pytest.raises(ValueError, match="separate"):
        plan_spatial(config)
    config["output"]["base_dir"] = str(tmp_path / "output")
    run_spatial(config)
    config["time"]["months"] = [1]
    with pytest.raises(ValueError, match="changed"):
        run_spatial(config)


def test_output_cannot_enter_a_paired_static_store_outside_catalog_root(tmp_path):
    config = _config(tmp_path, "points")
    dynamic_path = Path(config["source"]["root"])
    dynamic = zarr.open_group(dynamic_path, mode="r")
    static_path = tmp_path / "external_static.zarr"
    static = zarr.open_group(static_path, mode="w", zarr_format=3)
    static.attrs.update({**dict(dynamic.attrs), "product_type": "static"})
    create_zarr_array(static, "x", data=dynamic["x"][:], dimension_names=["x"])
    create_zarr_array(static, "y", data=dynamic["y"][:], dimension_names=["y"])
    create_zarr_array(static, "local_inc_angle", shape=(372, 744), dtype="float32", dimension_names=["y", "x"])
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    pd.DataFrame([{"zarr_path": str(p), "product_type": kind, "status": "complete"}
                  for p, kind in [(dynamic_path, "smonthly"), (static_path, "static")]]).to_parquet(catalog_root / "catalog.parquet", index=False)
    config["source"]["root"] = str(catalog_root)
    config["include_static"] = True
    config["output"]["base_dir"] = str(static_path / "exports")
    with pytest.raises(ValueError, match="separate"):
        run_spatial(config)
    assert not (static_path / "exports").exists()


def test_cli_dry_run_and_mgrs_parser_unchanged(tmp_path, monkeypatch, capsys):
    import yaml
    from s1grits.cli import main
    config = _config(tmp_path, "points")
    path = tmp_path / "points.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["s1grits", "points", "extract", "--config", str(path), "--dry-run"])
    main()
    assert json.loads(capsys.readouterr().out)["spatial_mode"] == "points"
    monkeypatch.setattr("sys.argv", ["s1grits", "process_scenes", "--help"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 0
    assert "--config" in capsys.readouterr().out
