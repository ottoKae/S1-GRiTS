"""
Data format adapter module

Provides data format conversion functionality between different modules:
- dist-s1-enumerator output → distmetrics-improve input
- Flight direction filtering
- MGRS tile grouping
"""

import pandas as pd
import geopandas as gpd
from warnings import warn

from s1grits.logger_config import get_logger

logger = get_logger(__name__)


def adapt_enumerator_to_distmetrics(df_rtc_ts: gpd.GeoDataFrame) -> pd.DataFrame:
    """
    Convert the output of dist-s1-enumerator to the input format of distmetrics-improve

    Input (from get_rtc_s1_ts_metadata_from_mgrs_tiles):
        - opera_id, jpl_burst_id, acq_dt, url_copol, url_crosspol, track_number, ...

    Output (to load_and_despeckle_rtc_strict):
        - url_copol, url_crosspol, acq_datetime

    Args:
        df_rtc_ts: RTC time-series metadata from dist-s1-enumerator

    Returns:
        pd.DataFrame: distmetrics-improve compatible format
    """
    if df_rtc_ts.empty:
        warn("Input DataFrame is empty")
        return pd.DataFrame(columns=['url_copol', 'url_crosspol', 'acq_datetime'])

    # Select required columns
    required_cols = ['url_copol', 'url_crosspol']

    # Check if required columns exist
    missing_cols = [col for col in required_cols if col not in df_rtc_ts.columns]
    if missing_cols:
        raise ValueError(f"Input DataFrame missing required columns: {missing_cols}")

    # Create output DataFrame
    df_output = df_rtc_ts[required_cols].copy()
    if '_source_row' in df_rtc_ts.columns:
        df_output['_source_row'] = df_rtc_ts['_source_row'].to_numpy()

    # Convert time field name: acq_dt → acq_datetime
    if 'acq_dt' in df_rtc_ts.columns:
        df_output['acq_datetime'] = pd.to_datetime(df_rtc_ts['acq_dt'], utc=True)
    elif 'acq_datetime' in df_rtc_ts.columns:
        df_output['acq_datetime'] = pd.to_datetime(df_rtc_ts['acq_datetime'], utc=True)
    else:
        raise ValueError("Input DataFrame missing time field 'acq_dt' or 'acq_datetime'")

    # Ensure timezone is consistent as UTC
    if df_output['acq_datetime'].dt.tz is None:
        df_output['acq_datetime'] = df_output['acq_datetime'].dt.tz_localize('UTC')

    # Sort by time
    df_output = df_output.sort_values('acq_datetime').reset_index(drop=True)

    logger.info("Format adaptation complete: %d records", len(df_output))
    return df_output


def filter_by_flight_direction(
    df_rtc_ts: gpd.GeoDataFrame,
    flight_direction: str | None
) -> gpd.GeoDataFrame:
    """
    Filter metadata based on flight direction

    Args:
        df_rtc_ts: RTC time-series metadata
        flight_direction: 'ASCENDING' or 'DESCENDING' or None

    Returns:
        gpd.GeoDataFrame: Filtered metadata
    """
    if flight_direction is None:
        logger.info("No flight direction filter specified, using all data")
        return df_rtc_ts

    if df_rtc_ts.empty:
        return df_rtc_ts

    # Check if orbit_pass field exists
    if 'orbit_pass' not in df_rtc_ts.columns:
        warn(f"Metadata missing 'orbit_pass' field, cannot filter by direction {flight_direction}")
        return df_rtc_ts

    # Filter
    flight_direction_upper = flight_direction.upper()
    df_filtered = df_rtc_ts[df_rtc_ts['orbit_pass'] == flight_direction_upper].copy()

    if df_filtered.empty:
        warn(f"No data found for flight direction: {flight_direction}")
    else:
        logger.info("Flight direction filter: %s (retained %d/%d)", flight_direction, len(df_filtered), len(df_rtc_ts))

    return df_filtered.reset_index(drop=True)


def group_by_mgrs_tile(df_rtc_ts: gpd.GeoDataFrame) -> dict[str, gpd.GeoDataFrame]:
    """
    Group metadata by MGRS tile

    Args:
        df_rtc_ts: RTC time-series metadata (containing multiple MGRS tiles)

    Returns:
        {mgrs_tile_id: GeoDataFrame} dictionary
    """
    if df_rtc_ts.empty:
        return {}

    if 'mgrs_tile_id' not in df_rtc_ts.columns:
        warn("Metadata missing 'mgrs_tile_id' field, cannot group")
        return {'unknown': df_rtc_ts}

    grouped = {}
    for mgrs_id, group in df_rtc_ts.groupby('mgrs_tile_id'):
        grouped[mgrs_id] = group.reset_index(drop=True)

    logger.info("Grouped by MGRS: %d tiles", len(grouped))
    for mgrs_id, gdf in grouped.items():
        logger.debug("  %s: %d records", mgrs_id, len(gdf))

    return grouped


def validate_url_pairs(df: pd.DataFrame) -> pd.DataFrame:
    """
    Validate and filter valid URL pairs (both copol + crosspol exist)

    Args:
        df: DataFrame containing url_copol and url_crosspol

    Returns:
        pd.DataFrame: Data containing only valid URL pairs
    """
    if df.empty:
        return df

    # Check if URLs are empty
    valid_copol = df['url_copol'].notna() & (df['url_copol'] != '')
    valid_crosspol = df['url_crosspol'].notna() & (df['url_crosspol'] != '')

    # Retain only if both are valid
    valid_mask = valid_copol & valid_crosspol

    n_invalid = (~valid_mask).sum()
    if n_invalid > 0:
        warn(f"Filtered out {n_invalid} invalid URL pairs (missing copol or crosspol)")

    df_valid = df[valid_mask].copy().reset_index(drop=True)
    return df_valid


def deduplicate_by_time(df: pd.DataFrame, time_column: str = 'acq_datetime') -> pd.DataFrame:
    """
    Deduplicate by time, keeping the latest record

    Args:
        df: Input DataFrame
        time_column: Time column name

    Returns:
        pd.DataFrame: Deduplicated data
    """
    if df.empty:
        return df

    if time_column not in df.columns:
        warn(f"Column '{time_column}' does not exist, skipping deduplication")
        return df

    # Group by time, keeping the last record (latest) for each group
    df_sorted = df.sort_values(time_column)
    df_dedup = df_sorted.drop_duplicates(subset=[time_column], keep='last')

    n_duplicates = len(df) - len(df_dedup)
    if n_duplicates > 0:
        logger.info("Removed %d duplicate time records", n_duplicates)

    return df_dedup.reset_index(drop=True)
