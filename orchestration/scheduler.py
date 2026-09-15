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
import os
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
        # Close the DB→JSON loop: UI "Mark Applied" and extension submits land in the DB; without
        # this, the JSON keeps showing them pending_review and the two stores drift (the root cause of
        # the queue-count wobble). reconcile is FORWARD-ONLY (rank-gated) so it only ADVANCES a JSON
        # app to a decided status the DB already holds — never regresses a submitted app or resurrects
        # a stale one. Safe to run every cycle now that the historical bogus-batch drift is cleaned.
        try:
            from core.decision_sync import reconcile_db_decisions_to_json
            rec = await reconcile_db_decisions_to_json()
            if rec.get("changed", {}).get("total"):
                logger.info("scheduled_decision_reconcile_done", changed=rec["changed"])
        except Exception as e:
            logger.warning("scheduled_decision_reconcile_failed", error=str(e)[:140])
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
        return report
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
        return summary
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


async def job_phenom_scan() -> None:
    """Off-LinkedIn DISCOVERY across Phenom-platform STAFFING firms (TEKsystems + Aston Carter +
    Actalent, all Allegis Group H1B sponsors) via their PUBLIC sitemaps + JSON-LD — crawler-facing,
    so no bot-fight, no browser. Cheap-parks fresh, US, on-target roles (Haiku scoring, tailor=False;
    Opus tailoring stays on-demand). Supersedes the single-host teksystems_scanner. Contract roles —
    sourced separately from the sponsor boards; the W2 gates + employment classifier handle them."""
    try:
        from core.state_store import load_state
        from agents.discovery.phenom_scanner import run
        state = load_state()
        _, summary = await run(state, limit=400, tailor=False, persist=True, max_details=80)
        await _sync_db()
        logger.info("scheduled_phenom_scan_done", **summary)
        return summary
    except Exception as e:
        logger.error("scheduled_phenom_scan_failed", error=str(e)[:140])


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
        return summary
    except Exception as e:
        logger.error("scheduled_kforce_scan_failed", error=str(e)[:140])


async def job_dice_scan() -> None:
    """Fresh C2C contract roles from Dice — the board US IT staffing actually posts on, and the only
    source that labels corp-to-corp explicitly (employmentType "Contract, Third Party"). Enriches
    each posting with the FULL job description from its detail page (the search API truncates at 500
    chars and sometimes returns text from an unrelated req), then cheap-parks what fits.

    This is the freshest contract supply the pipeline has: a 7-day sweep yields ~150 roles where the
    vendor ATS boards yield ~1. Runs every 4h on a 3-day window so new postings are caught early
    without re-reading the same week repeatedly."""
    try:
        from core.state_store import load_state
        from agents.discovery.dice_scanner import run
        _, summary = await run(load_state(), age="THREE", pages=2, limit=200,
                               tailor=False, persist=True)
        await _sync_db()
        logger.info("scheduled_dice_scan_done", **summary)
        return summary
    except Exception as e:
        logger.error("scheduled_dice_scan_failed", error=str(e)[:140])


async def job_gov_scan() -> None:
    """State/local-GOVERNMENT contract (C2C) discovery — public-sector staff-augmentation vendor
    boards (data/gov_vendors.json). Plain HTTP JSON, no browser, no LinkedIn. Drops FEDERAL clients
    and USC/GC-only reqs before spending anything, tags the government end client, and cheap-parks
    the rest (Haiku scoring, tailor=False). Runs on a slow cadence: these vendor boards turn over a
    handful of live reqs a day — the value is catching those, not re-reading 5k zombie postings."""
    try:
        from core.state_store import load_state
        from agents.discovery.gov_scanner import run
        _, summary = await run(load_state(), limit=200, tailor=False, persist=True)
        await _sync_db()
        logger.info("scheduled_gov_scan_done", **summary)
        return summary
    except Exception as e:
        logger.error("scheduled_gov_scan_failed", error=str(e)[:140])


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
        return summary
    except Exception as e:
        logger.error("scheduled_insightglobal_scan_failed", error=str(e)[:140])


