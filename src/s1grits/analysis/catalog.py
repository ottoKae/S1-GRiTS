"""
Catalog management API

This module provides functions for managing the global catalog.parquet file
and per-tile catalog files.

Functions:
    rebuild_global_catalog: Rebuild global catalog from tile catalogs
    validate_catalog: Validate catalog structure and integrity
    get_catalog_statistics: Get statistics from catalog
"""

from pathlib import Path
from typing import Any, Dict, Optional, Union
import pandas as pd
import rasterio

from s1grits.logger_config import get_logger

logger = get_logger(__name__)


def _guess_month(filename: str, tile_id: str, direction: str) -> Optional[str]:
    """
    Extract YYYY-MM from a monthly COG filename.

    Recognised patterns:
      - New:  {tile}_S1_Monthly_{direction}_{YYYY-MM}.tif
      - Old:  {tile}_S1_Monthly_{direction}_{YYYY-MM}.tif  (same)
    """
    marker = f"{tile_id}_S1_Monthly"
    if marker in filename:
        suffix = filename.rsplit("_", 1)[-1]
        if len(suffix) == 7 and suffix[4] == "-":
            return suffix
    return None


def _guess_datetime_str(filename: str, tile_id: str, direction: str) -> Optional[str]:
    """
    Extract YYYYMMDDTHHMMSS from a scenes COG filename.

    Pattern: {tile}_{direction}_{YYYYMMDDTHHMMSS}.tif
    """
    prefix = f"{tile_id}_{direction}_"
    if filename.startswith(prefix):
        date_part = filename[len(prefix):]
        # date_part looks like "20210103T045522"
        if len(date_part) == 15 and "T" in date_part:
            return date_part
    return None


def _rebuild_records_from_cog_dir(
    cog_dir: Path,
    zarr_dir: Path,
    preview_dir: Path,
    mgrs_tile_id: str,
    flight_direction: str,
    output_type: str,
    zarr_store_name: str = "S1_monthly.zarr",
) -> list[dict]:
    """
    Build catalog records from a single COG directory.

    Args:
        output_type: 'scenes' or 'monthly'.
        zarr_store_name: e.g. 'S1_scenes.zarr' or 'S1_monthly.zarr'.
    """
    cog_files = sorted(cog_dir.glob("*.tif"))
    records = []

    for cog_file in cog_files:
        try:
            filename = cog_file.stem

            # Determine time label
            month = None
            dt_str = None
            if output_type == "monthly":
                month = _guess_month(filename, mgrs_tile_id, flight_direction)
                if month is None:
                    # Fallback: try last underscore-separated segment
                    parts = filename.split("_")
                    last = parts[-1]
                    if len(last) >= 7 and last[4] == "-":
                        month = last[:7]
                if month is None:
                    continue
                dt = pd.Timestamp(f"{month}-01")
            else:
                dt_str = _guess_datetime_str(filename, mgrs_tile_id, flight_direction)
                if dt_str is None:
                    continue
                dt = pd.Timestamp(dt_str)
                month = dt.strftime("%Y-%m")

            # Read spatial metadata from COG
            with rasterio.open(cog_file) as src:
                crs = src.crs.to_string()
                width = src.width
                height = src.height
                transform = src.transform

            # Zarr path (may not exist; record the expected location)
            zarr_full = zarr_dir / zarr_store_name
            zarr_path = str(zarr_full) if zarr_full.exists() else str(zarr_full)

            # Preview
            preview_name = f"{month}.png" if month else f"{dt_str}.png"
            preview_full = preview_dir / preview_name
            preview_path = str(preview_full) if preview_full.exists() else ""

            record = {
                "tile_id": mgrs_tile_id,
                "flight_direction": flight_direction,
                "month": month,
                "datetime": dt,
                "cog_path": str(cog_file),
                "zarr_path": zarr_path,
                "preview_path": preview_path,
                "crs": crs,
                "width": width,
                "height": height,
                "transform": list(transform)[:6],
                "output_type": output_type,
                "preview_crs": "EPSG:4326",
                "preview_width": 0,
                "preview_height": 0,
                "preview_bounds": None,
            }
            records.append(record)
        except Exception:
            logger.debug(
                "Skipping COG file %s: failed to read metadata",
                cog_file, exc_info=True,
            )
            continue

    return records


