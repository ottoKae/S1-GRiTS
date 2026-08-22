"""Windowed mosaic readers for the blockwise writers.

Extracted move-only from workflow_scenes.py (which re-exports every name here).
Per-block first-valid mosaics with a direct-copy fast path for master-grid-
aligned bursts, per-scene destination bounds for block skipping, and the
one-time pre-alignment warp for cross-grid scenes.

The full-frame fallback is resolved through the workflow_scenes facade at call
time (_ws_mosaic_align) so the established test seam — monkeypatching
workflow_scenes._mosaic_align — keeps covering the windowed readers.
"""
from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pyproj
from rasterio.enums import Resampling
from rasterio.transform import Affine, array_bounds
from rasterio.warp import reproject, transform_bounds

from s1grits.asf_io import NODATA_SENTINEL
from s1grits.logger_config import get_logger
from s1grits.scenes.blocks import _block_transform

logger = get_logger(__name__)


def _ws_mosaic_align(*args, **kwargs):
    """Full-frame mosaic fallback, dispatched through the workflow_scenes
    facade at call time so tests (and downstream code) that patch
    ``workflow_scenes._mosaic_align`` keep covering the windowed readers."""
    from s1grits import workflow_scenes as _ws
    return _ws._mosaic_align(*args, **kwargs)

def _can_window_reproject(indices: list[int], final_arr: list, prof_arr: list) -> bool:
    if not indices or prof_arr is None or len(prof_arr) <= max(indices):
        return False
    for idx in indices:
        if idx >= len(final_arr):
            return False
        prof = prof_arr[idx]
        if not isinstance(prof, Mapping):
            # This should never happen after sanitization
            logger.debug(
                "[CAN_REPROJECT] prof_arr[%d] is %s, not Mapping (should have been sanitized)",
                idx, type(prof).__name__ if prof is not None else "None"
            )
            return False
        if prof.get("transform") is None or prof.get("crs") is None:
            # This should never happen after sanitization
            logger.debug(
                "[CAN_REPROJECT] prof_arr[%d] missing transform=%s or crs=%s (should have been sanitized)",
                idx, prof.get("transform") is not None, prof.get("crs") is not None
            )
            return False
    return True

def _as_affine(value) -> Affine:
    return value if isinstance(value, Affine) else Affine(*value)

def _crs_equal(left, right) -> bool:
    try:
        return pyproj.CRS.from_user_input(left) == pyproj.CRS.from_user_input(right)
    except Exception:
        return str(left).lower() == str(right).lower()

def _direct_copy_offsets(
    src_prof: Mapping,
    dst_transform: Affine,
    target_crs: str,
    *,
    atol: float = 1e-6,
) -> tuple[int, int] | None:
    """Return source row/col for a destination block if grids are aligned."""
    if not isinstance(src_prof, Mapping):
        return None
    src_crs = src_prof.get("crs")
    src_transform = src_prof.get("transform")
    if src_crs is None or src_transform is None:
        return None
    if not _crs_equal(src_crs, target_crs):
        return None

    src_t = _as_affine(src_transform)
    dst_t = _as_affine(dst_transform)
    # Direct slicing is only valid for north-up, non-skewed grids with the same
    # pixel size. Rotated/sheared grids stay on the GDAL reproject path.
    if not (
        np.isclose(src_t.b, 0.0, atol=atol)
        and np.isclose(src_t.d, 0.0, atol=atol)
        and np.isclose(dst_t.b, 0.0, atol=atol)
        and np.isclose(dst_t.d, 0.0, atol=atol)
        and np.isclose(src_t.a, dst_t.a, atol=atol)
        and np.isclose(src_t.e, dst_t.e, atol=atol)
    ):
        return None

    src_col_f, src_row_f = (~src_t) * (dst_t.c, dst_t.f)
    src_col = int(round(src_col_f))
    src_row = int(round(src_row_f))
    if not (
        np.isclose(src_col_f, src_col, atol=atol)
        and np.isclose(src_row_f, src_row, atol=atol)
    ):
        return None
    return src_row, src_col

def _mosaic_align_window_direct_copy(
    src_arr: np.ndarray,
    src_prof: Mapping,
    dst_transform: Affine,
    target_crs: str,
    bh: int,
    bw: int,
) -> np.ndarray | None:
    offsets = _direct_copy_offsets(src_prof, dst_transform, target_crs)
    if offsets is None:
        return None
    src_row0, src_col0 = offsets
    src_h, src_w = src_arr.shape[-2], src_arr.shape[-1]

    src_row_start = max(0, src_row0)
    src_col_start = max(0, src_col0)
    src_row_stop = min(src_h, src_row0 + bh)
    src_col_stop = min(src_w, src_col0 + bw)
    if src_row_stop <= src_row_start or src_col_stop <= src_col_start:
        return np.full((bh, bw), np.nan, dtype=np.float32)

    dst_row_start = src_row_start - src_row0
    dst_col_start = src_col_start - src_col0
    dst_row_stop = dst_row_start + (src_row_stop - src_row_start)
    dst_col_stop = dst_col_start + (src_col_stop - src_col_start)

    out = np.full((bh, bw), np.nan, dtype=np.float32)
    out[dst_row_start:dst_row_stop, dst_col_start:dst_col_stop] = src_arr[
        src_row_start:src_row_stop,
        src_col_start:src_col_stop,
    ].astype(np.float32, copy=False)
    return out

