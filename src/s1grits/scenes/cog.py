"""COG / preview export for the scenes and smonthly writers.

Extracted move-only from workflow_scenes.py (which re-exports every name
here). The streamed writer writes ALL bands of each tile-row strip in one
call — the correct order for pixel-interleaved compressed tiled GeoTIFFs
(band-sequential writes fail with a GDAL "dirty block" error once the image
exceeds GDAL_CACHEMAX; see _write_multiband_cog_streamed).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pyproj
import rasterio
import zarr
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.windows import (
    Window,
    from_bounds as window_from_bounds,
    transform as window_transform,
)
from shapely import wkt as shapely_wkt
from shapely.ops import transform as shp_transform

from s1grits.asf_output_writing import (
    _generate_preview_png,
    _get_mgrs_tile_geometry_wkt,
)
from s1grits.logger_config import get_logger
from s1grits.scenes.blocks import N_OBS_BAND

logger = get_logger(__name__)

# Working-set budget for a COG strip write: bounds BOTH the NumPy read buffer
# (all bands of one strip) and GDAL's dirty-tile cache for that strip, so a
# large multi-band write never exceeds the default 512 MB GDAL_CACHEMAX and the
# strip shrinks automatically as the band count grows.
_COG_STRIP_BUDGET_BYTES: int = 256 * 1024 * 1024

def _free_disk_bytes(path: Path) -> int:
    """Free bytes on the volume that will hold ``path`` (nearest existing
    ancestor), or -1 if it cannot be determined."""
    p = Path(path)
    for cand in (p.parent, *p.parents):
        try:
            return int(shutil.disk_usage(cand).free)
        except (OSError, ValueError):
            continue
    return -1

def _write_multiband_cog_streamed(
    cog_path: Path,
    band_reads: list,
    profile: dict,
    *,
    overview_resampling: str = "average",
) -> None:
    """Write a tiled, internally-overviewed multi-band COG, ALL bands per strip.

    ``band_reads`` is ``[(name, read(rows: slice) -> 2D float32 strip), ...]``.
    The image is written in tile-row-aligned horizontal strips, and every band
    of a strip is written in a single ``dst.write(block, window=...)`` call.

    Why all-bands-per-strip: writing a pixel-interleaved, compressed, tiled
    GeoTIFF band-by-band fails once the image exceeds the GDAL block cache
    (``GDAL_CACHEMAX``, 512 MB by default). Finishing band 1 flushes (compresses)
    its tiles to disk; writing band 2 into the SAME tiles — pixel interleave
    stores every band of a tile together — forces GDAL to re-access an already
    flushed compressed block, which raises
    ``GDALRasterBand::IRasterIO -> An error occurred while writing a dirty
    block``. Populating every band of a tile before it is ever flushed removes
    the revisit entirely. A 6608x5620x12 float32 image (~1.8 GB) reproduced this
    deterministically; the strip form is bounded to
    ``_COG_STRIP_BUDGET_BYTES`` regardless of band count.

    The file is written atomically (temp + rename). A write failure is
    re-raised with the band count, dimensions, target path and free disk space,
    so a genuine out-of-space condition is actionable instead of surfacing as a
    deep GDAL "dirty block" error.
    """
    from s1grits.atomic_write import atomic_path
    from s1grits.runtime_limits import rasterio_env_kwargs

    h = int(profile["height"]); w = int(profile["width"])
    nb = len(band_reads)
    block_y = int(profile.get("blockysize") or profile.get("blockxsize") or 256)
    block_y = max(1, block_y)
    # Tile-row-aligned strip sized so all-bands working set <= budget; >= 1 tile
    # row, <= the image height.
    rows_budget = max(1, _COG_STRIP_BUDGET_BYTES // max(1, nb * w * 4))
    strip = max(block_y, (rows_budget // block_y) * block_y)
    strip = min(strip, h)

    # Preflight: a compressed 12-band float32 tile grid + overviews still needs
    # real scratch. Warn early (never block on an estimate — deflate ratios vary)
    # so a near-full volume is visible before the write.
    _uncompressed = nb * w * h * 4
    _free = _free_disk_bytes(cog_path)
    if 0 <= _free < _uncompressed // 3:  # optimistic ~3x deflate lower bound
        logger.warning(
            "[COG] Low free disk for %s: %.1f GB free vs ~%.1f GB uncompressed "
            "(%d bands, %dx%d). deflate usually fits, but overviews + the atomic "
            "temp copy may not — free space or point output at a larger volume.",
            cog_path.name, _free / 1e9, _uncompressed / 1e9, nb, w, h,
        )

    try:
        with atomic_path(cog_path) as _cog_tmp:
            with rasterio.Env(**rasterio_env_kwargs()):
                with rasterio.open(_cog_tmp, "w", **profile) as dst:
                    for _r0 in range(0, h, strip):
                        _r1 = min(h, _r0 + strip)
                        block = np.empty((nb, _r1 - _r0, w), dtype=np.float32)
                        for _bi, (_name, _read) in enumerate(band_reads):
                            block[_bi] = _read(slice(_r0, _r1))
                        dst.write(block, window=Window(0, _r0, w, _r1 - _r0))
                    for _bi, (_name, _read) in enumerate(band_reads, 1):
                        dst.set_band_description(_bi, _name)

                    _short = min(w, h)
                    _factors = [f for f in (2, 4, 8, 16, 32) if _short // f >= 128]
                    if _factors:
                        dst.build_overviews(_factors, Resampling[overview_resampling])
                        dst.update_tags(ns="rio_overview", resampling=overview_resampling)
    except Exception as exc:
        _free = _free_disk_bytes(cog_path)
        _free_str = f"{_free / 1e9:.1f} GB" if _free >= 0 else "unknown"
        raise RuntimeError(
            f"COG export failed writing {nb}-band {w}x{h} GeoTIFF to {cog_path} "
            f"({type(exc).__name__}: {exc}). Free space on the target volume: "
            f"{_free_str}. The Zarr store is already written, so a re-run with "
            f"output.existing_month: skip regenerates only the missing COG once "
            f"space is available (or point output.base_dir at a larger volume)."
        ) from exc

def _write_multiband_cog(
    cog_path: Path,
    bands: list[tuple[str, np.ndarray]],
    profile: dict,
    *,
    overview_resampling: str = "average",
) -> None:
    """Write a tiled, internally-overviewed multi-band GeoTIFF from in-RAM bands.

    Thin wrapper over :func:`_write_multiband_cog_streamed`: the already-resident
    band arrays are exposed as row-slice readers, so the same all-bands-per-strip
    write path is used (robust for large multi-band compressed tiled output; see
    that function). The STAC items advertise these assets as
    ``profile=cloud-optimized``; the internal tiling (from ``profile``) plus the
    overviews written here make that claim accurate.
    """
    band_reads = [
        (name, (lambda rows, a=arr: np.asarray(a[rows], dtype=np.float32)))
        for name, arr in bands
    ]
    _write_multiband_cog_streamed(
        cog_path, band_reads, profile, overview_resampling=overview_resampling
    )

def _tile_clip_crop_window(
    wkt_4326: str,
    target_crs: str,
    transform: Affine,
    height: int,
    width: int,
) -> tuple[int, int, int, int, Affine]:
    """Crop window (r0, r1, c0, c1) + transform for a WKT clip, without arrays.

    Replicates ``_clip_arrays_to_wkt_4326``'s window arithmetic exactly (same
    floor/ceil/clamp and the same empty-window ValueError) so the blockwise
    COG/preview exporter crops to the identical extent the legacy in-memory
    clip produced.
    """
    crs_ll = pyproj.CRS.from_epsg(4326)
    crs_t = pyproj.CRS.from_user_input(target_crs)
    proj = pyproj.Transformer.from_crs(crs_ll, crs_t, always_xy=True).transform
    geom_proj = shp_transform(proj, shapely_wkt.loads(wkt_4326))

    minx, miny, maxx, maxy = geom_proj.bounds
    win = window_from_bounds(minx, miny, maxx, maxy, transform=transform)

    r0 = max(0, int(np.floor(win.row_off)))
    c0 = max(0, int(np.floor(win.col_off)))
    r1 = min(int(height), int(np.ceil(win.row_off + win.height)))
    c1 = min(int(width), int(np.ceil(win.col_off + win.width)))
    if r1 <= r0 or c1 <= c0:
        raise ValueError("Clip window is empty; check WKT/CRS/transform.")
    new_transform = window_transform(
        Window(col_off=c0, row_off=r0, width=c1 - c0, height=r1 - r0), transform
    )
    return r0, r1, c0, c1, new_transform

def _write_multiband_cog_windowed(
    cog_path: Path,
    band_reads: list,
    profile: dict,
    *,
    row_strip: int = 0,
    overview_resampling: str = "average",
) -> None:
    """Stream a multi-band COG from ``band_reads`` (name, row-slice reader).

    Delegates to :func:`_write_multiband_cog_streamed`, which writes ALL bands
    of each tile-row-aligned strip in one call — the correct order for a
    pixel-interleaved compressed tiled GeoTIFF (band-sequential writes fail with
    a GDAL "dirty block" error once the image exceeds GDAL_CACHEMAX; see the
    streamed writer). ``row_strip`` is accepted for backward compatibility but
    ignored: the strip height is derived from the tile size and the working-set
    budget so it is always tile-aligned and memory-bounded.
    """
    _write_multiband_cog_streamed(
        cog_path, band_reads, profile, overview_resampling=overview_resampling
    )

def _export_scene_cog_preview_from_zarr(
    g: zarr.Group,
    time_index: int,
    band_names: list[str],
    transform: Affine,
    height: int,
    width: int,
    target_crs: str,
    tile_clip: bool,
    mgrs_tile_id: str,
    generate_cog: bool,
    generate_preview: bool,
    cog_path: Path,
    png_path: Path,
    cog_block: int,
    chunk_y: int,
    copol_name: str,
    crosspol_name: str,
    tile_dir: Path,
    dt_str: str,
) -> tuple[str | None, str | None]:
    """Export one scene timestep's COG + preview by reading back from Zarr.

    The COG streams band strips straight from the store (memory: one strip),
    cropped to the MGRS tile bbox when ``tile_clip`` is on — the Zarr bands
    are already polygon-masked by the block clip, so the bbox crop reproduces
    the legacy ``_clip_arrays_to_wkt_4326`` output exactly. The preview needs
    the two dB planes (plus the derived display ratio) of the crop resident at
    once — an O(crop) term, still far below the legacy all-bands residency.
    """
    band_names = [b for b in band_names if b != N_OBS_BAND]

    r0, r1, c0, c1 = 0, int(height), 0, int(width)
    out_transform = transform
    if tile_clip:
        try:
            mgrs_wkt = _get_mgrs_tile_geometry_wkt(mgrs_tile_id)
            r0, r1, c0, c1, out_transform = _tile_clip_crop_window(
                mgrs_wkt, target_crs, transform, int(height), int(width)
            )
        except Exception as _clip_e:
            logger.warning(
                "tile_clip spatial crop failed for %s %s: %s",
                mgrs_tile_id, dt_str, _clip_e,
            )
            r0, r1, c0, c1 = 0, int(height), 0, int(width)
            out_transform = transform

    cog_relpath = None
    if generate_cog:
        prof = {
            'driver': 'GTiff', 'dtype': 'float32', 'nodata': float('nan'),
            'width': c1 - c0, 'height': r1 - r0,
            'count': len(band_names),
            'crs': target_crs, 'transform': out_transform,
            'compress': 'deflate', 'tiled': True,
            'blockxsize': cog_block, 'blockysize': cog_block,
        }
        _strip = max(int(chunk_y or cog_block), int(cog_block))
        _strip = ((_strip + int(cog_block) - 1) // int(cog_block)) * int(cog_block)

        def _band_reader(_name):
            _z = g[_name]

            def _read(rows: slice) -> np.ndarray:
                return np.asarray(
                    _z[time_index, r0 + rows.start:r0 + rows.stop, c0:c1],
                    dtype=np.float32,
                )
            return _read

        _write_multiband_cog_windowed(
            cog_path, [(n, _band_reader(n)) for n in band_names], prof,
            row_strip=_strip,
        )
        cog_relpath = str(cog_path.relative_to(tile_dir))

    preview_relpath = None
    if generate_preview:
        _vv = np.asarray(g[copol_name][time_index, r0:r1, c0:c1], dtype=np.float32)
        _vh = np.asarray(g[crosspol_name][time_index, r0:r1, c0:c1], dtype=np.float32)
        # Same display-ratio derivation as the legacy path: linear power ratio
        # from the dB planes, valid where both are finite.
        _valid = np.isfinite(_vv) & np.isfinite(_vh)
        ratio_arr = np.full_like(_vv, np.nan, dtype=np.float32)
        ratio_arr[_valid] = np.power(
            10.0, (_vh[_valid] - _vv[_valid]) / 10.0
        ).astype(np.float32)
        _generate_preview_png(
            vv_db=_vv,
            vh_db=_vh,
            ratio=ratio_arr,
            src_transform=out_transform,
            src_crs=target_crs,
            output_path=str(png_path),
        )
        preview_relpath = str(png_path.relative_to(tile_dir))

    return cog_relpath, preview_relpath
