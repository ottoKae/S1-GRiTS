"""Small real stores exercise read-only historical provenance boundaries."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import zarr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from s1grits.provenance_inventory import collect_inventory
from s1grits.provenance_contract import validate_records


def _files(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def _s1(root, *, static=False):
    relative = "18NUF/static.zarr" if static else "18NUF/monthly.zarr"
    group = zarr.open_group(str(root / relative), mode="w", zarr_format=3)
    group.attrs.update({"product_type": "static" if static else "smonthly",
                        "grid_id": "grid-30", "geometry_group_id": "18NUF_ASCENDING_TK120"})
    if static:
        group.create_array("local_inc_angle", data=np.ones((3, 4), dtype="f4"))
    else:
        group.create_array("time", data=np.array(["2025-01-16T10:00", "2025-02-12T11:00", "2025-03-14T10:00"], dtype="datetime64[ns]"))
        group.create_array("VV_dB", data=np.arange(36, dtype="f4").reshape(3, 3, 4), chunks=(1, 3, 4))
    return relative, group


def _s2(root, *, quality=False):
    relative = "18NUF/quality.zarr" if quality else "18NUF/native.zarr"
    group = zarr.open_group(str(root / relative), mode="w", zarr_format=3)
    group.attrs.update({"product_type": "scenes", "collection": "sentinel-2-c1-l2a", "tile_id": "18NUF"})
    stamps = ["2025-01-22T15:32:00Z", "2025-04-07T15:32:01Z", "2025-06-11T15:32:02Z"]
    times = np.array([s[:-1] for s in stamps], dtype="datetime64[ns]")
    group.create_array("time", data=times)
    rows = []
    names = [("20m", "scl", "scl", [2, 0, 1])] if quality else [("10m", "blue", "blue", [0, 1, 2]), ("20m", "red_edge_1", "rededge1", [1, 2, 0])]
    for name, variable, asset_key, order in names:
        child = group.create_group(name)
        child.attrs.update({"grid_id": "grid-" + name, "product_type": "scenes"})
        child.create_array("time", data=times[order])
        a = child.create_array(variable, data=np.ones((3, 3, 4), dtype="u1" if quality else "u2"), chunks=(1, 3, 4))
        a.attrs.update({"source_asset": asset_key, "scale": 0.0001, "offset": -0.1, "nodata": 0})
        for i in range(3):
            rows.append({"zarr_path": relative, "zarr_group": name, "item_id": "scene-" + str(i),
                         "datetime": stamps[i], "grid_id": "grid-" + name,
                         "source_collection": "sentinel-2-c1-l2a",
                         "source_assets_json": json.dumps({asset_key: {"href": f"https://example.com/scene-{i}/{asset_key}.tif?versionId=public-v1&token=secret",
                                 "raster:bands": [{"scale": 0.0001, "offset": -0.1}]}})})
    pq.write_table(pa.Table.from_pylist(rows), root / "catalog.parquet")
    items = [{"item_id": "scene-" + str(i), "datetime": stamps[i], "tile_id": "18NUF"} for i in range(3)]
    (root / relative / "s2grits_resume_state.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    return relative, group


@pytest.mark.parametrize("layout", ["root_relative", "tile_relative", "tile_windows", "legacy_absolute", "legacy_windows"])
def test_catalog_store_paths_keep_tile_identity_across_supported_layouts(tmp_path, layout):
    from s1grits.provenance_inventory import _catalog_rows
    tiles = ("18NUF", "18NUH")
    relative = "smonthly_ASCENDING/zarr/shared.zarr"
    selected = [tile + "/" + relative for tile in tiles]
    rows = []
    for tile, store in zip(tiles, selected):
        candidate = store
        if layout.startswith("tile_"):
            candidate = relative
        elif layout.startswith("legacy_"):
            candidate = "/old/archive/" + store if layout == "legacy_absolute" else "D:/old/archive/" + store
        if layout.endswith("windows"):
            candidate = candidate.replace("/", chr(92))
        rows.append({"tile_id": tile, "zarr_path": candidate})
    pq.write_table(pa.Table.from_pylist(rows), tmp_path / "catalog.parquet")
    before = _files(tmp_path)
    witnesses = {}
    matched = _catalog_rows(tmp_path, selected, witnesses)
    assert {row["tile_id"]: row["_relative_store"] for row in matched} == dict(zip(tiles, selected))
    assert witnesses["catalog.parquet"] == before["catalog.parquet"]
    assert _files(tmp_path) == before


def test_catalog_tile_relative_path_does_not_match_another_tile_or_unscoped_suffix(tmp_path):
    from s1grits.provenance_inventory import _catalog_rows
    relative = "smonthly_ASCENDING/zarr/shared.zarr"
    rows = [{"tile_id": "18NUH", "zarr_path": relative},
            {"tile_id": None, "zarr_path": relative}]
    pq.write_table(pa.Table.from_pylist(rows), tmp_path / "catalog.parquet")
    assert _catalog_rows(tmp_path, ["18NUF/" + relative], {}) == []


def test_s1_month_identity_survives_reorder_and_midmonth_change(tmp_path):
    relative, group = _s1(tmp_path)
    first = collect_inventory(tmp_path, store_paths=[relative])
    before = _files(tmp_path)
    again = collect_inventory(tmp_path, store_paths=[relative])
    assert first == again and before == _files(tmp_path)
    assert first["outputs"][0]["period_start"] == "2025-01-01"
    assert first["outputs"][0]["period_end"] == "2025-01-31"
    assert all(o["generation_id"] is None and o["run_id"] is None for o in first["outputs"])
    assert first["variables"][0]["stored_physical_quantity"] is None
    assert not first["sources"] and not first["source_links"]
    group["time"][:] = np.array(["2025-03-21", "2025-01-23", "2025-02-11"], dtype="datetime64[ns]")
    second = collect_inventory(tmp_path, store_paths=[relative])
    assert {o["period_start"]: o["output_id"] for o in first["outputs"]} == {o["period_start"]: o["output_id"] for o in second["outputs"]}
    assert first["witnesses"] != second["witnesses"]
    validate_records(first["outputs"], first["sources"], first["source_links"])


def test_static_has_no_fabricated_time_or_execution(tmp_path):
    relative, _ = _s1(tmp_path, static=True)
    inv = collect_inventory(tmp_path, store_paths=[relative], start="2025-01-01", end="2025-12-31")
    row = inv["outputs"][0]
    assert row["record_kind"] == "static" and row["time_index"] is None
    assert row["time_value_utc"] is None and row["source_item_id"] is None
    validate_records(inv["outputs"], [], [])


def test_three_group_indices_are_independent_and_source_scaling_is_declaration(tmp_path):
    native_root = tmp_path / "S2"
    native_root.mkdir()
    relative, _ = _s2(native_root)
    quality_root = tmp_path / "quality"
    quality_root.mkdir()
    quality, _ = _s2(quality_root, quality=True)
    native = collect_inventory(native_root, store_paths=[relative])
    scl = collect_inventory(quality_root, store_paths=[quality])
    indices = [o["time_index"] for o in native["outputs"] + scl["outputs"] if o["source_item_id"] == "scene-0"]
    assert indices == [0, 2, 1]
    assert len(native["sources"]) == 6 and len(native["source_links"]) == 6
    assert all(s["declared_scale"] == 0.0001 and s["declared_offset"] == -0.1 for s in native["sources"])
    assert all("token" not in s["href"] and "versionId=public-v1" in s["href"] for s in native["sources"])
    assert all(v["applied_scale"] is None for v in native["variables"])
    assert {link["role"] for link in native["source_links"]} == {"selected"}
    assert all(o["evidence_ref"].removeprefix("evidence/") in native["evidence"] for o in native["outputs"])
    validate_records(native["outputs"], native["sources"], native["source_links"])


def test_never_reads_pixels_and_excludes_companion_version(tmp_path, monkeypatch):
    relative, _ = _s2(tmp_path)
    original = zarr.Array.__getitem__
    def read(array, key):
        if array.ndim >= 2:
            raise AssertionError("Inventory read pixel data")
        return original(array, key)
    monkeypatch.setattr(zarr.Array, "__getitem__", read)
    first = collect_inventory(tmp_path, store_paths=[relative])
    (tmp_path / "companion.json").write_text('{"version":2}')
    second = collect_inventory(tmp_path, store_paths=[relative])
    assert first == second


def test_pixel_only_replacement_is_explicitly_outside_historical_witness_scope(tmp_path):
    relative, group = _s1(tmp_path)
    first = collect_inventory(tmp_path, store_paths=[relative])
    group["VV_dB"][1] = np.zeros((3, 4), dtype="f4")
    second = collect_inventory(tmp_path, store_paths=[relative])
    assert first["witnesses"] == second["witnesses"]
    assert "cannot detect pixel-only replacement" in second["limitations"][0]["revision_scope"]


@pytest.mark.parametrize("change", ["duplicate_time", "missing_commit", "wrong_catalog_grid", "wrong_catalog_item"])
def test_rejects_ambiguous_or_conflicting_scene_binding(tmp_path, change):
    relative, group = _s2(tmp_path)
    if change == "duplicate_time":
        group["10m/time"][1] = group["10m/time"][0]
    elif change == "missing_commit":
        p = tmp_path / relative / "s2grits_resume_state.json"
        data = json.loads(p.read_text()); data["items"].pop(); p.write_text(json.dumps(data))
    else:
        p = tmp_path / "catalog.parquet"
        rows = pq.read_table(p).to_pylist()
        rows[0]["grid_id" if change == "wrong_catalog_grid" else "item_id"] = "wrong"
        pq.write_table(pa.Table.from_pylist(rows), p)
    with pytest.raises(ValueError):
        collect_inventory(tmp_path, store_paths=[relative])


def test_only_requested_date_range_is_returned(tmp_path):
    relative, _ = _s2(tmp_path)
    inv = collect_inventory(tmp_path, store_paths=[relative], start="2025-04-07", end="2025-04-07")
    assert len(inv["outputs"]) == 2
    assert {o["source_item_id"] for o in inv["outputs"]} == {"scene-1"}


@pytest.mark.parametrize("selection", [[], ["../escape"], ["C:/private"], ["/absolute"]])
def test_rejects_unbounded_or_escaping_selection(tmp_path, selection):
    with pytest.raises(ValueError):
        collect_inventory(tmp_path, store_paths=selection)


def test_append_adds_only_new_logical_layer(tmp_path):
    relative, group = _s1(tmp_path)
    first = collect_inventory(tmp_path, store_paths=[relative])
    group["time"].resize((4,)); group["time"][3] = np.datetime64("2025-04-22", "ns")
    group["VV_dB"].resize((4, 3, 4)); group["VV_dB"][3] = np.ones((3, 4), dtype="f4")
    second = collect_inventory(tmp_path, store_paths=[relative])
    assert [o["output_id"] for o in first["outputs"]] == [o["output_id"] for o in second["outputs"][:3]]
    assert len(second["outputs"]) == 4


def test_spatial_coordinate_edit_changes_witness(tmp_path):
    relative, group = _s1(tmp_path)
    group.create_array("x", data=np.arange(4, dtype="f8"))
    group.create_array("y", data=np.arange(3, dtype="f8"))
    first = collect_inventory(tmp_path, store_paths=[relative])
    group["x"][:] = np.arange(4, dtype="f8") + 30
    second = collect_inventory(tmp_path, store_paths=[relative])
    assert first["witnesses"] != second["witnesses"]


def test_pending_write_blocks_inventory_but_unknown_history_does_not(tmp_path):
    relative, _ = _s1(tmp_path)
    assert collect_inventory(tmp_path, store_paths=[relative])["outputs"]
    pending = tmp_path / "provenance" / "pending"
    pending.mkdir(parents=True)
    (pending / "write-1.json").write_text('{"state":"prepared"}')
    with pytest.raises(ValueError, match="pending"):
        collect_inventory(tmp_path, store_paths=[relative])


def test_same_source_url_with_changed_declared_checksum_has_new_identity(tmp_path):
    relative, _ = _s2(tmp_path)
    catalog = tmp_path / "catalog.parquet"
    rows = pq.read_table(catalog).to_pylist()
    assets = json.loads(rows[0]["source_assets_json"])
    assets["blue"]["sha256"] = "a" * 64
    rows[0]["source_assets_json"] = json.dumps(assets)
    pq.write_table(pa.Table.from_pylist(rows), catalog)
    first = collect_inventory(tmp_path, store_paths=[relative])
    assets["blue"]["sha256"] = "b" * 64
    rows[0]["source_assets_json"] = json.dumps(assets)
    pq.write_table(pa.Table.from_pylist(rows), catalog)
    second = collect_inventory(tmp_path, store_paths=[relative])
    a = next(s for s in first["sources"] if s["product_id"] == "scene-0" and s["asset_key"] == "blue")
    b = next(s for s in second["sources"] if s["product_id"] == "scene-0" and s["asset_key"] == "blue")
    assert a["href"] == b["href"] and a["source_id"] != b["source_id"]
    assert a["evidence_status"] == b["evidence_status"] == "source_declared"


def _run_pending(root, run_id="run-one", **updates):
    pending = root / "provenance/pending"
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / (run_id + ".json")
    path.write_text(json.dumps({"run_id": run_id, "phase": "writing", **updates}), encoding="utf-8")
    return path


def test_run_inventory_observes_only_own_pending_without_removing_it(tmp_path):
    from s1grits.provenance_inventory import _collect_inventory_for_run
    relative, _ = _s1(tmp_path)
    expected = collect_inventory(tmp_path, store_paths=[relative])
    pending = _run_pending(tmp_path)
    before = _files(tmp_path)
    observed = _collect_inventory_for_run(tmp_path, store_paths=[relative], run_id="run-one")
    assert observed == expected and pending.exists()
    assert _files(tmp_path) == before
    assert all(row["generation_id"] is None for row in observed["outputs"])
    with pytest.raises(ValueError, match="pending"):
        collect_inventory(tmp_path, store_paths=[relative])


@pytest.mark.parametrize("problem", ["missing", "other", "wrong_payload", "invalid_id"])
def test_run_inventory_rejects_missing_or_unrelated_pending(tmp_path, problem):
    from s1grits.provenance_inventory import _collect_inventory_for_run
    relative, _ = _s1(tmp_path)
    run_id = "run-one"
    if problem != "missing":
        path = _run_pending(tmp_path)
        if problem == "other":
            _run_pending(tmp_path, "run-two")
        elif problem == "wrong_payload":
            path.write_text('{"run_id":"run-two"}', encoding="utf-8")
        else:
            run_id = "../run-one"
    with pytest.raises(ValueError, match="pending|Pending|run_id"):
        _collect_inventory_for_run(tmp_path, store_paths=[relative], run_id=run_id)


@pytest.mark.parametrize("change", ["other", "modify", "remove"])
def test_run_inventory_rechecks_pending_after_observation(tmp_path, monkeypatch, change):
    from s1grits import provenance_inventory as inventory
    relative, _ = _s1(tmp_path)
    pending = _run_pending(tmp_path)
    groups = inventory._groups
    def changed(group):
        yield from groups(group)
        if change == "other":
            _run_pending(tmp_path, "run-two")
        elif change == "modify":
            _run_pending(tmp_path, phase="committed_provenance_pending")
        else:
            pending.unlink()
    monkeypatch.setattr(inventory, "_groups", changed)
    with pytest.raises(ValueError, match="pending|Pending"):
        inventory._collect_inventory_for_run(tmp_path, store_paths=[relative], run_id="run-one")
