import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import shapely
from pyproj import Geod

from s1grits.spatial_inputs import build_region_grid, read_spatial_input, region_mask


def test_csv_points_preserve_ids_attributes_and_multipart(tmp_path):
    csv = tmp_path / "points.csv"
    pd.DataFrame({"sample": ["001", "002"], "lon": [111.25, 111.25],
                  "lat": [31.25, 31.25], "note": ["a", "b"]}).to_csv(csv, index=False)
    result = read_spatial_input({"spatial": {"mode": "points"},
                                 "input": {"path": str(csv), "id_column": "sample"}})
    assert result.point_id.tolist() == ["001", "002"]
    assert result.source_feature_id.tolist() == ["001", "002"]
    assert result.part_index.tolist() == [0, 0]
    assert result.note.tolist() == ["a", "b"]
    assert result.crs.to_epsg() == 4326
    assert result.geom_type.eq("Point").all()

    vector = tmp_path / "multipart.geojson"
    gpd.GeoDataFrame({"sample": ["multi"]}, geometry=[shapely.MultiPoint([(111, 31), (112, 32)])],
                     crs=4326).to_file(vector, driver="GeoJSON")
    parts = read_spatial_input({"spatial": {"mode": "points"},
                                "input": {"path": str(vector), "id_column": "sample"}})
    assert parts.point_id.tolist() == ["multi::part0", "multi::part1"]
    assert parts.part_index.tolist() == [0, 1]
    assert parts.geometry.x.tolist() == [111, 112]


def test_shapefile_keeps_features_holes_attributes_and_subset(tmp_path):
    polygon = shapely.Polygon([(111, 31), (111.1, 31), (111.1, 31.1), (111, 31.1)],
                              holes=[[(111.02, 31.02), (111.04, 31.02),
                                      (111.04, 31.04), (111.02, 31.04)]])
    source = gpd.GeoDataFrame({"code": ["west", "east"], "region_id": ["original", "keep"]},
                              geometry=[polygon, shapely.box(111.05, 31.05, 111.2, 31.2)], crs=4326)
    path = tmp_path / "grid.shp"
    source.to_crs(32649).to_file(path)
    config = {"spatial": {"mode": "vector_grid"}, "input": {"path": str(path), "id_column": "code"}}
    units = read_spatial_input(config)
    assert units.region_id.tolist() == ["west", "east"]
    assert units.input_region_id.tolist() == ["original", "keep"]
    assert len(units.geometry.iloc[0].interiors) == 1
    assert units.region_internal_id.is_unique
    selected = read_spatial_input({**config, "spatial": {"mode": "vector_grid", "unit_ids": ["west"]}})
    assert selected.region_internal_id.iloc[0] == units.region_internal_id.iloc[0]
    assert selected.geometry.iloc[0].equals(units.geometry.iloc[0])
    with pytest.raises(ValueError, match="Unknown spatial unit"):
        read_spatial_input({**config, "spatial": {"mode": "vector_grid", "unit_ids": ["missing"]}})


def test_geographic_cells_full_bounds_deduplicated_and_negative(tmp_path):
    path = tmp_path / "aoi.geojson"
    gpd.GeoDataFrame({"id": ["a", "b"]}, geometry=[shapely.box(-.08, -.09, .02, -.01),
                                                    shapely.box(-.07, -.08, -.02, -.02)],
                     crs=4326).to_file(path, driver="GeoJSON")
    config = {"spatial": {"mode": "geographic_0p1"}, "input": {"path": str(path), "id_column": "id"}}
    cells = read_spatial_input(config)
    assert cells.region_id.tolist() == ["deg10_x-1_y-1", "deg10_x0_y-1"]
    assert cells.region_id.is_unique
    assert cells.iloc[0].tessera_parent == "grid_-0.05_-0.05"
    assert cells.iloc[0].geometry.equals(shapely.box(-.1, -.1, 0, 0))
    assert cells.iloc[0].input_feature_ids == ["a", "b"]
    exact = tmp_path / "exact.geojson"
    gpd.GeoDataFrame(geometry=[shapely.box(111.2, 31.2, 111.3, 31.3)], crs=4326).to_file(exact)
    selected = read_spatial_input({**config, "input": {"path": str(exact)}})
    assert selected.region_id.tolist() == ["deg10_x1112_y312"]


def test_large_geographic_request_fails_before_grid_materialization(tmp_path):
    path = tmp_path / "large.geojson"
    gpd.GeoDataFrame(geometry=[shapely.box(-170, -80, 0, 80)], crs=4326).to_file(path)
    with pytest.raises(ValueError, match="2,720,000.*limit is 2,000,000"):
        read_spatial_input({"spatial": {"mode": "geographic_0p1"}, "input": {"path": str(path)}})


def test_half_open_cell_mask_at_roundoff_boundaries():
    grid = {"crs": "EPSG:4326", "transform": [.1, 0, -.15, 0, -.1, .05], "height": 1, "width": 3}
    west = {"spatial_mode": "geographic_0p1", "ix": -1, "iy": 0}
    east = {"spatial_mode": "geographic_0p1", "ix": 0, "iy": 0}
    np.testing.assert_array_equal(region_mask(west, grid), [[True, False, False]])
    np.testing.assert_array_equal(region_mask(east, grid), [[False, True, False]])
    perturbed = {**grid, "transform": [.1, 0, -.15 - 1e-14, 0, -.1, .05 - 1e-14]}
    np.testing.assert_array_equal(region_mask(west, perturbed), region_mask(west, grid))
    np.testing.assert_array_equal(region_mask(east, perturbed), region_mask(east, grid))


