"""
Mosaic module for combining multiple MGRS tiles into a single image

This module provides functionality to create virtual mosaics from monthly COG files
across multiple MGRS tiles, with strict validation rules.

Rules:
- Only tiles with the same flight direction can be mosaicked together
  (unless using "ALL" strategy which combines both directions)
- Only tiles from the same month can be mosaicked together
- Output is saved in analysis_results/mosaic/ directory
- Naming convention: TopLeft-BottomRight-YYYY-MM-DIRECTION

Direction Options:
- ASCENDING: Use only ascending orbit data
- DESCENDING: Use only descending orbit data
- ALL: Combine both directions (ASCENDING priority, DESCENDING fills gaps)
"""

import subprocess
import re
from pathlib import Path
from typing import List, Optional, Tuple, Dict
import tempfile

# Suppress GDAL 4.0 future warning about exceptions
try:
    from osgeo import gdal
    gdal.UseExceptions()
except ImportError:
    pass

from s1grits.logger_config import get_logger

logger = get_logger(__name__)


def plan_grid_mosaic(catalog_root, item_ids, output_file, *, target_crs,
                     resolution, bounds, output_format='VRT') -> dict:
    """Validate the existing fixed-grid contract and COG headers without writing."""
    import math
    from pyproj import CRS
    from s1grits.product_instance import make_processing_signature

    crs = CRS.from_user_input(target_crs)
    if not crs.is_projected or any(a.unit_conversion_factor != 1 for a in crs.axis_info[:2]):
        raise ValueError('Grid mosaic requires a projected target CRS in metres')
    resolution = float(resolution)
    bounds = tuple(float(v) for v in bounds)
    if (len(bounds) != 4 or not all(math.isfinite(v) for v in (*bounds, resolution))
            or resolution <= 0 or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
        raise ValueError('Provide finite ordered bounds and a positive metre resolution')
    shape = [(bounds[3]-bounds[1])/resolution, (bounds[2]-bounds[0])/resolution]
    if any(v < 1 or abs(v-round(v)) > 1e-7 for v in shape):
        raise ValueError('Bounds must define an integer number of target pixels')
    output_format = output_format.upper()
    if output_format not in ('VRT', 'COG'):
        raise ValueError('output_format must be VRT or COG')
    output = Path(output_file).resolve()
    if output.is_relative_to(Path(catalog_root).resolve()):
        raise ValueError('Write the mosaic outside the source Cube root')
    if output.suffix.lower() != ('.vrt' if output_format == 'VRT' else '.tif'):
        raise ValueError('Output suffix must be .vrt for VRT or .tif for COG')
    dependencies = output.with_name(output.name + '.sources')
    receipt = output.with_suffix(output.suffix + '.json')
    if any(p.exists() for p in (output, dependencies, receipt)):
        raise FileExistsError('Use a new output path; grid mosaics never overwrite products')
    records, sources, bands = _grid_mosaic_inputs(catalog_root, item_ids, crs, resolution, bounds)
    contract = {
        'version': 'inter_tile_grid_v1', 'crs': crs.to_wkt(), 'bounds': list(bounds),
        'resolution_m': resolution, 'shape': [round(v) for v in shape],
        'resampling': 'nearest', 'transform_tolerance_pixels': 0.0,
        'overlap': 'first_valid_per_band_in_item_order', 'bands': bands,
        'parent_processing_signature': records[0]['processing_signature'],
        'ordered_item_ids': list(item_ids),
    }
    signature = make_processing_signature(contract)
    return dict(contract=contract, processing_signature=signature, inputs=records, sources=sources,
                output=str(output), format=output_format)


def create_grid_mosaic(catalog_root, item_ids, output_file, *, target_crs,
                       resolution, bounds, output_format='VRT') -> str:
    """Map explicit COGs with per-band first-valid priority; retain VRT dependencies."""
    import json
    from pyproj import CRS
    from osgeo import gdal

    plan = plan_grid_mosaic(catalog_root, item_ids, output_file, target_crs=target_crs,
                            resolution=resolution, bounds=bounds, output_format=output_format)
    contract, signature = plan['contract'], plan['processing_signature']
    records, sources, bands = plan['inputs'], plan['sources'], contract['bands']
    crs, resolution, bounds = CRS.from_wkt(contract['crs']), contract['resolution_m'], contract['bounds']
    output, output_format = Path(plan['output']), plan['format']
    dependencies = output.with_name(output.name + '.sources')
    receipt = output.with_suffix(output.suffix + '.json')
    dependencies.mkdir(parents=True)
    warped = []
    for index, source in enumerate(sources):
        path = dependencies/f'{index:06d}.vrt'
        ds = gdal.Warp(str(path), source['path'], format='VRT', dstSRS=crs.to_wkt(),
                       outputBounds=source['target_bounds'], xRes=resolution, yRes=resolution,
                       resampleAlg='near', errorThreshold=0.0, dstNodata=float('nan'),
                       outputType=gdal.GDT_Float32, overviewLevel='NONE',
                       warpOptions=['UNIFIED_SRC_NODATA=NO'], warpMemoryLimit=64)
        if ds is None:
            raise RuntimeError(f'Failed to map {source["path"]}; partial outputs retained in {dependencies}')
        ds = None
        warped.append(str(path))
    vrt = output if output_format == 'VRT' else dependencies/'mosaic.vrt'
    # GDAL gives the last file priority, so reverse the declared first-valid order.
    ds = gdal.BuildVRT(str(vrt), warped[::-1], options=gdal.BuildVRTOptions(
        resolution='user', xRes=resolution, yRes=resolution, outputBounds=bounds,
        resampleAlg='nearest', srcNodata='nan', VRTNodata='nan', strict=True))
    if ds is None:
        raise RuntimeError(f'Failed to build mosaic; dependencies retained in {dependencies}')
    for index, band in enumerate(bands, 1):
        ds.GetRasterBand(index).SetDescription(band)
    ds.SetMetadataItem('S1GRITS_MOSAIC_SIGNATURE', signature)
    ds.FlushCache()
    ds = None
    fix_vrt_paths(str(vrt), make_absolute=True)
    if output_format == 'COG':
        ds = gdal.Translate(str(output), str(vrt), format='COG', creationOptions=[
            'COMPRESS=DEFLATE', 'BLOCKSIZE=512', 'BIGTIFF=IF_SAFER', 'RESAMPLING=NEAREST'])
        if ds is None:
            raise RuntimeError(f'Failed to materialize COG from {vrt}')
        ds = None
    # Check georeferencing without materializing the regional raster.
    import rasterio
    with rasterio.open(output) as result:
        expected = (resolution, 0, bounds[0], 0, -resolution, bounds[3])
        if (result.shape != tuple(contract['shape']) or tuple(result.transform)[:6] != expected
                or result.descriptions != tuple(bands)
                or CRS.from_user_input(result.crs) != crs):
            raise RuntimeError('Mosaic output does not match the requested grid/bands')
    receipt.write_text(json.dumps({
        'status': 'complete', 'processing_signature': signature, 'contract': contract,
        'catalog_root': str(Path(catalog_root).resolve()), 'inputs': records, 'sources': sources,
        'output': str(output), 'format': output_format, 'dependencies': str(dependencies),
        'coverage': 'Selected source footprints intersect bounds; pixel validity is not pre-scanned.',
        'source_integrity': 'Catalog identities and file size/mtime recorded; source bytes are not hashed.',
        'gdal_version': gdal.VersionInfo('--version'),
    }, indent=2, allow_nan=False), encoding='utf-8')
    return str(output)


def _grid_mosaic_inputs(catalog_root, item_ids, crs, resolution, bounds):
    """Validate selected identities, temporal meaning and headers before writing."""
    import json
    import math
    import pandas as pd
    import rasterio
    from rasterio.warp import transform_bounds
    from s1grits.resolver import CubeResolver

    if (not isinstance(item_ids, list) or not item_ids
            or any(not isinstance(v, str) or not v for v in item_ids)
            or len(set(item_ids)) != len(item_ids)):
        raise ValueError('item_ids must be a nonempty ordered list of distinct catalog IDs')
    root = Path(catalog_root).resolve()
    catalog = CubeResolver(root).catalog
    selected = catalog[catalog['item_id'].isin(item_ids)]
    if len(selected) != len(item_ids) or selected['item_id'].duplicated().any():
        raise ValueError('Requested catalog items are missing, incomplete or ambiguous')
    selected = selected.set_index('item_id').loc[item_ids].reset_index()
    for field in ('product_type', 'processing_signature', 'flight_direction'):
        if selected[field].isna().any() or selected[field].nunique() != 1 or not selected.iloc[0][field]:
            raise ValueError(f'Grid mosaic requires one explicit {field}')
    product = selected.iloc[0]['product_type']
    if product not in ('scenes', 'smonthly', 'monthly'):
        raise ValueError('Grid mosaic supports scientific scenes/monthly COGs only')
    if selected.iloc[0]['flight_direction'] not in ('ASCENDING', 'DESCENDING'):
        raise ValueError('Grid mosaic requires one explicit orbit direction')
    if selected['tile_id'].isna().any() or selected['tile_id'].duplicated().any():
        raise ValueError('Select exactly one product per tile; inter-pass fusion is separate')
    if product == 'scenes':
        dates = pd.to_datetime(selected['datetime'], utc=True, errors='coerce')
        if dates.isna().any() or dates.nunique() != 1:
            raise ValueError('Scenes require identical acquisition timestamps; no implicit time window')
        if selected['track'].isna().any() or selected['track'].nunique() != 1:
            raise ValueError('Scenes require the same relative orbit track')
    else:
        months = selected['month']
        if months.isna().any() or months.nunique() != 1:
            raise ValueError('Monthly inputs must describe the same month')
    records = json.loads(selected.to_json(orient='records', date_format='iso'))
    sources, bands = [], None
    for record in records:
        asset = record.get('cog_path')
        if not asset:
            raise ValueError(f'No COG asset for {record["item_id"]}')
        path = root/asset
        if not path.exists():
            path = root/record['tile_id']/asset
        path = path.resolve()
        with rasterio.open(path) as src:
            names = list(src.descriptions)
            if src.crs is None or any(n is None or n == '' for n in names):
                raise ValueError(f'COG needs georeferencing and named bands: {path}')
            if any(t != 'float32' for t in src.dtypes) or any(n is None for n in src.nodatavals):
                raise ValueError(f'Expected float32 scientific COG with declared NoData: {path}')
            expected_bands = record.get('bands')
            if isinstance(expected_bands, str):
                expected_bands = json.loads(expected_bands)
            expected_bands = [b for b in (expected_bands or [])
                              if b not in ('n_obs', 'n_contributors', 'n_candidates', 'source_index', 'weight_sum')]
            if names != expected_bands or (bands is not None and names != bands):
                raise ValueError('COG band names/order must match catalog and all selected inputs')
            bands = names
            left, bottom, right, top = transform_bounds(src.crs, crs, *src.bounds, densify_pts=21)
            # Snap each footprint to the requested lattice, keeping VRTs spatially bounded.
            left = max(bounds[0], bounds[0] + math.floor((left-bounds[0])/resolution)*resolution)
            right = min(bounds[2], bounds[0] + math.ceil((right-bounds[0])/resolution)*resolution)
            bottom = max(bounds[1], bounds[1] + math.floor((bottom-bounds[1])/resolution)*resolution)
            top = min(bounds[3], bounds[1] + math.ceil((top-bounds[1])/resolution)*resolution)
            if left >= right or bottom >= top:
                raise ValueError(f'Selected asset does not intersect requested bounds: {path}')
            stat = path.stat()
            sources.append({'item_id': record['item_id'], 'path': path.as_posix(),
                            'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                            'target_bounds': [left, bottom, right, top]})
    return records, sources, bands


def parse_mgrs_from_path(cog_path: str) -> Tuple[str, str]:
    """
    Parse MGRS tile ID and flight direction from COG file path

    Args:
        cog_path: Path to COG file (e.g., "output/17MPU_ASCENDING/cog/VV_2024-01.tif")

    Returns:
        Tuple of (mgrs_tile_id, direction)
        Example: ("17MPU", "ASCENDING")

    Raises:
        ValueError: If path format is invalid
    """
    path = Path(cog_path)

    # Extract directory name like "17MPU_ASCENDING"
    tile_dir = path.parent.parent.name

    # Parse using regex
    match = re.match(r'^([0-9]{2}[A-Z]{3})_(ASCENDING|DESCENDING)$', tile_dir)
    if not match:
        raise ValueError(f"Invalid tile directory format: {tile_dir}")

    mgrs_id = match.group(1)
    direction = match.group(2)

    return mgrs_id, direction


def parse_month_from_filename(cog_path: str) -> str:
    """
    Parse month from COG filename

    Args:
        cog_path: Path to COG file
            Examples:
            - "output/17MPU_ASCENDING/cog/17MPU_S1_Monthly_ASCENDING_2024-01.tif"
            - "output/17MPU_ASCENDING/cog/VV_2024-01.tif" (legacy format)

    Returns:
        Month string in YYYY-MM format

    Raises:
        ValueError: If filename format is invalid
    """
    filename = Path(cog_path).stem  # e.g., "17MPU_S1_Monthly_ASCENDING_2024-01"

    # Extract YYYY-MM pattern
    match = re.search(r'(\d{4}-\d{2})', filename)
    if not match:
        raise ValueError(f"Invalid filename format: {filename}")

    return match.group(1)


def get_mgrs_bounds(mgrs_id: str) -> Tuple[int, int]:
    """
    Get numeric bounds from MGRS tile ID for sorting

    Args:
        mgrs_id: MGRS tile ID (e.g., "17MPU")

    Returns:
        Tuple of (zone_number, letter_code)
        Used for determining top-left and bottom-right tiles

    Example:
        "17MPU" -> (17, ord('M')*1000 + ord('P')*100 + ord('U'))
    """
    zone = int(mgrs_id[:2])
    letters = mgrs_id[2:]

    # Create a sortable numeric value from letters
    letter_code = sum(ord(c) * (100 ** (2-i)) for i, c in enumerate(letters))

    return zone, letter_code


def find_topleft_bottomright(mgrs_tiles: List[str]) -> Tuple[str, str]:
    """
    Find the top-left and bottom-right MGRS tiles from a list

    Args:
        mgrs_tiles: List of MGRS tile IDs

    Returns:
        Tuple of (topleft_tile, bottomright_tile)

    Example:
        ["17MPU", "17MPV", "17MQU", "17MQV"] -> ("17MPU", "17MQV")
    """
    if not mgrs_tiles:
        raise ValueError("No tiles provided")

    # Sort by zone and letter code
    sorted_tiles = sorted(mgrs_tiles, key=lambda t: get_mgrs_bounds(t))

    return sorted_tiles[0], sorted_tiles[-1]


def validate_mosaic_inputs(cog_files: List[str], allow_mixed_directions: bool = False) -> Dict:
    """
    Validate that all COG files can be mosaicked together

    Checks:
    - All files have the same flight direction (unless allow_mixed_directions=True)
    - All files have the same month
    - At least one file is provided

    Args:
        cog_files: List of COG file paths
        allow_mixed_directions: If True, allows multiple directions (for "ALL" strategy)

    Returns:
        Dict with validation results:
        {
            'valid': bool,
            'direction': str or None,
            'month': str or None,
            'mgrs_tiles': List[str],
            'errors': List[str]
        }
    """
    result = {
        'valid': True,
        'direction': None,
        'month': None,
        'mgrs_tiles': [],
        'errors': []
    }

    if not cog_files:
        result['valid'] = False
        result['errors'].append("No COG files provided")
        return result

    # Collect directions, months, and MGRS tiles
    directions = set()
    months = set()
    mgrs_tiles = set()

    for cog_path in cog_files:
        try:
            mgrs_id, direction = parse_mgrs_from_path(cog_path)
            month = parse_month_from_filename(cog_path)

            directions.add(direction)
            months.add(month)
            mgrs_tiles.add(mgrs_id)

        except ValueError as e:
            result['errors'].append(str(e))
            result['valid'] = False

    # Validate single direction (unless allow_mixed_directions=True)
    if len(directions) > 1:
        if not allow_mixed_directions:
            result['valid'] = False
            result['errors'].append(f"Multiple flight directions found: {directions}. Only same direction can be mosaicked.")
        else:
            # For ALL strategy, use "ALL" as the direction identifier
            result['direction'] = "ALL"
    elif len(directions) == 1:
        result['direction'] = list(directions)[0]

    # Validate single month
    if len(months) > 1:
        result['valid'] = False
        result['errors'].append(f"Multiple months found: {months}. Only same month can be mosaicked.")
    elif len(months) == 1:
        result['month'] = list(months)[0]

    result['mgrs_tiles'] = sorted(list(mgrs_tiles))

    return result


def generate_mosaic_filename(mgrs_tiles: List[str], month: str, direction: str, crs_suffix: Optional[str] = None) -> str:
    """
    Generate mosaic filename following the naming convention

    Format: TopLeft-BottomRight-YYYY-MM-DIRECTION[-CRS].tif

    Args:
        mgrs_tiles: List of MGRS tile IDs
        month: Month string (YYYY-MM)
        direction: Flight direction (ASCENDING or DESCENDING)
        crs_suffix: Optional CRS suffix (e.g., "4326" for EPSG:4326)

    Returns:
        Filename string

    Example:
        ["17MPU", "17MQV"], "2024-01", "ASCENDING" -> "17MPU-17MQV-2024-01-ASCENDING.tif"
        ["17MPU", "17MQV"], "2024-01", "ASCENDING", "4326" -> "17MPU-17MQV-2024-01-ASCENDING-4326.tif"
    """
    topleft, bottomright = find_topleft_bottomright(mgrs_tiles)
    base_name = f"{topleft}-{bottomright}-{month}-{direction}"

    if crs_suffix:
        base_name += f"-{crs_suffix}"

    return f"{base_name}.tif"


def create_mosaic_vrt(
    cog_files: List[str],
    output_dir: str = "analysis_results/mosaic",
    output_format: str = "VRT",
    validate: bool = True,
    target_crs: Optional[str] = "EPSG:4326",
    allow_mixed_directions: bool = False,
    create_display: bool = False,
    display_params: Optional[dict] = None
) -> Optional[str]:
    """
    Create a virtual mosaic (VRT) or COG from multiple COG files

    Args:
        cog_files: List of COG file paths to mosaic
        output_dir: Output directory (default: "analysis_results/mosaic")
        output_format: Output format - "VRT" or "COG" (default: "VRT")
        validate: Whether to validate inputs (default: True)
        target_crs: Target coordinate reference system (default: "EPSG:4326")
            - "EPSG:4326": WGS84 geographic coordinates (default)
            - "EPSG:3857": Web Mercator
            - None: Keep original CRS (no reprojection)
            - Any valid EPSG code or PROJ string
        allow_mixed_directions: If True, allows mosaicking files with different directions
                                (used for "ALL" strategy)
        create_display: If True, also create display-optimized VRT/COG (default: False)
        display_params: Optional dict with display enhancement parameters:
            - method: 'vrt' or 'cog' (default: same as output_format)
            - wallis_window: int (default: 51)
            - destripe_window: int (default: 101)
            - destripe_direction: 'column' or 'row' (default: 'column')
            - global_percentiles: tuple (default: (2, 98))

    Returns:
        Path to output file if successful, None otherwise
        If create_display=True, returns path to display VRT/COG instead

    Raises:
        ValueError: If validation fails
        RuntimeError: If GDAL command fails

    Example:
        >>> cog_files = [
        ...     "output/17MPU_ASCENDING/cog/VV_2024-01.tif",
        ...     "output/17MPV_ASCENDING/cog/VV_2024-01.tif",
        ... ]
        >>> # Create mosaic in EPSG:4326 (default)
        >>> mosaic_path = create_mosaic_vrt(cog_files)
        >>> print(mosaic_path)
        analysis_results/mosaic/17MPU-17MPV-2024-01-ASCENDING.vrt

        >>> # Keep original UTM projection
        >>> mosaic_path = create_mosaic_vrt(cog_files, target_crs=None)

        >>> # Use ALL direction strategy (ASCENDING priority)
        >>> cog_files_all = [
        ...     "output/17MPU_DESCENDING/cog/VV_2024-01.tif",
        ...     "output/17MPU_ASCENDING/cog/VV_2024-01.tif",
        ... ]
        >>> mosaic_path = create_mosaic_vrt(cog_files_all, allow_mixed_directions=True)

        >>> # Create with display-optimized version
        >>> display_path = create_mosaic_vrt(
        ...     cog_files,
        ...     create_display=True,
        ...     display_params={'wallis_window': 51, 'destripe_window': 101}
        ... )
    """
    # Validate inputs
    if validate:
        validation = validate_mosaic_inputs(cog_files, allow_mixed_directions=allow_mixed_directions)

        if not validation['valid']:
            error_msg = "Mosaic validation failed:\n" + "\n".join(f"  - {e}" for e in validation['errors'])
            raise ValueError(error_msg)

        direction = validation['direction']
        month = validation['month']
        mgrs_tiles = validation['mgrs_tiles']
    else:
        # If not validating, try to extract from first file
        mgrs_id, direction = parse_mgrs_from_path(cog_files[0])
        month = parse_month_from_filename(cog_files[0])
        mgrs_tiles = [mgrs_id]

    # Create output directory
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Extract CRS suffix for filename (e.g., "EPSG:4326" -> "4326")
    crs_suffix = None
    if target_crs:
        # Extract numeric part from EPSG code
        if "EPSG:" in target_crs.upper():
            crs_suffix = target_crs.split(":")[-1]
        elif "EPSG" in target_crs.upper():
            # Handle "EPSG3857" format
            crs_suffix = target_crs.replace("EPSG", "").replace("epsg", "")
        else:
            # For non-EPSG CRS, use a hash or simplified name
            crs_suffix = target_crs.replace(":", "").replace("+", "")[:10]

    # Generate filename with CRS suffix
    base_filename = generate_mosaic_filename(mgrs_tiles, month, direction, crs_suffix)

    if output_format.upper() == "VRT":
        output_file = output_path / base_filename.replace(".tif", ".vrt")
    else:
        output_file = output_path / base_filename

    logger.info("Creating mosaic: %s", output_file.name)
    logger.info("MGRS tiles: %s", ', '.join(mgrs_tiles))
    logger.info("Month: %s", month)
    logger.info("Direction: %s", direction)
    logger.info("File count: %d", len(cog_files))
    if target_crs:
        logger.info("Target CRS: %s", target_crs)
    else:
        logger.info("Target CRS: Original (no reprojection)")

    # Create temporary file list
    # Convert to absolute paths and Unix-style paths for GDAL compatibility
    # Using absolute paths ensures VRT works from any working directory
    normalized_paths = [str(Path(f).resolve().as_posix()) for f in cog_files]

    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as tmp:
        tmp.write('\n'.join(normalized_paths))
        filelist_path = tmp.name

    try:
        if output_format.upper() == "VRT":
            if target_crs:
                # For cross-UTM zones: Two-step approach
                # Step 1: Create reprojected VRT for each input file
                # Step 2: Merge all reprojected VRTs
                logger.info("Creating VRT with reprojection to %s...", target_crs)
                logger.info("Step 1: Reprojecting each file to %s...", target_crs)

                temp_vrts = []
                temp_dir = output_path / f".tmp_{output_file.stem}"
                temp_dir.mkdir(exist_ok=True)

                for i, cog_file in enumerate(cog_files, 1):
                    cog_path = Path(cog_file)
                    temp_vrt = temp_dir / f"{cog_path.stem}_reprojected.vrt"

                    # Reproject individual file to target CRS
                    cmd_warp = [
                        "gdalwarp",
                        "-t_srs", target_crs,
                        "-of", "VRT",
                        "-srcnodata", "-9999",  # Specify source NoData value
                        "-dstnodata", "-9999",  # Preserve NoData in output
                        "-overwrite",
                        str(cog_path),
                        str(temp_vrt)
                    ]

                    result = subprocess.run(cmd_warp, capture_output=True, text=True)

                    # Check if gdalwarp succeeded and output file is valid
                    if result.returncode != 0:
                        logger.warning("gdalwarp failed for %s: %s", cog_path.name, result.stderr)
                        continue

                    # Verify the output VRT has bands
                    try:
                        from osgeo import gdal
                        ds = gdal.Open(str(temp_vrt))
                        if ds is None:
                            logger.warning("Cannot open reprojected VRT: %s", temp_vrt.name)
                            continue

                        band_count = ds.RasterCount
                        ds = None

                        if band_count == 0:
                            logger.warning("Reprojected VRT has no bands: %s", temp_vrt.name)
                            continue

                        temp_vrts.append(str(temp_vrt.resolve().as_posix()))

                    except ImportError:
                        # If GDAL Python bindings not available, skip validation
                        temp_vrts.append(str(temp_vrt.resolve().as_posix()))

                    if i % 10 == 0:
                        logger.info("Reprojected %d/%d files (%d valid)...", i, len(cog_files), len(temp_vrts))

                logger.info("Step 2: Merging %d reprojected VRTs...", len(temp_vrts))

                # Check if we have any valid reprojected VRTs
                if not temp_vrts:
                    raise RuntimeError(
                        "No valid reprojected VRT files were created. "
                        "All gdalwarp operations failed. Check:\n"
                        "  1. Source COG files have proper georeferencing\n"
                        "  2. Source COG files have NoData=-9999 set\n"
                        "  3. GDAL version is compatible\n"
                        "Run with verbose mode to see detailed errors."
                    )

                # Create file list for reprojected VRTs
                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as tmp:
                    tmp.write('\n'.join(temp_vrts))
                    reprojected_list = tmp.name

                # Merge all reprojected VRTs
                cmd_build = [
                    "gdalbuildvrt",
                    "-srcnodata", "-9999",  # Treat NoData as transparent for intelligent gap filling
                    "-input_file_list", reprojected_list,
                    str(output_file)
                ]
                subprocess.run(cmd_build, check=True, capture_output=True, text=True)

                # Clean up file list only (keep temp VRTs - they are referenced by final VRT)
                Path(reprojected_list).unlink()

                logger.info("VRT created successfully: %s", output_file)
                logger.info("Note: Temporary reprojected VRTs preserved in %s/", temp_dir.name)
            else:
                # No target CRS specified, use source CRS
                logger.info("Creating VRT in source CRS...")
                cmd = [
                    "gdalbuildvrt",
                    "-srcnodata", "-9999",  # Treat NoData as transparent for intelligent gap filling
                    "-input_file_list", filelist_path,
                    str(output_file)
                ]
                subprocess.run(cmd, check=True, capture_output=True, text=True)
                logger.info("VRT created successfully: %s", output_file)

            # Fix VRT paths to use absolute paths so the VRT can be opened
            # from any working directory (e.g., Jupyter notebooks)
            fix_vrt_paths(str(output_file), make_absolute=True)
            logger.info("VRT paths converted to absolute paths")

        else:
            # Build VRT first, then convert to COG
            temp_vrt = output_file.with_suffix('.temp.vrt')

            if target_crs:
                # Two-step approach for cross-UTM zones
                logger.info("Creating mosaic with reprojection to %s...", target_crs)
                logger.info("Step 1: Reprojecting each file to %s...", target_crs)

                temp_vrts = []
                temp_dir = output_path / f".tmp_{output_file.stem}"
                temp_dir.mkdir(exist_ok=True)

                for i, cog_file in enumerate(cog_files, 1):
                    cog_path = Path(cog_file)
                    temp_vrt_file = temp_dir / f"{cog_path.stem}_reprojected.vrt"

                    # Reproject individual file to target CRS
                    cmd_warp = [
                        "gdalwarp",
                        "-t_srs", target_crs,
                        "-of", "VRT",
                        "-srcnodata", "-9999",  # Specify source NoData value
                        "-dstnodata", "-9999",  # Preserve NoData in output
                        "-overwrite",
                        str(cog_path),
                        str(temp_vrt_file)
                    ]

                    result = subprocess.run(cmd_warp, capture_output=True, text=True)

                    # Check if gdalwarp succeeded and output file is valid
                    if result.returncode != 0:
                        logger.warning("gdalwarp failed for %s: %s", cog_path.name, result.stderr)
                        continue

                    # Verify the output VRT has bands
                    try:
                        from osgeo import gdal
                        ds = gdal.Open(str(temp_vrt_file))
                        if ds is None:
                            logger.warning("Cannot open reprojected VRT: %s", temp_vrt_file.name)
                            continue

                        band_count = ds.RasterCount
                        ds = None

                        if band_count == 0:
                            logger.warning("Reprojected VRT has no bands: %s", temp_vrt_file.name)
                            continue

                        temp_vrts.append(str(temp_vrt_file.resolve().as_posix()))

                    except ImportError:
                        # If GDAL Python bindings not available, skip validation
                        temp_vrts.append(str(temp_vrt_file.resolve().as_posix()))

                    if i % 10 == 0:
                        logger.info("Reprojected %d/%d files (%d valid)...", i, len(cog_files), len(temp_vrts))

                logger.info("Step 2: Merging %d reprojected VRTs...", len(temp_vrts))

                # Check if we have any valid reprojected VRTs
                if not temp_vrts:
                    raise RuntimeError(
                        "No valid reprojected VRT files were created. "
                        "All gdalwarp operations failed. Check:\n"
                        "  1. Source COG files have proper georeferencing\n"
                        "  2. Source COG files have NoData=-9999 set\n"
                        "  3. GDAL version is compatible\n"
                        "Run with verbose mode to see detailed errors."
                    )

                # Create file list for reprojected VRTs
                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as tmp:
                    tmp.write('\n'.join(temp_vrts))
                    reprojected_list = tmp.name

                # Merge all reprojected VRTs
                cmd_build = [
                    "gdalbuildvrt",
                    "-srcnodata", "-9999",  # Treat NoData as transparent for intelligent gap filling
                    "-input_file_list", reprojected_list,
                    str(temp_vrt)
                ]
                subprocess.run(cmd_build, check=True, capture_output=True, text=True)

                # Clean up reprojected list
                Path(reprojected_list).unlink()
            else:
                # No target CRS specified, use gdalbuildvrt
                logger.info("Creating VRT in source CRS...")
                cmd_vrt = [
                    "gdalbuildvrt",
                    "-srcnodata", "-9999",  # Treat NoData as transparent for intelligent gap filling
                    "-input_file_list", filelist_path,
                    str(temp_vrt)
                ]
                subprocess.run(cmd_vrt, check=True, capture_output=True, text=True)
                logger.info("VRT created: %s", temp_vrt.name)

            # Convert VRT to COG
            logger.info("Converting to COG...")
            cmd_cog = [
                "gdal_translate",
                "-of", "COG",
                "-co", "COMPRESS=LZW",
                "-co", "PREDICTOR=3",
                "-co", "BLOCKSIZE=512",
                str(temp_vrt),
                str(output_file)
            ]
            subprocess.run(cmd_cog, check=True, capture_output=True, text=True)
            logger.info("COG created successfully: %s", output_file)

            # Clean up temporary mosaic VRT only (keep reprojected VRTs - they were used to create COG)
            temp_vrt.unlink()
            logger.info("Temporary mosaic VRT cleaned up")
            if target_crs and temp_dir.exists():
                logger.info("Note: Reprojected VRTs preserved in %s/", temp_dir.name)

        # Clean up file list
        Path(filelist_path).unlink()

        # Create display-optimized version if requested
        if create_display:
            from .display_mosaic import create_display_vrt

            # Prepare display parameters
            if display_params is None:
                display_params = {}

            # Set default method to match output_format
            if 'method' not in display_params:
                display_params['method'] = output_format.lower()

            # Generate display output path
            display_suffix = '_display'
            if display_params['method'] == 'vrt':
                display_output = str(output_file).replace('.vrt', f'{display_suffix}.vrt')
            else:
                display_output = str(output_file).replace('.vrt', f'{display_suffix}.tif').replace('.tif', f'{display_suffix}.tif')

            logger.info("Creating display-optimized mosaic...")
            display_path = create_display_vrt(
                data_mosaic_vrt=str(output_file),
                output_path=display_output,
                **display_params
            )

            return display_path

        return str(output_file)

    except subprocess.CalledProcessError as e:
        error_msg = f"GDAL command failed: {' '.join(e.cmd)}"
        if e.stderr:
            error_msg += f"\nStderr: {e.stderr}"
        raise RuntimeError(error_msg)

    except FileNotFoundError:
        raise RuntimeError(
            "GDAL tools not found (gdalbuildvrt, gdal_translate). "
            "Please ensure GDAL is installed and in PATH. "
            "Install with: conda install gdal"
        )
    finally:
        # Ensure temp file is cleaned up
        if Path(filelist_path).exists():
            Path(filelist_path).unlink()


def find_cog_files_for_mosaic(
    month: str,
    direction: str,
    output_root: str = "output",
    band: str = "VV",
    mgrs_prefix: Optional[str] = None
) -> List[str]:
    """
    Find all COG files matching the specified month and direction

    Args:
        month: Month in YYYY-MM format (e.g., "2024-01")
        direction: Flight direction ("ASCENDING", "DESCENDING", or "ALL")
                   "ALL": Returns interleaved ASCENDING and DESCENDING files.
                          For each MGRS tile, ASCENDING is placed first, then DESCENDING.
                          - ASCENDING data takes priority at pixel level
                          - DESCENDING fills NoData gaps within the same tile
                          - Result: Complete tile coverage with ASCENDING preference
        output_root: Root output directory (default: "output")
        band: Band name ("VV" or "VH", default: "VV")
              Note: This parameter is kept for API compatibility but may not
              be used if COG files contain both bands
        mgrs_prefix: Optional MGRS tile prefix filter (e.g., "49Q", "17M")
                     Only tiles starting with this prefix will be included

    Returns:
        List of COG file paths

    Example:
        >>> # Find all tiles for 2024-01 ASCENDING
        >>> files = find_cog_files_for_mosaic("2024-01", "ASCENDING")
        >>> print(files)
        ['output/17MPU_ASCENDING/cog/17MPU_S1_Monthly_ASCENDING_2024-01.tif',
         'output/17MPV_ASCENDING/cog/17MPV_S1_Monthly_ASCENDING_2024-01.tif']

        >>> # Find only 49Q tiles for 2024-01 ASCENDING
        >>> files = find_cog_files_for_mosaic("2024-01", "ASCENDING", mgrs_prefix="49Q")
        >>> print(files)
        ['output/49QEF_ASCENDING/cog/49QEF_S1_Monthly_ASCENDING_2024-01.tif',
         'output/49QEG_ASCENDING/cog/49QEG_S1_Monthly_ASCENDING_2024-01.tif',
         'output/49QFF_ASCENDING/cog/49QFF_S1_Monthly_ASCENDING_2024-01.tif',
         'output/49QFG_ASCENDING/cog/49QFG_S1_Monthly_ASCENDING_2024-01.tif']

        >>> # Find all directions for 2024-01 (ASCENDING priority, interleaved by tile)
        >>> files = find_cog_files_for_mosaic("2024-01", "ALL")
        >>> # Returns: [17MPU_ASC, 17MPU_DESC, 17MPV_ASC, 17MPV_DESC, ...]
    """
    import glob

    if direction == "ALL":
        # Find both directions and interleave by MGRS tile
        # Interleaving ensures that for each tile:
        #   1. ASCENDING data is tried first (priority)
        #   2. DESCENDING immediately follows to fill gaps within the same tile
        # Result: Pixel-level gap-filling within each tile
        from collections import defaultdict

        descending_files = find_cog_files_for_mosaic(
            month, "DESCENDING", output_root, band, mgrs_prefix
        )
        ascending_files = find_cog_files_for_mosaic(
            month, "ASCENDING", output_root, band, mgrs_prefix
        )

        # Group files by MGRS tile ID
        asc_by_tile = defaultdict(list)
        desc_by_tile = defaultdict(list)

        for f in ascending_files:
            tile_id = Path(f).parent.parent.name.split('_')[0]  # e.g., "17MPU"
            asc_by_tile[tile_id].append(f)

        for f in descending_files:
            tile_id = Path(f).parent.parent.name.split('_')[0]
            desc_by_tile[tile_id].append(f)

        # Get all unique tiles
        all_tiles = sorted(set(asc_by_tile.keys()) | set(desc_by_tile.keys()))

        # Interleave: for each tile, add ASCENDING first, then DESCENDING
        files = []
        for tile_id in all_tiles:
            files.extend(asc_by_tile.get(tile_id, []))
            files.extend(desc_by_tile.get(tile_id, []))

        logger.info("Direction: ALL (ASCENDING priority, interleaved by tile)")
        logger.info("DESCENDING files: %d", len(descending_files))
        logger.info("ASCENDING files: %d", len(ascending_files))
        logger.info("Total files: %d", len(files))

        return files

    # Pattern matches: output/*_DIRECTION/cog/*_DIRECTION_YYYY-MM.tif
    if mgrs_prefix:
        # Filter by MGRS prefix (e.g., "49Q" matches 49QEF, 49QEG, etc.)
        pattern = f"{output_root}/{mgrs_prefix}*_{direction}/cog/{mgrs_prefix}*_{direction}_{month}.tif"
    else:
        pattern = f"{output_root}/*_{direction}/cog/*_{direction}_{month}.tif"

    files = sorted(glob.glob(pattern))

    logger.info("Searching for COG files:")
    logger.info("Pattern: %s", pattern)
    if mgrs_prefix:
        logger.info("MGRS prefix filter: %s", mgrs_prefix)
    logger.info("Found: %d files", len(files))

    return files


def fix_vrt_paths(vrt_file: str, make_absolute: bool = True) -> None:
    """
    Fix paths in an existing VRT file to use absolute or relative paths

    Args:
        vrt_file: Path to the VRT file to fix
        make_absolute: If True, convert to absolute paths; if False, convert to relative paths

    Example:
        >>> fix_vrt_paths("analysis_results/mosaic/17MPU-17MPU-2025-07-ASCENDING.vrt")
        INFO   - Fixed VRT file: analysis_results/mosaic/17MPU-17MPU-2025-07-ASCENDING.vrt
        INFO   - Converted 4 source files to absolute paths
    """
    import defusedxml.ElementTree as ET

    vrt_path = Path(vrt_file)
    if not vrt_path.exists():
        raise FileNotFoundError(f"VRT file not found: {vrt_file}")

    # Parse the VRT XML
    tree = ET.parse(vrt_path)
    root = tree.getroot()

    # Get the directory containing the VRT file
    vrt_dir = vrt_path.parent.resolve()

    # Find all SourceFilename elements
    source_files = root.findall(".//SourceFilename")
    count = 0

    for elem in source_files:
        original_path = elem.text

        if make_absolute:
            # Convert to absolute path
            # First try as relative to VRT directory
            abs_path = (vrt_dir / original_path).resolve()

            # If that doesn't exist, try relative to current working directory
            if not abs_path.exists():
                abs_path = Path(original_path).resolve()

            # Update the element
            elem.text = str(abs_path.as_posix())
            elem.set('relativeToVRT', '0')
            count += 1
        else:
            # Convert to relative path (relative to VRT directory)
            abs_path = Path(original_path).resolve()
            rel_path = abs_path.relative_to(vrt_dir)

            elem.text = str(rel_path.as_posix())
            elem.set('relativeToVRT', '1')
            count += 1

    # Write back to file
    tree.write(vrt_path, encoding='utf-8', xml_declaration=True)

    path_type = "absolute" if make_absolute else "relative"
    logger.info("Fixed VRT file: %s", vrt_file)
    logger.info("Converted %d source file paths to %s paths", count, path_type)

