"""
catalog_sync.py
===============
Unified post-write hooks for catalog and STAC consistency.

Call ``update_root_catalog()`` after writing the global catalog.parquet
in every workflow's merge step to ensure the root ``catalog.json`` is
always up-to-date regardless of which workflow last ran.
"""

import json as _json
import os as _os
from pathlib import Path
from typing import Any

import pandas as pd

from s1grits.logger_config import get_logger

logger = get_logger(__name__)

# All known collection IDs and their human-readable titles.
_ALL_COLLECTIONS: dict[str, str] = {
    "s1grits-scenes":   "S1-GRiTS Scenes (per-acquisition)",
    "s1grits-smonthly": "S1-GRiTS Monthly (per-track composites)",
    "s1grits-static":   "S1-GRiTS Static Layers",
    "s1grits-monthly":  "S1-GRiTS Monthly (legacy, all-track composites)",
}


def update_root_catalog(
    output_root: str | Path,
    df_global: pd.DataFrame | None = None,
) -> None:
    """
    Write or update the root STAC Catalog at ``{output_root}/catalog.json``.

    Reads the existing catalog (if present) to preserve child links added
    by other workflows, then adds/updates links for every collection ID
    that is actually present in ``df_global`` (or all known collections
    if ``df_global`` is None).

    This function should be called by EVERY workflow after it finishes
    merging the global catalog.parquet.

    Args:
        output_root: DataCube root directory.
        df_global: The merged global catalog DataFrame (may be None).
    """
    output_root = Path(output_root)
    root_path = output_root / "catalog.json"

    existing_links: dict[str, dict] = {}
    if root_path.exists():
        try:
            _old = _json.loads(root_path.read_text(encoding="utf-8"))
            for _l in _old.get("links", []):
                if _l.get("rel") == "child":
                    existing_links[_l["href"]] = _l
        except Exception:
            pass

    present_ids: set[str] = set()
    if df_global is not None and "collection_id" in df_global.columns:
        present_ids = set(df_global["collection_id"].dropna().unique())

    children = []
    for cid, title in _ALL_COLLECTIONS.items():
        href = f"./collections/{cid}/collection.json"
        if not present_ids or cid in present_ids:
            children.append({
                "rel": "child", "href": href,
                "type": "application/json", "title": title,
            })
        elif href in existing_links:
            children.append(existing_links[href])

    catalog: dict[str, Any] = {
        "stac_version": "1.1.0",
        "type": "Catalog",
        "id": "s1-grits-root",
        "title": "S1-GRiTS DataCube",
        "description": (
            f"Sentinel-1 analysis-ready data"
            + (f": {len(df_global)} records" if df_global is not None else "")
        ),
        "links": [
            {"rel": "self", "href": "./catalog.json"},
            *sorted(children, key=lambda x: x["href"]),
        ],
    }

    root_path.parent.mkdir(parents=True, exist_ok=True)
    with open(root_path, "w", encoding="utf-8") as _f:
        _json.dump(catalog, _f, indent=2, ensure_ascii=False)

    logger.info("Root catalog updated: %s (%d children)", root_path, len(children))
