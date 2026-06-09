"""
Job Scanner Agent
For each company in state, finds open technical job listings using three strategies
in priority order:

  1. Greenhouse public API  — boards-api.greenhouse.io (no auth, JSON)
  2. Lever public API       — api.lever.co/v0/postings (no auth, JSON)
  3. Playwright scrape      — careers page → LLM extraction (fallback)

Also enriches early JobListing hints already in state from hiring_feed_scanner.
"""

import asyncio
import uuid
import re
import json
import httpx
import yaml
from pathlib import Path
from datetime import datetime, timezone

from core.state import Company, JobListing, JobSearchState
from core.memory import Memory
from core.llm import get_llm
from core.logger import get_logger
from core.stealth import throttle, is_off_hours

logger = get_logger("job_scanner")

_SYSTEM_PROMPT = Path("prompts/system/job_scanner.txt").read_text()
_PROFILE_PATH = Path("data/user_profile.yaml")

# ── ATS slug guesser ───────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    """'Datadog, Inc.' → 'datadog'"""
    slug = name.lower()
    slug = re.sub(r"\b(inc|llc|ltd|corp|co|technologies|technology|solutions|labs|ai)\b\.?", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug


def _slug_variants(name: str) -> list[str]:
    base = _slugify(name)
    # Also try without hyphens, and the raw first word
    no_hyphen = base.replace("-", "")
    first_word = base.split("-")[0]
    return list(dict.fromkeys([base, no_hyphen, first_word]))  # dedup, preserve order


# ── Strategy 1: Greenhouse API ─────────────────────────────────────────────

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
LEVER_API      = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY_API      = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

# ── Location pre-filter ────────────────────────────────────────────────────

_INTL_LOCATION_KEYWORDS = [
    "united kingdom", " uk,", " uk ", "london", "england", "scotland",
    "germany", "berlin", "munich", "austria", "switzerland", "zurich",
    "france", "paris", "netherlands", "amsterdam", "belgium", "brussels",
    "spain", "madrid", "barcelona", "italy", "milan", "ireland", "dublin",
    "israel", "tel aviv", "india", "bangalore", "hyderabad", "mumbai",
    "canada", "toronto", "vancouver", "montreal", "australia", "sydney",
    "melbourne", "singapore", "hong kong", "japan", "tokyo", "brazil",
    "mexico city", "new zealand", "poland", "warsaw", "sweden", "stockholm",
    "denmark", "copenhagen", "norway", "oslo", "finland", "helsinki",
    "czech republic", "prague",
]

def _is_target_location(location: str) -> bool:
    """True if location is remote, US-based, or unspecified (safe to score)."""
    if not location:
        return True
    loc = location.lower()
    if "remote" in loc or "anywhere" in loc or "worldwide" in loc:
        return True
    if any(intl in loc for intl in _INTL_LOCATION_KEYWORDS):
        return False
    return True  # unknown US city or unlisted → pass through


async def _try_greenhouse(
    client: httpx.AsyncClient,
    company: Company,
    keywords: list[str],
) -> list[JobListing]:
    jobs: list[JobListing] = []
    for slug in _slug_variants(company["name"]):
        try:
            url = GREENHOUSE_API.format(slug=slug)
            resp = await client.get(url, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            listings = data.get("jobs", [])
            if not listings:
                continue

            logger.info("greenhouse_found", company=company["name"], slug=slug, count=len(listings))

            for item in listings:
                title = item.get("title") or ""
                if not _is_tech_role(title):
                    continue
                content = item.get("content") or ""
                location = ""
                loc_data = item.get("location") or {}
                if isinstance(loc_data, dict):
                    location = loc_data.get("name") or ""
                elif isinstance(loc_data, str):
                    location = loc_data

                if not _is_target_location(location):
                    continue

                job = _build_job(
                    company=company,
                    title=title,
                    description=_strip_html(content),
                    apply_url=item.get("absolute_url") or "",
                    source="greenhouse",
                    location=location,
                )
                jobs.append(job)

            if jobs:
                return jobs  # found via this slug, no need to try others
        except Exception as e:
            logger.warning("greenhouse_error", company=company["name"], slug=slug, error=str(e))
            continue
    return jobs


# ── Strategy 2: Lever API ──────────────────────────────────────────────────

async def _try_lever(
    client: httpx.AsyncClient,
    company: Company,
) -> list[JobListing]:
    jobs: list[JobListing] = []
    for slug in _slug_variants(company["name"]):
        try:
            url = LEVER_API.format(slug=slug)
            resp = await client.get(url, timeout=10)
            if resp.status_code != 200:
                continue
            listings = resp.json()
            if not listings:
                continue

            logger.info("lever_found", company=company["name"], slug=slug, count=len(listings))

            for item in listings:
                title = item.get("text") or ""
                if not _is_tech_role(title):
                    continue
                categories = item.get("categories") or {}
                location = categories.get("location") or categories.get("team") or ""
                if not _is_target_location(location):
                    continue
                description = ""
                lists = item.get("lists") or []
                for section in lists:
                    description += f"\n{section.get('text','')}\n"
                    for content in (section.get("content") or "").split("<li>"):
                        description += _strip_html(content) + "\n"
                description = description.strip()[:3000]

                job = _build_job(
                    company=company,
                    title=title,
                    description=description,
                    apply_url=item.get("hostedUrl") or item.get("applyUrl") or "",
                    source="lever",
                    location=location,
                )
                jobs.append(job)

            if jobs:
                return jobs
        except Exception as e:
            logger.warning("lever_error", company=company["name"], slug=slug, error=str(e))
            continue
    return jobs


# ── Strategy 2.5: Ashby API ───────────────────────────────────────────────

async def _try_ashby(
    client: httpx.AsyncClient,
    company: Company,
) -> list[JobListing]:
    jobs: list[JobListing] = []
    for slug in _slug_variants(company["name"]):
        try:
            url = ASHBY_API.format(slug=slug)
            resp = await client.get(url, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            listings = data.get("jobPostings", [])
            if not listings:
                continue

            logger.info("ashby_found", company=company["name"], slug=slug, count=len(listings))

            for item in listings:
                title = item.get("title") or ""
                if not _is_tech_role(title):
                    continue
                location = item.get("locationName") or item.get("location") or ""
                if not _is_target_location(location):
                    continue

                # Prefer plain text; fall back to stripping HTML from body
                desc = (item.get("descriptionPlain") or
                        _strip_html(item.get("descriptionBody") or ""))
                apply_url = item.get("externalLink") or item.get("jobUrl") or ""

                job = _build_job(
                    company=company,
                    title=title,
                    description=desc[:4000],
                    apply_url=apply_url,
                    source="ashby",
                    location=location,
                )
                jobs.append(job)

            if jobs:
                return jobs
        except Exception as e:
            logger.warning("ashby_error", company=company["name"], slug=slug, error=str(e))
            continue
    return jobs


# ── Strategy 3: Playwright + LLM ──────────────────────────────────────────

CAREERS_URL_PATTERNS = [
    "{website}/careers",
    "{website}/jobs",
    "{website}/about/careers",
    "{website}/company/careers",
    "{website}/work-with-us",
]

async def _try_playwright_scrape(
    company: Company,
    llm,
    keywords: list[str],
) -> list[JobListing]:
    from tools.browser import BrowserClient

    website = company.get("website") or ""
    if not website or "hacker-news" in website or "remoteok" in website:
        return []

    # Normalize website URL
    if not website.startswith("http"):
        website = "https://" + website
    website = website.rstrip("/")

    from agents.discovery.apply_url_resolver import _collect_anchors, _best_match, is_listing_url

    urls_to_try = [p.format(website=website) for p in CAREERS_URL_PATTERNS]

    text = ""
    anchors: list[tuple[str, str]] = []
    landing = website
    async with BrowserClient(headless=True) as browser:
        for url in urls_to_try:
            page = None
            try:
                page = await browser.new_page(url)
                import asyncio
                await asyncio.sleep(1.0)
                t = await page.inner_text("body")
                if t and len(t) > 200:
                    text = t[:8000]
                    landing = page.url or url
                    anchors = await _collect_anchors(page)  # real per-job links on the page
                    logger.info("careers_page_found", company=company["name"], url=url,
                                anchors=len(anchors))
                    break
            except Exception:
                continue
            finally:
                if page is not None:
                    try:
                        await page.context.close()
                    except Exception:
                        pass

    if not text:
        logger.info("careers_page_not_found", company=company["name"])
        return []

    # Use LLM to extract structured job listings from the page text
    messages = [
        {
            "role": "user",
            "content": (
                f"Company: {company['name']}\n"
                f"Return at most 10 tech role listings.\n"
                f"Careers page text:\n\n{text[:4000]}"
            ),
        }
    ]
    try:
        result = await llm.chat_json(_SYSTEM_PROMPT, messages, max_tokens=2048)
        if not isinstance(result, list):
            return []
    except Exception as e:
        logger.error("llm_job_extraction_failed", company=company["name"], error=str(e))
        return []

    jobs: list[JobListing] = []
    for item in result:
        title = item.get("title") or ""
        if not title:
            continue
        # Prefer a real per-job link found on the page; only fall back to the listing URL
        # when neither the LLM nor the page anchors yield a single-job application route.
        apply_url = item.get("apply_url") or ""
        if not apply_url or is_listing_url(apply_url):
            matched = _best_match(anchors, title, landing) if anchors else None
            apply_url = matched or apply_url or (website + "/careers")
        job = _build_job(
            company=company,
            title=title,
            description=item.get("description") or "",
            apply_url=apply_url,
            source="careers_page",
            location=item.get("location") or "",
        )
        job["required_skills"] = item.get("required_skills") or []
        job["nice_to_have_skills"] = item.get("nice_to_have_skills") or []
        job["salary_min"] = item.get("salary_min")
        job["salary_max"] = item.get("salary_max")
        job["remote"] = item.get("remote", False)
        jobs.append(job)

    return jobs


# ── Shared helpers ─────────────────────────────────────────────────────────

TECH_ROLE_KEYWORDS = [
    "engineer", "developer", "architect", "devops", "sre", "platform",
    "data", "ml", "machine learning", "ai ", "backend", "fullstack",
    "full-stack", "cloud", "infrastructure", "python", "java", "scala",
    "spark", "kubernetes", "mlops", "staff", "principal",
]

def _is_tech_role(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in TECH_ROLE_KEYWORDS)


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html).strip()


def _build_job(
    company: Company,
    title: str,
    description: str,
    apply_url: str,
    source: str,
    location: str = "",
) -> JobListing:
    return {
        "id": str(uuid.uuid4()),
        "company_id": company["id"],
        "company_name": company["name"],
        "title": title,
        "description": description[:4000],
        "required_skills": [],
        "nice_to_have_skills": [],
        "location": location or "Unknown",
        "remote": any(w in (location or "").lower() for w in ["remote", "anywhere", "distributed"]),
        "salary_min": None,
        "salary_max": None,
        "apply_url": apply_url,
        "source": source,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_profile() -> dict:
    return yaml.safe_load(_PROFILE_PATH.read_text())


# ── Seed company loader ────────────────────────────────────────────────────

_SEEDS_PATH = Path("data/company_seeds.yaml")

def _load_seed_companies(existing_names: set[str]) -> list[Company]:
    """
    Load curated seed companies from data/company_seeds.yaml.
    Only returns companies not already in state (deduped by lowercase name).
    """
    if not _SEEDS_PATH.exists():
        return []
    try:
        data = yaml.safe_load(_SEEDS_PATH.read_text()) or {}
        seeds = data.get("companies", [])
    except Exception:
        return []

    new: list[Company] = []
    for s in seeds:
        name = (s.get("name") or "").strip()
        if not name or name.lower() in existing_names:
            continue
        new.append({
            "id": str(uuid.uuid4()),
            "name": name,
            "website": s.get("website") or "",
            "linkedin_url": "",
            "size": s.get("size") or "unknown",
            "domain": s.get("domain") or "",
            "tech_stack": [],
            "open_role_count": 0,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        })
        existing_names.add(name.lower())
    return new


# ── Main scanner loop ──────────────────────────────────────────────────────

async def _scan_company(
    company: Company,
    client: httpx.AsyncClient,
    llm,
    keywords: list[str],
) -> list[JobListing]:
    """Try all three strategies for one company. Return first non-empty result."""
    await throttle("job_board_requests")

    # 1. Greenhouse
    jobs = await _try_greenhouse(client, company, keywords)
    if jobs:
        return jobs

    # 2. Lever
    jobs = await _try_lever(client, company)
    if jobs:
        return jobs

    # 2.5 Ashby
    jobs = await _try_ashby(client, company)
    if jobs:
        return jobs

    # 3. Playwright scrape (slower — only for companies with a real website)
    if company.get("website") and "hacker-news" not in company.get("website", ""):
        jobs = await _try_playwright_scrape(company, llm, keywords)

    return jobs


async def run(
    state: JobSearchState,
    max_companies: int = 150,
    batch_size: int = 5,
) -> JobSearchState:
    """
    Scan up to max_companies from state for open tech roles.
    Skips companies that already have job listings in state.
    """
    if is_off_hours():
        logger.info("skipping_off_hours")
        return state

    profile = _load_profile()
    target_roles = profile.get("target", {}).get("roles", [])
    keywords = [r.lower() for r in target_roles] + [
        s.lower() for s in profile.get("skills", {}).get("primary", [])[:5]
    ]

    salary_min = profile.get("target", {}).get("salary_min_usd", 0)

    # Companies that have been attempted (have jobs OR were tried and found nothing)
    attempted_ids = set(state.get("scanned_company_ids", []))
    # Also skip companies that already have job listings
    existing_company_ids = {j["company_id"] for j in state.get("job_listings", [])}
    skip_ids = attempted_ids | existing_company_ids

    all_companies = state.get("companies", [])
    # Prioritise never-attempted companies
    never_tried = [c for c in all_companies if c["id"] not in skip_ids]
    companies_to_scan = never_tried[:max_companies]

    logger.info(
        "job_scanner_start",
        total_companies=len(all_companies),
        already_attempted=len(attempted_ids | existing_company_ids),
        to_scan=len(companies_to_scan),
    )

    # Merge curated seed companies not yet in state
    existing_names = {c["name"].lower() for c in all_companies}
    seed_companies = _load_seed_companies(existing_names)
    if seed_companies:
        logger.info("seed_companies_loaded", count=len(seed_companies))
        all_companies = all_companies + seed_companies
        never_tried = [c for c in all_companies if c["id"] not in skip_ids]
        companies_to_scan = never_tried[:max_companies]

    memory = Memory.get()
    llm = get_llm("job_scanner", model="claude-haiku-4-5")
    all_new_jobs: list[JobListing] = []

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (job-search-bot)"},
        follow_redirects=True,
    ) as client:
        # Process in batches to avoid overwhelming sites
        for i in range(0, len(companies_to_scan), batch_size):
            batch = companies_to_scan[i:i + batch_size]
            tasks = [_scan_company(c, client, llm, keywords) for c in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for company, result in zip(batch, results):
                attempted_ids.add(company["id"])  # mark attempted regardless of result
                if isinstance(result, Exception):
                    logger.error("company_scan_failed", company=company["name"], error=str(result))
                    continue
                if not result:
                    logger.info("no_jobs_found", company=company["name"])
                    continue

                # Apply salary filter
                relevant = [
                    j for j in result
                    if j.get("salary_max") is None or (j.get("salary_max") or 0) >= salary_min
                ]

                for job in relevant:
                    memory.add_job_listing(
                        job["id"],
                        f"{job['title']} {job['description'][:300]}",
                        {
                            "company_name": job["company_name"],
                            "title": job["title"],
                            "source": job["source"],
                            "apply_url": job["apply_url"],
                        },
                    )

                all_new_jobs.extend(relevant)
                logger.log_decision(
                    action="company_scanned",
                    outcome="jobs_found",
                    details={
                        "company": company["name"],
                        "jobs_found": len(relevant),
                        "source": relevant[0]["source"] if relevant else "none",
                    },
                )

            # Pause between batches
            await asyncio.sleep(2)

    logger.log_decision(
        action="job_scan_complete",
        outcome="success",
        details={
            "companies_scanned": len(companies_to_scan),
            "new_jobs_found": len(all_new_jobs),
            "total_jobs": len(state.get("job_listings", [])) + len(all_new_jobs),
        },
    )

    updated = dict(state)
    # Merge: keep existing job listings, add new ones (deduplicate by apply_url)
    existing_urls = {j["apply_url"] for j in state.get("job_listings", [])}
    deduped_new = [j for j in all_new_jobs if j["apply_url"] not in existing_urls]

    updated["job_listings"] = state.get("job_listings", []) + deduped_new
    # Persist attempted company IDs so future runs skip them
    updated["scanned_company_ids"] = list(attempted_ids | existing_company_ids)
    # Persist any newly-loaded seed companies in state so they survive across runs
    if seed_companies:
        updated["companies"] = all_companies
    return updated


# ── CLI smoke test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    async def _test():
        # Seed a few known companies with Greenhouse/Lever
        test_companies: list[Company] = [
            {
                "id": str(uuid.uuid4()), "name": "Datadog", "website": "https://www.datadoghq.com",
                "linkedin_url": "", "size": "mid", "domain": "Developer Tools",
                "tech_stack": [], "open_role_count": 0,
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "id": str(uuid.uuid4()), "name": "Cloudflare", "website": "https://www.cloudflare.com",
                "linkedin_url": "", "size": "mid", "domain": "Developer Tools",
                "tech_stack": [], "open_role_count": 0,
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "id": str(uuid.uuid4()), "name": "Snowflake", "website": "https://www.snowflake.com",
                "linkedin_url": "", "size": "large", "domain": "Data Infrastructure",
                "tech_stack": [], "open_role_count": 0,
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            },
        ]
        state: JobSearchState = {
            "companies": test_companies, "job_listings": [], "fit_scores": [],
            "applications": [], "pending_approvals": [], "recruiter_contacts": [],
            "email_events": [], "meetings_scheduled": [],
            "weekly_quality_score": 0.0, "weekly_quantity_count": 0,
            "response_rate": 0.0, "interview_rate": 0.0,
            "last_run_at": None, "errors": [], "agent_statuses": {},
        }
        result = await run(state, max_companies=3, batch_size=3)
        print(f"\nJobs found: {len(result['job_listings'])}")
        for j in result["job_listings"][:15]:
            print(f"  [{j['source']:15s}] {j['title']:45s} @ {j['company_name']}")

    asyncio.run(_test())
