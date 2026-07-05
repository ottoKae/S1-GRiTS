from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import Affine

from s1grits import workflow_scenes as ws
from s1grits import workflow_static as wstatic
from s1grits.runtime_limits import (
    RuntimeLimits,
    apply_runtime_limits,
    rasterio_env_kwargs,
    runtime_limits_from_config,
)


RUNTIME_ENV_KEYS = [
    "GDAL_CACHEMAX",
    "GDAL_NUM_THREADS",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
]


def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in RUNTIME_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_runtime_limits_defaults_are_conservative():
    limits = runtime_limits_from_config({})

    assert limits.enabled is True
    assert limits.gdal_cachemax_mb == 512
    assert limits.gdal_num_threads == 1
    assert limits.env()["GDAL_CACHEMAX"] == "512"
    assert limits.env()["OMP_NUM_THREADS"] == "1"
    assert limits.env()["BLIS_NUM_THREADS"] == "1"


def test_apply_runtime_limits_sets_process_environment(monkeypatch):
    _clear_runtime_env(monkeypatch)

    env = apply_runtime_limits(
        {
            "runtime": {
                "gdal_cachemax_mb": 256,
                "gdal_num_threads": 2,
                "omp_num_threads": 3,
                "openblas_num_threads": 4,
                "mkl_num_threads": 5,
                "blis_num_threads": 6,
                "veclib_maximum_threads": 7,
                "numexpr_num_threads": 8,
            }
        }
    )

    assert env["GDAL_CACHEMAX"] == "256"
    assert os.environ["GDAL_NUM_THREADS"] == "2"
    assert os.environ["OMP_NUM_THREADS"] == "3"
    assert os.environ["OPENBLAS_NUM_THREADS"] == "4"
    assert os.environ["MKL_NUM_THREADS"] == "5"
    assert os.environ["BLIS_NUM_THREADS"] == "6"
    assert os.environ["VECLIB_MAXIMUM_THREADS"] == "7"
    assert os.environ["NUMEXPR_NUM_THREADS"] == "8"
    assert rasterio_env_kwargs() == {"GDAL_CACHEMAX": 256, "GDAL_NUM_THREADS": "2"}


def test_runtime_limits_disabled_does_not_apply_defaults(monkeypatch):
    _clear_runtime_env(monkeypatch)

    env = apply_runtime_limits({"runtime": {"enabled": False}})

    assert env == {}
    assert rasterio_env_kwargs() == {}
    assert "GDAL_CACHEMAX" not in os.environ
    assert "OMP_NUM_THREADS" not in os.environ


def test_worker_initializer_applies_limits_before_task_work(monkeypatch):
    _clear_runtime_env(monkeypatch)
    limits = RuntimeLimits(gdal_cachemax_mb=128, gdal_num_threads=1, omp_num_threads=2)

    ws._init_scenes_worker(limits)

    assert os.environ["GDAL_CACHEMAX"] == "128"
    assert os.environ["GDAL_NUM_THREADS"] == "1"
    assert os.environ["OMP_NUM_THREADS"] == "2"


def test_static_cog_writer_uses_rasterio_env(tmp_path, monkeypatch):
    _clear_runtime_env(monkeypatch)
    apply_runtime_limits({"runtime": {"gdal_cachemax_mb": 384, "gdal_num_threads": 1}})
    seen: dict[str, object] = {}

    class FakeEnv:
        def __init__(self, **kwargs):
            seen["env_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeDataset:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write(self, data):
            seen["write_shape"] = data.shape

    def fake_open(path, mode, **profile):
        seen["open_mode"] = mode
        seen["profile"] = profile
        Path(path).touch()
        return FakeDataset()

    monkeypatch.setattr(wstatic.rasterio, "Env", FakeEnv)
    monkeypatch.setattr(wstatic.rasterio, "open", fake_open)

    wstatic._build_static_cog(
        np.ones((2, 3), dtype=np.float32),
        Affine.identity(),
        "EPSG:32617",
        str(tmp_path / "static.tif"),
        cog_block=2,
    )

    assert seen["env_kwargs"] == {"GDAL_CACHEMAX": 384, "GDAL_NUM_THREADS": "1"}
    assert seen["open_mode"] == "w"
    assert seen["write_shape"] == (1, 2, 3)


@pytest.mark.parametrize(
    "config",
    [
        {"runtime": {"gdal_cachemax_mb": 0}},
        {"runtime": {"gdal_num_threads": "many"}},
        {"runtime": {"enabled": "maybe"}},
        {"runtime": []},
    ],
)
def test_runtime_limits_reject_invalid_config(config):
    with pytest.raises(ValueError):
        runtime_limits_from_config(config)
