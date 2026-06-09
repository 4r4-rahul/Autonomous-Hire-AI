"""
LangGraph orchestration for AutonomousHire.

Node execution order:
  discover_companies
       ↓
  scan_jobs  ←── also feeds from linkedin_scanner (scheduled separately)
       ↓
  score_fit
       ↓
  tailor_resumes
       ↓
  review_quality
       ↓
  submit_applications   ← human gate inside each submission

LinkedIn outreach nodes (contact_finder, outreach_runner) run independently
via orchestration/scheduler.py — they are not part of the linear pipeline.

Each node receives and returns JobSearchState.
Errors in a node are caught and appended to state["errors"] — pipeline continues.
"""

import asyncio
from datetime import datetime, timezone
from typing import Literal

from langgraph.graph import StateGraph, END

from core.state import JobSearchState
from core.logger import get_logger

logger = get_logger("graph")


# ── Node wrappers ──────────────────────────────────────────────────────────────
# Each wrapper catches exceptions and records them so the graph continues.

async def node_discover_companies(state: JobSearchState) -> JobSearchState:
    from agents.discovery.company_finder import run_all
    try:
        return await run_all(state)
    except Exception as e:
        logger.error("node_error", node="discover_companies", error=str(e))
        updated = dict(state)
        updated["errors"] = state.get("errors", []) + [{"node": "discover_companies", "error": str(e)}]
        return updated


async def node_scan_jobs(state: JobSearchState) -> JobSearchState:
    from agents.discovery.job_scanner import run
    try:
        return await run(state)
    except Exception as e:
        logger.error("node_error", node="scan_jobs", error=str(e))
        updated = dict(state)
        updated["errors"] = state.get("errors", []) + [{"node": "scan_jobs", "error": str(e)}]
        return updated


async def node_score_fit(state: JobSearchState) -> JobSearchState:
    from agents.discovery.fit_analyst import run
    try:
        return await run(state)
    except Exception as e:
        logger.error("node_error", node="score_fit", error=str(e))
        updated = dict(state)
        updated["errors"] = state.get("errors", []) + [{"node": "score_fit", "error": str(e)}]
        return updated


async def node_tailor_resumes(state: JobSearchState) -> JobSearchState:
    from agents.application.resume_tailor import run
    try:
        return await run(state)
    except Exception as e:
        logger.error("node_error", node="tailor_resumes", error=str(e))
        updated = dict(state)
        updated["errors"] = state.get("errors", []) + [{"node": "tailor_resumes", "error": str(e)}]
        return updated


async def node_review_quality(state: JobSearchState) -> JobSearchState:
    from agents.application.app_quality_reviewer import run
    try:
        return await run(state)
    except Exception as e:
        logger.error("node_error", node="review_quality", error=str(e))
        updated = dict(state)
        updated["errors"] = state.get("errors", []) + [{"node": "review_quality", "error": str(e)}]
        return updated


async def node_submit_applications(state: JobSearchState) -> JobSearchState:
    from agents.application.form_filler import run
    try:
        return await run(state)
    except Exception as e:
        logger.error("node_error", node="submit_applications", error=str(e))
        updated = dict(state)
        updated["errors"] = state.get("errors", []) + [{"node": "submit_applications", "error": str(e)}]
        return updated


# ── LinkedIn nodes (wired into graph for manual/on-demand runs) ───────────────

async def node_linkedin_scan(state: JobSearchState) -> JobSearchState:
    from agents.discovery.linkedin_scanner import run
    try:
        return await run(state)
    except Exception as e:
        logger.error("node_error", node="linkedin_scan", error=str(e))
        updated = dict(state)
        updated["errors"] = state.get("errors", []) + [{"node": "linkedin_scan", "error": str(e)}]
        return updated


async def node_find_contacts(state: JobSearchState) -> JobSearchState:
    from agents.outreach.contact_finder import run
    try:
        return await run(state)
    except Exception as e:
        logger.error("node_error", node="find_contacts", error=str(e))
        updated = dict(state)
        updated["errors"] = state.get("errors", []) + [{"node": "find_contacts", "error": str(e)}]
        return updated


