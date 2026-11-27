"""Real COG and HTTP fixtures compare range reads against complete raster reads."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import numpy as np
import pytest
import requests
import rasterio
from rasterio.io import MemoryFile
from rasterio.shutil import copy as copy_raster
from rasterio.transform import from_origin
from rasterio.warp import reproject, Resampling, transform

from s1grits.opera_spatial.reader import RangeReadError, read_asset_window, read_point_asset


@pytest.fixture
def server():
    state = {"body": b"", "calls": [], "honor_range": True, "wrong_size": False, "delay": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested = self.headers.get("Range")
            state["calls"].append(requested)
            time.sleep(state["delay"])
            body = state["body"]
            if requested and state["honor_range"]:
                start, end = map(int, requested.removeprefix("bytes=").split("-"))
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(body) + state['wrong_size']}")
                body = body[start:end + 1]
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionResetError, BrokenPipeError):
                pass

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}/asset.tif", state
    finally:
        http.shutdown()
        http.server_close()
        worker.join(timeout=5)


def make_cog(dtype="float32", compress="deflate", nodata=-9999):
    rng = np.random.default_rng(87)
    values = (rng.random((768, 768)) * 200).astype(dtype)
    values[300:306, 310:316] = nodata
    affine = from_origin(600000, 2470000, 30, 30)
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=768, height=768, count=1, dtype=dtype,
                         tiled=True, blockxsize=256, blockysize=256, nodata=nodata,
                         transform=affine, crs="EPSG:32649") as dataset:
            dataset.write(values, 1)
            dataset.scales = (2,)
            dataset.offsets = (1,)
            dataset.update_tags(PROCESSING_INFORMATION_OUTPUT_BACKSCATTER_NORMALIZATION_CONVENTION="gamma0",
                                PROCESSING_INFORMATION_OUTPUT_BACKSCATTER_EXPRESSION_CONVENTION="linear backscatter intensity")
            if dtype == "uint8":
                dataset.write_colormap(1, {i: (i, i, i, 255) for i in range(256)})
        with MemoryFile() as cog:
            copy_raster(memory.name, cog.name, driver="COG", blocksize=256, compress=compress,
                        predictor=3 if dtype == "float32" else 2)
            return cog.read()


@pytest.mark.parametrize("dtype,compress,nodata", [
    ("float32", "deflate", -9999), ("float32", "lzw", float("nan")), ("uint8", "deflate", 255),
])
def test_range_native_point_and_window_match_rasterio(server, tmp_path, dtype, compress, nodata):
    url, state = server
    state["body"] = make_cog(dtype, compress, nodata)
    asset = {"asset_id": "fixture", "url": url, "expected_bytes": len(state["body"])}
    with requests.Session() as session, MemoryFile(state["body"]) as memory, memory.open() as dataset:
        grid = {"crs": dataset.crs.to_string(), "transform": list(dataset.transform)[:6],
                "width": dataset.width, "height": dataset.height}
        window = (290, 300, 24, 27)
        result = read_asset_window(session, asset, grid, window, cache_dir=tmp_path)
        expected = dataset.read(1, window=rasterio.windows.Window(300, 290, 27, 24), masked=True)
        np.testing.assert_equal(result["values"], expected.astype("float32").filled(np.nan))
        assert result["scale"] == 2 and result["offset"] == 1
        assert result["scale_offset_applied"] is False
        assert result["tags"]["dataset"]["PROCESSING_INFORMATION_OUTPUT_BACKSCATTER_NORMALIZATION_CONVENTION"] == "gamma0"
        assert result["downloaded_bytes"] < len(state["body"])
        assert result["http_requests"] == 2
        for row, col in ((295, 315), (302, 312)):
            x, y = dataset.xy(row, col)
            lon, lat = transform(dataset.crs, 4326, [x], [y])
            point = read_point_asset(session, asset, lon[0], lat[0], cache_dir=tmp_path)
            expected_point = dataset.read(1, window=rasterio.windows.Window(col, row, 1, 1), masked=True)
            assert point["status"] == ("nodata" if expected_point.mask[0, 0] else "valid")
            assert (point["row"], point["col"]) == (row, col)
            assert point["value"] == (None if np.isnan(expected_point.data[0, 0]) else float(expected_point.data[0, 0]))


def test_range_reprojection_matches_complete_raster(server, tmp_path):
    url, state = server
    state["body"] = make_cog()
    asset = {"asset_id": "fixture", "url": url, "expected_bytes": len(state["body"])}
    with requests.Session() as session, MemoryFile(state["body"]) as memory, memory.open() as dataset:
        x, y = dataset.xy(300, 300)
        xs, ys = transform(dataset.crs, "EPSG:3857", [x], [y])
        affine = from_origin(xs[0], ys[0], 40, 40)
        grid = {"crs": "EPSG:3857", "transform": list(affine)[:6], "width": 40, "height": 30}
        result = read_asset_window(session, asset, grid, (0, 0, 30, 40), cache_dir=tmp_path)
        expected = np.full((30, 40), np.nan, dtype="float32")
        reproject(rasterio.band(dataset, 1), expected, dst_transform=affine, dst_crs=grid["crs"],
                  dst_nodata=np.nan, resampling=Resampling.nearest)
        np.testing.assert_equal(result["values"], expected)
        assert result["crs"] == "EPSG:32649"


def test_full_fetch_then_cache_has_no_network(server, tmp_path):
    url, state = server
    state["body"] = make_cog()
    asset = {"asset_id": "fixture", "url": url, "expected_bytes": len(state["body"])}
    with requests.Session() as session, MemoryFile(state["body"]) as memory, memory.open() as dataset:
        x, y = dataset.xy(300, 300)
        lon, lat = transform(dataset.crs, 4326, [x], [y])
        first = read_point_asset(session, asset, lon[0], lat[0], cache_dir=tmp_path, read_mode="burst_fetch")
        second = read_point_asset(session, asset, lon[0], lat[0], cache_dir=tmp_path, read_mode="local_cache")
        assert first["value"] == second["value"]
        assert first["downloaded_bytes"] == len(state["body"]) and first["read_mode"] == "burst_fetch"
        assert second["downloaded_bytes"] == second["http_requests"] == 0
        assert state["calls"] == [None]
        grid = {"crs": dataset.crs.to_string(), "transform": list(dataset.transform)[:6]}
        local = read_asset_window(session, asset, grid, (290, 300, 24, 27), cache_dir=tmp_path,
                                  read_mode="local_cache")
        np.testing.assert_equal(local["values"], dataset.read(1, window=rasterio.windows.Window(300, 290, 27, 24),
                                                            masked=True).filled(np.nan))
        assert local["read_mode"] == "local_cache" and local["http_requests"] == 0


def test_concurrent_points_fetch_shared_burst_once(server, tmp_path):
    url, state = server
    state.update(body=make_cog(), delay=0.1)
    asset = {"asset_id": "shared", "url": url, "expected_bytes": len(state["body"])}
    locations, expected = [], []
    with MemoryFile(state["body"]) as memory, memory.open() as dataset:
        for row, col in ((280, 285), (290, 295)):
            x, y = dataset.xy(row, col)
            lon, lat = transform(dataset.crs, 4326, [x], [y])
            locations.append((lon[0], lat[0]))
            expected.append(float(dataset.read(1, window=rasterio.windows.Window(col, row, 1, 1))[0, 0]))
    ready = threading.Barrier(2)

    def sample(location):
        with requests.Session() as session:
            ready.wait(timeout=5)
            return read_point_asset(session, asset, *location, cache_dir=tmp_path, read_mode="burst_fetch")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(sample, locations))
    assert [r["value"] for r in results] == expected
    assert sorted(r["read_mode"] for r in results) == ["burst_fetch", "local_cache"]
    assert sum(r["downloaded_bytes"] for r in results) == len(state["body"])
    assert state["calls"] == [None]
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize("setting", ["honor_range", "wrong_size"])
def test_invalid_range_is_rejected_without_consuming_body(server, tmp_path, setting):
    url, state = server
    state["body"] = b"x" * 100000
    state[setting] = setting == "wrong_size"
    asset = {"asset_id": "bad", "url": url, "expected_bytes": len(state["body"])}
    with requests.Session() as session, pytest.raises(RangeReadError) as caught:
        read_point_asset(session, asset, 112, 22, cache_dir=tmp_path)
    assert caught.value.downloaded_bytes == 0 and caught.value.http_requests == 1
