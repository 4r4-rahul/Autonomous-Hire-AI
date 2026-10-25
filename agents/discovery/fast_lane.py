"""Surface a contract role MINUTES after it posts, not hours.

The problem this solves, measured: a role posted at 7am reached the queue around 10:53am. Scans ran
every ~4-9h, and one full Dice cycle took 79 minutes — so tightening the interval alone would just
overlap slow cycles. Almost none of that 79 minutes is fetching listings. It is the per-posting JD
fetch (~160 extra round-trips) plus a Haiku fit score for every lead. Both are worth doing; neither
belongs on the latency path.

So discovery is split in two:

  FAST (this module, every ~10 min in market hours)
      fetch listing pages -> deterministic filters -> straight into the queue, unscored.
      No JD enrichment, no LLM, no ATS resolution. ~13 HTTP GETs, a few seconds.
      The role is visible with title, company, location and apply link — enough to act on.

  DEEP (the existing dice_scanner run, every 4h)
      full pagination, full JDs, resolution, scoring. Unchanged.

A fast-lane row carries `needs_scoring: True`. `score_pending()` below is the reconciler that fills
in the JD and fit score afterwards and retires roles that turn out not to fit — so the queue is
eventually exactly as curated as the deep path, it just stops making the user wait for that.

The deterministic filters here are the ones that need no network and no model: title exclusions, the
experience ceiling, US-only, and the C2C check. They are cheap and they are the same ones the deep
path applies, so a role the fast lane admits is never one the deep path would have rejected outright
— it is only one whose FIT is not known yet.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.logger import get_logger

logger = get_logger("fast_lane")

_ROOT = Path(__file__).parent.parent.parent
_SEEN = _ROOT / "data" / "fast_lane_seen.json"
_HEALTH = _ROOT / "data" / "fast_lane_health.json"

# The queries worth paying 10-minute latency for. The deep scan still sweeps all 13 — this is the
# subset where being first actually converts, kept short so the poll stays light on Dice.
FAST_QUERIES = ("data engineer", "aws data engineer", "pyspark", "snowflake", "python developer")

# How deep the fast poll goes. ONE page, on purpose. A brand-new posting surfaces on page 1 within
# its relevance band, and the deep scan sweeps 6 pages anyway. This was 2 until Dice started
# refusing connections outright during this module's build-out: 5 queries x 1 page = 5 requests a
# poll, ~20/hour, against 10 requests and ~60/hour before. The source is worth more than the extra
# page.
FAST_PAGES = 1


# ── Circuit breaker ────────────────────────────────────────────────────────────────────────
# Polling a board every 10 minutes is a privilege it can revoke. During this module's own build-out
# Dice went from 200 to refusing connections outright (curl code 000) after a burst of probing, and
# SimplyHired flipped to 403 — so this is not hypothetical. A fast poll that keeps hammering a host
# that just cut us off turns a 20-minute cooldown into a lasting ban, and Dice is the single best
# contract source there is. Losing it would cost far more than the latency this module buys.
#
# So: an empty fetch is treated as "possibly blocked" and the poll backs off exponentially, from one
# skipped cycle up to a 4-hour hold. Any successful fetch clears it immediately. The deep scan runs
# independently on its own slow cadence and will recover the window's postings regardless, so a
# backed-off fast lane costs latency, never coverage.
_BACKOFF_BASE_S = 600
_BACKOFF_MAX_S = 14400


def _health() -> dict:
    try:
        return json.loads(_HEALTH.read_text())
    except Exception:
        return {"consecutive_empty": 0, "backoff_until": 0.0}


def _set_health(consecutive_empty: int, backoff_until: float) -> None:
    try:
        _HEALTH.write_text(json.dumps(
            {"consecutive_empty": consecutive_empty, "backoff_until": backoff_until}) + "\n")
    except Exception:
        pass


def _load_seen() -> set[str]:
    try:
        return set(json.loads(_SEEN.read_text()).get("urls", []))
    except Exception:
        return set()


def _save_seen(urls: set[str]) -> None:
    # Bounded: the deep scan and the state store are the real dedup authorities. This file only has
    # to stop the fast poll re-queueing something it queued an hour ago, so recent history is enough.
    keep = list(urls)[-8000:]
    _SEEN.write_text(json.dumps({"urls": keep}, indent=0) + "\n")


def _passes_cheap_filters(job: dict, profile: dict) -> tuple[bool, str]:
    """Every filter that needs neither a network call nor a model. Mirrors the deep path's hard
    gates so the fast lane can't admit something the deep path would reject outright."""
    from agents.discovery.fit_analyst import _quick_prefilter

    # The listing summary is all we have; _quick_prefilter reads title + description and applies the
    # title excludes, the experience ceiling and the sponsorship/salary gates.
    stub = {
        "title": job.get("title", ""),
        "company_name": job.get("company", ""),
        "description": job.get("summary") or "",
        "salary_max": None,
    }
    ok, reason = _quick_prefilter(stub, profile)
    if not ok:
        return False, reason

    from agents.application.ats_intake import _is_relevant_location
    if not _is_relevant_location(job.get("title", ""), job.get("location"),
                                 (job.get("summary") or "")[:200]):
        return False, "non-US location"
    return True, ""


