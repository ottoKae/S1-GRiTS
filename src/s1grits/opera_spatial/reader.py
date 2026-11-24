"""Read OPERA COG pixels and windows without invoking MGRS production.

Byte-range verification is promoted from the 2026-11-11 OPERA point pilot.
"""
from __future__ import annotations

from contextlib import nullcontext
import io
import math
from pathlib import Path
import re
import time
import threading

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import Affine, array_bounds
from rasterio.warp import reproject, Resampling, transform, transform_bounds
from rasterio.windows import Window, from_bounds
import tifffile


_BURST_FETCH_LOCK = threading.Lock()


class RangeReadError(RuntimeError):
    """An endpoint or raster could not supply a verified sample."""


class RangeReader(io.RawIOBase):
    """Seekable TIFF access retaining byte ranges within one asset read."""

    def __init__(self, session, url, size, timeout):
        self.session, self.url, self.size, self.timeout = session, url, int(size), timeout
        self.position = self.http_requests = self.http_redirects = self.downloaded_bytes = 0
        self.ranges = []
        self.name = "remote.tif"
        if self.size <= 0:
            raise ValueError("expected_bytes must be positive")
        self.fetch(0, min(65536, self.size))

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        self.position = (offset if whence == io.SEEK_SET else
                         self.position + offset if whence == io.SEEK_CUR else self.size + offset)
        if self.position < 0:
            raise ValueError("negative seek")
        return self.position

    def read(self, size=-1):
        end = self.size if size < 0 else min(self.size, self.position + size)
        if end <= self.position:
            return b""
        body = self.fetch(self.position, end)
        self.position = end
        return body

    def readinto(self, buffer):
        target = memoryview(buffer).cast("B")
        body = self.read(len(target))
        target[:len(body)] = body
        return len(body)

    def fetch(self, start, end):
        for lower, upper, body in self.ranges:
            if lower <= start and end <= upper:
                return body[start - lower:end - lower]
        self.http_requests += 1
        try:
            with self.session.get(self.url,
                                  headers={"Range": f"bytes={start}-{end - 1}", "Accept-Encoding": "identity"},
                                  timeout=self.timeout, stream=True) as response:
                self.http_redirects += len(response.history)
                if response.status_code != 206:
                    raise RangeReadError(f"HTTP_{response.status_code}: byte range not honored")
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not match or tuple(map(int, match.groups())) != (start, end - 1, self.size):
                    raise RangeReadError("Content-Range differs from requested bytes or provider size")
                body = response.raw.read(end - start + 1, decode_content=False)
                self.downloaded_bytes += len(body)
                if len(body) != end - start:
                    raise RangeReadError("range body length differs from Content-Range")
                self.url = response.url
        except Exception as exc:
            error = exc if isinstance(exc, RangeReadError) else RangeReadError("range request failed: " + type(exc).__name__)
            _error_counters(error, self)
            raise error from None
        self.ranges.append((start, end, body))
        return body


