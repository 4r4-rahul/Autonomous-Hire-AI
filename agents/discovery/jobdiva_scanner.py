"""JobDiva discovery via headless browser — the FRAGILE branch (opt-in, SLEPT by default).

JobDiva career portals (www2.jobdiva.com/portal/?a=<token>&compid=<n>) are JS-gated: jobs load only
after the SPA renders + a search is submitted, over a token-authenticated, pako-compressed API with
NO clean public HTTP endpoint (verified 2026-08-09 — every static endpoint 404'd/returned the shell).
So the ONLY way in is to drive a real browser: render the portal, submit an empty search, scrape the
result rows. This is exactly the fragile approach the rest of discovery avoids (breaks on any JobDiva
UI change, risks bot-blocks, needs a browser per scan) — hence it is DISABLED by default and lives on
its own branch. It DOES reach ~6 large US IT-staffing H1B sponsors (Diverse Lynx, Net2Source, Russell
Tobin, …) that no HTTP scanner can.

Add a firm: open its careers page, grab the `?a=<token>` (+ `compid`) from the JobDiva portal link,
append to JOBDIVA_FIRMS. Tokens rotate rarely; re-grab if a firm stops returning jobs.
"""
from __future__ import annotations

import asyncio

from core.logger import get_logger
from agents.discovery.apply_url_resolver import loc_is_us_ok
from agents.discovery.board_scanner import _title_ok
from agents.application.ats_intake import intake_jobs

logger = get_logger("jobdiva_scanner")

# (company, portal_token, compid). Verified to return US tech roles.
JOBDIVA_FIRMS: list[tuple[str, str, str]] = [
    ("Diverse Lynx", "9xjdnw687b7a7nvvdyut936kpjlgy0023blrozaecads0pdnwppcswnaaku8ji2g", "0"),
    ("Net2Source",   "fyjdnwkqny26xqof9rceu6y6gam6750308agqi8uui1cmk3v9j6duy26aoewnusi", "0"),
    ("Russell Tobin", "nyjdnw8rs3eurnjvdink7d2fl4mnyy0b22tjlzi328snknlo1pzpk0ue533mvm7r", "2"),
    ("Mitchell Martin", "tljdnwoud6v9boh28cixx23toh209t0979fey921kmnyw0w0sxpmbw1kv43osbi4", "0"),
    ("Sunrise Systems", "5djdnwqftf6j9lq7k9c43cxglxnkql01c6w0hhxdvew2n3jwd2ruxp0ekhfhpm5d", "0"),
]

# Extract [{title, location, url}] from the rendered results. JobDiva has no semantic job-card class
# (bootstrap flex divs), so we find title-looking clickable elements and read the nearest location
# text from their card. Best-effort + defensive — returns whatever it can, never throws.
_EXTRACT_JS = r"""
() => {
  const roleRx = /(engineer|developer|architect|analyst|consultant|scientist|devops|sre|programmer|administrator|specialist|lead|manager)/i;
  const stateRx = /\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|United States|USA|Remote)\b/;
  const seen = new Set(), out = [];
  // JobDiva renders titles in generic div/span (no semantic class), so scan broadly then dedup by
  // text. Length cap keeps leaf title nodes and drops parent containers that include extra card text.
  const cands = [...document.querySelectorAll('a, div, span, h3, h4, h5, [class*=title], [class*=Title], strong, b')]
    .filter(e => { const t = (e.innerText||'').trim(); return t.length>6 && t.length<90 && roleRx.test(t); });
  for (const e of cands) {
    const title = (e.innerText||'').trim();
    if (seen.has(title)) continue;
    // walk up to a card container, read its text for a location + a job link
    let card = e, loc = '', url = '';
    for (let i=0;i<5 && card;i++) {
      const txt = (card.innerText||'');
      const m = txt.match(stateRx); if (m && !loc) loc = (txt.split('\n').find(l=>stateRx.test(l))||m[0]).trim().slice(0,60);
      const a = card.querySelector && card.querySelector("a[href*='job'],a[href*='#/']"); if (a && !url) url = a.href;
      if (loc) break;
      card = card.parentElement;
    }
    seen.add(title);
    out.push({ title, location: loc, url });
  }
  return out;
}
"""


async def _scrape_firm(browser, company: str, token: str, compid: str, per_firm_timeout: int) -> list[dict]:
    page = await browser.new_page()
    portal = f"https://www2.jobdiva.com/portal/?a={token}&compid={compid}"
    out: list[dict] = []
    try:
        await page.goto(portal + "#/", wait_until="networkidle", timeout=per_firm_timeout)
        await page.wait_for_timeout(2500)
        # submit an empty search → all open jobs
        for sel in ["button:has-text('Search Jobs')", "input[value*='Search']",
                    "button:has-text('Search')", "a:has-text('Search Jobs')"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    break
            except Exception:
                continue
        await page.wait_for_timeout(5000)
        rows = await page.evaluate(_EXTRACT_JS)
        for r in rows:
            title = (r.get("title") or "").strip()
            loc = (r.get("location") or "").strip()
            # US gate: accept an explicit US signal OR (JobDiva is US-domestic) an empty/unknown loc.
            if not _title_ok(title):
                continue
            if loc and not loc_is_us_ok(loc):
                continue
            out.append({
                "company": company,
                "title": title,
                "description": "",
                "location": loc or "United States",
                "posted_at": "",
                "source": f"jobdiva:{company.lower().replace(' ', '')}",
                "fallback_url": r.get("url") or portal,
            })
        logger.info("jobdiva_firm_scraped", company=company, roles=len(out))
    except Exception as e:
        logger.warning("jobdiva_firm_failed", company=company, error=str(e)[:120])
    finally:
        try:
            await page.close()
        except Exception:
            pass
    return out


async def run(state, *, limit: int = 10, tailor: bool = False, persist: bool = False,
              per_firm_timeout: int = 45000):
    """Headless-scrape every configured JobDiva firm, intake the fitting US roles. Returns (state, report)."""
    from playwright.async_api import async_playwright

    discovered: list[dict] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                for company, token, compid in JOBDIVA_FIRMS:
                    discovered += await _scrape_firm(browser, company, token, compid, per_firm_timeout)
            finally:
                await browser.close()
    except Exception as e:
        logger.error("jobdiva_browser_failed", error=str(e)[:140])
        return state, {"firms": len(JOBDIVA_FIRMS), "discovered": 0, "queued": 0, "error": str(e)[:120]}

    logger.info("jobdiva_discovered", firms=len(JOBDIVA_FIRMS), fitting=len(discovered))
    if not discovered:
        return state, {"firms": len(JOBDIVA_FIRMS), "discovered": 0, "queued": 0}

    pre_app_ids = {a.get("id") for a in state.get("applications", [])}
    pre_job_ids = {j.get("id") for j in state.get("job_listings", [])}
    updated, report = await intake_jobs(discovered, state, limit=limit, tailor=tailor)
    report["firms"] = len(JOBDIVA_FIRMS)
    report["discovered"] = len(discovered)

    if persist:
        from core.state_store import merge_new
        new_jobs = [j for j in updated.get("job_listings", []) if j.get("id") not in pre_job_ids]
        new_apps = [a for a in updated.get("applications", []) if a.get("id") not in pre_app_ids]
        report["persisted"] = merge_new(new_jobs, new_apps)

    logger.log_decision(action="jobdiva_scan_complete", outcome="success", details=report)
    return updated, report
