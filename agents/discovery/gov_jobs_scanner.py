"""Direct state & local GOVERNMENT job discovery — every state, via the NEOGOV sitemap.

This reverses an earlier conclusion. NEOGOV (governmentjobs.com) hosts the job boards of most US
states, counties and cities, and its search UI is a Knockout SPA behind reCAPTCHA with no public
API — which is why it was written off as unreachable without a browser.

It turns out the SPA is only the front door. Two server-rendered paths sit wide open:

  * https://www.governmentjobs.com/sitemap.xml  — every live posting, ~42k URLs, slug included
  * https://www.governmentjobs.com/jobs/{id}-0  — the full bulletin as plain HTML, including the
    agency's own hiring contact ("hiring.contact@agency.wa.gov")

So the whole national public-sector board is enumerable with two plain HTTP calls per role and no
browser at all — the same shape as every other scanner here.

IMPORTANT, and the reason this lane is ranked below the vendor C2C lane: these are direct-employer
CIVIL SERVICE roles. They are W2, they frequently carry residency requirements, and they very rarely
sponsor an H-1B. `_citizenship_bar()` drops the ones that say so outright, but silence is not
permission — most agencies simply never mention sponsorship, and will decline it when asked. Treat
this as a real but low-conversion lane for a candidate needing sponsorship.
"""
from __future__ import annotations

import asyncio
import html
import re
from pathlib import Path

import httpx

from core.logger import get_logger
from core.gov_client import detect_gov_client, STATE_ABBR

logger = get_logger("gov_jobs_scanner")

_ROOT = Path(__file__).parent.parent.parent
_SITEMAP = "https://www.governmentjobs.com/sitemap.xml"
_DETAIL = "https://www.governmentjobs.com/jobs/{jid}-0"
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}

# Slug patterns worth reading the bulletin for. Two hard-won rules shape this:
#
#  1. NEVER match a bare "engineer". In the public sector that word overwhelmingly means civil,
#     traffic, water, air-quality or building engineering — a broad match returned 774 candidates of
#     which ~590 were public works ("city-engineer", "engineer-in-training", "engineer-tech").
#     Every pattern here carries an IT qualifier.
#  2. Word-bound the short tokens. A bare `elt` matched "animal-shELTer-attendant"; `etl`/`elt` now
#     require non-letter neighbours.
#
# Scope is data engineering, BI/warehouse, and general software development — Rahul takes software
# developer roles too. Business/systems ANALYST work is excluded: it is a different job, and the
# contract lane's title filter rejects it for the same reason.
_IN_LANE = re.compile(
    r"("
    r"data[-_](engineer|architect|scientist|analyst|warehouse|platform|pipeline)|"
    r"big[-_]data|(?<![a-z])(etl|elt)(?![a-z])|business[-_]intelligence|"
    r"analytics[-_](engineer|developer)|"
    r"software[-_](engineer|developer|architect)|application[s]?[-_](developer|engineer|architect)|"
    r"programmer[-_]?(analyst|ii|iii|iv|v)?(?![a-z])|computer[-_]programmer|"
    r"web[-_]developer|full[-_]?stack|back[-_]?end|front[-_]?end|"
    r"cloud[-_](engineer|architect|developer)|devops|site[-_]reliability|platform[-_]engineer|"
    r"systems[-_]programmer|database[-_](developer|engineer)|sql[-_]developer|"
    r"integration[-_](developer|engineer)|api[-_]developer|(java|python|\.net|dotnet)[-_]developer|"
    r"information[-_](technology|systems)[-_](specialist|analyst|developer|engineer)"
    r")", re.I)
# Public boards carry every job a government does, so the exclusions are broad: teaching, helpdesk,
# non-IC roles, stacks outside the candidate's profile, and analyst work that isn't engineering.
_NOT_LANE = re.compile(
    r"(adjunct|faculty|lecturer|professor|instructor|teacher|librarian|intern\b|"
    r"technician|helpdesk|help-desk|manager|director|supervisor|chief|part-time|student|"
    r"apprentice|trainee|police|fire|nurse|clerk|aide|custodian|driver|dean|animal|shelter|"
    r"veterinar|sharepoint|sitecore|peoplesoft|salesforce|siebel|mainframe|cobol|abap|"
    r"business-analyst|business-systems-analyst|budget|policy-analyst|gis-|maintenance|"
    # "Programmer" in a government title can mean someone who programs EVENTS, not software:
    # "Facility Programmer (Ice Arena)", "Performing Arts Educational Programmer".
    r"facility-programmer|facilities-programmer|arts-|recreation|arena|cultural|event-|"
    r"performing|theatre|theater|museum|park-)", re.I)

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NOT_A_CONTACT = re.compile(r"(neogov|governmentjobs|adobe|\.png|\.jpg|example\.|sentry)", re.I)
# HARD knock-out: the employer will not sponsor. No amount of willingness fixes this.
_CITIZEN_BAR = re.compile(
    r"(must be a (u\.?s\.?|united states) citizen|citizenship (is )?required|"
    r"legally authorized to work.{0,40}without sponsorship|"
    r"(not|unable to) (provide|offer|sponsor).{0,20}sponsorship)", re.I)
# SOFT: a residency requirement is a relocation question, not an eligibility one, and public-sector
# postings usually require residency at APPOINTMENT rather than at application. Rahul is open to
# relocating, so these are flagged and kept — the first version lumped them in with citizenship bars
# and silently discarded roles he can actually take.
_RESIDENCY = re.compile(
    r"(resident of the (state|county|city)|residency (is )?required|must reside (in|within)|"
    r"must (be|become) a resident)", re.I)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