async def job_workday_scan() -> None:
    """Off-LinkedIn DISCOVERY across big H1B-sponsoring IT-services firms on WORKDAY (NTT Data,
    Genpact, Accenture) via the public cxs JSON API — plain HTTP, no browser, no key. US-filtered
    (location facet where exposed + client-side). LOW-YIELD by nature (these firms are US-sparse and
    India-heavy), but unlocks sponsors no board API reaches. Haiku scoring, tailor=False."""
    try:
        from core.state_store import load_state
        from agents.discovery.workday_scanner import run
        state = load_state()
        _, summary = await run(state, limit=400, tailor=False, persist=True)
        await _sync_db()
        logger.info("scheduled_workday_scan_done", **summary)
        return summary
    except Exception as e:
        logger.error("scheduled_workday_scan_failed", error=str(e)[:140])


async def job_jobdiva_scan() -> None:
    """Headless-browser discovery of JobDiva staffing firms (Diverse Lynx, Net2Source, Russell Tobin,
    Mitchell Martin) — the C2C/bench vendors that live on JobDiva's JS-gated portals (no HTTP API).
    Central to CONTRACT mode. Fragile (breaks on JobDiva UI change, browser per scan). Haiku scoring."""
    try:
        from core.state_store import load_state
        from agents.discovery.jobdiva_scanner import run
        state = load_state()
        _, summary = await run(state, limit=400, tailor=False, persist=True)
        await _sync_db()
        logger.info("scheduled_jobdiva_scan_done", **summary)
        return summary
    except Exception as e:
        logger.error("scheduled_jobdiva_scan_failed", error=str(e)[:140])


async def job_log_rotate() -> None:
    """Keep the scheduler's stdout log bounded. launchd appends to data/scheduler.out.log forever —
    it grew to 184MB and made /pipeline/status (which reads its tail) take ~20s, blanking the UI.
    When it passes the cap, keep the last ~4000 lines and truncate in place. Safe because launchd
    opens the log O_APPEND, so its next write resumes at the new (smaller) EOF."""
    CAP = 20 * 1024 * 1024   # 20 MB
    KEEP = 512 * 1024        # keep the last 512KB (~4000 log lines) so recent activity survives
    try:
        p = Path("data/scheduler.out.log")
        if not p.exists() or p.stat().st_size < CAP:
            return
        with p.open("rb") as f:
            f.seek(-KEEP, 2)
            tail = f.read()
        tail = b"\n".join(tail.split(b"\n")[1:])   # drop the partial first line
        with p.open("wb") as f:
            f.write(tail)
        logger.info("scheduler_log_rotated", kept_bytes=len(tail))
    except Exception as e:
        logger.warning("scheduler_log_rotate_failed", error=str(e)[:120])


def _pull_remote_candidates() -> int:
    """Union the candidate slugs the CLOUD source-discovery routine has queued into origin's
    data/discovered_sources.json. The cloud routine can WebSearch (queues candidates) but its sandbox
    is egress-blocked from the ATS board APIs, so it can never PROBE them — this local machine can.
    Read-only git (fetch + show): no working-tree change, no merge/conflict risk. Returns #added."""
    import subprocess
    from agents.discovery.source_discovery import add_discovered
    root = str(Path(__file__).parent.parent)
    try:
        branch = subprocess.run(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, timeout=30).stdout.strip() or "HEAD"
        subprocess.run(["git", "-C", root, "fetch", "--quiet", "origin", branch],
                       capture_output=True, text=True, timeout=90)
        blob = subprocess.run(["git", "-C", root, "show", f"origin/{branch}:data/discovered_sources.json"],
                              capture_output=True, text=True, timeout=30)
        if blob.returncode != 0 or not blob.stdout.strip():
            return 0
        remote = json.loads(blob.stdout)
        return add_discovered(remote.get("slugs", []))
    except Exception as e:
        logger.warning("pull_remote_candidates_failed", error=str(e)[:120])
        return 0


