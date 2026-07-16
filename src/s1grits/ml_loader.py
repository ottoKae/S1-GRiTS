"""
ML data loader for S1-GRiTS.

Thin bridge from the STAC layer to a model-ready tensor:

    query the stac-geoparquet by tile / time / bbox
        -> locate the backing Zarr store(s)
            -> slice space + time, stack bands
                -> (time, band, y, x) xarray.DataArray

The geoparquet is the spatio-temporal index (one row per STAC Item, with a
GeoParquet geometry + flattened ``properties`` columns); the Zarr store holds
the actual (time, y, x) band cube. This module keeps the two concerns thin:
``query_items`` is pure metadata filtering, ``load_timeseries`` does the cube
read. Both work directly off the files produced by ``catalog resync`` — no STAC
API or database required.

Example
-------
    import s1grits.ml_loader as ml
    da = ml.load_timeseries(
        "G:/data/cube", collection="s1grits-monthly",
        tile="49QGF", time=("2024-01-01", "2024-12-31"),
        bbox=(113.5, 30.0, 114.9, 30.9), bands=["VV_dB", "VH_dB"],
    )
    tensor = da.transpose("time", "band", "y", "x").values   # numpy, model-ready
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _resolve_parquet(source: str | Path, collection: str | None) -> Path:
    """Resolve ``source`` to an items.parquet path.

    ``source`` may be the parquet file itself, or a datacube root in which case
    ``collection`` selects ``{root}/collections/{collection}/items.parquet``.
    """
    p = Path(source)
    if p.is_file() and p.suffix == ".parquet":
        return p
    if p.is_dir():
        if collection:
            cand = p / "collections" / collection / "items.parquet"
            if cand.is_file():
                return cand
            raise FileNotFoundError(f"No items.parquet for collection {collection!r} under {p}")
        # auto-detect a single collection
        coll_root = p / "collections"
        parquets = sorted(coll_root.glob("*/items.parquet")) if coll_root.is_dir() else []
        if len(parquets) == 1:
            return parquets[0]
        if not parquets:
            raise FileNotFoundError(f"No collections/*/items.parquet under {p}")
        names = [pp.parent.name for pp in parquets]
        raise ValueError(f"Multiple collections under {p}: {names}; pass collection=...")
    raise FileNotFoundError(f"Not a parquet file or datacube root: {source}")


def _as_time_range(time):
    """Normalize ``time`` to (start, end) UTC Timestamps (either may be None)."""
    import pandas as pd
    if time is None:
        return None, None
    if isinstance(time, (str, bytes)) or not isinstance(time, Sequence):
        # single value -> treat as a lower bound
        return pd.Timestamp(time, tz="UTC"), None
    t0, t1 = time
    return (pd.Timestamp(t0, tz="UTC") if t0 is not None else None,
            pd.Timestamp(t1, tz="UTC") if t1 is not None else None)


def _bbox_overlaps(item_bbox: dict, q) -> bool:
    """WGS84 bbox-vs-bbox overlap test. ``item_bbox`` is the geoparquet struct
    {xmin,ymin,xmax,ymax}; ``q`` is (minlon, minlat, maxlon, maxlat)."""
    qx0, qy0, qx1, qy1 = q
    return not (item_bbox["xmax"] < qx0 or item_bbox["xmin"] > qx1
                or item_bbox["ymax"] < qy0 or item_bbox["ymin"] > qy1)


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

def query_items(
    source: str | Path,
    *,
    collection: str | None = None,
    tile: str | None = None,
    time=None,
    bbox: tuple | None = None,
    direction: str | None = None,
):
    """Filter the stac-geoparquet items by tile / time / bbox / orbit direction.

    Args:
        source: items.parquet path, or datacube root (+ ``collection``).
        tile: MGRS tile id (matches ``mgrs:tile_id``).
        time: (start, end) — strings/Timestamps; either bound may be None.
        bbox: (minlon, minlat, maxlon, maxlat) in WGS84.
        direction: 'ASCENDING' | 'DESCENDING' (matches ``sat:orbit_state``).

    Returns:
        pandas.DataFrame of matching items (geoparquet columns). ``df.attrs
        ['parquet_dir']`` holds the directory the zarr hrefs resolve against.
    """
    import pandas as pd
    import pyarrow.parquet as pq

    pqp = _resolve_parquet(source, collection)
    df = pq.read_table(str(pqp)).to_pandas()

    if tile is not None and "mgrs:tile_id" in df.columns:
        if isinstance(tile, (list, tuple, set)):
            df = df[df["mgrs:tile_id"].isin(list(tile))]
        else:
            df = df[df["mgrs:tile_id"] == tile]
    if direction is not None and "sat:orbit_state" in df.columns:
        df = df[df["sat:orbit_state"].astype(str).str.lower() == direction.lower()]
    if time is not None and "datetime" in df.columns:
        t0, t1 = _as_time_range(time)
        dt = pd.to_datetime(df["datetime"], utc=True)
        if t0 is not None:
            df = df[dt >= t0]
        if t1 is not None:
            df = df[dt <= t1]
    if bbox is not None and "bbox" in df.columns:
        df = df[df["bbox"].apply(lambda b: _bbox_overlaps(b, bbox))]

    df = df.reset_index(drop=True)
    df.attrs["parquet_dir"] = str(pqp.parent)
    return df


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------

def _reproject_bbox(bbox, dst_epsg: int):
    """WGS84 (minlon,minlat,maxlon,maxlat) -> (xmin,ymin,xmax,ymax) in dst_epsg."""
    import pyproj
    tr = pyproj.Transformer.from_crs(4326, int(dst_epsg), always_xy=True)
    lons = [bbox[0], bbox[2], bbox[2], bbox[0]]
    lats = [bbox[1], bbox[1], bbox[3], bbox[3]]
    xs, ys = tr.transform(lons, lats)
    return min(xs), min(ys), max(xs), max(ys)


def _open_zarr_subset(zarr_path, *, bbox=None, time=None, bands=None, epsg=None):
    """Open one Zarr store and return a (time, band, y, x) DataArray, sliced to
    ``time`` / ``bbox`` and restricted to ``bands`` (canonical order by default).
    """
    import numpy as np
    import pandas as pd
    import xarray as xr
    from s1grits.stac_builder import _canonical_band_order

    ds = xr.open_zarr(str(zarr_path), consolidated=False)

    # ensure a datetime time coordinate
    if "time" in ds.coords and not np.issubdtype(ds["time"].dtype, np.datetime64):
        ds = ds.assign_coords(time=pd.to_datetime(ds["time"].values))

    # band selection: data bands only (drop grid_mapping/coords), canonical order
    data_bands = _canonical_band_order(list(ds.data_vars))
    if bands:
        sel = [b for b in bands if b in data_bands]
        missing = [b for b in bands if b not in data_bands]
        if missing:
            raise KeyError(f"bands not in store {zarr_path}: {missing}; available: {data_bands}")
    else:
        sel = data_bands
    if not sel:
        raise ValueError(f"no data bands found in {zarr_path}")

    # time subset
    if time is not None and "time" in ds.coords:
        t0, t1 = _as_time_range(time)
        # zarr time is tz-naive UTC; compare with tz-naive bounds
        ds = ds.sel(time=slice(
            t0.tz_localize(None) if t0 is not None else None,
            t1.tz_localize(None) if t1 is not None else None,
        ))

    # spatial subset (reproject WGS84 bbox -> store CRS -> x/y slice)
    if bbox is not None and {"x", "y"} <= set(ds.coords):
        _epsg = epsg or ds.attrs.get("crs")
        if isinstance(_epsg, str) and _epsg.upper().startswith("EPSG:"):
            _epsg = int(_epsg.split(":")[-1])
        if _epsg:
            x0, y0, x1, y1 = _reproject_bbox(bbox, int(_epsg))
            y_desc = bool(ds["y"].values[0] > ds["y"].values[-1])
            ds = ds.sel(
                x=slice(x0, x1),
                y=slice(y1, y0) if y_desc else slice(y0, y1),
            )

    da = ds[sel].to_array(dim="band")
    # canonical model layout
    dims = [d for d in ("time", "band", "y", "x") if d in da.dims]
    da = da.transpose(*dims)
    da.attrs["crs"] = epsg if epsg else ds.attrs.get("crs")
    da.name = "s1grits"
    return da


def load_timeseries(
    source: str | Path,
    *,
    collection: str | None = None,
    tile: str | None = None,
    time=None,
    bbox: tuple | None = None,
    bands: list[str] | None = None,
    direction: str | None = None,
):
    """Query the geoparquet and load the matching Zarr cube(s) as a model-ready
    time-series tensor.

    Args:
        source: items.parquet path, or datacube root (+ ``collection``).
        tile, time, bbox, direction: see :func:`query_items`.
        bands: band names to stack (default: all data bands, canonical order —
            VV/VH first). Selection is by NAME, so it is robust to band-count
            changes across products.

    Returns:
        ``xarray.DataArray`` with dims (time, band, y, x) when the query
        resolves to a single Zarr store; otherwise a dict mapping each store's
        absolute path to its DataArray (e.g. when spanning tiles/directions).
        The ``band`` coordinate carries the band names; ``.attrs['crs']`` the EPSG.
    """
    df = query_items(source, collection=collection, tile=tile, time=time,
                     bbox=bbox, direction=direction)
    if len(df) == 0:
        raise ValueError("No items match the query (tile/time/bbox/direction).")

    parquet_dir = Path(df.attrs["parquet_dir"])

    # group matching items by their backing zarr store (items are time slices of
    # the same full-series store, so dedupe to unique stores)
    stores: dict[str, Any] = {}
    for _, row in df.iterrows():
        assets = row.get("assets") or {}
        zasset = assets.get("zarr") if isinstance(assets, dict) else None
        if not zasset or not zasset.get("href"):
            continue
        zpath = (parquet_dir / zasset["href"]).resolve()
        stores.setdefault(str(zpath), row.get("proj:epsg"))

    if not stores:
        raise ValueError("Matching items have no zarr asset to load.")

    out = {
        zp: _open_zarr_subset(zp, bbox=bbox, time=time, bands=bands, epsg=epsg)
        for zp, epsg in stores.items()
    }
    return next(iter(out.values())) if len(out) == 1 else out


# ---------------------------------------------------------------------------
# patch sampling
# ---------------------------------------------------------------------------

def _as_pair(v):
    return (int(v), int(v)) if isinstance(v, (int, float)) else (int(v[0]), int(v[1]))


def _patch_origins(Y, X, ph, pw, sy, sx, mode, n, rng, drop_partial):
    """Yield (y0, x0) top-left origins for patches."""
    if mode == "grid":
        ys = list(range(0, max(Y - ph, 0) + 1, sy))
        xs = list(range(0, max(X - pw, 0) + 1, sx))
        if not drop_partial:
            if ys and ys[-1] != Y - ph and Y - ph > 0:
                ys.append(Y - ph)
            if xs and xs[-1] != X - pw and X - pw > 0:
                xs.append(X - pw)
        for y0 in ys:
            for x0 in xs:
                yield y0, x0
    elif mode == "random":
        if n is None:
            raise ValueError("mode='random' requires n=<number of patches>")
        if Y < ph or X < pw:
            return
        for _ in range(n):
            yield int(rng.integers(0, Y - ph + 1)), int(rng.integers(0, X - pw + 1))
    else:
        raise ValueError(f"unknown mode {mode!r} (use 'grid' or 'random')")


def sample_patches(
    da,
    *,
    patch_size,
    stride=None,
    mode: str = "grid",
    n: int | None = None,
    min_valid: float = 0.0,
    seed: int | None = None,
    drop_partial: bool = True,
    return_meta: bool = False,
):
    """Cut spatial patches out of a (time, band, y, x) cube for model training.

    The full time series and all bands are kept per patch (the sampling is
    purely spatial), so the output is ready for a per-patch temporal model
    (LSTM / transformer).

    Args:
        da: an ``xarray.DataArray`` from :func:`load_timeseries` (dims include
            time, band, y, x). Missing dims are treated as size 1.
        patch_size: int or (ph, pw) — spatial patch height/width.
        stride: int or (sy, sx) — step between patches (default: patch_size, i.e.
            non-overlapping). Only used for mode='grid'.
        mode: 'grid' (regular sliding window) or 'random' (n random origins).
        n: number of patches for mode='random'.
        min_valid: keep a patch only if at least this fraction of its values are
            finite (non-NaN). 0.0 keeps all; e.g. 0.8 drops mostly-NoData patches.
        seed: RNG seed for mode='random'.
        drop_partial: grid mode — drop edge windows smaller than the patch
            (True) or clamp them to the border so the edge is covered (False).
        return_meta: also return per-patch metadata (origin indices + geographic
            bounds in the cube CRS).

    Returns:
        ``patches`` ndarray of shape (N, time, band, ph, pw); or
        ``(patches, meta)`` when ``return_meta`` — ``meta`` is a list of dicts
        ``{y0, x0, ph, pw, y_min, y_max, x_min, x_max, crs}``.
    """
    import numpy as np

    # normalise to (time, band, y, x)
    da = da.transpose(*[d for d in ("time", "band", "y", "x") if d in da.dims])
    arr = da.values
    while arr.ndim < 4:  # promote missing leading dims (e.g. single band/time)
        arr = arr[np.newaxis, ...]
    T, B, Y, X = arr.shape

    ph, pw = _as_pair(patch_size)
    sy, sx = _as_pair(stride) if stride is not None else (ph, pw)
    rng = np.random.default_rng(seed)

    yv = da["y"].values if "y" in da.coords else np.arange(Y)
    xv = da["x"].values if "x" in da.coords else np.arange(X)
    crs = da.attrs.get("crs")

    patches, meta = [], []
    for y0, x0 in _patch_origins(Y, X, ph, pw, sy, sx, mode, n, rng, drop_partial):
        patch = arr[:, :, y0:y0 + ph, x0:x0 + pw]
        if patch.shape[-2:] != (ph, pw):
            continue
        if min_valid > 0.0 and float(np.isfinite(patch).mean()) < min_valid:
            continue
        patches.append(patch)
        if return_meta:
            ys = yv[y0:y0 + ph]; xs = xv[x0:x0 + pw]
            meta.append({
                "y0": y0, "x0": x0, "ph": ph, "pw": pw,
                "y_min": float(min(ys[0], ys[-1])), "y_max": float(max(ys[0], ys[-1])),
                "x_min": float(min(xs[0], xs[-1])), "x_max": float(max(xs[0], xs[-1])),
                "crs": crs,
            })

    if patches:
        out = np.stack(patches, axis=0)
    else:
        out = np.empty((0, T, B, ph, pw), dtype=arr.dtype)
    return (out, meta) if return_meta else out


# ---------------------------------------------------------------------------
# normalization / training statistics
# ---------------------------------------------------------------------------

def compute_band_stats(arrays, *, bands: list[str] | None = None) -> dict[str, dict]:
    """Per-band mean/std/min/max over finite (non-NaN) values, for normalization.

    Args:
        arrays: a DataArray (time, band, y, x), a dict of them (multi-store, e.g.
            from :func:`load_timeseries`), or a list of DataArrays. Stats are
            accumulated across all of them in one pass.
        bands: restrict to these band names (default: all bands present).

    Returns:
        ``{band: {"mean", "std", "min", "max", "count"}}``. Use as ``normalize=``
        for :class:`PatchDataset`, or persist with :func:`save_band_stats`.
    """
    import numpy as np

    if hasattr(arrays, "dims"):           # single DataArray
        das = [arrays]
    elif isinstance(arrays, dict):
        das = list(arrays.values())
    else:
        das = list(arrays)

    acc: dict[str, dict] = {}
    for da in das:
        da_bands = [str(b) for b in da["band"].values] if "band" in da.coords else ["band"]
        use = bands or da_bands
        for b in use:
            if b not in da_bands:
                continue
            v = np.asarray(da.sel(band=b).values, dtype="float64").ravel()
            v = v[np.isfinite(v)]
            if v.size == 0:
                continue
            a = acc.setdefault(b, {"n": 0, "sum": 0.0, "sumsq": 0.0,
                                   "min": float("inf"), "max": float("-inf")})
            a["n"] += int(v.size)
            a["sum"] += float(v.sum())
            a["sumsq"] += float(np.square(v).sum())
            a["min"] = min(a["min"], float(v.min()))
            a["max"] = max(a["max"], float(v.max()))

    stats: dict[str, dict] = {}
    for b, a in acc.items():
        n = a["n"]
        mean = a["sum"] / n
        var = max(a["sumsq"] / n - mean * mean, 0.0)
        stats[b] = {"mean": mean, "std": var ** 0.5, "min": a["min"],
                    "max": a["max"], "count": n}
    return stats


def save_band_stats(path, stats: dict) -> None:
    """Persist band stats to a JSON file (e.g. next to the items.parquet)."""
    import json
    Path(path).write_text(json.dumps(stats, indent=2), encoding="utf-8")


def load_band_stats(path) -> dict:
    """Load band stats written by :func:`save_band_stats`."""
    import json
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _stats_to_arrays(stats: dict, bands: list[str]):
    """Build (mean[B], std[B]) float32 arrays aligned to ``bands`` from a stats
    dict; missing bands default to mean 0 / std 1 (no-op)."""
    import numpy as np
    mean = np.array([stats.get(b, {}).get("mean", 0.0) for b in bands], dtype="float32")
    std = np.array([stats.get(b, {}).get("std", 1.0) for b in bands], dtype="float32")
    std[std == 0] = 1.0  # avoid divide-by-zero on constant bands
    return mean, std


# ---------------------------------------------------------------------------
# patch index (torch-free) + PyTorch Dataset
# ---------------------------------------------------------------------------

def build_patch_index(
    source: str | Path,
    *,
    collection: str | None = None,
    tile=None,
    time=None,
    bbox: tuple | None = None,
    bands: list[str] | None = None,
    direction: str | None = None,
    patch_size=64,
    stride=None,
    mode: str = "grid",
    n: int | None = None,
    seed: int | None = None,
    drop_partial: bool = True,
    time_grid=None,
):
    """Build a lazy patch index across one or many Zarr stores (cross-tile).

    Opens each matching store lazily (no pixel reads), reindexes them onto a
    COMMON time grid (the union of their timestamps within ``time``, unless
    ``time_grid`` is given) so every patch has identical (time, band) shape, and
    enumerates patch origins per store. Pure metadata — torch not required.

    Returns a dict with:
      * ``stores``: {store_path: lazy DataArray (time, band, y, x)} on the common grid,
      * ``index``: list of (store_path, y0, x0),
      * ``time_grid`` (np.datetime64[]), ``bands`` (list), ``patch_size`` (ph, pw).
    """
    import numpy as np

    df = query_items(source, collection=collection, tile=tile, time=time,
                     bbox=bbox, direction=direction)
    if len(df) == 0:
        raise ValueError("No items match the query (tile/time/bbox/direction).")
    parquet_dir = Path(df.attrs["parquet_dir"])

    # unique backing stores (+ their epsg)
    store_epsg: dict[str, Any] = {}
    for _, row in df.iterrows():
        assets = row.get("assets") or {}
        za = assets.get("zarr") if isinstance(assets, dict) else None
        if za and za.get("href"):
            store_epsg.setdefault(str((parquet_dir / za["href"]).resolve()),
                                  row.get("proj:epsg"))
    if not store_epsg:
        raise ValueError("Matching items have no zarr asset to load.")

    # open each store lazily, subset to time/bbox/bands
    raw = {zp: _open_zarr_subset(zp, bbox=bbox, time=time, bands=bands, epsg=epsg)
           for zp, epsg in store_epsg.items()}

    # common time grid (union) so all patches share T
    if time_grid is None:
        all_t = np.unique(np.concatenate(
            [da["time"].values for da in raw.values() if "time" in da.coords]
        )) if any("time" in da.coords for da in raw.values()) else None
        time_grid = all_t
    bands_final = [str(b) for b in next(iter(raw.values()))["band"].values]

    stores: dict[str, Any] = {}
    index: list[tuple] = []
    ph, pw = _as_pair(patch_size)
    sy, sx = _as_pair(stride) if stride is not None else (ph, pw)
    rng = np.random.default_rng(seed)
    for zp, da in raw.items():
        if time_grid is not None and "time" in da.coords:
            da = da.reindex(time=time_grid)
        stores[zp] = da
        Y = da.sizes.get("y", 1)
        X = da.sizes.get("x", 1)
        for y0, x0 in _patch_origins(Y, X, ph, pw, sy, sx, mode, n, rng, drop_partial):
            index.append((zp, y0, x0))

    return {"stores": stores, "index": index, "time_grid": time_grid,
            "bands": bands_final, "patch_size": (ph, pw)}


class PatchDataset:
    """Lazy PyTorch ``Dataset`` of (time, band, ph, pw) patches over one or many
    tiles, reading each patch on demand from Zarr and applying normalization.

    Usable with ``torch.utils.data.DataLoader``. ``torch`` is imported lazily so
    the rest of the module works without it.

    Args mirror :func:`build_patch_index` plus:
        normalize: ``None`` (raw), a band-stats dict (from
            :func:`compute_band_stats` / :func:`load_band_stats`), or the string
            ``"zscore"`` to compute z-score stats from the selected data.
        nan_to: replace NaN with this value after normalization (e.g. 0.0).
        dtype: output numpy/torch dtype (default 'float32').
        return_meta: if True, ``__getitem__`` returns (tensor, meta_dict).
    """

    def __init__(self, source, *, collection=None, tile=None, time=None, bbox=None,
                 bands=None, direction=None, patch_size=64, stride=None, mode="grid",
                 n=None, seed=None, drop_partial=True, time_grid=None,
                 normalize=None, nan_to=None, dtype="float32", return_meta=False):
        try:
            import torch.utils.data as _tud
            self.__class__.__bases__ = (_tud.Dataset,)
        except Exception:
            pass  # works as a plain sequence too
        idx = build_patch_index(
            source, collection=collection, tile=tile, time=time, bbox=bbox,
            bands=bands, direction=direction, patch_size=patch_size, stride=stride,
            mode=mode, n=n, seed=seed, drop_partial=drop_partial, time_grid=time_grid,
        )
        self.stores = idx["stores"]
        self.index = idx["index"]
        self.time_grid = idx["time_grid"]
        self.bands = idx["bands"]
        self.patch_size = idx["patch_size"]
        self.nan_to = nan_to
        self.dtype = dtype
        self.return_meta = return_meta

        if normalize == "zscore":
            normalize = compute_band_stats(self.stores, bands=self.bands)
        self.stats = normalize
        if normalize:
            self._mean, self._std = _stats_to_arrays(normalize, self.bands)
        else:
            self._mean = self._std = None

    def __len__(self):
        return len(self.index)

    def __getitem__(self, i):
        import numpy as np
        zp, y0, x0 = self.index[i]
        ph, pw = self.patch_size
        da = self.stores[zp]
        patch = np.asarray(
            da.isel(y=slice(y0, y0 + ph), x=slice(x0, x0 + pw)).values,
            dtype=self.dtype,
        )  # (time, band, ph, pw)
        if self._mean is not None:
            patch = (patch - self._mean[None, :, None, None]) / self._std[None, :, None, None]
        if self.nan_to is not None:
            patch = np.nan_to_num(patch, nan=float(self.nan_to))
        out = np.ascontiguousarray(patch)
        try:
            import torch
            out = torch.from_numpy(out)
        except ImportError:
            pass  # torch-free: return the numpy patch as documented
        if self.return_meta:
            return out, {"store": zp, "y0": y0, "x0": x0}
        return out


