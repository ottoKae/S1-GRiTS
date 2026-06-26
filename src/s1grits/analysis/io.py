"""
IO utilities for loading and reading S1 monthly mosaic datasets

This module provides convenient functions to load Zarr/COG outputs
from the s1grits processing workflow. Supports both legacy monthly
outputs and the newer workflow_scenes output structure.
"""

import xarray as xr
import pandas as pd
from pathlib import Path
from typing import Any, Optional, List, Dict, Tuple

from s1grits.logger_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Zarr path resolution helpers
# ---------------------------------------------------------------------------

# Search order: new product structure first, then legacy fallback
_ZARR_CANDIDATES = {
    "scenes": [
        "scenes/zarr/S1_scenes.zarr",           # legacy pre-s1grits
    ],
    "monthly": [
        "zarr/s1grits_monthly.zarr",            # asf_output_writing legacy
    ],
}

# Glob pattern for new product-based zarr stores
_ZARR_GLOB_SCENES = "scenes_*/zarr/s1grits_scenes_*.zarr"
_ZARR_GLOB_SMONTHLY = "smonthly_*/zarr/s1grits_smonthly_*.zarr"
_ZARR_GLOB_STATIC = "static_*/zarr/s1grits_static_*.zarr"


def _resolve_zarr_path(tile_dir: Path, output_type: str) -> Optional[Path]:
    """
    Return the first existing Zarr path for the given output_type, or None.

    Searches new product-based structure first (scenes_*/zarr/), then
    legacy paths. If multiple product variants or track stores exist,
    the first one found is returned; use _list_scenes_zarrs() to
    enumerate all of them.
    """
    # Try new product structure first
    if output_type == "scenes":
        for p in sorted(tile_dir.glob(_ZARR_GLOB_SCENES)):
            return p
    elif output_type == "monthly":
        for p in sorted(tile_dir.glob(_ZARR_GLOB_SMONTHLY)):
            return p
    elif output_type == "static":
        for p in sorted(tile_dir.glob(_ZARR_GLOB_STATIC)):
            return p

    # Fallback: legacy paths
    candidates = _ZARR_CANDIDATES.get(output_type, [])
    for rel in candidates:
        p = tile_dir / rel
        if p.exists():
            return p

    return None


def _list_scenes_zarrs(scenes_zarr_dir: Path) -> list[Path]:
    """List all Zarr stores (handles new s1grits_ prefix + legacy naming)."""
    if not scenes_zarr_dir.exists():
        return []
    results = sorted(scenes_zarr_dir.glob("s1grits_scenes_*.zarr"))
    if not results:
        results = sorted(scenes_zarr_dir.glob("s1grits_acq_group_*.zarr"))  # legacy
    if not results:
        results = sorted(scenes_zarr_dir.glob("S1_scenes*.zarr"))
    return results


