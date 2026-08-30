from __future__ import annotations

import numpy as np
import pytest
from rasterio.enums import Resampling
from rasterio.transform import from_origin

from s1grits.resampling import (
    rasterio_resampling,
    resolve_resampling_method,
    validate_target_resolution,
)
from s1grits.scenes.mosaic import _prealign_scenes_to_master_grid


def test_fixed_resolution_and_auto_contract():
    assert validate_target_resolution(30) == 30.0
    assert validate_target_resolution("10") == 10.0
    assert resolve_resampling_method(30, "auto") == "nearest"
    assert resolve_resampling_method(10, "auto") == "bilinear"
    assert resolve_resampling_method(10, "bilinear", categorical=True) == "nearest"
    assert rasterio_resampling("bilinear") is Resampling.bilinear
    with pytest.raises(ValueError, match="30 or 10"):
        validate_target_resolution(20)
    with pytest.raises(ValueError, match="auto, nearest, or bilinear"):
        resolve_resampling_method(10, "cubic")


def test_10m_bilinear_prealignment_interpolates_linear_power_without_nodata_bleed():
    source = np.array([[1.0, 4.0], [7.0, -9999.0]], dtype=np.float32)
    profile = {
        "transform": from_origin(0, 60, 30, 30),
        "crs": "EPSG:32649",
        "nodata": -9999.0,
        "height": 2,
        "width": 2,
    }
    destination_transform = from_origin(0, 60, 10, 10)

    arrays, profiles = _prealign_scenes_to_master_grid(
        [source], [profile], destination_transform, "EPSG:32649", 6, 6,
        resampling=Resampling.bilinear,
    )

    result = arrays[0]
    assert profiles[0]["transform"] == destination_transform
    assert result.shape == (6, 6)
    finite = result[np.isfinite(result)]
    assert finite.size > 0
    assert finite.min() >= 1.0
    assert finite.max() <= 7.0
    assert np.any(~np.isin(np.round(finite, 5), np.array([1.0, 4.0, 7.0])))
    assert not np.any(result == -9999.0)
