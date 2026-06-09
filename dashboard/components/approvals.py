"""
Approvals component — Human-in-the-loop review page.
The most important page: every pending approval gets a prominent card.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    import plotly.graph_objects as go
    _PLOTLY = True
except ImportError:
    _PLOTLY = False


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%b %d %H:%M")
    except Exception:
        return str(iso)[:16]


def _radar_chart(dim_scores: dict) -> "go.Figure | None":
    if not _PLOTLY or not dim_scores:
        return None

    labels_map = {
        "ats_keyword_coverage":  "ATS",
        "value_story_impact":    "Value Story",
        "ai_technical_depth":    "AI Depth",
        "role_company_fit":      "Role Fit",
        "factual_integrity":     "Factual",
        "formatting":            "Formatting",
    }
    max_scores = {
        "ats_keyword_coverage":  25,
        "value_story_impact":    25,
        "ai_technical_depth":    20,
        "role_company_fit":      15,
        "factual_integrity":     10,
        "formatting":            5,
    }

    keys = list(labels_map.keys())
    labels = [labels_map[k] for k in keys]
    raw_vals = [dim_scores.get(k, 0) for k in keys]
    max_vals = [max_scores[k] for k in keys]
    # Normalize to 0-100 for visual consistency
    norm_vals = [round(r / m * 100) if m else 0 for r, m in zip(raw_vals, max_vals)]
    # Close the polygon
    labels_closed = labels + [labels[0]]
    norm_closed   = norm_vals + [norm_vals[0]]

    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(
        r=norm_closed,
        theta=labels_closed,
        fill="toself",
        fillcolor="rgba(59,130,246,0.2)",
        line=dict(color="#3b82f6", width=2),
        name="Score",
        hovertemplate="%{theta}: %{r}%<extra></extra>",
    ))
    fig.update_layout(
        polar=dict(
            bgcolor="#1e293b",
            radialaxis=dict(
                visible=True,
                range=[0, 100],
                tickfont=dict(size=9, color="#94a3b8"),
                gridcolor="#334155",
                linecolor="#334155",
            ),
            angularaxis=dict(
                tickfont=dict(size=10, color="#e2e8f0"),
                gridcolor="#334155",
                linecolor="#334155",
            ),
        ),
        paper_bgcolor="#1e293b",
        plot_bgcolor="#1e293b",
        margin=dict(l=40, r=40, t=20, b=20),
        height=240,
        showlegend=False,
    )
    return fig


def _score_color(score: int) -> str:
    if score >= 80:
        return "#22c55e"
    if score >= 65:
        return "#f59e0b"
    return "#ef4444"


def render(state: dict | None, approvals: list[dict]) -> None:
    from core.checkpoints import resolve_approval

    st.markdown("""
    <div style="margin-bottom:8px">
        <h2 style="color:#f1f5f9;font-size:1.5rem;margin:0">
            🔔 Pending Approvals
        </h2>
        <p style="color:#94a3b8;font-size:0.875rem;margin:4px 0 0 0">
            Review each application below. Approving will submit — rejecting marks it skipped.
        </p>
    </div>
    """, unsafe_allow_html=True)

    if not approvals:
        st.markdown("""
        <div style="
            background:#1e293b;
            border:1px solid #334155;
            border-radius:12px;
            padding:48px 32px;
            text-align:center;
            margin-top:32px;
        ">
            <div style="font-size:3rem;margin-bottom:12px">✅</div>
            <div style="color:#22c55e;font-size:1.25rem;font-weight:600;margin-bottom:8px">
                All caught up — no applications waiting for review
            </div>
            <div style="color:#64748b;font-size:0.875rem">
                The pipeline will surface new approvals here when applications are ready.
            </div>
        </div>
        """, unsafe_allow_html=True)
        return

    st.markdown(
        f'<div style="color:#f59e0b;font-weight:600;margin-bottom:16px">'
        f'⏳ {len(approvals)} application(s) waiting for your review</div>',
        unsafe_allow_html=True,
    )

    for ap in approvals:
        payload        = ap.get("payload", {})
        company        = payload.get("company", "Unknown")
        role           = payload.get("role", "Unknown")
        ats            = payload.get("ats", "?").upper()
        score          = int(payload.get("quality_score", 0))
        url            = payload.get("apply_url", "")
        filled_fields  = payload.get("filled_fields", [])
        manual_steps   = payload.get("manual_steps", [])
        screenshot     = payload.get("screenshot_path", "")
        resume_preview = payload.get("tailored_resume_preview", "")
        resume_path    = payload.get("tailored_resume_path", "")
        # Fall back to loading from disk if preview is missing or very short
        if len(resume_preview) < 200 and resume_path:
            try:
                full = Path(resume_path).read_text(encoding="utf-8")
                resume_preview = full.split("<!-- TAILOR_META")[0].strip()
            except Exception:
                pass
        expires        = ap.get("expires_at")
        ap_id          = ap["id"]

        score_color = _score_color(score)

        # -- Card container --
        st.markdown(f"""
        <div style="
            background:#1e293b;
            border:1px solid #334155;
            border-radius:12px;
            padding:24px;
            margin-bottom:24px;
        ">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">
                <div>
                    <div style="font-size:1.35rem;font-weight:700;color:#f1f5f9">{company}</div>
                    <div style="font-size:1rem;color:#cbd5e1;margin-top:2px">{role}</div>
                </div>
                <div style="
                    background:{score_color}22;
                    border:1px solid {score_color};
                    border-radius:8px;
                    padding:8px 18px;
                    text-align:center;
                ">
                    <div style="font-size:1.6rem;font-weight:800;color:{score_color}">{score}</div>
                    <div style="font-size:0.7rem;color:{score_color};letter-spacing:0.05em">QUALITY</div>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Left/right layout inside real streamlit columns for interactivity
        col_left, col_right = st.columns([1, 1], gap="large")

        with col_left:
            # Meta badges
            meta_cols = st.columns(3)
            meta_cols[0].markdown(
                f'<div style="background:#0f172a;border:1px solid #334155;border-radius:6px;'
                f'padding:6px 10px;text-align:center">'
                f'<div style="font-size:0.65rem;color:#64748b">ATS</div>'
                f'<div style="font-size:0.9rem;font-weight:600;color:#e2e8f0">{ats}</div></div>',
                unsafe_allow_html=True,
            )
            meta_cols[1].markdown(
                f'<div style="background:#0f172a;border:1px solid #334155;border-radius:6px;'
                f'padding:6px 10px;text-align:center">'
                f'<div style="font-size:0.65rem;color:#64748b">EXPIRES</div>'
                f'<div style="font-size:0.9rem;font-weight:600;color:#e2e8f0">{_fmt_dt(expires)}</div></div>',
                unsafe_allow_html=True,
            )
            meta_cols[2].markdown(
                f'<div style="background:#0f172a;border:1px solid #334155;border-radius:6px;'
                f'padding:6px 10px;text-align:center">'
                f'<div style="font-size:0.65rem;color:#64748b">ID</div>'
                f'<div style="font-size:0.75rem;font-weight:600;color:#94a3b8;overflow:hidden;text-overflow:ellipsis">{ap_id[:8]}...</div></div>',
                unsafe_allow_html=True,
            )

            # ── Application URL + automation summary ──────────────────────
            if url:
                st.markdown(
                    f'<div style="margin-top:14px;background:#0f172a;border:1px solid #1e40af;'
                    f'border-radius:8px;padding:12px 14px">'
                    f'<div style="font-size:0.7rem;color:#64748b;letter-spacing:0.05em;'
                    f'text-transform:uppercase;margin-bottom:4px">Application URL</div>'
                    f'<a href="{url}" target="_blank" style="color:#60a5fa;font-size:0.82rem;'
                    f'word-break:break-all;text-decoration:none">{url}</a>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            if filled_fields:
                fields_html = "".join(
                    f'<div style="font-size:0.78rem;color:#86efac;padding:2px 0">'
                    f'<span style="color:#22c55e">✓</span>&nbsp;{f}</div>'
                    for f in filled_fields
                )
                st.markdown(
                    f'<div style="margin-top:10px;background:#052e16;border:1px solid #166534;'
                    f'border-radius:8px;padding:10px 14px">'
                    f'<div style="font-size:0.7rem;color:#4ade80;letter-spacing:0.05em;'
                    f'text-transform:uppercase;margin-bottom:6px">Auto-filled by bot</div>'
                    f'{fields_html}</div>',
                    unsafe_allow_html=True,
                )

            if manual_steps:
                steps_html = "".join(
                    f'<div style="font-size:0.78rem;color:#fcd34d;padding:3px 0">'
                    f'<span style="color:#f59e0b">⚠</span>&nbsp;{s}</div>'
                    for s in manual_steps
                )
                st.markdown(
                    f'<div style="margin-top:10px;background:#431407;border:1px solid #92400e;'
                    f'border-radius:8px;padding:10px 14px">'
                    f'<div style="font-size:0.7rem;color:#fb923c;letter-spacing:0.05em;'
                    f'text-transform:uppercase;margin-bottom:6px">Manual action required</div>'
                    f'{steps_html}</div>',
                    unsafe_allow_html=True,
                )

            # Quality scorecard radar
            quality_report = None
            # Try to find the matching application quality_report
            if state:
                apps = state.get("applications", [])
                for a in apps:
                    if (a.get("company_name", "").lower() == company.lower() or
                            a.get("role_title", "").lower() == role.lower()):
                        quality_report = a.get("quality_report", {})
                        break

            dim_scores = quality_report.get("dimension_scores", {}) if quality_report else {}
            if _PLOTLY and dim_scores:
                fig = _radar_chart(dim_scores)
                if fig:
                    st.markdown(
                        '<div style="font-size:0.8rem;color:#94a3b8;margin-top:16px;margin-bottom:4px">'
                        'Quality Scorecard</div>',
                        unsafe_allow_html=True,
                    )
                    st.plotly_chart(fig, use_container_width=True, key=f"radar_{ap_id}")
            elif score:
                # Fallback: simple score bar
                bar_pct = score
                bar_color = score_color
                st.markdown(f"""
                <div style="margin-top:16px">
                    <div style="font-size:0.8rem;color:#94a3b8;margin-bottom:6px">Quality Score</div>
                    <div style="background:#0f172a;border-radius:4px;height:8px;overflow:hidden">
                        <div style="width:{bar_pct}%;height:100%;background:{bar_color};border-radius:4px;
                        transition:width 0.3s ease"></div>
                    </div>
                    <div style="font-size:0.75rem;color:{bar_color};margin-top:4px">{score}/100</div>
                </div>
                """, unsafe_allow_html=True)

            if quality_report:
                strengths = quality_report.get("strengths", [])
                issues    = quality_report.get("critical_issues", [])
                if strengths or issues:
                    st.markdown('<div style="margin-top:16px"></div>', unsafe_allow_html=True)
                    det_col1, det_col2 = st.columns(2)
                    with det_col1:
                        if strengths:
                            st.markdown('<div style="font-size:0.75rem;color:#22c55e;font-weight:600">STRENGTHS</div>', unsafe_allow_html=True)
                            for s in strengths[:2]:
                                st.markdown(f'<div style="font-size:0.75rem;color:#94a3b8;margin-top:2px">• {s}</div>', unsafe_allow_html=True)
                    with det_col2:
                        if issues:
                            st.markdown('<div style="font-size:0.75rem;color:#ef4444;font-weight:600">ISSUES</div>', unsafe_allow_html=True)
                            for i in issues[:2]:
                                st.markdown(f'<div style="font-size:0.75rem;color:#94a3b8;margin-top:2px">• {i}</div>', unsafe_allow_html=True)

        with col_right:
            # ── Resume preview ────────────────────────────────────────────
            clean_preview = resume_preview or ""
            if "<!-- TAILOR_META" in clean_preview:
                clean_preview = clean_preview.split("<!-- TAILOR_META")[0].strip()

            def _render_resume_html(text: str, preview_lines: int | None = None) -> str:
                """Convert markdown resume text to styled HTML for dark background."""
                escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                lines = escaped.split("\n")
                if preview_lines:
                    lines = lines[:preview_lines]
                out = []
                for ln in lines:
                    s = ln.strip()
                    if s.startswith("# "):
                        out.append(f'<div style="font-size:1.05rem;font-weight:700;color:#f1f5f9;margin:6px 0 2px">{s[2:]}</div>')
                    elif s.startswith("## "):
                        out.append(f'<div style="font-size:0.75rem;font-weight:700;color:#94a3b8;text-transform:uppercase;'
                                   f'letter-spacing:0.08em;border-bottom:1px solid #334155;padding-bottom:2px;margin:8px 0 3px">{s[3:]}</div>')
                    elif s.startswith("### "):
                        out.append(f'<div style="font-size:0.82rem;font-weight:600;color:#e2e8f0;margin:5px 0 2px">{s[4:]}</div>')
                    elif s.startswith("- "):
                        out.append(f'<div style="font-size:0.78rem;color:#cbd5e1;padding-left:14px;margin:1px 0">&#x2022; {s[2:]}</div>')
                    elif s.startswith("---"):
                        out.append('<hr style="border:none;border-top:1px solid #334155;margin:5px 0">')
                    elif s == "":
                        out.append('<div style="height:3px"></div>')
                    else:
                        out.append(f'<div style="font-size:0.78rem;color:#94a3b8;margin:1px 0">{s}</div>')
                return "\n".join(out)

            if clean_preview:
                all_lines = clean_preview.split("\n")
                preview_cutoff = 18  # ~first section visible without scrolling

                # Always-visible short preview
                short_html = _render_resume_html(clean_preview, preview_cutoff)
                has_more = len(all_lines) > preview_cutoff

                st.markdown(
                    f'<div style="font-size:0.8rem;color:#94a3b8;margin-bottom:6px">📄 Tailored Resume'
                    + (f' <span style="color:#475569;font-size:0.7rem">({len(all_lines)} lines)</span>' if has_more else '')
                    + '</div>'
                    f'<div style="background:#0f172a;border:1px solid #334155;border-radius:8px 8px '
                    + ('0 0' if has_more else '8px 8px')
                    + f';padding:14px 16px;font-family:monospace">'
                    f'{short_html}</div>',
                    unsafe_allow_html=True,
                )

                # Full resume expander below preview
                if has_more:
                    with st.expander(f"▼  View full resume ({len(all_lines)} lines)", expanded=False):
                        full_html = _render_resume_html(clean_preview)
                        st.markdown(
                            f'<div style="background:#0f172a;padding:16px;font-family:monospace;'
                            f'border-radius:8px">{full_html}</div>',
                            unsafe_allow_html=True,
                        )
                        # Also offer raw text copy
                        st.download_button(
                            "⬇ Download resume (.md)",
                            data=clean_preview,
                            file_name=f"{company.replace(' ','_')}_{role.replace(' ','_')}.md",
                            mime="text/markdown",
                            key=f"dl_{ap_id}",
                        )
            else:
                st.markdown(
                    '<div style="background:#0f172a;border:1px dashed #334155;border-radius:8px;'
                    'padding:32px;text-align:center;color:#475569">No resume preview available</div>',
                    unsafe_allow_html=True,
                )

            # ── Screenshot ────────────────────────────────────────────────
            screenshot_path = Path(screenshot) if screenshot else None
            screenshot_exists = screenshot_path and screenshot_path.exists()
            screenshot_real   = screenshot_exists and screenshot_path.stat().st_size > 30_000

            st.markdown('<div style="margin-top:14px"></div>', unsafe_allow_html=True)
            if screenshot_real:
                st.markdown('<div style="font-size:0.8rem;color:#94a3b8;margin-bottom:4px">🖥 Filled Form Screenshot</div>',
                            unsafe_allow_html=True)
                st.image(str(screenshot_path), use_container_width=True)
            else:
                is_hn = "ycombinator" in url
                note  = "HN job post — no ATS form to fill. Apply via the link above." if is_hn else "Screenshot not available."
                st.markdown(
                    f'<div style="background:#0f172a;border:1px dashed #334155;border-radius:8px;'
                    f'padding:14px 16px;display:flex;align-items:center;gap:10px">'
                    f'<span style="font-size:1.3rem">🔗</span>'
                    f'<span style="color:#64748b;font-size:0.8rem">{note}</span></div>',
                    unsafe_allow_html=True,
                )

        # Action buttons
        st.markdown('<div style="margin-top:16px"></div>', unsafe_allow_html=True)
        btn_col1, btn_col2, btn_spacer = st.columns([1, 1, 3])

        with btn_col1:
            if st.button(
                "✅  Approve",
                key=f"approve_{ap_id}",
                type="primary",
                use_container_width=True,
            ):
                try:
                    resolve_approval(ap_id, "approved")
                    st.success(f"Approved — **{company}** will be submitted shortly.")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"Error approving: {e}")

        with btn_col2:
            if st.button(
                "❌  Reject",
                key=f"reject_{ap_id}",
                use_container_width=True,
            ):
                try:
                    resolve_approval(ap_id, "rejected")
                    st.warning(f"Rejected — **{company}** skipped.")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"Error rejecting: {e}")

        st.markdown("<hr style='border-color:#1e293b;margin:8px 0 0 0'>", unsafe_allow_html=True)
