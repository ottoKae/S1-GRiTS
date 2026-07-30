from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

zarr = pytest.importorskip("zarr")
from rasterio.transform import Affine  # noqa: E402
from s1grits.workflow_static import (  # noqa: E402
    ALL_STATIC_LAYERS,
    _dynamic_grid_for_static_group,
    _get_enabled_layers,
    _scene_reference_tracks,
)

TILE = "17MPU"
DIRN = "DESCENDING"


def test_static_adopts_matching_scenes_zarr_grid(tmp_path):
    tile_dir = tmp_path / TILE
    path = tile_dir / f"scenes_{DIRN}_ARDC" / "zarr" / \
        f"s1grits_scenes_{TILE}_{DIRN}_TK40.zarr"
    path.parent.mkdir(parents=True)
    tfm = Affine(30.0, 0.0, 100.0, 0.0, -30.0, 200.0)
    xs = np.array([115.0, 145.0, 175.0])
    ys = np.array([185.0, 155.0])
    g = zarr.open_group(str(path), mode="w", zarr_format=3)
    g.attrs.update({"crs": "EPSG:32617", "transform": list(tfm)[:6], "grid_id": "scene-grid"})
    g.create_array("x", data=xs, dimension_names=["x"])
    g.create_array("y", data=ys, dimension_names=["y"])

    ref = _dynamic_grid_for_static_group(
        tile_dir, TILE, DIRN, "40", {"static_layers": {"grid_reference": "required"}}
    )
    assert ref is not None
    assert ref["path"] == path
    assert ref["grid_id"] == "scene-grid"
    assert ref["transform"] == tfm
    np.testing.assert_array_equal(ref["x"], xs)
    np.testing.assert_array_equal(ref["y"], ys)


def test_required_scenes_grid_fails_when_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="Run workflow_scenes first"):
        _dynamic_grid_for_static_group(
            tmp_path / TILE, TILE, DIRN, "40",
            {"static_layers": {"grid_reference": "required"}},
        )


def test_integrated_mode_discovers_only_written_scene_tracks(tmp_path):
    tile_dir = tmp_path / TILE
    zdir = tile_dir / f"scenes_{DIRN}_ARDC" / "zarr"
    zdir.mkdir(parents=True)
    (zdir / f"s1grits_scenes_{TILE}_{DIRN}_TK40.zarr").mkdir()
    (zdir / f"s1grits_scenes_{TILE}_{DIRN}_TK69-172.zarr").mkdir()
    (zdir / f"unrelated_{TILE}_TK99.zarr").mkdir()
    assert _scene_reference_tracks(tile_dir, TILE, DIRN) == {40, 69, 172}


def test_integrated_mode_defaults_to_all_static_layers():
    cfg = {"static_layers": {"run_after_scenes": True}}
    assert _get_enabled_layers(cfg) == ALL_STATIC_LAYERS
