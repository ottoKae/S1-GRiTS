"""
s1grits_track_extract — track-arbitrated pixel time-series extraction (reusable)
================================================================================

A small, dependency-light utility for **S1-GRiTS**'s multi-track storage layout:
one *isolated* Zarr store per relative orbit/track inside an MGRS tile, with
partial, overlapping footprints. For a pixel that may be observed by several
tracks, this extracts the series from **every candidate track store** and returns
the series from **exactly one** track — the best-covered one.

Geometric-stability guarantee
-----------------------------
A returned ``TrackSeries`` always comes from a **single track store**
(``.track``). Backscatter is *never mosaicked across tracks* (that would mix
incidence-angle / look-direction regimes into one series). Arbitration selects
one whole series; it never combines observations element-wise. This invariant
lives in one place — :func:`arbitrate` — and is asserted in :meth:`extract`.

Dependencies: ``numpy`` + ``xarray`` (with ``zarr``; ``dask`` optional). No
dependency on the rest of the pipeline, so this file can be copied into any
project, or imported:

    from s1grits.track_extract import S1TrackExtractor

Example
-------
    ex = S1TrackExtractor(bands=("VV_dB", "VH_dB"), t0="2017-01-01", t1="2025-12-31")

    # one pixel, several candidate track stores for its tile:
    series = ex.extract(
        candidate_stores=[
            "/data/17NPA/.../s1grits_smonthly_17NPA_ASCENDING_TK18.zarr",
            "/data/17NPA/.../s1grits_smonthly_17NPA_ASCENDING_TK19.zarr",
        ],
        cy=812, cx=1503, point_id="balsa_17563", tile="17NPA")
    if series:                       # None → pixel outside every track's swath
        print(series.track, series.n_valid)      # exactly one store
        df_rows = series.to_records(extra={"cls": "balsa"})

    # or batch (opens each store once):
    out, stats = ex.extract_batch([
        {"point_id": "balsa_17563", "tile": "17NPA",
         "candidates": [(storeA, 812, 1503), (storeB, 812, 1503)]},
        ...
    ])

Catalog convenience — go from catalog.parquet to extract_batch in a couple of lines
(discovers candidate track stores + per-track pixel coords for each point):

    ex = S1TrackExtractor.from_catalog(
        "/data/.../catalog.parquet", "/data/.../Dong_11tiles_.../",
        bands=("VV_dB", "VH_dB"), t0="2017-01-01", t1="2025-12-31")

    points = [{"point_id": pid, "tile": tile,
               "candidates": ex.candidates_for_point(utm_x, utm_y, "EPSG:32717", tile)}
              for pid, tile, utm_x, utm_y in my_points]   # tile=None searches all tiles

    out, stats = ex.extract_batch(points)   # one single-track TrackSeries per point
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

# Store names carry the relative-orbit token: s1grits_smonthly_..._TK18.zarr
_TRACK_TOKEN_RE = re.compile(r"_TK(\d+)")


def track_id_from_store(store: str) -> int | None:
    """Relative-orbit number parsed from a store name's ``_TK{nn}`` token
    (``.../s1grits_smonthly_17NPA_ASCENDING_TK18.zarr`` -> ``18``), or None
    when the name carries no token. Downstream tables usually want this
    integer, not the absolute store path."""
    m = _TRACK_TOKEN_RE.search(os.path.basename(str(store).rstrip("/\\")))
    return int(m.group(1)) if m else None


@dataclass
class TrackSeries:
    """A pixel's time series from exactly ONE track store."""
    point_id: str
    tile: str
    track: str                       # winning store path — the only track present
    cy: int
    cx: int
    dates: np.ndarray                # datetime64, timesteps where all bands finite
    values: dict = field(default_factory=dict)   # {band: float32 array aligned to dates}
    n_valid: int = 0
    n_candidates: int = 0            # how many track stores were queried
    track_id: int | None = None      # relative orbit parsed from the store name

    def to_records(self, extra: dict | None = None) -> list[dict]:
        """Long-format rows (one per valid month) for CSV/DataFrame output."""
        extra = extra or {}
        rows = []
        for i, d in enumerate(self.dates):
            row = {"point_id": self.point_id, "tile": self.tile, "track": self.track,
                   "track_id": self.track_id,
                   "row": self.cy, "col": self.cx, "date": str(d)[:10]}
            row.update({b: float(self.values[b][i]) for b in self.values})
            row.update(extra)
            rows.append(row)
        return rows


def valid_count(values: dict, bands) -> int:
    """Timesteps where *all* bands are finite (the co-observed months)."""
    finite = None
    for b in bands:
        f = np.isfinite(np.asarray(values[b]))
        finite = f if finite is None else (finite & f)
    return int(finite.sum()) if finite is not None else 0


