"""Sparse geographic retrieval grids alongside the MGRS production grid.

Cells are aligned to Tessera's 0.1 degree boundaries, with integer IDs.
GeoParquet holds geometry/indexes, not raster values. No network I/O.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely


def geographic_grid(bounds, step: float = 0.01, max_cells: int = 2_000_000):
    """Build bounded EPSG:4326 cells. Step must divide 0.1 degrees exactly.

    Antimeridian-spanning regions must be split by the caller. Global dense
    materialization is deliberately bounded; build only the region needed.
    """
    if not np.isfinite(step) or step <= 0 or not np.isclose(0.1 / step, round(0.1 / step), rtol=1e-10, atol=0):
        raise ValueError('step must be a positive exact divisor of 0.1 degrees')
    subdivisions = round(0.1 / step)
    scale = 10 * subdivisions
    west, south, east, north = map(float, bounds)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError('bounds must be ordered WGS84 bounds without antimeridian crossing')
    # Snap only numerical roundoff at exact cell boundaries.
    def snap(value):
        return round(value) if abs(value - round(value)) < 1e-8 else value
    x0, y0 = int(np.floor(snap(west * scale))), int(np.floor(snap(south * scale)))
    x1, y1 = int(np.ceil(snap(east * scale))), int(np.ceil(snap(north * scale)))
    if (x1 - x0) * (y1 - y0) > max_cells:
        raise ValueError('Region exceeds max_cells; split the region before building')
    ix, iy = np.meshgrid(np.arange(x0, x1), np.arange(y0, y1))
    ix, iy = ix.ravel(), iy.ravel()
    px, py = ix // subdivisions, iy // subdivisions
    return gpd.GeoDataFrame({
        'cell_id': [f'deg{scale}_x{x}_y{y}' for x, y in zip(ix, iy)],
        'grid_scheme': f'geographic_1over{scale}_v1',
        'step_deg': 1 / scale, 'ix': ix, 'iy': iy,
        'west': ix / scale, 'south': iy / scale,
        'east': (ix + 1) / scale, 'north': (iy + 1) / scale,
        'tessera_parent': [f'grid_{(x + .5) / 10:.2f}_{(y + .5) / 10:.2f}' for x, y in zip(px, py)],
    }, geometry=shapely.box(ix / scale, iy / scale, (ix + 1) / scale, (iy + 1) / scale), crs=4326)


def prepare_stands(source: str | Path, layer=None, id_column=None):
    """Read source FIDs as stable stand IDs and repair geometry on a copy."""
    stands = gpd.read_file(source, layer=layer, engine='pyogrio', fid_as_index=True)
    if stands.crs is None or stands.empty:
        raise ValueError('Stands must be nonempty and have a declared CRS')
    if not stands.geom_type.isin(['Polygon', 'MultiPolygon']).all() or stands.geometry.isna().any():
        raise ValueError('Every stand must have a polygon geometry')
    ids = pd.Series(stands.index.astype(str), index=stands.index) if id_column is None else stands[id_column]
    if ids.isna().any() or ids.astype(str).duplicated().any():
        raise ValueError('Stand IDs must be non-null and unique; use source FIDs or a unique column')
    invalid = ~stands.is_valid
    stands = stands.copy()
    stands['stand_id'] = ids.astype(str)
    stands['geometry_repaired'] = invalid
    stands.geometry = stands.geometry.make_valid()
    # make_valid can return polygon + collapsed line collections. Retain area only.
    def polygonal(geom):
        if geom.geom_type in ('Polygon', 'MultiPolygon'):
            return geom
        parts = [polygonal(part) for part in getattr(geom, 'geoms', [])]
        return shapely.union_all(parts)
    mixed = ~stands.geom_type.isin(['Polygon', 'MultiPolygon'])
    stands.loc[mixed, 'geometry'] = stands.loc[mixed, 'geometry'].map(polygonal)
    if stands.is_empty.any() or not stands.is_valid.all():
        raise ValueError('Geometry repair left empty or invalid stands; inspect source')
    return stands.reset_index(drop=True)


def build_stand_grid(stands: gpd.GeoDataFrame, step=0.01):
    """Return occupied cells, all positive-area stand/cell links and area CRS.

    Membership uses polygon intersection, never centroid-only assignment.
    Areas use a regional Lambert azimuthal equal-area projection. Intersections
    are computed before projection so complementary fragments share boundaries.
    """
    if 'stand_id' not in stands or stands.stand_id.duplicated().any():
        raise ValueError('Unique stand_id required; use prepare_stands first')
    geo = stands.to_crs(4326)
    west, south, east, north = geo.total_bounds
    if east - west > 180:
        raise ValueError('Split antimeridian-spanning data before indexing')
    area_crs = f'+proj=laea +lat_0={(south+north)/2} +lon_0={(west+east)/2} +datum=WGS84 +units=m +no_defs'
    cells = geographic_grid(geo.total_bounds, step)
    stand_i, cell_i = cells.sindex.query(geo.geometry, predicate='intersects')
    fragments = shapely.intersection(geo.geometry.array[stand_i], cells.geometry.array[cell_i])
    positive = shapely.area(fragments) > 0
    stand_i, cell_i, fragments = stand_i[positive], cell_i[positive], fragments[positive]
    areas = gpd.GeoSeries(fragments, crs=4326).to_crs(area_crs).area.to_numpy()
    stand_areas = geo.to_crs(area_crs).area.to_numpy()
    if np.any(stand_areas <= 0):
        raise ValueError('Stands must have positive area')
    links = pd.DataFrame({
        'stand_id': geo.stand_id.to_numpy()[stand_i],
        'cell_id': cells.cell_id.to_numpy()[cell_i],
        'intersection_m2': areas,
        'stand_fraction': areas / stand_areas[stand_i],
    }).sort_values(['stand_id', 'cell_id']).reset_index(drop=True)
    cells = cells.iloc[np.unique(cell_i)].copy().reset_index(drop=True)
    cells['cell_area_m2'] = cells.to_crs(area_crs).area.to_numpy()
    cells['stand_count'] = cells.cell_id.map(links.groupby('cell_id').stand_id.nunique())
    cells['summed_stand_area_m2'] = cells.cell_id.map(links.groupby('cell_id').intersection_m2.sum())
    return cells, links, area_crs


def grid_source_links(cells, source, source_id):
    """Positive-area cell/source crosswalk (supports MGRS or burst footprints)."""
    source = source.to_crs(cells.crs)
    ci, si = source.sindex.query(cells.geometry, predicate='intersects')
    positive = shapely.area(shapely.intersection(cells.geometry.array[ci], source.geometry.array[si])) > 0
    return pd.DataFrame({'cell_id': cells.cell_id.to_numpy()[ci[positive]],
                         source_id: source[source_id].to_numpy()[si[positive]]}
                        ).astype({'cell_id': 'string', source_id: 'string'}).drop_duplicates()


def query_stand(index_dir, stand_id):
    """Resolve a source FID to all cells and candidate MGRS/bursts, offline."""
    root = Path(index_dir)
    links = pd.read_parquet(root / 'stand_cells.parquet', filters=[('stand_id', '=', str(stand_id))])
    if links.empty:
        raise KeyError(f'No indexed stand {stand_id}')
    ids = links.cell_id.tolist()
    cells = gpd.read_parquet(root / 'cells.parquet', filters=[('cell_id', 'in', ids)])
    result = {'stand_id': str(stand_id), 'cells': ids,
              'bbox_wgs84': cells.total_bounds.tolist(),
              'tessera_parents': sorted(cells.tessera_parent.unique().tolist()),
              'links': links.to_dict('records')}
    for filename, key in [('cell_mgrs.parquet', 'mgrs_tile_id'), ('cell_bursts.parquet', 'jpl_burst_id')]:
        if (root / filename).exists():
            rows = pd.read_parquet(root / filename, filters=[('cell_id', 'in', ids)])
            result[key] = sorted(rows[key].unique().tolist())
    return result


def clip_stand_window(dataset, index_dir, stand_id, *, crs=None, all_touched=False):
    """Subset an opened raster cube before masking one stand, preserving pixels.

    Caller selects product/track/time and opens lazily before calling. No
    resampling or track mixing is performed. Only the overlap with this cube
    is returned; cross-MGRS stands require each relevant cube separately.
    ``all_touched=False`` uses pixel centers; tiny stands may have no centers.
    """
    import rioxarray  # noqa: F401 - register the rio accessor

    stand = gpd.read_parquet(Path(index_dir) / 'stands.parquet',
                             filters=[('stand_id', '=', str(stand_id))])
    if len(stand) != 1:
        raise KeyError(f'Expected exactly one stand for {stand_id}')
    if dataset.rio.crs is None:
        if crs is None:
            raise ValueError('Cube CRS missing; supply the catalog record CRS')
        dataset = dataset.rio.write_crs(crs)
    projected = stand.to_crs(dataset.rio.crs)
    window = dataset.rio.clip_box(*projected.total_bounds, allow_one_dimensional_raster=True)
    return window.rio.clip(projected.geometry.values, projected.crs, all_touched=all_touched, drop=False)


def cmd_grid_build(args):
    root = Path(args.output_dir)
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError('Use an empty output directory to avoid mixing index versions')
    stands = prepare_stands(args.stands, args.layer, args.id_column)
    cells, links, area_crs = build_stand_grid(stands, args.step)
    cells.to_parquet(root / 'cells.parquet', index=False, compression='zstd')
    links.to_parquet(root / 'stand_cells.parquet', index=False, compression='zstd', row_group_size=8192)
    stands.to_parquet(root / 'stands.parquet', index=False, compression='zstd', row_group_size=8192)
    data = Path(__file__).parent / 'data'
    for filename, key, output in [('mgrs.parquet', 'mgrs_tile_id', 'cell_mgrs.parquet'),
                                   ('jpl_burst_geo.parquet', 'jpl_burst_id', 'cell_bursts.parquet')]:
        grid_source_links(cells, gpd.read_parquet(data / filename), key).to_parquet(root / output, index=False)
    with open(args.stands, 'rb') as source_file:
        source_hash = hashlib.file_digest(source_file, 'sha256').hexdigest()
    metadata = {'schema_version': 1, 'source': str(Path(args.stands).resolve()),
                'source_sha256': source_hash,
                'layer': args.layer, 'id_column': args.id_column or 'source_fid',
                'step_deg': args.step, 'area_crs': area_crs, 'stands': len(stands),
                'cells': len(cells), 'repaired_geometries': int(stands.geometry_repaired.sum()),
                'purpose': 'retrieval_index; existing MGRS production is unchanged'}
    (root / 'manifest.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(metadata, ensure_ascii=True))


def cmd_grid_query(args):
    print(json.dumps(query_stand(args.index_dir, args.stand_id), ensure_ascii=True, indent=2))


def register_cli(subparsers):
    parser = subparsers.add_parser('grid', help='Build/query a geographic GeoParquet stand index')
    commands = parser.add_subparsers(dest='grid_command', required=True)
    build = commands.add_parser('build', help='Build an offline index from polygon stands')
    build.add_argument('--stands', required=True)
    build.add_argument('--layer')
    build.add_argument('--id-column')
    build.add_argument('--step', type=float, default=0.01)
    build.add_argument('--output-dir', required=True)
    build.set_defaults(func=cmd_grid_build)
    query = commands.add_parser('query', help='Find all cells and candidate source tiles for one stand')
    query.add_argument('--index-dir', required=True)
    query.add_argument('--stand-id', required=True)
    query.set_defaults(func=cmd_grid_query)
