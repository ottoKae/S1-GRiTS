"""Opt-in source identities survive the real eager loader and monthly pipeline."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import zarr
from rasterio.transform import Affine

from s1grits import asf_io
from s1grits.scenes import pipeline, smonthly_writer


def _native_kwargs(root):
    transform = Affine(30, 0, 500000, 0, -30, 8500000)
    dates = [pd.Timestamp("2025-01-05T00:00:00Z")] * 2
    profile = dict(crs="EPSG:32717", transform=transform, width=4, height=3,
                   dtype="float32", nodata=np.nan)
    return dict(
        mgrs_tile_id="17MPU", direction_label="ASCENDING", tile_dir=root / "17MPU",
        final_vv=[np.full((3, 4), value, np.float32) for value in (1, 100)],
        final_vh=[np.full((3, 4), value, np.float32) for value in (0.5, 50)],
        prof_vv=[dict(profile), dict(profile)], prof_vh=[dict(profile), dict(profile)],
        clean_dates=dates, target_crs="EPSG:32717", target_res=30.0,
        generate_cog=False, generate_preview=False, chunk_y=2, chunk_x=2, cog_block=16,
        on_time_conflict="skip", monthly_cfg={"composite_method": "median", "blockwise_threads": 1},
        processing_level="ARDC", transform=transform, width=4, height=3,
        x_coords=500000 + (np.arange(4) + 0.5) * 30,
        y_coords=8500000 - (np.arange(3) + 0.5) * 30,
        tile_clip=False, valid_clean_indices={0, 1},
    )


def test_paired_loader_opt_in_returns_input_positions_with_same_timestamp(monkeypatch):
    dates = [pd.Timestamp("2025-01-05T00:00:00Z")] * 3
    frame = pd.DataFrame({"url_copol": ["a", "b", "c"],
                          "url_crosspol": ["d", "e", "f"], "acq_datetime": dates})
    values = [np.full((2, 2), i, np.float32) for i in (1, 2, 3)]
    profile = {"crs": "EPSG:32717", "transform": Affine.identity()}
    monkeypatch.setattr(asf_io, "_download_paired_with_retry", lambda *args: (
        values, [profile] * 3, [None] * 3,
        [values[0], None, values[2]], [profile, None, profile], [None, "not_found", None],
    ))
    default = asf_io.load_and_despeckle_rtc_strict(frame)
    tracked = asf_io.load_and_despeckle_rtc_strict(frame, return_source_indices=True)
    assert len(default) == 5
    assert len(tracked) == 6 and tracked[-1] == [0, 2]
    assert tracked[-2] == [dates[0], dates[2]]
    for old, new in zip(default[0], tracked[0]):
        np.testing.assert_array_equal(old, new)


def test_monthly_qc_uses_exact_rows_without_same_timestamp_cross_contamination(monkeypatch):
    dates = [pd.Timestamp("2025-01-05T00:00:00Z")] * 2
    frame = pd.DataFrame({
        "_source_row": [100, 101, 102], "acq_dt": [dates[0]] * 3,
        "pass_id": [1, 1, 1], "acq_group_id_within_mgrs_tile": [1, 1, 2],
        "track_token": ["18"] * 3, "jpl_burst_id": ["B001", "B002", "B003"],
    })
    seen = []
    monkeypatch.setattr(pipeline, "_prealign_scenes_to_master_grid", lambda arrays, profiles, *a, **k: (arrays, profiles))
    monkeypatch.setattr(pipeline, "_compute_scene_dst_bounds", lambda *a, **k: [(0, 2, 0, 2)] * 2)
    monkeypatch.setattr(pipeline, "_missing_interior_bursts", lambda *a, **k: set())
    def mask(indices, *args, **kwargs):
        seen.append(indices)
        return np.ones((2, 2), dtype=bool)
    monkeypatch.setattr(pipeline, "_track_valid_mask_block", mask)
    fractions = iter([1.0, 0.0])
    monkeypatch.setattr(pipeline, "_interior_hole_fraction", lambda *a, **k: next(fractions))
    incomplete = []
    valid = pipeline._prepare_valid_clean_indices_for_monthly(
        "17MPU", "ASCENDING", [object()] * 2, [], [object()] * 2, [], dates, frame,
        "EPSG:32717", Affine.identity(), 2, 2, tile_clip=False,
        track_footprint={"18": 2}, track_footprint_ids={"18": {"B001", "B002", "B003"}},
        incomplete_policy="skip", incomplete_sink=incomplete, interior_hole_max_frac=0.5,
        clean_source_rows=[100, 102],
    )
    assert seen == [[0], [1]]
    assert valid == {1}
    assert len(incomplete) == 1
    assert incomplete[0]["metadata_bursts"] == 2
    assert incomplete[0]["loaded_bursts"] == 1
    assert incomplete[0]["network_missing"] == 1


def test_native_monthly_tracks_keep_same_timestamp_arrays_separate(tmp_path):
    kwargs = _native_kwargs(tmp_path)
    kwargs["df_batch"] = pd.DataFrame({
        "_source_row": [10, 20], "acq_dt": kwargs["clean_dates"],
        "track_token": ["18", "19"], "track_number": [18, 19],
        "jpl_burst_id": ["B001", "B002"], "opera_id": ["OPERA-1", "OPERA-2"],
        "pass_id": [1, 2], "acq_group_id_within_mgrs_tile": [1, 2],
    })
    records = smonthly_writer._write_monthly_output_scenes(**kwargs, clean_source_rows=[10, 20])
    assert len(records) == 2
    stores = list(tmp_path.glob("17MPU/smonthly_ASCENDING/zarr/*.zarr"))
    assert len(stores) == 2
    for store in stores:
        group = zarr.open_group(str(store), mode="r")
        expected = 0 if "TK18" in store.name else 20
        np.testing.assert_allclose(group["VV_dB"][:], expected, atol=1e-6)
        np.testing.assert_allclose(group["VH_dB"][:], expected + 10 * np.log10(0.5), atol=1e-5)
        np.testing.assert_array_equal(group["n_obs"][:], 1)


def test_native_tracker_receives_catalog_after_pixels_without_stac(tmp_path, monkeypatch):
    kwargs = _native_kwargs(tmp_path)
    events = []
    class Tracker:
        def bind(self, arrays, band):
            return arrays
        def begin_write(self, selected):
            assert selected == [0, 1]
            events.append("begin")
        def commit(self, *, catalog_record):
            group = zarr.open_group(str(next(tmp_path.rglob("*.zarr"))), mode="r")
            assert group["time"].shape == (1,)
            assert group["VV_dB"].shape == (1, 3, 4)
            assert np.isfinite(group["VV_dB"][:]).all()
            events.append(catalog_record)
    monkeypatch.setattr(smonthly_writer, "_ws_write_monthly_stac_item", lambda *a, **k: pytest.fail("tracked path must not write STAC"))
    records = smonthly_writer._write_smonthly_one_track(**kwargs, track_token="18", _provenance_tracker=Tracker())
    assert len(records) == 1
    assert events == ["begin", records[0]]
    assert records[0]["month"] == "2025-01"


def _pipeline_fixture(monkeypatch, tmp_path, *, download_failure=False):
    kwargs = _native_kwargs(tmp_path)
    frame = pd.DataFrame({
        "acq_dt": [kwargs["clean_dates"][0]] * 6,
        "opera_id": [f"OPERA-{i}" for i in range(6)],
        "jpl_burst_id": [f"B{i:03d}" for i in range(6)],
        "track_token": ["18"] * 6, "track_number": [18] * 6,
        "pass_id": [1] * 6, "acq_group_id_within_mgrs_tile": [1] * 6,
        "orbit_pass": ["ASCENDING"] * 6,
        "url_copol": [f"https://example.org/{i}_VV.tif" for i in range(6)],
        "url_crosspol": [f"https://example.org/{i}_VH.tif" for i in range(6)],
    })
    captured = {}
    class Tracking:
        def register_candidates(self, tile, direction, candidate_frame):
            assert "download" not in captured
            assert (tile, direction) == ("17MPU", "ASCENDING")
            captured["plan"] = candidate_frame.copy()
        def filter_download_rows(self, download_frame):
            return download_frame[download_frame["_source_row"] != 0].copy()
        def write_monthly(self, *, writer_kwargs, candidate_frame, clean_source_rows):
            captured["writer"] = writer_kwargs
            captured["candidates"] = candidate_frame.copy()
            captured["rows"] = list(clean_source_rows)
            return [{"month": "2025-01"}]
    monkeypatch.setattr(pipeline, "query_rtc_metadata_for_tile", lambda *a: frame.iloc[::-1].copy())
    monkeypatch.setattr(pipeline, "get_memory_strategy_from_config", lambda *a, **k: "monthly")
    monkeypatch.setattr(pipeline, "_get_mgrs_tile_geometry_wkt", lambda *a: "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))")
    monkeypatch.setattr(pipeline, "_adopt_existing_master_grid", lambda *a: (
        kwargs["transform"], 4, 3, kwargs["x_coords"], kwargs["y_coords"]))
    monkeypatch.setattr(pipeline, "_prefilter_metadata_incomplete_acquisitions", lambda **k:
                        k["df_rtc_ts"].loc[k["df_rtc_ts"]["_source_row"] != 1].copy())
    def download(download_frame, **options):
        captured["download"] = download_frame.copy()
        assert options["return_source_indices"] is True
        assert download_frame["_source_row"].tolist() == [2, 3, 4, 5]
        if download_failure:
            raise RuntimeError("synthetic download interruption")
        # Row 3 failed to download; row 4 decoded with an invalid profile.
        selected = [0, 2, 3]
        arrays = [np.full((3, 4), i + 1, dtype=np.float32) for i in selected]
        profiles = [kwargs["prof_vv"][0], None, kwargs["prof_vv"][0]]
        return arrays, profiles, arrays, profiles, download_frame.iloc[selected]["acq_datetime"].tolist(), selected
    monkeypatch.setattr(pipeline, "load_and_despeckle_rtc_strict", download)
    def qc(*args, **options):
        assert options["clean_source_rows"] == [2, 5]
        assert len(args[2]) == len(args[6]) == 2
        assert args[7]["_source_row"].tolist() == [2, 3, 4, 5]
        return {1}
    monkeypatch.setattr(pipeline, "_prepare_valid_clean_indices_for_monthly", qc)
    config = {
        "_provenance_tracking": Tracking(),
        "roi": {"flight_direction": "ASCENDING", "polarization": "VV+VH"},
        "processing": {"monthly": {"enabled": True, "only": True},
                       "tile_clip": False, "spatial_despeckle": False},
        "query": {"footprint_lookback_months": 0},
        "memory": {"batch_max_retries": 0, "clear_cache_per_batch": False},
    }
    result = pipeline.process_single_scenes_tile(
        "17MPU", [("2025-01-01", "2025-01-31")], config, tmp_path, quiet=True)
    return result, captured


def test_pipeline_keeps_full_plan_and_filters_array_positions_exactly(tmp_path, monkeypatch):
    result, captured = _pipeline_fixture(monkeypatch, tmp_path)
    assert result["status"] == "success", result
    assert captured["plan"]["_source_row"].tolist() == list(range(6))
    assert captured["candidates"]["_source_row"].tolist() == list(range(6))
    assert captured["rows"] == [2, 5]
    assert captured["writer"]["valid_clean_indices"] == {1}
    assert not (tmp_path / "17MPU/catalog.parquet").exists()


def test_tracked_download_failure_cannot_finish_tile_as_success(tmp_path, monkeypatch):
    result, captured = _pipeline_fixture(monkeypatch, tmp_path, download_failure=True)
    assert result["status"] == "failed"
    assert "writer" not in captured


@pytest.mark.parametrize("time_block", [
    {"time": {"full": 2026}},
    {"time_range": {"mode": "full", "full_mode": {"end_year": 2026}}},
])
def test_tracked_full_archive_rejects_before_time_discovery(tmp_path, monkeypatch, time_block):
    import yaml
    from s1grits import workflow_scenes, time_utils
    config = {
        "metadata": {"provenance": {"enabled": True, "dataset_id": "tiny", "run_id": "full-reject"}},
        "roi": {"manual_mgrs_tiles": ["17MPU"], "flight_direction": "ASCENDING"},
        "processing": {"monthly": {"enabled": True, "only": True}},
        "memory": {"batch_strategy": "monthly"},
        "output": {"base_dir": str(tmp_path / "output")},
        **time_block,
    }
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    def forbidden(*args, **kwargs):
        pytest.fail("tracked full mode must reject before time discovery or ASF queries")
    monkeypatch.setattr(workflow_scenes, "parse_time_range_config", forbidden)
    monkeypatch.setattr(time_utils, "detect_earliest_available_date_from_mgrs_tiles", forbidden)
    monkeypatch.setattr(time_utils, "detect_earliest_available_date", forbidden)
    with pytest.raises(ValueError, match="explicit years/months"):
        workflow_scenes.run_scenes_workflow(path)
    assert not (tmp_path / "output").exists()


def test_real_workflow_accepts_asf_geodataframe_with_polygon_geometry(tmp_path, monkeypatch):
    import geopandas as gpd
    from shapely.geometry import box
    from s1grits import workflow
    from test_provenance_workflow import Case, _tables
    case = Case(tmp_path, monkeypatch)
    original_query = case.query
    def geodataframe_query(*args, **kwargs):
        frame = original_query(*args, **kwargs)
        return gpd.GeoDataFrame(frame, geometry=[box(-81.0, -13.6, -80.9, -13.5)
                                                for _ in range(len(frame))], crs="EPSG:4326")
    monkeypatch.setattr(workflow, "get_rtc_s1_ts_metadata_from_mgrs_tiles", geodataframe_query)
    assert case.run()["17MPU"]["status"] == "success"
    _, (outputs, sources, links) = _tables(case.root)
    assert len([row for row in outputs if row["generation_id"]]) == 4
    href = {row["source_id"]: row["href"] for row in sources}
    assert {href[row["source_id"]] for row in links if row["role"] == "used"} == set(case.payloads)


def test_changed_current_blocks_recovery_before_any_catalog_install(tmp_path, monkeypatch):
    import json
    from s1grits import provenance_catalog
    from test_provenance_workflow import Case, _snapshot, _assert_no_network, _forbid_image_writes
    case = Case(tmp_path, monkeypatch)
    actual_install = provenance_catalog._install_catalog
    global_installs = 0
    def fail_fourth_global(root, target, entry):
        nonlocal global_installs
        if target == "catalog.parquet":
            global_installs += 1
            if global_installs == 4:
                raise OSError("synthetic final global Catalog interruption")
        return actual_install(root, target, entry)
    monkeypatch.setattr(provenance_catalog, "_install_catalog", fail_fourth_global)
    with pytest.raises((OSError, RuntimeError), match="Catalog"):
        case.run()
    assert global_installs == 4
    pending = list((case.root / "provenance/pending").glob("*.json"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_text(encoding="utf-8"))["phase"] == "committed_provenance_pending"
    pointer_path = case.root / "provenance/current.json"
    current = json.loads(pointer_path.read_text(encoding="utf-8"))
    older = next(value for path in (case.root / "provenance/runs").glob("*/completed.json")
                 if (value := json.loads(path.read_text(encoding="utf-8"))["pointer"]) != current)
    pointer_path.write_text(json.dumps(older), encoding="utf-8")
    before = _snapshot(case.root)
    def forbidden(*args, **kwargs):
        pytest.fail("a changed current pointer must reject before installing either Catalog")
    monkeypatch.setattr(provenance_catalog, "_install_catalog", forbidden)
    _assert_no_network(monkeypatch)
    _forbid_image_writes(monkeypatch)
    with pytest.raises(ValueError, match="metadata base|lost update|current"):
        case.run()
    assert _snapshot(case.root) == before
