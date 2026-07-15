"""Phase 3.2 — windowed burst reads straight from the burst-cache GeoTIFFs.

Phase 3 (``batch_spill``) made the resident batch of decoded burst arrays
file-backed (memmap) so the kernel can evict it under pressure. But each burst
was still fully decoded (``dataset.read(1)``) at download time and copied to a
``.npy`` file. This module removes both: when the on-disk burst cache holds the
GeoTIFF, a burst enters the batch as a ``LazyBurstArray`` — an ndarray-like
handle that reads only the destination window it is asked for, straight from
the cached GeoTIFF, with no whole-array decode and no ``.npy`` copy.

Correctness contract (locked by tests/test_lazy_burst.py):
- ``.shape``/``.dtype``/``.ndim`` mirror a ``float32`` decode of band 1;
- ``lazy[y0:y1, x0:x1]`` equals ``dataset.read(1).astype(float32)[y0:y1, x0:x1]``
  byte-for-byte (NaN included), i.e. the block readers get identical values;
- ``np.asarray(lazy)`` / ``lazy.astype(float32)`` equal the full decode (the
  legacy full-frame and reproject-fallback paths stay correct);
- disabled or cache-miss -> the caller keeps the eager decode+spill path, so
  output is byte-for-byte unchanged.

Windowed reads open the (local, page-cached) GeoTIFF per call; GDAL's dataset
handle pool amortises the open, and reading only the block's rows keeps peak
resident memory O(block) rather than O(scene). The handle is never held open
across calls, so a batch of hundreds of bursts cannot exhaust file
descriptors.
"""
from __future__ import annotations

import logging

import numpy as np
import rasterio
from rasterio.windows import Window

logger = logging.getLogger(__name__)

# Opt-in toggle (memory.windowed_burst_reads). Requires the on-disk burst
# cache; inert otherwise, so the default decode path is unchanged.
_ENABLED: bool = False


def configure(enabled: bool) -> None:
    global _ENABLED
    _ENABLED = bool(enabled)
    if _ENABLED:
        logger.info("[LazyBurst] windowed burst reads enabled")


def is_enabled() -> bool:
    return _ENABLED


class LazyBurstArray:
    """A read-only, 2-D, ``float32`` ndarray-like view over a GeoTIFF band.

    Supports exactly the operations the mosaic/composite readers perform on a
    decoded burst: ``.shape``/``.dtype``/``.ndim``, ``__getitem__`` windowed
    slicing (the block path), and ``__array__``/``.astype`` full reads (the
    legacy full-frame and reproject-fallback paths). Values are byte-identical
    to ``rasterio`` ``read(band).astype(float32)`` of the same file.
    """

    __slots__ = ("path", "band", "_shape", "_nodata")

    def __init__(self, path, band: int, shape, nodata=None):
        self.path = str(path)
        self.band = int(band)
        self._shape = (int(shape[0]), int(shape[1]))
        self._nodata = nodata

    # -- ndarray-like metadata -------------------------------------------------
    @property
    def shape(self):
        return self._shape

    @property
    def ndim(self) -> int:
        return 2

    @property
    def dtype(self):
        return np.dtype(np.float32)

    @property
    def size(self) -> int:
        return self._shape[0] * self._shape[1]

    def __len__(self) -> int:
        return self._shape[0]

    # -- reads -----------------------------------------------------------------
    def _read_window(self, row0: int, row1: int, col0: int, col1: int) -> np.ndarray:
        row0 = max(0, int(row0)); col0 = max(0, int(col0))
        row1 = min(self._shape[0], int(row1)); col1 = min(self._shape[1], int(col1))
        if row1 <= row0 or col1 <= col0:
            return np.empty((max(0, row1 - row0), max(0, col1 - col0)), dtype=np.float32)
        win = Window(col_off=col0, row_off=row0, width=col1 - col0, height=row1 - row0)
        with rasterio.open(self.path) as ds:
            return ds.read(self.band, window=win).astype(np.float32, copy=False)

    def _full(self) -> np.ndarray:
        with rasterio.open(self.path) as ds:
            return ds.read(self.band).astype(np.float32, copy=False)

    @staticmethod
    def _norm(sl, n: int):
        if isinstance(sl, slice):
            start, stop, step = sl.indices(n)
            if step != 1:
                raise IndexError("LazyBurstArray supports contiguous slices only")
            return start, stop
        idx = int(sl)
        if idx < 0:
            idx += n
        return idx, idx + 1

    def __getitem__(self, key):
        if isinstance(key, tuple):
            if len(key) != 2:
                raise IndexError("LazyBurstArray is 2-D; use [rows, cols]")
            rkey, ckey = key
        else:
            rkey, ckey = key, slice(None)
        r0, r1 = self._norm(rkey, self._shape[0])
        c0, c1 = self._norm(ckey, self._shape[1])
        out = self._read_window(r0, r1, c0, c1)
        # A scalar index on either axis collapses that axis, matching ndarray.
        if not isinstance(rkey, slice):
            out = out[0, :]
        if not isinstance(ckey, slice):
            out = out[..., 0]
        return out

    def __array__(self, dtype=None):
        arr = self._full()
        return arr.astype(dtype) if dtype is not None else arr

    def astype(self, dtype, copy: bool = True):
        return self._full().astype(dtype, copy=copy)


def maybe_lazy(url: str, prof: dict):
    """Return a ``LazyBurstArray`` for ``url`` if windowed reads are enabled and
    the burst cache holds a checksum-valid copy; otherwise ``None`` (caller
    keeps the eager decode+spill path). ``prof`` supplies shape + nodata."""
    if not _ENABLED:
        return None
    from s1grits import burst_cache
    if not burst_cache.is_enabled():
        return None
    path = burst_cache.path_for(url)
    if path is None:
        return None
    try:
        h = int(prof["height"]); w = int(prof["width"])
    except (KeyError, TypeError, ValueError):
        return None
    return LazyBurstArray(path, 1, (h, w), nodata=prof.get("nodata"))
