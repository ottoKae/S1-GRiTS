"""
Tile tab — s1grits tile inspect --tile <ID> --output-dir <dir>

Layout (5 | 7 columns):
  Left  — Tile ID input, direction filter, output dir, example tiles
  Right — Status, live log, completeness cards
"""

import os
import re
from pathlib import Path

import streamlit as st

from gui.config_builder import validate_output_dir
from gui.runner import CommandRunner, build_cmd
from gui.styles import (
    build_cmd_preview,
    render_completeness_card,
    render_log_viewer,
    render_path_panel,
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
            title="Select s1grits tile output directory",
            initialdir=start or "/",
        )
        root.destroy()
        return chosen or ""
    except Exception:
        return ""

_RUNNER_KEY = "tile_runner"
_LOGS_KEY   = "tile_logs"
_STATUS_KEY = "tile_status"
_OUTDIR_KEY = "tile_outdir"
_CMD_KEY    = "tile_cmd"
_TILE_DIR_KEY = "tile_output_dir_browse"

_EXAMPLE_TILES = [
    ("50RKV", "Wuhan, China"),
    ("49QGE", "Zhaoqing, China"),
    ("51LWF", "Port Hedland, Australia"),
    ("32UPU", "Munich, Germany"),
    ("17MPS", "Ecuador coast"),
    ("56MNT", "Papua New Guinea"),
]


