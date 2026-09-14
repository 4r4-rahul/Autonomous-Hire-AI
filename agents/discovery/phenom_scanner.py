"""Generalized Phenom-platform staffing discovery via public sitemaps + JSON-LD — no browser.

Phenom-hosted career sites (careers.<brand>.com) expose crawler-facing sitemaps that list every US
posting as …/us/en/job/<id>/<Title-Slug>, and each job page carries a JSON-LD `JobPosting`
(title/location/description/datePosted/employmentType). Both surfaces are served to bots (so they're
PerimeterX-exempt where PX guards the JS search), and we ONLY ever touch them over plain HTTP.

This generalizes the original TEKsystems scanner to ANY Phenom host — add a verified entry to
`PHENOM_HOSTS` (probe: `careers.<brand>.com/sitemap_index.xml` must list `/us/en/job/` URLs whose
pages contain a JSON-LD JobPosting). Currently the Allegis Group staffing brands, all H1B sponsors.

NOTE: these are STAFFING agencies — postings are CONTRACT / contract-to-hire placements at client
companies, not direct-sponsor employer roles. Sourced separately from the sponsor boards so they're
easy to weight/disable; the employment classifier + W2 gates downstream handle contract-vs-W2.
"""
from __future__ import annotations

import asyncio
import json
import re

import httpx

from core.logger import get_logger
from agents.discovery.apply_url_resolver import loc_is_us_ok
from agents.discovery.board_scanner import _title_ok
from agents.application.ats_intake import intake_jobs

logger = get_logger("phenom_scanner")

# Verified Phenom staffing hosts. (company, sitemap_index_url, source_slug). Extend after probing a
# new brand: careers.<brand>.com/sitemap_index.xml → child sitemaps → /us/en/job/<id> pages w/ JSON-LD.
PHENOM_HOSTS: list[tuple[str, str, str]] = [
    ("TEKsystems",   "https://careers.teksystems.com/us/en/sitemap_index.xml",       "teksystems"),
    ("Aston Carter", "https://careers.astoncarter.com/sitemap_index.xml",            "astoncarter"),
    ("Actalent",     "https://careers.actalentservices.com/sitemap_index.xml",       "actalent"),
]

_CRAWLER_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

# Slug prefilter — only fetch detail pages for plausibly-fitting roles (a brand's sitemap holds
# hundreds-to-thousands of jobs; this cuts it to a few dozen). Authoritative fit = _title_ok on the
# parsed title.
_SLUG_KW = ("data-engineer", "data-scientist", "machine-learning", "ml-engineer", "ml-ops",
            "mlops", "software-engineer", "software-developer", "backend", "back-end", "platform",
            "ai-engineer", "ai-", "analytics-engineer", "data-platform", "python", "cloud-engineer",
            "devops", "site-reliability", "full-stack", "fullstack")


async def _job_urls(client: httpx.AsyncClient, sitemap_index: str) -> list[str]:
    """Every job URL (…/job/<id>/<slug>) reachable from a Phenom sitemap index."""
    urls: list[str] = []
    try:
        idx = (await client.get(sitemap_index)).text
    except Exception as e:
        logger.warning("phenom_sitemap_index_failed", index=sitemap_index, error=str(e)[:100])
        return urls
    # Job URLs may be listed directly in the index or (usually) in child sitemaps.
    urls += [u for u in re.findall(r"<loc>([^<]+)</loc>", idx) if "/job/" in u]
    children = [u for u in re.findall(r"<loc>([^<]+)</loc>", idx) if "sitemap" in u.lower()]
    for ch in children:
        try:
            t = (await client.get(ch)).text
        except Exception:
            continue
        urls += [u for u in re.findall(r"<loc>([^<]+)</loc>", t) if "/job/" in u]
    return urls


