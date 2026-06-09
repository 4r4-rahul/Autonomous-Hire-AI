"""
Automated Workday password reset → normalize an account to the stored password.

Flow: Apply → Apply Manually → Sign in with email → "Forgot your password?" →
enter email → Reset Password → fetch the reset LINK from Gmail → open it →
set new password (= stored workday_password) twice → submit.

Use when an account exists but its password is unknown/ambiguous.

Usage:
    python scripts/workday_reset_password.py <workday_job_url>
"""
from __future__ import annotations

import asyncio
import base64
import re
import sys
import time
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.config import get_settings
from tools.browser import BrowserClient
from agents.application import form_filler as ff

_PROFILE = Path("data/user_profile.yaml")


def _emit(m: str) -> None:
    print(m, flush=True)


def _fetch_reset_link(not_before: float) -> str | None:
    """Fetch a Workday password-reset link from the applicant inbox."""
    try:
        cfg = get_settings()
        at = httpx.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "refresh_token", "refresh_token": cfg.gmail_refresh_token,
            "client_id": cfg.gmail_client_id, "client_secret": cfg.gmail_client_secret},
            timeout=10).json().get("access_token")
        if not at:
            return None
        H = {"Authorization": f"Bearer {at}"}
        sr = httpx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages",
                       params={"q": "newer_than:1h in:anywhere (password OR reset OR workday)",
                               "maxResults": 10}, headers=H, timeout=10).json()
        for msg in sr.get("messages", []):
            md = httpx.get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}",
                           params={"format": "full"}, headers=H, timeout=10).json()
            if int(md.get("internalDate", 0)) / 1000 < not_before:
                continue
            body = ""
            pl = md.get("payload", {})
            for part in (pl.get("parts") or [pl]):
                for blob in (part, *(part.get("parts") or [])):
                    data = (blob.get("body") or {}).get("data", "")
                    if data:
                        try: body += base64.urlsafe_b64decode(data + "==").decode("utf-8", "ignore")
                        except Exception: pass
            m = re.search(r'https://[^"\s>]*(?:resetPassword|password[/?=][^"\s>]*token|reset[^"\s>]*)', body, re.I) \
                or re.search(r'https://[^"\s>]*myworkdayjobs[^"\s>]*', body, re.I)
            if m:
                return m.group(0).replace("&amp;", "&")
    except Exception:
        pass
    return None


async def _click(page, sels, wait, tag) -> bool:
    for s in sels:
        try:
            b = page.locator(s).first
            if await b.count() > 0 and await b.is_visible(timeout=1500):
                try: await b.click(timeout=4000)
                except Exception: await b.evaluate("el => el.click()")
                await asyncio.sleep(wait); _emit(f"<<STEP>> {tag}"); return True
        except Exception:
            pass
    _emit(f"<<STEP>> {tag}: not found"); return False


async def main() -> None:
    url = sys.argv[1]
    get_settings()
    prof = yaml.safe_load(_PROFILE.read_text())
    p = prof["personal"]; defs = prof.get("application_defaults", {})
    email = p.get("email", ""); from core.config import get_settings as _gs
    pw = _gs().workday_password or defs.get("workday_password", "")

    _emit(f"<<START>> Workday password reset | {url[:60]}")
    async with BrowserClient(headless=False, profile_dir="data/chrome_profile_wdreset", patched=True) as b:
        page = await b.new_page(url); await asyncio.sleep(6)
        await _click(page, ["a[data-automation-id='adventureButton']", "button:has-text('Apply')"], 3, "Apply")
        await _click(page, ["[data-automation-id='applyManually']"], 5, "Apply Manually")
        await _click(page, ["button:has-text('Sign in with email')", "a:has-text('Sign in with email')"], 4, "Sign in with email")
        await _click(page, ["[data-automation-id='forgotPasswordLink']", "a:has-text('Forgot')"], 3, "Forgot password")

        ei = page.locator("[data-automation-id='email']").first
        if await ei.count() == 0:
            _emit("<<RESULT>> status=no_reset_form"); return
        await ei.fill(email); await asyncio.sleep(0.3)
        ts = time.time() - 30
        await _click(page, ["[data-automation-id='resetPasswordButton']", "button:has-text('Reset Password')"], 3, "Reset Password")
        _emit("<<STEP>> reset requested — fetching reset link from Gmail…")

        link = None
        for delay in (5, 6, 10, 16, 26, 40):
            await asyncio.sleep(delay)
            link = _fetch_reset_link(not_before=ts)
            if link:
                break
        if not link:
            _emit(f"<<RESULT>> status=reset_link_not_found (check {email})"); return
        _emit(f"<<STEP>> opening reset link: {link[:70]}")
        await page.goto(link, wait_until="domcontentloaded"); await asyncio.sleep(3.0)

        # Set the new password (= stored)
        np = page.locator("[data-automation-id='password'], input[type='password']").first
        if await np.count() == 0:
            _emit("<<RESULT>> status=reset_form_not_found_after_link"); return
        await np.fill(pw); await asyncio.sleep(0.2)
        vp = page.locator("[data-automation-id='verifyPassword']").first
        if await vp.count() > 0:
            await vp.fill(pw); await asyncio.sleep(0.2)
        await _click(page, ["[data-automation-id='submitButton']", "[data-automation-id='resetPasswordButton']",
                            "button:has-text('Submit')", "button:has-text('Change Password')",
                            "button:has-text('Reset')"], 3, "submit new password")
        body = (await page.inner_text("body")).lower()
        ok = any(k in body for k in ("success", "signed in", "my information", "your password has been",
                                     "start your application", "application"))
        _emit(f"<<RESULT>> status={'reset_ok — password now ' + pw if ok else 'reset_submitted (verify on screen)'}")
        await asyncio.sleep(2.0)


if __name__ == "__main__":
    asyncio.run(main())
