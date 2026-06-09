"""
Veeva Lever application helper — handles hCaptcha-first flow.

New Lever flow:
  Phase 1 → Browser opens → hCaptcha appears → You solve it
  Phase 2 → Form loads  → Automation fills everything → You review & submit

Usage:
    python -m tools.veeva_apply 1
    python -m tools.veeva_apply 2
"""

import asyncio
import sys
import time
import yaml
from pathlib import Path
from playwright.async_api import async_playwright
from tools.pdf_parser import markdown_to_pdf

JOBS = [
    {
        "url":    "https://jobs.lever.co/veeva/026aba61-8d3a-4f62-8af3-cbae8321f5cf",
        "resume": "data/resumes/f804e09d-f81b-4cc7-89c4-2e9848f1ee09.md",
        "role":   "Senior AI Data Engineer (1)",
    },
    {
        "url":    "https://jobs.lever.co/veeva/2bfc3e0b-cab1-4210-a9b4-f85075af75fd",
        "resume": "data/resumes/0031129c-1918-4335-910f-9fa9d127df42.md",
        "role":   "Senior AI Data Engineer (2)",
    },
]

PROFILE = yaml.safe_load(Path("data/user_profile.yaml").read_text())
P    = PROFILE["personal"]
DEFS = PROFILE.get("application_defaults", {})

SENTINEL_CAPTCHA = Path("/tmp/veeva_captcha_done.flag")
SENTINEL_SUBMIT  = Path("/tmp/veeva_done.flag")


def _wait_for_flag(flag: Path, msg: str):
    flag.unlink(missing_ok=True)
    print(f"\n{'='*60}")
    print(msg)
    print(f"  Then signal with:  touch {flag}")
    print('='*60)
    try:
        while not flag.exists():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    flag.unlink(missing_ok=True)


async def fill_all(page, resume_pdf: Path | None):
    """Fill every visible field on the Lever application form."""

    async def try_fill(sel: str, val: str):
        if not val:
            return
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.fill(val)
        except Exception:
            pass

    # ── Standard Lever fields ───────────────────────────────────────────────
    await try_fill("input[name='name']",           P.get("name", ""))
    await try_fill("input[placeholder*='Name']",   P.get("name", ""))
    await try_fill("input[name='email']",          P.get("email", ""))
    await try_fill("input[placeholder*='mail']",   P.get("email", ""))
    await try_fill("input[name='phone']",          P.get("phone", ""))
    await try_fill("input[placeholder*='Phone']",  P.get("phone", ""))
    await try_fill("input[name='org']",            DEFS.get("current_company", "JPMorgan Chase"))
    await try_fill("input[placeholder*='ompany']", DEFS.get("current_company", "JPMorgan Chase"))
    await try_fill("input[name='urls[LinkedIn]']", P.get("linkedin_url", ""))
    await try_fill("input[placeholder*='inkedIn']",P.get("linkedin_url", ""))
    await try_fill("input[name='urls[GitHub]']",   P.get("github_url", ""))
    await try_fill("input[placeholder*='itHub']",  P.get("github_url", ""))
    await try_fill("input[name='urls[Portfolio]']",P.get("github_url", ""))
    await try_fill("input[name='location']",       "Plano, TX")
    await try_fill("input[placeholder*='ocation']","Plano, TX")
    await asyncio.sleep(0.4)

    # ── Resume upload ────────────────────────────────────────────────────────
    if resume_pdf and resume_pdf.exists():
        try:
            up = page.locator("input[type='file']").first
            if await up.count() > 0:
                await up.set_input_files(str(resume_pdf))
                print("  Resume uploaded.")
                await asyncio.sleep(1.5)
        except Exception as e:
            print(f"  Resume upload failed: {e}")

    # ── Generic label-based scan: all remaining inputs & selects ────────────
    # Covers any custom question containers Lever uses
    containers = await page.locator(
        ".application-question, .field, [class*='question'], [class*='field-row']"
    ).all()

    for ctr in containers:
        try:
            lbl_el = ctr.locator("label").first
            lbl = (await lbl_el.inner_text()).strip().lower() if await lbl_el.count() > 0 else ""
            if not lbl:
                continue

            # --- text / textarea ---
            inp = ctr.locator("input[type='text'], input:not([type]), textarea").first
            if await inp.count() > 0 and await inp.is_visible():
                cur = await inp.input_value()
                if cur:
                    continue
                val = _answer_for_label(lbl)
                if val:
                    await inp.fill(val)
                    await asyncio.sleep(0.2)
                continue

            # --- native select ---
            sel_el = ctr.locator("select").first
            if await sel_el.count() > 0 and await sel_el.is_visible():
                cur = await sel_el.input_value()
                if cur and cur not in ("", "0"):
                    continue
                opts = await sel_el.locator("option").all()
                opt_texts = [(await o.inner_text()).strip() for o in opts]
                chosen = _choose_option(lbl, opt_texts)
                if chosen:
                    await sel_el.select_option(label=chosen)
                    await asyncio.sleep(0.2)
        except Exception:
            pass

    await asyncio.sleep(0.5)
    print("  Form filled. Scroll through the form to review before submitting.")


