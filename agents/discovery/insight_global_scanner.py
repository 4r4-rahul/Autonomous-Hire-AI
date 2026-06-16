"""Insight Global discovery via its public Jibe careers API — plain HTTP, no browser.

Insight Global has TWO job surfaces:
  • careers.insightglobal.com — CORPORATE / direct-hire roles (FULL_TIME, employed by Insight Global
    itself). Powered by a Jibe careers site with a clean public JSON API (/api/jobs). We use THIS.
  • jobs.insightglobal.com — candidate CONTRACT placements at client companies. That portal is an
    iCIMS SPA with no clean public API/sitemap/JSON-LD and resists HTTP (would need a headless
    browser on a schedule — the fragile class we avoid). NOT scanned.

So this scanner brings in Insight Global's direct-hire engineering/data roles (a better sponsorship
profile than contract), robustly and with zero maintenance. ats_intake falls back to the job URL.
Cap-exempt in ats_intake. If the user later wants the contract placements too, that needs a
browser-rendered iCIMS adapter (deliberately deferred).
"""
from __future__ import annotations

import httpx

from core.logger import get_logger
from agents.discovery.apply_url_resolver import loc_is_us_ok
from agents.discovery.board_scanner import _title_ok
from agents.application.ats_intake import intake_jobs

logger = get_logger("insight_global_scanner")

_API = "https://careers.insightglobal.com/api/jobs"


def _to_discovered(d: dict) -> dict | None:
    title = (d.get("title") or "").strip()
    url = (d.get("apply_url") or "").strip()
    # Keep only Insight Global's own postings — the Jibe site also cross-lists a few other iCIMS
    # tenants (e.g. careers-monument); we only want "Insight Global" here.
    if not title or "insightglobal" not in url.lower():
        return None
    loc = (d.get("full_location") or ", ".join(p for p in (d.get("city"), d.get("state")) if p)
           or d.get("country") or "United States")
    desc = " ".join(x for x in (d.get("responsibilities") or "", d.get("qualifications") or "",
                                d.get("description") or "") if x).strip()
    return {
        "company": "Insight Global",
        "title": title,
        "description": desc,
        "location": loc,
        "posted_at": (d.get("posted_date") or d.get("create_date") or "")[:10],
        "source": "insightglobal:jibe",
        "fallback_url": url,
    }


async def run(state, *, limit: int = 10, tailor: bool = True, persist: bool = False, max_pages: int = 15):
    """Page the Jibe API, intake the fitting US direct-hire roles. Returns (state, report)."""
    known = {j.get("apply_url") for j in state.get("job_listings", [])}
    rows: list[dict] = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}) as client:
        for page in range(1, max_pages + 1):
            try:
                r = await client.get(_API, params={"page": page, "sortBy": "posted_date", "descending": "true"})
                jobs = (r.json() or {}).get("jobs", []) if r.status_code == 200 else []
            except Exception:
                break
            if not jobs:
                break
            rows.extend(j.get("data", {}) for j in jobs)
    logger.info("insightglobal_fetched", rows=len(rows))

    discovered = []
    for d in rows:
        item = _to_discovered(d)
        if not item or item["fallback_url"] in known:
            continue
        if not _title_ok(item["title"]) or not loc_is_us_ok(item["location"]):
            continue
        discovered.append(item)

    logger.info("insightglobal_discovered", fitting=len(discovered))
    if not discovered:
        return state, {"fetched": len(rows), "discovered": 0, "queued": 0}

    pre_app_ids = {a.get("id") for a in state.get("applications", [])}
    pre_job_ids = {j.get("id") for j in state.get("job_listings", [])}
    updated, report = await intake_jobs(discovered, state, limit=limit, tailor=tailor)
    report["fetched"] = len(rows)
    report["discovered"] = len(discovered)

    if persist:
        from core.state_store import merge_new
        new_jobs = [j for j in updated.get("job_listings", []) if j.get("id") not in pre_job_ids]
        new_apps = [a for a in updated.get("applications", []) if a.get("id") not in pre_app_ids]
        report["persisted"] = merge_new(new_jobs, new_apps)

    logger.log_decision(action="insightglobal_scan_complete", outcome="success", details=report)
    return updated, report
