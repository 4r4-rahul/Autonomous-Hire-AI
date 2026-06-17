"""Application routes — list, detail, approve, reject, proof."""
from __future__ import annotations

import json
from typing import Optional
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from api.schemas import ApplicationOut, ApproveIn
from db.models import Application, ApplicationStatus, JobListing, PipelineEvent
from db.session import get_async_session

_APPLIED_LOG    = Path(__file__).parent.parent / "data" / "applied_jobs.json"
_SCREENSHOT_DIR = Path(__file__).parent.parent / "data" / "screenshots"


def _load_applied_record(application_id: str) -> dict:
    """Look up an entry in applied_jobs.json by application_id."""
    if not _APPLIED_LOG.exists():
        return {}
    try:
        entries = json.loads(_APPLIED_LOG.read_text())
        for e in entries:
            if e.get("application_id") == application_id:
                return e
    except Exception:
        pass
    return {}

router = APIRouter(prefix="/applications", tags=["applications"])


def _enrich(app: Application) -> ApplicationOut:
    out = ApplicationOut.model_validate(app)
    if app.job:
        out.company_name = app.job.company_name
        out.role_title   = app.job.title
        out.posted_at    = app.job.posted_at
        # Multi-location roles (US + foreign) show the US office first + a flag, so the candidate
        # applies to the US location and isn't alarmed by a foreign city shown first.
        from agents.discovery.apply_url_resolver import us_first_location
        disp, multi = us_first_location(app.job.location or "")
        out.location = disp
        out.multi_location = multi
    return out


@router.get("/", response_model=list[ApplicationOut])
async def list_applications(
    status: Optional[str] = None,
    company: Optional[str] = None,
    min_score: int = 0,
    sort_by: str = "updated_at",
    limit: int = Query(100, le=500),
    offset: int = 0,
):
    async with get_async_session() as session:
        q = select(Application).options(selectinload(Application.job)).join(JobListing, JobListing.id == Application.job_id, isouter=True)
        if status:
            q = q.where(Application.status == status)
        if company:
            q = q.where(JobListing.company_name.ilike(f"%{company}%"))
        if min_score:
            q = q.where(Application.quality_score >= min_score)
        if sort_by == "score":
            q = q.order_by(Application.quality_score.desc().nulls_last())
        elif sort_by == "company":
            q = q.order_by(JobListing.company_name)
        elif sort_by == "recency":
            # Freshest posting first — apply to the newest roles before the applicant pile grows.
            q = q.order_by(JobListing.posted_at.desc().nulls_last())
        else:
            q = q.order_by(Application.updated_at.desc())
        q = q.limit(limit).offset(offset)
        apps = (await session.execute(q)).scalars().all()

    return [_enrich(a) for a in apps]


@router.get("/{app_id}", response_model=ApplicationOut)
async def get_application(app_id: str):
    async with get_async_session() as session:
        app = (await session.execute(
            select(Application)
            .options(selectinload(Application.job))
            .where(Application.id == app_id)
        )).scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")
    return _enrich(app)


@router.get("/{app_id}/resume")
async def get_resume(app_id: str):
    """Return the tailored resume markdown text."""
    async with get_async_session() as session:
        app = (await session.execute(
            select(Application).where(Application.id == app_id)
        )).scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")
    if not app.tailored_resume_path:
        raise HTTPException(404, "No tailored resume for this application")
    p = Path(app.tailored_resume_path)
    if not p.exists():
        raise HTTPException(404, f"Resume file missing: {p}")
    return {"resume_md": p.read_text(encoding="utf-8")}


