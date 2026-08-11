"""Contract tests for a co-located smonthly + static cube.

The intended production contract is one root/catalog, sibling product
directories, track-level geometry pairing, and 2-D static resolver variables.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from rasterio.transform import Affine

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

zarr = pytest.importorskip("zarr")
xr = pytest.importorskip("xarray")

from s1grits.analysis.catalog import resync_catalog_from_filesystem  # noqa: E402
from s1grits.resolver import CubeResolver  # noqa: E402
from s1grits.workflow_scenes import _append_zarr_timestep, _init_zarr_2band  # noqa: E402
from s1grits.workflow_static import (  # noqa: E402
    _dynamic_grid_for_static_group,
    _filter_lut_to_dynamic_tracks,
    _init_zarr_static,
    _scene_reference_tracks,
)

TILE = "17MNV"
DIRN = "ASCENDING"
TRACK = 18


def _write_smonthly(root: Path) -> Path:
    transform = Affine(30.0, 0.0, 500000.0, 0.0, -30.0, 10000000.0)
    x = transform.c + (np.arange(8) + 0.5) * transform.a
    y = transform.f + (np.arange(6) + 0.5) * transform.e
    path = (
        root / TILE / f"smonthly_{DIRN}" / "zarr"
        / f"s1grits_smonthly_{TILE}_{DIRN}_TK{TRACK}.zarr"
    )
    group = _init_zarr_2band(
        path, x, y, "EPSG:32617", transform, 4, 4,
        processing_level="monthly_ARDC",
        band_names=["VV_dB", "VH_dB", "n_obs"],
    )
    group.attrs["geometry_group_id"] = f"{TILE}_{DIRN}_TK{TRACK}"
    _append_zarr_timestep(
        group,
        np.datetime64("2026-01-15", "ns"),
        [
            ("VV_dB", np.full((6, 8), -12.0, dtype=np.float32)),
            ("VH_dB", np.full((6, 8), -18.0, dtype=np.float32)),
            ("n_obs", np.full((6, 8), 3, dtype=np.uint8)),
        ],
    )
    return path


def _write_static(root: Path, reference: Path) -> Path:
    dynamic = zarr.open_group(str(reference), mode="r")
    transform = Affine(*dynamic.attrs["transform"][:6])
    path = (
        root / TILE / f"static_{DIRN}" / "zarr"
        / f"s1grits_static_{TILE}_{DIRN}_TK{TRACK}_N05.zarr"
    )
    group = _init_zarr_static(
        path,
        np.asarray(dynamic["x"][:]),
        np.asarray(dynamic["y"][:]),
        dynamic.attrs["crs"],
        transform,
        4,
        4,
        band_names=["local_inc_angle", "ls_map"],
    )
    group.attrs["geometry_group_id"] = f"{TILE}_{DIRN}_TK{TRACK}"
    group.attrs["grid_source"] = "workflow_smonthly"
    group.attrs["reference_grid_id"] = dynamic.attrs.get("grid_id")
    group.attrs["reference_zarr_path"] = str(reference)
    group["local_inc_angle"][:] = np.full((6, 8), 35.0, dtype=np.float32)
    group["ls_map"][:] = np.zeros((6, 8), dtype=np.float32)
    return path


@pytest.fixture()
def cube(tmp_path):
    root = tmp_path / "cube"
    root.mkdir()
    dynamic = _write_smonthly(root)
    static = _write_static(root, dynamic)
    result = resync_catalog_from_filesystem(root, write_stac=False)
    assert result["success"], result
    return root, dynamic, static


def test_static_grid_discovery_accepts_smonthly_as_authority(tmp_path):
    root = tmp_path / "cube"
    root.mkdir()
    dynamic = _write_smonthly(root)
    reference = _dynamic_grid_for_static_group(
        root / TILE,
        TILE,
        DIRN,
        str(TRACK),
        {"static_layers": {
            "grid_reference": "required",
            "reference_product_type": "smonthly",
        }},
    )
    assert reference is not None
    assert reference["path"] == dynamic
    assert reference["grid_id"] == zarr.open_group(str(dynamic), mode="r").attrs["grid_id"]


def test_integrated_track_discovery_includes_smonthly_stores(tmp_path):
    root = tmp_path / "cube"
    root.mkdir()
    _write_smonthly(root)
    assert _scene_reference_tracks(root / TILE, TILE, DIRN) == {TRACK}


def test_integrated_static_filters_lut_to_smonthly_tracks(tmp_path):
    root = tmp_path / "cube"
    root.mkdir()
    _write_smonthly(root)
    lut = pd.DataFrame({
        "track_number": [TRACK, 91],
        "jpl_burst_id": ["T018_000001_IW1", "T091_000001_IW1"],
    })
    filtered = _filter_lut_to_dynamic_tracks(
        lut,
        root / TILE,
        TILE,
        DIRN,
        {"static_layers": {"run_after_scenes": True}},
    )
    assert filtered["track_number"].tolist() == [TRACK]


def test_catalog_pairs_sibling_products_by_geometry_group(cube):
    root, dynamic, static = cube
    assert dynamic.parent.parent.name == f"smonthly_{DIRN}"
    assert static.parent.parent.name == f"static_{DIRN}"
    catalog = pd.read_parquet(root / "catalog.parquet")
    monthly = catalog[catalog["product_type"] == "smonthly"].iloc[0]
    auxiliary = catalog[catalog["product_type"] == "static"].iloc[0]
    expected = f"{TILE}_{DIRN}_TK{TRACK}"
    assert monthly["geometry_group_id"] == expected
    assert auxiliary["geometry_group_id"] == expected
    assert monthly["grid_id"] == auxiliary["grid_id"]


def test_resolver_keeps_static_two_dimensional(cube):
    root, _, _ = cube
    stack = CubeResolver(root).open_stack(
        TILE,
        ["smonthly", "static"],
        direction=DIRN,
        track=TRACK,
    )
    assert isinstance(stack, xr.Dataset)
    assert stack["VV_dB"].dims == ("time", "y", "x")
    assert stack["local_inc_angle"].dims == ("y", "x")
    assert "time" not in stack["local_inc_angle"].dims
    assert stack["local_inc_angle"].shape == stack["VV_dB"].shape[-2:]
