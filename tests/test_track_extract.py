"""track_extract: catalog dedup, track-token parsing, single-track arbitration.

The catalog holds one row per (store, month); TrackCatalog.from_parquet must
collapse those to one candidate per store, or every per-point pixel read is
amplified by the number of months (the biggest read-amplification hazard of
the naive row walk). TrackSeries carries track_id — the integer relative
orbit parsed from the store name's _TK{nn} token — because downstream tables
want ``18``, not an absolute path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from s1grits.track_extract import (  # noqa: E402
    S1TrackExtractor,
    TrackCatalog,
    arbitrate,
    track_id_from_store,
)

TILE = "17MPU"
CRS = "EPSG:32717"
RES = 30.0
W, H = 8, 6
MINX, MAXY = 499980.0, 8499990.0
TRANSFORM = [RES, 0.0, MINX, 0.0, -RES, MAXY]


def _store_rel(track: int) -> str:
    return (f"smonthly_ASCENDING/zarr/"
            f"s1grits_smonthly_{TILE}_ASCENDING_TK{track}.zarr")


def _catalog(tmp_path: Path, months_per_store: int = 3) -> Path:
    """One row per (store, month) — the real catalog layout — plus a row
    without a zarr store (e.g. a static product)."""
    rows = []
    for track in (18, 91):
        for m in range(1, months_per_store + 1):
            rows.append({
                "tile_id": TILE, "zarr_path": _store_rel(track),
                "crs": CRS, "transform": TRANSFORM, "width": W, "height": H,
                "month": f"2026-{m:02d}", "track": track,
            })
    rows.append({  # store-less row must be ignored, not crash the walk
        "tile_id": TILE, "zarr_path": None,
        "crs": CRS, "transform": TRANSFORM, "width": W, "height": H,
        "month": "2026-01", "track": 18,
    })
    path = tmp_path / "catalog.parquet"
    pd.DataFrame(rows).to_parquet(path)
    return path


# ---------------------------------------------------------------------------
# Hazard 1: per-store dedup in from_parquet
# ---------------------------------------------------------------------------

def test_from_parquet_dedups_monthly_rows_to_one_candidate_per_store(tmp_path):
    cat = TrackCatalog.from_parquet(_catalog(tmp_path, months_per_store=5),
                                    str(tmp_path))
    stores = cat.stores(TILE)
    assert len(stores) == 2, "5 monthly rows per store must collapse to 1 entry"
    assert len(set(stores)) == 2
    assert all(s.endswith(".zarr") for s in stores)


def test_candidates_for_point_yields_each_store_once(tmp_path):
    cat = TrackCatalog.from_parquet(_catalog(tmp_path), str(tmp_path))
    # Pixel (2, 3) -> UTM centre of that cell, same CRS as the grid.
    x = MINX + (3 + 0.5) * RES
    y = MAXY - (2 + 0.5) * RES
    cands = cat.candidates_for_point(x, y, CRS, TILE)
    assert len(cands) == 2, "one candidate per track store, not per month"
    assert sorted(c[1:] for c in cands) == [(2, 3), (2, 3)]
    assert len({c[0] for c in cands}) == 2


# ---------------------------------------------------------------------------
# track_id: _TK{nn} token from the store name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store, expected", [
    ("/data/17NPA/s1grits_smonthly_17NPA_ASCENDING_TK18.zarr", 18),
    ("s1grits_smonthly_17MPU_DESCENDING_TK142.zarr/", 142),
    (r"C:\out\s1grits_smonthly_17MPU_ASCENDING_TK7.zarr", 7),
    ("/data/legacy_store_without_token.zarr", None),
])
def test_track_id_from_store(store, expected):
    assert track_id_from_store(store) == expected


# ---------------------------------------------------------------------------
# End-to-end: extraction + arbitration surface track_id, never mosaic tracks
# ---------------------------------------------------------------------------

def _write_store(path: Path, months: list[str], valid_at_px: bool):
    """Tiny 2-band store; the probe pixel (2, 3) is NaN when not valid."""
    xr = pytest.importorskip("xarray")
    time = np.array([np.datetime64(f"{m}-15") for m in months], dtype="datetime64[ns]")
    shape = (len(months), H, W)
    vv = np.full(shape, -12.0, np.float32)
    vh = np.full(shape, -18.0, np.float32)
    if not valid_at_px:
        vv[:, 2, 3] = np.nan
    xs = MINX + (np.arange(W) + 0.5) * RES
    ys = MAXY - (np.arange(H) + 0.5) * RES
    ds = xr.Dataset(
        {"VV_dB": (("time", "y", "x"), vv), "VH_dB": (("time", "y", "x"), vh)},
        coords={"time": time, "y": ys, "x": xs},
    )
    ds.to_zarr(path, mode="w", consolidated=False)


def test_extract_picks_best_covered_track_and_carries_track_id(tmp_path):
    pytest.importorskip("zarr")
    good = tmp_path / _store_rel(18)
    poor = tmp_path / _store_rel(91)
    good.parent.mkdir(parents=True, exist_ok=True)
    _write_store(good, ["2026-01", "2026-02", "2026-03"], valid_at_px=True)
    _write_store(poor, ["2026-01", "2026-02", "2026-03"], valid_at_px=False)

    ex = S1TrackExtractor(bands=("VV_dB", "VH_dB"))
    series = ex.extract([str(good), str(poor)], cy=2, cx=3,
                        point_id="p1", tile=TILE)
    assert series is not None
    assert series.track == str(good), "arbitration must pick ONE whole store"
    assert series.track_id == 18
    assert series.n_valid == 3 and series.n_candidates == 2

    recs = series.to_records(extra={"cls": "forest"})
    assert len(recs) == 3
    assert all(r["track_id"] == 18 and r["cls"] == "forest" for r in recs)
    assert recs[0]["VV_dB"] == pytest.approx(-12.0)


def test_arbitrate_never_merges_and_returns_none_without_coverage():
    dates = np.array([np.datetime64("2026-01-15")])
    a = ("storeA_TK18.zarr", dates, {"VV_dB": np.array([np.nan], np.float32)})
    b = ("storeB_TK91.zarr", dates, {"VV_dB": np.array([np.nan], np.float32)})
    best, n = arbitrate([a, b], ["VV_dB"])
    assert best is None and n == 0
