"""Probe NVIDIA Workday apply modal to enumerate ALL apply options (incl. any LinkedIn)."""
import asyncio, json, sys
sys.path.insert(0, ".")
from tools.browser import BrowserClient

URL = ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/"
       "job/US-CA-Santa-Clara/AI-Platform-Engineer_JR2014718-1")


async def main():
    async with BrowserClient(headless=False, profile_dir="data/chrome_profile", patched=True) as b:
        page = await b.new_page(URL)
        await asyncio.sleep(5)
        for t in ["Apply", "Apply Now"]:
            el = page.locator(f"a[data-automation-id='adventureButton'], button:has-text('{t}'), a:has-text('{t}')").first
            if await el.count() > 0 and await el.is_visible(timeout=1500):
                await el.click(); await asyncio.sleep(3); break
        info = await page.evaluate("""() => {
            const vis = (e)=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
            const opts = Array.from(document.querySelectorAll('a,button,[role=button],[data-automation-id]'))
                .filter(vis).filter(e => /apply|linkedin|resume|guest|autofill|last application|continue/i.test((e.innerText||'')+(e.getAttribute('data-automation-id')||'')))
                .map(e => ({aid:e.getAttribute('data-automation-id'), text:(e.innerText||'').slice(0,40)}));
            return {url:location.href, options:opts,
                    bodyHint: document.body.innerText.slice(0,400).replace(/\\n+/g,' | ')};
        }""")
        print(json.dumps(info, indent=2))
        await asyncio.sleep(2)


asyncio.run(main())
