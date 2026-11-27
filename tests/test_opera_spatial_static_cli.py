"""CLI routing exercises real point archives and HTTP COGs."""
import json

import pytest
import yaml

from test_opera_spatial_points import inputs, product
from test_opera_spatial_points import endpoint as endpoint
from test_opera_spatial_point_static import ROLES, dynamic_hashes, static_product
from s1grits.opera_spatial import __main__ as cli
from s1grits.opera_spatial.points import run_points
from s1grits.opera_spatial.point_static import read_point_static_context, run_static


def arguments(tmp_path, config, operation, *options):
    (tmp_path / "points.csv").write_text("id,lon,lat\npoint,0,0\n", encoding="utf-8")
    config = {**config, "input": {"path": "points.csv", "id_column": "id"},
              "output": {"base_dir": "archive"}}
    path = tmp_path / "points.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return [operation, "--config", str(path), "--start", "2025-01-01", "--end", "2026-01-01", *options]


def read_result(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_static_only_uses_frozen_products_without_discovery(tmp_path, endpoint, monkeypatch, capsys):
    config, features = inputs()
    output = tmp_path / "archive"
    run_points(config, features, [product(endpoint, "A", "e1", "2025-01-01")], output)
    config["static"] = {"layers": ROLES}
    run_static(config, features, output, static_products=[static_product(endpoint)], repair=True)
    before = dynamic_hashes(output)
    calls = len(endpoint["calls"])

    def no_query(*args, **kwargs):
        pytest.fail("Frozen static-only repair must not query dynamic or static catalogs")

    monkeypatch.setattr(cli, "query_inventory", no_query)
    assert cli.main(arguments(tmp_path, config, "repair", "--static-only")) == 0
    result = read_result(capsys)
    assert result["status"] == "complete" and result["dynamic_http_requests"] == 0
    assert result["http_requests"] == 0 and len(endpoint["calls"]) == calls
    assert dynamic_hashes(output) == before
    assert read_point_static_context(output).iloc[0].selected_number_of_looks == 12.5
    changed = {**config, "static": {"layers": ["lia", "number_of_looks"]}}
    with pytest.raises(SystemExit) as error:
        cli.main(arguments(tmp_path, changed, "update"))
    assert error.value.code == 2
    replaced = {**config, "static": {"layers": ROLES, "reference_products": {"A": "another-product"}}}
    with pytest.raises(ValueError, match="Cannot replace a frozen STATIC"):
        cli.main(arguments(tmp_path, replaced, "repair", "--static-only"))
    assert dynamic_hashes(output) == before


def test_old_archive_requires_explicit_static_only_before_discovery(tmp_path, endpoint, monkeypatch):
    config, features = inputs()
    output = tmp_path / "archive"
    run_points(config, features, [product(endpoint, "A", "e1", "2025-01-01")], output)
    before = dynamic_hashes(output)
    config["static"] = {"layers": ROLES}

    def no_query(*args, **kwargs):
        pytest.fail("Migration rejection must happen before catalog discovery")

    monkeypatch.setattr(cli, "query_inventory", no_query)
    with pytest.raises(SystemExit) as error:
        cli.main(arguments(tmp_path, config, "update"))
    assert error.value.code == 2 and dynamic_hashes(output) == before


@pytest.mark.parametrize("missing_reference", [False, True])
def test_static_repair_discovers_new_roles_or_first_reference_only(tmp_path, endpoint, monkeypatch, capsys,
                                                                missing_reference):
    config, features = inputs()
    output = tmp_path / "archive"
    run_points(config, features, [product(endpoint, "A", "e1", "2025-01-01")], output)
    reference = static_product(endpoint)
    if missing_reference:
        config["static"] = {"layers": ROLES}
        run_static(config, features, output, static_products=[], repair=True)
    else:
        config["static"] = {"layers": ["lia"]}
        limited = {**reference, "assets": {"lia": reference["assets"]["lia"]}}
        run_static(config, features, output, static_products=[limited], repair=True)
    before = dynamic_hashes(output)
    config["static"] = {"layers": ROLES, "reference_products": {"A": reference["product_id"]}}
    queries = []

    def query_static(geometry, start, end, static=False):
        queries.append((start, end, static))
        assert static is True and start is None and end is None
        return {"products": [reference]}

    monkeypatch.setattr(cli, "query_inventory", query_static)
    monkeypatch.setattr(cli, "products_from_snapshot", lambda snapshot, static_roles=("lia",): snapshot["products"])
    calls = len(endpoint["calls"])
    assert cli.main(arguments(tmp_path, config, "repair", "--static-only")) == 0
    result = read_result(capsys)
    assert queries == [(None, None, True)]
    assert result["status"] == "complete" and result["dynamic_http_requests"] == 0
    assert all(path.startswith("/STATIC_") for path in endpoint["calls"][calls:])
    row = read_point_static_context(output).iloc[0]
    assert row.selected_number_of_looks == 12.5 and row.selected_rtc_anf_beta0 == 0
    assert row.selected_rtc_anf_sigma0 == .75 and dynamic_hashes(output) == before
    assert cli.main(arguments(tmp_path, config, "repair", "--static-only")) == 0
    repeated = read_result(capsys)
    assert len(queries) == 1 and repeated["http_requests"] == 0


def test_new_point_update_automatically_attaches_static_after_dynamic_commit(tmp_path, endpoint, monkeypatch, capsys):
    config, _ = inputs()
    config["static"] = {"layers": ROLES}
    dynamic = product(endpoint, "A", "e1", "2025-01-01")
    static = static_product(endpoint)
    # Provider parsing has separate tests; this boundary supplies normalized local HTTP assets.
    monkeypatch.setattr(cli, "products_from_snapshot", lambda snapshot, static_roles=("lia",): snapshot["products"])
    inventory, auxiliary = tmp_path / "dynamic.json", tmp_path / "static.json"
    inventory.write_text(json.dumps({"products": [dynamic]}), encoding="utf-8")
    auxiliary.write_text(json.dumps({"products": [static]}), encoding="utf-8")
    options = ["--inventory", str(inventory), "--static-inventory", str(auxiliary)]
    calls = len(endpoint["calls"])
    assert cli.main(arguments(tmp_path, config, "plan", *options)) == 0
    planned = read_result(capsys)
    assert planned["static"]["unique_assets"] == 4
    assert len(endpoint["calls"]) == calls and not (tmp_path / "archive/manifest.json").exists()

    assert cli.main(arguments(tmp_path, config, "update", *options)) == 0
    result = read_result(capsys)
    assert result["dynamic_status"] == result["static_status"] == "complete"
    assert result["legacy_result"]["selected_rows"] == 1
    assert read_point_static_context(tmp_path / "archive").iloc[0].selected_number_of_looks == 12.5
    calls = len(endpoint["calls"])
    assert cli.main(arguments(tmp_path, config, "update", *options)) == 0
    repeated = read_result(capsys)
    assert repeated["static"]["http_requests"] == 0 and len(endpoint["calls"]) == calls


def test_new_group_static_reference_requires_unique_or_explicit_product(tmp_path, endpoint, monkeypatch, capsys):
    config, _ = inputs()
    config["static"] = {"layers": ROLES}
    dynamic = product(endpoint, "A", "e1", "2025-01-01")
    first, second = static_product(endpoint), static_product(endpoint, revision=2)
    monkeypatch.setattr(cli, "products_from_snapshot", lambda snapshot, static_roles=("lia",): snapshot["products"])
    inventory, auxiliary = tmp_path / "dynamic.json", tmp_path / "static.json"
    inventory.write_text(json.dumps({"products": [dynamic]}), encoding="utf-8")
    auxiliary.write_text(json.dumps({"products": [first, second]}), encoding="utf-8")
    options = ["--inventory", str(inventory), "--static-inventory", str(auxiliary)]
    with pytest.raises(ValueError, match="Multiple STATIC products"):
        cli.main(arguments(tmp_path, config, "plan", *options))
    assert not (tmp_path / "archive/manifest.json").exists()
    config["static"]["reference_products"] = {"A": first["product_id"]}
    assert cli.main(arguments(tmp_path, config, "update", *options)) == 0
    read_result(capsys)
    state = json.loads((tmp_path / "archive/manifest.json").read_text(encoding="utf-8"))
    assert next(iter(state["groups"].values()))["sources"][0]["static_product_id"] == first["product_id"]
