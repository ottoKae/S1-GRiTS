"""v3 Chinese-console contracts: MGRS, plans, catalogs, and static assets."""
from __future__ import annotations

import io
import re
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from s1grits.__version__ import __version__  # noqa: E402
from s1grits.canonical_catalog_schema import (  # noqa: E402
    CANONICAL_CATALOG_COLUMNS,
    normalize_catalog_record,
)
from s1grits.webapp import server as web_server  # noqa: E402
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
    assert caps["version"] == __version__
    assert caps["catalog_schema_version"] == 8
    assert caps["stac_format"] == "geoparquet"
    assert caps["basemaps"]["road"][0]["name"] == "Google 道路"
    assert caps["basemaps"]["road"][1]["name"] == "OpenStreetMap（备用）"
    assert caps["basemaps"]["satellite"][0]["name"] == "Google 卫星"
    assert caps["basemaps"]["satellite"][1]["name"] == "Esri 卫星（备用）"
    assert caps["mgrs_map"]["count_only_below_min_zoom"] is True
    assert caps["catalog_map"]["endpoint"] == "/api/catalog/map"
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
    mapped = cn_client.get(
        "/api/catalog/map", params={"output": "cube", "tile": "17MPU"}
    ).json()
    assert mapped["total_records"] == 1
    assert mapped["tile_count"] == 1
    assert mapped["mapped_tile_count"] == 1
    assert mapped["truncated"] is False
    feature = mapped["features"]["features"][0]
    assert feature["properties"]["tile_id"] == "17MPU"
    assert feature["properties"]["record_count"] == 1
    assert feature["geometry"]["type"] in {"Polygon", "MultiPolygon"}
    assert cn_client.get(
        "/api/catalog/report", params={"output": "cube"}
    ).json()["overall"]["tile_count"] == 1


