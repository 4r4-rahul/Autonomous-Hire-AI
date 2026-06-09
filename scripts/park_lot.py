"""Reliable Workday 'parking lot': fill several applications to Review SEQUENTIALLY in
one authenticated tab (each auto-saves as a draft in the candidate home), then land on
Candidate Home — Workday's native list of all in-progress applications, each one click
from Review → Submit. (New tabs don't inherit Workday's apply-session, so true
simultaneous multi-tab parking isn't reliable; this is the dependable equivalent.)"""
import asyncio, os, sys
import yaml
sys.path.insert(0, ".")
os.environ.setdefault("WORKDAY_GOOGLE_LOGIN", "auto")
from tools.browser import BrowserClient
from tools.pdf_parser import markdown_to_pdf
from core.llm import get_llm
from agents.application import form_filler as ff

NV = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job"
HOME = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/userHome"
JOBS = [
    ("Senior MLOps Engineer, GenAI Framework",
     f"{NV}/US-CA-Santa-Clara/Senior-MLOps-Engineer--GenAI-Framework_JR2013494"),
    ("Senior Data Engineer - Financial Transactions & Automation",
     f"{NV}/US-CA-Santa-Clara/Senior-Data-Engineer---Financial-Transactions---Automation_JR2009512"),
    ("Senior Platform Telemetry Engineer",
     f"{NV}/US-CA-Santa-Clara/Senior-Platform-Telemetry-Engineer_JR2002276"),
    ("Senior Software Engineer, Deep Learning - MLIR TRT",
     f"{NV}/US-CA-Santa-Clara/Senior-Software-Engineer--Deep-Learning---MLIR-TRT_JR2009329"),
    ("Solutions Architect, AI and ML",
     f"{NV}/US-WA-Redmond/Solutions-Architect--AI-and-ML_JR2000691"),
]
STOP = "/tmp/park_stop"


async def main():
    profile = yaml.safe_load(open("data/user_profile.yaml"))
    resume_pdf = markdown_to_pdf("data/resume_base.md")
    llm = get_llm("collab", model="claude-haiku-4-5")
    try:
        os.unlink(STOP)
    except OSError:
        pass

    async with BrowserClient(headless=False, profile_dir="data/chrome_profile", patched=True) as b:
        ctx = b._context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        done = []
        for i, (title, url) in enumerate(JOBS):
            # First job: interactive login (refreshes the throttled session); rest: auto.
            os.environ["WORKDAY_GOOGLE_LOGIN"] = "interactive" if i == 0 else "auto"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            try:
                await page.locator(
                    "a[data-automation-id='adventureButton'], button:has-text('Apply'), "
                    "[data-automation-id='legalNameSection_firstName']").first.wait_for(
                    state="visible", timeout=30000)
            except Exception:
                pass
            await asyncio.sleep(2)
            job = {"id": f"lot-{i}", "company_name": "NVIDIA", "title": title,
                   "description": f"{title} at NVIDIA. Python, ML/AI infrastructure, MLOps, "
                   "Kubernetes, distributed systems, cloud, data pipelines.", "apply_url": url}
            try:
                await asyncio.wait_for(ff._fill_workday(page, profile, resume_pdf, job, llm),
                                       timeout=320)
            except Exception as e:
                print(f"{i+1}. fill error: {str(e)[:80]}", flush=True)
            try:
                step = await ff._wd_step_index(page)
                blocked = await ff._wd_on_signin_page(page)
                tag = "NEEDS_LOGIN" if blocked else ("REVIEW(draft saved)" if "step6" in step
                                                     else f"saved@{step}")
            except Exception:
                tag = "?"
            done.append((title, tag))
            print(f"{i+1}. [{tag}] {title}", flush=True)

        # Land on the parking lot: Candidate Home lists every in-progress application.
        try:
            await page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(4)
        except Exception:
            pass
        print("PARKING_LOT_READY — Candidate Home open; each application is one click "
              "from Review → Submit.", flush=True)
        for t, tag in done:
            print(f"   • [{tag}] {t}", flush=True)
        for _ in range(720):
            await asyncio.sleep(30)
            if os.path.exists(STOP):
                break


asyncio.run(main())