def _answer_for_label(lbl: str) -> str | None:
    """Return the right answer string for a given label."""
    if "linkedin" in lbl:
        return P.get("linkedin_url", "")
    if "github" in lbl or "portfolio" in lbl or "website" in lbl:
        return P.get("github_url", "")
    if "salary" in lbl or "compensation" in lbl or "expected" in lbl:
        return DEFS.get("salary_display", "$150,000 – $200,000")
    if "start" in lbl or "available" in lbl or "notice" in lbl:
        return DEFS.get("start_date", "Immediately")
    if "hear" in lbl or "referral" in lbl or "how did" in lbl or "source" in lbl:
        return DEFS.get("referral_source", "LinkedIn")
    if "city" in lbl or "location" in lbl:
        return "Plano, TX"
    if "phone" in lbl:
        return P.get("phone", "")
    if "name" in lbl and "company" not in lbl and "last" not in lbl:
        return P.get("name", "")
    if "company" in lbl or "employer" in lbl or "organization" in lbl:
        return DEFS.get("current_company", "JPMorgan Chase")
    if "email" in lbl:
        return P.get("email", "")
    if "sponsorship" in lbl or "visa" in lbl or "authoriz" in lbl or "lawfully" in lbl:
        return "Yes"
    return None


def _choose_option(lbl: str, opts: list[str]) -> str | None:
    """Pick the best option from a select for the given label."""
    def first(pred):
        return next((t for t in opts if pred(t.lower())), None)

    if "sponsorship" in lbl or "visa" in lbl or "authoriz" in lbl:
        return first(lambda t: t.startswith("yes"))
    if "relocat" in lbl or "commute" in lbl or "willing" in lbl:
        return first(lambda t: t.startswith("yes"))
    if "gender" in lbl:
        return first(lambda t: t == "male" or t == "man")
    if "race" in lbl or "ethnicity" in lbl:
        return first(lambda t: "asian" in t)
    if "veteran" in lbl:
        return first(lambda t: "not" in t or "no" in t)
    if "disability" in lbl:
        return first(lambda t: t.startswith("no") or "do not" in t)
    if "hear" in lbl or "source" in lbl or "referral" in lbl:
        return first(lambda t: "linkedin" in t)
    return None


async def run(job_idx: int):
    job = JOBS[job_idx]
    print(f"\nApplying to: {job['role']}")
    print(f"URL: {job['url']}")

    # Generate PDF
    try:
        resume_pdf = markdown_to_pdf(job["resume"])
        print(f"  PDF: {resume_pdf}")
    except Exception as e:
        print(f"  PDF generation failed: {e}")
        resume_pdf = Path(job["resume"]).with_suffix(".pdf")
        if not resume_pdf.exists():
            resume_pdf = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=60)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        print("\nOpening browser...")
        await page.goto(job["url"], timeout=30000)
        await asyncio.sleep(2.0)

        # Click Apply if on the job description landing page
        for sel in ["a[href*='/apply']", "a:has-text('Apply')", "button:has-text('Apply')"]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(2.5)
                    break
            except Exception:
                pass

        # ── PHASE 1: hCaptcha gate ───────────────────────────────────────────
        # On Lever's updated flow, hCaptcha appears before the application form.
        captcha_present = await page.locator(
            "iframe[src*='hcaptcha'], .h-captcha, #h-captcha, [data-hcaptcha]"
        ).count() > 0

        if captcha_present:
            _wait_for_flag(
                SENTINEL_CAPTCHA,
                "PHASE 1 — Solve the hCaptcha in the browser window.\n"
                "  After solving, the application form should appear."
            )
            await asyncio.sleep(2.0)  # let the form render after captcha
        else:
            print("  No captcha detected — proceeding directly to form fill.")

        # ── PHASE 2: Fill the form ───────────────────────────────────────────
        print("\nFilling form fields...")
        await fill_all(page, resume_pdf)

        # ── PHASE 3: User reviews and submits ───────────────────────────────
        _wait_for_flag(
            SENTINEL_SUBMIT,
            "PHASE 2 — Review all filled fields, then click Submit.\n"
            "  Signal when you have submitted (or want to close)."
        )

        final_url = page.url
        print(f"\nFinal URL: {final_url}")
        await browser.close()

    print(f"\nDone: {job['role']}")


if __name__ == "__main__":
    idx = int(sys.argv[1]) - 1 if len(sys.argv) > 1 else 0
    asyncio.run(run(idx))
