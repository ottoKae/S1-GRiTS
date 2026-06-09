"""
Process tab — s1grits process --config <yaml>

Workflow:
  1. User uploads a YAML config file.
  2. Key parameters are parsed and displayed in read-only fields for review.
  3. The original YAML file path is passed directly to the CLI — no rebuilding.

Layout (5 | 7 columns):
  Left  — YAML upload + read-only parameter review
  Right — Run/Stop, Status, Live log, Output paths, Run summary
"""

import os
import re
from pathlib import Path

import streamlit as st

from gui.config_builder import (
    extract_form_state_from_config,
    load_yaml_config,
    validate_output_dir,
)
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

_RUNNER_KEY   = "process_runner"
_LOGS_KEY     = "process_logs"
_STATUS_KEY   = "process_status"
_YAML_PATH_KEY = "process_yaml_path"   # path to the uploaded YAML (saved to temp)
_OUTDIR_KEY   = "process_outdir"
_LOGFILE_KEY  = "process_logfile"
_CMD_KEY      = "process_cmd"
_CFG_KEY      = "process_cfg"          # parsed config dict, for display


def _init_state():
    defaults = {
        _RUNNER_KEY:    CommandRunner(),
        _LOGS_KEY:      [],
        _STATUS_KEY:    "idle",
        _YAML_PATH_KEY: None,
        _OUTDIR_KEY:    "",
        _LOGFILE_KEY:   "",
        _CMD_KEY:       [],
        _CFG_KEY:       {},
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def render():
    _init_state()
    runner: CommandRunner = st.session_state[_RUNNER_KEY]

    col_left, col_right = st.columns([5, 7], gap="large")

    # =========================================================================
    # LEFT PANEL — YAML upload + read-only review
    # =========================================================================
    with col_left:

        st.markdown(
            '<div style="margin-bottom:6px;">'
            '<span style="font-size:20px;font-weight:800;color:#134E4A;letter-spacing:-0.4px;">'
            'SAR Processing</span>'
            '<span style="font-size:12px;font-weight:500;color:#4D7C78;margin-left:10px;">'
            's1grits process</span></div>',
            unsafe_allow_html=True,
        )
        render_tip(
            "Upload your <strong>processing_config.yaml</strong> file. "
            "Parameters are shown below for review. "
            "Click <strong>Run Process</strong> to start."
        )

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

        # ── STEP 1: Upload YAML ────────────────────────────────────────────
        _step_header(1, "Config File", "Upload processing_config.yaml")

        uploaded = st.file_uploader(
            "Upload YAML",
            type=["yaml", "yml"],
            key="process_upload",
            label_visibility="collapsed",
        )

        last_applied = st.session_state.get("process_upload_applied", None)
        if uploaded is not None:
            file_id = (uploaded.name, uploaded.size)
            if file_id != last_applied:
                try:
                    # Read bytes before parsing (need raw bytes to save as temp file)
                    raw_bytes = uploaded.read()
                    raw_text  = raw_bytes.decode("utf-8")

                    import yaml
                    cfg_raw = yaml.safe_load(raw_text) or {}
                    state   = extract_form_state_from_config(cfg_raw)

                    # Save a temp file so the CLI can read it directly
                    import tempfile
                    fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="s1grits_gui_")
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as fh:
                            fh.write(raw_text)
                    except Exception:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        raise

                    # Clean up previous temp file
                    old = st.session_state.get(_YAML_PATH_KEY)
                    if old and Path(old).exists():
                        try:
                            os.unlink(old)
                        except OSError:
                            pass

                    st.session_state[_YAML_PATH_KEY]          = tmp_path
                    st.session_state[_CFG_KEY]                = state
                    st.session_state["process_upload_applied"] = file_id
                    st.rerun()

                except Exception as exc:
                    st.error(f"Failed to parse YAML: {exc}")
            # File already applied — show name badge
            else:
                st.markdown(
                    f'<div style="display:inline-flex;align-items:center;gap:7px;'
                    f'background:#F0FDFA;border:1.5px solid #CCEBE8;border-radius:8px;'
                    f'padding:6px 12px;font-size:12px;color:#0D9488;font-weight:600;">'
                    f'<span style="font-family:\'JetBrains Mono\',monospace;">'
                    f'{uploaded.name}</span>'
                    f'<span style="color:#4D7C78;font-weight:400;">loaded</span></div>',
                    unsafe_allow_html=True,
                )
        else:
            # No file — clear stored config if user removed the file
            if st.session_state.get("process_upload_applied") is not None:
                old_path = st.session_state.get(_YAML_PATH_KEY)
                if old_path and Path(old_path).exists():
                    try:
                        os.unlink(old_path)
                    except OSError:
                        pass
                st.session_state["process_upload_applied"] = None
                st.session_state[_CFG_KEY]                = {}
                st.session_state[_YAML_PATH_KEY]          = None

        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

        # ── STEP 2: Parameter Review (read-only) ───────────────────────────
        cfg = st.session_state.get(_CFG_KEY, {})
        has_cfg = bool(cfg)

        _step_header(2, "Parameter Review", "Read-only — edit the YAML file to change values")

        if not has_cfg:
            st.markdown(
                '<div style="color:#80A8A4;font-size:12.5px;font-style:italic;'
                'padding:16px 0;">No config loaded yet — upload a YAML file above.</div>',
                unsafe_allow_html=True,
            )
        else:
            _render_review(cfg)

        # Command preview
        cmd = st.session_state.get(_CMD_KEY, [])
        if cmd:
            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
            st.markdown(build_cmd_preview(cmd), unsafe_allow_html=True)

        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    # =========================================================================
    # RIGHT PANEL — Actions, Status, Logs, Results
    # =========================================================================
    with col_right:
        _render_right_panel(runner)


