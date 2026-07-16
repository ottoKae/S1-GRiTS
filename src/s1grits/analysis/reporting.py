"""
Reporting API for data coverage analysis

This module provides functions for generating coverage reports,
analyzing temporal gaps, and computing statistics from catalog data.

Functions:
    generate_coverage_report: Generate comprehensive coverage report
    analyze_temporal_gaps: Analyze temporal gaps in time series
    get_tile_statistics: Get statistics for a specific tile
"""

from pathlib import Path
from typing import Any, Dict, Union, Optional
import pandas as pd


def generate_coverage_report(output_dir: Union[str, Path]) -> Dict[str, Any]:
    """
    Generate comprehensive coverage report from catalog

    Analyzes catalog.parquet to provide statistics about data coverage,
    including tile counts, date ranges, temporal gaps, and completeness.

    Args:
        output_dir: Output directory containing catalog.parquet

    Returns:
        dict: Coverage report with keys:
            - success (bool): Whether operation succeeded
            - message (str): Status message
            - overall (dict): Overall statistics
            - tiles (list): Per-tile coverage information
            - gaps (dict): Temporal gaps analysis

    Example:
        >>> report = generate_coverage_report("./output")
        >>> if report['success']:
        ...     print(f"Total tiles: {report['overall']['tile_count']}")
        ...     for tile in report['tiles']:
        ...         print(f"{tile['tile_id']}: {tile['completeness']:.1f}% complete")
    """
    output_dir = Path(output_dir)
    catalog_path = output_dir / "catalog.parquet"

    if not catalog_path.exists():
        return {
            'success': False,
            'message': f"Catalog not found: {catalog_path}"
        }

    try:
        # Load catalog
        df = pd.read_parquet(catalog_path)
        df['datetime'] = pd.to_datetime(df['datetime'])

        # Overall statistics
        overall = {
            'total_records': len(df),
            'tile_count': df['mgrs_tile_id'].nunique(),
            'date_range': (
                df['datetime'].min().strftime('%Y-%m-%d'),
                df['datetime'].max().strftime('%Y-%m-%d')
            ),
            'total_months': df['datetime'].dt.to_period('M').nunique()
        }

        # Add direction info if available
        if 'direction' in df.columns:
            overall['directions'] = df['direction'].unique().tolist()
        elif 'flight_direction' in df.columns:
            overall['directions'] = df['flight_direction'].unique().tolist()

        # Coverage by tile
        tile_coverage = []
        direction_col = 'direction' if 'direction' in df.columns else 'flight_direction'

        for tile_id in sorted(df['mgrs_tile_id'].unique()):
            tile_df = df[df['mgrs_tile_id'] == tile_id]

            if direction_col in df.columns:
                directions = sorted(tile_df[direction_col].unique())
            else:
                directions = [None]

            for direction in directions:
                if direction is not None:
                    dir_df = tile_df[tile_df[direction_col] == direction]
                else:
                    dir_df = tile_df

                # Calculate date range
                min_date = dir_df['datetime'].min()
                max_date = dir_df['datetime'].max()

                # Calculate months
                months = dir_df['datetime'].dt.to_period('M').unique()
                month_count = len(months)

                # Calculate expected months (from min to max)
                expected_months = pd.period_range(
                    start=min_date.to_period('M'),
                    end=max_date.to_period('M'),
                    freq='M'
                )
                expected_count = len(expected_months)

                # Find missing months
                missing_months = set(expected_months) - set(months)
                missing_count = len(missing_months)

                # Completeness percentage
                completeness = (month_count / expected_count * 100) if expected_count > 0 else 0

                tile_info = {
                    'tile_id': tile_id,
                    'records': len(dir_df),
                    'months': month_count,
                    'expected_months': expected_count,
                    'missing_months': missing_count,
                    'completeness': completeness,
                    'start_date': min_date.strftime('%Y-%m'),
                    'end_date': max_date.strftime('%Y-%m'),
                    'missing_list': sorted([str(m) for m in missing_months])
                }

                if direction is not None:
                    tile_info['direction'] = direction

                tile_coverage.append(tile_info)

        # Temporal gaps analysis
        tiles_with_gaps = [t for t in tile_coverage if t['missing_months'] > 0]
        gaps_summary = {
            'total_tiles': len(tile_coverage),
            'tiles_with_gaps': len(tiles_with_gaps),
            'tiles_complete': len(tile_coverage) - len(tiles_with_gaps),
            'gap_details': tiles_with_gaps
        }

        return {
            'success': True,
            'message': f"Coverage report generated for {len(tile_coverage)} tile-direction combinations",
            'overall': overall,
            'tiles': tile_coverage,
            'gaps': gaps_summary
        }

    except Exception as e:
        return {
            'success': False,
            'message': f"Failed to generate coverage report: {str(e)}"
        }