def _scene_dst_bounds(
    src,
    prof: Mapping,
    transform: Affine,
    target_crs: str,
    height: int,
    width: int,
    *,
    margin: int = 2,
) -> tuple[int, int, int, int] | None:
    """Pixel bounds ``(row0, row1, col0, col1)`` of a scene on the master grid.

    Bounds are clipped to the grid, so a scene fully outside the grid yields an
    empty range (``row1 <= row0`` or ``col1 <= col0``).  Returns ``None`` when
    the footprint cannot be determined (missing profile), which callers must
    treat as "may cover anything".
    """
    if src is None or not isinstance(prof, Mapping):
        return None
    src_transform = prof.get("transform")
    src_crs = prof.get("crs")
    if src_transform is None or src_crs is None:
        return None
    try:
        h, w = np.asarray(src).shape[-2:]
        left, bottom, right, top = array_bounds(h, w, _as_affine(src_transform))
        if not _crs_equal(src_crs, target_crs):
            left, bottom, right, top = transform_bounds(
                src_crs, target_crs, left, bottom, right, top, densify_pts=21
            )
        inv = ~_as_affine(transform)
        c0, r0 = inv * (left, top)
        c1, r1 = inv * (right, bottom)
    except Exception as exc:
        logger.debug("Scene footprint bounds failed: %s", exc)
        return None
    eps = 1e-9  # tolerate float noise so exact pixel edges stay tight
    row0 = int(np.floor(min(r0, r1) + eps)) - margin
    row1 = int(np.ceil(max(r0, r1) - eps)) + margin
    col0 = int(np.floor(min(c0, c1) + eps)) - margin
    col1 = int(np.ceil(max(c0, c1) - eps)) + margin
    return (
        max(0, row0), min(int(height), row1),
        max(0, col0), min(int(width), col1),
    )

def _compute_scene_dst_bounds(
    final_arr: list,
    prof_arr: list,
    transform: Affine,
    target_crs: str,
    height: int,
    width: int,
) -> list[tuple[int, int, int, int] | None]:
    """Per-scene master-grid pixel bounds, aligned with ``final_arr`` indices."""
    bounds: list[tuple[int, int, int, int] | None] = []
    for src, prof in zip(final_arr, prof_arr or []):
        bounds.append(
            _scene_dst_bounds(src, prof, transform, target_crs, height, width)
        )
    # prof_arr may be shorter than final_arr (tests, degraded inputs)
    bounds.extend([None] * (len(final_arr) - len(bounds)))
    return bounds

def _bounds_intersect_block(
    bounds: tuple[int, int, int, int],
    y_slice: slice,
    x_slice: slice,
) -> bool:
    row0, row1, col0, col1 = bounds
    return (
        row0 < int(y_slice.stop or 0)
        and row1 > int(y_slice.start or 0)
        and col0 < int(x_slice.stop or 0)
        and col1 > int(x_slice.start or 0)
    )

