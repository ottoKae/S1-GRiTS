"""
Catalog tab — s1grits catalog rebuild / validate / inspect

Layout (5 | 7 columns):
  Left  — Operation selector, directory input, run controls
  Right — Status, live log, output paths, results table
"""

import os
import re
from pathlib import Path

import streamlit as st

from gui.config_builder import validate_output_dir
from gui.runner import CommandRunner, build_cmd
from gui.styles import (
    build_cmd_preview,
    render_log_viewer,
    render_path_panel,
    render_stat_row,
    render_status_badge,
    render_tip,
)
from gui.utils import open_in_explorer

from html import escape


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

_RUNNER_KEY = "catalog_runner"
_LOGS_KEY   = "catalog_logs"
_STATUS_KEY = "catalog_status"
_OUTDIR_KEY = "catalog_outdir"
_CMD_KEY    = "catalog_cmd"
_DIR_KEY    = "catalog_dir"

_SUBCMD_DESCRIPTIONS = {
    "rebuild": (
        "Scans all COG files in the output directory, rebuilds "
        "<strong>catalog.parquet</strong>, and re-generates STAC Item JSON files. "
        "Run this after adding new tiles or recovering from a failed run."
    ),
    "validate": (
        "Validates the catalog schema and checks that all STAC Item JSON files "
        "are present on disk. Reports missing items and schema violations."
    ),
    "inspect": (
        "Shows a per-tile coverage summary: months present, expected, missing, "
        "and overall completeness %. Useful for monitoring data gaps."
    ),
}

_SUBCMD_ICONS = {
    "rebuild":  "Rebuild",
    "validate": "Validate",
    "inspect":  "Inspect",
}


