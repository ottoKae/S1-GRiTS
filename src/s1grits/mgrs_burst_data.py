from functools import lru_cache
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, Polygon

from s1grits.exceptions import NoMGRSCoverage
from s1grits.tabular_models import burst_mgrs_lut_schema, burst_schema, mgrs_tile_schema, reorder_columns


DATA_DIR = Path(__file__).resolve().parent / 'data'


def get_mgrs_burst_lut_path() -> Path:
    parquet_path = DATA_DIR / 'mgrs_burst_lookup_table.parquet'
    return parquet_path


def get_mgrs_data_path() -> Path:
    parquet_path = DATA_DIR / 'mgrs.parquet'
    return parquet_path


def get_burst_data_path() -> Path:
    parquet_path = DATA_DIR / 'jpl_burst_geo.parquet'
    return parquet_path


def get_burst_table(burst_ids: list[str] | str | None = None) -> gpd.GeoDataFrame:
    parquet_path = get_burst_data_path()
    if burst_ids is None:
        df = gpd.read_parquet(parquet_path)
    else:
        if isinstance(burst_ids, str):
            burst_ids = [burst_ids]
        filters = [('jpl_burst_id', 'in', burst_ids)]
        df = gpd.read_parquet(parquet_path, filters=filters)
    if df.empty:
        burst_ids_str = ', '.join(map(str, burst_ids))
        raise ValueError(f'No burst data found for {burst_ids_str}.')
    burst_schema.validate(df)
    df = reorder_columns(df, burst_schema)
    return df.reset_index(drop=True)


@lru_cache
def get_mgrs_burst_lut() -> gpd.GeoDataFrame:
    parquet_path = get_mgrs_burst_lut_path()
    df = pd.read_parquet(parquet_path)
    burst_mgrs_lut_schema.validate(df)
    df = reorder_columns(df, burst_mgrs_lut_schema)
    return df.reset_index(drop=True)


def get_lut_by_mgrs_tile_ids(mgrs_tile_ids: str | list[str]) -> gpd.GeoDataFrame:
    if isinstance(mgrs_tile_ids, str):
        mgrs_tile_ids = [mgrs_tile_ids]
    parquet_path = get_mgrs_burst_lut_path()
    filters = [('mgrs_tile_id', 'in', mgrs_tile_ids)]
    df_mgrs_burst_lut = pd.read_parquet(parquet_path, filters=filters)
    if df_mgrs_burst_lut.empty:
        mgrs_tile_ids_str = ', '.join(map(str, mgrs_tile_ids))
        raise ValueError(f'No LUT data found for MGRS tile ids {mgrs_tile_ids_str}.')
    burst_mgrs_lut_schema.validate(df_mgrs_burst_lut)
    df_mgrs_burst_lut = reorder_columns(df_mgrs_burst_lut, burst_mgrs_lut_schema)
    return df_mgrs_burst_lut.reset_index(drop=True)


@lru_cache
def get_mgrs_table() -> gpd.GeoDataFrame:
    path = get_mgrs_data_path()
    df_mgrs = gpd.read_parquet(path)
    mgrs_tile_schema.validate(df_mgrs)
    df_mgrs = reorder_columns(df_mgrs, mgrs_tile_schema)
    return df_mgrs


def get_mgrs_tile_table_by_ids(mgrs_tile_ids: list[str]) -> gpd.GeoDataFrame:
    df_mgrs = get_mgrs_table()
    if isinstance(mgrs_tile_ids, str):
        mgrs_tile_ids = [mgrs_tile_ids]
    ind = df_mgrs.mgrs_tile_id.isin(mgrs_tile_ids)
    if not ind.any():
        mgrs_tile_ids_str = ', '.join(map(str, mgrs_tile_ids))
        raise ValueError(f'No MGRS tile data found for {mgrs_tile_ids_str}.')
    df_mgrs_subset = df_mgrs[ind].reset_index(drop=True)
    return df_mgrs_subset