def arbitrate(candidates, bands):
    """Single-track arbiter (the whole geometric-stability guarantee).

    ``candidates``: list of ``(store, dates, values)`` — one per track that
    covers the pixel. Returns the ONE candidate with the most co-valid months
    (ties broken by store name for reproducibility), or ``None``. **Never merges
    across tracks.**
    """
    best, best_key = None, (-1, "")
    for store, dates, values in sorted(candidates, key=lambda c: c[0]):
        key = (valid_count(values, bands), )
        if key[0] > best_key[0]:
            best, best_key = (store, dates, values), key
    return (best, best_key[0]) if best is not None and best_key[0] > 0 else (None, 0)


def _parse_transform(v):
    """Catalog transform → 6-float rasterio Affine list [a, b, c, d, e, f]."""
    if isinstance(v, str):
        v = json.loads(v)
    return [float(x) for x in (v.tolist() if hasattr(v, "tolist") else v)]


class TrackCatalog:
    """Per-tile track stores + geo-transforms discovered from S1-GRiTS
    ``catalog.parquet`` (columns: tile_id, zarr_path, crs, transform, width,
    height). Lets you go catalog → candidate (store, cy, cx) for a point."""

    def __init__(self, tiles: dict):
        self.tiles = tiles                       # {tile_id: [track dict, ...]}

    @classmethod
    def from_parquet(cls, catalog_path, base_path):
        import pandas as pd
        df = pd.read_parquet(catalog_path)
        # The catalog holds one row per (store, month). Dedup to one row per
        # store, or every candidate — and therefore every per-point pixel
        # read — is amplified by the number of months in that store.
        df = df.dropna(subset=["zarr_path"])
        df = df.drop_duplicates(subset=["tile_id", "zarr_path"])
        tiles = defaultdict(list)
        for _, r in df.iterrows():
            tile = r["tile_id"]
            tiles[tile].append({
                "store": os.path.join(base_path, tile, r["zarr_path"]),
                "crs": str(r["crs"]),
                "transform": _parse_transform(r["transform"]),
                "width": int(r["width"]), "height": int(r["height"]),
            })
        return cls(dict(tiles))

    def stores(self, tile) -> list[str]:
        return [t["store"] for t in self.tiles.get(tile, [])]

    def candidates_for_point(self, utm_x, utm_y, src_crs, tile=None):
        """[(store, cy, cx)] for every track whose grid contains the point.
        Reprojects the point into each track's CRS (per-track pixel coords).
        ``tile=None`` searches all tiles (MGRS tiles can overlap at edges)."""
        from pyproj import CRS, Transformer
        src = CRS.from_user_input(src_crs)
        tile_ids = [tile] if tile is not None else list(self.tiles.keys())
        out = []
        for tl in tile_ids:
            for tr in self.tiles.get(tl, []):
                tgt = CRS.from_user_input(tr["crs"])
                if tgt == src:
                    x, y = utm_x, utm_y
                else:
                    x, y = Transformer.from_crs(src, tgt, always_xy=True).transform(utm_x, utm_y)
                a = tr["transform"]                       # rasterio Affine [a,b,c,d,e,f]
                col = (x - a[2]) / a[0]
                row = (y - a[5]) / a[4]
                # floor, not round: the integer part of the fractional pixel
                # coordinate indexes the CONTAINING cell (a point at a pixel
                # centre is col 3.5 -> pixel 3; round() would put it in 4).
                r, c = int(np.floor(row)), int(np.floor(col))
                if 0 <= r < tr["height"] and 0 <= c < tr["width"]:
                    out.append((tr["store"], r, c))
        return out