async def node_run_outreach(state: JobSearchState) -> JobSearchState:
    from agents.outreach.outreach_runner import run
    try:
        return await run(state)
    except Exception as e:
        logger.error("node_error", node="run_outreach", error=str(e))
        updated = dict(state)
        updated["errors"] = state.get("errors", []) + [{"node": "run_outreach", "error": str(e)}]
        return updated


async def node_monitor_email(state: JobSearchState) -> JobSearchState:
    from agents.monitoring.email_monitor import run
    try:
        return await run(state)
    except Exception as e:
        logger.error("node_error", node="monitor_email", error=str(e))
        updated = dict(state)
        updated["errors"] = state.get("errors", []) + [{"node": "monitor_email", "error": str(e)}]
        return updated


# ── Routing ────────────────────────────────────────────────────────────────────

def route_after_discovery(state: JobSearchState) -> Literal["scan_jobs", "__end__"]:
    if state.get("companies"):
        return "scan_jobs"
    logger.warning("routing_no_companies")
    return "__end__"


def route_after_scan(state: JobSearchState) -> Literal["score_fit", "__end__"]:
    if state.get("job_listings"):
        return "score_fit"
    logger.warning("routing_no_jobs")
    return "__end__"


def route_after_scoring(state: JobSearchState) -> Literal["tailor_resumes", "__end__"]:
    from agents.discovery.fit_analyst import get_passing_jobs
    passing = get_passing_jobs(state)
    if passing:
        return "tailor_resumes"
    logger.warning("routing_no_passing_jobs")
    return "__end__"


def route_after_tailoring(state: JobSearchState) -> Literal["review_quality", "submit_applications", "__end__"]:
    apps = state.get("applications", [])
    # Apps that haven't been quality-reviewed yet
    if any(a.get("quality_score", 0) == 0 for a in apps):
        return "review_quality"
    # Apps already reviewed and passing — send straight to submit
    from core.config import get_settings
    cfg = get_settings()
    if any(
        a.get("status") == "pending_review" and a.get("quality_score", 0) >= cfg.min_app_quality
        for a in apps
    ):
        return "submit_applications"
    logger.warning("routing_no_applications_to_review")
    return "__end__"


def route_after_review(state: JobSearchState) -> Literal["submit_applications", "__end__"]:
    from core.config import get_settings
    cfg = get_settings()
    ready = any(
        a.get("status") == "pending_review" and a.get("quality_score", 0) >= cfg.min_app_quality
        for a in state.get("applications", [])
    )
    if ready:
        return "submit_applications"
    logger.warning("routing_no_applications_passed_quality")
    return "__end__"


# ── Graph construction ─────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    g = StateGraph(JobSearchState)

    g.add_node("discover_companies",   node_discover_companies)
    g.add_node("scan_jobs",            node_scan_jobs)
    g.add_node("score_fit",            node_score_fit)
    g.add_node("tailor_resumes",       node_tailor_resumes)
    g.add_node("review_quality",       node_review_quality)
    g.add_node("submit_applications",  node_submit_applications)
    g.add_node("monitor_email",        node_monitor_email)

    g.set_entry_point("discover_companies")

    g.add_conditional_edges("discover_companies", route_after_discovery,
                            {"scan_jobs": "scan_jobs", "__end__": END})
    g.add_conditional_edges("scan_jobs",  route_after_scan,
                            {"score_fit": "score_fit", "__end__": END})
    g.add_conditional_edges("score_fit", route_after_scoring,
                            {"tailor_resumes": "tailor_resumes", "__end__": END})
    g.add_conditional_edges("tailor_resumes", route_after_tailoring,
                            {"review_quality": "review_quality",
                             "submit_applications": "submit_applications",
                             "__end__": END})
    g.add_conditional_edges("review_quality", route_after_review,
                            {"submit_applications": "submit_applications", "__end__": END})

    g.add_edge("submit_applications", "monitor_email")
    g.add_edge("monitor_email", END)

    return g


def compile_graph():
    """Compile and return the runnable graph."""
    return build_graph().compile()
