"""Dump NVIDIA My Information's 'How Did You Hear' taxonomy + the previously-worked
widget, using the saved Google session (no login needed)."""
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
            el = page.locator(f"a[data-automation-id='adventureButton'], button:has-text('{t}')").first
            if await el.count() > 0 and await el.is_visible(timeout=1500):
                await el.click(); await asyncio.sleep(3); break
        for g in ["[data-automation-id='applyManually']", "button:has-text('Apply Manually')"]:
            el = page.locator(g).first
            if await el.count() > 0 and await el.is_visible(timeout=1500):
                await el.click(); await asyncio.sleep(4); break
        # Auto Google sign-in via saved session
        gbtn = page.locator("[data-automation-id='GoogleSignInButton'], button:has-text('Sign in with Google')").first
        if await gbtn.count() > 0 and await gbtn.is_visible(timeout=1500):
            box = await gbtn.bounding_box()
            await page.mouse.click(box["x"]+box["width"]/2, box["y"]+box["height"]/2) if box else await gbtn.click(force=True)
            await asyncio.sleep(6)
        # wait for My Information (auto google sign-in via session)
        for _ in range(20):
            if await page.locator("[data-automation-id='legalNameSection_firstName'], "
                                  "[data-automation-id='firstName']").count() > 0:
                break
            await asyncio.sleep(3)
        print("on My Information:", await page.locator("[data-automation-id='firstName'], [data-automation-id='legalNameSection_firstName']").count() > 0)

        # Dump all prompt dropdowns/buttons + their labels (find previously-worked widget)
        widgets = await page.evaluate("""() => {
            const lab = (el)=>{let p=el;for(let i=0;i<9;i++){p=p.parentElement;if(!p)break;
                const l=p.querySelector('label,legend,[id*=label]');if(l&&(l.innerText||'').trim())return l.innerText.trim().slice(0,60);}return '';};
            const out=[];
            document.querySelectorAll("button[aria-haspopup='listbox'],[data-automation-id='selectinput'],input[type='radio'],[data-automation-id='multiselectInputContainer']").forEach(e=>{
                out.push({tag:e.tagName, type:e.type||'', aid:e.getAttribute('data-automation-id'),
                          role:e.getAttribute('aria-haspopup')||'', label:lab(e)});
            });
            return out.slice(0,25);
        }""")
        print("\n=== WIDGETS ===\n" + json.dumps(widgets, indent=2))

        # Open How Did You Hear multiselect and dump category tree
        ms = page.locator("[data-automation-id='multiselectInputContainer']").first
        if await ms.count() > 0:
            await ms.click(); await asyncio.sleep(1)
            cats = await page.evaluate("""() => Array.from(document.querySelectorAll(
                "[data-automation-id='promptOption'],[data-automation-id='promptLeafNode'],[role='option']"))
                .map(e=>({aid:e.getAttribute('data-automation-id'),text:(e.innerText||'').slice(0,30)})).slice(0,30)""")
            print("\n=== HOW-DID-YOU-HEAR top level ===\n" + json.dumps(cats, indent=2))
            # click a 'Social Media' / 'Job' category if present, dump leaves
            for parent in ("social", "job", "internet", "online", "linkedin"):
                c = page.locator("[data-automation-id='promptOption']").filter(has_text=__import__('re').compile(parent, 2)).first
                if await c.count() > 0:
                    await c.click(); await asyncio.sleep(1)
                    leaves = await page.evaluate("""() => Array.from(document.querySelectorAll(
                        "[data-automation-id='promptLeafNode'],[data-automation-id='promptOption'],[role='option']"))
                        .map(e=>({aid:e.getAttribute('data-automation-id'),text:(e.innerText||'').slice(0,30)})).slice(0,30)""")
                    print(f"\n=== leaves under '{parent}' ===\n" + json.dumps(leaves, indent=2))
                    break
        await asyncio.sleep(2)


asyncio.run(main())
