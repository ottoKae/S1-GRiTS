"""
product_registry.py
====================
Configurable product type registry for S1-GRiTS.

Registry defines STRUCTURAL metadata only:
  - collection_id  → STAC collection identity
  - time_varying    → static vs time-series
  - array_dims      → Zarr dimension layout
  - derived_from    → minimum guaranteed bands (for validator)

Actual bands are determined by workflow config (features_ratio, features_rvi,
features_glcm, processing.bands), NOT by the registry.
The catalog.parquet ``bands`` column is the authoritative source.

Loading priority:
  1. Workflow config ``metadata.product_config`` field
  2. Default project path ``config/s1grits_products.yaml``
  3. Built-in ``DEFAULT_REGISTRY``

Usage::

    from s1grits.product_registry import ProductRegistry, load_product_registry

    registry = load_product_registry(workflow_config=config)
    spec = registry.get("scenes")
    record["collection_id"] = spec.collection_id
    record["bands"] = actual_bands   # from workflow, not registry
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Product type specification
# ---------------------------------------------------------------------------

@dataclass
class ProductSpec:
    """Structural metadata for one product type.  No bands — those come from
    workflow config / catalog.parquet."""

    product_type: str
    collection_id: str
    time_varying: bool
    array_dims: list[str]
    derived_from: list[str] = field(default_factory=list)
    """Minimum guaranteed bands.  Validator checks catalog.bands includes these."""
    variant_fields: list[str] = field(default_factory=list)
    """Workflow config dotted-paths that affect product semantics.
    Used to compute processing_signature and product_variant at runtime."""


# ---------------------------------------------------------------------------
# Built-in default — mirrors config/s1grits_products.yaml
# ---------------------------------------------------------------------------

DEFAULT_REGISTRY: dict[str, dict[str, Any]] = {
    "schema_version": 3,
    "products": {
        "scenes": {
            "collection_id": "s1grits-scenes",
            "time_varying": True,
            "array_dims": ["time", "y", "x"],
            "derived_from": ["VV_dB", "VH_dB"],
            "variant_fields": [
                "processing.spatial_despeckle",
                "processing.despeckle_method",
                "processing.despeckle_strength",
                "processing.features_ratio",
                "processing.features_rvi",
                "processing.features_glcm",
            ],
        },
        "smonthly": {
            "collection_id": "s1grits-smonthly",
            "time_varying": True,
            "array_dims": ["time", "y", "x"],
            "derived_from": ["VV_dB", "VH_dB"],
            "variant_fields": [
                "processing.spatial_despeckle",
                "processing.despeckle_method",
                "processing.despeckle_strength",
                "processing.features_ratio",
                "processing.features_rvi",
                "processing.features_glcm",
                "processing.monthly.composite_method",
            ],
        },
        "static": {
            "collection_id": "s1grits-static",
            "time_varying": False,
            "array_dims": ["y", "x"],
            "derived_from": [
                "local_inc_angle",
            ],
            "variant_fields": [
                "processing.bands",
            ],
        },
        "monthly": {
            "collection_id": "s1grits-monthly",
            "time_varying": True,
            "array_dims": ["time", "y", "x"],
            "derived_from": ["VV_dB", "VH_dB"],
            "variant_fields": [
                "processing.monthly_despeckle",
                "processing.despeckle_method",
                "processing.composite_method",
                "processing.features_ratio",
                "processing.features_rvi",
            ],
        },
    },
}

DEFAULT_CONFIG_PATH = "config/s1grits_products.yaml"


# ---------------------------------------------------------------------------
# ProductRegistry
# ---------------------------------------------------------------------------

class ProductRegistry:
    """
    Registry of S1-GRiTS product type structural metadata.

    Provides collection_id, time_varying, array_dims, and derived_from
    (minimum guaranteed bands).  Does NOT define the full band list —
    that is driven by workflow config and stored in catalog.parquet.
    """

    def __init__(self, config_path: str | Path | None = None):
        self._config_path: str | None = None
        self._products: dict[str, ProductSpec] = {}

        if config_path is not None:
            _path = Path(config_path)
            if not _path.exists():
                raise FileNotFoundError(
                    f"Product registry not found: {_path}. "
                    f"Provide a valid path or omit to use defaults."
                )
            self._load_from(_path)
        else:
            _default = Path(DEFAULT_CONFIG_PATH)
            if _default.exists():
                self._load_from(_default)
            else:
                self._load_dict(DEFAULT_REGISTRY)

    # ---- internal ----

    def _load_from(self, path: Path) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict) or "products" not in data:
            raise ValueError(
                f"Invalid product registry in {path}: "
                f"expected a dict with a 'products' key."
            )
        self._load_dict(data)
        self._config_path = str(path)

    def _load_dict(self, data: dict[str, Any]) -> None:
        products = data.get("products", {})
        self._products = {
            key: ProductSpec(
                product_type=key,
                collection_id=val["collection_id"],
                time_varying=val.get("time_varying", True),
                array_dims=val.get("array_dims", ["time", "y", "x"]),
                derived_from=val.get("derived_from", []),
                variant_fields=val.get("variant_fields", []),
            )
            for key, val in products.items()
        }

    # ---- public ----

    @property
    def config_path(self) -> str | None:
        return self._config_path

    @property
    def products(self) -> dict[str, ProductSpec]:
        return self._products

    def get(self, product_type: str) -> ProductSpec:
        """Return ProductSpec for a product type.  Raises KeyError if unknown."""
        if product_type not in self._products:
            raise KeyError(
                f"Unknown product_type '{product_type}'. "
                f"Known types: {sorted(self._products.keys())}"
            )
        return self._products[product_type]

    def collection_id(self, product_type: str) -> str:
        return self.get(product_type).collection_id

    def derived_from(self, product_type: str) -> list[str]:
        """Return minimum guaranteed bands that catalog.bands should contain."""
        return self.get(product_type).derived_from

    def is_time_varying(self, product_type: str) -> bool:
        return self.get(product_type).time_varying

    def array_dims(self, product_type: str) -> list[str]:
        return self.get(product_type).array_dims

    def list_product_types(self) -> list[str]:
        return sorted(self._products.keys())

    def is_valid(self, product_type: str) -> bool:
        return product_type in self._products


# ---------------------------------------------------------------------------
# Convenience loader
# ---------------------------------------------------------------------------

def load_product_registry(
    workflow_config: dict[str, Any] | None = None,
) -> ProductRegistry:
    """
    Load ProductRegistry respecting priority:

    1. *workflow_config* ``metadata.product_config``
    2. Default project path ``config/s1grits_products.yaml``
    3. Built-in ``DEFAULT_REGISTRY``
    """
    if workflow_config:
        meta = workflow_config.get("metadata", {})
        meta_path = meta.get("product_config")
        if meta_path:
            return ProductRegistry(meta_path)
    return ProductRegistry()
