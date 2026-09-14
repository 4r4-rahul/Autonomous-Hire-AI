"""Dice — the C2C board, and the freshest contract supply available.

The vendor ATS boards this project already scans turned out to be mostly abandoned: 4,572 of 5,183
postings older than a year, 33 inside the freshness window. Dice is the opposite — it is where US IT
staffing actually posts, and where `employmentType: "Contract, Third Party"` marks corp-to-corp
explicitly, so the C2C lane no longer has to be inferred from JD language.

There is no public API (the documented endpoint 403s without a key), but the search page is a
Next.js app that streams its results into the HTML as `self.__next_f.push` chunks. The job objects
are complete in there — title, company, employment type, location, posted date, detail URL — so a
plain GET per search page is enough. No browser, no key, same shape as every other scanner here.

Two parsing notes, both learned by getting them wrong:
  * The chunk payload is JSON-escaped, so it must be unescaped and then re-decoded as UTF-8 —
    `unicode_escape` alone turns an em dash into "â".
  * Take the jobList array and json.loads it. Regex-pairing adjacent fields silently misaligns
    company N with title N+1 the moment one record is missing a field.
"""
from __future__ import annotations

import asyncio
import json
import re

import httpx

from core.logger import get_logger

logger = get_logger("dice_scanner")

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
_SEARCH = ("https://www.dice.com/jobs?q={q}&page={p}&countryCode=US"
           "&filters.employmentType=CONTRACTS&filters.postedDate={age}")

# Rahul's lane, in the vocabulary Dice postings actually use.
QUERIES = ("data engineer", "big data", "etl developer", "pyspark", "aws data engineer",
           "azure data engineer", "databricks", "snowflake", "python developer",
           "java developer", "spark", "data platform engineer", "data architect")

# Wrong stack or wrong discipline — the same exclusions the contract lane uses elsewhere, because a
# tailored résumé for these would be fabrication.
_NOT_LANE = re.compile(
    r"(salesforce|sharepoint|power ?platform|d365|dynamics|peoplesoft|sap\b|abap|siebel|"
    r"mainframe|cobol|guidewire|duck creek|pega|workday|servicenow|oracle apps|"
    r"\bqa\b|tester|testing|sdet|manual test|business analyst|scrum master|project manager|"
    r"program manager|product manager|product owner|network engineer|help ?desk|desktop|"
    r"\.net\b|c#|php|ruby|drupal|wordpress|android|ios\b|mobile developer)", re.I)


def _unescape(raw: str) -> str:
    """JSON-escaped stream chunk -> real text. The double decode is deliberate: unicode_escape
    alone leaves UTF-8 bytes reinterpreted as latin-1 ("Databricks & AI" -> "Databricks â AI")."""
    s = raw.encode().decode("unicode_escape", "ignore")
    try:
        return s.encode("latin-1", "ignore").decode("utf-8", "ignore")
    except Exception:
        return s


def parse_jobs(html: str) -> list[dict]:
    blob = _unescape("".join(re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)))
    i = blob.find('"jobList":{"data":[')
    if i < 0:
        return []
    start = blob.index("[", i)
    depth = 0
    end = None
    for k in range(start, len(blob)):
        if blob[k] == "[":
            depth += 1
        elif blob[k] == "]":
            depth -= 1
            if depth == 0:
                end = k + 1
                break
    if not end:
        return []
    try:
        arr = json.loads(blob[start:end])
    except Exception:
        return []
    out = []
    for j in arr:
        loc = j.get("jobLocation") or {}
        out.append({
            "title": (j.get("title") or "").strip(),
            "company": (j.get("companyName") or "").strip(),
            "employment": j.get("employmentType") or "",
            "location": ", ".join(x for x in (loc.get("city"), loc.get("state")) if x),
            "posted_at": (j.get("postedDate") or "")[:10],
            "url": j.get("detailsPageUrl") or "",
            "summary": (j.get("summary") or "")[:1500],
        })
    return out


# The search page's `summary` is truncated at exactly 500 characters, and for some postings it is
# text from an unrelated req entirely (a "Data Engineer" title carrying a "Product Owner" blurb).
# Tailoring a résumé against that is tailoring against nothing. The DETAIL page carries the real
# posting — ~3.3k chars — in a rendered job-description container.
_JD_BLOCK = re.compile(r'<div[^>]*class="[^"]*job-?description[^"]*"[^>]*>(.*?)</div>', re.S | re.I)


