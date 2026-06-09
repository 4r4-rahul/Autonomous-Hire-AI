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
        tail = "\n".join(_LOG_FILE.read_text().splitlines()[-40:]).lower()
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

    return PipelineStatusOut(
        running=running,
        pid=pid,
        current_step=_current_step_from_log() if running else None,
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
