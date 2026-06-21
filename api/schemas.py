"""Pydantic response schemas for the API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class JobOut(BaseModel):
    id: str
    source: str
    title: str
    company_name: str
    location: str
    remote: bool
    apply_url: str
    ats_type: Optional[str]
    easy_apply: bool
    salary_min: Optional[int]
    salary_max: Optional[int]
    discovered_at: datetime
    fit_score: Optional[int] = None
    passed_threshold: Optional[bool] = None

    class Config:
        from_attributes = True


class ApplicationOut(BaseModel):
    id: str
    job_id: str
    status: str
    quality_score: Optional[int]
    fit_score: Optional[int] = None
    quality_report: Optional[dict[str, Any]]
    tailored_resume_path: Optional[str]
    ats_url: Optional[str]
    confirmation_url: Optional[str]
    rejection_reason: Optional[str]
    pipeline_step: Optional[str]
    submitted_at: Optional[datetime]
    updated_at: datetime
    created_at: Optional[datetime] = None    # when the app entered the queue ("added X ago" sorting)
    # Denormalized from job
    company_name: Optional[str] = None
    role_title: Optional[str] = None
    posted_at: Optional[datetime] = None
    location: Optional[str] = None          # US-first display for multi-location roles
    multi_location: bool = False            # true when also offered abroad (apply to the US office)

    class Config:
        from_attributes = True


class PipelineStatusOut(BaseModel):
    running: bool
    pid: Optional[int]
    current_step: Optional[str]
    queue_depth: int
    submitted_today: int
    last_run_at: Optional[datetime]


class PipelineStartIn(BaseModel):
    mode: str = "linkedin"       # linkedin | submit_only | dry_run
    auto_approve: bool = False


class ApproveIn(BaseModel):
    action: str   # approve | reject
    reason: Optional[str] = None


class PipelineRunOut(BaseModel):
    id: int
    trigger: str
    status: str
    jobs_attempted: int
    jobs_submitted: int
    jobs_skipped: int
    jobs_failed: int
    scan_new_jobs: int
    rate_limited: bool
    error: Optional[str]
    started_at: datetime
    finished_at: Optional[datetime]
    duration_seconds: Optional[float]

    class Config:
        from_attributes = True


class StatsOut(BaseModel):
    total_jobs: int
    linkedin_jobs: int
    queued_jobs: int
    total_applications: int
    submitted: int
    pending_review: int
    pending_manual: int
    blocked: int
    avg_quality_score: float
    pass_rate: float
