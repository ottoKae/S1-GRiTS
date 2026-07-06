"""Opt-in on-disk cache for downloaded burst GeoTIFF bytes (roadmap item 6).

Adjacent MGRS tiles share many of the same OPERA burst products (e.g. burst
``T040_084852_IW1`` appears in both 17MPU's and 17MPV's download lists), so a
multi-tile run re-downloads the shared bursts once per tile. This cache
deduplicates them: the first download stores the raw bytes keyed by URL, and
every later request (this run or a re-run) reads from disk instead of ASF.

The cache is **opt-in**: it is only active when ``configure(dir)`` has been
called (driven by ``memory.burst_cache_dir`` in config). When unconfigured,
``get``/``put`` are inert and the download path is byte-for-byte unchanged.

Correctness contract (validated by tests/test_burst_cache_contract.py):
  - a miss returns None;
  - a stored entry reads back byte-identical;
  - keys are isolated (distinct URLs -> distinct entries);
  - an interrupted write leaves no visible entry (atomic publish via
    temp-file + os.replace, checksum sidecar committed last);
  - a corrupted/truncated committed entry is detected (sha256 mismatch) and
    treated as a miss so the caller re-downloads;
  - overwriting a key is atomic.

Concurrency: multiple tile worker processes share one cache directory. Writes
are atomic (temp + rename) and the checksum sidecar is written last, so a
reader never sees a half-written entry, and concurrent writers of the same URL
simply race to publish identical bytes.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class BurstCache:
    """Directory-backed, atomic, checksum-validated byte cache keyed by URL."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _key(self, url: str) -> str:
        # Hash the full URL so arbitrary URL characters map to a safe, fixed
        # filename; keep a short readable suffix for debuggability.
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()
        tail = url.rstrip("/").split("/")[-1][-40:].replace("/", "_")
        return f"{h[:32]}__{tail}"

    def _paths(self, url: str):
        base = self.root / self._key(url)
        return base.with_suffix(base.suffix + ".bin"), base.with_suffix(base.suffix + ".sha256")

    def get(self, url: str) -> bytes | None:
        data_p, meta_p = self._paths(url)
        if not (data_p.exists() and meta_p.exists()):
            return None
        try:
            want = meta_p.read_text().strip()
            raw = data_p.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(raw).hexdigest() != want:
            logger.warning("[BurstCache] checksum mismatch for %s; treating as miss", url)
            return None
        return raw

    def put(self, url: str, content: bytes) -> None:
        if content is None:
            return
        data_p, meta_p = self._paths(url)
        digest = hashlib.sha256(content).hexdigest()
        # Data first (temp + atomic rename), checksum sidecar committed last so
        # get() only trusts data whose checksum exists.
        tmp = data_p.with_suffix(data_p.suffix + f".part.{os.getpid()}")
        try:
            tmp.write_bytes(content)
            os.replace(tmp, data_p)
            meta_tmp = meta_p.with_suffix(meta_p.suffix + f".part.{os.getpid()}")
            meta_tmp.write_text(digest)
            os.replace(meta_tmp, meta_p)
        except OSError as exc:
            logger.warning("[BurstCache] failed to store %s: %s", url, exc)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Module-level opt-in singleton (configured once per worker process).
# --------------------------------------------------------------------------- #
_CACHE: BurstCache | None = None


def configure(cache_dir) -> None:
    """Enable the burst cache at ``cache_dir`` (None/empty disables it)."""
    global _CACHE
    if cache_dir:
        _CACHE = BurstCache(cache_dir)
        logger.info("[BurstCache] enabled at %s", cache_dir)
    else:
        _CACHE = None


def is_enabled() -> bool:
    return _CACHE is not None


def get(url: str) -> bytes | None:
    return _CACHE.get(url) if _CACHE is not None else None


def put(url: str, content: bytes) -> None:
    if _CACHE is not None:
        _CACHE.put(url, content)
