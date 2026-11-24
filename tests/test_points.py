"""Real Zarr and Parquet checks for the local point extraction path."""
import hashlib
import json

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import zarr
from affine import Affine
from shapely.geometry import Point

from s1grits.atomic_write import atomic_write_json
from s1grits import points


def _source(tmp_path, name, arrays, *, crs="EPSG:32650", transform=None, times=None, static=False):
    transform = transform or Affine(30, 0, 500000, 0, -30, 3500000)
    path = tmp_path / f"{name}.zarr"
    group = zarr.open_group(path, mode="w", zarr_format=3)
    variables = {}
    for variable, data in arrays.items():
        data = np.asarray(data)
        dims = ["y", "x"] if static else ["time", "y", "x"]
        chunks = (2, 2) if static else (1, 2, 2)
        group.create_array(variable, data=data, chunks=chunks, dimension_names=dims)
        variables[variable] = {"dtype": str(data.dtype), "dims": dims, "chunks": list(chunks),
                               "nodata": None, "units": "dB" if variable.endswith("dB") else "count"}
    shape = next(iter(arrays.values())).shape
    if times is None:
        times = [] if static else [{"time_index": i, "time_utc": "2024-01-15T00:00:00Z",
                                   "period_start": None, "period_end": None,
                                   "acquisition_id": f"acq-{i}", "platform": "S1A"}
                                  for i in range(shape[0])]
    return {"source_unit_id": name, "path": str(path), "product_type": "static" if static else "scenes",
            "direction": "ASCENDING", "track": 18, "track_members": [18],
            "processing_signature": "original", "geometry_group_id": name,
            "crs": crs, "transform": list(transform)[:6], "width": shape[-1], "height": shape[-2],
            "variables": variables, "times": times, "static_source": None}


def _points(source):
    transform = Affine(*source["transform"])
    geometry = [Point(*(transform * xy)) for xy in [(0.5, 0.5), (1.5, 0.5), (3.5, 2.5), (-0.5, 0.5)]]
    return gpd.GeoDataFrame({"point_id": ["a", "b", "c", "outside"],
                             "source_feature_id": ["a", "b", "c", "outside"],
                             "part_index": [0] * 4, "user_label": ["one", "two", "three", "four"]},
                            geometry=geometry, crs=source["crs"]).to_crs("EPSG:4326")


def _run(config, frame, sources, output, manifest=None):
    manifest = {"completed": []} if manifest is None else manifest
    output.mkdir(parents=True, exist_ok=True)

    def checkpoint():
        atomic_write_json(manifest, output / "manifest.json")

    result = points.extract_points(config, frame, sources, output, manifest, checkpoint)
    return result, manifest


def _observations(output):
    return pd.concat([pd.read_parquet(path) for path in sorted((output / "observations").glob("*.parquet"))],
                     ignore_index=True)


def test_original_values_duplicate_times_variable_nodata_and_chunk_sharing(tmp_path, monkeypatch):
    vv = np.arange(24, dtype="f4").reshape(2, 3, 4) / 7
    vh = vv.copy() - 20
    vh[0, 0, 0] = np.nan
    source = _source(tmp_path, "first", {"VV_dB": vv, "VH_dB": vh})
    frame = _points(source)
    reads = []
    original_read = points.read_source_window

    def recording_read(*args):
        reads.append((args[1], args[2], args[3]))
        return original_read(*args)

    monkeypatch.setattr(points, "read_source_window", recording_read)
    output = tmp_path / "points"
    result, manifest = _run({"variables": ["VV_dB", "VH_dB"], "output": {"export_csv": True}},
                            frame, [source], output)
    assert result["status"] == "complete"
    assert len(manifest["completed"]) == 4
    assert len(reads) == 8  # Two occupied spatial chunks per time/variable.
    table = _observations(output)
    assert len(table) == 16
    assert not table.duplicated(["point_id", "source_id"]).any()
    a_vv = table[(table.point_id == "a") & (table.variable == "VV_dB")].sort_values("time_index")
    np.testing.assert_array_equal(a_vv.value.to_numpy(dtype=float), vv[:, 0, 0])
    assert a_vv.time_utc.nunique() == 1
    assert a_vv.source_id.nunique() == 2
    assert table[(table.point_id == "a") & (table.variable == "VH_dB") & (table.time_index == 0)].iloc[0].status == "nodata"
    assert set(table[table.point_id == "outside"].status) == {"outside"}
    assert table[table.point_id == "outside"].sample_x.isna().all()
    assert len(pd.read_csv(output / "export.csv")) == 16
    assert gpd.read_parquet(output / "points.parquet").user_label.tolist() == frame.user_label.tolist()
    source_table = pd.read_parquet(output / "sources.parquet")
    assert source_table.source_id.is_unique
    assert set(source_table.acquisition_id) == {"acq-0", "acq-1"}
    summary = pd.read_parquet(output / "point_summary.parquet").set_index("point_id")
    assert summary.loc["a", "valid_count"] == 3
    assert summary.loc["outside", "status"] == "no_source"