def _init_state():
    defaults = {
        _RUNNER_KEY: CommandRunner(),
        _LOGS_KEY:   [],
        _STATUS_KEY: "idle",
        _OUTDIR_KEY: "",
        _CMD_KEY:    [],
        _TILE_DIR_KEY: "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _validate_tile_id(tile_id: str) -> tuple[bool, str]:
    tile_id = tile_id.strip().upper()
    if not tile_id:
        return False, "MGRS tile ID is required."
    if not re.match(r'^[0-9]{1,2}[A-Z]{3}$', tile_id):
        return False, f"'{tile_id}' is not a valid MGRS tile ID (e.g. 50RKV)."
    return True, ""


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
            'Tile Inspect</span>'
            '<span style="font-size:12px;font-weight:500;color:#4D7C78;margin-left:10px;">'
            's1grits tile inspect</span></div>',
            unsafe_allow_html=True,
        )
        render_tip(
            "Enter the <strong>tile subdirectory</strong> path, e.g. "
            "<code>D:/QGIS/s1grits-dataset/BFA_hARDCp/30PXA_ASCENDING</code>, "
            "or the <strong>root output directory</strong> containing multiple tile folders. "
            "Then enter the MGRS tile ID to inspect."
        )

        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

        # ── STEP 1: Output Directory ──────────────────────────────────────
        _step_header(1, "Output Directory", "Tile folder, e.g. MyRegion_hARDCp/")

        if st.button("Browse Folder", key="tile_dir_browse", use_container_width=True):
            picked = _pick_folder(st.session_state.get(_TILE_DIR_KEY, ""))
            if picked:
                st.session_state[_TILE_DIR_KEY] = picked
                st.rerun()

        tile_dir_path = st.session_state.get(_TILE_DIR_KEY, "").strip()
        if tile_dir_path:
            st.markdown(
                f'<div style="background:#F0FDFA;border:1px solid #CCEBE8;border-radius:6px;'
                f'padding:6px 10px;margin:4px 0 6px 0;font-size:12px;'
                f'color:#134E4A;word-break:break-all;line-height:1.4;">'
                f'{escape(tile_dir_path)}</div>',
                unsafe_allow_html=True,
            )
        output_dir = tile_dir_path
        ok_dir = _render_tile_dir_status(output_dir)

        # Resolve: if user picked a tile subdir (has catalog.parquet in parent), use parent
        # as the --output-dir arg; if they picked the root, use it directly
        _p = Path(output_dir) if output_dir else None
        if _p and not (_p / "catalog.parquet").exists() and (_p.parent / "catalog.parquet").exists():
            cli_output_dir = str(_p.parent)
        else:
            cli_output_dir = output_dir

        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        # ── STEP 2: Tile ID & Direction ────────────────────────────────────
        _step_header(2, "Tile ID & Direction")

        tile_id = st.text_input(
            "MGRS Tile ID",
            value="",
            key="tile_tile_id",
            placeholder="50RKV",
            max_chars=10,
            help="5-character MGRS tile identifier, e.g. 50RKV",
            label_visibility="collapsed",
        ).strip().upper()

        ok_tile, err_tile = _validate_tile_id(tile_id) if tile_id else (True, "")
        if tile_id and not ok_tile:
            st.error(err_tile)
        elif tile_id:
            st.success(f"Tile ID: {tile_id}")

        direction = st.radio(
            "Orbit direction filter",
            ["ASCENDING", "DESCENDING"],
            horizontal=True,
            key="tile_direction",
            help="Filter results by orbit direction.",
        )

        # Quick tile-folder existence check
        _tile_exists, _tile_exists_msg = _check_tile_folder_exists(
            tile_dir_path, tile_id, direction
        )
        if tile_id and tile_dir_path and ok_tile and ok_dir:
            if _tile_exists:
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:6px;'
                    f'font-size:11.5px;color:#059669;margin-top:4px;">'
                    f'<span style="font-weight:700;">[OK]</span>{escape(_tile_exists_msg)}</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:6px;'
                    f'font-size:11.5px;color:#DC2626;margin-top:4px;">'
                    f'<span style="font-weight:700;">[ERROR]</span>{escape(_tile_exists_msg)}</div>',
                    unsafe_allow_html=True,
                )

        # Command preview
        cmd = st.session_state.get(_CMD_KEY, [])
        if cmd:
            st.markdown(build_cmd_preview(cmd), unsafe_allow_html=True)

        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

        # ── Example tiles reference ────────────────────────────────────────
        st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
        _step_header(0, "Example Tile IDs", "Quick reference for common locations")

        rows_html = "".join(
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:6px 0;border-bottom:1px solid #CCEBE8;">'
            f'<span style="font-family:\'JetBrains Mono\',monospace;font-size:12px;'
            f'color:#0D9488;font-weight:600;">{tid}</span>'
            f'<span style="font-size:12px;color:#4D7C78;">{loc}</span></div>'
            for tid, loc in _EXAMPLE_TILES
        )
        st.markdown(
            f'<div style="background:#FFFFFF;border:1.5px solid #CCEBE8;'
            f'border-radius:10px;padding:10px 14px;">{rows_html}</div>',
            unsafe_allow_html=True,
        )

    # =========================================================================
    # RIGHT PANEL
    # =========================================================================
    with col_right:
        status = st.session_state.get(_STATUS_KEY, "idle")
        logs   = st.session_state.get(_LOGS_KEY, [])

        _right_section_title("Actions")
        _is_running = runner.is_running()
        _tile_id    = st.session_state.get("tile_tile_id", "").strip().upper()
        _tile_dir   = st.session_state.get(_TILE_DIR_KEY, "").strip()
        _direction_r = st.session_state.get("tile_direction", "ASCENDING")
        _p_dir = Path(_tile_dir) if _tile_dir else None
        _cli_dir = (
            str(_p_dir.parent)
            if _p_dir and not (_p_dir / "catalog.parquet").exists()
               and (_p_dir.parent / "catalog.parquet").exists()
            else _tile_dir
        )
        _ok_tile_r, _ = _validate_tile_id(_tile_id) if _tile_id else (True, "")
        _ok_dir_r = bool(_tile_dir) and Path(_tile_dir).is_dir()
        _tile_folder_ok, _ = _check_tile_folder_exists(_tile_dir, _tile_id, _direction_r)
        _can_run = (
            not _is_running
            and bool(_tile_id)
            and _ok_tile_r
            and _ok_dir_r
            and _tile_folder_ok
        )

        col_run_r, col_stop_r = st.columns([3, 1])
        with col_run_r:
            if st.button(
                "Run: tile inspect",
                type="primary",
                use_container_width=True,
                disabled=not _can_run,
                key="tile_run",
            ):
                kwargs = {"tile": _tile_id, "output_dir": _cli_dir}
                kwargs["direction"] = _direction_r
                cmd = build_cmd("tile inspect", **kwargs)
                st.session_state[_LOGS_KEY]   = []
                st.session_state[_STATUS_KEY] = "running"
                st.session_state[_OUTDIR_KEY] = str(Path(_tile_dir).resolve())
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
                key="tile_stop_r",
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
            key="tile_log_levels",
            label_visibility="collapsed",
        )
        st.components.v1.html(render_log_viewer(logs, selected_levels), height=440, scrolling=False)

        out_dir = st.session_state.get(_OUTDIR_KEY, "")
        if out_dir:
            _right_section_title("Output Paths")
            st.markdown(render_path_panel(out_dir, ""), unsafe_allow_html=True)

        if status in ("success", "failed") and logs:
            _right_section_title("Temporal Completeness")
            _render_completeness_cards(logs)

    if runner.is_running():
        st.rerun()


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _check_tile_folder_exists(output_dir: str, tile_id: str, direction: str) -> tuple[bool, str]:
    """
    Check whether a tile subfolder matching <TILE_ID>_<DIRECTION> exists
    inside the given output directory (or its parent).
    Returns (found: bool, message: str).
    """
    if not output_dir or not tile_id or not direction:
        return True, ""  # Not enough info to check — don't block

    p = Path(output_dir)
    if not p.exists() or not p.is_dir():
        return True, ""  # Dir validation handled elsewhere

    # Determine the root to search: if user selected a tile subdir, use parent
    root = p.parent if (p.parent / "catalog.parquet").exists() else p

    folder_name = f"{tile_id}_{direction}"
    candidate = root / folder_name
    if candidate.is_dir():
        return True, f"Tile folder found: {folder_name}/"

    # Also try case-insensitive search (Windows is case-insensitive anyway,
    # but be explicit for cross-platform safety)
    try:
        matches = [d for d in root.iterdir()
                   if d.is_dir() and d.name.upper() == folder_name.upper()]
        if matches:
            return True, f"Tile folder found: {matches[0].name}/"
    except PermissionError:
        return True, ""

    return (
        False,
        f"Tile folder '{folder_name}' not found in {root.name}/. "
        "Check the tile ID and orbit direction, or run Process first."
    )


