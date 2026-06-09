"""
AutonomousHire Dashboard — Production-quality Streamlit UI
Run with: streamlit run dashboard/app.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

# ── Project root on path ────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

# ── Page config (must be first Streamlit call) ─────────────────────────────────
st.set_page_config(
    page_title="AutonomousHire",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Auto-refresh ────────────────────────────────────────────────────────────────
_REFRESH_OK = False
try:
    from streamlit_autorefresh import st_autorefresh
    _REFRESH_OK = True
except ImportError:
    pass

if _REFRESH_OK:
    st_autorefresh(interval=10_000, key="global_autorefresh")

# ── Global CSS ──────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* Dark base */
    .stApp { background-color: #0f172a; }

    /* Sidebar */
    [data-testid="stSidebar"] {
        background-color: #0f172a !important;
        border-right: 1px solid #1e293b !important;
    }
    [data-testid="stSidebar"] .stMarkdown p,
    [data-testid="stSidebar"] label { color: #94a3b8 !important; }

    /* Main content area */
    [data-testid="stMain"] { background-color: #0f172a; }
    .block-container { padding-top: 1.5rem !important; max-width: 1400px; }

    /* Headers */
    h1, h2, h3, h4 { color: #f1f5f9 !important; }

    /* Buttons */
    .stButton > button {
        border-radius: 8px !important;
        font-weight: 600 !important;
        transition: all 0.15s ease !important;
    }
    .stButton > button[kind="primary"] {
        background: #3b82f6 !important;
        border: none !important;
        color: white !important;
    }
    .stButton > button[kind="primary"]:hover {
        background: #2563eb !important;
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(59,130,246,0.3) !important;
    }
    .stButton > button:not([kind="primary"]) {
        background: transparent !important;
        border: 1px solid #334155 !important;
        color: #94a3b8 !important;
    }
    .stButton > button:not([kind="primary"]):hover {
        border-color: #64748b !important;
        color: #f1f5f9 !important;
    }

    /* Inputs & text areas */
    .stTextInput > div > div > input,
    .stTextArea > div > div > textarea,
    .stNumberInput > div > div > input {
        background-color: #1e293b !important;
        border: 1px solid #334155 !important;
        border-radius: 8px !important;
        color: #f1f5f9 !important;
    }
    .stTextInput > div > div > input:focus,
    .stTextArea > div > div > textarea:focus {
        border-color: #3b82f6 !important;
        box-shadow: 0 0 0 1px #3b82f6 !important;
    }

    /* Multiselect */
    .stMultiSelect > div > div {
        background-color: #1e293b !important;
        border: 1px solid #334155 !important;
        border-radius: 8px !important;
    }
    .stMultiSelect span[data-baseweb="tag"] {
        background-color: #3b82f622 !important;
        border: 1px solid #3b82f6 !important;
        color: #3b82f6 !important;
    }

    /* Selectbox */
    .stSelectbox > div > div {
        background-color: #1e293b !important;
        border: 1px solid #334155 !important;
        border-radius: 8px !important;
        color: #f1f5f9 !important;
    }

    /* Dataframe */
    .stDataFrame { border-radius: 8px; overflow: hidden; }
    [data-testid="stDataFrameResizable"] {
        background-color: #1e293b;
        border: 1px solid #334155;
        border-radius: 8px;
    }

    /* Expander */
    .streamlit-expanderHeader {
        background-color: #1e293b !important;
        border: 1px solid #334155 !important;
        border-radius: 8px !important;
        color: #94a3b8 !important;
    }
    .streamlit-expanderContent {
        background-color: #0f172a !important;
        border: 1px solid #334155 !important;
        border-top: none !important;
    }

    /* Tabs (used in sub-components) */
    .stTabs [data-baseweb="tab-list"] {
        background-color: transparent !important;
        border-bottom: 1px solid #334155 !important;
        gap: 4px;
    }
    .stTabs [data-baseweb="tab"] {
        background-color: transparent !important;
        color: #94a3b8 !important;
        border-radius: 6px 6px 0 0 !important;
        padding: 8px 16px !important;
        font-size: 0.875rem !important;
    }
    .stTabs [aria-selected="true"] {
        background-color: #1e293b !important;
        color: #f1f5f9 !important;
        border-bottom: 2px solid #3b82f6 !important;
    }

    /* Alerts */
    .stAlert { border-radius: 8px !important; }
    [data-testid="stNotification"] { border-radius: 8px !important; }

    /* Metrics */
    [data-testid="stMetricValue"] { color: #f1f5f9 !important; font-size: 1.8rem !important; }
    [data-testid="stMetricLabel"] { color: #94a3b8 !important; }
    [data-testid="stMetricDelta"] { font-size: 0.8rem !important; }

    /* Number input */
    .stNumberInput button { background: #334155 !important; border: none !important; color: #94a3b8 !important; }

    /* Download button */
    .stDownloadButton > button {
        background: #1e293b !important;
        border: 1px solid #334155 !important;
        color: #94a3b8 !important;
        border-radius: 8px !important;
        font-size: 0.8rem !important;
    }

    /* Radio in sidebar */
    .stRadio > div { gap: 0 !important; }
    .stRadio label { padding: 8px 12px !important; border-radius: 8px !important; }

    /* Hide default streamlit branding */
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    header { visibility: hidden; }

    /* Container borders */
    [data-testid="stVerticalBlockBorderWrapper"] {
        background-color: #1e293b !important;
        border: 1px solid #334155 !important;
        border-radius: 12px !important;
    }

    /* Plotly charts */
    [data-testid="stPlotlyChart"] { border-radius: 8px; overflow: hidden; }

    /* Scrollbar */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #0f172a; }
    ::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: #475569; }
</style>
""", unsafe_allow_html=True)