# ---------------------------------------------------------------------------
# Read-only parameter review panel
# ---------------------------------------------------------------------------

def _render_review(cfg: dict):
    """
    Display parsed YAML parameters in disabled widgets for human review.
    All widgets are read-only (disabled=True).
    """
    roi_mode = cfg.get("roi_mode", "WKT polygon")

    # ROI
    _review_section("Area of Interest")
    col_a, col_b = st.columns(2)
    with col_a:
        st.text_input("ROI mode",       value=roi_mode,                              disabled=True, key="rv_roi_mode")
    with col_b:
        st.text_input("Polarization",   value=cfg.get("polarization", ""),          disabled=True, key="rv_pol")

    if roi_mode == "WKT polygon":
        st.text_area("WKT polygon",
                     value=cfg.get("wkt", ""),
                     height=72, disabled=True, key="rv_wkt")
    else:
        tiles = cfg.get("mgrs_tiles", [])
        st.text_area("MGRS tile IDs",
                     value="\n".join(tiles) if isinstance(tiles, list) else str(tiles),
                     height=72, disabled=True, key="rv_mgrs")

    st.text_input("Orbit direction", value=cfg.get("flight_direction", ""), disabled=True, key="rv_dir")

    # Time
    _review_section("Time Range")
    time_mode = cfg.get("time_mode", "")
    col_tm, col_tv = st.columns(2)
    with col_tm:
        st.text_input("Time mode", value=time_mode, disabled=True, key="rv_time_mode")
    with col_tv:
        if time_mode == "Full archive":
            st.text_input("End year", value=str(cfg.get("full_end_year", "")), disabled=True, key="rv_full_year")
        else:
            years = cfg.get("years", [])
            st.text_input("Years", value=", ".join(str(y) for y in years), disabled=True, key="rv_years")

    months = cfg.get("months", [])
    if months:
        st.text_input("Months", value=", ".join(str(m) for m in months), disabled=True, key="rv_months")

    # Output
    _review_section("Output")
    st.text_input("Base directory",  value=cfg.get("base_dir", ""),          disabled=True, key="rv_base_dir")
    col_c1, col_c2, col_c3 = st.columns(3)
    with col_c1:
        st.text_input("COG",      value="Yes" if cfg.get("fmt_cog", True)     else "No", disabled=True, key="rv_cog")
    with col_c2:
        st.text_input("Preview",  value="Yes" if cfg.get("fmt_preview", True) else "No", disabled=True, key="rv_prev")
    with col_c3:
        st.text_input("Overwrite",value="Yes" if cfg.get("overwrite_cog", False) else "No", disabled=True, key="rv_ow")

    # Advanced (collapsed)
    with st.expander("Advanced Settings", expanded=False):
        col_w, col_m = st.columns(2, gap="small")
        with col_w:
            st.text_input("Parallel workers",   value=str(cfg.get("max_workers", "")),         disabled=True, key="rv_workers")
        with col_m:
            st.text_input("Max memory (GB)",    value=str(cfg.get("max_memory_gb", "auto")),   disabled=True, key="rv_mem")
        col_b2, col_d = st.columns(2, gap="small")
        with col_b2:
            st.text_input("Batch strategy",     value=str(cfg.get("batch_strategy", "auto")), disabled=True, key="rv_batch")
        with col_d:
            st.text_input("Download workers",   value=str(cfg.get("max_download_workers", "")), disabled=True, key="rv_dlw")

        st.text_input("TV Despeckle",
                      value="On" if cfg.get("post_processing", True) else "Off",
                      disabled=True, key="rv_desp")
        if cfg.get("post_processing", True):
            st.text_input("Reg. strength",      value=str(cfg.get("reg_param", 5.0)),          disabled=True, key="rv_reg")

        glcm = cfg.get("glcm_enabled", False)
        st.text_input("GLCM Texture",
                      value="On" if glcm else "Off",
                      disabled=True, key="rv_glcm")
        if glcm:
            col_gi, col_gm = st.columns(2, gap="small")
            with col_gi:
                st.text_input("GLCM inputs",    value=", ".join(cfg.get("glcm_inputs", [])),   disabled=True, key="rv_gi")
            with col_gm:
                st.text_input("GLCM metrics",   value=", ".join(cfg.get("glcm_metrics", [])),  disabled=True, key="rv_gm")
            st.text_input("Window size",        value=str(cfg.get("glcm_window_size", 5)),     disabled=True, key="rv_gw")