def _prealign_scenes_to_master_grid(
    final_arr: list,
    prof_arr: list,
    transform: Affine,
    target_crs: str,
    height: int,
    width: int,
    resampling: Resampling = Resampling.nearest,
) -> tuple[list, list]:
    """Warp scenes that cannot be direct-copied onto the master grid, once.

    Scenes whose grid differs from the master grid (other CRS/UTM zone,
    non-integer pixel offset, different resolution) would otherwise hit the
    GDAL ``reproject`` path once per spatial block — O(scenes x blocks) warps.
    This replaces each such scene with a master-grid-aligned window covering
    its footprint, so every later block read is a plain slice copy.

    Returns shallow-copied lists; the caller's originals are not mutated (they
    may be shared with the per-scene writers).  Memory cost is roughly one
    extra copy of each warped scene for the lifetime of the returned lists.
    """
    if not final_arr or not prof_arr:
        return final_arr, prof_arr
    new_final = list(final_arr)
    new_prof = list(prof_arr)
    base_t = _as_affine(transform)
    n_warped = 0
    for idx in range(min(len(final_arr), len(prof_arr))):
        src = final_arr[idx]
        prof = prof_arr[idx]
        if src is None or not isinstance(prof, Mapping):
            continue
        if prof.get("transform") is None or prof.get("crs") is None:
            continue
        if _direct_copy_offsets(prof, base_t, target_crs) is not None:
            continue  # already slice-copyable for every block
        b = _scene_dst_bounds(src, prof, base_t, target_crs, height, width)
        if b is None:
            continue
        row0, row1, col0, col1 = b
        if row1 <= row0 or col1 <= col0:
            continue  # outside the master grid; block filtering skips it
        dst_transform = base_t * Affine.translation(col0, row0)
        dst = np.full((row1 - row0, col1 - col0), np.nan, dtype=np.float32)
        try:
            reproject(
                source=np.asarray(src, dtype=np.float32),
                destination=dst,
                src_transform=prof["transform"],
                src_crs=prof["crs"],
                src_nodata=prof.get("nodata"),
                dst_transform=dst_transform,
                dst_crs=target_crs,
                dst_nodata=np.nan,
                resampling=resampling,
                num_threads=1,
            )
        except Exception as exc:
            logger.debug(
                "Pre-align warp failed for scene %d; keeping original: %s",
                idx, exc,
            )
            continue
        p = dict(prof)
        p.update(
            transform=dst_transform,
            crs=target_crs,
            nodata=np.nan,
            height=dst.shape[0],
            width=dst.shape[1],
        )
        new_final[idx] = dst
        new_prof[idx] = p
        n_warped += 1
    if n_warped:
        logger.info(
            "Pre-aligned %d cross-grid scene(s) to the master grid "
            "(one-time warp replaces per-block reprojection)",
            n_warped,
        )
    return new_final, new_prof

def _mosaic_align_window(
    indices: list[int],
    final_arr: list,
    prof_arr: list,
    height: int,
    width: int,
    transform: Affine,
    target_crs: str,
    y_slice: slice,
    x_slice: slice,
    scene_bounds: list | None = None,
    resampling: Resampling = Resampling.nearest,
) -> np.ndarray | None:
    """Mosaic only one destination block.

    This is the blockwise equivalent of ``_mosaic_align`` for the scenes
    monthly writer.  It keeps the same first-valid-pixel source policy while
    avoiding construction of full-tile arrays for each acquisition.
    """
    bh = int((y_slice.stop or 0) - (y_slice.start or 0))
    bw = int((x_slice.stop or 0) - (x_slice.start or 0))
    if bh <= 0 or bw <= 0:
        return None

    # Tests and unusual in-memory callers may not provide rasterio profiles.
    # Fall back to the existing full-grid helper and slice the result.
    if not _can_window_reproject(indices, final_arr, prof_arr):
        logger.warning(
            "[BLOCKWISE FALLBACK] Block y=%d:%d x=%d:%d falling back to full-tile "
            "mosaic (prof_arr incomplete: %d entries, %d arrays needed). "
            "This negates blockwise memory efficiency.",
            y_slice.start or 0, y_slice.stop or 0,
            x_slice.start or 0, x_slice.stop or 0,
            len(prof_arr) if prof_arr else 0,
            len(final_arr) if final_arr else 0,
        )
        full = _ws_mosaic_align(
            indices, final_arr, prof_arr, height, width, transform, target_crs,
            resampling=resampling,
        )
        if full is None:
            return None
        return full[y_slice, x_slice].astype(np.float32, copy=False)

    out: np.ndarray | None = None
    dst_transform = _block_transform(transform, y_slice, x_slice)

    for idx in indices:
        src = final_arr[idx]
        prof = prof_arr[idx]
        if src is None:
            continue
        if scene_bounds is not None and idx < len(scene_bounds):
            sb = scene_bounds[idx]
            # Known footprint that misses this block entirely: skip without
            # allocating a window.  None means "footprint unknown" -> keep.
            if sb is not None and not _bounds_intersect_block(sb, y_slice, x_slice):
                continue
        # Pass the source straight to direct-copy so a lazy (GeoTIFF-backed) or
        # memmap burst reads only the block window (Phase 3.2). Full
        # materialisation happens only in the reproject fallback below.
        tmp = _mosaic_align_window_direct_copy(
            src, prof, dst_transform, target_crs, bh, bw
        )
        if tmp is None:
            tmp = np.full((bh, bw), np.nan, dtype=np.float32)
            try:
                reproject(
                    source=np.asarray(src, dtype=np.float32),
                    destination=tmp,
                    src_transform=prof["transform"],
                    src_crs=prof["crs"],
                    src_nodata=prof.get("nodata"),
                    dst_transform=dst_transform,
                    dst_crs=target_crs,
                    dst_nodata=np.nan,
                    resampling=resampling,
                    num_threads=1,
                )
            except Exception as exc:
                logger.debug("Windowed reproject failed; falling back to full mosaic: %s", exc)
                full = _ws_mosaic_align(
                    indices, final_arr, prof_arr, height, width, transform,
                    target_crs, resampling=resampling,
                )
                if full is None:
                    return None
                return full[y_slice, x_slice].astype(np.float32, copy=False)

        tmp[~np.isfinite(tmp) | (tmp <= 0)] = np.nan
        if out is None:
            # First contributing scene: adopt its buffer directly instead of
            # merging into a pre-allocated NaN window.  With one scene per
            # call (the composite path) this skips the merge entirely.
            out = tmp
        else:
            take = np.isnan(out) & np.isfinite(tmp)
            if take.any():
                out[take] = tmp[take]

    return out

