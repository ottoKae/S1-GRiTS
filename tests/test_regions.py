"""Small real stores exercise region values, provenance and resumable writes."""
from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import xarray as xr
import zarr
from pyproj import Transformer
from shapely.geometry import box, mapping
from shapely.ops import transform as transform_geometry

from s1grits.regions import export_regions, plan_region_outputs, _warp_window
from s1grits import regions as region_module
from s1grits.spatial_sources import close_sources
from s1grits.zarr_encoding import create_zarr_array


@pytest.fixture(autouse=True)
def source_handles():
    close_sources()
    yield
    close_sources()


def _geographic(geometry):
    return transform_geometry(Transformer.from_crs(32650, 4326, always_xy=True).transform, geometry)


def _regions():
    return gpd.GeoDataFrame([{"region_id": "site", "region_internal_id": "r_site",
                             "spatial_mode": "vector_grid",
                             "geometry": _geographic(box(500005, 2999885, 500115, 2999995))}], crs=4326)


def _config(strategy="S0", static=False):
    return {"spatial": {"mode": "vector_grid"}, "source_strategy": strategy,
            "grid": {"mode": "target_crs", "target_crs": "EPSG:32650", "origin": [0, 0],
                     "target_resolution": 30, "resampling_method": "nearest"}, "include_static": static}


def _source(tmp_path, name, vv, vh, *, product="scenes", counts=None, static=False):
    vv, vh = np.asarray(vv, dtype="float32"), np.asarray(vh, dtype="float32")
    if vv.ndim == 1:
        vv = np.broadcast_to(vv[:, None, None], (len(vv), 4, 4)).copy()
        vh = np.broadcast_to(vh[:, None, None], vv.shape).copy()
    nt, height, width = vv.shape
    path = tmp_path / f"{name}.zarr"
    group = zarr.open_group(str(path), mode="w", zarr_format=3)
    variables = {}
    for band, values in (("VV_dB", vv), ("VH_dB", vh)):
        create_zarr_array(group, band, data=values, chunks=(1, min(2, height), min(2, width)),
                          dimension_names=["time", "y", "x"], fill_value=np.nan)
        variables[band] = {"dtype": "float32", "dims": ["time", "y", "x"],
                           "chunks": [1, min(2, height), min(2, width)], "nodata": np.nan, "units": "dB"}
    if counts is not None:
        values = np.broadcast_to(np.asarray(counts, dtype="uint8")[:, None, None], vv.shape).copy()
        create_zarr_array(group, "n_obs", data=values, dimension_names=["time", "y", "x"])
        variables["n_obs"] = {"dtype": "uint8", "dims": ["time", "y", "x"], "nodata": None}
    times = []
    for index in range(nt):
        start = pd.Timestamp("2024-01-01", tz="UTC") + pd.DateOffset(months=index)
        times.append({"time_index": index, "time_utc": (start + pd.Timedelta(seconds=2 if name == "B" else 0)).isoformat(),
                      "period_start": start.isoformat() if product == "smonthly" else None,
                      "period_end": (start + pd.DateOffset(months=1)).isoformat() if product == "smonthly" else None,
                      "platform": "S1A", "acquisition_id": f"orbit-{index}"})
    source = {"source_unit_id": name, "path": str(path), "product_type": product,
              "direction": "ASCENDING", "track": 18, "track_members": [18],
              "processing_signature": "original", "semantic_signature": "median-raw",
              "aggregation_method": "nanmedian", "aggregation_domain": "linear_power",
              "data_domain": "db", "geometry_group_id": name,
              "crs": "EPSG:32650", "transform": [30, 0, 500000, 0, -30, 3000000],
              "width": width, "height": height,
              "nominal_geometry": mapping(_geographic(box(500000, 3000000-height*30, 500000+width*30, 3000000))),
              "variables": variables, "times": times, "static_source": None,
              "n_obs_definition": "finite_VV_aligned_burst_stack_entries"}
    if static:
        static_path = tmp_path / f"{name}_static.zarr"
        stat = zarr.open_group(str(static_path), mode="w", zarr_format=3)
        create_zarr_array(stat, "local_incidence_angle", data=np.full((height, width), 10 if name == "A" else 20, dtype="float32"),
                          dimension_names=["y", "x"], fill_value=np.nan)
        source["static_source"] = {**source, "path": str(static_path), "times": [], "product_type": "static",
                                   "variables": {"local_incidence_angle": {"dtype": "float32", "dims": ["y", "x"],
                                                                          "nodata": np.nan, "units": "degrees"}}}
    return source


