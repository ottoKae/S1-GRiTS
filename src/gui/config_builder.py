"""
Build a valid S1-GRiTS processing YAML config from GUI form state.

The config dict is constructed programmatically and serialised with
yaml.dump() — user strings are stored as YAML *values*, never interpolated
into the YAML structure, preventing injection.
"""

import os
import re
import tempfile
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_wkt(wkt: str) -> tuple[bool, str]:
    """
    Perform a lightweight WKT polygon sanity check.

    Returns:
        (is_valid, error_message)
    """
    wkt = wkt.strip()
    if not wkt:
        return False, "WKT polygon is required."
    if not re.match(r'^POLYGON\s*\(', wkt, re.I):
        return False, "WKT must start with POLYGON(..."
    if wkt.count("(") != wkt.count(")"):
        return False, "Unbalanced parentheses in WKT."
    return True, ""


def validate_mgrs_tiles(raw: str) -> tuple[bool, str, list[str]]:
    """
    Parse and validate a newline/comma/space-separated list of MGRS tile IDs.

    Returns:
        (is_valid, error_message, tile_list)
    """
    tiles = [t.strip().upper() for t in re.split(r'[\n,\s]+', raw) if t.strip()]
    if not tiles:
        return False, "At least one MGRS tile ID is required.", []
    bad = [t for t in tiles if not re.match(r'^[0-9]{1,2}[A-Z]{3}$', t)]
    if bad:
        return False, f"Invalid MGRS tile ID(s): {', '.join(bad)}", []
    return True, "", tiles


def validate_output_dir(path_str: str) -> tuple[bool, str]:
    """
    Check that the output path is valid and not a path traversal attempt.

    Returns:
        (is_valid, error_message)
    """
    if not path_str.strip():
        return False, "Output directory is required."
    try:
        resolved = Path(path_str.strip()).resolve()
    except Exception as exc:
        return False, f"Invalid path: {exc}"
    # Reject obvious traversal
    if ".." in Path(path_str.strip()).parts:
        return False, "Path traversal ('..') is not allowed."
    # Reject filesystem roots
    if resolved == resolved.parent:
        return False, "Output path cannot be a filesystem root."
    return True, ""


# ---------------------------------------------------------------------------
# Config builder
# ---------------------------------------------------------------------------

def build_process_config(state: dict) -> dict:
    """
    Convert GUI form state to the s1grits processing config dict.

    Args:
        state: Dictionary of form values collected from the Streamlit widgets.

    Returns:
        Full config dict compatible with processing_config_en.yaml schema.
    """
    cfg: dict = {}

    # ── ROI ───────────────────────────────────────────────────────────────
    roi: dict = {}
    # Accept both "WKT" (internal key) and "WKT polygon" (widget label)
    if state["roi_mode"] in ("WKT", "WKT polygon"):
        roi["wkt"] = state["wkt"].strip()
    else:
        roi["manual_mgrs_tiles"] = state["mgrs_tiles"]
    roi["flight_direction"] = state["flight_direction"]
    roi["polarization"] = state["polarization"]
    cfg["roi"] = roi

    # ── Time ──────────────────────────────────────────────────────────────
    time_cfg: dict = {}
    if state["time_mode"] == "Full archive":
        time_cfg["full"] = int(state["full_end_year"])
    else:
        time_cfg["years"] = [int(y) for y in state["years"]]
        if state.get("months"):
            time_cfg["months"] = [int(m) for m in state["months"]]
    cfg["time"] = time_cfg

    # ── Output ────────────────────────────────────────────────────────────
    cfg["output"] = {
        "base_dir": state["base_dir"].strip(),
        "overwrite_cog": bool(state.get("overwrite_cog", False)),
        "formats": {
            "cog":     bool(state.get("fmt_cog", True)),
            "preview": bool(state.get("fmt_preview", True)),
        },
    }

    # ── Parallel ──────────────────────────────────────────────────────────
    cfg["parallel"] = {
        "enabled":     True,
        "max_workers": int(state.get("max_workers", 4)),
    }

    # ── Memory ────────────────────────────────────────────────────────────
    max_mem = state.get("max_memory_gb", "auto")
    try:
        max_mem = float(max_mem)
    except (ValueError, TypeError):
        max_mem = "auto"

    cfg["memory"] = {
        "max_memory_gb":          max_mem,
        "batch_strategy":         state.get("batch_strategy", "auto"),
        "max_download_workers":   int(state.get("max_download_workers", 2)),
        "clear_cache_per_batch":  True,
        "scene_retry_timeout_seconds": 600,
        "batch_max_retries":      2,
        "max_failed_ratio":       0.0,
    }

    # ── Processing ────────────────────────────────────────────────────────
    despeckle_on = bool(state.get("post_processing", True))
    processing: dict = {
        "post_processing":  despeckle_on,
        "target_crs":       None,
        "target_resolution": 30.0,
        "use_roi_mask":     False,
        "mosaic_strategy":  "mean",
        "group_mode":       "minute",
        "trim_fraction":    0.15,
        "on_time_conflict": "skip",
        "min_valid_lin":    1.0e-6,
        "eps_lin":          1.0e-7,
        "despeckle": {
            "monthly_despeckle": despeckle_on,
            "method":            "tv_bregman",
            "kwargs": {
                "reg_param": float(state.get("reg_param", 5.0)),
            },
        },
        "zarr_chunks":    {"y": 1024, "x": 1024},
        "cog_block_size": 256,
        "zarr_time_fix": {
            "enabled":       True,
            "create_backup": True,
            "backup_dir":    None,
        },
        "texture_features": _build_texture(state),
    }
    cfg["processing"] = processing

    # ── Logging ───────────────────────────────────────────────────────────
    cfg["logging"] = {
        "file_level":           "DEBUG",
        "console_level":        "INFO",
        "suppress_third_party": True,
        "log_file":             "./logs/s1grits_{timestamp}.log",
    }

    return cfg


