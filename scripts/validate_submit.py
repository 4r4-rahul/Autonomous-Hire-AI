"""Automated reviewer/validator gate: for each application, ensure it's at Review, run
_workday_validate_review (a hard checklist: at-review, no errors, identity, work-auth,
experience, education, EEO, source), print a pass/fail report, and submit ONLY the ones
that pass every check. Validate-only by default; set SUBMIT_VALIDATED=1 to submit passes."""
import asyncio, os, sys
from datetime import datetime, timezone
import yaml
sys.path.insert(0, ".")
os.environ.setdefault("WORKDAY_GOOGLE_LOGIN", "auto")
from tools.browser import BrowserClient
from tools.pdf_parser import markdown_to_pdf
from core.llm import get_llm
from agents.application import form_filler as ff

NV = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job"
JOBS = [
    ("Senior Data Analysis Engineer, AD Metrics",
     f"{NV}/US-CA-Santa-Clara/Senior-Data-Analysis-Engineer--AD-Metrics---Autonomous-Vehicles_JR2016051"),
    ("Senior Technical Data Analyst - Operations E2E Data",
     f"{NV}/US-CA-Santa-Clara/Senior-Technical-Data-Analyst---Operations-E2E-Data-Intelligent-Systems_JR2018470"),
    ("Senior Applied AI and AI Infrastructure Engineer",
     f"{NV}/US-CA-Santa-Clara/Senior-Applied-AI-and-AI-Infrastructure-Engineer---Chip-Design-and-DFX_JR2018256"),
    ("Senior Data Center Connectivity Engineer",
     f"{NV}/US-CA-Remote/Senior-Data-Center-Connectivity-Engineer_JR2018272"),
]
SUBMIT = os.environ.get("SUBMIT_VALIDATED") == "1"
STOP = "/tmp/park_stop"


async def main():
    profile = yaml.safe_load(open("data/user_profile.yaml"))
    resume_pdf = markdown_to_pdf("data/resume_base.md")
    llm = get_llm("collab", model="claude-haiku-4-5")
    try: os.unlink(STOP)
    except OSError: pass

    print(f"MODE: {'VALIDATE + SUBMIT passes' if SUBMIT else 'VALIDATE ONLY (no submit)'}", flush=True)
    async with BrowserClient(headless=False, profile_dir="data/chrome_profile", patched=True) as b:
        ctx = b._context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for i, (title, url) in enumerate(JOBS):
            os.environ["WORKDAY_GOOGLE_LOGIN"] = "interactive" if i == 0 else "auto"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.locator(
                    "a[data-automation-id='adventureButton'], button:has-text('Apply'), "
                    "[data-automation-id='legalNameSection_firstName']").first.wait_for(
                    state="visible", timeout=30000)
            except Exception:
                pass
            await asyncio.sleep(2)
            job = {"id": f"val-{i}", "company_name": "NVIDIA", "title": title,
                   "description": f"{title} at NVIDIA. Python, data/ML infrastructure.",
                   "apply_url": url}
            # Ensure the application is at Review (resume + complete any remaining steps).
            try:
                await asyncio.wait_for(ff._fill_workday(page, profile, resume_pdf, job, llm),
                                       timeout=480 if i == 0 else 360)
            except Exception:
                pass

            v = await ff._workday_validate_review(page, profile)
            verdict = "✅ VALID" if v["valid"] else "❌ ISSUES"
            print(f"\n=== [{verdict}] {title} ===", flush=True)
            for c in v["checks"]:
                mark = "ok " if c["ok"] else "FAIL"
                print(f"   [{mark}] {c['name']}{' — ' + c['detail'] if c['detail'] else ''}", flush=True)

            if v["valid"] and SUBMIT:
                try:
                    btn = page.locator("[data-automation-id='bottom-navigation-done-button'], "
                                       "button:has-text('Submit')").first
                    box = await btn.bounding_box()
                    if box:
                        await page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
                        await asyncio.sleep(6)
                    after = (await page.inner_text("body")).lower()
                    ok = any(m in after for m in ("you have applied", "application received",
                                                  "thank you", "submitted", "congratulations"))
                    print(f"   >>> SUBMITTED — confirmation={'yes' if ok else 'check screen'}", flush=True)
                    ff._log_applied({
                        "job_id": job["id"], "company": "NVIDIA", "role": title, "ats": "workday",
                        "apply_url": url, "confirmation_url": "validated+submitted via gate",
                        "submitted_at": datetime.now(timezone.utc).isoformat(),
                        "submit_mode": "validated_auto_submit"})
                except Exception as e:
                    print(f"   submit error: {str(e)[:80]}", flush=True)
            elif v["valid"]:
                print("   → passes validation; SUBMIT_VALIDATED=1 to submit.", flush=True)
            else:
                print("   → held back (failed validation).", flush=True)

        print("\nDONE — window stays open. Touch /tmp/park_stop to close.", flush=True)
        for _ in range(720):
            await asyncio.sleep(30)
            if os.path.exists(STOP):
                break


asyncio.run(main())
