"""Plan, append or repair independent OPERA spatial archives."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import pandas as pd
import yaml

from s1grits.spatial_inputs import read_spatial_input
from .inventory import products_from_snapshot, query_inventory
from .state import prepare_state, job_budget, group_key, requested_static_layers


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "update", "repair"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True, help="Exclusive upper acquisition time")
    parser.add_argument("--inventory", help="Complete frozen ASF JSON snapshot, otherwise query OPERA")
    parser.add_argument("--static-inventory", help="Complete RTC-STATIC snapshot; otherwise query on initialization")
    parser.add_argument("--static-only", action="store_true", help="Plan or repair static layers of an existing point archive")
    parser.add_argument("--confirm-large-job", action="store_true")
    args = parser.parse_args(argv)
    started = time.monotonic()
    config_path = Path(args.config).resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        parser.error("config must be a mapping")
    config["operation"] = args.operation
    config["confirm_large_job"] = args.confirm_large_job
    config["protected_roots"] = [str((config_path.parent / Path(p)).resolve())
                                 for p in config.get("protected_roots", [])]
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    if start >= end:
        parser.error("start must precede end")
    config.update(start=start.isoformat(), end=end.isoformat())
    input_path = Path(config["input"]["path"])
    config["input"]["path"] = str((config_path.parent / input_path).resolve())
    output = (config_path.parent / config["output"]["base_dir"]).resolve()
    features = read_spatial_input(config)
    layers = requested_static_layers(config)
    extended = len(layers) > 1
    manifest_path = output / "manifest.json"
    current = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    static_path = output / "static/manifest.json"
    frozen_static = json.loads(static_path.read_text(encoding="utf-8")) if static_path.exists() else None
    if args.static_only and (args.operation == "update" or current is None or config["spatial"]["mode"] != "points"):
        parser.error("--static-only requires plan/repair and an existing point archive")
    if extended and current and not frozen_static and not args.static_only:
        parser.error("Adding static layers to an existing archive requires repair --static-only")
    if extended and frozen_static and layers != frozen_static["layers"] and not args.static_only:
        parser.error("Changing static layers requires repair --static-only")
    if extended and frozen_static:
        pins = config.get("static", {}).get("reference_products", {})
        for binding in frozen_static["bindings"].values():
            if (not binding["product"].get("missing_reference") and pins.get(binding["burst_id"])
                    and pins[binding["burst_id"]] != binding["product"]["product_id"]):
                raise ValueError("Cannot replace a frozen STATIC product: " + binding["burst_id"])
    static = None
    if args.static_inventory:
        static = products_from_snapshot(json.loads(Path(args.static_inventory).read_text(encoding="utf-8")),
                                        static_roles=layers)
    elif (extended and frozen_static and set(layers) <= set(frozen_static["layers"])
          and all(not b["product"].get("missing_reference") for b in frozen_static["bindings"].values())):
        static = list({b["product"]["product_id"]: b["product"]
                       for b in frozen_static["bindings"].values()}.values())
    if args.static_only:
        from .point_static import plan_static, run_static
        if static is None:
            static = products_from_snapshot(query_inventory(features.geometry.union_all(), None, None, static=True),
                                            static_roles=layers)
        result = (plan_static(config, features, output, static_products=static) if args.operation == "plan" else
                  run_static(config, features, output, static_products=static, repair=True))
        result["command_elapsed_seconds"] = time.monotonic() - started
        print(json.dumps(result, ensure_ascii=True, default=str, allow_nan=False))
        return 0 if result["status"] in {"planned", "complete", "complete_with_gaps"} else 2
    if args.inventory:
        snapshot = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    else:
        snapshot = query_inventory(features.geometry.union_all(), start.isoformat(), end.isoformat())
    all_products = products_from_snapshot(snapshot)
    products = [p for p in all_products if start <= pd.Timestamp(p["acquisition_utc"]) < end]
    selection = config.get("source", {})
    if selection.get("burst_ids"):
        wanted = {str(b).upper().replace("_", "-") for b in selection["burst_ids"]}
        products = [p for p in products if p["burst_id"] in wanted]
    if selection.get("relative_orbits"):
        products = [p for p in products if p.get("relative_orbit") in selection["relative_orbits"]]
    if static is None:
        static = (products_from_snapshot(query_inventory(features.geometry.union_all(), None, None, static=True),
                                         static_roles=layers) if current is None and products else [])
    elif extended and not args.static_inventory and {p["burst_id"] for p in products} - {p["burst_id"] for p in static}:
        discovered = products_from_snapshot(query_inventory(features.geometry.union_all(), None, None, static=True),
                                            static_roles=layers)
        static = list({p["product_id"]: p for p in discovered + static}.values())
    references = _static_references(products, current, static, config) if extended else static
    state = prepare_state(config, features, products, output, references)
    budget = job_budget(state, state.get("products", []) + products, config)
    if args.operation == "plan":
        result = {"status": "planned", "contract_id": state["contract_id"],
                  "units": state["units"], "groups": state["groups"],
                  "products": len(products), "budget": budget}
        if extended:
            if current:
                from .point_static import plan_static
                result["static"] = plan_static(config, features, output, static_products=static)
            else:
                assets = {a["asset_id"]: a for p in references for role, a in p["assets"].items() if role in layers}
                result["static"] = {"layers": layers, "unique_assets": len(assets),
                                    "sample_jobs_upper": len(features) * len(references) * len(layers),
                                    "full_asset_bytes_reference": sum(a["expected_bytes"] for a in assets.values())}
    else:
        if config["spatial"]["mode"] == "points":
            from .points import run_points
            result = run_points(config, features, products, output, repair=args.operation == "repair", static_products=references)
            if extended:
                from .point_static import run_static
                auxiliary = run_static(config, features, output, static_products=static, repair=args.operation == "repair")
                dynamic_status = ("repair_required" if result["repair_required"] else "partial"
                                  if result["failed_rows"] or result["retained_after_failed_repair"] else "complete")
                result = {"status": dynamic_status if dynamic_status != "complete" else auxiliary["status"],
                          "dynamic_status": dynamic_status, "static_status": auxiliary["status"],
                          "dynamic_status_scope": "selected_rows_and_pending_repairs",
                          "legacy_result": result, "static": auxiliary}
        else:
            from .regions import run_regions
            result = run_regions(config, features, products, output, repair=args.operation == "repair", static_products=static)
    result["command_elapsed_seconds"] = time.monotonic() - started
    print(json.dumps(result, ensure_ascii=True, default=str, allow_nan=False))
    return 0 if result.get("status") in {"planned", "complete", "complete_with_gaps", "completed", "passed", "no_change", "no_source"} else 2


def _static_references(products, current, static_products, config):
    """Select new-group references before legacy state preparation freezes one."""
    old_groups = (current or {}).get("groups", {})
    pins = config.get("static", {}).get("reference_products", {})
    selected = {}
    for product in products:
        burst, group = product["burst_id"], group_key(product)
        sources = old_groups.get(group, {}).get("sources", [])
        frozen = next((s.get("static_product_id") for s in sources if s["burst_id"] == burst), None)
        if frozen and pins.get(burst) and pins[burst] != frozen:
            raise ValueError("Cannot replace a frozen STATIC product: " + burst)
        if group in old_groups:
            continue
        candidates = [p for p in static_products if p["burst_id"] == burst]
        inherited = {s["static_product_id"] for g in old_groups.values() for s in g["sources"]
                     if s["burst_id"] == burst and s.get("static_product_id")}
        pid = pins.get(burst) or (next(iter(inherited)) if len(inherited) == 1 else None)
        if pid:
            candidates = [p for p in candidates if p["product_id"] == pid]
            if not candidates:
                raise ValueError("Selected STATIC product absent from inventory: " + pid)
        if len(candidates) > 1:
            raise ValueError("Multiple STATIC products; set static.reference_products for " + burst)
        if candidates:
            selected[candidates[0]["product_id"]] = candidates[0]
    return list(selected.values())


if __name__ == "__main__":
    raise SystemExit(main())
