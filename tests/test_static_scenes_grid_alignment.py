"""Static ↔ scenes grid-alignment invariant.

The static workflow builds its output grid straight from the MGRS tile bounds
(``_build_grid_from_mgrs_tile``), while the scenes workflow builds its master
grid from the burst-footprint UNION and then grows it to at least the tile
bounds (``_build_grid_from_geoms`` → ``_expand_grid_to_tile_bounds``). The two
grids therefore have DIFFERENT extents (and hence different ``grid_id``), but
they MUST remain the same pixel lattice: same CRS, same resolution, and the
static tile grid must be an integer-pixel-aligned SUB-WINDOW of the scenes grid
(so a static layer overlays a scenes cube by an exact crop/pad — never a
resample).

This is the load-bearing premise of the static/scenes pairing design (plan A:
pair on ``tile_id + track`` and window the co-registered static grid onto the
dynamic grid at load time). Lock it so a future refactor of either grid builder
cannot silently break co-registration.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import shapely.geometry as sg
from shapely import wkt as shapely_wkt

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

pytest.importorskip("pyproj")
pytest.importorskip("rasterio")

from s1grits.asf_io import _mgrs_to_utm_epsg  # noqa: E402
from s1grits.asf_output_writing import (  # noqa: E402
    _build_grid_from_geoms,
    _build_grid_from_mgrs_tile,
    _get_mgrs_tile_geometry_wkt,
)
from s1grits.scenes.store import _expand_grid_to_tile_bounds  # noqa: E402

TILE = "17MPU"
RES = 30.0


def _static_grid(tile=TILE, res=RES):
    crs = _mgrs_to_utm_epsg(tile)
    tfm, w, h, xs, ys = _build_grid_from_mgrs_tile(tile, crs, res)
    return crs, tfm, w, h, xs, ys


def _scenes_grid(tile=TILE, res=RES, pad_deg=0.2):
    """A scenes-like master grid: burst-union (mimicked by a geom larger than
    the tile) grown to tile bounds — always strictly larger than the tile."""
    crs = _mgrs_to_utm_epsg(tile)
    tile_ll = shapely_wkt.loads(_get_mgrs_tile_geometry_wkt(tile))
    minx, miny, maxx, maxy = tile_ll.bounds
    big = sg.box(minx - pad_deg, miny - pad_deg, maxx + pad_deg, maxy + pad_deg)
    tfm, w, h, xs, ys = _build_grid_from_geoms([big], crs, res)
    return (crs, *_expand_grid_to_tile_bounds(tfm, w, h, xs, ys, tile, crs, res))


def test_same_crs_and_resolution():
    s_crs, s_tfm, *_ = _static_grid()
    d_crs, d_tfm, *_ = _scenes_grid()
    assert s_crs == d_crs
    assert s_tfm.a == d_tfm.a == RES          # +res on x
    assert s_tfm.e == d_tfm.e == -RES         # -res on y


def test_static_is_strict_subwindow_of_scenes():
    _, s_tfm, s_w, s_h, *_ = _static_grid()
    _, d_tfm, d_w, d_h, *_ = _scenes_grid()
    # Scenes grid is larger in both dimensions (burst overhang + tile grow).
    assert d_w > s_w and d_h > s_h
    # Static origin sits on the scenes lattice at an INTEGER pixel offset.
    col_off = (s_tfm.c - d_tfm.c) / RES
    row_off = (d_tfm.f - s_tfm.f) / RES
    assert col_off == pytest.approx(round(col_off), abs=1e-6)
    assert row_off == pytest.approx(round(row_off), abs=1e-6)
    col_off, row_off = int(round(col_off)), int(round(row_off))
    assert col_off >= 0 and row_off >= 0
    # The static window fits entirely inside the scenes grid.
    assert col_off + s_w <= d_w
    assert row_off + s_h <= d_h


def test_overlap_coordinates_are_bit_identical():
    """Where the two grids overlap (the whole static extent), the pixel-centre
    coordinates must match exactly — the guarantee that lets a static layer be
    cropped onto a scenes cube with no interpolation."""
    _, s_tfm, s_w, s_h, s_xs, s_ys = _static_grid()
    _, d_tfm, d_w, d_h, d_xs, d_ys = _scenes_grid()
    col_off = int(round((s_tfm.c - d_tfm.c) / RES))
    row_off = int(round((d_tfm.f - s_tfm.f) / RES))
    np.testing.assert_allclose(d_xs[col_off:col_off + s_w], s_xs, atol=1e-6)
    np.testing.assert_allclose(d_ys[row_off:row_off + s_h], s_ys, atol=1e-6)


def test_expand_never_shrinks_below_tile():
    """A scenes grid whose union already exceeds the tile is not shrunk by the
    tile-bounds expansion (union ⊇ tile always holds)."""
    _, s_tfm, s_w, s_h, *_ = _static_grid()
    _, d_tfm, d_w, d_h, *_ = _scenes_grid()
    # Scenes fully covers the static (tile) extent.
    s_maxx = s_tfm.c + s_w * RES
    s_miny = s_tfm.f - s_h * RES
    d_maxx = d_tfm.c + d_w * RES
    d_miny = d_tfm.f - d_h * RES
    assert d_tfm.c <= s_tfm.c and d_tfm.f >= s_tfm.f      # top-left ≤ static
    assert d_maxx >= s_maxx and d_miny <= s_miny           # bottom-right ≥ static