async def poll(*, age: str = "ONE", persist: bool = True) -> dict:
    """One fast pass. Returns a report dict. `age=ONE` is the last 24h — the fast lane only cares
    about what is genuinely new, and a narrow window keeps the response small."""
    from agents.discovery.dice_scanner import scan
    from agents.application.resume_tailor import _load_profile
    from core.employment import classify_employment_for_job

    report = {"fetched": 0, "new": 0, "queued": 0, "skipped_seen": 0,
              "skipped_filter": 0, "skipped_not_contract": 0}

    import time as _time
    h = _health()
    now_ts = _time.time()
    if now_ts < h.get("backoff_until", 0):
        report["backoff_s_remaining"] = int(h["backoff_until"] - now_ts)
        logger.info("fast_lane_backoff_skip", **report)
        return report

    jobs = await scan(age=age, pages=FAST_PAGES, queries=FAST_QUERIES, enrich_jds=False)
    report["fetched"] = len(jobs)
    if not jobs:
        # Empty is indistinguishable from blocked at this layer, and the expensive mistake is
        # assuming "just quiet" and carrying on at full rate. Back off; a real quiet spell costs
        # only latency the deep scan already covers.
        n = int(h.get("consecutive_empty", 0)) + 1
        hold = min(_BACKOFF_BASE_S * (2 ** (n - 1)), _BACKOFF_MAX_S)
        _set_health(n, now_ts + hold)
        report["consecutive_empty"] = n
        report["backing_off_s"] = hold
        logger.warning("fast_lane_empty_backoff", **report)
        return report
    if h.get("consecutive_empty"):
        _set_health(0, 0.0)          # recovered

    seen = _load_seen()
    profile = _load_profile()

    fresh = []
    for j in jobs:
        url = j.get("url") or ""
        if not url:
            continue
        if url in seen:
            report["skipped_seen"] += 1
            continue
        seen.add(url)
        report["new"] += 1
        ok, _reason = _passes_cheap_filters(j, profile)
        if not ok:
            report["skipped_filter"] += 1
            continue
        fresh.append(j)

    # Only remember what we actually committed. Marking URLs seen on a dry run (persist=False)
    # would make the next REAL poll skip them as already-handled and they'd never be queued.
    if persist:
        _save_seen(seen)
    if not fresh:
        return report

    now = datetime.now(timezone.utc).isoformat()
    new_jobs, new_apps = [], []
    for j in fresh:
        summary = j.get("summary") or f"{j['title']} at {j['company']}. {j['employment']}."
        employ = classify_employment_for_job(
            source="dice:contract", company=j.get("company", ""), title=j.get("title", ""),
            description=summary, employment_type=j.get("employment", ""))
        if employ != "contract":
            report["skipped_not_contract"] += 1
            continue
        jid = str(uuid.uuid4())
        new_jobs.append({
            "id": jid, "company_id": "", "company_name": j.get("company", ""),
            "title": j.get("title", ""), "description": summary,
            "required_skills": [], "nice_to_have_skills": [],
            "location": j.get("location") or "Remote", "remote": True,
            "salary_min": None, "salary_max": None,
            "apply_url": j.get("url", ""), "source": "dice:fastlane",
            "discovered_at": now, "posted_at": j.get("posted_at", ""),
        })
        new_apps.append({
            "id": str(uuid.uuid4()), "job_id": jid,
            "company_name": j.get("company", ""), "role_title": j.get("title", ""),
            "status": "pending_review", "tailored_resume_path": None,
            "quality_score": None, "fit_score": None,
            # The two flags the reconcilers key off: this row still owes a fit score, and (as with
            # every cheap-parked role) a résumé is tailored only when the user engages it.
            "needs_scoring": True, "needs_tailoring": True,
            "employment_type": employ, "created_at": now,
        })

    if persist and new_jobs:
        from core.state_store import merge_new
        report["queued"] = merge_new(new_jobs, new_apps)
    else:
        report["queued"] = len(new_jobs)

    logger.info("fast_lane_poll", **report)
    return report


