"""Generalized Workday discovery via the public `cxs` JSON API — no browser, no key.

Every Workday-hosted careers site ({tenant}.wd{N}.myworkdayjobs.com/{site}) is backed by a public
JSON search endpoint: POST /wday/cxs/{tenant}/{site}/jobs  {"appliedFacets":{}, "limit","offset",
"searchText"} → {total, jobPostings:[{title, locationsText, externalPath, postedOn}]}. The per-job
detail is GET /wday/cxs/{tenant}/{site}{externalPath} → jobPostingInfo.jobDescription. This unlocks
the big H1B-sponsoring IT-services firms (NTT Data, Genpact, Accenture…) that aren't on any board API.

US filtering is the catch: Workday has no single "United States" country facet — locations are
per-city. Where the `locations` facet exposes US-city ids we apply them server-side (clean, e.g. NTT);
everywhere we ALSO filter client-side by locationsText (belt-and-suspenders). Bounded pagination +
a small role-keyword set keep it cheap and stop us drowning in these firms' huge global pools.

Add a tenant: find its careers URL → {tenant}.wd{N}.myworkdayjobs.com/{site} → add the tuple below.

NOTE: IT-services/consulting placements — often contract/onsite-at-client and India-heavy, so US
software yield is modest and fit varies. Sourced separately from the sponsor boards; the employment
classifier + fit gates downstream weight them. Easy to disable by emptying WORKDAY_TENANTS.
"""
from __future__ import annotations

import asyncio

import httpx

from core.logger import get_logger
from agents.discovery.apply_url_resolver import loc_is_us_ok
from agents.discovery.board_scanner import _title_ok
from agents.application.ats_intake import intake_jobs

logger = get_logger("workday_scanner")

# (company, tenant, wd, site) — VERIFIED cxs endpoints (careers page → myworkdayjobs URL).
WORKDAY_TENANTS: list[tuple[str, str, str, str]] = [
    ("NTT Data",          "nttlimited", "wd3",   "NTT_Careers"),
    ("Genpact",           "genpact",    "wd108", "External_Careers"),
    ("Accenture",         "accenture",  "wd103", "AccentureCareers"),
    ("Synechron",         "synechron",  "wd1",   "SynechronCareers"),   # fintech IT consultancy, AI/SWE roles
    ("Fractal Analytics", "fractal",    "wd1",   "Careers"),            # AI/analytics firm
]

# Narrow the firms' huge global pools to plausibly-fitting roles (server-side searchText).
_SEARCH_TERMS = ["software engineer", "data engineer", "machine learning", "backend",
                 "platform engineer", "python", "site reliability", "ai engineer"]
_PAGES_PER_TERM = 2          # bounded pagination per (tenant, term); cxs page = 20
_PAGE = 20


def _us_location_ids(facets: list) -> list[str]:
    """US-city facet value ids from the `locations` facet (when the tenant exposes them flat)."""
    ids: list[str] = []
    for f in facets or []:
        groups = f.get("values", []) if (f.get("facetParameter") == "locations"
                 or any(v.get("facetParameter") == "locations" for v in f.get("values", []))) else []
        for g in groups:
            for v in (g.get("values") or [g]):
                if "United States" in (v.get("descriptor") or "") and v.get("id"):
                    ids.append(v["id"])
    return list(dict.fromkeys(ids))


async def _scan_tenant(client, company, tenant, wd, site, known) -> list[dict]:
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    public = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
    # Discover US-location facet ids once (clean server-side filter where available, e.g. NTT).
    applied: dict = {}
    try:
        seed = (await client.post(base, json={"appliedFacets": {}, "limit": 1, "offset": 0,
                                              "searchText": "engineer"}, timeout=15)).json()
        us_ids = _us_location_ids(seed.get("facets", []))
        if us_ids:
            applied = {"locations": us_ids}
    except Exception as e:
        logger.warning("workday_facet_probe_failed", company=company, error=str(e)[:100])

    seen_paths: set[str] = set()
    out: list[dict] = []
    for term in _SEARCH_TERMS:
        for page in range(_PAGES_PER_TERM):
            try:
                r = await client.post(base, json={"appliedFacets": applied, "limit": _PAGE,
                                                  "offset": page * _PAGE, "searchText": term}, timeout=15)
                if r.status_code != 200:
                    break
                posts = r.json().get("jobPostings", [])
            except Exception:
                break
            if not posts:
                break
            for p in posts:
                path = p.get("externalPath") or ""
                title = (p.get("title") or "").strip()
                loc = p.get("locationsText") or ""
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                url = public + path
                if url in known or not title:
                    continue
                if not loc_is_us_ok(loc) or not _title_ok(title):
                    continue
                out.append({"company": company, "title": title, "location": loc,
                            "posted_at": p.get("postedOn") or "", "source": f"workday:{tenant}",
                            "fallback_url": url, "_detail": base.rsplit("/jobs", 1)[0] + path})
    # Fetch descriptions for the US candidates (bounded — only the ones that passed the gates).
    sem = asyncio.Semaphore(6)

    async def _desc(job):
        async with sem:
            try:
                d = (await client.get(job.pop("_detail"), timeout=15)).json()
                info = d.get("jobPostingInfo", {}) or {}
                import re
                job["description"] = re.sub(r"<[^>]+>", " ", info.get("jobDescription", "") or "").strip()
            except Exception:
                job["description"] = job.get("description", "")
        return job

    return await asyncio.gather(*[_desc(j) for j in out]) if out else []


async def run(state, *, limit: int = 10, tailor: bool = False, persist: bool = False):
    """Scan every configured Workday tenant, intake the fitting US roles. Returns (state, report)."""
    known = {j.get("apply_url") for j in state.get("job_listings", [])}
    discovered: list[dict] = []
    async with httpx.AsyncClient(follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"}) as client:
        for company, tenant, wd, site in WORKDAY_TENANTS:
            try:
                got = await _scan_tenant(client, company, tenant, wd, site, known)
                logger.info("workday_tenant_scanned", company=company, fitting=len(got))
                discovered += got
            except Exception as e:
                logger.warning("workday_tenant_failed", company=company, error=str(e)[:120])

    logger.info("workday_discovered", tenants=len(WORKDAY_TENANTS), fitting=len(discovered))
    if not discovered:
        return state, {"tenants": len(WORKDAY_TENANTS), "discovered": 0, "queued": 0}

    pre_app_ids = {a.get("id") for a in state.get("applications", [])}
    pre_job_ids = {j.get("id") for j in state.get("job_listings", [])}
    updated, report = await intake_jobs(discovered, state, limit=limit, tailor=tailor)
    report["tenants"] = len(WORKDAY_TENANTS)
    report["discovered"] = len(discovered)

    if persist:
        from core.state_store import merge_new
        new_jobs = [j for j in updated.get("job_listings", []) if j.get("id") not in pre_job_ids]
        new_apps = [a for a in updated.get("applications", []) if a.get("id") not in pre_app_ids]
        report["persisted"] = merge_new(new_jobs, new_apps)

    logger.log_decision(action="workday_scan_complete", outcome="success", details=report)
    return updated, report
