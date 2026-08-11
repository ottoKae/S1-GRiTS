"""Sample static-layer quality and exercise pixel-aligned ML patch reads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from s1grits.resolver import CubeResolver

STATIC_BANDS = (
    "local_inc_angle", "inc_angle", "ls_map", "number_of_looks",
    "rtc_anf_beta0", "rtc_anf_sigma0",
)


def _sample_stats(array, step=1024, block=128):
    values, gradients = [], []
    height, width = array.shape
    sampled = finite = 0
    for y in range(0, height, step):
        for x in range(0, width, step):
            part = np.asarray(array[y:min(y + block, height), x:min(x + block, width)])
            sampled += part.size
            good = part[np.isfinite(part)]
            finite += good.size
            if good.size:
                values.append(good.astype(np.float64, copy=False))
            if part.shape[1] > 1:
                dx = np.abs(np.diff(part, axis=1))
                gradients.append(dx[np.isfinite(dx)].astype(np.float64, copy=False))
            if part.shape[0] > 1:
                dy = np.abs(np.diff(part, axis=0))
                gradients.append(dy[np.isfinite(dy)].astype(np.float64, copy=False))
    vals = np.concatenate(values) if values else np.empty(0)
    grads = np.concatenate(gradients) if gradients else np.empty(0)
    return {
        "sample_count": sampled,
        "finite_fraction": finite / sampled if sampled else 0.0,
        "min": float(np.min(vals)) if vals.size else None,
        "p01": float(np.quantile(vals, 0.01)) if vals.size else None,
        "median": float(np.median(vals)) if vals.size else None,
        "p99": float(np.quantile(vals, 0.99)) if vals.size else None,
        "max": float(np.max(vals)) if vals.size else None,
        "gradient_p99": float(np.quantile(grads, 0.99)) if grads.size else None,
        "unique": sorted(float(v) for v in np.unique(vals))[:32]
        if vals.size and np.unique(vals).size <= 32 else None,
    }


def _quality_problems(band, stats):
    if not stats["finite_fraction"]:
        return [f"{band}: no finite sampled pixels"]
    lo, hi = stats["min"], stats["max"]
    problems = []
    if band == "local_inc_angle" and not (0 <= lo <= hi <= 180):
        problems.append("local_inc_angle: angle outside [0, 180]")
    if band == "inc_angle" and not (0 <= lo <= hi <= 90):
        problems.append("inc_angle: angle outside [0, 90]")
    if band == "number_of_looks" and lo < 0:
        problems.append("number_of_looks: negative value")
    if band.startswith("rtc_anf_") and lo < 0:
        problems.append(f"{band}: negative normalization factor")
    if band == "ls_map" and stats["unique"] is None:
        problems.append("ls_map: unexpectedly many sampled classes")
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--group", action="append", required=True)
    parser.add_argument("--patches", type=int, default=5)
    parser.add_argument("--patch-size", type=int, default=64)
    args = parser.parse_args()
    root = args.root.resolve()
    resolver = CubeResolver(root)
    catalog = pd.read_parquet(root / "catalog.parquet")
    report = {"root": str(root), "groups": {}, "problems": []}
    rng = np.random.default_rng(20260811)

    for geometry_group in args.group:
        dynamic_row = catalog[
            (catalog.product_type == "smonthly")
            & (catalog.geometry_group_id == geometry_group)
        ]
        if len(dynamic_row) != 1:
            report["problems"].append(
                f"{geometry_group}: expected one smonthly record, got {len(dynamic_row)}"
            )
            continue
        row = dynamic_row.iloc[0]
        ds = resolver.open_stack(
            str(row.tile_id), ["smonthly", "static"],
            direction=str(row.flight_direction), geometry_group_id=geometry_group,
        )
        group_report = {
            "sizes": {k: int(v) for k, v in ds.sizes.items()},
            "static": {}, "ml_patches": [],
        }
        for band in STATIC_BANDS:
            stats = _sample_stats(ds[band].data)
            group_report["static"][band] = stats
            report["problems"].extend(
                f"{geometry_group}: {p}" for p in _quality_problems(band, stats)
            )

        height, width = ds.sizes["y"], ds.sizes["x"]
        patch = args.patch_size
        accepted = attempts = 0
        while accepted < args.patches and attempts < args.patches * 100:
            attempts += 1
            y0 = int(rng.integers(0, height - patch + 1))
            x0 = int(rng.integers(0, width - patch + 1))
            view = ds.isel(
                time=slice(max(0, ds.sizes["time"] - 12), None),
                y=slice(y0, y0 + patch), x=slice(x0, x0 + patch),
            )
            dynamic = np.stack([
                np.asarray(view["VV_dB"]), np.asarray(view["VH_dB"])
            ], axis=1)
            static = np.stack([np.asarray(view[b]) for b in STATIC_BANDS], axis=0)
            if np.isfinite(dynamic).mean() < 0.05 or np.isfinite(static).mean() < 0.05:
                continue
            static_time = np.broadcast_to(static, (dynamic.shape[0],) + static.shape)
            model_input = np.concatenate([dynamic, static_time], axis=1)
            coords_ok = (
                np.array_equal(view["VV_dB"].x, view["local_inc_angle"].x)
                and np.array_equal(view["VV_dB"].y, view["local_inc_angle"].y)
            )
            expected = (dynamic.shape[0], 2 + len(STATIC_BANDS), patch, patch)
            if model_input.shape != expected or not coords_ok:
                report["problems"].append(
                    f"{geometry_group}: invalid ML patch shape/coordinates"
                )
            group_report["ml_patches"].append({
                "origin": [y0, x0], "shape": list(model_input.shape),
                "dynamic_finite": float(np.isfinite(dynamic).mean()),
                "static_finite": float(np.isfinite(static).mean()),
                "coordinates_equal": bool(coords_ok),
            })
            accepted += 1
        if accepted != args.patches:
            report["problems"].append(
                f"{geometry_group}: found only {accepted}/{args.patches} usable patches"
            )
        report["groups"][geometry_group] = group_report

    print(json.dumps(report, indent=2))
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
