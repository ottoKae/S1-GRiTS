"""Shared synthetic tile-month builder for benchmarks and heavy tests.

Produces in-memory burst-like scenes (arrays + rasterio-style profiles) and an
initialised Zarr store matching the smonthly layout, so the blockwise writer
can be exercised without any network or ASF access.  Deterministic given a
seed.  Not part of the shipped package; used only by ``benchmarks/`` and the
memory/concurrency tests.
"""
from __future__ import annotations

import numpy as np
import zarr
from rasterio.transform import Affine


def build_month(
    *,
    height: int = 2048,
    width: int = 2048,
    n_scenes: int = 60,
    n_tracks: int = 2,
    scene_h: int = 640,
    scene_w: int = 1400,
    crs: str = "EPSG:32717",
    seed: int = 3,
):
    """Return ``(final_vv, prof_vv, final_vh, prof_vh, idx_by_track, transform)``.

    Scenes are strips placed at deterministic offsets on the master grid so
    each block is touched by only a handful of them (mirroring real burst
    footprints).  All scenes share the master CRS/grid (direct-copy path).
    """
    rng = np.random.default_rng(seed)
    master = Affine.translation(500000.0, 9500000.0) * Affine.scale(30.0, -30.0)
    final_vv, prof_vv, final_vh, prof_vh = [], [], [], []
    idx_by_track: dict[int, list[int]] = {}
    tracks = [18 + 22 * k for k in range(n_tracks)]
    for i in range(n_scenes):
        row_off = (i * 131) % max(1, height - scene_h)
        col_off = (i * 71) % max(1, width - scene_w)
        t = master * Affine.translation(col_off, row_off)
        prof = {"transform": t, "crs": crs, "nodata": np.nan}
        final_vv.append((rng.random((scene_h, scene_w), dtype=np.float32) + 0.05))
        prof_vv.append(prof)
        final_vh.append((rng.random((scene_h, scene_w), dtype=np.float32) * 0.3 + 0.02))
        prof_vh.append(dict(prof))
        idx_by_track.setdefault(tracks[i % n_tracks], []).append(i)
    return final_vv, prof_vv, final_vh, prof_vh, idx_by_track, master


def init_store(path, height, width, chunk, band_names=("VV_dB", "VH_dB")):
    """Create a fresh smonthly-layout Zarr store for the synthetic grid."""
    g = zarr.open_group(str(path), mode="w", zarr_format=3)
    g.create_array("x", data=np.arange(width, dtype=np.float64))
    g.create_array("y", data=np.arange(height, dtype=np.float64))
    g.create_array("time", shape=(0,), chunks=(64,), dtype="int64")
    for b in band_names:
        g.create_array(
            b, shape=(0, height, width), chunks=(1, chunk, chunk),
            dtype="float32", fill_value=np.nan,
        )
    return g


def band_checksum(g, band="VV_dB", t=0) -> float:
    """Stable scalar checksum of a written band timestep (NaN-safe)."""
    arr = g[band][t]
    return float(np.nansum(arr)) + float(np.isnan(arr).sum())