async def job_source_ingest() -> None:
    """UNBLOCK the cloud discovery routine: pull the candidate slugs it queued to origin, then PROBE
    them here (local egress reaches greenhouse/lever/ashby/smartrecruiters APIs the cloud sandbox 403s)
    and promote the ones with a live US contract role in-lane into the scan universe. Idempotent."""
    try:
        from agents.discovery.source_discovery import discover_and_ingest
        pulled = _pull_remote_candidates()
        r = await discover_and_ingest()
        r["pulled_candidates"] = pulled
        if r.get("added"):
            await _sync_db()
        logger.info("scheduled_source_ingest_done", **r)
        return r
    except Exception as e:
        logger.error("scheduled_source_ingest_failed", error=str(e)[:140])


async def job_pretailor() -> None:
    """Batch PRE-TAILOR the top un-tailored W2/contract roles on the CHEAP provider (DeepSeek) so
    both lanes have résumés ready ahead of time: the hands-off W2 auto-apply lane can actually fire,
    and contract roles are one-click-ready. Bounded per run (PRETAILOR_BATCH, default 15) to keep
    spend small — DeepSeek runs ~$0.008/résumé (~30x cheaper than Opus)."""
    try:
        from agents.application.on_demand_tailor import pretailor_batch
        n = int(os.getenv("PRETAILOR_BATCH", "15"))
        r = await pretailor_batch(limit=n, provider="deepseek")
        await _sync_db()
        logger.info("scheduled_pretailor_done", **r)
        return r
    except Exception as e:
        logger.error("scheduled_pretailor_failed", error=str(e))


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
        # Dedicated conservative cap for the HANDS-OFF lane (separate from the manual apply budget)
        # — a brand-new irreversible sender should start small; raise AUTO_SUBMIT_MAX_PER_DAY once trusted.
        # PER CALENDAR DAY, not per run: `limit` bounds a single select() call, so with the scheduler
        # firing this job several times a day the raw cap could send N×5. Subtract what already went out
        # today (any submit path — auto OR manual) so the lane never pushes the daily total past the cap.
        _auto_cap = get_settings().auto_submit_max_per_day
        _today = datetime.now(timezone.utc).date().isoformat()   # UTC day, matches stored submitted_at
        _sent_today = sum(
            1 for a in state.get("applications", [])
            if a.get("status") == "submitted" and a.get("submitted_at")
            and str(a.get("submitted_at"))[:10] == _today
        )
        _remaining = max(0, _auto_cap - _sent_today)
        if _remaining <= 0:
            logger.info("auto_submit_daily_cap_reached", cap=_auto_cap, sent_today=_sent_today)
            return
        sel = select_auto_submit(state, profile, _remaining)
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
            # Defensive re-finalize right before the IRREVERSIBLE send — guarantees no résumé ever
            # goes out un-scrubbed (7+ years floor, no fabrication/AI-tells/company-pitch), even a
            # stale artifact tailored by older code. Cheap deterministic pass; skip on any error.
            try:
                from core.text_scrub import finalize_resume_md
                rp = app.get("tailored_resume_path") or ""
                fp = Path(rp) if os.path.isabs(rp) else Path(rp)
                if rp and fp.exists():
                    _md, _fx = finalize_resume_md(fp.read_text(encoding="utf-8"),
                                                  company=job.get("company_name", ""))
                    if any(_fx.values()):
                        fp.write_text(_md, encoding="utf-8")
                        logger.info("auto_submit_prefinalize_scrubbed", company=job.get("company_name"), **_fx)
            except Exception as e:
                logger.warning("auto_submit_prefinalize_failed", error=str(e)[:120])
            r = await submit_application(app, job, profile, llm, headless=True, auto_safe=True)
            if app["id"] in by_id:
                state["applications"][by_id[app["id"]]] = r
            n += 1
            _save_state(state)
        await _sync_db()
        logger.info("scheduled_auto_submit_done", submitted=n, queued=len(sel["queue"]))
    except Exception as e:
        logger.error("scheduled_auto_submit_failed", error=str(e))


