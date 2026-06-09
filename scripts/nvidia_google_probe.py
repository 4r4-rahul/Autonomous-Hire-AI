"""Probe NVIDIA Workday 'Sign in with Google': does the chrome_profile have a live
Google session (account chooser) or is it logged out (password form / automation block)?"""
import asyncio, json, sys
sys.path.insert(0, ".")
from tools.browser import BrowserClient

URL = ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/"
       "job/US-CA-Santa-Clara/AI-Platform-Engineer_JR2014718-1")


async def main():
    pages = []
    async with BrowserClient(headless=False, profile_dir="data/chrome_profile", patched=True) as b:
        ctx = b._context
        ctx.on("page", lambda p: pages.append(p))
        page = await b.new_page(URL)
        await asyncio.sleep(5)
        # Apply -> Apply Manually -> sign-in page
        for t in ["Apply", "Apply Now"]:
            el = page.locator(f"a[data-automation-id='adventureButton'], button:has-text('{t}')").first
            if await el.count() > 0 and await el.is_visible(timeout=1500):
                await el.click(); await asyncio.sleep(3); break
        for g in ["[data-automation-id='applyManually']", "button:has-text('Apply Manually')"]:
            el = page.locator(g).first
            if await el.count() > 0 and await el.is_visible(timeout=1500):
                await el.click(); await asyncio.sleep(3); break
        # Click "Sign in with Google"
        gbtn = page.locator("[data-automation-id='GoogleSignInButton'], button:has-text('Sign in with Google')").first
        print("Google button present:", await gbtn.count() > 0)
        if await gbtn.count() > 0:
            box = await gbtn.bounding_box()
            await page.mouse.click(box["x"]+box["width"]/2, box["y"]+box["height"]/2) if box else await gbtn.click(force=True)
        await asyncio.sleep(6)
        # Inspect the Google OAuth popup/redirect
        for pg in list(ctx.pages):
            if "google.com" in pg.url or "accounts.google" in pg.url:
                info = await pg.evaluate("""() => ({url:location.href.slice(0,100),
                    hint: document.body.innerText.slice(0,260).replace(/\\n+/g,' | '),
                    hasPassword: !!document.querySelector("input[type='password']"),
                    hasEmail: !!document.querySelector("input[type='email']"),
                    chooseAccount: /choose an account|4r4rahul|use another account/i.test(document.body.innerText||''),
                    blocked: /browser or app may not be secure|couldn.t sign you in/i.test(document.body.innerText||'')})""")
                print("\nGOOGLE OAUTH PAGE:", json.dumps(info, indent=2))
        await page.screenshot(path="data/screenshots/nvidia_google_result.png")
        await asyncio.sleep(2)


asyncio.run(main())
