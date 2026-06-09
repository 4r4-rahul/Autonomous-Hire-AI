"""
Company Finder Agent
Discovers tech companies via two complementary sources:

  1. DuckDuckGo web search + LLM extraction  → private startups & scaleups
  2. Financial screener (yfinance)            → public companies with strong
                                               revenue growth, profit margins,
                                               and market cap expansion

Both sources feed into the same Company list, deduplicated by name.
"""

import uuid
import asyncio
import yaml
from pathlib import Path
from typing import Any

from core.state import Company, JobSearchState
from core.memory import Memory
from core.llm import get_llm
from core.logger import get_logger
from core.stealth import throttle, is_off_hours
from tools.search import get_search_client

logger = get_logger("company_finder")
_SYSTEM_PROMPT = Path("prompts/system/company_finder.txt").read_text()
_PROFILE_PATH = Path("data/user_profile.yaml")


def _load_profile() -> dict:
    return yaml.safe_load(_PROFILE_PATH.read_text())


def _build_search_queries(profile: dict) -> list[str]:
    """Generate diverse search queries from the user's target criteria."""
    targets = profile.get("target", {})
    roles = targets.get("roles", [])
    domains = targets.get("domains", [])
    locations = targets.get("locations", [])
    sizes = targets.get("company_sizes", [])

    size_map = {
        "startup": "startup",
        "scaleup": "Series B C D startup",
        "mid": "mid-size tech company",
        "large": "enterprise tech company",
    }
    size_terms = [size_map.get(s, s) for s in sizes]

    queries: list[str] = []

    # Domain + size combos
    for domain in domains:
        for size in size_terms[:2]:  # avoid combinatorial explosion
            queries.append(f"hiring {size} {domain} companies {' OR '.join(roles[:2])} 2024 2025")

    # Location-specific
    for loc in locations:
        if loc.lower() != "remote":
            queries.append(f"tech companies hiring {' OR '.join(roles[:2])} {loc} 2025")

    # Remote-first
    queries.append(f"remote-first tech companies hiring {' OR '.join(roles[:2])} {' '.join(domains[:2])}")

    # AI/ML focus
    queries.append("AI ML platform engineering companies hiring senior engineers 2025")
    queries.append("LLM infrastructure data platform companies Series B C hiring 2025")

    return queries[:8]  # cap at 8 to stay within rate limits


async def _extract_company(llm, search_snippet: str, url: str) -> dict | None:
    """Use LLM to extract structured company data from a search result snippet."""
    messages = [
        {
            "role": "user",
            "content": f"Extract company data from this search result.\n\nURL: {url}\n\nContent:\n{search_snippet[:1500]}",
        }
    ]
    try:
        result = await llm.chat_json(_SYSTEM_PROMPT, messages)
        if isinstance(result, dict) and result.get("name"):
            return result
    except Exception as e:
        logger.error("company_extraction_failed", url=url, error=str(e))
    return None


async def run(state: JobSearchState) -> JobSearchState:
    """
    Main entry point for the company_finder agent.
    Discovers companies, deduplicates against memory, updates state.
    """
    if is_off_hours():
        logger.info("skipping_off_hours")
        return state

    profile = _load_profile()
    blacklist = set(profile.get("preferences", {}).get("blacklist_companies", []) or [])
    existing_names = {c["name"].lower() for c in state.get("companies", [])}

    search = get_search_client()
    llm = get_llm("company_finder", model="claude-haiku-4-5")
    memory = Memory.get()

    queries = _build_search_queries(profile)
    new_companies: list[Company] = []

    logger.info("company_finder_start", query_count=len(queries))

    for query in queries:
        await throttle("search_queries")
        results = await search.search(query, max_results=5, search_depth="advanced")

        for result in results:
            url = result.get("url", "")
            content = result.get("content", "")
            title = result.get("title", "")

            if not content:
                continue

            snippet = f"Title: {title}\nContent: {content}"
            data = await _extract_company(llm, snippet, url)

            if not data:
                continue

            name = data.get("name", "").strip()
            if not name:
                continue

            # Dedup
            if name.lower() in existing_names:
                continue
            if any(b.lower() in name.lower() for b in blacklist if b):
                logger.info("company_blacklisted", name=name)
                continue

            company: Company = {
                "id": str(uuid.uuid4()),
                "name": name,
                "website": data.get("website") or url,
                "linkedin_url": data.get("linkedin_url") or "",
                "size": data.get("size") or "unknown",
                "domain": data.get("domain") or "",
                "tech_stack": data.get("tech_stack") or [],
                "open_role_count": data.get("open_role_count") or 0,
                "discovered_at": __import__("datetime").datetime.utcnow().isoformat(),
            }

            # Persist to memory
            memory.add_company(company["id"], company)
            new_companies.append(company)
            existing_names.add(name.lower())

            logger.log_decision(
                action="company_discovered",
                outcome="added",
                details={"name": name, "size": company["size"], "domain": company["domain"]},
            )

        await throttle("company_page_visits")

    logger.info(
        "company_finder_complete",
        new_companies=len(new_companies),
        total_companies=len(state.get("companies", [])) + len(new_companies),
    )

    updated_state = dict(state)
    updated_state["companies"] = state.get("companies", []) + new_companies
    return updated_state


