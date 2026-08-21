"""v3 Chinese-console contracts: MGRS, plans, catalogs, and static assets."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from s1grits.canonical_catalog_schema import (  # noqa: E402
    CANONICAL_CATALOG_COLUMNS,
    normalize_catalog_record,
)
from s1grits.webapp.server import create_app  # noqa: E402


def _stub() -> list[str]:
    return [sys.executable, "-c", "print('ok', flush=True)", "--ignored"]


@pytest.fixture()
def cn_client(tmp_path: Path) -> TestClient:
    record = normalize_catalog_record({
        "item_id": "17MPU_ASCENDING_TK18_2026-01",
        "collection_id": "s1grits-smonthly",
        "product_type": "smonthly",
        "product_label": "smonthly_ASCENDING",
        "tile_id": "17MPU",
        "flight_direction": "ASCENDING",
        "datetime": pd.Timestamp("2026-01-15"),
        "month": "2026-01",
        "grid_id": "abc123",
        "zarr_path": "17MPU/smonthly_ASCENDING/zarr/cube.zarr",
        "status": "complete",
    })
    cube = tmp_path / "cube"
    cube.mkdir()
    pd.DataFrame([record], columns=CANONICAL_CATALOG_COLUMNS).to_parquet(
        cube / "catalog.parquet"
    )
    return TestClient(create_app(tmp_path, job_cmd_prefix=_stub()))


def test_capabilities_and_packaged_frontend(cn_client):
    caps = cn_client.get("/api/capabilities").json()
    assert caps["version"] == "3.0.0"
    assert caps["catalog_schema_version"] == 8
    assert caps["stac_format"] == "geoparquet"
    assert cn_client.get("/").status_code == 200
    assert cn_client.get("/static/leaflet/leaflet.js").status_code == 200
    assert cn_client.get("/static/logo-mark.png").status_code == 200


def test_catalog_picker_schema_v8_and_query(cn_client):
    listing = cn_client.get(
        "/api/output-directories", params={"path": "", "mode": "catalog"}
    ).json()
    cube = next(item for item in listing["directories"] if item["name"] == "cube")
    assert cube["catalog_available"] is True
    inspected = cn_client.get("/api/catalog/inspect", params={"output": "cube"}).json()
    assert inspected["valid"] is True
    assert inspected["schema_versions"] == [8]
    queried = cn_client.get(
        "/api/catalog", params={"output": "cube", "tile": "17MPU"}
    ).json()
    assert queried["total"] == 1
    assert queried["records"][0]["grid_id"] == "abc123"
    assert cn_client.get(
        "/api/catalog/report", params={"output": "cube"}
    ).json()["overall"]["tile_count"] == 1


def test_directory_traversal_is_rejected(cn_client):
    assert cn_client.get(
        "/api/output-directories", params={"path": "../outside"}
    ).status_code == 400


def test_monthly_static_plan_maps_only_to_v3_process_scenes(cn_client):
    year = time.gmtime().tm_year
    planned = cn_client.post("/api/plan", json={
        "workflow": "monthly",
        "selection_mode": "tiles",
        "tiles": "17MPU",
        "direction": "BOTH",
        "years": [year],
        "months": [1],
        "output_subdir": "monthly_cube",
        "zarr_only": True,
        "include_static": True,
        "target_resolution": 30,
        "max_workers": 1,
    })
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    created = cn_client.post("/api/tasks", json={
        "plan_id": plan["plan_id"],
        "confirmation": plan["confirmation_phrase"],
    })
    assert created.status_code == 201, created.text
    job = cn_client.app.state.jobs.get(created.json()["run_id"])
    assert len(job.commands) == 5  # two directions + three Catalog gates
    flattened = [part for command in job.commands for part in command]
    assert flattened.count("process_scenes") == 2
    assert "process" not in flattened
    assert "process_static" not in flattened
    for config_path in sorted(job.job_dir.glob("config_*.yaml")):
        import yaml

        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert cfg["processing"]["monthly"]["enabled"] is True
        assert cfg["processing"]["monthly"]["only"] is True
        assert cfg["static_layers"]["run_after_scenes"] is True
        assert cfg["output"]["existing_store"] == "resume"
        assert cfg["output"]["existing_month"] == "skip"


def test_mgrs_bbox_endpoint_uses_packaged_geoparquet(cn_client):
    response = cn_client.get(
        "/api/map/mgrs", params={"bbox": "-82,-3,-78,2", "zoom": 7}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["source_crs"] == "EPSG:4326"
    assert data["display_crs"] == "EPSG:3857"
    assert data["truncated"] is False
    assert data["returned"] > 0
    assert any(f["properties"]["tile_id"] == "17MPU" for f in data["features"]["features"])
