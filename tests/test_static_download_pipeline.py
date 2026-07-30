from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

zarr = pytest.importorskip("zarr")
from rasterio.transform import Affine  # noqa: E402
import s1grits.workflow_static as ws  # noqa: E402


def test_downloaded_static_layer_is_written_on_reference_grid(tmp_path, monkeypatch):
    tile = "17MPU"
    direction = "DESCENDING"
    static_dir = tmp_path / tile / f"static_{direction}"
    scene_path = tmp_path / tile / f"scenes_{direction}_ARDC" / "zarr" / \
        f"s1grits_scenes_{tile}_{direction}_TK40.zarr"
    scene_path.parent.mkdir(parents=True)

    transform = Affine(30.0, 0.0, 100.0, 0.0, -30.0, 200.0)
    x = np.array([115.0, 145.0], dtype=np.float64)
    y = np.array([185.0, 155.0], dtype=np.float64)
    expected = np.array([[10.0, 11.0], [12.0, 13.0]], dtype=np.float32)
    calls = []

    def fake_download(urls, **kwargs):
        calls.append(list(urls))
        return [expected.copy()], [{"transform": transform, "crs": "EPSG:32617"}], [None]

    monkeypatch.setattr(ws, "read_asf_rtc_image_data", fake_download)
    monkeypatch.setattr(ws, "_mosaic_align", lambda *args, **kwargs: expected.copy())
    monkeypatch.setattr(ws, "_build_static_cog", lambda *args, **kwargs: None)

    rows = pd.DataFrame({
        "url_local_inc_angle": ["https://example.test/static-lia.tif"],
        "acq_dt": ["2024-01-01T00:00:00Z"],
    })
    reference = {
        "path": scene_path,
        "product_label": f"scenes_{direction}_ARDC",
        "grid_id": "reference-grid",
    }

    # Match the canonical id that a real scenes store would carry.
    from s1grits.canonical_catalog_schema import make_grid_id
    reference["grid_id"] = make_grid_id(
        tile, "EPSG:32617", list(transform)[:6], len(x), len(y)
    )
    result = ws._process_one_static_group(
        rows=rows,
        layers=["local_inc_angle"],
        static_dir=static_dir,
        mgrs_tile_id=tile,
        direction_label=direction,
        track_token="40",
        n_bursts=1,
        master_transform=transform,
        width=2,
        height=2,
        target_crs="EPSG:32617",
        cog_block=16,
        overwrite=True,
        max_workers=1,
        retry_timeout=30.0,
        config={"static_layers": {"enabled": True}},
        product_label=f"static_{direction}",
        generate_zarr=True,
        chunk_y=2,
        chunk_x=2,
        x_coords=x,
        y_coords=y,
        reference_grid=reference,
    )

    assert result["status"] == "success"
    assert calls == [["https://example.test/static-lia.tif"]]
    g = zarr.open_group(result["zarr_path"], mode="r")
    np.testing.assert_array_equal(g["local_inc_angle"][:], expected)
    np.testing.assert_array_equal(g["x"][:], x)
    np.testing.assert_array_equal(g["y"][:], y)
    assert g.attrs["grid_id"] == reference["grid_id"]
    assert g.attrs["reference_grid_id"] == reference["grid_id"]
    assert g.attrs["geometry_group_id"] == f"{tile}_{direction}_TK40"
    assert g.attrs["grid_source"] == "workflow_scenes"