def sample_remote_point(session, url, expected_bytes, longitude, latitude, *, timeout=(15, 45)):
    """Return raw containing-pixel value and metadata without applying scale/offset."""
    started = time.monotonic()
    reader = RangeReader(session, url, expected_bytes, timeout)
    result = {"read_mode": "range", "value": None}
    try:
        with tifffile.TiffFile(reader) as tiff:
            page = tiff.pages[0]
            metadata = _range_metadata(reader, page)
            affine = Affine(*metadata["transform"])
            xs, ys = transform("EPSG:4326", metadata["crs"], [longitude], [latitude])
            col, row = (~affine) * (xs[0], ys[0])
            row, col = math.floor(row), math.floor(col)
            centre_x, centre_y = affine * (col + .5, row + .5)
            result.update(metadata, row=row, col=col, centre_x=centre_x, centre_y=centre_y,
                          sampling_method="native_containing_pixel")
            if row < 0 or col < 0 or row >= page.imagelength or col >= page.imagewidth:
                result["status"] = "outside"
            else:
                columns = math.ceil(page.imagewidth / page.tilewidth)
                tile = (row // page.tilelength) * columns + col // page.tilewidth
                decoded = _decode_tile(reader, page, tile)
                if decoded is None:
                    result["status"] = "nodata"
                else:
                    raw = float(decoded[row % page.tilelength, col % page.tilewidth])
                    nodata = metadata["nodata"]
                    result["value"] = raw if math.isfinite(raw) else None
                    result["status"] = ("nodata" if (nodata is not None and raw == nodata) or
                                        (metadata["nodata_is_nan"] and math.isnan(raw)) else
                                        "nonfinite" if not math.isfinite(raw) else "valid")
    except Exception as exc:
        error = exc if isinstance(exc, RangeReadError) else RangeReadError("point decode failed: " + type(exc).__name__)
        _error_counters(error, reader)
        raise error from None
    result.update(_counters(reader), elapsed_seconds=time.monotonic() - started)
    return result


def read_point_asset(session, asset, longitude, latitude, *, cache_dir, read_mode="range", timeout=(15, 45)):
    """Sample the native containing pixel using a remote range or complete burst."""
    if read_mode == "range":
        return sample_remote_point(session, asset["url"], asset["expected_bytes"], longitude, latitude,
                                   timeout=timeout)
    started = time.monotonic()
    path, actual_mode, counters = _local_asset(session, asset, cache_dir, read_mode, timeout)
    try:
        with rasterio.open(path) as dataset:
            metadata = _dataset_metadata(dataset)
            xs, ys = transform("EPSG:4326", dataset.crs, [longitude], [latitude])
            row, col = dataset.index(xs[0], ys[0])
            centre_x, centre_y = dataset.xy(row, col)
            value, status = None, "outside"
            if 0 <= row < dataset.height and 0 <= col < dataset.width:
                raw = float(dataset.read(1, window=Window(col, row, 1, 1))[0, 0])
                value = raw if math.isfinite(raw) else None
                status = ("nodata" if (dataset.nodata is not None and raw == dataset.nodata) or
                          (metadata["nodata_is_nan"] and math.isnan(raw)) else
                          "nonfinite" if not math.isfinite(raw) else "valid")
    except Exception as exc:
        error = exc if isinstance(exc, RangeReadError) else RangeReadError("point decode failed: " + type(exc).__name__)
        for name, counter in counters.items():
            setattr(error, name, counter)
        raise error from None
    return dict(metadata, row=int(row), col=int(col), centre_x=centre_x, centre_y=centre_y,
                sampling_method="native_containing_pixel", value=value, status=status,
                read_mode=actual_mode, **counters, elapsed_seconds=time.monotonic() - started)


def read_asset_window(session, asset, grid, window, *, cache_dir, read_mode="range", timeout=(15, 45)):
    """Return raw float32 samples nearest-warped onto one target window.

    NoData is represented by NaN. The caller applies declared scale/offset once.
    """
    started = time.monotonic()
    row, col, height, width = map(int, window)
    target = Affine(*grid["transform"]) * Affine.translation(col, row)
    reader = None
    counters = {"http_requests": 0, "http_redirects": 0, "downloaded_bytes": 0}
    try:
        if read_mode == "range":
            reader = RangeReader(session, asset["url"], asset["expected_bytes"], timeout)
            with tifffile.TiffFile(reader) as tiff:
                page = tiff.pages[0]
                metadata = _range_metadata(reader, page)
                source_window = _native_window(metadata, grid["crs"], target, height, width)
                native = _range_window(reader, page, source_window)
            counters = _counters(reader)
        elif read_mode in {"burst_fetch", "local_cache"}:
            path, read_mode, counters = _local_asset(session, asset, cache_dir, read_mode, timeout)
            with rasterio.open(path) as dataset:
                metadata = _dataset_metadata(dataset)
                source_window = _native_window(metadata, grid["crs"], target, height, width)
                native = (dataset.read(1, window=source_window).astype("float32")
                          if source_window.width and source_window.height else np.empty((0, 0), "float32"))
        else:
            raise ValueError("read_mode must be range, burst_fetch or local_cache")
        if metadata["nodata"] is not None:
            native[native == metadata["nodata"]] = np.nan
        native[~np.isfinite(native)] = np.nan
        values = np.full((height, width), np.nan, dtype="float32")
        if native.size:
            source_transform = Affine(*metadata["transform"]) * Affine.translation(
                source_window.col_off, source_window.row_off)
            reproject(native, values, src_transform=source_transform, src_crs=metadata["crs"],
                      src_nodata=np.nan, dst_transform=target, dst_crs=grid["crs"],
                      dst_nodata=np.nan, resampling=Resampling.nearest)
    except Exception as exc:
        error = exc if isinstance(exc, RangeReadError) else RangeReadError("window read failed: " + type(exc).__name__)
        if reader is not None:
            counters = _counters(reader)
        else:
            counters = {name: getattr(exc, name, value) for name, value in counters.items()}
        for name, value in counters.items():
            setattr(error, name, value)
        raise error from None
    return dict(metadata, values=values, status="valid" if np.isfinite(values).any() else "nodata",
                read_mode=read_mode, **counters, elapsed_seconds=time.monotonic() - started,
                sampling_method="nearest_target_grid", source_window=list(source_window.flatten()))


def _dataset_metadata(dataset):
    if dataset.crs is None or dataset.count != 1:
        raise RangeReadError("expected a georeferenced single-band raster")
    nodata = dataset.nodata
    return {"crs": dataset.crs.to_string(), "transform": list(dataset.transform)[:6],
            "width": dataset.width, "height": dataset.height, "dtype": dataset.dtypes[0],
            "nodata": nodata if nodata is None or math.isfinite(nodata) else None,
            "nodata_is_nan": bool(nodata is not None and math.isnan(nodata)),
            "scale": dataset.scales[0], "offset": dataset.offsets[0], "unit": dataset.units[0],
            "tags": {"dataset": dataset.tags(), "band": dataset.tags(1)}, "scale_offset_applied": False}


def _local_asset(session, asset, cache_dir, read_mode, timeout):
    if read_mode not in {"burst_fetch", "local_cache"}:
        raise ValueError("read_mode must be range, burst_fetch or local_cache")
    counters = {"http_requests": 0, "http_redirects": 0, "downloaded_bytes": 0}
    path = Path(asset.get("local_path") or Path(cache_dir) / (asset["asset_id"] + ".tif"))
    # shortcut: Full-burst fetches serialize; use per-asset locks if this mode becomes throughput-critical.
    with _BURST_FETCH_LOCK if read_mode == "burst_fetch" else nullcontext():
        try:
            if path.exists():
                read_mode = "local_cache"
            else:
                if read_mode == "local_cache":
                    raise RangeReadError("local_cache asset is absent")
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".partial")
                try:
                    counters["http_requests"] = 1
                    with session.get(asset["url"], stream=True, timeout=timeout,
                                     headers={"Accept-Encoding": "identity"}) as response:
                        counters["http_redirects"] = len(response.history)
                        if response.status_code != 200:
                            raise RangeReadError(f"HTTP_{response.status_code}: burst fetch failed")
                        with temporary.open("wb") as output:
                            for body in response.iter_content(1024 * 1024):
                                counters["downloaded_bytes"] += len(body)
                                if counters["downloaded_bytes"] > asset["expected_bytes"]:
                                    raise RangeReadError("burst body exceeds provider size")
                                output.write(body)
                    if temporary.stat().st_size != asset["expected_bytes"]:
                        raise RangeReadError("burst body length differs from provider size")
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            if path.stat().st_size != asset["expected_bytes"]:
                raise RangeReadError("cached burst size differs from provider size")
        except Exception as exc:
            error = exc if isinstance(exc, RangeReadError) else RangeReadError("burst read failed: " + type(exc).__name__)
            for name, value in counters.items():
                setattr(error, name, value)
            raise error from None
        return path, read_mode, counters


