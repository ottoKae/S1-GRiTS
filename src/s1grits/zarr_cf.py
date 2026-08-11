"""CF grid-mapping helpers for Zarr stores.

Centralises the CF-compliant CRS encoding so every Zarr creator in the codebase
(``workflow_scenes._init_zarr_2band`` and the monthly creator in
``asf_output_writing``) emits an identical, generic-reader-friendly grid mapping
instead of only the private ``g.attrs['crs']`` string.

It also provides :func:`band_data_vars`, the single source of truth for "which
variables in a store are 3-D data bands" — important because the CF encoding adds
a scalar ``spatial_ref`` variable that must be excluded when iterating bands.
"""
from __future__ import annotations

# Coordinate variables / grid-mapping variable that are never data bands.
_AUX_VARS = frozenset({"time", "x", "y", "spatial_ref"})


def band_data_vars(g) -> list[str]:
    """Return the names of 3-D data band arrays in an open Zarr group.

    Excludes coordinate variables (time/x/y) and the scalar ``spatial_ref``
    grid-mapping variable. Robust to auxiliary variables of other ranks.
    """
    out: list[str] = []
    for name in g.keys():
        if name in _AUX_VARS:
            continue
        try:
            if g[name].ndim == 3:
                out.append(name)
        except Exception:
            continue
    return out


def add_cf_grid_mapping(g, target_crs: str) -> None:
    """Write a CF-compliant grid mapping onto an already-populated Zarr group.

    Adds a scalar ``spatial_ref`` variable (``grid_mapping_name`` + ``crs_wkt``
    via :meth:`pyproj.CRS.to_cf`), sets ``grid_mapping='spatial_ref'`` on every
    3-D data band, and stamps CF ``standard_name``/``long_name``/``units``/``axis``
    on the ``x`` and ``y`` coordinates. Call once at store creation, after the
    coordinate and band arrays exist.
    """
    from pyproj import CRS as _PyCRS

    crs_obj = _PyCRS.from_user_input(str(target_crs))
    cf = crs_obj.to_cf()
    is_geographic = crs_obj.is_geographic
    if is_geographic:
        x_std, y_std, units = "longitude", "latitude", "degrees"
    else:
        x_std, y_std, units = "projection_x_coordinate", "projection_y_coordinate", "metre"

    if "x" in g:
        _ax = g["x"]
        _ax.attrs["standard_name"] = x_std
        _ax.attrs["long_name"] = "x coordinate of projection"
        _ax.attrs["units"] = units
        _ax.attrs["axis"] = "X"
    if "y" in g:
        _ay = g["y"]
        _ay.attrs["standard_name"] = y_std
        _ay.attrs["long_name"] = "y coordinate of projection"
        _ay.attrs["units"] = units
        _ay.attrs["axis"] = "Y"

    # Scalar CF grid_mapping variable carrying the full CRS (WKT + CF params).
    from s1grits.zarr_encoding import create_zarr_array
    sr = create_zarr_array(g, "spatial_ref", shape=(), dtype="int64", overwrite=True, dimension_names=[])
    sr[...] = 0
    sr.attrs["_ARRAY_DIMENSIONS"] = []
    for k, v in cf.items():
        sr.attrs[k] = v
    # GDAL convention duplicates the WKT under 'spatial_ref'.
    if cf.get("crs_wkt"):
        sr.attrs.setdefault("spatial_ref", cf["crs_wkt"])
    if "grid_mapping_name" not in sr.attrs:
        sr.attrs["grid_mapping_name"] = "latitude_longitude" if is_geographic else "transverse_mercator"

    for name in band_data_vars(g):
        g[name].attrs["grid_mapping"] = "spatial_ref"
