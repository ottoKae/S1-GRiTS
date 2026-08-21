"""Chinese-console service layer for the v3 FastAPI application.

The browser is a client of the public v3 CLI contract.  This module owns only
input validation, MGRS display queries, catalog browsing, and translation of a
confirmed plan into whitelisted ``process_scenes`` jobs.
"""
from __future__ import annotations

import math
import re
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from s1grits.canonical_catalog_schema import (
    CANONICAL_CATALOG_COLUMNS,
    SCHEMA_VERSION,
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

    def __init__(self, root: Path | str, jobs, *, max_tiles: int = 200):
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
        if zoom < self.mgrs_min_zoom:
            payload = {
                **base,
                "visible": False,
                "reason": "zoom",
                "count": 0,
                "returned": 0,
                "truncated": False,
                "features": {"type": "FeatureCollection", "features": []},
            }
        else:
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
    # Safe directories and catalog access
    # ------------------------------------------------------------------
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

    def catalog_inspect(self, relative: str) -> dict:
        cube = self.resolve_relative(relative)
        path = cube / "catalog.parquet"
        if not path.is_file():
            return {
                "valid": False,
                "output": relative,
                "catalog": str(path),
                "issues": ["所选目录根部不存在 catalog.parquet"],
                "warnings": [],
            }
        from s1grits.analysis.catalog import validate_catalog

        result = validate_catalog(path)
        frame = pd.read_parquet(path)
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
            "output": "" if cube == self.root else cube.relative_to(self.root).as_posix(),
            "catalog": str(path),
            "schema_versions": versions,
            "tile_count": int(frame["tile_id"].nunique()) if "tile_id" in frame else 0,
            "version": f"{path.stat().st_size:x}-{path.stat().st_mtime_ns:x}",
        }

    def _valid_catalog(self, relative: str) -> tuple[Path, pd.DataFrame, dict]:
        info = self.catalog_inspect(relative)
        if not info["valid"]:
            raise ValueError("；".join(info.get("issues") or ["Catalog 校验失败"]))
        path = Path(info["catalog"])
        return path, pd.read_parquet(path), info

    def catalog_query(self, relative: str, **filters) -> dict:
        path, frame, info = self._valid_catalog(relative)
        for key, column in (
            ("tile", "tile_id"),
            ("product", "product_type"),
            ("direction", "flight_direction"),
            ("month", "month"),
        ):
            value = str(filters.get(key) or "").strip()
            if value and column in frame:
                frame = frame[frame[column].fillna("").astype(str) == value]
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

    def catalog_report(self, relative: str) -> dict:
        _, frame, info = self._valid_catalog(relative)
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
            from shapely.geometry import shape

            geom = shape(body.get("aoi") or {})
            if geom.is_empty or not geom.is_valid:
                raise ValueError("AOI 几何为空或无效")
            minx, miny, maxx, maxy = geom.bounds
            if not (-180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90):
                raise ValueError("AOI 必须使用 EPSG:4326 经纬度")
            subset = frame.iloc[list(frame.sindex.query(geom, predicate="intersects"))]
            subset = subset[[g.intersection(geom).area > 0 for g in subset.geometry]]
            tiles = sorted(subset["mgrs_tile_id"].astype(str).tolist())
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
        resolution = float(body.get("target_resolution") or 30)
        if not 10 <= resolution <= 100:
            raise ValueError("目标分辨率必须在 10—100 米之间")
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
        task = self.task(self.jobs.get(job_id).to_dict())
        events = [{
            "timestamp": task["created_at"],
            "event": "task_queued",
            "level": "info",
            "stage": "queued",
            "message": "任务已进入受控队列",
        }]
        if task.get("started_at"):
            events.append({
                "timestamp": task["started_at"],
                "event": "task_started",
                "level": "info",
                "stage": task.get("stage"),
                "message": f"正在执行第 {task.get('current_command', 0)}/{task.get('command_total', 0)} 条命令",
            })
        if task.get("finished_at"):
            events.append({
                "timestamp": task["finished_at"],
                "event": f"task_{task['status']}",
                "level": "error" if task["status"] == "failed" else "info",
                "stage": task.get("stage"),
                "message": task.get("error") or "任务结束",
            })
        return events