# ── Data loaders ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=10)
def _load_state() -> dict | None:
    try:
        from orchestration.runner import load_state
        return load_state()
    except Exception:
        try:
            p = _ROOT / "data" / "pipeline_state.json"
            if p.exists():
                return json.loads(p.read_text())
        except Exception:
            pass
        return None


@st.cache_data(ttl=10)
def _load_approvals() -> list[dict]:
    try:
        from core.checkpoints import get_pending_approvals
        return get_pending_approvals()
    except Exception:
        try:
            p = _ROOT / "data" / "pending_approvals.json"
            if p.exists():
                data = json.loads(p.read_text())
                if isinstance(data, dict):
                    return [r for r in data.values() if r.get("status") == "pending"]
                if isinstance(data, list):
                    return [r for r in data if r.get("status") == "pending"]
        except Exception:
            pass
        return []


# ── Pipeline process management ─────────────────────────────────────────────────

_PID_FILE = Path("/tmp/autonomoushire_pipeline.pid")


def _pipeline_running() -> bool:
    if not _PID_FILE.exists():
        return False
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        _PID_FILE.unlink(missing_ok=True)
        return False


_PIPELINE_LOG = Path("/tmp/autonomoushire_pipeline.log")


def _start_pipeline(dry_run: bool = False, submit_only: bool = False, auto_approve: bool = False) -> int | None:
    try:
        venv_python = str(_ROOT / ".venv" / "bin" / "python")
        python_exe = venv_python if Path(venv_python).exists() else sys.executable
        args = [python_exe, "-m", "orchestration.runner"]
        if dry_run:
            args.append("--dry-run")
        if submit_only:
            args.append("--submit-only")
        if auto_approve:
            args.append("--auto-approve")
        # Redirect to file instead of PIPE — avoids macOS fork() crash in Network.framework
        # when a multithreaded process (Streamlit) forks a subprocess.
        log_fh = open(_PIPELINE_LOG, "w")
        proc = subprocess.Popen(
            args,
            cwd=str(_ROOT),
            stdout=log_fh,
            stderr=log_fh,
        )
        _PID_FILE.write_text(str(proc.pid))
        return proc.pid
    except Exception as e:
        st.error(f"Failed to start pipeline: {e}")
        return None


def _stop_pipeline() -> bool:
    if not _PID_FILE.exists():
        return False
    try:
        pid = int(_PID_FILE.read_text().strip())
        import signal
        os.kill(pid, signal.SIGTERM)
        _PID_FILE.unlink(missing_ok=True)
        return True
    except Exception:
        _PID_FILE.unlink(missing_ok=True)
        return False


# ── Load data ───────────────────────────────────────────────────────────────────
state     = _load_state()
approvals = _load_approvals()
n_pending = len(approvals)
running   = _pipeline_running()
now_ts    = datetime.now(timezone.utc).strftime("%H:%M:%S")


