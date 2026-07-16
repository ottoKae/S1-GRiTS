"""
Plotting and visualization tools for S1 time series analysis

Create publication-quality figures for time series, comparisons, and previews.
"""

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from s1grits.logger_config import get_logger

logger = get_logger(__name__)


def plot_timeseries_figure(
    ts_dict: Dict,
    title: Optional[str] = None,
    output_path: Optional[str] = None,
    figsize: tuple = (12, 10),
    show_outliers: bool = False
) -> plt.Figure:
    """
    Plot VV, VH, Ratio, and RVI time series in a 4-panel figure

    Args:
        ts_dict: Time series dict from extract_pixel_timeseries()
        title: Figure title (optional)
        output_path: Path to save figure (if None, figure is shown)
        figsize: Figure size (width, height) in inches
        show_outliers: Highlight outliers if detected

    Returns:
        matplotlib.figure.Figure object

    Example:
        >>> from s1grits.analysis import (
        ...     load_zarr_dataset,
        ...     extract_pixel_timeseries,
        ...     plot_timeseries_figure
        ... )
        >>> ds = load_zarr_dataset("17MPV", "DESCENDING")
        >>> ts = extract_pixel_timeseries(ds, 1843, 1831)
        >>> plot_timeseries_figure(ts, output_path="timeseries.png")
    """
    vv_ts = ts_dict['vv_ts']
    vh_ts = ts_dict['vh_ts']
    ratio_ts = ts_dict.get('ratio_ts')
    rvi_ts = ts_dict.get('rvi_ts')
    dates = ts_dict['dates']

    # Check if time series is empty
    if len(dates) == 0:
        raise ValueError("Time series is empty - all values are NoData for this pixel")

    # Convert dates to datetime
    if not isinstance(dates[0], datetime):
        dates = [np.datetime64(d, 'D').astype(datetime) for d in dates]

    # Create figure
    fig, axes = plt.subplots(4, 1, figsize=figsize, sharex=True)

    if title is None:
        row = ts_dict.get('row', '?')
        col = ts_dict.get('col', '?')
        title = f"SAR Time Series - Pixel ({row}, {col})"

    # Subplot 1: VV_dB
    axes[0].plot(dates, vv_ts, 'o-', color='steelblue',
                 linewidth=2, markersize=6, label='VV')
    axes[0].set_ylabel('VV (dB)', fontsize=12, fontweight='bold')
    axes[0].set_title(title, fontsize=14, fontweight='bold')
    axes[0].set_ylim(-30, 5)
    axes[0].grid(True, alpha=0.3)
    axes[0].axhline(y=0, color='k', linestyle='--', linewidth=0.5, alpha=0.5)

    # Subplot 2: VH_dB
    axes[1].plot(dates, vh_ts, 'o-', color='darkgreen',
                 linewidth=2, markersize=6, label='VH')
    axes[1].set_ylabel('VH (dB)', fontsize=12, fontweight='bold')
    axes[1].set_ylim(-30, 5)
    axes[1].grid(True, alpha=0.3)
    axes[1].axhline(y=0, color='k', linestyle='--', linewidth=0.5, alpha=0.5)

    # Subplot 3: Ratio
    if ratio_ts is not None and len(ratio_ts) > 0:
        axes[2].plot(dates, ratio_ts, 'o-', color='darkred',
                     linewidth=2, markersize=6, label='Ratio')
        axes[2].set_ylabel('Ratio (VH/VV)', fontsize=12, fontweight='bold')
        axes[2].set_ylim(0, 1)
        axes[2].grid(True, alpha=0.3)
    else:
        axes[2].text(0.5, 0.5, 'Ratio data not available',
                     ha='center', va='center', transform=axes[2].transAxes,
                     fontsize=12, color='gray')
        axes[2].set_ylabel('Ratio (VH/VV)', fontsize=12, fontweight='bold')
        axes[2].set_ylim(0, 1)

    # Subplot 4: RVI
    if rvi_ts is not None and len(rvi_ts) > 0:
        axes[3].plot(dates, rvi_ts, 'o-', color='purple',
                     linewidth=2, markersize=6, label='RVI')
        axes[3].set_ylabel('RVI', fontsize=12, fontweight='bold')
        axes[3].set_xlabel('Date', fontsize=12, fontweight='bold')
        axes[3].set_ylim(0, 4)
        axes[3].grid(True, alpha=0.3)
        axes[3].axhline(y=0.5, color='k', linestyle='--', linewidth=0.5, alpha=0.5)
    else:
        axes[3].text(0.5, 0.5, 'RVI data not available',
                     ha='center', va='center', transform=axes[3].transAxes,
                     fontsize=12, color='gray')
        axes[3].set_ylabel('RVI', fontsize=12, fontweight='bold')
        axes[3].set_xlabel('Date', fontsize=12, fontweight='bold')
        axes[3].set_ylim(0, 4)

    # Format x-axis - show only years to avoid overlap
    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_minor_locator(mdates.MonthLocator())

    plt.xticks(rotation=45)
    plt.tight_layout()

    # Save or show
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        logger.info("Figure saved: %s", output_path)
    else:
        plt.show()

    return fig


