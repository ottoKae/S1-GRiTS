"""Integration tests for the opt-in burst cache wiring (roadmap item 6).

Verify that when the cache is configured, _download_to_bytes serves a second
request from disk without any HTTP, and that when it is NOT configured the
download path is unchanged (cache inert).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from s1grits import asf_io  # noqa: E402
from s1grits import burst_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache():
    burst_cache.configure(None)
    yield
    burst_cache.configure(None)


def test_cache_disabled_by_default_is_inert():
    assert not burst_cache.is_enabled()
    assert burst_cache.get("https://x/y.tif") is None
    burst_cache.put("https://x/y.tif", b"data")  # no-op, no dir
    assert burst_cache.get("https://x/y.tif") is None


def test_second_download_served_from_cache_without_http(tmp_path, monkeypatch):
    burst_cache.configure(tmp_path / "cache")
    url = "https://asf.example/T040_084852_IW1/VV.tif"
    payload = b"\x89GEOTIFF" + bytes(range(256)) * 20

    calls = {"n": 0}

    class _Resp:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_content(self, chunk_size=0):
            yield payload

    class _Session:
        def get(self, u, stream=True, timeout=None):
            calls["n"] += 1
            return _Resp()

    monkeypatch.setattr(asf_io, "_get_session", lambda: _Session())

    first = asf_io._download_to_bytes(url)
    assert first == payload
    assert calls["n"] == 1                 # one real HTTP GET

    second = asf_io._download_to_bytes(url)
    assert second == payload
    assert calls["n"] == 1                 # served from cache, no new GET

    # A different URL is a distinct entry and does hit HTTP.
    other = asf_io._download_to_bytes("https://asf.example/T018_038650_IW2/VH.tif")
    assert other == payload
    assert calls["n"] == 2


def test_configure_none_disables_after_enable(tmp_path):
    burst_cache.configure(tmp_path / "c")
    assert burst_cache.is_enabled()
    burst_cache.configure(None)
    assert not burst_cache.is_enabled()
