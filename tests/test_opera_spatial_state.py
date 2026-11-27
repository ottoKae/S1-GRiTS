"""Contract stability and physical units for the independent OPERA archive."""
from copy import deepcopy

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point, box, mapping

from s1grits.opera_spatial.state import (
    prepare_state, persist, group_key, latest_products, physical_sample, job_budget,
    requested_static_layers,
)


def product(pid="p1", acquired="2025-01-01T00:00:00Z", burst="T084-178405-IW3"):
    return dict(product_id=pid, burst_id=burst, acquisition_id="S1A:100:T084",
                acquisition_identity="verified", acquisition_utc=acquired,
                platform="Sentinel-1A", direction="ASCENDING", relative_orbit=84,
                absolute_orbit=100, processing_version="1", product_version="1",
                processing_date="2025-01-02T00:00:00Z", geometry=mapping(box(111, 21, 113, 23)),
                assets={})


def features():
    return gpd.GeoDataFrame({"point_id": ["p"], "spatial_mode": ["points"]},
                           geometry=[Point(112.02, 22.25)], crs=4326)


def test_date_extension_preserves_registry_and_rejects_new_owner_candidates(tmp_path):
    config = {"spatial": {"mode": "points"}, "start": "2025-01-01", "end": "2025-02-01"}
    first = product()
    state = prepare_state(config, features(), [first], tmp_path)
    state["products"] = [first]
    persist(state, tmp_path)
    added = product("p2", "2026-01-01T00:00:00Z")
    added["acquisition_id"] = "S1A:200:T084"
    added["geometry"] = mapping(box(111.1, 21.1, 113.1, 23.1))
    updated = prepare_state({**config, "end": "2026-02-01", "workers": 8}, features(), [added], tmp_path)
    assert updated["contract_id"] == state["contract_id"]
    assert updated["groups"] == state["groups"]
    with pytest.raises(ValueError, match="candidate set"):
        prepare_state(config, features(), [product("p3", burst="T084-178404-IW3")], tmp_path)
    with pytest.raises(ValueError, match="contract differs"):
        prepare_state({**config, "source_strategy": "S1"}, features(), [first], tmp_path)


def test_new_track_is_an_independent_group_and_versions_do_not_duplicate():
    old = product()
    new = {**old, "product_id": "new", "processing_date": "2025-02-01"}
    other = {**old, "product_id": "other", "relative_orbit": 11}
    assert len(latest_products([old, new, other])) == 2
    assert group_key(old) == group_key(new) != group_key(other)
    assert old not in latest_products([old, new])


def test_actual_radiometry_and_scaling_once():
    source = product()
    tags = {"PROCESSING_INFORMATION_OUTPUT_BACKSCATTER_NORMALIZATION_CONVENTION": "gamma0",
            "PROCESSING_INFORMATION_OUTPUT_BACKSCATTER_EXPRESSION_CONVENTION": "linear backscatter intensity",
            "BURST_ID": source["burst_id"], "ABSOLUTE_ORBIT_NUMBER": "100"}
    sample = {"status": "valid", "value": .1, "scale": 2., "offset": 1., "tags": {"dataset": tags}}
    value = physical_sample(sample, "vv", source)
    assert value["value"] == 1.2 and value["units"] == "1"
    assert physical_sample(value, "vv", source)["value"] == 1.2
    arrays = physical_sample({**sample, "scale": 1, "offset": 0,
                              "values": np.array([[.1, 0, -1, np.inf]], dtype=np.float32)}, "vv", source)
    assert arrays["values"][0, 0] == np.float32(.1)
    assert np.isnan(arrays["values"][0, 1:]).all()
    bad = deepcopy(sample)
    bad["tags"]["dataset"]["BURST_ID"] = "wrong"
    with pytest.raises(ValueError, match="identity differ"):
        physical_sample(bad, "vv", source)
    with pytest.raises(ValueError, match="radiometry"):
        physical_sample({"status": "valid", "value": .1, "unit": "1"}, "vv", source)
    with pytest.raises(ValueError, match="Categorical"):
        physical_sample({"status": "valid", "value": 0, "scale": 1, "offset": 1}, "mask", source)


