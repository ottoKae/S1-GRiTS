"""OPERA discovery and stable asset identities, independent of MGRS workflows."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import urlparse


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def query_inventory(geometry, start, end, static=False):
    """Return a complete provider snapshot; dynamic dates are half-open UTC."""
    import asf_search as asf

    query = {"dataset": "OPERA-S1", "processingLevel": "RTC-STATIC" if static else "RTC",
             "intersectsWith": geometry.wkt}
    if not static:
        lower, upper = _utc(start), _utc(end)
        if lower >= upper:
            raise ValueError("Inventory start must precede end")
        query.update(start=lower.isoformat(), end=upper.isoformat())
    result = asf.search(**query, opts=asf.ASFSearchOptions(session=asf.ASFSession()))
    result.raise_if_incomplete()
    features = result.geojson()["features"]
    if not static:
        features = [f for f in features if lower <= _utc(f["properties"]["startTime"]) < upper]
    return {"query": query, "complete": True, "features": features, "count": len(features),
            "retrieved_utc": datetime.now(timezone.utc).isoformat(),
            "asf_search_version": asf.__version__}


def products_from_snapshot(snapshot, static_roles=("lia",)):
    """Normalize all provider product versions without discarding missing roles."""
    if snapshot.get("complete") is not True:
        raise ValueError("A complete ASF inventory snapshot is required")
    products = []
    seen = set()
    static_query = snapshot.get("query", {}).get("processingLevel") == "RTC-STATIC"
    for feature in snapshot["features"]:
        p = feature["properties"]
        pid = p["sceneName"]
        if pid in seen:
            raise ValueError(f"Duplicate product in inventory: {pid}")
        seen.add(pid)
        burst = p["operaBurstID"].upper().replace("_", "-")
        static = static_query or "RTC-S1-STATIC" in pid or p.get("processingLevel") == "RTC-STATIC"
        orbit, platform, direction, track = (p.get("orbit"), p.get("platform"),
                                             p.get("flightDirection"), p.get("pathNumber"))
        verified = bool(orbit and platform and direction and track) and not static
        acquisition = (f"{platform}:{orbit}:{direction}:T{int(track):03d}" if verified else
                       None if static else "unknown:" + pid)
        row = {"product_id": pid, "burst_id": burst, "is_static": static,
               "acquisition_id": acquisition,
               "acquisition_identity": "static" if static else "verified" if verified else "unknown",
               "acquisition_utc": None if static else p.get("startTime"),
               "platform": platform, "direction": direction,
               "relative_orbit": int(track) if track else None,
               "absolute_orbit": orbit,
               "processing_version": p.get("pgeVersion"), "product_version": p.get("productVersion"),
               "processing_date": p.get("processingDate") or "",
               "validity_start": p.get("validityStartDate"),
               "geometry": feature["geometry"],
               "identity_evidence": {"type": "provider platform + absolute physical orbit + direction + relative orbit",
                                     "source_group_id": p.get("groupID")},
               "assets": _assets(p, static_roles if static else {"vv", "vh", "mask"})}
        products.append(row)
    return sorted(products, key=lambda p: p["product_id"])


def _assets(p, roles):
    cloud_keys = {Path(urlparse(s).path).name: urlparse(s).path.lstrip("/")
                  for s in p.get("s3Urls", [])
                  if s.startswith("s3://asf-cumulus-prod-opera-products/")}
    assets = {}
    suffixes = {"_vv.tif": "vv", "_vh.tif": "vh", "_mask.tif": "mask",
                "_local_incidence_angle.tif": "lia", "_number_of_looks.tif": "number_of_looks",
                "_rtc_anf_gamma0_to_beta0.tif": "rtc_anf_beta0",
                "_rtc_anf_gamma0_to_sigma0.tif": "rtc_anf_sigma0"}
    for url in [p.get("url", "")] + list(p.get("additionalUrls") or []):
        filename = Path(urlparse(url).path).name
        role = next((v for k, v in suffixes.items() if filename.lower().endswith(k)), None)
        if role not in roles or not url.startswith("https://"):
            continue
        info = p.get("bytes", {}).get(filename, {})
        size = info.get("bytes") if isinstance(info, dict) else info
        if not isinstance(size, (int, float)) or not math.isfinite(size) or size <= 0:
            raise ValueError(f"No reliable asset size for {filename}")
        original = url
        if urlparse(url).hostname == "datapool.asf.alaska.edu" and filename in cloud_keys:
            url = "https://cumulus.asf.earthdatacloud.nasa.gov/OPERA/" + cloud_keys[filename]
        if role in assets and assets[role]["filename"] != filename:
            raise ValueError(f"Ambiguous {role} asset for {p['sceneName']}")
        assets[role] = {"asset_id": hashlib.sha256((p["sceneName"] + ":" + role).encode()).hexdigest()[:32],
                        "product_id": p["sceneName"], "role": role, "url": url,
                        "provider_url": original, "provider_s3_key": cloud_keys.get(filename),
                        "filename": filename, "expected_bytes": int(size)}
    return assets


def _utc(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
