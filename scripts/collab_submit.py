"""
Collaborative live submission — one ATS form, with a human CAPTCHA handoff.

Flow (designed to be driven by an assistant via chat + a signal file):
  1. Tailor resume, open a VISIBLE browser, auto-fill the form.
  2. Screenshot + review; print the filled state and any still-empty required fields.
  3. Print  <<AWAITING_HUMAN>>  and wait for the proceed signal file
     (data/_collab_proceed). The human solves the CAPTCHA + does any manual fields,
     then the signal file is touched.
  4. Click Submit, handle Greenhouse email-verification (auto-fetch code from Gmail),
     capture the confirmation, print  <<RESULT ...>>  and log it.

Usage:
    python scripts/collab_submit.py <ats> <job_id>
The assistant launches this in the background, watches stdout for <<AWAITING_HUMAN>>,
asks the user to solve the CAPTCHA, then `touch data/_collab_proceed` to continue.
"""
from __future__ import annotations

import asyncio
import json
import os as _os
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_settings
from core.llm import get_llm
from tools.browser import BrowserClient
from tools import humanize
from tools.pdf_parser import markdown_to_pdf
from agents.application.resume_tailor import tailor_one
from agents.application.eligibility import (
    screen_eligibility, looks_like_non_form_url, page_closed_reason,
    already_applied_reason, NO_EMAIL_ATS)
from agents.application import form_filler as ff

_STATE_FILE   = Path("data/pipeline_state.json")
_PROFILE_PATH = Path("data/user_profile.yaml")
_PROCEED      = Path("data/_collab_proceed")
_STATUS       = Path("data/_collab_status.json")

_SUBMIT_SELECTORS = [
    "[data-automation-id='bottom-navigation-done-button']",  # Workday Review-page Submit
    "button[type='submit']", "input[type='submit']",
    "button:has-text('Submit Application')", "button:has-text('Submit application')",
    "button:has-text('Submit')", "button:has-text('Apply Now')",
    "button:has-text('Send Application')", "button:has-text('Complete Application')",
    "[data-qa='btn-submit']", "[data-test='btn-send-application']",
    "[data-testid='submit-application-button']", "[data-testid='apply-button']",
]
_SUCCESS_MARKERS = (
    "thank you", "thanks for applying", "application received", "successfully submitted",
    "application submitted", "we received your", "we've received your",
    "we have received your", "your application has been", "application complete",
    "you have applied", "you've applied", "we'll be in touch", "hear from us",
)
_CAPTCHA_SELECTORS = (
    "iframe[src*='hcaptcha']", "iframe[src*='recaptcha']", ".h-captcha", ".g-recaptcha",
    "[data-sitekey]", "iframe[src*='turnstile']", "iframe[src*='challenges.cloudflare']",
)


def _emit(msg: str) -> None:
    print(msg, flush=True)


def _status(**kw) -> None:
    try:
        _STATUS.write_text(json.dumps(kw))
    except Exception:
        pass


def _find_job(state: dict, jid: str) -> dict | None:
    for j in state.get("job_listings", []):
        if j.get("id") == jid:
            return dict(j)
    return None


async def _detect_captcha_type(page) -> str | None:
    """
    Classify the CAPTCHA so the flow can branch:
      'hcaptcha' / 'recaptcha_v2' / 'turnstile'  → human-solvable widget (pause)
      'recaptcha_v3'                              → invisible/score-based (no checkbox;
                                                     rely on the patched browser, submit)
      None                                        → none detected
    """
    frame_urls = []
    try:
        frame_urls = [(fr.url or "").lower() for fr in page.frames]
    except Exception:
        pass
    if any("hcaptcha" in u for u in frame_urls):
        return "hcaptcha"
    if any(("turnstile" in u) or ("challenges.cloudflare" in u) for u in frame_urls):
        return "turnstile"
    # reCAPTCHA v2 renders a visible checkbox in an api2/anchor iframe.
    if any("recaptcha/api2/anchor" in u for u in frame_urls):
        return "recaptcha_v2"
    try:
        if await page.locator(".h-captcha").count() > 0:
            return "hcaptcha"
        if await page.locator(".g-recaptcha:not(.g-recaptcha-v3)").count() > 0:
            return "recaptcha_v2"
        # v3: grecaptcha present (script/badge) but no visible anchor checkbox.
        v3 = await page.evaluate("""() => {
            const badge = document.querySelector('.grecaptcha-badge');
            const scr = Array.from(document.querySelectorAll('script[src*="recaptcha/api.js"]'))
                .some(s => /[?&]render=/.test(s.src));
            return !!(badge || scr);
        }""")
        if v3:
            return "recaptcha_v3"
        for s in _CAPTCHA_SELECTORS:
            if await page.locator(s).count() > 0:
                return "recaptcha_v2"
    except Exception:
        pass
    return None