def analyze_temporal_gaps(catalog_df: pd.DataFrame, tile_id: Optional[str] = None,
                         direction: Optional[str] = None) -> Dict[str, Any]:
    """
    Analyze temporal gaps in time series

    Identifies missing months in the time series and calculates completeness.

    Args:
        catalog_df: Catalog DataFrame with datetime column
        tile_id: Optional tile ID to filter by
        direction: Optional direction to filter by

    Returns:
        dict: Gap analysis with keys:
            - has_gaps (bool): Whether gaps exist
            - total_months (int): Total months in range
            - present_months (int): Months with data
            - missing_months (int): Number of missing months
            - completeness (float): Completeness percentage
            - missing_list (list): List of missing month strings
            - date_range (tuple): (start_date, end_date)

    Example:
        >>> df = pd.read_parquet("output/catalog.parquet")
        >>> gaps = analyze_temporal_gaps(df, tile_id="50RKV", direction="ASCENDING")
        >>> if gaps['has_gaps']:
        ...     print(f"Missing {gaps['missing_months']} months")
    """
    # Filter by tile and direction if specified
    df = catalog_df.copy()
    if tile_id:
        df = df[df['mgrs_tile_id'] == tile_id]
    if direction:
        direction_col = 'direction' if 'direction' in df.columns else 'flight_direction'
        if direction_col in df.columns:
            df = df[df[direction_col] == direction]

    if len(df) == 0:
        return {
            'has_gaps': False,
            'total_months': 0,
            'present_months': 0,
            'missing_months': 0,
            'completeness': 0.0,
            'missing_list': [],
            'date_range': (None, None)
        }

    # Ensure datetime column
    if 'datetime' not in df.columns:
        return {
            'has_gaps': False,
            'error': "No datetime column in catalog"
        }

    df['datetime'] = pd.to_datetime(df['datetime'])

    # Calculate date range
    min_date = df['datetime'].min()
    max_date = df['datetime'].max()

    # Calculate present months
    present_months = df['datetime'].dt.to_period('M').unique()
    present_count = len(present_months)

    # Calculate expected months
    expected_months = pd.period_range(
        start=min_date.to_period('M'),
        end=max_date.to_period('M'),
        freq='M'
    )
    expected_count = len(expected_months)

    # Find missing months
    missing_months = set(expected_months) - set(present_months)
    missing_count = len(missing_months)

    # Completeness
    completeness = (present_count / expected_count * 100) if expected_count > 0 else 0

    return {
        'has_gaps': missing_count > 0,
        'total_months': expected_count,
        'present_months': present_count,
        'missing_months': missing_count,
        'completeness': completeness,
        'missing_list': sorted([str(m) for m in missing_months]),
        'date_range': (
            min_date.strftime('%Y-%m'),
            max_date.strftime('%Y-%m')
        )
    }


def get_tile_statistics(catalog_df: pd.DataFrame, tile_id: str) -> Dict[str, Any]:
    """
    Get statistics for a specific tile

    Computes detailed statistics for a single tile, including per-direction
    coverage if applicable.

    Args:
        catalog_df: Catalog DataFrame
        tile_id: MGRS tile ID

    Returns:
        dict: Tile statistics with keys:
            - tile_id (str): Tile ID
            - total_records (int): Total number of records
            - date_range (tuple): (start_date, end_date)
            - months (int): Number of unique months
            - directions (dict): Per-direction statistics (if applicable)

    Example:
        >>> df = pd.read_parquet("output/catalog.parquet")
        >>> stats = get_tile_statistics(df, "50RKV")
        >>> print(f"Tile {stats['tile_id']} has {stats['total_records']} records")
    """
    tile_df = catalog_df[catalog_df['mgrs_tile_id'] == tile_id].copy()

    if len(tile_df) == 0:
        return {
            'tile_id': tile_id,
            'error': 'No data found for this tile'
        }

    tile_df['datetime'] = pd.to_datetime(tile_df['datetime'])

    stats = {
        'tile_id': tile_id,
        'total_records': len(tile_df),
        'date_range': (
            tile_df['datetime'].min().strftime('%Y-%m-%d'),
            tile_df['datetime'].max().strftime('%Y-%m-%d')
        ),
        'months': tile_df['datetime'].dt.to_period('M').nunique()
    }

    # Per-direction statistics
    direction_col = 'direction' if 'direction' in tile_df.columns else 'flight_direction'
    if direction_col in tile_df.columns:
        stats['directions'] = {}
        for direction in tile_df[direction_col].unique():
            dir_df = tile_df[tile_df[direction_col] == direction]
            stats['directions'][direction] = {
                'records': len(dir_df),
                'months': dir_df['datetime'].dt.to_period('M').nunique(),
                'date_range': (
                    dir_df['datetime'].min().strftime('%Y-%m-%d'),
                    dir_df['datetime'].max().strftime('%Y-%m-%d')
                )
            }

    return stats
