"""Indeed's inventory, through the one door that still opens.

Indeed itself is closed to this pipeline, checked directly rather than assumed:

    indeed.com/jobs              403
    indeed.com/m/jobs (mobile)   403
    indeed.com/rss                404   (feed retired)
    api.indeed.com/ads/apisearch  DNS failure (Publisher API shut down)
    indeed.com/viewjob            401

SimplyHired is the same group's aggregator and serves the same postings — its job records literally
carry a `dateOnIndeed` field. It returns 200 to a plain GET and streams its results as Next.js
`__NEXT_DATA__`, so the listing array can be read with no browser and no key.

Two things measured here that shape the whole module:

  * `job_types=contract` in the URL DOES NOT FILTER — a contract-filtered response still came back
    7 full-time / 7 contract. So employment is filtered client-side off each record's `jobTypes`.

  * There is NO working recency sort. `sb=dd`, `sort=date`, `s=date` and `fdb=7` were each tried:
    every one returned a median posting age of about three weeks. That makes this a fundamentally
    STALE source, and stale C2C reqs are worse than no reqs — they are already filled, and they
    crowd out fresh roles in the queue. Hence `_MAX_AGE_DAYS` below is hard and deliberately tight.
    Expect a small yield. That is the honest trade for having Indeed's supply at all, and it is why
    this scanner is NOT part of the fast lane.

`dateOnIndeed` is epoch milliseconds — a real timestamp, unlike Dice's date-only `postedDate`. So
posting age here is exact rather than rounded to midnight.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import httpx

from core.logger import get_logger

logger = get_logger("simplyhired_scanner")

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
       "Accept-Language": "en-US,en;q=0.9"}
_SEARCH = "https://www.simplyhired.com/search?q={q}&pn={p}"
_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# The source's median posting is ~3 weeks old, so this gate is what makes it usable at all. Anything
# older is a filled req; queueing it wastes a slot the user has to triage past.
_MAX_AGE_DAYS = 4

QUERIES = ("data engineer contract", "aws data engineer contract", "etl developer contract",
           "pyspark contract", "snowflake contract", "python developer contract",
           "data engineer corp to corp")

# Same lane exclusions the Dice scanner uses — tailoring for these would be fabrication.
from agents.discovery.dice_scanner import _NOT_LANE  # noqa: E402


def parse_jobs(html: str) -> list[dict]:
    m = _NEXT.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        arr = data["props"]["pageProps"]["jobs"]
    except Exception:
        return []
    out = []
    for j in arr:
        key = j.get("jobKey") or ""
        ts = j.get("dateOnIndeed")
        posted = ""
        if ts:
            try:
                posted = datetime.fromtimestamp(int(ts) / 1000, timezone.utc).isoformat()
            except Exception:
                posted = ""
        out.append({
            "title": (j.get("title") or "").strip(),
            "company": (j.get("company") or "").strip(),
            "employment": ", ".join(j.get("jobTypes") or []),
            "location": (j.get("location") or "").strip(),
            "posted_at": posted,
            # The canonical viewjob link; SimplyHired's own /job/<key> also resolves.
            "url": f"https://www.simplyhired.com/job/{key}" if key else "",
            "summary": (j.get("snippet") or "")[:1500],
            "salary": j.get("salaryInfo") or "",
        })
    return out


def _fresh(job: dict) -> bool:
    p = job.get("posted_at") or ""
    if not p:
        return False          # unknown age on a source this stale = assume stale
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(p)
    except Exception:
        return False
    return age <= timedelta(days=_MAX_AGE_DAYS)


def _is_contract(job: dict) -> bool:
    """Off the record's own jobTypes, because the URL filter is ignored by the server."""
    e = (job.get("employment") or "").lower()
    if "contract" in e or "temporary" in e:
        return True
    # No jobTypes at all: fall back to the title/snippet saying so explicitly.
    if not e:
        blob = f"{job.get('title','')} {job.get('summary','')}".lower()
        return any(k in blob for k in ("c2c", "corp to corp", "corp-to-corp", "contract"))
    return False


def in_lane(job: dict) -> bool:
    return bool(job.get("title")) and not _NOT_LANE.search(job["title"])


async def scan(*, pages: int = 3, contract_only: bool = True) -> list[dict]:
    seen: dict[str, dict] = {}
    async with httpx.AsyncClient(follow_redirects=True, headers=_UA) as client:
        sem = asyncio.Semaphore(4)

        async def one(q, p):
            async with sem:
                try:
                    r = await client.get(_SEARCH.format(q=q.replace(" ", "+"), p=p), timeout=25)
                except Exception:
                    return []
                return parse_jobs(r.text) if r.status_code == 200 else []

        for res in await asyncio.gather(*[one(q, p) for q in QUERIES
                                          for p in range(1, pages + 1)]):
            for j in res:
                if j.get("url"):
                    seen[j["url"]] = j

    jobs = list(seen.values())
    n_all = len(jobs)
    jobs = [j for j in jobs if in_lane(j)]
    if contract_only:
        jobs = [j for j in jobs if _is_contract(j)]
    n_contract = len(jobs)
    jobs = [j for j in jobs if _fresh(j)]
    logger.info("simplyhired_scanned", found=n_all, contract=n_contract, fresh=len(jobs),
                max_age_days=_MAX_AGE_DAYS)
    return jobs


async def run(state, *, pages: int = 3, limit: int = 60, tailor: bool = False,
              persist: bool = False, fit_gate: bool = True):
    """Scan SimplyHired and hand fresh contract roles to ats_intake."""
    from agents.application.ats_intake import intake_jobs

    jobs = await scan(pages=pages)
    if not jobs:
        return state, {"discovered": 0, "queued": 0}

    discovered = [{
        "company": j["company"], "title": j["title"],
        "description": j.get("summary") or f"{j['title']} at {j['company']}. {j['employment']}.",
        "location": j["location"], "posted_at": j["posted_at"],
        "source": "simplyhired:contract", "fallback_url": j["url"],
        "employment_type": j["employment"] or "Contract",
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
    logger.log_decision(action="simplyhired_scan_complete", outcome="success", details=report)
    return updated, report
