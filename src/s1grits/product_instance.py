"""
product_instance.py
====================
Pure functions for resolving a workflow config + product registry entry
into a concrete Runtime Product Instance.

This module is the bridge between:
  - Product Registry (static structural contract: variant_fields)
  - Workflow Config (runtime values: despeckle settings, features flags)
  - Catalog/STAC records (product_variant, processing_signature, bands)

Public API
----------
  resolve_variant_values(workflow_config, variant_fields) -> dict
  make_processing_signature(values) -> str  # "p" + 11 hex chars
  make_product_variant(product_type, values, actual_bands) -> str
  derive_actual_bands(workflow_config, product_type) -> list[str]
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_nested(config: dict[str, Any], dotted_path: str) -> Any:
    """Traverse a nested dict by dotted path. Returns None if any key is absent."""
    parts = dotted_path.split(".")
    current: Any = config
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _normalize_value(val: Any) -> Any:
    """
    Normalize a value for stable JSON serialization.

    - None stays None (serializes as null)
    - bool stays bool (True/False, not 1/0)
    - str: stripped and lowercased
    - list: each element normalized, order preserved
    - int/float: unchanged
    - other: converted to str
    """
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower()
    if isinstance(val, list):
        return [_normalize_value(v) for v in val]
    if isinstance(val, (int, float)):
        return val
    return str(val).strip().lower()


# ---------------------------------------------------------------------------
# resolve_variant_values
# ---------------------------------------------------------------------------

def resolve_variant_values(
    workflow_config: dict[str, Any],
    variant_fields: list[str],
) -> dict[str, Any]:
    """
    Extract the values for each variant_field from a workflow config.

    Parameters
    ----------
    workflow_config:
        Full workflow config dict (loaded from YAML).
    variant_fields:
        List of dotted paths, e.g. ["processing.spatial_despeckle",
        "processing.despeckle_method", "processing.features_ratio"].

    Returns
    -------
    dict[str, Any]
        Keys are the dotted paths, values are the resolved (raw) values.
        Missing keys map to None — they are never omitted.

    Example
    -------
    >>> cfg = {"processing": {"spatial_despeckle": True,
    ...                       "despeckle_method": "tv_bregman",
    ...                       "despeckle_strength": 5,
    ...                       "features_ratio": True,
    ...                       "features_rvi": False,
    ...                       "features_glcm": False}}
    >>> fields = ["processing.spatial_despeckle", "processing.despeckle_method",
    ...           "processing.despeckle_strength", "processing.features_ratio",
    ...           "processing.features_rvi", "processing.features_glcm"]
    >>> resolve_variant_values(cfg, fields)
    {
      "processing.spatial_despeckle": True,
      "processing.despeckle_method": "tv_bregman",
      "processing.despeckle_strength": 5,
      "processing.features_ratio": True,
      "processing.features_rvi": False,
      "processing.features_glcm": False
    }
    """
    return {field: _get_nested(workflow_config, field) for field in variant_fields}


# ---------------------------------------------------------------------------
# make_processing_signature
# ---------------------------------------------------------------------------

def make_processing_signature(values: dict[str, Any]) -> str:
    """
    Produce a stable, reproducible 12-char processing signature.

    Algorithm:
      1. Normalize all values via _normalize_value()
      2. Sort by key (keys are already deterministic dotted paths)
      3. Serialize to compact JSON (sort_keys=True, ensure_ascii=True)
      4. sha256 → take first 11 hex chars → prefix with "p"

    Parameters
    ----------
    values:
        Output of resolve_variant_values() — dict of field → value.

    Returns
    -------
    str
        12-char string like "p095eb8333c".
    """
    normalized = {k: _normalize_value(v) for k, v in sorted(values.items())}
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:11]
    return f"p{digest}"


# ---------------------------------------------------------------------------
# make_product_variant
# ---------------------------------------------------------------------------

# Abbreviation map for despeckle_method strings
_DESPECKLE_ABBREV: dict[str, str] = {
    "tv_bregman": "tvbregman",
    "tvbregman": "tvbregman",
    "lee": "lee",
    "enhanced_lee": "elee",
    "frost": "frost",
    "kuan": "kuan",
    "none": "none",
}

# Band abbreviations for static product variant
_BAND_ABBREV: dict[str, str] = {
    "local_inc_angle": "lia",
    "inc_angle": "ia",
    "ls_map": "lsmap",
    "number_of_looks": "nol",
    "rtc_anf_beta0": "beta0",
    "rtc_anf_sigma0": "sigma0",
    "rtc_anf_gamma0": "gamma0",
}


def make_product_variant(
    product_type: str,
    values: dict[str, Any],
    actual_bands: list[str],
) -> str:
    """
    Generate a human-readable product variant string.

    This is for human identification only — NOT used as a machine key.
    processing_signature is the machine-unique identifier.

    Parameters
    ----------
    product_type:
        One of "scenes", "smonthly", "monthly", "static".
    values:
        Output of resolve_variant_values().
    actual_bands:
        Output of derive_actual_bands().

    Returns
    -------
    str
        Examples:
        - "scenes_raw"
        - "scenes_tvbregman5_Ratio"
        - "scenes_tvbregman25_Ratio_RVI_GLCM"
        - "static_lia_lsmap"
        - "smonthly_tvbregman5_Ratio_median"
        - "monthly_raw_Ratio"
    """
    if product_type == "static":
        abbrevs = [
            _BAND_ABBREV.get(b.lower(), b.lower())
            for b in actual_bands
        ]
        return "static_" + "_".join(abbrevs)

    # For time-varying products: scenes / smonthly / monthly
    despeckle = values.get("processing.spatial_despeckle") or \
                values.get("processing.monthly_despeckle")
    method_raw = values.get("processing.despeckle_method") or ""
    strength = values.get("processing.despeckle_strength")

    parts = [product_type]

    if despeckle:
        method_abbrev = _DESPECKLE_ABBREV.get(
            str(method_raw).strip().lower(), str(method_raw).strip().lower()
        )
        if strength is not None:
            parts.append(f"{method_abbrev}{strength}")
        else:
            parts.append(method_abbrev)
    else:
        parts.append("raw")

    # Append band feature suffixes
    if "Ratio" in actual_bands:
        parts.append("Ratio")
    if "RVI" in actual_bands:
        parts.append("RVI")
    if any("GLCM" in b or "glcm" in b or b.startswith("glcm_") for b in actual_bands):
        parts.append("GLCM")

    # For smonthly: append composite method
    if product_type == "smonthly":
        composite = values.get("processing.monthly.composite_method") or \
                    values.get("processing.composite_method")
        if composite:
            parts.append(str(composite).strip().lower())

    # For monthly: append composite method
    if product_type == "monthly":
        composite = values.get("processing.composite_method")
        if composite:
            parts.append(str(composite).strip().lower())

    return "_".join(parts)


# ---------------------------------------------------------------------------
# derive_actual_bands
# ---------------------------------------------------------------------------

def derive_actual_bands(
    workflow_config: dict[str, Any],
    product_type: str,
) -> list[str]:
    """
    Derive the actual output band list from a workflow config.

    Parameters
    ----------
    workflow_config:
        Full workflow config dict.
    product_type:
        One of "scenes", "smonthly", "monthly", "static".

    Returns
    -------
    list[str]
        Ordered list of band names that will be written by the workflow.

    Notes
    -----
    - For scenes/smonthly/monthly: base bands are always ["VV_dB", "VH_dB"].
    - Optional bands are appended in the order: Ratio, RVI, GLCM bands.
    - For static: reads processing.bands from config (required field).
    """
    proc = workflow_config.get("processing", {}) or {}

    if product_type == "static":
        bands = proc.get("bands", [])
        return list(bands) if bands else []

    # Time-varying products
    bands: list[str] = ["VV_dB", "VH_dB"]

    if product_type in ("scenes", "smonthly"):
        if proc.get("features_ratio"):
            bands.append("Ratio")
        if proc.get("features_rvi"):
            bands.append("RVI")
        if proc.get("features_glcm"):
            glcm_features = proc.get("glcm_features") or []
            if glcm_features:
                bands.extend(glcm_features)
            else:
                # Default GLCM band name when list not specified
                bands.append("GLCM_contrast")

    elif product_type == "monthly":
        monthly = proc.get("monthly", {}) or {}
        if proc.get("features_ratio") or monthly.get("features_ratio"):
            bands.append("Ratio")
        if proc.get("features_rvi") or monthly.get("features_rvi"):
            bands.append("RVI")

    return bands
