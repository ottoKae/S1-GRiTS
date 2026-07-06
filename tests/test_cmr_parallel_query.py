"""Result-identity tests for parallel CMR subqueries (roadmap item 7).

The metadata query issues one asf.geo_search over the whole burst-ID list;
asf_search runs the per-burst subqueries sequentially. Item 7 splits the list
into chunks and runs geo_search per chunk concurrently. Because bursts are
independent and the downstream dedup + sort_values(['jpl_burst_id','acq_dt'])
is deterministic, the merged result must be BIT-IDENTICAL to the serial call.
These tests pin that invariant (including out-of-order chunk completion) with a
mocked geo_search — no network.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

import s1grits.asf_tiles as at  # noqa: E402

BURSTS = [
    "T018_038650_IW2", "T018_038651_IW2", "T018_038652_IW1",
    "T040_084852_IW1", "T040_084853_IW1", "T120_257741_IW1", "T142_303936_IW3",
]


class _FakeProd:
    def __init__(self, burst, t):
        scene = f"OPERA_L2_RTC-S1_{burst}_{t}"
        base = f"https://datapool.asf.alaska.edu/RTC/OPERA-S1/{scene}"
        # track number derived from burst prefix so results vary by burst
        track = int(burst.split("_")[0][1:])
        self.properties = {
            "sceneName": scene,
            "startTime": f"2026-01-{t[-2:]}T00:00:00Z",
            "pathNumber": track,
            "polarization": "VV+VH",
            "url": f"{base}_VV.tif",
            "additionalUrls": [f"{base}_VH.tif"],
        }

    def geojson(self):
        return {"geometry": {"type": "Point", "coordinates": [-79.5, -2.0]}}


def _make_geo_search(delay_last_chunk=False):
    seen = {"first": True}

    def fake_geo_search(operaBurstID=None, processingLevel=None, start=None, end=None):
        # Simulate out-of-order completion: make the FIRST-dispatched chunk slow
        # so a later chunk finishes first, stressing the merge ordering.
        if delay_last_chunk and seen["first"]:
            seen["first"] = False
            time.sleep(0.05)
        out = []
        for b in operaBurstID:
            for t in ("20260105", "20260117"):
                out.append(_FakeProd(b, t))
        return out

    return fake_geo_search


def _run(query_workers, geo_search):
    at.asf.geo_search = geo_search
    return at.get_rtc_s1_ts_metadata_by_burst_ids(
        BURSTS, "2026-01-01", "2026-01-31", "VV+VH", query_workers=query_workers
    )


@pytest.mark.parametrize("workers", [2, 3, 4, 8])
def test_parallel_matches_serial(workers):
    serial = _run(1, _make_geo_search())
    parallel = _run(workers, _make_geo_search())
    # Non-geometry columns bit-identical (order + values)
    a = serial.drop(columns="geometry").reset_index(drop=True)
    b = parallel.drop(columns="geometry").reset_index(drop=True)
    assert a.equals(b), f"workers={workers} changed the result set"
    # Geometry identical too
    assert (serial.geometry.values == parallel.geometry.values).all()


def test_out_of_order_completion_still_identical():
    serial = _run(1, _make_geo_search())
    parallel = _run(4, _make_geo_search(delay_last_chunk=True))
    a = serial.drop(columns="geometry").reset_index(drop=True)
    b = parallel.drop(columns="geometry").reset_index(drop=True)
    assert a.equals(b)


def test_chunk_list_exact_coverage():
    for n in (1, 2, 3, 4, 8, 16):
        chunks = at._chunk_list(BURSTS, n)
        flat = [x for c in chunks for x in c]
        assert flat == BURSTS               # order preserved, no drops/dupes
        assert len(chunks) <= max(1, min(n, len(BURSTS)))


def test_single_burst_not_chunked():
    assert at._chunk_list(["only"], 4) == [["only"]]
