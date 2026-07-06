"""ml_loader: the thin downstream ML access layer (torch-free).

Builds a synthetic datacube (stac-geoparquet items.parquet + a real Zarr
store written by the production writer) and exercises query_items,
load_timeseries, sample_patches, band stats, build_patch_index, and
PatchDataset. No torch, no network — this locks in the documented contract
that the loader works as a plain-numpy sequence.
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

pytest.importorskip("xarray")
zarr = pytest.importorskip("zarr")

import s1grits.ml_loader as ml  # noqa: E402
from s1grits.workflow_scenes import (  # noqa: E402
    _append_zarr_timestep,
    _init_zarr_2band,
)

CRS = "EPSG:32717"
RES = 30.0
H, W = 32, 40
BANDS = ["VV_dB", "VH_dB"]
MONTHS = ["2026-01-15", "2026-02-14", "2026-03-15"]
TILE = "17MPU"
COLLECTION = "s1grits-smonthly"


@pytest.fixture(scope="module")
def cube_root(tmp_path_factory):
    """Datacube root with one tile store (3 months, 2 bands) + items.parquet."""
    root = tmp_path_factory.mktemp("cube")

    # --- Zarr store via the production writer ---
    minx, maxy = 500000.0, 8500000.0
    transform = Affine(RES, 0.0, minx, 0.0, -RES, maxy)
    x = (minx + (np.arange(W) + 0.5) * RES).astype("float64")
    y = (maxy - (np.arange(H) + 0.5) * RES).astype("float64")
    zdir = root / TILE / "smonthly_VV_dB_VH_dB" / "zarr"
    zpath = zdir / f"s1grits_smonthly_{TILE}_ASCENDING_TK18_N3.zarr"
    g = _init_zarr_2band(
        zpath, x, y, CRS, transform, chunk_y=16, chunk_x=16,
        processing_level="monthly_ARDC", band_names=BANDS,
    )
    for i, when in enumerate(MONTHS):
        vv = np.full((H, W), float(i), np.float32)         # VV == month index
        vh = np.full((H, W), 10.0 + i, np.float32)         # VH == 10 + index
        vh[0:4, 0:4] = np.nan                              # a NoData corner
        dt = np.datetime64(pd.Timestamp(when).to_datetime64(), "ns")
        _append_zarr_timestep(g, dt, [("VV_dB", vv), ("VH_dB", vh)])

    # --- stac-geoparquet items.parquet (one item per month) ---
    pq_dir = root / "collections" / COLLECTION
    pq_dir.mkdir(parents=True)
    href = Path("..") / ".." / zpath.relative_to(root)
    rows = [{
        "item_id": f"{TILE}_ASCENDING_TK18_N3_{m[:7]}",
        "mgrs:tile_id": TILE,
        "sat:orbit_state": "ascending",
        "datetime": m,
        "proj:epsg": 32717,
        "bbox": {"xmin": -79.6, "ymin": -13.6, "xmax": -79.2, "ymax": -13.2},
        "assets": {"zarr": {"href": str(href)}},
    } for m in MONTHS]
    pd.DataFrame(rows).to_parquet(pq_dir / "items.parquet")
    return root


# ---------------------------------------------------------------------------
# query_items
# ---------------------------------------------------------------------------
def test_query_items_filters_tile_time_direction(cube_root):
    df = ml.query_items(cube_root, collection=COLLECTION, tile=TILE)
    assert len(df) == 3
    assert Path(df.attrs["parquet_dir"]).name == COLLECTION

    df = ml.query_items(cube_root, collection=COLLECTION,
                        time=("2026-02-01", "2026-12-31"))
    assert len(df) == 2  # Feb + Mar

    assert len(ml.query_items(cube_root, collection=COLLECTION,
                              tile="99ZZZ")) == 0
    assert len(ml.query_items(cube_root, collection=COLLECTION,
                              direction="DESCENDING")) == 0
    assert len(ml.query_items(cube_root, collection=COLLECTION,
                              direction="ASCENDING")) == 3


def test_query_items_autodetects_single_collection(cube_root):
    # No collection argument: exactly one collection exists -> found.
    assert len(ml.query_items(cube_root)) == 3


# ---------------------------------------------------------------------------
# load_timeseries
# ---------------------------------------------------------------------------
def test_load_timeseries_shape_bands_values(cube_root):
    da = ml.load_timeseries(cube_root, collection=COLLECTION, tile=TILE)
    assert da.dims == ("time", "band", "y", "x")
    assert da.shape == (3, 2, H, W)
    assert [str(b) for b in da["band"].values] == BANDS  # canonical: VV first
    # Values survive the round trip exactly.
    assert float(da.sel(band="VV_dB").isel(time=2)[5, 5]) == 2.0
    assert float(da.sel(band="VH_dB").isel(time=0)[10, 10]) == 10.0
    assert np.isnan(float(da.sel(band="VH_dB").isel(time=0)[0, 0]))


def test_load_timeseries_time_and_band_subset(cube_root):
    da = ml.load_timeseries(cube_root, collection=COLLECTION, tile=TILE,
                            time=("2026-02-01", "2026-02-28"),
                            bands=["VH_dB"])
    assert da.shape == (1, 1, H, W)
    assert float(da.isel(time=0, band=0)[10, 10]) == 11.0


def test_load_timeseries_missing_band_raises(cube_root):
    with pytest.raises(KeyError, match="GLCM_contrast"):
        ml.load_timeseries(cube_root, collection=COLLECTION,
                           bands=["GLCM_contrast"])


def test_load_timeseries_no_match_raises(cube_root):
    with pytest.raises(ValueError, match="No items match"):
        ml.load_timeseries(cube_root, collection=COLLECTION, tile="99ZZZ")


# ---------------------------------------------------------------------------
# sample_patches
# ---------------------------------------------------------------------------
def test_sample_patches_grid_count_and_shape(cube_root):
    da = ml.load_timeseries(cube_root, collection=COLLECTION, tile=TILE)
    patches = ml.sample_patches(da, patch_size=16)  # non-overlapping grid
    # 32x40 with 16x16 patches -> 2 rows x 2 cols (40//16=2 full cols)
    assert patches.shape == (4, 3, 2, 16, 16)


def test_sample_patches_min_valid_drops_nodata_patch(cube_root):
    da = ml.load_timeseries(cube_root, collection=COLLECTION, tile=TILE)
    all_p = ml.sample_patches(da, patch_size=16, min_valid=0.0)
    clean = ml.sample_patches(da, patch_size=16, min_valid=1.0)
    # The NaN corner lives in exactly one 16x16 patch (top-left).
    assert all_p.shape[0] - clean.shape[0] == 1


def test_sample_patches_random_reproducible(cube_root):
    da = ml.load_timeseries(cube_root, collection=COLLECTION, tile=TILE)
    a = ml.sample_patches(da, patch_size=8, mode="random", n=5, seed=42)
    b = ml.sample_patches(da, patch_size=8, mode="random", n=5, seed=42)
    assert a.shape == (5, 3, 2, 8, 8)
    np.testing.assert_array_equal(
        np.nan_to_num(a, nan=-9e9), np.nan_to_num(b, nan=-9e9)
    )


def test_sample_patches_meta_carries_crs_and_bounds(cube_root):
    da = ml.load_timeseries(cube_root, collection=COLLECTION, tile=TILE)
    patches, meta = ml.sample_patches(da, patch_size=16, return_meta=True)
    assert len(meta) == patches.shape[0]
    m = meta[0]
    # crs comes from proj:epsg in the items.parquet (int) or the store's
    # EPSG string — either way it must identify UTM 17S.
    assert str(m["crs"]).lstrip("EPSG:").lstrip("epsg:") == "32717"
    assert m["x_min"] < m["x_max"] and m["y_min"] < m["y_max"]


# ---------------------------------------------------------------------------
# band statistics
# ---------------------------------------------------------------------------
def test_band_stats_and_roundtrip(cube_root, tmp_path):
    da = ml.load_timeseries(cube_root, collection=COLLECTION, tile=TILE)
    stats = ml.compute_band_stats(da)
    # VV values are exactly {0, 1, 2} over three equal planes.
    assert stats["VV_dB"]["mean"] == pytest.approx(1.0)
    assert stats["VV_dB"]["std"] == pytest.approx(np.sqrt(2 / 3))
    assert stats["VV_dB"]["min"] == 0.0 and stats["VV_dB"]["max"] == 2.0
    # NaN corner excluded from VH count.
    assert stats["VH_dB"]["count"] == 3 * H * W - 3 * 16

    p = tmp_path / "stats.json"
    ml.save_band_stats(p, stats)
    loaded = ml.load_band_stats(p)
    assert set(loaded) == set(stats)
    for band in stats:
        assert loaded[band] == pytest.approx(stats[band])


# ---------------------------------------------------------------------------
# build_patch_index + PatchDataset (torch-free)
# ---------------------------------------------------------------------------
def test_build_patch_index_enumerates_origins(cube_root):
    idx = ml.build_patch_index(cube_root, collection=COLLECTION, tile=TILE,
                               patch_size=16)
    assert len(idx["index"]) == 4
    assert idx["bands"] == BANDS
    assert idx["patch_size"] == (16, 16)
    assert len(idx["time_grid"]) == 3


def test_patch_dataset_works_without_torch(cube_root):
    assert "torch" not in sys.modules or pytest.skip("torch installed; torch-free path untestable")
    ds = ml.PatchDataset(cube_root, collection=COLLECTION, tile=TILE,
                         patch_size=16, nan_to=0.0)
    assert len(ds) == 4
    patch = ds[0]
    assert isinstance(patch, np.ndarray)  # numpy fallback, as documented
    assert patch.shape == (3, 2, 16, 16)
    assert np.isfinite(patch).all()  # nan_to applied


def test_patch_dataset_zscore_normalization(cube_root):
    ds = ml.PatchDataset(cube_root, collection=COLLECTION, tile=TILE,
                         patch_size=16, normalize="zscore", nan_to=0.0)
    all_vv = np.concatenate([np.asarray(ds[i])[:, 0].ravel() for i in range(len(ds))])
    assert abs(all_vv.mean()) < 0.05  # ~zero mean after z-score
    ds_meta = ml.PatchDataset(cube_root, collection=COLLECTION, tile=TILE,
                              patch_size=16, return_meta=True)
    _, meta = ds_meta[0]
    assert set(meta) == {"store", "y0", "x0"}
