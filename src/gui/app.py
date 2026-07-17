"""
S1-GRiTS GUI — Main Streamlit application entry point.

Layout:
  - Full-page gradient background with custom CSS
  - Navbar: brand logo in col[0], 5 centered nav buttons in cols[1-5], spacer col[6]
  - Tab content: Process | Mapping | Catalog | Mosaic | Tile
  - Each tab delegates to its page module
"""

import sys
from pathlib import Path

import streamlit as st

# When running directly (streamlit run src/gui/app.py), ensure src/ is on
# sys.path so that `gui.*` imports resolve correctly.
# When installed via pip, gui is already importable as a package.
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from gui.styles import inject_css, render_footer
from gui.pages import page_process, page_catalog, page_tile, page_mosaic, page_mapping

# ---------------------------------------------------------------------------
# Page configuration — must be the very first Streamlit call
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="S1-GRiTS — Sentinel-1 Processor",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="collapsed",
    menu_items={
        "Get Help": "https://github.com/yourusername/s1-grits-core",
        "Report a bug": "https://github.com/yourusername/s1-grits-core/issues",
        "About": "S1-GRiTS — Sentinel-1 Gridded Time-Series Processor v1.0.0",
    },
)

# ---------------------------------------------------------------------------
# Deprecation: this Streamlit GUI is superseded by the v2.3 web interface
# (`s1grits serve`, see docs/webapp.md). It remains functional for now and
# will be removed in v3.0.0; new features land in the web UI only.
# ---------------------------------------------------------------------------
st.warning(
    "**This Streamlit GUI is deprecated** and will be removed in v3.0.0. "
    " Use the new web interface instead: `s1grits serve --root "
    "<workspace>` (see docs/webapp.md). New features land in the web UI only.",
    icon="⚠️",
)

# ---------------------------------------------------------------------------
# Active tab state
# Tab order: Process=0, Mapping=1, Catalog=2, Tile=3, Mosaic=4
# ---------------------------------------------------------------------------
TAB_NAMES = ["Process", "Mapping", "Catalog", "Tile", "Mosaic"]

if "active_tab" not in st.session_state:
    st.session_state["active_tab"] = 0

active_idx: int = st.session_state["active_tab"]

# ---------------------------------------------------------------------------
# Inject global CSS
# ---------------------------------------------------------------------------
inject_css()

# ---------------------------------------------------------------------------
# Navbar — single st.columns() row
# col[0] = brand (logo + name + badge)
# cols[1-5] = nav buttons, centered via equal weights
# col[6] = right spacer so buttons stay centered, not right-aligned
# ---------------------------------------------------------------------------

_ICON_SVG = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>'
    '</svg>'
)

# Inject a white background band that sits behind the navbar row.
# We cannot wrap st.columns() in a <div>, so instead we inject a full-width
# sticky white bar as a ::before layer and also force the stHorizontalBlock
# background via a targeted inline style injected right before it renders.
st.markdown(
    """
    <style>
    /* Nuclear: strip ALL block/column backgrounds in Streamlit 1.56 */
    [data-testid="stColumn"],
    [data-testid="column"],
    div[data-testid^="column"],
    .stColumn,
    [class*="stColumn"],
    [data-testid="stVerticalBlock"],
    div[data-testid="stVerticalBlock"],
    [data-testid="stVerticalBlockBorderWrapper"],
    div[data-testid="stVerticalBlockBorderWrapper"],
    [data-testid="stHorizontalBlock"],
    div[data-testid="stHorizontalBlock"],
    [data-testid="stHorizontalBlock"] > div,
    div[data-testid="stHorizontalBlock"] > div,
    div[data-testid="stElementContainer"],
    [data-testid="stLayoutWrapper"],
    div[data-testid="stLayoutWrapper"],
    .e1rw0b1u0, .e1rw0b1u1, .e1rw0b1u2, .e1rw0b1u3, .e1rw0b1u4 {
        background: transparent !important;
        background-color: transparent !important;
        background-image: none !important;
        box-shadow: none !important;
    }
    /* Restore navbar background using precise :has() selector */
    [data-testid="stHorizontalBlock"]:has(.st-key-nav_process) {
        background: #FFFFFF !important;
        background-image: none !important;
        border-bottom: 1.5px solid #E8F5F3 !important;
        box-shadow: 0 2px 14px rgba(0,0,0,0.07) !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
col_brand, col_n0, col_n1, col_n2, col_n3, col_n4, col_spacer = st.columns(
    [3, 1, 1, 1, 1, 1, 3], gap="small"
)

with col_brand:
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:10px;height:48px;">'
        f'  <div style="width:34px;height:34px;background:#0D9488;border-radius:9px;'
        f'       display:flex;align-items:center;justify-content:center;flex-shrink:0;'
        f'       box-shadow:0 3px 10px rgba(13,148,136,0.35);">'
        f'    {_ICON_SVG}'
        f'  </div>'
        f'  <span style="font-size:18px;font-weight:800;color:#134E4A;'
        f'       letter-spacing:-0.5px;white-space:nowrap;">S1-GRiTS</span>'
        f'  <span style="font-size:11px;font-weight:600;color:#4D7C78;'
        f'       background:#F0FDFA;border:1px solid #CCEBE8;border-radius:12px;'
        f'       padding:3px 10px;margin-left:4px;white-space:nowrap;">v1.0.0</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

with col_n0:
    if st.button("Process", key="nav_process", use_container_width=True,
                 type="primary" if active_idx == 0 else "secondary"):
        st.session_state["active_tab"] = 0
        st.rerun()

with col_n1:
    if st.button("Mapping", key="nav_mapping", use_container_width=True,
                 type="primary" if active_idx == 1 else "secondary"):
        st.session_state["active_tab"] = 1
        st.rerun()

with col_n2:
    if st.button("Catalog", key="nav_catalog", use_container_width=True,
                 type="primary" if active_idx == 2 else "secondary"):
        st.session_state["active_tab"] = 2
        st.rerun()

with col_n3:
    if st.button("Tile", key="nav_tile", use_container_width=True,
                 type="primary" if active_idx == 3 else "secondary"):
        st.session_state["active_tab"] = 3
        st.rerun()

with col_n4:
    if st.button("Mosaic", key="nav_mosaic", use_container_width=True,
                 type="primary" if active_idx == 4 else "secondary"):
        st.session_state["active_tab"] = 4
        st.rerun()

# col_spacer intentionally left empty — acts as a right-side counterweight

# Spacer below navbar
st.markdown('<div class="s1-content-spacer"></div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Tab content — render only the active tab
# ---------------------------------------------------------------------------
if active_idx == 0:
    page_process.render()
elif active_idx == 1:
    page_mapping.render()
elif active_idx == 2:
    page_catalog.render()
elif active_idx == 3:
    page_tile.render()
elif active_idx == 4:
    page_mosaic.render()

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
render_footer()
