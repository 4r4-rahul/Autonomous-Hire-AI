"""Probe NVIDIA Workday 'Apply with LinkedIn' flow: does the shared chrome_profile's
LinkedIn session let us authorize and bypass the reCAPTCHA-gated create-account?"""
import asyncio, json, sys
sys.path.insert(0, ".")
from tools.browser import BrowserClient

URL = ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/"
       "job/US-CA-Santa-Clara/AI-Platform-Engineer_JR2014718-1")


async def snap(page, tag):
    try:
        info = await page.evaluate("""() => ({
            url: location.href,
            hint: document.body.innerText.slice(0,300).replace(/\\n+/g,' | '),
            hasAllow: !!Array.from(document.querySelectorAll('button')).find(b=>/allow|authorize|accept/i.test(b.innerText||'')),
            hasLogin: !!document.querySelector("input[name='session_key'],input#username,input[name='session_password']"),
        })""")
        print(f"\n--- {tag} ---\n{json.dumps(info, indent=2)}")
    except Exception as e:
        print(f"\n--- {tag} (eval failed: {e}) ---")


async def main():
    pages_seen = []
    async with BrowserClient(headless=False, profile_dir="data/chrome_profile", patched=True) as b:
        ctx = b._context
        ctx.on("page", lambda p: pages_seen.append(p))
        page = await b.new_page(URL)
        await asyncio.sleep(5)
        # Apply
        for t in ["Apply", "Apply Now"]:
            el = page.locator(f"a[data-automation-id='adventureButton'], button:has-text('{t}')").first
            if await el.count() > 0 and await el.is_visible(timeout=1500):
                await el.click(); await asyncio.sleep(3); break
        # Click Apply with LinkedIn
        liw = page.locator("[data-automation-id='applyWithLinkedIn']").first
        print("applyWithLinkedIn present:", await liw.count() > 0)
        if await liw.count() > 0:
            box = await liw.bounding_box()
            if box:
                await page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
            else:
                await liw.click(force=True)
        await asyncio.sleep(6)

        # Inspect any popup/new tab from OAuth
        all_pages = list(ctx.pages)
        print(f"\ntotal pages now: {len(all_pages)}")
        for i, pg in enumerate(all_pages):
            await snap(pg, f"page[{i}] {pg.url[:80]}")
            # If a LinkedIn authorize/allow button is present, click it
            try:
                allow = pg.locator("button:has-text('Allow'), button:has-text('Authorize'), "
                                   "button:has-text('Accept'), button[type='submit']").first
                if await allow.count() > 0 and await allow.is_visible(timeout=1500):
                    await allow.click(); await asyncio.sleep(5)
                    print(f"  -> clicked allow on page[{i}]; now at {pg.url[:90]}")
            except Exception:
                pass
        await asyncio.sleep(5)
        # Where did the main page end up?
        await snap(page, "MAIN PAGE FINAL")
        await asyncio.sleep(3)


asyncio.run(main())
