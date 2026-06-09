"""Open ONE browser window and park multiple Workday applications, each in its own TAB
at the Review page, for human review + submit. Shares one Google session across tabs
(after the first auth, Workday's session cookie signs the rest in automatically).
Keeps the window open until /tmp/park_stop appears."""
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
        ctx = b._context  # use the raw context so each job gets its OWN tab
        # (BrowserClient.new_page reuses pages[0] — that would navigate one tab N times).
        for i, (title, url) in enumerate(JOBS):
            page = ctx.pages[0] if (i == 0 and ctx.pages) else await ctx.new_page()
            # A background tab's Google OAuth popup/redirect won't complete — bring the
            # tab to the foreground so its sign-in (and the coordinate clicks) work.
            try:
                await page.bring_to_front()
            except Exception:
                pass
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            # Wait for Workday's SPA to actually render the Apply control before filling —
            # a fresh tab otherwise runs the filler against a half-loaded page and stalls.
            try:
                await page.locator(
                    "a[data-automation-id='adventureButton'], button:has-text('Apply'), "
                    "[data-automation-id='legalNameSection_firstName']").first.wait_for(
                    state="visible", timeout=30000)
            except Exception:
                pass
            await asyncio.sleep(2)
            job = {"id": f"park-{i}", "company_name": "NVIDIA", "title": title,
                   "description": f"{title} at NVIDIA. Python, ML/AI infrastructure, MLOps, "
                   "distributed systems, Kubernetes, cloud, data pipelines.",
                   "apply_url": url}
            try:
                await asyncio.wait_for(ff._fill_workday(page, profile, resume_pdf, job, llm),
                                       timeout=300)
            except Exception as e:
                print(f"TAB{i+1} fill error: {str(e)[:90]}", flush=True)
            try:
                step = await ff._wd_step_index(page)
                blocked = await ff._wd_on_signin_page(page)
                state = "NEEDS_LOGIN" if blocked else ("REVIEW" if "step6" in step or "step5" in step
                                                       else step)
            except Exception:
                state = "?"
            print(f"TAB{i+1} [{state}] {title[:46]}", flush=True)

        print("ALL_PARKED — window stays open with tabs. Review + Submit each, "
              "then `touch /tmp/park_stop` to close.", flush=True)
        for _ in range(720):  # ~6h max
            await asyncio.sleep(30)
            if os.path.exists(STOP):
                print("stop signal — closing", flush=True)
                break


asyncio.run(main())
