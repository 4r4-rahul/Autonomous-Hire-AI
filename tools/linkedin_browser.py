"""
LinkedIn-specific browser context using a persistent Chrome profile.
Cookies, localStorage, and login state survive across runs.
Uses launch_persistent_context() so the profile directory is written to disk.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import random
import signal
from pathlib import Path
from typing import AsyncIterator, Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from core.config import get_settings
from core.logger import get_logger
from core.stealth import random_user_agent, jitter

logger = get_logger("linkedin_browser")

# LinkedIn's stable desktop viewport range
_VIEWPORTS = [
    {"width": 1440, "height": 900},
    {"width": 1680, "height": 1050},
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 800},
]

_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    window.chrome = { runtime: {} };
    // Track mouse position for humanize.py
    window._mouseX = 0; window._mouseY = 0;
    document.addEventListener('mousemove', e => { window._mouseX = e.clientX; window._mouseY = e.clientY; });
"""


class LinkedInBrowser:
    """
    Persistent-profile Playwright context for LinkedIn.
    Always non-headless so fingerprinting checks see a real viewport.

    Usage:
        async with LinkedInBrowser() as ctx:
            page = await ctx.new_page()
            await page.goto("https://www.linkedin.com/feed/")
    """

    def __init__(self, profile_dir: Optional[str] = None) -> None:
        settings = get_settings()
        self._profile_dir = Path(profile_dir or settings.linkedin_profile_dir)
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None

    def _stable_user_agent(self) -> str:
        """Return a UA that is fixed for this profile so all sessions look like the same browser."""
        ua_file = self._profile_dir / "ua.txt"
        if ua_file.exists():
            return ua_file.read_text().strip()
        ua = random_user_agent()
        ua_file.write_text(ua)
        return ua

    def _stable_viewport(self) -> dict:
        """Return a viewport that is fixed for this profile."""
        vp_file = self._profile_dir / "viewport.txt"
        if vp_file.exists():
            w, h = vp_file.read_text().strip().split("x")
            return {"width": int(w), "height": int(h)}
        vp = random.choice(_VIEWPORTS)
        vp_file.write_text(f"{vp['width']}x{vp['height']}")
        return vp

    def _clear_singleton_locks(self) -> None:
        """Remove stale Chrome singleton files left by a previous crashed run."""
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            (self._profile_dir / name).unlink(missing_ok=True)

    async def __aenter__(self) -> "LinkedInBrowser":
        self._clear_singleton_locks()
        self._playwright = await async_playwright().start()
        vp = self._stable_viewport()
        ua = self._stable_user_agent()

        # ignore_default_args removes Playwright's injected --enable-automation flag,
        # which is what causes the "Chrome is being controlled by automated test
        # software" banner that Google and LinkedIn use to block sign-in.
        ignore = ["--enable-automation", "--disable-background-networking"]

        launch_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--disable-blink-features=AutomationControlled",
            f"--window-size={vp['width']},{vp['height']}",
        ]
        common_kwargs = dict(
            user_data_dir=str(self._profile_dir),
            headless=False,
            args=launch_args,
            ignore_default_args=ignore,
            viewport=vp,
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

        # Use the user's real installed Chrome — genuine Chrome internals mean
        # sites can't fingerprint it as automation via canvas/WebGL/etc.
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                channel="chrome",
                **common_kwargs,
            )
            logger.info("linkedin_browser_started", profile=str(self._profile_dir), channel="chrome")
        except Exception as chrome_err:
            logger.warning("real_chrome_unavailable", error=str(chrome_err),
                           note="Falling back to bundled Chromium")
            common_kwargs["user_agent"] = ua
            self._context = await self._playwright.chromium.launch_persistent_context(
                **common_kwargs,
            )
            logger.info("linkedin_browser_started", profile=str(self._profile_dir), channel="chromium")

        await self._context.add_init_script(_INIT_SCRIPT)

        # Inject the li_at session cookie from settings so an updated .env actually takes
        # effect. The persistent profile keeps its own session, but when that expires (or on
        # a fresh profile) it would stay logged out forever — this seeds/refreshes it.
        try:
            from core.config import get_settings
            li_at = (get_settings().linkedin_li_at or "").strip()
            if li_at:
                existing = await self._context.cookies()
                cur = next((c for c in existing if c["name"] == "li_at"), None)
                if not cur or cur.get("value") != li_at:
                    await self._context.add_cookies([{
                        "name": "li_at", "value": li_at, "domain": ".linkedin.com",
                        "path": "/", "httpOnly": True, "secure": True, "sameSite": "None",
                    }])
                    logger.info("linkedin_li_at_injected", source="settings")
        except Exception as e:
            logger.warning("linkedin_li_at_inject_failed", error=str(e)[:100])

        return self

    async def __aexit__(self, *_) -> None:
        lock_file = self._profile_dir / "SingletonLock"
        chrome_pid: Optional[int] = None

        # Read the Chrome PID from SingletonLock before closing
        if lock_file.is_symlink():
            try:
                target = os.readlink(str(lock_file))   # e.g. "MacBook-Pro.local-59372"
                chrome_pid = int(target.rsplit("-", 1)[-1])
            except (ValueError, OSError):
                pass

        if self._context:
            try:
                await asyncio.wait_for(self._context.close(), timeout=20.0)
            except (asyncio.TimeoutError, Exception):
                # context.close() hung (common after a forced kill + WAL recovery).
                # Terminate Chrome directly so the next run isn't blocked.
                logger.warning("linkedin_browser_close_timeout_force_kill")
                if chrome_pid:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        os.kill(chrome_pid, signal.SIGTERM)
                    await asyncio.sleep(1)
                    with contextlib.suppress(ProcessLookupError, OSError):
                        os.kill(chrome_pid, signal.SIGKILL)

        if self._playwright:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._playwright.stop(), timeout=5.0)

        # Remove stale lock if Chrome didn't clean up
        with contextlib.suppress(OSError):
            if lock_file.exists():
                lock_file.unlink()

        logger.info("linkedin_browser_closed")

    async def new_page(self) -> Page:
        """Open a new tab inside the persistent context."""
        if not self._context:
            raise RuntimeError("LinkedInBrowser not started — use as async context manager")
        page = await self._context.new_page()
        await page.add_init_script(_INIT_SCRIPT)
        return page

    async def goto(self, page: Page, url: str, wait: str = "commit") -> None:
        """
        Navigate with human-like delay.
        Default wait is 'commit' (first response byte received) rather than
        'domcontentloaded' — LinkedIn returns HTTP 999 to detected bots which
        makes Playwright raise ERR_HTTP_RESPONSE_CODE_FAILURE on domcontentloaded.
        'commit' ignores the status code and just waits for the navigation to start.
        """
        await asyncio.sleep(jitter(1.5))
        await page.goto(url, wait_until=wait, timeout=45_000)
        await asyncio.sleep(jitter(1.0, pct=0.4))

    async def session_status(self, page: Page) -> tuple[bool, str]:
        """
        Navigate to the feed and return (is_ok, description).
        description is one of: 'ok', 'logged_out', 'captcha', 'restricted', 'unknown:<url>'.
        Cookie check runs first so we avoid a full navigation when the session is definitely gone.
        """
        # Fast path: no li_at cookie means definitely logged out
        try:
            cookies = await self._context.cookies()
            li_at = next((c for c in cookies if c["name"] == "li_at"), None)
            if not li_at:
                return False, "logged_out"
        except Exception:
            pass

        try:
            await self.goto(page, "https://www.linkedin.com/feed/", wait="commit")
            await asyncio.sleep(3.0)   # allow redirects to settle
            url = page.url.lower()

            if any(s in url for s in ("checkpoint/challenge", "checkpoint/lg", "captcha")):
                return False, "captcha"
            if any(s in url for s in ("authwall", "/uas/login", "linkedin.com/login")):
                return False, "logged_out"
            if "unusual activity" in url or "restricted" in url:
                return False, "restricted"
            if any(s in url for s in ("linkedin.com/feed", "linkedin.com/in/", "linkedin.com/jobs")):
                return True, "ok"

            return False, f"unknown:{page.url[:100]}"
        except Exception as e:
            return False, f"error:{e}"

    async def is_logged_in(self, page: Page) -> bool:
        """Return True if LinkedIn session is active."""
        ok, _ = await self.session_status(page)
        return ok

    async def close_extra_pages(self) -> None:
        """Close all pages except the first (keeps memory footprint low)."""
        if not self._context:
            return
        pages = self._context.pages
        for p in pages[1:]:
            with contextlib.suppress(Exception):
                await p.close()


@contextlib.asynccontextmanager
async def linkedin_browser(profile_dir: Optional[str] = None) -> AsyncIterator[LinkedInBrowser]:
    """Async context manager shorthand."""
    browser = LinkedInBrowser(profile_dir=profile_dir)
    async with browser as ctx:
        yield ctx
