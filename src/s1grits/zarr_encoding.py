"""Shared lossless encoding policy for S1-GRiTS Zarr v3 stores."""
from __future__ import annotations

from typing import Any

from zarr.codecs import ZstdCodec


ZARR_COMPRESSION_CODEC = "zstd"
ZARR_COMPRESSION_LEVEL = 7
ZARR_SHUFFLE = "none"


def zstd7_compressors() -> list[ZstdCodec]:
    """Return a fresh Zarr v3 codec pipeline: lossless Zstd level 7."""
    return [ZstdCodec(level=ZARR_COMPRESSION_LEVEL, checksum=False)]


def create_zarr_array(group: Any, name: str, **kwargs: Any) -> Any:
    """Create an array using the project-wide lossless compression policy."""
    kwargs.setdefault("compressors", zstd7_compressors())
    return group.create_array(name, **kwargs)


def xarray_zstd7_encoding(dataset: Any) -> dict[str, dict[str, Any]]:
    """Build per-variable Zarr v3 encoding for an xarray Dataset."""
    return {
        name: {"compressors": zstd7_compressors()}
        for name in dataset.variables
    }


def record_zarr_compression(attrs: Any) -> None:
    """Record the physical encoding without changing scientific metadata."""
    attrs["compression_codec"] = ZARR_COMPRESSION_CODEC
    attrs["compression_level"] = ZARR_COMPRESSION_LEVEL
    attrs["compression_shuffle"] = ZARR_SHUFFLE
