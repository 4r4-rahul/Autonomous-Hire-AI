"""
Hiring Feed Scanner
Discovers companies that are ACTIVELY hiring right now by pulling from
free public job feed sources:

  1. Hacker News "Who's Hiring"  — monthly thread, heavy on startups
  2. RemoteOK API                — remote tech jobs, free JSON API
  3. Y Combinator Jobs           — YC-backed startups
  4. Crunchbase funding news     — recently funded = expansion mode

This is the reverse of company_finder: instead of finding companies then
checking if they're hiring, we start from job postings and extract companies.
Feeds both Company records and early JobListing hints into state.
"""

import asyncio
import uuid
import re
import httpx
import yaml
from datetime import datetime, timezone
from pathlib import Path

from core.state import Company, JobListing, JobSearchState
from core.memory import Memory
from core.llm import get_llm
from core.logger import get_logger
from core.stealth import throttle

logger = get_logger("hiring_feed_scanner")

_PROFILE_PATH = Path("data/user_profile.yaml")

# ── Helpers ────────────────────────────────────────────────────────────────

def _load_profile() -> dict:
    return yaml.safe_load(_PROFILE_PATH.read_text())


def _target_keywords(profile: dict) -> list[str]:
    roles = profile.get("target", {}).get("roles", [])
    skills = profile.get("skills", {}).get("primary", [])
    keywords = [r.lower() for r in roles] + [s.lower() for s in skills[:5]]
    return keywords


def _is_relevant(text: str, keywords: list[str]) -> bool:
    """Quick keyword match to pre-filter irrelevant postings before LLM."""
    t = text.lower()
    return any(kw in t for kw in keywords)


_CAREERS_HINTS = (
    "careers", "jobs", "apply", "greenhouse", "lever", "workday",
    "ashby", "workable", "recruitee", "breezy", "smartrecruiters",
    "job", "hiring", "/o/", "/j/",
)

def _extract_apply_url(html_text: str) -> str | None:
    """
    Extract the most likely direct application URL from an HN comment's HTML body.
    Prefers URLs that look like careers pages or ATS forms over generic links.
    Falls back to mailto: links when the job is email-only.
    Returns None if no suitable URL found.
    """
    from html import unescape
    decoded = unescape(html_text)

    # Check for mailto: apply links first (email-only applications)
    mailto_matches = re.findall(r'href="(mailto:[^"]+)"', decoded)
    apply_emails = [m for m in mailto_matches if any(
        kw in m.lower() for kw in ("apply", "work@", "jobs@", "hiring@", "careers@", "recruit")
    )]

    # All https hrefs
    hrefs = re.findall(r'href="(https?://[^"]+)"', decoded)
    external = [u for u in hrefs if "ycombinator.com" not in u and "hacker-news" not in u]

    if not external and not apply_emails:
        return None

    if external:
        # Prefer URLs that look like job/careers pages
        scored = []
        for url in external:
            url_lower = url.lower()
            score = sum(1 for hint in _CAREERS_HINTS if hint in url_lower)
            scored.append((score, url))
        scored.sort(key=lambda x: -x[0])
        best_score, best_url = scored[0]
        if best_score > 0:
            return best_url
        # No careers-like URL — fall back to first external or email
        return apply_emails[0] if apply_emails else external[0]

    return apply_emails[0] if apply_emails else None


def _make_company(name: str, website: str, domain: str, size: str = "unknown") -> Company:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "website": website,
        "linkedin_url": "",
        "size": size,
        "domain": domain,
        "tech_stack": [],
        "open_role_count": 1,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }


