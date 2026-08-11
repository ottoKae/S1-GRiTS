"""Zarr store creation, master-grid adoption, and timestep appends.

Extracted move-only from workflow_scenes.py (which re-exports every name
here): store init with grid lock, adoption of an existing store's grid,
MGRS-bounds grid expansion for fresh grids, and the append-timestep path.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyproj
import zarr
from rasterio.transform import Affine

from shapely import wkt as shapely_wkt
from shapely.ops import transform as shp_transform

from s1grits.asf_output_writing import _get_mgrs_tile_geometry_wkt
from s1grits.scenes.blocks import N_OBS_BAND
from s1grits.logger_config import get_logger
from s1grits.zarr_encoding import create_zarr_array, record_zarr_compression

logger = get_logger(__name__)

def _init_zarr_2band(
    zarr_path: Path,
    x_coords: np.ndarray,
    y_coords: np.ndarray,
    target_crs: str,
    transform: Affine,
    chunk_y: int,
    chunk_x: int,
    processing_level: str,
    band_names: list[str] = None,
    rebuild_on_mismatch: bool = False,
) -> zarr.Group:
    """
    Create (or open) a Zarr store with variable band support.
    Dimensions: time(0,), y(H,), x(W,).

    Band names default to ["VV_dB", "VH_dB"] for backward compatibility.
    Additional bands (Ratio, RVI, GLCM) are created when specified.

    ``rebuild_on_mismatch`` (wired from ``output.overwrite``) controls what
    happens when a store exists but is incompatible with the requested grid or
    band set: False (default) raises with recovery options; True deletes the
    incompatible store and re-creates it fresh on the requested grid.
    """
    if band_names is None:
        band_names = ["VV_dB", "VH_dB"]
    zarr_path.parent.mkdir(parents=True, exist_ok=True)

    if zarr_path.exists():
        _incompat: str | None = None
        try:
            g_check = zarr.open_group(str(zarr_path), mode='r', zarr_format=3)
            z_h = g_check['y'].shape[0]
            z_w = g_check['x'].shape[0]

            H, W = len(y_coords), len(x_coords)
            if H != z_h or W != z_w:
                _incompat = (
                    f"grid mismatch: existing=({z_h},{z_w}) new=({H},{W})"
                )
            else:
                # Validate that all expected band datasets exist
                _missing = [b for b in band_names if b not in g_check]
                if _missing:
                    _incompat = f"missing band(s): {_missing}"
            if _incompat is None:
                g = zarr.open_group(str(zarr_path), mode='r+', zarr_format=3)
                logger.info(
                    "[Zarr] Opened existing store (%d time steps), grid locked %dx%d",
                    g['time'].shape[0], z_w, z_h,
                )
                return g
        except (KeyError, ValueError) as e:
            _incompat = f"unreadable or malformed store: {e}"

        if rebuild_on_mismatch:
            logger.warning(
                "[Zarr] existing_store=rebuild-incompatible: rebuilding "
                "incompatible store %s (%s). Existing time steps in this "
                "store are discarded.",
                zarr_path, _incompat,
            )
            import shutil
            shutil.rmtree(zarr_path)
        else:
            raise RuntimeError(
                f"Cannot resume: existing Zarr at {zarr_path} is incompatible "
                f"({_incompat}). A grid mismatch usually means the processing "
                f"window changed (e.g. pilot month -> full year), so this run's "
                f"burst-union master grid differs from the stored grid. "
                f"Options: (1) rerun with output.existing_store: "
                f"rebuild-incompatible (legacy alias: output.overwrite=true) "
                f"to rebuild this store on the new grid, (2) delete this store "
                f"to re-create it, or (3) use a fresh output.base_dir. Other "
                f"tiles/stores are unaffected."
            )

    # Fresh store
    g = zarr.open_group(str(zarr_path), mode='w', zarr_format=3)
    g.attrs['crs'] = str(target_crs)
    g.attrs['transform'] = list(transform)[:6]
    g.attrs['processing_level'] = processing_level
    # Grid identity and metadata for cross-product alignment
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
    g.attrs['product_type'] = 'scenes'  # overridden by smonthly variant
    g.attrs['geometry_group_id'] = None  # set by caller
    g.attrs['time_varying'] = True
    g.attrs['array_dims'] = ['time', 'y', 'x']
    record_zarr_compression(g.attrs)
    _a = create_zarr_array(g, 'x', data=x_coords, overwrite=True, dimension_names=['x'])
    _a.attrs['_ARRAY_DIMENSIONS'] = ['x']
    _a = create_zarr_array(g, 'y', data=y_coords, overwrite=True, dimension_names=['y'])
    _a.attrs['_ARRAY_DIMENSIONS'] = ['y']
    _a = create_zarr_array(g, 'time', shape=(0,), chunks=(1,), dtype='datetime64[ns]', overwrite=True, dimension_names=['time'])
    _a.attrs['_ARRAY_DIMENSIONS'] = ['time']
    for var in band_names:
        # fill_value=NaN lets blockwise appends resize without materialising
        # all-NaN chunks: unwritten blocks read back as NaN for free. The
        # n_obs count band is uint8 with fill 0 — "no observations" — so
        # unwritten blocks read back as 0 by the same mechanism.
        if var == N_OBS_BAND:
            _dtype, _fill = 'uint8', 0
        else:
            _dtype, _fill = 'float32', np.nan
        _a = create_zarr_array(g, var, shape=(0, _h, _w), chunks=(1, chunk_y, chunk_x), dtype=_dtype, fill_value=_fill, overwrite=True, dimension_names=['time', 'y', 'x'])
        _a.attrs['_ARRAY_DIMENSIONS'] = ['time', 'y', 'x']

    # CF-compliant grid_mapping so generic readers (GDAL/QGIS/rioxarray) can
    # auto-detect the CRS, not just our own code via g.attrs['crs'].
    from s1grits.zarr_cf import add_cf_grid_mapping
    add_cf_grid_mapping(g, str(target_crs))

    return g

def _adopt_existing_master_grid(
    tile_dir: Path,
    target_crs: str,
    target_res: float,
) -> tuple | None:
    """Reuse the grid locked into an existing product store for this tile.

    The burst-union master grid depends on the processing window (and on which
    scenes actually downloaded in the run's first batch), so a rerun over a
    different window (pilot month -> full year) derives a different H x W and
    would fail the store's grid-lock check. On resume (output.overwrite=false)
    we instead adopt the grid already locked into an existing store: it is
    itself a previous burst union (so support-before-clip is preserved), and
    both grids live on the same ``target_res``-aligned lattice
    (``_build_grid_from_bursts`` floor/ceil-snaps bounds), so aligning new
    scenes onto it is an exact crop/pad with no resampling shift.

    Returns ``(transform, width, height, x_coords, y_coords)`` or ``None``
    when there is nothing to adopt (fresh tile, or CRS/resolution changed —
    in which case the caller derives a fresh burst-union grid as before).

    Sibling stores can carry DIFFERENT locked grids (e.g. a per-track store
    created by an interrupted pre-adoption run next to a fully populated
    pilot store). No single master grid can satisfy both, so the grid backed
    by the MOST time steps wins — it protects the most data — and every
    disagreeing store is named in a warning. Under
    ``existing_store: rebuild-incompatible`` the minority stores are then
    rebuilt onto the adopted grid at open time (self-heal); under ``resume``
    they fail their grid-lock check with the recovery options.
    """
    stores = sorted(tile_dir.glob('smonthly_*/zarr/*.zarr')) + \
        sorted(tile_dir.glob('scenes_*/zarr/*.zarr'))
    # candidate grid groups keyed by (w, h, origin): [total_steps, grid, paths]
    groups: dict[tuple, list] = {}
    for zp in stores:
        try:
            g = zarr.open_group(str(zp), mode='r', zarr_format=3)
            crs = g.attrs.get('crs')
            tfm = g.attrs.get('transform')
            if crs is None or tfm is None:
                continue
            if str(crs).upper() != str(target_crs).upper():
                logger.info(
                    "[Grid] Not adopting %s: CRS %s != target %s",
                    zp.name, crs, target_crs,
                )
                continue
            transform = Affine(*[float(v) for v in tfm[:6]])
            if abs(transform.a - float(target_res)) > 1e-6 or \
                    abs(-transform.e - float(target_res)) > 1e-6:
                logger.info(
                    "[Grid] Not adopting %s: resolution (%.3f, %.3f) != target %.3f",
                    zp.name, transform.a, -transform.e, target_res,
                )
                continue
            x_coords = np.asarray(g['x'][:], dtype='float64')
            y_coords = np.asarray(g['y'][:], dtype='float64')
            if x_coords.size == 0 or y_coords.size == 0:
                continue
            n_steps = int(g['time'].shape[0]) if 'time' in g else 0
            key = (int(x_coords.size), int(y_coords.size),
                   round(transform.c, 3), round(transform.f, 3))
            grp = groups.setdefault(key, [0, None, []])
            grp[0] += n_steps
            if grp[1] is None:
                grp[1] = (transform, int(x_coords.size), int(y_coords.size),
                          x_coords, y_coords)
            grp[2].append((zp, n_steps))
        except Exception as exc:
            logger.debug("[Grid] Could not read %s for grid adoption: %s", zp, exc)
            continue

    if not groups:
        return None

    # Most data wins; deterministic tie-break on the grid key.
    chosen_key = max(groups, key=lambda k: (groups[k][0], k))
    if len(groups) > 1:
        detail = "; ".join(
            f"{w}x{h}: " + ", ".join(f"{p.name} ({n} steps)" for p, n in grp[2])
            for (w, h, *_), grp in sorted(groups.items())
        )
        logger.warning(
            "[Grid] Tile %s has stores on %d DIFFERENT grids (%s). Adopting "
            "the grid with the most time steps (%dx%d). Stores on other "
            "grids will fail their grid-lock check under existing_store="
            "resume, or be rebuilt onto the adopted grid under "
            "existing_store=rebuild-incompatible.",
            tile_dir.name, len(groups), detail,
            chosen_key[0], chosen_key[1],
        )
    _, grid, members = groups[chosen_key]
    logger.info(
        "[Grid] Adopting locked grid %dx%d from %s (%d store(s), %d total steps)",
        chosen_key[0], chosen_key[1], members[0][0].name,
        len(members), groups[chosen_key][0],
    )
    return grid

def _expand_grid_to_tile_bounds(
    transform: Affine,
    width: int,
    height: int,
    x_coords: np.ndarray,
    y_coords: np.ndarray,
    mgrs_tile_id: str,
    target_crs: str,
    target_res: float,
) -> tuple:
    """Grow a freshly derived burst-union master grid to fully cover the tile.

    The master grid comes from the FIRST batch's burst union, and batches run
    chronologically — so a fresh full-archive run derives its grid from the
    earliest era (e.g. 2016), whose data-take framing may miss bursts that
    later eras have. If those bursts touch a tile edge, every later batch
    would be silently cropped to the smaller grid for the store's lifetime.

    Expanding the grid to at least the MGRS tile bounds makes it
    era-independent where it matters: outputs are tile-clipped anyway, so
    covering the tile is the only grid property that affects data content,
    while the burst-union margins beyond the tile are preserved for the
    support-before-clip invariant. Snapping the tile bounds to the
    ``target_res`` lattice keeps the expanded grid on the same pixel lattice
    as the input grid (both floor/ceil to multiples of the resolution).

    Applies only to freshly derived grids — adopted (store-locked) grids must
    never be modified, or they would fail their stores' grid-lock checks.
    Returns the input tuple unchanged when the union already covers the tile
    or the tile geometry cannot be resolved.
    """
    grid = (transform, width, height, x_coords, y_coords)
    try:
        wkt = _get_mgrs_tile_geometry_wkt(mgrs_tile_id)
        crs_ll = pyproj.CRS.from_epsg(4326)
        crs_t = pyproj.CRS.from_user_input(target_crs)
        proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
        t_minx, t_miny, t_maxx, t_maxy = shp_transform(
            proj, shapely_wkt.loads(wkt)
        ).bounds
    except Exception as exc:
        logger.warning(
            "[Grid] Could not resolve MGRS tile bounds for %s; keeping the "
            "burst-union grid as-is: %s",
            mgrs_tile_id, exc,
        )
        return grid

    res = float(target_res)
    minx, maxy = float(transform.c), float(transform.f)
    maxx = minx + width * res
    miny = maxy - height * res
    n_minx = min(minx, float(np.floor(t_minx / res) * res))
    n_miny = min(miny, float(np.floor(t_miny / res) * res))
    n_maxx = max(maxx, float(np.ceil(t_maxx / res) * res))
    n_maxy = max(maxy, float(np.ceil(t_maxy / res) * res))
    if (n_minx, n_miny, n_maxx, n_maxy) == (minx, miny, maxx, maxy):
        return grid

    new_width = int(round((n_maxx - n_minx) / res))
    new_height = int(round((n_maxy - n_miny) / res))
    new_transform = Affine(res, 0.0, n_minx, 0.0, -res, n_maxy)
    new_x = (n_minx + (np.arange(new_width) + 0.5) * res).astype("float64")
    new_y = (n_maxy - (np.arange(new_height) + 0.5) * res).astype("float64")
    logger.warning(
        "[Grid] First-batch burst union for %s does not cover the MGRS tile; "
        "expanding master grid %dx%d -> %dx%d so later-era bursts inside the "
        "tile are never cropped.",
        mgrs_tile_id, width, height, new_width, new_height,
    )
    return new_transform, new_width, new_height, new_x, new_y

def _zarr_append(g: "zarr.Group", var_name: str, data: "np.ndarray") -> None:
    """Append one slice along axis-0 to a zarr v3 array (v3 has no .append())."""
    arr = g[var_name]
    t = arr.shape[0]
    arr.resize((t + 1,) + arr.shape[1:])
    arr[t, ...] = data

def _append_zarr_timestep(
    g: zarr.Group,
    dt_ns: np.datetime64,
    band_arrays: list[tuple[str, np.ndarray]],
) -> None:
    """Append one time step to an open Zarr group with variable bands.

    Raises ValueError if dt_ns already exists (duplicate time step)
    or if any band array shape does not match the existing dataset.
    """
    # ---- Time dedup (int64 ns comparison for cross-platform stability) ----
    _existing = set(g['time'][:].tolist()) if g['time'].shape[0] > 0 else set()
    _new_key = np.datetime64(dt_ns, 'ns').astype('int64')
    if _new_key in _existing:
        _ts = pd.Timestamp(dt_ns).strftime('%Y-%m-%dT%H:%M:%S')
        raise ValueError(
            f"Duplicate time step {_ts} — already exists in Zarr store. "
            f"Use overwrite mode or delete the store to re-process."
        )

    # ---- Validate every band shape BEFORE mutating the store, so a mismatch
    # cannot leave an orphaned time step (time length > data length). ----
    for var, arr in band_arrays:
        _dset = g[var]
        _expected = (_dset.shape[1], _dset.shape[2]) if _dset.ndim == 3 else _dset.shape
        if len(_dset.shape) >= 2 and arr.shape != _expected:
            raise ValueError(
                f"Shape mismatch for '{var}': got {arr.shape}, "
                f"expected {_expected}. Grid may have changed between runs."
            )

    # Append band data first, then the time coordinate last: an interrupted
    # band write then cannot orphan a time step. Cast to each band's stored
    # dtype (float32 for radiometric bands, uint8 for the n_obs count band).
    for var, arr in band_arrays:
        _zarr_append(g, var, arr.astype(g[var].dtype, copy=False))
    _zarr_append(g, 'time', np.array([_new_key], dtype='int64'))