# NEOGOV titles carry the worksite: "Application Developer 3/4 in Juneau, Alaska | Government Jobs".
# That trailing location is the reliable state signal — detect_gov_client reads the CLIENT from prose
# and found a state on only 2 of 131 roles, because the bulletin body rarely names one.
_TITLE_LOCATION = re.compile(r"\bin\s+([^|]{3,60}?)\s*(?:\||$)", re.I)
_STATE_NAMES = re.compile(r"\b(" + "|".join(sorted(STATE_ABBR, key=len, reverse=True)) + r")\b", re.I)
_STATE_CODE = re.compile(r",\s*([A-Z]{2})\b")


def _state_from_title(title: str) -> str:
    loc = _TITLE_LOCATION.search(title or "")
    text = loc.group(1) if loc else (title or "")
    m = _STATE_NAMES.search(text)
    if m:
        return STATE_ABBR[m.group(1).lower()]
    m = _STATE_CODE.search(text)
    return m.group(1).upper() if m and m.group(1).upper() in set(STATE_ABBR.values()) else ""


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


async def _sitemap_urls(client: httpx.AsyncClient) -> list[str]:
    r = await client.get(_SITEMAP, timeout=90)
    r.raise_for_status()
    return [u for u in re.findall(r"<loc>(.*?)</loc>", r.text) if "/jobs/" in u]


def in_lane(url: str) -> bool:
    slug = url.rsplit("/", 1)[-1]
    return bool(_IN_LANE.search(slug)) and not _NOT_LANE.search(slug)


async def _bulletin(client: httpx.AsyncClient, url: str) -> dict | None:
    m = re.search(r"/jobs/(\d+)", url)
    if not m:
        return None
    try:
        r = await client.get(_DETAIL.format(jid=m.group(1)), timeout=25)
        if r.status_code != 200:
            return None
    except Exception:
        return None
    body = _text(r.text)
    title_m = _TITLE.search(r.text)
    raw_title = html.unescape(title_m.group(1).strip()) if title_m else ""
    state = _state_from_title(raw_title)
    # Strip the site suffix AND the trailing " in <location>" so the title is just the role.
    title = re.sub(r"\s*\|\s*Government Jobs.*$", "", raw_title)
    title = re.sub(r"\s+in\s+[^|]{3,60}$", "", title).strip()
    contacts = [e for e in dict.fromkeys(_EMAIL.findall(body)) if not _NOT_A_CONTACT.search(e)]
    gov = detect_gov_client(title, body[:6000], "", "")
    return {
        "title": title or url.rsplit("/", 1)[-1].replace("-", " ").title(),
        "url": _DETAIL.format(jid=m.group(1)),
        "contacts": contacts[:2],
        "citizenship_bar": bool(_CITIZEN_BAR.search(body)),
        "residency_required": bool(_RESIDENCY.search(body)),
        "gov_client": gov,
        "state": state or (gov or {}).get("state", ""),
        "location": (_TITLE_LOCATION.search(raw_title).group(1).strip()
                     if _TITLE_LOCATION.search(raw_title) else ""),
        "description": body[:6000],
    }


async def scan(*, limit: int = 150, concurrency: int = 6) -> dict:
    """Enumerate live public-sector postings in-lane and read each bulletin.

    Returns {"roles": [...], "stats": {...}}. Roles with an explicit citizenship/residency bar are
    excluded from `roles` but counted, so the drop is visible rather than silent.
    """
    async with httpx.AsyncClient(follow_redirects=True, headers=_UA) as client:
        try:
            urls = await _sitemap_urls(client)
        except Exception as e:
            logger.error("gov_sitemap_failed", error=str(e)[:120])
            return {"roles": [], "stats": {"error": "sitemap unreachable"}}

        candidates = [u for u in urls if in_lane(u)][:limit]
        sem = asyncio.Semaphore(concurrency)

        async def one(u):
            async with sem:
                return await _bulletin(client, u)

        results = [r for r in await asyncio.gather(*[one(u) for u in candidates]) if r]

    barred = [r for r in results if r["citizenship_bar"]]
    roles = [r for r in results if not r["citizenship_bar"]]
    stats = {"live_postings": len(urls), "in_lane": len([u for u in urls if in_lane(u)]),
             "fetched": len(results), "citizenship_barred": len(barred),
             "residency_required": sum(1 for r in roles if r["residency_required"]),
             "kept": len(roles), "with_contact": sum(1 for r in roles if r["contacts"]),
             "states": len({r["state"] for r in roles if r["state"]})}
    logger.info("gov_jobs_scanned", **stats)
    return {"roles": roles, "stats": stats}


async def run(state, *, limit: int = 150, tailor: bool = False, persist: bool = False):
    """Scan and hand the surviving roles to ats_intake, tagged as public-sector."""
    from agents.application.ats_intake import intake_jobs

    found = await scan(limit=limit)
    roles = found["roles"]
    if not roles:
        return state, {**found["stats"], "queued": 0}

    discovered = [{
        "company": (r["gov_client"] or {}).get("signal", "") or "Government Agency",
        "title": r["title"], "description": r["description"],
        "location": r.get("location") or r.get("state", ""),
        "source": "govjobs:neogov", "fallback_url": r["url"],
        "employment_type": "Full-time",
        "gov_client": r["gov_client"], "gov_state": r.get("state", ""),
    } for r in roles]

    pre_apps = {a.get("id") for a in state.get("applications", [])}
    pre_jobs = {j.get("id") for j in state.get("job_listings", [])}
    updated, report = await intake_jobs(discovered, state, limit=limit, tailor=tailor)
    report.update(found["stats"])

    if persist:
        from core.state_store import merge_new
        report["persisted"] = merge_new(
            [j for j in updated.get("job_listings", []) if j.get("id") not in pre_jobs],
            [a for a in updated.get("applications", []) if a.get("id") not in pre_apps])
    return updated, report
