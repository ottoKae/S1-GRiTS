"""
catalog_validator.py
====================
Cross-check consistency between catalog.parquet, STAC item.json, and Zarr attrs.

Usage:
    from s1grits.catalog_validator import validate_catalog_integrity
    result = validate_catalog_integrity(output_dir, strict=True)
    print(result.report())
"""

import json as _json
import os as _os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from s1grits.logger_config import get_logger
from s1grits.product_registry import ProductRegistry

logger = get_logger(__name__)


@dataclass
class ValidationResult:
    """Structured result of a catalog integrity check."""
    total_records: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return len(self.errors) == 0

    def report(self) -> str:
        lines = [f"Catalog integrity: {len(self.errors)} error(s), "
                 f"{len(self.warnings)} warning(s), {len(self.passed)} OK"]
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        for w in self.warnings:
            lines.append(f"  WARN:  {w}")
        return "\n".join(lines)


def validate_catalog_integrity(
    output_dir: str | Path,
    strict: bool = False,
    registry: "ProductRegistry | None" = None,
) -> ValidationResult:
    """
    Cross-check catalog.parquet, STAC items, and Zarr attrs for consistency.

    Checks:
      1. Every catalog record's zarr_path points to an existing Zarr store
      2. Every Zarr store has expected attrs (crs, transform, grid_id)
      3. Catalog grid_id matches Zarr attrs grid_id
      4. STAC item.json exists for records with item_path
      5. STAC item.collection matches catalog.collection_id
      6. catalog.bands includes registry.derived_from minimum bands

    Args:
        output_dir: Path to the DataCube root directory.
        strict: If True, warnings become errors.
        registry: Optional ProductRegistry for derived_from check.

    Returns:
        ValidationResult with pass/fail details.
    """
    result = ValidationResult()
    output_root = Path(output_dir)

    catalog_path = output_root / "catalog.parquet"
    if not catalog_path.exists():
        result.errors.append(f"catalog.parquet not found at {output_root}")
        return result

    try:
        df = pd.read_parquet(catalog_path)
    except Exception as e:
        result.errors.append(f"Failed to read catalog.parquet: {e}")
        return result

    result.total_records = len(df)

    # Filter to complete, schema-compatible records
    from s1grits.canonical_catalog_schema import filter_complete_records
    df = filter_complete_records(df, require_grid=True)
    if len(df) == 0:
        result.errors.append("No complete, schema-compatible records found")
        return result

    # ---- Check 1: zarr_path exists ----
    for _, row in df.iterrows():
        _zp = row.get("zarr_path")
        if not _zp:
            result.warnings.append(f"Record {row.get('item_id','?')}: zarr_path is null")
            continue
        _zarr_full = output_root / _zp
        if not _zarr_full.exists():
            msg = f"Zarr missing: {_zp}"
            result.errors.append(msg) if strict else result.warnings.append(msg)

    # ---- Check 2+3: Zarr attrs vs catalog ----
    import zarr
    _zarr_checked = set()
    for _, row in df.iterrows():
        _zp = row.get("zarr_path")
        if not _zp or _zp in _zarr_checked:
            continue
        _zarr_checked.add(_zp)
        _zarr_full = output_root / _zp
        if not _zarr_full.exists():
            continue

        try:
            g = zarr.open_group(str(_zarr_full), mode="r")
        except Exception as e:
            result.errors.append(f"Cannot open Zarr {_zp}: {e}")
            continue

        # Check attrs
        for attr in ("crs", "transform", "grid_id"):
            if attr not in g.attrs:
                result.warnings.append(f"Zarr {_zp}: missing attr '{attr}'")

        _cat_gid = row.get("grid_id")
        _zarr_gid = g.attrs.get("grid_id")
        if _cat_gid and _zarr_gid and _cat_gid != _zarr_gid:
            result.errors.append(
                f"grid_id mismatch for {_zp}: catalog={_cat_gid}, Zarr={_zarr_gid}"
            )

        # Check time dimension
        _cat_time = row.get("product_type") not in ("static",)
        _has_time = "time" in g
        if _cat_time and not _has_time:
            result.errors.append(f"Zarr {_zp}: expected time dimension (product={row.get('product_type')})")
        if not _cat_time and _has_time:
            result.errors.append(f"Zarr {_zp}: unexpected time dimension (static product)")

    # ---- Check 4: STAC item.json exists ----
    for _, row in df.iterrows():
        _ip = row.get("item_path")
        if not _ip:
            _ip = _infer_item_path(row)
        if not _ip:
            continue
        _item_full = output_root / _ip
        if not _item_full.exists():
            msg = f"STAC item missing: {_ip}"
            if strict:
                result.errors.append(msg)
            else:
                result.warnings.append(msg)

    # ---- Check 4b: STAC item.collection matches catalog.collection_id ----
    for _, row in df.iterrows():
        _ip = row.get("item_path")
        if not _ip:
            _ip = _infer_item_path(row)
        if not _ip:
            continue
        _item_full = output_root / _ip
        if not _item_full.exists():
            continue
        try:
            with open(_item_full) as _f:
                _item = _json.load(_f)
            _item_coll = _item.get("collection")
            _cat_coll = row.get("collection_id")
            if _item_coll and _cat_coll and _item_coll != _cat_coll:
                msg = (
                    f"Collection mismatch in {_ip}: "
                    f"item.collection={_item_coll}, catalog.collection_id={_cat_coll}"
                )
                result.errors.append(msg) if strict else result.warnings.append(msg)
            # ---- Check 4c: asset hrefs resolve to real files ----
            for _akey, _asset in (_item.get("assets") or {}).items():
                _href = _asset.get("href")
                if not _href:
                    continue
                if not (_item_full.parent / _href).exists():
                    msg = f"Asset '{_akey}' missing in {_ip}: href -> {_href}"
                    result.errors.append(msg) if strict else result.warnings.append(msg)
        except Exception as _e:
            result.errors.append(f"Cannot parse STAC item {_ip}: {_e}") if strict else None

    # ---- Check 5: catalog.bands includes registry.derived_from ----
    if registry is None:
        try:
            registry = ProductRegistry()
        except Exception:
            registry = None
    if registry is not None:
        for _, row in df.iterrows():
            _pt = row.get("product_type")
            if not _pt or not registry.is_valid(_pt):
                continue
            _spec = registry.get(_pt)
            if not _spec.derived_from:
                continue
            _record_bands_raw = row.get("bands")
            if _record_bands_raw is None:
                msg = f"Record {row.get('item_id','?')}: bands is null, expected at least {_spec.derived_from}"
                result.errors.append(msg) if strict else result.warnings.append(msg)
                continue
            if isinstance(_record_bands_raw, str):
                try:
                    _record_bands = _json.loads(_record_bands_raw)
                except _json.JSONDecodeError:
                    _record_bands = [_record_bands_raw]
            else:
                _record_bands = list(_record_bands_raw)
            _missing = [b for b in _spec.derived_from if b not in _record_bands]
            if _missing:
                msg = (
                    f"Record {row.get('item_id','?')} (product_type={_pt}): "
                    f"catalog.bands missing required derived_from bands {_missing}"
                )
                result.errors.append(msg) if strict else result.warnings.append(msg)

    # ---- Check 6: product_variant non-empty ----
    if "product_variant" in df.columns:
        _missing_pv = df["product_variant"].isna().sum()
        if _missing_pv > 0:
            # Only error in strict mode if catalog is partially migrated
            # (some records have the field, some don't). Fully-legacy = warn only.
            _has_any_pv = _missing_pv < len(df)
            msg = f"{_missing_pv} record(s) missing product_variant"
            if strict and _has_any_pv:
                result.errors.append(msg)
            else:
                result.warnings.append(msg)

    # ---- Check 7: processing_signature non-empty ----
    if "processing_signature" in df.columns:
        _missing_ps = df["processing_signature"].isna().sum()
        if _missing_ps > 0:
            _has_any_ps = _missing_ps < len(df)
            msg = f"{_missing_ps} record(s) missing processing_signature"
            if strict and _has_any_ps:
                result.errors.append(msg)
            else:
                result.warnings.append(msg)

    # ---- Check 8: STAC s1grits:processing_signature matches catalog ----
    for _, row in df.iterrows():
        _ip = row.get("item_path")
        if not _ip:
            _ip = _infer_item_path(row)
        if not _ip:
            continue
        _item_full = output_root / _ip
        if not _item_full.exists():
            continue
        _cat_sig = row.get("processing_signature")
        if not _cat_sig:
            continue
        try:
            with open(_item_full) as _f:
                _item = _json.load(_f)
            _stac_sig = _item.get("properties", {}).get("s1grits:processing_signature")
            if _stac_sig and _stac_sig != _cat_sig:
                msg = (
                    f"processing_signature mismatch in {_ip}: "
                    f"STAC={_stac_sig}, catalog={_cat_sig}"
                )
                if strict:
                    result.errors.append(msg)
                else:
                    result.warnings.append(msg)
            # Check s1grits:bands vs catalog.bands
            _stac_bands = _item.get("properties", {}).get("s1grits:bands")
            _cat_bands_raw = row.get("bands")
            if _stac_bands and _cat_bands_raw:
                _cat_bands = (
                    _json.loads(_cat_bands_raw)
                    if isinstance(_cat_bands_raw, str) else list(_cat_bands_raw)
                )
                if _stac_bands != _cat_bands:
                    msg = (
                        f"bands mismatch in {_ip}: "
                        f"STAC={_stac_bands}, catalog={_cat_bands}"
                    )
                    if strict:
                        result.errors.append(msg)
                    else:
                        result.warnings.append(msg)
        except Exception:
            pass

    # ---- Check 9: Same Zarr store must have same processing_signature ----
    if "processing_signature" in df.columns and "zarr_path" in df.columns:
        _zarr_sig_groups = (
            df[df["processing_signature"].notna() & df["zarr_path"].notna()]
            .groupby("zarr_path")["processing_signature"]
            .nunique()
        )
        _mixed = _zarr_sig_groups[_zarr_sig_groups > 1]
        for zarr_path, n_sigs in _mixed.items():
            msg = (
                f"Zarr store '{zarr_path}' has {n_sigs} distinct "
                f"processing_signatures — must be uniform"
            )
            result.errors.append(msg)

    # ---- Final summary ----
    _n_ok = result.total_records - len(result.errors)
    result.passed.append(
        f"{_n_ok}/{result.total_records} records validated "
        f"({len(_zarr_checked)} unique Zarr stores checked)"
    )

    return result


def _infer_item_path(row: pd.Series) -> Optional[str]:
    """Infer STAC item path from catalog record columns."""
    tile_id = row.get("tile_id")
    product_label = row.get("product_label") or ""
    item_id = row.get("item_id")
    if not tile_id or not product_label or not item_id:
        return None
    return f"{tile_id}/items/{product_label}/{item_id}.json"