@pytest.mark.parametrize("strategy,expected", [("S0", [1, 0, 1]), ("S1", [1, 2, 1])])
def test_owner_strategy_values_counts_static_and_reader(tmp_path, strategy, expected):
    sources = [_source(tmp_path, "A", [10, 10, 10], [20, np.nan, 20], product="smonthly", counts=[1, 4, 2], static=True),
               _source(tmp_path, "B", [30, 30, 30], [40, 40, 40], product="smonthly", counts=[5, 6, 7], static=True)]
    config, regions = _config(strategy, static=True), _regions()
    plans = plan_region_outputs(config, regions, sources)
    state = {"completed": []}
    result = export_regions(config, regions, sources, tmp_path / "out", state, lambda: None, plans)
    assert result["status"] == "complete"
    store_path = tmp_path / "out" / plans[0]["zarr_path"]
    group = zarr.open_group(str(store_path), mode="r")
    mask = group["roi_mask"][:]
    assert mask.any()
    assert np.all(group["owner_index"][:][mask] == 1)
    for index, source_index in enumerate(expected):
        assert np.all(group["source_index"][index][mask] == source_index)
        if source_index == 0:
            assert np.isnan(group["VV_dB"][index][mask]).all()
            assert np.isnan(group["VH_dB"][index][mask]).all()
        else:
            np.testing.assert_array_equal(group["VV_dB"][index][mask], 10 if source_index == 1 else 30)
            np.testing.assert_array_equal(group["n_obs"][index][mask], ([1, 4, 2] if source_index == 1 else [5, 6, 7])[index])
    assert group["n_obs"].dtype == np.dtype("uint8")
    static_group = zarr.open_group(str(store_path.parent / "static.zarr"), mode="r")
    np.testing.assert_array_equal(static_group["local_incidence_angle"][:][mask], 10)
    assert static_group.attrs["grid_id"] == group.attrs["grid_id"]
    assert not {"aggregation_method", "aggregation_domain", "data_domain"}.intersection(static_group.attrs)
    frame = pd.read_parquet(tmp_path / "out" / "region_catalog.parquet")
    pixels = int(mask.sum())
    assert frame.iloc[0].source_switch_event_count == (2 * pixels if strategy == "S1" else 0)
    assert frame.iloc[0].fallback_observation_count == (pixels if strategy == "S1" else 0)
    assert frame.iloc[0].gap_pair_count == (pixels if strategy == "S0" else 0)
    assert frame.iloc[0].burst_source_switch_pixel_count is None
    with xr.open_zarr(store_path, consolidated=False) as dataset:
        assert dataset.sizes["time"] == 3
        assert np.issubdtype(dataset.time.dtype, np.datetime64)
        np.testing.assert_array_equal(dataset.x, group["x"][:])
    previous = group["source_index"][:].copy()
    export_regions(config, regions, sources, tmp_path / "out", state, lambda: None, plans)
    np.testing.assert_array_equal(group["source_index"][:], previous)
    assert len(state["completed"]) == 4


def test_scene_identity_not_timestamp_and_unknown_rejected(tmp_path):
    sources = [_source(tmp_path, "A", [1, 2], [3, 4]), _source(tmp_path, "B", [5, 6], [7, 8])]
    plans = plan_region_outputs(_config(), _regions(), sources)
    assert len(plans[0]["times"]) == 2
    assert len(plans[0]["times"][0]["members"]) == 2
    sources[1]["times"][0]["acquisition_id"] = "different-same-day"
    assert len(plan_region_outputs(_config(), _regions(), sources)[0]["times"]) == 3
    sources[1]["times"][0]["acquisition_id"] = None
    with pytest.raises(ValueError, match="acquisition identity"):
        plan_region_outputs(_config(), _regions(), sources)
    for record in sources[0]["times"]:
        record["acquisition_id"] = None
        record["time_utc"] = "2024-01-01T00:00:00+00:00"
    assert len(plan_region_outputs(_config(), _regions(), sources[:1])[0]["times"]) == 2