async def _has_captcha(page) -> bool:
    return (await _detect_captcha_type(page)) is not None


async def _wait_for_proceed(timeout_s: int = 1200) -> bool:
    """Poll for the proceed signal file. Returns True if signalled, False on timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _PROCEED.exists():
            try:
                _PROCEED.unlink()
            except Exception:
                pass
            return True
        await asyncio.sleep(2)
    return False


async def main() -> None:
    ats, jid = sys.argv[1], sys.argv[2]
    get_settings()
    state = json.loads(_STATE_FILE.read_text())
    profile = yaml.safe_load(_PROFILE_PATH.read_text())
    job = _find_job(state, jid)
    if not job:
        _emit(f"<<ERROR>> job {jid} not found"); return

    # clear any stale signal
    if _PROCEED.exists():
        try: _PROCEED.unlink()
        except Exception: pass

    _emit(f"<<START>> {ats} | {job['company_name']} | {job['title']} | {job.get('location','')}")

    # ── #6 Aggregator / board-page URL check (before any work) ───────────────────
    nf = looks_like_non_form_url(job.get("apply_url", ""))
    if nf and "force=1" not in sys.argv:
        _emit(f"<<BLOCKED>> {nf}")
        _emit("<<RESULT>> status=not_an_application_form (skipped) — pass force=1 to override")
        _status(state="not_a_form", ats=ats, reason=nf)
        return

    # ── #1 Pre-submission eligibility screen (before any tailoring/filling) ──────
    elig = screen_eligibility(job, profile)
    for w in elig["warnings"]:
        _emit(f"<<WARN>> {w}")
    if not elig["eligible"]:
        for b in elig["blockers"]:
            _emit(f"<<BLOCKED>> {b}")
        _emit("<<RESULT>> status=ineligible (skipped before filling) — "
              "pass force=1 to override")
        if "force=1" not in sys.argv:
            _status(state="ineligible", ats=ats, blockers=elig["blockers"])
            return
        _emit("<<STEP>> force=1 — overriding eligibility block, proceeding anyway")

    _status(state="tailoring", ats=ats, company=job["company_name"], role=job["title"])

    llm = get_llm("collab")
    llm_cheap = get_llm("collab", model="claude-haiku-4-5")

    # #10 — reuse a previously-tailored resume for this job (re-runs are common with the
    # Workday human-gate). Skips the ~$0.13 LLM tailoring + web research on retries.
    # Override with COLLAB_REUSE_RESUME=0 to force a fresh tailor.
    _cached_md = Path(f"data/resumes/{job['id']}.md")
    if (_os.environ.get("COLLAB_REUSE_RESUME", "1") != "0"
            and _cached_md.exists() and _cached_md.stat().st_size > 500):
        _emit("<<STEP>> reusing cached tailored resume (set COLLAB_REUSE_RESUME=0 to re-tailor)")
        app = {"id": str(uuid.uuid4()), "tailored_resume_path": str(_cached_md),
               "quality_score": "cached"}
    else:
        _emit("<<STEP>> tailoring resume…")
        fit = {"id": str(uuid.uuid4()), "job_id": job["id"], "score": 82, "passed_threshold": True,
               "highlights": [], "missing_keywords": [], "model": "manual",
               "scored_at": datetime.now(timezone.utc).isoformat()}
        app = await tailor_one(job, fit, llm, profile, llm_cheap=llm_cheap)
        if not app:
            _emit("<<ERROR>> tailoring failed"); return
    resume_md = Path(app["tailored_resume_path"])
    try:
        resume_pdf = markdown_to_pdf(resume_md)
    except Exception:
        resume_pdf = resume_md
    _emit(f"<<STEP>> resume ready (quality={app.get('quality_score','?')})")

    filler = {"greenhouse": ff._fill_greenhouse, "lever": ff._fill_lever, "ashby": ff._fill_ashby,
              "smartrecruiters": ff._fill_smartrecruiters, "icims": ff._fill_icims,
              "workday": ff._fill_workday, "rippling": ff._fill_rippling}.get(ats, ff._fill_generic)

    # patchright (patched Playwright) + persistent real-Chrome profile: closes the CDP
    # Runtime.enable leak that hCaptcha-Enterprise (Lever) detects, on top of the
    # profile reputation. Best available shot at passing enterprise verification.
    _profile = _os.environ.get("COLLAB_PROFILE_DIR", "data/chrome_profile")
    async with BrowserClient(headless=False, profile_dir=_profile, patched=True) as browser:
        # ashby application form lives at /application
        url = job["apply_url"]
        if ats == "ashby" and not url.rstrip("/").endswith("application"):
            url = url.rstrip("/") + "/application"
        page = await browser.new_page(url)
        # Workday/iCIMS render slowly and gate the form behind an Apply/account step,
        # so give them longer before judging "no form".
        await asyncio.sleep(5.0 if ats in ("workday", "icims") else 2.5)

        # ── #6 Runtime check: closed posting or no real form? (before filling) ───
        try:
            body = await page.inner_text("body")
        except Exception:
            body = ""
        dup = already_applied_reason(body)
        if dup:
            _emit(f"<<BLOCKED>> {dup}")
            _emit("<<RESULT>> status=already_applied (skipped)")
            _status(state="already_applied", ats=ats, reason=dup)
            try: await page.context.close()
            except Exception: pass
            return
        closed = page_closed_reason(body)
        try:
            n_inputs = await page.locator(
                "input:not([type=hidden]):visible, textarea:visible, select:visible, "
                "[contenteditable='true']").count()
            # A real posting may gate its form behind an Apply/Sign-in/account button.
            has_gate = await page.locator(
                "a[data-automation-id='adventureButton'], button:has-text('Apply'), "
                "a:has-text('Apply'), button:has-text('Sign In'), button:has-text('Create Account'), "
                "button:has-text('Apply Manually')").count()
        except Exception:
            n_inputs, has_gate = 0, 0
        # Skip only a genuinely empty/closed page: no fields AND no apply gate AND not a
        # multi-step ATS whose form appears after navigation.
        empty_no_gate = n_inputs < 2 and not has_gate and ats not in ("workday", "icims")
        if (closed or empty_no_gate) and "force=1" not in sys.argv:
            reason = closed or f"only {n_inputs} form field(s) and no apply button — not an application form"
            _emit(f"<<BLOCKED>> {reason}")
            _emit("<<RESULT>> status=no_form (skipped) — pass force=1 to override")
            _status(state="no_form", ats=ats, reason=reason)
            try: await page.context.close()
            except Exception: pass
            return

        _emit("<<STEP>> auto-filling form…")
        _status(state="filling", ats=ats)
        try:
            await asyncio.wait_for(filler(page, profile, resume_pdf, job, llm),
                                   timeout=int(_os.environ.get("COLLAB_FILLER_TIMEOUT", "320")))
        except Exception as e:
            _emit(f"<<STEP>> fill note: {str(e)[:100]}")
        await asyncio.sleep(1.0)

        # ── Auth blocked? (#2/#3) Workday couldn't get past the sign-in gate. ──────
        auth_blocked = getattr(page, "_wd_auth_blocked", None)
        if ats == "workday" and (auth_blocked or await ff._wd_on_signin_page(page)):
            reason = (auth_blocked or {}).get("reason", "Workday sign-in not authenticated.")
            has_sso = (auth_blocked or {}).get("sso", await ff._wd_sso_available(page))
            shot = await ff._screenshot(page, app["id"])
            _emit(f"<<BLOCKED>> {reason}")
            hint = ("Run once with WORKDAY_GOOGLE_LOGIN=interactive and complete the Google "
                    "login in the window." if has_sso else
                    "This tenant has no SSO — create the Workday account once by hand, then re-run.")
            _emit(f"<<NEEDS_AUTH>> {hint}")
            _emit("<<RESULT>> status=needs_auth")
            _status(state="needs_auth", ats=ats, reason=reason, sso=has_sso, screenshot=shot)
            try: await page.context.close()
            except Exception: pass
            return

        # Human warmup — natural scrolling + mouse entropy so hCaptcha's passive
        # behavioral analysis sees a human-like session, not an instant scripted fill.
        try:
            for _ in range(3):
                await humanize.scroll_down_human(page, 350)
                await humanize.move_mouse_human(page, random.randint(300, 1100), random.randint(200, 700))
            await page.evaluate("window.scrollTo(0, 0)")
            await humanize.page_think_pause()
        except Exception:
            pass

        shot = await ff._screenshot(page, app["id"])
        review = await ff._review_form(page, job, ats)
        empties = [e for e in review.get("empty_required", []) if (e or "").strip()]
        cap_type = await _detect_captcha_type(page)
        # reCAPTCHA v3 is invisible/score-based — no checkbox to solve; rely on the
        # patched browser and submit. v2 / hCaptcha / Turnstile need a human solve.
        human_captcha = cap_type in ("hcaptcha", "recaptcha_v2", "turnstile")
        _emit(f"<<FILLED>> {len(review['filled'])} fields | empty_required={len(empties)} | "
              f"captcha={cap_type or 'none'}")
        for e in empties:
            _emit(f"<<TODO>> {e}")
        if shot:
            _emit(f"<<SHOT>> {shot}")
        _status(state="awaiting_human", ats=ats, filled=len(review["filled"]),
                empties=empties, captcha=cap_type, screenshot=shot)

        # Batch/test mode: capture the outcome and close WITHOUT the human gate or submit.
        if _os.environ.get("COLLAB_NO_WAIT"):
            try:
                reached = ats == "workday" and "step6" in (await ff._wd_step_index(page))
            except Exception:
                reached = False
            outcome = ("reached_review" if (reached or not empties) else
                       f"stuck:{len(empties)}_empty")
            _emit(f"<<RESULT>> status=filled outcome={outcome} empties={len(empties)} "
                  f"captcha={cap_type or 'none'}")
            _status(state="filled_no_submit", ats=ats, outcome=outcome,
                    empties=empties, screenshot=shot)
            try: await page.context.close()
            except Exception: pass
            return

        if human_captcha:
            _emit(f"<<AWAITING_HUMAN>> Solve the {cap_type} CAPTCHA + any listed fields, then signal proceed.")
        elif cap_type == "recaptcha_v3":
            _emit("<<AWAITING_HUMAN>> Invisible reCAPTCHA v3 (no checkbox) — review fields, "
                  "then signal proceed to submit (patched browser handles the score).")
        else:
            _emit("<<AWAITING_HUMAN>> No CAPTCHA detected — review fields, then signal proceed to submit.")
        ok = await _wait_for_proceed()
        if not ok:
            _emit("<<RESULT>> status=timeout (no proceed signal within 20 min)")
            return

        _emit("<<STEP>> submitting…")
        _status(state="submitting", ats=ats)

        # Choose submit context: greenhouse embed iframe or main page
        submit_ctx = page
        for fr in page.frames:
            if "greenhouse.io/embed" in (fr.url or ""):
                submit_ctx = fr
                break

        url_before = page.url
        clicked = False
        for sel in _SUBMIT_SELECTORS:
            try:
                btn = submit_ctx.locator(sel).first
                if await btn.count() > 0 and await btn.is_enabled():
                    # Human-like: move the mouse to the button along a curve, then click.
                    box = None
                    try:
                        box = await btn.bounding_box()
                        if box:
                            await humanize.move_mouse_human(
                                page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                            await asyncio.sleep(0.3)
                    except Exception:
                        pass
                    # Workday's click_filter overlay intercepts a direct .click() (no-op),
                    # so submit via a trusted coordinate click on the button centre.
                    if ats == "workday" and box:
                        await page.mouse.click(box["x"] + box["width"] / 2,
                                               box["y"] + box["height"] / 2)
                    else:
                        await btn.click()
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            _emit("<<RESULT>> status=submit_button_not_found")
            return
        await asyncio.sleep(7.0)

        # Greenhouse email-verification handoff (auto-fetch the 8-char code from Gmail)
        try:
            body = (await submit_ctx.inner_text("body")).lower()
        except Exception:
            body = ""
        needs_code = any(k in body for k in (
            "verification code was sent", "security code", "verify your email",
            "check your email", "enter the 8-character code", "enter the 8 character code"))
        if needs_code:
            # Gmail token health-check — fail loudly with the fix, not silently.
            if not ff.gmail_token_healthy():
                _emit("<<RESULT>> status=email_verification_pending — Gmail token invalid/expired; "
                      "run: python scripts/gmail_reauth.py, then re-run")
                _status(state="gmail_token_invalid", ats=ats)
                await ff._capture_confirmation(page, app["id"])
                return
            _emit("<<STEP>> email verification required — fetching code from Gmail…")
            submit_ts = time.time() - 30
            code = None
            for delay in (3, 5, 8, 13, 21, 34, 55):
                await asyncio.sleep(delay)
                code = await ff._fetch_greenhouse_verification_code(not_before=submit_ts)
                if code:
                    break
            if code:
                try:
                    ci = page.locator("input[name*='code'], input[id*='code'], "
                                      "input[placeholder*='code'], input[type='text']:visible").first
                    if await ci.count() > 0:
                        await ci.fill(code)
                        await asyncio.sleep(0.4)
                        for sel in _SUBMIT_SELECTORS + ["button:visible"]:
                            b = submit_ctx.locator(sel).first
                            if await b.count() > 0 and await b.is_enabled():
                                await b.click(); await asyncio.sleep(3.0); break
                        _emit(f"<<STEP>> verification code entered: {code}")
                except Exception as e:
                    _emit(f"<<STEP>> code entry failed: {str(e)[:80]}")
            else:
                _emit("<<RESULT>> status=email_verification_pending (code not found in Gmail)")
                conf = await ff._capture_confirmation(page, app["id"])
                return

        conf = await ff._capture_confirmation(page, app["id"])
        try:
            full = (await submit_ctx.inner_text("body")).lower()
        except Exception:
            full = ""
        url_after = page.url
        # Duplicate detection on the post-submit page (some ATS reject re-applies here).
        dup_after = already_applied_reason(full)
        if dup_after:
            _emit(f"<<RESULT>> status=already_applied ({dup_after})")
            _status(state="already_applied", ats=ats)
            await asyncio.sleep(2.0)
            return
        success = (url_after != url_before or any(m in full for m in _SUCCESS_MARKERS)
                   or any(k in url_after.lower() for k in ("confirm", "success", "applied", "thank")))

        if success:
            # No-email ATS (ashby/rippling/smartrecruiters) verify on-screen only.
            verify = (conf or "on-screen success (no confirmation email for this ATS)"
                      if ats in NO_EMAIL_ATS else conf or "(captured screenshot)")
            ff._log_applied({
                "application_id": app["id"], "job_id": job["id"], "company": job["company_name"],
                "role": job["title"], "ats": ats, "apply_url": job["apply_url"],
                "quality_score": app.get("quality_score", 0), "confirmation_url": verify,
                "submitted_at": datetime.now(timezone.utc).isoformat(), "submit_mode": "collab"})
            ff._record_ats_outcome(job["company_name"], ats, submitted=True)
            _emit(f"<<RESULT>> status=submitted confirmation={verify}")
            if ats in NO_EMAIL_ATS:
                _emit(f"<<NOTE>> {ats} sends no confirmation email — verified on-screen only")
            _status(state="submitted", ats=ats, confirmation=verify)
        else:
            _emit("<<RESULT>> status=no_confirmation_signal (check the browser)")
            _status(state="no_signal", ats=ats)

        await asyncio.sleep(3.0)


if __name__ == "__main__":
    asyncio.run(main())
