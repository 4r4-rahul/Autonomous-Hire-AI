"""
Companies & Jobs component — discovered companies and top-scoring job listings.
"""

from __future__ import annotations

import streamlit as st

try:
    import pandas as pd
    _PANDAS = True
except ImportError:
    _PANDAS = False


def _score_color(score: float) -> str:
    if score >= 80:
        return "#22c55e"
    if score >= 60:
        return "#f59e0b"
    return "#ef4444"


def _score_badge(score: float | int | None) -> str:
    if score is None:
        return '<span style="color:#475569">—</span>'
    s = float(score)
    color = _score_color(s)
    return (
        f'<span style="background:{color}22;border:1px solid {color};'
        f'border-radius:4px;padding:2px 8px;font-size:0.8rem;'
        f'font-weight:700;color:{color}">{s:.0f}</span>'
    )


def _remote_tag(job: dict) -> str:
    remote = job.get("remote", False) or "remote" in job.get("title", "").lower()
    if remote:
        return '<span style="background:#3b82f622;border:1px solid #3b82f6;border-radius:20px;padding:2px 8px;font-size:0.7rem;color:#3b82f6">REMOTE</span>'
    return ""


def render(state: dict | None, approvals: list[dict]) -> None:
    st.markdown("""
    <h2 style="color:#f1f5f9;font-size:1.5rem;margin:0 0 20px 0">🏢 Companies &amp; Jobs</h2>
    """, unsafe_allow_html=True)

    if not state:
        st.markdown("""
        <div style="background:#1e293b;border:1px solid #334155;border-radius:12px;
        padding:48px 32px;text-align:center">
            <div style="font-size:3rem;margin-bottom:12px">🏢</div>
            <div style="color:#94a3b8;font-size:1rem">No pipeline data yet.</div>
        </div>
        """, unsafe_allow_html=True)
        return

    companies    = state.get("companies", [])
    jobs         = state.get("job_listings", [])
    fit_scores   = state.get("fit_scores", [])

    # Build lookup: job_id → fit score
    score_map = {fs["job_id"]: fs for fs in fit_scores}

    # ── Tab selector ──────────────────────────────────────────────────────────────
    sec_tab1, sec_tab2 = st.tabs([
        f"🏢 Companies ({len(companies)})",
        f"📝 Top Jobs ({min(len(jobs), 50)})",
    ])

    # ════════════════════════════════════════════════════════════════════════════
    # Companies sub-tab
    # ════════════════════════════════════════════════════════════════════════════
    with sec_tab1:
        if not companies:
            st.markdown('<div style="color:#475569;margin-top:24px">No companies discovered yet.</div>', unsafe_allow_html=True)
        else:
            # Filters
            c1, c2, c3 = st.columns([2, 2, 1])
            with c1:
                company_search = st.text_input(
                    "Search companies",
                    placeholder="Name or domain...",
                    key="co_search",
                    label_visibility="collapsed",
                )
            with c2:
                all_domains = sorted({c.get("domain", "") for c in companies if c.get("domain")})
                domain_filter = st.multiselect(
                    "Domain",
                    options=all_domains,
                    placeholder="All domains",
                    key="co_domain",
                    label_visibility="collapsed",
                )
            with c3:
                sort_co = st.selectbox(
                    "Sort",
                    ["Score ↓", "Name A-Z", "Size"],
                    key="co_sort",
                    label_visibility="collapsed",
                )

            filtered_cos = companies
            if company_search:
                q = company_search.lower()
                filtered_cos = [c for c in filtered_cos if
                                q in c.get("name", "").lower() or
                                q in c.get("domain", "").lower()]
            if domain_filter:
                filtered_cos = [c for c in filtered_cos if c.get("domain") in domain_filter]

            size_order = {"large": 0, "mid": 1, "small": 2, "startup": 3}
            sort_fn = {
                "Score ↓":  lambda c: -(float(c.get("hiring_score") or c.get("financial_score") or 0)),
                "Name A-Z": lambda c: c.get("name", "").lower(),
                "Size":     lambda c: size_order.get(c.get("size", ""), 99),
            }.get(sort_co, lambda c: -(float(c.get("hiring_score") or 0)))

            filtered_cos = sorted(filtered_cos, key=sort_fn)

            st.markdown(
                f'<div style="color:#64748b;font-size:0.8rem;margin-bottom:12px">'
                f'{len(filtered_cos)} companies</div>',
                unsafe_allow_html=True,
            )

            if _PANDAS and filtered_cos:
                df = pd.DataFrame([{
                    "Company":  c.get("name", ""),
                    "Domain":   c.get("domain", ""),
                    "Size":     c.get("size", ""),
                    "Score":    float(c.get("hiring_score") or c.get("financial_score") or 0),
                } for c in filtered_cos])

                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Company": st.column_config.TextColumn("Company", width="large"),
                        "Domain":  st.column_config.TextColumn("Domain",  width="medium"),
                        "Size":    st.column_config.TextColumn("Size",    width="small"),
                        "Score":   st.column_config.ProgressColumn(
                            "Hiring Score",
                            format="%.0f",
                            min_value=0,
                            max_value=100,
                        ),
                    },
                )
            else:
                # Fallback cards
                for co in filtered_cos[:50]:
                    name   = co.get("name", "Unknown")
                    domain = co.get("domain", "")
                    size   = co.get("size", "")
                    score  = co.get("hiring_score") or co.get("financial_score")
                    s_html = _score_badge(score)

                    st.markdown(
                        f'<div style="display:flex;justify-content:space-between;align-items:center;'
                        f'padding:10px 14px;background:#1e293b;border:1px solid #334155;'
                        f'border-radius:6px;margin-bottom:6px">'
                        f'<div>'
                        f'<span style="font-weight:600;color:#f1f5f9">{name}</span>'
                        f'{"  <span style=\'color:#64748b;font-size:0.8rem\'>" + domain + "</span>" if domain else ""}'
                        f'</div>'
                        f'<div style="display:flex;gap:12px;align-items:center">'
                        f'<span style="font-size:0.75rem;color:#94a3b8">{size}</span>'
                        f'{s_html}</div></div>',
                        unsafe_allow_html=True,
                    )

    # ════════════════════════════════════════════════════════════════════════════
    # Jobs sub-tab
    # ════════════════════════════════════════════════════════════════════════════
    with sec_tab2:
        if not jobs:
            st.markdown('<div style="color:#475569;margin-top:24px">No jobs found yet.</div>', unsafe_allow_html=True)
        else:
            # Sort by fit score descending
            jobs_with_score = []
            for j in jobs:
                fs = score_map.get(j.get("id", ""), {})
                jobs_with_score.append((j, fs.get("score", 0), fs))

            jobs_with_score.sort(key=lambda x: -x[1])
            top_jobs = jobs_with_score[:50]

            # Filters
            jc1, jc2, jc3 = st.columns([2, 2, 1])
            with jc1:
                job_search = st.text_input(
                    "Search jobs",
                    placeholder="Title or company...",
                    key="job_search",
                    label_visibility="collapsed",
                )
            with jc2:
                all_job_companies = sorted({j.get("company_name", "") for j, _, _ in top_jobs if j.get("company_name")})
                co_filter = st.multiselect(
                    "Company",
                    options=all_job_companies,
                    placeholder="All companies",
                    key="job_co",
                    label_visibility="collapsed",
                )
            with jc3:
                min_fit = st.number_input("Min fit score", value=0, min_value=0, max_value=100, step=10, key="min_fit")

            filtered_jobs = top_jobs
            if job_search:
                q = job_search.lower()
                filtered_jobs = [(j, s, fs) for j, s, fs in filtered_jobs if
                                 q in j.get("title", "").lower() or
                                 q in j.get("company_name", "").lower()]
            if co_filter:
                filtered_jobs = [(j, s, fs) for j, s, fs in filtered_jobs if j.get("company_name") in co_filter]
            if min_fit > 0:
                filtered_jobs = [(j, s, fs) for j, s, fs in filtered_jobs if s >= min_fit]

            st.markdown(
                f'<div style="color:#64748b;font-size:0.8rem;margin-bottom:12px">'
                f'{len(filtered_jobs)} jobs</div>',
                unsafe_allow_html=True,
            )

            for job, fit_score, fs in filtered_jobs:
                title     = job.get("title", "Unknown")
                company   = job.get("company_name", "Unknown")
                apply_url = job.get("apply_url", "") or job.get("url", "")
                passed    = fs.get("passed_threshold", False)
                reasoning = fs.get("reasoning", "")
                missing   = fs.get("missing_skills", [])
                highlights = fs.get("match_highlights", [])

                score_color = _score_color(fit_score)
                passed_indicator = (
                    '<span style="color:#22c55e;font-size:0.75rem">✓ Passed threshold</span>'
                    if passed else
                    '<span style="color:#64748b;font-size:0.75rem">Below threshold</span>'
                )

                with st.container():
                    st.markdown(
                        f'<div style="background:#1e293b;border:1px solid #334155;'
                        f'border-left:3px solid {score_color};'
                        f'border-radius:8px;padding:12px 16px;margin-bottom:2px">',
                        unsafe_allow_html=True,
                    )
                    col1, col2, col3 = st.columns([4, 2, 1])
                    with col1:
                        link_html = (
                            f'<a href="{apply_url}" target="_blank" style="font-weight:600;color:#f1f5f9;text-decoration:none">{title}</a>'
                            if apply_url else
                            f'<span style="font-weight:600;color:#f1f5f9">{title}</span>'
                        )
                        st.markdown(
                            f'{link_html}'
                            f'<br><span style="font-size:0.85rem;color:#94a3b8">{company}</span>'
                            f'  {_remote_tag(job)}',
                            unsafe_allow_html=True,
                        )
                    with col2:
                        st.markdown(passed_indicator, unsafe_allow_html=True)
                    with col3:
                        st.markdown(_score_badge(fit_score), unsafe_allow_html=True)
                    st.markdown("</div>", unsafe_allow_html=True)

                    if reasoning or missing or highlights:
                        with st.expander("Fit details", expanded=False):
                            if reasoning:
                                st.markdown(f'<div style="font-size:0.8rem;color:#94a3b8;margin-bottom:8px">{reasoning}</div>', unsafe_allow_html=True)
                            if highlights:
                                st.markdown('<div style="font-size:0.75rem;font-weight:600;color:#22c55e;margin-bottom:4px">MATCH HIGHLIGHTS</div>', unsafe_allow_html=True)
                                for h in highlights[:3]:
                                    st.markdown(f'<div style="font-size:0.78rem;color:#94a3b8">✓ {h}</div>', unsafe_allow_html=True)
                            if missing:
                                st.markdown('<div style="font-size:0.75rem;font-weight:600;color:#f59e0b;margin-top:8px;margin-bottom:4px">MISSING SKILLS</div>', unsafe_allow_html=True)
                                st.markdown(
                                    f'<div style="font-size:0.78rem;color:#94a3b8">{", ".join(missing[:6])}</div>',
                                    unsafe_allow_html=True,
                                )
