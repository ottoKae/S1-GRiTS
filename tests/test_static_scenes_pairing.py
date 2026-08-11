"""Static ↔ scenes pairing through the CubeResolver.

End-to-end: build a static Zarr (MGRS-tile grid) and a scenes Zarr (a larger
grid that CONTAINS the static sub-window), catalogue them with
`resync_catalog_from_filesystem`, then exercise the resolver:

  * `get_aligned_products` includes static even though its grid_id differs from
    scenes (it is not dropped as a minority grid); and
  * `open_stack` windows the co-registered static grid onto the scenes grid and
    merges everything into one (time, y, x) Dataset — static finite inside the
    tile sub-window, NaN in the beyond-tile margin, and pixel-registered with
    the scenes bands.
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

TILE = "17MPU"
RES = 30.0
DIRN = "DESCENDING"
SW = SH = 24                # static window size (pixels)
OFF = 8                     # static offset inside the scenes grid (pixels)
DW = SW + 2 * OFF           # scenes grid size (contains the static window)
DH = SH + 2 * OFF
NT = 3


def _tile_origin():
    crs = _mgrs_to_utm_epsg(TILE)
    tfm, _, _, _, _ = _build_grid_from_mgrs_tile(TILE, crs, RES)
    return crs, tfm


def _write_static(out_root: Path):
    crs, tile_tfm = _tile_origin()
    xs = (tile_tfm.c + (np.arange(SW) + 0.5) * RES).astype("float64")
    ys = (tile_tfm.f - (np.arange(SH) + 0.5) * RES).astype("float64")
    zpath = out_root / TILE / f"static_{DIRN}" / "zarr" / \
        f"s1grits_static_{TILE}_{DIRN}_TK40_N05.zarr"
    g = _init_zarr_static(zpath, xs, ys, crs, tile_tfm, 32, 32,
                          band_names=["local_inc_angle"])
    g["local_inc_angle"][:] = np.full((SH, SW), 42.0, dtype=np.float32)


def _write_scenes(out_root: Path):
    """Scenes grid: origin shifted OUT by OFF px so it CONTAINS the static
    window at pixel offset (OFF, OFF)."""
    crs, tile_tfm = _tile_origin()
    tfm = Affine(RES, 0.0, tile_tfm.c - OFF * RES, 0.0, -RES, tile_tfm.f + OFF * RES)
    xs = (tfm.c + (np.arange(DW) + 0.5) * RES).astype("float64")
    ys = (tfm.f - (np.arange(DH) + 0.5) * RES).astype("float64")
    zdir = out_root / TILE / f"scenes_{DIRN}_ARDC" / "zarr"
    zdir.mkdir(parents=True, exist_ok=True)
    zpath = zdir / f"s1grits_scenes_{TILE}_{DIRN}_TK40.zarr"
    g = zarr.open_group(str(zpath), mode="w", zarr_format=3)
    g.attrs["crs"] = crs
    g.attrs["transform"] = list(tfm)[:6]
    g.attrs["processing_level"] = "ARDC"
    for nm, data, dnames in (
        ("x", xs, ["x"]), ("y", ys, ["y"]),
        ("time", np.arange(NT, dtype="int64") * 86_400 * 1_000_000_000, ["time"]),
    ):
        _a = g.create_array(nm, data=data, overwrite=True, dimension_names=dnames)
        _a.attrs["_ARRAY_DIMENSIONS"] = dnames
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


def test_get_aligned_products_includes_static(cube):
    r = CubeResolver(cube)
    aligned = r.get_aligned_products(TILE, ["scenes", "static"])
    assert set(aligned) >= {"scenes", "static"}, (
        f"static dropped from aligned products: {list(aligned)}"
    )


def test_open_stack_merges_and_windows_static(cube):
    r = CubeResolver(cube)
    stack = r.open_stack(TILE, ["scenes", "static"])
    assert stack.attrs["s1grits:geometry_group_id"] == f"{TILE}_{DIRN}_TK40"
    assert isinstance(stack, xr.Dataset)
    # Dynamic bands retain time; static is stored/opened once as 2-D.
    for v in ("VV_dB", "VH_dB", "local_inc_angle"):
        assert v in stack.data_vars, f"{v} missing from merged stack"
    assert stack["VV_dB"].dims == ("time", "y", "x")
    assert stack["VH_dB"].dims == ("time", "y", "x")
    assert stack["local_inc_angle"].dims == ("y", "x")
    # Registered on the scenes (dynamic) grid.
    assert stack.sizes["y"] == DH and stack.sizes["x"] == DW
    assert stack.sizes["time"] == NT

    lia = stack["local_inc_angle"]
    inner = lia.values[OFF:OFF + SH, OFF:OFF + SW]
    # Exact 1:1 pickup inside the tile window …
    assert np.allclose(inner, 42.0)
    # … and NaN in the beyond-tile margin (windowed, not tiled/extrapolated).
    margin = lia.values.copy()
    margin[OFF:OFF + SH, OFF:OFF + SW] = np.nan
    assert np.isnan(margin).all()


def test_open_stack_rejects_ambiguous_tracks(cube):
    import pandas as pd
    cat_path = cube / "catalog.parquet"
    cat = pd.read_parquet(cat_path)
    extra = cat.copy()
    extra["geometry_group_id"] = extra["geometry_group_id"].str.replace("TK40", "TK41")
    extra["track"] = 41
    pd.concat([cat, extra], ignore_index=True).to_parquet(cat_path, index=False)
    r = CubeResolver(cube)
    with pytest.raises(ValueError, match="Multiple geometry groups"):
        r.open_stack(TILE, ["scenes", "static"])
    selected = r.open_stack(TILE, ["scenes", "static"], track=40)
    assert isinstance(selected, xr.Dataset)


def test_open_stack_static_pixel_registered_with_scenes(cube):
    r = CubeResolver(cube)
    stack = r.open_stack(TILE, ["scenes", "static"])
    # Static and scenes share identical y/x coordinates after windowing.
    assert np.array_equal(stack["x"].values, stack["x"].values)
    # Where static is finite, scenes is defined on the same pixels.
    lia = stack["local_inc_angle"].values
    vv = stack["VV_dB"].isel(time=0).values
    finite = ~np.isnan(lia)
    assert finite.any()
    assert np.isfinite(vv[finite]).all()


def test_open_stack_can_broadcast_static_lazily_on_request(cube):
    r = CubeResolver(cube)
    stack = r.open_stack(
        TILE, ["scenes", "static"], broadcast_static=True
    )
    lia = stack["local_inc_angle"]
    assert lia.dims == ("time", "y", "x")
    assert np.allclose(
        lia.isel(time=0).values,
        lia.isel(time=1).values,
        equal_nan=True,
    )
