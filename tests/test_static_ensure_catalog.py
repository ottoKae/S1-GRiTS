from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

import s1grits.workflow_static as ws  # noqa: E402


def test_catalog_ensure_derives_all_identity_from_dynamic_catalog(tmp_path, monkeypatch):
    pd.DataFrame([{
        "product_type": "smonthly",
        "product_label": "smonthly_ASCENDING",
        "tile_id": "17MPU",
        "flight_direction": "ASCENDING",
        "geometry_group_id": "17MPU_ASCENDING_TK18-19",
        "zarr_path": "smonthly_ASCENDING/zarr/a.zarr",
    }]).to_parquet(tmp_path / "catalog.parquet", index=False)
    captured = {}

    def fake_run(config_path, **kwargs):
        captured.update(kwargs)
        return {"17MPU": {"status": "success"}}

    monkeypatch.setattr(ws, "run_static_layer_workflow", fake_run)
    result = ws.ensure_static_from_catalog(
        tmp_path, "smonthly_ASCENDING", ["17MPU"]
    )
    assert result["17MPU"]["status"] == "success"
    cfg = captured["config_data"]
    assert cfg["roi"] == {
        "manual_mgrs_tiles": ["17MPU"], "flight_direction": "ASCENDING"
    }
    assert cfg["static_layers"]["grid_reference"] == "required"
    assert cfg["static_layers"]["reference_product_label"] == "smonthly_ASCENDING"
    assert "processing" not in cfg
    assert "layers" not in cfg["static_layers"]


def test_catalog_ensure_rejects_uncataloged_label(tmp_path):
    pd.DataFrame(columns=[
        "product_type", "product_label", "tile_id", "flight_direction", "zarr_path"
    ]).to_parquet(tmp_path / "catalog.parquet", index=False)
    with pytest.raises(ValueError, match="No cataloged dynamic stores"):
        ws.ensure_static_from_catalog(tmp_path, "smonthly_ASCENDING")
