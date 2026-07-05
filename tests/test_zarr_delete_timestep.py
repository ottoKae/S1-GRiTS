"""Unit test for _zarr_delete_timestep under zarr>=3.2 (roadmap item 1).

The helper previously read kept timesteps with ``g[v][keep_idx, :, :]`` where
``keep_idx`` is a Python list — rejected by zarr>=3.2 basic indexing with
"unsupported selection item for basic indexing". The fix reads the full band
and NumPy-fancy-indexes the kept timesteps. This test builds a small
multi-timestep store, deletes a middle month, and asserts the remaining
timesteps and band data are correct and correctly ordered.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import zarr

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

ow = pytest.importorskip("s1grits.asf_output_writing")


def _make_store(path, months):
    g = zarr.open_group(str(path), mode="w", zarr_format=3)
    h = w = 8
    g.create_array("x", data=np.arange(w, dtype=np.float64))
    g.create_array("y", data=np.arange(h, dtype=np.float64))
    times = np.array(
        [np.datetime64(f"{m}-01", "ns").astype("int64") for m in months],
        dtype="int64",
    )
    g.create_array("time", data=times, chunks=(64,))
    for band in ("VV_dB", "VH_dB"):
        arr = np.stack([
            np.full((h, w), float(i + 1), dtype=np.float32)
            for i in range(len(months))
        ], axis=0)
        g.create_array(band, data=arr, chunks=(1, h, w), fill_value=np.nan)
    return g


def test_delete_middle_timestep(tmp_path):
    months = ["2026-01", "2026-02", "2026-03"]
    g = _make_store(tmp_path / "s.zarr", months)

    ow._zarr_delete_timestep(g, "2026-02")

    remaining = pd.to_datetime(np.asarray(g["time"][:]).astype("datetime64[ns]"))
    assert list(remaining.strftime("%Y-%m")) == ["2026-01", "2026-03"]
    # Band values: month i had constant value i+1; after dropping month 2 (=2.0)
    # the kept planes are 1.0 (Jan) and 3.0 (Mar), in order.
    vv = np.asarray(g["VV_dB"][:])
    assert vv.shape[0] == 2
    assert np.allclose(vv[0], 1.0)
    assert np.allclose(vv[1], 3.0)


def test_delete_absent_month_is_noop(tmp_path):
    months = ["2026-01", "2026-02"]
    g = _make_store(tmp_path / "s2.zarr", months)
    ow._zarr_delete_timestep(g, "2026-09")  # not present
    assert g["time"].shape[0] == 2
    assert g["VV_dB"].shape[0] == 2


def test_delete_all_but_one(tmp_path):
    months = ["2026-01", "2026-02", "2026-03"]
    g = _make_store(tmp_path / "s3.zarr", months)
    ow._zarr_delete_timestep(g, "2026-01")
    ow._zarr_delete_timestep(g, "2026-03")
    remaining = pd.to_datetime(np.asarray(g["time"][:]).astype("datetime64[ns]"))
    assert list(remaining.strftime("%Y-%m")) == ["2026-02"]
    assert np.allclose(np.asarray(g["VV_dB"][:])[0], 2.0)
