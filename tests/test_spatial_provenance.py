"""Exercise the adapter against verified companions and real Zarr arrays."""
from __future__ import annotations

import builtins
from copy import deepcopy
import hashlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import zarr

from s1grits import provenance as publisher
from s1grits.file_lock import output_lock
from s1grits.spatial_provenance import enrich_sources
from s1grits.spatial_sources import close_sources, describe_source, read_source_window


STORE = "fixture/monthly.zarr"
HISTORY = "evidence/history/processing.json"
HISTORY_BODY = {
    "schema": "s1grits-historical-monthly-domain-v1",
    "basis": "historical_runtime_revision_and_rendered_configs",
    "output_units": "dB", "output_domain": "db", "aggregation_domain": "linear_power",
    "aggregation_method": "nanmedian", "physical_quantity": "unknown",
    "n_obs_definition": "Finite VV burst-array contributions; not unique acquisitions.",
    "statement_extends_to_future_batches": False,
}


@pytest.fixture(autouse=True)
def _close_groups():
    yield
    close_sources()


def _source(root, store=STORE):
    return describe_source(root / store, root=root, record={"nominal_geometry": {
        "type": "Polygon", "coordinates": [[[10, 19], [11, 19], [11, 20], [10, 20], [10, 19]]]},
    })


def _dataset(tmp_path, *, stores=(STORE,), annotated_indices=(0, 1), history=None, variable_attrs=None):
    root, metadata = tmp_path / "dataset", tmp_path / "metadata"
    for store in stores:
        group = zarr.open_group(root / store, mode="w", zarr_format=3)
        group.attrs.update(product_type="smonthly", crs="EPSG:4326", transform=[.1, 0, 10, 0, -.1, 20],
                           flight_direction="ASCENDING", track=18, geometry_group_id="fixture_ASCENDING_TK18")
        for name, values in (("x", [10.05, 10.15]), ("y", [19.95]),
                             ("time", np.array(["2024-01-15", "2024-02-15"], dtype="datetime64[ns]"))):
            group.create_array(name, data=np.asarray(values), dimension_names=[name])
        for name in ("VV_dB", "VH_dB", "n_obs"):
            array = group.create_array(name, data=np.ones((2, 1, 2), dtype="u1" if name == "n_obs" else "f4"),
                                       dimension_names=["time", "y", "x"], chunks=(1, 1, 2))
            if name != "n_obs":
                array.attrs.update(scale_factor=2.0, add_offset=10.0)
                array.attrs.update(variable_attrs or {})
    inventory = publisher.collect_inventory(root, store_paths=list(stores))
    scope = {row["output_id"]: row["generation_id"] for row in inventory["outputs"]
             if row["store_path"] == STORE and row["time_index"] in annotated_indices}
    if scope:
        inventory["evidence"][HISTORY.removeprefix("evidence/")] = dict(HISTORY_BODY if history is None else history)
        for record in inventory["evidence"].values():
            if record.get("store_path") == STORE:
                record["historical_context"] = {"historical_processing_ref": HISTORY, "output_generations": scope}
        for record in inventory["variables"]:
            if record["store_path"] == STORE and record["variable"] in ("VV_dB", "VH_dB"):
                record.update(historical_processing_ref=HISTORY, historical_output_generations=scope,
                              historical_processing_basis="retained_runtime_and_config")
                if history is None:
                    record["units"] = "dB"
    with output_lock(metadata):
        pointer = publisher._publish_records_locked(metadata, dataset_id="spatial-test", inventory=inventory,
                                                    selection={"store_paths": list(stores)})
    config = {"source": {"root": str(root / STORE), "dataset_root": str(root), "metadata_root": str(metadata)},
              "spatial": {"mode": "points"}, "variables": None}
    return root, metadata, config, pointer, inventory