def _init_state():
    defaults = {
        _RUNNER_KEY: CommandRunner(),
        _LOGS_KEY:   [],
        _STATUS_KEY: "idle",
        _OUTDIR_KEY: "",
        _CMD_KEY:    [],
        _DIR_KEY:    "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def render():
    _init_state()
    runner: CommandRunner = st.session_state[_RUNNER_KEY]

    col_left, col_right = st.columns([5, 7], gap="large")

    # =========================================================================
    # LEFT PANEL
    # =========================================================================
    with col_left:

        # Page title
        st.markdown(
            '<div style="margin-bottom:6px;">'
            '<span style="font-size:20px;font-weight:800;color:#134E4A;letter-spacing:-0.4px;">'
            'Catalog Management</span>'
            '<span style="font-size:12px;font-weight:500;color:#4D7C78;margin-left:10px;">'
            's1grits catalog</span></div>',
            unsafe_allow_html=True,
        )

        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

        # ── STEP 1: Output Directory ───────────────────────────────────────
        _step_header(1, "Output Directory", "Root folder containing tile subdirectories")

        if st.button("Browse Folder", key="catalog_browse", use_container_width=True):
            picked = _pick_folder(st.session_state.get(_DIR_KEY, ""))
            if picked:
                st.session_state[_DIR_KEY] = picked
                st.rerun()

        current_path = st.session_state.get(_DIR_KEY, "").strip()
        if current_path:
            st.markdown(
                f'<div style="background:#F0FDFA;border:1px solid #CCEBE8;border-radius:6px;'
                f'padding:6px 10px;margin:4px 0 6px 0;font-size:12px;'
                f'color:#134E4A;word-break:break-all;line-height:1.4;">'
                f'{escape(current_path)}</div>',
                unsafe_allow_html=True,
            )

        output_dir = current_path
        ok_dir = _render_catalog_dir_status(output_dir)

        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        # ── STEP 2: Choose operation ───────────────────────────────────────
        _step_header(2, "Choose Operation")

        subcmd = st.radio(
            "Operation",
            ["rebuild", "validate", "inspect"],
            horizontal=True,
            key="catalog_subcmd",
            format_func=lambda x: _SUBCMD_ICONS[x],
        )
        render_tip(_SUBCMD_DESCRIPTIONS[subcmd])

        # Workflow hint for inspect
        if subcmd == "inspect":
            st.markdown(
                '<div style="font-size:11.5px;color:#4D7C78;'
                'background:#F0FDFA;border:1px solid #CCEBE8;'
                'border-radius:8px;padding:8px 12px;margin-top:6px;">'
                'Run <strong style="color:#0D9488;">catalog rebuild</strong> first '
                'if the catalog does not exist yet.</div>',
                unsafe_allow_html=True,
            )

        # Command preview
        cmd = st.session_state.get(_CMD_KEY, [])
        if cmd:
            st.markdown(build_cmd_preview(cmd), unsafe_allow_html=True)

        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
    with col_right:
        status = st.session_state.get(_STATUS_KEY, "idle")
        logs   = st.session_state.get(_LOGS_KEY, [])

        _is_running = runner.is_running()
        _subcmd_val = st.session_state.get("catalog_subcmd", "rebuild")
        _output_dir = st.session_state.get(_DIR_KEY, "").strip()
        _ok_run = _validate_catalog_dir_silent(_output_dir) if _output_dir else False
        _can_run = not _is_running and _ok_run
        col_run_r, col_stop_r = st.columns([3, 1])
        with col_run_r:
            if st.button(
                f"Run: catalog {_subcmd_val}",
                type="primary",
                use_container_width=True,
                disabled=not _can_run,
                key="catalog_run",
            ):
                cmd = build_cmd(f"catalog {_subcmd_val}", output_dir=_output_dir)
                st.session_state[_LOGS_KEY]   = []
                st.session_state[_STATUS_KEY] = "running"
                st.session_state[_OUTDIR_KEY] = str(Path(_output_dir).resolve())
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
                key="catalog_stop",
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
        _render_live_log_catalog()

        out_dir = st.session_state.get(_OUTDIR_KEY, "")
        if out_dir:
            _right_section_title("Output Paths")
            st.markdown(render_path_panel(out_dir, ""), unsafe_allow_html=True)
            if Path(out_dir).exists():
                if st.button("Open Output Folder", key="catalog_open",
                             use_container_width=True):
                    open_in_explorer(out_dir)

        # Inspect results table
        subcmd_val = st.session_state.get("catalog_subcmd", "rebuild")
        if subcmd_val == "inspect" and status in ("success", "failed") and logs:
            _right_section_title("Coverage Summary")
            _render_inspect_summary(logs)

        # Rebuild / validate summary
        if subcmd_val in ("rebuild", "validate") and status in ("success", "failed") and logs:
            _right_section_title("Run Result")
            _render_run_summary(logs, status)

    if runner.is_running():
        st.rerun()


# ---------------------------------------------------------------------------
# Live log fragment (partial refresh only)
# ---------------------------------------------------------------------------

@st.fragment(run_every=2)
def _render_live_log_catalog():
    """Fragment: polls logs every 2 s while running; stops when idle/done."""
    runner: CommandRunner = st.session_state[_RUNNER_KEY]

    if runner.is_running() or runner.status in ("running", "success", "failed", "stopped"):
        new_lines = runner.drain_logs()
        if new_lines:
            st.session_state[_LOGS_KEY].extend(new_lines)
        if not runner.is_running() and st.session_state.get(_STATUS_KEY) == "running":
            st.session_state[_STATUS_KEY] = runner.status

    logs = st.session_state.get(_LOGS_KEY, [])
    selected_levels = st.multiselect(
        "Log filter",
        ["DEBUG", "INFO", "WARNING", "ERROR"],
        default=["INFO", "WARNING", "ERROR"],
        key="catalog_log_levels",
        label_visibility="collapsed",
    )
    st.components.v1.html(render_log_viewer(logs, selected_levels), height=440, scrolling=False)


# ---------------------------------------------------------------------------
# Layout helpers (shared pattern)
# ---------------------------------------------------------------------------

def _validate_catalog_dir_silent(path_str: str) -> bool:
    """
    Silently validate whether the output directory is usable (no st.markdown output).
    Used in the right panel to compute _can_run without rendering duplicate status rows.
    """
    if not path_str:
        return False
    p = Path(path_str)
    return p.exists() and p.is_dir()


def _render_catalog_dir_status(path_str: str) -> bool:
    """
    Validate the output directory and render inline status rows.
    Returns True if the directory is usable (can run).
    Checks for: existence, catalog.parquet, tile subdirectories.
    """
    if not path_str:
        return False

    p = Path(path_str)

    def _row(icon: str, color: str, text: str) -> str:
        return (
            f'<div style="display:flex;align-items:center;gap:6px;'
            f'font-size:11.5px;color:{color};margin-top:3px;">'
            f'<span style="font-weight:700;">{icon}</span>{escape(text)}</div>'
        )

    rows = ""
    if not p.exists():
        rows += _row("[ERROR]", "#DC2626", "Directory does not exist.")
        st.markdown(f'<div style="margin:6px 0 8px 0;">{rows}</div>', unsafe_allow_html=True)
        return False
    if not p.is_dir():
        rows += _row("[ERROR]", "#DC2626", "Path is not a directory.")
        st.markdown(f'<div style="margin:6px 0 8px 0;">{rows}</div>', unsafe_allow_html=True)
        return False

    if (p / "catalog.parquet").exists():
        rows += _row("[OK]", "#059669", "catalog.parquet found")
    else:
        rows += _row("[WARN]", "#D97706", "catalog.parquet not found — run Rebuild first.")

    try:
        tile_dirs = [d for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")]
        if tile_dirs:
            rows += _row("[OK]", "#059669", f"{len(tile_dirs)} subdirectory(ies) found")
    except PermissionError:
        rows += _row("[WARN]", "#D97706", "Cannot read directory (permission denied).")

    if rows:
        st.markdown(f'<div style="margin:6px 0 8px 0;">{rows}</div>', unsafe_allow_html=True)
    return True


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


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------

def _render_inspect_summary(logs: list):
    rows = []
    tile_pat = re.compile(
        r'\|\s*([A-Z0-9]{5,6})\s*\|\s*(\w+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([\d.]+%)'
    )
    for line in logs:
        m = tile_pat.search(line)
        if m:
            rows.append({
                "Tile":         m.group(1),
                "Direction":    m.group(2),
                "Months":       int(m.group(3)),
                "Expected":     int(m.group(4)),
                "Missing":      int(m.group(5)),
                "Completeness": m.group(6),
            })

    if not rows:
        return

    total_tiles  = len(rows)
    complete     = sum(1 for r in rows if r["Missing"] == 0)
    total_months = sum(r["Months"] for r in rows)

    stats = [
        {"value": str(total_tiles),             "label": "Tile-Directions", "color": "#0D9488"},
        {"value": str(complete),                 "label": "Complete",        "color": "#059669"},
        {"value": str(total_tiles - complete),   "label": "With Gaps",       "color": "#D97706"},
        {"value": str(total_months),             "label": "Total Months",    "color": "#4D7C78"},
    ]
    st.markdown(render_stat_row(stats), unsafe_allow_html=True)
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_run_summary(logs: list, status: str):
    if status == "success":
        st.success("Command completed successfully.")
    else:
        st.error("Command completed with errors — check the log above.")
    with st.expander("Last 8 log lines", expanded=False):
        st.code("\n".join(logs[-8:]), language=None)


def _open_in_explorer(path: str):
    """Kept for reference; use open_in_explorer from gui.utils instead."""
    open_in_explorer(path)
