"""
workflow_static.py
==================
Per-acquisition-group static layer mosaic workflow for S1-GRiTS.

Groups bursts by (orbit_pass, acq_group_id_within_mgrs_tile) — the same
acquisition group strategy used by workflow_scenes — and produces one set
of static layer COGs per group. Since RTC-STATIC layers are time-invariant,
there is no need to process per-scene; each track-based burst group gets
one composite mosaic.

Downloads incidence angle maps, layover/shadow mask, number of looks, and
RTC area normalisation factors from OPERA RTC-STATIC products and mosaics
them to MGRS tile-level COG files.

Processing is skipped if outputs already exist (unless overwrite: true in config).

Output structure:
    {base_dir}_{DIR}_static/
      {TILE}_{DIR}/
        static/
          zarr/S1_static_TK{track_token}.zarr/    (per-track Zarr, one per acq group)
          cog/{TILE}_{DIR}_TK{track}_N{bursts:02d}_local_inc_angle.tif
          cog/{TILE}_{DIR}_TK{track}_N{bursts:02d}_inc_angle.tif
          cog/{TILE}_{DIR}_TK{track}_N{bursts:02d}_ls_map.tif
          ...

CLI entry point: s1grits process_static --config config.yaml
"""

import json as _json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import rasterio
import zarr
from rasterio.transform import Affine
from rich.console import Console

from s1grits.logger_config import get_logger
from s1grits.workflow import (
    load_config,
    enumerate_mgrs_tiles,
)
from s1grits.asf_io import read_asf_rtc_image_data, NODATA_SENTINEL, _mgrs_to_utm_epsg
from s1grits.asf_output_writing import _mosaic_align, _build_grid_from_mgrs_tile
from s1grits.stac_builder import ITEM_STAC_EXTENSION_URIS
from s1grits.mgrs_burst_data import get_lut_by_mgrs_tile_ids
from s1grits.canonical_catalog_schema import (
    normalize_catalog_record,
    validate_collection_mapping,
)
from s1grits.product_instance import (
    resolve_variant_values, make_processing_signature,
    make_product_variant,
)
from s1grits.product_registry import load_product_registry
from s1grits.file_lock import acquire_lock, release_lock

logger = get_logger(__name__)
console = Console(legacy_windows=True, no_color=False)

# All supported static layer names, in output order.
# Note: 'dem' is NOT available in RTC-STATIC products.
ALL_STATIC_LAYERS: list[str] = [
    'local_inc_angle',
    'inc_angle',
    'ls_map',
    'number_of_looks',
    'rtc_anf_beta0',
    'rtc_anf_sigma0',
]

# Maps layer name to the URL column in the RTC-STATIC metadata DataFrame.
_LAYER_URL_COL: dict[str, str] = {
    'local_inc_angle':  'url_local_inc_angle',
    'inc_angle':        'url_inc_angle',
    'ls_map':           'url_ls_map',
    'number_of_looks':  'url_number_of_looks',
    'rtc_anf_beta0':    'url_rtc_anf_beta0',
    'rtc_anf_sigma0':   'url_rtc_anf_sigma0',
}

# LUT columns to merge into RTC-STATIC metadata for acquisition group info.
_LUT_MERGE_COLS: list[str] = [
    'jpl_burst_id', 'mgrs_tile_id', 'track_number',
    'acq_group_id_within_mgrs_tile', 'orbit_pass',
]


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_enabled_layers(config: dict) -> list[str]:
    """
    Read static_layers config section and return list of enabled layer names.

    Args:
        config: Full configuration dictionary.

    Returns:
        List of enabled layer names, e.g. ['local_inc_angle', 'ls_map'].
        Empty list if static_layers.enabled is false or section is absent.
    """
    static_cfg = config.get('static_layers', {})
    if not static_cfg.get('enabled', False):
        return []
    layer_cfg = static_cfg.get('layers', {})
    return [name for name in ALL_STATIC_LAYERS if layer_cfg.get(name, False)]


# ---------------------------------------------------------------------------
# COG output
# ---------------------------------------------------------------------------