async def run_all(state: JobSearchState) -> JobSearchState:
    """
    Master discovery entry point. Three sources run in parallel:

      1. Web search (DuckDuckGo + LLM)    — private startups & scaleups
      2. Financial screener (yfinance)    — public companies in expansion mode
      3. Hiring feed scanner              — HN Who's Hiring, RemoteOK, YC Jobs,
                                           recent funding news

    Companies are merged and deduplicated. Financially-scored companies
    are ranked first. Hiring feed results also inject early JobListing hints.
    """
    from agents.discovery.financial_screener import run as financial_run
    from agents.discovery.hiring_feed_scanner import run as feed_run

    logger.info("company_discovery_start", sources=["web_search", "financial_screener", "hiring_feeds"])

    # All three sources run concurrently
    web_state, fin_state, feed_state = await asyncio.gather(
        run(state),
        financial_run(state),
        feed_run(state),
    )

    # Merge companies — financial first (ranked by score), then web, then feeds
    merged: list[dict] = []
    existing_names: set[str] = set()

    def _add_unique(companies: list[dict]) -> None:
        for c in companies:
            if c["name"].lower() not in existing_names:
                merged.append(c)
                existing_names.add(c["name"].lower())

    # Financial screener results sorted by hiring score
    fin_sorted = sorted(
        fin_state.get("companies", []),
        key=lambda c: c.get("_financial", {}).get("hiring_score", 0),  # type: ignore[attr-defined]
        reverse=True,
    )
    _add_unique(fin_sorted)
    _add_unique(web_state.get("companies", []))
    _add_unique(feed_state.get("companies", []))

    # Merge early job listing hints from hiring feeds (job_scanner enriches these later)
    existing_job_urls = {j["apply_url"] for j in state.get("job_listings", [])}
    new_jobs = [
        j for j in feed_state.get("job_listings", [])
        if j["apply_url"] not in existing_job_urls
    ]

    logger.log_decision(
        action="company_discovery_complete",
        outcome="merged",
        details={
            "web_search": len(web_state.get("companies", [])),
            "financial_screener": len(fin_state.get("companies", [])),
            "hiring_feeds": len(feed_state.get("companies", [])),
            "total_unique_companies": len(merged),
            "early_job_hints": len(new_jobs),
        },
    )

    updated_state = dict(state)
    updated_state["companies"] = merged
    updated_state["job_listings"] = state.get("job_listings", []) + new_jobs
    return updated_state


# ── CLI smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio

    async def _test():
        empty_state: JobSearchState = {
            "companies": [], "job_listings": [], "fit_scores": [],
            "applications": [], "pending_approvals": [], "recruiter_contacts": [],
            "email_events": [], "meetings_scheduled": [],
            "weekly_quality_score": 0.0, "weekly_quantity_count": 0,
            "response_rate": 0.0, "interview_rate": 0.0,
            "last_run_at": None, "errors": [], "agent_statuses": {},
        }
        result = await run_all(empty_state)
        print(f"\nTotal companies found: {len(result['companies'])}")
        print("\n── Financial screener results (sorted by hiring score) ──")
        for c in result["companies"]:
            fin = c.get("_financial", {})
            if fin:
                print(f"  {c['name']:35s} score={fin.get('hiring_score'):5}  "
                      f"rev_growth={fin.get('revenue_growth_pct')}%  "
                      f"employees={fin.get('employees')}")
        print("\n── Web search results ──")
        for c in result["companies"]:
            if not c.get("_financial"):
                print(f"  {c['name']} ({c['size']}) — {c['domain']}")

    asyncio.run(_test())
