"""
Board scanner — off-LinkedIn DISCOVERY.

Scans sponsor-company public ATS boards (Greenhouse/Lever/Ashby/Workable) DIRECTLY for fresh
roles that fit Rahul, then hands them to ats_intake (resolve → score → tailor → park). It never
touches LinkedIn or any browser — just public board-API requests — so it carries ZERO account
risk and keeps the apply queue refuelling even while the LinkedIn radar is blocked or healing.

Rotates through the H1B-sponsor seed list a slice per run (cursor persisted to disk) so the whole
list is covered over time without hammering any one board. The board fetch is the SAME cached call
ats_intake uses for resolution, so resolving each role inside intake is a cache hit (no re-fetch).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.logger import get_logger
from agents.discovery.apply_url_resolver import _fetch_company_postings, _loc_class
from agents.application.ats_intake import intake_jobs

logger = get_logger("board_scanner")

_ROOT = Path(__file__).parent.parent.parent
# Curated rotation list: sponsor companies discovered to expose a public ATS board with >=1
# fitting role (built by probing h1b_sponsors once). Falls back to the raw sponsor list.
_BOARD_SPONSORS = _ROOT / "data" / "board_sponsors.json"
_SPONSORS = _ROOT / "data" / "h1b_sponsors.json"
_CURSOR = _ROOT / "data" / "board_scan_cursor.json"

# Titles worth tailoring for — Rahul's target categories: data-platform, ML, AI-engineer (LLM apps/
# prompt/Claude), solutions-eng, systems-eng, platform/backend. At least one must hit.
_TITLE_INCLUDE = (
    "data engineer", "machine learning", "ml engineer", "mlops", "ai engineer", "ai/ml",
    "platform engineer", "software engineer", "software developer", "backend", "back end",
    "back-end", "data platform", "data infrastructure", "infrastructure engineer", "devops",
    "site reliability", "sre", "cloud engineer", "applied scientist", "full stack", "fullstack",
    "genai", "llm", "distributed systems",
    # AI-engineer / prompt / Claude-code / dev-tools side
    "prompt engineer", "ai/ml engineer", "generative ai", "developer tool", "developer experience",
    "developer advocate", "devrel", "agent", "rag", "applied ai",
    # Solutions / forward-deployed side
    "solutions engineer", "solution engineer", "forward deployed", "forward-deployed",
    "sales engineer", "implementation engineer", "customer engineer", "integration engineer",
    "deployment strategist", "professional services", "field engineer",
    # Systems side
    "systems engineer", "reliability engineer",
)
# Obvious non-fits even if a title keyword slips through (seniority / discipline mismatch).
_TITLE_EXCLUDE = (
    # NOTE: "principal"/"staff " are targets (Staff Engineer). Solutions/sales/forward-deployed
    # ENGINEER titles are now TARGETS too (candidate wants the solutions-engineering side) — only
    # pure non-engineering sales roles stay excluded.
    "intern", "manager", "director", " vp ", "vp,", "head of", "lead ",
    "marketing", "recruit", "designer", "ios", "android", "mobile",
    "account executive", "sales development", "sales representative", "sdr ", "bdr ",
    "qa ", "test engineer", "security engineer", "hardware", "firmware", "embedded",
)


def _sponsors() -> list[str]:
    try:
        return [s for s in json.loads(_SPONSORS.read_text()).get("sponsors", []) if s]
    except Exception:
        return []


def _rotation_companies() -> list[str]:
    """The board-friendly rotation list (deduped, order preserved). Prefers the curated
    board_sponsors.json (companies with a real public board); falls back to the full list."""
    raw = []
    try:
        raw = [s for s in json.loads(_BOARD_SPONSORS.read_text()).get("companies", []) if s]
    except Exception:
        raw = []
    if not raw:
        raw = _sponsors()
    seen, out = set(), []
    for c in raw:
        k = c.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _title_ok(title: str) -> bool:
    t = (title or "").lower()
    if any(x in t for x in _TITLE_EXCLUDE):
        return False
    return any(x in t for x in _TITLE_INCLUDE)


def _cursor() -> int:
    try:
        return int(json.loads(_CURSOR.read_text()).get("i", 0))
    except Exception:
        return 0


def _save_cursor(i: int) -> None:
    try:
        _CURSOR.write_text(json.dumps({"i": i}))
    except Exception:
        pass


async def run(state, *, max_companies: int = 25, intake_limit: int = 10, tailor: bool = True,
              persist: bool = False):
    """Scan a rotating slice of sponsor boards, intake the fitting roles. Returns (state, report).

    max_companies: how many sponsor boards to fetch this run (slice walks the list via a cursor).
    intake_limit:  cap on expensive Opus tailorings (parked apps) per run.
    persist:       if True, merge the new jobs/apps into the live state under lock (race-safe) —
                   the caller must then NOT also save the returned snapshot.
    """
    sponsors = _rotation_companies()
    if not sponsors:
        return state, {"error": "no sponsor list"}

    start = _cursor() % len(sponsors)
    n = min(max_companies, len(sponsors))
    slice_ = [sponsors[(start + k) % len(sponsors)] for k in range(n)]
    _save_cursor((start + n) % len(sponsors))

    # Fetch the boards concurrently (cached; bounded). Each returns normalized postings or None.
    sem = asyncio.Semaphore(8)

    async def _board(company):
        async with sem:
            try:
                return company, await _fetch_company_postings(company)
            except Exception:
                return company, None

    boards = await asyncio.gather(*[_board(c) for c in slice_])

    discovered: list[dict] = []
    companies_with_jobs = 0
    for company, postings in boards:
        if not postings:
            continue
        # Title must fit AND the posting must not be foreign-only (strict US — drop France/EMEA/etc.
        # right at discovery so foreign roles never reach the queue). _loc_class keeps US + unknown.
        hits = [p for p in postings
                if _title_ok(p.get("title")) and _loc_class(p.get("location", "")) != "foreign"]
        if hits:
            companies_with_jobs += 1
        for p in hits:
            discovered.append({
                "company": company,
                "title": p.get("title"),
                "description": p.get("description", ""),
                "location": p.get("location", ""),
                "posted_at": p.get("posted_at", ""),
                "source": f"board:{p.get('ats', '')}",
                "fallback_url": p.get("url"),
            })

    logger.info("board_scan_discovered", scanned=len(slice_), with_jobs=companies_with_jobs,
                roles=len(discovered), cursor=start)
    if not discovered:
        return state, {"scanned": len(slice_), "discovered": 0, "queued": 0}

    pre_app_ids = {a.get("id") for a in state.get("applications", [])}
    pre_job_ids = {j.get("id") for j in state.get("job_listings", [])}
    updated, report = await intake_jobs(discovered, state, limit=intake_limit, tailor=tailor)
    report["scanned"] = len(slice_)
    report["discovered"] = len(discovered)

    if persist:
        # Persist the DELTA via a locked merge — never overwrite the whole state from this
        # (possibly stale) snapshot, so concurrent writers (scheduler/on-demand) aren't clobbered.
        from core.state_store import merge_new
        new_jobs = [j for j in updated.get("job_listings", []) if j.get("id") not in pre_job_ids]
        new_apps = [a for a in updated.get("applications", []) if a.get("id") not in pre_app_ids]
        report["persisted"] = merge_new(new_jobs, new_apps)

    logger.log_decision(action="board_scan_complete", outcome="success", details=report)
    return updated, report


async def topup(target: int = 12, *, batch: int = 25, tailor: bool = False, max_rounds: int = 4):
    """Scan sponsor boards in batches until the queue holds >= `target` FRESH pending_review apps
    (or the boards are exhausted / max_rounds hit). The freshness gate drops ~85% of postings, so a
    single small batch rarely nets enough — this keeps scanning the rotation until the target is met.
    Persists via locked merge. Returns a summary dict."""
    from core.state_store import load_state
    from agents.application.dedup_ledger import reconcile_with_state
    reconcile_with_state(load_state())   # self-heal: ledger must match reality before scanning
    rounds, total_added = 0, 0
    while rounds < max_rounds:
        cur = sum(1 for a in load_state().get("applications", []) if a.get("status") == "pending_review")
        if cur >= target:
            break
        state = load_state()
        _, report = await run(state, max_companies=batch, intake_limit=max(target - cur + 5, 10),
                              tailor=tailor, persist=True)
        added = report.get("persisted", 0)
        total_added += added
        rounds += 1
        logger.info("board_topup_round", round=rounds, fresh_now=cur, added=added,
                    discovered=report.get("discovered", 0))
        if added == 0 and report.get("discovered", 0) == 0:
            break   # this batch found nothing new — boards exhausted for now
    final = sum(1 for a in load_state().get("applications", []) if a.get("status") == "pending_review")
    return {"target": target, "fresh_in_queue": final, "added": total_added, "rounds": rounds}
