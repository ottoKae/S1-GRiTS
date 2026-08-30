"""Chinese-console service layer for the v3 FastAPI application.

The browser is a client of the public v3 CLI contract.  This module owns only
input validation, MGRS display queries, catalog browsing, and translation of a
confirmed plan into whitelisted ``process_scenes`` jobs.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from s1grits.canonical_catalog_schema import (
    CANONICAL_CATALOG_COLUMNS,
    SCHEMA_VERSION,
)
from s1grits.resampling import (
    resolve_resampling_method,
    validate_target_resolution,
)


TILE_RE = re.compile(r"^\d{2}[C-HJ-NP-X][A-HJ-NP-Z]{2}$")
STATIC_LAYER_NAMES = (
    "local_inc_angle",
    "inc_angle",
    "ls_map",
    "number_of_looks",
    "rtc_anf_beta0",
    "rtc_anf_sigma0",
)


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class ChineseConsole:
    """Stateful adapter shared by all Chinese-console API routes."""

    def __init__(self, root: Path | str, jobs, *, max_tiles: int = 200,
                 catalog_roots: list[str] | None = None):
        self.root = Path(root).resolve()
        self.jobs = jobs
        self.max_tiles = max(1, int(max_tiles))
        self.mgrs_path = Path(__file__).resolve().parents[1] / "data" / "mgrs.parquet"
        self.mgrs_min_zoom = 4
        self.mgrs_max_features = 5000
        self._lock = threading.RLock()
        self._mgrs = None
        self._mgrs_version = ""
        self._map_cache: OrderedDict[tuple, dict] = OrderedDict()
        self._plans: dict[str, dict] = {}
        self._catalog_roots_file = self.root / ".webapp" / "catalog_roots.json"
        self._catalog_roots = self._build_catalog_roots(catalog_roots or [])
        self._load_persisted_catalog_roots()

    max_aoi_upload_bytes = 50 * 1024 * 1024
    max_aoi_expanded_bytes = 200 * 1024 * 1024
    max_aoi_archive_entries = 20
    max_aoi_features = 10_000
    max_catalog_candidate_entries = 1000
    max_catalog_candidate_directories = 200
    max_catalog_candidates = 100
    max_catalog_folder_entries = 2000
    max_catalog_folder_directories = 200

    # ------------------------------------------------------------------
    # MGRS dictionary and viewport endpoint
    # ------------------------------------------------------------------
    def _load_mgrs(self):
        stat = self.mgrs_path.stat()
        version = f"{stat.st_size:x}-{stat.st_mtime_ns:x}"
        with self._lock:
            if self._mgrs is not None and self._mgrs_version == version:
                return self._mgrs
            import geopandas as gpd

            frame = gpd.read_parquet(self.mgrs_path)
            required = {"mgrs_tile_id", "utm_epsg", "geometry"}
            missing = required.difference(frame.columns)
            if missing:
                raise ValueError(f"MGRS 空间字典缺少字段：{sorted(missing)}")
            if frame.crs is None:
                raise ValueError("MGRS 空间字典缺少 CRS")
            frame = frame.to_crs("EPSG:4326")
            if frame.empty or frame.geometry.is_empty.any() or not frame.geometry.is_valid.all():
                raise ValueError("MGRS 空间字典包含空或无效几何")
            if frame["mgrs_tile_id"].astype(str).duplicated().any():
                raise ValueError("MGRS 空间字典包含重复 mgrs_tile_id")
            frame.sindex
            self._mgrs = frame
            self._mgrs_version = version
            self._map_cache.clear()
            return frame

    @staticmethod
    def _geometry(geometry) -> dict:
        interface = geometry.__geo_interface__

        def rounded(value):
            if isinstance(value, (list, tuple)):
                return [rounded(v) for v in value]
            return round(float(value), 6)

        return {"type": interface["type"], "coordinates": rounded(interface["coordinates"])}

    def mgrs_map(self, bbox_text: str, zoom: int) -> dict:
        try:
            bbox = tuple(float(v.strip()) for v in bbox_text.split(","))
        except (TypeError, ValueError) as exc:
            raise ValueError("bbox 必须是 west,south,east,north") from exc
        if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
            raise ValueError("bbox 必须包含四个有限数值")
        west, south, east, north = bbox
        if not (-180 <= west <= 180 and -180 <= east <= 180):
            raise ValueError("bbox 经度超出范围")
        if not (-85.051129 <= south < north <= 85.051129) or west == east:
            raise ValueError("bbox 纬度或东西边界无效")
        zoom = int(zoom)
        if not 0 <= zoom <= 22:
            raise ValueError("zoom 必须是 0—22")

        frame = self._load_mgrs()
        rounded_bbox = tuple(round(v, 6) for v in bbox)
        key = (self._mgrs_version, rounded_bbox, zoom)
        with self._lock:
            if key in self._map_cache:
                self._map_cache.move_to_end(key)
                return self._map_cache[key]
        base = {
            "schema_version": 1,
            "source": "s1grits/data/mgrs.parquet",
            "source_version": self._mgrs_version,
            "source_crs": "EPSG:4326",
            "display_crs": "EPSG:3857",
            "min_zoom": self.mgrs_min_zoom,
            "max_features": self.mgrs_max_features,
            "bbox": list(rounded_bbox),
            "zoom": zoom,
        }
        from shapely.geometry import box

        boxes = [box(west, south, east, north)] if west < east else [
            box(west, south, 180, north),
            box(-180, south, east, north),
        ]
        positions: set[int] = set()
        for query_box in boxes:
            positions.update(
                int(v) for v in frame.sindex.query(query_box, predicate="intersects")
            )
        subset = frame.iloc[sorted(positions)]
        subset = subset.drop_duplicates("mgrs_tile_id").sort_values("mgrs_tile_id")
        count = len(subset)

        if zoom < self.mgrs_min_zoom:
            payload = {
                **base,
                "visible": False,
                "reason": "zoom",
                "count": count,
                "returned": 0,
                "truncated": False,
                "message": (
                    f"当前视窗包含 {count} 个 MGRS 格网；"
                    f"缩放至 {self.mgrs_min_zoom} 级后显示边界"
                ),
                "features": {"type": "FeatureCollection", "features": []},
            }
        else:
            truncated = count > self.mgrs_max_features
            features = [] if truncated else [
                {
                    "type": "Feature",
                    "properties": {
                        "tile_id": str(row.mgrs_tile_id),
                        "utm_epsg": int(row.utm_epsg),
                    },
                    "geometry": self._geometry(row.geometry),
                }
                for row in subset.itertuples(index=False)
            ]
            payload = {
                **base,
                "visible": True,
                "reason": "feature_limit" if truncated else None,
                "count": count,
                "returned": len(features),
                "truncated": truncated,
                "features": {"type": "FeatureCollection", "features": features},
            }
        with self._lock:
            self._map_cache[key] = payload
            self._map_cache.move_to_end(key)
            while len(self._map_cache) > 128:
                self._map_cache.popitem(last=False)
        return payload

    # ------------------------------------------------------------------
    # AOI adapters and candidate MGRS lookup
    # ------------------------------------------------------------------
    @staticmethod
    def _polygonal_geometry(value):
        """Return the polygonal part of any GeoJSON-like object."""
        from shapely import make_valid
        from shapely.geometry import GeometryCollection, shape
        from shapely.ops import unary_union

        if not value:
            raise ValueError("AOI 几何为空")
        kind = value.get("type") if isinstance(value, dict) else None
        if kind == "FeatureCollection":
            geometries = [
                ChineseConsole._polygonal_geometry(feature)
                for feature in value.get("features") or []
            ]
            if not geometries:
                raise ValueError("AOI FeatureCollection 不包含要素")
            return unary_union(geometries)
        if kind == "Feature":
            return ChineseConsole._polygonal_geometry(value.get("geometry"))
        geometry = make_valid(shape(value))
        if geometry.geom_type in ("Polygon", "MultiPolygon"):
            return geometry
        if isinstance(geometry, GeometryCollection):
            polygons = [
                part for part in geometry.geoms
                if part.geom_type in ("Polygon", "MultiPolygon") and not part.is_empty
            ]
            if polygons:
                return unary_union(polygons)
        raise ValueError("AOI 只支持 Polygon 或 MultiPolygon")

    def _aoi_result(self, geometry, *, source: str, source_crs: str) -> dict:
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError("AOI 几何为空或无法修复")
        minx, miny, maxx, maxy = geometry.bounds
        if not (-180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90):
            raise ValueError("AOI 转换到 EPSG:4326 后超出经纬度范围")
        frame = self._load_mgrs()
        positions = frame.sindex.query(geometry, predicate="intersects")
        subset = frame.iloc[sorted({int(v) for v in positions})].copy()
        if not subset.empty:
            subset = subset[
                subset.geometry.map(
                    lambda tile: not tile.intersection(geometry).is_empty
                    and tile.intersection(geometry).area > 0
                )
            ]
        subset = subset.drop_duplicates("mgrs_tile_id").sort_values("mgrs_tile_id")
        features = [
            {
                "type": "Feature",
                "properties": {
                    "tile_id": str(row.mgrs_tile_id),
                    "utm_epsg": int(row.utm_epsg),
                },
                "geometry": self._geometry(row.geometry),
            }
            for row in subset.itertuples(index=False)
        ]
        return {
            "geometry": self._geometry(geometry),
            "source": source,
            "source_crs": source_crs,
            "normalized_crs": "EPSG:4326",
            "bbox": [round(float(v), 6) for v in geometry.bounds],
            "candidate_tiles": [f["properties"]["tile_id"] for f in features],
            "candidate_count": len(features),
            "tile_features": features,
            "max_task_tiles": self.max_tiles,
        }

    def resolve_aoi(self, value: dict, *, source: str = "GeoJSON") -> dict:
        geometry = self._polygonal_geometry(value)
        return self._aoi_result(geometry, source=source, source_crs="EPSG:4326")

    def resolve_aoi_upload(self, uploads: list[tuple[str, bytes]]) -> dict:
        """Safely normalize GeoJSON or Shapefile uploads to EPSG:4326."""
        if not uploads:
            raise ValueError("请选择 AOI 文件")
        if len(uploads) > self.max_aoi_archive_entries:
            raise ValueError(f"AOI 文件最多 {self.max_aoi_archive_entries} 个")
        total = sum(len(data) for _, data in uploads)
        if total > self.max_aoi_upload_bytes:
            raise ValueError("AOI 上传总大小不能超过 50 MiB")
        for name, _ in uploads:
            if not name or Path(name).name != name or "/" in name or "\\" in name:
                raise ValueError("AOI 文件名包含不安全路径")

        if len(uploads) == 1 and Path(uploads[0][0]).suffix.lower() in (".json", ".geojson"):
            try:
                value = json.loads(uploads[0][1].decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"GeoJSON 解析失败：{exc}") from exc
            return self.resolve_aoi(value, source=uploads[0][0])

        with tempfile.TemporaryDirectory(prefix="s1grits-aoi-") as temp_name:
            temp = Path(temp_name)
            components: list[tuple[str, bytes]] = []
            if len(uploads) == 1 and Path(uploads[0][0]).suffix.lower() == ".zip":
                import io

                try:
                    with zipfile.ZipFile(io.BytesIO(uploads[0][1])) as archive:
                        infos = [info for info in archive.infolist() if not info.is_dir()]
                        if len(infos) > self.max_aoi_archive_entries:
                            raise ValueError(
                                f"Shapefile ZIP 解压后最多 {self.max_aoi_archive_entries} 个文件"
                            )
                        if sum(info.file_size for info in infos) > self.max_aoi_expanded_bytes:
                            raise ValueError("Shapefile ZIP 解压后不能超过 200 MiB")
                        for info in infos:
                            pure = Path(info.filename)
                            mode = (info.external_attr >> 16) & 0o170000
                            if (
                                pure.is_absolute() or ".." in pure.parts
                                or mode == 0o120000
                            ):
                                raise ValueError("Shapefile ZIP 包含不安全路径或符号链接")
                            components.append((pure.name, archive.read(info)))
                except zipfile.BadZipFile as exc:
                    raise ValueError("上传文件不是有效的 ZIP") from exc
            else:
                components = uploads

            allowed = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
            if any(Path(name).suffix.lower() not in allowed for name, _ in components):
                raise ValueError("Shapefile 仅接受 .shp/.shx/.dbf/.prj/.cpg 组件")
            shp_files = [name for name, _ in components if Path(name).suffix.lower() == ".shp"]
            if len(shp_files) != 1:
                raise ValueError("每次必须且只能上传一套 Shapefile")
            stem = Path(shp_files[0]).stem.lower()
            selected = {
                Path(name).suffix.lower(): (name, data)
                for name, data in components if Path(name).stem.lower() == stem
            }
            required = {".shp", ".shx", ".dbf", ".prj"}
            missing = sorted(required.difference(selected))
            if missing:
                raise ValueError("Shapefile 缺少必要组件：" + "、".join(missing))
            for suffix, (_, data) in selected.items():
                (temp / f"aoi{suffix}").write_bytes(data)

            import geopandas as gpd
            from shapely.ops import unary_union

            frame = gpd.read_file(temp / "aoi.shp")
            if len(frame) > self.max_aoi_features:
                raise ValueError(f"AOI 要素不能超过 {self.max_aoi_features} 个")
            if frame.crs is None:
                raise ValueError("Shapefile 缺少可识别的 .prj 坐标参考系")
            source_crs = frame.crs.to_string()
            frame = frame.to_crs("EPSG:4326")
            geometries = [
                self._polygonal_geometry(geometry.__geo_interface__)
                for geometry in frame.geometry if geometry is not None and not geometry.is_empty
            ]
            if not geometries:
                raise ValueError("Shapefile 不包含有效面要素")
            geometry = unary_union(geometries)
            return self._aoi_result(
                geometry,
                source=uploads[0][0] if len(uploads) == 1 else "Shapefile 组件",
                source_crs=source_crs,
            )

    # ------------------------------------------------------------------
    # Safe directories and catalog access
    # ------------------------------------------------------------------
    @staticmethod
    def _catalog_root_id(path: Path) -> str:
        folded = str(path).casefold()
        return "catalog-" + hashlib.sha256(folded.encode("utf-8")).hexdigest()[:12]

    def _add_catalog_root(
        self,
        roots: dict[str, dict],
        path: Path,
        *,
        label: str = "",
        removable: bool,
        source: str,
    ) -> dict:
        folded = str(path).casefold()
        for item in roots.values():
            if str(item["path"]).casefold() == folded:
                return item
        root_id = self._catalog_root_id(path)
        item = {
            "root_id": root_id,
            "label": label.strip() or path.name or str(path),
            "path": path,
            "removable": bool(removable),
            "source": source,
        }
        roots[root_id] = item
        return item

    def _build_catalog_roots(self, specifications: list[str]) -> dict[str, dict]:
        roots = {
            "workspace": {
                "root_id": "workspace",
                "label": "服务器输出",
                "path": self.root,
                "removable": False,
                "source": "workspace",
            }
        }
        for raw in specifications:
            text = str(raw or "").strip()
            if not text:
                raise ValueError("--catalog-root 不能为空")
            label = ""
            path_text = text
            direct_path = Path(text).expanduser()
            if not direct_path.is_dir() and "=" in text:
                candidate_label, candidate_path = text.split("=", 1)
                if candidate_label.strip() and candidate_path.strip():
                    label, path_text = candidate_label.strip(), candidate_path.strip()
            path = Path(path_text).expanduser().resolve()
            if not path.is_dir():
                raise ValueError(f"本地数据根不存在或不是目录：{path}")
            self._add_catalog_root(
                roots,
                path,
                label=label,
                removable=False,
                source="cli",
            )
        return roots

    def _load_persisted_catalog_roots(self) -> None:
        if not self._catalog_roots_file.is_file():
            return
        try:
            payload = json.loads(self._catalog_roots_file.read_text(encoding="utf-8"))
            entries = payload.get("roots", []) if isinstance(payload, dict) else []
            if not isinstance(entries, list):
                return
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                path_text = str(entry.get("path") or "").strip()
                if not path_text:
                    continue
                path = Path(path_text).expanduser().resolve()
                self._add_catalog_root(
                    self._catalog_roots,
                    path,
                    label=str(entry.get("label") or ""),
                    removable=True,
                    source="gui",
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # A stale/corrupt preference file must not prevent the web service
            # from starting. The user can register the data root again.
            return

    def _persist_catalog_roots(self) -> None:
        entries = [
            {"label": item["label"], "path": str(item["path"])}
            for item in self._catalog_roots.values()
            if item.get("source") == "gui"
        ]
        self._catalog_roots_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._catalog_roots_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"version": 1, "roots": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._catalog_roots_file)

    @staticmethod
    def _public_catalog_root(item: dict) -> dict:
        return {
            "root_id": item["root_id"],
            "label": item["label"],
            "path_display": str(item["path"]),
            "exists": item["path"].is_dir(),
            "writable": False,
            "removable": bool(item.get("removable")),
            "catalog_available": (item["path"] / "catalog.parquet").is_file(),
        }

    def catalog_roots(self) -> dict:
        return {
            "roots": [self._public_catalog_root(item) for item in self._catalog_roots.values()]
        }

    @staticmethod
    def _catalog_folder_roots() -> list[dict]:
        """Return roots suitable for the server-local folder browser."""
        if os.name != "nt":
            return [{"name": "/", "path": "/", "drive_type": "root"}]

        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_logical_drives = kernel32.GetLogicalDrives
        get_logical_drives.argtypes = []
        get_logical_drives.restype = ctypes.c_uint32
        get_drive_type = kernel32.GetDriveTypeW
        get_drive_type.argtypes = [ctypes.c_wchar_p]
        get_drive_type.restype = ctypes.c_uint
        mask = int(get_logical_drives())
        if not mask:
            raise OSError(
                ctypes.get_last_error(),
                "Windows \u65e0\u6cd5\u5217\u4e3e\u53ef\u7528\u76d8\u7b26",
            )
        drive_types = {
            0: "unknown",
            1: "invalid",
            2: "removable",
            3: "fixed",
            4: "network",
            5: "cdrom",
            6: "ramdisk",
        }
        roots = []
        for index in range(26):
            if mask & (1 << index):
                name = f"{chr(ord('A') + index)}:"
                path = name + "\\"
                roots.append({
                    "name": name,
                    "path": path,
                    "drive_type": drive_types.get(int(get_drive_type(path)), "unknown"),
                })
        return roots

    def catalog_folders(self, path_text: str = "") -> dict:
        """Browse absolute directories without exposing files or granting writes."""
        raw = str(path_text or "").strip()
        limits = {
            "entries": self.max_catalog_folder_entries,
            "directories": self.max_catalog_folder_directories,
        }
        if not raw:
            drives = self._catalog_folder_roots()
            return {
                "schema_version": 1,
                "mode": "drives",
                "path": "",
                "name": "",
                "parent": None,
                "catalog_available": False,
                "drives": drives,
                "directories": [],
                "entries_scanned": len(drives),
                "directories_scanned": 0,
                "returned": len(drives),
                "skipped": 0,
                "truncated": False,
                "limits": limits,
            }

        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError("\u8bf7\u4f7f\u7528\u672c\u673a\u7edd\u5bf9\u8def\u5f84\u6d4f\u89c8\u6587\u4ef6\u5939")
        try:
            current = candidate.resolve(strict=True)
            current_stat = current.stat()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"\u76ee\u5f55\u4e0d\u5b58\u5728: {candidate}") from exc
        except PermissionError as exc:
            raise PermissionError(f"\u65e0\u6743\u8bbf\u95ee\u76ee\u5f55: {candidate}") from exc
        if not stat.S_ISDIR(current_stat.st_mode):
            raise ValueError(f"\u8def\u5f84\u4e0d\u662f\u6587\u4ef6\u5939: {current}")

        directories = []
        entries_scanned = 0
        directories_scanned = 0
        skipped = 0
        truncated = False
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entries_scanned >= self.max_catalog_folder_entries:
                        truncated = True
                        break
                    entries_scanned += 1
                    try:
                        is_directory = entry.is_dir(follow_symlinks=True)
                    except OSError:
                        skipped += 1
                        continue
                    if not is_directory:
                        continue
                    if directories_scanned >= self.max_catalog_folder_directories:
                        truncated = True
                        break
                    directories_scanned += 1
                    child = Path(entry.path)
                    try:
                        catalog_available = (child / "catalog.parquet").is_file()
                    except OSError:
                        catalog_available = False
                        skipped += 1
                    directories.append({
                        "name": entry.name,
                        "path": str(child),
                        "catalog_available": bool(catalog_available),
                    })
        except PermissionError as exc:
            raise PermissionError(f"\u65e0\u6743\u8bfb\u53d6\u76ee\u5f55: {current}") from exc

        directories.sort(key=lambda item: item["name"].casefold())
        parent_path = current.parent
        parent = None if parent_path == current else str(parent_path)
        try:
            catalog_available = (current / "catalog.parquet").is_file()
        except OSError:
            catalog_available = False
        return {
            "schema_version": 1,
            "mode": "directory",
            "path": str(current),
            "name": current.name or current.anchor,
            "parent": parent,
            "catalog_available": bool(catalog_available),
            "drives": [],
            "directories": directories,
            "entries_scanned": entries_scanned,
            "directories_scanned": directories_scanned,
            "returned": len(directories),
            "skipped": skipped,
            "truncated": truncated,
            "limits": limits,
        }

    def register_catalog_root(self, path_text: str, label: str = "") -> dict:
        raw = str(path_text or "").strip()
        if not raw:
            raise ValueError("请选择或输入本地数据文件夹")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError("请使用本机绝对路径，例如 D:\\data\\s1_cube")
        path = candidate.resolve()
        if not path.is_dir():
            raise ValueError(f"本地数据文件夹不存在或不是目录：{path}")
        with self._lock:
            existing_ids = set(self._catalog_roots)
            item = self._add_catalog_root(
                self._catalog_roots,
                path,
                label=label,
                removable=True,
                source="gui",
            )
            if item.get("source") == "gui":
                try:
                    self._persist_catalog_roots()
                except OSError:
                    if item["root_id"] not in existing_ids:
                        del self._catalog_roots[item["root_id"]]
                    raise
        return {"root": self._public_catalog_root(item), **self.catalog_roots()}

    def remove_catalog_root(self, root_id: str) -> dict:
        key = str(root_id or "").strip()
        with self._lock:
            item = self._catalog_roots.get(key)
            if item is None:
                raise ValueError(f"未知本地数据根：{key}")
            if not item.get("removable") or item.get("source") != "gui":
                raise ValueError("服务器输出和命令行配置的数据根不能从网页移除")
            del self._catalog_roots[key]
            try:
                self._persist_catalog_roots()
            except OSError:
                self._catalog_roots[key] = item
                raise
        return self.catalog_roots()

    def resolve_catalog(self, root_id: str, relative: str = "") -> tuple[Path, dict]:
        key = str(root_id or "workspace").strip()
        if key not in self._catalog_roots:
            raise ValueError(f"未知本地数据根：{key}")
        item = self._catalog_roots[key]
        root = item["path"]
        if not root.is_dir():
            raise ValueError(f"本地数据文件夹已经移动、删除或当前不可访问：{root}")
        text = str(relative or "").strip().replace("\\", "/").strip("/")
        if not text:
            return root, item
        rel = Path(text)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("本地数据目录必须位于已授权数据根内")
        target = (root / rel).resolve()
        if target != root and not target.is_relative_to(root):
            raise ValueError("本地数据目录越过已授权数据根")
        return target, item

    def catalog_browse(self, root_id: str = "workspace", relative: str = "") -> dict:
        current, item = self.resolve_catalog(root_id, relative)
        root = item["path"]
        if not current.is_dir():
            raise FileNotFoundError(f"目录不存在：{relative}")
        rel = "" if current == root else current.relative_to(root).as_posix()
        directories = []
        for child in sorted(current.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                resolved_child = child.resolve()
                if resolved_child != root and not resolved_child.is_relative_to(root):
                    continue
                directories.append({
                    "name": child.name,
                    "path": child.relative_to(root).as_posix(),
                    "catalog_available": (child / "catalog.parquet").is_file(),
                })
        parent = None if current == root else (
            "" if current.parent == root else current.parent.relative_to(root).as_posix()
        )
        return {
            "root_id": item["root_id"],
            "root_label": item["label"],
            "root": str(root),
            "path": rel,
            "parent": parent,
            "directories": directories,
            "catalog_mode": True,
            "catalog_available": (current / "catalog.parquet").is_file(),
        }

    def catalog_candidates(self, root_id: str) -> dict:
        """Find cube roots exactly one directory below an authorised root."""
        root, item = self.resolve_catalog(root_id, "")
        candidates = []
        entries_scanned = 0
        directories_scanned = 0
        entries_skipped = 0
        truncated = False
        for child in root.iterdir():
            if entries_scanned >= self.max_catalog_candidate_entries:
                truncated = True
                break
            entries_scanned += 1
            try:
                is_directory = child.is_dir()
            except OSError:
                entries_skipped += 1
                continue
            if not is_directory or child.name.startswith(".") or child.name.lower().endswith(".zarr"):
                continue
            if directories_scanned >= self.max_catalog_candidate_directories:
                truncated = True
                break
            directories_scanned += 1
            try:
                resolved = child.resolve()
                has_catalog = (resolved / "catalog.parquet").is_file()
            except OSError:
                entries_skipped += 1
                continue
            if resolved != root and not resolved.is_relative_to(root):
                continue
            if has_catalog:
                candidates.append({
                    "name": child.name,
                    "path": child.relative_to(root).as_posix(),
                    "catalog": str(resolved / "catalog.parquet"),
                })
                if len(candidates) >= self.max_catalog_candidates:
                    truncated = True
                    break
        candidates.sort(key=lambda value: value["name"].casefold())
        return {
            "root_id": item["root_id"],
            "root_label": item["label"],
            "root": str(root),
            "root_catalog_available": (root / "catalog.parquet").is_file(),
            "candidates": candidates,
            "candidate_count": len(candidates),
            "entries_scanned": entries_scanned,
            "entries_skipped": entries_skipped,
            "directories_scanned": directories_scanned,
            "truncated": truncated,
            "limits": {
                "entries": self.max_catalog_candidate_entries,
                "directories": self.max_catalog_candidate_directories,
                "candidates": self.max_catalog_candidates,
            },
        }

    def resolve_relative(self, relative: str, *, allow_root: bool = True) -> Path:
        text = str(relative or "").strip().replace("\\", "/").strip("/")
        if not text:
            if allow_root:
                return self.root
            raise ValueError("目录不能为空")
        rel = Path(text)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("目录必须位于服务工作区内")
        target = (self.root / rel).resolve()
        if target != self.root and not target.is_relative_to(self.root):
            raise ValueError("目录越过服务工作区")
        return target

    def browse(self, relative: str, catalog_mode: bool = False) -> dict:
        current = self.resolve_relative(relative)
        if not current.is_dir():
            raise FileNotFoundError(f"目录不存在：{relative}")
        rel = "" if current == self.root else current.relative_to(self.root).as_posix()
        directories = []
        for child in sorted(current.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                child_rel = child.relative_to(self.root).as_posix()
                directories.append({
                    "name": child.name,
                    "path": child_rel,
                    "catalog_available": (child / "catalog.parquet").is_file(),
                })
        parent = None if current == self.root else (
            "" if current.parent == self.root else current.parent.relative_to(self.root).as_posix()
        )
        return {
            "root": str(self.root),
            "path": rel,
            "parent": parent,
            "directories": directories,
            "catalog_mode": bool(catalog_mode),
            "catalog_available": (current / "catalog.parquet").is_file(),
        }

    def mkdir(self, parent: str, name: str) -> dict:
        if not re.fullmatch(r"[\w\-.\u4e00-\u9fff]{1,80}", str(name or "")):
            raise ValueError("目录名只能包含中英文、数字、下划线、连字符和点")
        base = self.resolve_relative(parent)
        target = self.resolve_relative(
            (Path(parent) / name).as_posix() if parent else name,
            allow_root=False,
        )
        if target.parent != base:
            raise ValueError("目录层级无效")
        target.mkdir(exist_ok=False)
        return {"path": target.relative_to(self.root).as_posix()}

    def catalog_inspect(self, relative: str, root_id: str = "workspace") -> dict:
        cube, root_info = self.resolve_catalog(root_id, relative)
        root = root_info["path"]
        path = cube / "catalog.parquet"
        if not path.is_file():
            return {
                "valid": False,
                "root_id": root_info["root_id"],
                "output": relative,
                "catalog": str(path),
                "issues": ["所选目录根部不存在 catalog.parquet"],
                "warnings": [],
            }
        from s1grits.analysis.catalog import validate_catalog

        try:
            result = validate_catalog(path)
            frame = pd.read_parquet(path)
        except Exception as exc:
            # Parquet engines expose several unrelated exception classes.
            # A corrupt/local-incompatible catalog is a validation result,
            # not an unhandled web-service failure.
            return {
                "valid": False,
                "root_id": root_info["root_id"],
                "root_label": root_info["label"],
                "output": relative,
                "catalog": str(path),
                "issues": [f"catalog.parquet 无法读取：{exc}"],
                "warnings": [],
            }
        missing = sorted(set(CANONICAL_CATALOG_COLUMNS).difference(frame.columns))
        versions = sorted(
            int(v) for v in frame.get("schema_version", pd.Series(dtype="int64")).dropna().unique()
        )
        issues = list(result.get("issues") or [])
        if missing:
            issues.append(f"缺少 Schema v{SCHEMA_VERSION} 字段：{', '.join(missing)}")
        if versions and versions != [SCHEMA_VERSION]:
            issues.append(f"Catalog 版本为 {versions}，服务要求 Schema v{SCHEMA_VERSION}")
        return {
            **result,
            "valid": bool(result.get("valid")) and not issues,
            "issues": issues,
            "root_id": root_info["root_id"],
            "root_label": root_info["label"],
            "output": "" if cube == root else cube.relative_to(root).as_posix(),
            "catalog": str(path),
            "schema_versions": versions,
            "tile_count": int(frame["tile_id"].nunique()) if "tile_id" in frame else 0,
            "version": f"{path.stat().st_size:x}-{path.stat().st_mtime_ns:x}",
        }

    def _valid_catalog(self, relative: str, root_id: str = "workspace") -> tuple[Path, pd.DataFrame, dict]:
        info = self.catalog_inspect(relative, root_id=root_id)
        if not info["valid"]:
            raise ValueError("；".join(info.get("issues") or ["Catalog 校验失败"]))
        path = Path(info["catalog"])
        return path, pd.read_parquet(path), info

    @staticmethod
    def _filter_catalog(frame: pd.DataFrame, **filters) -> pd.DataFrame:
        for key, column in (
            ("tile", "tile_id"),
            ("product", "product_type"),
            ("direction", "flight_direction"),
            ("month", "month"),
            ("status", "status"),
        ):
            value = str(filters.get(key) or "").strip()
            if value and column in frame:
                frame = frame[frame[column].fillna("").astype(str) == value]
        return frame

    def catalog_query(self, relative: str, root_id: str = "workspace", **filters) -> dict:
        path, frame, info = self._valid_catalog(relative, root_id=root_id)
        frame = self._filter_catalog(frame, **filters)
        total = int(len(frame))
        if "datetime" in frame:
            frame = frame.sort_values("datetime", na_position="last")
        records = []
        fields = (
            "item_id", "tile_id", "product_type", "flight_direction",
            "datetime", "month", "grid_id", "zarr_path", "status",
        )
        for _, row in frame.head(500).iterrows():
            records.append({field: _json_safe(row.get(field)) for field in fields})
        return {
            "catalog": str(path),
            "catalog_version": info["version"],
            "total": total,
            "returned": len(records),
            "records": records,
        }

    def catalog_map(self, relative: str, root_id: str = "workspace", **filters) -> dict:
        """Aggregate a valid Catalog by MGRS tile and attach dictionary geometry."""
        path, frame, info = self._valid_catalog(relative, root_id=root_id)
        frame = self._filter_catalog(frame, **filters)
        total = int(len(frame))
        tile_ids = sorted(
            set(frame.get("tile_id", pd.Series(dtype=str)).dropna().astype(str))
        )
        tile_count = len(tile_ids)
        if tile_count > self.mgrs_max_features:
            return {
                "catalog": str(path),
                "catalog_version": info["version"],
                "total_records": total,
                "tile_count": tile_count,
                "mapped_tile_count": 0,
                "truncated": True,
                "max_features": self.mgrs_max_features,
                "missing_tiles": [],
                "date_range": [None, None],
                "features": {"type": "FeatureCollection", "features": []},
            }

        dictionary = self._load_mgrs().set_index("mgrs_tile_id", drop=False)
        features = []
        missing_tiles = []
        all_dates = pd.to_datetime(frame.get("datetime"), errors="coerce").dropna()
        for tile_id in tile_ids:
            if tile_id not in dictionary.index:
                missing_tiles.append(tile_id)
                continue
            group = frame[frame["tile_id"].astype(str) == tile_id]
            months = sorted(set(group.get("month", pd.Series(dtype=str)).dropna().astype(str)))
            products = sorted(
                set(group.get("product_type", pd.Series(dtype=str)).dropna().astype(str))
            )
            directions = sorted(
                set(group.get("flight_direction", pd.Series(dtype=str)).dropna().astype(str))
            )
            statuses = group.get("status", pd.Series(dtype=str)).value_counts().to_dict()
            grids = group.get("grid_id", pd.Series(dtype=str)).dropna().astype(str).nunique()
            row = dictionary.loc[tile_id]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            features.append({
                "type": "Feature",
                "properties": {
                    "tile_id": tile_id,
                    "record_count": int(len(group)),
                    "product_types": products,
                    "directions": directions,
                    "month_min": months[0] if months else None,
                    "month_max": months[-1] if months else None,
                    "status_counts": _json_safe(statuses),
                    "grid_count": int(grids),
                    "utm_epsg": int(row.utm_epsg),
                },
                "geometry": self._geometry(row.geometry),
            })
        return {
            "catalog": str(path),
            "catalog_version": info["version"],
            "total_records": total,
            "tile_count": tile_count,
            "mapped_tile_count": len(features),
            "truncated": False,
            "max_features": self.mgrs_max_features,
            "missing_tiles": missing_tiles,
            "date_range": (
                [all_dates.min().isoformat(), all_dates.max().isoformat()]
                if len(all_dates) else [None, None]
            ),
            "features": {"type": "FeatureCollection", "features": features},
        }

    def catalog_report(self, relative: str, root_id: str = "workspace") -> dict:
        _, frame, info = self._valid_catalog(relative, root_id=root_id)
        dates = pd.to_datetime(frame.get("datetime"), errors="coerce").dropna()
        months = sorted(set(dates.dt.to_period("M").astype(str)))
        rows = []
        group_cols = [c for c in ("tile_id", "flight_direction") if c in frame]
        for keys, group in frame.groupby(group_cols, dropna=False) if group_cols else [((), frame)]:
            group_dates = pd.to_datetime(group.get("datetime"), errors="coerce").dropna()
            present = set(group_dates.dt.to_period("M").astype(str))
            missing: list[str] = []
            if present:
                expected = pd.period_range(min(present), max(present), freq="M").astype(str)
                missing = sorted(set(expected).difference(present))
            if not isinstance(keys, tuple):
                keys = (keys,)
            rows.append({
                **dict(zip(group_cols, keys)),
                "records": int(len(group)),
                "missing_months": len(missing),
                "missing_list": missing,
            })
        counts = {
            "products": frame.get("product_type", pd.Series(dtype=str)).value_counts().to_dict(),
            "statuses": frame.get("status", pd.Series(dtype=str)).value_counts().to_dict(),
            "directions": frame.get("flight_direction", pd.Series(dtype=str)).value_counts().to_dict(),
        }
        with_gaps = sum(1 for row in rows if row["missing_months"])
        return {
            "catalog": info,
            "overall": {
                "total_records": int(len(frame)),
                "tile_count": int(frame["tile_id"].nunique()) if "tile_id" in frame else 0,
                "total_months": len(months),
                "date_range": [dates.min().isoformat(), dates.max().isoformat()] if len(dates) else [None, None],
            },
            "counts": _json_safe(counts),
            "gaps": {
                "tiles_with_gaps": with_gaps,
                "tiles_complete": len(rows) - with_gaps,
            },
            "tile_rows": rows[:500],
            "tile_rows_total": len(rows),
            "truncated": len(rows) > 500,
        }

    # ------------------------------------------------------------------
    # Plan -> v3 process_scenes configs -> existing JobManager
    # ------------------------------------------------------------------
    def _resolve_tiles(self, body: dict) -> tuple[list[str], list[dict]]:
        frame = self._load_mgrs()
        mode = str(body.get("selection_mode") or "tiles")
        if mode == "tiles":
            raw = body.get("tiles") or []
            if isinstance(raw, str):
                raw = re.split(r"[\s,;]+", raw)
            tiles = sorted({str(v).strip().upper() for v in raw if str(v).strip()})
            invalid = [v for v in tiles if not TILE_RE.fullmatch(v)]
            if invalid:
                raise ValueError(f"MGRS 编号格式无效：{', '.join(invalid[:8])}")
            known = set(frame["mgrs_tile_id"].astype(str))
            missing = [v for v in tiles if v not in known]
            if missing:
                raise ValueError(f"MGRS 空间字典中不存在：{', '.join(missing[:8])}")
        elif mode == "aoi":
            resolved = self.resolve_aoi(body.get("aoi") or {})
            tiles = resolved["candidate_tiles"]
        else:
            raise ValueError("selection_mode 必须为 tiles 或 aoi")
        if not tiles:
            raise ValueError("没有选中任何 MGRS 瓦片")
        if len(tiles) > self.max_tiles:
            raise ValueError(f"一次最多处理 {self.max_tiles} 个瓦片")
        subset = frame[frame["mgrs_tile_id"].astype(str).isin(tiles)]
        features = [
            {
                "type": "Feature",
                "properties": {"tile_id": str(r.mgrs_tile_id), "utm_epsg": int(r.utm_epsg)},
                "geometry": self._geometry(r.geometry),
            }
            for r in subset.itertuples(index=False)
        ]
        return tiles, features

    def create_plan(self, body: dict) -> dict:
        workflow = str(body.get("workflow") or "scenes").lower()
        if workflow not in {"scenes", "monthly"}:
            raise ValueError("产品必须为逐景时序或全瓦片月合成")
        direction = str(body.get("direction") or "ASCENDING").upper()
        if direction not in {"ASCENDING", "DESCENDING", "BOTH"}:
            raise ValueError("轨道方向无效")
        directions = ["ASCENDING", "DESCENDING"] if direction == "BOTH" else [direction]
        tiles, features = self._resolve_tiles(body)
        years = sorted({int(v) for v in (body.get("years") or [])})
        months = sorted({int(v) for v in (body.get("months") or range(1, 13))})
        current_year = datetime.now().year
        if not years or any(v < 2014 or v > current_year for v in years):
            raise ValueError(f"年份范围必须为 2014—{current_year}")
        if any(v < 1 or v > 12 for v in months):
            raise ValueError("月份必须为 1—12")
        resolution = validate_target_resolution(body.get("target_resolution") or 30)
        resampling_method = resolve_resampling_method(
            resolution, body.get("resampling_method", "auto")
        )
        output_subdir = str(body.get("output_subdir") or "s1_cube").strip()
        output_dir = self.resolve_relative(output_subdir, allow_root=False)
        include_static = bool(body.get("include_static"))
        static_layers = list(STATIC_LAYER_NAMES) if include_static else []
        band_count = 2 + bool(body.get("features_ratio")) + bool(body.get("features_rvi"))
        steps = len(years) * len(months) * (4 if workflow == "scenes" else 1)
        pixels = (109800 / resolution) ** 2
        imagery = pixels * band_count * 4 * steps * len(tiles) * len(directions) / 1024**3
        static = pixels * len(static_layers) * 4 * len(tiles) * len(directions) / 1024**3
        raw_gib = imagery + static
        if raw_gib > 500:
            raise ValueError(f"规划估算 {raw_gib:.2f} GiB，超过服务上限 500 GiB")
        token = secrets.token_urlsafe(32)
        plan = {
            "workflow": workflow,
            "directions": directions,
            "tiles": tiles,
            "tile_features": features,
            "years": years,
            "months": months,
            "output_subdir": output_subdir,
            "output_dir": str(output_dir),
            "target_resolution": resolution,
            "resampling_method": resampling_method,
            "zarr_only": bool(body.get("zarr_only", True)),
            "include_static": include_static,
            "static_layers": static_layers,
            "features_ratio": bool(body.get("features_ratio")),
            "features_rvi": bool(body.get("features_rvi")),
            "spatial_despeckle": bool(body.get("spatial_despeckle")),
            "smonthly": bool(body.get("smonthly")),
            "max_workers": max(1, min(8, int(body.get("max_workers") or 2))),
            "raw_gib": round(raw_gib, 3),
            "imagery_raw_gib": round(imagery, 3),
            "static_raw_gib": round(static, 3),
            "estimate_note": "按未压缩 float32、109.8 km 名义瓦片估算；逐景按每月 4 景估计。",
            "confirmation_phrase": f"下载 {raw_gib:.2f} GiB" if raw_gib >= 10 else "",
            "confirmation_required": raw_gib >= 10,
            "confirmation_reason": (
                "规划估算达到 10 GiB，需要输入所示短语以防止误提交大任务。"
                if raw_gib >= 10 else ""
            ),
            "expires": time.time() + 3600,
            "consumed": False,
        }
        with self._lock:
            self._plans[token] = plan
        return {**plan, "plan_id": token, "expires_at": int(plan["expires"])}

    @staticmethod
    def _config(plan: dict, direction: str) -> dict:
        monthly_enabled = plan["workflow"] == "monthly" or plan["smonthly"]
        monthly_only = plan["workflow"] == "monthly"
        visual = not plan["zarr_only"]
        return {
            "workflow": "scenes",
            "roi": {
                "manual_mgrs_tiles": plan["tiles"],
                "flight_direction": direction,
                "polarization": "VV+VH",
                "min_tile_coverage_frac": 0.2,
            },
            "time": {"years": plan["years"], "months": plan["months"]},
            "output": {
                "base_dir": plan["output_dir"],
                "existing_store": "resume",
                "existing_month": "skip",
                "formats": {"cog": visual, "preview": visual},
            },
            "processing": {
                "target_resolution": plan["target_resolution"],
                "resampling_method": plan["resampling_method"],
                "tile_clip": True,
                "spatial_despeckle": plan["spatial_despeckle"],
                "features_ratio": plan["features_ratio"],
                "features_rvi": plan["features_rvi"],
                "monthly": {
                    "enabled": monthly_enabled,
                    "only": monthly_only,
                    "composite_method": "nanmedian",
                    "generate_cog": visual,
                    "generate_preview": visual,
                    "blockwise_threads": 2,
                },
            },
            "static_layers": {
                "run_after_scenes": plan["include_static"],
                "grid_reference": "required",
                "reference_product_type": "auto",
                "on_failure": "fail",
            },
            "memory": {
                "max_memory_gb": "auto",
                "batch_strategy": "auto",
                "max_download_workers": 4,
                "download_prefetch": False,
            },
            "parallel": {
                "enabled": plan["max_workers"] > 1,
                "max_workers": plan["max_workers"],
            },
        }

    def create_task(self, token: str, confirmation: str = "") -> dict:
        with self._lock:
            plan = self._plans.get(str(token))
            if not plan or plan["expires"] < time.time():
                raise ValueError("规划不存在或已过期，请重新预检")
            if plan["consumed"]:
                raise ValueError("规划已使用，不能重复创建任务")
            if plan["confirmation_phrase"] and confirmation != plan["confirmation_phrase"]:
                raise ValueError("确认短语不匹配")
            plan["consumed"] = True
        Path(plan["output_dir"]).mkdir(parents=True, exist_ok=True)
        configs = [self._config(plan, direction) for direction in plan["directions"]]
        metadata = {
            key: _json_safe(plan[key])
            for key in (
                "workflow", "directions", "tiles", "years", "months",
                "output_subdir", "output_dir", "include_static", "static_layers",
            )
        }
        job = self.jobs.submit_batch(
            configs,
            output_dir=plan["output_dir"],
            title=f"{plan['workflow']} · {len(plan['tiles'])} 瓦片",
            metadata=metadata,
        )
        return self.task(job.to_dict())

    def task(self, data: dict) -> dict:
        status = {"success": "done"}.get(data.get("status"), data.get("status"))
        pct = data.get("progress", {}).get("pct")
        if pct is None:
            total = max(1, int(data.get("command_total") or 1))
            current = max(0, int(data.get("current_command") or 0))
            pct = 100 if status == "done" else max(0, (current - 1) * 100 / total)
        result = {
            **data,
            "run_id": data["id"],
            "status": status,
            "progress": round(float(pct) / 100, 4),
            "created_at": _iso(data.get("created_at")),
            "started_at": _iso(data.get("started_at")),
            "finished_at": _iso(data.get("ended_at")),
            "exit_code": data.get("returncode"),
            "command_index": data.get("current_command", 0),
        }
        if status == "done" and data.get("output_subdir"):
            try:
                info = self.catalog_inspect(data["output_subdir"])
                result["validation"] = {
                    "records": info.get("record_count", 0),
                    "tiles": info.get("tile_count", 0),
                    "schema_version": info.get("schema_versions", []),
                }
            except Exception:
                pass
        return result

    def tasks(self) -> list[dict]:
        return [self.task(data) for data in self.jobs.list()]

    def task_events(self, job_id: str) -> list[dict]:
        job = self.jobs.get(job_id)
        task = self.task(job.to_dict())
        events = [{
            "timestamp": task["created_at"],
            "event": "task_queued",
            "level": "info",
            "stage": "queued",
            "message": "任务已进入受控队列",
        }]
        persisted = [{
            **event,
            "timestamp": _iso(event.get("timestamp"))
            if isinstance(event.get("timestamp"), (int, float))
            else event.get("timestamp"),
        } for event in job.events]
        events.extend(persisted)
        if task.get("started_at") and not any(
            event.get("event") == "task_attempt_started" for event in persisted
        ):
            events.append({
                "timestamp": task["started_at"],
                "event": "task_started",
                "level": "info",
                "stage": task.get("stage"),
                "message": f"正在执行第 {task.get('current_command', 0)}/{task.get('command_total', 0)} 条命令",
            })
        if task.get("finished_at") and not any(
            str(event.get("event", "")).startswith("task_attempt_")
            and event.get("event") != "task_attempt_started"
            for event in persisted
        ):
            events.append({
                "timestamp": task["finished_at"],
                "event": f"task_{task['status']}",
                "level": "error" if task["status"] == "failed" else "info",
                "stage": task.get("stage"),
                "message": task.get("error") or "任务结束",
            })
        return events
