"""
Runtime resource limits for worker processes.

The scenes workflow can combine process-level parallelism, threaded downloads,
GDAL/rasterio decoding, and NumPy/BLAS kernels.  This module centralizes the
small set of environment variables that keep each worker's cache and thread
fan-out bounded.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeLimits:
    enabled: bool = True
    gdal_cachemax_mb: int = 512
    gdal_num_threads: int = 1
    omp_num_threads: int = 1
    openblas_num_threads: int = 1
    mkl_num_threads: int = 1
    blis_num_threads: int = 1
    veclib_maximum_threads: int = 1
    numexpr_num_threads: int = 1

    def env(self) -> dict[str, str]:
        if not self.enabled:
            return {}
        return {
            "GDAL_CACHEMAX": str(self.gdal_cachemax_mb),
            "GDAL_NUM_THREADS": str(self.gdal_num_threads),
            "OMP_NUM_THREADS": str(self.omp_num_threads),
            "OPENBLAS_NUM_THREADS": str(self.openblas_num_threads),
            "MKL_NUM_THREADS": str(self.mkl_num_threads),
            "BLIS_NUM_THREADS": str(self.blis_num_threads),
            "VECLIB_MAXIMUM_THREADS": str(self.veclib_maximum_threads),
            "NUMEXPR_NUM_THREADS": str(self.numexpr_num_threads),
        }

    def rasterio_env(self) -> dict[str, int | str]:
        if not self.enabled:
            return {}
        return {
            "GDAL_CACHEMAX": self.gdal_cachemax_mb,
            "GDAL_NUM_THREADS": str(self.gdal_num_threads),
        }


DEFAULT_RUNTIME_LIMITS = RuntimeLimits()


def _bool_value(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"runtime.{key} must be a boolean")


def _positive_int(value: Any, key: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"runtime.{key} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"runtime.{key} must be a positive integer")
    return parsed


def runtime_limits_from_config(config: Mapping | None) -> RuntimeLimits:
    runtime_cfg: Mapping[str, Any] = {}
    if isinstance(config, Mapping):
        candidate = config["runtime"] if "runtime" in config else {}
        if candidate is None:
            candidate = {}
        if not isinstance(candidate, Mapping):
            raise ValueError("runtime must be a mapping")
        runtime_cfg = candidate

    defaults = DEFAULT_RUNTIME_LIMITS
    enabled = _bool_value(runtime_cfg.get("enabled", defaults.enabled), "enabled")

    return RuntimeLimits(
        enabled=enabled,
        gdal_cachemax_mb=_positive_int(
            runtime_cfg.get("gdal_cachemax_mb", defaults.gdal_cachemax_mb),
            "gdal_cachemax_mb",
        ),
        gdal_num_threads=_positive_int(
            runtime_cfg.get("gdal_num_threads", defaults.gdal_num_threads),
            "gdal_num_threads",
        ),
        omp_num_threads=_positive_int(
            runtime_cfg.get("omp_num_threads", defaults.omp_num_threads),
            "omp_num_threads",
        ),
        openblas_num_threads=_positive_int(
            runtime_cfg.get("openblas_num_threads", defaults.openblas_num_threads),
            "openblas_num_threads",
        ),
        mkl_num_threads=_positive_int(
            runtime_cfg.get("mkl_num_threads", defaults.mkl_num_threads),
            "mkl_num_threads",
        ),
        blis_num_threads=_positive_int(
            runtime_cfg.get("blis_num_threads", defaults.blis_num_threads),
            "blis_num_threads",
        ),
        veclib_maximum_threads=_positive_int(
            runtime_cfg.get("veclib_maximum_threads", defaults.veclib_maximum_threads),
            "veclib_maximum_threads",
        ),
        numexpr_num_threads=_positive_int(
            runtime_cfg.get("numexpr_num_threads", defaults.numexpr_num_threads),
            "numexpr_num_threads",
        ),
    )


def apply_runtime_limits(config_or_limits: Mapping | RuntimeLimits | None) -> dict[str, str]:
    limits = (
        config_or_limits
        if isinstance(config_or_limits, RuntimeLimits)
        else runtime_limits_from_config(config_or_limits)
    )
    env = limits.env()
    for key, value in env.items():
        os.environ[key] = value
    return env


def rasterio_env_kwargs(config_or_limits: Mapping | RuntimeLimits | None = None) -> dict[str, int | str]:
    """Return GDAL options for ``rasterio.Env``.

    When no config is supplied, this function only reflects already-applied
    process environment variables.  That keeps ``runtime.enabled: false`` from
    accidentally re-applying default limits inside rasterio read/write paths.
    """
    if config_or_limits is not None:
        limits = (
            config_or_limits
            if isinstance(config_or_limits, RuntimeLimits)
            else runtime_limits_from_config(config_or_limits)
        )
        return limits.rasterio_env()

    kwargs: dict[str, int | str] = {}
    cache = os.environ.get("GDAL_CACHEMAX")
    if cache:
        try:
            kwargs["GDAL_CACHEMAX"] = int(cache)
        except ValueError:
            kwargs["GDAL_CACHEMAX"] = cache
    threads = os.environ.get("GDAL_NUM_THREADS")
    if threads:
        kwargs["GDAL_NUM_THREADS"] = threads
    return kwargs