def rebuild_tile_catalog_from_cogs(tile_dir: Path) -> Optional[pd.DataFrame]:
    """
    Rebuild a single tile's catalog from its COG files.

    Supports both the legacy flat layout and the newer workflow_scenes
    layout (scenes/ + monthly/ subdirectories).

    Args:
        tile_dir: Path to tile directory (e.g., output/50RKV_ASCENDING)

    Returns:
        DataFrame with catalog records, or None if no COG files found
    """
    # Extract tile info from directory name
    tile_name = tile_dir.name
    parts = tile_name.split("_")
    if len(parts) < 2:
        return None

    mgrs_tile_id = parts[0]
    flight_direction = parts[1]

    all_records = []

    # --- New workflow_scenes layout: scenes/ subdirectory ---
    scenes_cog = tile_dir / "scenes" / "cog"
    if scenes_cog.exists():
        all_records.extend(
            _rebuild_records_from_cog_dir(
                scenes_cog,
                tile_dir / "scenes" / "zarr",
                tile_dir / "scenes" / "preview",
                mgrs_tile_id,
                flight_direction,
                output_type="scenes",
                zarr_store_name="S1_scenes.zarr",
            )
        )

    # --- New workflow_scenes layout: monthly/ subdirectory ---
    monthly_cog = tile_dir / "monthly" / "cog"
    if monthly_cog.exists():
        all_records.extend(
            _rebuild_records_from_cog_dir(
                monthly_cog,
                tile_dir / "monthly" / "zarr",
                tile_dir / "monthly" / "preview",
                mgrs_tile_id,
                flight_direction,
                output_type="monthly",
                zarr_store_name="S1_monthly.zarr",
            )
        )

    # --- Legacy flat layout (no scenes/monthly nesting) ---
    flat_cog = tile_dir / "cog"
    if flat_cog.exists():
        all_records.extend(
            _rebuild_records_from_cog_dir(
                flat_cog,
                tile_dir / "zarr",
                tile_dir / "preview",
                mgrs_tile_id,
                flight_direction,
                output_type="monthly",
                zarr_store_name="S1_monthly.zarr",
            )
        )

    if not all_records:
        return None

    return pd.DataFrame(all_records)


def rebuild_global_catalog(output_dir: Union[str, Path]) -> Dict[str, Any]:
    """
    Rebuild global catalog from COG files in all tile directories.

    This function:
    1. Scans all tile directories for COG files
    2. Rebuilds each tile's catalog.parquet from its COG files
    3. Merges all tile catalogs into a global catalog.parquet

    Args:
        output_dir: Output directory path containing tile subdirectories

    Returns:
        dict: Result dictionary with keys:
            - success (bool): Whether operation succeeded
            - message (str): Status message
            - catalog_path (Path): Path to global catalog (if successful)
            - total_records (int): Total number of records (if successful)
            - tile_count (int): Number of tiles (if successful)
            - month_count (int): Number of unique months (if successful)
            - tiles (list): List of tile IDs (if successful)
            - directions (list): List of flight directions (if successful)

    Example:
        >>> result = rebuild_global_catalog("./output")
        >>> if result['success']:
        ...     print(f"Rebuilt catalog with {result['total_records']} records")
    """
    output_root = Path(output_dir)

    if not output_root.exists():
        return {
            'success': False,
            'message': f"Output directory does not exist: {output_root}"
        }

    # Find all tile directories (format: {TILE}_{DIRECTION})
    tile_dirs = [d for d in output_root.iterdir()
                 if d.is_dir() and '_' in d.name and not d.name.startswith('.')]

    if not tile_dirs:
        return {
            'success': False,
            'message': f'No tile directories found in {output_dir}'
        }

    all_catalogs = []
    rebuilt_count = 0

    for tile_dir in sorted(tile_dirs):
        # Rebuild tile catalog from COG files
        tile_catalog = rebuild_tile_catalog_from_cogs(tile_dir)

        if tile_catalog is not None and len(tile_catalog) > 0:
            # Save tile catalog
            tile_catalog_path = tile_dir / 'catalog.parquet'
            tile_catalog.to_parquet(tile_catalog_path, index=False)
            all_catalogs.append(tile_catalog)
            rebuilt_count += 1

    if not all_catalogs:
        return {
            'success': False,
            'message': 'No valid tile catalogs found to merge'
        }

    # Merge all catalogs
    try:
        merged = pd.concat(all_catalogs, ignore_index=True)

        # Sort by mgrs_tile_id and datetime
        merged = merged.sort_values(["tile_id", "datetime"]).reset_index(drop=True)

        # Save global catalog
        global_catalog_path = output_root / "catalog.parquet"
        merged.to_parquet(global_catalog_path, index=False)

        # Collect statistics
        month_count = (
            merged['month'].nunique()
            if 'month' in merged.columns
            else merged['datetime'].dt.to_period('M').nunique()
        )
        result = {
            'success': True,
            'message': f"Successfully rebuilt global catalog with {len(merged)} records",
            'catalog_path': global_catalog_path,
            'total_records': len(merged),
            'tile_count': merged['tile_id'].nunique(),
            'month_count': month_count,
            'tiles': sorted(merged['tile_id'].unique().tolist()),
        }

        # Add flight direction info if available
        if 'flight_direction' in merged.columns:
            result['directions'] = merged['flight_direction'].unique().tolist()

        # Rebuild STAC Items and Collection JSON in sync with catalog
        try:
            from s1grits.stac_builder import rebuild_stac_from_catalog
            rebuild_stac_from_catalog(str(output_root))
        except Exception as _stac_e:
            logger.warning("STAC rebuild failed (catalog rebuild succeeded): %s", _stac_e)

        return result

    except Exception as e:
        return {
            'success': False,
            'message': f"Failed to merge catalogs: {str(e)}"
        }


