import geopandas as gpd
import numpy as np
import pytest
import shapely
from shapely.geometry import box

from s1grits.stand_grid import geographic_grid, build_stand_grid, grid_source_links, prepare_stands, query_stand


def test_tessera_alignment_and_negative_coordinates():
    cells = geographic_grid((-.1, -.1, 0, 0), .01)
    assert len(cells) == 100
    assert cells.cell_id.is_unique
    assert set(cells.tessera_parent) == {'grid_-0.05_-0.05'}
    assert np.isclose(shapely.area(cells.geometry.array).sum(), .01)
    assert len(geographic_grid((111.01, 31.01, 111.02, 31.02))) == 1


@pytest.mark.parametrize('step', [0, -1, .03, np.nan, np.inf])
def test_invalid_steps(step):
    with pytest.raises(ValueError):
        geographic_grid((0, 0, 1, 1), step)


def test_bounded_materialization():
    with pytest.raises(ValueError, match='max_cells'):
        geographic_grid((-180, -90, 180, 90))


def test_crossing_stand_and_touch_only_exclusion(tmp_path):
    stands = gpd.GeoDataFrame({'stand_id': ['cross', 'exact']}, geometry=[
        box(.009, .001, .011, .003), box(0, 0, .01, .01)], crs=4326)
    cells, links, _ = build_stand_grid(stands)
    assert links.groupby('stand_id').size().to_dict() == {'cross': 2, 'exact': 1}
    np.testing.assert_allclose(links.groupby('stand_id').stand_fraction.sum(), 1, atol=1e-7)
    cells.to_parquet(tmp_path / 'cells.parquet')
    links.to_parquet(tmp_path / 'stand_cells.parquet')
    answer = query_stand(tmp_path, 'cross')
    assert len(answer['cells']) == 2
    with pytest.raises(KeyError):
        query_stand(tmp_path, 'missing')


def test_crs_transform_and_crosswalk():
    stands = gpd.GeoDataFrame({'stand_id': ['a']}, geometry=[box(111.001,31.001,111.019,31.009)], crs=4326)
    cells, links, _ = build_stand_grid(stands.to_crs(32649))
    assert len(links) == 2
    source = gpd.GeoDataFrame({'source_id':['west','east','touch']}, geometry=[
        box(111,31,111.01,31.01), box(111.01,31,111.02,31.01), box(111.02,31,111.03,31.01)], crs=4326)
    cross = grid_source_links(cells, source, 'source_id')
    assert set(cross.source_id) == {'west','east'}


def test_source_fid_and_duplicate_business_id(tmp_path):
    source = tmp_path / 'stands.gpkg'
    gpd.GeoDataFrame({'name':['same','same']}, geometry=[box(0,0,.01,.01),box(.01,0,.02,.01)], crs=4326).to_file(source)
    stands = prepare_stands(source)
    assert stands.stand_id.is_unique
    with pytest.raises(ValueError, match='unique'):
        prepare_stands(source, id_column='name')


def test_clip_reads_subset_without_resampling(tmp_path):
    import xarray as xr
    from s1grits.stand_grid import clip_stand_window
    stand = gpd.GeoDataFrame({'stand_id':['s']}, geometry=[box(2, 2, 4, 4)], crs=3857)
    stand.to_parquet(tmp_path / 'stands.parquet')
    values = np.arange(100, dtype=float).reshape(10, 10)
    ds = xr.Dataset({'VV': (('y', 'x'), values)}, coords={'x':np.arange(10)+.5, 'y':9.5-np.arange(10)})
    clipped = clip_stand_window(ds, tmp_path, 's', crs=3857)
    np.testing.assert_array_equal(clipped.VV.values, values[6:8, 2:4])
    assert clipped.sizes == {'y':2, 'x':2}


def test_repairs_invalid_polygon_without_changing_source(tmp_path):
    from shapely.geometry import Polygon
    source = tmp_path / 'invalid.gpkg'
    gpd.GeoDataFrame({'name':['bowtie']}, geometry=[Polygon([(0,0),(1,1),(1,0),(0,1),(0,0)])], crs=4326).to_file(source)
    repaired = prepare_stands(source)
    assert repaired.geometry_repaired.all()
    assert repaired.is_valid.all()
    assert not gpd.read_file(source).is_valid.all()


def test_no_source_coverage_still_queryable(tmp_path):
    stands = gpd.GeoDataFrame({'stand_id':['a']}, geometry=[box(0,0,.01,.01)], crs=4326)
    cells, links, _ = build_stand_grid(stands)
    sources = gpd.GeoDataFrame({'mgrs_tile_id':['far']}, geometry=[box(10,10,11,11)], crs=4326)
    crosswalk = grid_source_links(cells, sources, 'mgrs_tile_id')
    assert crosswalk.empty
    crosswalk.to_parquet(tmp_path / 'cell_mgrs.parquet')
    cells.to_parquet(tmp_path / 'cells.parquet')
    links.to_parquet(tmp_path / 'stand_cells.parquet')
    assert query_stand(tmp_path, 'a')['mgrs_tile_id'] == []
