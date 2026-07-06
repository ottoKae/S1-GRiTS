"""Metadata contract: Zarr attrs + catalog columns + the static⇄time-series
join rule.

Downstream (ML loaders, `catalog resync`, cross-product joins) depends on a
stable metadata surface. These tests freeze it:

* every product Zarr store carries the attrs needed to reconstruct grid,
  CRS, provenance, and product identity without the catalog;
* the canonical catalog schema keeps the columns ML/index consumers key on;
* the static⇄time-series JOIN RULE is (tile_id, track, grid_id):
  ``grid_id`` alone identifies the pixel grid, but a tile has one static
  geometry layer PER TRACK (incidence angle etc. depend on acquisition
  geometry), so matching a time-series store to its geometry layer requires
  the track/geometry-group as well. grid_id then guarantees pixel alignment.
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

zarr = pytest.importorskip("zarr")

from s1grits.canonical_catalog_schema import (  # noqa: E402
    CANONICAL_CATALOG_COLUMNS,
    make_grid_id,
    normalize_catalog_record,
)
from s1grits.workflow_scenes import _init_zarr_2band  # noqa: E402

CRS = "EPSG:32717"
RES = 30.0

# Attrs every product store must carry for standalone interpretation.
REQUIRED_STORE_ATTRS = [
    "crs", "transform", "width", "height", "resolution",
    "grid_id", "grid_name", "grid_version",
    "product_type", "processing_level", "time_varying", "array_dims",
]

# Catalog columns downstream consumers key on (subset of the full schema).
REQUIRED_CATALOG_COLUMNS = [
    "item_id", "collection_id", "product_type", "tile_id",
    "flight_direction", "crs", "transform", "width", "height",
    "grid_id", "resolution_x", "resolution_y",
    "datetime", "month", "geometry_group_id", "track",
    "n_scenes", "jpl_burst_ids", "zarr_path", "bands", "status",
    "processing_signature", "software_version",
]


def _make_store(tmp_path):
    w, h = 24, 16
    minx, maxy = 500000.0, 8500000.0
    transform = Affine(RES, 0.0, minx, 0.0, -RES, maxy)
    x = (minx + (np.arange(w) + 0.5) * RES).astype("float64")
    y = (maxy - (np.arange(h) + 0.5) * RES).astype("float64")
    zp = (tmp_path / "17MPU" / "smonthly_x" / "zarr" / "s.zarr")
    g = _init_zarr_2band(zp, x, y, CRS, transform, 8, 8,
                         processing_level="monthly_ARDC")
    return g, transform, w, h


def test_store_attrs_complete(tmp_path):
    g, transform, w, h = _make_store(tmp_path)
    for attr in REQUIRED_STORE_ATTRS:
        assert attr in g.attrs, f"store attr {attr!r} missing"
    assert g.attrs["crs"] == CRS
    assert list(g.attrs["transform"]) == pytest.approx(list(transform)[:6])
    assert (g.attrs["width"], g.attrs["height"]) == (w, h)
    assert g.attrs["resolution"] == [RES, RES]
    # CF grid mapping present so generic readers resolve the CRS.
    assert any(
        "grid_mapping_name" in g[v].attrs or "crs_wkt" in g[v].attrs
        for v in g.array_keys()
    ), "CF grid_mapping variable missing"
    # Dimension arrays exist with declared dims.
    for coord in ("x", "y", "time"):
        assert coord in g


def test_grid_id_is_deterministic_join_key(tmp_path):
    tfm = [30.0, 0.0, 500000.0, 0.0, -30.0, 8500000.0]
    a = make_grid_id("17MPU", CRS, tfm, 24, 16)
    b = make_grid_id("17MPU", CRS, tfm, 24, 16)
    c = make_grid_id("17MPU", CRS, tfm, 25, 16)  # different grid
    assert a == b and a != c


def test_catalog_schema_keeps_ml_columns():
    missing = [c for c in REQUIRED_CATALOG_COLUMNS
               if c not in CANONICAL_CATALOG_COLUMNS]
    assert missing == [], f"canonical catalog lost ML-required columns: {missing}"


def test_normalize_record_fills_full_schema():
    rec = normalize_catalog_record({
        "tile_id": "17MPU", "product_type": "smonthly",
        "grid_id": "abc", "track": 18, "geometry_group_id":
        "17MPU_ASCENDING_TK18_N3", "month": "2026-01", "n_scenes": 4,
    })
    assert set(rec) == set(CANONICAL_CATALOG_COLUMNS)
    assert rec["status"] == "complete"
    # The static<->time-series join tuple survives normalization intact.
    assert (rec["tile_id"], rec["track"], rec["grid_id"]) == ("17MPU", 18, "abc")
    # n_scenes (per-month observation count) is preserved for temporal masking.
    assert rec["n_scenes"] == 4


def test_join_rule_track_disambiguates_same_grid():
    """Two products on the SAME grid but different tracks must not join:
    static geometry (incidence angle etc.) is per acquisition geometry."""
    tfm = [30.0, 0.0, 500000.0, 0.0, -30.0, 8500000.0]
    gid = make_grid_id("17MPU", CRS, tfm, 24, 16)
    smonthly_tk18 = normalize_catalog_record(
        {"tile_id": "17MPU", "grid_id": gid, "track": 18,
         "product_type": "smonthly"})
    static_tk18 = normalize_catalog_record(
        {"tile_id": "17MPU", "grid_id": gid, "track": 18,
         "product_type": "static"})
    static_tk91 = normalize_catalog_record(
        {"tile_id": "17MPU", "grid_id": gid, "track": 91,
         "product_type": "static"})

    def join_key(r):
        return (r["tile_id"], r["track"], r["grid_id"])

    assert join_key(smonthly_tk18) == join_key(static_tk18)
    assert join_key(smonthly_tk18) != join_key(static_tk91)
