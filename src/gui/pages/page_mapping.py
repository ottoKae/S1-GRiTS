"""
Mapping tab — interactive MGRS tile map + point time-series viewer.

Layout (4 | 8 columns):
  Left  — output folder input, Load button, direction filter, clicked point info
  Right — Folium map (tile polygons + click marker), Plotly time-series chart
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st
from html import escape
from pathlib import Path

# ---------------------------------------------------------------------------
# Session state keys
# ---------------------------------------------------------------------------
_DIR_KEY        = "mapping_output_dir"
_LOADED_KEY     = "mapping_loaded"
_CATALOG_KEY    = "mapping_catalog"
_TILES_KEY      = "mapping_tiles"
_CLICK_KEY      = "mapping_click"
_TS_KEY         = "mapping_timeseries"
_DIRECTION_KEY  = "mapping_direction"
_LAST_CLICK_KEY = "mapping_last_folium_click"


def _pick_folder(initial_dir: str = "") -> str:
    """
    Open a native OS folder-picker dialog using tkinter (works on Windows/macOS/Linux
    as long as a display is available).  Returns the chosen path, or "" on cancel.
    Runs synchronously — safe to call from a Streamlit button handler.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()          # hide the tiny root window
        root.wm_attributes("-topmost", True)

        start = initial_dir if initial_dir and Path(initial_dir).is_dir() else ""
        chosen = filedialog.askdirectory(
            title="Select s1grits output directory",
            initialdir=start or "/",
        )
        root.destroy()
        return chosen or ""
    except Exception:
        # tkinter not available (headless server) — return empty string silently
        return ""


