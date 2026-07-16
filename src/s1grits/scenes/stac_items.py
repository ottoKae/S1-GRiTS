"""STAC item writers for the scenes and smonthly products.

Extracted move-only from workflow_scenes.py (which re-exports every name
here; callers remain in workflow_scenes, so the established test seams —
patching workflow_scenes._write_scene_stac_item / _write_monthly_stac_item —
keep working unchanged).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
from rasterio.transform import Affine

from s1grits.logger_config import get_logger
from s1grits.stac_builder import (
    _utm_extent_to_wgs84,
    _epsg_int,
    _resolve_bands,
    STAC_VERSION,
    ITEM_STAC_EXTENSION_URIS,
)

logger = get_logger(__name__)

def _stac_extensions() -> list[str]:
    """STAC extension URIs shared by every item this module emits."""
    return list(ITEM_STAC_EXTENSION_URIS)

def _resolve_item_bands(
    cog_relpath: str | None, tile_dir: Path, polarization: str
) -> list[str]:
    """Band names for an item: read from the COG when present, else the default pair."""
    bands = ["VV_dB", "VH_dB"]
    if cog_relpath:
        try:
            cog_abs = (
                str(tile_dir / cog_relpath)
                if not os.path.isabs(cog_relpath)
                else cog_relpath
            )
            if os.path.exists(cog_abs):
                bands = _resolve_bands(
                    {"cog_path": cog_relpath}, str(tile_dir), polarization
                )
        except Exception:
            pass
    return bands

def _build_stac_item(
    *,
    mgrs_tile_id: str,
    direction_label: str,
    item_id: str,
    datetime_str: str,
    time_step: str,
    collection_id: str,
    cog_title: str,
    tile_dir: Path,
    transform: Affine,
    width: int,
    height: int,
    target_crs: str,
    processing_level: str,
    pass_id: int | None,
    track: int | None,
    jpl_burst_ids: list[str] | None,
    opera_ids: list[str] | None,
    variant_properties: dict,
    cog_relpath: str | None,
    zarr_relpath: str | None,
    preview_relpath: str | None,
    product_label: str,
    polarization: str,
) -> str:
    """Build and atomically write one STAC Item JSON; return the item ID.

    Shared by the scene and monthly writers. Everything that differs between the
    two variants is passed in: identity (``item_id``/``datetime_str``), the cube
    ``time_step`` (P1D vs P1M), the ``collection_id``, the COG asset ``cog_title``
    and the ``variant_properties`` merged into ``properties``.
    """
    bbox, geometry = _utm_extent_to_wgs84(
        list(transform)[:6], width, height, target_crs
    )
    x_min = transform[2]
    x_max = transform[2] + transform[0] * width
    y_max = transform[5]
    y_min = transform[5] + transform[4] * height
    pixel_size = abs(transform[0])
    epsg = _epsg_int(target_crs)
    tform_9 = list(transform)[:9]
    bands = _resolve_item_bands(cog_relpath, tile_dir, polarization)

    item = {
        "stac_version": STAC_VERSION,
        "stac_extensions": _stac_extensions(),
        "type": "Feature",
        "id": item_id,
        "geometry": geometry,
        "bbox": bbox,
        "properties": {
            "datetime": datetime_str,
            "platform": "sentinel-1",
            "instruments": ["c-sar"],
            "mgrs:tile_id": mgrs_tile_id,
            "s1:orbit_direction": direction_label.lower(),
            "s1:processing_level": processing_level,
            "sat:orbit_state": direction_label.lower(),
            "sat:absolute_orbit": pass_id,
            "sat:relative_orbit": track,
            "s1grits:jpl_burst_ids": jpl_burst_ids,
            "s1grits:opera_ids": opera_ids,
            **variant_properties,
            "proj:epsg": epsg,
            "proj:shape": [height, width],
            "proj:transform": tform_9,
            "proj:geometry": geometry,
            "proj:bbox": bbox,
            "cube:dimensions": {
                "x": {
                    "type": "spatial", "axis": "x",
                    "extent": [round(x_min, 3), round(x_max, 3)],
                    "step": pixel_size, "reference_system": epsg,
                },
                "y": {
                    "type": "spatial", "axis": "y",
                    "extent": [round(y_min, 3), round(y_max, 3)],
                    "step": pixel_size, "reference_system": epsg,
                },
                "time": {
                    "type": "temporal",
                    "extent": [datetime_str, datetime_str],
                    "step": time_step,
                },
                "spectral": {
                    "type": "bands",
                    "values": bands,
                },
            },
        },
        "collection": collection_id,
        "assets": {},
        "links": [
            {"rel": "self", "href": f"./{item_id}.json"},
            {"rel": "collection", "href": f"../../../../collections/{collection_id}/collection.json"},
            {"rel": "root", "href": "../../../../catalog.json"},
        ],
    }

    # Asset hrefs are computed relative to the item JSON location.
    items_dir = tile_dir / "items" / product_label
    _rel = lambda p: os.path.relpath(str(tile_dir / p), str(items_dir)).replace("\\", "/") if p else None

    if cog_relpath:
        item["assets"]["cog"] = {
            "href": _rel(cog_relpath),
            "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            "roles": ["data"],
            "title": cog_title,
            "eo:bands": [{"name": _b} for _b in bands],
        }
    if zarr_relpath:
        item["assets"]["zarr"] = {
            "href": _rel(zarr_relpath),
            "type": "application/vnd.zarr; version=3",
            "roles": ["data"],
            "title": "Full Time Series Zarr Store",
        }
    if preview_relpath:
        item["assets"]["preview"] = {
            "href": _rel(preview_relpath),
            "type": "image/png",
            "roles": ["overview"],
            "title": "RGB Preview (VH / VV / Ratio)",
        }

    items_dir.mkdir(parents=True, exist_ok=True)
    item_path = items_dir / f"{item_id}.json"
    from s1grits.atomic_write import atomic_write_json
    atomic_write_json(item, item_path)
    return item_id

def _write_scene_stac_item(
    mgrs_tile_id: str,
    direction_label: str,
    acq_ts: pd.Timestamp,
    tile_dir: Path,
    transform: Affine,
    width: int,
    height: int,
    target_crs: str,
    cog_relpath: str | None,
    zarr_relpath: str,
    preview_relpath: str | None = None,
    processing_level: str = "hARDCp",
    polarization: str = "VV+VH",
    product_label: str = "scenes",
    product_variant: str | None = None,
    processing_signature: str | None = None,
    processing_variant_json: str | None = None,
    actual_bands: list[str] | None = None,
    jpl_burst_ids: list[str] | None = None,
    opera_ids: list[str] | None = None,
    pass_id: int | None = None,
    track: int | None = None,
) -> str:
    """
    Write a STAC Item JSON for a single acquisition (per-pass scene).

    Returns the item ID. No-op (returns None) when STAC output is disabled
    (catalog-only build).
    """
    from s1grits.stac_builder import stac_output_enabled
    if not stac_output_enabled():
        return None
    dt_str = acq_ts.strftime('%Y%m%dT%H%M%S')
    item_id = f"{mgrs_tile_id}_{direction_label}_{dt_str}"
    datetime_str = acq_ts.strftime('%Y-%m-%dT%H:%M:%SZ')

    variant_properties = {
        "s1grits:product_variant": product_variant,
        "s1grits:processing_signature": processing_signature,
        "s1grits:processing_variant": (
            json.loads(processing_variant_json)
            if processing_variant_json and isinstance(processing_variant_json, str)
            else None
        ),
        "s1grits:bands": actual_bands,
    }

    item_id = _build_stac_item(
        mgrs_tile_id=mgrs_tile_id,
        direction_label=direction_label,
        item_id=item_id,
        datetime_str=datetime_str,
        time_step="P1D",
        collection_id="s1grits-scenes",
        cog_title="Per-Acquisition Scene COG",
        tile_dir=tile_dir,
        transform=transform,
        width=width,
        height=height,
        target_crs=target_crs,
        processing_level=processing_level,
        pass_id=pass_id,
        track=track,
        jpl_burst_ids=jpl_burst_ids,
        opera_ids=opera_ids,
        variant_properties=variant_properties,
        cog_relpath=cog_relpath,
        zarr_relpath=zarr_relpath,
        preview_relpath=preview_relpath,
        product_label=product_label,
        polarization=polarization,
    )

    logger.info(
        "Scene STAC Item: %s",
        tile_dir / "items" / product_label / f"{item_id}.json",
    )
    return item_id

def _write_monthly_stac_item(
    mgrs_tile_id: str,
    direction_label: str,
    month_str: str,
    rep_dt: pd.Timestamp,
    tile_dir: Path,
    transform: Affine,
    width: int,
    height: int,
    target_crs: str,
    cog_relpath: str | None,
    zarr_relpath: str,
    preview_relpath: str | None = None,
    processing_level: str = "hARDCp",
    polarization: str = "VV+VH",
    product_label: str = "smonthly",
    product_variant: str | None = None,
    processing_signature: str | None = None,
    processing_variant_json: str | None = None,
    actual_bands: list[str] | None = None,
    jpl_burst_ids: list[str] | None = None,
    opera_ids: list[str] | None = None,
    pass_id: int | None = None,
    track: int | None = None,
    primary_track: int | None = None,
    track_coverage: list[dict] | None = None,
    item_id_override: str | None = None,
) -> str:
    """
    Write a STAC Item JSON for one monthly composite.

    Returns the item ID. No-op (returns None) when STAC output is disabled
    (catalog-only build).
    """
    from s1grits.stac_builder import stac_output_enabled
    if not stac_output_enabled():
        return None
    item_id = item_id_override or f"{mgrs_tile_id}_{direction_label}_{month_str}"
    datetime_str = f"{month_str}-01T00:00:00Z"

    variant_properties = {
        "s1:monthly_composite": "median",
        "s1grits:primary_track": primary_track,
        "s1grits:track_coverage": track_coverage,
        **({"s1grits:product_variant": product_variant} if product_variant else {}),
        **({"s1grits:processing_signature": processing_signature} if processing_signature else {}),
        **({"s1grits:processing_variant": processing_variant_json} if processing_variant_json else {}),
        **({"s1grits:bands": actual_bands} if actual_bands else {}),
    }

    item_id = _build_stac_item(
        mgrs_tile_id=mgrs_tile_id,
        direction_label=direction_label,
        item_id=item_id,
        datetime_str=datetime_str,
        time_step="P1M",
        collection_id="s1grits-smonthly",
        cog_title="Monthly Composite COG",
        tile_dir=tile_dir,
        transform=transform,
        width=width,
        height=height,
        target_crs=target_crs,
        processing_level=processing_level,
        pass_id=pass_id,
        track=track,
        jpl_burst_ids=jpl_burst_ids,
        opera_ids=opera_ids,
        variant_properties=variant_properties,
        cog_relpath=cog_relpath,
        zarr_relpath=zarr_relpath,
        preview_relpath=preview_relpath,
        product_label=product_label,
        polarization=polarization,
    )

    logger.info(
        "Monthly STAC Item: %s",
        tile_dir / "items" / product_label / f"{item_id}.json",
    )
    return item_id