def _review_section(title: str) -> None:
    """Small inline section label inside the review panel."""
    st.markdown(
        f'<div style="font-size:11px;font-weight:700;color:#4D7C78;'
        f'text-transform:uppercase;letter-spacing:0.10em;'
        f'margin:12px 0 6px 0;padding-bottom:5px;border-bottom:1px solid #CCEBE8;">'
        f'{title}</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Right panel
# ---------------------------------------------------------------------------

def _render_right_panel(runner: CommandRunner):
    status    = st.session_state.get(_STATUS_KEY, "idle")
    logs      = st.session_state.get(_LOGS_KEY, [])
    is_running = runner.is_running()

    yaml_path = st.session_state.get(_YAML_PATH_KEY)
    cfg       = st.session_state.get(_CFG_KEY, {})
    base_dir  = cfg.get("base_dir", "")
    ok_dir, _ = validate_output_dir(base_dir) if base_dir else (False, "")
    can_run   = (not is_running) and bool(yaml_path) and ok_dir

    # ── Run / Stop ──
    _right_section_title("Actions")
    col_run, col_stop = st.columns([3, 1])
    with col_run:
        if st.button(
            "Run Process",
            type="primary",
            use_container_width=True,
            disabled=not can_run,
            key="process_run",
        ):
            cmd = build_cmd("process", config=yaml_path)
            st.session_state[_LOGS_KEY]   = []
            st.session_state[_STATUS_KEY] = "running"
            # Compute actual output root with processing-level suffix
            post_proc = st.session_state.get(_CFG_KEY, {}).get("post_processing", True)
            suffix    = "_hARDCp" if post_proc else "_ARDC"
            st.session_state[_OUTDIR_KEY]  = str(Path(base_dir).resolve()) + suffix
            st.session_state[_LOGFILE_KEY] = ""   # resolved later from CLI output
            st.session_state[_CMD_KEY] = cmd
            try:
                runner.run(cmd)
            except FileNotFoundError:
                st.error(
                    "ERROR: s1grits executable not found. "
                    "Ensure s1grits is installed in the current Python environment: "
                    "pip install -e .[gui]"
                )
                st.session_state[_STATUS_KEY] = "failed"

    with col_stop:
        if st.button(
            "Stop",
            type="secondary",
            use_container_width=True,
            disabled=not is_running,
            key="process_stop",
        ):
            runner.stop()
            st.session_state[_STATUS_KEY] = "stopped"
            st.rerun()

    # ── Status ──
    _right_section_title("Status")
    st.markdown(
        render_status_badge(status, started=runner.started_at, duration=runner.elapsed),
        unsafe_allow_html=True,
    )

    # ── Live log ──
    _right_section_title("Live Output")
    _render_live_log()

    # ── Output paths ──
    out_dir  = st.session_state.get(_OUTDIR_KEY, "")
    logs     = st.session_state.get(_LOGS_KEY, [])
    if out_dir:
        # Resolve actual log file from CLI output (pattern: "Log file: ./logs/s1grits_*.log")
        log_file = _extract_log_path(logs, out_dir)
        st.session_state[_LOGFILE_KEY] = log_file
        st.markdown(render_path_panel(out_dir, log_file), unsafe_allow_html=True)
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("Open Data Folder", key="proc_open_dir",
                         use_container_width=True):
                open_in_explorer(out_dir)
        with col_b:
            if st.button("Open Log File", key="proc_open_log",
                         use_container_width=True,
                         disabled=not bool(log_file)):
                open_in_explorer(log_file)

    # ── Run summary ──
    if status in ("success", "failed") and logs:
        _right_section_title("Run Summary")
        _render_summary(logs, status)


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


def _right_section_title(title: str) -> None:
    st.markdown(
        f'<div style="font-size:10.5px;font-weight:700;color:#4D7C78;'
        f'text-transform:uppercase;letter-spacing:0.10em;'
        f'margin:14px 0 8px 0;">{title}</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Log file path resolver
# ---------------------------------------------------------------------------

def _extract_log_path(logs: list[str], out_dir: str) -> str:
    """
    Scan CLI output lines for the actual log file path announced by s1grits.
    The CLI emits a line like:
        INFO -   - Log file: ./logs/s1grits_20260416_032435.log
    Returns the resolved absolute path, or empty string if not found yet.
    """
    _LOG_RE = re.compile(r'[Ll]og\s+file[:\s]+(\S+\.log)')
    for line in logs:
        m = _LOG_RE.search(line)
        if m:
            raw = m.group(1)
            p = Path(raw)
            if not p.is_absolute():
                # Relative path is relative to CWD where the CLI was launched
                p = Path.cwd() / raw
            return str(p.resolve())
    return ""


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------

def _render_summary(logs: list, status: str):
    import re
    success_count = sum(1 for line in logs if re.search(r'\bOK\b|\bsuccess\b', line, re.I))
    error_count   = sum(1 for line in logs if re.search(r'\bERROR\b|\bFAIL\b',  line, re.I))
    warn_count    = sum(1 for line in logs if re.search(r'\bWARN\b',             line, re.I))

    stats = [
        {"value": str(success_count), "label": "OK",        "color": "#059669"},
        {"value": str(error_count),   "label": "Errors",    "color": "#DC2626"},
        {"value": str(warn_count),    "label": "Warnings",  "color": "#D97706"},
        {"value": str(len(logs)),     "label": "Log lines", "color": "#4D7C78"},
    ]
    st.markdown(render_stat_row(stats), unsafe_allow_html=True)

    if status == "success":
        st.success("Processing completed successfully.")
    else:
        st.error("Processing completed with errors — check the log above.")

    with st.expander("Last 10 log lines", expanded=False):
        st.code("\n".join(logs[-10:]), language=None)


# ---------------------------------------------------------------------------
# OS file explorer — provided by gui.utils.open_in_explorer
# ---------------------------------------------------------------------------


@st.fragment(run_every=2)
def _render_live_log():
    """Fragment: polls logs every 2 s while running; stops when idle/done."""
    runner: CommandRunner = st.session_state[_RUNNER_KEY]

    # Drain new log lines into session state
    if runner.is_running() or runner.status in ("running", "success", "failed", "stopped"):
        new_lines = runner.drain_logs()
        if new_lines:
            st.session_state[_LOGS_KEY].extend(new_lines)
        if not runner.is_running() and st.session_state.get(_STATUS_KEY) == "running":
            st.session_state[_STATUS_KEY] = runner.status

    logs = st.session_state.get(_LOGS_KEY, [])

    selected_levels = st.multiselect(
        "Log level filter",
        ["DEBUG", "INFO", "WARNING", "ERROR"],
        default=["INFO", "WARNING", "ERROR"],
        key="proc_log_levels",
        label_visibility="collapsed",
    )
    st.components.v1.html(
        render_log_viewer(logs, selected_levels), height=440, scrolling=False
    )
