"""
Time series extraction and analysis tools

Extract time series from Zarr datasets for pixels, regions, or geographic coordinates.
"""

import numpy as np
import xarray as xr
from typing import Any, Dict, Tuple
from rasterio.transform import Affine
from pyproj import Transformer

from s1grits.logger_config import get_logger

logger = get_logger(__name__)


def extract_pixel_timeseries(
    dataset: xr.Dataset,
    row: int,
    col: int
) -> Dict[str, Any]:
    """
    Extract time series for a specific pixel

    Args:
        dataset: xarray.Dataset from load_zarr_dataset()
        row: Pixel row index
        col: Pixel column index

    Returns:
        dict containing:
            - vv_ts: VV backscatter time series (dB)
            - vh_ts: VH backscatter time series (dB)
            - ratio_ts: VH/VV ratio (if available)
            - rvi_ts: RVI index (if available)
            - dates: Time coordinates
            - valid_count: Number of valid observations
            - total_count: Total time steps

    Raises:
        IndexError: If pixel coordinates are out of bounds

    Example:
        >>> from s1grits.analysis import load_zarr_dataset, extract_pixel_timeseries
        >>> ds = load_zarr_dataset("17MPV", "DESCENDING")
        >>> ts = extract_pixel_timeseries(ds, 1843, 1831)
        >>> print(f"Valid observations: {ts['valid_count']}/{ts['total_count']}")
    """
    vv_db = dataset['VV_dB']
    vh_db = dataset['VH_dB']
    dates = dataset['time'].values

    # Check bounds
    if row < 0 or row >= vv_db.shape[1] or col < 0 or col >= vv_db.shape[2]:
        raise IndexError(
            f"Pixel coordinates ({row}, {col}) out of bounds. "
            f"Valid range: (0-{vv_db.shape[1]-1}, 0-{vv_db.shape[2]-1})"
        )

    logger.info("Extracting time series for pixel (%d, %d)...", row, col)

    # Extract time series (force dask computation)
    vv_ts = vv_db[:, row, col].compute().values
    vh_ts = vh_db[:, row, col].compute().values

    # Optional variables
    ratio_ts = None
    rvi_ts = None

    if 'Ratio' in dataset:
        ratio_ts = dataset['Ratio'][:, row, col].compute().values

    if 'RVI' in dataset:
        rvi_ts = dataset['RVI'][:, row, col].compute().values

    # Filter NoData values
    valid_mask = ~np.isnan(vv_ts) & ~np.isnan(vh_ts)
    valid_mask = valid_mask & (vv_ts != -9999) & (vh_ts != -9999)

    valid_count = valid_mask.sum()
    total_count = len(dates)

    if valid_count == 0:
        logger.warning("All time steps are NoData for this pixel")

    return {
        'vv_ts': vv_ts[valid_mask],
        'vh_ts': vh_ts[valid_mask],
        'ratio_ts': ratio_ts[valid_mask] if ratio_ts is not None else None,
        'rvi_ts': rvi_ts[valid_mask] if rvi_ts is not None else None,
        'dates': dates[valid_mask],
        'valid_count': int(valid_count),
        'total_count': int(total_count),
        'row': row,
        'col': col,
    }


def extract_region_timeseries(
    dataset: xr.Dataset,
    row_slice: slice,
    col_slice: slice,
    aggregation: str = 'mean'
) -> Dict[str, Any]:
    """
    Extract aggregated time series for a rectangular region

    Args:
        dataset: xarray.Dataset from load_zarr_dataset()
        row_slice: Row slice (e.g., slice(100, 200))
        col_slice: Column slice (e.g., slice(150, 250))
        aggregation: Aggregation method ('mean', 'median', 'std', 'min', 'max')

    Returns:
        dict similar to extract_pixel_timeseries but with aggregated values

    Example:
        >>> ts_region = extract_region_timeseries(
        ...     ds,
        ...     row_slice=slice(1800, 1900),
        ...     col_slice=slice(1800, 1900),
        ...     aggregation='mean'
        ... )
    """
    vv_db = dataset['VV_dB']
    vh_db = dataset['VH_dB']
    dates = dataset['time'].values

    logger.info("Extracting %s time series for region...", aggregation)

    # Extract region
    vv_region = vv_db[:, row_slice, col_slice].compute()
    vh_region = vh_db[:, row_slice, col_slice].compute()

    # Aggregate over spatial dimensions
    agg_func = {
        'mean': np.nanmean,
        'median': np.nanmedian,
        'std': np.nanstd,
        'min': np.nanmin,
        'max': np.nanmax,
    }.get(aggregation, np.nanmean)

    vv_ts = agg_func(vv_region.values, axis=(1, 2))
    vh_ts = agg_func(vh_region.values, axis=(1, 2))

    # Filter NoData
    valid_mask = ~np.isnan(vv_ts) & ~np.isnan(vh_ts)

    return {
        'vv_ts': vv_ts[valid_mask],
        'vh_ts': vh_ts[valid_mask],
        'dates': dates[valid_mask],
        'valid_count': int(valid_mask.sum()),
        'total_count': len(dates),
        'aggregation': aggregation,
        'region': f"rows[{row_slice.start}:{row_slice.stop}], cols[{col_slice.start}:{col_slice.stop}]"
    }


