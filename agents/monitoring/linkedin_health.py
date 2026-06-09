"""
LinkedIn Health Monitor.
Detects CAPTCHA challenges, rate-limit warnings, and account restrictions.
Auto-pauses LinkedIn agents by setting LI_PAUSE in a runtime override file.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

from core.config import get_settings
from core.logger import get_logger
from tools.humanize import page_think_pause
from tools.linkedin_browser import LinkedInBrowser

logger = get_logger("linkedin_health")

_PAUSE_FILE = Path("data/linkedin_pause.json")

# URL fragments that indicate LinkedIn is blocking or challenging
_CAPTCHA_URLS = [
    "checkpoint/challenge",
    "checkpoint/lg/",
    "authwall",
    "captcha",
    "/uas/login",  # forced logout
]

# Page text patterns that indicate rate-limiting or restriction warnings
_RESTRICTION_PATTERNS = [
    "your account has been restricted",
    "unusual activity",
    "too many requests",
    "you've reached the commercial use limit",
    "please verify you're not a robot",
    "security verification",
    "we've limited some account features",
]

# Healthy indicator — if this is present we're definitely on LinkedIn
_HEALTHY_INDICATORS = [
    "linkedin.com/feed",
    "linkedin.com/in/",
    "linkedin.com/jobs/",
    "linkedin.com/search/",
]


class HealthStatus:
    OK = "ok"
    CAPTCHA = "captcha"
    RESTRICTED = "restricted"
    LOGGED_OUT = "logged_out"
    UNKNOWN = "unknown"


def _write_pause(reason: str, duration_hours: int = 24) -> None:
    """Write a pause file that the kill switch reads."""
    _PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PAUSE_FILE.write_text(json.dumps({
        "paused_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "resume_after_hours": duration_hours,
    }, indent=2))
    logger.warning("linkedin_auto_paused", reason=reason, duration_hours=duration_hours)


def _clear_pause() -> None:
    if _PAUSE_FILE.exists():
        _PAUSE_FILE.unlink()
        logger.info("linkedin_pause_cleared")


def is_auto_paused() -> bool:
    """Check whether the auto-pause file is active (not yet expired)."""
    if not _PAUSE_FILE.exists():
        return False
    try:
        data = json.loads(_PAUSE_FILE.read_text())
        from datetime import timedelta
        paused_at = datetime.fromisoformat(data["paused_at"])
        duration = timedelta(hours=data.get("resume_after_hours", 24))
        if datetime.now(timezone.utc) > paused_at + duration:
            _clear_pause()
            return False
        return True
    except Exception:
        return False


async def _detect_page_status(page: Page) -> str:
    """Inspect the current page URL and content for health signals."""
    try:
        url = page.url.lower()

        for captcha_url in _CAPTCHA_URLS:
            if captcha_url in url:
                return HealthStatus.CAPTCHA

        # Check visible page text for restriction indicators
        try:
            body_text = (await page.inner_text("body")).lower()
        except Exception:
            body_text = ""

        for pattern in _RESTRICTION_PATTERNS:
            if pattern in body_text:
                return HealthStatus.RESTRICTED

        # Check if redirected to login
        if "linkedin.com/login" in url or "linkedin.com/uas/login" in url:
            return HealthStatus.LOGGED_OUT

        # Confirm we're somewhere expected on LinkedIn
        for indicator in _HEALTHY_INDICATORS:
            if indicator in url:
                return HealthStatus.OK

        return HealthStatus.UNKNOWN

    except Exception as e:
        logger.warning("health_check_page_error", error=str(e))
        return HealthStatus.UNKNOWN


async def run_health_check() -> str:
    """
    Perform a lightweight health check by loading the LinkedIn feed.
    Returns a HealthStatus constant. Auto-pauses on CAPTCHA or restriction.
    """
    if is_auto_paused():
        logger.info("health_check_skipped_auto_paused")
        return HealthStatus.RESTRICTED

    settings = get_settings()
    if settings.linkedin_pause:
        return HealthStatus.RESTRICTED

    status = HealthStatus.UNKNOWN

    try:
        async with LinkedInBrowser() as browser:
            page = await browser.new_page()
            try:
                await browser.goto(page, "https://www.linkedin.com/feed/", wait="load")
                await page_think_pause()
                status = await _detect_page_status(page)
            finally:
                await page.close()

    except Exception as e:
        # A navigation ERROR (ERR_HTTP_RESPONSE_CODE_FAILURE / 429 / redirect loop) is LinkedIn
        # pushing back. The old code returned UNKNOWN with NO pause → the 30-min health check
        # kept re-probing a flagged account every half hour (a 7.5h poke-loop on 2026-06-08
        # that prevented recovery). Now: back off HARD so the block gets a quiet window.
        msg = str(e)
        logger.error("health_check_failed", error=msg[:140])
        hours = 8 if any(s in msg for s in (
            "RESPONSE_CODE_FAILURE", "TOO_MANY_REDIRECTS", "ERR_FAILED", "999")) else 3
        _write_pause(f"health probe errored ({msg[:50]}) — backing off", duration_hours=hours)
        return HealthStatus.UNKNOWN

    _handle_status(status)
    return status


def _handle_status(status: str) -> None:
    if status == HealthStatus.CAPTCHA:
        _write_pause("captcha_detected", duration_hours=4)
        _notify_discord(
            "LinkedIn CAPTCHA detected. Auto-paused for 4 hours. "
            "Please solve the CAPTCHA manually in the browser profile, then delete `data/linkedin_pause.json`."
        )
    elif status == HealthStatus.RESTRICTED:
        _write_pause("account_restriction_detected", duration_hours=48)
        _notify_discord(
            "LinkedIn account restriction detected. Auto-paused for 48 hours. "
            "Check your LinkedIn account for any action items."
        )
    elif status == HealthStatus.LOGGED_OUT:
        # 1h was far too short — it meant re-probing a likely-flagged account every ~90 min.
        # 4h gives the user time to re-auth AND stops the poke loop.
        _write_pause("session_expired", duration_hours=4)
        _notify_discord(
            "LinkedIn session expired (logged out). Auto-paused for 4 hours. "
            "Please log in again via the persistent Chrome profile."
        )
    elif status == HealthStatus.UNKNOWN:
        # Unexpected landing page (often a soft restriction/interstitial) → don't keep probing.
        _write_pause("unknown_state — likely soft restriction", duration_hours=6)
    elif status == HealthStatus.OK:
        _clear_pause()
        logger.info("linkedin_health_ok")


def _notify_discord(message: str) -> None:
    """Fire-and-forget Discord notification."""
    import httpx, asyncio
    settings = get_settings()
    webhook = settings.discord_webhook_review
    if not webhook:
        return
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_post_discord(webhook, message))
        else:
            loop.run_until_complete(_post_discord(webhook, message))
    except Exception as e:
        logger.warning("discord_notify_failed", error=str(e))


async def _post_discord(webhook: str, message: str) -> None:
    try:
        async with __import__("httpx").AsyncClient() as client:
            await client.post(webhook, json={"content": message}, timeout=5)
    except Exception:
        pass