@pytest.mark.parametrize("mutation,match", [
    ({"track_members": [18, 19]}, "single-track"),
    ({"semantic_signature": None}, "compatibility"),
    ({"nominal_geometry": None}, "nominal"),
    ({"direction": None}, "flight direction"),
    ({"metadata_time_conflict": True}, "selected times"),
])
def test_reject_unverified_region_sources(tmp_path, mutation, match):
    source = _source(tmp_path, "A", [1], [2])
    source.update(mutation)
    with pytest.raises(ValueError, match=match):
        plan_region_outputs(_config(), _regions(), [source])


def test_region_rejects_packed_values_and_conflicting_domains(tmp_path):
    source = _source(tmp_path, "A", [1], [2])
    source["variables"]["VV_dB"]["declared_scale"] = 0.1
    with pytest.raises(ValueError, match="unpacked values"):
        plan_region_outputs(_config(), _regions(), [source])
    source["variables"]["VV_dB"]["declared_scale"] = 1
    source["variables"]["VV_dB"]["data_domain"] = "linear_power"
    with pytest.raises(ValueError, match="Conflicting units"):
        plan_region_outputs(_config(), _regions(), [source])
    source["data_domain"] = "unknown"
    for name in ("VV_dB", "VH_dB"):
        source["variables"][name].update(units="1", data_domain="unknown")
    with pytest.raises(ValueError, match="units are unknown"):
        plan_region_outputs(_config(), _regions(), [source])


def test_monthly_sources_require_compatible_aggregation_domains(tmp_path):
    sources = [_source(tmp_path, name, [1], [2], product="smonthly") for name in ("A", "B")]
    for bad in ("unknown", "db"):
        sources[1]["aggregation_domain"] = bad
        with pytest.raises(ValueError, match="aggregation domains"):
            plan_region_outputs(_config(), _regions(), sources)


def test_deep_region_path_writes_chunks_and_resumes(tmp_path):
    from s1grits.provenance import _io_path

    source = _source(tmp_path, "A", [1], [2], product="smonthly")
    output = tmp_path / ("long-spatial-job-" + "a" * 70) / ("b" * 70)
    config, regions, state = _config(), _regions(), {"completed": []}
    plans = plan_region_outputs(config, regions, [source])
    result = export_regions(config, regions, [source], output, state, lambda: None, plans)
    assert result["status"] == "complete"
    group = zarr.open_group(str(_io_path(output / plans[0]["zarr_path"])), mode="r")
    mask = group["roi_mask"][:]
    np.testing.assert_array_equal(group["VV_dB"][0][mask], 1)
    before = list(state["completed"])
    export_regions(config, regions, [source], output, state, lambda: None, plans)
    assert state["completed"] == before


def test_reader_failure_is_not_s1_nodata_and_resume_replaces(tmp_path):
    sources = [_source(tmp_path, "A", [1], [2]), _source(tmp_path, "B", [3], [4])]
    config = _config("S1")
    plans = plan_region_outputs(config, _regions(), sources)
    group = zarr.open_group(sources[0]["path"], mode="a")
    del group["VH_dB"]
    state = {"completed": []}
    result = export_regions(config, _regions(), sources, tmp_path / "out", state, lambda: None, plans)
    assert result["failed_outputs"] == 1
    assert not state["completed"]
    output = zarr.open_group(str(tmp_path / "out" / plans[0]["zarr_path"]), mode="r")
    assert not output["source_index"][:].any()
    source_table = pd.read_parquet(tmp_path / "out" / "sources.parquet")
    assert source_table.loc[source_table.source_unit_id == "A", "source_status"].iloc[0] == "read_failed"
    assert json.loads(source_table.loc[source_table.source_unit_id == "A", "errors_json"].iloc[0])[0]["time_index"] == 0
    create_zarr_array(group, "VH_dB", data=np.full((1, 4, 4), 2, dtype="float32"), dimension_names=["time", "y", "x"])
    close_sources()
    result = export_regions(config, _regions(), sources, tmp_path / "out", state, lambda: None, plans)
    assert result["status"] == "complete"
    assert len(state["completed"]) == 1
    assert not state["region_errors"]
    mask = output["roi_mask"][:]
    np.testing.assert_array_equal(output["source_index"][0][mask], 1)


