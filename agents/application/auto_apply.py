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
from core.employment import classify_employment
from agents.discovery.targeting import sponsorship_signal, rank_for_application, profile_fit

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
    """Master switch: config opt-in AND no kill-switch file present. Reads via Settings (which loads
    .env) rather than os.getenv — the raw-env read was a silent no-op because nothing exports .env
    into os.environ, so the opt-in never took effect. The kill-switch file still hard-disables."""
    from core.config import get_settings
    return get_settings().auto_submit_enabled and not _KILL_SWITCH.exists()


def _company_counts(state: JobSearchState) -> Counter:
    """How many applications are already live (submitted/queued) per company."""
    c: Counter = Counter()
    for a in state.get("applications", []):
        if _counts_against_cap(a, company=a.get("company_name")):   # PENDING + SUBMITTED-within-(per-company)-window
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

    # 2) Fit + quality bars (stricter than discovery). Coerce — state persists these as STRINGS
    # ("72"), and a str<int comparison raises; a hands-off lane must never crash on a bad type.
    def _num(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    # Fit lives in EITHER the state['fit_scores'] record OR on the app row (app['fit_score'], a
    # string) depending on the scoring path — read the record first, fall back to the app, so the
    # lane isn't silently blocked (fit=0) for apps whose fit was persisted on the row.
    fit = _num((fit_score or {}).get("score")) or _num(app.get("fit_score"))
    if fit < MIN_FIT_AUTO:
        reasons.append(f"fit {fit:g} < {MIN_FIT_AUTO}")
    # Missing/zero quality = NOT reviewed → must fail the gate (was `if q and q<MIN`, which let an
    # un-reviewed résumé with quality_score=0 auto-submit). Require a real score >= threshold.
    q = _num(app.get("quality_score"))
    if q < MIN_QUALITY_AUTO:
        reasons.append(f"quality {q:g} < {MIN_QUALITY_AUTO} (or not reviewed)")

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

    # 6) W2-ONLY: hands-off auto-apply is restricted to direct-employer W2 roles. Never cold-submit a
    # contract/staffing role automatically (they rarely sponsor and the terms need a human's eyes).
    etype = classify_employment(job.get("source", ""),
                                job.get("role_title") or job.get("title", ""),
                                job.get("description", ""))
    if etype != "w2":
        reasons.append(f"employment: {etype} (auto-apply is W2-only)")

    # 7) PROFILE FIT: don't auto-burn an apply slot on a structural mismatch (research/devrel/heavy-ML).
    # Sink < 1.0 means off-profile — defer those to human judgement rather than a hands-off send.
    pfit, pfit_reason = profile_fit(job.get("role_title") or job.get("title", ""),
                                    job.get("description", ""))
    if pfit < 1.0:
        reasons.append(f"profile-fit {pfit:g}: {pfit_reason}")

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