def _parse_job_ld(html: str, default_org: str) -> dict | None:
    """Pull the JSON-LD JobPosting → {title, location, description, posted_at, employment_type, org}.
    Returns None if absent."""
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            o = json.loads(block)
        except Exception:
            continue
        items = o if isinstance(o, list) else [o]
        for it in items:
            if not isinstance(it, dict) or it.get("@type") != "JobPosting":
                continue
            loc = it.get("jobLocation") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            addr = (loc.get("address") if isinstance(loc, dict) else {}) or {}
            parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
            location = ", ".join(p for p in parts if p)
            if not location and (it.get("jobLocationType") or "").upper() == "TELECOMMUTE":
                location = "Remote, USA"
            etype = it.get("employmentType")
            if isinstance(etype, list):
                etype = ", ".join(etype)
            return {
                "title": (it.get("title") or "").strip(),
                "location": location,
                "description": re.sub(r"<[^>]+>", " ", it.get("description") or "").strip(),
                "posted_at": it.get("datePosted") or "",
                "employment_type": etype or "",
                "org": (it.get("hiringOrganization") or {}).get("name") or default_org,
            }
    return None


async def _scan_host(client: httpx.AsyncClient, company: str, sitemap_index: str, slug: str,
                     known: set, max_details: int) -> list[dict]:
    """Scan ONE Phenom host → fitting US job dicts (no intake here — caller batches intake)."""
    all_urls = await _job_urls(client, sitemap_index)
    cand = [u for u in all_urls
            if any(k in u.lower() for k in _SLUG_KW) and u not in known][:max_details]
    logger.info("phenom_host_scanned", company=company, total=len(all_urls), candidates=len(cand))
    if not cand:
        return []
    sem = asyncio.Semaphore(8)

    async def _one(url):
        async with sem:
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    return None
                info = _parse_job_ld(r.text, company)
            except Exception:
                return None
        if not info or not info.get("title"):
            return None
        if not _title_ok(info["title"]) or not loc_is_us_ok(info.get("location", "")):
            return None
        return {
            "company": company,
            "title": info["title"],
            "description": info["description"],
            "location": info["location"],
            "posted_at": info["posted_at"],
            "source": f"phenom:{slug}",
            "fallback_url": url,
        }

    return [d for d in await asyncio.gather(*[_one(u) for u in cand]) if d]


async def run(state, *, limit: int = 10, tailor: bool = False, persist: bool = False,
              max_details: int = 60, fit_gate: bool = True):
    """Scan every configured Phenom host, intake the fitting US roles. Returns (state, report).

    limit:       cap on expensive Opus tailorings (parked apps) this run (tailor=False keeps it cheap).
    max_details: per-HOST cap on job detail-page fetches (bounds load; slug prefilter picks them).
    persist:     merge new jobs/apps into live state under lock (caller must not also save a snapshot).
    """
    known = {j.get("apply_url") for j in state.get("job_listings", [])}
    discovered: list[dict] = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                 headers={"User-Agent": _CRAWLER_UA}) as client:
        for company, sitemap_index, slug in PHENOM_HOSTS:
            try:
                discovered += await _scan_host(client, company, sitemap_index, slug, known, max_details)
            except Exception as e:
                logger.warning("phenom_host_failed", company=company, error=str(e)[:120])

    logger.info("phenom_discovered", hosts=len(PHENOM_HOSTS), fitting=len(discovered))
    if not discovered:
        return state, {"hosts": len(PHENOM_HOSTS), "discovered": 0, "queued": 0}

    pre_app_ids = {a.get("id") for a in state.get("applications", [])}
    pre_job_ids = {j.get("id") for j in state.get("job_listings", [])}
    updated, report = await intake_jobs(discovered, state, limit=limit, tailor=tailor, fit_gate=fit_gate)
    report["hosts"] = len(PHENOM_HOSTS)
    report["discovered"] = len(discovered)

    if persist:
        from core.state_store import merge_new
        new_jobs = [j for j in updated.get("job_listings", []) if j.get("id") not in pre_job_ids]
        new_apps = [a for a in updated.get("applications", []) if a.get("id") not in pre_app_ids]
        report["persisted"] = merge_new(new_jobs, new_apps)

    logger.log_decision(action="phenom_scan_complete", outcome="success", details=report)
    return updated, report
