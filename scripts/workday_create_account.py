"""
Fully-automated Workday account creation (no human step).

Replicates the exact DOM-proven sequence for a Workday career site:
  Apply → Apply Manually → Sign in with email → Create Account →
  fill email/password/verify → FORCE-CLICK the acknowledgment checkbox
  (native click fires Workday's trusted-event validation; JS .checked does not) →
  Create Account submit → poll Gmail for the verification LINK → open it.

Creates an account with 4r4rahul@gmail.com + the stored password so the
application wizard can sign in later. Generalizes to any *.myworkdayjobs.com site.

Usage:
    python scripts/workday_create_account.py <workday_job_url>
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.config import get_settings
from tools.browser import BrowserClient
from agents.application import form_filler as ff

_PROFILE = Path("data/user_profile.yaml")


def _emit(m: str) -> None:
    print(m, flush=True)


async def _click(page, sels, wait, tag) -> bool:
    for s in sels:
        try:
            b = page.locator(s).first
            if await b.count() > 0 and await b.is_visible(timeout=1500):
                try:
                    await b.click(timeout=4000)
                except Exception:
                    await b.evaluate("el => el.click()")
                await asyncio.sleep(wait)
                _emit(f"<<STEP>> clicked {tag}")
                return True
        except Exception:
            pass
    _emit(f"<<STEP>> {tag}: not found")
    return False


async def main() -> None:
    url = sys.argv[1]
    get_settings()
    prof = yaml.safe_load(_PROFILE.read_text())
    p = prof["personal"]; defs = prof.get("application_defaults", {})
    email = p.get("email", ""); from core.config import get_settings as _gs
    pw = _gs().workday_password or defs.get("workday_password", "")

    _emit(f"<<START>> Workday account creation | {url[:70]}")
    async with BrowserClient(headless=False, profile_dir="data/chrome_profile_wdacct", patched=True) as b:
        page = await b.new_page(url); await asyncio.sleep(6)

        await _click(page, ["button:has-text('Accept Cookies')", "button:has-text('Accept')",
                            "#onetrust-accept-btn-handler"], 0.5, "cookies")
        await _click(page, ["a[data-automation-id='adventureButton']", "button:has-text('Apply')"], 3, "Apply")
        await _click(page, ["[data-automation-id='applyManually']"], 5, "Apply Manually")
        await _click(page, ["button:has-text('Sign in with email')", "a:has-text('Sign in with email')"], 4, "Sign in with email")
        await _click(page, ["[data-automation-id='createAccountLink']", "a:has-text('Create Account')",
                            "button:has-text('Create Account')"], 3, "Create Account toggle")

        # Fill credentials
        ei = page.locator("[data-automation-id='email'], input[type='email']").first
        pi = page.locator("[data-automation-id='password'], input[type='password']").first
        if await ei.count() == 0 or await pi.count() == 0:
            await ff._screenshot(page, "wd_acct_noform")
            _emit("<<RESULT>> status=no_create_form (email/password fields not found)")
            return
        await ei.fill(email); await asyncio.sleep(0.2)
        await pi.fill(pw); await asyncio.sleep(0.2)
        vp = page.locator("[data-automation-id='verifyPassword']").first
        if await vp.count() > 0:
            await vp.fill(pw); await asyncio.sleep(0.2)
        _emit(f"<<STEP>> filled email + password ({email})")

        # FORCE-click the acknowledgment checkbox (proven to register with Workday)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); await asyncio.sleep(0.6)
        checked = False
        for cbsel in ("[data-automation-id='createAccountCheckbox']", "input[type='checkbox']"):
            cb = page.locator(cbsel).first
            if await cb.count() > 0:
                try:
                    await cb.scroll_into_view_if_needed(timeout=2000)
                    await cb.click(timeout=3000, force=True); checked = True
                except Exception as e:
                    _emit(f"<<STEP>> checkbox click note: {str(e)[:50]}")
                break
        _emit(f"<<STEP>> acknowledgment checkbox: {'checked' if checked else 'NOT checked'}")
        await asyncio.sleep(0.4)

        # Submit — Workday wraps the button in a click_filter overlay that intercepts
        # pointer events, so clicking the inner button silently no-ops. Click at the
        # button's COORDINATES so the overlay receives the trusted click.
        sb = page.locator("[data-automation-id='createAccountSubmitButton']").first
        if await sb.count() > 0:
            try:
                await sb.scroll_into_view_if_needed(timeout=2000); await asyncio.sleep(0.3)
                box = await sb.bounding_box()
                if box:
                    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                else:
                    await sb.click(timeout=5000, force=True)
            except Exception:
                await sb.evaluate("el => el.click()")
            await asyncio.sleep(4.0)
            _emit("<<STEP>> create-account submitted (coordinate-click)")

        # Detect outcome on the page
        body = (await page.inner_text("body")).lower()
        if "already" in body and ("account" in body or "exist" in body):
            _emit("<<RESULT>> status=account_exists — try sign-in instead (password stored)")
            return
        err = next((k for k in ("must accept", "is required", "invalid", "does not meet",
                                "password requirements") if k in body), None)
        if err:
            await ff._screenshot(page, "wd_acct_err")
            _emit(f"<<RESULT>> status=create_error (\"{err}\") — see screenshot")
            return

        # Email verification link via Gmail
        _emit("<<STEP>> waiting for verification email + fetching link from Gmail…")
        ts = time.time() - 60
        link = None
        for delay in (4, 6, 10, 16, 26, 40):
            await asyncio.sleep(delay)
            link = ff._fetch_workday_verify_link(not_before=ts)
            if link:
                break
        if link:
            await page.goto(link, wait_until="domcontentloaded"); await asyncio.sleep(3.0)
            _emit(f"<<RESULT>> status=verified — account created + email verified ({email})")
        else:
            _emit(f"<<RESULT>> status=created_pending_verify — account submitted; "
                  f"no verify link in Gmail yet (check {email})")
        await asyncio.sleep(2.0)


if __name__ == "__main__":
    asyncio.run(main())