# ── Sidebar ─────────────────────────────────────────────────────────────────────
with st.sidebar:
    # Logo + header
    st.markdown("""
    <div style="padding:12px 0 20px 0">
        <div style="font-size:1.4rem;font-weight:800;color:#f1f5f9;letter-spacing:-0.02em">
            🤖 AutonomousHire
        </div>
        <div style="font-size:0.75rem;color:#475569;margin-top:2px">
            AI-powered job search pipeline
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Status badge row
    status_color = "#22c55e" if running else "#475569"
    status_label = "● Running" if running else "● Idle"
    approval_badge = (
        f'<span style="background:#ef444422;border:1px solid #ef4444;'
        f'border-radius:20px;padding:2px 8px;font-size:0.72rem;color:#ef4444;font-weight:700">'
        f'🔔 {n_pending}</span>'
        if n_pending > 0 else ""
    )
    st.markdown(
        f'<div style="display:flex;gap:8px;align-items:center;margin-bottom:20px;flex-wrap:wrap">'
        f'<span style="font-size:0.75rem;color:{status_color};font-weight:600">{status_label}</span>'
        f'{approval_badge}'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Navigation ────────────────────────────────────────────────────────────────
    st.markdown(
        '<div style="font-size:0.65rem;font-weight:700;color:#334155;'
        'letter-spacing:0.1em;text-transform:uppercase;margin-bottom:8px">NAVIGATION</div>',
        unsafe_allow_html=True,
    )

    nav_options = [
        ("🔔", f"Approvals ({n_pending})" if n_pending else "Approvals"),
        ("📊", "Dashboard"),
        ("🔗", "LinkedIn Pipeline"),
        ("📋", "Applications"),
        ("🏢", "Companies & Jobs"),
        ("📜", "Activity Log"),
    ]
    nav_labels = [f"{icon}  {label}" for icon, label in nav_options]

    # Use radio for navigation
    selected_nav = st.radio(
        "nav",
        options=nav_labels,
        index=0,
        label_visibility="collapsed",
    )
    page_index = nav_labels.index(selected_nav)

    st.markdown("<div style='margin-top:24px'></div>", unsafe_allow_html=True)

    # ── Pipeline Controls ─────────────────────────────────────────────────────────
    st.markdown(
        '<div style="font-size:0.65rem;font-weight:700;color:#334155;'
        'letter-spacing:0.1em;text-transform:uppercase;margin-bottom:10px">PIPELINE CONTROL</div>',
        unsafe_allow_html=True,
    )

    if not running:
        if st.button("🚀 Run Full Pipeline", use_container_width=True, type="primary"):
            pid = _start_pipeline(dry_run=False)
            if pid:
                st.success(f"Pipeline started (PID {pid})")
                st.cache_data.clear()
                st.rerun()

        # Count pending_review apps so the button label is informative
        _n_ready = sum(
            1 for a in (state or {}).get("applications", [])
            if a.get("status") == "pending_review"
        ) if state else 0
        _submit_label = f"📬 Submit Ready ({_n_ready})" if _n_ready else "📬 Submit Ready"
        if st.button(_submit_label, use_container_width=True, type="primary" if _n_ready else "secondary"):
            pid = _start_pipeline(submit_only=True, auto_approve=True)
            if pid:
                st.success(f"Submit-only run started (PID {pid})")
                st.cache_data.clear()
                st.rerun()

        if st.button("🔍 Dry Run", use_container_width=True):
            pid = _start_pipeline(dry_run=True)
            if pid:
                st.success(f"Dry run started (PID {pid})")
                st.cache_data.clear()
                st.rerun()
    else:
        st.markdown(
            '<div style="background:#22c55e11;border:1px solid #22c55e33;border-radius:6px;'
            'padding:8px 10px;margin-bottom:8px;font-size:0.8rem;color:#22c55e">'
            '● Pipeline is running...</div>',
            unsafe_allow_html=True,
        )
        if st.button("⏹ Stop Pipeline", use_container_width=True):
            if _stop_pipeline():
                st.warning("Pipeline stopped.")
                st.cache_data.clear()
                st.rerun()
            else:
                st.error("Could not stop — process not found.")

    if st.button("🔄 Refresh Now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    # ── Pipeline log tail ─────────────────────────────────────────────────────────
    if running and _PIPELINE_LOG.exists():
        try:
            log_lines = _PIPELINE_LOG.read_text().splitlines()
            recent = "\n".join(log_lines[-12:])
            if recent.strip():
                st.markdown(
                    f'<div style="background:#0d1117;border:1px solid #1e293b;border-radius:6px;'
                    f'padding:8px;margin-top:8px;font-family:monospace;font-size:0.65rem;'
                    f'color:#4ade80;white-space:pre-wrap;word-break:break-all;max-height:120px;'
                    f'overflow-y:auto">{recent}</div>',
                    unsafe_allow_html=True,
                )
        except Exception:
            pass

    # ── Pipeline stats summary ────────────────────────────────────────────────────
    if state:
        n_cos  = len(state.get("companies", []))
        n_jobs = len(state.get("job_listings", []))
        n_apps = len(state.get("applications", []))
        n_subs = sum(1 for a in state.get("applications", []) if a.get("status") == "submitted")

        st.markdown("<div style='margin-top:20px'></div>", unsafe_allow_html=True)
        st.markdown(
            '<div style="font-size:0.65rem;font-weight:700;color:#334155;'
            'letter-spacing:0.1em;text-transform:uppercase;margin-bottom:8px">QUICK STATS</div>',
            unsafe_allow_html=True,
        )
        for label, val in [
            ("Companies", n_cos),
            ("Jobs", n_jobs),
            ("Applications", n_apps),
            ("Submitted", n_subs),
        ]:
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;'
                f'padding:4px 0;border-bottom:1px solid #1e293b">'
                f'<span style="font-size:0.78rem;color:#64748b">{label}</span>'
                f'<span style="font-size:0.78rem;font-weight:600;color:#94a3b8">{val}</span></div>',
                unsafe_allow_html=True,
            )

    # ── Last updated ──────────────────────────────────────────────────────────────
    st.markdown(
        f'<div style="position:relative;margin-top:24px;font-size:0.7rem;color:#334155">'
        f'Updated {now_ts} UTC'
        f'{"  · autorefresh 10s" if _REFRESH_OK else ""}</div>',
        unsafe_allow_html=True,
    )


# ── Main content area ────────────────────────────────────────────────────────────

# Sticky-style header bar
last_run = (state or {}).get("last_run_at")
try:
    if last_run:
        dt = datetime.fromisoformat(last_run)
        elapsed = int((datetime.now(timezone.utc) - dt).total_seconds())
        if elapsed < 60:
            last_run_str = f"{elapsed}s ago"
        elif elapsed < 3600:
            last_run_str = f"{elapsed // 60}m ago"
        else:
            last_run_str = dt.strftime("%b %d %H:%M")
    else:
        last_run_str = "never"
except Exception:
    last_run_str = "—"

pending_str = f"  ·  {n_pending} approval{'s' if n_pending != 1 else ''} pending" if n_pending else ""
pipeline_str = "  ·  ● running" if running else ""
pipeline_color = "#22c55e" if running else "#475569"

st.markdown(
    f'<div style="'
    f'background:#0f172a;'
    f'border-bottom:1px solid #1e293b;'
    f'padding:8px 0 16px 0;'
    f'margin-bottom:20px;'
    f'display:flex;justify-content:space-between;align-items:center;'
    f'">'
    f'<div style="font-size:0.8rem;color:#475569">'
    f'Last run: {last_run_str}'
    f'<span style="color:#f59e0b">{pending_str}</span>'
    f'<span style="color:{pipeline_color}">{pipeline_str}</span>'
    f'</div>'
    f'<div style="font-size:0.7rem;color:#334155">Updated {now_ts} UTC</div>'
    f'</div>',
    unsafe_allow_html=True,
)


# ── Route to page ────────────────────────────────────────────────────────────────
# Import components lazily to avoid slow startup
if page_index == 0:
    from dashboard.components.approvals import render as render_approvals
    render_approvals(state, approvals)

elif page_index == 1:
    from dashboard.components.overview import render as render_overview
    render_overview(state, approvals)

elif page_index == 2:
    from dashboard.components.linkedin import render as render_linkedin
    render_linkedin(state, approvals)

elif page_index == 3:
    from dashboard.components.applications import render as render_applications
    render_applications(state, approvals)

elif page_index == 4:
    from dashboard.components.companies import render as render_companies
    render_companies(state, approvals)

elif page_index == 5:
    from dashboard.components.activity import render as render_activity
    render_activity(state, approvals)
