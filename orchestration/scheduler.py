"""
APScheduler-based scheduler for all LinkedIn jobs.
Runs alongside the main LangGraph pipeline; LinkedIn tasks are independent
(they don't block or depend on the application pipeline).

FOCUS: LinkedIn apply pipeline is LIVE. The recruiter-outreach jobs
(contact_finder, outreach_runner) are SLEPT so the focus stays on
scan → score → tailor → fill → park → learn. Toggle via _ACTIVE_JOBS below.

Schedule (approximate — all intervals have ±20% jitter):
  [LIVE]  linkedin_health_check   every 30 min   — check for CAPTCHA/restrictions
  [LIVE]  linkedin_scanner        every 2 hours  — discover new LinkedIn postings (ACCOUNT-SAFE: do NOT speed up)
  [SLEEP] contact_finder          every 6 hours  — find HMs/recruiters for target companies
  [SLEEP] outreach_runner         every 2 hours  — advance cadence for due contacts
  [LIVE]  board_scanner           every 1 hour   — off-LinkedIn discovery via public ATS APIs (safe to speed up)
  [LIVE]  apply_pipeline          every 1 hour   — score → park (cheap, Haiku); tailor on-demand
  [LIVE]  email_monitor           every 1 hour   — capture outcomes → learning loop → insights
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from core.logger import get_logger
from core.state import JobSearchState

logger = get_logger("scheduler")

_STATE_FILE = Path("data/pipeline_state.json")


def _load_state() -> JobSearchState:
    """Load via the shared safe store. A CORRUPT file raises (the calling job's try/except logs +
    skips) — we never silently return empty, which would let _sync_db prune-wipe the DB."""
    from core.state_store import load_state, StateCorruptError
    try:
        state = load_state()
    except StateCorruptError as e:
        logger.error("state_file_corrupt", error=str(e)[:160])
        raise
    return state


# Back-compat re-exports (dedup logic now lives in core.state_store).
def _dedup_state(state: JobSearchState) -> JobSearchState:
    from core.state_store import dedup_state
    return dedup_state(state)


def _save_state(state: JobSearchState) -> None:
    """Cross-process-locked, atomic, deduped write — safe against concurrent writers/crashes.
    Stamps last_run_at so the freshness health check reflects real scheduler activity."""
    from core.state_store import save_state
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state, dedup=True)


def _jitter_seconds(base_seconds: int) -> int:
    """Add ±20% jitter to a base interval."""
    delta = int(base_seconds * 0.2)
    return base_seconds + random.randint(-delta, delta)


async def _sync_db() -> None:
    """Refresh the SQL DB the UI reads from (idempotent, decision-preserving).
    Never raises — a DB outage must not break the scheduler."""
    try:
        from db.migrate_from_json import sync_to_db_async
        counts = await sync_to_db_async()
        if counts:
            logger.info("scheduled_db_synced",
                        apps=counts.get("apps"),
                        decisions_preserved=counts.get("decisions_preserved"))
    except Exception as e:
        logger.warning("scheduled_db_sync_failed", error=str(e))


# ── Job functions ──────────────────────────────────────────────────────────

async def job_health_check() -> None:
    # Heartbeat: prove the scheduler is alive every tick (feeds the freshness health check),
    # independent of whether any pipeline job saved state this cycle.
    try:
        from pathlib import Path
        Path("data/scheduler_heartbeat").write_text(datetime.now(timezone.utc).isoformat())
    except Exception:
        pass
    from agents.monitoring.linkedin_health import run_health_check
    try:
        status = await run_health_check()
        logger.info("scheduled_health_check", status=status)
    except Exception as e:
        logger.error("scheduled_health_check_failed", error=str(e))


async def job_digest() -> None:
    """Daily system-health + activity digest. Writes data/daily_digest.json and LOGS an alert when
    health is degraded (JSON↔DB drift, stale pipeline, etc.) — so silent breakage gets surfaced."""
    try:
        from agents.monitoring.health import system_health, build_digest
        health = system_health(probe_llm=False)   # no LLM spend on a routine probe
        build_digest(write=True)
        checks = health.get("checks", [])
        failing = [c.get("name") for c in checks if c.get("status") != "ok"]
        # Only these signal SILENT breakage. linkedin-paused / gmail-token / anthropic-credits are
        # known/managed states that would otherwise spam the alert every day.
        critical = {"datastore_sync", "pipeline_freshness"}
        crit_fail = [n for n in failing if n in critical]
        if crit_fail:
            logger.error("system_health_degraded", critical=crit_fail, all_failing=failing)
        elif failing:
            logger.warning("system_health_warnings", failing=failing)
        else:
            logger.info("system_health_ok", checks=len(checks))
    except Exception as e:
        logger.error("scheduled_digest_failed", error=str(e)[:140])


async def job_linkedin_scan() -> None:
    from agents.monitoring.linkedin_health import is_auto_paused
    from agents.discovery.linkedin_scanner import run
    if is_auto_paused():
        logger.info("linkedin_scan_skipped_paused")
        return
    try:
        state = _load_state()
        updated = await run(state)
        _save_state(updated)
        new_jobs = len(updated.get("job_listings", [])) - len(state.get("job_listings", []))
        logger.info("scheduled_linkedin_scan_done", new_jobs=new_jobs)
    except Exception as e:
        logger.error("scheduled_linkedin_scan_failed", error=str(e))


async def job_contact_finder() -> None:
    from agents.monitoring.linkedin_health import is_auto_paused
    from agents.outreach.contact_finder import run
    if is_auto_paused():
        logger.info("contact_finder_skipped_paused")
        return
    try:
        state = _load_state()
        updated = await run(state)
        _save_state(updated)
        new_contacts = len(updated.get("linkedin_contacts", [])) - len(state.get("linkedin_contacts", []))
        logger.info("scheduled_contact_finder_done", new_contacts=new_contacts)
    except Exception as e:
        logger.error("scheduled_contact_finder_failed", error=str(e))


async def job_outreach_runner() -> None:
    from agents.monitoring.linkedin_health import is_auto_paused
    from agents.outreach.outreach_runner import run
    if is_auto_paused():
        logger.info("outreach_runner_skipped_paused")
        return
    try:
        state = _load_state()
        updated = await run(state)
        _save_state(updated)
        logger.info("scheduled_outreach_runner_done")
    except Exception as e:
        logger.error("scheduled_outreach_runner_failed", error=str(e))


async def job_email_monitor() -> None:
    from agents.monitoring.email_monitor import run
    try:
        state = _load_state()
        updated = await run(state)
        # New interview → generate a prep brief grounded in the experience bank + scope notes,
        # and wire MeetingScheduled.prep_doc_path. Runs BEFORE the learning loop so this cycle's
        # insights report can join interviews against the fresh prep records. Idempotent.
        try:
            from agents.application.interview_prep import prep_new_interviews
            n_prep = await prep_new_interviews(updated)
            if n_prep:
                logger.info("scheduled_interview_prep_done", generated=n_prep)
        except Exception as e:
            logger.warning("scheduled_interview_prep_failed", error=str(e)[:140])
        # Capture → learn: convert outcomes into learnings + insights report.
        try:
            from agents.learning.feedback_loop import run as learn_run
            updated = await learn_run(updated)
        except Exception as e:
            logger.warning("scheduled_learning_failed", error=str(e))
        _save_state(updated)
        await _sync_db()
    except Exception as e:
        logger.error("scheduled_email_monitor_failed", error=str(e))


async def job_apply_pipeline() -> None:
    """ATS intake — resolve each discovered lead to its real company ATS form + Opus-tailored
    résumé, then PARK in the review queue. Runs OFF LinkedIn (resolve_ats_for hits company
    board APIs: Greenhouse/Lever/Ashby/Workable) — no browser, no LinkedIn navigation, no
    account risk. Works on leads from ANY source (the LinkedIn radar OR the board scanners)."""
    try:
        from core import budget_guard
        if budget_guard.over_budget():
            logger.info("apply_pipeline_skipped_budget", spent=round(budget_guard.spent_today(), 2))
            return
        import yaml
        from agents.application.ats_intake import intake_jobs
        from agents.discovery.targeting import rank_for_application
        state = _load_state()
        # Leads = discovered job_listings that don't have an application yet.
        app_job_ids = {a.get("job_id") for a in state.get("applications", [])}
        leads = [j for j in state.get("job_listings", [])
                 if j.get("id") not in app_job_ids
                 and j.get("company_name") and j.get("title")]
        if not leads:
            logger.info("apply_pipeline_no_leads")
            return
        # Rank by fit × sponsorship × recency (drops sponsorship knock-outs + agencies) so the
        # freshest, most-winnable roles are intaken first — early-applicant on the best ones.
        try:
            profile = yaml.safe_load((Path("data/user_profile.yaml")).read_text())
        except Exception:
            profile = {}
        fit_by_id = {f.get("job_id"): f for f in state.get("fit_scores", [])}
        ranked = rank_for_application(leads, fit_by_id, profile, state)
        ordered = [r["job"] for r in ranked] if ranked else leads
        # Only examine the top-ranked (freshest × best-fit × sponsor) slice — NOT the whole
        # backlog. Phase-1 resolution is concurrent but bounded, so this keeps each run fast
        # and aimed at the newest roles (early-applicant). 12 of these get tailored + parked.
        ordered = ordered[:120]
        discovered = [{"company": j["company_name"], "title": j["title"],
                       "description": j.get("description"), "location": j.get("location"),
                       "source": j.get("source"), "job_id": j["id"],
                       "fallback_url": j.get("apply_url")} for j in ordered]
        logger.info("apply_pipeline_intake_start", leads=len(leads), examining=len(discovered))
        # Cheap-park: score + resolve + park, NO Opus (tailored on demand when engaged).
        updated, report = await intake_jobs(discovered, state, limit=30, tailor=False)
        # Keep the queue pointing at LIVE forms: re-resolve stale/dead/orphan apply_urls so the
        # user never opens a filled (404) role or a general careers page from the review queue.
        try:
            from agents.application.url_refresher import refresh_pending_urls
            updated, refresh_report = await refresh_pending_urls(updated, apply=True)
            report["url_refresh"] = refresh_report
        except Exception as e:
            logger.warning("apply_pipeline_url_refresh_failed", error=str(e)[:120])
        _save_state(updated)
        await _sync_db()
        logger.info("scheduled_apply_pipeline_done", **report)
    except Exception as e:
        logger.error("scheduled_apply_pipeline_failed", error=str(e)[:140])


async def job_board_scan() -> None:
    """Off-LinkedIn DISCOVERY — every cycle, scan ALL sponsor-company public ATS boards (Greenhouse/
    Lever/Ashby/Workable) and cheap-park EVERY fresh, US, on-target, NON-DUPLICATE role that passes
    the gates. Unbounded parking lot (no target), dedup-protected. Zero browser, zero LinkedIn —
    pure board-API requests, so it refuels the queue even while the LinkedIn radar is blocked."""
    try:
        # No budget gate here: discovery scoring is Haiku-only (pennies) and is exempt from the cap
        # in core/llm.py, so the queue keeps refreshing even on an Opus-heavy day.
        from core.state_store import load_state
        from agents.discovery.board_scanner import run
        from agents.application.dedup_ledger import reconcile_with_state
        # CONTINUOUS unbounded discovery (the user's model): every cycle, scan ALL sponsor boards and
        # cheap-park EVERY fresh, US, on-target role that passes the gates — no target/cap on the
        # parking lot. The dedup ledger guarantees no role is ever added twice (skipped_dup). Haiku-
        # only scoring (tailor=False), so an unbounded lot is cheap; Opus tailoring stays on-demand.
        state = load_state()
        reconcile_with_state(state)              # self-heal the dedup ledger so 'no duplicates' holds
        _, summary = await run(state, max_companies=200, intake_limit=400, tailor=False, persist=True)
        await _sync_db()
        logger.info("scheduled_board_scan_done", **summary)
    except Exception as e:
        logger.error("scheduled_board_scan_failed", error=str(e)[:140])


async def job_teksystems_scan() -> None:
    """Off-LinkedIn DISCOVERY for TEKsystems (Phenom, PerimeterX-protected) via its PUBLIC sitemaps +
    JSON-LD — crawler-facing, so no bot-fight, no browser. Cheap-parks fresh, US, on-target roles
    (Haiku scoring, tailor=False; Opus tailoring stays on-demand). NOTE: staffing/contract roles —
    sourced separately from the sponsor boards, and the per-company cap keeps it to a few at a time."""
    try:
        from core.state_store import load_state
        from agents.discovery.teksystems_scanner import run
        state = load_state()
        _, summary = await run(state, limit=400, tailor=False, persist=True, max_details=80)
        await _sync_db()
        logger.info("scheduled_teksystems_scan_done", **summary)
    except Exception as e:
        logger.error("scheduled_teksystems_scan_failed", error=str(e)[:140])


async def job_kforce_scan() -> None:
    """Off-LinkedIn DISCOVERY for Kforce via its PUBLIC Azure Search API — plain HTTP, no browser,
    no bot-fight. Cheap-parks fresh, US, on-target roles (Haiku scoring, tailor=False; Opus tailoring
    stays on-demand). NOTE: staffing/contract roles — cap-exempt in ats_intake."""
    try:
        from core.state_store import load_state
        from agents.discovery.kforce_scanner import run
        state = load_state()
        _, summary = await run(state, limit=400, tailor=False, persist=True, top=30)
        await _sync_db()
        logger.info("scheduled_kforce_scan_done", **summary)
    except Exception as e:
        logger.error("scheduled_kforce_scan_failed", error=str(e)[:140])


async def job_insight_global_scan() -> None:
    """Off-LinkedIn DISCOVERY for Insight Global's DIRECT-HIRE roles via its public Jibe careers API
    — plain HTTP, no browser. (Their contract-placement portal is a browser-only iCIMS SPA and is
    deliberately not scanned.) Haiku scoring, tailor=False; Opus tailoring stays on-demand."""
    try:
        from core.state_store import load_state
        from agents.discovery.insight_global_scanner import run
        state = load_state()
        _, summary = await run(state, limit=400, tailor=False, persist=True, max_pages=15)
        await _sync_db()
        logger.info("scheduled_insightglobal_scan_done", **summary)
    except Exception as e:
        logger.error("scheduled_insightglobal_scan_failed", error=str(e)[:140])


async def job_auto_submit() -> None:
    """Auto-submit the safe (gate-passed, CAPTCHA-free) reviewed apps; queue the rest.
    No-ops unless AUTONOMOUSHIRE_AUTO_SUBMIT=1 and no kill-switch — so it's inert until
    the user opts into hands-off applying."""
    from agents.application.auto_apply import auto_submit_enabled, select_auto_submit
    if not auto_submit_enabled():
        logger.info("auto_submit_disabled")
        return
    try:
        import yaml
        from agents.application.form_filler import submit_application
        from core.config import get_settings
        from core.llm import get_llm
        state = _load_state()
        profile = yaml.safe_load(Path("data/user_profile.yaml").read_text())
        sel = select_auto_submit(state, profile, get_settings().apply_max_per_day)
        if not sel["submit"]:
            logger.info("auto_submit_nothing_eligible", queued=len(sel["queue"]))
            return
        llm = get_llm("form_filler", model="claude-haiku-4-5")
        by_id = {a["id"]: i for i, a in enumerate(state["applications"])}
        apps_by_job = {a["job_id"]: a for a in state["applications"]}
        n = 0
        for job in sel["submit"]:
            app = apps_by_job.get(job["id"])
            if not app:
                continue
            r = await submit_application(app, job, profile, llm, headless=True, auto_safe=True)
            if app["id"] in by_id:
                state["applications"][by_id[app["id"]]] = r
            n += 1
            _save_state(state)
        await _sync_db()
        logger.info("scheduled_auto_submit_done", submitted=n, queued=len(sel["queue"]))
    except Exception as e:
        logger.error("scheduled_auto_submit_failed", error=str(e))


async def job_referral_prep() -> None:
    """Route the highest-fit reviewed roles through the REFERRAL channel FIRST — draft a warm-intro
    kit and flag each app `referral_first` so the review queue surfaces the referral path before a
    cold apply. Referrals convert ~10x the ATS pile, so the best-fit roles should never go in cold
    without at least offering the warm path. Bounded to a few cheap Haiku calls per run."""
    try:
        from agents.outreach.referral import prepare_referral_targets
        res = await prepare_referral_targets(limit=5)
        await _sync_db()
        logger.info("scheduled_referral_prep_done", **res)
    except Exception as e:
        logger.error("scheduled_referral_prep_failed", error=str(e)[:140])


# ── Scheduler factory ──────────────────────────────────────────────────────

# Which jobs are LIVE. The LinkedIn apply pipeline (scan → fill → park) plus the
# capture→learn loop. Recruiter-outreach jobs are SLEPT to keep the focus clear —
# add "contact_finder"/"outreach_runner" back here to wake them.
_ACTIVE_JOBS = {"linkedin_health", "linkedin_scanner", "board_scanner", "teksystems_scanner",
                "kforce_scanner", "insightglobal_scanner", "apply_pipeline", "referral_prep",
                "auto_submit", "email_monitor", "digest"}

# id → (func, base_interval_seconds, display_name, misfire_grace_time)
_JOB_SPECS = {
    "linkedin_health": (job_health_check,    1800,  "LinkedIn Health Check",                  120),
    "linkedin_scanner": (job_linkedin_scan,   7200, "LinkedIn Job Scanner (2h, account-safe)", 300),
    "contact_finder":  (job_contact_finder,  21600, "LinkedIn Contact Finder",                300),
    "outreach_runner": (job_outreach_runner, 7200,  "LinkedIn Outreach Runner",               120),
    "board_scanner":   (job_board_scan,      3600,  "Board Scanner (off-LinkedIn discovery, 1h)", 600),
    "teksystems_scanner": (job_teksystems_scan, 21600, "TEKsystems Scanner (sitemap discovery, 6h)", 600),
    "kforce_scanner":  (job_kforce_scan,     21600, "Kforce Scanner (Azure Search API, 6h)",   600),
    "insightglobal_scanner": (job_insight_global_scan, 21600, "Insight Global Scanner (Jibe API, 6h)", 600),
    "apply_pipeline":  (job_apply_pipeline,  3600,  "Apply Pipeline (score+park cheap, 1h)",   600),
    "referral_prep":   (job_referral_prep,   7200,  "Referral Prep (high-fit warm-intro kits, 2h)", 300),
    "auto_submit":     (job_auto_submit,     43200, "Auto-Submit Safe Apps (opt-in)",         600),
    "email_monitor":   (job_email_monitor,   3600,  "Gmail Email Monitor (1h)",               120),
    "digest":          (job_digest,          86400, "Daily Health + Activity Digest",          600),
}


def build_scheduler() -> AsyncIOScheduler:
    """Build and return a configured APScheduler (not yet started).

    Only jobs in `_ACTIVE_JOBS` are registered; the rest are slept (defined but
    not scheduled) so the LinkedIn apply pipeline stays the clear focus.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")

    for job_id, (func, interval, name, grace) in _JOB_SPECS.items():
        if job_id not in _ACTIVE_JOBS:
            logger.info("scheduler_job_slept", job=job_id)
            continue
        scheduler.add_job(
            func,
            trigger=IntervalTrigger(seconds=_jitter_seconds(interval)),
            id=job_id,
            name=name,
            max_instances=1,
            misfire_grace_time=grace,
        )

    return scheduler


async def run_scheduler() -> None:
    """Start the scheduler and run forever (Ctrl+C to stop)."""
    scheduler = build_scheduler()
    scheduler.start()
    logger.info(
        "scheduler_started",
        jobs=[j.id for j in scheduler.get_jobs()],
    )
    try:
        # Run an immediate health check on startup
        await job_health_check()
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("scheduler_stopped")


if __name__ == "__main__":
    asyncio.run(run_scheduler())
