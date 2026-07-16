"""Learning feedback loop — closes the loop by turning application OUTCOMES into
learnings that `fit_analyst` already reads from memory.

Pipeline: ghosting sweep → join outcomes with fit_score/job metadata → conversion
analytics by company / role-pattern / fit-band / source / ATS → volume-weighted
`memory.add_learning(...)` + a human-readable insights report.

Design (conservative, scales with data):
  • Down-weights via learnings; NEVER hard-excludes (reversible only).
  • A "deprioritize" learning needs ≥3 RESOLVED apps (responded or ghosted) with 0
    responses; a "prioritize" learning needs ≥1 positive (interview/offer).
  • SAFETY: if zero responses have been captured at all, it writes NO learnings — it
    can't distinguish real ghosting from `email_monitor` not having run — and flags it.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from core.state import JobSearchState
from core.memory import Memory
from core.logger import get_logger

logger = get_logger("learning")

# Below this overall response rate, the channel (not fit) is the problem — suppress all NEGATIVE
# (deprioritize) learnings so we don't teach the system to avoid good roles on channel noise.
_NEG_LEARNING_FLOOR = 0.05

_GHOST_DAYS = 21
_REPORT_JSON = Path("data/learning_insights.json")
_REPORT_MD = Path("data/learning_insights.md")
_APPLIED = Path("data/applied_jobs.json")

_POSITIVE = {"interview", "offer"}
_RESPONDED_NEG = {"rejected"}
_SUBMITTED = {"submitted", "awaiting_confirmation", "approved", "pending_review",
              "pending_manual_submit", "interview", "offer", "rejected", "ghosted"}
_PENDINGISH = {"submitted", "awaiting_confirmation", "approved", "pending_review",
               "pending_manual_submit"}

_ROLE_KEYWORDS = [
    "mlops", "ml platform", "ai platform", "ai infrastructure", "data platform",
    "machine learning", "ml engineer", "llm", "genai", "data engineer",
    "platform engineer", "data scientist", "data analyst", "site reliability", "devops",
    "solutions architect", "backend", "software engineer",
]


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _role_pattern(title: str) -> str:
    t = (title or "").lower()
    return next((kw for kw in _ROLE_KEYWORDS if kw in t), "other")


def _fit_band(score):
    if score is None:
        return "unknown"
    try:
        s = int(score)
    except Exception:
        return "unknown"
    return "65-69" if s < 70 else "70-79" if s < 80 else "80-89" if s < 90 else "90-100"


def _outcome(app, now) -> str | None:
    """positive | rejected | ghosted | pending | None(not a real submission)."""
    st = app.get("status")
    if st in _POSITIVE:
        return "positive"
    if st in _RESPONDED_NEG:
        return "rejected"
    if st == "ghosted":
        return "ghosted"
    if st in _PENDINGISH:
        sub = _parse(app.get("submitted_at"))
        if sub and (now - sub).days >= _GHOST_DAYS:
            return "ghosted"
        return "pending"
    return None


async def run(state: JobSearchState) -> JobSearchState:
    apps = state.get("applications", []) or []
    fit_by_job = {f.get("job_id"): f for f in (state.get("fit_scores", []) or [])}
    job_by_id = {j.get("id"): j for j in (state.get("job_listings", []) or [])}
    ats_by_job = {}
    try:
        for a in json.loads(_APPLIED.read_text()):
            if a.get("job_id"):
                ats_by_job[a["job_id"]] = a.get("ats")
    except Exception:
        pass

    now = datetime.now(timezone.utc)

    # 1) Ghosting sweep — mark outcome on each app (status left intact for other logic).
    ghosted = 0
    recs = []
    for app in apps:
        if app.get("status") not in _SUBMITTED:
            continue
        oc = _outcome(app, now)
        if oc is None:
            continue
        if oc == "ghosted" and app.get("outcome") != "ghosted":
            ghosted += 1
        app["outcome"] = oc
        jid = app.get("job_id")
        job = job_by_id.get(jid, {})
        fit = fit_by_job.get(jid, {})
        title = app.get("role_title") or job.get("title") or ""
        recs.append({
            "company": app.get("company_name") or job.get("company_name") or "?",
            "title": title, "role": _role_pattern(title),
            "source": job.get("source") or "?", "ats": ats_by_job.get(jid) or "?",
            "band": _fit_band(fit.get("score")), "outcome": oc,
            "status": app.get("status"),
            "at": app.get("interview_at") or app.get("updated_at") or app.get("submitted_at"),
        })
    if ghosted:
        logger.info("learning_ghosting_sweep", marked=ghosted, after_days=_GHOST_DAYS)

    n_sub = len(recs)
    n_resp = sum(r["outcome"] in ("positive", "rejected") for r in recs)
    n_pos = sum(r["outcome"] == "positive" for r in recs)
    n_ghost = sum(r["outcome"] == "ghosted" for r in recs)
    n_pending = sum(r["outcome"] == "pending" for r in recs)
    resp_rate = round(n_resp / n_sub, 3) if n_sub else 0.0
    intv_rate = round(n_pos / n_sub, 3) if n_sub else 0.0
    state["response_rate"] = resp_rate
    state["interview_rate"] = intv_rate

    # 2) Aggregate per dimension.
    def agg(key):
        g = defaultdict(lambda: {"submitted": 0, "responded": 0, "positive": 0,
                                 "ghosted": 0, "resolved": 0})
        for r in recs:
            d = g[r[key]]; d["submitted"] += 1
            if r["outcome"] in ("positive", "rejected"):
                d["responded"] += 1; d["resolved"] += 1
            if r["outcome"] == "positive":
                d["positive"] += 1
            if r["outcome"] == "ghosted":
                d["ghosted"] += 1; d["resolved"] += 1
        return dict(g)

    dims = {d: agg(d) for d in ("company", "role", "band", "source", "ats")}

    # 3) Derive + write learnings — ONLY if we actually have a response signal.
    has_signal = n_resp > 0
    mem = Memory.get()
    written = []

    def emit(lid, text, tags):
        try:
            mem.add_learning(lid, text, tags); written.append(text)
        except Exception as e:
            logger.warning("learning_write_err", error=str(e)[:80])

    # Gate NEGATIVE learnings behind a baseline response-rate floor. When the overall response rate
    # is near-zero (e.g. 1%, a broken/cold channel), a "0 responses" group is evidence of the CHANNEL
    # failing, NOT of bad company/role fit — deprioritizing on it teaches the system to avoid good
    # roles for the wrong reason. Positive learnings (a real response) are always safe to write.
    neg_ok = resp_rate >= _NEG_LEARNING_FLOOR
    if has_signal and not neg_ok:
        logger.info("negative_learnings_suppressed", resp_rate=round(resp_rate, 3),
                    floor=_NEG_LEARNING_FLOOR, reason="channel-broken — 0-response != bad fit")

    if has_signal:
        for company, g in dims["company"].items():
            if company == "?":
                continue
            if g["positive"] >= 1:
                emit(f"resp-company-{company}".lower(),
                     f"{company}: responded to {g['positive']}/{g['submitted']} applications "
                     f"— PRIORITIZE roles at {company} and similar.", ["outcome", "positive", "company"])
            elif neg_ok and g["resolved"] >= 3 and g["responded"] == 0:
                emit(f"dead-company-{company}".lower(),
                     f"{company}: {g['resolved']} resolved applications, 0 responses "
                     f"— DEPRIORITIZE (down-weight) roles at {company}.", ["outcome", "negative", "company"])
        for role, g in dims["role"].items():
            if role == "other":
                continue
            if g["positive"] >= 1:
                emit(f"resp-role-{role}".lower(),
                     f"'{role}' roles converted {g['positive']}/{g['submitted']} "
                     f"— PRIORITIZE this role type.", ["outcome", "positive", "role"])
            elif neg_ok and g["resolved"] >= 4 and g["responded"] == 0:
                emit(f"dead-role-{role}".lower(),
                     f"'{role}' roles: {g['resolved']} resolved, 0 responses "
                     f"— DEPRIORITIZE this role type.", ["outcome", "negative", "role"])

    # 4) Insights report.
    # Company → prep-brief path, from the interview-prep records (interview_prep.prep_new_interviews
    # writes these). Lets the dashboard link "Prep ready" on each interview card.
    _prep_by_company = {
        (m.get("company_name") or "").strip().lower(): m.get("prep_doc_path")
        for m in state.get("meetings_scheduled", []) or [] if m.get("prep_doc_path")
    }
    band_lines = []
    for band in ("65-69", "70-79", "80-89", "90-100"):
        g = dims["band"].get(band)
        if g and g["resolved"] >= 3:
            band_lines.append(f"fit {band}: {g['responded']}/{g['resolved']} resolved responded "
                              f"({g['responded'] / g['resolved']:.0%})")
    report = {
        "generated_at": now.isoformat(),
        "totals": {"submitted": n_sub, "responded": n_resp, "interviews_or_offers": n_pos,
                   "ghosted": n_ghost, "pending": n_pending, "ghosted_this_run": ghosted,
                   "response_rate": resp_rate, "interview_rate": intv_rate,
                   "has_response_signal": has_signal},
        "by_company": dims["company"], "by_role": dims["role"], "by_fit_band": dims["band"],
        "by_source": dims["source"], "by_ats": dims["ats"], "learnings_written": written,
        # The actual wins, for the dashboard — first-party interviews/offers, newest first. Joined
        # against meetings_scheduled (by company) so the card can link the generated prep brief.
        "interviews": sorted(
            [{"company": r["company"], "role": r["title"], "status": r["status"], "at": r["at"],
              "prep_doc_path": _prep_by_company.get((r["company"] or "").strip().lower())}
             for r in recs if r["outcome"] == "positive"],
            key=lambda x: x.get("at") or "", reverse=True),
    }
    try:
        _REPORT_JSON.write_text(json.dumps(report, indent=2))
        _REPORT_MD.write_text(_render_md(report, band_lines, has_signal))
    except Exception as e:
        logger.warning("learning_report_err", error=str(e)[:80])

    logger.log_decision("learning_loop", "complete", {
        "submitted": n_sub, "responded": n_resp, "interviews": n_pos, "ghosted": n_ghost,
        "pending": n_pending, "learnings_written": len(written),
        "response_rate": resp_rate, "interview_rate": intv_rate, "has_signal": has_signal})
    return state


def _render_md(report, band_lines, has_signal) -> str:
    t = report["totals"]
    L = [f"# Learning Insights — {report['generated_at'][:19]}", "",
         f"**Submitted:** {t['submitted']}  ·  **Responded:** {t['responded']} "
         f"({t['response_rate']:.0%})  ·  **Interviews/Offers:** {t['interviews_or_offers']} "
         f"({t['interview_rate']:.0%})  ·  **Ghosted:** {t['ghosted']}  ·  **Pending:** {t['pending']}", ""]
    if not has_signal:
        L += ["> ⚠️ **No responses captured yet** — no learnings were written (can't tell real "
              "ghosting from `email_monitor` not having run). Verify email monitoring is active; "
              "the report below is informational only.", ""]
    if band_lines:
        L += ["## Conversion by fit band", *[f"- {b}" for b in band_lines], ""]

    def tbl(title, d, lbl):
        rows = sorted(d.items(), key=lambda kv: (-kv[1]["positive"], -kv[1]["responded"], -kv[1]["submitted"]))
        out = [f"## {title}", f"| {lbl} | submitted | responded | interviews | ghosted |", "|---|---|---|---|---|"]
        for k, g in rows[:15]:
            if k in ("?", "other"):
                continue
            out.append(f"| {k} | {g['submitted']} | {g['responded']} | {g['positive']} | {g['ghosted']} |")
        return out + [""]

    L += tbl("By company", report["by_company"], "company")
    L += tbl("By role type", report["by_role"], "role")
    L += tbl("By source", report["by_source"], "source")
    L += tbl("By ATS", report["by_ats"], "ats")
    if report["learnings_written"]:
        L += ["## Learnings written to memory (feed fit-scoring next run)",
              *[f"- {x}" for x in report["learnings_written"]], ""]
    return "\n".join(L)


if __name__ == "__main__":
    import asyncio
    from orchestration.runner import load_state, save_state
    st = load_state()
    if not st:
        print("no pipeline_state.json found"); raise SystemExit(1)
    st = asyncio.run(run(st))
    save_state(st)
    print(f"Learning loop complete → {_REPORT_MD}")