def test_scoped_history_preserves_raw_values_and_binds_release(tmp_path):
    root, metadata, config, pointer, _ = _dataset(tmp_path)
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    source = _source(root)
    view = enrich_sources(config, [source])
    assert view.available and not source["metadata_time_conflict"]
    assert source["data_revision"] == pointer["data_revision"]
    assert source["data_domain"] == "db" and source["aggregation_domain"] == "linear_power"
    assert source["aggregation_method"] == "nanmedian"
    assert source["n_obs_definition"] == HISTORY_BODY["n_obs_definition"]
    assert source["variables"]["n_obs"]["units"] == "unknown"
    assert source["variables"]["VV_dB"]["declared_scale"] == 2
    assert source["variables"]["VV_dB"]["scale_offset_applied"] is False
    np.testing.assert_array_equal(read_source_window(source, "VV_dB", 0, (0, 0, 1, 2)), [[1, 1]])
    for time in source["times"]:
        assert time["output_id"] and time["generation_id"] is None
        semantics = time["variable_semantics"]["VV_dB"]
        evidence = semantics["metadata_evidence"]
        assert evidence["basis"] == "historical_annotation"
        assert evidence["metadata_release_id"] == pointer["metadata_release_id"]
        assert evidence["historical"][0]["reference"] == HISTORY
        assert evidence["historical"][0]["output_generations"] == {time["output_id"]: None}
        assert semantics["physical_quantity"] == "unknown"
    assert "no fresh image chunk checksum" in source["metadata_evidence"]["provenance"]["verification_scope"]
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


def test_scope_does_not_spread_to_unannotated_month_or_store(tmp_path):
    other = "fixture/other.zarr"
    root, _, config, _, _ = _dataset(tmp_path, stores=(STORE, other), annotated_indices=(0,))
    first, second = _source(root), _source(root, other)
    enrich_sources(config, [first, second])
    assert first["metadata_time_conflict"]
    assert first["data_domain"] == "unknown"
    assert first["variables"]["VV_dB"]["units"] == "unknown"
    january, february = first["times"]
    assert january["variable_semantics"]["VV_dB"]["data_domain"] == "db"
    assert february["variable_semantics"]["VV_dB"]["data_domain"] == "unknown"
    assert february["variable_semantics"]["VV_dB"]["metadata_evidence"]["basis"] == "artifact_observed"
    assert not second["metadata_time_conflict"] and second["data_domain"] == "unknown"
    first = _source(root)
    first["times"] = first["times"][:1]
    enrich_sources(config, [first])
    assert not first["metadata_time_conflict"] and first["data_domain"] == "db"


def test_free_text_and_variable_names_do_not_establish_domains(tmp_path):
    root, _, config, _, _ = _dataset(tmp_path, history={"basis": "historical_note", "text": "VV_dB is dB after median in linear power"})
    source = _source(root)
    enrich_sources(config, [source])
    assert source["variables"]["VV_dB"]["units"] == "unknown"
    assert source["data_domain"] == "unknown" and source["aggregation_domain"] == "unknown"
    assert source["n_obs_definition"] == "unknown"


@pytest.mark.parametrize("declaration", [{"units": "amplitude"}, {"data_domain": "amplitude"}])
def test_known_source_declarations_cannot_be_overridden(tmp_path, declaration):
    root, _, config, _, _ = _dataset(tmp_path, variable_attrs=declaration)
    with pytest.raises(ValueError, match="Source declaration conflicts"):
        enrich_sources(config, [_source(root)])


def test_db_aliases_do_not_create_a_time_conflict(tmp_path):
    root, _, config, _, _ = _dataset(tmp_path, annotated_indices=(0,), variable_attrs={"units": "db"})
    source = _source(root)
    enrich_sources(config, [source])
    assert source["data_domain"] == "db"
    assert not any(conflict["field"] in ("units", "data_domain") for conflict in source["metadata_time_conflicts"])


def test_actual_writer_receipt_binds_generation_and_count_semantics(tmp_path):
    from test_provenance_monthly import _write, _store

    root = tmp_path / "tracked"
    _write(root)
    source = _source(root, _store(root).relative_to(root))
    config = {"source": {"root": str(root)}, "spatial": {"mode": "point_buffer"}, "variables": ["VV_dB"]}
    view = enrich_sources(config, [source])
    assert source["times"][0]["generation_id"]
    semantics = source["times"][0]["variable_semantics"]
    assert set(semantics) >= {"VV_dB", "VH_dB", "n_obs"}
    assert semantics["VV_dB"]["metadata_evidence"]["basis"] == "execution_receipt"
    assert semantics["n_obs"]["units"] == "aligned_scene_observations"
    assert semantics["n_obs"]["data_domain"] == "count"
    assert source["data_domain"] == "db" and source["aggregation_domain"] == "linear_power"
    assert source["aggregation_method"] == "nanmedian"
    assert not source["metadata_time_conflict"]
    view.assert_current(stores=[source["path"]])


