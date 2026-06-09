"""Park each application in its OWN tab, fully filled, at the Review page. Uses FRESH
jobs (no prior application → no 'already applied' interference). One browser, one tab
per job, brought to front before filling. Keeps the window open with all tabs parked."""
import asyncio, os, sys
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
STOP = "/tmp/park_stop"


async def main():
    profile = yaml.safe_load(open("data/user_profile.yaml"))
    resume_pdf = markdown_to_pdf("data/resume_base.md")
    llm = get_llm("collab", model="claude-haiku-4-5")
    try: os.unlink(STOP)
    except OSError: pass

    async with BrowserClient(headless=False, profile_dir="data/chrome_profile", patched=True) as b:
        ctx = b._context
        done = []
        for i, (title, url) in enumerate(JOBS):
            # Tab 1: interactive login (refreshes the session, which degrades under
            # repeated runs). Tabs 2+: ride the freshly-established session.
            os.environ["WORKDAY_GOOGLE_LOGIN"] = "interactive" if i == 0 else "auto"
            page = ctx.pages[0] if (i == 0 and ctx.pages) else await ctx.new_page()
            try:
                await page.bring_to_front()
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.locator(
                    "a[data-automation-id='adventureButton'], button:has-text('Apply'), "
                    "[data-automation-id='legalNameSection_firstName']").first.wait_for(
                    state="visible", timeout=30000)
            except Exception:
                pass
            await asyncio.sleep(2)
            job = {"id": f"tab-{i}", "company_name": "NVIDIA", "title": title,
                   "description": f"{title} at NVIDIA. Python, data/ML infrastructure, "
                   "analytics, distributed systems, cloud.", "apply_url": url}
            try:
                await asyncio.wait_for(ff._fill_workday(page, profile, resume_pdf, job, llm),
                                       timeout=720 if i == 0 else 480)
            except Exception as e:
                print(f"TAB{i+1} fill error: {str(e)[:80]}", flush=True)
            try:
                await page.screenshot(path=f"data/screenshots/parktab_{i+1}.png")
            except Exception:
                pass
            try:
                step = await ff._wd_step_index(page)
                blocked = await ff._wd_on_signin_page(page)
                body = (await page.inner_text("body")).lower()
                applied = "already applied" in body
                tag = ("ALREADY_APPLIED" if applied else "NEEDS_LOGIN" if blocked
                       else "REVIEW✓" if "step6" in step else f"@{step[:22]}")
            except Exception:
                tag = "?"
            done.append((title, tag))
            print(f"TAB{i+1} [{tag}] {title}", flush=True)

        print("ALL_PARKED", flush=True)
        for t, tag in done:
            print(f"   • [{tag}] {t}", flush=True)
        for _ in range(720):
            await asyncio.sleep(30)
            if os.path.exists(STOP):
                break


asyncio.run(main())
