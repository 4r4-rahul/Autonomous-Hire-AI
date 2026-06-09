"""
Semi-automatic e2e verification of job-application submission, one per ATS type.

For each ATS, the script:
  1. Tailors a resume for a real, US-based queued job.
  2. Opens a VISIBLE browser, auto-fills the form using the production filler.
  3. Re-reviews the form and prints which required fields are still empty.
  4. Detects CAPTCHAs and PAUSES — you solve the CAPTCHA, fix any flagged
     fields, and click Submit yourself.
  5. After you press Enter, it captures a confirmation screenshot and checks
     for success signals, then logs the result.

Nothing is submitted automatically — you are the final click. This verifies the
full pipeline (tailor → fill → review → submit → confirm) end-to-end per form
type while protecting your real applications from CAPTCHA/validation gaps.

Run:
    python scripts/verify_ats_e2e.py
    python scripts/verify_ats_e2e.py workday=https://...  icims=https://...
        (pass apply URLs for ATS types with no queued job)

Ctrl+C stops after the current form.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_settings
from core.llm import get_llm
from tools.browser import BrowserClient
from tools.pdf_parser import markdown_to_pdf
from agents.application.resume_tailor import tailor_one
from agents.application import form_filler as ff

_STATE_FILE   = Path("data/pipeline_state.json")
_APPLIED_FILE = Path("data/applied_jobs.json")
_PROFILE_PATH = Path("data/user_profile.yaml")

# One real, US-based queued job per ATS type (work-auth answers stay correct).
# Override / extend with CLI args:  ats=<apply_url>
SEED_JOBS: dict[str, str] = {
    "greenhouse": "d6c33049-3876-416a-862a-c754c937bc9d",  # Robinhood (Menlo Park, CA)
    "lever":      "4da45930-f4c0-4eb3-ba5f-c7af676b1cd6",  # Palantir (New York, NY)
    "ashby":      "c1590b1b-d238-4a6c-8944-7e55d6a120ad",  # River (US/Remote)
    "rippling":   "7606bf0f-7e51-4f79-98c7-fa78507f860d",  # Piedmont Global (US Remote)
}

# CAPTCHA fingerprints — element selectors + body-text markers.
_CAPTCHA_SELECTORS = (
    "iframe[src*='hcaptcha']", "iframe[src*='recaptcha']",
    "iframe[title*='captcha' i]", ".h-captcha", ".g-recaptcha",
    "[data-sitekey]", "iframe[src*='turnstile']", "iframe[src*='challenges.cloudflare']",
)
_CAPTCHA_TEXT = (
    "find all", "select all images", "i'm not a robot", "verify you are human",
    "confirm you're a human", "select each image", "click verify",
)


def _load_state() -> dict:
    return json.loads(_STATE_FILE.read_text())


def _find_job(state: dict, job_id: str) -> dict | None:
    for j in state.get("job_listings", []):
        if j.get("id") == job_id:
            return dict(j)
    return None


def _synthetic_job(ats: str, url: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "company_name": f"({ats} test)",
        "title": "Software Engineer",
        "location": "United States (Remote)",
        "description": "Senior software / data / platform engineering role.",
        "apply_url": url,
    }


async def _detect_captcha(page) -> bool:
    for sel in _CAPTCHA_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            pass
    # Also scan visible frames + body text
    try:
        for fr in page.frames:
            if any(k in (fr.url or "").lower() for k in ("hcaptcha", "recaptcha", "turnstile")):
                return True
    except Exception:
        pass
    try:
        body = (await page.inner_text("body")).lower()
        if any(t in body for t in _CAPTCHA_TEXT):
            return True
    except Exception:
        pass
    return False


def _ainput(prompt: str) -> "asyncio.Future[str]":
    """Non-blocking input() so the event loop (and browser) stays responsive."""
    return asyncio.get_event_loop().run_in_executor(None, input, prompt)


async def _success_signals(page) -> tuple[bool, str]:
    try:
        text = (await page.inner_text("body")).lower()
    except Exception:
        text = ""
    markers = (
        "thank you", "thanks for applying", "application received",
        "successfully submitted", "application submitted", "we received your",
        "we've received your", "we have received your", "your application has been",
        "application complete", "you have applied", "you've applied",
        "we'll be in touch", "hear from us",
    )
    url = (page.url or "").lower()
    hit = next((m for m in markers if m in text), None)
    if hit:
        return True, f"page text: '{hit}'"
    if any(k in url for k in ("confirm", "success", "applied", "thank")):
        return True, f"url: {url[:70]}"
    return False, ""


async def verify_one(browser: BrowserClient, ats: str, job: dict, profile: dict,
                     llm_tailor, llm_cheap, idx: int, total: int) -> dict:
    company, role = job["company_name"], job["title"]
    print("\n" + "─" * 74)
    print(f"  [{idx}/{total}]  {ats.upper()}  —  {company} | {role}")
    print(f"  location: {job.get('location','?')}")
    print(f"  url: {job['apply_url'][:72]}")
    print("─" * 74)

    # 1. Tailor resume → PDF
    print("  → tailoring resume…")
    fit = {
        "id": str(uuid.uuid4()), "job_id": job["id"], "score": 80,
        "passed_threshold": True, "highlights": [], "missing_keywords": [],
        "model": "manual", "scored_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        app = await tailor_one(job, fit, llm_tailor, profile, llm_cheap=llm_cheap)
    except Exception as e:
        print(f"  ✗ tailor failed: {e}")
        return {"ats": ats, "company": company, "status": "tailor_failed"}
    if not app:
        return {"ats": ats, "company": company, "status": "tailor_none"}
    resume_md = Path(app["tailored_resume_path"])
    try:
        resume_pdf = markdown_to_pdf(resume_md)
    except Exception:
        resume_pdf = resume_md
    print(f"  → resume ready (quality={app.get('quality_score','?')})")

    # 2. Open page + auto-fill with the production filler
    filler = {
        "greenhouse": ff._fill_greenhouse, "lever": ff._fill_lever,
        "workday": ff._fill_workday, "ashby": ff._fill_ashby,
        "smartrecruiters": ff._fill_smartrecruiters, "icims": ff._fill_icims,
        "bamboohr": ff._fill_bamboohr, "rippling": ff._fill_rippling,
        "generic": ff._fill_generic,
    }.get(ats, ff._fill_generic)

    page = await browser.new_page(job["apply_url"])
    await asyncio.sleep(2.0)
    print("  → auto-filling form…")
    try:
        await asyncio.wait_for(filler(page, profile, resume_pdf, job, llm_tailor), timeout=240)
    except asyncio.TimeoutError:
        print("  ⚠ fill exceeded 4 min — continuing to review anyway")
    except Exception as e:
        print(f"  ⚠ fill error: {str(e)[:120]}")
    await asyncio.sleep(1.0)
    await ff._screenshot(page, app["id"])

    # 3. Review — surface empty required fields for manual completion
    review = await ff._review_form(page, job, ats)
    print(f"  → filled {len(review['filled'])} fields")
    empties = [e for e in review.get("empty_required", []) if (e or "").strip()]
    if empties:
        print(f"  ⚠ {len(empties)} REQUIRED field(s) need your input:")
        for lbl in empties:
            print(f"       • {lbl}")
        print("    (Review the auto-filled EEO answers too — gender=Male/Cisgender man,")
        print("     race=Asian/South Asian — and adjust if a form word it differently.)")
    else:
        print("  ✓ no empty required fields detected")

    # 4. CAPTCHA?
    has_captcha = await _detect_captcha(page)
    if has_captcha:
        print("  🔒 CAPTCHA detected — you must solve it before submitting.")

    # 5. Human pause
    print("\n  ┌─ MANUAL STEP ─────────────────────────────────────────────────")
    print("  │ In the open browser:")
    if empties:
        print("  │   1. Fill any required fields listed above")
    if has_captcha:
        print("  │   2. Solve the CAPTCHA")
    print("  │   3. Click Submit and wait for the confirmation page")
    print("  │ Then press ENTER here (or type 's' + ENTER to skip this one).")
    print("  └───────────────────────────────────────────────────────────────")
    choice = (await _ainput("  > ")).strip().lower()
    if choice == "s":
        await page.context.close()
        return {"ats": ats, "company": company, "status": "skipped_by_user"}

    # 6. Verify outcome
    await asyncio.sleep(1.0)
    conf = await ff._capture_confirmation(page, app["id"])
    ok, why = await _success_signals(page)
    status = "submitted" if ok else "no_confirmation_signal"
    print(f"  {'✅' if ok else '❓'} {status}" + (f"  ({why})" if why else ""))

    if ok:
        ff._log_applied({
            "application_id": app["id"], "job_id": job["id"], "company": company,
            "role": role, "ats": ats, "apply_url": job["apply_url"],
            "quality_score": app.get("quality_score", 0),
            "confirmation_url": conf or "",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "submit_mode": "semi_auto_verify",
        })
        ff._record_ats_outcome(company, ats, submitted=True)

    await asyncio.sleep(0.5)
    await page.context.close()
    return {"ats": ats, "company": company, "role": role, "status": status,
            "confirmation": conf or ""}


async def main() -> None:
    get_settings()
    state = _load_state()
    profile = yaml.safe_load(_PROFILE_PATH.read_text())
    llm_tailor = get_llm("verify")                          # default (opus)
    llm_cheap  = get_llm("verify", model="claude-haiku-4-5")

    # Build the work list: seed jobs + any CLI-supplied URLs
    cli_urls = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
    items: list[tuple[str, dict]] = []
    for ats, job_id in SEED_JOBS.items():
        if ats in cli_urls:
            continue  # CLI override handled below
        job = _find_job(state, job_id)
        if job:
            items.append((ats, job))
        else:
            print(f"  ⚠ {ats}: seed job {job_id[:8]} not found — skipping")
    for ats, url in cli_urls.items():
        job = _synthetic_job(ats, url)
        items.append((ats, job))

    print("\n" + "═" * 74)
    print("  AutonomousHire — Semi-Auto e2e ATS Verification")
    print(f"  {len(items)} form(s) — browser visible, you make the final click")
    print("═" * 74)
    for ats, job in items:
        print(f"    {ats:<14} {job['company_name'][:30]:<30} {job['title'][:30]}")

    results: list[dict] = []
    async with BrowserClient(headless=False) as browser:
        for i, (ats, job) in enumerate(items, 1):
            try:
                results.append(await verify_one(browser, ats, job, profile,
                                                 llm_tailor, llm_cheap, i, len(items)))
            except KeyboardInterrupt:
                print("\n  Interrupted — stopping after this form.")
                break
            except Exception as e:
                print(f"  ✗ {ats} error: {str(e)[:140]}")
                results.append({"ats": ats, "company": job["company_name"], "status": "error"})

    print("\n\n" + "═" * 74)
    print("  Verification Summary")
    print("═" * 74)
    print(f"  {'ATS':<14} {'Company':<30} {'Result'}")
    print(f"  {'─'*13} {'─'*29} {'─'*22}")
    for r in results:
        icon = {"submitted": "✅", "skipped_by_user": "⏭ ",
                "no_confirmation_signal": "❓"}.get(r["status"], "❌")
        print(f"  {r['ats']:<14} {r['company'][:30]:<30} {icon} {r['status']}")
    n_ok = sum(1 for r in results if r["status"] == "submitted")
    print(f"\n  {n_ok}/{len(results)} confirmed submitted (you clicked submit)\n")


if __name__ == "__main__":
    asyncio.run(main())