def _mosaic_align_scene_window(
    indices: list[int],
    arr_list: list,
    prof_list: list,
    height: int,
    width: int,
    transform: Affine,
    target_crs: str,
    y_slice: slice,
    x_slice: slice,
    scene_bounds: list | None = None,
    resampling: Resampling = Resampling.nearest,
) -> np.ndarray | None:
    """Windowed replica of ``_mosaic_align`` for the blockwise scenes writer.

    ``_mosaic_align_window`` (the smonthly composite reader) drops non-positive
    values, which is fine for its dB-bound consumers but NOT value-identical to
    the scene writer's full-frame mosaic: the legacy scenes path keeps zeros /
    negatives in the linear arrays and lets Ratio/RVI/_linear_to_db decide.
    This variant reproduces ``_mosaic_align``'s exact validity rule per window
    (finite, not the NODATA_SENTINEL, not the profile nodata) and its
    first-valid-pixel fill order, so a window is bit-identical to the same
    slice of the full-frame mosaic (locked by
    tests/test_scenes_blockwise_writer.py).

    Returns ``None`` when no burst contributes to the window (callers leave the
    NaN-initialised Zarr slot untouched — same values as writing NaN).
    """
    bh = int((y_slice.stop or 0) - (y_slice.start or 0))
    bw = int((x_slice.stop or 0) - (x_slice.start or 0))
    if bh <= 0 or bw <= 0:
        return None
    valid_idx = [i for i in indices if arr_list[i] is not None]
    if not valid_idx:
        return None

    if not _can_window_reproject(valid_idx, arr_list, prof_list):
        # Degraded inputs without rasterio profiles (tests, unusual callers):
        # fall back to the full-grid mosaic and slice it.
        logger.debug(
            "Scene window mosaic falling back to full-frame _mosaic_align "
            "(profiles incomplete)"
        )
        full = _ws_mosaic_align(
            indices, arr_list, prof_list, height, width, transform, target_crs,
            resampling=resampling,
        )
        if full is None:
            return None
        return np.ascontiguousarray(full[y_slice, x_slice]).astype(
            np.float32, copy=False
        )

    dst_transform = _block_transform(transform, y_slice, x_slice)
    out = np.full((bh, bw), np.nan, dtype=np.float32)
    wrote_any = False
    for i in valid_idx:
        if scene_bounds is not None and i < len(scene_bounds):
            sb = scene_bounds[i]
            if sb is not None and not _bounds_intersect_block(sb, y_slice, x_slice):
                continue
        prof = prof_list[i]
        src_nd = prof.get("nodata", None)
        src_nd = np.nan if src_nd is None else float(src_nd)
        # Pass the source straight to the direct-copy helper — for a lazy
        # (GeoTIFF-backed) or memmap burst this reads ONLY the block window, the
        # Phase 3.2 memory bound. Full materialisation happens only in the
        # reproject fallback below (rare cross-grid case).
        _src = arr_list[i]

        tmp = _mosaic_align_window_direct_copy(
            _src, prof, dst_transform, target_crs, bh, bw
        )
        if tmp is not None:
            # Direct slice copy: apply the same exclusions the sentinel warp
            # produces — profile nodata, non-finite, and (like the full-frame
            # path) any genuine value equal to the sentinel itself.
            valid_mask = np.isfinite(tmp) & (tmp != NODATA_SENTINEL)
            if np.isfinite(src_nd):
                valid_mask &= tmp != np.float32(src_nd)
        else:
            tmp = np.full((bh, bw), NODATA_SENTINEL, dtype=np.float32)
            reproject(
                source=np.asarray(_src, dtype=np.float32),
                destination=tmp,
                src_transform=prof["transform"],
                src_crs=prof["crs"],
                dst_transform=dst_transform,
                dst_crs=target_crs,
                resampling=resampling,
                src_nodata=float(src_nd),
                dst_nodata=NODATA_SENTINEL,
                init_dest_nodata=True,
                num_threads=1,
            )
            valid_mask = (tmp != NODATA_SENTINEL) & np.isfinite(tmp)

        take = np.isnan(out) & valid_mask
        if take.any():
            out[take] = tmp[take]
            wrote_any = True
    return out if wrote_any else None
