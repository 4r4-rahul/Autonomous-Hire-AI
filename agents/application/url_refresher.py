"""Keep queued applications pointing at LIVE application forms.

Stored apply_urls go stale: a role gets filled and its branded job page starts 404ing (or
redirects to a general careers landing), so the user "opens a general URL". Some queued apps are
also orphans whose job_listing is missing entirely. This module re-points every pending_review
app at a currently-open posting:

  • stored URL still 200       → keep it
  • stored URL dead / missing  → re-resolve company+title on the LIVE board API (authoritative:
                                  it only returns open postings, so we trust a hit without re-GET)
  • orphan (no job_listing)    → re-resolve and synthesize a minimal job_listing
  • no live posting found      → leave the app untouched, just count it (never auto-skip)

Used by scripts/refresh_apply_urls.py (CLI) and the scheduler's periodic refresh job.
"""
from __future__ import annotations

import asyncio

import httpx

from core.logger import get_logger
from agents.discovery.apply_url_resolver import resolve_ats_for, _loc_class, _kw

logger = get_logger("url_refresher")


def _same_role(orig_title: str, matched_title: str) -> bool:
    """True if the US match is the SAME role as the foreign one (just a different-location posting),
    so re-pointing keeps the tailored résumé valid. A weak overlap means it's a DIFFERENT US role —
    don't silently swap (the role is genuinely non-US; drop it instead)."""
    ka, kb = _kw(orig_title), _kw(matched_title)
    if not ka:
        return False
    inter = ka & kb
    return len(inter) >= max(2, len(ka) - 2) or len(inter) / len(ka) >= 0.6


async def _alive(client, url: str) -> bool:
    """True only if the stored branded URL still serves the job (HTTP 200). Used ONLY on stored
    URLs — never on board-API results, since some ATS hosts block plain GETs (false negatives)."""
    if not url:
        return False
    try:
        r = await client.get(url, follow_redirects=True, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


async def refresh_pending_urls(state: dict, *, apply: bool = True) -> tuple[dict, dict]:
    """Re-point every pending_review app at a live application form. Mutates `state` when apply.
    Returns (state, report). Never raises on a single bad app."""
    jl = {j["id"]: j for j in state.get("job_listings", [])}
    pending = [a for a in state.get("applications", []) if a.get("status") == "pending_review"]

    sem = asyncio.Semaphore(8)
    report = {"checked": len(pending), "kept_live": 0, "refreshed": 0,
              "orphan_fixed": 0, "unresolved": 0, "us_refixed": 0, "dropped_foreign": 0}

    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        async def _one(app):
            job = jl.get(app.get("job_id"))
            company = (job or {}).get("company_name") or app.get("company_name")
            title = (job or {}).get("title") or app.get("role_title")
            url = (job or {}).get("apply_url")
            if not company or not title:
                report["unresolved"] += 1
                return

            # STRICT US enforcement: a queued role whose location is foreign must be re-pointed
            # to its US variant, or dropped if no US posting exists.
            if _loc_class(((job or {}).get("location") or "") + " " + title) == "foreign":
                async with sem:
                    try:
                        res = await resolve_ats_for(company, title, us_only=True)
                    except Exception:
                        res = None
                if res and res.get("url") and _same_role(title, res.get("matched_title", "")):
                    report["us_refixed"] += 1
                    if apply and job:
                        job["apply_url"] = res["url"]
                        job["location"] = res.get("location", "")
                        if res.get("description"):
                            job["description"] = res["description"]
                else:
                    # No US variant of THIS role (only a different US role matched) → genuinely
                    # non-US. Drop it from the queue per the strict US-only rule.
                    report["dropped_foreign"] += 1
                    if apply:
                        app["status"] = "skipped"
                        app["skip_reason"] = "non-US role — no US-based posting for this role on the ATS board"
                return

            if url:
                async with sem:
                    if await _alive(client, url):
                        report["kept_live"] += 1
                        return

            async with sem:
                try:
                    res = await resolve_ats_for(company, title, us_only=True)   # STRICT US-only
                except Exception:
                    res = None

            if res and res.get("url"):
                if job:
                    report["refreshed"] += 1
                    if apply:
                        job["apply_url"] = res["url"]
                        if res.get("location"):
                            job["location"] = res["location"]
                        if res.get("description"):
                            job["description"] = res["description"]
                else:
                    report["orphan_fixed"] += 1
                    if apply:
                        state.setdefault("job_listings", []).append({
                            "id": app.get("job_id"), "company_id": "",
                            "company_name": company, "title": title,
                            "description": res.get("description") or f"{title} at {company}",
                            "apply_url": res["url"], "remote": True, "location": "Remote",
                            "source": f"refresh:{res.get('ats', '')}",
                            "required_skills": [], "nice_to_have_skills": [],
                            "salary_min": None, "salary_max": None,
                        })
                return

            report["unresolved"] += 1

        await asyncio.gather(*[_one(a) for a in pending])

    logger.log_decision(action="url_refresh_complete", outcome="success", details=report)
    return state, report
