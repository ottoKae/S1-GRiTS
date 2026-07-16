"""Acquisition QC: interior-hole detection and burst accounting.

Extracted move-only from workflow_scenes.py (which re-exports every name
here). Edge truncation is normal; a genuinely missing interior burst (or an
enclosed NoData hole) is the defect these helpers detect.
"""
from __future__ import annotations

import numpy as np

from s1grits.logger_config import get_logger

logger = get_logger(__name__)

def _burst_coverage_status(loaded: int, expected: int, date_label: str, dt_str: str):
    """Assess per-acquisition burst coverage.

    ``expected`` is the burst count from ASF metadata; ``loaded`` is how many
    bursts actually came through and will be mosaicked. A shortfall leaves a
    NoData gap in the COG/Zarr (commonly a 404 — not yet published on ASF for
    very recent acquisitions, or transient S3 — or a network drop).

    Returns ``(missing_count, message)`` when coverage is incomplete, else
    ``(0, None)``. Pure / side-effect-free so it can be unit-tested.
    """
    if loaded >= expected:
        return 0, None
    missing = expected - loaded
    msg = (
        f"[Coverage] Scene {date_label} ({dt_str}): only {loaded}/{expected} "
        f"expected burst(s) mosaicked — {missing} missing (likely 404/not-yet-"
        f"published on ASF or a network drop); this acquisition's COG/Zarr will "
        f"have a NoData gap."
    )
    return missing, msg

def _interior_hole_fraction(
    valid_mask: np.ndarray,
    tile_mask: np.ndarray | None = None,
    denom: int | None = None,
) -> float:
    """Fraction of the tile that is an *interior* NoData hole — NoData fully
    enclosed by valid data, as opposed to NoData at the swath/tile edge.

    Edge truncation (a data-take ending a burst or two short along-track) leaves
    NoData connected to the array/tile border and is NORMAL; an interior hole is
    a missing burst surrounded by data and is the real defect. binary_fill_holes
    fills only fully-enclosed NoData regions, so the filled-minus-valid set is
    exactly the interior holes.

    ``valid_mask``: True where data is finite. ``tile_mask``: True inside the
    MGRS tile (restricts the measure to the tile interior). ``denom`` overrides
    the denominator so callers may pass a *window* of the tile (containing all
    valid pixels) while keeping the fraction relative to the full tile; interior
    holes are window-invariant because a region enclosed by valid data is
    enclosed in any window that contains all the valid data. Pure / testable.
    """
    if valid_mask is None or not valid_mask.any():
        return 0.0
    try:
        from scipy.ndimage import binary_fill_holes
    except Exception:
        return 0.0
    filled = binary_fill_holes(valid_mask)
    interior = filled & (~valid_mask)
    if tile_mask is not None:
        interior = interior & tile_mask
        if denom is None:
            denom = int(tile_mask.sum())
    elif denom is None:
        denom = int(valid_mask.size)
    return float(int(interior.sum()) / denom) if denom else 0.0

def _burst_subswath_index(bid: str) -> tuple[str, int]:
    """Parse an OPERA burst id like 'T011-021605-IW1' (or 'T011_021605_IW1')
    into ('IW1', 21605) — the sub-swath and its along-track burst index."""
    s = str(bid).replace('_', '-').upper()
    parts = s.split('-')
    iw = next((p for p in parts if p.startswith('IW')), '')
    nums = [int(p) for p in parts if p.isdigit()]
    return iw, (nums[-1] if nums else -1)

def _missing_interior_bursts(footprint_ids, present_ids) -> list[str]:
    """Burst ids that are in the track footprint but missing from this
    acquisition AND fall BETWEEN present bursts along-track within their
    sub-swath — i.e. a real interior gap (a missing burst with neighbours on
    both along-track sides), as opposed to missing at the along-track ends
    (normal edge truncation, which is kept).

    This is metadata-only and catches gaps that the raster fill-holes test
    misses (a full-width along-track segment, or an edge sub-swath, both connect
    to the swath border and are not "enclosed"). Pure / testable.
    """
    from collections import defaultdict
    present = {str(b) for b in present_ids}
    missing = {str(b) for b in footprint_ids} - present
    if not missing:
        return []
    pres_idx: dict[str, list[int]] = defaultdict(list)
    for b in present:
        iw, idx = _burst_subswath_index(b)
        if idx >= 0:
            pres_idx[iw].append(idx)
    interior = []
    for b in missing:
        iw, idx = _burst_subswath_index(b)
        ps = pres_idx.get(iw)
        if ps and min(ps) < idx < max(ps):
            interior.append(b)
    return sorted(interior)

def _estimate_interior_hole_frac(
    interior_missing: list[str],
    present_ids: list[str],
    burst_geoms: dict,
    tile_geom,
    n_footprint_bursts: int,
) -> tuple[float | None, str | None]:
    """Metadata-only estimate of the tile-area fraction an interior-missing
    burst leaves uncovered, so the prefilter can honour
    ``interior_hole_max_frac`` instead of dropping on ANY interior gap.

    Exact where the missing burst's footprint is known from other acquisitions
    (OPERA burst footprints are fixed per burst id): the hole is the missing
    footprint ∩ tile minus the union of the acquisition's present bursts
    (along-track neighbours overlap and fill part of the gap). For a burst
    that is absent from the ENTIRE query (never seen -> no geometry, the
    common ASF_MISSING_STALE case), fall back to a per-burst share proxy:
    (track's tile coverage) / (footprint burst count) per unknown burst.

    All geometries are EPSG:4326; the area RATIO is what matters, and the
    distortion across a single 110 km tile is negligible for a threshold test.

    Returns (fraction, basis) with basis in {"geometry", "proxy",
    "geometry+proxy"}; (None, None) when nothing can be estimated (no burst
    geometry at all) — the caller then keeps its conservative behaviour.
    """
    try:
        from shapely.ops import unary_union
    except ImportError:
        return None, None
    if tile_geom is None or not burst_geoms:
        return None, None
    tile_area = float(tile_geom.area)
    if tile_area <= 0:
        return None, None

    known = [burst_geoms[b] for b in interior_missing if b in burst_geoms]
    unknown = [b for b in interior_missing if b not in burst_geoms]
    present_geoms = [burst_geoms[str(b)] for b in present_ids
                     if str(b) in burst_geoms]

    frac = 0.0
    basis: list[str] = []
    if known:
        hole = unary_union(known).intersection(tile_geom)
        if present_geoms and not hole.is_empty:
            hole = hole.difference(unary_union(present_geoms))
        frac += float(hole.area) / tile_area
        basis.append("geometry")
    if unknown:
        if not present_geoms:
            return None, None
        track_in_tile = unary_union(present_geoms).intersection(tile_geom)
        per_burst = float(track_in_tile.area) / max(int(n_footprint_bursts), 1)
        frac += len(unknown) * per_burst / tile_area
        basis.append("proxy")
    if not basis:
        return None, None
    return frac, "+".join(basis)
