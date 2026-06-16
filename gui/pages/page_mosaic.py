"""
Mosaic tab — s1grits mosaic --month <YYYY-MM> --direction <DIR> [options]

Layout (5 | 7 columns):
  Left  — Step-numbered form: source dir, month, direction, output options
  Right — Status, live log, output file path
"""

import re
from pathlib import Path

import streamlit as st

from gui.config_builder import validate_output_dir
from gui.runner import CommandRunner, build_cmd
from gui.styles import (
    build_cmd_preview,
    render_log_viewer,
    render_path_panel,
    render_status_badge,
    render_tip,
)
from gui.utils import open_in_explorer

from html import escape

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUNNER_KEY  = "mosaic_runner"
_LOGS_KEY    = "mosaic_logs"
_STATUS_KEY  = "mosaic_status"
_OUTDIR_KEY  = "mosaic_outdir"
_CMD_KEY     = "mosaic_cmd"
_SRC_DIR_KEY = "mosaic_src_dir"
_OUT_DIR_KEY = "mosaic_out_dir"
_SCAN_KEY    = "mosaic_scan"       # cached scan result keyed by src path
_MAX_TILES   = 100                 # tile limit per direction before blocking


# ---------------------------------------------------------------------------
# Folder picker
# ---------------------------------------------------------------------------

