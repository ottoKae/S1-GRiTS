"""Master-grid era-independence guard: union with MGRS tile bounds.

A fresh full-archive run derives its master grid from batch 1 (the earliest
era). If that era's data-take framing misses bursts at a tile edge, the grid
would silently crop every later era's data inside the tile. The guard grows a
freshly derived burst-union grid to at least the MGRS tile bounds:

* covering grids are returned unchanged (identity for the common case);
* undersized grids are expanded to cover both the burst union AND the tile;
* the expanded grid stays on the same target_res pixel lattice;
* unresolvable tile geometry degrades gracefully to the input grid.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import Affine

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

pytest.importorskip("rasterio")

from s1grits import workflow_scenes as ws  # noqa: E402

RES = 30.0
CRS = "EPSG:32717"


def _grid(minx, maxy, width, height):
    transform = Affine(RES, 0.0, minx, 0.0, -RES, maxy)
    x = (minx + (np.arange(width) + 0.5) * RES).astype("float64")
    y = (maxy - (np.arange(height) + 0.5) * RES).astype("float64")
    return transform, width, height, x, y


def _patch_tile_wkt(monkeypatch, minx, miny, maxx, maxy):
    """Fake the MGRS tile geometry as a UTM box expressed in EPSG:4326."""
    from pyproj import Transformer
    to_ll = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True).transform
    lons, lats = zip(*[
        to_ll(x, y)
        for x, y in [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]
    ])
    wkt = (
        "POLYGON(("
        + ",".join(f"{lon} {lat}" for lon, lat in
                   list(zip(lons, lats)) + [(lons[0], lats[0])])
        + "))"
    )
    monkeypatch.setattr(ws, "_get_mgrs_tile_geometry_wkt", lambda _tile: wkt)


def test_covering_grid_is_returned_unchanged(monkeypatch):
    # Grid 600x600 px with lattice-aligned origin (multiples of RES, as
    # _build_grid_from_bursts produces); tile box strictly inside.
    grid = _grid(499980.0, 8499990.0, 600, 600)
    _patch_tile_wkt(
        monkeypatch, 503010.0, 8484990.0, 515010.0, 8496990.0
    )
    out = ws._expand_grid_to_tile_bounds(*grid, "17MPU", CRS, RES)
    assert out[1] == 600 and out[2] == 600
    assert out[0] == grid[0]
    np.testing.assert_array_equal(out[3], grid[3])
    np.testing.assert_array_equal(out[4], grid[4])


def test_undersized_grid_is_expanded_to_cover_tile(monkeypatch):
    # Grid covers x [499980, 511980], y [8487990, 8499990]; the tile extends
    # ~3 km further east and south, as if batch 1 missed an edge burst.
    grid = _grid(499980.0, 8499990.0, 400, 400)
    _patch_tile_wkt(
        monkeypatch, 506010.0, 8484990.0, 515010.0, 8493990.0
    )
    transform, width, height, x, y = ws._expand_grid_to_tile_bounds(
        *grid, "17MPU", CRS, RES
    )
    minx, maxy = transform.c, transform.f
    maxx = minx + width * RES
    miny = maxy - height * RES
    # Covers the original burst union...
    assert minx <= 499980.0 and maxy >= 8499990.0
    assert maxx >= 511980.0 and miny <= 8487990.0
    # ...AND the tile (within one snapped pixel of the round-tripped bounds).
    assert minx <= 506010.0 and miny <= 8484990.0 + RES
    assert maxx >= 515010.0 - RES and maxy >= 8493990.0
    # Same pixel lattice: origin stays on the RES grid, coords consistent.
    assert minx % RES == 0 and maxy % RES == 0
    assert len(x) == width and len(y) == height
    np.testing.assert_allclose(x[0], minx + 0.5 * RES)
    np.testing.assert_allclose(y[0], maxy - 0.5 * RES)
    # Expansion is minimal on the sides that already covered the tile.
    assert minx == 499980.0 and maxy == 8499990.0


def test_unresolvable_tile_geometry_keeps_grid(monkeypatch):
    grid = _grid(499980.0, 8499990.0, 100, 100)

    def _boom(_tile):
        raise RuntimeError("no LUT for tile")

    monkeypatch.setattr(ws, "_get_mgrs_tile_geometry_wkt", _boom)
    out = ws._expand_grid_to_tile_bounds(*grid, "17MPU", CRS, RES)
    assert out[0] == grid[0] and out[1] == 100 and out[2] == 100
