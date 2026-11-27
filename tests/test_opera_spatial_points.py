"""Exercise point increment/repair against real COGs over HTTP Range."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from shapely.geometry import Point, box, mapping

from s1grits.opera_spatial.inventory import digest
from s1grits.opera_spatial import points as point_module
from s1grits.opera_spatial.points import run_points


@pytest.fixture
def endpoint():
    state = {"bodies": {}, "blocked": set(), "calls": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["calls"].append(self.path)
            if self.path in state["blocked"]:
                self.send_error(404)
                return
            body = state["bodies"][self.path]
            request = self.headers.get("Range")
            if request:
                first, last = map(int, request[6:].split("-"))
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {first}-{last}/{len(body)}")
                body = body[first:last + 1]
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def product(endpoint, burst, acquisition, day, value=.1, *, revision=1, shift=0):
    pid = f"{burst}_{acquisition}_v{revision}"
    assets = {}
    for role, level in (("vv", value), ("vh", value / 4), ("mask", 0)):
        name = f"/{pid}_{role}.tif"
        body = cog(level, burst=burst, shift=shift)
        endpoint["bodies"][name] = body
        assets[role] = {"asset_id": digest(pid + role), "product_id": pid, "role": role,
                        "url": endpoint["url"] + name, "expected_bytes": len(body)}
    extent = .02 if burst == "A" else .015
    return {"product_id": pid, "burst_id": burst, "acquisition_id": acquisition,
            "acquisition_identity": "verified", "acquisition_utc": day + "T00:00:00Z",
            "direction": "ASCENDING", "relative_orbit": 84, "platform": "S1A",
            "processing_version": "1", "product_version": "1",
            "processing_date": f"2026-11-{revision:02d}T00:00:00Z",
            "geometry": mapping(box(-extent, -extent, extent, extent)), "assets": assets}


def cog(value, *, burst="A", shift=0):
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=256, height=256, count=1, dtype="float32",
                         crs="EPSG:4326", transform=from_origin(-.01 + shift, .01, .0001, .0001),
                         tiled=True, blockxsize=128, blockysize=128, compress="deflate", nodata=-9999) as ds:
            ds.write(np.full((256, 256), value, dtype=np.float32), 1)
            ds.update_tags(BURST_ID=burst, TRACK_NUMBER="84", ORBIT_PASS_DIRECTION="ASCENDING", PLATFORM="S1A",
                           PROCESSING_INFORMATION_OUTPUT_BACKSCATTER_NORMALIZATION_CONVENTION="gamma0",
                           PROCESSING_INFORMATION_OUTPUT_BACKSCATTER_EXPRESSION_CONVENTION="linear backscatter intensity")
        return memory.read()


def inputs(strategy="S0"):
    features = gpd.GeoDataFrame({"point_id": ["point"], "spatial_mode": ["points"]},
                               geometry=[Point(0, 0)], crs=4326)
    return {"spatial": {"mode": "points"}, "source_strategy": strategy, "workers": 2}, features


def selected(path):
    return pd.concat([pd.read_parquet(p) for p in sorted(path.glob("points/*/selected/*.parquet"))],
                     ignore_index=True).sort_values(["acquisition_utc", "acquisition_id"]).reset_index(drop=True)


def test_increment_equals_full_and_repeat_has_no_requests(tmp_path, endpoint):
    config, features = inputs()
    first = product(endpoint, "A", "e1", "2024-12-20")
    second = product(endpoint, "A", "e2", "2025-01-02", .2)
    output = tmp_path / "increment"
    result = run_points(config, features, [first], output)
    assert result["status"] == "complete" and result["http_requests"] > 0
    before = selected(output).copy()
    manifest = json.loads((output / "manifest.json").read_text())
    result = run_points(config, features, [second], output)
    assert result["products_processed"] == 1 and result["selected_rows"] == 2
    after = selected(output)
    pd.testing.assert_frame_equal(before, after.iloc[:1].reset_index(drop=True))
    current = json.loads((output / "manifest.json").read_text())
    assert manifest["point_owners"] == current["point_owners"]
    assert manifest["groups"] == current["groups"]
    calls = len(endpoint["calls"])
    again = run_points(config, features, [first, second], output)
    assert again["products_processed"] == 0 and again["http_requests"] == 0
    assert len(endpoint["calls"]) == calls
    run_points(config, features, [first, second], tmp_path / "full")
    pd.testing.assert_frame_equal(selected(output), selected(tmp_path / "full"))
    assert after.vv_db.iloc[0] == pytest.approx(-10)
    assert len(pd.read_parquet(output / "products.parquet")) == 2


def test_s1_late_owner_restores_source_only_in_repair(tmp_path, endpoint):
    config, features = inputs("S1")
    a1 = product(endpoint, "A", "e1", "2025-01-01", .1)
    b1 = product(endpoint, "B", "e1", "2025-01-01", .4)
    b2 = product(endpoint, "B", "e2", "2025-02-01", .5)
    output = tmp_path / "s1"
    run_points(config, features, [a1, b1, b2], output)
    before = selected(output)
    assert before.burst_id.tolist() == ["A", "B"]
    assert before.fallback.tolist() == [False, True]
    a2 = product(endpoint, "A", "e2", "2025-02-01", .2)
    calls = len(endpoint["calls"])
    pending = run_points(config, features, [a2], output)
    assert pending["status"] == "repair_required" and pending["repair_required"] == [a2["product_id"]]
    assert len(endpoint["calls"]) == calls
    pd.testing.assert_frame_equal(before, selected(output))
    done = run_points(config, features, [a2], output, repair=True)
    assert done["status"] == "complete"
    assert selected(output).burst_id.tolist() == ["A", "A"]
    assert selected(output).fallback.tolist() == [False, False]
    summary = pd.read_parquet(output / "point_catalog.parquet").iloc[0]
    assert summary.source_switch_event_count == 0 and summary.fallback_observation_count == 0


def test_read_failure_is_not_s1_fallback_and_can_be_repaired(tmp_path, endpoint):
    config, features = inputs("S1")
    a = product(endpoint, "A", "e1", "2025-01-01")
    b = product(endpoint, "B", "e1", "2025-01-01", .4)
    bad = a["assets"]["vv"]["url"].removeprefix(endpoint["url"])
    endpoint["blocked"].add(bad)
    output = tmp_path / "failure"
    failed = run_points(config, features, [a, b], output)
    assert failed["status"] == "partial" and failed["failed_assets"] == 1
    row = selected(output).iloc[0]
    assert row.status == "read_failed" and row.source_index == 0 and not row.fallback
    calls = len(endpoint["calls"])
    repeated = run_points(config, features, [a, b], output)
    assert repeated["status"] == "repair_required" and len(endpoint["calls"]) == calls
    endpoint["blocked"].clear()
    repaired = run_points(config, features, [a], output, repair=True)
    assert repaired["status"] == "complete"
    assert selected(output).iloc[0].burst_id == "A"
    assert len(endpoint["calls"]) == calls + 1


def test_historical_insertion_and_product_revision_do_not_duplicate_acquisition(tmp_path, endpoint):
    config, features = inputs()
    early = product(endpoint, "A", "e1", "2025-01-01")
    last = product(endpoint, "A", "e3", "2025-03-01", .3)
    middle = product(endpoint, "A", "e2", "2025-02-01", .2)
    output = tmp_path / "history"
    run_points(config, features, [early, last], output)
    assert run_points(config, features, [middle], output)["status"] == "repair_required"
    run_points(config, features, [middle], output, repair=True)
    assert selected(output).acquisition_id.tolist() == ["e1", "e2", "e3"]
    revision = product(endpoint, "A", "e2", "2025-02-01", .25, revision=2)
    assert run_points(config, features, [revision], output)["status"] == "repair_required"
    run_points(config, features, [revision], output, repair=True)
    frame = selected(output)
    assert len(frame) == 3 and frame.vv.iloc[1] == pytest.approx(.25)
    assert len(pd.read_parquet(output / "products.parquet")) == 4


def test_native_grid_change_and_unknown_burst_do_not_silently_change_owner(tmp_path, endpoint):
    config, features = inputs()
    a = product(endpoint, "A", "e1", "2025-01-01")
    output = tmp_path / "grid"
    run_points(config, features, [a], output)
    prior = json.loads((output / "manifest.json").read_text())
    shifted = product(endpoint, "A", "e2", "2025-02-01", shift=.00001)
    result = run_points(config, features, [shifted], output)
    assert result["status"] == "partial"
    assert selected(output).status.tolist() == ["valid", "read_failed"]
    current = json.loads((output / "manifest.json").read_text())
    assert prior["point_grids"] == current["point_grids"]
    assert prior["point_owners"] == current["point_owners"]
    cached_invalid = run_points(config, features, [shifted], output, repair=True)
    assert cached_invalid["status"] == "partial" and cached_invalid["http_requests"] == 0
    assert selected(output).status.tolist() == ["valid", "read_failed"]
    b = product(endpoint, "B", "e3", "2025-03-01")
    with pytest.raises(ValueError, match="frozen candidate"):
        run_points(config, features, [b], output)
    with pytest.raises(ValueError, match="product identity changed"):
        run_points(config, features, [dict(a, acquisition_id="other")], output, repair=True)


def test_s0_keeps_gap_and_lia_tracks_selected_source(tmp_path, endpoint):
    config, features = inputs()
    a1 = product(endpoint, "A", "e1", "2025-01-01")
    b1 = product(endpoint, "B", "e1", "2025-01-01", .4)
    b2 = product(endpoint, "B", "e2", "2025-02-01", .4)
    static = []
    for burst, value in (("A", 55), ("B", 57)):
        name = "/" + burst + "_lia.tif"
        body = cog(value, burst=burst)
        endpoint["bodies"][name] = body
        asset = {"asset_id": digest(name), "product_id": "static_" + burst, "role": "lia",
                 "url": endpoint["url"] + name, "expected_bytes": len(body)}
        static.append({"product_id": "static_" + burst, "burst_id": burst, "assets": {"lia": asset}})
    output = tmp_path / "s0"
    run_points(config, features, [a1, b1, b2], output, static_products=static)
    frame = selected(output)
    assert frame.status.tolist() == ["valid", "no_source"]
    assert frame.owner_nominal_lia.tolist() == [55, 55]
    config["source_strategy"] = "S1"
    output = tmp_path / "s1"
    run_points(config, features, [a1, b1, b2], output, static_products=static)
    assert selected(output).delta_nominal_lia_to_owner.tolist() == [0, 2]


def test_direct_api_checks_budget_before_receipts_or_transfer(tmp_path, endpoint):
    config, features = inputs()
    config["read_mode"] = "burst_fetch"
    item = product(endpoint, "A", "e1", "2025-01-01")
    item["assets"]["vv"]["expected_bytes"] = 50_000_000_001
    output = tmp_path / "over-budget"
    with pytest.raises(ValueError, match="50 GB"):
        run_points(config, features, [item], output)
    assert endpoint["calls"] == []
    assert not (output / "manifest.json").exists()
    assert not (output / "assets").exists()


def test_budget_includes_previously_committed_assets(tmp_path, endpoint):
    config, features = inputs()
    first = product(endpoint, "A", "e1", "2025-01-01")
    output = tmp_path / "full-job-budget"
    run_points(config, features, [first], output)
    second = product(endpoint, "A", "e2", "2025-02-01")
    prior_bytes = sum(asset["expected_bytes"] for asset in first["assets"].values())
    other_bytes = sum(asset["expected_bytes"] for role, asset in second["assets"].items() if role != "vv")
    second["assets"]["vv"]["expected_bytes"] = 50_000_000_000 - other_bytes - prior_bytes // 2
    config["read_mode"] = "burst_fetch"
    calls = len(endpoint["calls"])
    with pytest.raises(ValueError, match="50 GB"):
        run_points(config, features, [second], output)
    assert len(endpoint["calls"]) == calls
    assert selected(output).acquisition_id.tolist() == ["e1"]


def test_pending_year_requires_replay_and_recovers_cached_assets(tmp_path, endpoint, monkeypatch):
    config, features = inputs()
    first = product(endpoint, "A", "e1", "2025-01-01")
    second = product(endpoint, "A", "e2", "2025-02-01", .2)
    third = product(endpoint, "A", "e3", "2025-03-01", .3)
    output = tmp_path / "interrupted"
    run_points(config, features, [first], output)
    original = point_module.atomic_write_parquet

    def fail_after_write(frame, path):
        original(frame, path)
        if "observations" in path.parts:
            raise OSError("Injected failure after completed raw partition write")

    monkeypatch.setattr(point_module, "atomic_write_parquet", fail_after_write)
    with pytest.raises(OSError, match="Injected failure"):
        run_points(config, features, [second], output)
    pending = json.loads((output / "manifest.json").read_text())
    assert pending["status"] == "running" and pending["pending_points"]["product_ids"] == [second["product_id"]]
    assert len(pending["products"]) == 1
    monkeypatch.setattr(point_module, "atomic_write_parquet", original)
    calls = len(endpoint["calls"])
    with pytest.raises(ValueError, match="unpublished"):
        run_points(config, features, [third], output)
    repaired = run_points(config, features, [second], output)
    assert repaired["status"] == "complete" and repaired["http_requests"] == 0
    assert len(endpoint["calls"]) == calls
    assert selected(output).acquisition_id.tolist() == ["e1", "e2"]
    completed = json.loads((output / "manifest.json").read_text())
    assert "pending_points" not in completed and len(completed["products"]) == 2


def test_output_protection_precedes_lock_creation(tmp_path, endpoint):
    config, features = inputs()
    protected = tmp_path / "production"
    protected.mkdir()
    config["protected_roots"] = [str(protected)]
    item = product(endpoint, "A", "e1", "2025-01-01")
    output = protected / "new-output"
    with pytest.raises(ValueError, match="protected"):
        run_points(config, features, [item], output)
    assert not output.exists() and endpoint["calls"] == []


def test_repair_does_not_retry_static_assets_of_unrequested_group(tmp_path, endpoint):
    config, features = inputs()
    a = product(endpoint, "A", "e1", "2025-01-01")
    b = product(endpoint, "B", "e2", "2025-01-01")
    b["processing_version"] = "2"
    static = []
    for burst in ("A", "B"):
        name = "/" + burst + "_static.tif"
        body = cog(55, burst=burst)
        endpoint["bodies"][name] = body
        asset = {"asset_id": digest(name), "product_id": "static_" + burst, "role": "lia",
                 "url": endpoint["url"] + name, "expected_bytes": len(body)}
        static.append({"product_id": "static_" + burst, "burst_id": burst, "assets": {"lia": asset}})
    endpoint["blocked"].add("/B_static.tif")
    output = tmp_path / "scoped-repair"
    run_points(config, features, [a, b], output, static_products=static)
    calls = len(endpoint["calls"])
    endpoint["blocked"].clear()
    run_points(config, features, [a], output, repair=True)
    assert len(endpoint["calls"]) == calls
    frame = selected(output)
    assert pd.isna(frame.loc[frame.burst_id == "B", "selected_nominal_lia"]).all()


def test_failed_product_revision_preserves_committed_pair_until_repair(tmp_path, endpoint):
    config, features = inputs()
    initial = product(endpoint, "A", "e1", "2025-01-01", .1)
    revision = product(endpoint, "A", "e1", "2025-01-01", .2, revision=2)
    output = tmp_path / "failed-revision"
    run_points(config, features, [initial], output)
    original = selected(output)
    bad = revision["assets"]["vv"]["url"].removeprefix(endpoint["url"])
    endpoint["blocked"].add(bad)
    failed = run_points(config, features, [revision], output, repair=True)
    assert failed["status"] == "partial" and len(failed["retained_after_failed_repair"]) == 1
    pd.testing.assert_frame_equal(original, selected(output))
    products = pd.read_parquet(output / "products.parquet").set_index("product_id")
    assert products.loc[revision["product_id"], "source_status"] == "read_failed"
    later = product(endpoint, "A", "e2", "2025-02-01", .3)
    still_pending = run_points(config, features, [later], output)
    assert still_pending["status"] == "partial"
    pd.testing.assert_frame_equal(original, selected(output).iloc[:1].reset_index(drop=True))
    endpoint["blocked"].clear()
    recovered = run_points(config, features, [revision], output, repair=True)
    assert recovered["status"] == "complete" and recovered["retained_after_failed_repair"] == []
    frame = selected(output)
    assert len(frame) == 2 and frame.product_id.iloc[0] == revision["product_id"]
    assert frame.vv.iloc[0] == pytest.approx(.2) and frame.vh.iloc[0] == pytest.approx(.05)
    confirmed_nodata = product(endpoint, "A", "e1", "2025-01-01", -9999, revision=3)
    result = run_points(config, features, [confirmed_nodata], output, repair=True)
    assert result["status"] == "complete"
    assert selected(output).status.iloc[0] == "invalid"
