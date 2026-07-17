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

Layered loading (the effective registry is a pure function of the package
version + the workflow config — never of the current working directory):

  1. Built-in ``DEFAULT_REGISTRY`` (in-package, the single source of truth)
  2. Optional registry FILE overlay: ``metadata.product_config: path.yaml``
     — its ``products:`` entries are merged over the built-ins per product
     (add new product types, or field-merge into a built-in one). A file
     declaring top-level ``replace: true`` keeps the legacy wholesale-
     replacement semantics instead.
  3. Optional INLINE overlay: ``metadata.products: {...}`` directly in the
     workflow YAML — same shape as a file's ``products:`` mapping, merged
     last. Lets an external project define its own Data Cube in one
     self-contained workflow config, with no second file and no dependency
     on this repository's config tree.

DEPRECATED (removal in v3.0.0): when neither override is given and a
``config/s1grits_products.yaml`` exists relative to the CURRENT WORKING
DIRECTORY, it is still loaded with the legacy replace semantics, with a
loud deprecation warning. This CWD probing is the behaviour being retired —
it made the effective registry depend on where the process was launched.

Usage::

    from s1grits.product_registry import ProductRegistry, load_product_registry

    registry = load_product_registry(workflow_config=config)
    spec = registry.get("scenes")
    record["collection_id"] = spec.collection_id
    record["bands"] = actual_bands   # from workflow, not registry
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from s1grits.logger_config import get_logger

logger = get_logger(__name__)

# The schema generation this code understands; files declaring a NEWER
# schema_version trigger a warning (fields this code doesn't know are
# ignored, which may not be what the file's author intended).
SUPPORTED_SCHEMA_VERSION = 3


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

