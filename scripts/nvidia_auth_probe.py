"""One-off probe: drive NVIDIA Workday sign-in → create-account and dump the page
structure to learn why unattended account creation fails (CAPTCHA vs missing field)."""
import asyncio, json, sys
sys.path.insert(0, ".")
from tools.browser import BrowserClient

URL = ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/"
       "job/US-CA-Santa-Clara/AI-Platform-Engineer_JR2014718-1")


async def dump(page, tag):
    info = await page.evaluate("""() => {
        const vis = (e) => { const r=e.getBoundingClientRect();
            return r.width>0 && r.height>0; };
        const inputs = Array.from(document.querySelectorAll('input,textarea'))
            .filter(vis).map(e => ({aid: e.getAttribute('data-automation-id'),
                type: e.type, name: e.name, ph: e.placeholder}));
        const buttons = Array.from(document.querySelectorAll('button,a[role=button],[data-automation-id]'))
            .filter(e => /button|link/i.test(e.tagName+ (e.getAttribute('role')||'')) || /submit|create|account|sign/i.test(e.getAttribute('data-automation-id')||''))
            .filter(vis).map(e => ({aid: e.getAttribute('data-automation-id'),
                text: (e.innerText||'').slice(0,30)})).slice(0,25);
        const frames = Array.from(document.querySelectorAll('iframe'))
            .map(f => f.src||f.title||'').slice(0,10);
        const captcha = /captcha|recaptcha|hcaptcha|turnstile/i.test(document.body.innerHTML);
        return {url: location.href, inputs, buttons, frames, captcha,
                bodyHint: document.body.innerText.slice(0,300).replace(/\\n+/g,' | ')};
    }""")
    print(f"\n===== {tag} =====")
    print(json.dumps(info, indent=2))


async def main():
    async with BrowserClient(headless=False, profile_dir="data/chrome_profile", patched=True) as b:
        page = await b.new_page(URL)
        await asyncio.sleep(5)
        # Click "Apply" then guest/manual to reach auth
        for t in ["Apply", "Apply Now"]:
            el = page.locator(f"a[data-automation-id='adventureButton'], button:has-text('{t}'), a:has-text('{t}')").first
            if await el.count() > 0 and await el.is_visible(timeout=1500):
                await el.click(); await asyncio.sleep(2.5); break
        for g in ["[data-automation-id='applyManually']", "button:has-text('Apply Manually')",
                  "[data-automation-id='autofillWithResume']"]:
            el = page.locator(g).first
            if await el.count() > 0 and await el.is_visible(timeout=1500):
                await el.click(); await asyncio.sleep(3); break
        await dump(page, "AFTER APPLY (sign-in landing)")
        # Reveal email form
        for r in ("button:has-text('Sign in with email')", "a:has-text('Sign in with email')"):
            el = page.locator(r).first
            if await el.count() > 0 and await el.is_visible(timeout=1500):
                await el.click(); await asyncio.sleep(2); break
        await dump(page, "AFTER 'Sign in with email'")
        # Go to Create Account
        for c in ("a:has-text('Create Account')", "button:has-text('Create Account')",
                  "[data-automation-id='createAccountLink']"):
            el = page.locator(c).first
            if await el.count() > 0 and await el.is_visible(timeout=1500):
                await el.click(); await asyncio.sleep(2.5); break
        await dump(page, "CREATE ACCOUNT page")
        # Fill + submit create-account, then capture the result/error.
        from core.config import get_settings as _gs
        import yaml as _yaml
        _defs = (_yaml.safe_load(open("data/user_profile.yaml")).get("application_defaults", {})
                 if __import__("os").path.exists("data/user_profile.yaml") else {})
        EMAIL = _defs.get("workday_email") or "4r4rahul@gmail.com"
        PW = _gs().workday_password or _defs.get("workday_password", "")
        try:
            await page.locator("[data-automation-id='email']").first.fill(EMAIL)
            await page.locator("[data-automation-id='password']").first.fill(PW)
            vp = page.locator("[data-automation-id='verifyPassword']").first
            if await vp.count() > 0:
                await vp.fill(PW)
            cb = page.locator("[data-automation-id='createAccountCheckbox']").first
            if await cb.count() > 0 and not await cb.is_checked():
                await cb.click(force=True)
            await asyncio.sleep(0.5)
            sb = page.locator("[data-automation-id='createAccountSubmitButton']").first
            box = await sb.bounding_box()
            if box:
                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await asyncio.sleep(5)
        except Exception as e:
            print("submit error:", e)
        result = await page.evaluate("""() => {
            const errs = Array.from(document.querySelectorAll(
                "[data-automation-id*='error'],[class*='error'],[role='alert'],[id*='error']"))
                .map(e => (e.innerText||'').trim()).filter(t => t.length>3).slice(0,8);
            return {url: location.href, errors: errs,
                    captcha: /captcha|recaptcha/i.test(document.body.innerHTML),
                    bodyHint: document.body.innerText.slice(0,500).replace(/\\n+/g,' | ')};
        }""")
        print("\n===== AFTER CREATE-ACCOUNT SUBMIT =====")
        print(json.dumps(result, indent=2))
        await asyncio.sleep(2)


asyncio.run(main())
