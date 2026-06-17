"""
SQLAlchemy ORM models for AutonomousHire.
Single-user, single-machine. No auth/multi-tenancy.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ── Enums ──────────────────────────────────────────────────────────────────────

class JobSource(str, enum.Enum):
    linkedin     = "linkedin"
    greenhouse   = "greenhouse"
    lever        = "lever"
    hn_hiring    = "hn_hiring"
    remoteok     = "remoteok"
    careers_page = "careers_page"
    manual       = "manual"


class ApplicationStatus(str, enum.Enum):
    queued               = "queued"
    scoring              = "scoring"
    tailoring            = "tailoring"
    reviewing            = "reviewing"
    pending_approval     = "pending_approval"
    approved             = "approved"
    submitting           = "submitting"
    submitted            = "submitted"
    pending_manual_submit = "pending_manual_submit"
    blocked              = "blocked"
    skipped              = "skipped"
    rejected             = "rejected"


class AtsType(str, enum.Enum):
    greenhouse    = "greenhouse"
    lever         = "lever"
    workday       = "workday"
    rippling      = "rippling"
    ashby         = "ashby"
    bamboohr      = "bamboohr"
    icims         = "icims"
    smartrecruiters = "smartrecruiters"
    generic       = "generic"
    manual        = "manual"
    unknown       = "unknown"


# ── Tables ─────────────────────────────────────────────────────────────────────

class JobListing(Base):
    __tablename__ = "job_listings"

    id: Mapped[str]              = mapped_column(String(36), primary_key=True)
    source: Mapped[str]          = mapped_column(Enum(JobSource), nullable=False, index=True)
    title: Mapped[str]           = mapped_column(String(300), nullable=False)
    company_name: Mapped[str]    = mapped_column(String(200), nullable=False, index=True)
    location: Mapped[str]        = mapped_column(String(200), default="Remote")
    remote: Mapped[bool]         = mapped_column(Boolean, default=True)
    description: Mapped[str]     = mapped_column(Text, default="")
    apply_url: Mapped[str]       = mapped_column(String(1000), nullable=False)
    ats_type: Mapped[Optional[str]] = mapped_column(Enum(AtsType), nullable=True)
    easy_apply: Mapped[bool]     = mapped_column(Boolean, default=False)
    salary_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    required_skills: Mapped[list] = mapped_column(JSON, default=list)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    junk: Mapped[bool]           = mapped_column(Boolean, default=False)
    junk_reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    fit_score: Mapped[Optional["FitScore"]] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )
    application: Mapped[Optional["Application"]] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("apply_url", name="uq_job_apply_url"),
        Index("ix_job_source_company", "source", "company_name"),
    )


class FitScore(Base):
    __tablename__ = "fit_scores"

    id: Mapped[str]               = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str]           = mapped_column(ForeignKey("job_listings.id", ondelete="CASCADE"), nullable=False, index=True, unique=True)
    score: Mapped[int]            = mapped_column(Integer, nullable=False)
    passed_threshold: Mapped[bool] = mapped_column(Boolean, nullable=False)
    highlights: Mapped[list]      = mapped_column(JSON, default=list)
    missing_keywords: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str]            = mapped_column(String(100), default="claude-haiku-4-5")
    scored_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_now)

    job: Mapped["JobListing"] = relationship(back_populates="fit_score")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[str]                = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str]            = mapped_column(ForeignKey("job_listings.id", ondelete="CASCADE"), nullable=False, index=True, unique=True)
    status: Mapped[str]            = mapped_column(Enum(ApplicationStatus), nullable=False, default=ApplicationStatus.queued, index=True)
    quality_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    quality_report: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Quick Haiku fit score from intake — present on EVERY app (incl. cheap-parked ones not yet
    # tailored), so the UI can show a score before the full quality review runs.
    fit_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tailored_resume_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    cover_letter_path: Mapped[Optional[str]]    = mapped_column(String(500), nullable=True)
    ats_url: Mapped[Optional[str]]   = mapped_column(String(1000), nullable=True)
    confirmation_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pipeline_step: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_now)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    job: Mapped["JobListing"] = relationship(back_populates="application")
    events: Mapped[list["PipelineEvent"]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="PipelineEvent.created_at"
    )

    __table_args__ = (
        Index("ix_app_status_updated", "status", "updated_at"),
    )


class PipelineEvent(Base):
    """Audit trail: every status transition and step attempt for an application."""
    __tablename__ = "pipeline_events"

    id: Mapped[int]                 = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[str]     = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    step: Mapped[str]               = mapped_column(String(50), nullable=False)
    status: Mapped[str]             = mapped_column(String(50), nullable=False)
    message: Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    detail: Mapped[Optional[dict]]  = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]    = mapped_column(DateTime(timezone=True), default=_now, index=True)

    application: Mapped["Application"] = relationship(back_populates="events")


class PipelineRun(Base):
    """One execution of python -m orchestration.runner --linkedin."""
    __tablename__ = "pipeline_runs"

    id: Mapped[int]                = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str]           = mapped_column(String(50), default="manual")
    status: Mapped[str]            = mapped_column(String(30), default="running", index=True)
    jobs_attempted: Mapped[int]    = mapped_column(Integer, default=0)
    jobs_submitted: Mapped[int]    = mapped_column(Integer, default=0)
    jobs_skipped: Mapped[int]      = mapped_column(Integer, default=0)
    jobs_failed: Mapped[int]       = mapped_column(Integer, default=0)
    scan_new_jobs: Mapped[int]     = mapped_column(Integer, default=0)
    rate_limited: Mapped[bool]     = mapped_column(Boolean, default=False)
    error: Mapped[Optional[str]]   = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None