def _build_texture(state: dict) -> dict:
    """Build the texture_features section of the config."""
    enabled = bool(state.get("glcm_enabled", False))
    if not enabled:
        return {
            "enabled": False,
            "inputs":  ["VV_dB", "VH_dB"],
            "metrics": ["contrast", "homogeneity", "entropy", "correlation"],
            "window_size":     5,
            "distance":        1,
            "angles":          [0, 90],
            "average_angles":  True,
            "levels":          16,
            "vv_db_range":     [-25, 5],
            "vh_db_range":     [-32, -5],
        }

    window = int(state.get("glcm_window_size", 5))
    if window % 2 == 0:
        window += 1   # Ensure odd

    return {
        "enabled":        True,
        "inputs":         list(state.get("glcm_inputs", ["VV_dB", "VH_dB"])),
        "metrics":        list(state.get("glcm_metrics",
                               ["contrast", "homogeneity", "entropy", "correlation"])),
        "window_size":    window,
        "distance":       1,
        "angles":         [0, 90],
        "average_angles": True,
        "levels":         int(state.get("glcm_levels", 16)),
        "vv_db_range":    [-25, 5],
        "vh_db_range":    [-32, -5],
    }


# ---------------------------------------------------------------------------
# Temp file helpers
# ---------------------------------------------------------------------------

def write_temp_config(cfg: dict) -> str:
    """
    Write config dict to a temporary YAML file and return its path.

    The caller is responsible for deleting the file after use.
    """
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="s1grits_gui_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.dump(cfg, fh, default_flow_style=False, allow_unicode=True,
                      sort_keys=False)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def load_yaml_config(path_or_file) -> dict:
    """
    Safely load a YAML config from a file path or a Streamlit UploadedFile object.

    Accepts:
        path_or_file: str | Path  — local file path
                      UploadedFile — Streamlit file-uploader object (has .read())

    Uses yaml.safe_load to prevent arbitrary code execution.
    """
    # Streamlit UploadedFile exposes a .read() bytes interface
    if hasattr(path_or_file, "read"):
        content = path_or_file.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        return yaml.safe_load(content) or {}
    # Regular file path
    with open(path_or_file, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def extract_form_state_from_config(cfg: dict) -> dict:
    """
    Reverse-map a loaded YAML config dict back to GUI form state values.

    Allows users to pre-populate the form by uploading an existing config.
    """
    roi = cfg.get("roi", {})
    time_cfg = cfg.get("time", {})
    output = cfg.get("output", {})
    parallel = cfg.get("parallel", {})
    memory = cfg.get("memory", {})
    processing = cfg.get("processing", {})
    texture = processing.get("texture_features", {})
    despeckle = processing.get("despeckle", {})

    state: dict = {}

    # ROI — values must match the radio widget options exactly
    if "wkt" in roi:
        state["roi_mode"] = "WKT polygon"
        state["wkt"] = roi.get("wkt", "")
        state["mgrs_tiles"] = []
    else:
        state["roi_mode"] = "Manual MGRS tiles"
        state["wkt"] = ""
        state["mgrs_tiles"] = roi.get("manual_mgrs_tiles", [])
    state["flight_direction"] = roi.get("flight_direction", "ASCENDING")
    state["polarization"] = roi.get("polarization", "VV+VH")

    # Time
    if "full" in time_cfg:
        state["time_mode"] = "Full archive"
        # number_input expects int
        state["full_end_year"] = int(time_cfg.get("full", 2026))
        state["years"] = []
        state["months"] = []
    else:
        state["time_mode"] = "Specific years"
        state["full_end_year"] = 2026
        # Keep as int — multiselect options are list(range(2014, 2028)) integers
        state["years"] = [int(y) for y in time_cfg.get("years", [2024])]
        state["months"] = [int(m) for m in time_cfg.get("months", [])]

    # Output
    state["base_dir"] = output.get("base_dir", "./output")
    state["overwrite_cog"] = output.get("overwrite_cog", False)
    formats = output.get("formats", {})
    state["fmt_cog"] = formats.get("cog", True)
    state["fmt_preview"] = formats.get("preview", True)

    # Parallel & memory — ensure correct types for number_input/slider widgets
    state["max_workers"] = int(parallel.get("max_workers", 4))
    state["max_memory_gb"] = str(memory.get("max_memory_gb", "auto"))
    state["batch_strategy"] = memory.get("batch_strategy", "auto")
    state["max_download_workers"] = int(memory.get("max_download_workers", 2))

    # Processing — ensure bool for toggle widget
    state["post_processing"] = bool(processing.get("post_processing", True))
    state["reg_param"] = float(despeckle.get("kwargs", {}).get("reg_param", 5.0))

    # GLCM
    state["glcm_enabled"] = texture.get("enabled", False)
    state["glcm_inputs"] = texture.get("inputs", ["VV_dB", "VH_dB"])
    state["glcm_metrics"] = texture.get("metrics",
                                ["contrast", "homogeneity", "entropy", "correlation"])
    # select_slider options are [3,5,7,9,11,13,15] — snap to nearest valid odd value
    raw_win = int(texture.get("window_size", 5))
    valid_windows = [3, 5, 7, 9, 11, 13, 15]
    state["glcm_window_size"] = min(valid_windows, key=lambda v: abs(v - raw_win))
    state["glcm_levels"] = texture.get("levels", 16)

    return state