def _range_metadata(reader, page):
    if not page.is_tiled or page.samplesperpixel != 1 or page.imagedepth != 1:
        raise RangeReadError("expected a tiled, single-band OPERA COG")
    for tag in page.tags.values():
        _ = tag.value
    # Rasterio needs all TIFF metadata, including tags outside the first 64 KiB.
    header = bytearray(max(upper for _, upper, _ in reader.ranges))
    for lower, upper, body in reader.ranges:
        header[lower:upper] = body
    with MemoryFile(bytes(header)) as memory, memory.open() as dataset:
        return _dataset_metadata(dataset)


def _native_window(metadata, target_crs, target, height, width):
    bounds = transform_bounds(target_crs, metadata["crs"], *array_bounds(height, width, target), densify_pts=21)
    source = from_bounds(*bounds, transform=Affine(*metadata["transform"]))
    # A source-pixel halo covers nearest-neighbour selection at transformed edges.
    left = min(metadata["width"], max(0, math.floor(source.col_off) - 1))
    top = min(metadata["height"], max(0, math.floor(source.row_off) - 1))
    right = min(metadata["width"], max(0, math.ceil(source.col_off + source.width) + 1))
    bottom = min(metadata["height"], max(0, math.ceil(source.row_off + source.height) + 1))
    return Window(left, top, max(0, right - left), max(0, bottom - top))


def _range_window(reader, page, window):
    height, width = int(window.height), int(window.width)
    native = np.full((height, width), np.nan, dtype="float32")
    if not height or not width:
        return native
    top, left = int(window.row_off), int(window.col_off)
    columns = math.ceil(page.imagewidth / page.tilewidth)
    for row in range(top // page.tilelength, math.ceil((top + height) / page.tilelength)):
        for col in range(left // page.tilewidth, math.ceil((left + width) / page.tilewidth)):
            decoded = _decode_tile(reader, page, row * columns + col)
            if decoded is None:
                continue
            y, x = row * page.tilelength, col * page.tilewidth
            a, b = max(top, y), min(top + height, y + decoded.shape[0])
            c, d = max(left, x), min(left + width, x + decoded.shape[1])
            native[a - top:b - top, c - left:d - left] = decoded[a - y:b - y, c - x:d - x]
    return native


def _decode_tile(reader, page, tile):
    offset, count = int(page.dataoffsets[tile]), int(page.databytecounts[tile])
    decoded, _, _ = page.decode(reader.fetch(offset, offset + count) if count else None, tile)
    return None if decoded is None else decoded[0, :, :, 0]


def _counters(reader):
    return {name: getattr(reader, name) for name in ("http_requests", "http_redirects", "downloaded_bytes")}


def _error_counters(error, reader):
    for name, value in _counters(reader).items():
        setattr(error, name, value)