DEFAULT_REGISTRY: dict[str, Any] = {
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
            # Explicit file = legacy wholesale-replace semantics (kept for
            # direct constructor callers). The layered merge behaviour lives
            # in load_product_registry().
            _path = Path(config_path)
            if not _path.exists():
                raise FileNotFoundError(
                    f"Product registry not found: {_path}. "
                    f"Provide a valid path or omit to use defaults."
                )
            self._load_from(_path)
        else:
            # No CWD probing here: the no-arg registry is exactly the
            # built-ins, wherever the process was launched from.
            self._load_dict(DEFAULT_REGISTRY)

    @classmethod
    def from_builtin(cls) -> "ProductRegistry":
        """The in-package default registry (layer 1)."""
        return cls()

    def apply_overlay(self, products: dict[str, Any], source: str = "overlay") -> None:
        """Merge a ``products:``-shaped mapping over this registry.

        Per product type: an unknown key ADDS a new product (and must carry
        at least ``collection_id``); a known key FIELD-MERGES into the
        existing spec, so an overlay can tweak e.g. ``variant_fields`` of a
        built-in product without restating the rest. Lists replace wholesale.
        """
        if not isinstance(products, dict):
            raise ValueError(
                f"Invalid product overlay from {source}: expected a mapping "
                f"of product_type -> spec, got {type(products).__name__}"
            )
        for key, val in products.items():
            if not isinstance(val, dict):
                raise ValueError(
                    f"Invalid product '{key}' from {source}: expected a "
                    f"mapping of spec fields, got {type(val).__name__}"
                )
            _unknown = set(val) - {
                "collection_id", "time_varying", "array_dims",
                "derived_from", "variant_fields",
            }
            if _unknown:
                logger.warning(
                    "[ProductRegistry] product '%s' from %s carries fields "
                    "this version does not read: %s",
                    key, source, sorted(_unknown),
                )
            if key in self._products:
                base = self._products[key]
                self._products[key] = ProductSpec(
                    product_type=key,
                    collection_id=val.get("collection_id", base.collection_id),
                    time_varying=val.get("time_varying", base.time_varying),
                    array_dims=val.get("array_dims", base.array_dims),
                    derived_from=val.get("derived_from", base.derived_from),
                    variant_fields=val.get("variant_fields", base.variant_fields),
                )
            else:
                if "collection_id" not in val:
                    raise ValueError(
                        f"New product '{key}' from {source} must define at "
                        f"least 'collection_id' (got fields: {sorted(val)})"
                    )
                self._products[key] = ProductSpec(
                    product_type=key,
                    collection_id=val["collection_id"],
                    time_varying=val.get("time_varying", True),
                    array_dims=val.get("array_dims", ["time", "y", "x"]),
                    derived_from=val.get("derived_from", []),
                    variant_fields=val.get("variant_fields", []),
                )

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

def _read_registry_file(path: str | Path) -> dict[str, Any]:
    """Read + sanity-check a registry/overlay YAML."""
    _path = Path(path)
    if not _path.exists():
        raise FileNotFoundError(
            f"Product registry not found: {_path}. "
            f"Provide a valid metadata.product_config path or omit it."
        )
    with open(_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "products" not in data:
        raise ValueError(
            f"Invalid product registry in {_path}: "
            f"expected a dict with a 'products' key."
        )
    ver = data.get("schema_version")
    if ver is not None and int(ver) > SUPPORTED_SCHEMA_VERSION:
        logger.warning(
            "[ProductRegistry] %s declares schema_version %s but this "
            "s1grits understands %s — unknown fields will be ignored.",
            _path, ver, SUPPORTED_SCHEMA_VERSION,
        )
    return data


def load_product_registry(
    workflow_config: dict[str, Any] | None = None,
) -> ProductRegistry:
    """Build the effective registry from the layered configuration.

    1. Built-in ``DEFAULT_REGISTRY`` (always the base).
    2. ``metadata.product_config`` file overlay — per-product merge over the
       built-ins; a file with top-level ``replace: true`` replaces them
       instead (the legacy semantics of this key).
    3. ``metadata.products`` inline overlay — merged last, so a single
       workflow YAML can define a fully custom Data Cube.

    DEPRECATED transition path (removal in v3.0.0): with neither override
    set, a ``config/s1grits_products.yaml`` under the current working
    directory is still auto-loaded with legacy replace semantics.
    """
    meta = (workflow_config or {}).get("metadata") or {}
    meta_path = meta.get("product_config")
    inline = meta.get("products")

    if meta_path:
        data = _read_registry_file(meta_path)
        if data.get("replace"):
            registry = ProductRegistry(meta_path)
        else:
            registry = ProductRegistry.from_builtin()
            registry.apply_overlay(data.get("products", {}), source=str(meta_path))
            registry._config_path = str(meta_path)
    elif inline is None and Path(DEFAULT_CONFIG_PATH).exists():
        data = _read_registry_file(DEFAULT_CONFIG_PATH)
        if data == DEFAULT_REGISTRY:
            # The repo's own example file, unedited: a byte-equal mirror of
            # the built-ins. Nothing changes for this run when the CWD
            # probing is removed, so no warning noise.
            registry = ProductRegistry.from_builtin()
        else:
            warnings.warn(
                "Auto-loading config/s1grits_products.yaml from the current "
                "working directory is deprecated and will be removed in "
                "v3.0.0. Point metadata.product_config at the file "
                "explicitly, or define overrides inline under "
                "metadata.products (built-in defaults ship in the package).",
                DeprecationWarning,
                stacklevel=2,
            )
            logger.warning(
                "[ProductRegistry] DEPRECATED: auto-loaded ./%s from the "
                "current working directory (legacy replace semantics). This "
                "CWD probing is removed in v3.0.0 — use "
                "metadata.product_config or metadata.products instead.",
                DEFAULT_CONFIG_PATH,
            )
            registry = ProductRegistry(DEFAULT_CONFIG_PATH)
    else:
        registry = ProductRegistry.from_builtin()

    if inline is not None:
        registry.apply_overlay(inline, source="metadata.products")
    return registry