def lonlat_to_pixel(
    lon: float,
    lat: float,
    dataset: xr.Dataset,
    src_crs: str = "EPSG:4326"
) -> Tuple[int, int]:
    """
    Convert lon/lat coordinates to pixel row/col indices

    Args:
        lon: Longitude
        lat: Latitude
        dataset: xarray.Dataset (must have 'crs' and 'transform' attributes)
        src_crs: Source CRS of lon/lat (default: "EPSG:4326")

    Returns:
        Tuple of (row, col) pixel indices

    Raises:
        ValueError: If dataset lacks required spatial metadata

    Example:
        >>> ds = load_zarr_dataset("17MPV", "DESCENDING")
        >>> row, col = lonlat_to_pixel(-122.5, 37.8, ds)
        >>> print(f"Pixel coordinates: ({row}, {col})")
    """
    # Get CRS and transform from dataset
    if 'crs' not in dataset.attrs:
        raise ValueError("Dataset missing 'crs' attribute")

    if 'transform' not in dataset.attrs:
        raise ValueError("Dataset missing 'transform' attribute")

    dst_crs = dataset.attrs['crs']
    transform = dataset.attrs['transform']

    # Convert transform to Affine
    if isinstance(transform, (list, tuple)):
        transform = Affine(*transform)

    # Coordinate transformation
    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    x, y = transformer.transform(lon, lat)

    # Inverse transform: world coordinates → pixel coordinates
    col, row = ~transform * (x, y)

    return int(row), int(col)


def compute_time_series_statistics(ts_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute basic statistics for a time series

    Args:
        ts_dict: Time series dict from extract_pixel_timeseries()

    Returns:
        dict with statistics for VV and VH

    Example:
        >>> stats = compute_time_series_statistics(ts)
        >>> print(f"VV mean: {stats['vv']['mean']:.2f} dB")
    """
    vv_ts = ts_dict['vv_ts']
    vh_ts = ts_dict['vh_ts']

    return {
        'vv': {
            'mean': float(np.mean(vv_ts)),
            'std': float(np.std(vv_ts)),
            'min': float(np.min(vv_ts)),
            'max': float(np.max(vv_ts)),
            'median': float(np.median(vv_ts)),
        },
        'vh': {
            'mean': float(np.mean(vh_ts)),
            'std': float(np.std(vh_ts)),
            'min': float(np.min(vh_ts)),
            'max': float(np.max(vh_ts)),
            'median': float(np.median(vh_ts)),
        },
        'valid_count': ts_dict['valid_count'],
        'total_count': ts_dict['total_count'],
    }


def detect_outliers(
    ts_dict: Dict[str, Any],
    method: str = 'iqr',
    threshold: float = 1.5
) -> Dict[str, np.ndarray]:
    """
    Detect outliers in time series using IQR or Z-score method

    Args:
        ts_dict: Time series dict from extract_pixel_timeseries()
        method: 'iqr' or 'zscore'
        threshold: Threshold for outlier detection (default: 1.5 for IQR, 3.0 for zscore)

    Returns:
        dict with 'vv_outliers' and 'vh_outliers' boolean masks

    Example:
        >>> outliers = detect_outliers(ts, method='iqr')
        >>> print(f"VV outliers: {outliers['vv_outliers'].sum()}")
    """
    vv_ts = ts_dict['vv_ts']
    vh_ts = ts_dict['vh_ts']

    if method == 'iqr':
        # Interquartile range method
        def find_outliers_iqr(data):
            q1, q3 = np.percentile(data, [25, 75])
            iqr = q3 - q1
            lower = q1 - threshold * iqr
            upper = q3 + threshold * iqr
            return (data < lower) | (data > upper)

        vv_outliers = find_outliers_iqr(vv_ts)
        vh_outliers = find_outliers_iqr(vh_ts)

    elif method == 'zscore':
        # Z-score method
        def find_outliers_zscore(data):
            mean = np.mean(data)
            std = np.std(data)
            z_scores = np.abs((data - mean) / std)
            return z_scores > threshold

        vv_outliers = find_outliers_zscore(vv_ts)
        vh_outliers = find_outliers_zscore(vh_ts)

    else:
        raise ValueError(f"Unknown method: {method}. Use 'iqr' or 'zscore'")

    return {
        'vv_outliers': vv_outliers,
        'vh_outliers': vh_outliers,
        'vv_outlier_count': int(vv_outliers.sum()),
        'vh_outlier_count': int(vh_outliers.sum()),
    }