def validate_catalog(catalog_path: Union[str, Path]) -> Dict[str, Any]:
    """
    Validate catalog structure and integrity

    Checks that the catalog file exists, is readable, and contains
    expected columns and data types.

    Args:
        catalog_path: Path to catalog.parquet file

    Returns:
        dict: Validation result with keys:
            - valid (bool): Whether catalog is valid
            - issues (list): List of validation issues found
            - warnings (list): List of warnings
            - record_count (int): Number of records (if readable)
            - columns (list): List of column names (if readable)

    Example:
        >>> result = validate_catalog("./output/catalog.parquet")
        >>> if not result['valid']:
        ...     for issue in result['issues']:
        ...         print(f"Issue: {issue}")
    """
    catalog_path = Path(catalog_path)
    issues = []
    warnings = []

    # Check if file exists
    if not catalog_path.exists():
        return {
            'valid': False,
            'issues': [f"Catalog file does not exist: {catalog_path}"],
            'warnings': []
        }

    # Try to read catalog
    try:
        df = pd.read_parquet(catalog_path)
    except Exception as e:
        return {
            'valid': False,
            'issues': [f"Failed to read catalog: {str(e)}"],
            'warnings': []
        }

    # Check for required columns (canonical schema)
    required_columns = ['tile_id', 'product_type', 'zarr_path']
    optional_columns = ['tile_id', 'item_id', 'collection_id', 'datetime',
                        'month', 'flight_direction', 'output_type', 'product_label',
                        'schema_version', 'status']
    missing_columns = [col for col in required_columns if col not in df.columns]

    if missing_columns:
        issues.append(f"Missing required columns: {missing_columns}")

    for col in optional_columns:
        if col not in df.columns:
            warnings.append(f"Optional column '{col}' is missing")

    # Check for empty catalog
    if len(df) == 0:
        warnings.append("Catalog is empty (0 records)")

    # Check datetime column
    if 'datetime' in df.columns:
        try:
            pd.to_datetime(df['datetime'])
        except Exception as _e:
            logger.debug("datetime column validation failed: %s", _e)
            issues.append("datetime column contains invalid values")

    # Check for duplicate records
    dup_subset = ['tile_id', 'datetime']
    if 'flight_direction' in df.columns:
        dup_subset.append('flight_direction')
    if 'output_type' in df.columns:
        dup_subset.append('output_type')
    if all(c in df.columns for c in dup_subset):
        duplicates = df.duplicated(subset=dup_subset)
        if duplicates.any():
            warnings.append(f"Found {duplicates.sum()} duplicate records")

    # Check for missing values in critical columns
    for col in required_columns:
        if col in df.columns:
            null_count = df[col].isnull().sum()
            if null_count > 0:
                warnings.append(f"Column '{col}' has {null_count} null values")

    return {
        'valid': len(issues) == 0,
        'issues': issues,
        'warnings': warnings,
        'record_count': len(df),
        'columns': df.columns.tolist()
    }


