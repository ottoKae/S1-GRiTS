"""Multi-band COG writer robustness (the "dirty block" fix).

Writing a pixel-interleaved, compressed, tiled GeoTIFF band-by-band fails once
the image exceeds GDAL_CACHEMAX: finishing band 1 flushes (compresses) its
tiles, and writing band 2 into the same tiles forces a re-access of an already
flushed compressed block ->
``GDALRasterBand::IRasterIO -> An error occurred while writing a dirty block``.

``_write_multiband_cog_streamed`` writes ALL bands of each tile-row strip in one
call, so every tile is fully populated before it is flushed and never
revisited. These tests drive it under a deliberately tiny GDAL cache (the
condition that triggered the production failure on a 6608x5620x12 float32 COG)
and confirm a correct round trip, plus the actionable error wrapping and the
free-disk helper.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import Affine  # noqa: E402

from s1grits import workflow_scenes as ws  # noqa: E402

CRS = "EPSG:32717"
T = Affine(30.0, 0.0, 499980.0, 0.0, -30.0, 8_500_000.0)


def _profile(h, w, count, block=256):
    return {
        "driver": "GTiff", "dtype": "float32", "nodata": float("nan"),
        "width": w, "height": h, "count": count, "crs": CRS, "transform": T,
        "compress": "deflate", "tiled": True, "blockxsize": block, "blockysize": block,
    }


def _bands(h, w, count):
    rng = np.random.default_rng(7)
    arrs = []
    for b in range(count):
        a = rng.normal(-14 + b, 3, (h, w)).astype(np.float32)
        a[:5, :5] = np.nan  # keep NaN nodata in play
        arrs.append((f"B{b:02d}", a))
    return arrs


def _readers(arrs):
    return [(n, (lambda rows, a=a: np.asarray(a[rows], dtype=np.float32)))
            for n, a in arrs]


def test_streamed_multiband_roundtrip_under_tiny_gdal_cache(tmp_path, monkeypatch):
    # A tiny cache forces GDAL to flush mid-write — the exact condition that
    # makes band-sequential writes raise "dirty block". The all-bands-per-strip
    # writer must still succeed and round-trip every band.
    monkeypatch.setenv("GDAL_CACHEMAX", "1")  # 1 MB
    h, w, count = 640, 512, 8   # 8 tiled+compressed bands, several tiles tall
    arrs = _bands(h, w, count)
    out = tmp_path / "cog" / "multiband.tif"
    ws._write_multiband_cog_streamed(out, _readers(arrs), _profile(h, w, count))
    assert out.exists()
    with rasterio.open(out) as ds:
        assert (ds.width, ds.height, ds.count) == (w, h, count)
        assert ds.profile["compress"].lower() == "deflate"
        assert ds.block_shapes[0] == (256, 256)  # tiled
        assert ds.descriptions == tuple(n for n, _ in arrs)
        assert ds.overviews(1)  # internal overviews were built
        for i, (_, a) in enumerate(arrs, 1):
            np.testing.assert_array_equal(ds.read(i), a, err_msg=f"band {i}")


def test_in_ram_wrapper_matches_direct(tmp_path):
    # The in-RAM _write_multiband_cog wrapper must produce the same bytes-values
    # as the streamed core it now delegates to.
    h, w, count = 300, 400, 4
    arrs = _bands(h, w, count)
    p1 = tmp_path / "wrap.tif"
    ws._write_multiband_cog(p1, arrs, _profile(h, w, count))
    with rasterio.open(p1) as ds:
        for i, (_, a) in enumerate(arrs, 1):
            np.testing.assert_array_equal(ds.read(i), a, err_msg=f"band {i}")


def test_write_failure_raises_actionable_error(tmp_path):
    # A failure mid-write (here a reader raising, standing in for an I/O error)
    # is re-raised with band count, dimensions, path and free-disk context.
    h, w, count = 128, 128, 3

    def _boom(rows):
        raise OSError("simulated write failure")

    band_reads = [("B00", _boom)] + [
        (f"B{b:02d}", (lambda rows: np.zeros((rows.stop - rows.start, w), np.float32)))
        for b in range(1, count)
    ]
    out = tmp_path / "fail.tif"
    with pytest.raises(RuntimeError) as ei:
        ws._write_multiband_cog_streamed(out, band_reads, _profile(h, w, count))
    msg = str(ei.value)
    assert "COG export failed" in msg
    assert f"{count}-band" in msg and f"{w}x{h}" in msg
    assert "Free space" in msg


def test_free_disk_bytes(tmp_path):
    free = ws._free_disk_bytes(tmp_path / "nonexistent" / "cog.tif")
    assert isinstance(free, int) and free > 0  # resolves to an existing ancestor