def test_static_configuration_is_opt_in_and_point_only():
    assert requested_static_layers({}) == ["lia"]
    layers = ["lia", "number_of_looks", "rtc_anf_beta0", "rtc_anf_sigma0"]
    config = {"spatial": {"mode": "points"}, "static": {"layers": layers,
              "reference_products": {"T084-178405-IW3": "static-product"}}}
    assert requested_static_layers(config) == layers
    with pytest.raises(ValueError, match="points only"):
        requested_static_layers({**config, "spatial": {"mode": "geographic_0p1"}})
    for invalid in ("lia", [], ["lia", "lia"], ["lia", "unknown"], ["lia", []]):
        with pytest.raises(ValueError, match="static.layers"):
            requested_static_layers({**config, "static": {"layers": invalid}})
    for invalid in (None, {"burst": ""}, {"burst": 3}, {3: "product"}):
        with pytest.raises(ValueError, match="reference_products"):
            requested_static_layers({**config, "static": {"reference_products": invalid}})
    with pytest.raises(ValueError, match="static must be a mapping"):
        requested_static_layers({"static": None})


@pytest.mark.parametrize("role,layer_name,domain", [
    ("number_of_looks", "Number of Looks", "sample_contribution_count"),
    ("rtc_anf_beta0", "RTC Area Normalization Factor Gamma0 to Beta0", "linear_ratio"),
    ("rtc_anf_sigma0", "RTC Area Normalization Factor Gamma0 to Sigma0", "linear_ratio"),
])
def test_static_physical_values_preserve_zero_fraction_and_scale_once(role, layer_name, domain):
    reference = {**product(), "is_static": True}
    sample = {"status": "valid", "value": .25, "scale": 2., "offset": .5,
              "tags": {"dataset": {"LAYER_NAME": layer_name, "PLATFORM": reference["platform"]}}}
    result = physical_sample(sample, role, reference)
    assert result["value"] == 1 and result["units"] == "1" and result["data_domain"] == domain
    assert result["scope"] == "static_nominal_burst_reference"
    assert physical_sample(result, role, reference)["value"] == 1
    if role.startswith("rtc_anf_"):
        assert result["conversion_from"] == "gamma0"
        assert result["conversion_to"] == role.removeprefix("rtc_anf_")
    for value in (0., .25, 1234.5):
        actual = physical_sample({**sample, "value": value, "scale": 1., "offset": 0.}, role, reference)
        assert actual["status"] == "valid" and actual["value"] == value
    negative = physical_sample({**sample, "value": -.5, "scale": 1., "offset": 0.}, role, reference)
    assert negative["status"] == "negative" and negative["value"] is None
    nonfinite = physical_sample({**sample, "value": np.inf}, role, reference)
    assert nonfinite["status"] == "nonfinite" and nonfinite["value"] is None
    array = physical_sample({**sample, "values": np.array([[0, .25, -1, np.inf]]),
                             "scale": 1., "offset": 0.}, role, reference)["values"]
    assert np.array_equal(array[0, :2], [0., .25]) and np.isnan(array[0, 2:]).all()
    with pytest.raises(ValueError, match="LAYER_NAME"):
        physical_sample({**sample, "tags": {"LAYER_NAME": "Local Incidence Angle"}}, role, reference)
    with pytest.raises(ValueError, match="PLATFORM"):
        physical_sample({**sample, "tags": {"PLATFORM": "Sentinel-1B"}}, role, reference)


def test_output_cannot_be_within_production_catalog(tmp_path):
    production = tmp_path / "production"
    production.mkdir()
    (production / "catalog.parquet").write_bytes(b"existing production")
    with pytest.raises(ValueError, match="production catalog"):
        prepare_state({"spatial": {"mode": "points"}}, features(), [product()], production / "new")
    assert not (production / "new").exists()


def test_budget_counts_full_requested_span_and_repair_staging(tmp_path):
    config = {"spatial": {"mode": "points"}, "operation": "repair"}
    state = prepare_state(config, features(), [product()], tmp_path)
    second = {**product("p2"), "acquisition_id": "S1A:200:T084"}
    budget = job_budget(state, [product(), second], config)
    assert budget["acquisition_group_count"] == 2
    assert budget["required_bytes_upper"] == 2 * budget["uncompressed_output_bytes"]