async def score_pending(*, limit: int = 40) -> dict:
    """Fill in what the fast lane deliberately skipped: the real JD and the fit score.

    Runs behind the fast poll, so the user sees a role immediately and it sharpens a few minutes
    later. A role that fails the fit gate here is marked skipped rather than deleted — the same
    disposition the deep path gives it, and it keeps the decision auditable.
    """
    from core.state_store import mutate_state
    from agents.discovery.dice_scanner import fetch_full_jd
    from agents.application.linkedin_apply_pipeline import _score_job
    from core.llm import get_llm
    from core.config import get_settings
    import httpx
    from agents.discovery.dice_scanner import _UA

    report = {"examined": 0, "scored": 0, "dropped_fit": 0, "errors": 0}

    # Snapshot the work OUTSIDE the state lock: scoring is slow (network + model) and holding the
    # cross-process lock across it would stall every other writer.
    from core.state_store import load_state
    state = load_state()
    apps = {a["id"]: a for a in state.get("applications", [])}
    jobs = {j["id"]: j for j in state.get("job_listings", [])}
    todo = [a for a in state.get("applications", [])
            if a.get("needs_scoring") and a.get("status") == "pending_review"][:limit]
    if not todo:
        return report
    report["examined"] = len(todo)

    llm = get_llm("fast_lane", model="claude-haiku-4-5")
    threshold = get_settings().min_fit_score
    sem = asyncio.Semaphore(6)
    results: dict[str, dict] = {}

    async with httpx.AsyncClient(follow_redirects=True, headers=_UA) as client:
        async def one(app):
            job = jobs.get(app.get("job_id"))
            if not job:
                return
            async with sem:
                try:
                    full = await fetch_full_jd(client, job.get("apply_url", ""))
                    if len(full) > max(600, len(job.get("description") or "")):
                        job = dict(job, description=full)
                    fit = await _score_job(job, llm)
                except Exception as e:
                    logger.warning("fast_lane_score_failed", app=app["id"], error=str(e)[:90])
                    report["errors"] += 1
                    return
            results[app["id"]] = {"fit": fit, "description": job.get("description", "")}

        await asyncio.gather(*[one(a) for a in todo])

    if not results:
        return report

    with mutate_state() as st:
        by_job = {j["id"]: j for j in st.get("job_listings", [])}
        for a in st.get("applications", []):
            r = results.get(a.get("id"))
            if not r:
                continue
            score = int(r["fit"].get("score", 0))
            a["fit_score"] = score
            a["needs_scoring"] = False
            j = by_job.get(a.get("job_id"))
            if j and r["description"]:
                j["description"] = r["description"]
            if score < threshold:
                # Same disposition the deep path gives a below-threshold role, and it stays visible
                # as a decision rather than vanishing.
                a["status"] = "skipped"
                a["human_decision"] = "auto_skipped_fit_fastlane"
                report["dropped_fit"] += 1
            else:
                report["scored"] += 1

    logger.info("fast_lane_scored", **report)
    return report
