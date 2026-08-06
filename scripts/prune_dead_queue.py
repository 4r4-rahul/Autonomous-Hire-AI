"""One-off queue cleanup: skip pending_review apps whose posting is DEFINITIVELY gone.

Liveness is checked against each ATS's authoritative JSON API (NOT a plain GET — Greenhouse/
Ashby bot-block GETs and return 403, which is a false "dead"). We only remove an app on a
DEFINITIVE dead signal:
  • Greenhouse  → boards-api.greenhouse.io/v1/boards/{slug}/jobs/{token}  → 404 = dead
  • Lever       → api.lever.co/v0/postings/{org}/{id}                     → 404/410 = dead
  • Ashby       → api.ashbyhq.com/posting-api/job-board/{org}  job id absent = dead
  • other hosts → plain GET; dead ONLY on 404/410 (403/timeout/blocked → kept as UNKNOWN)

Anything unknown (blocked, timeout, unrecognized host, board API down) is KEPT — we never drop
on an ambiguous signal. Reversible: sets status→skipped, does not delete.

Run: PYTHONPATH=. .venv/bin/python scripts/prune_dead_queue.py [--apply]
"""
import asyncio, sys, json, re
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent
STATE = ROOT / "data" / "pipeline_state.json"

_ashby_cache: dict[str, set | None] = {}


async def _ashby_ids(client, org):
    if org not in _ashby_cache:
        try:
            r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}", timeout=20)
            _ashby_cache[org] = {j.get("id") for j in r.json().get("jobs", [])} if r.status_code == 200 else None
        except Exception:
            _ashby_cache[org] = None
    return _ashby_cache[org]


async def liveness(client, url: str) -> str:
    """Return 'live' | 'dead' | 'unknown' for an apply URL."""
    if not url:
        return "unknown"

    m = re.search(r"greenhouse\.io.*?for=([a-z0-9_.-]+)&token=(\d+)", url, re.I) \
        or re.search(r"greenhouse\.io/([a-z0-9_.-]+)/jobs/(\d+)", url, re.I)
    if m:
        api = f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}"
        try:
            r = await client.get(api, timeout=20)
            if r.status_code == 200: return "live"
            if r.status_code == 404: return "dead"
        except Exception:
            pass
        return "unknown"

    m = re.search(r"lever\.co/([^/]+)/([0-9a-f-]{20,})", url, re.I)
    if m:
        try:
            r = await client.get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}", timeout=20)
            if r.status_code == 200: return "live"
            if r.status_code in (404, 410): return "dead"
        except Exception:
            pass
        return "unknown"

    m = re.search(r"ashbyhq\.com/([^/]+)/([0-9a-f-]{20,})", url, re.I)
    if m:
        ids = await _ashby_ids(client, m.group(1))
        if ids is None: return "unknown"
        return "live" if m.group(2) in ids else "dead"

    # generic host: only a hard 404/410 counts as dead
    try:
        r = await client.get(url, follow_redirects=True, timeout=15)
        if r.status_code in (404, 410): return "dead"
        return "live" if r.status_code == 200 else "unknown"
    except Exception:
        return "unknown"


async def main(apply: bool):
    state = json.loads(STATE.read_text())
    jl = {j["id"]: j for j in state.get("job_listings", [])}
    pending = [a for a in state.get("applications", []) if a.get("status") == "pending_review"]

    sem = asyncio.Semaphore(8)
    results = {}

    async def _one(app):
        url = (jl.get(app.get("job_id")) or {}).get("apply_url")
        async with sem:
            results[app["id"]] = (await liveness(client, url), url)

    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        await asyncio.gather(*[_one(a) for a in pending])

    verdicts = Counter(v for v, _ in results.values())
    dead = [a for a in pending if results[a["id"]][0] == "dead"]

    print(f"pending_review: {len(pending)} | live={verdicts['live']} dead={verdicts['dead']} unknown={verdicts['unknown']}")
    print(f"\n{len(dead)} DEAD (definitive — posting gone):")
    for a in sorted(dead, key=lambda x: x.get("company_name") or ""):
        print(f"  ✗ {str(a.get('company_name')):22.22} | {str(a.get('role_title')):50.50}")
        if apply:
            a["status"] = "skipped"
            a["skip_reason"] = "posting expired — ATS API reports job removed"

    if apply:
        STATE.write_text(json.dumps(state, indent=2))
        print(f"\nAPPLIED — {len(dead)} apps marked skipped (expired). {verdicts['live']+verdicts['unknown']} remain in queue.")
    else:
        print("\nDRY RUN — pass --apply to skip these.")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
