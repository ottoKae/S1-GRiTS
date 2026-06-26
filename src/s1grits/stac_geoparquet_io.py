"""
stac-geoparquet I/O for S1-GRiTS.

Serializes STAC Item dicts to a single GeoParquet file per collection (one row
per item, with a WKB ``geometry`` column + GeoParquet CRS metadata) and reads
them back into STAC Item dicts. This is the default STAC distribution format:
it replaces tens of thousands of per-item JSON files with one queryable parquet
per collection, and is consumable directly by DuckDB / pyarrow / geopandas and
servable via stac-fastapi-geoparquet or pgstac.

The catalog (``catalog.parquet``) remains the canonical internal index; this
module only handles the STAC *projection* of it.

``stac-geoparquet`` (and its deps shapely/pyarrow/geopandas/orjson) is imported
lazily so the rest of the package works without it installed; a clear,
actionable error is raised only if geoparquet output is actually requested.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from s1grits.logger_config import get_logger

logger = get_logger(__name__)

_INSTALL_HINT = (
    "stac-geoparquet output requires the 'stac-geoparquet' package. "
    "Install it with:  pip install stac-geoparquet"
)


def stac_geoparquet_available() -> bool:
    """True if the stac-geoparquet stack can be imported."""
    try:
        import stac_geoparquet.arrow  # noqa: F401
        return True
    except Exception:
        return False


def write_items_geoparquet(item_dicts: list[dict[str, Any]], out_path: str | Path) -> str | None:
    """
    Write STAC Item dicts to a single GeoParquet file.

    Args:
        item_dicts: STAC Item dicts (e.g. from ``build_stac_item_dict``).
        out_path: Destination ``.parquet`` path.

    Returns:
        The output path, or None if there were no items to write.
    """
    if not item_dicts:
        logger.warning("No items — geoparquet not written: %s", out_path)
        return None
    try:
        import stac_geoparquet.arrow as sga
    except Exception as e:  # pragma: no cover - exercised via clear message
        raise RuntimeError(_INSTALL_HINT) from e

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish write: build to a temp file, then replace.
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    reader = sga.parse_stac_items_to_arrow(item_dicts)
    sga.to_parquet(reader, str(tmp))
    tmp.replace(out_path)
    logger.info("STAC items written: %s (%d items)", out_path, len(item_dicts))
    return str(out_path)


def read_items_geoparquet(path: str | Path) -> list[dict[str, Any]]:
    """Read a stac-geoparquet file back into STAC Item dicts (round-trip)."""
    try:
        import pyarrow.parquet as pq
        import stac_geoparquet.arrow as sga
    except Exception as e:  # pragma: no cover
        raise RuntimeError(_INSTALL_HINT) from e
    table = pq.read_table(str(path))
    return list(sga.stac_table_to_items(table))


def iter_items_geoparquet(path: str | Path) -> Iterable[dict[str, Any]]:
    """Lazily iterate STAC Item dicts from a stac-geoparquet file."""
    try:
        import pyarrow.parquet as pq
        import stac_geoparquet.arrow as sga
    except Exception as e:  # pragma: no cover
        raise RuntimeError(_INSTALL_HINT) from e
    table = pq.read_table(str(path))
    yield from sga.stac_table_to_items(table)
