"""Read real Catalog/Zarr metadata without changing the legacy workflow."""
import json

import numpy as np
import pandas as pd
import pytest
import zarr
from affine import Affine
from shapely.geometry import box, mapping, shape

from s1grits.spatial_sources import close_sources, describe_source, discover_local_sources, read_source_window


@pytest.fixture(autouse=True)
def _close_task_sources():
    yield
    close_sources()


def _store(root, name, *, version=3, attrs=None, times=None, values=None, dims=None, variable_attrs=None):
    metadata = {"product_type": "scenes", "crs": "EPSG:4326",
                "transform": [.1, 0, 10, 0, -.1, 20], "flight_direction": "ASCENDING",
                "track": 18, "geometry_group_id": "test_ASCENDING_TK18", "processing_signature": "p1"}
    metadata.update(attrs or {})
    static = metadata["product_type"] == "static"
    times = ["2024-01-15T00:00:00"] if times is None else times
    dims = (["y", "x"] if static else ["time", "y", "x"]) if dims is None else dims
    if values is None:
        values = np.ones((2, 3) if static else (len(times), 2, 3), dtype="f4")
    path = root / f"{name}.zarr"
    group = zarr.open_group(path, mode="w", zarr_format=version)
    group.attrs.update(metadata)
    sizes = dict(zip(dims, values.shape))
    affine = Affine(*metadata["transform"])
    for dimension, data in [("x", affine.c + (np.arange(sizes["x"]) + .5) * affine.a),
                            ("y", affine.f + (np.arange(sizes["y"]) + .5) * affine.e)]:
        options = {"dimension_names": [dimension]} if version == 3 else {}
        array = group.create_array(dimension, data=data, **options)
        array.attrs["_ARRAY_DIMENSIONS"] = [dimension]
    if not static:
        options = {"dimension_names": ["time"]} if version == 3 else {}
        array = group.create_array("time", data=np.asarray(times, dtype="datetime64[ns]").astype("i8"), **options)
        array.attrs.update({"units": "nanoseconds since 1970-01-01", "_ARRAY_DIMENSIONS": ["time"]})
    options = {"dimension_names": dims} if version == 3 else {}
    array = group.create_array("VV_dB", data=values, chunks=tuple(min(2, n) for n in values.shape), **options)
    array.attrs.update({"_ARRAY_DIMENSIONS": dims, **(variable_attrs or {})})
    return path


def _catalog(root, paths, **columns):
    rows = [{"zarr_path": str(path.relative_to(root)), "status": "complete", **columns} for path in paths]
    pd.DataFrame(rows).to_parquet(root / "catalog.parquet", index=False)


def _config(root, **source_options):
    return {"source": {"root": str(root), "product_type": "scenes", **source_options}}


def test_filters_keep_mixed_tracks_and_duplicate_time_indices(tmp_path):
    dates = ["2024-01-15", "2024-02-15", "2024-02-15"]
    mixed = _store(tmp_path, "mixed", times=dates,
                   attrs={"track_members": [], "track_token": "18_19", "track": 18,
                          "time_records": [{"time_index": 1, "acquisition_id": "S1A:101", "platform": "S1A"},
                                           {"time_index": 2, "acquisition_id": "S1B:202", "platform": "S1B"}]})
    descending = _store(tmp_path, "descending", times=dates, attrs={"flight_direction": "DESCENDING"})
    _catalog(tmp_path, [mixed, descending], datetime=pd.NaT, tile_id=None)
    config = _config(tmp_path)
    config.update(selection={"flight_directions": ["ASCENDING"], "track_numbers": [19]},
                  time={"years": [2024], "months": [2]})
    sources = discover_local_sources(config)
    assert len(sources) == 1
    source = sources[0]
    assert source["track"] is None
    assert source["track_members"] == [18, 19]
    assert [r["time_index"] for r in source["times"]] == [1, 2]
    assert source["times"][0]["time_utc"] == source["times"][1]["time_utc"]
    assert [r["platform"] for r in source["times"]] == ["S1A", "S1B"]
    assert "NaT" not in json.dumps(source, allow_nan=False)
    config["time"]["years"] = [2025]
    assert discover_local_sources(config) == []


