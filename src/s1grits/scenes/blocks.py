"""Blockwise Zarr write machinery shared by the scenes and smonthly writers.

Extracted move-only from workflow_scenes.py (which re-exports every name here,
so existing imports and test monkeypatch seams keep working). Chunk-aligned
spatial blocks, the reserve/finalize/rollback timestep protocol, per-block
tile clipping, and the generic band-block writer.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyproj
import zarr
from rasterio.features import rasterize
from rasterio.transform import Affine
from shapely import wkt as shapely_wkt
from shapely.geometry import mapping
from shapely.ops import transform as shp_transform

from s1grits.asf_output_writing import _get_mgrs_tile_geometry_wkt
from s1grits.logger_config import get_logger

logger = get_logger(__name__)

# Per-pixel valid-observation-count band written by the smonthly composite
# writers: how many finite scene observations (from the track whose composite
# fills each pixel) went into that month's composite value. uint8 with
# fill_value=0 ("no observations"): a track's monthly stack depth is bounded
# by the Sentinel-1 revisit (<= ~6 acquisitions/month), far below 255.
N_OBS_BAND: str = "n_obs"

def _iter_spatial_blocks(height: int, width: int, block_y: int, block_x: int):
    """Yield y/x slices aligned to the configured Zarr spatial chunks."""
    by = max(1, int(block_y or height or 1))
    bx = max(1, int(block_x or width or 1))
    for y0 in range(0, int(height), by):
        y1 = min(int(height), y0 + by)
        for x0 in range(0, int(width), bx):
            x1 = min(int(width), x0 + bx)
            yield slice(y0, y1), slice(x0, x1)

def _block_transform(transform: Affine, y_slice: slice, x_slice: slice) -> Affine:
    """Return the geotransform for a spatial block."""
    return transform * Affine.translation(int(x_slice.start or 0), int(y_slice.start or 0))

def _run_blocks(worker, blocks: list, num_threads: int) -> list:
    """Run per-block work serially or in a thread pool, preserving order.

    Spatial blocks are aligned to the Zarr chunk grid, so concurrent block
    writes touch disjoint chunks (safe in zarr v3).  The heavy per-block work
    (bottleneck nanmedian, NumPy copies, GDAL warps, codec compression)
    releases the GIL, so threads give near-linear speedup without the memory
    cost of extra worker processes.
    """
    if num_threads <= 1 or len(blocks) <= 1:
        return [worker(i, ys, xs) for i, (ys, xs) in enumerate(blocks, 1)]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(int(num_threads), len(blocks))) as ex:
        futures = [
            ex.submit(worker, i, ys, xs)
            for i, (ys, xs) in enumerate(blocks, 1)
        ]
        return [f.result() for f in futures]

def _begin_zarr_timestep_blockwise(
    g: zarr.Group,
    dt_ns: np.datetime64,
    band_names: list[str],
) -> tuple[int, int]:
    """Reserve a new Zarr time slot for blockwise writes.

    Band arrays are resized and initialized to NaN.  The time coordinate is
    written only after every block succeeds, matching the existing "time last"
    append semantics.
    """
    existing = set(g['time'][:].tolist()) if g['time'].shape[0] > 0 else set()
    new_key = np.datetime64(dt_ns, 'ns').astype('int64')
    if new_key in existing:
        ts = pd.Timestamp(dt_ns).strftime('%Y-%m-%dT%H:%M:%S')
        raise ValueError(
            f"Duplicate time step {ts} already exists in Zarr store. "
            f"Use overwrite mode or delete the store to re-process."
        )

    t = int(g['time'].shape[0])
    for var in band_names:
        arr = g[var]
        if arr.ndim != 3:
            raise ValueError(f"Band '{var}' is not a 3D time/y/x array")
        if arr.shape[0] > t:
            # Previous interrupted blockwise append left data without a time
            # coordinate.  Time is authoritative because it is committed last.
            arr.resize((t,) + arr.shape[1:])
        if arr.shape[0] < t:
            raise ValueError(
                f"Band '{var}' has fewer timesteps ({arr.shape[0]}) than "
                f"time ({t}); cannot append safely."
            )
        arr.resize((t + 1,) + arr.shape[1:])
        if not _fill_value_is_nan(arr) and np.issubdtype(arr.dtype, np.floating):
            # Legacy FLOAT stores were created with fill_value=0, so the new
            # slot must be NaN-initialised explicitly.  NaN-filled stores get
            # the same semantics from resize alone, without writing any chunks.
            # Integer bands (n_obs, fill 0) get "no data" from resize alone —
            # and cannot hold NaN.
            arr[t, :, :] = np.nan
    return t, int(new_key)

def _fill_value_is_nan(arr) -> bool:
    try:
        return bool(np.isnan(arr.fill_value))
    except (TypeError, ValueError, AttributeError):
        return False

def _finalize_zarr_timestep_blockwise(
    g: zarr.Group,
    time_index: int,
    new_key: int,
) -> None:
    time_arr = g['time']
    if time_arr.shape[0] > time_index:
        time_arr.resize((time_index,))
    time_arr.resize((time_index + 1,))
    time_arr[time_index] = np.array(new_key, dtype='int64')

def _rollback_zarr_timestep_blockwise(
    g: zarr.Group,
    time_index: int,
    band_names: list[str],
) -> None:
    """Remove a reserved blockwise timestep after skip or failure."""
    for name in band_names:
        arr = g[name]
        if arr.shape[0] > time_index:
            arr.resize((time_index,) + arr.shape[1:])
    time_arr = g['time']
    if time_arr.shape[0] > time_index:
        time_arr.resize((time_index,))

def _prepare_block_clip_geom(
    tile_clip: bool,
    mgrs_tile_id: str,
    target_crs: str,
):
    if not tile_clip:
        return None
    mgrs_wkt = _get_mgrs_tile_geometry_wkt(mgrs_tile_id)
    crs_ll = pyproj.CRS.from_epsg(4326)
    crs_t = pyproj.CRS.from_user_input(target_crs)
    proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
    return shp_transform(proj, shapely_wkt.loads(mgrs_wkt))

def _apply_block_clip(
    block_bands: dict[str, np.ndarray],
    clip_geom,
    transform: Affine,
    y_slice: slice,
    x_slice: slice,
) -> None:
    """Mask a block's bands to the MGRS tile polygon (NaN outside).

    Always the LAST step for a block: per-pixel composites and windowed GLCM are
    both produced on the larger burst-union support first, so this clip only
    removes the beyond-tile margin — it never truncates a spatial window
    (support-before-clip invariant).
    """
    if clip_geom is None:
        return
    first = next(iter(block_bands.values()), None)
    if first is None:
        return
    bh, bw = first.shape
    block_mask = rasterize(
        [(mapping(clip_geom), 1)],
        out_shape=(bh, bw),
        transform=_block_transform(transform, y_slice, x_slice),
        fill=0, dtype="uint8", all_touched=False,
    ).astype(bool)
    inv = ~block_mask
    for arr in block_bands.values():
        arr[inv] = np.nan

def _write_smonthly_block_bands(
    g: zarr.Group,
    time_index: int,
    y_slice: slice,
    x_slice: slice,
    band_names: list[str],
    block_bands: dict[str, np.ndarray],
) -> None:
    for name in band_names:
        dst = g[name]
        arr = block_bands[name]
        if np.issubdtype(dst.dtype, np.integer):
            # Count bands (n_obs) travel through the block pipeline as float32
            # so _apply_block_clip can NaN them like every other band; NaN
            # means "no observations" and lands as 0 in the integer store.
            arr = np.nan_to_num(arr, nan=0.0).astype(dst.dtype)
        else:
            arr = arr.astype(np.float32, copy=False)
        dst[time_index, y_slice, x_slice] = arr

# GLCM co-occurrence support radius (window_size//2 + distance) for the fixed
# smonthly texture config (window_size=5, distance=1) is 3 px; 8 is a safe halo
# that makes each block's GLCM bit-identical to the full-tile computation
# (verified in tests/test_glcm_halo_equivalence.py).
GLCM_BLOCK_HALO: int = 8
