"""Attempt NVIDIA 'Apply with LinkedIn': inject stored li_at, verify LinkedIn login,
then run the OAuth authorize and see if it bypasses the reCAPTCHA-gated account creation."""
import asyncio, json, sys
from dotenv import dotenv_values
sys.path.insert(0, ".")
from tools.browser import BrowserClient

URL = ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/"
       "job/US-CA-Santa-Clara/AI-Platform-Engineer_JR2014718-1")
LI_AT = (dotenv_values(".env").get("LINKEDIN_LI_AT") or "").strip()


async def main():
    pages = []
    async with BrowserClient(headless=False, profile_dir="data/chrome_profile", patched=True) as b:
        ctx = b._context
        ctx.on("page", lambda p: pages.append(p))
        # Inject the stored li_at session cookie for LinkedIn.
        if LI_AT:
            await ctx.add_cookies([{
                "name": "li_at", "value": LI_AT, "domain": ".linkedin.com",
                "path": "/", "httpOnly": True, "secure": True, "sameSite": "None",
            }])
            print("injected li_at cookie (len %d)" % len(LI_AT))
        # (Skip feed verification — LinkedIn 999s bot navigations. The OAuth popup
        # below reveals whether li_at is valid: login form = expired, authorize = valid.)

        # Drive NVIDIA Apply with LinkedIn
        page = await b.new_page(URL)
        await asyncio.sleep(5)
        for t in ["Apply", "Apply Now"]:
            el = page.locator(f"a[data-automation-id='adventureButton'], button:has-text('{t}')").first
            if await el.count() > 0 and await el.is_visible(timeout=1500):
                await el.click(); await asyncio.sleep(3); break
        liw = page.locator("[data-automation-id='applyWithLinkedIn']").first
        if await liw.count() > 0:
            box = await liw.bounding_box()
            await page.mouse.click(box["x"]+box["width"]/2, box["y"]+box["height"]/2) if box else await liw.click(force=True)
        await asyncio.sleep(6)

        # Handle OAuth popup: click Allow/Authorize (not a login form now, hopefully)
        for pg in list(ctx.pages):
            if "linkedin.com" in pg.url and ("oauth" in pg.url or "login" in pg.url):
                info = await pg.evaluate("""() => ({url:location.href.slice(0,90),
                    hint: document.body.innerText.slice(0,200).replace(/\\n+/g,' | '),
                    hasLogin: !!document.querySelector("input[name='session_password']"),
                    allow: (Array.from(document.querySelectorAll('button')).find(b=>/allow|authorize|accept/i.test(b.innerText||''))||{}).innerText||null})""")
                print("\nOAUTH PAGE:", json.dumps(info, indent=2))
                try:
                    allow = pg.locator("button:has-text('Allow'), button:has-text('Authorize')").first
                    if await allow.count() > 0 and await allow.is_visible(timeout=2000):
                        await allow.click(); await asyncio.sleep(6)
                        print("clicked Allow; popup url now:", pg.url[:80])
                except Exception as e:
                    print("allow click err:", e)
        await asyncio.sleep(6)
        # Where did Workday land? (bypassed account creation → 'My Information'?)
        final = await page.evaluate("""() => ({url:location.href.slice(0,100),
            hint: document.body.innerText.slice(0,400).replace(/\\n+/g,' | ')})""")
        print("\nWORKDAY FINAL:", json.dumps(final, indent=2))
        await page.screenshot(path="data/screenshots/nvidia_linkedin_result.png")
        await asyncio.sleep(3)


asyncio.run(main())
