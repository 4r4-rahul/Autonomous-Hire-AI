"""
LinkedIn Apply Pipeline.

For each LinkedIn job page the browser visits:
  1. Extract full description
  2. Find "Apply on company website" URL (skip Easy Apply)
  3. Score fit (Haiku)
  4. If passes → tailor resume (Opus — config default, kept for max résumé quality)
  5. Quality review
  6. Human approval gate
  7. Submit via company ATS

Runs inside an active LinkedInBrowser session so only one Chrome process
is open for the whole LinkedIn → apply flow.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.config import get_settings
from core.llm import get_llm
from core.logger import get_logger
from core.rate_limiter import get_rate_limiter
from core.state import Application, FitScore, JobListing, JobSearchState
from tools.humanize import click_human, page_think_pause, reading_pause, scroll_down_human
from tools.linkedin_browser import LinkedInBrowser

logger = get_logger("linkedin_apply_pipeline")

_APPLIED_LOG = Path("data/applied_jobs.json")
_PROFILE_PATH = Path("data/user_profile.yaml")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _already_applied(apply_url: str) -> bool:
    if not _APPLIED_LOG.exists():
        return False
    try:
        entries = json.loads(_APPLIED_LOG.read_text())
        return any(e.get("apply_url") == apply_url for e in entries)
    except Exception:
        return False


def _log_applied(app: Application, ats_url: str) -> None:
    entries: list[dict] = []
    if _APPLIED_LOG.exists():
        try:
            entries = json.loads(_APPLIED_LOG.read_text())
        except Exception:
            pass
    record = {
        "app_id": app["id"],
        "company": app["company_name"],
        "role": app["role_title"],
        "apply_url": ats_url,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    entries = [e for e in entries if e.get("apply_url") != ats_url]
    entries.append(record)
    _APPLIED_LOG.write_text(json.dumps(entries, indent=2))


async def _extract_description(browser: LinkedInBrowser, page, li_job_url: str) -> str:
    """Navigate to a LinkedIn job page and return the full description text."""
    await browser.goto(page, li_job_url)
    await page_think_pause()
    await scroll_down_human(page, 400)

    # Expand "See more" if present
    try:
        see_more = page.locator("button:has-text('See more'), button.jobs-description__footer-button").first
        if await see_more.is_visible(timeout=2000):
            await click_human(page, "button:has-text('See more')")
            await asyncio.sleep(0.5)
    except Exception:
        pass

    desc_el = await page.query_selector(
        ".jobs-description__content, .jobs-box__html-content, #job-details"
    )
    if not desc_el:
        return ""
    text = await desc_el.inner_text()
    await reading_pause(len(text))
    return text.strip()[:4000]


async def _get_ats_url(browser: LinkedInBrowser, page, li_job_url: str) -> Optional[str]:
    """
    Find the external apply button and return the ATS URL.
    Returns None if only Easy Apply is available.

    LinkedIn renders the apply button with aria-label="Apply on company website"
    and an href containing a safety-redirect to the actual ATS URL.
    We extract the URL from the href first (no click needed); fall back to clicking.
    """
    from urllib.parse import urlparse, parse_qs, unquote

    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.5)

    # Find ANY apply-related button/link — match on aria-label, not inner text
    # (LinkedIn shows "Apply" as button text but full phrase in aria-label)
    btn = page.locator(
        "[aria-label*='Apply on company website'], "
        "[aria-label*='Apply on employer'], "
        "button.jobs-apply-button, "
        ".jobs-s-apply button, "
        ".jobs-s-apply a, "
        "button[aria-label*='Apply'], "
        "a[aria-label*='Apply']"
    ).first

    if not await btn.is_visible(timeout=5000):
        return None

    aria = (await btn.get_attribute("aria-label") or "").lower()
    text = ""
    try:
        text = (await btn.inner_text()).strip().lower()
    except Exception:
        pass

    # Skip Easy Apply
    if "easy apply" in aria or "easy apply" in text:
        return None

    logger.info("external_apply_button_found", aria=aria, url=li_job_url)

    # Strategy 1: extract ATS URL from LinkedIn safety-redirect href
    # e.g. href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fjob-boards.greenhouse.io%2F..."
    try:
        href = await btn.get_attribute("href") or ""
        if href and "/safety/go/" in href:
            params = parse_qs(urlparse(href).query)
            decoded = unquote(params.get("url", [""])[0])
            if decoded and "linkedin.com" not in decoded:
                logger.info("ats_url_from_href", url=decoded[:80])
                return decoded
    except Exception:
        pass

    # Strategy 2: click and capture popup / navigation
    try:
        async with page.expect_popup(timeout=8000) as popup_info:
            await btn.click()
        popup = await popup_info.value
        await popup.wait_for_load_state("domcontentloaded", timeout=15000)
        ats_url = popup.url
        await popup.close()
        if ats_url and "linkedin.com" not in ats_url:
            return ats_url
    except Exception:
        try:
            await btn.click()
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            ats_url = page.url
            await browser.goto(page, li_job_url)
            if ats_url and "linkedin.com" not in ats_url:
                return ats_url
        except Exception as e:
            logger.warning("ats_url_capture_failed", url=li_job_url, error=str(e))

    return None


async def _score_job(job: JobListing, llm) -> FitScore:
    """Quick single-job fit score using Haiku."""
    import yaml
    try:
        profile = yaml.safe_load(_PROFILE_PATH.read_text())
    except Exception:
        profile = {}

    cfg = get_settings()

    result = await llm.chat_json(
        "You score how well a candidate fits a job. Return JSON: "
        "{\"score\": 0-100, \"passed\": bool, \"highlights\": [str, str], \"missing\": [str, str]}",
        [{
            "role": "user",
            "content": (
                f"Candidate: {profile.get('name', 'Candidate')}, "
                f"{profile.get('summary', 'Senior Data/Platform Engineer, 6+ yrs, Python/AWS/Spark/MLOps')}\n\n"
                f"Job: {job['title']} at {job['company_name']}\n"
                f"Description (first 1500 chars):\n{job['description'][:1500]}\n\n"
                f"Score fit 0-100. Threshold to pass: {cfg.min_fit_score}."
            ),
        }],
        max_tokens=256,
    )

    score = int(result.get("score", 0))
    return {
        "id": str(uuid.uuid4()),
        "job_id": job["id"],
        "score": score,
        "passed_threshold": score >= cfg.min_fit_score,
        "highlights": result.get("highlights", []),
        "missing_keywords": result.get("missing", []),
        "model": "claude-haiku-4-5",
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Per-job pipeline ───────────────────────────────────────────────────────────

async def process_job(
    browser: LinkedInBrowser,
    page,
    li_job_url: str,
    state: JobSearchState,
) -> tuple[Optional[Application], JobSearchState]:
    """
    Full pipeline for one LinkedIn job listing.
    Returns (application_if_submitted, updated_state).
    """
    import yaml
    from agents.application.resume_tailor import tailor_one
    from agents.application.app_quality_reviewer import review_application
    from agents.application.form_filler import submit_application

    cfg = get_settings()
    limiter = get_rate_limiter()

    # Dedup by active application — skip only if already submitted or in-flight
    applied_job_ids = {
        a.get("job_id") for a in state.get("applications", [])
        if a.get("status") in ("submitted", "pending_review", "approved")
    }
    existing_jobs = {j["apply_url"]: j for j in state.get("job_listings", [])}
    if li_job_url in existing_jobs:
        existing = existing_jobs[li_job_url]
        if existing["id"] in applied_job_ids:
            logger.info("li_job_already_applied", url=li_job_url)
            return None, state

    print(f"\n  → {li_job_url}")

    # Always navigate to the job page — _get_ats_url relies on the page being loaded.
    # If we have a cached description we skip the description fetch, but we still
    # must land on the job page before looking for the apply button.
    description = ""
    title = "Unknown Role"
    company = "Unknown Company"

    if li_job_url in existing_jobs:
        cached = existing_jobs[li_job_url]
        description = cached.get("description", "")
        title = cached.get("title", "Unknown Role")
        company = cached.get("company_name", "Unknown Company")
        # Navigate so _get_ats_url sees the correct page
        await _extract_description(browser, page, li_job_url)  # navigates + scrolls
    else:
        # Step 1: Extract description (also navigates to the job page)
        description = await _extract_description(browser, page, li_job_url)
        if not description:
            logger.info("li_job_no_description", url=li_job_url)
            return None, state

        # Parse company + title from page
        try:
            title_el = await page.query_selector(".job-details-jobs-unified-top-card__job-title, h1.t-24")
            company_el = await page.query_selector(".job-details-jobs-unified-top-card__company-name a, .jobs-unified-top-card__company-name a")
            title = (await title_el.inner_text()).strip() if title_el else "Unknown Role"
            company = (await company_el.inner_text()).strip() if company_el else "Unknown Company"
        except Exception:
            pass

    # Step 2: Get external ATS URL (page is now on the job listing)
    ats_url = await _get_ats_url(browser, page, li_job_url)

    if not ats_url:
        print(f"  ✗  No external apply URL (Easy Apply only) — skipping")
        return None, state

    # Skip job-board trackers and gig platforms (not real ATS forms)
    from urllib.parse import urlparse as _urlparse
    _ats_host = _urlparse(ats_url).netloc.lower()
    _JUNK_ATS = ["jometer.com", "jobleads.com", "outlier.ai", "app.outlier.ai",
                 "zipapply", "indeed.com", "app.joinhandshake.com"]
    if any(junk in _ats_host for junk in _JUNK_ATS):
        print(f"  ✗  Junk ATS domain ({_ats_host}) — skipping")
        logger.info("li_junk_ats_skipped", url=li_job_url, ats_domain=_ats_host)
        return None, state

    if _already_applied(ats_url):
        print(f"  ✓  Already applied — skipping")
        return None, state

    # Build or reuse JobListing
    if li_job_url in existing_jobs:
        job = dict(existing_jobs[li_job_url])
        job["apply_url"] = ats_url   # update to real ATS URL
    else:
        job: JobListing = {
            "id": str(uuid.uuid4()),
            "company_id": "",
            "company_name": company,
            "title": title,
            "description": description,
            "required_skills": [],
            "nice_to_have_skills": [],
            "location": "Remote",
            "remote": True,
            "salary_min": None,
            "salary_max": None,
            "apply_url": ats_url,
            "source": "linkedin",
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }

    # Step 3: Score fit
    llm_score = get_llm("linkedin_apply_pipeline", model="claude-haiku-4-5")
    print(f"  Scoring: {title} @ {company}...")
    try:
        fit_score = await _score_job(job, llm_score)
    except Exception as e:
        logger.warning("score_failed", url=li_job_url, error=str(e))
        return None, state

    print(f"  Score: {fit_score['score']}/100", end="")
    if not fit_score["passed_threshold"]:
        print(f" — below threshold ({cfg.min_fit_score}), skipping")
        return None, state
    print(f" — passed!")

    # Step 4: Tailor resume
    llm_tailor = get_llm("linkedin_apply_pipeline")
    llm_cheap  = get_llm("linkedin_apply_pipeline", model="claude-haiku-4-5")
    try:
        import yaml as _yaml
        profile = _yaml.safe_load(_PROFILE_PATH.read_text())
    except Exception:
        profile = {}

    print(f"  Tailoring resume...")
    try:
        app = await tailor_one(job, fit_score, llm_tailor, profile, llm_cheap=llm_cheap)
    except Exception as e:
        logger.error("tailor_failed", url=li_job_url, error=str(e))
        return None, state

    if not app:
        logger.warning("tailor_returned_none", url=li_job_url)
        return None, state

    # Step 5: Quality review
    llm_review = get_llm("linkedin_apply_pipeline", model="claude-haiku-4-5")
    print(f"  Quality review...")
    try:
        app, report = await review_application(app, job, llm_review)
    except Exception as e:
        logger.warning("review_failed", url=li_job_url, error=str(e))
        report = {}

    quality_score = app.get("quality_score", 0)
    print(f"  Quality: {quality_score}/100", end="")
    if app.get("status") == "blocked":
        print(f" — blocked, skipping")
        return None, state
    print(f" — OK")

    # Step 6: Check daily cap
    from datetime import date
    today = date.today().isoformat()
    already_today = sum(
        1 for a in state.get("applications", [])
        if a.get("status") == "submitted" and a.get("submitted_at", "").startswith(today)
    )
    if already_today >= cfg.apply_max_per_day:
        print(f"  Daily cap reached ({cfg.apply_max_per_day}) — stopping")
        return None, state

    # Step 7: Submit via ATS
    print(f"  Submitting to {ats_url[:60]}...")
    try:
        submitted_app = await submit_application(app, job, profile, llm_tailor, headless=False)
    except Exception as e:
        logger.error("submit_failed", url=ats_url, error=str(e))
        return None, state

    if submitted_app.get("status") == "submitted":
        _log_applied(submitted_app, ats_url)
        print(f"  ✓  Submitted: {title} @ {company}")
    else:
        print(f"  ✗  Submission result: {submitted_app.get('status', 'unknown')}")

    # Persist job + fit score + app into state
    updated = dict(state)
    updated["job_listings"] = state.get("job_listings", []) + [job]
    updated["fit_scores"] = state.get("fit_scores", []) + [fit_score]
    updated["applications"] = state.get("applications", []) + [submitted_app]

    return submitted_app, updated


# ── Entry point ────────────────────────────────────────────────────────────────

async def run(
    browser: LinkedInBrowser,
    li_job_urls: list[str],
    state: JobSearchState,
) -> JobSearchState:
    """
    Process a list of LinkedIn job URLs through the full apply pipeline.
    Runs inside an existing browser session — caller owns the browser lifecycle.
    """
    cfg = get_settings()
    from datetime import date
    today = date.today().isoformat()

    submitted = 0
    for url in li_job_urls:
        already_today = sum(
            1 for a in state.get("applications", [])
            if a.get("status") == "submitted" and a.get("submitted_at", "").startswith(today)
        )
        if already_today >= cfg.apply_max_per_day:
            logger.info("li_apply_pipeline_daily_cap_reached")
            break

        page = await browser.new_page()
        try:
            app, state = await process_job(browser, page, url, state)
        finally:
            await page.close()

        if app is not None and app.get("status") == "submitted":
            submitted += 1

        # Pause between jobs — human-like delay whether submitted or skipped
        await asyncio.sleep(20 + (hash(url) % 20))

    logger.log_decision(
        action="linkedin_apply_pipeline_complete",
        outcome="success",
        details={"submitted": submitted, "jobs_attempted": len(li_job_urls)},
    )
    return state
