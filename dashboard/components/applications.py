"""
Applications component — Filterable list of all applications with quality details.
"""

from __future__ import annotations

import json
import io
from pathlib import Path

import streamlit as st

try:
    import pandas as pd
    _PANDAS = True
except ImportError:
    _PANDAS = False


_STATUS_COLORS = {
    "submitted":              "#22c55e",
    "pending_review":         "#f59e0b",
    "pending_manual_submit":  "#f59e0b",
    "blocked":                "#ef4444",
    "skipped":                "#64748b",
}

_STATUS_LABELS = {
    "submitted":              "Submitted",
    "pending_review":         "Pending Review",
    "pending_manual_submit":  "Manual Submit",
    "blocked":                "Blocked",
    "skipped":                "Skipped",
}


def _status_pill(status: str) -> str:
    color = _STATUS_COLORS.get(status, "#64748b")
    label = _STATUS_LABELS.get(status, status.replace("_", " ").title())
    return (
        f'<span style="background:{color}22;border:1px solid {color};'
        f'border-radius:20px;padding:2px 10px;font-size:0.72rem;'
        f'font-weight:600;color:{color}">{label}</span>'
    )


def _score_badge(score: int) -> str:
    if score >= 80:
        color = "#22c55e"
    elif score >= 65:
        color = "#f59e0b"
    else:
        color = "#ef4444"
    return (
        f'<span style="background:{color}22;border:1px solid {color};'
        f'border-radius:4px;padding:2px 8px;font-size:0.8rem;'
        f'font-weight:700;color:{color}">{score}</span>'
    )


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%b %d %H:%M")
    except Exception:
        return str(iso)[:16]


def _dimension_bar(label: str, score: int, max_score: int, color: str = "#3b82f6") -> str:
    pct = round(score / max_score * 100) if max_score else 0
    return f"""
    <div style="margin-bottom:6px">
        <div style="display:flex;justify-content:space-between;margin-bottom:2px">
            <span style="font-size:0.75rem;color:#94a3b8">{label}</span>
            <span style="font-size:0.75rem;font-weight:600;color:{color}">{score}/{max_score}</span>
        </div>
        <div style="background:#0f172a;border-radius:3px;height:5px">
            <div style="width:{pct}%;height:100%;background:{color};border-radius:3px"></div>
        </div>
    </div>
    """


