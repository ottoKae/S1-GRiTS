"""
CSS theme constants and injection utilities for S1-GRiTS GUI.
Design system: Flat Design, teal primary (#0D9488), orange CTA (#F97316).
Font: Plus Jakarta Sans (body) + JetBrains Mono (log/code).
Matches the style of the S1-GRiTS landing page.
All styling is injected via st.markdown with unsafe_allow_html=True.
"""

# ---------------------------------------------------------------------------
# Color tokens (mirrors landing page design system)
# ---------------------------------------------------------------------------
COLOR = {
    "primary":       "#0D9488",   # teal-600
    "primary_light": "#14B8A6",   # teal-500
    "primary_dim":   "rgba(13,148,136,0.12)",
    "primary_border":"rgba(13,148,136,0.30)",
    "cta":           "#F97316",   # orange-500
    "cta_hover":     "#EA6C0A",
    "bg_page":       "#F0FDFA",   # teal-50
    "bg_card":       "#FFFFFF",
    "bg_surface":    "#F8FFFE",
    "bg_code":       "#F0FDFA",
    "text_dark":     "#134E4A",   # teal-900
    "text_mid":      "#1F6B62",   # teal-800
    "text_mute":     "#4D7C78",   # teal-700
    "text_faint":    "#80A8A4",
    "border":        "#CCEBE8",   # teal-100
    "border_mid":    "#99D6D0",
    # semantic
    "success":       "#10B981",   # emerald-500
    "warning":       "#F59E0B",   # amber-500
    "error":         "#EF4444",   # red-500
    "info":          "#0D9488",
}

MAIN_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');

/* ── Global reset & base font ── */
html, body, [class*="css"] {
    font-family: 'Plus Jakarta Sans', 'Segoe UI', system-ui, -apple-system, sans-serif !important;
    font-size: 15px !important;
    color: #134E4A !important;
}
#MainMenu { visibility: hidden; }
footer    { visibility: hidden; }
header    { visibility: hidden; }
html { scroll-behavior: smooth; }

