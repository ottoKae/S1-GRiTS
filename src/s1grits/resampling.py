"""Target-grid resolution and resampling contract for S1-GRiTS products.

Sentinel-1 RTC backscatter is a continuous field.  When a 10 m analysis grid
is requested, cross-grid reprojection is therefore performed with GDAL's
NoData-aware bilinear kernel while the values are still in linear power.
Categorical layers must opt out and use nearest-neighbour resampling.
"""
from __future__ import annotations

from numbers import Real

from rasterio.enums import Resampling


TARGET_RESOLUTIONS_M = (30.0, 10.0)
RESAMPLING_METHODS = ("auto", "nearest", "bilinear")


def validate_target_resolution(value: Real) -> float:
    """Return a canonical target resolution or raise a user-facing error."""
    try:
        resolution = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("processing.target_resolution must be 30 or 10 metres") from exc
    for allowed in TARGET_RESOLUTIONS_M:
        if abs(resolution - allowed) < 1e-9:
            return allowed
    raise ValueError(
        "processing.target_resolution must be one of 30 or 10 metres; "
        f"got {value!r}"
    )


def resolve_resampling_method(
    target_resolution: Real,
    method: str | None = "auto",
    *,
    categorical: bool = False,
) -> str:
    """Resolve ``auto`` to the stable v1 interpolation contract.

    * 30 m continuous products preserve the historical nearest-neighbour path.
    * 10 m continuous products use bilinear interpolation.
    * categorical products always use nearest-neighbour interpolation.
    """
    resolution = validate_target_resolution(target_resolution)
    normalized = str(method or "auto").strip().lower().replace("-", "_")
    aliases = {"nearest_neighbour": "nearest", "nearest_neighbor": "nearest"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in RESAMPLING_METHODS:
        raise ValueError(
            "processing.resampling_method must be auto, nearest, or bilinear; "
            f"got {method!r}"
        )
    if categorical:
        return "nearest"
    if normalized == "auto":
        return "bilinear" if resolution == 10.0 else "nearest"
    return normalized


def rasterio_resampling(method: str | Resampling) -> Resampling:
    """Convert the public method name to Rasterio's enum."""
    if isinstance(method, Resampling):
        return method
    normalized = str(method).strip().lower()
    if normalized == "nearest":
        return Resampling.nearest
    if normalized == "bilinear":
        return Resampling.bilinear
    raise ValueError(f"Resolved resampling method must be nearest or bilinear; got {method!r}")
