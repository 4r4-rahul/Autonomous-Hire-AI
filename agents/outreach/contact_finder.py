"""
LinkedIn Contact Finder Agent.
For each company with a job listing that passed the fit threshold, find 1-2
relevant contacts (hiring managers, engineering leads, recruiters).
Adds them to state as LinkedInContact records with cadence_stage=0.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

from core.config import get_settings
from core.llm import get_llm
from core.logger import get_logger
from core.rate_limiter import get_rate_limiter
from core.state import Application, LinkedInContact, JobSearchState
from tools.humanize import page_think_pause, reading_pause, scroll_down_human
from tools.linkedin_browser import LinkedInBrowser

logger = get_logger("contact_finder")

_SYSTEM_PROMPT = Path("prompts/system/contact_finder.txt").read_text() if Path("prompts/system/contact_finder.txt").exists() else ""

# Titles that indicate decision-maker or relevant contact
_TARGET_TITLE_KEYWORDS = [
    "hiring manager", "engineering manager", "staff engineer", "principal engineer",
    "vp engineering", "director of engineering", "head of engineering",
    "head of data", "director of data", "data engineering manager",
    "platform engineering", "tech lead", "recruiter", "talent", "technical recruiter",
    "engineering recruiter",
]

_MAX_CONTACTS_PER_COMPANY = 2


async def _is_paused() -> bool:
    settings = get_settings()
    if settings.linkedin_pause:
        logger.info("contact_finder_paused_kill_switch")
        return True
    hour = datetime.now().hour
    if not (settings.linkedin_active_hours_start <= hour < settings.linkedin_active_hours_end):
        return True
    return False


def _is_relevant_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _TARGET_TITLE_KEYWORDS)


async def _search_people_at_company(
    browser: LinkedInBrowser,
    company_name: str,
) -> list[dict]:
    """Search LinkedIn for contacts at a company. Returns raw card data."""
    limiter = get_rate_limiter()
    if not limiter.check_and_consume("linkedin_search"):
        logger.info("rate_limited_people_search", company=company_name)
        return []

    # LinkedIn people search URL — filter by current company name
    from urllib.parse import quote_plus
    search_url = (
        "https://www.linkedin.com/search/results/people/"
        f"?keywords={quote_plus(company_name)}"
        "&origin=GLOBAL_SEARCH_HEADER"
        "&sid=contact_finder"
    )

    page = await browser.new_page()
    contacts = []

    try:
        await browser.goto(page, search_url)
        await page_think_pause()
        await scroll_down_human(page, 300)

        # Try to filter by current company
        try:
            await page.click("button[aria-label='Current company filter']", timeout=3000)
            await asyncio.sleep(1.0)
        except Exception:
            pass

        cards = await page.query_selector_all(".reusable-search__result-container, .entity-result")
        for card in cards[:20]:
            try:
                name_el = await card.query_selector(
                    ".entity-result__title-text a span[aria-hidden='true'], "
                    ".app-aware-link span[aria-hidden='true']"
                )
                title_el = await card.query_selector(".entity-result__primary-subtitle, .entity-result__secondary-subtitle")
                profile_el = await card.query_selector("a.app-aware-link[href*='/in/']")

                name = (await name_el.inner_text()).strip() if name_el else ""
                title = (await title_el.inner_text()).strip() if title_el else ""
                profile_url = await profile_el.get_attribute("href") if profile_el else ""

                # Clean up profile URL — strip query params
                if profile_url:
                    profile_url = re.sub(r"\?.*$", "", profile_url.split("?")[0])
                    if not profile_url.startswith("https://"):
                        profile_url = "https://www.linkedin.com" + profile_url

                if not name or not profile_url:
                    continue
                if not _is_relevant_title(title):
                    continue

                contacts.append({
                    "name": name,
                    "title": title,
                    "profile_url": profile_url,
                    "company": company_name,
                })
            except Exception:
                continue

    except Exception as e:
        logger.warning("people_search_failed", company=company_name, error=str(e))
    finally:
        await page.close()

    return contacts[:_MAX_CONTACTS_PER_COMPANY]


async def _score_contact_relevance(
    llm,
    contact: dict,
    job_title: str,
    company: str,
) -> int:
    """Use Haiku to score how relevant a contact is (0-100)."""
    try:
        result = await llm.chat_json(
            "You score how relevant a LinkedIn contact is for a job search outreach. "
            "Return JSON: {\"score\": 0-100, \"reason\": \"one sentence\"}",
            [{
                "role": "user",
                "content": (
                    f"Job seeker: Senior Data/Platform Engineer (6+ yrs, Python/AWS/Spark)\n"
                    f"Target role: {job_title} at {company}\n"
                    f"Contact: {contact['name']}, {contact['title']} at {contact['company']}\n"
                    f"Score relevance for warm outreach (hiring manager/tech lead = high, recruiter = medium)."
                ),
            }],
            max_tokens=128,
        )
        return int(result.get("score", 50))
    except Exception:
        return 50


def _build_contact(
    raw: dict,
    job_id: Optional[str],
    relevance: int,
) -> LinkedInContact:
    return {
        "id": str(uuid.uuid4()),
        "li_profile_url": raw["profile_url"],
        "full_name": raw["name"],
        "headline": raw["title"],
        "title": raw["title"],
        "company": raw["company"],
        "related_job_id": job_id,
        "relevance_score": relevance,
        "connection_status": "none",
        "cadence_stage": 0,
        "next_action_at": None,
        "last_action_at": None,
        "notes": "",
        "messages": [],
    }


def _companies_needing_contacts(state: JobSearchState) -> list[tuple[str, str, str]]:
    """
    Return list of (company_name, job_id, job_title) for approved applications
    or high-fit jobs that don't already have a contact in state.
    """
    existing_company_names = {c["company"].lower() for c in state.get("linkedin_contacts", [])}

    targets = []
    for app in state.get("applications", []):
        if app["status"] in ("approved", "submitted"):
            if app["company_name"].lower() not in existing_company_names:
                targets.append((app["company_name"], app["job_id"], app["role_title"]))
                existing_company_names.add(app["company_name"].lower())

    # Also target companies with fit score ≥ 80 even without an application yet
    job_map = {j["id"]: j for j in state.get("job_listings", [])}
    for fs in state.get("fit_scores", []):
        if fs["score"] >= 80 and fs["passed_threshold"]:
            job = job_map.get(fs["job_id"])
            if job and job["company_name"].lower() not in existing_company_names:
                targets.append((job["company_name"], job["id"], job["title"]))
                existing_company_names.add(job["company_name"].lower())

    return targets[:10]  # cap to 10 companies per run


async def run(state: JobSearchState) -> JobSearchState:
    """Find LinkedIn contacts for target companies and add them to state."""
    if await _is_paused():
        return state

    targets = _companies_needing_contacts(state)
    if not targets:
        logger.info("contact_finder_no_targets")
        return state

    llm = get_llm("contact_finder", model="claude-haiku-4-5")
    new_contacts: list[LinkedInContact] = []

    try:
        async with LinkedInBrowser() as browser:
            page = await browser.new_page()
            if not await browser.is_logged_in(page):
                logger.warning("linkedin_not_logged_in")
                await page.close()
                return state
            await page.close()

            for company_name, job_id, job_title in targets:
                logger.info("searching_contacts", company=company_name)
                raw_contacts = await _search_people_at_company(browser, company_name)

                for raw in raw_contacts:
                    relevance = await _score_contact_relevance(llm, raw, job_title, company_name)
                    if relevance < 40:
                        logger.info("contact_low_relevance", name=raw["name"], score=relevance)
                        continue
                    contact = _build_contact(raw, job_id, relevance)
                    new_contacts.append(contact)
                    logger.log_decision(
                        action="contact_found",
                        outcome="queued",
                        details={
                            "name": raw["name"],
                            "title": raw["title"],
                            "company": company_name,
                            "relevance": relevance,
                        },
                    )

                # Pause between companies — 30-60 seconds
                await asyncio.sleep(30 + (hash(company_name) % 30))

    except Exception as e:
        logger.error("contact_finder_failed", error=str(e))

    logger.log_decision(
        action="contact_finder_complete",
        outcome="success",
        details={"new_contacts": len(new_contacts)},
    )

    updated = dict(state)
    updated["linkedin_contacts"] = state.get("linkedin_contacts", []) + new_contacts
    return updated
