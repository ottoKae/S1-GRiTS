"""Static products are discovered by `catalog resync` via their Zarr store.

`resync_catalog_from_filesystem` is the authoritative catalog/STAC builder: it
discovers products by scanning Zarr stores under {tile}/{product}_{DIR}/zarr/
and rebuilds catalog.parquet from what it finds. A static run must therefore
write a Zarr store to appear in the production catalog (the reason the static
workflow now always writes one). These tests lock that:

  * a static Zarr store is discovered and catalogued as product_type='static'
    with a canonical (SHA-256) grid_id, alongside a scenes store on the same
    tile; and
  * the static and scenes grid_ids DIFFER (static = tile grid, scenes =
    burst-union grid) — the pairing challenge the resolver must bridge.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

pytest.importorskip("pyproj")
zarr = pytest.importorskip("zarr")
pd = pytest.importorskip("pandas")

from rasterio.transform import Affine  # noqa: E402

from s1grits.asf_io import _mgrs_to_utm_epsg  # noqa: E402
from s1grits.asf_output_writing import (  # noqa: E402
    _build_grid_from_mgrs_tile,
)
from s1grits.workflow_static import _init_zarr_static  # noqa: E402
from s1grits.analysis.catalog import resync_catalog_from_filesystem  # noqa: E402

TILE = "17MPU"
RES = 30.0
DIRN = "DESCENDING"


def _coords(tfm, w, h):
    xs = (tfm.c + (np.arange(w) + 0.5) * RES).astype("float64")
    ys = (tfm.f - (np.arange(h) + 0.5) * RES).astype("float64")
    return xs, ys


def _write_static_store(out_root: Path) -> Path:
    """A static Zarr on the MGRS tile grid, laid out as the workflow writes it:
    {tile}/static_{DIR}/zarr/s1grits_static_{tile}_{DIR}_TK40_N05.zarr"""
    crs = _mgrs_to_utm_epsg(TILE)
    tfm, w, h, xs, ys = _build_grid_from_mgrs_tile(TILE, crs, RES)
    # Keep the test store tiny: crop to a 32x32 corner (same lattice/origin).
    w, h = 32, 32
    xs, ys = xs[:w], ys[:h]
    zdir = out_root / TILE / f"static_{DIRN}" / "zarr"
    zpath = zdir / f"s1grits_static_{TILE}_{DIRN}_TK40_N05.zarr"
    g = _init_zarr_static(zpath, xs, ys, crs, tfm, 32, 32, band_names=["local_inc_angle"])
    g["local_inc_angle"][:] = np.full((h, w), 30.0, dtype=np.float32)
    return zpath


def _write_scenes_store(out_root: Path) -> Path:
    """A minimal scenes Zarr on a LARGER (burst-union-like) grid — different
    origin/size from the tile grid, matching what the scenes writer produces."""
    crs = _mgrs_to_utm_epsg(TILE)
    tile_tfm, _, _, _, _ = _build_grid_from_mgrs_tile(TILE, crs, RES)
    # Shift origin out and up by whole pixels → a distinct, larger grid.
    tfm = Affine(RES, 0.0, tile_tfm.c - 100 * RES, 0.0, -RES, tile_tfm.f + 100 * RES)
    w, h, nt = 48, 48, 3
    xs, ys = _coords(tfm, w, h)
    zdir = out_root / TILE / f"scenes_{DIRN}_ARDC" / "zarr"
    zdir.mkdir(parents=True, exist_ok=True)
    zpath = zdir / f"s1grits_scenes_{TILE}_{DIRN}_TK40.zarr"
    g = zarr.open_group(str(zpath), mode="w", zarr_format=3)
    g.attrs["crs"] = crs
    g.attrs["transform"] = list(tfm)[:6]
    g.attrs["processing_level"] = "ARDC"
    _a = g.create_array("x", data=xs, overwrite=True, dimension_names=["x"])
    _a.attrs["_ARRAY_DIMENSIONS"] = ["x"]
    _a = g.create_array("y", data=ys, overwrite=True, dimension_names=["y"])
    _a.attrs["_ARRAY_DIMENSIONS"] = ["y"]
    _time_ns = (np.arange(nt, dtype="int64") * 86_400 * 1_000_000_000)  # daily, ns
    _t = g.create_array("time", data=_time_ns,
                        overwrite=True, dimension_names=["time"])
    _t.attrs["_ARRAY_DIMENSIONS"] = ["time"]
    for b in ("VV_dB", "VH_dB"):
        _a = g.create_array(b, shape=(nt, h, w), chunks=(1, h, w), dtype="float32",
                            fill_value=np.nan, overwrite=True,
                            dimension_names=["time", "y", "x"])
        _a.attrs["_ARRAY_DIMENSIONS"] = ["time", "y", "x"]
        _a[:] = np.full((nt, h, w), -12.0, dtype=np.float32)
    return zpath


def test_static_zarr_is_discovered_by_resync(tmp_path):
    out_root = tmp_path / "cube"
    out_root.mkdir()
    _write_static_store(out_root)
    _write_scenes_store(out_root)

    result = resync_catalog_from_filesystem(out_root, write_stac=False)
    assert result["success"], result

    cat = pd.read_parquet(out_root / "catalog.parquet")
    pts = set(cat["product_type"])
    assert "static" in pts, f"static not discovered; got {pts}"
    assert "scenes" in pts

    stat = cat[cat["product_type"] == "static"].iloc[0]
    assert stat["tile_id"] == TILE
    assert stat["collection_id"] == "s1grits-static"
    assert stat["flight_direction"] == DIRN
    assert stat["bands"] and "local_inc_angle" in stat["bands"]
    # Canonical grid_id: 12-hex SHA-256, not the legacy "{tile}_native_{res}m".
    assert isinstance(stat["grid_id"], str) and len(stat["grid_id"]) == 12
    assert "native" not in stat["grid_id"]
    # Static carries no time axis.
    assert pd.isna(stat["datetime"])


def test_static_and_scenes_grids_differ(tmp_path):
    """Documents the pairing challenge: same tile, DIFFERENT grid_id (the
    resolver must bridge this — plan A windows the co-registered grids)."""
    out_root = tmp_path / "cube"
    out_root.mkdir()
    _write_static_store(out_root)
    _write_scenes_store(out_root)

    resync_catalog_from_filesystem(out_root, write_stac=False)
    cat = pd.read_parquet(out_root / "catalog.parquet")

    g_static = cat[cat["product_type"] == "static"].iloc[0]["grid_id"]
    g_scenes = cat[cat["product_type"] == "scenes"].iloc[0]["grid_id"]
    assert g_static != g_scenes  # different extents → different grid_id