def _detect_available_types(tile_dir: Path) -> list[str]:
    """Return which output_types ('scenes', 'monthly') are available under tile_dir."""
    types = []
    if _resolve_zarr_path(tile_dir, "scenes"):
        types.append("scenes")
    if _resolve_zarr_path(tile_dir, "monthly"):
        types.append("monthly")
    if _resolve_zarr_path(tile_dir, "static"):
        types.append("static")
    return types


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_zarr_dataset(
    tile_id: str,
    direction: str,
    output_dir: str = "./output",
    output_type: str = "auto",
) -> xr.Dataset:
    """
    Load Zarr dataset for a specific MGRS tile and flight direction.

    Supports both the legacy flat output layout and the newer
    workflow_scenes layout (scenes/monthly subdirectories).

    Args:
        tile_id: MGRS tile ID (e.g., "17MPV")
        direction: Flight direction ("ASCENDING" or "DESCENDING")
        output_dir: Output root directory (default: "./output")
        output_type: Which product to load:
            - "auto" (default): try scenes first, then monthly, then legacy
            - "scenes": per-acquisition scenes Zarr
            - "monthly": monthly composite Zarr

    Returns:
        xarray.Dataset containing VV_dB, VH_dB, time coordinates, and attributes

    Raises:
        FileNotFoundError: If no matching Zarr dataset exists

    Example:
        >>> ds = load_zarr_dataset("17MPV", "DESCENDING")
        >>> ds_monthly = load_zarr_dataset("17MPV", "DESCENDING", output_type="monthly")
        >>> ds_scenes = load_zarr_dataset("17MPV", "DESCENDING", output_type="scenes")
    """
    tile_dir = Path(output_dir) / tile_id

    if output_type == "auto":
        # Try scenes first, then monthly (including legacy)
        for ot in ("scenes", "monthly"):
            zarr_path = _resolve_zarr_path(tile_dir, ot)
            if zarr_path is not None:
                output_type = ot
                break
        else:
            raise FileNotFoundError(
                f"No Zarr dataset found for {tile_id}_{direction}\n"
                f"Available tiles: {list_available_tiles(output_dir)}"
            )
    else:
        zarr_path = _resolve_zarr_path(tile_dir, output_type)

    if zarr_path is None:
        available = _detect_available_types(tile_dir)
        raise FileNotFoundError(
            f"Zarr dataset not found for output_type='{output_type}': "
            f"{tile_dir}\n"
            f"Available types: {available if available else 'none'}"
        )

    logger.info("Loading Zarr [%s]: %s", output_type, zarr_path)
    ds = xr.open_zarr(zarr_path)

    logger.info("Dataset shape: %s (time, y, x)", ds['VV_dB'].shape)
    if 'time' in ds and len(ds.time) > 0:
        logger.info("Time range: %s to %s", ds.time.values[0], ds.time.values[-1])
    logger.info("Variables: %s", list(ds.data_vars))

    return ds


def load_catalog(output_dir: str = "./output") -> pd.DataFrame:
    """
    Load the global catalog.parquet file

    Args:
        output_dir: Output root directory (default: "./output")

    Returns:
        pandas.DataFrame with catalog metadata

    Raises:
        FileNotFoundError: If catalog does not exist

    Example:
        >>> cat = load_catalog()
        >>> print(cat[['tile_id', 'flight_direction', 'month']].head())
    """
    catalog_path = Path(output_dir) / "catalog.parquet"

    if not catalog_path.exists():
        raise FileNotFoundError(
            f"Catalog not found: {catalog_path}\n"
            f"Run 's1grits catalog resync --output-dir {output_dir}' to generate it"
        )

    cat = pd.read_parquet(catalog_path)

    logger.info("Loaded catalog: %d records", len(cat))
    logger.info("Tiles: %d", cat['tile_id'].nunique())
    if 'flight_direction' in cat.columns:
        logger.info("Directions: %s", cat['flight_direction'].unique().tolist())
    if 'month' in cat.columns:
        logger.info("Months: %d", cat['month'].nunique())
    if 'output_type' in cat.columns:
        logger.info("Output types: %s", cat['output_type'].unique().tolist())

    return cat


def list_available_tiles(output_dir: str = "./output") -> List[Dict[str, str]]:
    """
    List all available tiles in the output directory.

    Checks both legacy flat layout and the newer workflow_scenes layout
    (scenes/monthly subdirectories). Returns tiles that have at least one
    readable Zarr store.

    Args:
        output_dir: Output root directory (default: "./output")

    Returns:
        List of dicts with keys:
            - tile_id: MGRS tile ID (e.g., "17MPV")
            - direction: Flight direction
            - path: Absolute path to the tile directory
            - output_types: List of available types ("scenes", "monthly")

    Example:
        >>> tiles = list_available_tiles()
        >>> for tile in tiles:
        >>>     print(f"{tile['tile_id']} - {tile['direction']} "
        >>>           f"types={tile['output_types']}")
    """
    output_root = Path(output_dir)

    if not output_root.exists():
        return []

    tiles = []
    for item in output_root.iterdir():
        if not item.is_dir() or "_" not in item.name:
            continue

        # Parse tile_id and direction from folder name
        # Format: {tile_id}_{direction}
        parts = item.name.split("_")
        if len(parts) < 2:
            continue

        types_available = _detect_available_types(item)
        if not types_available:
            continue

        tile_id = parts[0]
        direction = "_".join(parts[1:])
        tiles.append({
            'tile_id': tile_id,
            'direction': direction,
            'path': str(item),
            'output_types': types_available,
        })

    return sorted(tiles, key=lambda x: (x['tile_id'], x['direction']))


