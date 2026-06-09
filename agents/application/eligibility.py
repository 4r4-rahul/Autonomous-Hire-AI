"""
Pre-submission eligibility screening.

Catches structural dealbreakers BEFORE we tailor a resume and fill a form, so we
don't waste applications (and the employer's time) on roles that can't hire the
candidate. Deterministic keyword scan over the job title + description, checked
against the profile's work-authorization and location/comp preferences.

Returns {"eligible": bool, "blockers": [...], "warnings": [...]}.
  - blockers  → hard no-go (skip the application)
  - warnings  → proceed but surface (e.g. location/comp mismatch)
"""
from __future__ import annotations

import re


# Employer-does-not-sponsor phrasings (hard block when candidate needs sponsorship).
_NO_SPONSOR = (
    "not able to sponsor", "unable to sponsor", "does not sponsor", "do not sponsor",
    "cannot sponsor", "can not sponsor", "won't sponsor", "will not sponsor",
    "no visa sponsorship", "no sponsorship", "without sponsorship",
    "not provide sponsorship", "not offer sponsorship", "not be able to sponsor",
    "sponsorship is not available", "unable to provide sponsorship",
    "not able to provide visa", "without the need for sponsorship",
    "without requiring sponsorship", "do not provide visa sponsorship",
    "authorized to work in the united states without",
    "authorized to work in the us without", "not require sponsorship now or in the future",
)
# Citizenship / clearance hard requirements (block when candidate is non-citizen).
_CITIZEN_CLEARANCE = (
    "must be a u.s. citizen", "must be a us citizen", "u.s. citizenship is required",
    "us citizenship is required", "u.s. citizenship required", "requires u.s. citizenship",
    "us citizens only", "u.s. citizens only", "citizenship required",
    "active security clearance", "security clearance is required", "security clearance required",
    "secret clearance", "top secret", "ts/sci", "able to obtain a security clearance",
    "ability to obtain a security clearance", "must be able to obtain a security clearance",
    "active clearance", "government security clearance",
)


# Hosts that are aggregators / listing pages, not real application forms.
_AGGREGATOR_HOSTS = (
    "news.ycombinator.com", "ycombinator.com", "remoteok.com", "remoteok.io",
    "weworkremotely.com", "indeed.com", "glassdoor.com", "linkedin.com",
    "ziprecruiter.com", "dice.com", "monster.com", "builtin.com",
)


def looks_like_non_form_url(url: str) -> str | None:
    """Return a reason string if the URL is an aggregator / board landing page
    (no single-job application form), else None."""
    from urllib.parse import urlparse
    u = (url or "").lower()
    host = urlparse(u).netloc
    path = urlparse(u).path.strip("/")
    segs = [s for s in path.split("/") if s]
    if any(h in host for h in _AGGREGATOR_HOSTS):
        return f"Aggregator/listing host ({host}); not a direct application form"
    # ATS company-root boards with no specific job id → a list of postings, not a form.
    if "ashbyhq.com" in host and len(segs) < 2:
        return "Ashby company board (no specific job) — not an application form"
    if "lever.co" in host and len(segs) < 2:
        return "Lever company board (no specific job) — not an application form"
    if "greenhouse.io" in host and ("embed" not in u) and len(segs) < 2:
        return "Greenhouse company board (no specific job) — not an application form"
    return None


# Page-text markers that a posting is closed / not accepting applications.
_CLOSED_MARKERS = (
    "no longer accepting applications", "position has been filled", "this job is closed",
    "posting is no longer available", "no longer available", "position is closed",
    "applications are closed", "this role is no longer", "job posting not found",
    "posting not found", "page not found", "404", "this position is no longer open",
    "page you are looking for doesn't exist", "page you are looking for does not exist",
    "job requisition is not available", "requisition is no longer available",
    "we couldn't find that page", "this job is no longer posted",
)


def page_closed_reason(body_text: str) -> str | None:
    """Return a reason if the page text indicates a closed/missing posting."""
    t = (body_text or "").lower()[:3000]
    hit = next((m for m in _CLOSED_MARKERS if m in t), None)
    return f"Posting appears closed/unavailable (\"{hit}\")" if hit else None


# Markers that the candidate has already applied to this posting.
_DUPLICATE_MARKERS = (
    "you have already applied", "already applied to this", "already submitted an application",
    "application already submitted", "duplicate application", "you've already applied",
    "we already have an application", "you already applied",
    # Workday-flavoured
    "already applied for this", "already applied to this job", "have an active application",
    "you have an application in progress for this",
)


def already_applied_reason(body_text: str) -> str | None:
    """Return a reason if the page indicates a prior application to this posting."""
    t = (body_text or "").lower()[:4000]
    hit = next((m for m in _DUPLICATE_MARKERS if m in t), None)
    return f"Already applied (\"{hit}\")" if hit else None


# ATS platforms that do not send an applicant confirmation email — verification is
# by on-screen success only (don't wait for / expect an inbox confirmation).
NO_EMAIL_ATS = {"ashby", "rippling", "smartrecruiters"}


def _profile_locations(profile: dict) -> list[str]:
    return [str(l).lower() for l in profile.get("target", {}).get("locations", [])]


def screen_eligibility(job: dict, profile: dict) -> dict:
    """Screen a job for hard blockers + soft warnings against the candidate profile."""
    text = f"{job.get('title','')} \n {job.get('description','')}".lower()
    wa = profile.get("work_authorization", {})
    target = profile.get("target", {})
    blockers: list[str] = []
    warnings: list[str] = []

    needs_sponsorship = bool(wa.get("requires_sponsorship"))
    is_citizen = not needs_sponsorship  # candidate on a visa is not a citizen/GC holder

    # ── Hard blocker: employer doesn't sponsor & candidate needs it ──────────────
    if needs_sponsorship:
        hit = next((p for p in _NO_SPONSOR if p in text), None)
        if hit:
            blockers.append(f"Employer does not sponsor visas (\"{hit}\") — you require sponsorship")

    # ── Hard blocker: citizenship / clearance required & candidate is non-citizen ─
    if not is_citizen:
        hit = next((p for p in _CITIZEN_CLEARANCE if p in text), None)
        if hit:
            blockers.append(f"Role requires US citizenship / security clearance (\"{hit}\")")

    # ── Warning: location / relocation mismatch ─────────────────────────────────
    loc = (job.get("location", "") or "").lower()
    if loc:
        prof_locs = _profile_locations(profile)
        is_remote = any(k in loc for k in ("remote", "anywhere", "distributed"))
        matches_target = is_remote or any(
            city.split(",")[0].strip() in loc for city in prof_locs if city and "remote" not in city)
        if not matches_target and not target.get("relocation", False):
            warnings.append(f"On-site/location '{job.get('location')}' not in your locations "
                            f"and relocation=False")

    # ── Warning: comp below floor (best-effort parse of $NNN(k|,000) ranges) ─────
    floor = int(target.get("salary_min_usd", 0) or 0)
    if floor:
        nums = []
        for m in re.finditer(r"\$\s?(\d{2,3})\s?[kK]\b", text):
            nums.append(int(m.group(1)) * 1000)
        for m in re.finditer(r"\$\s?(\d{3}),(\d{3})", text):
            nums.append(int(m.group(1) + m.group(2)))
        if nums and max(nums) < floor:
            warnings.append(f"Posted comp max ~${max(nums):,} is below your floor ${floor:,}")

    return {"eligible": len(blockers) == 0, "blockers": blockers, "warnings": warnings}