async def fetch_full_jd(client: httpx.AsyncClient, url: str) -> str:
    """The complete posting text from a Dice job-detail page, or "" if it can't be read."""
    try:
        r = await client.get(url, timeout=25)
        if r.status_code != 200:
            return ""
    except Exception:
        return ""
    m = _JD_BLOCK.search(r.text)
    if not m:
        return ""
    txt = re.sub(r"<[^>]+>", " ", m.group(1))
    txt = re.sub(r"&nbsp;|&amp;", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


async def enrich(jobs: list[dict], *, concurrency: int = 6) -> int:
    """Replace each job's truncated summary with the full JD. Returns how many were improved."""
    improved = 0
    async with httpx.AsyncClient(follow_redirects=True, headers=_UA) as client:
        sem = asyncio.Semaphore(concurrency)

        async def one(j):
            nonlocal improved
            async with sem:
                full = await fetch_full_jd(client, j.get("url") or j.get("apply_url") or "")
            # Only accept a materially richer JD — a short/failed read must not overwrite what we have.
            if len(full) > max(600, len(j.get("summary") or j.get("description") or "")):
                j["summary"] = full
                j["description"] = full
                improved += 1

        await asyncio.gather(*[one(j) for j in jobs])
    logger.info("dice_jd_enriched", of=len(jobs), improved=improved)
    return improved


def in_lane(job: dict) -> bool:
    return bool(job.get("title")) and not _NOT_LANE.search(job["title"])


async def scan(*, age: str = "THREE", pages: int = 2, c2c_only: bool = True) -> list[dict]:
    """Fetch fresh contract postings. `age`: ONE=24h, THREE=3d, SEVEN=7d."""
    seen: dict[str, dict] = {}
    async with httpx.AsyncClient(follow_redirects=True, headers=_UA) as client:
        sem = asyncio.Semaphore(4)

        async def one(q, p):
            async with sem:
                try:
                    r = await client.get(
                        _SEARCH.format(q=q.replace(" ", "%20"), p=p, age=age), timeout=25)
                except Exception:
                    return []
                return parse_jobs(r.text) if r.status_code == 200 else []

        for res in await asyncio.gather(*[one(q, p) for q in QUERIES
                                          for p in range(1, pages + 1)]):
            for j in res:
                if j.get("url"):
                    seen[j["url"]] = j

    jobs = list(seen.values())
    if c2c_only:
        # "Contract, Third Party" is Dice's own label for corp-to-corp — an explicit employer
        # declaration, far better than inferring C2C from JD wording.
        jobs = [j for j in jobs if "third party" in j["employment"].lower()]
    jobs = [j for j in jobs if in_lane(j)]
    logger.info("dice_scanned", found=len(seen), kept=len(jobs), age=age)
    await enrich(jobs)
    return jobs


async def run(state, *, age: str = "THREE", pages: int = 2, limit: int = 200,
              tailor: bool = False, persist: bool = False, fit_gate: bool = True):
    """Scan Dice and hand fresh C2C roles to ats_intake."""
    from agents.application.ats_intake import intake_jobs

    jobs = await scan(age=age, pages=pages)
    if not jobs:
        return state, {"discovered": 0, "queued": 0}

    discovered = [{
        "company": j["company"], "title": j["title"],
        "description": j.get("summary") or f"{j['title']} at {j['company']}. {j['employment']}.",
        "location": j["location"], "posted_at": j["posted_at"],
        "source": "dice:contract", "fallback_url": j["url"],
        "employment_type": j["employment"],
    } for j in jobs]

    pre_apps = {a.get("id") for a in state.get("applications", [])}
    pre_jobs = {jj.get("id") for jj in state.get("job_listings", [])}
    updated, report = await intake_jobs(discovered, state, limit=limit, tailor=tailor,
                                        fit_gate=fit_gate)
    report["discovered"] = len(discovered)

    if persist:
        from core.state_store import merge_new
        report["persisted"] = merge_new(
            [jj for jj in updated.get("job_listings", []) if jj.get("id") not in pre_jobs],
            [a for a in updated.get("applications", []) if a.get("id") not in pre_apps])
    logger.log_decision(action="dice_scan_complete", outcome="success", details=report)
    return updated, report
