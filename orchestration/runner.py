"""
CLI entry point for AutonomousHire.

Usage:
    python -m orchestration.runner              # full pipeline run
    python -m orchestration.runner --dry-run    # discovery + scoring only, no applications
    python -m orchestration.runner --resume-from review  # skip to a specific stage
    python -m orchestration.runner --approve <id>        # approve a pending gate
    python -m orchestration.runner --reject  <id>        # reject a pending gate
    python -m orchestration.runner --status              # show pending approvals + stats
"""

import argparse
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.state import JobSearchState, Company
from core.logger import get_logger
from core.checkpoints import resolve_approval, get_pending_approvals

logger = get_logger("runner")
# State I/O delegates to core.state_store (locked + atomic + merge) — no module-level path needed.


# ── State persistence ──────────────────────────────────────────────────────────

def load_state() -> JobSearchState | None:
    # Delegate to the shared store so CLI runs read consistently (atomic reader view).
    from core.state_store import load_state as _ls, StateCorruptError
    try:
        return _ls()
    except StateCorruptError:
        return None


def save_state(state: JobSearchState) -> None:
    # CRITICAL: route through state_store so hand-run CLI commands DON'T clobber the live
    # scheduler — locked + atomic + union-merge with whatever's on disk. (Was a raw write_text
    # that voided all of that and silently lost concurrent appends.)
    from core.state_store import save_state as _ss
    _ss(state)


def fresh_state(companies: list[dict] | None = None) -> JobSearchState:
    return {
        "companies":            companies or [],
        "job_listings":         [],
        "fit_scores":           [],
        "applications":         [],
        "pending_approvals":    [],
        "recruiter_contacts":   [],
        "linkedin_contacts":    [],
        "email_events":         [],
        "meetings_scheduled":   [],
        "weekly_quality_score": 0.0,
        "weekly_quantity_count": 0,
        "response_rate":        0.0,
        "interview_rate":       0.0,
        "last_run_at":          None,
        "errors":               [],
        "agent_statuses":       {},
        "scanned_company_ids":  [],
    }


# ── Status display ─────────────────────────────────────────────────────────────

def show_status(state: JobSearchState | None) -> None:
    pending = get_pending_approvals()

    print("\n" + "═" * 60)
    print("  AutonomousHire — Status")
    print("═" * 60)

    if pending:
        print(f"\n⏳ PENDING APPROVALS ({len(pending)}):")
        for p in pending:
            payload = p.get("payload", {})
            print(f"   [{p['id'][:8]}] {payload.get('company', '?')} — {payload.get('role', '?')}")
            print(f"           Score: {payload.get('quality_score', 0)}/100  ATS: {payload.get('ats', '?')}")
            print(f"           Expires: {p.get('expires_at', 'never')}")
            print(f"           Screenshot: {payload.get('screenshot_path', 'N/A')}")
    else:
        print("\n✓ No pending approvals")

    if state:
        apps = state.get("applications", [])
        submitted = [a for a in apps if a.get("status") == "submitted"]
        blocked   = [a for a in apps if a.get("status") == "blocked"]
        pending_r = [a for a in apps if a.get("status") == "pending_review"]
        print(f"\n📊 PIPELINE STATE:")
        print(f"   Companies:    {len(state.get('companies', []))}")
        print(f"   Jobs found:   {len(state.get('job_listings', []))}")
        print(f"   Fit scores:   {len(state.get('fit_scores', []))}")
        print(f"   Applications: {len(apps)} total")
        print(f"     ✓ Submitted:       {len(submitted)}")
        print(f"     ⏳ Pending review: {len(pending_r)}")
        print(f"     ✗ Blocked:        {len(blocked)}")
        if state.get("last_run_at"):
            print(f"\n   Last run: {state['last_run_at']}")
        if state.get("errors"):
            print(f"\n⚠️  ERRORS ({len(state['errors'])}):")
            for e in state["errors"][-3:]:
                print(f"   [{e.get('node', '?')}] {e.get('error', '')[:80]}")
    print()


# ── Main run ───────────────────────────────────────────────────────────────────

