"""Bind spatial exports to verified, read-only provenance companions."""
from __future__ import annotations

from pathlib import Path


_FIELDS = ("units", "data_domain", "aggregation_domain", "aggregation_method", "n_obs_definition")


def enrich_sources(config, sources):
    """Enrich selected layers in place and return their task-lifetime view, if any."""
    options = config["source"]
    data_root = Path(options.get("dataset_root") or options["root"]).resolve()
    metadata_root = Path(options.get("metadata_root") or data_root).resolve()
    if not options.get("metadata_root") and not (metadata_root / "provenance").exists():
        return None
    try:
        import grits_resolver
    except ModuleNotFoundError as exc:
        if exc.name == "grits_resolver":
            raise ValueError("Provenance metadata was selected but GRiTS-Resolver with ProvenanceView is unavailable; "
                             "install the compatible reader documented in docs/spatial-products.md.") from exc
        raise
    if not hasattr(grits_resolver, "ProvenanceView"):
        raise ValueError("Spatial provenance requires GRiTS-Resolver with ProvenanceView; "
                         "the installed resolver is incompatible. See docs/spatial-products.md.")
    if (data_root / "zarr.json").is_file() or (data_root / ".zgroup").is_file():
        raise ValueError("Set source.dataset_root to the provenance dataset root when source.root is a Zarr store")
    view = grits_resolver.ProvenanceView(data_root, metadata_root=metadata_root, required=True)
    for source in sources:
        requested = (config.get("variables") or source["variables"]) if config.get("spatial", {}).get("mode") == "points" else source["variables"]
        names = sorted(name for name in requested if name in source["variables"])
        evidence = {"metadata_root": str(metadata_root), "data_root": str(data_root),
                    "metadata_release_id": view.pointer["metadata_release_id"],
                    "data_revision": view.data_revision, "bindings": []}
        source.setdefault("metadata_evidence", {})["provenance"] = evidence
        for time in source["times"]:
            info = view.explain(store_path=source["path"], zarr_group=source.get("zarr_group", ""),
                                time_index=time["time_index"])
            if "numeric_processing" not in info:
                raise ValueError("The resolver provenance API is incompatible; see docs/spatial-products.md")
            evidence["verification_scope"] = info["verification_scope"]
            time["variable_semantics"] = {}
            for name in names:
                meta = source["variables"][name]
                semantics = {field: meta.get(field) or source.get(field) or "unknown" for field in _FIELDS}
                matches = [match["output"] for match in info["outputs"] if name in match["output"]["variable_scope"]]
                if len(matches) > 1:
                    raise ValueError(f"Ambiguous provenance output for {source['path']} / {time['time_index']} / {name}")
                if matches:
                    row = matches[0]
                    identity = {key: row[key] for key in ("output_id", "generation_id")}
                    if "output_id" in time and any(time.get(key) != value for key, value in identity.items()):
                        raise ValueError("Variables in one selected layer bind to different provenance outputs")
                    time.update(identity)
                    additions, binding = _bound_semantics(view, info, row, name)
                    for field, value in list(additions.items()):
                        declared = semantics.get(field, meta.get(field))
                        if value in (None, "unknown") and declared not in (None, "unknown"):
                            additions.pop(field)
                            continue
                        if declared not in (None, "unknown") and _normalized(field, declared) != _normalized(field, value):
                            raise ValueError(f"Source declaration conflicts with provenance for {name}.{field}: {declared!r} != {value!r}")
                    semantics.update(additions)
                    semantics["metadata_evidence"] = {**binding, "metadata_release_id": evidence["metadata_release_id"],
                                                       "data_revision": view.data_revision}
                    evidence["bindings"].append({"time_index": time["time_index"], **semantics["metadata_evidence"]})
                time["variable_semantics"][name] = semantics
        if evidence["bindings"]:
            evidence["bindings"].sort(key=lambda item: (item["time_index"], item["variable"]))
            source["data_revision"] = view.data_revision
        _promote_consistent(source, names)
    view.assert_current(stores=[source["path"] for source in sources])
    return view


