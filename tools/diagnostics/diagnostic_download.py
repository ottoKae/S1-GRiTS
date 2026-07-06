"""
Diagnostic script to identify why downloads are failing.

Run this to check:
1. Network connectivity to ASF
2. Authentication status
3. Metadata query capability
4. Download capability
"""

import sys
sys.path.insert(0, 'src')

import asf_search as asf
import requests
from datetime import datetime

print("=" * 60)
print("S1-GRiTS Download Diagnostic")
print("=" * 60)

# Test 1: Network connectivity
print("\n[1/5] Testing ASF CMR connectivity...")
try:
    response = requests.get("https://cmr.earthdata.nasa.gov/search/health", timeout=10)
    if response.status_code == 200:
        print("✅ CMR API is reachable")
    else:
        print(f"⚠️  CMR returned status {response.status_code}")
except Exception as e:
    print(f"❌ Cannot reach CMR: {e}")

# Test 2: ASF Search library
print("\n[2/5] Testing ASF Search library...")
try:
    import asf_search
    print(f"✅ asf_search version: {asf_search.__version__}")
except Exception as e:
    print(f"❌ ASF Search import failed: {e}")

# Test 3: Authentication check
print("\n[3/5] Checking authentication...")
try:
    session = asf.ASFSession()
    if session.auth:
        print("✅ Authenticated session available")
    else:
        print("⚠️  No authentication - some data may be restricted")
except Exception as e:
    print(f"⚠️  Authentication check failed: {e}")

# Test 4: Metadata query test
print("\n[4/5] Testing metadata query for 17MQV, 2026-01...")
try:
    results = asf.search(
        platform=[asf.PLATFORM.SENTINEL1],
        processingLevel='RTC_GAMMA',
        intersectsWith='POLYGON((-96.5 40.0, -95.5 40.0, -95.5 41.0, -96.5 41.0, -96.5 40.0))',
        start=datetime(2026, 1, 1),
        end=datetime(2026, 1, 31),
        maxResults=5
    )
    print(f"✅ Query returned {len(results)} results (showing first 5)")
    if results:
        for i, r in enumerate(results[:3]):
            print(f"   {i+1}. {r.properties['fileID']} ({r.properties['sceneName']})")
    else:
        print("⚠️  No results found - check date range or tile location")
except Exception as e:
    print(f"❌ Metadata query failed: {e}")
    import traceback
    traceback.print_exc()

# Test 5: Download capability (check only, no actual download)
print("\n[5/5] Testing download access...")
try:
    if results:
        test_url = results[0].properties.get('url')
        if test_url:
            # Just check if URL is accessible (HEAD request)
            response = requests.head(test_url, timeout=10, allow_redirects=True)
            if response.status_code in [200, 302]:
                print(f"✅ Download URLs are accessible")
            else:
                print(f"⚠️  Download returned status {response.status_code}")
                print(f"   URL: {test_url}")
        else:
            print("⚠️  No download URL in metadata")
    else:
        print("⚠️  Skipped (no results from query)")
except Exception as e:
    print(f"⚠️  Download check failed: {e}")

print("\n" + "=" * 60)
print("Diagnostic complete")
print("=" * 60)

# Summary
print("\n📋 Summary:")
print("If all tests passed, the download infrastructure is working.")
print("If any failed, that's likely why prof_vv/prof_vh are empty.")
print("\nCommon issues:")
print("  - No authentication → restricted data inaccessible")
print("  - Wrong date range → no results for 2026-01")
print("  - Network firewall → CMR unreachable")
print("  - ASF server issue → metadata query fails")