def get_catalog_statistics(catalog_path: Union[str, Path]) -> Dict[str, Any]:
    """
    Get statistics from catalog

    Computes various statistics about the data coverage, including
    tile counts, date ranges, and temporal coverage.

    Args:
        catalog_path: Path to catalog.parquet file

    Returns:
        dict: Statistics dictionary with keys:
            - success (bool): Whether operation succeeded
            - message (str): Status message
            - total_records (int): Total number of records
            - tile_count (int): Number of unique tiles
            - date_range (tuple): (min_date, max_date)
            - month_count (int): Number of unique months
            - tiles (dict): Per-tile statistics
            - directions (list): Flight directions (if available)

    Example:
        >>> stats = get_catalog_statistics("./output/catalog.parquet")
        >>> if stats['success']:
        ...     print(f"Coverage: {stats['date_range'][0]} to {stats['date_range'][1]}")
    """
    catalog_path = Path(catalog_path)

    if not catalog_path.exists():
        return {
            'success': False,
            'message': f"Catalog file does not exist: {catalog_path}"
        }

    try:
        df = pd.read_parquet(catalog_path)
        df['datetime'] = pd.to_datetime(df['datetime'])

        # Overall statistics
        stats = {
            'success': True,
            'message': "Statistics computed successfully",
            'total_records': len(df),
            'tile_count': df['tile_id'].nunique(),
            'date_range': (
                df['datetime'].min().strftime('%Y-%m-%d'),
                df['datetime'].max().strftime('%Y-%m-%d')
            ),
        }

        if 'month' in df.columns:
            stats['month_count'] = df['month'].nunique()
        else:
            stats['month_count'] = df['datetime'].dt.to_period('M').nunique()

        if 'output_type' in df.columns:
            stats['output_types'] = df['output_type'].unique().tolist()

        # Add flight direction info if available
        if 'flight_direction' in df.columns:
            stats['directions'] = df['flight_direction'].unique().tolist()

        # Per-tile statistics
        tile_stats = {}
        for tile_id in sorted(df['tile_id'].unique()):
            tile_df = df[df['tile_id'] == tile_id]

            tile_info = {
                'record_count': len(tile_df),
                'date_range': (
                    tile_df['datetime'].min().strftime('%Y-%m-%d'),
                    tile_df['datetime'].max().strftime('%Y-%m-%d')
                ),
            }

            if 'month' in tile_df.columns:
                tile_info['month_count'] = tile_df['month'].nunique()
            else:
                tile_info['month_count'] = tile_df['datetime'].dt.to_period('M').nunique()

            if 'output_type' in tile_df.columns:
                tile_info['output_types'] = tile_df['output_type'].unique().tolist()

            # Add per-direction stats if available
            if 'flight_direction' in df.columns:
                tile_info['by_direction'] = {}
                for direction in tile_df['flight_direction'].unique():
                    dir_df = tile_df[tile_df['flight_direction'] == direction]
                    dir_info = {
                        'record_count': len(dir_df),
                        'month_count': dir_df['datetime'].dt.to_period('M').nunique(),
                    }
                    if 'output_type' in dir_df.columns:
                        dir_info['output_types'] = dir_df['output_type'].unique().tolist()
                    tile_info['by_direction'][direction] = dir_info

            tile_stats[tile_id] = tile_info

        stats['tiles'] = tile_stats

        return stats

    except Exception as e:
        return {
            'success': False,
            'message': f"Failed to compute statistics: {str(e)}"
        }


# ---------------------------------------------------------------------------
# Geometry-group sibling index — for STAC `related` cross-links
# ---------------------------------------------------------------------------

def _geometry_group_sibling_index(df) -> dict:
    """Map ``(tile_id, geometry_group_id)`` → ``{product_type: {item_rel,
    product_type}}`` with ONE representative item per product_type (first seen).

    Products of the same geometry group (scenes/smonthly and their auxiliary
    static layers, joined on the track-level ``geometry_group_id``) get STAC
    ``related`` cross-links to each other. Bounded to one link per sibling
    product_type so a many-acquisition scenes group does not bloat items.
    """
    idx: dict = {}
    _cols = {"tile_id", "geometry_group_id", "product_type", "item_path"}
    if not _cols <= set(df.columns):
        return idx
    for _, row in df.iterrows():
        ggid, tid = row.get("geometry_group_id"), row.get("tile_id")
        pt, ip = row.get("product_type"), row.get("item_path")
        if not (ggid and tid and pt and ip) or pd.isna(ggid) or pd.isna(ip):
            continue
        reps = idx.setdefault((str(tid), str(ggid)), {})
        reps.setdefault(str(pt), {
            "item_rel": f"{tid}/{ip}".replace("\\", "/"),
            "product_type": str(pt),
        })
    return idx


