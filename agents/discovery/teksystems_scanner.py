"""TEKsystems (Phenom) discovery via public sitemaps + JSON-LD — PerimeterX-exempt, no browser.

TEKsystems isn't on a board API the scanner supports (Greenhouse/Lever/Ashby/Workable), and its
Phenom job search sits behind PerimeterX bot protection, so neither the board scanner nor a headless
browser can read it (every automated search returns zero hits). But the PUBLIC job SITEMAPS are
crawler-facing (Google must index them) and therefore PX-exempt — they list every US posting — and
each job page carries a JSON-LD `JobPosting` (also crawler-facing) with title, location,
description, datePosted and employmentType. We parse those over plain HTTP and hand the fitting
roles to ats_intake, which falls back to the direct job URL (TEKsystems won't resolve to a board).

NOTE: TEKsystems is a STAFFING agency (Allegis Group) — its postings are CONTRACT / contract-to-hire
placements at client companies, not direct-sponsor employer roles. Sourced separately from the
sponsor boards so it's easy to weight/disable. Sponsorship on contract roles works differently.
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

logger = get_logger("teksystems_scanner")

_SITEMAP_INDEX = "https://careers.teksystems.com/us/en/sitemap_index.xml"
# A crawler UA: the sitemaps/JSON-LD are served to bots (PX lets these through) but the JS search
# behind PX is not — so we ONLY ever touch the crawler-facing surface, never the search API.
_CRAWLER_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

# Slug prefilter — only fetch detail pages for plausibly-fitting roles (the sitemap holds ~1700 US
# jobs; this cuts it to a few dozen so we don't fetch hundreds of pages). The authoritative fit
# check is _title_ok on the real title after we parse the page.
_SLUG_KW = ("data-engineer", "data-scientist", "machine-learning", "ml-engineer", "ml-ops",
            "mlops", "software-engineer", "software-developer", "backend", "back-end", "platform",
            "ai-engineer", "ai-", "analytics-engineer", "data-platform", "python", "cloud-engineer",
            "devops", "site-reliability", "full-stack", "fullstack")


async def _job_urls(client: httpx.AsyncClient) -> list[str]:
    """Every US job URL from the sitemap index (…/job/<id>/<Title-Slug>)."""
    urls: list[str] = []
    try:
        idx = (await client.get(_SITEMAP_INDEX)).text
    except Exception as e:
        logger.warning("teksystems_sitemap_index_failed", error=str(e)[:100])
        return urls
    children = re.findall(r"<loc>([^<]+)</loc>", idx)
    for ch in children:
        if "sitemap" not in ch.lower():
            continue
        try:
            t = (await client.get(ch)).text
        except Exception:
            continue
        urls += [u for u in re.findall(r"<loc>([^<]+)</loc>", t) if "/job/" in u]
    return urls


def _parse_job_ld(html: str) -> dict | None:
    """Pull the JSON-LD JobPosting (crawler-facing, PX-exempt) → {title, location, description,
    posted_at, employment_type, org}. Returns None if absent."""
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
                "org": (it.get("hiringOrganization") or {}).get("name") or "TEKsystems",
            }
    return None


async def run(state, *, limit: int = 10, tailor: bool = True, persist: bool = False,
              max_details: int = 60):
    """Scan TEKsystems' sitemap, intake the fitting US roles. Returns (state, report).

    limit:       cap on expensive Opus tailorings (parked apps) this run.
    max_details: cap on job detail-page fetches per run (bounds load; slug prefilter picks them).
    persist:     merge new jobs/apps into live state under lock (caller must not also save snapshot).
    """
    known = {j.get("apply_url") for j in state.get("job_listings", [])}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                 headers={"User-Agent": _CRAWLER_UA}) as client:
        all_urls = await _job_urls(client)
        # Slug prefilter + skip URLs we already have queued, then bound the count.
        cand = [u for u in all_urls
                if any(k in u.lower() for k in _SLUG_KW) and u not in known][:max_details]
        logger.info("teksystems_sitemap_scanned", total=len(all_urls), candidates=len(cand))
        if not cand:
            return state, {"scanned": len(all_urls), "candidates": 0, "discovered": 0, "queued": 0}

        sem = asyncio.Semaphore(8)

        async def _one(url):
            async with sem:
                try:
                    r = await client.get(url)
                    if r.status_code != 200:
                        return None
                    info = _parse_job_ld(r.text)
                except Exception:
                    return None
            if not info or not info.get("title"):
                return None
            if not _title_ok(info["title"]) or not loc_is_us_ok(info.get("location", "")):
                return None
            return {
                "company": "TEKsystems",
                "title": info["title"],
                "description": info["description"],
                "location": info["location"],
                "posted_at": info["posted_at"],
                "source": "teksystems:phenom",
                "fallback_url": url,
            }

        discovered = [d for d in await asyncio.gather(*[_one(u) for u in cand]) if d]

    logger.info("teksystems_discovered", candidates=len(cand), fitting=len(discovered))
    if not discovered:
        return state, {"scanned": len(all_urls), "candidates": len(cand), "discovered": 0, "queued": 0}

    pre_app_ids = {a.get("id") for a in state.get("applications", [])}
    pre_job_ids = {j.get("id") for j in state.get("job_listings", [])}
    updated, report = await intake_jobs(discovered, state, limit=limit, tailor=tailor)
    report["scanned"] = len(all_urls)
    report["candidates"] = len(cand)
    report["discovered"] = len(discovered)

    if persist:
        from core.state_store import merge_new
        new_jobs = [j for j in updated.get("job_listings", []) if j.get("id") not in pre_job_ids]
        new_apps = [a for a in updated.get("applications", []) if a.get("id") not in pre_app_ids]
        report["persisted"] = merge_new(new_jobs, new_apps)

    logger.log_decision(action="teksystems_scan_complete", outcome="success", details=report)
    return updated, report