def plot_orbit_comparison(
    ts_asc: Dict,
    ts_desc: Dict,
    output_path: Optional[str] = None,
    figsize: tuple = (14, 8)
) -> plt.Figure:
    """
    Compare ASCENDING and DESCENDING orbit time series

    Args:
        ts_asc: Time series dict for ASCENDING orbit
        ts_desc: Time series dict for DESCENDING orbit
        output_path: Path to save figure (optional)
        figsize: Figure size

    Returns:
        matplotlib.figure.Figure object

    Example:
        >>> from s1grits.analysis import load_zarr_dataset, extract_pixel_timeseries
        >>> ds_asc = load_zarr_dataset("17MPV", "ASCENDING")
        >>> ds_desc = load_zarr_dataset("17MPV", "DESCENDING")
        >>> ts_asc = extract_pixel_timeseries(ds_asc, 1843, 1831)
        >>> ts_desc = extract_pixel_timeseries(ds_desc, 1843, 1831)
        >>> plot_orbit_comparison(ts_asc, ts_desc, output_path="orbit_compare.png")
    """
    # Convert dates
    dates_asc = [np.datetime64(d, 'D').astype(datetime) for d in ts_asc['dates']]
    dates_desc = [np.datetime64(d, 'D').astype(datetime) for d in ts_desc['dates']]

    # Create figure
    fig, axes = plt.subplots(2, 1, figsize=figsize, sharex=True)

    row = ts_asc.get('row', '?')
    col = ts_asc.get('col', '?')
    title = f"Orbit Comparison - Pixel ({row}, {col})"

    # VV comparison
    axes[0].plot(dates_asc, ts_asc['vv_ts'], 'o-', label='ASCENDING',
                 color='steelblue', linewidth=1.5, markersize=4)
    axes[0].plot(dates_desc, ts_desc['vv_ts'], 's-', label='DESCENDING',
                 color='coral', linewidth=1.5, markersize=4)
    axes[0].set_ylabel('VV (dB)', fontsize=12, fontweight='bold')
    axes[0].set_title(title, fontsize=14, fontweight='bold')
    axes[0].legend(loc='upper right', fontsize=10)
    axes[0].grid(True, alpha=0.3)

    # VH comparison
    axes[1].plot(dates_asc, ts_asc['vh_ts'], 'o-', label='ASCENDING',
                 color='darkgreen', linewidth=1.5, markersize=4)
    axes[1].plot(dates_desc, ts_desc['vh_ts'], 's-', label='DESCENDING',
                 color='purple', linewidth=1.5, markersize=4)
    axes[1].set_ylabel('VH (dB)', fontsize=12, fontweight='bold')
    axes[1].set_xlabel('Date', fontsize=12, fontweight='bold')
    axes[1].legend(loc='upper right', fontsize=10)
    axes[1].grid(True, alpha=0.3)

    # Format x-axis
    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))

    plt.xticks(rotation=45)
    plt.tight_layout()

    # Save or show
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        logger.info("Figure saved: %s", output_path)
    else:
        plt.show()

    return fig


