"""
LinkedIn Job Scanner Agent.
Searches LinkedIn for tech roles matching the user profile and feeds them into
the existing JobListing pipeline. Respects rate limits and the LI_PAUSE kill switch.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import Page

from core.config import get_settings
from core.logger import get_logger
from core.rate_limiter import get_rate_limiter
from core.state import JobListing, JobSearchState
from core.stealth import is_off_hours
from tools.humanize import (page_think_pause, reading_pause, scroll_down_human,
                            move_mouse_human, session_idle)
from tools.linkedin_browser import LinkedInBrowser

logger = get_logger("linkedin_scanner")

# Signals LinkedIn is pushing back (soft block / challenge / rate limit).
_THROTTLE_SIGNALS = ("ERR_TOO_MANY_REDIRECTS", "ERR_HTTP_RESPONSE_CODE_FAILURE",
                     "ERR_FAILED", "ERR_ABORTED", "status code 999", "too many redirects",
                     "checkpoint", "authwall")
_THROTTLE_COUNT = Path("data/li_throttle_count.json")


# ── Human-session primitives ──────────────────────────────────────────────────
async def _human_click(page: Page, element) -> bool:
    """Move the mouse to an element along a human curve and click it — NEVER page.goto to a
    job URL (direct job-view navigation is the #1 automation tell that triggers LinkedIn's
    redirect-to-challenge loop). A human clicks the card; the JD loads in the right pane."""
    try:
        box = await element.bounding_box()
        if box:
            cx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            cy = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await move_mouse_human(page, cx, cy)
            await asyncio.sleep(random.uniform(0.08, 0.28))
        await element.click()
        return True
    except Exception:
        try:
            await element.click()
            return True
        except Exception:
            return False


async def _warm_up(browser: LinkedInBrowser) -> None:
    """Start the session like a person: glance at the feed and scroll a bit BEFORE going to
    jobs. Logging in and immediately hammering job pages is a dead giveaway."""
    page = await browser.new_page()
    try:
        await browser.goto(page, "https://www.linkedin.com/feed/")
        await page_think_pause()
        await scroll_down_human(page, random.randint(500, 1400))
        await asyncio.sleep(random.uniform(2.0, 6.0))
        await scroll_down_human(page, random.randint(300, 900))
        await asyncio.sleep(random.uniform(1.0, 4.0))
        logger.info("li_warmup_done")
    except Exception as e:
        logger.info("li_warmup_skipped", error=str(e)[:60])
    finally:
        await page.close()


async def _read_detail_pane(page: Page) -> str:
    """Read the JD from the right-hand detail pane that loads after clicking a card."""
    try:
        await page.wait_for_selector(
            ".jobs-search__job-details, .jobs-details__main-content, #job-details, "
            ".jobs-description__content", timeout=8000)
    except Exception:
        return ""
    el = await page.query_selector(
        ".jobs-description__content, .jobs-box__html-content, #job-details, "
        ".jobs-details__main-content")
    if not el:
        return ""
    try:
        await scroll_down_human(page, random.randint(300, 700))
        return (await el.inner_text()).strip()[:4000]
    except Exception:
        return ""


def _escalating_backoff(reason: str) -> None:
    """On a block signal, auto-pause with an ESCALATING duration: 2h → 6h → 24h. Resets on a
    clean session. Stops the pipeline from hammering an account LinkedIn is already watching."""
    from agents.monitoring.linkedin_health import _write_pause
    try:
        n = json.loads(_THROTTLE_COUNT.read_text()).get("n", 0) if _THROTTLE_COUNT.exists() else 0
    except Exception:
        n = 0
    n += 1
    try:
        _THROTTLE_COUNT.write_text(json.dumps({"n": n, "at": datetime.now(timezone.utc).isoformat()}))
    except Exception:
        pass
    hours = {1: 2, 2: 6}.get(n, 24)
    _write_pause(f"{reason} (backoff #{n}, {hours}h)", duration_hours=hours)
    logger.warning("li_escalating_backoff", count=n, hours=hours, reason=reason)


def _reset_throttle() -> None:
    """A clean session clears the escalation counter."""
    try:
        if _THROTTLE_COUNT.exists():
            _THROTTLE_COUNT.unlink()
    except Exception:
        pass


def _is_throttle(msg: str) -> bool:
    return any(s in msg for s in _THROTTLE_SIGNALS)

_PROFILE_PATH = Path("data/user_profile.yaml")

# Company name substrings that indicate staffing agencies, recruiting firms, or
# gig-work platforms — not real employers with ATS portals.
_JUNK_COMPANY_PATTERNS: list[str] = [
    "staffing", "recruiting", "recruitment", "talent solutions", "talent group",
    "quik hire", "quick hire", "hire solutions", "hire group",
    "infojini", "kforce", "tek systems", "teksystems", "insight global",
    "robert half", "randstad", "adecco", "manpower", "hays", "cognizant",
    "wipro", "infosys", "tata consultancy", "accenture federal",
    "outlier", "scale ai",  # gig/data-labeling platforms, not employment
]

# ATS URL domain patterns that are job-board trackers or gig platforms, not real ATS forms.
_JUNK_ATS_DOMAINS: list[str] = [
    "jometer.com", "jobleads.com", "zipapply", "indeed.com/applystart",
    "app.joinhandshake.com", "outlier.ai", "app.outlier.ai",
    "remotehub.com", "remote.co/job-listings",
]

# LinkedIn Jobs search base — remote + US + posted in past week
_LI_JOBS_URL = (
    "https://www.linkedin.com/jobs/search/"
    "?keywords={keywords}"
    "&location=United+States"
    "&f_WT=2"           # Remote
    "&f_TPR=r604800"    # Past week
    "&sortBy=DD"        # Most recent
)

_TECH_TITLE_KEYWORDS = [
    "engineer", "developer", "architect", "devops", "sre", "platform",
    "data", "ml ", "machine learning", "ai ", "backend", "fullstack",
    "full-stack", "cloud", "infrastructure", "mlops", "staff", "principal",
    "analytics", "scientist",
]


async def _is_paused() -> bool:
    settings = get_settings()
    if settings.linkedin_pause:
        logger.info("linkedin_paused_kill_switch")
        return True
    hour = datetime.now().hour
    if not (settings.linkedin_active_hours_start <= hour < settings.linkedin_active_hours_end):
        logger.info("linkedin_outside_active_hours", hour=hour)
        return True
    return False


def _vendor_allowlist() -> list[str]:
    """Lowercased browser-gated vendor names — these are DESIRED (we search LinkedIn for them), so
    their roles must BYPASS _JUNK_COMPANY_PATTERNS (which otherwise drops any 'X Staffing' name)."""
    try:
        import json
        cfg = json.loads((_PROFILE_PATH.parent / "browser_gated_vendors.json").read_text())
        return [v["name"].lower() for v in cfg.get("vendors", []) if v.get("name")]
    except Exception:
        return []


_VENDOR_ALLOW = _vendor_allowlist()


def _browser_gated_vendor_queries() -> list[str]:
    """Company-scoped LinkedIn queries for staffing firms whose OWN portals are browser-gated
    (JobDiva/iCIMS/Bullhorn/custom — not HTTP-scrapable). They SYNDICATE to LinkedIn, so a bounded
    set of "<company> engineer" searches surfaces their roles here instead of via fragile scraping.
    Bounded (linkedin_probe, default 8) to keep LinkedIn request volume/account-risk low."""
    try:
        import json
        cfg = json.loads((_PROFILE_PATH.parent / "browser_gated_vendors.json").read_text())
        n = int(cfg.get("linkedin_probe", 8))
        return [quote_plus(f"{v['name']} engineer") for v in cfg.get("vendors", [])[:n] if v.get("name")]
    except Exception:
        return []


def _load_search_queries() -> list[str]:
    """Build search query strings from user profile target roles + browser-gated vendor probes."""
    try:
        profile = yaml.safe_load(_PROFILE_PATH.read_text())
        roles = profile.get("target", {}).get("roles", [])[:10]
        base = [quote_plus(r) for r in roles]
    except Exception:
        base = [quote_plus("Senior Data Engineer"), quote_plus("Staff Platform Engineer")]
    return base + _browser_gated_vendor_queries()


def _is_tech_role(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _TECH_TITLE_KEYWORDS)


def _build_job(
    title: str,
    company: str,
    location: str,
    description: str,
    apply_url: str,
    li_job_id: str,
) -> JobListing:
    return {
        "id": str(uuid.uuid4()),
        "company_id": "",           # no company record yet; fit_analyst will match/create
        "company_name": company,
        "title": title,
        "description": description[:4000],
        "required_skills": [],
        "nice_to_have_skills": [],
        "location": location or "Unknown",
        "remote": any(w in (location or "").lower() for w in ["remote", "anywhere", "distributed"]),
        "salary_min": None,
        "salary_max": None,
        "apply_url": apply_url,
        "source": "linkedin",
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }


async def _extract_job_cards(page: Page) -> list[dict]:
    """Parse visible job cards from the LinkedIn search results page."""
    await page.wait_for_selector("ul.jobs-search__results-list, .scaffold-layout__list", timeout=15_000)
    cards = await page.query_selector_all("li.jobs-search-results__list-item, .job-card-container")

    jobs = []
    for card in cards[:25]:  # cap at 25 per query to stay within rate limits
        try:
            title_el = await card.query_selector("a.job-card-list__title, .job-card-container__link")
            company_el = await card.query_selector(".job-card-container__company-name, .artdeco-entity-lockup__subtitle")
            location_el = await card.query_selector(".job-card-container__metadata-wrapper li, .artdeco-entity-lockup__caption")
            href = await title_el.get_attribute("href") if title_el else None
            _raw_title = (await title_el.inner_text()).strip() if title_el else ""
            # LinkedIn occasionally injects "with verification" or duplicates the title
            # on a second line via accessibility markup — take only the first line.
            title = _raw_title.splitlines()[0].strip() if _raw_title else ""
            title = re.sub(r"\s+with verification\s*$", "", title, flags=re.IGNORECASE).strip()
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""

            if not title or not _is_tech_role(title):
                continue

            # Detect Easy Apply — scan the whole card text + aria labels
            # (LinkedIn frequently changes CSS class names, so don't rely on selectors alone)
            easy_apply = False
            try:
                card_text = (await card.inner_text()).lower()
                easy_apply = "easy apply" in card_text
                if not easy_apply:
                    # Also check aria-label on any button inside the card
                    btns = await card.query_selector_all("button[aria-label]")
                    for btn in btns:
                        label = (await btn.get_attribute("aria-label") or "").lower()
                        if "easy apply" in label:
                            easy_apply = True
                            break
            except Exception:
                pass

            # Extract LinkedIn job ID from URL
            li_id = ""
            if href:
                m = re.search(r"/jobs/view/(\d+)", href)
                li_id = m.group(1) if m else ""

            jobs.append({
                "title": title,
                "company": company,
                "location": location,
                "li_id": li_id,
                "href": href or "",
                "easy_apply": easy_apply,
            })
        except Exception:
            continue
    return jobs


async def _card_meta(card) -> Optional[dict]:
    """Extract a job card's metadata (title/company/location/li_id/easy_apply) from its
    element handle — no navigation, just reading the already-rendered list."""
    try:
        title_el = await card.query_selector(
            "a.job-card-list__title, .job-card-container__link, a[href*='/jobs/view/']")
        company_el = await card.query_selector(
            ".job-card-container__company-name, .artdeco-entity-lockup__subtitle")
        location_el = await card.query_selector(
            ".job-card-container__metadata-wrapper li, .artdeco-entity-lockup__caption")
        href = await title_el.get_attribute("href") if title_el else None
        raw = (await title_el.inner_text()).strip() if title_el else ""
        title = raw.splitlines()[0].strip() if raw else ""
        title = re.sub(r"\s+with verification\s*$", "", title, flags=re.IGNORECASE).strip()
        company = (await company_el.inner_text()).strip() if company_el else ""
        location = (await location_el.inner_text()).strip() if location_el else ""
        if not title or not _is_tech_role(title):
            return None
        easy_apply = False
        try:
            easy_apply = "easy apply" in (await card.inner_text()).lower()
        except Exception:
            pass
        li_id = ""
        if href:
            m = re.search(r"/jobs/view/(\d+)", href)
            li_id = m.group(1) if m else ""
        return {"title": title, "company": company, "location": location,
                "li_id": li_id, "href": href or "", "easy_apply": easy_apply,
                "link_el": title_el}
    except Exception:
        return None


async def _search_query(
    browser: LinkedInBrowser,
    query: str,
    existing_apply_urls: set[str],
    max_jd: int = 8,
) -> list[JobListing]:
    """Run ONE search like a human: open results, scroll the list, then CLICK a handful of
    cards to read each JD in the right pane. Never navigates to a job-view URL directly."""
    limiter = get_rate_limiter()
    if not limiter.check_and_consume("linkedin_search"):
        logger.info("rate_limited_search", query=query)
        return []

    search_url = _LI_JOBS_URL.format(keywords=query)
    page = await browser.new_page()
    listings: list[JobListing] = []

    try:
        await browser.goto(page, search_url)
        await page_think_pause()
        # Human: scroll the results list to load cards before interacting.
        await scroll_down_human(page, random.randint(500, 1100))
        await asyncio.sleep(random.uniform(1.0, 3.0))

        try:
            await page.wait_for_selector(
                "li.jobs-search-results__list-item, .scaffold-layout__list-item, "
                ".job-card-container", timeout=15_000)
        except Exception:
            logger.info("li_no_results", query=query)
            return []
        card_els = await page.query_selector_all(
            "li.jobs-search-results__list-item, .scaffold-layout__list-item, .job-card-container")
        logger.info("li_cards_found", query=query, count=len(card_els))

        fetched = 0
        for card in card_els:
            if fetched >= max_jd:
                break
            meta = await _card_meta(card)
            if not meta:
                continue
            if meta["easy_apply"]:
                continue  # company-ATS only
            _co = meta["company"].lower()
            # Browser-gated staffing vendors are DESIRED here (we searched for them), so allow their
            # roles through even though the generic junk filter would drop a staffing/recruiting name.
            if not any(v in _co for v in _VENDOR_ALLOW) and any(p in _co for p in _JUNK_COMPANY_PATTERNS):
                continue
            apply_url = (f"https://www.linkedin.com/jobs/view/{meta['li_id']}/"
                         if meta["li_id"] else meta["href"])
            if not apply_url or apply_url in existing_apply_urls:
                continue

            # HUMAN: scroll the card into view, then CLICK it → the JD loads in the right
            # pane (no job-view navigation, so no redirect-to-challenge).
            try:
                await card.scroll_into_view_if_needed(timeout=4000)
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.4, 1.2))
            desc = ""
            link = meta.get("link_el") or await card.query_selector("a[href*='/jobs/view/']")
            if link and await _human_click(page, link):
                await asyncio.sleep(random.uniform(1.0, 2.5))
                desc = await _read_detail_pane(page)
                await reading_pause(len(desc) or 400)
            if not desc:
                desc = f"{meta['title']} at {meta['company']} — {meta['location']}"

            listings.append(_build_job(
                title=meta["title"], company=meta["company"], location=meta["location"],
                description=desc, apply_url=apply_url, li_job_id=meta["li_id"]))
            existing_apply_urls.add(apply_url)
            fetched += 1
            # Human dwell between jobs; occasionally a longer "got distracted" pause.
            await page_think_pause()
            if random.random() < 0.25:
                await asyncio.sleep(random.uniform(4, 12))

    except Exception as e:
        msg = str(e)
        if _is_throttle(msg):
            _escalating_backoff("search throttled")
            logger.warning("li_search_throttled", query=query, error=msg[:100])
        else:
            logger.error("li_search_failed", query=query, error=msg)
    finally:
        await page.close()

    return listings


async def scan_in_session(
    browser: LinkedInBrowser,
    state: JobSearchState,
) -> tuple[list[str], JobSearchState]:
    """
    Scan LinkedIn inside an *existing* browser session.
    Returns (list_of_new_li_job_urls, updated_state).
    Used by the apply pipeline so it can reuse the same Chrome window.
    """
    existing_urls = {j["apply_url"] for j in state.get("job_listings", [])}
    queries = _load_search_queries()
    all_new: list[JobListing] = []

    for i, query in enumerate(queries):
        print(f"   [{i+1}/{len(queries)}] Searching: {query.replace('+', ' ')}")
        new_jobs = await _search_query(browser, query, existing_urls)
        all_new.extend(new_jobs)
        logger.info("li_query_done", query=query, new_jobs=len(new_jobs))
        if new_jobs:
            print(f"         → {len(new_jobs)} new job(s) found")
        # Skip sleep after the last query
        if i < len(queries) - 1:
            wait = 60 + (hash(query) % 30)
            print(f"         (waiting {wait}s before next query...)")
            await asyncio.sleep(wait)

    logger.log_decision(
        action="linkedin_scan_complete",
        outcome="success",
        details={
            "queries_run": len(queries),
            "new_jobs": len(all_new),
            "total_jobs": len(state.get("job_listings", [])) + len(all_new),
        },
    )

    updated = dict(state)
    updated["job_listings"] = state.get("job_listings", []) + all_new
    new_li_urls = [j["apply_url"] for j in all_new if "linkedin.com/jobs/view/" in j.get("apply_url", "")]
    return new_li_urls, updated


async def run(state: JobSearchState) -> JobSearchState:
    """
    Scan LinkedIn for new job listings and merge them into state.
    Skips jobs already present (by apply_url).
    """
    if await _is_paused():
        return state

    existing_urls = {j["apply_url"] for j in state.get("job_listings", [])}
    queries = _load_search_queries()
    # A person does 1–2 searches per sitting, not a sweep of all of them. Rotate a random
    # subset each run; the scheduler runs this every couple of hours → a steady trickle.
    random.shuffle(queries)
    queries = queries[:2]
    all_new: list[JobListing] = []
    threw = False

    try:
        async with LinkedInBrowser() as browser:
            page = await browser.new_page()
            if not await browser.is_logged_in(page):
                logger.warning("linkedin_not_logged_in")
                await page.close()
                return state
            await page.close()

            await _warm_up(browser)   # glance at the feed before touching jobs

            for query in queries:
                new_jobs = await _search_query(browser, query, existing_urls)
                all_new.extend(new_jobs)
                logger.info("li_query_done", query=query, new_jobs=len(new_jobs))
                if await _is_paused():   # a throttle tripped the breaker mid-session
                    threw = True
                    logger.info("li_scan_stopped_paused")
                    break
                await session_idle(40, 120)   # long human gap between searches

    except Exception as e:
        threw = True
        msg = str(e)
        if _is_throttle(msg):
            _escalating_backoff("scan throttled")
        logger.error("linkedin_scanner_failed", error=msg[:120])

    if not threw:
        _reset_throttle()   # clean session → clear the escalation counter

    logger.log_decision(
        action="linkedin_scan_complete",
        outcome="success",
        details={
            "queries_run": len(queries),
            "new_jobs": len(all_new),
            "total_jobs": len(state.get("job_listings", [])) + len(all_new),
        },
    )

    updated = dict(state)
    updated["job_listings"] = state.get("job_listings", []) + all_new
    return updated