def _bound_semantics(view, info, row, name):
    binding = {key: row[key] for key in ("output_id", "generation_id", "store_path", "zarr_group")}
    binding.update(variable=name, scope="output_generation_variable", basis="artifact_observed",
                   reference=row.get("evidence_ref"))
    numeric = next(item["record"] for item in info["numeric_processing"] if item["output_id"] == row["output_id"])
    if numeric is not None and name in numeric["output"]:
        output = numeric["output"][name]
        semantics = {"units": output["unit"], "data_domain": output["domain"],
                     "physical_quantity": output["physical_quantity"] or "unknown"}
        if name == "n_obs":
            counts = [step for step in numeric["steps"] if step["variable"] == name
                      and step["operation"] == "observation_count" and step["status"] == "executed"]
            definitions = {output["physical_quantity"] + "; method=" + step["parameters"].get("method", "sum_isfinite")
                           + "; count_unit=" + step["parameters"].get("count_unit", "aligned_scene_stack_entry")
                           + "; dtype=" + output["dtype"] for step in counts}
            if len(definitions) != 1:
                raise ValueError("Numerical processing receipt declares inconsistent observation counts")
            semantics["n_obs_definition"] = next(iter(definitions))
        composites = [step for step in numeric["steps"] if step["variable"] == name
                      and step["operation"] == "monthly_composite" and step["status"] == "executed"]
        for field, values in (("aggregation_method", {step["parameters"]["method"] for step in composites}),
                              ("aggregation_domain", {step["input_domain"] for step in composites})):
            if len(values) > 1:
                raise ValueError("One numerical processing receipt declares inconsistent " + field)
            if values:
                semantics[field] = next(iter(values))
        binding.update(basis="execution_receipt", numeric_scope=numeric["scope"])
        return semantics, binding

    variables = [record for record in info["variables"] if record["store_path"] == row["store_path"]
                 and record["zarr_group"] == row["zarr_group"] and record["variable"] == name]
    if len(variables) > 1:
        raise ValueError("Provenance contains duplicate variable annotations")
    semantics, references = {}, []
    if variables:
        annotation = variables[0]
        scope = annotation.get("historical_output_generations", {})
        if row["output_id"] in scope and scope[row["output_id"]] == row["generation_id"]:
            if annotation.get("units"):
                semantics["units"] = annotation["units"]
            if annotation.get("historical_processing_ref"):
                references.append((annotation["historical_processing_ref"], annotation.get("historical_processing_basis"), True))
    for record in info["evidence"].values():
        if not isinstance(record, dict) or record.get("store_path") != row["store_path"]:
            continue
        context = record.get("historical_context", {})
        scope = context.get("output_generations", {})
        if row["output_id"] in scope and scope[row["output_id"]] == row["generation_id"] and context.get("historical_processing_ref"):
            references.append((context["historical_processing_ref"], None, False))
    historical = []
    for reference, annotation_basis, variable_scoped in references:
        record = view.snapshot["evidence"][reference.removeprefix("evidence/")]
        # These explicit keys are the historical companion contract, not a text inference.
        keys = {"n_obs_definition": "n_obs_definition"} if name == "n_obs" else {
            "aggregation_domain": "aggregation_domain", "aggregation_method": "aggregation_method"}
        if variable_scoped and name in ("VV_dB", "VH_dB"):
            keys.update(output_units="units", output_domain="data_domain", physical_quantity="physical_quantity")
        for key, field in keys.items():
            if record.get(key) is not None:
                if field in semantics and _normalized(field, semantics[field]) != _normalized(field, record[key]):
                    raise ValueError("Conflicting historical evidence for " + field)
                semantics[field] = record[key]
        entry = {"reference": reference, "basis": record.get("basis", "historical_annotation"),
                 "annotation_basis": annotation_basis,
                 "scope": "output_generation_variable" if variable_scoped else "output_generation",
                 "output_generations": {row["output_id"]: row["generation_id"]}}
        if entry not in historical:
            historical.append(entry)
    if historical:
        binding.update(basis="historical_annotation", historical=historical)
    return semantics, binding


def _promote_consistent(source, names):
    conflicts = []
    for name in names:
        for field in _FIELDS:
            values = {_normalized(field, value): value for time in source["times"]
                      for value in [time["variable_semantics"][name].get(field) or "unknown"]}
            if len(values) == 1:
                source["variables"][name][field] = next(iter(values.values()))
            elif values:
                source["variables"][name][field] = "unknown"
                conflicts.append({"variable": name, "field": field, "values": sorted(values.values())})
    backscatter = [name for name in names if name in ("VV", "VH", "VV_dB", "VH_dB")]
    for field in ("data_domain", "aggregation_domain", "aggregation_method", "n_obs_definition"):
        candidates = ["n_obs"] if field == "n_obs_definition" and "n_obs" in names else ([] if field == "n_obs_definition" else backscatter)
        values = {_normalized(field, value): value for time in source["times"] for name in candidates
                  for value in [time["variable_semantics"][name].get(field) or "unknown"]}
        if len(values) == 1:
            source[field] = next(iter(values.values()))
        elif values:
            source[field] = "unknown"
            conflicts.append({"variable": "*", "field": field, "values": sorted(values.values())})
    source["metadata_time_conflict"] = bool(conflicts)
    source["metadata_time_conflicts"] = conflicts


def _normalized(field, value):
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if field in ("units", "data_domain") and text in ("db", "decibel", "decibels"):
        return "db"
    if field == "aggregation_method" and text in ("median", "mean", "min"):
        return "nan" + text
    return text
