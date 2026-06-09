"""
LinkedIn Pipeline Page — step-by-step visibility, queue, applied, blockers, challenges.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import streamlit as st

_ROOT = Path(__file__).parent.parent.parent
_APPLIED_LOG   = _ROOT / "data" / "applied_jobs.json"
_STATE_PATH    = _ROOT / "data" / "pipeline_state.json"
_PIPELINE_LOG  = Path("/tmp/autonomoushire_pipeline.log")

# ── Colour helpers ─────────────────────────────────────────────────────────────

_STEP_COLORS = {
    "done":    "#22c55e",
    "active":  "#3b82f6",
    "pending": "#334155",
    "skip":    "#f59e0b",
    "fail":    "#ef4444",
}

def _pill(label: str, color: str) -> str:
    return (
        f'<span style="background:{color}22;border:1px solid {color};'
        f'border-radius:20px;padding:2px 10px;font-size:0.72rem;'
        f'font-weight:600;color:{color}">{label}</span>'
    )

def _card(content: str, border_color: str = "#334155", extra_style: str = "") -> str:
    return (
        f'<div style="background:#1e293b;border:1px solid #334155;'
        f'border-left:3px solid {border_color};border-radius:8px;'
        f'padding:14px 16px;margin-bottom:10px;{extra_style}">'
        f'{content}</div>'
    )

def _metric(label: str, value: int | str, color: str = "#3b82f6") -> str:
    return (
        f'<div style="background:#1e293b;border:1px solid #334155;border-top:2px solid {color};'
        f'border-radius:8px;padding:14px;text-align:center">'
        f'<div style="font-size:1.8rem;font-weight:800;color:#f1f5f9">{value}</div>'
        f'<div style="font-size:0.7rem;color:#94a3b8;margin-top:4px;text-transform:uppercase;'
        f'letter-spacing:0.05em">{label}</div></div>'
    )

def _section(title: str) -> None:
    st.markdown(
        f'<div style="font-size:0.75rem;font-weight:700;color:#475569;letter-spacing:0.1em;'
        f'text-transform:uppercase;margin:28px 0 12px 0;border-bottom:1px solid #1e293b;padding-bottom:6px">'
        f'{title}</div>',
        unsafe_allow_html=True,
    )


# ── Data loaders ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=10)
def _load_applied_log() -> list[dict]:
    if not _APPLIED_LOG.exists():
        return []
    try:
        return json.loads(_APPLIED_LOG.read_text())
    except Exception:
        return []


@st.cache_data(ttl=10)
def _load_state() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _pipeline_running() -> bool:
    pid_file = Path("/tmp/autonomoushire_pipeline.pid")
    if not pid_file.exists():
        return False
    try:
        import os
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _live_step_from_log() -> Optional[str]:
    """Parse the tail of the pipeline log to infer the current step."""
    if not _PIPELINE_LOG.exists():
        return None
    try:
        lines = _PIPELINE_LOG.read_text().splitlines()
        tail = "\n".join(lines[-30:])
    except Exception:
        return None

    step_patterns = [
        ("navigating to job page",  "Navigate to job page"),
        ("extracting description",  "Extract job description"),
        ("external_apply_button",   "Detect ATS URL"),
        ("scoring",                 "Score fit (Haiku)"),
        ("tailoring resume",        "Tailor resume (Sonnet)"),
        ("quality review",          "Quality review (Haiku)"),
        ("submitting to",           "Submit via ATS form"),
        ("li_scan",                 "Scan LinkedIn for jobs"),
        ("searching linkedin",      "Scan LinkedIn for jobs"),
        ("linkedin_apply_pipeline", "Process LinkedIn job"),
    ]
    tail_lower = tail.lower()
    for pattern, label in reversed(step_patterns):
        if pattern in tail_lower:
            return label
    return None


# ── Pipeline steps definition ─────────────────────────────────────────────────

_PIPELINE_STEPS = [
    ("1", "Scan LinkedIn",        "Search job listings — 10/hr, 60/day rate limit"),
    ("2", "Navigate + Extract",   "Open each job page, expand description"),
    ("3", "Detect ATS URL",       "Find 'Apply on company website' button — skip Easy Apply"),
    ("4", "Filter junk",          "Skip staffing agencies, gig platforms, tracker domains"),
    ("5", "Score fit",            "Haiku scores 0-100, threshold 65 — low scores are dropped"),
    ("6", "Tailor resume",        "Sonnet rewrites resume bullets for job keywords"),
    ("7", "Quality review",       "Haiku checks ATS keywords, value story, formatting"),
    ("8", "Submit via ATS",       "Playwright fills the form — Greenhouse, Lever, Rippling, etc."),
]


def _steps_diagram(current_step_label: Optional[str] = None) -> None:
    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:4px">',
        unsafe_allow_html=True,
    )
    parts = []
    for num, name, desc in _PIPELINE_STEPS:
        active = current_step_label and name.lower() in (current_step_label or "").lower()
        if active:
            color = _STEP_COLORS["active"]
            bg    = "#3b82f622"
            border = "2px solid #3b82f6"
        else:
            color = "#64748b"
            bg    = "#0f172a"
            border = "1px solid #334155"

        parts.append(
            f'<div style="background:{bg};border:{border};border-radius:8px;'
            f'padding:8px 12px;min-width:110px;cursor:default" title="{desc}">'
            f'<div style="font-size:0.65rem;color:{color};font-weight:700;margin-bottom:2px">STEP {num}</div>'
            f'<div style="font-size:0.8rem;color:#cbd5e1;font-weight:600">{name}</div>'
            f'<div style="font-size:0.68rem;color:#475569;margin-top:2px;line-height:1.3">{desc[:45]}{"…" if len(desc)>45 else ""}</div>'
            f'</div>'
        )
        if num != "8":
            parts.append('<div style="color:#334155;font-size:1.2rem;align-self:center">→</div>')

    st.markdown("".join(parts) + "</div>", unsafe_allow_html=True)


# ── Blocker classifier ────────────────────────────────────────────────────────

def _classify_blocker(app: dict, job: dict | None) -> tuple[str, str]:
    """Return (blocker_label, explanation) for a non-submitted app."""
    status = app.get("status", "")
    rejection = (app.get("rejection_reason") or "").lower()
    company = app.get("company_name", "")
    ats_url = (job or {}).get("apply_url", "") if job else ""

    if status == "blocked":
        if "form load" in rejection or "timeout" in rejection or "fill failed" in rejection:
            return "ATS Form Timeout", f"Playwright couldn't load or fill the form — non-standard ATS or slow page ({ats_url[:50] or company})"
        if rejection:
            return "Form Fill Error", rejection[:120]
        return "Unknown Block", "Quality review set status=blocked but left no rejection reason"

    if status == "pending_manual_submit":
        return "Needs Manual Submit", "ATS automation couldn't complete — needs a human to finish the form"

    if status == "pending_review":
        score = app.get("quality_score", 0)
        if score and score < 75:
            return "Quality Below 75", f"Quality score {score}/100 — in queue but may need improvement before submit"
        return "Awaiting Submit", f"Quality score {score}/100 — ready but not yet sent (run pipeline again)"

    return "Unknown", status


# ── Main render ───────────────────────────────────────────────────────────────

def render(state: dict | None, approvals: list[dict]) -> None:
    st.markdown(
        '<h2 style="color:#f1f5f9;font-size:1.5rem;margin:0 0 6px 0">🔗 LinkedIn Pipeline</h2>',
        unsafe_allow_html=True,
    )

    state = state or _load_state()
    applied_log = _load_applied_log()
    running = _pipeline_running()
    current_step = _live_step_from_log() if running else None

    all_jobs  = state.get("job_listings", [])
    all_apps  = state.get("applications", [])
    all_scores = state.get("fit_scores", [])

    # LinkedIn-sourced jobs
    li_jobs = [j for j in all_jobs if j.get("source") == "linkedin"]

    # Applied log entries (ground truth submissions)
    li_applied_log = applied_log  # all entries in applied_jobs.json came from LI pipeline

    # Build applied URL set to find queue
    applied_urls: set[str] = {e.get("apply_url", "") for e in applied_log}
    applied_ids: set[str] = {
        a.get("job_id", "") for a in all_apps
        if a.get("status") in ("submitted", "pending_review", "approved")
    }
    li_queued = [
        j for j in li_jobs
        if "linkedin.com/jobs/view/" in j.get("apply_url", "")
        and j.get("apply_url", "") not in applied_urls
        and j.get("id", "") not in applied_ids
    ]

    # Apps whose source job was LinkedIn
    li_job_ids = {j["id"] for j in li_jobs}
    li_apps = [a for a in all_apps if a.get("job_id", "") in li_job_ids]

    # Fit scores for LinkedIn jobs
    li_score_map = {
        s["job_id"]: s for s in all_scores if s.get("job_id") in li_job_ids
    }

    # Status counts
    status_counts = Counter(a.get("status") for a in li_apps)
    n_submitted   = len([e for e in applied_log])  # actual submitted from log
    n_pending_rev = status_counts.get("pending_review", 0)
    n_manual      = status_counts.get("pending_manual_submit", 0)
    n_blocked     = status_counts.get("blocked", 0)
    n_queued      = len(li_queued)
    n_li_found    = len(li_jobs)

    # ── Running banner ────────────────────────────────────────────────────────
    if running:
        step_str = f" — currently: <strong>{current_step}</strong>" if current_step else ""
        st.markdown(
            f'<div style="background:#3b82f622;border:1px solid #3b82f6;border-radius:8px;'
            f'padding:10px 16px;margin-bottom:18px;color:#93c5fd;font-size:0.875rem">'
            f'● Pipeline is running{step_str}</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="color:#475569;font-size:0.8rem;margin-bottom:18px">'
            'Pipeline is idle — use sidebar to run LinkedIn batch</div>',
            unsafe_allow_html=True,
        )

    # ── Step diagram ──────────────────────────────────────────────────────────
    _section("Pipeline Steps")
    _steps_diagram(current_step)

    # ── Top KPIs ──────────────────────────────────────────────────────────────
    _section("LinkedIn Snapshot")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    metrics = [
        (c1, "Jobs Found",    n_li_found,    "#3b82f6"),
        (c2, "In Queue",      n_queued,      "#6366f1"),
        (c3, "Submitted",     n_submitted,   "#22c55e"),
        (c4, "Pending Review",n_pending_rev, "#f59e0b"),
        (c5, "Manual Submit", n_manual,      "#f59e0b"),
        (c6, "Blocked",       n_blocked,     "#ef4444"),
    ]
    for col, label, val, color in metrics:
        col.markdown(_metric(label, val, color), unsafe_allow_html=True)

    # ── Queue (next 10) ───────────────────────────────────────────────────────
    _section(f"Queue — {n_queued} jobs waiting ({min(n_queued,10)} will run next)")

    if not li_queued:
        st.markdown(
            '<div style="color:#64748b;font-size:0.875rem">Queue is empty — run a scan to discover new jobs.</div>',
            unsafe_allow_html=True,
        )
    else:
        next_batch = li_queued[:10]
        rest = li_queued[10:]

        rows_html = ""
        for i, job in enumerate(next_batch):
            score = li_score_map.get(job.get("id", ""), {})
            score_val = score.get("score") if score else None
            score_str = f'<span style="color:#f59e0b;font-weight:600">{score_val}</span>' if score_val else '<span style="color:#475569">unscored</span>'
            apply_url = job.get("apply_url", "")
            rows_html += (
                f'<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;'
                f'border-bottom:1px solid #0f172a;{"background:#1e293b;" if i%2==0 else ""}">'
                f'<div style="font-size:0.7rem;color:#475569;width:20px;text-align:right">{i+1}</div>'
                f'<div style="flex:1">'
                f'<div style="font-size:0.85rem;font-weight:600;color:#f1f5f9">{job.get("company_name","?")}</div>'
                f'<div style="font-size:0.75rem;color:#94a3b8">{job.get("title","?")[:60]}</div>'
                f'</div>'
                f'<div style="font-size:0.75rem">{score_str}</div>'
                f'<div style="font-size:0.65rem;color:#475569;max-width:200px;overflow:hidden;'
                f'text-overflow:ellipsis;white-space:nowrap">{apply_url}</div>'
                f'</div>'
            )

        st.markdown(
            f'<div style="background:#1e293b;border:1px solid #334155;border-radius:8px;'
            f'overflow:hidden">{rows_html}</div>',
            unsafe_allow_html=True,
        )
        if rest:
            st.markdown(
                f'<div style="color:#475569;font-size:0.78rem;margin-top:6px">'
                f'+ {len(rest)} more queued — run pipeline again after this batch completes</div>',
                unsafe_allow_html=True,
            )

    # ── Applied (submitted) ───────────────────────────────────────────────────
    _section(f"Submitted — {len(applied_log)} total")

    if not applied_log:
        st.markdown('<div style="color:#64748b;font-size:0.875rem">No submissions yet.</div>', unsafe_allow_html=True)
    else:
        # Deduplicate by apply_url
        seen: set[str] = set()
        unique_applied = []
        for e in reversed(applied_log):
            key = e.get("apply_url", e.get("app_id", ""))
            if key not in seen:
                seen.add(key)
                unique_applied.append(e)

        rows_html = ""
        for i, entry in enumerate(unique_applied):
            ts = entry.get("submitted_at", "")
            try:
                dt = datetime.fromisoformat(ts)
                ts_str = dt.strftime("%b %d %H:%M")
            except Exception:
                ts_str = ts[:16] if ts else "—"
            ats = entry.get("apply_url", "")
            rows_html += (
                f'<div style="display:flex;align-items:center;gap:10px;padding:9px 12px;'
                f'border-bottom:1px solid #0f172a;{"background:#1e293b;" if i%2==0 else ""}">'
                f'<div style="color:#22c55e;font-size:0.9rem">✓</div>'
                f'<div style="flex:1">'
                f'<div style="font-size:0.85rem;font-weight:600;color:#f1f5f9">{entry.get("company","?")}</div>'
                f'<div style="font-size:0.75rem;color:#94a3b8">{entry.get("role","?")[:60]}</div>'
                f'</div>'
                f'<div style="font-size:0.72rem;color:#64748b;text-align:right">{ts_str}</div>'
                f'</div>'
            )

        st.markdown(
            f'<div style="background:#1e293b;border:1px solid #334155;border-radius:8px;'
            f'overflow:hidden">{rows_html}</div>',
            unsafe_allow_html=True,
        )

    # ── Pending + Blocked analysis ────────────────────────────────────────────
    non_submitted = [a for a in all_apps if a.get("status") not in ("submitted", "skipped")]
    if non_submitted:
        _section(f"Pending & Blocked — {len(non_submitted)} applications need attention")

        job_lookup = {j["id"]: j for j in all_jobs}

        # Group by blocker type
        grouped: dict[str, list[tuple[dict, str]]] = defaultdict(list)
        for app in non_submitted:
            job = job_lookup.get(app.get("job_id", ""))
            blocker_label, explanation = _classify_blocker(app, job)
            grouped[blocker_label].append((app, explanation))

        for blocker_label, items in sorted(grouped.items(), key=lambda x: -len(x[1])):
            color_map = {
                "ATS Form Timeout":      "#ef4444",
                "Form Fill Error":       "#ef4444",
                "Needs Manual Submit":   "#f59e0b",
                "Quality Below 75":      "#f59e0b",
                "Awaiting Submit":       "#3b82f6",
                "Unknown Block":         "#ef4444",
                "Unknown":               "#64748b",
            }
            bcolor = color_map.get(blocker_label, "#64748b")

            with st.expander(
                f"{blocker_label}  ({len(items)})",
                expanded=(blocker_label in ("ATS Form Timeout", "Form Fill Error", "Needs Manual Submit")),
            ):
                for app, explanation in items:
                    company = app.get("company_name", "?")
                    role    = app.get("role_title", "?")[:55]
                    score   = app.get("quality_score", 0)
                    status  = app.get("status", "")

                    st.markdown(
                        f'<div style="background:#0f172a;border-left:3px solid {bcolor};'
                        f'border-radius:4px;padding:8px 12px;margin-bottom:6px">'
                        f'<div style="display:flex;justify-content:space-between;align-items:start">'
                        f'<div>'
                        f'<span style="font-size:0.875rem;font-weight:600;color:#f1f5f9">{company}</span>'
                        f'  <span style="font-size:0.8rem;color:#94a3b8">{role}</span>'
                        f'</div>'
                        f'<div style="text-align:right;flex-shrink:0;margin-left:12px">'
                        f'<span style="font-size:0.7rem;color:#64748b">{status.replace("_"," ")}</span>'
                        f'  <span style="font-size:0.75rem;color:#f59e0b;font-weight:600">{score}/100</span>'
                        f'</div></div>'
                        f'<div style="font-size:0.75rem;color:#64748b;margin-top:4px">{explanation}</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

    # ── Challenges & known issues ─────────────────────────────────────────────
    _section("Known Challenges & Why Jobs Get Skipped")

    challenges = [
        (
            "⏱  Rate Limits",
            "#f59e0b",
            [
                "LinkedIn Search: 10/hr, 60/day — running pipeline 3× in <1 hr exhausts the hourly quota",
                "Job View: 40/hr, 200/day — each job page navigation burns 1 credit",
                "When scan returns 0 new jobs, the pipeline falls back to the existing queue (149 jobs)",
                "Account age in data/linkedin_first_run.txt = 2026-04-28; warmup factor now at full capacity",
            ],
        ),
        (
            "✋  Easy Apply Jobs Skipped",
            "#6366f1",
            [
                "Most LinkedIn listings show Easy Apply — these are intentionally skipped (no real ATS form)",
                "Only jobs with 'Apply on company website' button proceed through the pipeline",
                "Button text is just 'Apply'; detection uses aria-label*='Apply on company website'",
                "Estimated 60-80% of queued jobs will be skipped at this step",
            ],
        ),
        (
            "🗑  Junk Companies & ATS Domains Filtered",
            "#64748b",
            [
                "Staffing agencies (Infojini, Kforce, Insight Global, etc.) skipped at card-scan level",
                "Tracker/gig platforms (jometer.com, outlier.ai, indeed.com redirects) skipped after ATS URL check",
                "Infojini is in queue — will be dropped at the junk-company filter step",
                "Saves API credits and apply-budget for real companies",
            ],
        ),
        (
            "🤖  ATS Form Fill Failures",
            "#ef4444",
            [
                "RadNet (careers.radnet.com): non-standard ATS, page load > 30s timeout → status=blocked",
                "SoFi blocked — quality review returned blocked with no rejection detail (needs investigation)",
                "12 jobs marked pending_manual_submit — these ATSes require interactive steps the bot can't handle",
                "Greenhouse, Lever, Rippling work reliably; Workday, BambooHR, iCIMS are not yet automated",
            ],
        ),
        (
            "🏷  'with verification' Title Artifact",
            "#94a3b8",
            [
                "LinkedIn scanner appends 'with verification' to some job titles — cosmetic bug in scanner parsing",
                "Doesn't affect scoring, ATS URL, or form fill — purely visual in the state/queue list",
                "Fix: strip ' with verification' suffix in linkedin_scanner.py card parsing",
            ],
        ),
        (
            "💳  API Credits Exhausted Mid-Run",
            "#f59e0b",
            [
                "Happened twice — Haiku scoring + Sonnet tailoring each consume Claude API credits",
                "One full 10-job batch costs ~$0.30-0.50 depending on description length",
                "When credits run out, the current job fails at scoring/tailoring and the pipeline stops",
                "Remaining jobs in the batch stay in queue and are retried on the next run",
            ],
        ),
    ]

    for title, color, bullets in challenges:
        with st.expander(title, expanded=False):
            for b in bullets:
                st.markdown(
                    f'<div style="font-size:0.82rem;color:#94a3b8;padding:4px 0;'
                    f'border-bottom:1px solid #1e293b">'
                    f'<span style="color:{color};margin-right:6px">›</span>{b}</div>',
                    unsafe_allow_html=True,
                )

    # ── Live log tail (if running) ────────────────────────────────────────────
    if running and _PIPELINE_LOG.exists():
        _section("Live Log")
        try:
            log_lines = _PIPELINE_LOG.read_text().splitlines()
            recent = "\n".join(log_lines[-25:])
            if recent.strip():
                st.markdown(
                    f'<div style="background:#0d1117;border:1px solid #1e293b;border-radius:8px;'
                    f'padding:12px 14px;font-family:monospace;font-size:0.72rem;'
                    f'color:#4ade80;white-space:pre-wrap;word-break:break-all;max-height:320px;'
                    f'overflow-y:auto">{recent}</div>',
                    unsafe_allow_html=True,
                )
        except Exception:
            pass

    # ── Rate limit health ─────────────────────────────────────────────────────
    _section("Rate Limit Health")
    try:
        from core.rate_limiter import get_rate_limiter
        limiter = get_rate_limiter()
        li_search_remaining = limiter.remaining("linkedin_search").get("hour", 0)
        li_view_remaining   = limiter.remaining("linkedin_job_view").get("hour", 0)
        rc1, rc2, rc3 = st.columns(3)
        rc1.markdown(
            _metric("Search / hr left", li_search_remaining, "#3b82f6" if li_search_remaining > 3 else "#ef4444"),
            unsafe_allow_html=True,
        )
        rc2.markdown(
            _metric("Job View / hr left", li_view_remaining, "#3b82f6" if li_view_remaining > 10 else "#ef4444"),
            unsafe_allow_html=True,
        )
        rc3.markdown(
            _metric("Queue Depth", n_queued, "#6366f1"),
            unsafe_allow_html=True,
        )
    except Exception:
        st.markdown(
            '<div style="color:#475569;font-size:0.82rem">Rate limiter not available — run pipeline at least once to initialize.</div>',
            unsafe_allow_html=True,
        )
