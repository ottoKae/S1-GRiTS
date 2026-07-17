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

    def path_for(self, url: str):
        """Return the on-disk path of a committed, checksum-valid entry, else
        None. Lets a caller window-read the cached GeoTIFF directly (Phase 3.2
        windowed burst reads) instead of decoding the whole array. The checksum
        is verified once here; the returned path is a plain GeoTIFF on disk."""
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
        return data_p

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


def prune(cache_dir, max_gb: float, *, dry_run: bool = False) -> dict:
    """LRU-prune the cache directory down to ``max_gb`` total.

    The cache grows without bound across multi-tile archive runs (hundreds of
    GB of shared bursts), so this gives operators a size cap:

    - eviction order is least-recently-USED first, judged by the data file's
      ``max(atime, mtime)`` — atime when the filesystem tracks it, mtime as
      the floor on ``noatime`` mounts (every ``put`` refreshes mtime);
    - entries are removed as ``.bin`` + ``.sha256`` pairs so a survivor is
      never left checksum-less (a half-removed entry would just read as a
      miss, but why leave litter);
    - stale ``.part`` temp files older than an hour (crashed writers) are
      removed regardless of the budget.

    Safe to run while workers are downloading: a concurrently re-added entry
    simply gets re-published, and readers of a just-evicted entry fall back to
    a normal download. Returns a summary dict (entries/bytes before/after,
    evicted count, reclaimed bytes).
    """
    import time as _time

    root = Path(cache_dir)
    budget = int(float(max_gb) * 1e9)
    entries = []  # (last_used, size, data_path, meta_path)
    stale_parts = 0
    now = _time.time()
    if root.is_dir():
        for data_p in root.glob("*.bin"):
            try:
                st = data_p.stat()
            except OSError:
                continue
            # _paths() derives both names from the same base: "<key>.bin" and
            # "<key>.sha256" — the sidecar swaps the .bin suffix, not appends.
            meta_p = data_p.with_suffix(".sha256")
            entries.append((max(st.st_atime, st.st_mtime), st.st_size, data_p, meta_p))
        for part in root.glob("*.part.*"):
            try:
                if now - part.stat().st_mtime > 3600:
                    if not dry_run:
                        part.unlink()
                    stale_parts += 1
            except OSError:
                pass

    total = sum(size for _, size, _, _ in entries)
    summary = {
        "entries": len(entries), "bytes": total,
        "evicted": 0, "reclaimed_bytes": 0, "stale_parts": stale_parts,
        "dry_run": bool(dry_run),
    }
    if total <= budget:
        return summary

    for last_used, size, data_p, meta_p in sorted(entries):  # oldest first
        if total <= budget:
            break
        if not dry_run:
            for p in (data_p, meta_p):
                try:
                    p.unlink()
                except OSError:
                    continue
        total -= size
        summary["evicted"] += 1
        summary["reclaimed_bytes"] += size
    summary["bytes"] = total
    summary["entries"] = len(entries) - summary["evicted"]
    logger.info(
        "[BurstCache] prune%s: evicted %d entrie(s), reclaimed %.2f GB "
        "(now %d entries, %.2f GB; %d stale .part removed)",
        " (dry-run)" if dry_run else "",
        summary["evicted"], summary["reclaimed_bytes"] / 1e9,
        summary["entries"], summary["bytes"] / 1e9, stale_parts,
    )
    return summary


def usage(cache_dir) -> tuple[int, int]:
    """(entry_count, total_bytes) of committed entries under ``cache_dir``."""
    root = Path(cache_dir)
    n = size = 0
    if root.is_dir():
        for data_p in root.glob("*.bin"):
            try:
                size += data_p.stat().st_size
                n += 1
            except OSError:
                continue
    return n, size


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


def path_for(url: str):
    """On-disk path of a committed cache entry (checksum-valid), else None."""
    return _CACHE.path_for(url) if _CACHE is not None else None


def put(url: str, content: bytes) -> None:
    if _CACHE is not None:
        _CACHE.put(url, content)