@router.get("/{app_id}/resume.pdf")
async def get_resume_pdf(app_id: str, download: bool = False):
    """Serve the EXACT tailored résumé PDF submitted for this application — the same bytes
    the extension attached (rendered from the stored markdown, cached). `download=true`
    forces a Save dialog with a human-readable filename; otherwise it opens inline."""
    from fastapi.responses import FileResponse
    from db.models import JobListing
    async with get_async_session() as session:
        app = (await session.execute(
            select(Application).where(Application.id == app_id)
        )).scalar_one_or_none()
        if not app:
            raise HTTPException(404, "Application not found")
        if not app.tailored_resume_path:
            raise HTTPException(404, "No tailored resume for this application")
        resume_rel = app.tailored_resume_path
        # Capture job company/title INSIDE the session (the relationship lazy-loads otherwise).
        job = (await session.execute(
            select(JobListing).where(JobListing.id == app.job_id)
        )).scalar_one_or_none()
        co_name = (job.company_name if job else "") or "Resume"
        role_title = (job.title if job else "") or ""

    md = Path(resume_rel)
    if not md.is_absolute():
        md = Path(__file__).parent.parent / md
    if not md.exists():
        raise HTTPException(404, f"Resume file missing: {md}")
    try:
        from agents.application.form_filler import markdown_to_pdf
        pdf = markdown_to_pdf(md)
    except Exception as e:
        raise HTTPException(500, f"resume render failed: {e}")
    # Friendly filename: Company__Role.pdf
    import re as _re
    co = _re.sub(r"[^A-Za-z0-9]+", "_", co_name).strip("_") or "Resume"
    role = _re.sub(r"[^A-Za-z0-9]+", "_", role_title).strip("_")
    fname = f"{co}{('__' + role) if role else ''}_Resume.pdf"
    return FileResponse(
        str(pdf), media_type="application/pdf", filename=fname,
        content_disposition_type=("attachment" if download else "inline"),
    )


@router.post("/{app_id}/referral-kit")
async def referral_kit_endpoint(app_id: str, draft: bool = True):
    """Build the warm-intro kit for a role: LinkedIn people-search links (instant) + drafted
    connection note & referral-ask (Haiku). Referrals are the biggest callback lever."""
    from agents.outreach.referral import build_referral_kit
    kit = await build_referral_kit(app_id=app_id, draft=draft)
    if kit.get("error"):
        raise HTTPException(404, kit["error"])
    return kit


@router.get("/{app_id}/cover-letter")
async def get_cover_letter(app_id: str):
    """Return the tailored cover letter markdown (with proactive work-authorization framing), if any."""
    async with get_async_session() as session:
        app = (await session.execute(
            select(Application).where(Application.id == app_id)
        )).scalar_one_or_none()
        if not app:
            raise HTTPException(404, "Application not found")
        if not app.cover_letter_path:
            raise HTTPException(404, "No cover letter for this application")
        p = Path(app.cover_letter_path)
        if not p.exists():
            raise HTTPException(404, "Cover letter file missing")
        return {"cover_letter_md": p.read_text(encoding="utf-8")}


@router.get("/{app_id}/form-answers")
async def get_form_answers(app_id: str):
    """The open-ended LLM answers drafted + cached for this application's forms (a record of what was
    filled, reusable across re-opens). Reads the JSON state — the source of truth for form_answers."""
    import json
    from pathlib import Path
    state_path = Path(__file__).resolve().parent.parent / "data" / "pipeline_state.json"
    try:
        st = json.loads(state_path.read_text())
    except Exception:
        st = {}
    app = next((a for a in st.get("applications", []) if a.get("id") == app_id), None)
    if app is None:
        raise HTTPException(404, "Application not found")
    fa = app.get("form_answers", {}) or {}
    items = sorted(
        ({"question": v.get("question", ""), "answer": v.get("answer", ""),
          "updated_at": v.get("updated_at", "")} for v in fa.values()),
        key=lambda x: x["updated_at"], reverse=True,
    )
    return {"count": len(items), "answers": items}


@router.post("/{app_id}/tailor")
async def tailor_application_endpoint(app_id: str):
    """Tailor-on-demand: generate the Opus résumé for ONE parked role when the user decides to
    pursue it ("prepare"). Spends Opus only on roles the user actually engages. Idempotent."""
    from agents.application.on_demand_tailor import tailor_application
    result = await tailor_application(app_id=app_id)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@router.post("/{app_id}/decide")
