"""Layered product registry: built-ins + file overlay + inline overlay.

Locks the decoupling contract: the effective registry is a pure function of
(package version + workflow config), never the current working directory. A
``config/s1grits_products.yaml`` in the CWD is ignored entirely (the v2.3.x
auto-load transition path was removed in v3.0.0).
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from s1grits.product_registry import (  # noqa: E402
    DEFAULT_REGISTRY,
    ProductRegistry,
    load_product_registry,
)

BUILTIN_TYPES = {"scenes", "smonthly", "static", "monthly"}


# ---------------------------------------------------------------------------
# Layer 1: built-ins, CWD-independent
# ---------------------------------------------------------------------------

def test_no_config_yields_builtins_regardless_of_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # empty dir: nothing to probe
    reg = load_product_registry()
    assert set(reg.list_product_types()) == BUILTIN_TYPES
    assert reg.collection_id("scenes") == "s1grits-scenes"


def test_noarg_constructor_never_probes_cwd(tmp_path, monkeypatch):
    """ProductRegistry() is exactly the built-ins even when a (different)
    config/s1grits_products.yaml sits in the CWD."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "s1grits_products.yaml").write_text(
        yaml.safe_dump({"products": {"alien": {"collection_id": "x"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    reg = ProductRegistry()
    assert set(reg.list_product_types()) == BUILTIN_TYPES


# ---------------------------------------------------------------------------
# Layer 3: inline metadata.products overlay
# ---------------------------------------------------------------------------

def test_inline_overlay_adds_custom_product():
    cfg = {"metadata": {"products": {
        "my_flood_cube": {
            "collection_id": "myproj-flood",
            "derived_from": ["VV_dB"],
        },
    }}}
    reg = load_product_registry(cfg)
    assert set(reg.list_product_types()) == BUILTIN_TYPES | {"my_flood_cube"}
    spec = reg.get("my_flood_cube")
    assert spec.collection_id == "myproj-flood"
    assert spec.time_varying is True                 # sensible default
    assert spec.array_dims == ["time", "y", "x"]     # sensible default
    # built-ins untouched
    assert reg.get("scenes").collection_id == "s1grits-scenes"


def test_inline_overlay_field_merges_builtin_product():
    cfg = {"metadata": {"products": {
        "scenes": {"variant_fields": ["processing.spatial_despeckle"]},
    }}}
    reg = load_product_registry(cfg)
    spec = reg.get("scenes")
    assert spec.variant_fields == ["processing.spatial_despeckle"]  # overridden
    assert spec.collection_id == "s1grits-scenes"                   # inherited
    assert spec.derived_from == ["VV_dB", "VH_dB"]                  # inherited


def test_inline_new_product_requires_collection_id():
    cfg = {"metadata": {"products": {"nameless": {"derived_from": ["VV_dB"]}}}}
    with pytest.raises(ValueError, match="collection_id"):
        load_product_registry(cfg)


def test_inline_overlay_must_be_mapping():
    with pytest.raises(ValueError, match="expected a mapping"):
        load_product_registry({"metadata": {"products": ["scenes"]}})


# ---------------------------------------------------------------------------
# Layer 2: metadata.product_config file overlay
# ---------------------------------------------------------------------------

def _write(tmp_path, data):
    p = tmp_path / "overlay.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_file_overlay_merges_over_builtins(tmp_path):
    p = _write(tmp_path, {"schema_version": 3, "products": {
        "custom": {"collection_id": "proj-custom", "time_varying": False,
                   "array_dims": ["y", "x"]},
    }})
    reg = load_product_registry({"metadata": {"product_config": str(p)}})
    assert set(reg.list_product_types()) == BUILTIN_TYPES | {"custom"}
    assert reg.get("custom").time_varying is False
    assert reg.config_path == str(p)


def test_file_overlay_replace_true_keeps_legacy_semantics(tmp_path):
    p = _write(tmp_path, {"replace": True, "products": {
        "only": {"collection_id": "proj-only"},
    }})
    reg = load_product_registry({"metadata": {"product_config": str(p)}})
    assert reg.list_product_types() == ["only"]  # built-ins gone, as declared


def test_file_overlay_then_inline_applies_last(tmp_path):
    p = _write(tmp_path, {"products": {
        "custom": {"collection_id": "from-file"},
    }})
    cfg = {"metadata": {
        "product_config": str(p),
        "products": {"custom": {"collection_id": "from-inline"}},
    }}
    reg = load_product_registry(cfg)
    assert reg.get("custom").collection_id == "from-inline"


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_product_registry(
            {"metadata": {"product_config": str(tmp_path / "nope.yaml")}}
        )


def test_newer_schema_version_warns_but_loads(tmp_path, caplog):
    p = _write(tmp_path, {"schema_version": 99, "products": {
        "future": {"collection_id": "later"},
    }})
    with caplog.at_level("WARNING"):
        reg = load_product_registry({"metadata": {"product_config": str(p)}})
    assert reg.is_valid("future")
    assert any("schema_version 99" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Removed transition: a CWD config/s1grits_products.yaml is fully ignored
# ---------------------------------------------------------------------------

def test_cwd_file_is_ignored_with_no_config(tmp_path, monkeypatch):
    """A CWD registry file (even one that differs from the built-ins) is never
    auto-loaded — the v2.3.x transition path was removed in v3.0.0."""
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "s1grits_products.yaml").write_text(
        yaml.safe_dump({"products": {"local": {"collection_id": "cwd-local"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)  # no warning expected
        reg = load_product_registry()
    assert set(reg.list_product_types()) == BUILTIN_TYPES  # CWD file ignored


def test_cwd_file_is_ignored_with_inline_overlay(tmp_path, monkeypatch):
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "s1grits_products.yaml").write_text(
        yaml.safe_dump({"products": {"local": {"collection_id": "cwd-local"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    reg = load_product_registry(
        {"metadata": {"products": {"mine": {"collection_id": "inline"}}}}
    )
    # builtins + inline only; the CWD file plays no part
    assert set(reg.list_product_types()) == BUILTIN_TYPES | {"mine"}


# ---------------------------------------------------------------------------
# Repo hygiene: the example file must stay a byte-equal mirror
# ---------------------------------------------------------------------------

def test_repo_example_file_mirrors_builtins():
    example = _ROOT / "config" / "s1grits_products.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert data == DEFAULT_REGISTRY, (
        "config/s1grits_products.yaml (documentation mirror) drifted from "
        "s1grits.product_registry.DEFAULT_REGISTRY — update one to match the "
        "other (the built-in dict is authoritative)."
    )
