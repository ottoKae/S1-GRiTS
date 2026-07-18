"""
Shared workflow helpers.

Config loading, MGRS-tile enumeration, and per-tile RTC metadata querying,
shared by the surviving workflows (``workflow_scenes``, ``workflow_static``,
``s1grits.scenes.pipeline``).

The legacy monthly-composite workflow (``process_single_mgrs_tile`` /
``run_multi_mgrs_monthly_workflow``, the old ``s1grits process`` command) was
removed in v3.0.0; the bounded-memory scenes pipeline (``s1grits process-scenes``,
``processing.monthly``) supersedes it. Historical ``monthly`` products remain
readable — the product type and its writers/STAC handling are retained.
"""

import time
from pathlib import Path
from typing import Any
from warnings import warn

import yaml
import pandas as pd
import geopandas as gpd
from shapely import wkt as shapely_wkt

# Import project modules
from s1grits.mgrs_burst_data import get_mgrs_tiles_overlapping_geometry
from s1grits.asf_tiles import get_rtc_s1_ts_metadata_from_mgrs_tiles
from s1grits.logger_config import get_logger

logger = get_logger(__name__)


def load_config(config_path: str | Path) -> dict[str, Any]:
    """
    Load YAML configuration file

    Args:
        config_path: Configuration file path

    Returns:
        dict[str, Any]: Configuration dictionary

    Raises:
        FileNotFoundError: If the config file does not exist.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file does not exist: {config_path}")

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    logger.info("Config file loaded: %s", config_path)
    return config


# Re-exported from the lightweight config_overrides module so workflow callers
# (and `from s1grits.workflow import apply_output_overrides_and_stac`) keep working.
from s1grits.config_overrides import deep_merge_config, apply_output_overrides_and_stac  # noqa: E402,F401


def enumerate_mgrs_tiles(config: dict) -> list[str]:
    """
    Get MGRS tiles list from config (auto-detect or manual specification)

    Args:
        config: Configuration dictionary

    Returns:
        list[str]: MGRS tile IDs

    Raises:
        ValueError: If no MGRS tiles intersect the ROI geometry.
    """
    roi_config = config.get('roi', {})

    # Check if MGRS tiles are manually specified
    manual_tiles = roi_config.get('manual_mgrs_tiles')
    if manual_tiles:
        logger.info("Using manually specified MGRS tiles: %s", manual_tiles)
        return manual_tiles

    # Auto-detect MGRS tiles
    wkt_str = roi_config.get('wkt', '')
    geom = shapely_wkt.loads(wkt_str)

    logger.info("Auto-detecting MGRS tiles from WKT...")
    df_mgrs = get_mgrs_tiles_overlapping_geometry(geom)

    if df_mgrs.empty:
        raise ValueError(f"No MGRS tiles intersecting ROI: {wkt_str[:100]}...")

    mgrs_tile_ids = df_mgrs['mgrs_tile_id'].tolist()
    logger.info("Detected %d MGRS tiles: %s", len(mgrs_tile_ids), mgrs_tile_ids)

    # Log UTM information for each tile
    for _, row in df_mgrs.iterrows():
        logger.debug("  %s: EPSG:%s", row['mgrs_tile_id'], row['utm_epsg'])

    return mgrs_tile_ids


def _filter_low_coverage_tracks(
    df_rtc_ts: "gpd.GeoDataFrame", mgrs_tile_id: str, min_frac: float,
) -> "gpd.GeoDataFrame":
    """Drop whole acquisition-group tracks (``track_token``) whose burst
    footprint covers less than ``min_frac`` of the MGRS tile.

    Coverage is the area of (union of the track's distinct burst footprints) ∩
    tile, divided by the tile area, computed in the tile's UTM CRS. Tracks that
    only clip a small corner of the tile (e.g. <10%) produce a mostly-empty
    product and waste download/compute, so they are removed before download.

    No-op when ``min_frac`` <= 0, the frame is empty, or required columns are
    missing. Returns the filtered frame.
    """
    if min_frac <= 0 or df_rtc_ts is None or df_rtc_ts.empty:
        return df_rtc_ts
    if 'track_token' not in df_rtc_ts.columns or 'geometry' not in df_rtc_ts.columns:
        return df_rtc_ts

    import pyproj
    from shapely.ops import transform as _shp_transform, unary_union
    from s1grits.asf_io import _mgrs_to_utm_epsg
    from s1grits.asf_output_writing import _get_mgrs_tile_geometry_wkt

    try:
        tile_geom = shapely_wkt.loads(_get_mgrs_tile_geometry_wkt(mgrs_tile_id))
        utm = _mgrs_to_utm_epsg(mgrs_tile_id)
        to_utm = pyproj.Transformer.from_crs(
            pyproj.CRS.from_epsg(4326), pyproj.CRS.from_user_input(utm),
            always_xy=True,
        ).transform
        tile_utm = _shp_transform(to_utm, tile_geom)
        tile_area = tile_utm.area
    except Exception as _e:
        logger.warning("Coverage filter skipped for %s (tile geom error): %s", mgrs_tile_id, _e)
        return df_rtc_ts
    if not tile_area:
        return df_rtc_ts

    keep_tokens, dropped = [], []
    for _tok, _g in df_rtc_ts.groupby('track_token'):
        try:
            _geoms = _g.drop_duplicates('jpl_burst_id').geometry.tolist()
            _union_utm = _shp_transform(to_utm, unary_union(_geoms))
            _frac = _union_utm.intersection(tile_utm).area / tile_area
        except Exception:
            keep_tokens.append(_tok)  # never drop on a computation error
            continue
        if _frac < min_frac:
            dropped.append((_tok, _frac))
        else:
            keep_tokens.append(_tok)

    if dropped:
        for _tok, _frac in sorted(dropped, key=lambda x: x[1]):
            logger.info(
                "  [Coverage] dropping track TK%s for %s: %.1f%% < %.1f%% of tile",
                str(_tok).replace('_', '-'), mgrs_tile_id, _frac * 100, min_frac * 100,
            )
        df_rtc_ts = df_rtc_ts[df_rtc_ts['track_token'].isin(keep_tokens)].reset_index(drop=True)
    # Record the drops on the frame so callers (run in quiet worker processes)
    # can surface a summary to the user even when worker logs aren't visible.
    try:
        df_rtc_ts.attrs['coverage_dropped'] = [
            {"track_token": str(_tok), "coverage_frac": round(float(_frac), 4)}
            for _tok, _frac in sorted(dropped, key=lambda x: x[1])
        ]
    except Exception:
        pass
    return df_rtc_ts


def query_rtc_metadata_for_tile(
    mgrs_tile_id: str,
    time_ranges: list[tuple[str, str]],
    config: dict
) -> gpd.GeoDataFrame:
    """
    Query RTC-S1 time series metadata for a single MGRS tile

    Args:
        mgrs_tile_id: MGRS tile ID
        time_ranges: [(start_date, end_date), ...] list of time range tuples
        config: Configuration dictionary

    Returns:
        gpd.GeoDataFrame: Merged metadata
    """
    roi_config = config.get('roi', {})
    polarization = roi_config.get('polarization', 'VV+VH')

    # Query robustness: ASF's CMR endpoint can be slow/flaky (especially behind a
    # VPN). Raise the per-request timeout and retry each time-range chunk with
    # exponential backoff before giving up. All configurable under `query:`.
    query_cfg = config.get('query', {}) or {}
    cmr_timeout = int(query_cfg.get('cmr_timeout_seconds', 90))
    max_retries = int(query_cfg.get('max_retries', 3))
    backoff = float(query_cfg.get('retry_backoff_seconds', 5))
    # Parallelize the per-burst CMR subqueries (identical merged result set).
    # Default 1 = serial single call, unchanged behaviour.
    query_workers = int(query_cfg.get('max_workers', 1))
    try:
        import asf_search
        asf_search.constants.INTERNAL.CMR_TIMEOUT = cmr_timeout
    except Exception as _te:
        logger.debug("Could not set CMR_TIMEOUT: %s", _te)

    all_metadata = []

    for start_date, end_date in time_ranges:
        logger.info("  Querying %s ~ %s...", start_date, end_date)

        df_chunk = None
        for _attempt in range(max_retries + 1):
            try:
                df_chunk = get_rtc_s1_ts_metadata_from_mgrs_tiles(
                    [mgrs_tile_id],
                    track_numbers=None,  # Get all tracks
                    start_acq_dt=start_date,
                    stop_acq_dt=end_date,
                    polarizations=polarization,
                    query_workers=query_workers,
                )
                break
            except Exception as e:
                if _attempt < max_retries:
                    _wait = backoff * (2 ** _attempt)
                    logger.warning(
                        "Query %s (%s ~ %s) attempt %d/%d failed: %s. Retrying in %.0fs…",
                        mgrs_tile_id, start_date, end_date,
                        _attempt + 1, max_retries + 1, e, _wait,
                    )
                    time.sleep(_wait)
                else:
                    logger.warning(
                        "Query failed for %s (%s ~ %s) after %d attempts: %s",
                        mgrs_tile_id, start_date, end_date, max_retries + 1, e,
                    )

        if df_chunk is None:
            continue
        if not df_chunk.empty:
            all_metadata.append(df_chunk)
            logger.info("    Found %d scenes", len(df_chunk))
        else:
            logger.warning("No data for %s (%s ~ %s)", mgrs_tile_id, start_date, end_date)

    if not all_metadata:
        warn(f"No RTC-S1 data found for tile: {mgrs_tile_id}")
        return gpd.GeoDataFrame()

    # Merge data from all time ranges
    df_rtc_ts = pd.concat(all_metadata, ignore_index=True)

    # Remove duplicates (by opera_id)
    if 'opera_id' in df_rtc_ts.columns:
        df_rtc_ts = df_rtc_ts.drop_duplicates(subset=['opera_id']).reset_index(drop=True)

    # Drop tracks that barely clip the tile (saves download + compute).
    _min_cov = float(roi_config.get('min_tile_coverage_frac', 0.0) or 0.0)
    _n_before = len(df_rtc_ts)
    df_rtc_ts = _filter_low_coverage_tracks(df_rtc_ts, mgrs_tile_id, _min_cov)
    if len(df_rtc_ts) != _n_before:
        logger.info(
            "Coverage filter (<%.0f%%): %d -> %d bursts for %s",
            _min_cov * 100, _n_before, len(df_rtc_ts), mgrs_tile_id,
        )

    logger.info("Total: %d scenes for %s", len(df_rtc_ts), mgrs_tile_id)
    return df_rtc_ts