def _render_tile_dir_status(path_str: str) -> bool:
    """
    Validate tile output directory and render inline status rows.
    Accepts both:
      - root output dir (e.g. BFA_hARDCp/) containing tile subdirs
      - tile subdir itself (e.g. BFA_hARDCp/30PXA_ASCENDING/)
    Returns True if directory is usable.
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

    # Check for catalog.parquet — in this dir OR one level up
    has_catalog = (p / "catalog.parquet").exists()
    parent_catalog = (p.parent / "catalog.parquet").exists()
    if has_catalog:
        rows += _row("[OK]", "#059669", f"catalog.parquet found in {p.name}/")
    elif parent_catalog:
        rows += _row("[OK]", "#059669", f"catalog.parquet found in parent ({p.parent.name}/)")
    else:
        rows += _row("[WARN]", "#D97706",
                     "catalog.parquet not found — run Catalog > Rebuild first.")

    if rows:
        st.markdown(f'<div style="margin:6px 0 8px 0;">{rows}</div>', unsafe_allow_html=True)
    return True


def _step_header(num: int, title: str, subtitle: str = "") -> None:
    """Numbered step header. Pass num=0 for a non-numbered reference header."""
    if num == 0:
        # Reference / info header (no circle)
        sub_html = (
            f'<span style="font-size:11.5px;font-weight:400;color:#80A8A4;margin-left:6px;">'
            f'— {subtitle}</span>'
            if subtitle else ""
        )
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:10px;'
            f'margin-bottom:12px;padding-bottom:8px;border-bottom:1.5px solid #CCEBE8;">'
            f'<span style="font-size:13px;font-weight:700;color:#134E4A;">{title}</span>'
            f'{sub_html}</div>',
            unsafe_allow_html=True,
        )
        return

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
# Completeness cards
# ---------------------------------------------------------------------------

def _render_completeness_cards(logs: list):
    completeness_pat = re.compile(r'Completeness[:\s]+([\d.]+)%', re.I)
    direction_pat    = re.compile(r'\b(ASCENDING|DESCENDING)\b', re.I)
    present_pat      = re.compile(r'Present months[:\s]+(\d+)', re.I)
    expected_pat     = re.compile(r'Expected months[:\s]+(\d+)', re.I)

    cards = []
    current: dict = {}
    for line in logs:
        dm = direction_pat.search(line)
        if dm and line.strip().upper().startswith(dm.group().upper()):
            if current:
                cards.append(current)
            current = {"direction": dm.group(1).upper()}
        cm = completeness_pat.search(line)
        if cm:
            current["completeness"] = float(cm.group(1))
        pm = present_pat.search(line)
        if pm:
            current["present"] = int(pm.group(1))
        em = expected_pat.search(line)
        if em:
            current["expected"] = int(em.group(1))
    if current:
        cards.append(current)

    if not cards:
        return

    cols = st.columns(max(len(cards), 1))
    for col, card in zip(cols, cards):
        with col:
            st.markdown(
                render_completeness_card(
                    direction=card.get("direction", "ALL"),
                    pct=card.get("completeness", 0),
                    present=card.get("present", 0),
                    expected=card.get("expected", 0),
                ),
                unsafe_allow_html=True,
            )


def _open_in_explorer(path: str):
    """Kept for reference; use open_in_explorer from gui.utils instead."""
    open_in_explorer(path)