def _init_state() -> None:
    defaults = {
        _DIR_KEY:               "",
        _LOADED_KEY:            False,
        _CATALOG_KEY:           None,
        _TILES_KEY:             [],
        _CLICK_KEY:             None,
        _TS_KEY:                None,
        _DIRECTION_KEY:         "All",
        _LAST_CLICK_KEY:        None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ---------------------------------------------------------------------------
# Zarr cache — one open dataset per (tile_id, direction, output_dir)
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def _load_cached_zarr(tile_id: str, direction: str, output_dir: str):
    from s1grits.analysis.io import load_zarr_dataset
    return load_zarr_dataset(tile_id, direction, output_dir)


# ---------------------------------------------------------------------------
# Tile bounds helper
# ---------------------------------------------------------------------------

def _tile_bounds_wgs84(row: pd.Series) -> list[list[float]] | None:
    """
    Compute WGS84 polygon corners [[lat,lon], ...] from catalog row.
    Returns None if conversion fails.
    """
    try:
        import pyproj
        t = list(row["transform"])
        w = int(row["width"])
        h = int(row["height"])
        src_crs = pyproj.CRS.from_user_input(row["crs"])
        dst_crs = pyproj.CRS.from_epsg(4326)
        tr = pyproj.Transformer.from_crs(src_crs, dst_crs, always_xy=True)

        x_min, x_max = t[2], t[2] + t[0] * w
        y_max, y_min = t[5], t[5] + t[4] * h

        corners_x = [x_min, x_max, x_max, x_min, x_min]
        corners_y = [y_min, y_min, y_max, y_max, y_min]
        lons, lats = tr.transform(corners_x, corners_y)
        return [[lat, lon] for lat, lon in zip(lats, lons)]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render() -> None:
    _init_state()

    col_left, col_right = st.columns([4, 8], gap="large")

    with col_left:
        _render_left_panel()

    with col_right:
        _render_right_panel()


# ---------------------------------------------------------------------------
# Left panel
# ---------------------------------------------------------------------------

def _render_left_panel() -> None:
    st.markdown(
        '<div style="margin-bottom:6px;">'
        '<span style="font-size:20px;font-weight:800;color:#134E4A;letter-spacing:-0.4px;">'
        'Mapping</span>'
        '<span style="font-size:12px;font-weight:500;color:#4D7C78;margin-left:10px;">'
        'Tile coverage + point time-series</span></div>',
        unsafe_allow_html=True,
    )

    # ── Step 1: Output folder ─────────────────────────────────────────────
    _step_header(1, "Output Folder", "Select the s1grits output directory")

    # Browse button — opens a native folder picker dialog via tkinter
    if st.button("Browse Folder", key="mapping_browse", use_container_width=True):
        picked = _pick_folder(st.session_state.get(_DIR_KEY, ""))
        if picked:
            st.session_state[_DIR_KEY] = picked
            st.rerun()

    # Show the currently selected path as a read-only display badge
    current_path = st.session_state.get(_DIR_KEY, "").strip()
    if current_path:
        st.markdown(
            f'<div style="'
            f'background:#F0FDFA;border:1px solid #CCEBE8;border-radius:6px;'
            f'padding:6px 10px;margin:4px 0 6px 0;font-size:12px;'
            f'color:#134E4A;word-break:break-all;line-height:1.4;">'
            f'{escape(current_path)}'
            f'</div>',
            unsafe_allow_html=True,
        )
    dir_status = _validate_output_dir(current_path)
    _render_dir_status(dir_status)

    if st.button(
        "Load",
        type="primary",
        disabled=not dir_status["can_load"],
        key="mapping_load",
        use_container_width=True,
    ):
        resolved = dir_status.get("resolved_dir", current_path)
        _do_load(resolved)

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    # ── Step 2: Options ────────────────────────────────────────────────────
    _step_header(2, "Options")

    st.session_state[_DIRECTION_KEY] = st.radio(
        "Orbit direction",
        ["All", "ASCENDING", "DESCENDING"],
        index=["All", "ASCENDING", "DESCENDING"].index(
            st.session_state.get(_DIRECTION_KEY, "All")
        ),
        horizontal=True,
        key="mapping_direction_radio",
        label_visibility="collapsed",
    )

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    # ── Step 3: Point info (after click) ──────────────────────────────────
    _step_header(3, "Point Info", "Click a point on the map")

    click = st.session_state.get(_CLICK_KEY)
    ts    = st.session_state.get(_TS_KEY)

    if click is None:
        st.markdown(
            '<div style="color:#80A8A4;font-size:12.5px;font-style:italic;padding:8px 0;">'
            'No point selected — click anywhere on the map.</div>',
            unsafe_allow_html=True,
        )
    else:
        _render_point_info(click, ts)
        if st.button("Clear Selection", key="mapping_clear"):
            st.session_state[_CLICK_KEY]      = None
            st.session_state[_TS_KEY]         = None
            st.session_state[_LAST_CLICK_KEY] = None
            st.rerun()


# ---------------------------------------------------------------------------
# Right panel
# ---------------------------------------------------------------------------

def _render_right_panel() -> None:
    loaded  = st.session_state.get(_LOADED_KEY, False)
    catalog = st.session_state.get(_CATALOG_KEY)
    tiles   = st.session_state.get(_TILES_KEY, [])

    if not loaded or catalog is None:
        st.markdown(
            '<div style="display:flex;align-items:center;justify-content:center;'
            'height:480px;background:#F0FDFA;border-radius:12px;border:1.5px dashed #CCEBE8;">'
            '<span style="color:#80A8A4;font-size:14px;">Load an output folder to view the map.</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    direction_filter = st.session_state.get(_DIRECTION_KEY, "All")

    # Filter catalog by direction
    df = catalog.copy()
    if direction_filter != "All":
        df = df[df["flight_direction"] == direction_filter]

    # Unique tiles for polygon rendering
    unique_tiles = df.drop_duplicates(subset=["mgrs_tile_id", "flight_direction"])

    # Compute map center from tile bounds
    center_lat, center_lon = _compute_center(unique_tiles)

    # ── Folium map ──────────────────────────────────────────────────────────
    try:
        import folium
        from streamlit_folium import st_folium
    except ImportError:
        st.error(
            "streamlit-folium and folium are required for the Mapping tab. "
            "Install with: pip install folium streamlit-folium"
        )
        return

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=6,
        tiles="CartoDB Positron",
    )

    # Add tile polygons
    for _, row in unique_tiles.iterrows():
        corners = _tile_bounds_wgs84(row)
        if corners is None:
            continue
        color = "#0D9488" if row.get("flight_direction") == "ASCENDING" else "#3B82F6"
        month_count = int(df[
            (df["mgrs_tile_id"] == row["mgrs_tile_id"]) &
            (df["flight_direction"] == row["flight_direction"])
        ].shape[0])
        tooltip = (
            f"{row['mgrs_tile_id']} {row.get('flight_direction', '')} "
            f"— {month_count} months"
        )
        folium.Polygon(
            locations=corners,
            color=color,
            weight=2,
            fill=True,
            fill_opacity=0.10,
            tooltip=tooltip,
        ).add_to(m)

    # Add clicked point marker
    click = st.session_state.get(_CLICK_KEY)
    if click:
        folium.Marker(
            [click["lat"], click["lon"]],
            icon=folium.Icon(color="orange", icon="circle", prefix="fa"),
            tooltip=f"{click['tile_id']} {click['direction']}",
        ).add_to(m)

    map_data = st_folium(
        m,
        width=None,
        height=480,
        returned_objects=["last_clicked"],
        key="mapping_folium_map",
    )

    # ── Handle click ────────────────────────────────────────────────────────
    if map_data and map_data.get("last_clicked"):
        raw = map_data["last_clicked"]
        new_click = (round(raw["lat"], 6), round(raw["lng"], 6))
        last = st.session_state.get(_LAST_CLICK_KEY)
        if new_click != last:
            st.session_state[_LAST_CLICK_KEY] = new_click
            _handle_map_click(raw["lat"], raw["lng"])

    # ── Time-series chart ───────────────────────────────────────────────────
    ts = st.session_state.get(_TS_KEY)
    if ts:
        _render_timeseries_chart(ts)


# ---------------------------------------------------------------------------
# Load handler
# ---------------------------------------------------------------------------

def _do_load(output_dir: str) -> None:
    """Load catalog and tile list from the given output directory."""
    from s1grits.analysis.io import load_catalog, list_available_tiles

    # Re-validate to catch any race between UI render and button click
    status = _validate_output_dir(output_dir)
    if not status["can_load"]:
        for err in status["errors"]:
            st.error(err)
        return

    try:
        catalog = load_catalog(output_dir)
        tiles   = list_available_tiles(output_dir)
    except Exception as exc:
        st.error(f"Failed to load catalog: {exc}")
        return

    st.session_state[_DIR_KEY]    = output_dir
    st.session_state[_CATALOG_KEY] = catalog
    st.session_state[_TILES_KEY]  = tiles
    st.session_state[_LOADED_KEY] = True
    st.session_state[_CLICK_KEY]  = None
    st.session_state[_TS_KEY]     = None
    st.session_state[_LAST_CLICK_KEY] = None
    st.rerun()


# ---------------------------------------------------------------------------
# Click handler
# ---------------------------------------------------------------------------

def _handle_map_click(lat: float, lon: float) -> None:
    """Find tile at click location, extract time series, update session state."""
    output_dir = st.session_state.get(_DIR_KEY, "")
    if not output_dir:
        return

    from s1grits.analysis.io import find_tile_by_lonlat
    from s1grits.analysis.timeseries import lonlat_to_pixel, extract_pixel_timeseries

    try:
        result = find_tile_by_lonlat(lon, lat, output_dir)
    except FileNotFoundError:
        st.info("Catalog not found — reload the folder.")
        return

    if result is None:
        st.info("No processed tile at this location.")
        st.session_state[_CLICK_KEY] = None
        st.session_state[_TS_KEY]    = None
        return

    tile_id, direction = result

    try:
        ds = _load_cached_zarr(tile_id, direction, output_dir)
    except FileNotFoundError:
        st.warning(f"Zarr data not found for tile {tile_id} {direction}.")
        return

    try:
        row, col = lonlat_to_pixel(lon, lat, ds)
        ts = extract_pixel_timeseries(ds, row, col)
    except (IndexError, ValueError) as exc:
        st.error(f"Could not extract time series: {exc}")
        return
    except Exception as exc:
        st.error(f"Unexpected error reading Zarr: {exc}")
        return

    # Compute Ratio and RVI if not present in dataset
    if ts.get("ratio_ts") is None and len(ts["vv_ts"]) > 0:
        vv_lin = 10 ** (ts["vv_ts"] / 10)
        vh_lin = 10 ** (ts["vh_ts"] / 10)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(vv_lin != 0, vh_lin / vv_lin, np.nan)
        ts["ratio_ts"] = ratio

    if ts.get("rvi_ts") is None and len(ts["vv_ts"]) > 0:
        vv_lin = 10 ** (ts["vv_ts"] / 10)
        vh_lin = 10 ** (ts["vh_ts"] / 10)
        denom = vv_lin + vh_lin
        with np.errstate(divide="ignore", invalid="ignore"):
            rvi = np.where(denom != 0, (4 * vh_lin) / denom, np.nan)
        ts["rvi_ts"] = rvi

    st.session_state[_CLICK_KEY] = {
        "lat": lat, "lon": lon,
        "tile_id": tile_id, "direction": direction,
        "valid_count": ts["valid_count"],
        "total_count": ts["total_count"],
    }
    st.session_state[_TS_KEY] = ts
    st.rerun()


# ---------------------------------------------------------------------------
# Time-series chart
# ---------------------------------------------------------------------------

def _render_timeseries_chart(ts: dict) -> None:
    """Render a Plotly multi-line time-series chart with gaps for missing months."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        st.error("plotly is required. Install with: pip install plotly")
        return

    # Build full monthly date spine 2015-01 → 2028-12
    all_dates = pd.date_range("2015-01-01", "2028-12-01", freq="MS")
    df = pd.DataFrame(index=all_dates)

    # Convert numpy datetime64 → pandas Timestamp for reindex
    ts_dates = pd.DatetimeIndex(ts["dates"])

    for var, values in [
        ("VV_dB",  ts.get("vv_ts")),
        ("VH_dB",  ts.get("vh_ts")),
        ("Ratio",  ts.get("ratio_ts")),
        ("RVI",    ts.get("rvi_ts")),
    ]:
        if values is not None and len(values) > 0:
            s = pd.Series(values, index=ts_dates)
            # Snap to month-start for alignment
            s.index = s.index.to_period("M").to_timestamp()
            df[var] = s.reindex(all_dates)
        else:
            df[var] = np.nan

    colors = {
        "VV_dB": "#0D9488",
        "VH_dB": "#3B82F6",
        "Ratio": "#F97316",
        "RVI":   "#10B981",
    }

    fig = go.Figure()
    for var in ["VV_dB", "VH_dB", "Ratio", "RVI"]:
        if df[var].notna().any():
            fig.add_trace(go.Scatter(
                x=df.index,
                y=df[var],
                name=var,
                mode="lines+markers",
                connectgaps=False,
                line=dict(color=colors[var], width=1.8),
                marker=dict(size=4),
                hovertemplate="%{x|%Y-%m}<br>" + var + ": %{y:.3f}<extra></extra>",
            ))

    fig.update_layout(
        xaxis=dict(
            range=["2015-01-01", "2028-12-31"],
            title="Date",
            tickformat="%Y",
        ),
        yaxis=dict(title="Value"),
        legend=dict(orientation="h", y=-0.25),
        margin=dict(l=0, r=0, t=30, b=0),
        height=320,
        plot_bgcolor="white",
        paper_bgcolor="white",
        hovermode="x unified",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#E5E7EB")
    fig.update_yaxes(showgrid=True, gridcolor="#E5E7EB")

    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_center(unique_tiles: pd.DataFrame) -> tuple[float, float]:
    """Return (lat, lon) center from tile catalog rows."""
    lats, lons = [], []
    for _, row in unique_tiles.iterrows():
        corners = _tile_bounds_wgs84(row)
        if corners:
            lats.extend(c[0] for c in corners)
            lons.extend(c[1] for c in corners)
    if lats:
        return float(np.mean(lats)), float(np.mean(lons))
    return 20.0, 100.0  # fallback


def _render_point_info(click: dict, ts: dict | None) -> None:
    """Render point info card in the left panel."""
    valid = ts["valid_count"] if ts else 0
    total = ts["total_count"] if ts else 0

    st.markdown(
        f'<div style="background:#F0FDFA;border:1.5px solid #CCEBE8;border-radius:8px;'
        f'padding:10px 14px;font-size:12.5px;line-height:1.8;">'
        f'<b>Lat:</b> {click["lat"]:.5f}<br>'
        f'<b>Lon:</b> {click["lon"]:.5f}<br>'
        f'<b>Tile:</b> {escape(click["tile_id"])} {escape(click["direction"])}<br>'
        f'<b>Valid months:</b> {valid} / {total}'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Year/month breakdown
    if ts and len(ts.get("dates", [])) > 0:
        dates = pd.DatetimeIndex(ts["dates"])
        by_year: dict[int, list[str]] = {}
        for d in dates:
            by_year.setdefault(d.year, []).append(d.strftime("%b"))

        _MONTH_ORDER = ["Jan","Feb","Mar","Apr","May","Jun",
                        "Jul","Aug","Sep","Oct","Nov","Dec"]

        rows_html = ""
        for year in sorted(by_year):
            months = sorted(by_year[year], key=lambda m: _MONTH_ORDER.index(m))
            month_tags = "".join(
                f'<span style="display:inline-block;background:#0D9488;color:white;'
                f'border-radius:4px;padding:1px 6px;font-size:10.5px;margin:1px 2px;">'
                f'{m}</span>'
                for m in months
            )
            rows_html += (
                f'<div style="display:flex;align-items:flex-start;gap:8px;'
                f'margin-bottom:4px;">'
                f'<span style="font-size:11.5px;font-weight:700;color:#134E4A;'
                f'min-width:36px;padding-top:2px;">{year}</span>'
                f'<div style="flex:1;">{month_tags}</div>'
                f'</div>'
            )

        st.markdown(
            f'<div style="margin-top:8px;background:#FAFFFE;border:1px solid #CCEBE8;'
            f'border-radius:8px;padding:10px 12px;">'
            f'<div style="font-size:10.5px;font-weight:700;color:#4D7C78;'
            f'text-transform:uppercase;letter-spacing:0.08em;margin-bottom:8px;">'
            f'Coverage by Year</div>'
            f'{rows_html}'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)


def _validate_output_dir(path_str: str) -> dict:
    """
    Check directory, catalog.parquet, STAC collection.json, and tile count.

    Strategy:
    - If path_str points to a catalog.parquet file directly, resolve its parent.
    - catalog.parquet missing → warning (not error); Load is still enabled so
      the user can run Catalog > Rebuild after inspecting the folder.
    - Only hard errors (non-existent path, not a directory) block the Load button.
    """
    result: dict = {
        "can_load":       False,
        "dir_exists":     False,
        "catalog_exists": False,
        "stac_exists":    False,
        "tile_count":     0,
        "errors":         [],
        "warnings":       [],
        "resolved_dir":   path_str,   # may be updated if user drops a .parquet file
    }

    if not path_str:
        return result

    p = Path(path_str)

    # Allow user to paste the path to catalog.parquet itself
    if p.is_file() and p.suffix.lower() == ".parquet" and p.name == "catalog.parquet":
        p = p.parent
        result["resolved_dir"] = str(p)

    if not p.exists():
        result["errors"].append("Directory does not exist.")
        return result
    if not p.is_dir():
        result["errors"].append("Path is not a directory.")
        return result

    result["dir_exists"] = True

    # catalog.parquet — show warning, but still allow Load
    if (p / "catalog.parquet").exists():
        result["catalog_exists"] = True
    else:
        result["warnings"].append(
            "catalog.parquet not found — run Catalog > Rebuild first."
        )

    # STAC collection.json — optional
    if (p / "collection.json").exists():
        result["stac_exists"] = True

    # Count tile directories that have a Zarr store
    try:
        result["tile_count"] = sum(
            1 for d in p.iterdir()
            if d.is_dir() and "_" in d.name
            and (d / "zarr" / "S1_monthly.zarr").exists()
        )
        if result["tile_count"] == 0:
            result["warnings"].append("No processed tiles found (no Zarr stores).")
    except PermissionError:
        result["warnings"].append("Cannot read directory (permission denied).")

    # Load is allowed as long as the directory exists — catalog issues are warnings
    result["can_load"] = result["dir_exists"]
    return result


def _render_dir_status(s: dict) -> None:
    """Render compact status rows below the directory text input."""
    if not s["dir_exists"] and not s["errors"]:
        return  # empty input — show nothing

    def _row(icon: str, color: str, text: str) -> str:
        return (
            f'<div style="display:flex;align-items:center;gap:6px;'
            f'font-size:11.5px;color:{color};margin-top:3px;">'
            f'<span style="font-weight:700;">{icon}</span>{escape(text)}</div>'
        )

    rows = ""
    for err in s["errors"]:
        rows += _row("[ERROR]", "#DC2626", err)

    if s["catalog_exists"]:
        rows += _row("[OK]", "#059669", "catalog.parquet found")
    elif s["dir_exists"]:
        rows += _row("[WARN]", "#D97706",
                     "catalog.parquet not found — run Catalog > Rebuild first.")

    if s["tile_count"] > 0:
        rows += _row("[OK]", "#059669", f"{s['tile_count']} tile(s) with Zarr data")
    elif s["dir_exists"] and s["tile_count"] == 0 and s["catalog_exists"]:
        rows += _row("[WARN]", "#D97706", "No processed tiles found (no Zarr stores).")

    if rows:
        st.markdown(
            f'<div style="margin:6px 0 8px 0;">{rows}</div>',
            unsafe_allow_html=True,
        )


def _step_header(num: int, title: str, subtitle: str = "") -> None:
    sub_html = (
        f'<span style="font-size:11.5px;font-weight:400;color:#80A8A4;margin-left:6px;">'
        f'— {escape(subtitle)}</span>'
        if subtitle else ""
    )
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:10px;'
        f'margin-bottom:12px;padding-bottom:8px;border-bottom:1.5px solid #CCEBE8;">'
        f'<span style="width:22px;height:22px;border-radius:50%;background:#0D9488;'
        f'color:white;font-size:11px;font-weight:700;display:flex;align-items:center;'
        f'justify-content:center;flex-shrink:0;">{num}</span>'
        f'<span style="font-size:13px;font-weight:700;color:#134E4A;">{escape(title)}</span>'
        f'{sub_html}</div>',
        unsafe_allow_html=True,
    )