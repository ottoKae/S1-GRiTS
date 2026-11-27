"""Provider snapshots retain product versions and trustworthy asset addresses."""
from copy import deepcopy

import pytest

from s1grits.opera_spatial.inventory import digest, products_from_snapshot


def feature(pid="product-a", static=False):
    filename = pid + ("_local_incidence_angle.tif" if static else "_VV.tif")
    return {"geometry": {"type": "Polygon", "coordinates": [[[112, 22], [113, 22], [113, 23], [112, 22]]]},
            "properties": {"sceneName": pid, "operaBurstID": "t084_178405_iw3", "orbit": 12345,
                           "platform": "Sentinel-1A", "flightDirection": "ASCENDING", "pathNumber": 84,
                           "startTime": "2025-01-01T01:00:00Z", "pgeVersion": "1.0", "productVersion": "v1",
                           "processingDate": "2025-01-02T00:00:00Z", "validityStartDate": "2016-04-01",
                           "url": "https://datapool.asf.alaska.edu/RTC/" + filename,
                           "s3Urls": ["s3://asf-cumulus-prod-opera-products/granule/" + filename],
                           "bytes": {filename: {"bytes": 123456}}}}


def test_preserve_versions_missing_roles_and_stable_asset_identity():
    raw = {"complete": True, "features": [feature("product-a"), feature("product-b")]}
    products = products_from_snapshot(raw)
    assert len(products) == 2
    assert products[0]["acquisition_id"] == products[1]["acquisition_id"]
    assert products[0]["burst_id"] == "T084-178405-IW3"
    assert set(products[0]["assets"]) == {"vv"}
    asset = products[0]["assets"]["vv"]
    assert asset["url"] == "https://cumulus.asf.earthdatacloud.nasa.gov/OPERA/granule/product-a_VV.tif"
    repeated = products_from_snapshot(deepcopy(raw))
    assert repeated[0]["assets"]["vv"]["asset_id"] == asset["asset_id"]
    assert products[0]["assets"]["vv"]["asset_id"] != products[1]["assets"]["vv"]["asset_id"]


def test_static_is_not_a_physical_acquisition():
    snapshot = {"complete": True, "query": {"processingLevel": "RTC-STATIC"}, "features": [feature(static=True)]}
    product = products_from_snapshot(snapshot)[0]
    assert product["is_static"] and product["acquisition_id"] is None
    assert product["acquisition_utc"] is None and set(product["assets"]) == {"lia"}


def test_optional_static_assets_keep_default_and_product_versions():
    raw = feature("OPERA_L2_RTC-S1-STATIC_T084-178405-IW3_20140403_S1A_30_v1.0", static=True)
    props = raw["properties"]
    props.update(productVersion=None, pgeVersion="2.1.0", additionalUrls=[])
    names = [props["sceneName"] + suffix for suffix in (
        "_number_of_looks.tif", "_rtc_anf_gamma0_to_beta0.tif", "_rtc_anf_gamma0_to_sigma0.TIF")]
    for name in names:
        props["additionalUrls"].append("https://datapool.asf.alaska.edu/RTC/" + name)
        props["bytes"][name] = {"bytes": 456789}
    snapshot = {"complete": True, "features": [raw]}
    assert set(products_from_snapshot(snapshot)[0]["assets"]) == {"lia"}
    roles = ["lia", "number_of_looks", "rtc_anf_beta0", "rtc_anf_sigma0"]
    product = products_from_snapshot(snapshot, static_roles=roles)[0]
    assert set(product["assets"]) == set(roles)
    assert len({a["asset_id"] for a in product["assets"].values()}) == 4
    assert product["product_version"] is None and product["processing_version"] == "2.1.0"
    assert product["acquisition_id"] is None
    assert product == products_from_snapshot(deepcopy(snapshot), static_roles=roles)[0]
    del props["bytes"][names[0]]
    assert set(products_from_snapshot(snapshot)[0]["assets"]) == {"lia"}
    with pytest.raises(ValueError, match="reliable asset size"):
        products_from_snapshot(snapshot, static_roles=roles)


def test_unverified_acquisition_is_explicit_and_input_failure_is_not_silent():
    raw = feature()
    raw["properties"].pop("orbit")
    assert products_from_snapshot({"complete": True, "features": [raw]})[0]["acquisition_identity"] == "unknown"
    with pytest.raises(ValueError, match="complete"):
        products_from_snapshot({"complete": False, "features": []})
    with pytest.raises(ValueError, match="Duplicate"):
        products_from_snapshot({"complete": True, "features": [raw, raw]})
    raw["properties"]["bytes"] = {}
    with pytest.raises(ValueError, match="reliable asset size"):
        products_from_snapshot({"complete": True, "features": [raw]})
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
