"""
file_lock.py
============
Cross-platform advisory file lock for S1-GRiTS output directories.

Uses ``msvcrt.locking`` on Windows and ``fcntl.flock`` on Linux/macOS.
No external dependencies required.

Usage::

    from s1grits.file_lock import output_lock

    with output_lock("/path/to/output"):
        # ... safe to write catalogs, STAC, etc.

Only one workflow instance can hold the lock on a given directory at a time.
If the lock is held, the second process will wait up to ``timeout`` seconds,
then raise ``TimeoutError``.
"""

import os
import sys
import time as _time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from s1grits.logger_config import get_logger

logger = get_logger(__name__)

LOCK_FILE_NAME = ".s1grits.lock"
DEFAULT_TIMEOUT = 300  # seconds


# ---------------------------------------------------------------------------
# Platform-specific lock primitives
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import msvcrt

    def _lock_file(fp) -> None:
        """Acquire exclusive lock (non-blocking). Raises OSError if held."""
        msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(fp) -> None:
        """Release lock."""
        try:
            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _lock_file(fp) -> None:
        """Acquire exclusive lock (non-blocking). Raises OSError if held."""
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(fp) -> None:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def acquire_lock(output_dir: str | Path, timeout: float = DEFAULT_TIMEOUT) -> 'tuple[Path, object]':
    """Acquire lock; returns (lock_path, file_handle). Call release_lock() to release."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / LOCK_FILE_NAME
    if not lock_path.exists():
        lock_path.write_text("")
    fp = open(str(lock_path), "r+")
    deadline = _time.monotonic() + timeout
    logger.info("Acquiring lock on %s (timeout=%ds)...", output_dir, int(timeout))
    while True:
        try:
            _lock_file(fp)
            logger.info("Lock acquired: %s", lock_path)
            return (lock_path, fp)
        except OSError:
            if _time.monotonic() > deadline:
                fp.close()
                msg = (
                    f"Timed out waiting for lock on {output_dir} ({timeout}s). "
                    f"Another S1-GRiTS process may be using this directory."
                )
                logger.error(msg)
                raise TimeoutError(msg) from None
            _time.sleep(0.5)


def release_lock(lock_info: 'tuple[Path, object]') -> None:
    """Release a previously acquired lock."""
    if lock_info is None:
        return
    _, fp = lock_info
    try:
        _unlock_file(fp)
    finally:
        fp.close()
    logger.info("Lock released")

@contextmanager
def output_lock(output_dir: str | Path, timeout: float = DEFAULT_TIMEOUT):
    """
    Context manager that acquires an exclusive lock on ``output_dir``.

    Creates a ``.s1grits.lock`` file in the directory and locks it.
    If another process holds the lock, this blocks for up to
    ``timeout`` seconds.  On timeout a ``TimeoutError`` is raised.

    The lock is released when the context exits (including on exception).

    Example::

        with output_lock("/data/datacube"):
            run_scenes_workflow("config.yaml")
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / LOCK_FILE_NAME

    # Ensure lock file exists
    if not lock_path.exists():
        lock_path.write_text("")

    fp = open(str(lock_path), "r+")
    deadline = _time.monotonic() + timeout

    logger.info("Acquiring lock on %s (timeout=%ds)...", output_dir, int(timeout))

    try:
        while True:
            try:
                _lock_file(fp)
                logger.info("Lock acquired: %s", lock_path)
                break
            except OSError:
                if _time.monotonic() > deadline:
                    msg = (
                        f"Timed out waiting for lock on {output_dir} "
                        f"({timeout}s). Another S1-GRiTS process may be "
                        f"using this directory. Try again later, or remove "
                        f"{lock_path} if you are sure no process is running."
                    )
                    logger.error(msg)
                    raise TimeoutError(msg) from None
                _time.sleep(0.5)

        yield

    finally:
        _unlock_file(fp)
        fp.close()
        logger.info("Lock released: %s", lock_path)
