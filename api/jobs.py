"""Job listings routes."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from sqlalchemy import func, select, not_, exists

from api.schemas import JobOut, StatsOut
from db.models import Application, ApplicationStatus, FitScore, JobListing
from db.session import get_async_session

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _job_row_to_out(job: JobListing, score: Optional[int], passed: Optional[bool]) -> JobOut:
    d = {c.key: getattr(job, c.key) for c in job.__table__.columns}
    d["fit_score"] = score
    d["passed_threshold"] = passed
    return JobOut.model_validate(d)


@router.get("/", response_model=list[JobOut])
async def list_jobs(
    source: Optional[str] = None,
    company: Optional[str] = None,
    queued_only: bool = False,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    async with get_async_session() as session:
        q = select(JobListing, FitScore.score, FitScore.passed_threshold).outerjoin(
            FitScore, FitScore.job_id == JobListing.id
        )
        if source:
            q = q.where(JobListing.source == source)
        if company:
            q = q.where(JobListing.company_name.ilike(f"%{company}%"))
        if queued_only:
            has_app = exists().where(Application.job_id == JobListing.id)
            q = q.where(not_(has_app)).where(JobListing.source == "linkedin")

        q = q.order_by(JobListing.discovered_at.desc()).limit(limit).offset(offset)
        rows = (await session.execute(q)).all()

    return [_job_row_to_out(job, score, passed) for job, score, passed in rows]


@router.get("/stats", response_model=StatsOut)
async def get_stats():
    async with get_async_session() as session:
        total_jobs = (await session.execute(select(func.count()).select_from(JobListing))).scalar_one()
        li_jobs    = (await session.execute(
            select(func.count()).select_from(JobListing).where(JobListing.source == "linkedin")
        )).scalar_one()

        has_app = exists().where(Application.job_id == JobListing.id)
        queued = (await session.execute(
            select(func.count()).select_from(JobListing)
            .where(JobListing.source == "linkedin")
            .where(not_(has_app))
        )).scalar_one()

        total_apps = (await session.execute(select(func.count()).select_from(Application))).scalar_one()

        def count_status(s):
            return select(func.count()).select_from(Application).where(Application.status == s)

        submitted   = (await session.execute(count_status(ApplicationStatus.submitted))).scalar_one()
        pend_review = (await session.execute(count_status(ApplicationStatus.pending_approval))).scalar_one()
        pend_manual = (await session.execute(count_status(ApplicationStatus.pending_manual_submit))).scalar_one()
        blocked     = (await session.execute(count_status(ApplicationStatus.blocked))).scalar_one()

        avg_score_row = (await session.execute(
            select(func.avg(Application.quality_score)).where(Application.quality_score.isnot(None))
        )).scalar_one()

        total_scored  = (await session.execute(select(func.count()).select_from(FitScore))).scalar_one()
        passed_scored = (await session.execute(
            select(func.count()).select_from(FitScore).where(FitScore.passed_threshold.is_(True))
        )).scalar_one()

    return StatsOut(
        total_jobs=total_jobs,
        linkedin_jobs=li_jobs,
        queued_jobs=queued,
        total_applications=total_apps,
        submitted=submitted,
        pending_review=pend_review,
        pending_manual=pend_manual,
        blocked=blocked,
        avg_quality_score=round(float(avg_score_row or 0), 1),
        pass_rate=round(passed_scored / max(total_scored, 1) * 100, 1),
    )


@router.get("/queue", response_model=list[JobOut])
async def get_queue(limit: int = Query(40, le=200)):
    """Next N LinkedIn jobs that will be attempted in the next pipeline run."""
    async with get_async_session() as session:
        has_app = exists().where(Application.job_id == JobListing.id)
        q = (
            select(JobListing, FitScore.score, FitScore.passed_threshold)
            .outerjoin(FitScore, FitScore.job_id == JobListing.id)
            .where(JobListing.source == "linkedin")
            .where(not_(has_app))
            .order_by(FitScore.score.desc().nulls_last(), JobListing.discovered_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(q)).all()

    return [_job_row_to_out(job, score, passed) for job, score, passed in rows]