def _make_job(company_id: str, company_name: str, title: str,
              description: str, apply_url: str, source: str,
              location: str = "Remote") -> JobListing:
    return {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "company_name": company_name,
        "title": title,
        "description": description,
        "required_skills": [],
        "nice_to_have_skills": [],
        "location": location,
        "remote": "remote" in location.lower() or source in ("remoteok", "hn_hiring"),
        "salary_min": None,
        "salary_max": None,
        "apply_url": apply_url,
        "source": source,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Source 1: Hacker News "Who's Hiring" ──────────────────────────────────

HN_API = "https://hacker-news.firebaseio.com/v0"

async def _fetch_hn_hiring(client: httpx.AsyncClient, keywords: list[str]) -> tuple[list[Company], list[JobListing]]:
    """
    Pulls the latest HN 'Who's Hiring' thread and parses top-level comments.
    Each comment is one company's hiring post.
    """
    companies: list[Company] = []
    jobs: list[JobListing] = []

    try:
        # Get whoishiring user's recent posts to find the latest thread
        resp = await client.get(f"{HN_API}/user/whoishiring.json", timeout=10)
        submitted = resp.json().get("submitted", [])[:5]  # check last 5 posts

        thread_id = None
        for item_id in submitted:
            item_resp = await client.get(f"{HN_API}/item/{item_id}.json", timeout=10)
            item = item_resp.json()
            if item and "who is hiring" in (item.get("title") or "").lower():
                thread_id = item_id
                break

        if not thread_id:
            logger.warning("hn_thread_not_found")
            return companies, jobs

        # Fetch the thread
        thread_resp = await client.get(f"{HN_API}/item/{thread_id}.json", timeout=10)
        thread = thread_resp.json()
        comment_ids = (thread.get("kids") or [])[:200]  # top 200 comments

        logger.info("hn_thread_found", thread_id=thread_id, comments=len(comment_ids))

        # Fetch comments in batches of 20
        for i in range(0, len(comment_ids), 20):
            batch = comment_ids[i:i + 20]
            tasks = [client.get(f"{HN_API}/item/{cid}.json", timeout=8) for cid in batch]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            for resp in responses:
                if isinstance(resp, Exception):
                    continue
                try:
                    comment = resp.json()
                except Exception:
                    continue

                text = comment.get("text") or ""
                if not text or not _is_relevant(text, keywords):
                    continue

                # Clean HTML tags and decode HTML entities
                clean = re.sub(r"<[^>]+>", " ", text).strip()
                clean = clean.replace("&#x2F;", "/").replace("&amp;", "&").replace("&#x27;", "'").replace("&gt;", ">").replace("&lt;", "<")

                # HN format: "Company | Role Title | Location | Type | Salary"
                first_line = clean.split("\n")[0][:300]
                parts = [p.strip() for p in first_line.split("|")]
                company_name = parts[0].strip() if parts else "Unknown"
                if not company_name or len(company_name) > 80:
                    continue

                # parts[1] = role title, parts[2] = location
                role_title = parts[1] if len(parts) > 1 else "Software Engineer"
                location    = parts[2] if len(parts) > 2 else "Remote"

                # Extract direct application URL from post body; fall back to HN URL
                hn_url      = f"https://news.ycombinator.com/item?id={comment.get('id')}"
                apply_url   = _extract_apply_url(text) or hn_url

                company = _make_company(
                    name=company_name,
                    website=apply_url,
                    domain="Tech",
                    size="startup",
                )
                job = _make_job(
                    company_id=company["id"],
                    company_name=company_name,
                    title=role_title,
                    description=clean[:2000],
                    apply_url=apply_url,
                    source="hn_hiring",
                    location=location,
                )
                companies.append(company)
                jobs.append(job)

            await asyncio.sleep(0.5)

    except Exception as e:
        logger.error("hn_hiring_failed", error=str(e))

    logger.info("hn_hiring_complete", companies=len(companies), jobs=len(jobs))
    return companies, jobs


# ── Source 2: RemoteOK API ─────────────────────────────────────────────────

REMOTEOK_API = "https://remoteok.com/api"

async def _fetch_remoteok(client: httpx.AsyncClient, keywords: list[str]) -> tuple[list[Company], list[JobListing]]:
    """Pulls RemoteOK's free JSON API. No auth required."""
    companies: list[Company] = []
    jobs: list[JobListing] = []

    try:
        resp = await client.get(
            REMOTEOK_API,
            headers={"User-Agent": "Mozilla/5.0 (job search bot; educational)"},
            timeout=15,
        )
        data = resp.json()
        # First element is a meta object, rest are job listings
        listings = [item for item in data if isinstance(item, dict) and item.get("company")]

        logger.info("remoteok_fetched", total_listings=len(listings))

        for item in listings:
            title = item.get("position") or ""
            description = item.get("description") or ""
            tags = item.get("tags") or []
            tag_text = " ".join(tags).lower()
            combined = f"{title} {description} {tag_text}".lower()

            if not _is_relevant(combined, keywords):
                continue

            company_name = item.get("company") or "Unknown"
            company = _make_company(
                name=company_name,
                website=item.get("url") or "https://remoteok.com",
                domain=_infer_domain_from_tags(tags),
                size="unknown",
            )
            job = _make_job(
                company_id=company["id"],
                company_name=company_name,
                title=title,
                description=description[:2000],
                apply_url=item.get("apply_url") or item.get("url") or "",
                source="remoteok",
                location="Remote",
            )
            job["required_skills"] = [t for t in tags if len(t) < 30][:10]
            job["salary_min"] = _parse_salary(item.get("salary_min"))
            job["salary_max"] = _parse_salary(item.get("salary_max"))

            companies.append(company)
            jobs.append(job)

    except Exception as e:
        logger.error("remoteok_failed", error=str(e))

    logger.info("remoteok_complete", companies=len(companies), jobs=len(jobs))
    return companies, jobs


# ── Source 3: Y Combinator Jobs ───────────────────────────────────────────

YC_JOBS_URL = "https://news.ycombinator.com/jobs"

async def _fetch_yc_jobs(client: httpx.AsyncClient, keywords: list[str]) -> tuple[list[Company], list[JobListing]]:
    """Scrapes YC's job board for YC-backed startups actively hiring."""
    companies: list[Company] = []
    jobs: list[JobListing] = []

    try:
        resp = await client.get(YC_JOBS_URL, timeout=15, follow_redirects=True)
        html = resp.text

        # Extract job entries: <tr class="athing"> blocks
        # Title pattern: <a href="item?id=...">Title at Company</a>
        pattern = re.compile(
            r'href="(item\?id=\d+)"[^>]*>([^<]+)</a>',
            re.IGNORECASE,
        )
        matches = pattern.findall(html)

        for href, title_text in matches:
            if not _is_relevant(title_text, keywords):
                continue

            # "Senior Data Engineer at Acme" → split on " at "
            if " at " in title_text:
                role, _, company_name = title_text.partition(" at ")
            else:
                role = title_text
                company_name = "Unknown"

            company_name = company_name.strip()[:80]
            role = role.strip()[:100]

            company = _make_company(
                name=company_name,
                website=f"https://news.ycombinator.com/{href}",
                domain="Tech",
                size="startup",
            )
            job = _make_job(
                company_id=company["id"],
                company_name=company_name,
                title=role,
                description=title_text,
                apply_url=f"https://news.ycombinator.com/{href}",
                source="yc_jobs",
                location="Remote",
            )
            companies.append(company)
            jobs.append(job)

    except Exception as e:
        logger.error("yc_jobs_failed", error=str(e))

    logger.info("yc_jobs_complete", companies=len(companies), jobs=len(jobs))
    return companies, jobs


# ── Source 4: Recent Funding News (via DuckDuckGo) ────────────────────────

async def _fetch_funded_companies(keywords: list[str]) -> list[Company]:
    """
    Search for recently funded tech companies — they almost always hire
    within 3–6 months of a raise.
    """
    from tools.search import get_search_client
    search = get_search_client()
    companies: list[Company] = []

    queries = [
        "AI startup raised Series A B 2025 hiring ML platform engineer LLM",
        "GenAI LLM startup funded 2025 hiring MLOps AI infrastructure engineer",
        "site:techcrunch.com AI startup funded 2025 Series A B hiring engineers",
        "machine learning platform company raised 2025 hiring Python Kubernetes",
    ]

    for query in queries:
        results = await search.search(query, max_results=5)
        for r in results:
            title = r.get("title", "")
            content = r.get("content", "")
            combined = f"{title} {content}".lower()

            if not _is_relevant(combined, keywords):
                continue

            # Extract company name from headline: "Acme raises $20M Series B..."
            match = re.match(r"^([A-Z][A-Za-z0-9\s\.\-]{2,40})\s+(raises|secures|closes|announces)", title)
            if match:
                name = match.group(1).strip()
                company = _make_company(
                    name=name,
                    website=r.get("url", ""),
                    domain="Tech",
                    size="startup",
                )
                companies.append(company)

        await asyncio.sleep(1)

    logger.info("funding_news_complete", companies=len(companies))
    return companies


# ── Utilities ──────────────────────────────────────────────────────────────

def _infer_domain_from_tags(tags: list[str]) -> str:
    tags_lower = [t.lower() for t in tags]
    if any(t in tags_lower for t in ["ai", "ml", "machine-learning", "llm", "gpt"]):
        return "AI/ML"
    if any(t in tags_lower for t in ["fintech", "finance", "payments", "crypto"]):
        return "FinTech"
    if any(t in tags_lower for t in ["devops", "platform", "kubernetes", "infrastructure"]):
        return "Developer Tools"
    if any(t in tags_lower for t in ["data", "spark", "databricks", "snowflake"]):
        return "Data Infrastructure"
    return "Tech"


def _parse_salary(val) -> int | None:
    if not val:
        return None
    try:
        return int(str(val).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _deduplicate(
    new_items: list[dict],
    existing_names: set[str],
    name_key: str = "name",
) -> list[dict]:
    seen: set[str] = set()
    result = []
    for item in new_items:
        name = item.get(name_key, "").lower().strip()
        if name and name not in existing_names and name not in seen:
            seen.add(name)
            result.append(item)
    return result


# ── Main entry point ───────────────────────────────────────────────────────

async def run(state: JobSearchState) -> JobSearchState:
    """
    Pull all four hiring feed sources concurrently and merge results into state.
    """
    profile = _load_profile()
    keywords = _target_keywords(profile)
    blacklist = set(profile.get("preferences", {}).get("blacklist_companies", []) or [])

    existing_company_names = {c["name"].lower() for c in state.get("companies", [])}
    existing_job_ids = {j["apply_url"] for j in state.get("job_listings", [])}

    memory = Memory.get()

    logger.info("hiring_feed_start", keywords=keywords[:5])

    async with httpx.AsyncClient(follow_redirects=True) as client:
        hn_task = _fetch_hn_hiring(client, keywords)
        remoteok_task = _fetch_remoteok(client, keywords)
        yc_task = _fetch_yc_jobs(client, keywords)

        (hn_companies, hn_jobs), (ro_companies, ro_jobs), (yc_companies, yc_jobs) = \
            await asyncio.gather(hn_task, remoteok_task, yc_task)

    funded_companies = await _fetch_funded_companies(keywords)

    # Merge all companies
    all_new_companies = hn_companies + ro_companies + yc_companies + funded_companies
    all_new_companies = [
        c for c in all_new_companies
        if not any(b.lower() in c["name"].lower() for b in blacklist if b)
    ]
    clean_companies = _deduplicate(all_new_companies, existing_company_names)

    # Merge all jobs (deduplicate by apply_url)
    all_new_jobs = hn_jobs + ro_jobs + yc_jobs
    clean_jobs = [j for j in all_new_jobs if j["apply_url"] not in existing_job_ids]
    seen_urls: set[str] = set()
    deduped_jobs = []
    for j in clean_jobs:
        if j["apply_url"] not in seen_urls:
            seen_urls.add(j["apply_url"])
            deduped_jobs.append(j)

    # Persist to memory
    for c in clean_companies:
        memory.add_company(c["id"], c)
    for j in deduped_jobs:
        memory.add_job_listing(j["id"], j["description"][:500], {
            "company_name": j["company_name"],
            "title": j["title"],
            "source": j["source"],
            "apply_url": j["apply_url"],
        })

    logger.log_decision(
        action="hiring_feeds_merged",
        outcome="complete",
        details={
            "hn_hiring": len(hn_companies),
            "remoteok": len(ro_companies),
            "yc_jobs": len(yc_companies),
            "funded_news": len(funded_companies),
            "new_companies_added": len(clean_companies),
            "new_jobs_added": len(deduped_jobs),
        },
    )

    updated = dict(state)
    updated["companies"] = state.get("companies", []) + clean_companies
    updated["job_listings"] = state.get("job_listings", []) + deduped_jobs
    return updated


# ── CLI smoke test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    async def _test():
        empty_state: JobSearchState = {
            "companies": [], "job_listings": [], "fit_scores": [],
            "applications": [], "pending_approvals": [], "recruiter_contacts": [],
            "email_events": [], "meetings_scheduled": [],
            "weekly_quality_score": 0.0, "weekly_quantity_count": 0,
            "response_rate": 0.0, "interview_rate": 0.0,
            "last_run_at": None, "errors": [], "agent_statuses": {},
        }
        result = await run(empty_state)
        print(f"\nNew companies found: {len(result['companies'])}")
        print(f"New job listings:    {len(result['job_listings'])}")
        print("\n── Sample companies ──")
        for c in result["companies"][:10]:
            print(f"  [{c['size']:8s}] {c['name']}")
        print("\n── Sample jobs ──")
        for j in result["job_listings"][:10]:
            print(f"  {j['title']:40s} @ {j['company_name']:25s}  [{j['source']}]")

    asyncio.run(_test())
