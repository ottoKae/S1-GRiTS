"""COG/preview backfill from an existing Zarr month (resume path).

When `existing_month: skip` and a month's Zarr timestep already exists, the
monthly composite must NOT be recomputed — but derived COG/preview assets that
are missing (first run had generate_cog/preview=false, or an interrupted
export) should be regenerated from the existing Zarr. This locks in that
`_generate_cog_preview_from_zarr(skip_if_exists=True)` is an idempotent
backfill primitive: it fills only missing assets and never rewrites present
ones (no recompute).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from rasterio.transform import Affine

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

pytest.importorskip("rasterio")
zarr = pytest.importorskip("zarr")

from s1grits.workflow_scenes import (  # noqa: E402
    _append_zarr_timestep,
    _generate_cog_preview_from_zarr,
    _init_zarr_2band,
)

CRS = "EPSG:32717"
RES = 30.0
TILE = "17MPU"
DIR = "ASCENDING"
TK = "18"
NB = 16
MONTH = "2026-01"
BANDS = ["VV_dB", "VH_dB"]
PRODUCT = f"smonthly_{DIR}"


def _make_store(tile_dir: Path):
    w, h = 40, 32
    minx, maxy = 500000.0, 8500000.0
    transform = Affine(RES, 0.0, minx, 0.0, -RES, maxy)
    x = (minx + (np.arange(w) + 0.5) * RES).astype("float64")
    y = (maxy - (np.arange(h) + 0.5) * RES).astype("float64")
    zp = tile_dir / PRODUCT / "zarr" / f"s1grits_smonthly_{TILE}_{DIR}_TK{TK}.zarr"
    g = _init_zarr_2band(zp, x, y, CRS, transform, 16, 16,
                         processing_level="monthly_ARDC", band_names=BANDS)
    dt = np.datetime64(pd.Timestamp(f"{MONTH}-15").to_datetime64(), "ns")
    _append_zarr_timestep(g, dt, [
        ("VV_dB", np.full((h, w), -12.0, np.float32)),
        ("VH_dB", np.full((h, w), -18.0, np.float32)),
    ])
    return zp


def _gen(zp, tile_dir, **kw):
    return _generate_cog_preview_from_zarr(
        zarr_path=zp, month_str=MONTH, tile_dir=tile_dir, direction_label=DIR,
        mgrs_tile_id=TILE, track_token=TK, n_bursts_track=NB, target_crs=CRS,
        tile_clip=False, cog_block=16, band_names=BANDS, product_label=PRODUCT,
        **kw,
    )


def test_backfill_generates_missing_cog(tmp_path):
    """Zarr month exists, COG missing -> backfill creates it from Zarr."""
    tile_dir = tmp_path / TILE
    zp = _make_store(tile_dir)
    cog = tile_dir / PRODUCT / "cog" / f"s1grits_smonthly_{TILE}_{DIR}_TK{TK}_{MONTH}.tif"
    assert not cog.exists()

    cog_rel, _ = _gen(zp, tile_dir, generate_cog=True, generate_preview=False,
                      skip_if_exists=True)
    assert cog.exists()
    assert cog_rel == str(cog.relative_to(tile_dir))


def test_backfill_is_idempotent_and_does_not_rewrite(tmp_path):
    """A present COG is returned untouched (byte-identical), not regenerated."""
    tile_dir = tmp_path / TILE
    zp = _make_store(tile_dir)
    cog = tile_dir / PRODUCT / "cog" / f"s1grits_smonthly_{TILE}_{DIR}_TK{TK}_{MONTH}.tif"

    rel1, _ = _gen(zp, tile_dir, generate_cog=True, generate_preview=False,
                   skip_if_exists=True)
    data1 = cog.read_bytes()
    mtime1 = cog.stat().st_mtime_ns

    rel2, _ = _gen(zp, tile_dir, generate_cog=True, generate_preview=False,
                   skip_if_exists=True)
    assert rel2 == rel1
    assert cog.stat().st_mtime_ns == mtime1, "present COG must not be rewritten"
    assert cog.read_bytes() == data1


def test_skip_if_exists_regenerates_only_the_missing_asset(tmp_path):
    """With COG present and preview missing, only the preview is generated."""
    tile_dir = tmp_path / TILE
    zp = _make_store(tile_dir)
    # First produce the COG.
    _gen(zp, tile_dir, generate_cog=True, generate_preview=False)
    cog = tile_dir / PRODUCT / "cog" / f"s1grits_smonthly_{TILE}_{DIR}_TK{TK}_{MONTH}.tif"
    png = tile_dir / PRODUCT / "preview" / f"s1grits_smonthly_{TILE}_{DIR}_TK{TK}_{MONTH}.png"
    cog_mtime = cog.stat().st_mtime_ns
    assert not png.exists()

    cog_rel, png_rel = _gen(zp, tile_dir, generate_cog=True,
                            generate_preview=True, skip_if_exists=True)
    # COG untouched, preview created.
    assert cog.stat().st_mtime_ns == cog_mtime
    assert png.exists() and png_rel == str(png.relative_to(tile_dir))
    assert cog_rel == str(cog.relative_to(tile_dir))


def test_without_skip_flag_behaviour_unchanged(tmp_path):
    """Default (skip_if_exists=False) still generates as before."""
    tile_dir = tmp_path / TILE
    zp = _make_store(tile_dir)
    cog_rel, _ = _gen(zp, tile_dir, generate_cog=True, generate_preview=False)
    assert cog_rel is not None
    assert (tile_dir / PRODUCT / "cog" /
            f"s1grits_smonthly_{TILE}_{DIR}_TK{TK}_{MONTH}.tif").exists()
