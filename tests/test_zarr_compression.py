"""Project-wide Zarr v3 compression contract."""
from __future__ import annotations

import numpy as np
from rasterio.transform import Affine
from zarr.codecs import ZstdCodec

from s1grits.scenes.store import _init_zarr_2band
from s1grits.workflow_static import _init_zarr_static


def _assert_zstd7(array) -> None:
    compressors = array.compressors
    assert len(compressors) == 1
    assert isinstance(compressors[0], ZstdCodec)
    assert compressors[0].level == 7


def test_dynamic_and_static_stores_use_lossless_zstd7(tmp_path):
    x = np.arange(4, dtype=np.float64) * 30.0 + 15.0
    y = 120.0 - np.arange(4, dtype=np.float64) * 30.0
    transform = Affine(30.0, 0.0, 0.0, 0.0, -30.0, 135.0)

    dynamic = _init_zarr_2band(
        tmp_path / "17MPU" / "scenes_ASCENDING" / "zarr" / "dynamic.zarr",
        x, y, "EPSG:32617", transform, 2, 2, "ARDC",
    )
    static = _init_zarr_static(
        tmp_path / "17MPU" / "static_ASCENDING" / "zarr" / "static.zarr",
        x, y, "EPSG:32617", transform, 2, 2, ["inc_angle"],
    )

    values = np.array(
        [[1.25, np.nan, 3.5, 4.75]] * 4,
        dtype=np.float32,
    )
    dynamic["VV_dB"].resize((1, 4, 4))
    dynamic["VV_dB"][0] = values
    static["inc_angle"][:] = values

    for group, name in ((dynamic, "VV_dB"), (static, "inc_angle")):
        _assert_zstd7(group[name])
        assert np.array_equal(
            np.asarray(group[name][0] if group[name].ndim == 3 else group[name][:]),
            values,
            equal_nan=True,
        )
        assert group.attrs["compression_codec"] == "zstd"
        assert group.attrs["compression_level"] == 7
        assert group.attrs["compression_shuffle"] == "none"
