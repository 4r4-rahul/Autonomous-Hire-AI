"""State/local-GOVERNMENT contract (C2C) discovery — the public-sector staff-augmentation lane.

Where the C2C volume actually is: a staffing vendor holds a master contract with a state agency
(a DOT, a Medicaid/MMIS program, a Department of Revenue, a county) and posts each individual
requirement on its own public ATS board. Those reqs are corp-to-corp / W2-contract by construction,
run 12-36 months, and — unlike FEDERAL contracts — rarely carry a clearance or citizenship bar.

Direct state-employee postings (NEOGOV / governmentjobs.com) are deliberately NOT scanned: they are
civil-service W2 roles that almost never sponsor, and the portal is a bot-gated SPA with no public
API — exactly the fragile branch this project avoids. The vendor boards are plain HTTP JSON.

Three filters make this lane safe for a sponsorship-needing candidate, and they run BEFORE the
expensive resolve/score/tailor path so nothing is spent on a dead end:
  * FEDERAL end clients are dropped (clearance / Public Trust / "US person" bars).
  * USC/GC-only reqs are dropped — including the compact TITLE tags ("(USC/GC/TN/CTH)") that the
    prose-based sponsorship knock-out list never sees, because a SmartRecruiters LISTING carries no
    description at all.
  * The title filter is the CONTRACT lane's, not the product-company one: gov reqs are titled
    "Programmer Analyst", "Java Developer", "ETL Developer" — never "Staff Software Engineer".

Roles that survive are tagged with the government client (level + state) and handed to ats_intake.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

from core.logger import get_logger
from core.gov_client import detect_gov_client, is_federal_client, usc_gc_only, visa_friendly
from agents.discovery.apply_url_resolver import _fetch_company_postings, loc_is_us_ok, posting_age_days
from agents.application.ats_intake import intake_jobs

logger = get_logger("gov_scanner")

_ROOT = Path(__file__).parent.parent.parent
_VENDORS = _ROOT / "data" / "gov_vendors.json"

# ── Contract-lane titles ──────────────────────────────────────────────────────────────────────
# Public-sector/C2C reqs use staffing vocabulary, not product-company vocabulary. Every entry here
# is a role Rahul can actually deliver (Java/Spring, Python, data platform, AWS/GCP/Azure, DevOps).
_TITLE_INCLUDE = (
    "java developer", "java engineer", "java consultant", "j2ee", "spring boot", "microservices",
    "python developer", "python engineer", "backend developer", "back end developer",
    "software developer", "software engineer", "application developer", "api developer",
    "integration developer", "middleware developer", "full stack", "fullstack",
    "data engineer", "big data", "etl developer", "etl engineer", "hadoop", "spark", "pyspark",
    "snowflake", "databricks", "kafka", "data warehouse", "data platform", "data architect",
    "cloud engineer", "cloud architect", "cloud developer", "aws engineer", "aws developer",
    "azure engineer", "gcp engineer", "devops", "site reliability", " sre", "sre ",
    "platform engineer", "infrastructure engineer", "kubernetes", "solution architect",
    "solutions architect", "technical architect", "programmer analyst", "database developer",
)
# Dropped even when an include term matches: wrong stack, wrong discipline, or wrong seniority.
# Ordered specific-before-general so a stack exclusion ("sharepoint developer") wins over a generic
# include ("software developer"). Excludes are evaluated FIRST, so this list is authoritative.
_TITLE_EXCLUDE = (
    # stacks outside the candidate's profile — a tailored résumé would be fabrication
    ".net", "dotnet", "c# ", "sharepoint", "sitecore", "peoplesoft", "sap ", "abap", "siebel",
    "salesforce", "servicenow", "mainframe", "cobol", "as/400", "as400", "iseries", "guidewire",
    "duck creek", "pega", "oracle apps", "ebs ", "hyperion", "tibco", "webmethods", "informatica",
    "sitefinity", "adobe", "drupal", "wordpress", "ruby on rails", "php", "golang developer",
    "webshpere", "websphere", "cast iron", "onbase", "documentum", "filenet",
    # wrong discipline
    "qa ", "quality assurance", "tester", "test engineer", "uat", "automation tester",
    "business analyst", "business systems analyst", "systems analyst", "scrum master",
    "project manager", "program manager", "product owner", "technical writer", "trainer",
    "help desk", "helpdesk", "desktop", "service desk", "network engineer", "network administrator",
    "security engineer", "cyber", "penetration", "dba", "database administrator",
    "recruiter", "sales", "account executive", "marketing", "customer service", "data entry",
    "nurse", "clinical", "medical", "physician", "pharmacist", "electrical", "mechanical",
    "civil engineer", "manufacturing", "scada", "drafter", "surveyor", "accountant",
    # wrong seniority / not an IC role
    "intern", "manager", "director", " vp ", "vp,", "head of", "principal consultant",
)
# Match ats_intake's freshness gate: a role it would drop as stale is not worth a JD fetch here.
_MAX_AGE_DAYS = 30
_SR_DETAIL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{pid}"
# Sections worth reading as the JD. companyDescription is vendor boilerplate ("we serve federal,
# state and local agencies") and would poison BOTH the gov-client and federal detectors, so it's
# deliberately excluded.
_JD_SECTIONS = ("jobDescription", "qualifications", "additionalInformation")


def _title_ok(title: str) -> bool:
    t = f" {(title or '').lower()} "
    if any(x in t for x in _TITLE_EXCLUDE):
        return False
    return any(x in t for x in _TITLE_INCLUDE)


def _vendors() -> list[dict]:
    try:
        return [v for v in json.loads(_VENDORS.read_text()).get("vendors", []) if v.get("slug")]
    except Exception:
        return []


async def _sr_detail(client: httpx.AsyncClient, slug: str, url: str) -> str:
    """Full JD text for a SmartRecruiters posting. The LISTING API returns no description at all,
    so without this the citizenship filter, the gov-client tag, fit scoring and the tailored résumé
    would all be working from a bare title."""
    m = re.search(r"smartrecruiters\.com/[^/]+/(\d+)", url or "")
    if not m:
        return ""
    try:
        r = await client.get(_SR_DETAIL.format(slug=slug, pid=m.group(1)), timeout=10)
        if r.status_code != 200:
            return ""
        sections = (r.json().get("jobAd") or {}).get("sections") or {}
    except Exception:
        return ""
    parts = []
    for key in _JD_SECTIONS:
        text = (sections.get(key) or {}).get("text") or ""
        if text:
            parts.append(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", " ".join(parts)).replace("&amp;", "&").strip()


async def run(state, *, limit: int = 200, tailor: bool = False, persist: bool = False,
              max_details: int = 120, gov_only: bool = False, include_employers: bool = False):
    """Scan the gov/C2C vendor boards and intake the fitting contract roles.

    max_details: cap on JD detail fetches per run (one cheap HTTP call each).
    gov_only:    keep ONLY roles with a detected state/local government end client. Off by default —
                 these vendors' non-gov commercial reqs are C2C too, and dropping them would throw
                 away most of the contract supply this lane exists to provide.
    include_employers: also scan government EMPLOYERS' own boards (kind="employer", e.g. City of
                 New York). Off by default: those are W2 civil-service roles that rarely sponsor —
                 the opposite of what this lane is for.
    """
    vendors = [v for v in _vendors()
               if include_employers or v.get("kind", "vendor") != "employer"]
    if not vendors:
        return state, {"error": "no gov vendors — run scripts/expand_gov_vendors.py"}

    sem = asyncio.Semaphore(8)

    async def _board(v):
        async with sem:
            try:
                return v, await _fetch_company_postings(v["slug"])
            except Exception:
                return v, None

    boards = await asyncio.gather(*[_board(v) for v in vendors])

    # Title + location gates first (free), so the bounded JD fetches go only to real candidates.
    leads: list[tuple[dict, dict]] = []
    stale = 0
    for vendor, postings in boards:
        for p in postings or []:
            if not (_title_ok(p.get("title")) and loc_is_us_ok(p.get("location", ""))):
                continue
            # FRESHNESS FIRST. These vendor boards are ~90% zombie reqs — measured across the
            # universe, 4,572 of 5,183 postings were over a year old and only 33 were inside the
            # 30-day window. Intake would drop the stale ones anyway, but only AFTER this scanner
            # had spent its whole JD-detail budget on 2017 postings and never reached the live
            # ones. So drop them here and sort what's left newest-first.
            age = posting_age_days(p.get("posted_at", ""))
            if age is not None and age > _MAX_AGE_DAYS:
                stale += 1
                continue
            leads.append((vendor, p))
    # Newest first; unknown-age postings sort last (they're a coin flip, live reqs are dated).
    leads.sort(key=lambda vp: posting_age_days(vp[1].get("posted_at", "")) if
               posting_age_days(vp[1].get("posted_at", "")) is not None else 10 ** 6)

    stats = {"vendors": len(vendors), "in_lane": len(leads), "dropped_stale": stale,
             "dropped_citizenship": 0, "dropped_federal": 0, "dropped_no_gov": 0,
             "gov_tagged": 0, "visa_friendly": 0}

    # Title-level citizenship bar — catches "(USC/GC/TN/CTH)" before we spend a detail fetch on it.
    kept = []
    for vendor, p in leads:
        if usc_gc_only(p.get("title", ""), ""):
            stats["dropped_citizenship"] += 1
            continue
        kept.append((vendor, p))

    async with httpx.AsyncClient(headers={"Accept": "application/json"},
                                 follow_redirects=True) as client:
        async def _detail(vendor, p):
            if p.get("description") or vendor.get("ats") != "smartrecruiters":
                return p.get("description", "")
            async with sem:
                return await _sr_detail(client, vendor["slug"], p.get("url", ""))

        details = await asyncio.gather(*[_detail(v, p) for v, p in kept[:max_details]])
    details = list(details) + [""] * max(0, len(kept) - max_details)

    discovered: list[dict] = []
    for (vendor, p), jd in zip(kept, details):
        title = p.get("title") or ""
        bar = usc_gc_only(title, jd)
        if bar:
            stats["dropped_citizenship"] += 1
            continue
        if is_federal_client(f"{title}\n{jd}"):
            stats["dropped_federal"] += 1
            continue
        gov = detect_gov_client(title, jd, "", p.get("location", ""))
        if gov:
            stats["gov_tagged"] += 1
        elif gov_only:
            stats["dropped_no_gov"] += 1
            continue
        if visa_friendly(title, jd):
            stats["visa_friendly"] += 1
        discovered.append({
            "company": vendor.get("name") or vendor["slug"],
            "title": title,
            "description": jd,
            "location": p.get("location", ""),
            "posted_at": p.get("posted_at", ""),
            # `govc2c:` prefix marks a staffing source for core.employment → classified contract,
            # and exempts the role from the per-company cap (a vendor may carry many live reqs).
            "source": f"govc2c:{vendor.get('ats', 'board')}",
            "fallback_url": p.get("url"),
            "employment_type": p.get("employment_type", "") or "Contract",
            "gov_client": gov or None,
            "gov_state": (gov or {}).get("state", ""),
        })

    logger.info("gov_scan_discovered", roles=len(discovered), **stats)
    if not discovered:
        return state, {**stats, "discovered": 0, "queued": 0}

    pre_app_ids = {a.get("id") for a in state.get("applications", [])}
    pre_job_ids = {j.get("id") for j in state.get("job_listings", [])}
    updated, report = await intake_jobs(discovered, state, limit=limit, tailor=tailor)
    report.update(stats)
    report["discovered"] = len(discovered)

    if persist:
        from core.state_store import merge_new
        new_jobs = [j for j in updated.get("job_listings", []) if j.get("id") not in pre_job_ids]
        new_apps = [a for a in updated.get("applications", []) if a.get("id") not in pre_app_ids]
        report["persisted"] = merge_new(new_jobs, new_apps)

    logger.log_decision(action="gov_scan_complete", outcome="success", details=report)
    return updated, report