async def job_dead_link_sweep() -> None:
    """Prune postings that have CLOSED since they were queued (Greenhouse/Lever/Ashby jobs 404 the
    moment they're filled — ~30-40/day here). Definitive-dead-only (ATS API 404/410), protects
    already-applied apps, reversible (status→skipped). Keeps the review-queue count trustworthy."""
    try:
        from agents.application.queue_sweeper import sweep_dead_queue
        r = await sweep_dead_queue(apply=True)
        logger.info("scheduled_dead_link_sweep_done", **r)
    except Exception as e:
        logger.error("scheduled_dead_link_sweep_failed", error=str(e)[:140])


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
_ACTIVE_JOBS = {"linkedin_health", "linkedin_scanner", "board_scanner", "phenom_scanner",
                "kforce_scanner", "insightglobal_scanner", "workday_scanner", "apply_pipeline",
                "referral_prep", "pretailor", "auto_submit", "email_monitor", "digest",
                "dead_link_sweep", "log_rotate", "source_ingest", "gov_scanner",
                "dice_scanner"}

# id → (func, base_interval_seconds, display_name, misfire_grace_time)
_JOB_SPECS = {
    "linkedin_health": (job_health_check,    1800,  "LinkedIn Health Check",                  120),
    "linkedin_scanner": (job_linkedin_scan,   7200, "LinkedIn Job Scanner (2h, account-safe)", 300),
    "contact_finder":  (job_contact_finder,  21600, "LinkedIn Contact Finder",                300),
    "outreach_runner": (job_outreach_runner, 7200,  "LinkedIn Outreach Runner",               120),
    "board_scanner":   (job_board_scan,      1800,  "Board Scanner (off-LinkedIn discovery, 30m)", 600),
    "teksystems_scanner": (job_teksystems_scan, 21600, "TEKsystems Scanner (single-host, superseded by phenom_scanner)", 600),
    "phenom_scanner":  (job_phenom_scan,     10800, "Phenom Staffing Scanner (TEKsystems+Aston Carter+Actalent, 3h)", 600),
    "kforce_scanner":  (job_kforce_scan,     10800, "Kforce Scanner (Azure Search API, 3h)",   600),
    "insightglobal_scanner": (job_insight_global_scan, 10800, "Insight Global Scanner (Jibe API, 3h)", 600),
    "gov_scanner":     (job_gov_scan,        14400, "State/Local Gov C2C Scanner (staff-aug vendor boards, 4h)", 600),
    "dice_scanner":    (job_dice_scan,       14400, "Dice C2C Scanner (contract/third-party, full JD, 4h)", 900),
    "workday_scanner": (job_workday_scan,    43200, "Workday IT-Services Scanner (NTT/Genpact/Accenture cxs, 12h)", 900),
    "jobdiva_scanner": (job_jobdiva_scan,    21600, "JobDiva C2C/Bench Scanner (headless, 6h)", 900),
    "apply_pipeline":  (job_apply_pipeline,  1800,  "Apply Pipeline (score+park cheap, 30m)",   600),
    "referral_prep":   (job_referral_prep,   7200,  "Referral Prep (high-fit warm-intro kits, 2h)", 300),
    "pretailor":       (job_pretailor,       1800,  "Pre-Tailor W2/contract on DeepSeek (cheap, 30m — keeps pace w/ discovery)", 300),
    "auto_submit":     (job_auto_submit,     43200, "Auto-Submit Safe Apps (opt-in)",         600),
    "email_monitor":   (job_email_monitor,   3600,  "Gmail Email Monitor (1h)",               120),
    "digest":          (job_digest,          86400, "Daily Health + Activity Digest",          600),
    "dead_link_sweep": (job_dead_link_sweep, 21600, "Dead-Link Queue Sweep (prune closed postings, 6h)", 600),
    "log_rotate":      (job_log_rotate,      3600,  "Scheduler Log Rotate (cap stdout log at 20MB, 1h)", 60),
    "source_ingest":   (job_source_ingest,   14400, "Source Ingest (probe cloud-queued candidates, 4h)", 300),
}