def _siblings_for(idx: dict, record: dict) -> list:
    """The geometry-group siblings of ``record`` (excluding its own product_type)."""
    reps = idx.get(
        (str(record.get("tile_id")), str(record.get("geometry_group_id"))), {}
    )
    _own = str(record.get("product_type"))
    return [v for k, v in reps.items() if k != _own]


# ---------------------------------------------------------------------------
# Filesystem resync — rebuild catalog + STAC from actual Zarr/COG data
# ---------------------------------------------------------------------------

def resync_catalog_from_filesystem(
    output_dir: Union[str, Path],
    write_stac: bool = True,
    stac_format: str = "geoparquet",
) -> Dict[str, Any]:
    """
    Scan the filesystem and rebuild per-tile + global catalog and STAC.

    Authoritative reconcile: the catalog (and, unless ``write_stac`` is
    False, the STAC tree) is made to mirror EXACTLY what is on disk. Records
    whose Zarr store no longer exists are dropped (the catalog is rebuilt
    from the surviving stores), and stale STAC artifacts — orphaned item
    JSONs, collection directories no longer backed by data, and dangling
    root-catalog child links — are pruned.

    Does NOT re-run any workflow — only reads existing Zarr stores, COG
    files, and product directories to reconstruct metadata.

    Args:
        output_dir: Path to the DataCube root directory.
        write_stac: When True (default), rebuild the STAC tree (items,
            per-collection collection.json, root catalog) and prune stale
            STAC. When False, reconcile only ``catalog.parquet`` and DELETE
            every STAC artifact (root catalog.json, collections/, and each
            tile's items/) — a catalog-only datacube.

        stac_format: STAC item serialization when write_stac is True.
            'geoparquet' (default) writes one stac-geoparquet file per
            collection (collections/{cid}/items.parquet) and NO per-item JSON —
            zero small files, queryable by DuckDB/pyarrow. 'json' writes the
            classic one-JSON-file-per-item static catalog.

    Returns:
        dict with keys: success, message, total_records, tile_count, tiles.
    """
    from s1grits.file_lock import output_lock

    output_root = Path(output_dir)
    if not output_root.exists():
        return {'success': False, 'message': f'Directory not found: {output_root}'}

    if stac_format not in ("geoparquet", "json"):
        return {'success': False, 'message': f"Invalid stac_format: {stac_format!r} (use 'geoparquet' or 'json')"}

    _skip_dirs = ('catalog.json', 'collection.json', 'collections', '.s1grits.lock')
    with output_lock(output_root):
        return _resync_locked(output_root, _skip_dirs, write_stac, stac_format)


