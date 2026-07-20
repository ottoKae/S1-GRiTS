"""Catalog linkage + materialized training cube.

Covers the two deliverables that turn the static↔scenes pairing into a DL-ready
data-cube design:

  * catalog linkage — static and its scenes/smonthly cube share a TRACK-level
    ``geometry_group_id`` (the burst count ``_N{nn}`` stays in item_id/n_bursts
    as provenance), and the STAC item marks static ``s1grits:role='auxiliary'``;
  * ``CubeResolver.materialize_training_cube`` — writes an analysis-ready store
    with dynamic ``(time,y,x)`` bands and a ``static/`` subgroup of ``(y,x)``
    layers, co-registered onto the dynamic grid, and ``open_training_cube``
    reads it back merged.
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
xr = pytest.importorskip("xarray")

from rasterio.transform import Affine  # noqa: E402

from s1grits.asf_io import _mgrs_to_utm_epsg  # noqa: E402
from s1grits.asf_output_writing import _build_grid_from_mgrs_tile  # noqa: E402
from s1grits.workflow_static import _init_zarr_static  # noqa: E402
from s1grits.analysis.catalog import resync_catalog_from_filesystem  # noqa: E402
from s1grits.resolver import CubeResolver  # noqa: E402
from s1grits.stac_builder import build_stac_item_dict  # noqa: E402

TILE = "17MPU"
RES = 30.0
DIRN = "DESCENDING"
TK = "40"
SW = SH = 24
OFF = 8
DW = SH + 2 * OFF
DH = SW + 2 * OFF
NT = 3


def _tile_tfm():
    crs = _mgrs_to_utm_epsg(TILE)
    tfm, _, _, _, _ = _build_grid_from_mgrs_tile(TILE, crs, RES)
    return crs, tfm


def _write_static(out_root: Path):
    crs, tfm = _tile_tfm()
    xs = (tfm.c + (np.arange(SW) + 0.5) * RES).astype("float64")
    ys = (tfm.f - (np.arange(SH) + 0.5) * RES).astype("float64")
    zpath = out_root / TILE / f"static_{DIRN}" / "zarr" / \
        f"s1grits_static_{TILE}_{DIRN}_TK{TK}_N05.zarr"
    g = _init_zarr_static(zpath, xs, ys, crs, tfm, 32, 32,
                          band_names=["local_inc_angle", "ls_map"])
    g["local_inc_angle"][:] = np.full((SH, SW), 42.0, dtype=np.float32)
    g["ls_map"][:] = np.zeros((SH, SW), dtype=np.float32)


def _write_scenes(out_root: Path):
    crs, tile_tfm = _tile_tfm()
    tfm = Affine(RES, 0.0, tile_tfm.c - OFF * RES, 0.0, -RES, tile_tfm.f + OFF * RES)
    xs = (tfm.c + (np.arange(DW) + 0.5) * RES).astype("float64")
    ys = (tfm.f - (np.arange(DH) + 0.5) * RES).astype("float64")
    zdir = out_root / TILE / f"scenes_{DIRN}_ARDC" / "zarr"
    zdir.mkdir(parents=True, exist_ok=True)
    zpath = zdir / f"s1grits_scenes_{TILE}_{DIRN}_TK{TK}.zarr"
    g = zarr.open_group(str(zpath), mode="w", zarr_format=3)
    g.attrs["crs"] = crs
    g.attrs["transform"] = list(tfm)[:6]
    g.attrs["processing_level"] = "ARDC"
    for nm, data, dn in (
        ("x", xs, ["x"]), ("y", ys, ["y"]),
        ("time", np.arange(NT, dtype="int64") * 86_400 * 1_000_000_000, ["time"]),
    ):
        _a = g.create_array(nm, data=data, overwrite=True, dimension_names=dn)
        _a.attrs["_ARRAY_DIMENSIONS"] = dn
    for b in ("VV_dB", "VH_dB"):
        _a = g.create_array(b, shape=(NT, DH, DW), chunks=(1, DH, DW),
                            dtype="float32", fill_value=np.nan, overwrite=True,
                            dimension_names=["time", "y", "x"])
        _a.attrs["_ARRAY_DIMENSIONS"] = ["time", "y", "x"]
        _a[:] = np.full((NT, DH, DW), -12.0, dtype=np.float32)


@pytest.fixture()
def cube(tmp_path):
    out_root = tmp_path / "cube"
    out_root.mkdir()
    _write_static(out_root)
    _write_scenes(out_root)
    resync_catalog_from_filesystem(out_root, write_stac=False)
    return out_root


# ---------------------------------------------------------------------------
# Catalog linkage
# ---------------------------------------------------------------------------

def test_static_and_scenes_share_track_level_geometry_group_id(cube):
    import pandas as pd
    cat = pd.read_parquet(cube / "catalog.parquet")
    stat = cat[cat["product_type"] == "static"].iloc[0]
    scn = cat[cat["product_type"] == "scenes"].iloc[0]
    expected = f"{TILE}_{DIRN}_TK{TK}"
    assert stat["geometry_group_id"] == expected
    assert scn["geometry_group_id"] == expected
    # join key matches; burst count stays in item_id/n_bursts as provenance.
    assert stat["geometry_group_id"] == scn["geometry_group_id"]
    assert "_N05" in stat["item_id"] and stat["item_id"].endswith("_static")
    assert stat["n_bursts"] == 5


def test_static_stac_item_marks_auxiliary_role(cube, tmp_path):
    import pandas as pd
    cat = pd.read_parquet(cube / "catalog.parquet")
    stat = cat[cat["product_type"] == "static"].iloc[0].to_dict()
    item, _ = build_stac_item_dict(stat, output_root=str(cube))
    props = item["properties"]
    assert props.get("s1grits:role") == "auxiliary"
    assert props.get("s1grits:geometry_group_id") == f"{TILE}_{DIRN}_TK{TK}"

    scn = cat[cat["product_type"] == "scenes"].iloc[0].to_dict()
    item_s, _ = build_stac_item_dict(scn, output_root=str(cube))
    # non-static products are not tagged auxiliary
    assert "s1grits:role" not in item_s["properties"]


# ---------------------------------------------------------------------------
# Materialized training cube
# ---------------------------------------------------------------------------

def test_materialize_training_cube_structure(cube, tmp_path):
    r = CubeResolver(cube)
    out = tmp_path / "train.zarr"
    path = r.materialize_training_cube(TILE, out, dynamic_product_type="scenes")
    assert Path(path).exists()

    # Root group: dynamic (time,y,x) bands; static/ subgroup: (y,x) layers.
    root = xr.open_zarr(str(path), consolidated=False)
    assert set(root.data_vars) >= {"VV_dB", "VH_dB"}
    assert root["VV_dB"].dims == ("time", "y", "x")
    static = xr.open_zarr(str(path), group="static", consolidated=False)
    assert set(static.data_vars) >= {"local_inc_angle", "ls_map"}
    assert static["local_inc_angle"].dims == ("y", "x")   # NO time axis
    assert "time" not in static.dims

    g = zarr.open_group(str(path), mode="r")
    assert g.attrs.get("s1grits:training_cube") is True
    assert g.attrs.get("s1grits:geometry_group_id") == f"{TILE}_{DIRN}_TK{TK}"
    assert set(g.attrs.get("s1grits:static_bands")) >= {"local_inc_angle", "ls_map"}


def test_open_training_cube_merges_and_registers(cube, tmp_path):
    r = CubeResolver(cube)
    out = tmp_path / "train.zarr"
    r.materialize_training_cube(TILE, out, dynamic_product_type="scenes")

    ds = CubeResolver.open_training_cube(out)
    # Dynamic + static in one Dataset, pixel-registered on the dynamic grid.
    for v in ("VV_dB", "VH_dB", "local_inc_angle", "ls_map"):
        assert v in ds.data_vars
    assert ds["VV_dB"].dims == ("time", "y", "x")
    assert ds["local_inc_angle"].dims == ("y", "x")   # broadcasts on use
    assert ds.sizes["y"] == DH and ds.sizes["x"] == DW

    lia = ds["local_inc_angle"].values
    inner = lia[OFF:OFF + SH, OFF:OFF + SW]
    assert np.allclose(inner, 42.0)                    # exact pickup in the tile
    margin = lia.copy()
    margin[OFF:OFF + SH, OFF:OFF + SW] = np.nan
    assert np.isnan(margin).all()                      # NaN in beyond-tile margin


def test_materialize_missing_pairing_raises(cube, tmp_path):
    # A tile/direction with no matching product should error clearly (here the
    # ASCENDING side has neither a dynamic nor a static product).
    r = CubeResolver(cube)
    with pytest.raises(ValueError, match="No '?(scenes|static)"):
        r.materialize_training_cube(
            TILE, tmp_path / "x.zarr", dynamic_product_type="scenes",
            direction="ASCENDING",
        )