def test_declared_product_metadata_keeps_store_and_time_scopes(tmp_path):
    path = _store(tmp_path, "declared", times=["2024-01-01", "2024-02-01", "2024-03-01"], attrs={
        "product_id": "store-product", "product_version": "v1", "production_time": "2024-04-01T00:00:00Z",
        "jpl_burst_ids": ["store-burst"], "opera_ids": ["store-opera"],
        "time_records": [
            {"time_index": 0, "product_id": "layer-product", "product_version": "v2",
             "production_time": "2024-05-01T00:00:00Z", "burst_ids": [], "opera_ids": ["layer-opera"]},
            {"time_index": 1, "jpl_burst_ids": ["layer-burst"]},
        ],
    })
    source = discover_local_sources(_config(path))[0]
    assert source["product_id"] == "store-product" and source["product_version"] == "v1"
    assert source["production_time"] == "2024-04-01T00:00:00Z"
    assert source["burst_ids"] == ["store-burst"] and source["opera_ids"] == ["store-opera"]
    first, second, third = source["times"]
    assert first["product_id"] == "layer-product" and first["product_version"] == "v2"
    assert first["production_time"] == "2024-05-01T00:00:00Z"
    assert first["burst_ids"] == [] and first["opera_ids"] == ["layer-opera"]
    assert second["burst_ids"] == ["layer-burst"]
    assert not {"burst_ids", "opera_ids", "product_id", "product_version", "production_time"} & third.keys()


def test_store_burst_count_and_catalog_software_fields_are_not_product_lineage(tmp_path):
    path = _store(tmp_path, "OPERA_fake_burst", attrs={"n_bursts": 15})
    _catalog(tmp_path, [path], jpl_burst_ids=None, opera_ids=None, item_id="catalog-row",
             software_version="3.0.0", created_at="2024-01-01T00:00:00Z")
    source = discover_local_sources(_config(tmp_path))[0]
    assert source["attrs"]["n_bursts"] == 15
    assert not {"burst_ids", "opera_ids", "product_id", "product_version", "production_time"} & source.keys()


@pytest.mark.parametrize("version", [2, 3])
def test_v2_v3_dimensions_raw_values_and_declared_encoding(tmp_path, version):
    values = np.array([[[0, -9999, 1.25], [2, 3, 4]]], dtype="f4")
    path = _store(tmp_path, f"v{version}", version=version, values=values,
                  variable_attrs={"missing_value": -9999, "scale_factor": 2., "add_offset": 100.})
    source = discover_local_sources(_config(path))[0]
    meta = source["variables"]["VV_dB"]
    assert meta["units"] == "unknown"
    assert meta["data_domain"] == "unknown"
    assert meta["declared_scale"] == 2
    assert meta["declared_offset"] == 100
    assert meta["scale_offset_applied"] is False
    result = read_source_window(source, "VV_dB", 0, (0, 0, 2, 3))
    np.testing.assert_array_equal(result, [[0, np.nan, 1.25], [2, 3, 4]])
    assert result.dtype == np.dtype("f4")


def test_unannotated_zero_fill_is_not_scientific_nodata_and_axis_order_is_honored(tmp_path):
    values = np.arange(12, dtype="f4").reshape(3, 2, 2)
    path = _store(tmp_path, "permuted", times=["2024-01-01", "2024-01-02"], values=values,
                  dims=["x", "time", "y"], variable_attrs={"units": "dB"})
    source = describe_source(path)
    assert source["variables"]["VV_dB"]["nodata"] is None
    assert source["variables"]["VV_dB"]["data_domain"] == "db"
    np.testing.assert_array_equal(read_source_window(source, "VV_dB", 0, (0, 0, 2, 3)), values[:, 0, :].T)
    np.testing.assert_array_equal(read_source_window(source, "VV_dB", 1, (0, 0, 2, 3)), values[:, 1, :].T)


