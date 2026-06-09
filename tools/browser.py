"""
Stealth-wrapped Playwright browser client.
All agents that need browser automation import from here.
Never instantiate Playwright directly in agents.
"""

import asyncio
import random
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright
from core.stealth import random_user_agent, jitter
from core.logger import get_logger

logger = get_logger("browser")


class BrowserClient:
    """
    Single shared browser instance with stealth settings applied.
    Use as an async context manager:

        async with BrowserClient() as browser:
            page = await browser.new_page()
            await page.goto(url)
    """

    def __init__(self, headless: bool = True, channel: str | None = None,
                 profile_dir: str | None = None, patched: bool = False):
        self._headless = headless
        # channel="chrome" drives the user's real Google Chrome instead of the
        # bundled "Chrome for Testing" — removes the biggest automation tell that
        # enterprise hCaptcha (e.g. Lever) flags. Falls back to Chromium if absent.
        self._channel = channel
        # profile_dir → persistent context: cookies/history/login reputation survive
        # across runs, which is the single biggest factor for hCaptcha-Enterprise
        # trust. Implies channel="chrome" and removal of the --enable-automation flag.
        self._profile_dir = profile_dir
        # patched → use patchright (a patched Playwright) which closes the CDP
        # Runtime.enable leak that enterprise hCaptcha/Cloudflare detect. In this mode
        # we use MINIMAL args and NO manual stealth — patchright handles masking, and
        # extra flags / init scripts would re-expose automation.
        self._patched = patched
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    _STEALTH_INIT = """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {} };
        window._mouseX = 0; window._mouseY = 0;
        document.addEventListener('mousemove', e => { window._mouseX = e.clientX; window._mouseY = e.clientY; });
    """

    async def __aenter__(self) -> "BrowserClient":
        if self._patched:
            # patchright: minimal-footprint patched Playwright. Real Chrome, persistent
            # profile, no manual stealth args/scripts (patchright masks CDP itself).
            from patchright.async_api import async_playwright as _pw
            from pathlib import Path as _Path
            self._playwright = await _pw().start()
            pdir = _Path(self._profile_dir or "data/chrome_profile")
            pdir.mkdir(parents=True, exist_ok=True)
            for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                (pdir / lock).unlink(missing_ok=True)
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(pdir),
                channel="chrome",
                headless=self._headless,
                no_viewport=True,
            )
            logger.info("browser_started", mode="patched", channel="chrome", profile=str(pdir))
            return self

        self._playwright = await async_playwright().start()

        if self._profile_dir:
            # Persistent-profile stealth mode (real Chrome, no automation banner).
            from pathlib import Path as _Path
            pdir = _Path(self._profile_dir)
            pdir.mkdir(parents=True, exist_ok=True)
            for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                (pdir / lock).unlink(missing_ok=True)
            common = dict(
                user_data_dir=str(pdir),
                headless=self._headless,
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage", "--disable-infobars",
                    "--disable-blink-features=AutomationControlled",
                ],
                ignore_default_args=["--enable-automation", "--disable-background-networking"],
                locale="en-US",
                timezone_id="America/Chicago",
            )
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    channel="chrome", **common)
                logger.info("browser_started", mode="persistent", channel="chrome",
                            profile=str(pdir))
            except Exception as e:
                logger.warning("browser_channel_fallback", error=str(e)[:80])
                self._context = await self._playwright.chromium.launch_persistent_context(**common)
                logger.info("browser_started", mode="persistent", channel="chromium")
            await self._context.add_init_script(self._STEALTH_INIT)
            return self

        launch_kwargs = dict(
            headless=self._headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
            ignore_default_args=["--enable-automation"],
        )
        if self._channel:
            launch_kwargs["channel"] = self._channel
        try:
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        except Exception as e:
            # Requested channel not installed — fall back to bundled Chromium.
            logger.warning("browser_channel_fallback", channel=self._channel, error=str(e)[:80])
            launch_kwargs.pop("channel", None)
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        logger.info("browser_started", headless=self._headless, channel=self._channel)
        return self

    async def __aexit__(self, *_) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("browser_closed")

    async def new_page(self, url: str | None = None) -> Page:
        """Create a new stealth page, optionally navigating to url."""
        if self._context:
            # Persistent mode: reuse the profile's single context.
            page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            if url:
                await self.goto(page, url)
            return page

        if not self._browser:
            raise RuntimeError("BrowserClient not started — use as async context manager")

        context: BrowserContext = await self._browser.new_context(
            user_agent=random_user_agent(),
            viewport={"width": random.randint(1280, 1920), "height": random.randint(800, 1080)},
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        await context.add_init_script(self._STEALTH_INIT)
        page = await context.new_page()
        if url:
            await self.goto(page, url)
        return page

    async def goto(self, page: Page, url: str, wait: str = "domcontentloaded") -> None:
        """Navigate with human-like delay before and after."""
        await asyncio.sleep(jitter(1.5))
        await page.goto(url, wait_until=wait, timeout=30_000)
        await self._human_scroll(page)

    async def get_text(self, url: str) -> str:
        """Open a page, extract all visible text, close the page."""
        page = await self.new_page(url)
        try:
            text = await page.inner_text("body")
            return text[:8000]
        except Exception as e:
            logger.warning("get_text_failed", url=url, error=str(e))
            return ""
        finally:
            await page.context.close()

    async def get_html(self, url: str) -> str:
        """Open a page, extract raw HTML, close the page."""
        page = await self.new_page(url)
        try:
            html = await page.content()
            return html[:20000]
        except Exception as e:
            logger.warning("get_html_failed", url=url, error=str(e))
            return ""
        finally:
            await page.context.close()

    async def _human_scroll(self, page: Page) -> None:
        """Simulate human scroll to trigger lazy-loaded content."""
        try:
            for _ in range(random.randint(2, 4)):
                await page.evaluate(
                    f"window.scrollBy(0, {random.randint(200, 600)})"
                )
                await asyncio.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass


# Convenience: fetch plain text from a URL without keeping browser alive
async def fetch_text(url: str) -> str:
    async with BrowserClient() as b:
        return await b.get_text(url)