async def decide_application(app_id: str, body: ApproveIn):
    """Record a human decision on a reviewed application. In the extension model the
    user applies in their own browser (Open & Apply → extension fills → they submit),
    so this only TRACKS the outcome — it never drives a browser:
      action="approve" → "Mark Applied"  → status = submitted
      action="reject"  → "Skip"          → status = skipped
    """
    from datetime import datetime, timezone
    async with get_async_session() as session:
        app = (await session.execute(
            select(Application).where(Application.id == app_id)
        )).scalar_one_or_none()
        if not app:
            raise HTTPException(404, "Application not found")
        if app.status not in (ApplicationStatus.pending_approval, ApplicationStatus.reviewing):
            raise HTTPException(400, f"Cannot decide on status={app.status}")

        if body.action == "approve":
            app.status = ApplicationStatus.submitted
            app.submitted_at = datetime.now(timezone.utc)
        elif body.action == "reject":
            app.status = ApplicationStatus.skipped
            app.rejection_reason = body.reason or "Skipped via UI"
        else:
            raise HTTPException(400, "action must be approve or reject")

        session.add(PipelineEvent(
            application_id=app_id,
            step="human_decision",
            status="applied" if body.action == "approve" else "skipped",
            message=body.reason,
        ))

    new_status = "submitted" if body.action == "approve" else "skipped"
    return {"ok": True, "new_status": new_status}


@router.get("/{app_id}/proof")
async def get_proof(app_id: str):
    """Return all submission evidence: resume, screenshots, filled fields, confirmation."""
    async with get_async_session() as session:
        app = (await session.execute(
            select(Application)
            .options(selectinload(Application.job))
            .where(Application.id == app_id)
        )).scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")

    log_entry = _load_applied_record(app_id)

    # Resume markdown
    resume_md = ""
    resume_path = app.tailored_resume_path or log_entry.get("resume_path", "")
    if resume_path:
        p = Path(resume_path)
        if not p.is_absolute():
            p = Path(__file__).parent.parent / p
        if p.exists():
            raw = p.read_text(encoding="utf-8")
            resume_md = raw.split("<!-- TAILOR_META")[0].strip()

    # Cover letter markdown (only some roles have one — selective). Surfaced so the UI can show it.
    cover_md = ""
    cover_path = getattr(app, "cover_letter_path", None) or log_entry.get("cover_letter_path", "")
    if cover_path:
        cp = Path(cover_path)
        if not cp.is_absolute():
            cp = Path(__file__).parent.parent / cp
        if cp.exists():
            cover_md = cp.read_text(encoding="utf-8").strip()

    # Screenshot URLs (relative to /screenshots static mount)
    def _screenshot_url(app_id: str, suffix: str) -> str | None:
        fname = f"{app_id}_{suffix}.png"
        if (_SCREENSHOT_DIR / fname).exists():
            return f"/screenshots/{fname}"
        return None

    # Job description from related job
    job_description = ""
    company_name = None
    role_title = None
    if app.job:
        job_description = app.job.description or ""
        company_name = app.job.company_name
        role_title = app.job.title

    return {
        "application_id":   app_id,
        "company":          company_name or log_entry.get("company"),
        "role":             role_title or log_entry.get("role"),
        "ats":              log_entry.get("ats", "unknown"),
        "ats_url":          app.ats_url or log_entry.get("apply_url"),
        "confirmation_url": app.confirmation_url or log_entry.get("confirmation_url"),
        "status":           app.status,
        "submitted_at":     app.submitted_at.isoformat() if app.submitted_at else log_entry.get("submitted_at"),
        "quality_score":    app.quality_score or log_entry.get("quality_score"),
        "fit_score":        app.fit_score,
        "quality_report":   app.quality_report,
        "resume_markdown":  resume_md,
        "cover_letter_markdown": cover_md,
        "has_cover_letter":      bool(cover_md),
        "filled_screenshot_url":        _screenshot_url(app_id, "filled"),
        "confirmation_screenshot_url":  _screenshot_url(app_id, "confirmation"),
        "debug_screenshot_url":         _screenshot_url(app_id, "submit_debug"),
        "job_description":  job_description[:3000] if job_description else "",
    }


@router.get("/{app_id}/events")
async def get_events(app_id: str):
    """Full audit trail for this application."""
    async with get_async_session() as session:
        events = (await session.execute(
            select(PipelineEvent)
            .where(PipelineEvent.application_id == app_id)
            .order_by(PipelineEvent.created_at)
        )).scalars().all()
    return [
        {
            "step": e.step,
            "status": e.status,
            "message": e.message,
            "detail": e.detail,
            "at": e.created_at.isoformat(),
        }
        for e in events
    ]
