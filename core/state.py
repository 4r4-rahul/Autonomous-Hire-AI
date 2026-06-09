from typing import TypedDict, List, Optional, Any, Literal
from datetime import datetime


# ── LinkedIn types ─────────────────────────────────────────────────────────────

class LinkedInMessage(TypedDict):
    stage: str        # connection_note | thanks | ask | followup_1 | followup_final
    body: str
    sent_at: Optional[str]
    status: str       # drafted | approved | sent | replied | failed


class LinkedInContact(TypedDict):
    id: str
    li_profile_url: str
    full_name: str
    headline: str
    title: str
    company: str
    related_job_id: Optional[str]   # JobListing.id that triggered discovery
    relevance_score: int            # 0-100
    connection_status: str          # none | requested | connected | declined | withdrawn
    cadence_stage: int              # 0=none 1=connect_sent 2=connected 3=thanks_sent
                                    # 4=ask_sent 5=followup_sent 6=archived
    next_action_at: Optional[str]   # ISO datetime
    last_action_at: Optional[str]
    notes: str
    messages: List[LinkedInMessage]


class Company(TypedDict):
    id: str
    name: str
    website: str
    linkedin_url: str
    size: str          # "startup" | "scaleup" | "mid" | "large"
    domain: str
    tech_stack: List[str]
    open_role_count: int
    discovered_at: str  # ISO datetime string


class JobListing(TypedDict):
    id: str
    company_id: str
    company_name: str
    title: str
    description: str
    required_skills: List[str]
    nice_to_have_skills: List[str]
    location: str
    remote: bool
    salary_min: Optional[int]
    salary_max: Optional[int]
    apply_url: str
    source: str         # "linkedin" | "greenhouse" | "lever" | "workday" | "indeed"
    discovered_at: str


class FitScore(TypedDict):
    job_id: str
    score: int          # 0–100
    reasoning: str
    missing_skills: List[str]
    match_highlights: List[str]
    passed_threshold: bool
    scored_at: str


class Application(TypedDict):
    id: str
    job_id: str
    company_name: str
    role_title: str
    status: str         # "pending_review" | "approved" | "submitted" | "rejected" | "interview" | "offer"
    tailored_resume_path: str
    cover_letter_path: Optional[str]
    quality_score: int
    quality_report: dict
    submitted_at: Optional[str]
    confirmation_url: Optional[str]
    rejection_reason: Optional[str]


class RecruiterContact(TypedDict):
    id: str
    name: str
    title: str
    linkedin_url: str
    company_id: str
    company_name: str
    message_sent: bool
    message_text: Optional[str]
    sent_at: Optional[str]
    responded: bool
    conversation_history: List[dict]  # [{"role": "sent"|"received", "text": str, "at": str}]


class PendingApproval(TypedDict):
    id: str
    gate_type: str      # "submit_application" | "send_linkedin_message" | "confirm_meeting" | "offer_received"
    description: str
    payload: dict       # gate-specific data to show the user
    created_at: str
    expires_at: Optional[str]
    status: str         # "pending" | "approved" | "rejected" | "expired"


class EmailEvent(TypedDict):
    id: str
    gmail_message_id: str
    subject: str
    sender: str
    received_at: str
    category: str       # "interview_invite" | "rejection" | "info_request" | "offer" | "follow_up_needed" | "other"
    company_name: Optional[str]
    job_id: Optional[str]
    summary: str
    actioned: bool


class MeetingScheduled(TypedDict):
    id: str
    company_name: str
    interviewer_name: str
    proposed_times: List[str]
    confirmed_time: Optional[str]
    calendar_event_id: Optional[str]
    status: str         # "proposed" | "confirmed" | "cancelled"
    prep_doc_path: Optional[str]


class AgentStatus(TypedDict):
    agent_name: str
    status: str         # "idle" | "running" | "error" | "paused"
    last_run_at: Optional[str]
    last_error: Optional[str]
    run_count: int


class JobSearchState(TypedDict):
    # Discovery
    companies: List[Company]
    job_listings: List[JobListing]
    fit_scores: List[FitScore]

    # Applications
    applications: List[Application]
    pending_approvals: List[PendingApproval]

    # Outreach (legacy RecruiterContact + new LinkedIn contacts)
    recruiter_contacts: List[RecruiterContact]
    linkedin_contacts: List[LinkedInContact]   # new: LinkedIn networking cadence

    # Monitoring
    email_events: List[EmailEvent]
    meetings_scheduled: List[MeetingScheduled]

    # Oversight KPIs
    weekly_quality_score: float
    weekly_quantity_count: int
    response_rate: float
    interview_rate: float

    # System
    last_run_at: Optional[str]
    errors: List[dict]
    agent_statuses: dict  # agent_name → AgentStatus
    scanned_company_ids: List[str]  # company IDs attempted by job_scanner
