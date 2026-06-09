"""Decoupled login + parking lot. Opens the Workday sign-in, brings up the Google popup,
then POLLS (long timeout) until the human finishes logging in — no 5-min cutoff. Once
signed in, fills strong-fit roles to Review (complete, with the section fixes) and lands
on Candidate Home (the parking lot)."""
import asyncio, os, sys
import yaml
sys.path.insert(0, ".")
from tools.browser import BrowserClient
from tools.pdf_parser import markdown_to_pdf
from core.llm import get_llm
from agents.application import form_filler as ff

SITE = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
HOME = f"{SITE}/userHome"
NV = f"{SITE}/job"
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


async def signed_in(page) -> bool:
    try:
        await page.goto(HOME, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3)
        body = (await page.inner_text("body")).lower()
        return "/login" not in page.url.lower() and "sign in with google" not in body
    except Exception:
        return False


async def main():
    profile = yaml.safe_load(open("data/user_profile.yaml"))
    resume_pdf = markdown_to_pdf("data/resume_base.md")
    llm = get_llm("collab", model="claude-haiku-4-5")
    try: os.unlink(STOP)
    except OSError: pass

    async with BrowserClient(headless=False, profile_dir="data/chrome_profile", patched=True) as b:
        ctx = b._context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(4)
        # Bring up the Google popup so the human only needs to pick account + password/2FA.
        try:
            g = page.locator("[data-automation-id='GoogleSignInButton'], "
                             "button:has-text('Sign in with Google')").first
            if await g.count() > 0:
                box = await g.bounding_box()
                if box:
                    await page.mouse.click(box["x"]+box["width"]/2, box["y"]+box["height"]/2)
        except Exception:
            pass
        print("WAITING_FOR_LOGIN — complete the Google login in the window…", flush=True)

        ok = False
        for n in range(135):           # ~18 min @ 8s
            await asyncio.sleep(8)
            if await signed_in(page):
                ok = True
                print("LOGIN_DETECTED — session is live; filling applications…", flush=True)
                break
            if n % 4 == 0:
                print(f"  …still waiting for login ({n*8}s)", flush=True)
        if not ok:
            print("LOGIN_TIMEOUT — no login detected; leaving the window open.", flush=True)

        done = []
        if ok:
            os.environ["WORKDAY_GOOGLE_LOGIN"] = "auto"
            for i, (title, url) in enumerate(JOBS):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
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
                    tag = "REVIEW(draft saved)" if "step6" in step else f"saved@{step[:24]}"
                except Exception:
                    tag = "?"
                done.append((title, tag)); print(f"{i+1}. [{tag}] {title}", flush=True)

        try:
            await page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(4)
        except Exception:
            pass
        print("PARKING_LOT_READY — Candidate Home open.", flush=True)
        for t, tag in done:
            print(f"   • [{tag}] {t}", flush=True)
        for _ in range(720):
            await asyncio.sleep(30)
            if os.path.exists(STOP):
                break


asyncio.run(main())
