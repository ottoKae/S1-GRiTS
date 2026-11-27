"""Normalize spatial units and build their fixed raster grids."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import CRS, Geod, Transformer
from rasterio.transform import Affine
from rasterio.warp import transform_bounds

from s1grits.canonical_catalog_schema import make_grid_id
from s1grits.resampling import validate_target_resolution
from s1grits.stand_grid import geographic_grid


def read_spatial_input(config: dict) -> gpd.GeoDataFrame:
    """Read points or complete spatial units as a WGS84 GeoDataFrame."""
    spatial = config.get("spatial", {})
    mode = spatial.get("mode")
    if mode not in {"points", "point_buffer", "vector_grid", "geographic_0p1"}:
        raise ValueError("spatial.mode must be points, point_buffer, vector_grid or geographic_0p1")
    source = _read_features(config["input"])
    point_mode = mode in {"points", "point_buffer"}
    allowed = ["Point", "MultiPoint"] if point_mode else ["Polygon", "MultiPolygon"]
    if not source.geom_type.isin(allowed).all():
        raise ValueError(f"{mode} requires only {'/'.join(allowed)} geometries")
    if not point_mode:
        repaired = ~source.is_valid
        source.geometry = source.geometry.make_valid()
        if not source.geom_type.isin(allowed).all() or source.is_empty.any():
            raise ValueError("Geometry repair must leave a nonempty Polygon or MultiPolygon")
        source["geometry_repaired"] = repaired

    records = []
    for _, row in source.iterrows():
        parts = list(row.geometry.geoms) if row.geometry.geom_type == "MultiPoint" else [row.geometry]
        for part_index, geometry in enumerate(parts):
            record = row.to_dict()
            feature_id = str(row["source_feature_id"])
            record.update(geometry=geometry, part_index=part_index)
            record["point_id" if point_mode else "region_id"] = (
                f"{feature_id}::part{part_index}" if row.geometry.geom_type == "MultiPoint" else feature_id
            )
            records.append(record)
    result = gpd.GeoDataFrame(records, geometry="geometry", crs=4326)

    if mode == "geographic_0p1":
        bounds = result.total_bounds
        scaled = bounds * 10
        scaled = np.where(np.abs(scaled - np.rint(scaled)) < 1e-8, np.rint(scaled), scaled)
        count = int(np.prod(np.ceil(scaled[2:]) - np.floor(scaled[:2])))
        if count > 2_000_000:
            raise ValueError(f"AOI bounds require {count:,} 0.1-degree cells; limit is 2,000,000. Split the AOI.")
        cells = geographic_grid(bounds, step=0.1)
        feature_indices, cell_indices = cells.sindex.query(result.geometry, predicate="intersects")
        intersections = shapely.intersection(
            result.geometry.array[feature_indices], cells.geometry.array[cell_indices]
        )
        positive = shapely.area(intersections) > 0
        feature_indices, cell_indices = feature_indices[positive], cell_indices[positive]
        chosen = np.unique(cell_indices)
        cells = cells.iloc[chosen].copy()
        cells["input_feature_ids"] = [
            result.source_feature_id.iloc[feature_indices[cell_indices == index]].tolist()
            for index in chosen
        ]
        cells["region_id"] = cells.cell_id
        result = cells.reset_index(drop=True)
    elif mode == "point_buffer":
        radius = float(spatial.get("radius_m", 100_000))
        if not math.isfinite(radius) or radius <= 0:
            raise ValueError("spatial.radius_m must be a positive finite distance in metres")
        resolution = validate_target_resolution(config.get("grid", {}).get("target_resolution", 30))
        result["region_id"] = result.point_id
        result["center_lon"] = result.geometry.x
        result["center_lat"] = result.geometry.y
        result["radius_m"] = radius
        result.geometry = [_circle(row.center_lon, row.center_lat, radius, resolution / 4)
                           for row in result.itertuples()]

    identity = "point_id" if mode == "points" else "region_id"
    if result[identity].duplicated().any():
        raise ValueError("Expanded feature IDs are not unique; choose different input IDs")
    unit_ids = spatial.get("unit_ids")
    if unit_ids is not None:
        if not isinstance(unit_ids, list):
            raise ValueError("spatial.unit_ids must be a list")
        requested = {str(value) for value in unit_ids}
        unknown = requested - set(result[identity])
        if unknown:
            raise ValueError(f"Unknown spatial unit IDs: {', '.join(sorted(unknown))}")
        result = result[result[identity].isin(requested)].copy()
    if result.empty:
        raise ValueError("No spatial units selected")
    result["spatial_mode"] = mode
    if mode != "points":
        result["region_internal_id"] = [
            "r_" + hashlib.sha256(f"{mode}:{value}".encode()).hexdigest()[:20]
            for value in result.region_id
        ]
    result.attrs.update(source.attrs)
    return result.reset_index(drop=True)


def build_region_grid(row, config: dict) -> dict:
    """Build an immutable north-up grid for one normalized region."""
    settings = config.get("grid", {})
    resolution = validate_target_resolution(settings.get("target_resolution", 30))
    mode = settings.get("mode", "aeqd" if row["spatial_mode"] == "point_buffer" else None)
    if mode == "aeqd" and row["spatial_mode"] == "point_buffer":
        crs = CRS.from_proj4(
            f"+proj=aeqd +lat_0={row['center_lat']} +lon_0={row['center_lon']} +datum=WGS84 +units=m"
        )
        radius = float(row["radius_m"])
        bounds = (-radius, -radius, radius, radius)
    elif mode == "target_crs":
        if not settings.get("target_crs"):
            raise ValueError("grid.target_crs is required for target_crs mode")
        crs = CRS.from_user_input(settings["target_crs"])
        if not crs.is_projected or any(abs(axis.unit_conversion_factor - 1) > 1e-12 for axis in crs.axis_info[:2]):
            raise ValueError("grid.target_crs must be a projected CRS with metre axes")
        bounds = transform_bounds("EPSG:4326", crs, *row["geometry"].bounds, densify_pts=101)
        if row["spatial_mode"] == "point_buffer":
            # The display polygon is inscribed; the grid must contain the true circle.
            margin = resolution / 4
            bounds = (bounds[0] - margin, bounds[1] - margin, bounds[2] + margin, bounds[3] + margin)
    else:
        raise ValueError("Fixed geographic/vector grids require grid.mode=target_crs; aeqd is for point_buffer")
    origin = np.asarray(settings.get("origin", [0, 0]), dtype=float)
    if origin.shape != (2,) or not np.isfinite(origin).all() or not np.isfinite(bounds).all():
        raise ValueError("Grid origin and projected bounds must be finite coordinate pairs")
    lower = (np.asarray(bounds[:2]) - origin) / resolution
    upper = (np.asarray(bounds[2:]) - origin) / resolution
    # Avoid adding a full pixel for coordinate-transform roundoff at an exact edge.
    lower = np.where(np.abs(lower - np.rint(lower)) < 1e-8, np.rint(lower), lower)
    upper = np.where(np.abs(upper - np.rint(upper)) < 1e-8, np.rint(upper), upper)
    start, stop = np.floor(lower), np.ceil(upper)
    width, height = (stop - start).astype(int)
    west = (origin + start * resolution)[0]
    north = (origin + stop * resolution)[1]
    transform = [resolution, 0.0, float(west), 0.0, -resolution, float(north)]
    crs_string = crs.to_string()
    return {"crs": crs_string, "transform": transform, "width": int(width), "height": int(height),
            "grid_id": make_grid_id(f"region:{row['spatial_mode']}:{row['region_id']}", crs_string,
                                    transform, int(width), int(height), resolution=resolution)}


def region_mask(row, grid: dict, window=None) -> np.ndarray:
    """Return pixel-centre membership, optionally for (rowoff, coloff, height, width)."""
    rowoff, coloff, height, width = window or (0, 0, grid["height"], grid["width"])
    transform = Affine(*grid["transform"])
    columns, rows = np.meshgrid(np.arange(coloff, coloff + width) + .5,
                               np.arange(rowoff, rowoff + height) + .5)
    x, y = transform * (columns, rows)
    lon, lat = Transformer.from_crs(grid["crs"], 4326, always_xy=True).transform(x, y)
    mode = row["spatial_mode"]
    if mode == "geographic_0p1":
        lon10, lat10 = lon * 10, lat * 10
        lon10 = np.where(np.abs(lon10 - np.rint(lon10)) < 1e-8, np.rint(lon10), lon10)
        lat10 = np.where(np.abs(lat10 - np.rint(lat10)) < 1e-8, np.rint(lat10), lat10)
        return (np.floor(lon10) == row["ix"]) & (np.floor(lat10) == row["iy"])
    if mode == "point_buffer":
        _, _, distance = Geod(ellps="WGS84").inv(
            np.full(lon.shape, row["center_lon"]), np.full(lat.shape, row["center_lat"]), lon, lat
        )
        return distance <= row["radius_m"]
    return shapely.covers(row["geometry"], shapely.points(lon, lat))


def _read_features(settings: dict) -> gpd.GeoDataFrame:
    path = Path(settings["path"])
    id_column = settings.get("id_column")
    if path.suffix.lower() == ".csv":
        table = pd.read_csv(path, dtype={id_column: "string"} if id_column else None)
        lon_column, lat_column = settings.get("lon_column", "lon"), settings.get("lat_column", "lat")
        lon = pd.to_numeric(table[lon_column], errors="raise")
        lat = pd.to_numeric(table[lat_column], errors="raise")
        frame = gpd.GeoDataFrame(table, geometry=gpd.points_from_xy(lon, lat), crs=4326)
    else:
        if path.suffix.lower() == ".shp":
            missing = [suffix for suffix in (".shp", ".shx", ".dbf", ".prj")
                       if not path.with_suffix(suffix).is_file()]
            if missing:
                raise ValueError(f"Shapefile is missing components: {', '.join(missing)}")
        frame = gpd.read_file(path, layer=settings.get("layer"), engine="pyogrio", fid_as_index=True)
    if frame.empty or frame.crs is None:
        raise ValueError("Spatial input must be nonempty and declare its CRS")
    if frame.geometry.isna().any() or frame.is_empty.any():
        raise ValueError("Spatial input contains empty geometry")
    if id_column and id_column not in frame.columns:
        raise ValueError(f"Input ID column is missing: {id_column}")
    ids = frame[id_column] if id_column else pd.Series(frame.index.astype(str), index=frame.index)
    if ids.isna().any() or ids.astype(str).duplicated().any():
        raise ValueError("Input IDs must be non-null and unique")
    source_crs = frame.crs.to_string()
    frame = frame.to_crs(4326)
    coordinates = shapely.get_coordinates(frame.geometry.array)
    if (not np.isfinite(coordinates).all() or np.any(np.abs(coordinates[:, 0]) > 180)
            or np.any(np.abs(coordinates[:, 1]) > 90)):
        raise ValueError("Input geometry is outside finite WGS84 longitude/latitude bounds")
    if np.any(frame.bounds.maxx - frame.bounds.minx > 180) or frame.total_bounds[2] - frame.total_bounds[0] > 180:
        raise ValueError("Antimeridian-spanning inputs must be split before this workflow")
    reserved = {"source_feature_id", "part_index", "spatial_mode", "point_id", "region_id",
                "region_internal_id", "center_lon", "center_lat", "radius_m", "geometry_repaired"}
    for name in reserved.intersection(frame.columns):
        renamed = "input_" + name
        while renamed in frame.columns:
            renamed = "input_" + renamed
        frame = frame.rename(columns={name: renamed})
    frame["source_feature_id"] = ids.astype(str).to_numpy()
    frame.attrs.update(source_path=str(path.resolve()), source_crs=source_crs)
    return frame


def _circle(lon: float, lat: float, radius: float, tolerance: float):
    geod = Geod(ellps="WGS84")
    if min(geod.inv(lon, lat, lon, pole)[2] for pole in (-90, 90)) <= radius:
        raise ValueError("Circles enclosing a pole are outside the supported spatial range")
    count = max(32, math.ceil(math.pi / math.acos(max(-1.0, 1 - tolerance / radius))))
    bearings = np.linspace(0, 360, count, endpoint=False)
    longitudes, latitudes, _ = geod.fwd(np.full(count, lon), np.full(count, lat),
                                      bearings, np.full(count, radius))
    polygon = shapely.Polygon(np.column_stack([longitudes, latitudes]))
    if polygon.bounds[2] - polygon.bounds[0] > 180:
        raise ValueError("Antimeridian-spanning circles must be split before this workflow")
    return polygon