def test_processing_variants_require_selection_but_same_semantics_can_span_stores(tmp_path):
    first = _store(tmp_path, "raw", attrs={"processing_signature": "raw"})
    second = _store(tmp_path, "filtered", attrs={"processing_signature": "tv"})
    _catalog(tmp_path, [first, second])
    with pytest.raises(ValueError, match="Multiple processing variants"):
        discover_local_sources(_config(tmp_path))
    selected = discover_local_sources(_config(tmp_path, processing_signature="tv"))
    assert [s["path"] for s in selected] == [str(second.resolve())]
    close_sources()
    for path in [first, second]:
        zarr.open_group(path, mode="a").attrs["processing_variant_json"] = json.dumps({"processing.spatial_despeckle": False})
    assert len(discover_local_sources(_config(tmp_path))) == 2


def test_static_pairing_requires_matching_grid_and_unambiguous_identity(tmp_path):
    dynamic = _store(tmp_path, "dynamic")
    static = _store(tmp_path, "static", attrs={"product_type": "static"})
    shifted = _store(tmp_path, "shifted", attrs={"product_type": "static", "transform": [.1, 0, 10.05, 0, -.1, 20]})
    _catalog(tmp_path, [dynamic, static, shifted])
    config = _config(tmp_path)
    config["include_static"] = True
    source = discover_local_sources(config)[0]
    assert source["static_status"] == "matched"
    assert source["static_source"]["path"] == str(static.resolve())
    _catalog(tmp_path, [dynamic, shifted])
    assert discover_local_sources(config)[0]["static_status"] == "missing"
    duplicate = _store(tmp_path, "duplicate", attrs={"product_type": "static"})
    _catalog(tmp_path, [dynamic, static, duplicate])
    source = discover_local_sources(config)[0]
    assert source["static_status"] == "ambiguous"
    assert source["static_source"] is None
    subset = _store(tmp_path, "subset", values=np.ones((1, 2), dtype="f4"),
                    attrs={"product_type": "static", "transform": [.1, 0, 10.1, 0, -.1, 20]})
    _catalog(tmp_path, [dynamic, subset])
    source = discover_local_sources(config)[0]
    assert source["static_status"] == "matched"
    assert source["static_source"]["width"] == 2


def test_store_root_catalog_root_nominal_geometry_and_month_boundaries(tmp_path):
    nominal = mapping(box(10.05, 19.85, 10.25, 19.95))
    path = _store(tmp_path, "month", times=["2024-02-15"],
                  attrs={"product_type": "smonthly", "nominal_geometry": nominal})
    config = {"source": {"root": str(path), "product_type": "smonthly"}}
    direct = discover_local_sources(config)[0]
    _catalog(tmp_path, [path])
    config["source"]["root"] = str(tmp_path)
    catalogued = discover_local_sources(config, geometry=box(10, 19.8, 10.3, 20))[0]
    assert direct["source_unit_id"] == catalogued["source_unit_id"]
    assert shape(catalogued["nominal_geometry"]).equals(shape(nominal))
    assert catalogued["nominal_geometry"] != catalogued["coverage_geometry"]
    assert catalogued["times"][0]["period_start"] == "2024-02-01T00:00:00+00:00"
    assert catalogued["times"][0]["period_end"] == "2024-03-01T00:00:00+00:00"
    unannotated = _store(tmp_path, "unknown_geometry")
    assert describe_source(unannotated)["nominal_geometry"] is None
    assert discover_local_sources(config, geometry=box(-60, -30, -59, -29)) == []


def test_catalog_missing_values_are_serializable_and_unknown_identity_stays_unknown(tmp_path):
    path = _store(tmp_path, "unknown", attrs={"track": None, "geometry_group_id": None,
                                              "flight_direction": None})
    record = {"track": np.nan, "datetime": pd.NaT, "data_revision": pd.NA,
              "production_time": pd.NaT, "flight_direction": None}
    source = describe_source(path, record)
    assert source["track_members"] == []
    assert source["direction"] is None
    assert source["data_revision"] is None
    assert source["times"][0]["acquisition_id"] is None
    json.dumps(source, allow_nan=False)
