"""
Pipeline control routes — start/stop/status + SSE event stream.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from api.schemas import PipelineStartIn, PipelineStatusOut, PipelineRunOut
from db.models import Application, ApplicationStatus, PipelineRun
from db.session import get_async_session

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

_ROOT     = Path(__file__).parent.parent
_PID_FILE = Path("/tmp/autonomoushire_pipeline.pid")
_LOG_FILE = Path("/tmp/autonomoushire_pipeline.log")
# The launchd SCHEDULER (the thing that actually runs day-to-day) logs here — NOT to _LOG_FILE
# and it never writes _PID_FILE. Status must read this too, or the dashboard wrongly shows
# "not running / never ran".
_SCHED_LOG = _ROOT / "data" / "scheduler.out.log"

# Friendly current-step names for the scheduler's recent activity (its JSON log "event" field).
_SCHED_EVENT_STEP = [
    ("scheduled_board_scan",    "Board scan (discovery)"),
    ("board_scan",              "Board scan (discovery)"),
    ("teksystems_scan",         "TEKsystems scan"),
    ("kforce_scan",             "Kforce scan"),
    ("insightglobal_scan",      "Insight Global scan"),
    ("apply_pipeline",          "Apply pipeline (score + park)"),
    ("ats_intake",              "ATS intake (resolve + park)"),
    ("auto_submit",             "Auto-submit"),
    ("email_monitor",           "Email monitor"),
    ("linkedin_scan",           "LinkedIn scan"),
    ("health_check",            "Health check"),
    ("db_synced",               "DB sync"),
]


def _tail_lines(path: Path, n: int = 60, max_bytes: int = 65536) -> list[str]:
    """Read only the last ~max_bytes of a file and return its final n lines — O(1) in file size.
    The scheduler log grows to hundreds of MB; read_text() on the whole thing blocked the event
    loop for ~20s per call (and /status is polled every few seconds). Seek-from-end instead."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        return data.decode("utf-8", errors="ignore").splitlines()[-n:]
    except Exception:
        return []


def _scheduler_activity():
    """(last_activity_dt, friendly_step) from the scheduler's JSON log tail — its REAL last run."""
    if not _SCHED_LOG.exists():
        return None, None
    try:
        lines = _tail_lines(_SCHED_LOG, 60)
    except Exception:
        return None, None
    last_dt, last_event = None, None
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        ts, ev = rec.get("timestamp"), rec.get("event", "")
        if ts and last_dt is None:
            try:
                last_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                pass
        if ev and last_event is None:
            last_event = ev
        if last_dt and last_event:
            break
    step = None
    if last_event:
        low = last_event.lower()
        step = next((name for key, name in _SCHED_EVENT_STEP if key in low), last_event)
    return last_dt, step

_STEP_PATTERNS = [
    ("Scanning LinkedIn",      ["li_scan", "searching linkedin", "li_cards_found"]),
    ("Navigating to job",      ["navigating to job", "linkedin_browser_started", "browser_started"]),
    ("Extracting description", ["extracting description", "li_job_no_description"]),
    ("Detecting ATS URL",      ["external_apply_button", "ats_url_from_href"]),
    ("Scoring fit",            ["scoring:", "score_failed", "tailoring_start"]),
    ("Tailoring resume",       ["tailoring resume", "tailoring_start", "pdf_generated"]),
    ("Quality review",         ["quality review", "quality_review_json"]),
    ("Submitting to ATS",      ["submitting to", "form_fill_start", "submission_confirmed"]),
]


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_pid() -> Optional[int]:
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
        return pid if _pid_running(pid) else None
    except Exception:
        return None


def _current_step_from_log() -> Optional[str]:
    if not _LOG_FILE.exists():
        return None
    try:
        tail = "\n".join(_tail_lines(_LOG_FILE, 40)).lower()
    except Exception:
        return None
    for step_name, patterns in reversed(_STEP_PATTERNS):
        if any(p in tail for p in patterns):
            return step_name
    return None