def get_zarr_info(zarr_path: str) -> Dict[str, Any]:
    """
    Get basic information about a Zarr dataset without loading all data

    Args:
        zarr_path: Path to Zarr dataset

    Returns:
        dict with dataset metadata

    Example:
        >>> info = get_zarr_info("./output/17MPV_DESCENDING/zarr/S1_monthly.zarr")
        >>> print(info['time_steps'])
    """
    with xr.open_zarr(zarr_path) as ds:
        info = {
            'variables': list(ds.data_vars),
            'coordinates': list(ds.coords),
            'dims': dict(ds.dims),
            'time_steps': len(ds.time) if 'time' in ds else 0,
            'time_range': (
                str(ds.time.values[0]),
                str(ds.time.values[-1])
            ) if 'time' in ds and len(ds.time) > 0 else None,
            'spatial_shape': (ds.dims.get('y', 0), ds.dims.get('x', 0)),
            'attrs': dict(ds.attrs),
        }

    return info


def find_tile_by_lonlat(
    lon: float,
    lat: float,
    output_dir: str = "./output"
) -> Optional[Tuple[str, str]]:
    """
    Find which MGRS tile contains a given lon/lat coordinate.

    Searches the global catalog for tiles whose bounding box contains
    the given coordinate. Returns the first match sorted by tile_id.

    Args:
        lon: Longitude (WGS84)
        lat: Latitude (WGS84)
        output_dir: Output root directory

    Returns:
        Tuple of (tile_id, direction) or None if not found

    Raises:
        FileNotFoundError: If catalog does not exist in output_dir

    Example:
        >>> result = find_tile_by_lonlat(-78.5, -2.1)
        >>> if result:
        ...     tile_id, direction = result
    """
    import pyproj
    from shapely.geometry import Point

    catalog_path = Path(output_dir) / "catalog.parquet"
    if not catalog_path.exists():
        raise FileNotFoundError(f"Catalog not found: {catalog_path}")

    cat = pd.read_parquet(catalog_path)
    if cat.empty:
        return None

    point_wgs84 = Point(lon, lat)

    for _, row in cat.drop_duplicates(subset=["tile_id", "flight_direction"]).iterrows():
        try:
            t = list(row["transform"])
            w = int(row["width"])
            h = int(row["height"])
            crs_str = row["crs"]

            src_crs = pyproj.CRS.from_user_input(crs_str)
            dst_crs = pyproj.CRS.from_epsg(4326)
            tr = pyproj.Transformer.from_crs(src_crs, dst_crs, always_xy=True)

            x_min, x_max = t[2], t[2] + t[0] * w
            y_max, y_min = t[5], t[5] + t[4] * h

            corners_x = [x_min, x_max, x_max, x_min]
            corners_y = [y_min, y_min, y_max, y_max]
            lons, lats = tr.transform(corners_x, corners_y)

            bbox_west = min(lons)
            bbox_east = max(lons)
            bbox_south = min(lats)
            bbox_north = max(lats)

            if bbox_west <= lon <= bbox_east and bbox_south <= lat <= bbox_north:
                return (row["tile_id"], row.get("flight_direction", ""))
        except Exception as _e:
            logger.debug("Coordinate lookup skipped for row: %s", _e)
            continue

    return None
