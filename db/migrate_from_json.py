"""
Idempotent sync: pipeline_state.json + applied_jobs.json → PostgreSQL.

Safe to re-run any time — upserts jobs/scores/applications by primary key, and
PRESERVES DB-side human decisions (an application already approved/rejected/
submitted/blocked in the DB is never overwritten by a staler JSON status).

Run:
    python -m db.migrate_from_json
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select, func

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from db.models import Application, ApplicationStatus, AtsType, FitScore, JobListing, JobSource
from db.session import create_tables_sync, get_sync_session

# Decisions/outcomes that the JSON sync must never clobber (DB is source of truth
# for these — set by the UI approve/reject or by a real Phase-2 submission).
_DECIDED_STATUSES = {
    ApplicationStatus.approved,
    ApplicationStatus.rejected,
    ApplicationStatus.submitted,
    ApplicationStatus.pending_manual_submit,
    ApplicationStatus.blocked,
}


def _detect_ats(url: str) -> str:
    if not url:
        return AtsType.unknown
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()
    if "greenhouse" in host or "greenhouse" in path or "gh_jid" in url:
        return AtsType.greenhouse
    if "lever.co" in host or "lever" in path:
        return AtsType.lever
    if "workday" in host or "myworkdayjobs" in host:
        return AtsType.workday
    if "rippling" in host:
        return AtsType.rippling
    if "ashbyhq" in host or "ashby" in host:
        return AtsType.ashby
    if "bamboohr" in host:
        return AtsType.bamboohr
    if "icims" in host:
        return AtsType.icims
    if "smartrecruiters" in host:
        return AtsType.smartrecruiters
    if "ycombinator" in host:
        return AtsType.manual
    return AtsType.generic


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _map_status(s: str | None) -> str:
    mapping = {
        "submitted":             ApplicationStatus.submitted,
        # Form sent, waiting on the employer/ATS confirmation email — reuse the
        # existing `submitting` enum value (no Postgres enum migration needed).
        "awaiting_confirmation": ApplicationStatus.submitting,
        "pending_review":        ApplicationStatus.pending_approval,
        "pending_manual_submit": ApplicationStatus.pending_manual_submit,
        "blocked":               ApplicationStatus.blocked,
        "skipped":               ApplicationStatus.skipped,
        "approved":              ApplicationStatus.approved,
        "rejected":              ApplicationStatus.rejected,
    }
    return mapping.get(s or "", ApplicationStatus.queued)


def _safe_id(val: str | None) -> str:
    """Return val if it looks like a UUID, otherwise generate a new one."""
    if val and len(val) >= 8 and "-" in val:
        return val
    return str(uuid.uuid4())


def _clean_source(s: str | None) -> str:
    try:
        return JobSource(s)
    except Exception:
        return JobSource.careers_page


def _should_skip_prune(db_jobs: int, json_jobs: int, db_apps: int, json_apps: int) -> bool:
    """Tripwire: refuse the destructive prune if the incoming JSON is suspiciously smaller than the
    DB (a truncated/partial state). Guards BOTH jobs and (non-decided) applications — a missing apps
    array with intact jobs would otherwise wipe the pending_review queue."""
    jobs_drop = db_jobs >= 50 and json_jobs < db_jobs * 0.5
    apps_drop = db_apps >= 20 and json_apps < db_apps * 0.5
    return jobs_drop or apps_drop


def migrate(quiet: bool = False, force_prune: bool = False) -> dict:
    """force_prune=True bypasses the >50%-drop tripwire for ONE deliberate run. Use only when the
    JSON is verified healthy but the DB has grown so large (stale jobs never pruned) that JSON is
    permanently under 50% of it — a deadlock where the tripwire blocks the very prune that fixes it.
    Decided/submitted rows are still preserved by the prune logic below."""
    create_tables_sync()

    state_path   = _ROOT / "data" / "pipeline_state.json"
    applied_path = _ROOT / "data" / "applied_jobs.json"

    if not state_path.exists():
        if not quiet:
            print("No pipeline_state.json found — nothing to migrate.")
        return {}

    state = json.loads(state_path.read_text())
    applied_log: list[dict] = []
    if applied_path.exists():
        applied_log = json.loads(applied_path.read_text())

    applied_by_url = {e.get("apply_url", ""): e for e in applied_log if e.get("apply_url")}

    raw_jobs  = state.get("job_listings", [])
    raw_apps  = state.get("applications", [])
    raw_scores = state.get("fit_scores", [])

    # Index raw data for lookups
    score_by_job: dict[str, dict] = {s["job_id"]: s for s in raw_scores if s.get("job_id")}
    app_by_job:   dict[str, dict] = {a["job_id"]: a for a in raw_apps   if a.get("job_id")}

    jobs_inserted = 0
    apps_inserted = 0
    scores_inserted = 0
    skipped = 0
    decisions_preserved = 0
    pruned = 0

    with get_sync_session() as session:
        # Snapshot existing rows so the sync upserts by NATURAL KEY (not the JSON id,
        # which may differ from what the DB was seeded with) and never clobbers a
        # human decision. job_id is the unique natural key for applications/scores.
        existing_app_id_by_job: dict[str, str] = {}
        existing_app_status_by_job: dict[str, ApplicationStatus] = {}
        for aid, jid, status in session.execute(
            select(Application.id, Application.job_id, Application.status)
        ).all():
            existing_app_id_by_job[jid] = aid
            existing_app_status_by_job[jid] = status

        existing_score_id_by_job: dict[str, str] = {
            jid: sid
            for sid, jid in session.execute(select(FitScore.id, FitScore.job_id)).all()
        }
        # apply_url is a UNIQUE natural key on job_listings — reuse the existing
        # row id when a URL is already present so merge upserts instead of colliding.
        existing_job_id_by_url: dict[str, str] = {
            row[1]: row[0]
            for row in session.execute(select(JobListing.id, JobListing.apply_url)).all()
            if row[1]
        }

        seen_urls: set[str] = set()
        seen_ids:  set[str] = set()
        seen_score_ids: set[str] = set()
        seen_app_ids:   set[str] = set()
        written_app_job_ids: set[str] = set()   # job_ids that have a live JSON application

        with session.no_autoflush:
            for raw in raw_jobs:
                url = raw.get("apply_url", "").strip()
                if not url or url in seen_urls:
                    skipped += 1
                    continue
                seen_urls.add(url)

                # Reuse the existing row id for a known URL (upsert by natural key);
                # otherwise use the JSON id, disambiguating any in-run collision.
                if url in existing_job_id_by_url:
                    job_id = existing_job_id_by_url[url]
                else:
                    job_id = _safe_id(raw.get("id"))
                    if job_id in seen_ids:
                        job_id = str(uuid.uuid4())
                seen_ids.add(job_id)

                title = re.sub(r"\s+with verification\s*$", "", raw.get("title", ""), flags=re.IGNORECASE).strip()
                title = title.splitlines()[0].strip() if title else "Unknown"

                job = JobListing(
                    id              = job_id,
                    source          = _clean_source(raw.get("source")),
                    title           = title[:299],
                    company_name    = (raw.get("company_name") or "Unknown")[:199],
                    location        = (raw.get("location") or "Remote")[:199],
                    remote          = raw.get("remote", True),
                    description     = (raw.get("description") or "")[:8000],
                    apply_url       = url,
                    ats_type        = _detect_ats(url),
                    easy_apply      = raw.get("easy_apply", False),
                    salary_min      = raw.get("salary_min"),
                    salary_max      = raw.get("salary_max"),
                    required_skills = raw.get("required_skills") or [],
                    discovered_at   = _parse_dt(raw.get("discovered_at")) or datetime.now(timezone.utc),
                    posted_at       = _parse_dt(raw.get("posted_at")),
                )
                session.merge(job)
                jobs_inserted += 1

                # Fit score — upsert by job_id (its unique natural key).
                fs = score_by_job.get(raw.get("id", ""))
                if fs:
                    fs_id = existing_score_id_by_job.get(job_id) or _safe_id(fs.get("id"))
                    if fs_id in seen_score_ids:
                        fs_id = str(uuid.uuid4())
                    seen_score_ids.add(fs_id)
                    session.merge(FitScore(
                        id               = fs_id,
                        job_id           = job_id,
                        score            = fs.get("score", 0),
                        passed_threshold = fs.get("passed_threshold", False),
                        highlights       = fs.get("highlights") or [],
                        missing_keywords = fs.get("missing_keywords") or [],
                        model            = fs.get("model", "claude-haiku-4-5"),
                        scored_at        = _parse_dt(fs.get("scored_at")) or datetime.now(timezone.utc),
                    ))
                    scores_inserted += 1

                # Application — upsert by job_id (its unique natural key), reusing the
                # DB row id so we update in place rather than colliding on a new id.
                app_raw = app_by_job.get(raw.get("id", ""))
                if app_raw:
                    app_id = existing_app_id_by_job.get(job_id) or _safe_id(app_raw.get("id"))
                    if app_id in seen_app_ids:
                        app_id = str(uuid.uuid4())
                    seen_app_ids.add(app_id)

                    status = _map_status(app_raw.get("status"))
                    applied_entry = applied_by_url.get(url) or applied_by_url.get(app_raw.get("confirmation_url", ""))
                    if applied_entry:
                        status = ApplicationStatus.submitted

                    # Preserve a human decision / terminal outcome already in the DB —
                    # don't let a staler JSON status overwrite it. (Submitted always wins.)
                    prior = existing_app_status_by_job.get(job_id)
                    if (prior in _DECIDED_STATUSES
                            and status != ApplicationStatus.submitted
                            and prior != status):
                        decisions_preserved += 1
                        continue

                    written_app_job_ids.add(job_id)
                    session.merge(Application(
                        id                   = app_id,
                        job_id               = job_id,
                        status               = status,
                        quality_score        = app_raw.get("quality_score"),
                        fit_score            = app_raw.get("fit_score"),
                        quality_report       = app_raw.get("quality_report"),
                        tailored_resume_path = app_raw.get("tailored_resume_path"),
                        cover_letter_path    = app_raw.get("cover_letter_path"),
                        ats_url              = url,
                        confirmation_url     = app_raw.get("confirmation_url"),
                        rejection_reason     = app_raw.get("rejection_reason"),
                        referral_first       = bool(app_raw.get("referral_first", False)),
                        referral_done        = bool(app_raw.get("referral_done", False)),
                        employment_type      = app_raw.get("employment_type", "") or "",
                        submitted_at         = _parse_dt(app_raw.get("submitted_at")),
                        # TRUE creation time (when the app entered the queue) — was wrongly set to
                        # submitted_at/now, which made created_at the SYNC time for pending apps and
                        # broke "added X ago" sorting. Prefer the JSON created_at, then the job's
                        # discovered_at, then submitted_at, then now.
                        created_at           = (_parse_dt(app_raw.get("created_at"))
                                                or _parse_dt(app_raw.get("discovered_at"))
                                                or _parse_dt(app_raw.get("submitted_at"))
                                                or datetime.now(timezone.utc)),
                    ))
                    apps_inserted += 1

        session.flush()

        # ── TRIPWIRE: refuse to prune if the incoming JSON is suspiciously smaller than the DB.
        #    A truncated/partial/empty state (e.g. a crashed write) would otherwise cascade-DELETE
        #    most of the jobs/apps tables. If the JSON has <50% of the DB's current job count (and
        #    the DB isn't tiny), skip pruning this sync — upserts already applied, nothing deleted.
        db_job_count = session.execute(select(func.count(JobListing.id))).scalar() or 0
        json_job_count = len(seen_ids)
        # Symmetric APPLICATIONS tripwire: a truncated apps array (right job count, missing apps)
        # would otherwise let the orphan-app prune wipe the whole pending_review queue. Guard it too.
        from db.models import ApplicationStatus as _AS
        db_app_count = session.execute(
            select(func.count(Application.id))
            .where(Application.status.notin_(list(_DECIDED_STATUSES)))).scalar() or 0
        json_app_count = len(written_app_job_ids)
        if not force_prune and _should_skip_prune(db_job_count, json_job_count, db_app_count, json_app_count):
            counts_note = (f"prune SKIPPED — tripwire: json={json_job_count}/{json_app_count} "
                           f"jobs/apps vs db={db_job_count}/{db_app_count} (>50% drop). "
                           f"Investigate the state file before re-syncing.")
            if not quiet:
                print(f"  ⚠️  {counts_note}")
            return {"jobs": jobs_inserted, "scores": scores_inserted, "apps": apps_inserted,
                    "decisions_preserved": decisions_preserved, "skipped": skipped, "pruned": 0,
                    "prune_skipped": True, "note": counts_note}

        # ── RECONCILE: the JSON state is the source of truth. Prune DB job_listings that are no
        #    longer in it — stale re-discoveries (old UUIDs), URL-normalized duplicates, and
        #    dropped foreign/non-US roles — so the UI mirrors the live queue instead of serving
        #    old branded/Warsaw rows. Preserve anything with a human/terminal decision.
        for jid, status in session.execute(
            select(JobListing.id, Application.status)
            .outerjoin(Application, Application.job_id == JobListing.id)
        ).all():
            if jid in seen_ids or status in _DECIDED_STATUSES:
                continue
            obj = session.get(JobListing, jid)
            if obj is not None:
                session.delete(obj)        # cascades to Application + FitScore
                pruned += 1
        # Also prune ORPHANED applications: the job_listing survived (still a lead in JSON) but its
        # application was removed from JSON. Otherwise a stale branded/foreign app lingers in the UI.
        for aid, jid, status in session.execute(
            select(Application.id, Application.job_id, Application.status)
        ).all():
            if jid in written_app_job_ids or status in _DECIDED_STATUSES:
                continue
            obj = session.get(Application, aid)
            if obj is not None:
                session.delete(obj)
                pruned += 1
        session.flush()

    counts = {
        "jobs": jobs_inserted, "scores": scores_inserted, "apps": apps_inserted,
        "decisions_preserved": decisions_preserved, "skipped": skipped, "pruned": pruned,
    }
    if not quiet:
        print(f"Sync complete:")
        print(f"  jobs upserted:        {jobs_inserted}")
        print(f"  scores upserted:      {scores_inserted}")
        print(f"  apps upserted:        {apps_inserted}")
        print(f"  decisions preserved:  {decisions_preserved}")
        print(f"  skipped (dups):       {skipped}")
    return counts


async def sync_to_db_async() -> dict:
    """Run the idempotent JSON→DB sync off the event loop. Never raises — if the DB
    is down the pipeline must keep going; the UI just stays stale until next sync."""
    import asyncio
    try:
        return await asyncio.to_thread(migrate, True)
    except Exception:
        return {}


if __name__ == "__main__":
    migrate()