def _resync_locked(output_root, _skip_dirs, write_stac, stac_format) -> Dict[str, Any]:
    """Body of resync, run while holding the output lock."""
    import json as _json  # noqa: F401  (kept for parity / future use)
    import shutil as _shutil
    from s1grits.canonical_catalog_schema import normalize_catalog_record
    from s1grits.stac_builder import (
        write_stac_item, write_stac_collection, build_stac_item_dict,
        set_stac_output_enabled,
    )
    from s1grits.catalog_sync import update_root_catalog

    # resync is the ONLY place that writes STAC. Workflows always leave the
    # process-level switch OFF, so enable it here when write_stac is requested
    # (the STAC file writers are gated on this switch).
    set_stac_output_enabled(write_stac)

    tile_dirs = sorted(d for d in output_root.iterdir()
                       if d.is_dir() and not d.name.startswith('.')
                       and d.name not in _skip_dirs)

    all_catalogs = []

    for tile_dir in tile_dirs:
        tile_id = tile_dir.name
        catalog_records = []

        # Scan product directories
        _valid_product_prefixes = ('scenes_', 'smonthly_', 'static_', 'monthly_')
        for prod_dir in sorted(tile_dir.iterdir()):
            if not prod_dir.is_dir() or not prod_dir.name.startswith(_valid_product_prefixes):
                continue
            product_label = prod_dir.name
            zarr_dir = prod_dir / 'zarr'
            if not zarr_dir.exists():
                continue

            # Determine product_type from directory name
            if product_label.startswith('scenes_'):
                product_type = 'scenes'
            elif product_label.startswith('smonthly_'):
                product_type = 'smonthly'
            elif product_label.startswith('static_'):
                product_type = 'static'
            elif product_label.startswith('monthly_'):
                product_type = 'monthly'
            else:
                continue

            # Parse direction from product_label
            _parts = product_label.split('_')
            flight_direction = _parts[1] if len(_parts) > 1 and _parts[1] in ('ASCENDING', 'DESCENDING') else None

            # Find Zarr stores
            for zarr_p in sorted(zarr_dir.glob('*.zarr')):
                try:
                    import zarr
                    g = zarr.open_group(str(zarr_p), mode='r')
                except Exception as _e:
                    logger.warning("Cannot open Zarr %s: %s", zarr_p, _e)
                    continue

                # Extract metadata from Zarr. zarr.keys() iteration order is NOT
                # insertion order, so canonicalize: VV/VH (or HH/HV) lead, then
                # Ratio/RVI, then the rest — a stable, deterministic band order.
                from s1grits.stac_builder import _canonical_band_order
                bands = _canonical_band_order(
                    [k for k in g.keys() if k not in ('x', 'y', 'time')]
                )
                has_time = 'time' in g

                # Read variant metadata from Zarr attrs (written by v3 workflows)
                _processing_sig = g.attrs.get('processing_signature', None)
                _product_variant = g.attrs.get('product_variant', None)
                _processing_variant_json = g.attrs.get('processing_variant_json', None)
                _product_label_from_attrs = g.attrs.get('product_label', None)

                time_steps = []
                if has_time and g['time'].shape[0] > 0:
                    try:
                        time_steps = sorted(pd.to_datetime(g['time'][:]).tolist())
                    except Exception:
                        pass

                start_dt = min(time_steps) if time_steps else None
                end_dt = max(time_steps) if time_steps else None
                dt_val = time_steps[len(time_steps)//2] if time_steps else None

                # Parse TK/N from zarr name or product_label
                _zname = zarr_p.name.replace('.zarr', '')
                import re
                _tk_match = re.search(r'TK([\d\-]+)', _zname)
                _nn_match = re.search(r'_N(\d+)', _zname)
                if _tk_match:
                    _tk_str = _tk_match.group(1)
                    track_val = int(_tk_str.split('-')[0]) if '-' in _tk_str else int(_tk_str)
                else:
                    track_val = None
                # n_bursts appears in static store names (_N{nn}) and in legacy
                # scenes/smonthly stores; current scenes/smonthly stores key on
                # the track alone (n_bursts is time-varying) and carry n_bursts
                # as Zarr provenance instead. Fall back to the attr, and keep
                # the _N segment out of the identity IDs whenever the store
                # name omits it so a track-only store is not crash-formatted
                # (None:02d) nor split by a fabricated burst count.
                n_bursts_val = int(_nn_match.group(1)) if _nn_match else None
                if n_bursts_val is None:
                    _attr_nb = g.attrs.get('n_bursts', None)
                    n_bursts_val = int(_attr_nb) if _attr_nb else None
                _nn_suffix = f"_N{int(_nn_match.group(1)):02d}" if _nn_match else ""

                item_id = f"{tile_id}_{flight_direction or 'UNK'}_TK{track_val}{_nn_suffix}_{product_type}" if track_val else f"{tile_id}_{flight_direction or 'UNK'}_{product_type}"

                # Check for COG files
                cog_dir = prod_dir / 'cog'
                cog_files = sorted(cog_dir.glob('*.tif')) if cog_dir.exists() else []
                cog_path = str(cog_files[0].relative_to(tile_dir)).replace('\\', '/') if cog_files else None

                # Build catalog record
                _grid_size = int(g['x'].shape[0]) if 'x' in g else None
                record = normalize_catalog_record({
                    'item_id': item_id,
                    'collection_id': f's1grits-{product_type}',
                    'product_type': product_type,
                    'product_label': _product_label_from_attrs or product_label,
                    'tile_id': tile_id,
                    'flight_direction': flight_direction,
                    'crs': str(g.attrs.get('crs', '')) if 'crs' in g.attrs else None,
                    'transform': list(g.attrs.get('transform', [])[:6]) if 'transform' in g.attrs else None,
                    'width': _grid_size,
                    'height': int(g['y'].shape[0]) if 'y' in g else None,
                    'grid_id': f"{tile_id}_native_{int(abs(float(g.attrs['transform'][0]))) if 'transform' in g.attrs else 30}m",
                    'grid_source': g.attrs.get('grid_source', None),
                    'reference_grid_id': g.attrs.get('reference_grid_id', None),
                    'reference_zarr_path': g.attrs.get('reference_zarr_path', None),
                    'datetime': dt_val,
                    'start_datetime': start_dt,
                    'end_datetime': end_dt,
                    'month': dt_val.strftime('%Y-%m') if dt_val else None,
                    # geometry_group_id is the TRACK-level join key shared by
                    # scenes/smonthly and their auxiliary static layers — the
                    # per-scene burst count (_N{nn}, present in static store
                    # names) stays in item_id/n_bursts as provenance, NOT here,
                    # so `static ⋈ scenes ON (tile_id, geometry_group_id)` works.
                    'geometry_group_id': (
                        g.attrs.get('geometry_group_id')
                        or (f"{tile_id}_{flight_direction or 'UNK'}_TK{track_val}" if track_val else None)
                    ),
                    'track': track_val,
                    'n_bursts': n_bursts_val,
                    'n_scenes': len(time_steps) if time_steps else None,
                    'zarr_path': str(zarr_p.relative_to(tile_dir)).replace('\\', '/'),
                    'cog_path': cog_path,
                    'preview_path': None,
                    'bands': _json.dumps(bands) if bands else None,
                    'processing_level': str(g.attrs.get('processing_level', '')) if 'processing_level' in g.attrs else None,
                    'product_variant': _product_variant,
                    'processing_signature': _processing_sig,
                    'processing_variant_json': _processing_variant_json,
                })
                # Set output_type so write_stac_item uses correct path logic
                record['output_type'] = product_type
                catalog_records.append(record)

                # STAC items are emitted after the full catalog is built (from
                # df_global), so the scan loop only reconstructs the catalog.

        # Write per-tile catalog
        if catalog_records:
            df_tile = pd.DataFrame(catalog_records)
            tile_catalog_path = tile_dir / 'catalog.parquet'
            df_tile.to_parquet(tile_catalog_path, index=False)
            all_catalogs.append(df_tile)
            logger.info("Tile catalog: %s (%d rows)", tile_catalog_path, len(df_tile))

    # Merge global catalog
    if not all_catalogs:
        return {'success': False, 'message': 'No Zarr stores found to catalog.'}

    df_global = pd.concat(all_catalogs, ignore_index=True)
    global_path = output_root / 'catalog.parquet'
    df_global.to_parquet(global_path, index=False)

    # Geometry-group sibling index for STAC `related` cross-links.
    _sib_index = _geometry_group_sibling_index(df_global)

    present_cids = (
        set(df_global["collection_id"].dropna().unique())
        if "collection_id" in df_global.columns else set()
    )

    if not write_stac:
        # Catalog-only datacube: delete every STAC artifact.
        _delete_all_stac(output_root, tile_dirs, _shutil)
    elif stac_format == "geoparquet":
        from s1grits.stac_geoparquet_io import write_items_geoparquet
        for _cid in sorted(present_cids):
            _df_sub = df_global[df_global["collection_id"] == _cid]
            if _df_sub.empty:
                continue
            coll_dir = output_root / "collections" / str(_cid)
            coll_dir.mkdir(parents=True, exist_ok=True)
            item_dicts = []
            for _, _row in _df_sub.iterrows():
                _rec = _row.to_dict()
                try:
                    _item, _ = build_stac_item_dict(
                        _rec, str(output_root),
                        tile_dir_override=_rec.get("tile_id"),
                        relative_to=str(coll_dir),
                        related_items=_siblings_for(_sib_index, _rec),
                    )
                    item_dicts.append(_item)
                except Exception as _ie:
                    logger.warning("STAC item build failed for %s: %s", _rec.get("item_id"), _ie)
            try:
                write_items_geoparquet(item_dicts, coll_dir / "items.parquet")
                write_stac_collection(_df_sub, str(output_root), str(_cid), items_parquet="items.parquet")
            except Exception as _ce:
                logger.warning("STAC geoparquet failed for %s: %s", _cid, _ce)

        update_root_catalog(output_root, df_global, prune=True)
        # geoparquet has no per-item JSON: drop any tile items/ dirs entirely,
        # plus collection dirs no longer backed by data.
        for _td in tile_dirs:
            _shutil.rmtree(_td / "items", ignore_errors=True)
        _prune_orphan_collections(output_root, present_cids, _shutil)
    else:  # stac_format == "json"
        written_item_abs: set = set()
        for _, _row in df_global.iterrows():
            _rec = _row.to_dict()
            try:
                _p = write_stac_item(
                    _rec, str(output_root), tile_dir_override=_rec.get("tile_id"),
                    related_items=_siblings_for(_sib_index, _rec),
                )
                written_item_abs.add(str(Path(_p).resolve()))
            except Exception as _ie:
                logger.warning("STAC item failed for %s: %s", _rec.get("item_id"), _ie)
        for _cid in sorted(present_cids):
            _df_sub = df_global[df_global["collection_id"] == _cid]
            if _df_sub.empty:
                continue
            try:
                write_stac_collection(_df_sub, str(output_root), str(_cid))
            except Exception as _ce:
                logger.warning("STAC collection failed for %s: %s", _cid, _ce)
            # drop a stale items.parquet left by a previous geoparquet run
            _stale_pq = output_root / "collections" / str(_cid) / "items.parquet"
            try:
                _stale_pq.unlink(missing_ok=True)
            except OSError:
                pass
        update_root_catalog(output_root, df_global, prune=True)
        _prune_orphan_items(tile_dirs, written_item_abs)
        _prune_orphan_collections(output_root, present_cids, _shutil)

    return {
        'success': True,
        'message': (
            f'Resynced {len(df_global)} records from {len(all_catalogs)} tile(s)'
            + ('' if write_stac else ' (catalog-only: STAC removed)')
            + (f' [STAC: {stac_format}]' if write_stac else '')
        ),
        'catalog_path': str(global_path),
        'total_records': len(df_global),
        'tile_count': len(all_catalogs),
        'stac': write_stac,
        'stac_format': stac_format if write_stac else None,
        'collections': sorted(present_cids),
        'collection_counts': {
            str(_cid): int((df_global["collection_id"] == _cid).sum())
            for _cid in sorted(present_cids)
        } if "collection_id" in df_global.columns else {},
        'tiles': sorted(d.name for d in tile_dirs if any(d.iterdir())),
    }


def _prune_orphan_items(tile_dirs, written_item_abs: set) -> None:
    """Delete item JSONs under any ``{tile}/items/`` that this resync did not
    (re)write — i.e. items whose backing Zarr no longer exists."""
    for tile_dir in tile_dirs:
        items_root = tile_dir / 'items'
        if not items_root.is_dir():
            continue
        for item_json in items_root.rglob('*.json'):
            if str(item_json.resolve()) not in written_item_abs:
                try:
                    item_json.unlink()
                    logger.info("Pruned orphan STAC item: %s", item_json)
                except OSError as _e:
                    logger.warning("Could not prune %s: %s", item_json, _e)


def _prune_orphan_collections(output_root, present_cids: set, _shutil) -> None:
    """Remove ``collections/{cid}`` dirs not backed by any present record."""
    collections_root = output_root / 'collections'
    if not collections_root.is_dir():
        return
    for coll_dir in collections_root.iterdir():
        if coll_dir.is_dir() and coll_dir.name not in present_cids:
            try:
                _shutil.rmtree(coll_dir)
                logger.info("Pruned orphan STAC collection: %s", coll_dir)
            except OSError as _e:
                logger.warning("Could not prune %s: %s", coll_dir, _e)


def _delete_all_stac(output_root, tile_dirs, _shutil) -> None:
    """Remove every STAC artifact, leaving catalog.parquet and data intact."""
    root_cat = output_root / 'catalog.json'
    try:
        root_cat.unlink(missing_ok=True)
    except OSError as _e:
        logger.warning("Could not remove %s: %s", root_cat, _e)
    _shutil.rmtree(output_root / 'collections', ignore_errors=True)
    for tile_dir in tile_dirs:
        _shutil.rmtree(tile_dir / 'items', ignore_errors=True)
    logger.info("catalog-only mode: removed all STAC artifacts under %s", output_root)
