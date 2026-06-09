"""
Debug script: opens SoFi Staff AI form in visible browser, runs the fill,
then pauses so you can inspect the "reasonable commute" and home address fields.
"""
import asyncio
import sys
sys.path.insert(0, '.')

from pathlib import Path
from playwright.async_api import async_playwright

URL = "https://sofi.com/careers/job/7718803003?gh_jid=7718803003"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=100)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        print("Opening SoFi form...")
        await page.goto(URL, timeout=30000)
        await asyncio.sleep(3)

        # Switch to iframe
        frame = None
        for f in page.frames:
            if "greenhouse.io/embed" in f.url:
                frame = f
                print(f"Found Greenhouse iframe: {f.url}")
                break

        if not frame:
            print("No Greenhouse iframe found — using outer page")
            frame = page

        await asyncio.sleep(1)

        # List ALL .select__control elements and their labels
        print("\n--- React Select controls found ---")
        controls = await frame.locator(".select__control").all()
        print(f"Total: {len(controls)}")
        for i, ctrl in enumerate(controls):
            try:
                label = await ctrl.evaluate("""el => {
                    const inp = el.querySelector('input');
                    if (inp) {
                        const lby = inp.getAttribute('aria-labelledby') || '';
                        for (const id of lby.split(' ')) {
                            const ref = id && document.getElementById(id);
                            if (ref && ref.innerText.trim().length > 5)
                                return 'aria-labelledby: ' + ref.innerText.trim();
                        }
                    }
                    let p = el;
                    for (let i = 0; i < 12; i++) {
                        p = p.parentElement;
                        if (!p) break;
                        const lbl = p.querySelector('label');
                        if (lbl && lbl.innerText.trim().length > 2)
                            return 'label: ' + lbl.innerText.trim();
                    }
                    return 'no label';
                }""")
                has_val = await ctrl.locator(".select__single-value").count()
                print(f"  [{i}] {label[:80]} | has_value={has_val>0}")
            except Exception as e:
                print(f"  [{i}] ERROR: {e}")

        # Try to click the "reasonable commute" control and inspect menu
        print("\n--- Clicking 'reasonable commute' control ---")
        for i, ctrl in enumerate(controls):
            try:
                label = await ctrl.evaluate("""el => {
                    let p = el;
                    for (let i = 0; i < 12; i++) {
                        p = p.parentElement;
                        if (!p) break;
                        const lbl = p.querySelector('label');
                        if (lbl) return lbl.innerText.toLowerCase();
                    }
                    return '';
                }""")
                if "reasonable commute" in label or "located within" in label:
                    print(f"  Found at index {i}: {label[:80]}")

                    # Try different open methods
                    print("  Trying ctrl.click()...")
                    await ctrl.click()
                    await asyncio.sleep(0.5)

                    menu_count = await frame.locator(".select__menu").count()
                    outer_menu = await page.locator(".select__menu").count()
                    print(f"  Menu in iframe: {menu_count}, in outer page: {outer_menu}")

                    if menu_count == 0 and outer_menu == 0:
                        print("  No menu! Trying ctrl.locator('input').click()...")
                        inp = ctrl.locator("input")
                        if await inp.count() > 0:
                            await inp.click()
                            await asyncio.sleep(0.5)
                            menu_count = await frame.locator(".select__menu").count()
                            outer_menu = await page.locator(".select__menu").count()
                            print(f"  Menu in iframe: {menu_count}, in outer page: {outer_menu}")

                    if menu_count == 0 and outer_menu == 0:
                        print("  Trying dispatchEvent mousedown...")
                        await ctrl.dispatch_event('mousedown')
                        await asyncio.sleep(0.5)
                        menu_count = await frame.locator(".select__menu").count()
                        outer_menu = await page.locator(".select__menu").count()
                        print(f"  Menu in iframe: {menu_count}, in outer page: {outer_menu}")

                    if menu_count > 0:
                        opts = await frame.locator(".select__menu .select__option").all()
                        print(f"  Options in iframe ({len(opts)}):")
                        for o in opts:
                            print(f"    - '{await o.inner_text()}'")
                    elif outer_menu > 0:
                        opts = await page.locator(".select__menu .select__option").all()
                        print(f"  Options in outer page ({len(opts)}):")
                        for o in opts:
                            print(f"    - '{await o.inner_text()}'")

                    break
            except Exception as e:
                print(f"  Error at [{i}]: {e}")

        print("\nPausing — inspect browser window. Press Enter to close.")
        input()
        await browser.close()

asyncio.run(main())
