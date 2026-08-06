"""
Auto-apply lane — "submit where safe, queue the rest".

Decides which reviewed applications are SAFE to submit headlessly without a human:
they must clear hard gates (fit, quality, sponsorship-friendliness, resume attached,
per-company cap, not already applied). The actual submit additionally requires NO
CAPTCHA on the form (detected at fill time in form_filler); CAPTCHA-gated forms are
parked for the user's one-tap extension flow.

Off by default — only runs when AUTONOMOUSHIRE_AUTO_SUBMIT=1 (or runner --auto-apply),
so nothing is ever sent unattended unless the user opts in.
"""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

from core.state import JobSearchState, JobListing
from core.logger import get_logger
from core.company_caps import cap_for as _cap_for, count_against_cap as _counts_against_cap
from agents.discovery.targeting import sponsorship_signal, rank_for_application

logger = get_logger("auto_apply")

# Higher bars than the discovery threshold (65): only auto-send genuinely strong fits.
MIN_FIT_AUTO = int(os.getenv("AUTO_SUBMIT_MIN_FIT", "75"))
MIN_QUALITY_AUTO = int(os.getenv("AUTO_SUBMIT_MIN_QUALITY", "78"))
# Per-company caps now come from data/company_caps.json (rolling-window, per-company overrides) via
# core.company_caps — same source as ats_intake, so the auto lane respects Roblox-style limits too.

_ROOT = Path(__file__).parent.parent.parent
_KILL_SWITCH = _ROOT / "data" / "auto_apply.disabled"

_ACTIVE = {"submitted", "awaiting_confirmation", "approved", "pending_manual_submit"}

# How likely a given ATS is to wall the submit with a CAPTCHA (0 = usually clear → can
# actually auto-send; 1 = almost always walled → will just queue for the extension).
# Greenhouse fires reCAPTCHA on nearly every form, so it's the worst auto-submit bet even
# when the fit is great. We try CAPTCHA-free ATS FIRST so the lane produces real sends
# instead of all-queue — walled forms still queue safely if slots remain.
_ATS_CAPTCHA_RISK = {
    "greenhouse": 0.9, "workday": 0.7, "icims": 0.6, "lever": 0.3,
    "ashby": 0.2, "smartrecruiters": 0.3, "bamboohr": 0.3, "rippling": 0.3,
    "generic": 0.4, "manual": 1.0,
}


def _captcha_risk(job: JobListing) -> float:
    from agents.application.form_filler import detect_ats
    url = job.get("apply_url") or job.get("url") or ""
    return _ATS_CAPTCHA_RISK.get(detect_ats(url), 0.5)


def auto_submit_enabled() -> bool:
    """Master switch: env opt-in AND no kill-switch file present."""
    return os.getenv("AUTONOMOUSHIRE_AUTO_SUBMIT", "0") == "1" and not _KILL_SWITCH.exists()


def _company_counts(state: JobSearchState) -> Counter:
    """How many applications are already live (submitted/queued) per company."""
    c: Counter = Counter()
    for a in state.get("applications", []):
        if _counts_against_cap(a):   # PENDING + SUBMITTED-within-window (rolling cap)
            c[(a.get("company_name") or "").strip().lower()] += 1
    return c


def auto_submit_gate(app: dict, job: JobListing, fit_score: dict, profile: dict,
                     company_counts: Counter) -> tuple[bool, list[str]]:
    """Hard gate — every check must pass for a hands-off submit. Returns (ok, reasons)."""
    reasons: list[str] = []
    requires = bool(profile.get("work_authorization", {}).get("requires_sponsorship"))

    # 1) Sponsorship-friendly (never burn a slot on an auto-reject company).
    label, _, why = sponsorship_signal(job.get("company_name", ""), job.get("description", ""), requires)
    if label in ("blocked", "unlikely"):
        reasons.append(f"sponsorship: {label} ({why})")

    # 2) Fit + quality bars (stricter than discovery).
    fit = (fit_score or {}).get("score", 0) or 0
    if fit < MIN_FIT_AUTO:
        reasons.append(f"fit {fit} < {MIN_FIT_AUTO}")
    # Missing/zero quality = NOT reviewed → must fail the gate (was `if q and q<MIN`, which let an
    # un-reviewed résumé with quality_score=0 auto-submit). Require a real score >= threshold.
    q = app.get("quality_score") or 0
    if q < MIN_QUALITY_AUTO:
        reasons.append(f"quality {q} < {MIN_QUALITY_AUTO} (or not reviewed)")

    # 3) Tailored resume present + non-empty.
    rp = app.get("tailored_resume_path") or ""
    p = Path(rp) if os.path.isabs(rp) else _ROOT / rp
    if not (rp and p.exists() and p.stat().st_size > 0):
        reasons.append("no tailored resume")

    # 4) Per-company rolling-window cap (respects employer limit policies, e.g. Roblox 5/30d).
    co = (job.get("company_name") or "").strip().lower()
    cap = _cap_for(co)
    if company_counts.get(co, 0) >= cap:
        reasons.append(f"per-company cap {cap:g} reached for {job.get('company_name')}")

    # 5) Referral-first: a high-fit role flagged for a warm intro should NOT be cold-auto-applied —
    # a referral converts ~10x the ATS pile, so defer to the human's referral outreach. Cleared once
    # the user acts (referral_done) so it can never be stuck in limbo forever.
    if app.get("referral_first") and not app.get("referral_done"):
        reasons.append("referral-first: warm intro pending (defer cold auto-apply)")

    return (len(reasons) == 0, reasons)


def select_auto_submit(state: JobSearchState, profile: dict, limit: int) -> dict:
    """Return {'submit': [job...], 'queue': [(job, reasons)...]} — the ranked reviewed
    apps split into auto-submittable (gate-passed) vs queue-for-human (gate-failed).
    Respects per-company cap as it accumulates within this run."""
    jobs = {j.get("id"): j for j in state.get("job_listings", [])}
    fits = {f.get("job_id"): f for f in state.get("fit_scores", [])}
    apps_by_job = {a.get("job_id"): a for a in state.get("applications", [])
                   if a.get("status") == "pending_review"}

    reviewed_jobs = [jobs[jid] for jid in apps_by_job if jid in jobs]
    ranked = rank_for_application(reviewed_jobs, fits, profile, state)  # sponsor/recency/fit order

    # Prefer CAPTCHA-free ATS so the auto lane actually SENDS rather than all-queues.
    # Stable-sort by CAPTCHA risk only — ties keep the sponsor/recency/fit order above, so
    # a great fit on a clear ATS still beats a clear ATS on a weak fit within the same tier.
    ranked.sort(key=lambda r: _captcha_risk(r["job"]))

    counts = _company_counts(state)
    submit: list = []
    queue: list = []
    for r in ranked:
        job = r["job"]
        app = apps_by_job.get(job.get("id"))
        if not app:
            continue
        ok, reasons = auto_submit_gate(app, job, fits.get(job.get("id"), {}), profile, counts)
        if ok and len(submit) < limit:
            submit.append(job)
            counts[(job.get("company_name") or "").strip().lower()] += 1  # reserve the slot
        else:
            queue.append((job, reasons or ["daily limit reached"]))
    logger.info("auto_apply_selected", submit=len(submit), queue=len(queue),
                kill_switch=_KILL_SWITCH.exists(), enabled=auto_submit_enabled())
    return {"submit": submit, "queue": queue}
