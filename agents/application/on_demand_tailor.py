"""
Tailor-on-demand — spend Opus only on the roles the user actually engages ("the real ones").

The queue is populated cheaply (scored + resolved, no résumé). When the user opens a role's apply
form (extension) or hits "prepare" in the UI, this tailors THAT one résumé on demand: re-score
(Haiku) → Opus-tailor → quality review → attach to the application → mark ready. Idempotent: a role
already tailored is returned as-is. Respects the daily spend cap (the Opus call goes through the
budget-guarded LLM layer).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from threading import Lock

import yaml

from core.llm import get_llm
from core.logger import get_logger

logger = get_logger("on_demand_tailor")

_STATE = Path("data/pipeline_state.json")
_PROFILE = Path("data/user_profile.yaml")
_LOCK = Lock()
_IN_FLIGHT: set[str] = set()   # job_ids currently being tailored — avoids double-spend on re-trigger

# Concurrency gate: cap how many résumés tailor AT ONCE in this process. Without it, the UI's bulk
# actions ("Open all" = 10, "Prepare all" = 5) fire that many Opus tailor jobs simultaneously via
# Promise.allSettled; they then contend for Anthropic's rate limits and starve company_research
# (every call times out at its 12s cap), so a résumé that finishes in ~15s alone drags to 45-60s in
# the crowd — including the ONE the user is actively waiting on. Bounding concurrency keeps each job
# fast and lets the rest queue quickly. Tunable via TAILOR_CONCURRENCY (default 3). The Semaphore is
# created lazily inside the running loop so importing this module never needs an event loop.
_TAILOR_CONCURRENCY = max(1, int(os.getenv("TAILOR_CONCURRENCY", "3")))
_TAILOR_GATE: asyncio.Semaphore | None = None


def _tailor_gate() -> asyncio.Semaphore:
    global _TAILOR_GATE
    if _TAILOR_GATE is None:
        _TAILOR_GATE = asyncio.Semaphore(_TAILOR_CONCURRENCY)
    return _TAILOR_GATE


async def tailor_application(app_id: str = "", *, job_id: str = "",
                             provider: str | None = None, force: bool = False) -> dict:
    """Tailor a single parked application on demand. Identify by app_id or job_id.
    `provider` overrides the tailoring provider (e.g. "deepseek" for cheap batch pre-tailoring;
    None = config default, i.e. Claude on the quality tier for a user-engaged role).
    `force` RE-tailors even if a résumé already exists (used to refresh the queue after a rule
    change, e.g. the mandatory-JPMorgan-AI-bullets rule) — overwrites data/resumes/{job_id}.md.
    Returns {status, app_id, resume, quality} or {error}."""
    from agents.application.resume_tailor import tailor_one
    from agents.application.linkedin_apply_pipeline import _score_job
    from agents.application.app_quality_reviewer import review_application

    state = json.loads(_STATE.read_text())
    apps = state.get("applications", [])
    app = next((a for a in apps if (app_id and a.get("id") == app_id)
                or (job_id and a.get("job_id") == job_id)), None)
    if not app:
        return {"error": "application not found"}
    if app.get("tailored_resume_path") and not force:
        return {"status": "already_tailored", "app_id": app.get("id"),
                "resume": app.get("tailored_resume_path")}

    job = next((j for j in state.get("job_listings", []) if j.get("id") == app.get("job_id")), None)
    if not job:
        return {"error": "job listing not found"}

    jid = job.get("id")
    if jid in _IN_FLIGHT:
        return {"status": "tailoring_in_progress", "app_id": app.get("id")}
    _IN_FLIGHT.add(jid)

    profile = yaml.safe_load(_PROFILE.read_text()) if _PROFILE.exists() else {}
    # provider=None → config default (Claude quality tier); provider="deepseek" → cheap batch lane.
    # On DeepSeek, force thinking OFF: the large résumé JSON otherwise overruns max_tokens (reasoning
    # eats the budget) and truncates mid-string, AND — critically for the INTERACTIVE path a user is
    # waiting on — reasoning triples v4-pro latency (~25s vs ~8s). Resolve the EFFECTIVE provider
    # (not just the passed arg): the on-demand click sends provider=None but still lands on DeepSeek
    # via LLM_QUALITY_PROVIDER / LLM_PROVIDER, so the literal `provider == "deepseek"` check missed it.
    from core.config import get_settings
    _cfg = get_settings()
    _eff_provider = (provider or getattr(_cfg, "llm_quality_provider", "")
                     or _cfg.llm_provider or "anthropic").lower()
    _think = "off" if _eff_provider == "deepseek" else None
    llm = get_llm("on_demand_tailor", provider=provider, think=_think)
    cheap = get_llm("on_demand_tailor", model="claude-haiku-4-5", provider=provider, think=_think)

    try:
        # Serialize the LLM-heavy work behind the concurrency gate so bulk fan-out doesn't collapse
        # into all-slow contention. Acquired here (not around the whole fn) so the dedup/state I/O
        # above stays lock-free and the gate only bounds the expensive score→tailor→review span.
        async with _tailor_gate():
            fit = await _score_job(job, cheap)
            tailored = await tailor_one(job, fit, llm, profile, llm_cheap=cheap)
            if not tailored:
                return {"error": "tailoring returned nothing"}
            try:
                tailored, _ = await review_application(tailored, job, cheap)
            except Exception:
                pass
    except Exception as e:
        logger.error("on_demand_tailor_failed", app_id=app.get("id"), error=str(e)[:120])
        return {"error": str(e)[:160]}
    finally:
        _IN_FLIGHT.discard(jid)

    # Merge tailored fields into the existing app row (preserve id + job_id), then persist via the
    # cross-process-locked + atomic store (re-reads under lock — never clobbers a concurrent write).
    from core.state_store import mutate_state
    app_id_final = app.get("id")
    with mutate_state() as state:
        app = next((a for a in state.get("applications", []) if a.get("id") == app_id_final), None)
        if app is None:
            return {"error": "application vanished during tailoring"}
        # Preserve a DECIDED status: re-tailoring must never resurrect a submitted/skipped app back
        # to pending_review (that reopened already-applied roles and drove duplicate applies).
        _DECIDED = {"submitted", "skipped", "blocked", "interview", "approved", "rejected",
                    "awaiting_confirmation", "pending_manual_submit"}
        prior_status = app.get("status")
        for k, v in tailored.items():
            if k not in ("id", "job_id", "status"):   # never let the tailored dict overwrite status
                app[k] = v
        app["status"] = prior_status if prior_status in _DECIDED else "pending_review"
        app["needs_tailoring"] = False

    try:
        from db.migrate_from_json import sync_to_db_async
        await sync_to_db_async()
    except Exception:
        pass

    logger.info("on_demand_tailored", app_id=app.get("id"),
                company=app.get("company_name"), quality=app.get("quality_score"))
    return {"status": "tailored", "app_id": app.get("id"),
            "resume": app.get("tailored_resume_path"), "quality": app.get("quality_score")}


async def pretailor_batch(limit: int = 20, provider: str = "deepseek") -> dict:
    """Batch PRE-TAILOR the highest-priority un-tailored roles on the CHEAP provider (DeepSeek), so
    both lanes have résumés ready ahead of time: the hands-off W2 auto-apply lane can actually fire,
    and contract roles are ready for one-click manual apply. Picks W2 first (they feed auto-apply),
    ranked by the same fit×sponsor×recency×profile-fit priority, capped per run to bound spend.
    Returns {tailored, skipped, errors, picked}."""
    import json as _json
    from agents.discovery.targeting import rank_for_application
    from core.employment import classify_employment
    from core.config import get_settings

    state = _json.loads(_STATE.read_text())
    profile = yaml.safe_load(_PROFILE.read_text()) if _PROFILE.exists() else {}
    jobs = {j.get("id"): j for j in state.get("job_listings", [])}
    fits = {f.get("job_id"): f for f in state.get("fit_scores", [])}

    def _employ(a) -> str:
        job = jobs[a["job_id"]]
        return classify_employment(job.get("source", ""),
                                   job.get("role_title") or job.get("title", ""),
                                   job.get("description", ""))

    # Un-tailored apps only (no résumé yet).
    untailored = [a for a in state.get("applications", [])
                  if not a.get("tailored_resume_path") and a.get("job_id") in jobs]

    # Contract-only mode: never spend DeepSeek budget on the paused W2 backlog — tailor CONTRACT only.
    contract_mode = (get_settings().pipeline_mode or "fulltime").lower() == "contract"
    if contract_mode:
        untailored = [a for a in untailored if _employ(a) == "contract"]

    ranked = rank_for_application([jobs[a["job_id"]] for a in untailored], fits, profile, state)
    order = {r["job"]["id"]: i for i, r in enumerate(ranked)}

    def _sort_key(a):
        # Contract mode: pure fit-priority (all contract already). Else: W2 first (feeds auto-apply).
        w2_first = 0 if (not contract_mode and _employ(a) == "w2") else 1
        return (w2_first, order.get(a["job_id"], 10**9))

    picks = sorted(untailored, key=_sort_key)[:limit]
    tailored = errors = 0
    for a in picks:
        try:
            r = await tailor_application(a["id"], provider=provider)
            if r.get("status") == "tailored":
                tailored += 1
            elif r.get("error"):
                errors += 1
        except Exception as e:
            errors += 1
            logger.error("pretailor_item_failed", app_id=a.get("id"), error=str(e)[:120])
    logger.info("pretailor_batch_done", picked=len(picks), tailored=tailored,
                errors=errors, provider=provider)
    return {"tailored": tailored, "errors": errors, "picked": len(picks),
            "skipped": len(untailored) - len(picks)}
