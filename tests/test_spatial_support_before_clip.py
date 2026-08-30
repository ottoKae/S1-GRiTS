"""Support-before-clip invariant for spatial-window operations.

S1-GRiTS composites the multi-burst mosaic on the *burst-union* grid
(``_build_grid_from_bursts``), which is strictly larger than the target MGRS
tile, and applies the MGRS tile clip only *after* all spatial-window operations
(GLCM texture, despeckle). This guarantees the neighbourhood window of a pixel
on the tile boundary is filled with real beyond-tile observations rather than a
replicated/NoData edge, so the product has no artificial tile-boundary
artifacts.

Two independent levels of spatial support cooperate:

* **burst-union support grid** — removes *tile*-boundary artifacts, because the
  support region extends beyond the tile before the clip; and
* **blockwise halo** (``GLCM_BLOCK_HALO``) — removes *block*-boundary artifacts,
  because each Zarr-chunk block is composited on a halo-expanded window before
  GLCM, then cropped back to the block.

These tests lock in that design so a future refactor cannot silently regress it
(e.g. by switching the scenes/smonthly master grid to the tile-bounds builder,
or by clipping before the spatial filter).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import Affine

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

pytest.importorskip("s1grits.asf_array_processing")

from s1grits import workflow_scenes as ws  # noqa: E402
from s1grits.asf_array_processing import compute_glcm_texture_bands  # noqa: E402
from s1grits.asf_output_writing import (  # noqa: E402
    _build_grid_from_bursts,
    _build_grid_from_mgrs_tile,
)

COPOL, CROSSPOL = "VV_dB", "VH_dB"
TILE = "17MPU"
CRS = "EPSG:32717"
RES = 30.0

# Support radius of the fixed smonthly GLCM config: window_size//2 + distance.
_CFG = ws._smonthly_texture_cfg(COPOL, CROSSPOL)
SUPPORT_RADIUS = _CFG["window_size"] // 2 + _CFG["distance"]  # = 3


# ---------------------------------------------------------------------------
# 1. The scenes/smonthly workflow builds its master grid from the burst union,
#    NOT from the tile bounds. This is what makes the support region larger than
#    the tile. Guard both the code path (source) and the geometric property.
# ---------------------------------------------------------------------------
def test_scenes_master_grid_uses_burst_union_not_tile_bounds():
    from s1grits.scenes import pipeline as ws_pipeline
    src = Path(ws_pipeline.__file__).read_text(encoding="utf-8")
    # Strip comment bodies so a comment that merely *names* the tile-bounds
    # builder (e.g. a "do NOT switch to ..." warning) does not trip the guard;
    # we only care about real call sites.
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    # The master-grid build site must call the burst-union builder ...
    assert "_build_grid_from_bursts(" in code, (
        "scenes/smonthly master grid must be built from the burst union"
    )
    # ... and must NOT call the tile-bounds builder (that would strip the
    # beyond-tile support and reintroduce tile-boundary artifacts).
    assert "_build_grid_from_mgrs_tile(" not in code, (
        "scenes/smonthly must not use the tile-bounds grid builder"
    )


def test_burst_union_grid_strictly_contains_the_tile_grid():
    # Tile grid extent (the final clip target).
    t_tr, t_w, t_h, _, _ = _build_grid_from_mgrs_tile(TILE, CRS, RES)
    t_minx, t_maxy = t_tr.c, t_tr.f
    t_maxx = t_minx + t_w * RES
    t_miny = t_maxy - t_h * RES

    # A burst footprint that overhangs the tile by 3 km on every side (bursts
    # routinely extend well past a single MGRS tile). Union grid must cover it.
    margin = 3000.0
    b_minx, b_maxy = t_minx - margin, t_maxy + margin
    b_w = int(round((t_maxx + margin - b_minx) / RES))
    b_h = int(round((b_maxy - (t_miny - margin)) / RES))
    prof = {
        "height": b_h, "width": b_w,
        "transform": Affine(RES, 0.0, b_minx, 0.0, -RES, b_maxy),
        "crs": CRS,
    }
    u_tr, u_w, u_h, _, _ = _build_grid_from_bursts([prof], CRS, RES)
    u_minx, u_maxy = u_tr.c, u_tr.f
    u_maxx = u_minx + u_w * RES
    u_miny = u_maxy - u_h * RES

    # Strictly larger on all four sides -> the tile boundary is interior to the
    # support grid, so its GLCM/despeckle window has real neighbours.
    assert u_minx < t_minx and u_miny < t_miny
    assert u_maxx > t_maxx and u_maxy > t_maxy


# ---------------------------------------------------------------------------
# 2 + 3. GLCM computed on the larger support grid and then cropped to the tile
#        uses beyond-tile pixels near the boundary; computing GLCM AFTER a
#        pre-crop to the tile gives different boundary values. This proves the
#        beyond-tile support actually contributes (i.e. clip-last matters).
# ---------------------------------------------------------------------------
def _synthetic_db(h, w, seed):
    rng = np.random.default_rng(seed)
    vv = (rng.random((h, w)) * 30 - 25).astype(np.float32)   # ~[-25, 5] dB
    vh = (rng.random((h, w)) * 27 - 32).astype(np.float32)   # ~[-32, -5] dB
    return vv, vh


def test_beyond_tile_support_changes_boundary_but_not_interior():
    # Full support grid (64x64); the "tile" is the interior 32x32 core [16:48].
    full_vv, full_vh = _synthetic_db(64, 64, seed=11)
    c0, c1 = 16, 48

    full_bands, names = compute_glcm_texture_bands(full_vv, full_vh, _CFG)
    # GLCM-on-support then clip to tile (production order: filter, then clip).
    clip_last = {n: a[c0:c1, c0:c1] for n, a in zip(names, full_bands)}

    # GLCM computed AFTER pre-cropping to the tile (the WRONG order we guard
    # against): the tile edge now sees a replicated/short neighbourhood.
    pre_vv, pre_vh = full_vv[c0:c1, c0:c1], full_vh[c0:c1, c0:c1]
    pre_bands, pre_names = compute_glcm_texture_bands(pre_vv, pre_vh, _CFG)
    crop_first = dict(zip(pre_names, pre_bands))

    r = SUPPORT_RADIUS
    boundary_differs = False
    for name in names:
        a = clip_last[name]
        b = crop_first[name]
        # (2/3) The first row within the support radius of the tile edge must
        # differ -> beyond-tile pixels genuinely changed the boundary texture.
        top_diff = np.nanmax(np.abs(a[0, :] - b[0, :]))
        if np.isfinite(top_diff) and top_diff > 1e-3:
            boundary_differs = True
        # Deep interior (>= support radius from every core edge) is identical:
        # there the whole window lies inside the tile, so order cannot matter.
        ai = a[r:-r, r:-r]
        bi = b[r:-r, r:-r]
        assert np.array_equal(
            np.nan_to_num(ai, nan=-9e9), np.nan_to_num(bi, nan=-9e9)
        ), f"interior GLCM must be clip-order-independent for band {name}"
    assert boundary_differs, (
        "beyond-tile support did not change any tile-boundary texture value; "
        "the clip-last invariant would then be unobservable"
    )


# ---------------------------------------------------------------------------
# 4. The blockwise halo path reproduces the full-mosaic GLCM bit-for-bit, using
#    the production halo constant and texture config.
# ---------------------------------------------------------------------------
def test_blockwise_halo_matches_full_mosaic_reference():
    vv, vh = _synthetic_db(96, 96, seed=7)
    full_bands, names = compute_glcm_texture_bands(vv, vh, _CFG)

    halo = ws.GLCM_BLOCK_HALO
    assert halo >= SUPPORT_RADIUS, "halo must cover the GLCM support radius"
    h, w = vv.shape
    block = 32
    out = [np.full((h, w), np.nan, np.float32) for _ in names]
    for y0 in range(0, h, block):
        for x0 in range(0, w, block):
            y1, x1 = min(h, y0 + block), min(w, x0 + block)
            hy0, hx0 = max(0, y0 - halo), max(0, x0 - halo)
            hy1, hx1 = min(h, y1 + halo), min(w, x1 + halo)
            sub, _ = compute_glcm_texture_bands(
                vv[hy0:hy1, hx0:hx1], vh[hy0:hy1, hx0:hx1], _CFG
            )
            oy, ox = y0 - hy0, x0 - hx0
            for bi in range(len(names)):
                out[bi][y0:y1, x0:x1] = sub[bi][oy:oy + (y1 - y0), ox:ox + (x1 - x0)]

    for bi, name in enumerate(names):
        assert np.array_equal(
            np.nan_to_num(out[bi], nan=-9e9), np.nan_to_num(full_bands[bi], nan=-9e9)
        ), f"blockwise halo GLCM not bit-exact to full mosaic for band {name}"