class S1TrackExtractor:
    def __init__(self, bands=("VV_dB", "VH_dB"), *, t0=None, t1=None,
                 time_dim="time", storage_options=None, chunk=256, catalog=None):
        self.bands = list(bands)
        self.time_dim = time_dim
        self.t0 = np.datetime64(t0) if t0 is not None else None
        self.t1 = np.datetime64(t1) if t1 is not None else None
        self.storage_options = storage_options
        self.chunk = chunk
        self.catalog = catalog                   # optional TrackCatalog
        self._ds_cache = {}

    @classmethod
    def from_catalog(cls, catalog_path, base_path, **kwargs):
        """Build an extractor with a TrackCatalog attached, so you can go from a
        point's UTM coords straight to ``extract_batch`` (see ``candidates_for_point``)."""
        return cls(catalog=TrackCatalog.from_parquet(catalog_path, base_path), **kwargs)

    def candidates_for_point(self, utm_x, utm_y, src_crs, tile=None):
        if self.catalog is None:
            raise RuntimeError("no catalog — build with S1TrackExtractor.from_catalog(...)")
        return self.catalog.candidates_for_point(utm_x, utm_y, src_crs, tile)

    # ---- store access (each store opened at most once) --------------------
    def _open(self, store):
        if store not in self._ds_cache:
            import xarray as xr
            try:
                ds = xr.open_zarr(
                    store, storage_options=self.storage_options, consolidated=False,
                    chunks={self.time_dim: -1, "y": self.chunk, "x": self.chunk})
            except (ImportError, ValueError):
                # chunks= needs dask; without it fall back to xarray's plain
                # lazy backend arrays (single-pixel reads stay cheap).
                ds = xr.open_zarr(
                    store, storage_options=self.storage_options,
                    consolidated=False, chunks=None)
            self._ds_cache[store] = ds
        return self._ds_cache[store]

    def _series_from_store(self, store, cy, cx):
        """Raw (store, dates, {band: array}) at pixel, time-windowed. None if the
        pixel is outside this store's grid or the window is empty."""
        ds = self._open(store)
        ny, nx = ds.sizes["y"], ds.sizes["x"]
        if not (0 <= cy < ny and 0 <= cx < nx):
            return None
        tv = ds[self.time_dim].values
        keep = np.ones(len(tv), bool)
        if self.t0 is not None:
            keep &= tv >= self.t0
        if self.t1 is not None:
            keep &= tv <= self.t1
        idx = np.where(keep)[0]
        if idx.size == 0:
            return None
        sub = ds.isel(y=int(cy), x=int(cx), **{self.time_dim: idx})
        values = {b: np.asarray(sub[b].values, dtype="float32") for b in self.bands}
        return (store, tv[idx], values)

    def _finalize(self, best, point_id, tile, cy, cx, n_candidates) -> TrackSeries:
        store, dates, values = best
        finite = None
        for b in self.bands:
            f = np.isfinite(values[b])
            finite = f if finite is None else (finite & f)
        ts = TrackSeries(point_id=point_id, tile=tile, track=store, cy=cy, cx=cx,
                         dates=dates[finite],
                         values={b: values[b][finite] for b in self.bands},
                         n_valid=int(finite.sum()), n_candidates=n_candidates,
                         track_id=track_id_from_store(store))
        # INVARIANT (geometric stability): exactly one track store per series.
        assert isinstance(ts.track, str) and ts.track, "series must carry one track"
        return ts

    # ---- public API -------------------------------------------------------
    def extract(self, candidate_stores, cy, cx, *, point_id="", tile="") -> TrackSeries | None:
        """Extract a pixel's series from all candidate track stores and return
        the single best-covered track's series (or None if no track covers it)."""
        cands = []
        for store in candidate_stores:
            s = self._series_from_store(store, cy, cx)
            if s is not None:
                cands.append(s)
        best, _n = arbitrate(cands, self.bands)
        if best is None:
            return None
        return self._finalize(best, point_id, tile, cy, cx, len(cands))

    def extract_batch(self, points, *, verbose=False):
        """points: iterable of dict(point_id, tile, candidates=[(store, cy, cx)]).
        Opens each store once; returns list[TrackSeries] — one per resolvable
        point, each from exactly one track. Also returns stats.
        """
        # group pixel requests by store so each store is opened/streamed once
        by_store = defaultdict(list)                  # store -> [(pid, cy, cx)]
        meta = {}
        for p in points:
            meta[p["point_id"]] = p.get("tile", "")
            for store, cy, cx in p["candidates"]:
                by_store[store].append((p["point_id"], cy, cx))

        series_by_point = defaultdict(list)           # pid -> [(store, dates, values)]
        coords = {}
        for si, (store, reqs) in enumerate(sorted(by_store.items()), 1):
            if verbose:
                print(f"[track_extract] store {si}/{len(by_store)}: {store} ({len(reqs)} px)")
            for pid, cy, cx in reqs:
                s = self._series_from_store(store, cy, cx)
                if s is not None:
                    series_by_point[pid].append(s)
                    coords[pid] = (cy, cx)

        out, stats = [], {"resolved": 0, "recovered": 0, "no_data": 0}
        all_pids = {p["point_id"] for p in points}
        for pid in all_pids:
            cands = series_by_point.get(pid, [])
            best, n = arbitrate(cands, self.bands)
            if best is None:
                stats["no_data"] += 1
                continue
            if len(cands) > 1 and min(valid_count(c[2], self.bands) for c in cands) < n:
                stats["recovered"] += 1
            cy, cx = coords[pid]
            out.append(self._finalize(best, pid, meta.get(pid, ""), cy, cx, len(cands)))
            stats["resolved"] += 1
        return out, stats