/* ── Page-level gradient background (remove all Streamlit white) ── */
html, body,
.stApp,
[data-testid="stAppViewContainer"],
[data-testid="stAppViewBlockContainer"],
section[data-testid="stMain"],
div[data-testid="stMain"],
.main {
    background: linear-gradient(160deg, #EDFAF8 0%, #F5FEFA 45%, #E2F8F3 100%) !important;
    background-attachment: fixed !important;
}

/* ── Nuclear transparency reset: ALL block/column/container wrappers ── */
/* Targets both data-testid selectors AND Streamlit 1.56 emotion CSS classes  */
/* (e1rw0b1u0..4 = element container, column, flex block, layout wrapper)     */
[data-testid="stColumn"],
[data-testid="column"],
div[data-testid^="column"],
.stColumn,
[class*="stColumn"],
[data-testid="stAppViewBlockContainer"] > div,
div[data-testid="stExpanderDetails"] [data-testid="stColumn"],
div[data-testid="stExpanderDetails"] [data-testid^="column"],
div[data-testid="stExpanderDetails"] .stColumn,
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
/* Streamlit 1.56 emotion CSS classes for block containers */
.e1rw0b1u0, .e1rw0b1u1, .e1rw0b1u2, .e1rw0b1u3, .e1rw0b1u4 {
    background: transparent !important;
    background-color: transparent !important;
    background-image: none !important;
    box-shadow: none !important;
}

/* ── Page container ── */
.main .block-container {
    padding-left: 2.5rem !important;
    padding-right: 2.5rem !important;
    padding-bottom: 2rem !important;
    padding-top: 0 !important;
    max-width: 1600px !important;
    margin-top: 0 !important;
    background: transparent !important;
}
/* Kill Streamlit's injected top gap */
.main .block-container > div:first-child { padding-top: 0 !important; margin-top: 0 !important; }
[data-testid="stVerticalBlock"] { gap: 0.5rem; }
/* Content spacer below navbar */
.s1-content-spacer { height: 20px; }

/* ================================================================
   NAVBAR — sticky white bar, stays in document flow.
   Uses :has(.st-key-nav_process) to PRECISELY target only the navbar
   row (the one containing the nav_process button), instead of the
   broken :first-of-type which matches every first stHorizontalBlock
   inside every parent container — causing white boxes on all pages.
   ================================================================ */
[data-testid="stHorizontalBlock"]:has(.st-key-nav_process) {
    position: sticky !important;
    top: 0 !important;
    z-index: 999 !important;
    background: #FFFFFF !important;
    background-image: none !important;
    border-bottom: 1.5px solid #E8F5F3 !important;
    box-shadow: 0 2px 14px rgba(0,0,0,0.07) !important;
    margin-left: -2.5rem !important;
    margin-right: -2.5rem !important;
    padding-left: 2.5rem !important;
    padding-right: 2.5rem !important;
    padding-top: 12px !important;
    padding-bottom: 12px !important;
}
[data-testid="stHorizontalBlock"]:has(.st-key-nav_process) > div {
    background: transparent !important;
    background-image: none !important;
}

/* ================================================================
   FOOTER  —  dark single-column, brand + copyright bar
   ================================================================ */
.s1-footer {
    margin: 40px -2.5rem 0 -2.5rem;
    background: #134E4A;
    padding: 40px 2.5rem 0 2.5rem;
}
/* Brand row */
.s1-footer-logo-box {
    width: 34px; height: 34px;
    background: #0D9488;
    border-radius: 9px;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
}
.s1-footer-brand-row {
    display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
}
.s1-footer-brand-name {
    font-size: 16px; font-weight: 700; color: #FFFFFF; letter-spacing: -0.3px;
}
.s1-footer-brand-desc {
    font-size: 13px; color: rgba(255,255,255,0.50); line-height: 1.6;
    max-width: 520px; margin-bottom: 28px;
}
/* Bottom copyright bar */
.s1-footer-bottom {
    border-top: 1px solid rgba(255,255,255,0.10);
    padding: 16px 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
}
.s1-footer-copy {
    font-size: 12.5px; color: rgba(255,255,255,0.35); font-weight: 400;
}
.s1-footer-github {
    display: inline-flex; align-items: center; gap: 8px;
    font-size: 13px; font-weight: 500; color: rgba(255,255,255,0.55);
    text-decoration: none;
    transition: color 150ms ease;
}
.s1-footer-github:hover { color: #FFFFFF; text-decoration: none; }

/* ================================================================
   SECTION TITLE
   ================================================================ */
.s1-section-title {
    font-size: 11.5px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.10em; color: #4D7C78;
    margin: 0 0 12px 0;
    display: flex; align-items: center; gap: 7px;
    padding-bottom: 8px; border-bottom: 1.5px solid #CCEBE8;
}

/* ================================================================
   STATUS BADGE
   ================================================================ */
.status-wrap {
    display: flex; align-items: center; justify-content: space-between;
    background: #FFFFFF; border: 1.5px solid #CCEBE8;
    border-radius: 12px; padding: 12px 16px; margin-bottom: 12px;
    box-shadow: 0 1px 4px rgba(13,148,136,0.06);
}
.status-badge {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 16px; border-radius: 20px;
    font-size: 13px; font-weight: 600; letter-spacing: 0.03em;
}
.status-idle    { background: #F1F5F9; color: #475569; border: 1.5px solid #E2E8F0; }
.status-running { background: rgba(13,148,136,0.10); color: #0D9488; border: 1.5px solid rgba(13,148,136,0.30); animation: badge-pulse 2s ease-in-out infinite; }
.status-success { background: rgba(16,185,129,0.10); color: #059669; border: 1.5px solid rgba(16,185,129,0.30); }
.status-failed  { background: rgba(239,68,68,0.10);  color: #DC2626; border: 1.5px solid rgba(239,68,68,0.30); }
.status-stopped { background: rgba(245,158,11,0.10); color: #B45309; border: 1.5px solid rgba(245,158,11,0.30); }
.status-meta { font-size: 12.5px; color: #4D7C78; text-align: right; line-height: 1.7; }
.status-meta-val { color: #134E4A; font-weight: 600; }
@keyframes badge-pulse {
    0%,100% { box-shadow: 0 0 0 0 rgba(13,148,136,0.20); }
    50%      { box-shadow: 0 0 0 6px rgba(13,148,136,0); }
}

/* ================================================================
   LOG VIEWER  (self-contained iframe — CSS here is for outer shell only)
   ================================================================ */
.log-outer {
    background: #FFFFFF; border: 1.5px solid #CCEBE8;
    border-radius: 12px; overflow: hidden; margin-bottom: 12px;
    box-shadow: 0 1px 4px rgba(13,148,136,0.06);
}

/* ================================================================
   PATH PANEL
   ================================================================ */
.path-outer {
    background: #FFFFFF; border: 1.5px solid #CCEBE8;
    border-radius: 12px; padding: 12px 16px; margin-bottom: 12px;
    box-shadow: 0 1px 4px rgba(13,148,136,0.06);
}
.path-outer-title {
    font-size: 11px; font-weight: 700; color: #4D7C78;
    text-transform: uppercase; letter-spacing: 0.10em; margin-bottom: 10px;
}
.path-row { display: flex; align-items: center; gap: 10px; margin-bottom: 5px; }
.path-row:last-child { margin-bottom: 0; }
.path-lbl {
    font-size: 11px; color: #80A8A4; width: 36px; flex-shrink: 0;
    font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em;
}
.path-val {
    font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #1F6B62;
    background: #F0FDFA; border: 1px solid #CCEBE8; border-radius: 6px;
    padding: 4px 10px; flex: 1; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
}

/* ================================================================
   COMPLETENESS CARD
   ================================================================ */
.completeness-card {
    background: #FFFFFF; border: 1.5px solid #CCEBE8;
    border-radius: 12px; padding: 20px 16px; text-align: center;
    transition: box-shadow 150ms ease, border-color 150ms ease;
}
.completeness-card:hover { border-color: #0D9488; }
.completeness-dir-label { font-size: 11px; font-weight: 700; color: #4D7C78; text-transform: uppercase; letter-spacing: 0.12em; margin-bottom: 10px; }
.completeness-pct { font-size: 40px; font-weight: 800; line-height: 1; letter-spacing: -1.5px; }
.completeness-bar-bg { width: 100%; height: 6px; background: #CCEBE8; border-radius: 99px; margin: 10px 0 8px; overflow: hidden; }
.completeness-bar-fill { height: 100%; border-radius: 99px; transition: width 0.6s ease; }
.completeness-months { font-size: 13px; color: #4D7C78; margin-top: 2px; font-weight: 500; }

/* ================================================================
   STAT ROW
   ================================================================ */
.stat-row { display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.stat-card {
    flex: 1; min-width: 80px;
    background: #FFFFFF; border: 1.5px solid #CCEBE8; border-radius: 12px;
    padding: 14px; text-align: center;
    box-shadow: 0 1px 4px rgba(13,148,136,0.06);
    transition: box-shadow 150ms ease, transform 150ms ease;
}
.stat-card:hover { box-shadow: 0 4px 14px rgba(13,148,136,0.12); transform: translateY(-1px); }
.stat-value { font-size: 28px; font-weight: 800; color: #134E4A; line-height: 1; letter-spacing: -0.5px; }
.stat-label { font-size: 11px; color: #4D7C78; margin-top: 5px; text-transform: uppercase; letter-spacing: 0.10em; font-weight: 700; }

/* ================================================================
   TIP BOX
   ================================================================ */
.s1-tip {
    background: #FFFFFF;
    border: 1.5px solid rgba(13,148,136,0.18);
    border-left: 3px solid #0D9488;
    border-radius: 0 10px 10px 0;
    padding: 10px 14px; font-size: 13.5px; color: #1F6B62;
    margin: 10px 0; line-height: 1.65;
    box-shadow: 0 1px 4px rgba(13,148,136,0.06);
}
.s1-tip strong { color: #0D9488; }

/* ================================================================
   COMMAND PREVIEW
   ================================================================ */
.cmd-preview {
    background: #F0FDFA; border: 1.5px solid #CCEBE8; border-radius: 10px;
    padding: 10px 14px; font-family: 'JetBrains Mono', monospace;
    font-size: 12px; color: #4D7C78; margin-top: 10px;
    word-break: break-all; line-height: 1.6;
}
.cmd-keyword { color: #0D9488; font-weight: 600; }
.cmd-flag    { color: #7C3AED; }
.cmd-value   { color: #059669; }

/* ================================================================
   STREAMLIT WIDGET OVERRIDES
   ================================================================ */

/* Primary button */
div[data-testid="stButton"] > button[kind="primary"] {
    background: #F97316 !important;
    border: none !important; border-radius: 10px !important;
    font-weight: 700 !important; font-size: 15px !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    padding: 11px 0 !important; width: 100% !important; color: white !important;
    transition: background 150ms ease, box-shadow 150ms ease, transform 150ms ease !important;
    box-shadow: 0 2px 8px rgba(249,115,22,0.30) !important;
    letter-spacing: 0.01em !important;
}
div[data-testid="stButton"] > button[kind="primary"]:hover:not(:disabled) {
    background: #EA6C0A !important;
    box-shadow: 0 5px 16px rgba(249,115,22,0.40) !important;
    transform: translateY(-1px) !important;
}
div[data-testid="stButton"] > button[kind="primary"]:active:not(:disabled) {
    transform: translateY(0) scale(0.99) !important;
}
div[data-testid="stButton"] > button[kind="primary"]:disabled {
    opacity: 0.45 !important; transform: none !important; box-shadow: none !important;
    background: #F97316 !important;
}

/* Secondary button (Stop) */
div[data-testid="stButton"] > button[kind="secondary"] {
    background: rgba(239,68,68,0.06) !important;
    border: 1.5px solid rgba(239,68,68,0.35) !important;
    color: #DC2626 !important; border-radius: 10px !important;
    font-weight: 700 !important; font-size: 15px !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    padding: 11px 0 !important; width: 100% !important;
    transition: all 150ms ease !important;
}
div[data-testid="stButton"] > button[kind="secondary"]:hover:not(:disabled) {
    background: rgba(239,68,68,0.12) !important;
    border-color: rgba(239,68,68,0.55) !important;
}
div[data-testid="stButton"] > button[kind="secondary"]:disabled { opacity: 0.35 !important; }

/* Generic (tertiary) buttons */
div[data-testid="stButton"] > button:not([kind="primary"]):not([kind="secondary"]) {
    background: #FFFFFF !important;
    border: 1.5px solid #CCEBE8 !important;
    color: #134E4A !important; border-radius: 10px !important;
    font-weight: 600 !important; font-size: 14px !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    transition: all 150ms ease !important;
    box-shadow: 0 1px 3px rgba(13,148,136,0.06) !important;
}
div[data-testid="stButton"] > button:not([kind="primary"]):not([kind="secondary"]):hover {
    border-color: #0D9488 !important; color: #0D9488 !important;
    box-shadow: 0 2px 8px rgba(13,148,136,0.14) !important;
}

/* Expander */
div[data-testid="stExpander"] > details > summary {
    background: #FFFFFF !important; border: 1.5px solid #CCEBE8 !important;
    border-radius: 10px !important; padding: 10px 14px !important;
    font-weight: 600 !important; font-size: 14px !important; color: #4D7C78 !important;
    transition: all 150ms ease !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    box-shadow: 0 1px 3px rgba(13,148,136,0.06) !important;
}
div[data-testid="stExpander"] > details > summary:hover {
    border-color: #0D9488 !important; color: #134E4A !important;
}
div[data-testid="stExpander"] > details[open] > summary {
    border-radius: 10px 10px 0 0 !important; color: #0D9488 !important; border-color: #0D9488 !important;
}
div[data-testid="stExpanderDetails"] {
    border: 1.5px solid #0D9488 !important; border-top: none !important;
    border-radius: 0 0 10px 10px !important; padding: 14px 16px !important;
    background: #F0FDFA !important;
}

/* Text inputs / textareas / number inputs */
div[data-testid="stTextInput"] > div > div > input,
div[data-testid="stTextArea"] > div > div > textarea,
div[data-testid="stNumberInput"] > div > div > input {
    background-color: #FFFFFF !important; border: 1.5px solid #CCEBE8 !important;
    border-radius: 8px !important; color: #134E4A !important;
    font-size: 14px !important; font-family: 'Plus Jakarta Sans', sans-serif !important;
    transition: border-color 150ms ease, box-shadow 150ms ease !important;
}
div[data-testid="stTextInput"] > div > div > input:focus,
div[data-testid="stTextArea"] > div > div > textarea:focus {
    border-color: #0D9488 !important;
    box-shadow: 0 0 0 3px rgba(13,148,136,0.12) !important; outline: none !important;
}
div[data-testid="stTextInput"] > div > div > input::placeholder,
div[data-testid="stTextArea"] > div > div > textarea::placeholder { color: #99D6D0 !important; }

/* Disabled inputs — used for read-only review fields */
div[data-testid="stTextInput"] > div > div > input:disabled,
div[data-testid="stTextArea"] > div > div > textarea:disabled {
    background-color: #F8FFFE !important; color: #1F6B62 !important;
    border-color: #CCEBE8 !important; opacity: 1 !important;
    -webkit-text-fill-color: #1F6B62 !important;
}

/* Selectbox */
div[data-testid="stSelectbox"] > div > div {
    background-color: #FFFFFF !important; border: 1.5px solid #CCEBE8 !important;
    border-radius: 8px !important; color: #134E4A !important;
    font-size: 14px !important;
    transition: border-color 150ms ease !important;
}
div[data-testid="stSelectbox"] > div > div:focus-within {
    border-color: #0D9488 !important;
    box-shadow: 0 0 0 3px rgba(13,148,136,0.12) !important;
}

/* Widget labels */
label[data-testid="stWidgetLabel"] > div > p {
    font-size: 13.5px !important; font-weight: 600 !important;
    color: #4D7C78 !important; margin-bottom: 5px !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
}

/* Radio */
div[data-testid="stRadio"] label,
div[data-testid="stRadio"] [data-testid="stMarkdownContainer"] p {
    font-size: 14.5px !important; color: #134E4A !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important; font-weight: 500 !important;
}

/* Checkbox */
div[data-testid="stCheckbox"] label {
    font-size: 14.5px !important; color: #134E4A !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important; font-weight: 500 !important;
}

/* Toggle */
div[data-testid="stToggle"] label {
    font-size: 14.5px !important; color: #134E4A !important; font-weight: 500 !important;
}

/* Multiselect tags */
div[data-testid="stMultiSelect"] span[data-baseweb="tag"] {
    background: rgba(13,148,136,0.12) !important;
    border: 1px solid rgba(13,148,136,0.30) !important;
    border-radius: 5px !important; color: #0D9488 !important;
    font-size: 12.5px !important; font-weight: 600 !important;
}
div[data-testid="stMultiSelect"] [data-baseweb="input"] {
    background: #FFFFFF !important; border: 1.5px solid #CCEBE8 !important;
    border-radius: 8px !important; font-size: 14px !important;
}

/* Alert boxes */
div[data-testid="stSuccess"] {
    background: rgba(16,185,129,0.08) !important;
    border: 1.5px solid rgba(16,185,129,0.28) !important;
    border-radius: 10px !important; font-size: 14px !important; color: #065F46 !important;
}
div[data-testid="stError"] {
    background: rgba(239,68,68,0.08) !important;
    border: 1.5px solid rgba(239,68,68,0.28) !important;
    border-radius: 10px !important; font-size: 14px !important; color: #7F1D1D !important;
}
div[data-testid="stWarning"] {
    background: rgba(245,158,11,0.08) !important;
    border: 1.5px solid rgba(245,158,11,0.28) !important;
    border-radius: 10px !important; font-size: 14px !important; color: #78350F !important;
}
div[data-testid="stInfo"] {
    background: rgba(13,148,136,0.08) !important;
    border: 1.5px solid rgba(13,148,136,0.25) !important;
    border-radius: 10px !important; font-size: 14px !important; color: #134E4A !important;
}

/* Dataframe */
div[data-testid="stDataFrame"] {
    border: 1.5px solid #CCEBE8 !important; border-radius: 10px !important;
    overflow: hidden !important; box-shadow: 0 1px 4px rgba(13,148,136,0.06) !important;
}

/* File uploader */
div[data-testid="stFileUploader"] {
    background: #FFFFFF !important; border: 1.5px dashed #99D6D0 !important;
    border-radius: 10px !important; transition: border-color 150ms ease !important;
}
div[data-testid="stFileUploader"]:hover { border-color: #0D9488 !important; }

/* Number input +/- buttons */
div[data-testid="stNumberInput"] button {
    background: #F0FDFA !important; border-color: #CCEBE8 !important; color: #0D9488 !important;
}

/* Slider */
div[data-testid="stSlider"] > div > div > div > div { color: #0D9488 !important; }

/* Divider */
hr { border-color: #CCEBE8 !important; margin: 14px 0 !important; }

/* Code blocks */
code, pre {
    font-family: 'JetBrains Mono', 'Consolas', monospace !important;
    font-size: 12.5px !important; background: #F0FDFA !important;
    color: #1F6B62 !important; border: 1px solid #CCEBE8 !important;
    border-radius: 6px !important;
}

/* Reduced motion */
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        transition-duration: 0.01ms !important; animation-duration: 0.01ms !important;
    }
}

/* ================================================================
   NAV BUTTON OVERRIDES — declared last so they win cascade over
   the generic stButton rules above.
   ================================================================ */

/* ── Inactive nav buttons: clean ghost style ── */
[data-testid="stHorizontalBlock"]:has(.st-key-nav_process) button[data-testid="baseButton-secondary"] {
    background: transparent !important;
    border: none !important;
    border-bottom: 3px solid transparent !important;
    color: #4D7C78 !important;
    font-size: 14px !important;
    font-weight: 600 !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    border-radius: 0 !important;
    padding: 10px 4px !important;
    letter-spacing: -0.2px !important;
    box-shadow: none !important;
    width: 100% !important;
    transition: color 150ms ease, border-color 150ms ease, background 150ms ease !important;
    text-align: center !important;
}
[data-testid="stHorizontalBlock"]:has(.st-key-nav_process) button[data-testid="baseButton-secondary"]:hover {
    color: #134E4A !important;
    background: rgba(13,148,136,0.05) !important;
    border-bottom-color: rgba(13,148,136,0.30) !important;
    transform: none !important;
    box-shadow: none !important;
}

/* ── Active nav button: teal text + teal underline, no fill ── */
[data-testid="stHorizontalBlock"]:has(.st-key-nav_process) button[data-testid="baseButton-primary"] {
    background: transparent !important;
    border: none !important;
    border-bottom: 3px solid #0D9488 !important;
    color: #0D9488 !important;
    font-size: 14px !important;
    font-weight: 700 !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    border-radius: 0 !important;
    padding: 10px 4px !important;
    letter-spacing: -0.2px !important;
    box-shadow: none !important;
    width: 100% !important;
    transform: none !important;
    text-align: center !important;
}
[data-testid="stHorizontalBlock"]:has(.st-key-nav_process) button[data-testid="baseButton-primary"]:hover {
    background: rgba(13,148,136,0.06) !important;
    transform: none !important;
    box-shadow: none !important;
}
</style>
"""


from html import escape
import re


def inject_css() -> None:
    """Inject the full CSS design system into the Streamlit page."""
    import streamlit as st
    st.markdown(MAIN_CSS, unsafe_allow_html=True)


def render_brand(version: str = "1.0.0") -> None:
    """
    Render the brand section of the navbar (logo + name + badge).
    The nav tab buttons are rendered separately in app.py as st.button widgets
    so they trigger Python reruns without any page navigation.
    """
    import streamlit as st
    icon_svg = (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        'stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
        '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>'
        '</svg>'
    )
    st.markdown(
        f'<div class="s1-header">'
        f'  <div class="s1-header-logo-box">{icon_svg}</div>'
        f'  <span class="s1-header-brand-name">S1-GRiTS</span>'
        f'  <div class="s1-header-spacer"></div>'
        f'  <div class="s1-header-badge">'
        f'    <span class="s1-header-badge-dot"></span>'
        f'    v{version}'
        f'  </div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def render_footer() -> None:
    """Render the dark footer with brand info, GitHub link, and copyright bar."""
    import streamlit as st
    icon_svg = (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        'stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
        '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>'
        '</svg>'
    )
    github_svg = (
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">'
        '<path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105'
        '.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035'
        '-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015'
        ' 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305'
        '.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225'
        '-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405'
        ' 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88'
        '.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925'
        '.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69'
        '.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/>'
        '</svg>'
    )
    st.markdown(
        f'<div class="s1-footer">'
        f'  <div class="s1-footer-brand-row">'
        f'    <div class="s1-footer-logo-box">{icon_svg}</div>'
        f'    <span class="s1-footer-brand-name">S1-GRiTS</span>'
        f'  </div>'
        f'  <p class="s1-footer-brand-desc">'
        f'    Sentinel-1 Gridded Time-Series Processor &mdash; automated SAR processing '
        f'    for land cover monitoring and change detection at scale.'
        f'  </p>'
        f'  <div class="s1-footer-bottom">'
        f'    <span class="s1-footer-copy">&copy; 2026 S1-GRiTS Project. All rights reserved.</span>'
        f'    <a class="s1-footer-github" href="https://github.com/yourusername/s1-grits-core" target="_blank">'
        f'      {github_svg} View on GitHub'
        f'    </a>'
        f'  </div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def render_section_title(icon: str, title: str) -> None:
    """Render an uppercase section heading with a left icon."""
    import streamlit as st
    st.markdown(
        f'<div class="s1-section-title"><span>{icon}</span>{title}</div>',
        unsafe_allow_html=True,
    )


# Alias kept for backward compatibility
# render_card_header alias removed (unused)


def render_tip(text: str) -> None:
    """Render a teal left-border tip box."""
    import streamlit as st
    st.markdown(f'<div class="s1-tip">{text}</div>', unsafe_allow_html=True)


def render_status_badge(status: str, started: str = "", duration: str = "") -> str:
    """Return HTML for the status badge panel."""
    dots   = {"idle": "●", "running": "◉", "success": "✓", "failed": "✗", "stopped": "■"}
    labels = {
        "idle":    "Idle",
        "running": "Running",
        "success": "Completed",
        "failed":  "Failed",
        "stopped": "Stopped",
    }
    dot   = dots.get(status, "●")
    label = labels.get(status, status.title())
    meta  = ""
    if started:
        meta += f"Started &nbsp;<span class='status-meta-val'>{escape(started)}</span><br>"
    if duration:
        meta += f"Duration &nbsp;<span class='status-meta-val'>{escape(duration)}</span>"
    return f"""
    <div class="status-wrap">
        <span class="status-badge status-{status}">{dot} {label}</span>
        <div class="status-meta">{meta}</div>
    </div>"""


def _classify_log_level(line: str) -> str:
    """
    Detect the log level of a line.
    Returns: 'ERROR', 'WARNING', 'DEBUG', 'SUCCESS', or 'INFO'.
    """
    import re
    s = line.upper()
    if re.search(r'\b(ERROR|CRITICAL|FAIL(ED)?)\b', s):
        return "ERROR"
    if re.search(r'\bWARN(ING)?\b', s):
        return "WARNING"
    if re.search(r'\bDEBUG\b', s):
        return "DEBUG"
    if re.search(r'\b(SUCCESS|DONE|COMPLET(ED)?)\b', s):
        return "SUCCESS"
    return "INFO"


# Maps widget multiselect labels → internal level keys
_FILTER_MAP = {
    "INFO":    {"INFO", "SUCCESS"},
    "WARNING": {"WARNING"},
    "ERROR":   {"ERROR"},
    "DEBUG":   {"DEBUG"},
}


def colorize_log_line(line: str) -> str:
    """
    Wrap a log line in a colored <div> based on detected severity.
    Lines without an explicit level keyword are shown as INFO (teal).
    Each line gets its own block-level element so newlines render properly.
    """
    level = _classify_log_level(line)
    cls_map = {
        "ERROR":   "log-line-error",
        "WARNING": "log-line-warn",
        "DEBUG":   "log-line-debug",
        "SUCCESS": "log-line-success",
        "INFO":    "log-line-info",
    }
    cls = cls_map[level]
    # Escape HTML special chars; preserve leading spaces as &nbsp; for indentation
    escaped = escape(line)
    return f'<div class="{cls}">{escaped}</div>'


def render_log_viewer(log_lines: list, selected_levels: list | None = None) -> str:
    """
    Render the live log viewer as a self-contained HTML page inside
    st.components.v1.html(). Designed to be rendered at height=520.

    Filtering rules:
    - If no levels selected: show everything
    - If levels selected: show lines whose detected level is in the filter set,
      PLUS any lines that have no recognized level keyword (they count as INFO)
    - Last 500 lines only

    Auto-scrolls to bottom on each render.
    """
    # Decide which internal levels are visible
    if selected_levels:
        visible: set = set()
        for lvl in selected_levels:
            visible.update(_FILTER_MAP.get(lvl, {lvl}))
    else:
        visible = {"INFO", "WARNING", "ERROR", "DEBUG", "SUCCESS"}

    filtered = [l for l in log_lines if _classify_log_level(l) in visible]
    display  = filtered[-500:]

    if display:
        body = "\n".join(colorize_log_line(l) for l in display)
    else:
        body = '<div class="log-empty-msg">Waiting for output...</div>'

    total_shown = len(display)
    total_all   = len(log_lines)
    count_label = f"{total_shown} / {total_all} lines" if total_all else "0 lines"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap');
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ height:100%; background:#FAFFFE; }}
  body {{
    font-family: 'JetBrains Mono', 'Consolas', 'Courier New', monospace;
    font-size: 11.5px;
    line-height: 1.65;
    color: #1F6B62;
    background: #FAFFFE;
  }}
  #log-wrap {{
    display: flex;
    flex-direction: column;
    height: 100vh;
    background: #FFFFFF;
    border: 1.5px solid #CCEBE8;
    border-radius: 12px;
    overflow: hidden;
  }}
  #log-header {{
    background: #F0FDFA;
    border-bottom: 1.5px solid #CCEBE8;
    padding: 7px 14px;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
  }}
  #log-title {{
    font-size: 10px;
    font-weight: 700;
    color: #4D7C78;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    flex: 1;
    font-family: 'JetBrains Mono', monospace;
  }}
  #log-count {{
    font-size: 10.5px;
    color: #80A8A4;
    background: #FFFFFF;
    border: 1px solid #CCEBE8;
    border-radius: 4px;
    padding: 2px 8px;
    font-family: 'JetBrains Mono', monospace;
  }}
  #log-body {{
    flex: 1;
    overflow-y: auto;
    padding: 10px 14px 12px;
    scroll-behavior: smooth;
  }}
  #log-body::-webkit-scrollbar {{ width: 5px; }}
  #log-body::-webkit-scrollbar-track {{ background: #F0FDFA; }}
  #log-body::-webkit-scrollbar-thumb {{ background: #99D6D0; border-radius: 3px; }}

  /* Per-line <div> elements */
  #log-body div {{
    padding: 1px 0;
    white-space: pre-wrap;
    word-break: break-all;
  }}
  .log-line-info    {{ color: #1F6B62; }}
  .log-line-debug   {{ color: #94A3B8; }}
  .log-line-warn    {{ color: #D97706; background: rgba(245,158,11,0.05); border-radius:2px; padding-left:4px; }}
  .log-line-error   {{ color: #DC2626; font-weight: 500; background: rgba(239,68,68,0.05); border-radius:2px; padding-left:4px; }}
  .log-line-success {{ color: #059669; font-weight: 500; }}
  .log-empty-msg    {{ color: #99D6D0; font-style: italic; padding: 8px 0; }}
</style>
</head>
<body>
<div id="log-wrap">
  <div id="log-header">
    <span id="log-title">Live Output</span>
    <span id="log-count">{count_label}</span>
  </div>
  <div id="log-body">
    {body}
  </div>
</div>
<script>
  (function() {{
    var b = document.getElementById('log-body');
    if (b) b.scrollTop = b.scrollHeight;
  }})();
</script>
</body>
</html>"""


def render_path_panel(data_dir: str = "", log_file: str = "") -> str:
    """Render the output paths panel."""
    d = escape(data_dir) if data_dir else "—"
    lf = escape(log_file) if log_file else "—"
    log_row = ""
    if log_file:
        log_row = f"""
        <div class="path-row">
            <span class="path-lbl">Log</span>
            <span class="path-val" title="{lf}">{lf}</span>
        </div>"""
    return f"""
    <div class="path-outer">
        <div class="path-outer-title">Output Paths</div>
        <div class="path-row">
            <span class="path-lbl">Data</span>
            <span class="path-val" title="{d}">{d}</span>
        </div>{log_row}
    </div>"""


def render_completeness_card(
    direction: str,
    pct: float,
    present: int,
    expected: int,
) -> str:
    """
    Render a tile temporal completeness card.
    Green >= 90%, yellow >= 60%, red < 60%.
    Includes an animated progress bar.
    """
    if pct >= 90:
        color = "#059669"   # emerald-600
        bar_color = "#10B981"
    elif pct >= 60:
        color = "#D97706"   # amber-600
        bar_color = "#F59E0B"
    else:
        color = "#DC2626"   # red-600
        bar_color = "#EF4444"
    bar_pct = min(pct, 100)
    safe_direction = escape(direction)
    return f"""
    <div class="completeness-card">
        <div class="completeness-dir-label">{safe_direction}</div>
        <div class="completeness-pct" style="color:{color};">{pct:.0f}%</div>
        <div class="completeness-bar-bg">
            <div class="completeness-bar-fill"
                 style="width:{bar_pct:.1f}%;background:{bar_color};"></div>
        </div>
        <div class="completeness-months">{present} / {expected} months</div>
    </div>"""


def render_stat_row(stats: list) -> str:
    """
    Render a horizontal row of KPI stat cards.
    Each stat is a dict with keys: value, label, color (optional).
    """
    cards = ""
    for s in stats:
        color = s.get("color", "#134E4A")
        if not re.match(r'^#[0-9a-fA-F]{3,8}$|^[a-z]+$', color):
            color = "#134E4A"
        safe_value = escape(str(s['value']))
        safe_label = escape(str(s['label']))
        cards += f"""<div class="stat-card">
            <div class="stat-value" style="color:{color};">{safe_value}</div>
            <div class="stat-label">{safe_label}</div>
        </div>"""
    return f'<div class="stat-row">{cards}</div>'


def build_cmd_preview(cmd_args: list) -> str:
    """
    Build a syntax-highlighted HTML command preview block.
    - Subcommand tokens: teal (primary)
    - Flags (--flag): purple
    - Values: green
    """
    if not cmd_args:
        return ""
    parts = []
    i = 0
    while i < len(cmd_args):
        t = cmd_args[i]
        if i <= 1:
            parts.append(f'<span class="cmd-keyword">{t}</span>')
        elif t.startswith("--"):
            parts.append(f'<span class="cmd-flag">{t}</span>')
            if i + 1 < len(cmd_args) and not cmd_args[i + 1].startswith("--"):
                i += 1
                parts.append(f'<span class="cmd-value"> {cmd_args[i]}</span>')
        else:
            parts.append(f'<span class="cmd-value">{t}</span>')
        i += 1
    return '<div class="cmd-preview">' + " ".join(parts) + "</div>"
