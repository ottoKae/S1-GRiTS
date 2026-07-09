"""Read-only data layer for the web UI: a stateless view over a workspace.

A *workspace* is an ``output.base_dir`` produced by the s1grits workflows:

    {root}/{TILE}/catalog.parquet
    {root}/{TILE}/{product_label}/zarr|cog|preview/...
    {root}/{TILE}/processing_report.json

Everything here is derived from those files on demand, with mtime-based
caching for the per-tile catalogs so repeated queries do not re-read parquet.
No state is written by this module.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Catalog columns exposed over the API (subset keeps payloads bounded and
# avoids leaking absolute paths embedded in free-form columns).
_ITEM_COLUMNS = [
    "item_id", "product_type", "product_label", "tile_id", "flight_direction",
    "track", "n_bursts", "n_scenes", "month", "datetime",
    "start_datetime", "end_datetime", "zarr_path", "cog_path", "preview_path",
    "bands", "crs", "width", "height", "processing_level", "geometry_group_id",
]


def _to_jsonable(value):
    """Convert catalog cell values to JSON-safe primitives."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        v = float(value)
        return None if math.isnan(v) else v
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.ndarray, list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _bounds_to_wgs84(crs: str, transform6: list, width: int, height: int):
    """Return [[south, west], [north, east]] Leaflet bounds, or None."""
    try:
        from rasterio.warp import transform_bounds
        from rasterio.transform import array_bounds
        from affine import Affine
        t = Affine(*[float(v) for v in transform6[:6]])
        left, bottom, right, top = array_bounds(int(height), int(width), t)
        w, s, e, n = transform_bounds(crs, "EPSG:4326", left, bottom, right, top)
        return [[s, w], [n, e]]
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("bounds_to_wgs84 failed (%s): %s", crs, exc)
        return None


