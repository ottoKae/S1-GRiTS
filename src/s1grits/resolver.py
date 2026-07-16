"""
resolver.py
===========
Virtual Cube Resolver for S1-GRiTS.

Scope and Responsibilities:
  - Spatial alignment: query products sharing the same grid_id on a tile
  - Processing consistency: verify same processing_signature within product_type
  - Asset opening: lazy-open Zarr stores as xarray Datasets (dask-backed)
  - Static broadcast: replicate (y,x) static layers to (time,y,x) on demand

NOT in scope (left to downstream analysis code):
  - Temporal semantics: no resampling, interpolation, or time-axis alignment
  - Cross-frequency joins: scenes (irregular ~6d) and smonthly (monthly) have
    different time semantics; the resolver does not merge or align them
  - Band arithmetic: no index computation (Ratio, NDVI, etc.)

Design rationale:
  Different downstream tasks need different temporal logic (change detection
  wants raw scene timestamps; trend analysis wants monthly composites; ML
  pipelines want custom time windows). Encoding any of these in the resolver
  would create a fat interface that grows with every use case. Instead, the
  resolver provides spatially-aligned, pixel-registered lazy Datasets, and
  consumers compose them as needed.

Usage:
    from s1grits.resolver import CubeResolver

    r = CubeResolver("path/to/output")

    # Low-level: open individual products
    scenes = r.query(tile_id="50RKV", product_type="scenes")
    ds = r.open(scenes.iloc[0], chunks={"time":1, "y":512, "x":512})

    # High-level: open aligned stack (scenes + static merged)
    stack = r.open_stack(tile_id="50RKV", product_types=["scenes", "static"])
"""

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import xarray as xr

from s1grits.canonical_catalog_schema import filter_complete_records
from s1grits.logger_config import get_logger

logger = get_logger(__name__)