def _pick_folder(initial_dir: str = "") -> str:
    """Open native OS folder-picker dialog via tkinter. Returns chosen path or ''."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        start = initial_dir if initial_dir and Path(initial_dir).is_dir() else ""
        chosen = filedialog.askdirectory(
            title="Select s1grits output directory",
            initialdir=start or "/",
        )
        root.destroy()
        return chosen or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Source directory scanner
# ---------------------------------------------------------------------------

def _scan_source_dir(src: str) -> dict:
    """
    Scan the source directory and return a summary dict:
      {
        "valid": bool,           # collection.json present
        "errors": [str],         # list of error messages
        "asc_tiles": int,        # count of *_ASCENDING tile subdirs
        "desc_tiles": int,       # count of *_DESCENDING tile subdirs
        "months_asc":  [str],    # sorted YYYY-MM strings found in ASC cog/ files
        "months_desc": [str],    # sorted YYYY-MM strings found in DESC cog/ files
      }
    """
    result = {
        "valid": False,
        "errors": [],
        "asc_tiles": 0,
        "desc_tiles": 0,
        "months_asc": [],
        "months_desc": [],
    }
    if not src:
        return result

    p = Path(src)
    if not p.exists() or not p.is_dir():
        result["errors"].append("Directory does not exist.")
        return result

    # Check collection.json
    if not (p / "collection.json").exists():
        result["errors"].append(
            "collection.json not found — this directory is not a valid s1grits output."
        )
        return result

    result["valid"] = True

    # Scan tile subdirs: <TILE>_ASCENDING or <TILE>_DESCENDING
    months_asc: set  = set()
    months_desc: set = set()
    month_pat = re.compile(r'_(\d{4}-\d{2})\.tif$', re.I)

    try:
        for d in sorted(p.iterdir()):
            if not d.is_dir():
                continue
            name_upper = d.name.upper()
            if name_upper.endswith("_ASCENDING"):
                result["asc_tiles"] += 1
                cog_dir = d / "cog"
                if cog_dir.is_dir():
                    for tif in cog_dir.glob("*.tif"):
                        m = month_pat.search(tif.name)
                        if m:
                            months_asc.add(m.group(1))
            elif name_upper.endswith("_DESCENDING"):
                result["desc_tiles"] += 1
                cog_dir = d / "cog"
                if cog_dir.is_dir():
                    for tif in cog_dir.glob("*.tif"):
                        m = month_pat.search(tif.name)
                        if m:
                            months_desc.add(m.group(1))
    except PermissionError:
        result["errors"].append("Cannot read directory (permission denied).")

    result["months_asc"]  = sorted(months_asc)
    result["months_desc"] = sorted(months_desc)
    return result


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def _init_state():
    defaults = {
        _RUNNER_KEY:  CommandRunner(),
        _LOGS_KEY:    [],
        _STATUS_KEY:  "idle",
        _OUTDIR_KEY:  "",
        _CMD_KEY:     [],
        _SRC_DIR_KEY: "",
        _OUT_DIR_KEY: "",
        _SCAN_KEY:    {"_src": "", **_scan_source_dir("")},
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _get_scan(src: str) -> dict:
    """Return cached scan if src matches; otherwise re-scan and cache."""
    cached = st.session_state.get(_SCAN_KEY, {})
    if cached.get("_src") == src:
        return cached
    scan = _scan_source_dir(src)
    scan["_src"] = src
    st.session_state[_SCAN_KEY] = scan
    return scan


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render():
    _init_state()
    runner: CommandRunner = st.session_state[_RUNNER_KEY]

    if runner.is_running() or runner.status in ("running", "success", "failed", "stopped"):
        new_lines = runner.drain_logs()
        if new_lines:
            st.session_state[_LOGS_KEY].extend(new_lines)
        if not runner.is_running() and st.session_state.get(_STATUS_KEY) == "running":
            st.session_state[_STATUS_KEY] = runner.status

    col_left, col_right = st.columns([5, 7], gap="large")

    # =========================================================================
    # LEFT PANEL
    # =========================================================================
    with col_left:

        # Page title
        st.markdown(
            '<div style="margin-bottom:6px;">'
            '<span style="font-size:20px;font-weight:800;color:#134E4A;letter-spacing:-0.4px;">'
            'Create Mosaic</span>'
            '<span style="font-size:12px;font-weight:500;color:#4D7C78;margin-left:10px;">'
            's1grits mosaic</span></div>',
            unsafe_allow_html=True,
        )
        render_tip(
            "Merge all COG tiles for a given month into a single "
            "<strong>VRT</strong> or <strong>COG</strong> mosaic. "
            "Output is reprojected to a common CRS (default EPSG:4326)."
        )

        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

        # ── STEP 1: Source Directory ───────────────────────────────────────
        _step_header(1, "Source Directory", "Root folder with COG tile subdirectories")

        if st.button("Browse Folder", key="mosaic_src_browse", use_container_width=True):
            picked = _pick_folder(st.session_state.get(_SRC_DIR_KEY, ""))
            if picked:
                st.session_state[_SRC_DIR_KEY] = picked
                # Invalidate cache so scan refreshes
                st.session_state.pop(_SCAN_KEY, None)
                st.rerun()

        src_path = st.session_state.get(_SRC_DIR_KEY, "").strip()
        if src_path:
            st.markdown(
                f'<div style="background:#F0FDFA;border:1px solid #CCEBE8;border-radius:6px;'
                f'padding:6px 10px;margin:4px 0 6px 0;font-size:12px;'
                f'color:#134E4A;word-break:break-all;line-height:1.4;">'
                f'{escape(src_path)}</div>',
                unsafe_allow_html=True,
            )

        # Scan and display status
        scan = _get_scan(src_path)
        ok_src = _render_source_dir_status(scan)

        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        # ── STEP 2: Direction & Month ──────────────────────────────────────
        _step_header(2, "Direction & Month", "Required parameters")

        direction = st.radio(
            "Orbit direction",
            ["ASCENDING", "DESCENDING", "ALL"],
            horizontal=True,
            key="mosaic_direction",
            help="ALL: ASCENDING pixels take priority; DESCENDING fills NoData gaps.",
        )

        # Build available months based on selected direction
        if direction == "ASCENDING":
            available_months = scan.get("months_asc", [])
        elif direction == "DESCENDING":
            available_months = scan.get("months_desc", [])
        else:  # ALL — union of both
            available_months = sorted(
                set(scan.get("months_asc", [])) | set(scan.get("months_desc", []))
            )

        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

        if available_months:
            month = st.selectbox(
                "Month",
                options=available_months,
                index=len(available_months) - 1,  # Default to latest
                key="mosaic_month",
                help="Available months detected from COG files in the source directory.",
                format_func=lambda x: x,
            )
        else:
            # No months found — show disabled placeholder
            month = ""
            no_months_msg = (
                "No COG files found for this direction."
                if src_path and scan.get("valid")
                else "Select a valid source directory first."
            )
            st.markdown(
                f'<div style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:6px;'
                f'padding:7px 12px;font-size:12px;color:#B45309;">'
                f'<strong>[WARN]</strong> {escape(no_months_msg)}</div>',
                unsafe_allow_html=True,
            )
            # Fallback hidden widget to keep key consistent
            st.session_state["mosaic_month"] = ""

        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        # ── STEP 3: Output Options ─────────────────────────────────────────
        with st.expander("Step 3 — Output Options", expanded=True):
            st.markdown(
                '<p style="font-size:11.5px;color:#4D7C78;margin-bottom:12px;">'
                'Customize the mosaic output format, CRS, and destination.</p>',
                unsafe_allow_html=True,
            )

            # Output directory — Browse + badge
            out_path = st.session_state.get(_OUT_DIR_KEY, "").strip()
            st.markdown(
                '<div style="font-size:12px;font-weight:600;color:#134E4A;margin-bottom:4px;">'
                'Mosaic output directory <span style="font-weight:400;color:#80A8A4;">'
                '(optional)</span></div>',
                unsafe_allow_html=True,
            )
            if st.button("Browse Output Folder", key="mosaic_out_browse", use_container_width=True):
                picked = _pick_folder(st.session_state.get(_OUT_DIR_KEY, ""))
                if picked:
                    st.session_state[_OUT_DIR_KEY] = picked
                    st.rerun()
            if out_path:
                st.markdown(
                    f'<div style="background:#F0FDFA;border:1px solid #CCEBE8;border-radius:6px;'
                    f'padding:6px 10px;margin:4px 0 6px 0;font-size:12px;'
                    f'color:#134E4A;word-break:break-all;line-height:1.4;">'
                    f'{escape(out_path)}</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div style="font-size:11.5px;color:#80A8A4;font-style:italic;'
                    'margin:2px 0 6px 0;">'
                    'Leave empty to use default: analysis_results/mosaic/</div>',
                    unsafe_allow_html=True,
                )
            output_dir = out_path

            col_fmt, col_crs = st.columns(2, gap="small")
            with col_fmt:
                fmt = st.selectbox(
                    "Output format",
                    ["VRT", "COG"],
                    key="mosaic_format",
                    help="VRT: lightweight virtual raster (fast).\nCOG: self-contained GeoTiff.",
                )
            with col_crs:
                keep_utm = st.checkbox(
                    "Keep UTM (no reproject)",
                    value=False,
                    key="mosaic_keep_utm",
                    help="Skip reprojection; keep each tile in its native UTM zone.",
                )

            if not keep_utm:
                crs = st.text_input(
                    "Target CRS",
                    value="EPSG:4326",
                    key="mosaic_crs",
                    help="Any valid EPSG code, e.g. EPSG:4326 or EPSG:3857.",
                )
            else:
                crs = None

            mgrs_prefix = st.text_input(
                "MGRS prefix filter (optional)",
                value="",
                key="mosaic_mgrs_prefix",
                placeholder="50R",
                help="Restrict to tiles whose ID starts with this prefix.",
            ).strip()

        # Command preview
        cmd = st.session_state.get(_CMD_KEY, [])
        if cmd:
            st.markdown(build_cmd_preview(cmd), unsafe_allow_html=True)

        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    # =========================================================================
    # RIGHT PANEL
    # =========================================================================
    with col_right:
        status = st.session_state.get(_STATUS_KEY, "idle")
        logs   = st.session_state.get(_LOGS_KEY, [])

        _right_section_title("Actions")
        _is_running  = runner.is_running()
        _month       = st.session_state.get("mosaic_month", "")
        _src_dir     = st.session_state.get(_SRC_DIR_KEY, "").strip()
        _direction_r = st.session_state.get("mosaic_direction", "ASCENDING")
        _scan_r      = _get_scan(_src_dir)

        # Tile-count guard: block if > _MAX_TILES for the selected direction
        _asc_count  = _scan_r.get("asc_tiles", 0)
        _desc_count = _scan_r.get("desc_tiles", 0)
        _tile_limit_hit = False
        if _direction_r == "ASCENDING" and _asc_count > _MAX_TILES:
            _tile_limit_hit = True
        elif _direction_r == "DESCENDING" and _desc_count > _MAX_TILES:
            _tile_limit_hit = True
        elif _direction_r == "ALL" and (_asc_count + _desc_count) > _MAX_TILES:
            _tile_limit_hit = True

        _can_run = (
            not _is_running
            and bool(_month)
            and _scan_r.get("valid", False)
            and not _tile_limit_hit
        )

        if _tile_limit_hit:
            if _direction_r == "ASCENDING":
                _limit_count = _asc_count
            elif _direction_r == "DESCENDING":
                _limit_count = _desc_count
            else:
                _limit_count = _asc_count + _desc_count
            st.error(
                f"ERROR: {_limit_count} tiles selected for direction {_direction_r} "
                f"exceeds the {_MAX_TILES}-tile limit. "
                "Use the MGRS prefix filter (Step 3) to reduce the tile count, "
                "or process sub-regions separately."
            )

        col_run_r, col_stop_r = st.columns([3, 1])
        with col_run_r:
            if st.button(
                "Run: mosaic",
                type="primary",
                use_container_width=True,
                disabled=not _can_run,
                key="mosaic_run",
            ):
                _fmt      = st.session_state.get("mosaic_format", "VRT")
                _keep_utm = st.session_state.get("mosaic_keep_utm", False)
                _crs      = st.session_state.get("mosaic_crs", "EPSG:4326")
                _mgrs_pfx = st.session_state.get("mosaic_mgrs_prefix", "").strip()
                _out_path = st.session_state.get(_OUT_DIR_KEY, "").strip()

                kwargs: dict = {
                    "month":      _month,
                    "direction":  _direction_r,
                    "output_dir": _src_dir,   # --output-dir: source tile directory
                    "format":     _fmt,
                }
                if _out_path:
                    kwargs["output"] = _out_path   # --output: mosaic destination
                    dest = str(Path(_out_path).resolve())
                else:
                    dest = str(
                        (Path(_src_dir) / ".." / "analysis_results" / "mosaic").resolve()
                    )
                if not _keep_utm and _crs:
                    kwargs["crs"] = _crs
                elif _keep_utm:
                    kwargs["keep_utm"] = True
                if _mgrs_pfx:
                    kwargs["mgrs_prefix"] = _mgrs_pfx

                cmd = build_cmd("mosaic", **kwargs)
                st.session_state[_LOGS_KEY]   = []
                st.session_state[_STATUS_KEY] = "running"
                st.session_state[_OUTDIR_KEY] = dest
                st.session_state[_CMD_KEY]    = cmd
                try:
                    runner.run(cmd)
                except FileNotFoundError:
                    st.error(
                        "ERROR: s1grits executable not found. "
                        "Ensure s1grits is installed in the current Python environment: "
                        "pip install -e .[gui]"
                    )
                    st.session_state[_STATUS_KEY] = "failed"
        with col_stop_r:
            if st.button(
                "Stop",
                type="secondary",
                use_container_width=True,
                disabled=not _is_running,
                key="mosaic_stop_r",
            ):
                runner.stop()
                st.session_state[_STATUS_KEY] = "stopped"
                st.rerun()

        _right_section_title("Status")
        st.markdown(
            render_status_badge(status, started=runner.started_at, duration=runner.elapsed),
            unsafe_allow_html=True,
        )

        _right_section_title("Live Output")
        selected_levels = st.multiselect(
            "Log filter",
            ["DEBUG", "INFO", "WARNING", "ERROR"],
            default=["INFO", "WARNING", "ERROR"],
            key="mosaic_log_levels",
            label_visibility="collapsed",
        )
        st.components.v1.html(render_log_viewer(logs, selected_levels), height=440, scrolling=False)

        out_dir = st.session_state.get(_OUTDIR_KEY, "")
        if out_dir:
            _right_section_title("Output Paths")
            st.markdown(render_path_panel(out_dir, ""), unsafe_allow_html=True)
            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("Open Mosaic Folder", key="mosaic_open",
                             use_container_width=True):
                    open_in_explorer(out_dir)
            with col_b:
                mosaic_file = _extract_mosaic_path(logs)
                if mosaic_file:
                    if st.button("Open Mosaic File", key="mosaic_open_file",
                                 use_container_width=True):
                        open_in_explorer(mosaic_file)

        # Success result card
        if status == "success" and logs:
            mosaic_path = _extract_mosaic_path(logs)
            if mosaic_path:
                _right_section_title("Mosaic Output File")
                st.success("Mosaic created successfully.")
                st.markdown(
                    f'<div style="background:#F0FDFA;border:1.5px solid #CCEBE8;'
                    f'border-radius:10px;padding:10px 14px;'
                    f'font-family:\'JetBrains Mono\',monospace;'
                    f'font-size:12px;color:#1F6B62;word-break:break-all;">'
                    f'{mosaic_path}</div>',
                    unsafe_allow_html=True,
                )

        if status == "failed" and logs:
            _right_section_title("Error Details")
            st.error("Mosaic creation failed — check the log above.")
            with st.expander("Last 8 log lines", expanded=False):
                st.code("\n".join(logs[-8:]), language=None)

    if runner.is_running():
        st.rerun()


# ---------------------------------------------------------------------------
# Source directory status renderer
# ---------------------------------------------------------------------------

def _render_source_dir_status(scan: dict) -> bool:
    """
    Render inline status rows for the scanned source directory.
    Returns True if directory is valid and usable.
    """
    if not scan.get("_src"):
        return False

    def _row(icon: str, color: str, text: str) -> str:
        return (
            f'<div style="display:flex;align-items:center;gap:6px;'
            f'font-size:11.5px;color:{color};margin-top:3px;">'
            f'<span style="font-weight:700;">{icon}</span>{escape(text)}</div>'
        )

    rows = ""

    for err in scan.get("errors", []):
        rows += _row("[ERROR]", "#DC2626", err)

    if not scan.get("valid"):
        if rows:
            st.markdown(f'<div style="margin:6px 0 8px 0;">{rows}</div>', unsafe_allow_html=True)
        return False

    # collection.json found
    rows += _row("[OK]", "#059669", "collection.json found")

    # Tile counts
    asc  = scan.get("asc_tiles", 0)
    desc = scan.get("desc_tiles", 0)
    if asc:
        color = "#DC2626" if asc > _MAX_TILES else "#059669"
        tag   = "[ERROR]" if asc > _MAX_TILES else "[OK]"
        rows += _row(tag, color, f"{asc} ASCENDING tile(s){' — exceeds limit!' if asc > _MAX_TILES else ''}")
    if desc:
        color = "#DC2626" if desc > _MAX_TILES else "#059669"
        tag   = "[ERROR]" if desc > _MAX_TILES else "[OK]"
        rows += _row(tag, color, f"{desc} DESCENDING tile(s){' — exceeds limit!' if desc > _MAX_TILES else ''}")
    if not asc and not desc:
        rows += _row("[WARN]", "#D97706", "No tile subdirectories found.")

    if rows:
        st.markdown(f'<div style="margin:6px 0 8px 0;">{rows}</div>', unsafe_allow_html=True)
    return True


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _step_header(num: int, title: str, subtitle: str = "") -> None:
    sub_html = (
        f'<span style="font-size:11.5px;font-weight:400;color:#80A8A4;margin-left:6px;">'
        f'— {subtitle}</span>'
        if subtitle else ""
    )
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:10px;'
        f'margin-bottom:12px;padding-bottom:8px;border-bottom:1.5px solid #CCEBE8;">'
        f'<span style="width:22px;height:22px;border-radius:50%;background:#0D9488;'
        f'color:white;font-size:11px;font-weight:700;display:flex;align-items:center;'
        f'justify-content:center;flex-shrink:0;">{num}</span>'
        f'<span style="font-size:13px;font-weight:700;color:#134E4A;">{title}</span>'
        f'{sub_html}</div>',
        unsafe_allow_html=True,
    )


def _action_divider() -> None:
    st.markdown(
        '<div style="border-top:1.5px solid #CCEBE8;margin:4px 0 14px 0;"></div>',
        unsafe_allow_html=True,
    )


def _right_section_title(title: str) -> None:
    st.markdown(
        f'<div style="font-size:10.5px;font-weight:700;color:#4D7C78;'
        f'text-transform:uppercase;letter-spacing:0.10em;'
        f'margin:14px 0 8px 0;">{title}</div>',
        unsafe_allow_html=True,
    )


def _extract_mosaic_path(logs: list) -> str:
    for line in reversed(logs):
        m = re.search(r'Mosaic created[:\s]+(.+\.(vrt|tif|tiff))', line, re.I)
        if m:
            return m.group(1).strip()
    return ""


def _open_in_explorer(path: str):
    """Kept for reference; use open_in_explorer from gui.utils instead."""
    open_in_explorer(path)