def test_multiple_crs_and_static_are_separate_and_resume_without_duplicates(tmp_path):
    first = _source(tmp_path, "utm", {"VV_dB": np.full((2, 3, 4), 7.125, dtype="f4")})
    geographic = _source(tmp_path, "geographic", {"VV_dB": np.full((1, 20, 20), 11.5, dtype="f4")},
                         crs="EPSG:4326", transform=Affine(.01, 0, 116.9, 0, -.01, 31.7))
    static = _source(tmp_path, "geometry", {"incidence_angle": np.full((3, 4), 42.5, dtype="f4"),
                                             "ls_map": np.zeros((3, 4), dtype="u1")}, static=True)
    first["static_source"] = static
    geographic["static_source"] = static
    output = tmp_path / "out"
    config = {"variables": ["VV_dB"], "include_static": True}
    result, manifest = _run(config, _points(first), [first, geographic], output)
    assert result["status"] == "complete"
    table = _observations(output)
    assert len(table) == 12
    assert len(table[table.point_id == "a"]) == 3
    np.testing.assert_array_equal(sorted(table[table.point_id == "a"].value.to_numpy(dtype=float)), [7.125, 7.125, 11.5])
    static_table = pd.read_parquet(output / "static.parquet")
    assert len(static_table) == 8
    assert "time_index" not in static_table
    assert len(list((output / ".state" / "static_parts").glob("*.parquet"))) == 2
    original = table.sort_values(["point_id", "source_id"]).reset_index(drop=True)
    rerun, rerun_manifest = _run(config, _points(first), [first, geographic], output, manifest)
    assert rerun["status"] == "complete"
    assert len(rerun_manifest["completed"]) == 5
    pd.testing.assert_frame_equal(original, _observations(output).sort_values(["point_id", "source_id"]).reset_index(drop=True))
    pd.testing.assert_frame_equal(static_table, pd.read_parquet(output / "static.parquet"))


def test_resume_keeps_static_and_source_tables_identical_after_variable_reordering(tmp_path):
    source = _source(tmp_path, "dynamic", {"VV_dB": np.ones((2, 3, 4), dtype="f4"),
                                          "VH_dB": np.full((2, 3, 4), 2, dtype="f4")})
    static = _source(tmp_path, "static", {"ls_map": np.zeros((3, 4), dtype="u1"),
                                         "incidence_angle": np.full((3, 4), 42.5, dtype="f4")}, static=True)
    source["static_source"] = static
    config = {"include_static": True}
    output = tmp_path / "ordered-output"
    result, manifest = _run(config, _points(source), [source], output)
    assert result["status"] == "complete"
    paths = [output / name for name in ("static.parquet", "sources.parquet")]
    before = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), pd.read_parquet(path)) for path in paths}
    source["variables"] = dict(reversed(list(source["variables"].items())))
    static["variables"] = dict(reversed(list(static["variables"].items())))
    rerun, _ = _run(config, _points(source), [source], output, manifest)
    assert rerun["status"] == "complete"
    for path, (digest, frame) in before.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        pd.testing.assert_frame_equal(pd.read_parquet(path), frame)


def test_read_failure_preserves_other_sources_and_resume_replaces_failed_part(tmp_path):
    values = np.full((1, 3, 4), 3.25, dtype="f4")
    source = _source(tmp_path, "repair", {"VV_dB": values, "VH_dB": values + 10})
    group = zarr.open_group(source["path"], mode="a")
    del group["VH_dB"]
    output = tmp_path / "out"
    config = {"variables": ["VV_dB", "VH_dB"]}
    result, manifest = _run(config, _points(source), [source], output)
    assert result["status"] == "partial"
    assert result["failed_source_count"] == 1
    assert len(manifest["completed"]) == 1
    before = _observations(output)
    assert len(before[before.status == "read_failed"]) == 3
    assert len(before[before.status == "valid"]) == 3
    group.create_array("VH_dB", data=values + 10, chunks=(1, 2, 2), dimension_names=("time", "y", "x"))
    result, manifest = _run(config, _points(source), [source], output, manifest)
    after = _observations(output)
    assert result["status"] == "complete"
    assert len(manifest["completed"]) == 2
    assert len(list((output / "observations").glob("*.parquet"))) == 2
    assert len(after) == len(before)
    assert not (after.status == "read_failed").any()
    assert not after.duplicated(["point_id", "source_id"]).any()
    assert set(pd.read_parquet(output / "sources.parquet").source_status) == {"complete"}