def _build_static_cog(
    mosaicked: np.ndarray,
    master_transform: Affine,
    target_crs: str,
    out_path: str,
    cog_block: int = 256,
) -> None:
    """
    Write a single-band float32 COG from a mosaicked array.

    Args:
        mosaicked: 2-D float32 array (height x width).
        master_transform: Affine transform of the output grid.
        target_crs: CRS string (e.g. 'EPSG:32618').
        out_path: Destination file path.
        cog_block: COG internal tile block size in pixels.
    """
    height, width = mosaicked.shape
    arr_out = np.where(np.isnan(mosaicked), NODATA_SENTINEL, mosaicked).astype('float32')

    profile = {
        'driver': 'GTiff',
        'dtype': 'float32',
        'width': width,
        'height': height,
        'count': 1,
        'crs': target_crs,
        'transform': master_transform,
        'nodata': NODATA_SENTINEL,
        'tiled': True,
        'blockxsize': cog_block,
        'blockysize': cog_block,
        'compress': 'deflate',
        'interleave': 'band',
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    from s1grits.atomic_write import atomic_path
    from s1grits.runtime_limits import rasterio_env_kwargs

    with atomic_path(out_path) as _tmp:
        with rasterio.Env(**rasterio_env_kwargs()):
            with rasterio.open(_tmp, 'w', **profile) as dst:
                dst.write(arr_out[np.newaxis, :, :])


# ---------------------------------------------------------------------------
# Zarr output — static (time-invariant) 2-D store
# ---------------------------------------------------------------------------

def _init_zarr_static(
    zarr_path: Path,
    x_coords: np.ndarray,
    y_coords: np.ndarray,
    target_crs: str,
    transform: Affine,
    chunk_y: int,
    chunk_x: int,
    band_names: list[str],
) -> zarr.Group:
    """
    Create or open a time-invariant 2-D Zarr store for static layers.

    Dimensions: y(H,), x(W,) — no time dimension.
    One dataset per static layer, each with shape (H, W) and float32 dtype.

    Args:
        zarr_path: Path to the Zarr store directory.
        x_coords, y_coords: 1-D coordinate arrays for the master grid.
        target_crs: CRS string.
        transform: Affine transform of the output grid.
        chunk_y, chunk_x: Zarr chunk sizes.
        band_names: List of static layer names to create as datasets.

    Returns:
        Opened zarr.Group (mode 'w' for new, 'r+' for existing).
    """
    zarr_path.parent.mkdir(parents=True, exist_ok=True)

    if zarr_path.exists():
        try:
            g_check = zarr.open_group(str(zarr_path), mode='r', zarr_format=3)
            z_h = g_check['y'].shape[0]
            z_w = g_check['x'].shape[0]

            H, W = len(y_coords), len(x_coords)
            if H != z_h or W != z_w:
                raise ValueError(
                    f"Grid mismatch for {zarr_path}: "
                    f"existing=({z_h},{z_w}) new=({H},{W}). "
                    f"Delete the Zarr store or use a matching grid to re-run."
                )
            g = zarr.open_group(str(zarr_path), mode='r+', zarr_format=3)
            logger.info("[Zarr] Opened existing static store, grid locked %dx%d",
                        z_w, z_h)
            return g
        except (KeyError, ValueError) as e:
            raise RuntimeError(
                f"Cannot resume: existing Zarr at {zarr_path} is incompatible. "
                f"Delete it or use a new output directory. Detail: {e}"
            ) from e

    # Fresh store
    g = zarr.open_group(str(zarr_path), mode='w', zarr_format=3)
    if 'time' in g:
        raise RuntimeError(
            f"Static Zarr at {zarr_path} unexpectedly contains a time dimension. "
            f"This indicates a corrupted store. Delete it and re-run."
        )
    g.attrs['crs'] = str(target_crs)
    g.attrs['transform'] = list(transform)[:6]
    g.attrs['processing_level'] = 'static'
    from s1grits.canonical_catalog_schema import make_grid_id, _format_grid_name
    _tile_from_path = zarr_path.parent.parent.parent.name if zarr_path.parent.parent.parent.name else 'UNKNOWN'
    _tfm = list(transform)[:6]
    _w, _h = len(x_coords), len(y_coords)
    _gid = make_grid_id(_tile_from_path, str(target_crs), _tfm, _w, _h)
    g.attrs['grid_id'] = _gid
    g.attrs['grid_name'] = _format_grid_name(_tile_from_path, _tfm, str(target_crs))
    g.attrs['grid_version'] = 1
    g.attrs['width'] = _w
    g.attrs['height'] = _h
    g.attrs['resolution'] = [float(abs(_tfm[0])), float(abs(_tfm[4]))]
    g.attrs['product_type'] = 'static'
    g.attrs['geometry_group_id'] = None  # set by caller if applicable
    g.attrs['time_varying'] = False
    g.attrs['array_dims'] = ['y', 'x']
    _a = g.create_array('x', data=x_coords, overwrite=True, dimension_names=['x'])
    _a.attrs['_ARRAY_DIMENSIONS'] = ['x']
    _a = g.create_array('y', data=y_coords, overwrite=True, dimension_names=['y'])
    _a.attrs['_ARRAY_DIMENSIONS'] = ['y']
    for var in band_names:
        _a = g.create_array(var, shape=(_h, _w), chunks=(chunk_y, chunk_x), dtype='float32', fill_value=np.nan, overwrite=True, dimension_names=['y', 'x'])
        _a.attrs['_ARRAY_DIMENSIONS'] = ['y', 'x']

    return g


# ---------------------------------------------------------------------------
# Data acquisition — LUT + RTC-STATIC metadata
# ---------------------------------------------------------------------------

def _get_lut_for_tile(
    mgrs_tile_id: str,
    config: dict,
) -> pd.DataFrame:
    """
    Query the MGRS-burst LUT for acquisition group info of one tile.

    Args:
        mgrs_tile_id: MGRS tile ID (e.g. '17MQV').
        config: Full configuration dictionary.

    Returns:
        pd.DataFrame with columns: jpl_burst_id, mgrs_tile_id, track_number,
        acq_group_id_within_mgrs_tile, orbit_pass.
        Empty DataFrame if no LUT data found.
    """
    track_numbers = config.get('roi', {}).get('track_numbers') or None

    try:
        df_lut = get_lut_by_mgrs_tile_ids([mgrs_tile_id])
    except ValueError as e:
        logger.warning("[Static] No LUT data for tile %s: %s", mgrs_tile_id, e)
        return pd.DataFrame()

    if track_numbers is not None:
        df_lut = df_lut[df_lut['track_number'].isin(track_numbers)]
        if df_lut.empty:
            logger.warning(
                "[Static] No LUT entries for tile %s with track_numbers=%s",
                mgrs_tile_id, track_numbers,
            )

    return df_lut.reset_index(drop=True)


def _query_static_metadata_for_tile(
    burst_ids: list[str],
    config: dict,
) -> pd.DataFrame:
    """
    Query ASF for RTC-STATIC products given a list of burst IDs.

    Args:
        burst_ids: List of JPL burst ID strings to query.
        config: Full configuration dictionary.

    Returns:
        pd.DataFrame with per-burst layer URL columns.
        Empty DataFrame if no results.
    """
    from s1grits.asf_tiles import query_rtc_static_metadata_by_burst_ids

    if not burst_ids:
        logger.warning("[Static] Empty burst ID list, skipping ASF query")
        return pd.DataFrame()

    logger.info("[Static] Querying RTC-STATIC for %d bursts", len(burst_ids))

    static_cfg = config.get('static_layers', {})
    batch_size = static_cfg.get('query_batch_size', 50)
    max_results = static_cfg.get('query_max_results', 5000)
    max_query_retries = static_cfg.get('query_max_retries', 5)
    query_retry_base_delay = static_cfg.get('query_retry_base_delay', 2.0)

    df = query_rtc_static_metadata_by_burst_ids(
        burst_ids=burst_ids,
        batch_size=batch_size,
        max_results=max_results,
        max_query_retries=max_query_retries,
        query_retry_base_delay=query_retry_base_delay,
    )

    logger.info("[Static] Found %d RTC-STATIC granules", len(df))
    return df


def _merge_static_with_lut(
    df_static: pd.DataFrame,
    df_lut: pd.DataFrame,
) -> pd.DataFrame:
    """
    Merge RTC-STATIC metadata with LUT acquisition group info.

    Inner join on jpl_burst_id. Both sides are normalised to
    UPPERCASE+UNDERSCORES before merging because the LUT may store
    burst IDs with hyphens or mixed case while RTC-STATIC always
    uses the normalised form.

    Args:
        df_static: RTC-STATIC metadata (from _query_static_metadata_for_tile).
        df_lut: LUT data (from _get_lut_for_tile).

    Returns:
        Merged DataFrame with layer URL columns + LUT acq_group columns.
    """
    # Normalise burst IDs to UPPERCASE+UNDERSCORES on both sides
    _norm = lambda s: str(s).upper().replace('-', '_')
    _df_s = df_static.copy()
    _df_l = df_lut.copy()
    _df_s['_merge_key'] = _df_s['jpl_burst_id'].apply(_norm)
    _df_l['_merge_key'] = _df_l['jpl_burst_id'].apply(_norm)

    n_before = len(_df_s)
    df = pd.merge(
        _df_s,
        _df_l[_LUT_MERGE_COLS + ['_merge_key']],
        on='_merge_key',
        how='inner',
    )
    n_after = len(df)
    if n_after < n_before:
        logger.warning(
            "[Static] Merge dropped %d burst(s): %d RTC-STATIC -> %d merged",
            n_before - n_after, n_before, n_after,
        )

    # Keep the RTC-STATIC side's jpl_burst_id (already normalised)
    # and drop the merge key column
    df = df.drop(columns=['_merge_key'])
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Acquisition group logic (mirrors workflow_scenes acq_group mode)
# ---------------------------------------------------------------------------

def _group_static_by_acq(df_merged: pd.DataFrame) -> list[dict]:
    """
    Group static layer bursts by acquisition group.

    Groups by (orbit_pass, acq_group_id_within_mgrs_tile), mirrors the
    acq_group mode logic in workflow_scenes.py (lines 725-733).

    Args:
        df_merged: Merged DataFrame with columns orbit_pass,
                   acq_group_id_within_mgrs_tile, track_number.

    Returns:
        List of group dicts sorted by (orbit_pass, acq_group_id). Each dict:
            key:          (orbit_pass, acq_group_id) tuple
            rows:         filtered DataFrame subset for this group
            track_token:  '_'.join(sorted unique track_numbers), e.g. "40" or "69_172"
            n_bursts:     number of bursts in this group (rows in merged data)
            is_primary:   True if this group has the most bursts
    """
    if df_merged.empty:
        return []

    # Primary group = acq_group_id with the most rows (best coverage)
    _group_burst_counts = df_merged.groupby('acq_group_id_within_mgrs_tile').size()
    _primary_acq_group = int(_group_burst_counts.idxmax()) if len(_group_burst_counts) > 0 else -1

    groups = []
    for (orbit_pass, acq_group_id), grp in df_merged.groupby(
        ['orbit_pass', 'acq_group_id_within_mgrs_tile'], sort=False
    ):
        track_numbers = sorted(grp['track_number'].unique())
        track_token = '_'.join(str(t) for t in track_numbers)
        n_bursts = len(grp)
        is_primary = (acq_group_id == _primary_acq_group)

        groups.append({
            'key': (orbit_pass, acq_group_id),
            'rows': grp.reset_index(drop=True),
            'track_token': track_token,
            'n_bursts': n_bursts,
            'is_primary': is_primary,
        })

    groups.sort(key=lambda g: g['key'])
    return groups


# ---------------------------------------------------------------------------
# Per-group processing
# ---------------------------------------------------------------------------

def _burst_time_range(rows: pd.DataFrame) -> tuple[str | None, str | None]:
    """Return (start_iso, end_iso) RFC3339-UTC strings from a group's ``acq_dt``.

    Returns (None, None) when no usable burst timestamps are present so the
    caller can fall back to a datetime-less item.
    """
    if 'acq_dt' not in rows.columns:
        return None, None
    _dt = pd.to_datetime(rows['acq_dt'], errors='coerce', utc=True).dropna()
    if _dt.empty:
        return None, None
    _fmt = lambda t: t.strftime('%Y-%m-%dT%H:%M:%SZ')
    return _fmt(_dt.min()), _fmt(_dt.max())


def _process_one_static_group(
    rows: pd.DataFrame,
    layers: list[str],
    static_dir: Path,
    mgrs_tile_id: str,
    direction_label: str,
    track_token: str,
    n_bursts: int,
    master_transform: Affine,
    width: int,
    height: int,
    target_crs: str,
    cog_block: int,
    overwrite: bool,
    max_workers: int,
    retry_timeout: float,
    config: dict,
    product_label: str,
    generate_zarr: bool = False,
    chunk_y: int = 512,
    chunk_x: int = 512,
    x_coords: np.ndarray | None = None,
    y_coords: np.ndarray | None = None,
) -> dict[str, Any]:
    """
    Download and mosaic static layers for ONE acquisition group.

    Args:
        rows: Merged DataFrame subset for this group (one row per burst).
        layers: Enabled static layer names to produce.
        static_dir: Output directory for static COGs.
        mgrs_tile_id: MGRS tile ID.
        direction_label: Flight direction label (e.g. 'DESCENDING').
        track_token: Sanitised track identifier (e.g. '40' or '69_172').
        n_bursts: Number of bursts in this group.
        master_transform, width, height, target_crs: Output grid definition.
        cog_block: COG internal tile block size.
        overwrite: Whether to overwrite existing outputs.
        max_workers: Max parallel download workers.
        retry_timeout: Download retry timeout in seconds.
        generate_zarr: Whether to write a per-track Zarr store.
        chunk_y, chunk_x: Zarr chunk sizes in pixels.
        x_coords, y_coords: 1-D coordinate arrays for the master grid.

    Returns:
        dict with keys: status, name_prefix, n_bursts, track_token,
        is_primary, layers_written, zarr_path.
    """
    _console = Console(legacy_windows=True, no_color=False)

    # Per-group output naming prefix (NO datetime suffix — static layers
    # are time-invariant so datetime is irrelevant).
    # Sanitise track_token for filename use (replace '_' with '-') to
    # match workflow_scenes COG naming convention.
    track_token_safe = track_token.replace('_', '-')
    name_prefix = (
        f"s1grits_static_{mgrs_tile_id}_{direction_label}_TK{track_token_safe}_N{n_bursts:02d}"
    )

    # Temporal range of the contributing bursts. Static layers are
    # time-invariant, but the bursts they derive from span an acquisition
    # window; surface it as the item's start/end_datetime (RFC3339, UTC).
    _start_iso, _end_iso = _burst_time_range(rows)

    cog_dir = static_dir / 'cog'

    # Skip-if-exists check
    if not overwrite:
        expected = {
            layer: cog_dir / f"{name_prefix}_{layer}.tif"
            for layer in layers
        }
        all_cogs_exist = all(p.exists() for p in expected.values())

        zarr_exists = True
        if generate_zarr:
            _zarr_check_path = static_dir / 'zarr' / f's1grits_static_{mgrs_tile_id}_{direction_label}_TK{track_token_safe}_N{n_bursts:02d}.zarr'
            zarr_exists = _zarr_check_path.exists()

        if all_cogs_exist and zarr_exists:
            logger.info(
                "[Static] All outputs exist for group %s, skipping",
                name_prefix,
            )
            return {
                'status': 'skipped',
                'name_prefix': name_prefix,
                'n_bursts': n_bursts,
                'track_token': track_token,
                'layers_written': {layer: str(p) for layer, p in expected.items()},
                'zarr_path': str(_zarr_check_path) if generate_zarr else None,
                'start_datetime': _start_iso,
                'end_datetime': _end_iso,
            }

    _console.print(
        f"  [dim]Group TK{track_token_safe}: {n_bursts} burst(s)[/dim]"
    )

    # Download each layer for this group's bursts
    layer_arrs: dict[str, list] = {}
    layer_profs: dict[str, list] = {}

    for layer in layers:
        url_col = _LAYER_URL_COL[layer]
        if url_col not in rows.columns:
            logger.warning(
                "[Static] URL column %s missing for layer %s in group %s",
                url_col, layer, name_prefix,
            )
            layer_arrs[layer] = [None] * n_bursts
            layer_profs[layer] = [None] * n_bursts
            continue

        urls = rows[url_col].fillna('').tolist()
        valid_count = sum(1 for u in urls if u)

        if valid_count == 0:
            logger.warning(
                "[Static] No valid URLs for layer %s in group %s",
                layer, name_prefix,
            )
            layer_arrs[layer] = [None] * n_bursts
            layer_profs[layer] = [None] * n_bursts
            continue

        logger.info(
            "[Static] Downloading layer %s for group %s: %d/%d URLs",
            layer, name_prefix, valid_count, n_bursts,
        )

        arrs, profs, error_types = read_asf_rtc_image_data(
            urls,
            max_workers=max_workers,
            retry_timeout_seconds=retry_timeout,
        )

        n_ok = sum(1 for a in arrs if a is not None)
        n_404 = sum(1 for e in error_types if e == 'not_found')
        n_err = sum(1 for e in error_types if e == 'network_error')
        logger.info(
            "[Static] Layer %-8s / group %s: %d/%d ok, %d not_found, %d network_error",
            layer, name_prefix, n_ok, n_bursts, n_404, n_err,
        )

        if n_ok == 0:
            _console.print(
                f"[yellow]    Layer {layer}: 0/{n_bursts} ok "
                f"({n_404} not_found, {n_err} network_error)[/yellow]"
            )

        layer_arrs[layer] = list(arrs)
        layer_profs[layer] = list(profs)

    # Mosaic and write one COG per layer
    layers_written: dict[str, str | None] = {}

    # --- Zarr: init before layer loop so each band is written immediately ---
    zarr_path: Path | None = None
    g_zarr: zarr.Group | None = None
    if generate_zarr:
        zarr_path = static_dir / 'zarr' / f's1grits_static_{mgrs_tile_id}_{direction_label}_TK{track_token_safe}_N{n_bursts:02d}.zarr'
        g_zarr = _init_zarr_static(
            zarr_path, x_coords, y_coords, target_crs, master_transform,
            chunk_y, chunk_x, band_names=layers,
        )
        # Write variant metadata to Zarr root attrs for resync self-sufficiency
        _registry_z = load_product_registry(workflow_config=config)
        _spec_z = _registry_z.get('static')
        _variant_vals_z = resolve_variant_values(config, _spec_z.variant_fields)
        _proc_sig_z = make_processing_signature(_variant_vals_z)
        _prod_var_z = make_product_variant('static', _variant_vals_z, layers)
        g_zarr.attrs['processing_signature'] = _proc_sig_z
        g_zarr.attrs['product_variant'] = _prod_var_z
        g_zarr.attrs['processing_variant_json'] = _json.dumps(_variant_vals_z)
        g_zarr.attrs['product_label'] = product_label

    for layer in layers:
        out_path = str(cog_dir / f"{name_prefix}_{layer}.tif")

        if os.path.exists(out_path) and not overwrite:
            logger.info("[Static] Skipping %s (already exists)", layer)
            layers_written[layer] = out_path
            continue

        arrs = layer_arrs.get(layer, [])
        profs = layer_profs.get(layer, [])
        indices = [i for i, a in enumerate(arrs) if a is not None]

        if not indices:
            logger.warning(
                "[Static] No valid burst arrays for layer %s in group %s, skipping",
                layer, name_prefix,
            )
            layers_written[layer] = None
            continue

        mosaicked = _mosaic_align(
            indices, arrs, profs, height, width, master_transform, target_crs,
        )
        if mosaicked is None:
            logger.warning(
                "[Static] Mosaic returned None for layer %s in group %s",
                layer, name_prefix,
            )
            layers_written[layer] = None
            continue

        _build_static_cog(mosaicked, master_transform, target_crs, out_path, cog_block)
        logger.info("[Static] Written: %s", out_path)
        layers_written[layer] = out_path

        # Zarr: write band immediately to keep peak memory low
        if mosaicked is not None and g_zarr is not None:
            g_zarr[layer][:] = mosaicked.astype(np.float32, copy=False)

    if g_zarr is not None and zarr_path is not None:
        logger.info("[Static] Zarr written: %s (%d bands)", zarr_path, len(layers))

    written_count = sum(1 for v in layers_written.values() if v is not None)
    status = 'success' if written_count > 0 else 'failed'

    if written_count > 0:
        _console.print(
            f"[green]    {written_count}/{len(layers)} layer(s) written "
            f"-> {name_prefix}[/green]"
        )

    return {
        'status': status,
        'name_prefix': name_prefix,
        'n_bursts': n_bursts,
        'track_token': track_token,
        'layers_written': layers_written,
        'zarr_path': str(zarr_path) if zarr_path else None,
        'start_datetime': _start_iso,
        'end_datetime': _end_iso,
    }


# ---------------------------------------------------------------------------
# STAC Item writer for static layers
# ---------------------------------------------------------------------------

def _write_static_stac_item(
    tile_dir: Path,
    mgrs_tile_id: str,
    direction_label: str,
    track_token_safe: str,
    n_bursts: int,
    zarr_path: Path,
    target_res: float,
    band_names: list[str],
    product_label: str,
    master_transform: list[float],
    width: int,
    height: int,
    target_crs: str,
    cog_path: Path | None = None,
    start_datetime: str | None = None,
    end_datetime: str | None = None,
) -> str:
    """
    Write one STAC Item per static Zarr store (per track group).
    Zarr is the primary asset; COG is optional. No-op (returns None) when STAC
    output is disabled (catalog-only build).
    """
    from s1grits.stac_builder import stac_output_enabled
    if not stac_output_enabled():
        return None
    # Compute spatial extent and resolution from transform
    pixel_size_x = abs(float(master_transform[0]))
    pixel_size_y = abs(float(master_transform[4]))

    x_min = float(master_transform[2])
    y_max = float(master_transform[5])
    x_max = x_min + width * pixel_size_x
    y_min = y_max - height * pixel_size_y  # GeoTIFF origin at top-left

    # Extract CRS string for reference_system
    crs_str = str(target_crs)

    # Build item ID from tile, direction, track, and burst count
    item_id = f"{mgrs_tile_id}_{direction_label}_TK{track_token_safe}_N{n_bursts:02d}_static"

    item = {
        "stac_version": "1.1.0",
        "stac_extensions": list(ITEM_STAC_EXTENSION_URIS),
        "type": "Feature",
        "id": item_id,
        "collection": "s1grits-static",
        "geometry": None,
        "bbox": None,
        "properties": {
            # Static layers are time-invariant, but the contributing bursts span
            # an acquisition window: expose it as a range with 'datetime' set to
            # the start instant (STAC requires a non-null datetime OR a
            # start/end pair; we provide both for maximum reader compatibility).
            "datetime": start_datetime,
            "start_datetime": start_datetime,
            "end_datetime": end_datetime,
            "s1grits:time_type": "static",
            "s1grits:product_type": "static",
            # Static geometry layers are auxiliary variables of the scenes/
            # smonthly cube sharing this geometry group (join on tile_id +
            # geometry_group_id).
            "s1grits:role": "auxiliary",
            "s1grits:tile_id": mgrs_tile_id,
            "s1grits:flight_direction": direction_label,
            "sat:orbit_state": direction_label.lower() if direction_label else None,
            "sat:relative_orbit": int(track_token_safe.replace('-', '_')) if track_token_safe.replace('-', '_').isdigit() else None,
            "s1grits:track": int(track_token_safe.replace('-', '_')) if track_token_safe.replace('-', '_').isdigit() else None,
            "s1grits:n_bursts": n_bursts,
            "s1grits:geometry_group_id": f"{mgrs_tile_id}_{direction_label}_TK{track_token_safe}",
            "s1grits:grid_id": f"{mgrs_tile_id}_native_{int(target_res)}m",
            "s1grits:time_varying": False,
            "s1grits:array_dims": ["y", "x"],
            "s1grits:bands": band_names,
            "cube:dimensions": {
                "x": {
                    "type": "spatial",
                    "axis": "x",
                    "step": pixel_size_x,
                    "extent": [x_min, x_max],
                    "reference_system": crs_str,
                },
                "y": {
                    "type": "spatial",
                    "axis": "y",
                    "step": pixel_size_y,
                    "extent": [min(y_min, y_max), max(y_min, y_max)],
                    "reference_system": crs_str,
                },
                "spectral": {
                    "type": "bands",
                    "values": band_names,
                },
            },
        },
        "assets": {},
        "links": [
            {"rel": "self", "href": f"./{item_id}.json"},
            {"rel": "collection", "href": "../../../../collections/s1grits-static/collection.json"},
            {"rel": "root", "href": "../../../../catalog.json"},
        ],
    }

    # Asset hrefs via os.path.relpath (robust against directory depth changes)
    items_dir = tile_dir / "items" / product_label
    items_dir.mkdir(parents=True, exist_ok=True)
    _items_dir_str = str(items_dir)

    def _rel(p: object) -> str | None:
        """Return a forward-slash relative path from items_dir to p, or None."""
        if not p:
            return None
        return os.path.relpath(str(p), _items_dir_str).replace("\\", "/")

    item["assets"]["zarr"] = {
        "href": _rel(zarr_path),
        "type": "application/vnd.zarr; version=3",
        "roles": ["data", "zarr", "static", "auxiliary"],
        "title": "S1-GRiTS static geometry layers Zarr",
    }
    if cog_path and Path(cog_path).exists():
        item["assets"]["cog"] = {
            "href": _rel(cog_path),
            "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            "roles": ["data", "visual", "cog"],
            "title": "Static layer multi-band COG",
            "eo:bands": [{"name": b} for b in band_names],
        }

    item_path = items_dir / f"{item_id}.json"
    from s1grits.atomic_write import atomic_write_json
    atomic_write_json(item, item_path)

    if start_datetime:
        logger.info(
            "[Static] STAC Item: %s  [time range %s .. %s]",
            item_path, start_datetime, end_datetime,
        )
    else:
        logger.warning(
            "[Static] STAC Item: %s  (datetime=null — no burst startTime found)",
            item_path,
        )
    return item_id


# ---------------------------------------------------------------------------
# Per-tile entry point
# ---------------------------------------------------------------------------

def process_static_for_tile(
    mgrs_tile_id: str,
    config: dict,
    output_root: Path,
    layers: list[str],
    df_static: pd.DataFrame | None = None,
    df_lut: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """
    Download and mosaic static layers by acquisition group for one MGRS tile.

    Groups bursts by (orbit_pass, acq_group_id_within_mgrs_tile) using the
    same strategy as workflow_scenes acq_group mode, then produces one set of
    static layer COGs per group.

    Args:
        mgrs_tile_id: MGRS tile ID (e.g. '17MQV').
        config: Full configuration dictionary.
        output_root: Root output directory (Path).
        layers: List of layer names to produce, e.g. ['local_inc_angle', 'ls_map'].
        df_static: Optional pre-queried RTC-STATIC metadata DataFrame.
        df_lut: Optional pre-queried LUT DataFrame.

    Returns:
        dict with keys:
            status:          'success' | 'skipped' | 'failed'
            error:           str or None
            tile_dir:        str path to tile directory
            groups_written:  list of per-group result dicts
    """
    try:
        # Workflows NEVER write STAC — STAC is produced only by `catalog resync`.
        # Set per-tile so this holds even if static is ever run in worker
        # processes (which re-import stac_builder with the default ON).
        from s1grits.stac_builder import set_stac_output_enabled
        set_stac_output_enabled(False)

        flight_direction = config.get('roi', {}).get('flight_direction', '').upper()
        direction_label = flight_direction if flight_direction else 'UNKNOWN'

        # Tile dir: {output_root}/{TILE}/  (direction in product subdirectory)
        tile_dir = output_root / mgrs_tile_id
        static_dir = tile_dir / f"static_{direction_label}" if direction_label else tile_dir / "static"
        overwrite = config.get('output', {}).get('overwrite', False)

        # ---- Acquire data ----
        # 1. LUT (burst -> acq_group mapping, also gives us burst IDs)
        if df_lut is None:
            df_lut = _get_lut_for_tile(mgrs_tile_id, config)
        if df_lut.empty:
            return {
                'status': 'failed',
                'error': f'No LUT data for tile {mgrs_tile_id}.',
                'tile_dir': str(tile_dir),
                'groups_written': [],
            }

        # 2. RTC-STATIC metadata (burst -> layer URLs)
        #    Use burst IDs from the LUT to avoid a second LUT read.
        if df_static is None:
            _lut_burst_ids = df_lut['jpl_burst_id'].unique().tolist()
            console.print(
                f"  [dim]Querying RTC-STATIC metadata for {len(_lut_burst_ids)} burst(s)…[/dim]"
            )
            df_static = _query_static_metadata_for_tile(_lut_burst_ids, config)
        if df_static.empty:
            return {
                'status': 'failed',
                'error': (
                    'No RTC-STATIC products found for this tile. '
                    'Check asf_search version (>= 6.7.3) and burst ID availability.'
                ),
                'tile_dir': str(tile_dir),
                'groups_written': [],
            }

        # Merge LUT + RTC-STATIC
        df_merged = _merge_static_with_lut(df_static, df_lut)
        if df_merged.empty:
            return {
                'status': 'failed',
                'error': (
                    'Merge of LUT and RTC-STATIC metadata produced no rows. '
                    'Burst IDs may not match between the two sources.'
                ),
                'tile_dir': str(tile_dir),
                'groups_written': [],
            }

        # Filter by flight direction if specified
        if flight_direction and 'flight_direction' in df_merged.columns:
            mask = df_merged['flight_direction'].str.upper() == flight_direction
            if mask.any():
                df_merged = df_merged[mask].reset_index(drop=True)
            else:
                logger.warning(
                    "[Static] No RTC-STATIC bursts for direction %s in tile %s; "
                    "using all %d available bursts",
                    flight_direction, mgrs_tile_id, len(df_merged),
                )

        n_bursts_total = len(df_merged)
        logger.info(
            "[Static] %d burst(s) available for tile %s after merge+filter",
            n_bursts_total, mgrs_tile_id,
        )

        # ---- Group by acquisition group ----
        groups = _group_static_by_acq(df_merged)
        if not groups:
            return {
                'status': 'failed',
                'error': 'No acquisition groups formed.',
                'tile_dir': str(tile_dir),
                'groups_written': [],
            }

        _console = Console(legacy_windows=True, no_color=False)
        _console.print(
            f"  [Static] {n_bursts_total} burst(s) across "
            f"{len(groups)} acq_group(s) for {mgrs_tile_id}"
        )

        # ---- Build master grid (MGRS tile grid, shared across all groups) ----
        processing_config = config.get('processing') or {}
        target_res = processing_config.get('target_resolution', 30.0)
        cog_block = processing_config.get('cog_block_size', 256)
        target_crs_cfg = processing_config.get('target_crs') or None

        # Zarr is the canonical, catalog-indexed static product. `catalog
        # resync` (the authoritative catalog/STAC builder) discovers products by
        # scanning Zarr stores under {tile}/{product}_{DIR}/zarr/, so a static
        # run MUST always write a store to be discoverable and pairable with the
        # scenes/smonthly cubes — a COG-only run would be silently dropped on the
        # next resync. Always on, matching the scenes workflow (COG remains a
        # derived visual asset). `output.formats.zarr` no longer gates static.
        generate_zarr = True
        chunk_y = processing_config.get('zarr_chunks', {}).get('y', 512)
        chunk_x = processing_config.get('zarr_chunks', {}).get('x', 512)

        target_crs = target_crs_cfg if target_crs_cfg else _mgrs_to_utm_epsg(mgrs_tile_id)

        master_transform, width, height, x_coords, y_coords = _build_grid_from_mgrs_tile(
            mgrs_tile_id, target_crs, target_res
        )

        static_dir.mkdir(parents=True, exist_ok=True)

        # Download config
        max_workers = config.get('memory', {}).get('max_download_workers', 4)
        retry_timeout = config.get('memory', {}).get('scene_retry_timeout_seconds', 600.0)

        # Compute product_label before group loop (needed for Zarr metadata)
        product_label = f"static_{direction_label}" if direction_label else "static"

        # ---- Process each group ----
        groups_written = []
        for group in groups:
            group_result = _process_one_static_group(
                rows=group['rows'],
                layers=layers,
                static_dir=static_dir,
                mgrs_tile_id=mgrs_tile_id,
                direction_label=direction_label,
                track_token=group['track_token'],
                n_bursts=group['n_bursts'],
                master_transform=master_transform,
                width=width,
                height=height,
                target_crs=target_crs,
                cog_block=cog_block,
                overwrite=overwrite,
                max_workers=max_workers,
                retry_timeout=retry_timeout,
                config=config,
                product_label=product_label,
                generate_zarr=generate_zarr,
                chunk_y=chunk_y,
                chunk_x=chunk_x,
                x_coords=x_coords,
                y_coords=y_coords,
            )
            groups_written.append(group_result)

        # ---- Write STAC items and per-tile catalog ----
        catalog_records = []
        for g in groups_written:
            if g['status'] not in ('success', 'skipped'):
                continue
            tk_safe = g.get('track_token', 'UNK').replace('_', '-')
            n_b = g.get('n_bursts', 0)
            zarr_p = static_dir / 'zarr' / f's1grits_static_{mgrs_tile_id}_{direction_label}_TK{tk_safe}_N{n_b:02d}.zarr'
            cog_rel = g.get('layers_written', {}).get(layers[0]) if layers else None

            # Write STAC item
            _write_static_stac_item(
                tile_dir=tile_dir,
                mgrs_tile_id=mgrs_tile_id,
                direction_label=direction_label,
                track_token_safe=tk_safe,
                n_bursts=n_b,
                zarr_path=zarr_p,
                target_res=target_res,
                band_names=layers,
                product_label=product_label,
                master_transform=master_transform,
                width=width,
                height=height,
                target_crs=target_crs,
                cog_path=Path(cog_rel) if cog_rel else None,
                start_datetime=g.get('start_datetime'),
                end_datetime=g.get('end_datetime'),
            )

            # Catalog record (normalized to canonical schema)
            # Compute product instance metadata
            _registry = load_product_registry(workflow_config=config)
            _spec = _registry.get('static')
            _variant_vals = resolve_variant_values(config, _spec.variant_fields)
            _actual_bands = layers  # static bands are the layers we wrote
            _proc_sig = make_processing_signature(_variant_vals)
            _prod_var = make_product_variant('static', _variant_vals, _actual_bands)

            _rec = normalize_catalog_record({
                'item_id': f"{mgrs_tile_id}_{direction_label}_TK{tk_safe}_N{n_b:02d}_static",
                'collection_id': 's1grits-static',
                'product_type': 'static',
                'product_label': product_label,
                'tile_id': mgrs_tile_id,
                'flight_direction': direction_label,
                'crs': str(target_crs),
                'transform': list(master_transform)[:6],
                'width': width,
                'height': height,
                'datetime': None,
                'start_datetime': None,
                'end_datetime': None,
                'month': None,
                # Track-level join key shared with scenes/smonthly (the _N{nn}
                # burst count stays in item_id/n_bursts as provenance, not here).
                'geometry_group_id': f"{mgrs_tile_id}_{direction_label}_TK{tk_safe}",
                'track': int(tk_safe.replace('-', '_')) if tk_safe.replace('-', '_').isdigit() else None,
                'n_bursts': n_b,
                'n_scenes': None,
                'zarr_path': str(zarr_p.relative_to(tile_dir)).replace('\\', '/'),
                'cog_path': str(Path(cog_rel).relative_to(tile_dir)).replace('\\', '/') if cog_rel else None,
                'preview_path': None,
                'bands': _json.dumps(_actual_bands),
                'processing_level': None,
                'product_variant': _prod_var,
                'processing_signature': _proc_sig,
                'processing_variant_json': _json.dumps(_variant_vals),
                'item_path': f"items/{product_label}/{mgrs_tile_id}_{direction_label}_TK{tk_safe}_N{n_b:02d}_static.json",
            })
            catalog_records.append(_rec)

        # Write per-tile catalog (read-merge-dedup-write)
        tile_catalog_path = tile_dir / 'catalog.parquet'
        if catalog_records:
            df_new = pd.DataFrame(catalog_records)
            if tile_catalog_path.exists():
                try:
                    df_existing = pd.read_parquet(tile_catalog_path)
                    df_merged = pd.concat([df_existing, df_new], ignore_index=True)
                    dedup_keys = [k for k in ['item_id', 'product_type', 'tile_id', 'flight_direction',
                                              'geometry_group_id']
                                  if k in df_merged.columns]
                    if dedup_keys:
                        df_merged = df_merged.drop_duplicates(subset=dedup_keys, keep='last')
                    df_catalog = df_merged.sort_values(['product_type', 'tile_id']).reset_index(drop=True)
                except Exception:
                    df_catalog = df_new
            else:
                df_catalog = df_new
            from s1grits.atomic_write import atomic_write_parquet as _awp; _awp(df_catalog, tile_catalog_path)
            logger.info("[Static] Tile catalog: %s (%d rows)", tile_catalog_path, len(df_catalog))

        n_ok = sum(1 for g in groups_written if g['status'] in ('success', 'skipped'))
        status = 'success' if n_ok > 0 else 'failed'
        error = None if n_ok > 0 else 'All groups failed to mosaic'

        return {
            'status': status,
            'error': error,
            'tile_dir': str(tile_dir),
            'groups_written': groups_written,
            'catalog_path': str(tile_catalog_path) if catalog_records else None,
        }

    except Exception as e:
        logger.error("[Static] Tile %s failed: %s", mgrs_tile_id, e, exc_info=True)
        return {
            'status': 'failed',
            'error': str(e),
            'tile_dir': None,
            'groups_written': [],
        }


# ---------------------------------------------------------------------------
# Workflow entry point
# ---------------------------------------------------------------------------

def run_static_layer_workflow(config_path: str | Path, overrides: dict | None = None) -> dict[str, dict]:
    """
    Main static layer workflow: YAML config -> per-acq-group static COG files.

    Groups bursts by acquisition group (track-based burst set) and produces
    one set of static layer COGs per group. RTC-STATIC products are time-
    invariant, so there is no time filtering.

    Args:
        config_path: Path to YAML configuration file.

    Returns:
        dict[mgrs_tile_id, result_dict] where each result_dict has keys:
            status, error, tile_dir, groups_written.
    """
    from s1grits.workflow import apply_output_overrides_and_stac
    from s1grits.runtime_limits import (
        apply_runtime_limits,
        runtime_limits_from_config,
    )
    config = load_config(config_path)
    config = apply_output_overrides_and_stac(config, overrides)
    runtime_limits = runtime_limits_from_config(config)
    applied_runtime_env = apply_runtime_limits(runtime_limits)
    if applied_runtime_env:
        logger.info(
            "[Static] Runtime limits applied: %s",
            ", ".join(f"{k}={v}" for k, v in sorted(applied_runtime_env.items())),
        )
    else:
        logger.info("[Static] Runtime limits disabled")

    layers = _get_enabled_layers(config)
    if not layers:
        logger.info("[Static] No static layers enabled in config, nothing to do.")
        console.print("[dim][Static] static_layers.enabled is false — nothing to do.[/dim]")
        return {}

    console.rule("[bold cyan]S1-GRiTS: Static Layer Workflow (per acq-group)[/bold cyan]", style="cyan")
    console.print(f"[dim]Layers: {', '.join(layers)}[/dim]\n")

    mgrs_tile_ids = enumerate_mgrs_tiles(config)

    # Output root: base_dir directly — multi-modal DataCube root
    output_root = Path(config.get('output', {}).get('base_dir', './output'))

    results: dict[str, dict] = {}
    _lock_info = acquire_lock(str(output_root))
    try:
        for mgrs_tile_id in mgrs_tile_ids:
            console.print(f"\n[bold cyan]Tile: {mgrs_tile_id}[/bold cyan]")

            result = process_static_for_tile(
                mgrs_tile_id=mgrs_tile_id,
                config=config,
                output_root=output_root,
                layers=layers,
            )
            results[mgrs_tile_id] = result

        # Summary
        n_tiles_ok = sum(
            1 for r in results.values() if r['status'] in ('success', 'skipped')
        )
        n_groups_ok = sum(
            sum(1 for g in r.get('groups_written', [])
                if g['status'] in ('success', 'skipped'))
            for r in results.values()
        )

        logger.info(
            "[Static] Complete: %d/%d tiles OK, %d groups written",
            n_tiles_ok, len(results), n_groups_ok,
        )
        console.print(
            f"\n[bold]Static results:[/bold] {n_tiles_ok}/{len(results)} tiles OK, "
            f"{n_groups_ok} group(s) written"
        )
        console.print(f"[bold]Output:[/bold] {output_root}")

        # ---- Merge global catalog and write STAC collection ----
        all_catalogs = []
        # Read existing global catalog (from other workflows) for merge safety
        _global_catalog_path = output_root / 'catalog.parquet'
        if _global_catalog_path.exists():
            try:
                all_catalogs.append(pd.read_parquet(_global_catalog_path))
                logger.info("[Static] Loaded existing global catalog: %d rows", len(all_catalogs[-1]))
            except Exception as _e:
                logger.warning("[Static] Could not read existing global catalog: %s", _e)

        for tile_id, r in results.items():
            cp = r.get('catalog_path')
            if cp and os.path.exists(cp):
                try:
                    all_catalogs.append(pd.read_parquet(cp))
                except Exception as e:
                    logger.warning("[Static] Could not read tile catalog %s: %s", cp, e)

        if all_catalogs:
            df_global = pd.concat(all_catalogs, ignore_index=True)
            dedup_keys = [k for k in ['item_id', 'product_type', 'tile_id', 'flight_direction',
                                      'geometry_group_id']
                          if k in df_global.columns]
            if dedup_keys:
                df_global = df_global.drop_duplicates(subset=dedup_keys, keep='last')

            # Validate collection_id mapping before writing
            validate_collection_mapping(df_global, raise_on_error=True)

            from s1grits.atomic_write import atomic_write_parquet as _awp
            _awp(df_global, _global_catalog_path)
            logger.info("[Static] Global catalog: %s (%d rows)", _global_catalog_path, len(df_global))

            from s1grits.catalog_sync import update_root_catalog
            update_root_catalog(output_root, df_global)

            # Generate STAC collection from global catalog
            try:
                from s1grits.stac_builder import write_stac_collection
                write_stac_collection(df_global, str(output_root), "s1grits-static")
                logger.info("[Static] STAC collection written")
            except Exception as e:
                logger.warning("[Static] STAC collection failed: %s", e)

    finally:
        release_lock(_lock_info)

    return results
