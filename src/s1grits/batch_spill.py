"""Disk-backed batch sources — Phase 3 of the scenes bounded-memory migration.

The dominant memory term in the scenes workflow is the batch of decoded
burst arrays held for the lifetime of each batch (``final_vv/final_vh``:
B bursts x 2 pol x ~35 MB = 5-15 GB of ANONYMOUS memory — the term that
drove both recorded OOM incidents; see docs/scenes_blockwise_architecture.md
S1). This module converts that term to FILE-BACKED memory: decoded arrays
are spilled to per-process ``.npy`` files and handed back as read-only
``np.memmap`` views.

Why this is the right mechanism:

- ``np.memmap`` IS an ndarray. Every downstream consumer (windowed mosaic
  slicing, full-frame mosaic, despeckle, pre-align, shape checks) works
  unchanged, and the values are byte-identical (lossless float32 round
  trip) — output parity is locked by tests.
- File-backed pages are RECLAIMABLE: under memory pressure the kernel
  evicts them and re-reads on demand, instead of OOM-killing the process
  the way anonymous pages do. Peak *resident* memory becomes a function of
  the pages actually touched (a block read touches only its window's rows),
  not of batch density.
- The blockwise smonthly path gets true windowed reads for free:
  ``_mosaic_align_window`` slices the memmap, which faults in only the
  needed pages.

Opt-in via ``memory.batch_spill: true`` (spill dir defaults to
``{output_root}/.spill``, override with ``memory.spill_dir`` — point it at
fast local scratch). Disk needed ≈ one batch of bursts (the same bytes the
downloads already transferred). Files are deleted at each batch boundary
and on process exit.

Inert unless configured, mirroring ``burst_cache``: the default decode path
is byte-for-byte unchanged.
"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import threading
import uuid
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_spill_dir: Path | None = None
_counter = 0


def configure(spill_dir: str | Path | None) -> None:
    """Enable (or disable with None) spilling for THIS process.

    Called per worker process, like ``burst_cache.configure``. The actual
    directory is namespaced by PID so parallel tile workers never collide,
    and is removed on process exit as a safety net behind the per-batch
    cleanup.
    """
    global _spill_dir
    with _lock:
        if not spill_dir:
            _spill_dir = None
            return
        _spill_dir = Path(spill_dir) / f"pid-{os.getpid()}"
        _spill_dir.mkdir(parents=True, exist_ok=True)
    atexit.register(_remove_dir_quiet)
    logger.info("[Spill] Batch arrays spill to %s (file-backed, reclaimable)",
                _spill_dir)


def is_enabled() -> bool:
    return _spill_dir is not None


def maybe_spill(arr: np.ndarray | None) -> np.ndarray | None:
    """Spill a decoded array to disk and return a read-only memmap view.

    Passthrough (returns ``arr`` unchanged) when disabled, on None, or on
    any filesystem error — spilling is an optimisation, never a failure
    mode. The round trip is lossless: identical dtype, shape, and bytes
    (NaNs included).
    """
    if arr is None or _spill_dir is None:
        return arr
    global _counter
    try:
        with _lock:
            _counter += 1
            path = _spill_dir / f"burst-{_counter:06d}-{uuid.uuid4().hex[:6]}.npy"
        np.save(path, arr, allow_pickle=False)
        return np.load(path, mmap_mode="r", allow_pickle=False)
    except OSError as exc:
        logger.warning("[Spill] Falling back to in-RAM array (%s)", exc)
        return arr


def cleanup_batch() -> int:
    """Delete this process's spill files (call at each batch boundary).

    The caller must have dropped its references to the batch's arrays
    first (the batch loop's ``del final_vv, final_vh`` does); a memmap
    whose file is unlinked stays readable on POSIX until closed, so even
    a stale reference degrades to an error on *Windows* only, where we
    skip files that are still locked.
    """
    removed = 0
    with _lock:
        d = _spill_dir
    if d is None or not d.exists():
        return 0
    for f in d.glob("burst-*.npy"):
        try:
            f.unlink()
            removed += 1
        except OSError:  # Windows: mapping still open — freed at process exit
            pass
    return removed


def _remove_dir_quiet() -> None:
    with _lock:
        d = _spill_dir
    if d is not None:
        shutil.rmtree(d, ignore_errors=True)
