"""
Source discovery — grows the CONTRACT (C2C/C2H) source universe.

Given candidate ATS board slugs (scraped from job-posting URLs found via web search), probes each
one DIRECTLY against the Greenhouse / Lever / Ashby / SmartRecruiters public board APIs and keeps
only LIVE boards that expose >=1 US-based CONTRACT role in Rahul's lane (backend / data-platform /
cloud / SRE / Python). Verified slugs are appended to board_sponsors.json — the same rotation list
board_scanner.py already scans — so contract-friendly employers join the ongoing scan without any
separate pipeline. Idempotent: a slug already in board_sponsors.json is skipped without a network
call. Every attempt (hit or miss) is recorded in discovered_sources.json as an audit trail, so a
rejected slug isn't silently re-probed forever either.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx

from core.logger import get_logger
from agents.discovery.apply_url_resolver import loc_is_us_ok

logger = get_logger("source_discovery")

_ROOT = Path(__file__).parent.parent.parent
_BOARD_SPONSORS = _ROOT / "data" / "board_sponsors.json"
_DISCOVERED = _ROOT / "data" / "discovered_sources.json"

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
LEVER_API = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
SMARTRECRUITERS_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"

_TIMEOUT = 8.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; source-discovery/1.0)"}

# Same lane as board_scanner._TITLE_INCLUDE, trimmed to the candidate's core contract-search lane
# (backend / data-platform / cloud / SRE / Python) per the discovery brief.
_LANE_INCLUDE = (
    "backend", "back end", "back-end", "data engineer", "data platform", "data infrastructure",
    "platform engineer", "cloud engineer", "site reliability", "sre", "devops",
    "python", "software engineer", "software developer", "infrastructure engineer",
    "distributed systems", "full stack", "fullstack",
)
_LANE_EXCLUDE = (
    "intern", "manager", "director", " vp ", "vp,", "head of",
    "marketing", "recruit", "designer", "ios", "android", "mobile",
    "account executive", "sales development", "sales representative", "sdr ", "bdr ",
    "qa ", "test engineer", "security engineer", "hardware", "firmware", "embedded",
)
_CONTRACT_HINTS = (
    "contract", "contractor", "c2c", "corp-to-corp", "corp to corp", "1099",
    "w2 contract", "contract-to-hire", "contract to hire", "temp-to-hire",
)
# A posting that explicitly rules out C2C/third-party still says "contract" in some ATS templates
# (e.g. "This is a full-time, W2 position — no C2C/third party") — don't let that count as a hit.
_EMPLOYEE_ONLY_HINTS = (
    "no c2c", "no corp-to-corp", "no corp to corp", "no third party", "no third-party",
    "not open to c2c", "w2 only",
)


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


def _title_ok(title: str) -> bool:
    t = (title or "").lower()
    if any(x in t for x in _LANE_EXCLUDE):
        return False
    return any(x in t for x in _LANE_INCLUDE)


def _is_contract(title: str, description: str) -> bool:
    blob = f"{title or ''} {description or ''}".lower()
    if any(x in blob for x in _EMPLOYEE_ONLY_HINTS):
        return False
    return any(x in blob for x in _CONTRACT_HINTS)


async def _probe_greenhouse(client: httpx.AsyncClient, slug: str):
    r = await client.get(GREENHOUSE_API.format(slug=slug), timeout=_TIMEOUT)
    if r.status_code != 200:
        return None
    try:
        jobs = (r.json() or {}).get("jobs", [])
    except Exception:
        return None
    out = []
    for j in jobs:
        out.append({
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "description": _strip_html(j.get("content", "")),
        })
    return out


async def _probe_lever(client: httpx.AsyncClient, slug: str):
    r = await client.get(LEVER_API.format(slug=slug), timeout=_TIMEOUT)
    if r.status_code != 200:
        return None
    try:
        jobs = r.json()
    except Exception:
        return None
    if not isinstance(jobs, list):
        return None
    out = []
    for j in jobs:
        cats = j.get("categories") or {}
        out.append({
            "title": j.get("text", ""),
            "location": cats.get("location", "") or "",
            "description": j.get("descriptionPlain") or _strip_html(j.get("description", "")),
        })
    return out


async def _probe_ashby(client: httpx.AsyncClient, slug: str):
    r = await client.get(ASHBY_API.format(slug=slug), timeout=_TIMEOUT)
    if r.status_code != 200:
        return None
    try:
        jobs = (r.json() or {}).get("jobs", [])
    except Exception:
        return None
    out = []
    for j in jobs:
        secondary = " ".join(s.get("location", "") for s in (j.get("secondaryLocations") or []))
        loc = (j.get("location") or "") + (" " + secondary if secondary else "")
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = (loc + " Remote").strip()
        out.append({
            "title": j.get("title", ""),
            "location": loc.strip(),
            "description": j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", "")),
        })
    return out


async def _probe_smartrecruiters(client: httpx.AsyncClient, slug: str):
    r = await client.get(SMARTRECRUITERS_API.format(slug=slug), timeout=_TIMEOUT)
    if r.status_code != 200:
        return None
    try:
        postings = (r.json() or {}).get("content", [])
    except Exception:
        return None
    out = []
    for p in postings:
        loc = p.get("location") or {}
        loc_str = " ".join(str(x) for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
        if loc.get("remote") and "remote" not in loc_str.lower():
            loc_str = (loc_str + " Remote").strip()
        out.append({"title": p.get("name", ""), "location": loc_str, "description": ""})
    return out


_PROBES = (
    ("greenhouse", _probe_greenhouse),
    ("lever", _probe_lever),
    ("ashby", _probe_ashby),
    ("smartrecruiters", _probe_smartrecruiters),
)


async def _probe_slug(client: httpx.AsyncClient, slug: str):
    """Try each ATS in turn against the exact slug given; the first that answers with a live
    (non-None) board wins. Returns (ats_name, postings) or (None, None) if none answered."""
    for ats, fn in _PROBES:
        try:
            postings = await fn(client, slug)
        except Exception:
            postings = None
        if postings is not None:
            return ats, postings
    return None, None


def _known_slugs() -> set:
    try:
        data = json.loads(_BOARD_SPONSORS.read_text())
    except Exception:
        return set()
    return {str(s).strip().lower() for s in data.get("companies", []) if s}


def _load_discovered() -> dict:
    try:
        data = json.loads(_DISCOVERED.read_text())
        if isinstance(data, dict) and isinstance(data.get("attempts"), dict):
            return data
    except Exception:
        pass
    return {"_comment": "audit trail of source_discovery probe attempts, keyed by slug", "attempts": {}}


def _save_discovered(data: dict) -> None:
    _DISCOVERED.write_text(json.dumps(data, indent=2, sort_keys=True))


def _append_board_sponsor(slug: str) -> None:
    try:
        data = json.loads(_BOARD_SPONSORS.read_text())
    except Exception:
        data = {"companies": []}
    companies = data.get("companies", [])
    if slug.strip().lower() not in {str(c).strip().lower() for c in companies}:
        companies.append(slug)
        data["companies"] = companies
        _BOARD_SPONSORS.write_text(json.dumps(data, indent=2))


async def ingest(slugs: list[str], *, require_contract: bool = True) -> dict:
    """Probe candidate ATS board slugs; keep the live ones that carry >=1 US in-lane role
    (also contract-flagged, if require_contract). Appends newly-verified slugs to
    board_sponsors.json. Already-known slugs are skipped without a network call — safe to call
    repeatedly with an overlapping list. Returns a summary dict (see keys below)."""
    seen, uniq = set(), []
    for s in slugs or []:
        k = (s or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(s.strip())

    known = _known_slugs()
    to_probe = [s for s in uniq if s.lower() not in known]
    skipped_known = [s for s in uniq if s.lower() in known]

    discovered_log = _load_discovered()
    attempts = discovered_log["attempts"]

    ingested: list[dict] = []
    rejected: dict[str, str] = {}

    if to_probe:
        sem = asyncio.Semaphore(8)

        async def _probe(s):
            async with sem:
                return s, await _probe_slug(client, s)

        async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
            results = await asyncio.gather(*[_probe(s) for s in to_probe])
    else:
        results = []

    for slug, (ats, postings) in zip(to_probe, results):
        ts = int(time.time())
        if postings is None:
            rejected[slug] = "dead_board"
            attempts[slug.lower()] = {"result": "dead_board", "ts": ts}
            continue

        hits = [p.get("title", "") for p in postings
                if _title_ok(p.get("title", "")) and loc_is_us_ok(p.get("location", ""))
                and (not require_contract or _is_contract(p.get("title", ""), p.get("description", "")))]

        if not hits:
            reason = "no_matching_contract_roles" if require_contract else "no_matching_roles"
            rejected[slug] = reason
            attempts[slug.lower()] = {"result": reason, "ats": ats, "ts": ts, "board_size": len(postings)}
            continue

        _append_board_sponsor(slug)
        ingested.append({"slug": slug, "ats": ats, "matching_roles": len(hits), "sample_roles": hits[:5]})
        attempts[slug.lower()] = {"result": "ingested", "ats": ats, "ts": ts,
                                   "matching_roles": len(hits), "sample_roles": hits[:5]}

    _save_discovered(discovered_log)

    summary = {
        "slugs_in": len(uniq),
        "skipped_known": skipped_known,
        "probed": len(to_probe),
        "ingested": [i["slug"] for i in ingested],
        "ingested_detail": ingested,
        "rejected": rejected,
    }
    logger.log_decision(action="source_discovery_ingest", outcome="success", details={
        "slugs_in": summary["slugs_in"], "skipped_known": len(skipped_known),
        "probed": summary["probed"], "ingested": len(ingested), "rejected": len(rejected),
    })
    return summary
