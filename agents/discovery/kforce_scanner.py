"""Kforce discovery via its public Azure Cognitive Search API — plain HTTP, no browser.

Kforce's job search (kforce.com/find-work) is a SPA backed by an Azure Search index whose query is
issued client-side with a PUBLIC query key (read-only, embedded in the page). We call that same
endpoint directly — it returns clean JSON with Title, City/State, PostDate, TypeCode, ApplyUrl and a
VisaSponsorshipJob flag — then hand the fitting US roles to ats_intake (which falls back to the
direct ApplyUrl since Kforce won't resolve to a board).

NOTE: Kforce is a STAFFING agency — roles are CONTRACT / Direct-Hire placements at client companies,
sourced separately from the sponsor boards. Cap-exempt in ats_intake (every posting is a different
client). The query key is a read-only Azure SEARCH key (not a secret); if it rotates, re-capture it
from the find-work page's network call.
"""
from __future__ import annotations

import asyncio

import httpx

from core.logger import get_logger
from agents.discovery.apply_url_resolver import loc_is_us_ok
from agents.discovery.board_scanner import _title_ok
from agents.application.ats_intake import intake_jobs

logger = get_logger("kforce_scanner")

_API = "https://kforcewebeast.search.windows.net/indexes/kforcewebjobentity/docs/search?api-version=2016-09-01"
_API_KEY = "1603E4DC4C87A8E41D6BBDE4EEA4EFB7"   # public read-only Azure Search query key (from find-work)
_SELECT = "Title, Id, PostDate, Responsibilities, Skills, City, State, Zip, TypeCode, VisaSponsorshipJob, ApplyUrl"

# Fit keywords — query each and union the results (the index has thousands of roles; this focuses on
# Rahul's lane). The authoritative title check is _title_ok after we pull each result.
_KEYWORDS = ("data engineer", "machine learning", "software engineer", "platform engineer",
             "backend engineer", "cloud engineer", "devops", "data scientist", "ai engineer",
             "python", "site reliability")


async def _search(client: httpx.AsyncClient, keyword: str, top: int = 30) -> list[dict]:
    body = {
        "count": True, "select": _SELECT, "search": keyword,
        "searchFields": "Title, Responsibilities, Skills", "queryType": "simple",
        "searchMode": "any", "skip": 0, "top": top,
    }
    try:
        r = await client.post(_API, json=body)
        if r.status_code != 200:
            return []
        return r.json().get("value", []) or []
    except Exception:
        return []


def _to_discovered(d: dict) -> dict | None:
    title = (d.get("Title") or "").strip()
    if not title:
        return None
    city, state = (d.get("City") or "").strip(), (d.get("State") or "").strip()
    location = ", ".join(p for p in (city, state) if p) or "United States"   # Kforce is US-only
    desc = " ".join(x for x in (d.get("Responsibilities") or "", d.get("Skills") or "") if x).strip()
    url = (d.get("ApplyUrl") or "").strip()
    if not url:
        return None
    return {
        "company": "Kforce",
        "title": title,
        "description": desc,
        "location": location,
        "posted_at": (d.get("PostDate") or "")[:10],
        "source": "kforce:azuresearch",
        "fallback_url": url,
    }


async def run(state, *, limit: int = 10, tailor: bool = True, persist: bool = False, top: int = 30, fit_gate: bool = True):
    """Query Kforce's Azure Search index across fit keywords, intake the fitting US roles.
    Returns (state, report). tailor=False parks cheap leads (Opus tailoring stays on-demand)."""
    known = {j.get("apply_url") for j in state.get("job_listings", [])}
    async with httpx.AsyncClient(timeout=15, headers={"api-key": _API_KEY,
                                                       "Content-Type": "application/json"}) as client:
        batches = await asyncio.gather(*[_search(client, kw, top) for kw in _KEYWORDS])

    by_id: dict[str, dict] = {}
    for batch in batches:
        for d in batch:
            jid = str(d.get("Id") or "")
            if jid and jid not in by_id:
                by_id[jid] = d
    logger.info("kforce_searched", keywords=len(_KEYWORDS), unique_results=len(by_id))

    discovered = []
    for d in by_id.values():
        item = _to_discovered(d)
        if not item or item["fallback_url"] in known:
            continue
        if not _title_ok(item["title"]) or not loc_is_us_ok(item["location"]):
            continue
        discovered.append(item)

    logger.info("kforce_discovered", fitting=len(discovered))
    if not discovered:
        return state, {"searched": len(by_id), "discovered": 0, "queued": 0}

    pre_app_ids = {a.get("id") for a in state.get("applications", [])}
    pre_job_ids = {j.get("id") for j in state.get("job_listings", [])}
    updated, report = await intake_jobs(discovered, state, limit=limit, tailor=tailor, fit_gate=fit_gate)
    report["searched"] = len(by_id)
    report["discovered"] = len(discovered)

    if persist:
        from core.state_store import merge_new
        new_jobs = [j for j in updated.get("job_listings", []) if j.get("id") not in pre_job_ids]
        new_apps = [a for a in updated.get("applications", []) if a.get("id") not in pre_app_ids]
        report["persisted"] = merge_new(new_jobs, new_apps)

    logger.log_decision(action="kforce_scan_complete", outcome="success", details=report)
    return updated, report
