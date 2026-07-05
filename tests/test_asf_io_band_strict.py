from __future__ import annotations

import numpy as np
import pandas as pd
from rasterio.transform import Affine
import threading
import time

from s1grits import asf_io


def test_load_rtc_band_strict_returns_source_indices(monkeypatch):
    df = pd.DataFrame(
        {
            "url_copol": ["vv-a", "vv-b", "vv-c"],
            "url_crosspol": ["vh-a", "vh-b", "vh-c"],
            "acq_datetime": pd.to_datetime(
                [
                    "2020-01-01T00:00:00Z",
                    "2020-01-02T00:00:00Z",
                    "2020-01-03T00:00:00Z",
                ],
                utc=True,
            ),
        }
    )
    prof = {"transform": Affine.identity(), "crs": "EPSG:32617", "nodata": np.nan}

    def fake_download(urls, label, dates, max_workers, retry_timeout_seconds, scene_max_retries):
        assert label == "copol"
        assert urls == ["vv-a", "vv-b", "vv-c"]
        return (
            [np.ones((2, 2), dtype=np.float32), None, np.full((2, 2), 3, dtype=np.float32)],
            [prof, None, prof],
            [None, "not_found", None],
        )

    monkeypatch.setattr(asf_io, "_download_with_retry", fake_download)

    arrs, profs, dates, source_indices = asf_io.load_rtc_band_strict(
        df,
        band="copol",
        max_workers=1,
        scene_max_retries=1,
        max_failed_ratio=0.0,
    )

    assert len(arrs) == 2
    assert profs == [prof, prof]
    assert [d.strftime("%Y-%m-%d") for d in pd.to_datetime(dates)] == [
        "2020-01-01",
        "2020-01-03",
    ]
    assert source_indices == [0, 2]


def test_load_and_despeckle_rtc_strict_uses_shared_download_pool(monkeypatch):
    df = pd.DataFrame(
        {
            "url_copol": ["vv-a", "vv-b", "vv-c"],
            "url_crosspol": ["vh-a", "vh-b", "vh-c"],
            "acq_datetime": pd.to_datetime(
                [
                    "2020-01-01T00:00:00Z",
                    "2020-01-02T00:00:00Z",
                    "2020-01-03T00:00:00Z",
                ],
                utc=True,
            ),
        }
    )
    prof = {"transform": Affine.identity(), "crs": "EPSG:32617", "nodata": np.nan}
    active = 0
    max_active = 0
    seen = []
    lock = threading.Lock()

    def fake_read_one_asf(url, retry_timeout_seconds=600.0):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            seen.append(url)

        time.sleep(0.02)

        with lock:
            active -= 1

        value = 1.0 if url.startswith("vv-") else 2.0
        return np.full((2, 2), value, dtype=np.float32), prof, None

    monkeypatch.setattr(asf_io, "read_one_asf", fake_read_one_asf)

    final_vv, prof_vv, final_vh, prof_vh, dates = asf_io.load_and_despeckle_rtc_strict(
        df,
        max_workers=2,
        scene_max_retries=1,
        max_failed_ratio=0.0,
    )

    assert max_active <= 2
    assert set(seen) == {"vv-a", "vv-b", "vv-c", "vh-a", "vh-b", "vh-c"}
    assert all(np.all(a == 1.0) for a in final_vv)
    assert all(np.all(a == 2.0) for a in final_vh)
    assert prof_vv == [prof, prof, prof]
    assert prof_vh == [prof, prof, prof]
    assert len(dates) == 3