def test_fixed_grid_alignment_hole_mask_and_window(tmp_path):
    polygon = shapely.Polygon([(500000, 3400000), (500120, 3400000),
                               (500120, 3400120), (500000, 3400120)],
                              holes=[[(500030, 3400030), (500090, 3400030),
                                      (500090, 3400090), (500030, 3400090)]])
    path = tmp_path / "grid.shp"
    gpd.GeoDataFrame({"id": ["one", "two"]}, geometry=[polygon, shapely.affinity.translate(polygon, xoff=120)],
                     crs=32649).to_file(path)
    config = {"spatial": {"mode": "vector_grid"}, "input": {"path": str(path), "id_column": "id"},
              "grid": {"mode": "target_crs", "target_crs": "EPSG:32649",
                       "target_resolution": 30, "origin": [500000, 3400000]}}
    units = read_spatial_input(config)
    grid = build_region_grid(units.iloc[0], config)
    second = build_region_grid(units.iloc[1], config)
    assert grid["transform"][0] == 30
    assert grid["transform"][4] == -30
    assert (second["transform"][2] - grid["transform"][2]) % 30 == 0
    full = region_mask(units.iloc[0], grid)
    np.testing.assert_array_equal(region_mask(units.iloc[0], grid, (1, 1, 2, 2)), full[1:3, 1:3])
    x = grid["transform"][2] + (np.arange(grid["width"]) + .5) * 30
    y = grid["transform"][5] - (np.arange(grid["height"]) + .5) * 30
    xx, yy = np.meshgrid(x, y)
    expected = shapely.covers(polygon, shapely.points(xx, yy))
    np.testing.assert_array_equal(full, expected)
    assert build_region_grid(units.iloc[0], config) == grid
    assert build_region_grid(units.iloc[0].to_dict(), config) == grid
    np.testing.assert_array_equal(region_mask(units.iloc[0].to_dict(), grid), full)
    fine = build_region_grid(units.iloc[0], {**config, "grid": {**config["grid"], "target_resolution": 10}})
    assert fine["transform"][0] == 10
    assert fine["grid_id"] != grid["grid_id"]


def test_circle_100km_geodesic_mask_and_fixed_aeqd(tmp_path):
    path = tmp_path / "centre.csv"
    pd.DataFrame({"id": ["centre"], "lon": [111.25], "lat": [31.25]}).to_csv(path, index=False)
    config = {"spatial": {"mode": "point_buffer"}, "input": {"path": str(path), "id_column": "id"}}
    row = read_spatial_input(config).iloc[0]
    assert row.radius_m == 100000
    assert row.geometry.geom_type == "Polygon"
    grid = build_region_grid(row, config)
    assert grid["transform"][2] <= -100000
    assert grid["transform"][5] >= 100000
    assert grid["width"] == grid["height"] == 6668
    center = grid["height"] // 2
    assert region_mask(row, grid, (center - 1, center - 1, 2, 2)).all()
    assert not region_mask(row, grid, (0, 0, 2, 2)).any()
    edge_grid = {"crs": grid["crs"], "transform": [10, 0, 99980, 0, -10, 5], "width": 4, "height": 1}
    np.testing.assert_array_equal(region_mask(row, edge_grid), [[True, True, False, False]])
    ring = np.asarray(row.geometry.exterior.coords)
    distances = Geod(ellps="WGS84").inv(np.full(len(ring), row.center_lon),
                                       np.full(len(ring), row.center_lat), ring[:, 0], ring[:, 1])[2]
    np.testing.assert_allclose(distances, 100000, atol=1e-7)


@pytest.mark.parametrize("grid", [{}, {"mode": "aeqd"},
                                    {"mode": "target_crs", "target_crs": "EPSG:4326"},
                                    {"mode": "target_crs", "target_crs": "EPSG:2263"},
                                    {"mode": "target_crs", "target_crs": "EPSG:32649", "target_resolution": 20}])
def test_fixed_units_reject_non_metric_or_missing_grid(grid):
    row = pd.Series({"spatial_mode": "vector_grid", "region_id": "one", "geometry": shapely.box(111, 31, 111.1, 31.1)})
    with pytest.raises(ValueError):
        build_region_grid(row, {"grid": grid})


def test_invalid_input_duplicate_missing_crs_components_and_dateline(tmp_path):
    path = tmp_path / "duplicates.csv"
    pd.DataFrame({"id": ["same", "same"], "lon": [1, 2], "lat": [1, 2]}).to_csv(path, index=False)
    with pytest.raises(ValueError, match="unique"):
        read_spatial_input({"spatial": {"mode": "points"}, "input": {"path": str(path), "id_column": "id"}})
    shp = tmp_path / "missing.shp"
    gpd.GeoDataFrame(geometry=[shapely.box(1, 1, 2, 2)], crs=4326).to_file(shp)
    shp.with_suffix(".prj").unlink()
    with pytest.raises(ValueError, match="missing components.*prj"):
        read_spatial_input({"spatial": {"mode": "vector_grid"}, "input": {"path": str(shp)}})
    path = tmp_path / "dateline.geojson"
    gpd.GeoDataFrame(geometry=[shapely.box(-179, 10, 179, 11)], crs=4326).to_file(path)
    with pytest.raises(ValueError, match="Antimeridian"):
        read_spatial_input({"spatial": {"mode": "vector_grid"}, "input": {"path": str(path)}})