def plot_monthly_preview(
    dataset,
    month: str,
    tile_id: str = None,
    direction: str = None,
    variable: str = 'VV_dB',
    output_path: Optional[str] = None,
    figsize: tuple = (12, 10),
    cmap: str = 'gray',
    vmin: Optional[float] = None,
    vmax: Optional[float] = None
):
    """
    Plot a false-color composite preview of a specific month from the dataset

    Creates RGB composite: R=VH_dB, G=VV_dB, B=Ratio(VH/VV)
    with histogram stretching for better visualization.

    Args:
        dataset: xarray.Dataset from load_zarr_dataset()
        month: Month to plot (e.g., "2024-01")
        tile_id: MGRS tile ID (e.g., "50RKV")
        direction: Flight direction (e.g., "ASCENDING")
        variable: Variable to plot (kept for backward compatibility, but ignored for RGB composite)
        output_path: Path to save figure (optional)
        figsize: Figure size
        cmap: Colormap name (ignored for RGB composite)
        vmin: Minimum value for colorbar (ignored for RGB composite)
        vmax: Maximum value for colorbar (ignored for RGB composite)

    Returns:
        matplotlib.figure.Figure object

    Example:
        >>> from s1grits.analysis import load_zarr_dataset, plot_monthly_preview
        >>> ds = load_zarr_dataset("17MPV", "DESCENDING")
        >>> plot_monthly_preview(ds, "2024-01", tile_id="17MPV", direction="DESCENDING")
    """
    # Find the time index for the specified month
    times = dataset.time.values
    time_strs = [str(t)[:7] for t in times]  # Extract YYYY-MM

    if month not in time_strs:
        raise ValueError(
            f"Month {month} not found in dataset. "
            f"Available: {sorted(set(time_strs))}"
        )

    time_idx = time_strs.index(month)

    # Extract data for this time step
    vh_db = dataset['VH_dB'].isel(time=time_idx).compute().values
    vv_db = dataset['VV_dB'].isel(time=time_idx).compute().values
    ratio = dataset['Ratio'].isel(time=time_idx).compute().values

    # Histogram stretching function (2% - 98% percentile)
    def stretch_band(band):
        """Apply 2-98 percentile stretch to a band"""
        valid_data = band[~np.isnan(band)]
        if len(valid_data) == 0:
            return np.zeros_like(band)

        p2, p98 = np.percentile(valid_data, [2, 98])
        stretched = (band - p2) / (p98 - p2)
        stretched = np.clip(stretched, 0, 1)

        # Handle NaN values
        stretched[np.isnan(band)] = 0

        return stretched

    # Create RGB composite with histogram stretching
    r_band = stretch_band(vh_db)    # Red: VH_dB
    g_band = stretch_band(vv_db)    # Green: VV_dB
    b_band = stretch_band(ratio)    # Blue: Ratio (VH/VV)

    # Stack to RGB
    rgb = np.stack([r_band, g_band, b_band], axis=-1)

    # Create figure
    fig, ax = plt.subplots(1, 1, figsize=figsize)

    # Plot RGB composite
    ax.imshow(rgb, aspect='auto')

    # Build title
    title_parts = ["False Color Composite"]
    if tile_id:
        title_parts.append(tile_id)
    if month:
        title_parts.append(month)
    if direction:
        title_parts.append(direction)
    title = " - ".join(title_parts)

    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.set_xlabel('X (pixels)', fontsize=12)
    ax.set_ylabel('Y (pixels)', fontsize=12)

    # Add legend explaining the RGB bands
    from matplotlib.patches import Rectangle
    legend_elements = [
        Rectangle((0, 0), 1, 1, fc='red', label='Red: VH_dB'),
        Rectangle((0, 0), 1, 1, fc='green', label='Green: VV_dB'),
        Rectangle((0, 0), 1, 1, fc='blue', label='Blue: Ratio (VH/VV)')
    ]
    ax.legend(handles=legend_elements, loc='upper right', fontsize=10,
              framealpha=0.9, edgecolor='black')

    plt.tight_layout()

    # Save or show
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        logger.info("Figure saved: %s", output_path)
    else:
        plt.show()

    return fig


def plot_time_series_heatmap(
    dataset,
    variable: str = 'VV_dB',
    row_slice: slice = None,
    col_slice: slice = None,
    output_path: Optional[str] = None,
    figsize: tuple = (14, 8)
):
    """
    Plot a space-time heatmap showing temporal evolution across a region

    Args:
        dataset: xarray.Dataset
        variable: Variable to plot
        row_slice: Row slice to extract (default: center 100 rows)
        col_slice: Column slice to extract (default: full width)
        output_path: Path to save figure
        figsize: Figure size

    Returns:
        matplotlib.figure.Figure object
    """
    if row_slice is None:
        center_row = dataset.dims['y'] // 2
        row_slice = slice(center_row - 50, center_row + 50)

    if col_slice is None:
        col_slice = slice(None)

    # Extract data
    data = dataset[variable][:, row_slice, col_slice].compute()

    # Average over x dimension to create (time, y) array
    data_avg = data.mean(dim='x')

    # Create figure
    fig, ax = plt.subplots(1, 1, figsize=figsize)

    # Plot heatmap
    im = ax.imshow(data_avg.T, aspect='auto', cmap='RdYlBu_r',
                   origin='lower', interpolation='nearest')

    # Labels
    ax.set_xlabel('Time', fontsize=12, fontweight='bold')
    ax.set_ylabel('Y (pixels)', fontsize=12, fontweight='bold')
    ax.set_title(f'{variable} Space-Time Evolution', fontsize=14, fontweight='bold')

    # Colorbar
    cbar = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label(f"{variable} ({'dB' if 'dB' in variable else ''})",
                   fontsize=12, fontweight='bold')

    plt.tight_layout()

    # Save or show
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        logger.info("Figure saved: %s", output_path)
    else:
        plt.show()

    return fig