async def run_pipeline(
    resume_from: str | None = None,
    dry_run: bool = False,
    submit_only: bool = False,
    assisted: bool = False,
) -> JobSearchState:
    from orchestration.graph import compile_graph

    # Load or create state
    state = load_state() or fresh_state()

    # In dry_run mode: remove submit node by running only through review
    graph = compile_graph()

    logger.info(
        "pipeline_start",
        dry_run=dry_run,
        resume_from=resume_from,
        companies=len(state.get("companies", [])),
    )

    state["last_run_at"] = datetime.now(timezone.utc).isoformat()

    # If resuming from a specific stage, skip earlier completed stages
    # by injecting the relevant state from saved file
    if resume_from:
        print(f"Resuming from stage: {resume_from}")
        # The graph will naturally skip stages where data already exists
        # because each node's `run()` checks for existing work

    try:
        if submit_only:
            # Skip discovery/scoring/tailoring — only submit gate-approved apps.
            # Headless (form_filler defaults headless=True). A lockfile serializes
            # runs so concurrent triggers (e.g. several UI approvals) can't double-
            # submit; a drain loop catches approvals that arrive mid-run.
            import os, time
            from agents.application.form_filler import run as submit_apps
            from db.migrate_from_json import sync_to_db_async

            lock  = Path("/tmp/autonomoushire_submit.lock")
            rerun = Path("/tmp/autonomoushire_submit.rerun")
            if lock.exists():
                age = time.time() - lock.stat().st_mtime if lock.exists() else 999
                if age < 900:           # a fresh run is active — ask it to loop again
                    rerun.touch()       # so an approval landing now isn't missed
                    print("   Another submit run is in progress — queued a rerun.")
                    return state
                lock.unlink(missing_ok=True)   # stale lock, reclaim
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode()); os.close(fd)
            except FileExistsError:
                rerun.touch()
                print("   Another submit run just started — queued a rerun.")
                return state

            try:
                print("\n📬  Submitting approved applications (headless)...")
                outer = 0
                while True:
                    rerun.unlink(missing_ok=True)   # claim any pending rerun request
                    for _ in range(20):             # drain; bounded backstop
                        before = sum(1 for a in state.get("applications", [])
                                     if a.get("status") == "pending_review")
                        state = await submit_apps(state, approved_only=True, assisted=assisted)
                        save_state(state)
                        after = sum(1 for a in state.get("applications", [])
                                    if a.get("status") == "pending_review")
                        if after >= before:         # nothing left the queue → done
                            break
                        state = load_state() or state   # pick up approvals that just landed
                    outer += 1
                    # If a trigger arrived mid-run it left a rerun marker — service it.
                    if not rerun.exists() or outer >= 20:
                        break
                    state = load_state() or state
            finally:
                lock.unlink(missing_ok=True)

            await sync_to_db_async()

        elif dry_run:
            # Run only discovery + scoring, stop before form filling
            from agents.discovery.company_finder import run_all as find_companies
            from agents.discovery.job_scanner import run as scan_jobs
            from agents.discovery.fit_analyst import run as score_fit, get_passing_jobs

            print("🔍  Discovering companies...")
            state = await find_companies(state)
            save_state(state)  # checkpoint — dashboard shows live
            print(f"   Found {len(state.get('companies', []))} companies")

            print("📋  Scanning jobs...")
            state = await scan_jobs(state)
            save_state(state)
            print(f"   Found {len(state.get('job_listings', []))} jobs")

            print("🎯  Scoring fit...")
            state = await score_fit(state)
            save_state(state)
            passing = get_passing_jobs(state)
            print(f"   {len(passing)} jobs passed threshold")

            for job, fs in passing[:10]:
                print(f"   [{fs['score']:3d}] {job['title']} @ {job['company_name']}")
        else:
            # Full pipeline — stream node-by-node so state is checkpointed after each stage
            async for chunk in graph.astream(state):
                for node_name, node_state in chunk.items():
                    if node_name == "__end__":
                        continue
                    state = node_state
                    save_state(state)  # checkpoint after every node
                    companies = len(state.get("companies", []))
                    jobs      = len(state.get("job_listings", []))
                    apps      = len(state.get("applications", []))
                    print(f"   ✓ {node_name}: {companies} companies, {jobs} jobs, {apps} applications")

    except Exception as e:
        logger.error("pipeline_failed", error=str(e))
        state["errors"] = state.get("errors", []) + [{"node": "runner", "error": str(e)}]

    # Learning loop — ghosting sweep + conversion analytics → learnings + insights report.
    try:
        from agents.learning.feedback_loop import run as learn_run
        state = await learn_run(state)
    except Exception as e:
        logger.warning("learning_loop_failed", error=str(e))

    save_state(state)

    # Refresh the SQL DB the UI reads from (idempotent, decision-preserving).
    from db.migrate_from_json import sync_to_db_async
    counts = await sync_to_db_async()
    if counts:
        logger.info("db_synced", **{k: counts[k] for k in ("apps", "decisions_preserved")})

    return state


