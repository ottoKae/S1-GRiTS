"""
resolver.py
===========
Virtual Cube Resolver for S1-GRiTS.

Scope and Responsibilities:
  - Spatial alignment: query products sharing the same grid_id on a tile
  - Processing consistency: verify same processing_signature within product_type
  - Asset opening: lazy-open Zarr stores as xarray Datasets (dask-backed)
  - Static handling: preserve (y,x) layers; optional lazy time broadcast

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
      4. Keep static (y,x) layers pixel-registered without time duplication
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
        geometry_group_id: str | None = None,
        track: int | None = None,
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
        if geometry_group_id:
            df = df[df["geometry_group_id"] == geometry_group_id]
        if track is not None:
            df = df[df["track"] == int(track)]
        return df.reset_index(drop=True)

    def get_aligned_products(
        self,
        tile_id: str,
        product_types: list[str] | None = None,
        require_same_grid: bool = False,
        require_same_processing: bool = True,
        geometry_group_id: str | None = None,
        track: int | None = None,
    ) -> dict[str, pd.DataFrame]:
        """
        Return products aligned for stacking on a tile.

        Time-varying products (scenes/smonthly/monthly) must share one spatial
        grid to co-stack, so they are grouped by ``grid_id`` and the largest
        group is returned. **Static** products sit on the MGRS-tile grid — a
        pixel-aligned SUB-WINDOW of the (larger, query-dependent) dynamic grid,
        hence a different ``grid_id`` — so they are included regardless of
        ``grid_id`` and spatially windowed onto the dynamic grid at load time
        (see :meth:`open_stack`). This is the static↔scenes pairing contract:
        join on ``tile_id`` (+ track/direction), align by windowing.

        Args:
            tile_id: MGRS tile.
            product_types: Optional list of product types to include.
            require_same_grid: If True, raises ValueError when the selected
                products (including static) do not all share one grid_id.
                Static normally differs, so this is for callers that need a
                byte-identical direct stack.
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
        df = self.query(
            tile_id=tile_id,
            geometry_group_id=geometry_group_id,
            track=track,
        )
        if product_types:
            df = df[df["product_type"].isin(product_types)]
        if df.empty or "grid_id" not in df.columns:
            return {}

        if require_same_grid:
            _all_gids = df["grid_id"].dropna().unique()
            if len(_all_gids) > 1:
                _counts = df["grid_id"].value_counts()
                _gid_summary = ", ".join(f"{gid} (n={n})" for gid, n in _counts.items())
                raise ValueError(
                    f"require_same_grid=True but tile '{tile_id}' has "
                    f"{len(_all_gids)} distinct grid_ids: {_gid_summary}. "
                    f"Products on the same tile must share identical CRS, "
                    f"transform, resolution, and dimensions (note: static sits "
                    f"on the tile grid and normally differs — use "
                    f"require_same_grid=False and open_stack's windowing)."
                )

        _is_static = df["product_type"].astype(str).str.lower() == "static"
        _dyn = df[~_is_static]
        _static = df[_is_static]

        # Majority grid among the co-stacking (time-varying) products; static is
        # aligned by windowing, so it is not constrained to that grid.
        _gid_counts = _dyn["grid_id"].value_counts()
        if _gid_counts.empty:
            # No time-varying products — fall back to the majority static grid.
            _gid_counts = _static["grid_id"].value_counts()
            if _gid_counts.empty:
                return {}
            target_gid = _gid_counts.index[0]
            _aligned = _static[_static["grid_id"] == target_gid]
        else:
            target_gid = _gid_counts.index[0]
            _aligned = pd.concat(
                [_dyn[_dyn["grid_id"] == target_gid], _static], ignore_index=True
            )

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
        if not _full.exists():
            # Global catalogs hold asset paths relative to the TILE directory
            # (resync/workflows write them relative_to(tile_dir)), so resolve
            # against {root}/{tile_id}/ when the root-relative path is absent.
            _tid = record.get("tile_id") if isinstance(record, dict) else record.get("tile_id", None)
            if _tid:
                _cand = self.root / str(_tid) / _zp
                if _cand.exists():
                    _full = _cand
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
        new_vars = {}
        for name, da in ds.data_vars.items():
            if "time" not in da.dims and da.ndim == 2:
                new_vars[name] = da.expand_dims(time=ref_time).transpose(
                    "time", "y", "x"
                )
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
        geometry_group_id: str | None = None,
        track: int | None = None,
        broadcast_static: bool = False,
    ) -> xr.Dataset | dict[str, xr.Dataset]:
        """
        Open aligned products and merge into a single pixel-registered Dataset.

        Merges time-varying products with static layers into one Dataset.
        Static remains ``(y, x)`` by default; downstream xarray expressions or
        model batches can broadcast it only where needed.

        Merge rules:
          - time-varying + static → merge (static remains 2-D by default)
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
            broadcast_static: Compatibility opt-in that exposes static as a
                lazy ``(time, y, x)`` view. Defaults to False.

        Returns:
            xr.Dataset if all products can be merged into one.
            dict[str, xr.Dataset] if time axes are incompatible
            (keys are product_type names).

        Example:
            # Merge scenes + static (most common use case)
            ds = resolver.open_stack("50RKV", ["scenes", "static"],
                                     chunks={"time":1, "y":512, "x":512})
            # ds has: VV_dB(time,y,x), VH_dB(time,y,x), local_inc_angle(y,x)

            # When time axes differ, get separate Datasets
            result = resolver.open_stack("50RKV", ["scenes", "smonthly"])
            # result = {"scenes": ds_scenes, "smonthly": ds_smonthly}
        """
        aligned = self.get_aligned_products(
            tile_id=tile_id,
            product_types=product_types,
            require_same_processing=require_same_processing,
            geometry_group_id=geometry_group_id,
            track=track,
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

        # Never pair the first scenes row with the first static row across
        # different tracks. A geometry group is the physical acquisition
        # geometry contract (tile + direction + track).
        _groups: set[str] = set()
        for _df in aligned.values():
            if "geometry_group_id" in _df.columns:
                _groups.update(str(v) for v in _df["geometry_group_id"].dropna().unique())
        if len(_groups) > 1:
            raise ValueError(
                f"Multiple geometry groups match tile '{tile_id}': "
                f"{sorted(_groups)}. Pass geometry_group_id=... or track=... "
                "so dynamic scenes and static layers cannot be paired across tracks."
            )
        _selected_geometry_group = (
            next(iter(_groups)) if _groups else geometry_group_id
        )

        def _stamp_geometry(ds: xr.Dataset) -> xr.Dataset:
            if _selected_geometry_group:
                ds.attrs["s1grits:geometry_group_id"] = str(
                    _selected_geometry_group
                )
            return ds

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

        # Determine the reference dynamic dataset (its time axis AND spatial
        # grid) for static alignment. Use the first time-varying product.
        reference_time = None
        reference_ds = None
        _primary_pt = None
        for pt, ds in time_varying_datasets.items():
            if "time" in ds.dims and len(ds.time) > 0:
                reference_time = ds.time.values
                reference_ds = ds
                _primary_pt = pt
                break

        # Window static layers onto the dynamic grid. This is an exact reindex,
        # never a resample. Keep static 2-D unless compatibility broadcasting
        # was explicitly requested.
        if static_datasets and reference_ds is not None:
            for static_ds in static_datasets:
                _aligned_static = self._align_static_grid(static_ds, reference_ds)
                if broadcast_static:
                    _aligned_static = self._broadcast_static_ds(
                        _aligned_static, reference_time, reference_ds=reference_ds
                    )
                opened["static"] = _aligned_static

        # Attempt merge
        if len(opened) == 0:
            # Only static, no time-varying
            if static_datasets:
                return _stamp_geometry(static_datasets[0])
            raise ValueError(f"No datasets could be opened for tile '{tile_id}'")

        if len(opened) == 1:
            return _stamp_geometry(next(iter(opened.values())))

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
            return _stamp_geometry(merged)
        else:
            # Different time axes → return dict, let user decide
            logger.info(
                "open_stack: products have different time axes, "
                "returning dict instead of merged Dataset. "
                "Products: %s", list(opened.keys())
            )
            return opened

    def _broadcast_static_ds(
        self, ds: xr.Dataset, reference_time, reference_ds: xr.Dataset | None = None
    ) -> xr.Dataset:
        """Align a static (y,x) Dataset onto the dynamic grid and broadcast it
        to (time,y,x).

        Static sits on the MGRS-tile grid — a pixel-aligned sub-window of the
        (larger, query-dependent) dynamic grid. When ``reference_ds`` is given,
        the static grid is reindexed onto the dynamic x/y coordinates by nearest
        match within half a pixel: an exact 1:1 pickup in the overlap (the grids
        are co-registered), NaN in the beyond-tile margin. The result is
        pixel-registered with the dynamic cube, so the later ``xr.merge`` is a
        clean alignment rather than a coordinate outer-join.
        """
        import numpy as np

        # Spatial window onto the dynamic grid (exact; no interpolation).
        if (
            reference_ds is not None
            and {"x", "y"} <= set(ds.coords)
            and {"x", "y"} <= set(reference_ds.coords)
        ):
            ds = self._align_static_grid(ds, reference_ds)

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

    @staticmethod
    def _align_static_grid(ds: xr.Dataset, reference_ds: xr.Dataset) -> xr.Dataset:
        """Align static to a dynamic grid without resampling.

        New static stores normally have byte-identical x/y coordinates because
        they adopt the scenes grid during production. Legacy tile-grid stores
        are accepted only when their resolution and origin offset prove they
        occupy the same integer pixel lattice.
        """
        sx = np.asarray(ds["x"].values, dtype=np.float64)
        sy = np.asarray(ds["y"].values, dtype=np.float64)
        rx = np.asarray(reference_ds["x"].values, dtype=np.float64)
        ry = np.asarray(reference_ds["y"].values, dtype=np.float64)
        if np.array_equal(sx, rx) and np.array_equal(sy, ry):
            return ds
        if min(sx.size, sy.size, rx.size, ry.size) < 2:
            raise ValueError("Cannot verify pixel alignment for a degenerate x/y grid")
        sdx, sdy = float(sx[1] - sx[0]), float(sy[1] - sy[0])
        rdx, rdy = float(rx[1] - rx[0]), float(ry[1] - ry[0])
        atol = max(abs(rdx), abs(rdy)) * 1e-7
        if not (np.isclose(sdx, rdx, atol=atol, rtol=0) and np.isclose(sdy, rdy, atol=atol, rtol=0)):
            raise ValueError(
                "Static and dynamic grids have different pixel resolutions; "
                "resampling is forbidden for pixel-exact resolver alignment."
            )
        xoff = (sx[0] - rx[0]) / rdx
        yoff = (sy[0] - ry[0]) / rdy
        if not (
            np.isclose(xoff, round(xoff), atol=1e-6, rtol=0)
            and np.isclose(yoff, round(yoff), atol=1e-6, rtol=0)
        ):
            raise ValueError(
                "Static and dynamic grids are not on the same integer-pixel lattice; "
                "rerun workflow_static with static_layers.grid_reference=required."
            )
        tol = min(abs(rdx), abs(rdy)) * 0.49
        return ds.reindex(x=rx, y=ry, method="nearest", tolerance=tol)

    # ------------------------------------------------------------------
    # Analysis-ready training cube (materialized view)
    # ------------------------------------------------------------------

    def materialize_training_cube(
        self,
        tile_id: str,
        output_path: str | Path,
        dynamic_product_type: str = "scenes",
        direction: str | None = None,
        static_bands: list[str] | None = None,
        chunks: dict | None = None,
        overwrite: bool = False,
        geometry_group_id: str | None = None,
        track: int | None = None,
    ) -> Path:
        """Write an analysis-ready training cube for one geometry group.

        Combines a dynamic ``(time, y, x)`` product (scenes/smonthly) with its
        **geometry-correct** static layers — matched on ``geometry_group_id``
        (same tile + track + direction), so the incidence-angle / LIA field
        belongs to the same acquisition geometry as the backscatter — into a
        single Zarr store:

            {output_path}                      # root group
              ├── VV_dB, VH_dB, Ratio, …       # (time, y, x) dynamic bands
              ├── x, y, time                   # coords
              └── static/                      # subgroup, NO time dimension
                   ├── local_inc_angle, …      # (y, x), co-registered
                   └── x, y

        Static is stored **once** as 2-D arrays (never tiled over time); the
        static grid is windowed onto the dynamic grid by an exact reindex
        (nearest within half a pixel — the grids are co-registered), so every
        pixel/patch sample carries both the SAR time series and its geometry
        context with no per-timestep duplication. This is a *derived*,
        regenerable product; the canonical archive stays the independent
        scenes/static stores. Load it back with :meth:`open_training_cube`.

        Args:
            tile_id: MGRS tile.
            output_path: Destination Zarr store path.
            dynamic_product_type: "scenes" (default) or "smonthly".
            direction: Optional "ASCENDING"/"DESCENDING" filter.
            static_bands: Optional subset of static layers to include (default:
                all bands of the matched static product).
            chunks: Dask chunks for the dynamic read/write (e.g.
                ``{"time": 1, "y": 512, "x": 512}``); static reuses the y/x part.
            overwrite: Overwrite an existing store at ``output_path``.

        Returns:
            Path to the written training cube.

        Raises:
            ValueError: If no dynamic product, or no static product for the
                geometry group, is found on the tile.
            FileExistsError: If ``output_path`` exists and ``overwrite`` is False.
        """
        out = Path(output_path)
        if out.exists() and not overwrite:
            raise FileExistsError(
                f"{out} exists; pass overwrite=True to replace it."
            )

        dyn_df = self.query(
            tile_id=tile_id, product_type=dynamic_product_type, direction=direction,
            geometry_group_id=geometry_group_id, track=track,
        )
        if dyn_df.empty:
            raise ValueError(
                f"No '{dynamic_product_type}' product for tile '{tile_id}'"
                + (f" direction '{direction}'" if direction else "")
            )
        _dyn_groups = dyn_df["geometry_group_id"].dropna().unique()
        if len(_dyn_groups) > 1:
            raise ValueError(
                f"Multiple geometry groups match tile '{tile_id}': {list(_dyn_groups)}. "
                "Pass geometry_group_id=... or track=...."
            )
        dyn_rec = dyn_df.iloc[0]
        ggid = dyn_rec.get("geometry_group_id")

        # Geometry-correct static: match the dynamic product's geometry group
        # (same track+direction) so LIA/incidence belong to the same geometry.
        stat_df = self.query(
            tile_id=tile_id, product_type="static", direction=direction,
            geometry_group_id=str(ggid) if ggid else geometry_group_id,
            track=track,
        )
        if ggid and "geometry_group_id" in stat_df.columns:
            _matched = stat_df[stat_df["geometry_group_id"] == ggid]
            if not _matched.empty:
                stat_df = _matched
        if stat_df.empty:
            raise ValueError(
                f"No static product for tile '{tile_id}' geometry group "
                f"'{ggid}' — run the static workflow for this tile/direction."
            )
        stat_rec = stat_df.iloc[0]

        dyn = self.open(dyn_rec, chunks=chunks)
        stat = self.open(stat_rec)

        # Window static onto the dynamic grid (exact; co-registered).
        if {"x", "y"} <= set(dyn.coords) and {"x", "y"} <= set(stat.coords):
            stat = self._align_static_grid(stat, dyn)
        if static_bands:
            _keep = [b for b in static_bands if b in stat.data_vars]
            if _keep:
                stat = stat[_keep]
        # Static must not carry a time axis in the materialized cube.
        if "time" in stat.dims:
            stat = stat.isel(time=0, drop=True)
        if chunks:
            _spatial = {k: v for k, v in chunks.items() if k in ("y", "x")}
            if _spatial:
                stat = stat.chunk(_spatial)

        # Write: dynamic to the root group, static to the `static/` subgroup.
        from s1grits.zarr_encoding import xarray_zstd7_encoding
        dyn.to_zarr(
            str(out), mode="w", consolidated=False,
            encoding=xarray_zstd7_encoding(dyn),
        )
        stat.to_zarr(
            str(out), group="static", mode="a", consolidated=False,
            encoding=xarray_zstd7_encoding(stat),
        )

        # Provenance + grid identity on the root group.
        import zarr
        g = zarr.open_group(str(out), mode="r+")
        g.attrs["s1grits:training_cube"] = True
        g.attrs["s1grits:tile_id"] = str(tile_id)
        g.attrs["s1grits:geometry_group_id"] = ggid
        g.attrs["s1grits:dynamic_product_type"] = str(dynamic_product_type)
        g.attrs["s1grits:static_bands"] = list(stat.data_vars)
        g.attrs["s1grits:source_dynamic_zarr"] = str(dyn_rec.get("zarr_path") or "")
        g.attrs["s1grits:source_static_zarr"] = str(stat_rec.get("zarr_path") or "")
        for _k in ("crs", "grid_id"):
            _v = dyn_rec.get(_k)
            if _v is not None and not pd.isna(_v):
                g.attrs[_k] = str(_v)
        _tfm = dyn_rec.get("transform")
        if _tfm is not None:
            g.attrs["transform"] = [float(v) for v in list(_tfm)[:6]]
        logger.info(
            "Training cube written: %s (dynamic=%s, static=%s, group=%s)",
            out, dynamic_product_type, list(stat.data_vars), ggid,
        )
        return out

    @staticmethod
    def open_training_cube(
        path: str | Path, chunks: dict | None = None
    ) -> xr.Dataset:
        """Open a cube written by :meth:`materialize_training_cube` as one
        Dataset: the dynamic ``(time, y, x)`` bands plus the ``static/`` layers
        (``(y, x)``, pixel-registered). Static stays 2-D — it broadcasts over
        time on use, so no per-timestep copy is materialised in memory.
        """
        root = xr.open_zarr(str(path), chunks=chunks, consolidated=False)
        try:
            stat = xr.open_zarr(
                str(path), group="static", chunks=chunks, consolidated=False
            )
        except (OSError, KeyError, ValueError, FileNotFoundError):
            return root
        return xr.merge([root, stat], compat="override")

    def close(self) -> None:
        """Release cached catalog DataFrame."""
        self._df = None