def test_external_catalog_root_is_read_only_browsable_and_root_selectable(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    record = normalize_catalog_record({
        "item_id": "17MPU_ASCENDING_TK18_2026-01",
        "collection_id": "s1grits-smonthly",
        "product_type": "smonthly",
        "product_label": "smonthly_ASCENDING",
        "tile_id": "17MPU",
        "flight_direction": "ASCENDING",
        "datetime": pd.Timestamp("2026-01-15"),
        "month": "2026-01",
        "grid_id": "external-grid",
        "zarr_path": "17MPU/smonthly_ASCENDING/zarr/cube.zarr",
    })
    pd.DataFrame([record], columns=CANONICAL_CATALOG_COLUMNS).to_parquet(
        external / "catalog.parquet"
    )
    client = TestClient(create_app(
        workspace,
        job_cmd_prefix=_stub(),
        catalog_roots=[f"历史成果={external}"],
    ))

    roots = client.get("/api/catalog-roots").json()["roots"]
    assert [item["root_id"] for item in roots][0] == "workspace"
    selected = next(item for item in roots if item["label"] == "历史成果")
    assert selected["writable"] is False
    assert selected["catalog_available"] is True
    root_id = selected["root_id"]

    browsing = client.get(
        "/api/catalog-directories", params={"root_id": root_id, "path": ""}
    ).json()
    assert browsing["path"] == ""
    assert browsing["catalog_available"] is True
    inspected = client.get(
        "/api/catalog/inspect", params={"root_id": root_id, "output": ""}
    ).json()
    assert inspected["valid"] is True
    assert inspected["root_id"] == root_id
    mapped = client.get(
        "/api/catalog/map", params={"root_id": root_id, "output": ""}
    ).json()
    assert mapped["total_records"] == 1
    assert mapped["mapped_tile_count"] == 1

    assert client.get(
        "/api/catalog-directories",
        params={"root_id": root_id, "path": "../workspace"},
    ).status_code == 400
    assert client.get(
        "/api/catalog-directories", params={"root_id": "unknown", "path": ""}
    ).status_code == 400
    assert client.get("/api/output-directories").json()["directories"] == []


def test_gui_catalog_root_registration_persists_and_can_be_removed(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "cube"
    child_cube = external / "hubei-2026"
    ignored_zarr = external / "internal.zarr"
    nested_cube = external / "archive" / "nested-cube"
    hidden_cube = external / ".hidden-cube"
    workspace.mkdir()
    child_cube.mkdir(parents=True)
    ignored_zarr.mkdir()
    nested_cube.mkdir(parents=True)
    hidden_cube.mkdir()
    (child_cube / "catalog.parquet").write_bytes(b"candidate")
    (ignored_zarr / "catalog.parquet").write_bytes(b"not-a-cube-root")
    (nested_cube / "catalog.parquet").write_bytes(b"too-deep")
    (hidden_cube / "catalog.parquet").write_bytes(b"hidden")

    first = TestClient(create_app(workspace, job_cmd_prefix=_stub()))
    response = first.post(
        "/api/catalog-roots",
        json={"path": str(external), "label": "湖北数据"},
    )
    assert response.status_code == 201
    registered = response.json()["root"]
    assert registered["label"] == "湖北数据"
    assert registered["path_display"] == str(external.resolve())
    assert registered["removable"] is True
    root_id = registered["root_id"]
    assert (workspace / ".webapp" / "catalog_roots.json").is_file()
    candidates = first.get(
        "/api/catalog-candidates", params={"root_id": root_id}
    ).json()
    assert candidates["candidate_count"] == 1
    assert candidates["candidates"][0]["path"] == "hubei-2026"
    assert candidates["directories_scanned"] <= 200
    assert candidates["limits"]["directories"] == 200
    broken = first.get(
        "/api/catalog/inspect",
        params={"root_id": root_id, "output": "hubei-2026"},
    )
    assert broken.status_code == 200
    assert broken.json()["valid"] is False
    assert "无法读取" in broken.json()["issues"][0]

    restarted = TestClient(create_app(workspace, job_cmd_prefix=_stub()))
    roots = restarted.get("/api/catalog-roots").json()["roots"]
    assert any(item["root_id"] == root_id for item in roots)
    assert restarted.get(
        "/api/catalog-directories", params={"root_id": root_id, "path": ""}
    ).status_code == 200
    assert restarted.delete(f"/api/catalog-roots/{root_id}").status_code == 200
    assert restarted.delete("/api/catalog-roots/workspace").status_code == 400
    assert restarted.post(
        "/api/catalog-roots", json={"path": "relative/cube"}
    ).status_code == 400

    after_removal = TestClient(create_app(workspace, job_cmd_prefix=_stub()))
    remaining = after_removal.get("/api/catalog-roots").json()["roots"]
    assert all(item["root_id"] != root_id for item in remaining)


def test_missing_gui_catalog_root_remains_visible_and_forgettable(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "movable-cube"
    workspace.mkdir()
    external.mkdir()
    client = TestClient(create_app(workspace, job_cmd_prefix=_stub()))
    registered = client.post(
        "/api/catalog-roots", json={"path": str(external), "label": "可移动数据"}
    ).json()["root"]
    root_id = registered["root_id"]
    external.rmdir()

    restarted = TestClient(create_app(workspace, job_cmd_prefix=_stub()))
    root = next(
        item for item in restarted.get("/api/catalog-roots").json()["roots"]
        if item["root_id"] == root_id
    )
    assert root["exists"] is False
    inspected = restarted.get(
        "/api/catalog/inspect", params={"root_id": root_id, "output": ""}
    )
    assert inspected.status_code == 400
    assert "移动、删除或当前不可访问" in inspected.json()["detail"]
    assert restarted.delete(f"/api/catalog-roots/{root_id}").status_code == 200


def test_native_catalog_picker_registers_selected_folder(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    external = tmp_path / "picked-cube"
    workspace.mkdir()
    external.mkdir()
    monkeypatch.setattr(
        "s1grits.webapp.server._pick_windows_directory",
        lambda: str(external),
    )
    client = TestClient(create_app(workspace, job_cmd_prefix=_stub()))

    picked = client.post("/api/catalog-roots/pick")
    assert picked.status_code == 200
    assert picked.json()["cancelled"] is False
    assert picked.json()["root"]["path_display"] == str(external.resolve())

    monkeypatch.setattr(
        "s1grits.webapp.server._pick_windows_directory",
        lambda: None,
    )
    assert client.post("/api/catalog-roots/pick").json() == {"cancelled": True}


def test_catalog_folder_browser_lists_roots_without_desktop_dialog(cn_client, monkeypatch):
    console = cn_client.app.state.console
    monkeypatch.setattr(
        console,
        "_catalog_folder_roots",
        lambda: [
            {"name": "C:", "path": "C:\\", "drive_type": "fixed"},
            {"name": "D:", "path": "D:\\", "drive_type": "fixed"},
        ],
    )

    response = cn_client.get("/api/catalog-folders")
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "drives"
    assert data["path"] == ""
    assert data["parent"] is None
    assert data["drives"] == [
        {"name": "C:", "path": "C:\\", "drive_type": "fixed"},
        {"name": "D:", "path": "D:\\", "drive_type": "fixed"},
    ]
    assert data["directories"] == []
    assert data["returned"] == 2
    assert data["truncated"] is False
    capability = cn_client.get("/api/capabilities").json()["catalog_folder_browser"]
    assert capability["endpoint"] == "/api/catalog-folders"
    assert capability["local_only"] is True
    assert capability["read_only"] is True


def test_catalog_folder_browser_handles_unicode_spaces_catalog_and_limits(
    cn_client, tmp_path: Path,
):
    root = tmp_path / "湖北 数据立方体"
    with_catalog = root / "月度 产品"
    without_catalog = root / "原始影像"
    with_catalog.mkdir(parents=True)
    without_catalog.mkdir()
    (root / "catalog.parquet").write_bytes(b"root catalog marker")
    (with_catalog / "catalog.parquet").write_bytes(b"child catalog marker")
    (root / "not-a-folder.txt").write_text("must not be returned", encoding="utf-8")

    response = cn_client.get("/api/catalog-folders", params={"path": str(root)})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["mode"] == "directory"
    assert data["path"] == str(root.resolve())
    assert data["name"] == root.name
    assert data["parent"] == str(root.resolve().parent)
    assert data["catalog_available"] is True
    assert [item["name"] for item in data["directories"]] == ["原始影像", "月度 产品"]
    child = next(item for item in data["directories"] if item["name"] == "月度 产品")
    assert child["path"] == str(with_catalog.resolve())
    assert child["catalog_available"] is True
    assert "not-a-folder.txt" not in {item["name"] for item in data["directories"]}
    assert data["returned"] == 2
    assert data["truncated"] is False

    console = cn_client.app.state.console
    console.max_catalog_folder_directories = 1
    limited = cn_client.get("/api/catalog-folders", params={"path": str(root)}).json()
    assert limited["returned"] == 1
    assert limited["directories_scanned"] == 1
    assert limited["truncated"] is True
    assert limited["limits"]["directories"] == 1


def test_catalog_folder_browser_rejects_remote_missing_file_and_permission(
    cn_client, tmp_path: Path, monkeypatch,
):
    remote = TestClient(cn_client.app, client=("203.0.113.10", 50000))
    assert remote.get("/api/catalog-folders").status_code == 403

    missing = tmp_path / "missing folder"
    assert cn_client.get(
        "/api/catalog-folders", params={"path": str(missing)}
    ).status_code == 404
    assert cn_client.get(
        "/api/catalog-folders", params={"path": "relative-folder"}
    ).status_code == 400
    file_path = tmp_path / "catalog.parquet"
    file_path.write_bytes(b"not a directory")
    assert cn_client.get(
        "/api/catalog-folders", params={"path": str(file_path)}
    ).status_code == 400

    def denied(_path=""):
        raise PermissionError("无权读取测试目录")

    monkeypatch.setattr(cn_client.app.state.console, "catalog_folders", denied)
    denied_response = cn_client.get(
        "/api/catalog-folders", params={"path": str(tmp_path)}
    )
    assert denied_response.status_code == 403
    assert "无权读取" in denied_response.json()["detail"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows picker contract")
def test_windows_picker_uses_visible_owned_dialog_without_console(monkeypatch):
    selected = "D:\\数据\\S1 Cube"
    encoded = web_server.base64.b64encode(selected.encode("utf-16-le")).decode("ascii")
    captured = {}

    class Result:
        returncode = 0
        stdout = encoded
        stderr = ""

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(web_server.subprocess, "run", fake_run)
    assert web_server._pick_windows_directory() == selected
    assert "-WindowStyle" not in captured["arguments"]
    command = captured["arguments"][-1]
    assert "$owner.TopMost=$true" in command
    assert "FormStartPosition]::CenterScreen" in command
    assert "$owner.Opacity=0" in command
    assert "-32000" not in command
    assert "$dialog.ShowDialog($owner)" in command
    assert "creationflags" not in captured["kwargs"]


def test_catalog_map_aggregates_beyond_record_list_limit(cn_client):
    root = Path(cn_client.app.state.workspace.root)
    rows = []
    for index in range(520):
        rows.append(normalize_catalog_record({
            "item_id": f"17MPU_ASCENDING_TK18_2026-01_{index:04d}",
            "collection_id": "s1grits-smonthly",
            "product_type": "smonthly",
            "product_label": "smonthly_ASCENDING",
            "tile_id": "17MPU",
            "flight_direction": "ASCENDING",
            "datetime": pd.Timestamp("2026-01-01") + pd.Timedelta(hours=index),
            "month": "2026-01",
            "grid_id": "abc123",
            "zarr_path": "17MPU/smonthly_ASCENDING/zarr/cube.zarr",
        }))
    pd.DataFrame(rows, columns=CANONICAL_CATALOG_COLUMNS).to_parquet(
        root / "cube" / "catalog.parquet"
    )

    listed = cn_client.get("/api/catalog", params={"output": "cube"}).json()
    assert listed["total"] == 520
    assert listed["returned"] == 500
    mapped = cn_client.get("/api/catalog/map", params={"output": "cube"}).json()
    assert mapped["total_records"] == 520
    assert mapped["mapped_tile_count"] == 1
    assert mapped["features"]["features"][0]["properties"]["record_count"] == 520


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
        assert cfg["processing"]["target_resolution"] == 30.0
        assert cfg["processing"]["resampling_method"] == "nearest"
        assert cfg["processing"]["monthly"]["enabled"] is True
        assert cfg["processing"]["monthly"]["only"] is True
        assert cfg["static_layers"]["run_after_scenes"] is True
        assert cfg["output"]["existing_store"] == "resume"
        assert cfg["output"]["existing_month"] == "skip"


def test_10m_plan_uses_optimized_bilinear_and_rejects_other_resolutions(cn_client):
    year = time.gmtime().tm_year
    base = {
        "workflow": "scenes",
        "selection_mode": "tiles",
        "tiles": "17MPU",
        "direction": "ASCENDING",
        "years": [year],
        "months": [1],
        "output_subdir": "ten_m_cube",
        "zarr_only": True,
        "target_resolution": 10,
        "max_workers": 1,
    }
    planned = cn_client.post("/api/plan", json=base)
    assert planned.status_code == 200, planned.text
    assert planned.json()["target_resolution"] == 10.0
    assert planned.json()["resampling_method"] == "bilinear"
    created = cn_client.post("/api/tasks", json={
        "plan_id": planned.json()["plan_id"],
        "confirmation": planned.json()["confirmation_phrase"],
    })
    assert created.status_code == 201, created.text
    job = cn_client.app.state.jobs.get(created.json()["run_id"])
    import yaml

    config_path = next(job.job_dir.glob("config_*.yaml"))
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert cfg["processing"]["target_resolution"] == 10.0
    assert cfg["processing"]["resampling_method"] == "bilinear"

    invalid = cn_client.post(
        "/api/plan", json={**base, "target_resolution": 20}
    )
    assert invalid.status_code == 400
    assert "30 or 10" in invalid.json()["detail"]


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

    low_zoom = cn_client.get(
        "/api/map/mgrs", params={"bbox": "-82,-3,-78,2", "zoom": 1}
    ).json()
    assert low_zoom["visible"] is False
    assert low_zoom["reason"] == "zoom"
    assert low_zoom["count"] > 0
    assert low_zoom["returned"] == 0
    assert low_zoom["features"]["features"] == []
    assert "缩放至 4 级" in low_zoom["message"]


def test_aoi_geojson_resolves_candidates_and_keeps_final_selection_explicit(cn_client):
    tile = cn_client.get(
        "/api/map/mgrs", params={"bbox": "-82,-3,-78,2", "zoom": 7}
    ).json()["features"]["features"]
    geometry = next(
        feature["geometry"] for feature in tile
        if feature["properties"]["tile_id"] == "17MPU"
    )
    response = cn_client.post(
        "/api/spatial/aoi/resolve", json={"geometry": geometry}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["normalized_crs"] == "EPSG:4326"
    assert "17MPU" in data["candidate_tiles"]
    assert data["candidate_count"] == len(data["tile_features"])

    upload = cn_client.post(
        "/api/spatial/aoi/resolve",
        files={"files": ("aoi.geojson", __import__("json").dumps(geometry), "application/geo+json")},
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["candidate_tiles"] == data["candidate_tiles"]

    # Browser multipart boundaries are case-sensitive and commonly contain
    # mixed-case "WebKitFormBoundary" text.
    boundary = "----WebKitFormBoundaryAbC123"
    raw = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="aoi.geojson"\r\n'
        "Content-Type: application/geo+json\r\n\r\n"
        + __import__("json").dumps(geometry)
        + f"\r\n--{boundary}--\r\n"
    ).encode()
    browser_upload = cn_client.post(
        "/api/spatial/aoi/resolve", content=raw,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert browser_upload.status_code == 200, browser_upload.text
    assert browser_upload.json()["candidate_tiles"] == data["candidate_tiles"]


def test_projected_shapefile_zip_is_reprojected_before_mgrs_lookup(cn_client, tmp_path):
    gpd = pytest.importorskip("geopandas")
    tile = cn_client.get(
        "/api/map/mgrs", params={"bbox": "-82,-3,-78,2", "zoom": 7}
    ).json()["features"]["features"]
    geometry = next(
        feature["geometry"] for feature in tile
        if feature["properties"]["tile_id"] == "17MPU"
    )
    from shapely.geometry import shape

    source = tmp_path / "shape"
    source.mkdir()
    gpd.GeoDataFrame({"id": [1]}, geometry=[shape(geometry)], crs="EPSG:4326").to_crs(
        "EPSG:3857"
    ).to_file(source / "area.shp")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        for path in source.glob("area.*"):
            output.write(path, path.name)
    response = cn_client.post(
        "/api/spatial/aoi/resolve",
        files={"files": ("area.zip", archive.getvalue(), "application/zip")},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert "17MPU" in data["candidate_tiles"]
    assert data["normalized_crs"] == "EPSG:4326"
    assert "3857" in data["source_crs"]


def test_aoi_zip_rejects_path_traversal(cn_client):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../area.shp", b"bad")
    response = cn_client.post(
        "/api/spatial/aoi/resolve",
        files={"files": ("area.zip", archive.getvalue(), "application/zip")},
    )
    assert response.status_code == 400
    assert "不安全" in response.json()["detail"]


def test_confirmation_contract_is_absent_for_small_plan_and_explicit_for_large(cn_client):
    year = time.gmtime().tm_year
    common = {
        "workflow": "scenes", "selection_mode": "tiles", "tiles": ["17MPU"],
        "direction": "ASCENDING", "years": [year], "output_subdir": "confirm_cube",
        "zarr_only": True, "max_workers": 1,
    }
    small = cn_client.post(
        "/api/plan", json={**common, "months": [1], "target_resolution": 30}
    ).json()
    assert small["confirmation_required"] is False
    assert small["confirmation_phrase"] == ""
    large = cn_client.post(
        "/api/plan", json={**common, "months": list(range(1, 13)), "target_resolution": 10}
    ).json()
    assert large["confirmation_required"] is True
    assert large["confirmation_phrase"].startswith("下载 ")
    assert "10 GiB" in large["confirmation_reason"]


def test_frontend_exposes_subset_selection_shapefile_and_recovery(cn_client):
    html = cn_client.get("/").text
    script = cn_client.get("/static/app.js").text
    assert "Shapefile ZIP" in html
    assert "清除 AOI" in html and "清空瓦片" in html
    assert "大任务二次确认" in html and "恢复中断任务" in html
    assert "任务中心" in html and "本地数据" in html
    assert ">卫星</button>" in html
    assert "Catalog 检索与报告" not in html
    assert "sessionStorage" in script
    assert "selection_mode:'tiles'" in script
    assert "interactive:false" in script
    assert "currentMgrsLayers" in script
    assert "currentCatalogLayers" in script
    assert "catalogCoverageLayer" in script
    assert "/api/catalog/map" in script
    assert "setWorkspaceTab" in script
    assert "/api/catalog-roots" in script
    assert "/api/catalog-roots/pick" not in script
    assert "/api/catalog-folders" in script
    assert "/api/catalog-candidates" in script
    assert "/api/catalog-directories" not in script
    assert "选择数据立方体文件夹" in html
    assert "select-catalog-folder" in html
    assert "catalog-folder-dialog" in html
    assert "catalog-folder-list" in html
    assert "select-current-catalog-folder" in html
    assert "native-catalog-folder" not in html
    assert "manual-catalog-folder" in html
    assert "进入 ›" in script
    assert "catalog-empty" in html
    assert "catalog-selection" in html
    assert "scan-catalog-children" in html
    assert "catalog-root-path" in html
    assert "forget-catalog-folder" in html
    assert 'id="cat-root"' not in html
    assert "浏览内部目录" not in html
    assert "add-catalog-root" not in html
    assert "catalogRootId" in script
    assert "$('select-catalog-folder').onclick=openCatalogFolderDialog" in script
    assert "catalogFolderRequestSerial" in script
    assert "catalogMode" not in script
    assert "updateVisibleTileStyle" in script
    assert "keepBuffer:2" in script
    assert "subdomains:provider.subdomains" not in script
    assert "updateWhenIdle:true" not in script
    assert "updateWhenZooming:false" not in script
    assert "drawSpatialSelection" not in script
    assert "resolvedGridLayer" not in script
    assert "mgrsGridLayer.setStyle" not in script
    referenced_ids = set(re.findall(r"\$\('([^']+)'\)", script))
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    assert referenced_ids <= html_ids