class CubeResolver:
    """
    Spatial alignment + asset opening + static broadcast resolver.

    Responsibilities:
      1. Query catalog by tile, product_type, direction, time range
      2. Verify grid_id and processing_signature consistency
      3. Open Zarr stores as lazy xarray Datasets
      4. Broadcast static (y,x) layers to (time,y,x)
      5. Merge time-varying + static into a single pixel-registered Dataset

    NOT responsible for temporal alignment, resampling, or cross-frequency
    joins. Time axis semantics are left to downstream consumers.

    Example:
        r = CubeResolver("G:/datacube")

        # Option A: open individual products
        scenes_ds = r.open(r.query(tile_id="50RKV", product_type="scenes").iloc[0])

        # Option B: open merged stack (scenes + static broadcast)
        stack = r.open_stack("50RKV", ["scenes", "static"],
                            chunks={"time":1, "y":512, "x":512})
        # stack is a single xr.Dataset with VV_dB, VH_dB, ..., local_inc_angle, ...
    """

    def __init__(self, output_dir: str | Path):
        self.root = Path(output_dir)
        self._df: Optional[pd.DataFrame] = None

    @property
    def catalog(self) -> pd.DataFrame:
        if self._df is None:
            self._load()
        return self._df

    def _load(self) -> None:
        path = self.root / "catalog.parquet"
        if not path.exists():
            raise FileNotFoundError(f"catalog.parquet not found at {self.root}")
        self._df = pd.read_parquet(path)
        self._df = filter_complete_records(self._df, require_grid=True)
        logger.info("Loaded %d complete records from %s", len(self._df), path)

    def query(
        self,
        tile_id: str | None = None,
        product_type: str | None = None,
        direction: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
    ) -> pd.DataFrame:
        """
        Query catalog for matching products.

        Args:
            tile_id: MGRS tile (e.g. "50RKV")
            product_type: "scenes", "smonthly", "static", "monthly"
            direction: "ASCENDING" or "DESCENDING"
            time_start: ISO date string (inclusive)
            time_end: ISO date string (inclusive)

        Returns:
            Filtered DataFrame of catalog records.
        """
        df = self.catalog.copy()
        if tile_id:
            df = df[df["tile_id"] == tile_id]
        if product_type:
            df = df[df["product_type"] == product_type]
        if direction:
            df = df[df["flight_direction"] == direction]
        if time_start:
            df = df[df["datetime"] >= pd.Timestamp(time_start)]
        if time_end:
            df = df[df["datetime"] <= pd.Timestamp(time_end)]
        return df.reset_index(drop=True)

    def get_aligned_products(
        self,
        tile_id: str,
        product_types: list[str] | None = None,
        require_same_grid: bool = False,
        require_same_processing: bool = True,
    ) -> dict[str, pd.DataFrame]:
        """
        Return products aligned on the same spatial grid.

        Groups records by grid_id, returning the largest group (most products
        share the same grid). Useful for finding scenes + static that can be
        directly stacked.

        Args:
            tile_id: MGRS tile.
            product_types: Optional list of product types to include.
            require_same_grid: If True, raises ValueError when products
                have different grid_ids on the same tile.
            require_same_processing: If True (default), raises ValueError when
                a single product_type has multiple processing_signatures.
                If False, returns results grouped by (product_type, signature).

        Returns:
            Dict mapping product_type (or "product_type:signature") to
            DataFrame of aligned records.

        Raises:
            ValueError: If require_same_grid=True and multiple grid_ids found.
            ValueError: If require_same_processing=True and multiple signatures found.
        """
        df = self.query(tile_id=tile_id)
        if product_types:
            df = df[df["product_type"].isin(product_types)]

        _gid_counts = df["grid_id"].value_counts()
        if _gid_counts.empty:
            return {}

        if require_same_grid and len(_gid_counts) > 1:
            _gid_summary = ", ".join(
                f"{gid} (n={n})" for gid, n in _gid_counts.items()
            )
            raise ValueError(
                f"require_same_grid=True but tile '{tile_id}' has "
                f"{len(_gid_counts)} distinct grid_ids: {_gid_summary}. "
                f"Products on the same tile must share identical CRS, "
                f"transform, resolution, and dimensions."
            )

        target_gid = _gid_counts.index[0]
        _aligned = df[df["grid_id"] == target_gid]

        # Check processing_signature consistency
        _has_sig = "processing_signature" in _aligned.columns
        if _has_sig and require_same_processing:
            for pt in _aligned["product_type"].unique():
                _pt_df = _aligned[_aligned["product_type"] == pt]
                _sigs = _pt_df["processing_signature"].dropna().unique()
                if len(_sigs) > 1:
                    raise ValueError(
                        f"require_same_processing=True but product_type '{pt}' "
                        f"on tile '{tile_id}' has {len(_sigs)} distinct "
                        f"processing_signatures: {list(_sigs)}. "
                        f"Use require_same_processing=False to get grouped results."
                    )

        # Build result dict
        result: dict[str, pd.DataFrame] = {}
        if _has_sig and not require_same_processing:
            # Group by (product_type, processing_signature)
            for pt in _aligned["product_type"].unique():
                _pt_df = _aligned[_aligned["product_type"] == pt]
                _sigs = _pt_df["processing_signature"].dropna().unique()
                if len(_sigs) <= 1:
                    result[pt] = _pt_df.reset_index(drop=True)
                else:
                    for sig in _sigs:
                        key = f"{pt}:{sig}"
                        result[key] = _pt_df[_pt_df["processing_signature"] == sig].reset_index(drop=True)
                    # Legacy records without signature
                    _legacy = _pt_df[_pt_df["processing_signature"].isna()]
                    if not _legacy.empty:
                        result[f"{pt}:legacy"] = _legacy.reset_index(drop=True)
        else:
            for pt in _aligned["product_type"].unique():
                result[pt] = _aligned[_aligned["product_type"] == pt].reset_index(drop=True)
        return result

    def open(
        self,
        record: pd.Series | dict,
        chunks: dict | None = None,
        broadcast_static: bool = False,
        reference_time: "np.ndarray | None" = None,
    ) -> xr.Dataset:
        """
        Open a Zarr store from a catalog record.

        Args:
            record: A single catalog row (Series or dict with 'zarr_path').
            chunks: Optional chunking dict for dask-backed loading.
            broadcast_static: If True and product is static, broadcast (y,x)
                variables to (time,y,x) using reference_time.
            reference_time: Time coordinate array for broadcasting static data.

        Returns:
            xarray Dataset.
        """
        _zp = record.get("zarr_path") if isinstance(record, dict) else record["zarr_path"]
        if not _zp:
            raise ValueError("Record has no zarr_path")
        _full = self.root / _zp
        logger.info("Opening %s", _full)

        try:
            ds = xr.open_zarr(str(_full), chunks=chunks, consolidated=False)
        except (ValueError, KeyError) as _xr_err:
            logger.warning(
                "xr.open_zarr failed (%s), falling back to manual construction for %s",
                _xr_err, _full,
            )
            ds = self._open_manual(str(_full), record, chunks)

        if broadcast_static:
            ds = self._maybe_broadcast(ds, record, reference_time)
        return ds

    def _open_manual(
        self, path: str, record: pd.Series | dict, chunks: dict | None = None
    ) -> xr.Dataset:
        """Fallback: manually construct xarray.Dataset from zarr arrays."""
        import zarr
        import numpy as np

        g = zarr.open_group(path, mode="r")
        product_type = record.get("product_type") if isinstance(record, dict) else record.get("product_type", "")
        is_static = str(product_type).lower() == "static"

        data_vars: dict = {}
        coords: dict = {}
        dims_seen: set[str] = set()

        for name in g:
            arr = g[name]
            if not hasattr(arr, "attrs"):
                continue

            # Read dimension names: try dimension_names first, then _ARRAY_DIMENSIONS
            dims = arr.attrs.get("dimension_names") or arr.attrs.get("_ARRAY_DIMENSIONS")
            if dims is None:
                continue

            # For static products, strip "time" dimension
            if is_static and "time" in dims:
                dims = [d for d in dims if d != "time"]

            data = arr[:] if chunks is None else arr
            da = xr.DataArray(data, dims=list(dims), name=name)

            if name in ("x", "y", "time"):
                coords[name] = da
            else:
                data_vars[name] = da

            for d in dims:
                dims_seen.add(d)

        # Ensure time coordinate for non-static products
        if not is_static and "time" in dims_seen:
            if "time" in coords:
                coords["time"] = coords["time"].astype("datetime64[ns]")
            else:
                coords["time"] = xr.DataArray(
                    np.array([], dtype="datetime64[ns]"), dims=["time"]
                )

        return xr.Dataset(data_vars=data_vars, coords=coords)

    def _maybe_broadcast(
        self, ds: xr.Dataset, record: pd.Series | dict, reference_time=None
    ) -> xr.Dataset:
        """Broadcast static (y,x) variables to (time,y,x) if applicable."""
        product_type = record.get("product_type") if isinstance(record, dict) else record.get("product_type", "")
        if str(product_type).lower() != "static":
            return ds
        if reference_time is None or "time" in ds.dims:
            return ds

        import numpy as np
        ref_time = np.asarray(reference_time)
        n_time = len(ref_time)

        new_vars = {}
        for name, da in ds.data_vars.items():
            if "time" not in da.dims and da.ndim == 2:
                expanded = da.expand_dims("time", axis=0)
                tiled = np.tile(expanded.values, (n_time, 1, 1))
                new_vars[name] = xr.DataArray(tiled, dims=["time", "y", "x"], coords={"time": ref_time})
            else:
                new_vars[name] = da
        for name, da in ds.coords.items():
            if name != "time":
                new_vars[name] = da
        new_vars["time"] = xr.DataArray(ref_time, dims=["time"])
        return xr.Dataset(new_vars)

    def open_stack(
        self,
        tile_id: str,
        product_types: list[str] | None = None,
        direction: str | None = None,
        chunks: dict | None = None,
        require_same_processing: bool = True,
    ) -> xr.Dataset | dict[str, xr.Dataset]:
        """
        Open aligned products and merge into a single pixel-registered Dataset.

        Merges time-varying products with static layers (broadcast) into one
        Dataset where all bands share the same (time, y, x) dimensions.

        Merge rules:
          - time-varying + static → merge (static is broadcast to time axis)
          - multiple time-varying with SAME time axis → merge
          - multiple time-varying with DIFFERENT time axes → return dict
            (scenes has irregular ~6d; smonthly has monthly; they cannot be
            automatically merged without temporal semantics)

        This method does NOT perform temporal resampling or alignment.
        It only merges products that are already pixel-registered on the
        same spatial grid and share a compatible time coordinate.

        Args:
            tile_id: MGRS tile (e.g. "50RKV").
            product_types: Product types to include. If None, includes all
                available on this tile.
            direction: Optional flight direction filter.
            chunks: Dask chunking dict for lazy loading.
            require_same_processing: If True, raises on mixed signatures.

        Returns:
            xr.Dataset if all products can be merged into one.
            dict[str, xr.Dataset] if time axes are incompatible
            (keys are product_type names).

        Example:
            # Merge scenes + static (most common use case)
            ds = resolver.open_stack("50RKV", ["scenes", "static"],
                                     chunks={"time":1, "y":512, "x":512})
            # ds has: VV_dB(time,y,x), VH_dB(time,y,x), local_inc_angle(time,y,x)

            # When time axes differ, get separate Datasets
            result = resolver.open_stack("50RKV", ["scenes", "smonthly"])
            # result = {"scenes": ds_scenes, "smonthly": ds_smonthly}
        """
        aligned = self.get_aligned_products(
            tile_id=tile_id,
            product_types=product_types,
            require_same_processing=require_same_processing,
        )
        if not aligned:
            raise ValueError(f"No aligned products found for tile '{tile_id}'")

        # Apply direction filter if specified
        if direction:
            aligned = {
                k: df[df["flight_direction"] == direction].reset_index(drop=True)
                if "flight_direction" in df.columns else df
                for k, df in aligned.items()
            }
            aligned = {k: df for k, df in aligned.items() if not df.empty}
            if not aligned:
                raise ValueError(
                    f"No products found for tile '{tile_id}' "
                    f"with direction '{direction}'"
                )

        # Open all products
        opened: dict[str, xr.Dataset] = {}
        static_datasets: list[xr.Dataset] = []
        time_varying_datasets: dict[str, xr.Dataset] = {}

        for pt, df in aligned.items():
            # Use the first record for each product_type (they share the same Zarr)
            record = df.iloc[0]
            ds = self.open(record, chunks=chunks)

            # Classify as static or time-varying
            _pt_name = pt.split(":")[0] if ":" in pt else pt
            if _pt_name == "static":
                static_datasets.append(ds)
            else:
                time_varying_datasets[pt] = ds
                opened[pt] = ds

        # Determine the reference time axis for static broadcast
        # Use the first time-varying product's time coordinate
        reference_time = None
        _primary_pt = None
        for pt, ds in time_varying_datasets.items():
            if "time" in ds.dims and len(ds.time) > 0:
                reference_time = ds.time.values
                _primary_pt = pt
                break

        # Broadcast static layers to reference time axis
        if static_datasets and reference_time is not None:
            for static_ds in static_datasets:
                _bc = self._broadcast_static_ds(static_ds, reference_time)
                opened["static"] = _bc

        # Attempt merge
        if len(opened) == 0:
            # Only static, no time-varying
            if static_datasets:
                return static_datasets[0]
            raise ValueError(f"No datasets could be opened for tile '{tile_id}'")

        if len(opened) == 1:
            return next(iter(opened.values()))

        # Check if all time-varying datasets share the same time axis
        _time_axes = {}
        for pt, ds in opened.items():
            if "time" in ds.dims:
                _time_axes[pt] = ds.time.values

        _can_merge = True
        if len(_time_axes) > 1:
            _ref = list(_time_axes.values())[0]
            for _other in list(_time_axes.values())[1:]:
                if len(_ref) != len(_other) or not (
                    all(_ref == _other)
                ):
                    _can_merge = False
                    break

        if _can_merge:
            # All share the same time axis (or static-only) → merge
            merged = xr.merge(list(opened.values()), compat="override")
            return merged
        else:
            # Different time axes → return dict, let user decide
            logger.info(
                "open_stack: products have different time axes, "
                "returning dict instead of merged Dataset. "
                "Products: %s", list(opened.keys())
            )
            return opened

    def _broadcast_static_ds(
        self, ds: xr.Dataset, reference_time
    ) -> xr.Dataset:
        """Broadcast a static (y,x) Dataset to (time,y,x)."""
        import numpy as np
        ref_time = np.asarray(reference_time)
        n_time = len(ref_time)

        new_vars = {}
        for name, da in ds.data_vars.items():
            if "time" not in da.dims and da.ndim == 2:
                expanded = da.expand_dims("time", axis=0)
                tiled = np.tile(expanded.values, (n_time, 1, 1))
                new_vars[name] = xr.DataArray(
                    tiled, dims=["time", "y", "x"],
                    coords={"time": ref_time},
                )
            else:
                new_vars[name] = da
        for name, da in ds.coords.items():
            if name != "time":
                new_vars[name] = da
        new_vars["time"] = xr.DataArray(ref_time, dims=["time"])
        return xr.Dataset(new_vars)

    def close(self) -> None:
        """Release cached catalog DataFrame."""
        self._df = None