def test_returned_view_detects_release_change(tmp_path):
    root, metadata, config, _, inventory = _dataset(tmp_path)
    source = _source(root)
    view = enrich_sources(config, [source])
    with output_lock(metadata):
        publisher._publish_records_locked(metadata, dataset_id="spatial-test", inventory=inventory,
                                          selection={"store_paths": [STORE]}, note="new metadata release")
    with pytest.raises(Exception, match="version changed"):
        view.assert_current(stores=[source["path"]])


def test_points_respects_requested_variables(tmp_path):
    root, _, config, _, _ = _dataset(tmp_path)
    source = _source(root)
    config["variables"] = ["VV_dB", "absent_variable"]
    enrich_sources(config, [source])
    assert set(source["times"][0]["variable_semantics"]) == {"VV_dB"}
    assert source["variables"]["VH_dB"]["units"] == "unknown"


@pytest.mark.parametrize("mode", ["points", "point_buffer"])
def test_variable_insertion_order_does_not_change_enrichment_or_plan_id(tmp_path, monkeypatch, mode):
    from s1grits import spatial

    root, _, config, _, _ = _dataset(tmp_path, variable_attrs={"scale_factor": 1, "add_offset": 0})
    source = _source(root)
    source["semantic_signature"] = "fixture-monthly-processing"
    orders = iter([sorted(source["variables"]), sorted(source["variables"], reverse=True)])
    selected = []

    def reordered_sources(*args, **kwargs):
        current = deepcopy(source)
        current["variables"] = {name: current["variables"][name] for name in next(orders)}
        selected.append(current)
        return [current]

    path = tmp_path / "points.csv"
    path.write_text("id,lon,lat\np1,10.05,19.95\n", encoding="utf-8")
    config.update(spatial={"mode": mode, "radius_m": 100},
                  input={"path": str(path), "id_column": "id"},
                  grid={"mode": "aeqd", "target_resolution": 30},
                  output={"base_dir": str(tmp_path / "output")})
    # Control enumeration order only; source metadata, release binding and planning are real.
    monkeypatch.setattr(spatial, "discover_local_sources", reordered_sources)
    first = spatial.plan_spatial(config)
    second = spatial.plan_spatial(config)
    assert list(selected[0]["variables"]) != list(selected[1]["variables"])
    assert selected[0] == selected[1]
    assert first["plan_id"] == second["plan_id"]


def test_no_companion_does_not_import_resolver(tmp_path, monkeypatch):
    native_import = builtins.__import__

    def forbid(name, *args, **kwargs):
        if name == "grits_resolver":
            raise AssertionError("A legacy dataset should not import the optional resolver")
        return native_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid)
    assert enrich_sources({"source": {"root": str(tmp_path)}}, []) is None


def test_old_or_absent_resolver_is_a_readable_error(tmp_path, monkeypatch):
    config = {"source": {"root": str(tmp_path), "metadata_root": str(tmp_path)}}
    monkeypatch.setitem(sys.modules, "grits_resolver", SimpleNamespace(__version__="0.1.0"))
    with pytest.raises(ValueError, match="incompatible"):
        enrich_sources(config, [])
    native_import = builtins.__import__

    def absent(name, *args, **kwargs):
        if name == "grits_resolver":
            raise ModuleNotFoundError("No module named grits_resolver", name="grits_resolver")
        return native_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", absent)
    with pytest.raises(ValueError, match="unavailable"):
        enrich_sources(config, [])


def test_store_root_requires_explicit_dataset_root_for_companion(tmp_path):
    root, _, config, _, _ = _dataset(tmp_path)
    config["source"].pop("dataset_root")
    with pytest.raises(ValueError, match="source.dataset_root"):
        enrich_sources(config, [_source(root)])
