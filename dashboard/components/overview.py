"""
Overview / Dashboard component — KPIs, funnel, quality distribution, activity chart, recent wins.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

try:
    import plotly.graph_objects as go
    import plotly.express as px
    _PLOTLY = True
except ImportError:
    _PLOTLY = False

_ACTIVITY_LOG = Path("/Users/rahul/Workspace/Job_Finder/data/activity_log.jsonl")


def _load_activity_entries(n: int = 500) -> list[dict]:
    if not _ACTIVITY_LOG.exists():
        return []
    try:
        lines = _ACTIVITY_LOG.read_text().strip().splitlines()
        entries = []
        for line in lines[-n:]:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
        return entries
    except Exception:
        return []


def _kpi_card(label: str, value: int | str, delta: str | None = None, color: str = "#3b82f6") -> str:
    delta_html = ""
    if delta:
        arrow = "↑" if not delta.startswith("-") else "↓"
        d_color = "#22c55e" if not delta.startswith("-") else "#ef4444"
        delta_html = f'<div style="font-size:0.75rem;color:{d_color};margin-top:2px">{arrow} {delta}</div>'

    return f"""
    <div style="
        background:#1e293b;
        border:1px solid #334155;
        border-radius:12px;
        padding:20px 16px;
        text-align:center;
        border-top:3px solid {color};
    ">
        <div style="font-size:2rem;font-weight:800;color:#f1f5f9;line-height:1">{value}</div>
        <div style="font-size:0.75rem;color:#94a3b8;margin-top:6px;letter-spacing:0.04em;text-transform:uppercase">{label}</div>
        {delta_html}
    </div>
    """


def _funnel_chart(counts: dict) -> "go.Figure | None":
    if not _PLOTLY:
        return None

    labels = list(counts.keys())
    values = list(counts.values())
    colors = ["#3b82f6", "#6366f1", "#8b5cf6", "#f59e0b", "#22c55e"]

    fig = go.Figure(go.Funnel(
        y=labels,
        x=values,
        textposition="inside",
        textinfo="value+percent initial",
        marker=dict(color=colors[:len(labels)]),
        connector=dict(line=dict(color="#334155", width=1)),
        textfont=dict(color="#f1f5f9", size=13),
    ))
    fig.update_layout(
        paper_bgcolor="#1e293b",
        plot_bgcolor="#1e293b",
        font=dict(color="#94a3b8", family="system-ui"),
        margin=dict(l=10, r=10, t=10, b=10),
        height=300,
        yaxis=dict(
            tickfont=dict(color="#cbd5e1", size=12),
            gridcolor="#334155",
        ),
    )
    return fig


def _quality_histogram(apps: list[dict]) -> "go.Figure | None":
    if not _PLOTLY or not apps:
        return None

    scores = [a.get("quality_score", 0) for a in apps if a.get("quality_score") is not None]
    if not scores:
        return None

    fig = go.Figure(go.Histogram(
        x=scores,
        nbinsx=20,
        marker=dict(
            color=scores,
            colorscale=[[0, "#ef4444"], [0.5, "#f59e0b"], [1.0, "#22c55e"]],
            line=dict(width=0),
        ),
        hovertemplate="Score %{x}: %{y} applications<extra></extra>",
    ))
    fig.update_layout(
        paper_bgcolor="#1e293b",
        plot_bgcolor="#1e293b",
        font=dict(color="#94a3b8", family="system-ui"),
        margin=dict(l=10, r=10, t=10, b=10),
        height=250,
        xaxis=dict(
            title="Quality Score",
            titlefont=dict(color="#94a3b8", size=12),
            tickfont=dict(color="#94a3b8"),
            gridcolor="#334155",
            range=[0, 100],
        ),
        yaxis=dict(
            title="Count",
            titlefont=dict(color="#94a3b8", size=12),
            tickfont=dict(color="#94a3b8"),
            gridcolor="#334155",
        ),
        bargap=0.05,
    )
    return fig


def _activity_timeline(entries: list[dict]) -> "go.Figure | None":
    if not _PLOTLY or not entries:
        return None

    # Group by hour
    hour_counts: dict[str, int] = defaultdict(int)
    for e in entries:
        ts = e.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts)
            hour_key = dt.strftime("%Y-%m-%d %H:00")
            hour_counts[hour_key] += 1
        except Exception:
            pass

    if not hour_counts:
        return None

    sorted_hours = sorted(hour_counts.keys())
    counts = [hour_counts[h] for h in sorted_hours]

    fig = go.Figure(go.Scatter(
        x=sorted_hours,
        y=counts,
        mode="lines+markers",
        line=dict(color="#3b82f6", width=2),
        marker=dict(color="#3b82f6", size=5),
        fill="tozeroy",
        fillcolor="rgba(59,130,246,0.1)",
        hovertemplate="%{x}: %{y} events<extra></extra>",
    ))
    fig.update_layout(
        paper_bgcolor="#1e293b",
        plot_bgcolor="#1e293b",
        font=dict(color="#94a3b8", family="system-ui"),
        margin=dict(l=10, r=10, t=10, b=10),
        height=220,
        xaxis=dict(
            tickfont=dict(color="#64748b", size=9),
            gridcolor="#1e293b",
            showgrid=False,
        ),
        yaxis=dict(
            title="Events",
            titlefont=dict(color="#94a3b8", size=11),
            tickfont=dict(color="#94a3b8"),
            gridcolor="#334155",
        ),
    )
    return fig


def _recent_win_card(app: dict) -> str:
    company = app.get("company_name", "Unknown")
    role    = app.get("role_title", "Unknown")
    score   = app.get("quality_score", 0)
    sub_at  = app.get("submitted_at", "")
    url     = app.get("confirmation_url", "")

    try:
        from datetime import datetime
        dt = datetime.fromisoformat(sub_at)
        ts = dt.strftime("%b %d")
    except Exception:
        ts = sub_at[:10] if sub_at else "—"

    link_html = f'<a href="{url}" target="_blank" style="color:#3b82f6;text-decoration:none;font-size:0.75rem">View confirmation</a>' if url else ""

    return f"""
    <div style="
        background:#0f172a;
        border:1px solid #334155;
        border-left:3px solid #22c55e;
        border-radius:8px;
        padding:14px 16px;
        margin-bottom:10px;
    ">
        <div style="display:flex;justify-content:space-between;align-items:center">
            <div>
                <div style="font-size:0.95rem;font-weight:600;color:#f1f5f9">{company}</div>
                <div style="font-size:0.8rem;color:#94a3b8">{role}</div>
            </div>
            <div style="text-align:right">
                <div style="font-size:1.1rem;font-weight:700;color:#22c55e">{score}</div>
                <div style="font-size:0.65rem;color:#64748b">{ts}</div>
            </div>
        </div>
        {f'<div style="margin-top:6px">{link_html}</div>' if link_html else ''}
    </div>
    """


def render(state: dict | None, approvals: list[dict]) -> None:
    st.markdown("""
    <h2 style="color:#f1f5f9;font-size:1.5rem;margin:0 0 20px 0">📊 Pipeline Overview</h2>
    """, unsafe_allow_html=True)

    if not state:
        st.markdown("""
        <div style="
            background:#1e293b;
            border:1px solid #334155;
            border-radius:12px;
            padding:48px 32px;
            text-align:center;
            margin-top:32px;
        ">
            <div style="font-size:3rem;margin-bottom:12px">🚀</div>
            <div style="color:#94a3b8;font-size:1.1rem;font-weight:600;margin-bottom:8px">
                No pipeline data yet
            </div>
            <div style="color:#64748b;font-size:0.875rem">
                Run the pipeline from the sidebar to start discovering companies and jobs.
            </div>
        </div>
        """, unsafe_allow_html=True)
        return

    apps      = state.get("applications", [])
    jobs      = state.get("job_listings", [])
    companies = state.get("companies", [])
    scores    = state.get("fit_scores", [])
    passing   = [s for s in scores if s.get("passed_threshold")]
    submitted = [a for a in apps if a.get("status") == "submitted"]
    errors    = state.get("errors", [])

    # ── KPI Row ──────────────────────────────────────────────────────────────────
    kpi_cols = st.columns(5)
    kpis = [
        ("Companies", len(companies),  "#3b82f6"),
        ("Jobs Found", len(jobs),       "#6366f1"),
        ("Passed Fit", len(passing),    "#8b5cf6"),
        ("Applications", len(apps),     "#f59e0b"),
        ("Submitted", len(submitted),   "#22c55e"),
    ]
    for col, (label, val, color) in zip(kpi_cols, kpis):
        with col:
            st.markdown(_kpi_card(label, val, color=color), unsafe_allow_html=True)

    st.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)

    # ── Pending approvals alert ───────────────────────────────────────────────────
    if approvals:
        st.markdown(
            f'<div style="background:#f59e0b22;border:1px solid #f59e0b;border-radius:8px;'
            f'padding:10px 16px;margin-bottom:20px;color:#f59e0b;font-size:0.9rem">'
            f'⏳ <strong>{len(approvals)} approval(s) pending</strong> — head to the Approvals page to review.</div>',
            unsafe_allow_html=True,
        )

    # ── Funnel + Quality Distribution ────────────────────────────────────────────
    fcol, qcol = st.columns([1, 1], gap="large")

    with fcol:
        st.markdown('<div style="font-size:0.85rem;font-weight:600;color:#94a3b8;letter-spacing:0.05em;text-transform:uppercase;margin-bottom:10px">Pipeline Funnel</div>', unsafe_allow_html=True)
        funnel_data = {
            "Companies":   len(companies),
            "Jobs Found":  len(jobs),
            "Passed Fit":  len(passing),
            "Applications": len(apps),
            "Submitted":   len(submitted),
        }
        if _PLOTLY and any(v > 0 for v in funnel_data.values()):
            fig = _funnel_chart(funnel_data)
            if fig:
                st.plotly_chart(fig, use_container_width=True, key="funnel_chart")
        else:
            # Fallback text funnel
            for label, val in funnel_data.items():
                pct = round(val / max(len(companies), 1) * 100) if label != "Companies" else 100
                st.markdown(
                    f'<div style="display:flex;justify-content:space-between;'
                    f'padding:6px 0;border-bottom:1px solid #1e293b">'
                    f'<span style="color:#cbd5e1">{label}</span>'
                    f'<span style="color:#3b82f6;font-weight:700">{val}</span></div>',
                    unsafe_allow_html=True,
                )

    with qcol:
        st.markdown('<div style="font-size:0.85rem;font-weight:600;color:#94a3b8;letter-spacing:0.05em;text-transform:uppercase;margin-bottom:10px">Quality Score Distribution</div>', unsafe_allow_html=True)
        if apps and _PLOTLY:
            fig = _quality_histogram(apps)
            if fig:
                st.plotly_chart(fig, use_container_width=True, key="quality_hist")
        elif apps:
            # Fallback buckets
            buckets = {"Excellent (85+)": 0, "Good (75-84)": 0, "Needs Work (65-74)": 0, "Below (< 65)": 0}
            for a in apps:
                s = a.get("quality_score", 0)
                if s >= 85:   buckets["Excellent (85+)"] += 1
                elif s >= 75: buckets["Good (75-84)"] += 1
                elif s >= 65: buckets["Needs Work (65-74)"] += 1
                else:         buckets["Below (< 65)"] += 1
            for label, count in buckets.items():
                st.markdown(
                    f'<div style="display:flex;justify-content:space-between;'
                    f'padding:6px 0;border-bottom:1px solid #1e293b">'
                    f'<span style="color:#cbd5e1">{label}</span>'
                    f'<span style="color:#3b82f6;font-weight:700">{count}</span></div>',
                    unsafe_allow_html=True,
                )
        else:
            st.markdown('<div style="color:#475569;font-size:0.875rem;margin-top:24px">No applications yet.</div>', unsafe_allow_html=True)

    st.markdown("<div style='margin-top:20px'></div>", unsafe_allow_html=True)

    # ── Activity Timeline ────────────────────────────────────────────────────────
    entries = _load_activity_entries(500)
    if entries:
        st.markdown('<div style="font-size:0.85rem;font-weight:600;color:#94a3b8;letter-spacing:0.05em;text-transform:uppercase;margin-bottom:10px">Agent Activity Timeline</div>', unsafe_allow_html=True)
        if _PLOTLY:
            fig = _activity_timeline(entries)
            if fig:
                st.plotly_chart(fig, use_container_width=True, key="activity_timeline")
        else:
            st.markdown(f'<div style="color:#94a3b8">{len(entries)} events logged</div>', unsafe_allow_html=True)

    st.markdown("<div style='margin-top:20px'></div>", unsafe_allow_html=True)

    # ── Recent Wins ──────────────────────────────────────────────────────────────
    recent_submitted = [a for a in apps if a.get("status") == "submitted"]
    recent_submitted.sort(key=lambda a: a.get("submitted_at") or "", reverse=True)

    if recent_submitted:
        st.markdown('<div style="font-size:0.85rem;font-weight:600;color:#94a3b8;letter-spacing:0.05em;text-transform:uppercase;margin-bottom:10px">Recent Submissions</div>', unsafe_allow_html=True)
        for app in recent_submitted[:3]:
            st.markdown(_recent_win_card(app), unsafe_allow_html=True)

    # ── Errors ───────────────────────────────────────────────────────────────────
    if errors:
        st.markdown("<div style='margin-top:20px'></div>", unsafe_allow_html=True)
        with st.expander(f"⚠️ Pipeline Errors ({len(errors)})", expanded=False):
            for e in errors[-5:]:
                st.markdown(
                    f'<div style="background:#ef444422;border-left:3px solid #ef4444;'
                    f'border-radius:4px;padding:8px 12px;margin-bottom:6px;font-size:0.8rem">'
                    f'<span style="color:#ef4444;font-weight:600">[{e.get("node", "?")}]</span> '
                    f'<span style="color:#fca5a5">{e.get("error", "")[:200]}</span></div>',
                    unsafe_allow_html=True,
                )

    # ── Last run ─────────────────────────────────────────────────────────────────
    last_run = state.get("last_run_at")
    if last_run:
        try:
            dt = datetime.fromisoformat(last_run)
            ts = dt.strftime("%b %d %Y, %H:%M UTC")
        except Exception:
            ts = last_run
        st.markdown(
            f'<div style="color:#475569;font-size:0.75rem;margin-top:16px">Last pipeline run: {ts}</div>',
            unsafe_allow_html=True,
        )