def test_bilinear_uses_linear_power_and_keeps_negative_db(tmp_path):
    source = _source(tmp_path, "A", np.array([[[-10, 0], [-10, 0]]]), np.array([[[-10, 0], [-10, 0]]]))
    grid = {"crs": source["crs"], "transform": [10, 0, 500020, 0, -10, 2999980], "height": 1, "width": 1}
    result = _warp_window(source, "VV_dB", 0, grid, (0, 0, 1, 1), "bilinear", "db")
    np.testing.assert_allclose(result, 10 * np.log10(.4), atol=1e-5)


def test_no_source_still_preserves_requested_region(tmp_path):
    result = export_regions(_config(), _regions(), [], tmp_path / "out", {}, lambda: None)
    assert result["status"] == "no_source"
    assert pd.read_parquet(tmp_path / "out" / "region_catalog.parquet").iloc[0].region_id == "site"
    assert len(gpd.read_parquet(tmp_path / "out" / "regions.parquet")) == 1


def test_owner_uses_full_nominal_boundaries_and_preserves_holes(tmp_path):
    sources = [_source(tmp_path, "A", [1], [2]), _source(tmp_path, "B", [3], [4])]
    sources[0]["nominal_geometry"] = mapping(_geographic(box(500000, 2999880, 500090, 3000000)))
    sources[1]["nominal_geometry"] = mapping(_geographic(box(500030, 2999880, 500120, 3000000)))
    regions = _regions()
    hole = _geographic(box(500030, 2999910, 500060, 2999940))
    regions.geometry = regions.geometry.difference(hole)
    plans = plan_region_outputs(_config(), regions, sources)
    export_regions(_config(), regions, sources, tmp_path / "out", {}, lambda: None, plans)
    group = zarr.open_group(str(tmp_path / "out" / plans[0]["zarr_path"]), mode="r")
    mask, owner = group["roi_mask"][:], group["owner_index"][:]
    assert (~mask).any()
    assert np.isnan(group["VV_dB"][0][~mask]).all()
    x = np.broadcast_to(group["x"][:], mask.shape)
    y = np.broadcast_to(group["y"][:][:, None], mask.shape)
    assert np.all(owner[mask & (x < 500030)] == 1)
    farther_from_boundary = mask & (x > 500070) & (x < 500090) & (y > 2999940) & (y < 2999970)
    assert farther_from_boundary.any()
    assert np.all(owner[farther_from_boundary] == 2)
    tied = mask & (x > 500040) & (x < 500060) & (y > 2999970)
    assert tied.any()
    assert np.all(owner[tied] == 1)


def test_interrupted_chunk_is_rewritten_before_time_commit(tmp_path, monkeypatch):
    values = np.arange(1200, dtype="float32").reshape(1, 2, 600)
    source = _source(tmp_path, "A", values, values + 1)
    regions = _regions()
    regions.geometry = [_geographic(box(500005, 2999945, 517995, 2999995))]
    plans = plan_region_outputs(_config(), regions, [source])
    original = region_module._select_chunk
    calls = 0

    def interrupted(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("Injected process interruption between real writes")
        return original(*args)

    monkeypatch.setattr(region_module, "_select_chunk", interrupted)
    state = {"completed": []}
    with pytest.raises(KeyboardInterrupt):
        export_regions(_config(), regions, [source], tmp_path / "out", state, lambda: None, plans)
    assert not state["completed"]
    path = tmp_path / "out" / plans[0]["zarr_path"]
    group = zarr.open_group(str(path), mode="a")
    assert np.isfinite(group["VV_dB"][0, :, :512]).any()
    group["VV_dB"][0, :, :512] = -999
    monkeypatch.setattr(region_module, "_select_chunk", original)
    export_regions(_config(), regions, [source], tmp_path / "out", state, lambda: None, plans)
    assert len(state["completed"]) == 1
    assert not np.any(group["VV_dB"][:] == -999)
    assert np.isfinite(group["VV_dB"][0, :, 512:]).any()