# CONTRACT MODE job set — full-time discovery (product-co boards + general LinkedIn scan) is PAUSED;
# every vendor/staffing/consulting/implementation-partner scanner runs, and the intake gate keeps only
# C2C/C2H roles. board_scanner stays ON: the intake filter drops its product-co W2 hits but KEEPS the
# staffing firms that live on board APIs (Vaco/Mastech/SGS/Softworld/…). LinkedIn full-time scan +
# referral_prep (a full-time lever) are dropped. jobdiva_scanner (C2C bench firms) is central here.
_CONTRACT_JOBS = {"linkedin_health", "board_scanner", "phenom_scanner", "kforce_scanner",
                  "insightglobal_scanner", "workday_scanner", "jobdiva_scanner", "apply_pipeline",
                  "pretailor", "email_monitor", "digest", "dead_link_sweep", "log_rotate",
                  "source_ingest", "gov_scanner", "dice_scanner"}


def _active_jobs() -> set:
    """The live job set for the current PIPELINE_MODE — contract set pauses full-time discovery."""
    try:
        from core.config import get_settings
        if (get_settings().pipeline_mode or "fulltime").lower() == "contract":
            return _CONTRACT_JOBS
    except Exception:
        pass
    return _ACTIVE_JOBS


# Count keys a job's return-dict may carry, mapped onto the PipelineRun columns the Activity page
# renders. Best-effort: a job that returns None still records status + timing (0 counts).
_SCAN_KEYS   = ("new", "intake", "intaken", "discovered", "scanned", "ingested", "added", "parked")
_APPLY_KEYS  = ("submitted", "applied", "tailored", "sent")
_ATTEMPT_KEYS = ("attempted", "picked", "examining", "processed", "total", "leads")


def _pick_count(d: dict, keys) -> int:
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _record_run(job_id: str, func):
    """Wrap a scheduled job so every execution is logged to the `pipeline_runs` table — the same
    activity feed the UI's Activity page reads. Gives the CONTRACT lane (and every scheduled job)
    the visibility the manual full-time run never actually had. Never lets logging break the job."""
    from functools import wraps

    @wraps(func)
    async def _wrapped():
        from db.session import AsyncSessionLocal
        from db.models import PipelineRun
        from core.config import get_settings
        mode = (get_settings().pipeline_mode or "fulltime").lower()
        run_id = None
        try:
            async with AsyncSessionLocal() as s:
                run = PipelineRun(trigger=f"{mode}:{job_id}", status="running")
                s.add(run)
                await s.commit()
                run_id = run.id
        except Exception as e:  # recording must never block the actual work
            logger.warning("run_record_start_failed", job=job_id, error=str(e)[:120])

        counts, err = {}, None
        try:
            result = await func()
            if isinstance(result, dict):
                counts = result
        except Exception as e:
            err = str(e)[:500]
            logger.error("scheduled_job_failed", job=job_id, error=err)

        if run_id is not None:
            try:
                async with AsyncSessionLocal() as s:
                    run = await s.get(PipelineRun, run_id)
                    if run is not None:
                        run.status = "failed" if err else "completed"
                        run.finished_at = datetime.now(timezone.utc)
                        run.error = err
                        run.scan_new_jobs = _pick_count(counts, _SCAN_KEYS)
                        run.jobs_submitted = _pick_count(counts, _APPLY_KEYS)
                        run.jobs_attempted = _pick_count(counts, _ATTEMPT_KEYS)
                        run.jobs_skipped = _pick_count(counts, ("skipped",))
                        run.jobs_failed = _pick_count(counts, ("errors", "failed"))
                        await s.commit()
            except Exception as e:
                logger.warning("run_record_finish_failed", job=job_id, error=str(e)[:120])
    return _wrapped


def build_scheduler() -> AsyncIOScheduler:
    """Build and return a configured APScheduler (not yet started).

    Only jobs in the active set (mode-dependent — see `_active_jobs`) are registered; the rest are
    slept. In CONTRACT mode the full-time discovery jobs are paused (reversible via PIPELINE_MODE).
    Every registered job is wrapped by `_record_run` so its execution shows on the Activity feed.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")
    active = _active_jobs()
    _mode = "contract" if active is _CONTRACT_JOBS else "fulltime"
    logger.info("scheduler_mode", mode=_mode, active=sorted(active))

    for job_id, (func, interval, name, grace) in _JOB_SPECS.items():
        if job_id not in active:
            logger.info("scheduler_job_slept", job=job_id)
            continue
        scheduler.add_job(
            _record_run(job_id, func),
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