def get_mgrs_tiles_overlapping_geometry(geometry: Polygon | Point) -> gpd.GeoDataFrame:
    df_mgrs = get_mgrs_table()
    ind = df_mgrs.intersects(geometry)
    if not ind.any():
        raise NoMGRSCoverage(
            'We only have MGRS tiles that overlap with DIST-HLS products (this is slightly less than Sentinel-2). '
        )
    df_mgrs_overlapping = df_mgrs[ind].reset_index(drop=True)
    mgrs_tile_schema.validate(df_mgrs_overlapping)
    df_mgrs_overlapping = reorder_columns(df_mgrs_overlapping, mgrs_tile_schema)
    return df_mgrs_overlapping


def get_burst_ids_in_mgrs_tiles(mgrs_tile_ids: list[str] | str, track_numbers: list[int] = None) -> list[str]:
    """Get all the burst ids in the provided MGRS tiles.

    If track numbers are provided gets all the burst ids for the provided pass associated with the tracks
    for each MGRS tile. Throws an error if there are multiple acq_group_id_within_mgrs_tile for a single MGRS tile.
    """
    df_mgrs_burst_luts = get_lut_by_mgrs_tile_ids(mgrs_tile_ids)
    if isinstance(mgrs_tile_ids, str):
        mgrs_tile_ids = [mgrs_tile_ids]
    if track_numbers is not None:
        if len(track_numbers) > 2:
            raise ValueError(
                'More than 2 track numbers provided. When track numbers are provided, we select data from a single '
                'pass so this is an invalid input.'
            )
        tile_data = []
        for mgrs_tile_id in mgrs_tile_ids:
            ind_temp = (df_mgrs_burst_luts.mgrs_tile_id == mgrs_tile_id) & (
                df_mgrs_burst_luts.track_number.isin(track_numbers)
            )
            df_lut_temp = df_mgrs_burst_luts[ind_temp].reset_index(drop=True)
            if df_lut_temp.empty:
                mgrs_tile_ids_str = ', '.join(map(str, mgrs_tile_ids))
                track_numbers_str = ', '.join(map(str, track_numbers))
                available_track_numbers = (
                    df_mgrs_burst_luts[df_mgrs_burst_luts.mgrs_tile_id == mgrs_tile_id].track_number.unique().tolist()
                )
                available_track_numbers_str = ', '.join(map(str, available_track_numbers))
                raise ValueError(
                    f'Mismatch - no LUT data found for MGRS tile ids {mgrs_tile_ids_str} '
                    f'and track numbers {track_numbers_str}. '
                    f'Available track numbers for tile {mgrs_tile_ids_str} are {available_track_numbers_str}.'
                )
            acq_ids = df_lut_temp.acq_group_id_within_mgrs_tile.unique().tolist()
            if len(acq_ids) != 1:
                track_numbers_str = ', '.join(map(str, track_numbers))
                raise ValueError(
                    f'Multiple acq_group_id_within_mgrs_tile found for mgrs_tile_id {mgrs_tile_id} and '
                    f'track_numbers {track_numbers_str}.'
                )
            acq_id = acq_ids[0]
            df_lut_pass = df_mgrs_burst_luts[df_mgrs_burst_luts.acq_group_id_within_mgrs_tile == acq_id].reset_index(
                drop=True
            )
            tile_data.append(df_lut_pass)
        df_mgrs_burst_luts = pd.concat(tile_data, axis=0)
        # Remove duplicates if sequential track numbers are provided.
        df_mgrs_burst_luts = df_mgrs_burst_luts.drop_duplicates().reset_index(drop=True)

    df_mgrs_burst_luts = df_mgrs_burst_luts.drop_duplicates(subset=['jpl_burst_id', 'mgrs_tile_id'])
    burst_ids = df_mgrs_burst_luts.jpl_burst_id.unique().tolist()
    return burst_ids


def get_burst_table_from_mgrs_tiles(mgrs_tile_ids: str | list[str]) -> list:
    df_mgrs_burst_luts = get_lut_by_mgrs_tile_ids(mgrs_tile_ids)
    burst_ids = df_mgrs_burst_luts.jpl_burst_id.unique().tolist()
    df_burst = get_burst_table(burst_ids)
    df_burst = pd.merge(
        df_burst,
        df_mgrs_burst_luts[['jpl_burst_id', 'track_number', 'acq_group_id_within_mgrs_tile', 'mgrs_tile_id']],
        how='left',
        on='jpl_burst_id',
    )
    burst_schema.validate(df_burst)
    df_burst = reorder_columns(df_burst, burst_schema)
    return df_burst.reset_index(drop=True)