@router.get("/status", response_model=PipelineStatusOut)
async def get_status():
    pid = _read_pid()
    running = pid is not None

    async with get_async_session() as session:
        today = date.today().isoformat()
        submitted_today = (await session.execute(
            select(func.count()).select_from(Application)
            .where(Application.status == ApplicationStatus.submitted)
            .where(Application.submitted_at >= datetime.fromisoformat(today))
        )).scalar_one()

        # LinkedIn queue depth
        from db.models import JobListing, FitScore
        from sqlalchemy import not_, exists
        has_app = exists().where(Application.job_id == JobListing.id)
        queue_depth = (await session.execute(
            select(func.count()).select_from(JobListing)
            .where(JobListing.source == "linkedin")
            .where(not_(has_app))
        )).scalar_one()

        last_run = (await session.execute(
            select(PipelineRun.started_at)
            .order_by(PipelineRun.started_at.desc())
            .limit(1)
        )).scalar_one_or_none()

    # Fold in the launchd SCHEDULER's real activity (it doesn't write the pid/PipelineRun).
    sched_dt, sched_step = _scheduler_activity()
    # last_run = the more recent of the API run and the scheduler's last log event.
    def _aware(d):
        return d.replace(tzinfo=timezone.utc) if d and d.tzinfo is None else d
    candidates = [d for d in (_aware(last_run), sched_dt) if d]
    last_run = max(candidates) if candidates else None
    # The scheduler is "running" right now if it logged within the last ~3 min (a scan in flight).
    sched_active = bool(sched_dt and (datetime.now(timezone.utc) - sched_dt).total_seconds() < 180)
    running = running or sched_active
    current_step = (_current_step_from_log() if pid else None) or (sched_step if sched_active else None)

    return PipelineStatusOut(
        running=running,
        pid=pid,
        current_step=current_step,
        queue_depth=queue_depth,
        submitted_today=submitted_today,
        last_run_at=last_run,
    )


@router.post("/start")
async def start_pipeline(body: PipelineStartIn):
    if _read_pid():
        return {"ok": False, "error": "Pipeline already running"}

    venv_python = str(_ROOT / ".venv" / "bin" / "python")
    python = venv_python if Path(venv_python).exists() else sys.executable
    args = [python, "-m", "orchestration.runner"]

    if body.mode == "linkedin":
        args.append("--linkedin")
    elif body.mode == "submit_only":
        args.append("--submit-only")
        if body.auto_approve:
            args.append("--auto-approve")
    elif body.mode == "dry_run":
        args.append("--dry-run")

    log_fh = open(_LOG_FILE, "w")
    proc = subprocess.Popen(args, cwd=str(_ROOT), stdout=log_fh, stderr=log_fh)
    _PID_FILE.write_text(str(proc.pid))
    return {"ok": True, "pid": proc.pid, "mode": body.mode}


@router.post("/stop")
async def stop_pipeline():
    pid = _read_pid()
    if not pid:
        return {"ok": False, "error": "Pipeline not running"}
    try:
        os.kill(pid, signal.SIGTERM)
        _PID_FILE.unlink(missing_ok=True)
        return {"ok": True, "killed_pid": pid}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/runs", response_model=list[PipelineRunOut])
async def list_runs(limit: int = 20):
    async with get_async_session() as session:
        rows = (await session.execute(
            select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit)
        )).scalars().all()
    return rows


async def _sse_log_stream() -> AsyncIterator[str]:
    """Stream new log lines as SSE events every second while pipeline runs."""
    last_pos = 0
    idle_ticks = 0
    while True:
        await asyncio.sleep(1)
        pid = _read_pid()
        if not pid:
            idle_ticks += 1
            if idle_ticks > 5:
                yield "data: {\"type\":\"done\"}\n\n"
                return
            yield f"data: {{\"type\":\"idle\"}}\n\n"
            continue
        idle_ticks = 0
        if _LOG_FILE.exists():
            try:
                content = _LOG_FILE.read_text()
                new_content = content[last_pos:]
                last_pos = len(content)
                for line in new_content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    # Try to parse as JSON log entry
                    try:
                        entry = json.loads(line)
                        payload = json.dumps({"type": "log", "entry": entry})
                    except Exception:
                        payload = json.dumps({"type": "text", "line": line})
                    yield f"data: {payload}\n\n"
            except Exception:
                pass
        step = _current_step_from_log()
        yield f"data: {json.dumps({'type':'step','step':step})}\n\n"


@router.get("/stream")
async def pipeline_stream():
    return StreamingResponse(
        _sse_log_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
