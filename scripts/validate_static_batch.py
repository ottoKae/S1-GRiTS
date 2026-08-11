"""Validate every cataloged smonthly/static geometry pair below one cube root."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import zarr

BANDS = (
    "local_inc_angle", "inc_angle", "ls_map", "number_of_looks",
    "rtc_anf_beta0", "rtc_anf_sigma0",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    catalog = pd.read_parquet(root / "catalog.parquet")
    dynamic = catalog[catalog.product_type == "smonthly"]
    static = catalog[catalog.product_type == "static"]
    problems: list[str] = []
    checked: list[str] = []

    for _, dyn in dynamic.iterrows():
        matches = static[
            (static.tile_id == dyn.tile_id)
            & (static.flight_direction == dyn.flight_direction)
            & (static.geometry_group_id == dyn.geometry_group_id)
        ]
        if len(matches) != 1:
            problems.append(f"{dyn.geometry_group_id}: static matches={len(matches)}")
            continue
        stat = matches.iloc[0]
        dg = zarr.open_group(str(root / dyn.tile_id / Path(dyn.zarr_path)), mode="r")
        sg = zarr.open_group(str(root / stat.tile_id / Path(stat.zarr_path)), mode="r")
        shape = (dg["y"].shape[0], dg["x"].shape[0])
        bands_ok = all(b in sg and sg[b].shape == shape and sg[b].ndim == 2 for b in BANDS)
        grid_ok = (
            np.array_equal(dg["x"][:], sg["x"][:])
            and np.array_equal(dg["y"][:], sg["y"][:])
            and dg.attrs.get("transform") == sg.attrs.get("transform")
            and dg.attrs.get("grid_id") == sg.attrs.get("grid_id")
        )
        raw_ok = (
            sg.attrs.get("static_value_policy") == "raw_aligned"
            and sg.attrs.get("spatial_filter") == "none"
            and sg.attrs.get("normalization") == "none"
            and sg.attrs.get("temporal_composite") == "none"
        )
        height, width = shape
        finite = any(
            np.isfinite(
                sg["local_inc_angle"][y:min(y + 64, height), x:min(x + 64, width)]
            ).any()
            for y in range(0, height, 1024)
            for x in range(0, width, 1024)
        )
        geometry_ok = sg.attrs.get("geometry_group_id") == dyn.geometry_group_id
        if not (bands_ok and grid_ok and raw_ok and finite and geometry_ok):
            problems.append(
                f"{dyn.geometry_group_id}: bands={bands_ok} grid={grid_ok} "
                f"raw={raw_ok} finite={finite} geometry={geometry_ok}"
            )
        checked.append(str(dyn.geometry_group_id))

    print(json.dumps({
        "dynamic_records": len(dynamic),
        "static_records": len(static),
        "groups_checked": len(checked),
        "tiles": sorted(set(dynamic.tile_id)),
        "problems": problems,
    }, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