class Workspace:
    """Cached, read-only access to one s1grits output directory."""

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Workspace root does not exist: {self.root}")
        self._lock = threading.Lock()
        # tile_id -> (catalog mtime_ns, DataFrame with derived columns)
        self._catalogs: dict[str, tuple[int, pd.DataFrame]] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def tile_ids(self) -> list[str]:
        out = []
        for p in sorted(self.root.iterdir()):
            if p.is_dir() and not p.name.startswith(".") and (p / "catalog.parquet").exists():
                out.append(p.name)
        return out

    def _load_tile_catalog(self, tile_id: str) -> pd.DataFrame | None:
        """Load one tile's catalog with derived columns, mtime-cached."""
        cat_path = self.root / tile_id / "catalog.parquet"
        try:
            mtime = cat_path.stat().st_mtime_ns
        except OSError:
            return None
        with self._lock:
            cached = self._catalogs.get(tile_id)
            if cached is not None and cached[0] == mtime:
                return cached[1]
        try:
            df = pd.read_parquet(cat_path)
        except Exception as exc:
            logger.warning("Unreadable catalog for %s: %s", tile_id, exc)
            return None
        df = df.copy()
        if "tile_id" not in df.columns:
            df["tile_id"] = tile_id
        # WGS84 bounds, computed once per unique grid (not per row).
        df["bounds4326"] = None
        if {"crs", "transform", "width", "height"}.issubset(df.columns):
            keys: dict[tuple, list | None] = {}
            bounds_col = []
            for crs, tfm, w, h in zip(df["crs"], df["transform"], df["width"], df["height"]):
                try:
                    key = (str(crs), tuple(float(v) for v in list(tfm)[:6]),
                           int(w), int(h))
                except (TypeError, ValueError):
                    bounds_col.append(None)
                    continue
                if key not in keys:
                    keys[key] = _bounds_to_wgs84(key[0], list(key[1]), key[2], key[3])
                bounds_col.append(keys[key])
            df["bounds4326"] = bounds_col
        with self._lock:
            self._catalogs[tile_id] = (mtime, df)
        return df

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def tiles(self) -> list[dict]:
        """Per-tile summary for the map layer."""
        out = []
        for tile_id in self.tile_ids():
            df = self._load_tile_catalog(tile_id)
            if df is None or df.empty:
                out.append({"tile_id": tile_id, "n_items": 0})
                continue
            bounds = None
            for b in df["bounds4326"]:
                if b is not None:
                    bounds = b
                    break
            months = sorted(
                {m for m in df.get("month", pd.Series(dtype=object)).dropna()}
            )
            report = None
            rp = self.root / tile_id / "processing_report.json"
            if rp.exists():
                try:
                    r = json.loads(rp.read_text())
                    report = {
                        "dropped_tracks": r.get("dropped_tracks", []),
                        "n_incomplete": len(r.get("incomplete_acquisitions", [])),
                    }
                except Exception:
                    report = None
            out.append({
                "tile_id": tile_id,
                "n_items": int(len(df)),
                "bounds4326": bounds,
                "product_types": sorted(df["product_type"].dropna().unique().tolist())
                if "product_type" in df.columns else [],
                "directions": sorted(df["flight_direction"].dropna().unique().tolist())
                if "flight_direction" in df.columns else [],
                "tracks": sorted({int(t) for t in df.get("track", pd.Series(dtype=object)).dropna()}),
                "month_min": months[0] if months else None,
                "month_max": months[-1] if months else None,
                "report": report,
            })
        return out

    def items(
        self,
        tile: str | None = None,
        product_type: str | None = None,
        direction: str | None = None,
        track: int | None = None,
        month_from: str | None = None,
        month_to: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict:
        """Filtered, paginated catalog records across the workspace."""
        tiles = [tile] if tile else self.tile_ids()
        frames = []
        for t in tiles:
            df = self._load_tile_catalog(t)
            if df is not None and not df.empty:
                frames.append(df)
        if not frames:
            return {"total": 0, "items": [], "months": {}}
        df = pd.concat(frames, ignore_index=True)

        if product_type and "product_type" in df.columns:
            df = df[df["product_type"] == product_type]
        if direction and "flight_direction" in df.columns:
            df = df[df["flight_direction"] == direction]
        if track is not None and "track" in df.columns:
            df = df[pd.to_numeric(df["track"], errors="coerce") == int(track)]
        if "month" in df.columns:
            if month_from:
                df = df[df["month"].fillna("") >= month_from]
            if month_to:
                df = df[df["month"].fillna("") <= month_to]

        # Month histogram over the FILTERED set (drives the timeline strip).
        months: dict[str, int] = {}
        if "month" in df.columns:
            months = df["month"].dropna().value_counts().sort_index().to_dict()
            months = {str(k): int(v) for k, v in months.items()}

        total = int(len(df))
        if "datetime" in df.columns:
            df = df.sort_values(["tile_id", "datetime"], na_position="last")
        page = df.iloc[offset: offset + max(0, int(limit))]

        items = []
        for _, row in page.iterrows():
            rec = {c: _to_jsonable(row[c]) for c in _ITEM_COLUMNS if c in page.columns}
            rec["bounds4326"] = row.get("bounds4326")
            items.append(rec)
        return {"total": total, "items": items, "months": months}

    # ------------------------------------------------------------------
    # Zarr point probe
    # ------------------------------------------------------------------

    def timeseries(
        self,
        tile: str,
        zarr_path: str,
        lon: float,
        lat: float,
        bands: list[str] | None = None,
        max_bands: int = 6,
    ) -> dict:
        """Per-pixel time series at (lon, lat) from one Zarr store.

        Reads O(n_time) elements per band via orthogonal indexing — never a
        spatial slab. Includes ``n_obs`` when present so the client can plot
        the confidence band under the radiometric series.
        """
        import zarr
        import pyproj

        store = self._resolve_asset(tile, zarr_path, must_be_file=False)
        g = zarr.open_group(str(store), mode="r", zarr_format=3)
        crs = str(g.attrs["crs"])
        tfm = [float(v) for v in g.attrs["transform"][:6]]
        res_x, minx = tfm[0], tfm[2]
        res_y, maxy = tfm[4], tfm[5]  # res_y negative
        proj = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        x, y = proj.transform(float(lon), float(lat))
        col = int((x - minx) // res_x)
        row = int((y - maxy) // res_y)
        height = int(g["y"].shape[0])
        width = int(g["x"].shape[0])
        if not (0 <= row < height and 0 <= col < width):
            raise ValueError("Point is outside this product's grid")

        times = pd.to_datetime(g["time"][:])
        order = np.argsort(times.values)  # stores can be append-ordered
        from s1grits.zarr_cf import band_data_vars
        available = band_data_vars(g)
        if bands:
            chosen = [b for b in bands if b in available]
        else:
            preferred = [b for b in ("VV_dB", "VH_dB", "Ratio", "RVI") if b in available]
            chosen = preferred or available
        if "n_obs" in available and "n_obs" not in chosen:
            chosen = chosen + ["n_obs"]
        chosen = chosen[:max_bands + 1]

        series: dict[str, list] = {}
        for band in chosen:
            vals = np.asarray(g[band][:, row, col], dtype="float64")[order]
            series[band] = [None if not np.isfinite(v) else round(float(v), 4)
                            for v in vals]
        return {
            "tile_id": tile,
            "zarr_path": zarr_path,
            "crs": crs,
            "pixel": {"row": row, "col": col, "x": x, "y": y},
            "time": [t.isoformat() for t in times[order]],
            "bands": series,
        }

    # ------------------------------------------------------------------
    # Asset footprints (for map overlays)
    # ------------------------------------------------------------------

    def asset_bounds(self, tile: str, relpath: str) -> dict:
        """True WGS84 footprint of a served asset, for Leaflet overlays.

        The catalog row's ``transform/width/height`` describe the FULL
        master grid (the burst-union support grid), but COG/preview assets
        cover only the tile-clipped crop — overlaying a preview on the grid
        bounds misplaces and stretches it. Resolution order:

        1. ``.tif`` → the file's own georeferencing (authoritative);
        2. ``.png`` → the sibling COG's georeferencing when it exists
           (previews are exported alongside COGs with the same stem);
        3. otherwise → the tile-clip estimate: MGRS tile bbox (in the item
           CRS) ∩ grid bounds, mirroring the writer's clip;
        4. last resort → the grid bounds themselves.

        Returns ``{"bounds4326": [[s, w], [n, e]] | None, "source": str}``.
        Results are cached by (path, mtime).
        """
        path = self._resolve_asset(tile, relpath)
        cache_key = (str(path), path.stat().st_mtime_ns)
        with self._lock:
            if not hasattr(self, "_asset_bounds_cache"):
                self._asset_bounds_cache: dict = {}
            hit = self._asset_bounds_cache.get(cache_key)
        if hit is not None:
            return hit

        result = None
        # (1)/(2): a georeferenced file, directly or via the sibling COG.
        geo_path = None
        if path.suffix.lower() in (".tif", ".tiff"):
            geo_path = path
        elif path.suffix.lower() == ".png":
            sibling = path.parent.parent / "cog" / (path.stem + ".tif")
            if sibling.is_file():
                geo_path = sibling
        if geo_path is not None:
            try:
                import rasterio
                from rasterio.warp import transform_bounds
                with rasterio.open(geo_path) as src:
                    w, s, e, n = transform_bounds(
                        src.crs, "EPSG:4326", *src.bounds
                    )
                result = {"bounds4326": [[s, w], [n, e]], "source": "georef"}
            except Exception as exc:
                logger.debug("asset_bounds georef failed for %s: %s",
                             geo_path, exc)

        # (3): tile-clip estimate from the catalog row's grid + MGRS bbox.
        if result is None:
            row = self._find_row_for_asset(tile, relpath)
            if row is not None:
                result = self._tile_clip_bounds(tile, row)

        if result is None:
            result = {"bounds4326": None, "source": "unknown"}
        with self._lock:
            self._asset_bounds_cache[cache_key] = result
        return result

    def _find_row_for_asset(self, tile: str, relpath: str):
        df = self._load_tile_catalog(tile)
        if df is None or df.empty:
            return None
        for col in ("preview_path", "cog_path", "zarr_path"):
            if col in df.columns:
                hits = df[df[col] == relpath]
                if len(hits):
                    return hits.iloc[0]
        return None

    def _tile_clip_bounds(self, tile: str, row) -> dict | None:
        """MGRS tile bbox ∩ grid bounds in WGS84 (mirrors the writer's clip)."""
        try:
            from affine import Affine
            from rasterio.transform import array_bounds
            from rasterio.warp import transform_bounds
            from s1grits.asf_output_writing import _get_mgrs_tile_geometry_wkt
            from shapely import wkt as shp_wkt

            crs = str(row["crs"])
            t = Affine(*[float(v) for v in list(row["transform"])[:6]])
            gl, gb, gr, gt = array_bounds(int(row["height"]),
                                          int(row["width"]), t)
            tw, ts, te, tn = transform_bounds(
                "EPSG:4326", crs,
                *shp_wkt.loads(_get_mgrs_tile_geometry_wkt(tile)).bounds,
            )
            # Intersect (in the item CRS), exactly like the writer's crop.
            il, ib = max(gl, tw), max(gb, ts)
            ir, it = min(gr, te), min(gt, tn)
            if ir <= il or it <= ib:  # no overlap -> the clip never fired
                w, s, e, n = transform_bounds(crs, "EPSG:4326", gl, gb, gr, gt)
                return {"bounds4326": [[s, w], [n, e]], "source": "grid"}
            w, s, e, n = transform_bounds(crs, "EPSG:4326", il, ib, ir, it)
            return {"bounds4326": [[s, w], [n, e]], "source": "tile-clip"}
        except Exception as exc:
            logger.debug("tile-clip bounds failed for %s: %s", tile, exc)
            b = row.get("bounds4326")
            return {"bounds4326": b, "source": "grid"} if b else None

    # ------------------------------------------------------------------
    # Safe asset access
    # ------------------------------------------------------------------

    def _resolve_asset(self, tile: str, relpath: str, must_be_file: bool = True) -> Path:
        """Resolve a catalog-relative asset path, confined to the workspace.

        Raises ``PermissionError`` on traversal attempts and
        ``FileNotFoundError`` when the target does not exist.
        """
        if not tile or "/" in tile or "\\" in tile or tile.startswith("."):
            raise PermissionError("Invalid tile id")
        tile_dir = (self.root / tile).resolve()
        if not tile_dir.is_relative_to(self.root) or not tile_dir.is_dir():
            raise FileNotFoundError(f"Unknown tile: {tile}")
        target = (tile_dir / relpath).resolve()
        if not target.is_relative_to(tile_dir):
            raise PermissionError("Path escapes the workspace")
        if any(part.startswith(".") for part in target.relative_to(self.root).parts):
            raise PermissionError("Hidden paths are not served")
        if not target.exists() or (must_be_file and not target.is_file()):
            raise FileNotFoundError(str(target.relative_to(self.root)))
        return target

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        import shutil
        tiles = self.tiles()
        du = shutil.disk_usage(self.root)
        return {
            "root": str(self.root),
            "n_tiles": len(tiles),
            "n_items": sum(t.get("n_items", 0) for t in tiles),
            "disk_free_gb": round(du.free / 1024**3, 1),
            "disk_total_gb": round(du.total / 1024**3, 1),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