def test_empty_source_selection_keeps_all_input_ids_and_export_schema(tmp_path):
    source = _source(tmp_path, "coordinates", {"VV_dB": np.ones((1, 3, 4), dtype="f4")})
    output = tmp_path / "empty"
    result, manifest = _run({"include_static": True, "output": {"export_csv": True}}, _points(source), [], output)
    assert result["status"] == "no_source"
    assert manifest["completed"] == []
    summary = pd.read_parquet(output / "point_summary.parquet")
    assert len(summary) == 4
    assert set(summary.status) == {"no_source"}
    assert len(pd.read_parquet(output / "static.parquet")) == 0
    assert len(pd.read_csv(output / "export.csv")) == 0
    assert pq.read_schema(output / "static.parquet").field("value").type == pa.float64()


def test_point_sources_keep_time_scoped_units_and_evidence(tmp_path):
    source = _source(tmp_path, "scoped", {"VV_dB": np.full((2, 3, 4), -10, dtype="f4")})
    source["variables"]["VV_dB"]["units"] = "unknown"
    source["times"][0].update(output_id="verified-month", generation_id=None,
                               variable_semantics={"VV_dB": {"units": "dB", "data_domain": "db",
                                                               "metadata_evidence": {"basis": "historical_code"}}})
    output = tmp_path / "scoped-out"
    result, _ = _run({"variables": ["VV_dB"]}, _points(source), [source], output)
    assert result["status"] == "complete"
    records = pd.read_parquet(output / "sources.parquet").sort_values("time_index")
    assert records.units.tolist() == ["dB", "unknown"]
    assert records.iloc[0].output_id == "verified-month"
    assert json.loads(records.iloc[0].metadata_evidence_json)["basis"] == "historical_code"
    assert set(_observations(output).query("status == 'valid'").value) == {-10}


def test_real_metadata_lineage_has_explicit_candidate_and_track_scopes(tmp_path):
    from test_spatial_sources import _store
    from s1grits.spatial_sources import describe_source

    mixed_path = _store(tmp_path, "mixed", times=["2024-01-01", "2024-02-01"], attrs={
        "track_members": [18, 19], "product_id": "store-product", "product_version": "v1",
        "production_time": "2024-03-01T00:00:00Z", "jpl_burst_ids": ["store-burst"],
        "opera_ids": ["store-opera"], "time_records": [{"time_index": 0,
            "burst_ids": [], "opera_ids": ["layer-opera"], "product_id": "layer-product",
            "product_version": "v2", "production_time": "2024-04-01T00:00:00Z"}],
    })
    single_path = _store(tmp_path, "single", attrs={"n_bursts": 15})
    unknown_path = _store(tmp_path, "unknown", attrs={"track": None, "geometry_group_id": None})
    mixed, single, unknown = [describe_source(path) for path in (mixed_path, single_path, unknown_path)]
    output = tmp_path / "lineage-out"
    result, _ = _run({"variables": ["VV_dB"]}, _points(mixed), [mixed, single, unknown], output)
    assert result["status"] == "complete"
    records = pd.read_parquet(output / "sources.parquet")
    assert set(records.provenance_level) == {"source_product"}
    layers = records[records.store_path == str(mixed_path.resolve())].sort_values("time_index")
    first, second = layers.iloc[0], layers.iloc[1]
    assert set(layers.track_mode) == {"mixed_tracks"} and layers.relative_orbit.isna().all()
    assert list(first.track_members) == [18, 19]
    assert list(first.burst_ids) == [] and first.burst_provenance_scope == "time_layer_candidates"
    assert list(first.opera_ids) == ["layer-opera"] and first.opera_provenance_scope == "time_layer_candidates"
    assert first.product_id == "layer-product" and first.product_version == "v2"
    assert first.production_time == "2024-04-01T00:00:00Z"
    assert list(second.burst_ids) == ["store-burst"] and second.burst_provenance_scope == "store_candidates"
    assert list(second.opera_ids) == ["store-opera"] and second.opera_provenance_scope == "store_candidates"
    assert second.product_id == "store-product" and second.product_version == "v1"
    assert second.production_time == "2024-03-01T00:00:00Z"
    one = records[records.store_path == str(single_path.resolve())].iloc[0]
    assert one.track_mode == "single_track" and one.relative_orbit == 18
    assert one.burst_ids is None and one.burst_provenance_scope == "unknown"
    assert one.opera_ids is None and one.opera_provenance_scope == "unknown"
    unidentified = records[records.store_path == str(unknown_path.resolve())].iloc[0]
    assert unidentified.track_mode == "unknown" and pd.isna(unidentified.relative_orbit)
    assert set(_observations(output).source_id) == set(records.source_id)
