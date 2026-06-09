"""
Targeting layer — prioritise which applications to spend effort on, for a candidate
who needs visa sponsorship. The single biggest lever on callback-rate for such a
candidate is NOT applying into auto-reject roles (no-sponsor companies, citizen-only,
clearance) and PRIORITISING sponsor-friendly + fresh + high-fit roles.

`rank_for_application()` is the entry point: it drops hard-blocked jobs and orders the
rest by priority = fit × sponsorship × recency. Deterministic, no extra LLM calls.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from core.state import JobSearchState, JobListing

_ROOT = Path(__file__).parent.parent.parent
_SPONSORS_FILE = _ROOT / "data" / "h1b_sponsors.json"

# Hard knock-outs for a sponsorship-needing candidate (citizen-only / clearance / no-sponsor).
_BLOCK_PHRASES = [
    "unable to sponsor", "cannot sponsor", "will not sponsor", "does not sponsor",
    "no sponsorship", "not able to sponsor", "sponsorship is not", "no visa sponsor",
    "not provide visa", "us citizens only", "u.s. citizens only", "citizenship is required",
    "must be a u.s. citizen", "must be a us citizen", "active security clearance",
    "security clearance required", "ts/sci", "secret clearance", "polygraph",
    "no work visa", "without sponsorship now or in the future",
]

# Company-name signals of a third-party staffing/recruiting agency (NOT a direct employer).
# Deliberately specific — excludes "consulting"/"solutions"/"technologies" since real
# sponsors (Deloitte, Accenture, Infosys) use those. Only clear agency words.
_AGENCY_PHRASES = [
    "staffing", "recruiters", "recruiting", "talent solutions", "talent acquisition",
    "staffing solutions", "search partners", "headhunter", "placement services",
    "staffing agency", "resourcing", "talent group", "staffing group", "recruitment",
]

# Priority weights (sum ~1). Fit dominates — never chase a sponsor we don't fit — but
# sponsorship is weighted heavily because it's the candidate's biggest auto-reject filter.
_W_FIT, _W_SPONSOR, _W_RECENCY = 0.55, 0.30, 0.15
_RECENCY_HALFLIFE_DAYS = 7.0


def _norm(s: str) -> str:
    return (s or "").lower().strip()


_SPONSOR_CACHE: dict | None = None


def _word_match(needle: str, haystack: str) -> bool:
    """True if `needle` appears as a whole word/phrase in `haystack` (word-boundary, not substring).
    Prevents 'citi'∈'velocity', 'box'∈'dropbox', 'block'∈'blockdaemon' false sponsor matches."""
    needle = (needle or "").strip()
    if not needle:
        return False
    return re.search(r"\b" + re.escape(needle) + r"\b", haystack or "") is not None


def _sponsor_data() -> dict:
    global _SPONSOR_CACHE
    if _SPONSOR_CACHE is None:
        try:
            d = json.loads(_SPONSORS_FILE.read_text())
            _SPONSOR_CACHE = {
                "sponsors": [x.lower() for x in d.get("sponsors", [])],
                "unlikely": [x.lower() for x in d.get("unlikely_keywords", [])],
            }
        except Exception:
            _SPONSOR_CACHE = {"sponsors": [], "unlikely": []}
    return _SPONSOR_CACHE


def sponsorship_signal(company: str, jd_text: str, requires_sponsorship: bool,
                       learned_no: set[str] | None = None) -> tuple[str, float, str]:
    """(label, score 0..1, reason). score is a priority multiplier, not a probability.
    Only `blocked` is a hard skip; everything else just ranks differently."""
    if not requires_sponsorship:
        return ("n/a", 1.0, "candidate does not need sponsorship")
    co = _norm(company)
    jd = _norm(jd_text)
    learned_no = learned_no or set()

    for p in _BLOCK_PHRASES:
        if p in jd:
            return ("blocked", 0.0, f"JD knock-out: '{p}'")
    if co and any(co == n or _word_match(n, co) for n in learned_no):
        return ("blocked", 0.0, "company previously rejected on sponsorship")
    # Third-party staffing/recruiting agencies (e.g. "Quik Hire Staffing") — they don't
    # employ/sponsor directly and flood the queue with reposts. Hard-skip (user-skip learning).
    if co and any(p in co for p in _AGENCY_PHRASES):
        return ("blocked", 0.0, "third-party staffing/recruiting agency — skip")

    data = _sponsor_data()
    # WORD-BOUNDARY match, not substring: 'citi' must not match 'veloCITY', 'box' not 'DropBOX',
    # 'block' not 'Blockdaemon'. \b ensures whole-word/phrase matches only.
    if co and any(_word_match(s, co) for s in data["sponsors"]):
        return ("sponsor", 1.0, "known H1B sponsor")
    if co and any(_word_match(k, co) for k in data["unlikely"]):
        return ("unlikely", 0.15, "staffing/consulting — rarely sponsors")
    return ("unknown", 0.55, "no sponsorship history on file")


def recency_score(job: JobListing, now: datetime | None = None) -> float:
    """0..1 — fresher postings score higher (callback odds fall fast after 48h)."""
    now = now or datetime.now(timezone.utc)
    ts = job.get("posted_at") or job.get("discovered_at")
    if not ts:
        return 0.5
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
        return round(math.exp(-age_days / _RECENCY_HALFLIFE_DAYS), 3)
    except Exception:
        return 0.5


def priority(fit_0_100: float, sponsor_score: float, recency_0_1: float) -> float:
    fit = max(0.0, min(1.0, (fit_0_100 or 0) / 100.0))
    return round(_W_FIT * fit + _W_SPONSOR * sponsor_score + _W_RECENCY * recency_0_1, 4)


def learned_non_sponsors(state: JobSearchState) -> set[str]:
    """Companies that rejected us citing sponsorship/visa → treat as non-sponsors."""
    out: set[str] = set()
    for ev in state.get("email_events", []) or []:
        if ev.get("category") != "rejection":
            continue
        text = _norm((ev.get("summary") or "") + " " + (ev.get("subject") or ""))
        if any(k in text for k in ("sponsor", "visa", "work authorization", "immigration")):
            co = _norm(ev.get("company_name") or "")
            if co:
                out.add(co)
    return out


def rank_for_application(jobs: list[JobListing], fit_by_id: dict, profile: dict,
                         state: JobSearchState | None = None) -> list[dict]:
    """Drop hard-blocked jobs; return the rest ordered by priority (best first).
    Each item: {job, fit, sponsor_label, sponsor_reason, recency, priority}."""
    requires = bool(profile.get("work_authorization", {}).get("requires_sponsorship"))
    learned_no = learned_non_sponsors(state) if state else set()
    now = datetime.now(timezone.utc)
    ranked: list[dict] = []
    for job in jobs:
        fs = fit_by_id.get(job.get("id"), {})
        # Unscored jobs (no fit_score record yet) get a NEUTRAL prior — the apply pipeline
        # scores them downstream. Without this, fit=0 buries unscored known-sponsors below
        # a rare pre-scored staffing firm. A real low score (record present) is respected.
        fit = fs["score"] if isinstance(fs, dict) and "score" in fs else 60
        label, sscore, reason = sponsorship_signal(
            job.get("company_name", ""), job.get("description", ""), requires, learned_no)
        if label == "blocked":
            continue
        rec = recency_score(job, now)
        ranked.append({
            "job": job, "fit": fit, "sponsor_label": label, "sponsor_reason": reason,
            "recency": rec, "priority": priority(fit, sscore, rec),
        })
    ranked.sort(key=lambda r: r["priority"], reverse=True)
    return ranked
