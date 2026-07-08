"""v2.3 web UI: API surface, safety properties, and job manager.

Runs against a synthetic workspace: one tile with a catalog.parquet, an
smonthly Zarr store (with n_obs), and a preview PNG. FastAPI is optional
(``s1grits[web]``), so the whole module skips when it is absent.

Job tests inject a stub command prefix (``python -c``-driven fake CLI) so no
real pipeline runs; what IS exercised is the queueing, whitelisting, config
pinning, progress parsing, logging, cancellation, and persistence.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

pytest.importorskip("rasterio")
zarr = pytest.importorskip("zarr")
fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")  # TestClient transport

from fastapi.testclient import TestClient  # noqa: E402
from rasterio.transform import Affine  # noqa: E402

from s1grits.canonical_catalog_schema import normalize_catalog_record  # noqa: E402
from s1grits.workflow_scenes import (  # noqa: E402
    N_OBS_BAND,
    _append_zarr_timestep,
    _init_zarr_2band,
)
from s1grits.webapp.jobs import JobManager  # noqa: E402
from s1grits.webapp.server import create_app  # noqa: E402

CRS = "EPSG:32717"
RES = 30.0
TILE = "17MPU"
W, H = 40, 32
MINX, MAXY = 499980.0, 8499990.0


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """One-tile workspace: catalog + zarr (2 months, n_obs) + preview PNG."""
    tile_dir = tmp_path / TILE
    product = "smonthly_ASCENDING"
    zrel = f"{product}/zarr/s1grits_smonthly_{TILE}_ASCENDING_TK18.zarr"
    prel = f"{product}/preview/s1grits_smonthly_{TILE}_ASCENDING_TK18_2026-01.png"

    transform = Affine(RES, 0.0, MINX, 0.0, -RES, MAXY)
    x = (MINX + (np.arange(W) + 0.5) * RES).astype("float64")
    y = (MAXY - (np.arange(H) + 0.5) * RES).astype("float64")
    g = _init_zarr_2band(
        tile_dir / zrel, x, y, CRS, transform, 16, 16,
        processing_level="monthly_ARDC",
        band_names=["VV_dB", "VH_dB", N_OBS_BAND],
    )
    for month, vv in (("2026-01", -12.0), ("2026-02", -10.0)):
        dt = np.datetime64(pd.Timestamp(f"{month}-15").to_datetime64(), "ns")
        _append_zarr_timestep(g, dt, [
            ("VV_dB", np.full((H, W), vv, np.float32)),
            ("VH_dB", np.full((H, W), vv - 6.0, np.float32)),
            (N_OBS_BAND, np.full((H, W), 3, np.uint8)),
        ])

    (tile_dir / product / "preview").mkdir(parents=True)
    # 1x1 black PNG
    (tile_dir / prel).write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
        "de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e"
        "44ae426082"
    ))

    records = []
    for month in ("2026-01", "2026-02"):
        records.append(normalize_catalog_record({
            "item_id": f"{TILE}_ASCENDING_TK18_{month}",
            "collection_id": "s1grits-smonthly",
            "product_type": "smonthly",
            "product_label": product,
            "tile_id": TILE,
            "flight_direction": "ASCENDING",
            "crs": CRS,
            "transform": list(transform)[:6],
            "width": W, "height": H,
            "datetime": pd.Timestamp(f"{month}-15"),
            "month": month,
            "track": 18,
            "n_scenes": 3,
            "zarr_path": zrel,
            "preview_path": prel if month == "2026-01" else None,
            "bands": json.dumps(["VV_dB", "VH_dB", N_OBS_BAND]),
        }))
    pd.DataFrame(records).to_parquet(tile_dir / "catalog.parquet")
    return tmp_path


@pytest.fixture()
def client(workspace: Path) -> TestClient:
    app = create_app(workspace, job_cmd_prefix=_stub_cli(ok=True))
    return TestClient(app)


def _stub_cli(ok: bool = True, slow: bool = False) -> list[str]:
    """A fake s1grits CLI: prints progress markers the parser understands."""
    code = (
        "import sys,time\n"
        "print('--- Batch 1/2 ---', flush=True)\n"
        "print('[PHASE] x END tile=17MPU batch=1/2', flush=True)\n"
        + ("time.sleep(30)\n" if slow else "") +
        "print('[PHASE] x END tile=17MPU batch=2/2', flush=True)\n"
        "print('password: hunter2', flush=True)\n"
        f"sys.exit({0 if ok else 3})\n"
    )
    return [sys.executable, "-c", code, "--ignored"]


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

def test_health_and_workspace(client):
    assert client.get("/api/health").json()["status"] == "ok"
    ws = client.get("/api/workspace").json()
    assert ws["n_tiles"] == 1 and ws["n_items"] == 2


def test_tiles_have_wgs84_bounds_and_metadata(client):
    tiles = client.get("/api/tiles").json()
    assert len(tiles) == 1
    t = tiles[0]
    assert t["tile_id"] == TILE and t["n_items"] == 2
    (s, w), (n, e) = t["bounds4326"]
    assert -90 < s < n < 90 and -180 < w < e < 180
    assert t["tracks"] == [18]
    assert t["month_min"] == "2026-01" and t["month_max"] == "2026-02"


def test_items_filtering_and_month_histogram(client):
    all_items = client.get("/api/items").json()
    assert all_items["total"] == 2
    assert all_items["months"] == {"2026-01": 1, "2026-02": 1}

    jan = client.get("/api/items", params={"month_to": "2026-01"}).json()
    assert jan["total"] == 1
    assert jan["items"][0]["month"] == "2026-01"
    assert jan["items"][0]["bounds4326"] is not None

    none = client.get("/api/items", params={"track": 99}).json()
    assert none["total"] == 0

    paged = client.get("/api/items", params={"limit": 1, "offset": 1}).json()
    assert paged["total"] == 2 and len(paged["items"]) == 1


# ---------------------------------------------------------------------------
# Visualisation
# ---------------------------------------------------------------------------

def test_timeseries_probe_reads_correct_values(client, workspace):
    import pyproj
    inv = pyproj.Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    lon, lat = inv.transform(MINX + 10 * RES, MAXY - 10 * RES)
    zrel = client.get("/api/items").json()["items"][0]["zarr_path"]
    ts = client.get("/api/timeseries", params={
        "tile": TILE, "zarr_path": zrel, "lon": lon, "lat": lat,
    }).json()
    assert ts["bands"]["VV_dB"] == [-12.0, -10.0]
    assert ts["bands"]["VH_dB"] == [-18.0, -16.0]
    assert ts["bands"][N_OBS_BAND] == [3.0, 3.0]
    assert ts["pixel"]["row"] == 10 and ts["pixel"]["col"] == 10
    assert [t[:7] for t in ts["time"]] == ["2026-01", "2026-02"]


def test_timeseries_outside_grid_is_400(client):
    zrel = client.get("/api/items").json()["items"][0]["zarr_path"]
    r = client.get("/api/timeseries", params={
        "tile": TILE, "zarr_path": zrel, "lon": 0.0, "lat": 0.0,
    })
    assert r.status_code == 400


def test_asset_serving_and_traversal_protection(client):
    prel = client.get("/api/items").json()["items"][0]["preview_path"]
    ok = client.get(f"/api/asset/{TILE}/{prel}")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/png"

    # Escape attempts must not read outside the workspace.
    assert client.get(f"/api/asset/{TILE}/../../etc/passwd").status_code in (403, 404)
    assert client.get(f"/api/asset/{TILE}/..%2f..%2fetc%2fpasswd").status_code in (403, 404)
    # The job ledger under .webapp is never served.
    assert client.get(f"/api/asset/{TILE}/../.webapp/jobs").status_code in (403, 404)


def test_token_auth_gates_api_but_not_static(workspace):
    app = create_app(workspace, token="sekrit", job_cmd_prefix=_stub_cli())
    c = TestClient(app)
    assert c.get("/api/tiles").status_code == 401
    assert c.get("/api/tiles", headers={"Authorization": "Bearer sekrit"}).status_code == 200
    assert c.get("/api/tiles", params={"token": "sekrit"}).status_code == 200
    assert c.get("/").status_code == 200  # SPA shell loads; it prompts for token


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _wait(jm: JobManager, job_id: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if jm.get(job_id).status not in ("queued", "running"):
            return
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def test_job_lifecycle_progress_and_redaction(workspace):
    jm = JobManager(workspace, cmd_prefix=_stub_cli(ok=True))
    job = jm.submit("process_scenes", config_text="output: {base_dir: /elsewhere}\n")
    _wait(jm, job.id)
    d = jm.get(job.id).to_dict()
    assert d["status"] == "success"
    assert d["progress"]["per_tile"]["17MPU"] == [2, 2]
    assert d["progress"]["pct"] is not None

    log = jm.log(job.id)
    joined = "\n".join(log["lines"])
    assert "hunter2" not in joined and "[REDACTED]" in joined

    # output.base_dir was pinned to the workspace root
    import yaml
    cfg = yaml.safe_load((job.job_dir / "config.yaml").read_text())
    assert Path(cfg["output"]["base_dir"]).resolve() == workspace.resolve()


def test_job_failure_and_unknown_type(workspace):
    jm = JobManager(workspace, cmd_prefix=_stub_cli(ok=False))
    job = jm.submit("process_scenes", config_text="workflow: scenes\n")
    _wait(jm, job.id)
    assert jm.get(job.id).status == "failed"
    with pytest.raises(ValueError):
        jm.submit("rm_rf_slash")
    with pytest.raises(ValueError):
        jm.submit("process_scenes", config_text="not: [valid")


def test_job_cancel(workspace):
    jm = JobManager(workspace, cmd_prefix=_stub_cli(ok=True, slow=True))
    job = jm.submit("process_scenes", config_text="workflow: scenes\n")
    deadline = time.time() + 10
    while jm.get(job.id).status == "queued" and time.time() < deadline:
        time.sleep(0.05)
    jm.cancel(job.id)
    _wait(jm, job.id)
    assert jm.get(job.id).status == "cancelled"


def test_job_history_survives_restart(workspace):
    jm = JobManager(workspace, cmd_prefix=_stub_cli(ok=True))
    job = jm.submit("process_scenes", config_text="workflow: scenes\n")
    _wait(jm, job.id)
    jm2 = JobManager(workspace, cmd_prefix=_stub_cli(ok=True))
    revived = jm2.get(job.id).to_dict()
    assert revived["status"] == "success"
    assert revived["progress"]["per_tile"]["17MPU"] == [2, 2]


def test_jobs_api_roundtrip(client):
    r = client.post("/api/jobs", json={
        "type": "process_scenes",
        "title": "test run",
        "config_yaml": "workflow: scenes\n",
    })
    assert r.status_code == 201
    job_id = r.json()["id"]
    deadline = time.time() + 15
    while time.time() < deadline:
        d = client.get(f"/api/jobs/{job_id}").json()
        if d["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert d["status"] == "success"
    log = client.get(f"/api/jobs/{job_id}/log").json()
    assert any("Batch 1/2" in ln for ln in log["lines"])
    # incremental read continues from the cursor
    log2 = client.get(f"/api/jobs/{job_id}/log", params={"after": log["next"]}).json()
    assert log2["lines"] == []
    assert client.get("/api/jobs").json()[0]["id"] == job_id
    assert client.post("/api/jobs", json={"type": "nope"}).status_code == 400
