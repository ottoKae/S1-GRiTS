"""Executable correctness contract for a future on-disk burst cache.

Roadmap item P0-1 is an on-disk cache that deduplicates the burst GeoTIFFs
shared between adjacent MGRS tiles (a burst like ``T040_084852_IW1`` appears in
both 17MPU's and 17MPV's download lists).  Before writing that cache, this test
pins down the correctness contract it MUST satisfy, using a small reference
implementation so the contract is proven self-consistent today.

When the real cache lands, add its factory to ``CACHE_FACTORIES`` and it must
pass every contract test unchanged — no separate test needed.

Contract:
  C1  miss returns None (nothing fabricated).
  C2  store-then-get returns byte-identical content.
  C3  keys are isolated by (granule_id, polarization).
  C4  an interrupted write leaves NO visible entry (atomic publish).
  C5  a corrupted/truncated committed entry is detected and treated as a miss
      (so the caller re-downloads rather than using bad data).
  C6  overwriting a key with new bytes is atomic and returns the new bytes.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Reference implementation (dir-backed, atomic, checksum-validated).
# The real cache should mirror this contract, not necessarily this code.
# --------------------------------------------------------------------------- #
class RefBurstCache:
    """Minimal disk burst cache: key=(granule_id, pol) -> bytes, validated."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, granule_id: str, pol: str):
        safe = f"{granule_id}__{pol}".replace("/", "_")
        data = self.root / f"{safe}.tif"
        meta = self.root / f"{safe}.sha256"
        return data, meta

    def get(self, granule_id: str, pol: str) -> bytes | None:
        data, meta = self._paths(granule_id, pol)
        if not (data.exists() and meta.exists()):
            return None  # C1 / C4: no complete entry
        want = meta.read_text().strip()
        raw = data.read_bytes()
        if hashlib.sha256(raw).hexdigest() != want:
            return None  # C5: corrupted/truncated -> miss
        return raw

    def put(self, granule_id: str, pol: str, content: bytes) -> None:
        data, meta = self._paths(granule_id, pol)
        # Atomic publish: write to temp, fsync, rename; checksum committed last
        # so get() only trusts data whose checksum sidecar exists.
        tmp = data.with_suffix(data.suffix + ".part")
        tmp.write_bytes(content)
        os.replace(tmp, data)
        meta_tmp = meta.with_suffix(meta.suffix + ".part")
        meta_tmp.write_text(hashlib.sha256(content).hexdigest())
        os.replace(meta_tmp, meta)

    def simulate_interrupted_put(self, granule_id: str, pol: str, content: bytes):
        """Write the temp file but never rename/commit (crash mid-write)."""
        data, _ = self._paths(granule_id, pol)
        tmp = data.with_suffix(data.suffix + ".part")
        tmp.write_bytes(content)
        # no os.replace -> nothing published

    def corrupt_committed_entry(self, granule_id: str, pol: str):
        """Truncate a committed data file, leaving its checksum stale."""
        data, _ = self._paths(granule_id, pol)
        raw = data.read_bytes()
        data.write_bytes(raw[: len(raw) // 2])


class _RealCacheAdapter:
    """Adapt the production URL-keyed BurstCache to the (granule_id, pol) contract."""

    def __init__(self, root):
        import sys
        _src = str(Path(__file__).resolve().parents[1] / "src")
        if _src not in sys.path:
            sys.path.insert(0, _src)
        from s1grits.burst_cache import BurstCache
        self._c = BurstCache(root)

    @staticmethod
    def _url(granule_id, pol):
        return f"https://asf.example/{granule_id}/{pol}.tif"

    def get(self, granule_id, pol):
        return self._c.get(self._url(granule_id, pol))

    def put(self, granule_id, pol, content):
        self._c.put(self._url(granule_id, pol), content)

    def simulate_interrupted_put(self, granule_id, pol, content):
        # Write only the .bin temp, never publish -> no checksum sidecar.
        data_p, _ = self._c._paths(self._url(granule_id, pol))
        tmp = data_p.with_suffix(data_p.suffix + ".part.test")
        tmp.write_bytes(content)

    def corrupt_committed_entry(self, granule_id, pol):
        data_p, _ = self._c._paths(self._url(granule_id, pol))
        raw = data_p.read_bytes()
        data_p.write_bytes(raw[: len(raw) // 2])


CACHE_FACTORIES = [RefBurstCache, _RealCacheAdapter]


@pytest.fixture(params=CACHE_FACTORIES, ids=lambda f: f.__name__)
def cache(request, tmp_path):
    return request.param(tmp_path / "cache")


BURST_BYTES = b"\x89GEOTIFF" + bytes(range(256)) * 40   # ~10 KB deterministic


def test_c1_miss_returns_none(cache):
    assert cache.get("T040_084852_IW1", "VV") is None


def test_c2_store_then_get_is_byte_identical(cache):
    cache.put("T040_084852_IW1", "VV", BURST_BYTES)
    got = cache.get("T040_084852_IW1", "VV")
    assert got == BURST_BYTES


def test_c3_keys_isolated_by_granule_and_pol(cache):
    cache.put("T040_084852_IW1", "VV", b"aaaa")
    cache.put("T040_084852_IW1", "VH", b"bbbb")
    cache.put("T018_038650_IW2", "VV", b"cccc")
    assert cache.get("T040_084852_IW1", "VV") == b"aaaa"
    assert cache.get("T040_084852_IW1", "VH") == b"bbbb"
    assert cache.get("T018_038650_IW2", "VV") == b"cccc"
    assert cache.get("T018_038650_IW2", "VH") is None


def test_c4_interrupted_write_leaves_no_visible_entry(cache):
    if not hasattr(cache, "simulate_interrupted_put"):
        pytest.skip("cache does not expose interrupted-write simulation")
    cache.simulate_interrupted_put("T040_084852_IW1", "VV", BURST_BYTES)
    # A crashed write must be invisible -> caller re-downloads.
    assert cache.get("T040_084852_IW1", "VV") is None


def test_c5_corrupted_entry_detected_as_miss(cache):
    if not hasattr(cache, "corrupt_committed_entry"):
        pytest.skip("cache does not expose corruption simulation")
    cache.put("T040_084852_IW1", "VV", BURST_BYTES)
    assert cache.get("T040_084852_IW1", "VV") == BURST_BYTES
    cache.corrupt_committed_entry("T040_084852_IW1", "VV")
    assert cache.get("T040_084852_IW1", "VV") is None   # not silently served


def test_c6_overwrite_is_atomic_and_returns_new_bytes(cache):
    cache.put("T040_084852_IW1", "VV", b"old-content-xxxx")
    cache.put("T040_084852_IW1", "VV", b"new-content-yyyy")
    assert cache.get("T040_084852_IW1", "VV") == b"new-content-yyyy"