def render(state: dict | None, approvals: list[dict]) -> None:
    st.markdown("""
    <h2 style="color:#f1f5f9;font-size:1.5rem;margin:0 0 20px 0">📋 Applications</h2>
    """, unsafe_allow_html=True)

    if not state:
        st.markdown("""
        <div style="background:#1e293b;border:1px solid #334155;border-radius:12px;
        padding:48px 32px;text-align:center">
            <div style="font-size:3rem;margin-bottom:12px">📋</div>
            <div style="color:#94a3b8;font-size:1rem">No pipeline data yet. Run the pipeline to generate applications.</div>
        </div>
        """, unsafe_allow_html=True)
        return

    apps = state.get("applications", [])

    if not apps:
        st.markdown("""
        <div style="background:#1e293b;border:1px solid #334155;border-radius:12px;
        padding:48px 32px;text-align:center">
            <div style="font-size:3rem;margin-bottom:12px">📋</div>
            <div style="color:#94a3b8;font-size:1rem">No applications yet.</div>
        </div>
        """, unsafe_allow_html=True)
        return

    # ── Summary metrics ──────────────────────────────────────────────────────────
    total     = len(apps)
    submitted = sum(1 for a in apps if a.get("status") == "submitted")
    pending   = sum(1 for a in apps if a.get("status") in ("pending_review", "pending_manual_submit"))
    blocked   = sum(1 for a in apps if a.get("status") == "blocked")
    avg_score = round(sum(a.get("quality_score", 0) for a in apps) / max(total, 1), 1)

    m1, m2, m3, m4, m5 = st.columns(5)
    for col, label, val, color in [
        (m1, "Total",       total,     "#3b82f6"),
        (m2, "Submitted",   submitted, "#22c55e"),
        (m3, "Pending",     pending,   "#f59e0b"),
        (m4, "Blocked",     blocked,   "#ef4444"),
        (m5, "Avg Score",   f"{avg_score}", "#8b5cf6"),
    ]:
        col.markdown(
            f'<div style="background:#1e293b;border:1px solid #334155;border-top:2px solid {color};'
            f'border-radius:8px;padding:14px;text-align:center">'
            f'<div style="font-size:1.6rem;font-weight:800;color:#f1f5f9">{val}</div>'
            f'<div style="font-size:0.7rem;color:#94a3b8;margin-top:4px;text-transform:uppercase;letter-spacing:0.05em">{label}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='margin-top:20px'></div>", unsafe_allow_html=True)

    # ── Filters ──────────────────────────────────────────────────────────────────
    f1, f2, f3, f4 = st.columns([2, 2, 1, 1])

    with f1:
        company_search = st.text_input(
            "Search company or role",
            placeholder="e.g. OpenAI, Platform Engineer...",
            label_visibility="collapsed",
        )
    with f2:
        all_statuses = sorted({a.get("status", "") for a in apps if a.get("status")})
        status_filter = st.multiselect(
            "Status filter",
            options=all_statuses,
            placeholder="All statuses",
            label_visibility="collapsed",
        )
    with f3:
        min_score = st.number_input("Min score", value=0, min_value=0, max_value=100, step=5)
    with f4:
        sort_by = st.selectbox(
            "Sort",
            ["Score ↓", "Score ↑", "Company A-Z", "Status", "Date"],
            label_visibility="collapsed",
        )

    # Apply filters
    filtered = apps
    if company_search:
        q = company_search.lower()
        filtered = [a for a in filtered if
                    q in a.get("company_name", "").lower() or
                    q in a.get("role_title", "").lower()]
    if status_filter:
        filtered = [a for a in filtered if a.get("status") in status_filter]
    if min_score > 0:
        filtered = [a for a in filtered if a.get("quality_score", 0) >= min_score]

    sort_map = {
        "Score ↓":    lambda a: -(a.get("quality_score") or 0),
        "Score ↑":    lambda a:  (a.get("quality_score") or 0),
        "Company A-Z": lambda a: a.get("company_name", "").lower(),
        "Status":     lambda a: a.get("status", ""),
        "Date":       lambda a: a.get("submitted_at") or "",
    }
    filtered = sorted(filtered, key=sort_map.get(sort_by, sort_map["Score ↓"]))

    st.markdown(
        f'<div style="color:#64748b;font-size:0.8rem;margin-bottom:12px">'
        f'Showing {len(filtered)} of {total} applications</div>',
        unsafe_allow_html=True,
    )

    # ── Export button ─────────────────────────────────────────────────────────────
    if _PANDAS and filtered:
        exp_data = [{
            "company": a.get("company_name", ""),
            "role": a.get("role_title", ""),
            "status": a.get("status", ""),
            "quality_score": a.get("quality_score", 0),
            "submitted_at": a.get("submitted_at", ""),
            "confirmation_url": a.get("confirmation_url", ""),
        } for a in filtered]
        df_export = pd.DataFrame(exp_data)
        csv_buf = io.StringIO()
        df_export.to_csv(csv_buf, index=False)
        st.download_button(
            "⬇ Export CSV",
            data=csv_buf.getvalue(),
            file_name="applications.csv",
            mime="text/csv",
        )

    # ── Application cards ─────────────────────────────────────────────────────────
    for app in filtered:
        company  = app.get("company_name", "Unknown")
        role     = app.get("role_title", "Unknown")
        status   = app.get("status", "unknown")
        score    = int(app.get("quality_score") or 0)
        sub_at   = app.get("submitted_at")
        app_id   = app.get("id", "")
        conf_url = app.get("confirmation_url", "")
        report   = app.get("quality_report", {}) or {}
        rejection = app.get("rejection_reason", "")

        status_color = _STATUS_COLORS.get(status, "#64748b")
        score_color = "#22c55e" if score >= 80 else ("#f59e0b" if score >= 65 else "#ef4444")

        # Main card row (always visible)
        with st.container():
            st.markdown(
                f'<div style="background:#1e293b;border:1px solid #334155;'
                f'border-left:3px solid {status_color};'
                f'border-radius:8px;padding:14px 16px;margin-bottom:2px">',
                unsafe_allow_html=True,
            )
            row1, row2, row3, row4 = st.columns([3, 2, 1, 1])
            with row1:
                st.markdown(
                    f'<div style="font-weight:600;color:#f1f5f9">{company}</div>'
                    f'<div style="font-size:0.85rem;color:#94a3b8">{role}</div>',
                    unsafe_allow_html=True,
                )
            with row2:
                st.markdown(_status_pill(status), unsafe_allow_html=True)
            with row3:
                st.markdown(_score_badge(score), unsafe_allow_html=True)
            with row4:
                st.markdown(
                    f'<div style="font-size:0.75rem;color:#64748b;text-align:right">{_fmt_dt(sub_at)}</div>',
                    unsafe_allow_html=True,
                )
            st.markdown("</div>", unsafe_allow_html=True)

            # Expandable details
            with st.expander("View details", expanded=False):
                d1, d2 = st.columns([1, 1], gap="large")

                with d1:
                    # Quality dimension bars
                    dim_scores = report.get("dimension_scores", {})
                    if dim_scores:
                        st.markdown('<div style="font-size:0.8rem;font-weight:600;color:#94a3b8;margin-bottom:8px">QUALITY DIMENSIONS</div>', unsafe_allow_html=True)
                        dim_map = {
                            "ats_keyword_coverage": ("ATS Keywords", 25, "#3b82f6"),
                            "value_story_impact":   ("Value Story",  25, "#6366f1"),
                            "ai_technical_depth":   ("AI Depth",     20, "#8b5cf6"),
                            "role_company_fit":     ("Role Fit",     15, "#f59e0b"),
                            "factual_integrity":    ("Factual",      10, "#22c55e"),
                            "formatting":           ("Formatting",    5, "#06b6d4"),
                        }
                        bars_html = ""
                        for key, (label, max_s, color) in dim_map.items():
                            val = dim_scores.get(key, 0)
                            bars_html += _dimension_bar(label, val, max_s, color)
                        st.markdown(bars_html, unsafe_allow_html=True)

                    # Verdict
                    verdict = report.get("verdict", "")
                    if verdict:
                        st.markdown(f'<div style="font-size:0.8rem;color:#cbd5e1;margin-top:8px"><strong>Verdict:</strong> {verdict}</div>', unsafe_allow_html=True)

                    # Missing keywords
                    missing_kw = report.get("missing_keywords", [])
                    if missing_kw:
                        kw_str = ", ".join(missing_kw[:8])
                        st.markdown(f'<div style="font-size:0.75rem;color:#64748b;margin-top:6px"><strong>Missing keywords:</strong> {kw_str}</div>', unsafe_allow_html=True)

                    if rejection:
                        st.markdown(f'<div style="color:#ef4444;font-size:0.8rem;margin-top:8px">Blocked: {rejection}</div>', unsafe_allow_html=True)

                    if conf_url:
                        st.markdown(f'<a href="{conf_url}" target="_blank" style="color:#3b82f6;font-size:0.8rem">View confirmation</a>', unsafe_allow_html=True)

                with d2:
                    # Strengths and issues
                    strengths = report.get("strengths", [])
                    issues    = report.get("critical_issues", [])
                    improvements = report.get("improvements", [])

                    if strengths:
                        st.markdown('<div style="font-size:0.75rem;font-weight:600;color:#22c55e;margin-bottom:4px">STRENGTHS</div>', unsafe_allow_html=True)
                        for s in strengths[:3]:
                            st.markdown(f'<div style="font-size:0.78rem;color:#94a3b8;margin-bottom:3px">✓ {s}</div>', unsafe_allow_html=True)

                    if issues:
                        st.markdown('<div style="font-size:0.75rem;font-weight:600;color:#ef4444;margin-top:10px;margin-bottom:4px">CRITICAL ISSUES</div>', unsafe_allow_html=True)
                        for i in issues[:3]:
                            st.markdown(f'<div style="font-size:0.78rem;color:#94a3b8;margin-bottom:3px">✗ {i}</div>', unsafe_allow_html=True)

                    if improvements:
                        st.markdown('<div style="font-size:0.75rem;font-weight:600;color:#f59e0b;margin-top:10px;margin-bottom:4px">IMPROVEMENTS</div>', unsafe_allow_html=True)
                        for imp in improvements[:3]:
                            st.markdown(f'<div style="font-size:0.78rem;color:#94a3b8;margin-bottom:3px">→ {imp}</div>', unsafe_allow_html=True)

                    # Resume preview
                    resume_path = app.get("tailored_resume_path", "")
                    if resume_path:
                        p = Path(resume_path)
                        if p.exists():
                            try:
                                resume_text = p.read_text(encoding="utf-8")
                                clean = resume_text.split("<!-- TAILOR_META")[0].strip()
                                st.text_area(
                                    "Tailored Resume",
                                    clean,
                                    height=300,
                                    disabled=True,
                                    key=f"resume_{app_id}",
                                )
                            except Exception:
                                st.caption("Could not read resume file.")