# ── Outcome feedback ──────────────────────────────────────────────────────────

_FEEDBACK_LOG = Path("data/outcome_feedback.json")


def _record_feedback(company: str, role: str, outcome: str, state: JobSearchState | None) -> None:
    """
    Record an interview/rejection outcome and save a learning to memory.
    Outcomes feed back into future scoring calibration.
    """
    from datetime import datetime, timezone
    from core.memory import Memory

    record = {
        "company": company,
        "role": role,
        "outcome": outcome,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    # Append to feedback log
    existing = []
    if _FEEDBACK_LOG.exists():
        try:
            existing = json.loads(_FEEDBACK_LOG.read_text())
        except Exception:
            pass
    existing.append(record)
    _FEEDBACK_LOG.write_text(json.dumps(existing, indent=2))

    # Save to memory so future scoring can reference it
    memory = Memory.get()
    sentiment = {
        "interview": "positive — got an interview",
        "offer":     "very positive — received offer",
        "rejected":  "negative — rejected after application",
        "ghosted":   "neutral — no response",
    }[outcome]
    memory.upsert(
        "learnings",
        f"outcome_{company.lower().replace(' ', '_')}_{outcome}",
        f"Applied to {role} at {company}: {sentiment}",
        {"company": company, "role": role, "outcome": outcome},
    )

    print(f"✓ Recorded: {outcome} from {company} ({role})")
    print(f"  Saved to {_FEEDBACK_LOG} and memory")

    # Show updated outcome summary
    outcomes = [r["outcome"] for r in existing]
    from collections import Counter
    counts = Counter(outcomes)
    total = len(outcomes)
    print(f"\n  Outcome history ({total} total):")
    for o, c in counts.most_common():
        print(f"    {o}: {c}  ({c/total*100:.0f}%)")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AutonomousHire — AI-powered job search pipeline"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Run discovery + scoring only, no applications")
    parser.add_argument("--submit-only", action="store_true",
                        help="Skip discovery/tailoring; submit only already-reviewed pending_review apps")
    parser.add_argument("--fill-park", action="store_true",
                        help="Fill + screenshot + PARK all reviewed (pending_review) apps for UI review; no submit")
    parser.add_argument("--assisted", action="store_true",
                        help="With --submit-only: open a VISIBLE browser, pre-fill, and let you review + solve CAPTCHA + submit")
    parser.add_argument("--auto-apply", action="store_true",
                        help="Auto-submit the safe (gate-passed, CAPTCHA-free) reviewed apps; queue the rest. "
                             "DRY-RUN unless AUTONOMOUSHIRE_AUTO_SUBMIT=1.")
    parser.add_argument("--health", action="store_true", help="Run system health checks (credits/Gmail/LinkedIn/sync)")
    parser.add_argument("--digest", action="store_true", help="Print + write the daily digest")
    parser.add_argument("--pause-auto", action="store_true", help="Kill-switch: stop all auto-submission")
    parser.add_argument("--resume-auto", action="store_true", help="Lift the auto-submission kill-switch")
    parser.add_argument("--auto-approve", action="store_true",
                        help="Auto-approve all submission gates without waiting for human input")
    parser.add_argument("--resume-from", metavar="STAGE",
                        help="Resume from a specific stage (discover/scan/score/tailor/review/submit)")
    parser.add_argument("--approve", metavar="ID",
                        help="Approve a pending gate by ID (or 'all')")
    parser.add_argument("--reject", metavar="ID",
                        help="Reject a pending gate by ID")
    parser.add_argument("--status", action="store_true",
                        help="Show pending approvals and pipeline stats")
    parser.add_argument("--feedback", nargs=3, metavar=("COMPANY", "ROLE", "OUTCOME"),
                        help="Record interview/rejection outcome. OUTCOME: interview|offer|rejected|ghosted")
    parser.add_argument("--li-login", action="store_true",
                        help="Open LinkedIn in browser for manual login / CAPTCHA solve, then save the session")
    parser.add_argument("--linkedin", action="store_true",
                        help="Run LinkedIn scan + contact finder + outreach (requires logged-in Chrome profile)")
    parser.add_argument("--li-debug-job", metavar="URL",
                        help="Debug a single LinkedIn job URL: show apply button details without submitting")
    parser.add_argument("--li-apply-one", metavar="URL",
                        help="Run the full apply pipeline on a single LinkedIn job URL (score→tailor→review→submit)")
    parser.add_argument("--email", action="store_true",
                        help="Run email monitor once — checks Gmail for interview invites, rejections, offers")
    parser.add_argument("--scheduler", action="store_true",
                        help="Start the background LinkedIn scheduler (runs continuously)")
    args = parser.parse_args()

    state = load_state()

    if args.status:
        show_status(state)
        return

    if args.approve:
        pending = get_pending_approvals()
        if args.approve == "all":
            for p in pending:
                resolve_approval(p["id"], "approved")
                payload = p.get("payload", {})
                print(f"✓ Approved: {payload.get('company', '?')} — {payload.get('role', '?')}")
        else:
            if resolve_approval(args.approve, "approved"):
                print(f"✓ Approved: {args.approve}")
            else:
                print(f"✗ Approval ID not found: {args.approve}")
        return

    if args.reject:
        if resolve_approval(args.reject, "rejected"):
            print(f"✓ Rejected: {args.reject}")
        else:
            print(f"✗ Approval ID not found: {args.reject}")
        return

    if args.feedback:
        company, role, outcome = args.feedback
        valid = {"interview", "offer", "rejected", "ghosted"}
        if outcome not in valid:
            print(f"✗ Invalid outcome '{outcome}'. Must be one of: {', '.join(sorted(valid))}")
            return
        _record_feedback(company, role, outcome, state)
        return

    if args.email:
        async def _run_email():
            from agents.monitoring.email_monitor import run as email_run
            from agents.learning.feedback_loop import run as learn_run
            s = load_state() or fresh_state()
            print("📬  Checking Gmail for job-related emails...")
            updated = await email_run(s)
            new_events = len(updated.get("email_events", [])) - len(s.get("email_events", []))
            print(f"   Processed {new_events} new email(s)")
            for ev in updated.get("email_events", [])[-new_events:] if new_events else []:
                print(f"   [{ev['category']}] {ev.get('company_name', '?')} — {ev['summary']}")
            # Capture → learn: turn outcomes into learnings + an insights report.
            print("🧠  Updating learning loop...")
            updated = await learn_run(updated)
            save_state(updated)
            from db.migrate_from_json import sync_to_db_async
            await sync_to_db_async()
            print(f"   Response {updated.get('response_rate', 0):.0%} · "
                  f"interview {updated.get('interview_rate', 0):.0%} → data/learning_insights.md")

        asyncio.run(_run_email())
        return

    if args.fill_park:
        async def _run_fill_park():
            from agents.application.form_filler import run as submit_apps
            from db.migrate_from_json import sync_to_db_async
            s = load_state() or fresh_state()
            print("\n🖊️   Filling + parking reviewed applications for review (no submit)...")
            s = await submit_apps(s, approved_only=False)
            save_state(s)
            await sync_to_db_async()
            parked = sum(1 for a in s.get("applications", []) if a.get("screenshot_path"))
            print(f"   {parked} applications now have a filled screenshot in the parking lot.")
        asyncio.run(_run_fill_park())
        return

    if args.pause_auto or args.resume_auto:
        kill = Path("data/auto_apply.disabled")
        if args.pause_auto:
            kill.parent.mkdir(parents=True, exist_ok=True); kill.touch()
            print("⏸  Auto-submission PAUSED (kill-switch on). Lift with --resume-auto.")
        else:
            kill.unlink(missing_ok=True)
            print("▶  Auto-submission kill-switch lifted.")
        return

    if args.health:
        from agents.monitoring.health import system_health
        h = system_health(probe_llm=True)
        print(f"\n🩺  System health: {'✅ ALL OK' if h['ok'] else '⚠️  ISSUES'}")
        for c in h["checks"]:
            icon = "✅" if c["status"] == "ok" else "❌"
            print(f"  {icon} {c['name']:18} {c['detail']}")
        return

    if args.digest:
        from agents.monitoring.health import build_digest
        d = build_digest(write=True)
        p = d["pipeline"]
        print(f"\n📊  Daily Digest ({d['date']})")
        print(f"  Queued for review: {p['queued_for_review']}  ·  Awaiting confirm: {p['awaiting_confirmation']}")
        print(f"  Submitted: {p['submitted']}  ·  Interviews: {p['interview']}  ·  Offers: {p['offer']}  ·  Rejected: {p['rejected']}")
        print(f"  Sent today: {d['today']['sent']}  ·  Confirmed today: {d['today']['confirmed']}")
        print(f"  Response {d['response_rate']:.0%} · Interview {d['interview_rate']:.0%}")
        print(f"  Health: {'OK' if d['health_ok'] else 'ISSUES'} → {d['health']}")
        print(f"  (written to data/daily_digest.md)")
        return

    if args.auto_apply:
        async def _run_auto_apply():
            import yaml
            from collections import Counter
            from agents.application.auto_apply import select_auto_submit, auto_submit_enabled
            from agents.application.form_filler import submit_application
            from db.migrate_from_json import sync_to_db_async
            from core.config import get_settings
            from core.llm import get_llm
            s = load_state() or fresh_state()
            profile = yaml.safe_load(Path("data/user_profile.yaml").read_text())
            limit = get_settings().apply_max_per_day
            sel = select_auto_submit(s, profile, limit)
            print(f"\n🤖  Auto-apply: {len(sel['submit'])} eligible to auto-submit, "
                  f"{len(sel['queue'])} queued for your review (gate-failed/CAPTCHA/limit)")
            for job in sel["submit"][:15]:
                print(f"   ✅ AUTO  {str(job.get('company_name'))[:22]:22} {str(job.get('title'))[:38]}")
            for job, reasons in sel["queue"][:10]:
                print(f"   ✋ QUEUE {str(job.get('company_name'))[:22]:22} {str(job.get('title'))[:30]} — {reasons[0]}")
            if not auto_submit_enabled():
                print("\n[DRY RUN] AUTONOMOUSHIRE_AUTO_SUBMIT not enabled (or kill-switch on) — nothing submitted.")
                print("  Enable with: export AUTONOMOUSHIRE_AUTO_SUBMIT=1   (remove data/auto_apply.disabled to un-kill)")
                return
            llm = get_llm("form_filler", model="claude-haiku-4-5")
            by_id = {a["id"]: i for i, a in enumerate(s["applications"])}
            apps_by_job = {a["job_id"]: a for a in s["applications"]}
            results: Counter = Counter()
            print("\n📬  Submitting eligible apps (headless, CAPTCHA-safe)…")
            for job in sel["submit"]:
                app = apps_by_job.get(job["id"])
                if not app:
                    continue
                r = await submit_application(app, job, profile, llm, headless=True, auto_safe=True)
                if app["id"] in by_id:
                    s["applications"][by_id[app["id"]]] = r
                results[r.get("status")] += 1
                print(f"   {r.get('status'):20} {str(job.get('company_name'))[:20]:20} {str(job.get('title'))[:30]}")
                save_state(s)
            await sync_to_db_async()
            print(f"\nDONE: {dict(results)}")
        asyncio.run(_run_auto_apply())
        return

    if args.scheduler:
        from orchestration.scheduler import run_scheduler
        print("\n🔗  Starting LinkedIn scheduler (Ctrl+C to stop)...")
        asyncio.run(run_scheduler())
        return

    if args.li_login:
        async def _run_li_login():
            from tools.linkedin_browser import LinkedInBrowser
            from pathlib import Path

            # Clear any existing auto-pause so the next run isn't blocked
            pause_file = Path("data/linkedin_pause.json")
            if pause_file.exists():
                pause_file.unlink()
                print("   Cleared auto-pause lock.")

            print("\n🌐  Opening LinkedIn in your browser.")
            print("   Enter your email + password, solve any CAPTCHA, then reach your feed.")
            print("   This window will close automatically once login is confirmed.\n")

            async with LinkedInBrowser() as browser:
                page = await browser.new_page()

                # Navigate to linkedin.com with a lenient wait — the redirect loop
                # only happens on /login with domcontentloaded; using "commit" (first
                # byte received) and falling back to a bare open() avoids the error.
                try:
                    await page.goto("https://www.linkedin.com", wait_until="commit", timeout=15000)
                except Exception:
                    pass  # browser is open — user can type the URL manually

                await asyncio.sleep(1)
                # If already on feed / not on login page, great. Otherwise try /login.
                if "feed" not in page.url and "login" not in page.url:
                    try:
                        await page.goto("https://www.linkedin.com/login", wait_until="commit", timeout=15000)
                    except Exception:
                        pass

                print("   Browser is open — sign in now.\n")
                try:
                    # Poll for li_at cookie — up to 10 minutes
                    for _ in range(600):
                        await asyncio.sleep(1)
                        cookies = await browser._context.cookies()
                        li_at = next((c for c in cookies if c["name"] == "li_at"), None)
                        if li_at:
                            print("   ✓  li_at session cookie detected — you are logged in!")
                            print("   Session saved. Closing browser in 3 seconds...")
                            await asyncio.sleep(3)
                            break
                    else:
                        print("   ⚠  Timed out waiting for login (10 min). Try again.")
                except (KeyboardInterrupt, asyncio.CancelledError):
                    cookies = await browser._context.cookies()
                    li_at = next((c for c in cookies if c["name"] == "li_at"), None)
                    if li_at:
                        print("\n   ✓  Session saved (li_at present).")
                    else:
                        print("\n   ⚠  Closed before login completed — li_at not found.")

        asyncio.run(_run_li_login())
        return

    if args.li_debug_job:
        async def _debug_job():
            from tools.linkedin_browser import LinkedInBrowser
            from tools.humanize import scroll_down_human

            url = args.li_debug_job
            print(f"\n🔍  Debug: inspecting apply button for {url}")

            async with LinkedInBrowser() as browser:
                page = await browser.new_page()
                ok, status = await browser.session_status(page)
                if not ok:
                    print(f"⚠️  Not logged in ({status})")
                    await page.close()
                    return
                await page.close()

                page = await browser.new_page()
                await browser.goto(page, url)
                await scroll_down_human(page, 300)

                # Find all apply-related buttons on the page
                btns = await page.query_selector_all(
                    "button.jobs-apply-button, .jobs-s-apply button, "
                    ".jobs-s-apply a, button[aria-label*='Apply'], a[aria-label*='Apply']"
                )
                print(f"\n   Found {len(btns)} apply button(s):")
                for i, b in enumerate(btns):
                    text = (await b.inner_text()).strip()
                    aria = await b.get_attribute("aria-label") or ""
                    cls = await b.get_attribute("class") or ""
                    href = await b.get_attribute("href") or ""
                    print(f"   [{i+1}] text={text!r:30s}  aria={aria!r:50s}")
                    if href:
                        print(f"        href={href[:80]}")

                # Also show the full job card text snippet
                print(f"\n   Current URL: {page.url}")
                await page.close()

        asyncio.run(_debug_job())
        return

    if args.li_apply_one:
        async def _apply_one():
            from agents.application.linkedin_apply_pipeline import process_job
            from tools.linkedin_browser import LinkedInBrowser

            url = args.li_apply_one
            s = load_state() or fresh_state()

            print(f"\n🎯  Applying to: {url}")
            print("   Steps: navigate → read JD → get ATS URL → score → tailor → review → submit\n")

            async with LinkedInBrowser() as browser:
                page = await browser.new_page()
                ok, status = await browser.session_status(page)
                if not ok:
                    print(f"⚠️  Not logged in ({status}) — run --li-login first.")
                    await page.close()
                    return
                await page.close()

                page = await browser.new_page()
                try:
                    app, s = await process_job(browser, page, url, s)
                    save_state(s)
                    if app and app.get("status") == "submitted":
                        print(f"\n✅  Application submitted successfully!")
                        print(f"   Company:  {app.get('company_name')}")
                        print(f"   Role:     {app.get('role_title')}")
                        print(f"   ATS URL:  {app.get('ats_url', '?')}")
                    elif app:
                        print(f"\n⚠️  Application result: {app.get('status', 'unknown')}")
                    else:
                        print(f"\n✗  No application submitted (job skipped — see above for reason)")
                except Exception as e:
                    print(f"\n✗  Pipeline error: {e}")
                    logger.error("li_apply_one_failed", url=url, error=str(e))
                finally:
                    await page.close()

        asyncio.run(_apply_one())
        return

    if args.linkedin:
        async def _run_linkedin():
            from agents.discovery.linkedin_scanner import scan_in_session
            from agents.application.linkedin_apply_pipeline import run as li_apply
            from agents.outreach.contact_finder import run as find_contacts
            from agents.outreach.outreach_runner import run as outreach
            from tools.linkedin_browser import LinkedInBrowser

            s = load_state() or fresh_state()

            # ── Single Chrome session: health-check → scan → apply ───────────
            # Opening Chrome once avoids the double-open race that triggers
            # LinkedIn bot detection and causes the SingletonLock conflict.
            print("🔍  Starting LinkedIn pipeline...")
            try:
                async with LinkedInBrowser() as browser:
                    # Step 1: verify session inside the same browser
                    print("🏥  Checking LinkedIn status...")
                    page = await browser.new_page()
                    ok, status_desc = await browser.session_status(page)
                    await page.close()

                    if not ok:
                        if status_desc == "logged_out":
                            print("⚠️  Not logged in — run:  python -m orchestration.runner --li-login")
                        elif status_desc == "captcha":
                            print("⚠️  LinkedIn is showing a CAPTCHA — run --li-login to solve it.")
                        elif status_desc == "restricted":
                            print("⚠️  LinkedIn account restricted — check your account.")
                        else:
                            print(f"⚠️  LinkedIn status unknown ({status_desc}) — check your browser.")
                        return

                    print(f"   ✓ LinkedIn session active")

                    # Step 2: scan for new non-Easy-Apply jobs
                    print("🔍  Scanning LinkedIn for new jobs...")
                    new_li_urls, s = await scan_in_session(browser, s)
                    save_state(s)
                    print(f"   Found {len(new_li_urls)} new jobs to evaluate")

                    # Step 3: build apply queue — new discoveries + unapplied jobs from state
                    # (When scan is rate-limited, new_li_urls is empty but state still has
                    #  previously discovered jobs that haven't been applied to yet.)
                    import json as _json
                    _applied_log = Path("data/applied_jobs.json")
                    _applied_urls: set[str] = set()
                    if _applied_log.exists():
                        try:
                            for e in _json.loads(_applied_log.read_text()):
                                if e.get("apply_url"):
                                    _applied_urls.add(e["apply_url"])
                        except Exception:
                            pass
                    _applied_job_ids = {
                        a.get("job_id") for a in s.get("applications", [])
                        if a.get("status") in ("submitted", "pending_review", "approved")
                    }
                    queued_li_urls = [
                        j["apply_url"]
                        for j in s.get("job_listings", [])
                        if "linkedin.com/jobs/view/" in j.get("apply_url", "")
                        and j["apply_url"] not in _applied_urls
                        and j.get("id") not in _applied_job_ids
                        and j["apply_url"] not in new_li_urls
                    ]
                    all_li_urls = new_li_urls + queued_li_urls
                    if all_li_urls:
                        print(f"   {len(all_li_urls)} total job(s) to try ({len(new_li_urls)} new, {len(queued_li_urls)} queued)")
                        # Pass up to 40 URLs per run — Easy Apply and junk are skipped in
                        # the pipeline so we need a large batch to ensure real ATS jobs
                        # are reached. The pipeline stops after the first successful submission.
                        batch = all_li_urls[:40]
                        s = await li_apply(browser, batch, s)
                        save_state(s)
                        remaining = len(all_li_urls) - len(batch)
                        if remaining > 0:
                            print(f"   {remaining} more job(s) queued — run --linkedin again to continue.")

            except Exception as e:
                logger.error("linkedin_scan_apply_failed", error=str(e))
                print(f"   ✗ Pipeline error: {e}")

            # ── Contact finder + outreach (each opens its own short session) ─
            print("\n👥  Finding contacts...")
            s = await find_contacts(s)
            save_state(s)
            print(f"   {len(s.get('linkedin_contacts', []))} total contacts queued")

            print("💬  Running outreach cadence...")
            s = await outreach(s)
            save_state(s)
            print("   Outreach done.")

        asyncio.run(_run_linkedin())
        return

    if args.auto_approve:
        os.environ["AUTONOMOUSHIRE_AUTO_APPROVE"] = "1"

    # Run pipeline
    print("\n🚀  AutonomousHire starting...")
    if args.dry_run:
        print("   Mode: DRY RUN (no applications will be submitted)")
    if args.submit_only:
        print("   Mode: SUBMIT ONLY (skipping discovery/tailoring)")
    if args.auto_approve:
        print("   Mode: AUTO-APPROVE (submitting all without manual gates)")

    final_state = asyncio.run(
        run_pipeline(resume_from=args.resume_from, dry_run=args.dry_run,
                     submit_only=args.submit_only, assisted=args.assisted)
    )

    show_status(final_state)


if __name__ == "__main__":
    main()
