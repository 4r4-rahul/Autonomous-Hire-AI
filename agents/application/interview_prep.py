"""
Interview-prep generator.

When an application flips to "interview" (the email monitor sets that from a first-party /
company-ATS interview invite), this turns the role into a concrete, TRUTHFUL prep brief
grounded in the SAME experience bank the résumé / cover letter / autofill draw from
(agents/application/resume_tailor.py), plus the hand-written scope notes under docs/. The brief
is written to data/interview_prep/<company>_<role>_prep.md and its path is recorded on a
MeetingScheduled record (wiring the previously-orphan prep_doc_path field), so the dashboard /
digest can surface "prep ready" alongside the interview.

Two entry points:
  • generate_prep(job, profile)      — produce ONE prep doc, return its path.
  • prep_new_interviews(state)       — find apps in "interview" status WITHOUT a prep doc, generate
                                       one each, and upsert their MeetingScheduled.prep_doc_path.
                                       Idempotent (skips when the prep file already exists), mutates
                                       the passed-in state in place, returns the count generated.

The batch hook is wired into the email-monitor scheduler job (orchestration/scheduler.py) so a new
interview automatically gets a prep doc; scripts/generate_interview_prep.py runs it on demand.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from core.llm import get_llm
from core.logger import get_logger

logger = get_logger("interview_prep")

_PREP_DIR = Path("data/interview_prep")
_PROFILE_PATH = Path("data/user_profile.yaml")
# Optional hand-written scope/context notes folded in as extra, human-authored grounding.
_SCOPE_DOCS = [Path("docs/chase_scope_context.md")]

_INTERVIEW_STATUSES = {"interview"}


_SYSTEM = """You are an interview coach preparing a specific candidate for a specific interview.
You write a tight, honest, immediately-usable prep brief — not generic advice.

HARD RULES:
- TRUTH ONLY. Every claim, story, number, and skill must come from the CANDIDATE BRIEF, the REAL
  EXPERIENCE block, or the SCOPE NOTES. Never invent an employer, a metric, a project, or a skill
  the candidate doesn't have. A plain true answer beats an impressive fabricated one.
- NO INVENTED NUMBERS. Do not add any volume / throughput / latency / cost / team-size / percentage
  figure unless it appears verbatim in the inputs. Never write things like "processes millions of
  records daily" or "reduced latency by X%" if that number isn't given — say it qualitatively
  instead. The candidate has to defend every number out loud in the room.
- Production work vs. personal projects: REAL EXPERIENCE lines are production work UNLESS a line
  says personal/side project. Keep that distinction in any answer you draft.
- Ground behavioural answers in the REAL EXPERIENCE stories (problem → what they did → impact),
  spoken in the FIRST PERSON, 2-4 sentences each. Don't read the YAML back; turn it into speech.
- Match the role: pull the technical topics, behavioural themes, and "why this company" angle from
  the JOB DESCRIPTION. If the JD needs something the candidate is light on, say so honestly in the
  GAPS section with a truthful way to address it — never paper over it.

OUTPUT — GitHub-flavoured Markdown, exactly these sections, in order:
# Interview Prep — <Role> @ <Company>
## Snapshot
2-3 sentences: what this team most likely wants and where the candidate is strong for it.
## Likely questions (with your answers)
6-8 questions the JD makes likely (mix technical + behavioural). Under each, a first-person answer
grounded in real experience. Bold the question.
## Your strongest stories
3-5 STAR talking points drawn from REAL EXPERIENCE, spoken form (Situation → what I did → result).
## Scope / team / scale
Crisp answers for "describe your role / your team / the scale you worked at", from the SCOPE NOTES
and experience. Skip this section only if there is genuinely no scope material.
## Gaps & risks
What the JD wants that the candidate is light on, and an honest way to handle each in the room.
## Questions to ask them
4-5 sharp, role-specific questions that show the candidate did the work.

Keep it concrete and skimmable. No preamble, no sign-off — start with the H1."""


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s or "role"


# Vague-magnitude volume claims ("processes millions of records daily") are the model's signature
# fabrication here — real grounded numbers in the bank are specific integers (587/1812 commits,
# 4 accounts, 23 contributors), never "millions of X". The prompt forbids these, but the model still
# slips them in, so we deterministically strip any sentence carrying one. For an interview brief a
# fabricated number the candidate might repeat out loud is the worst failure mode.
# vague-magnitude fabrication scrub — shared single source of truth (core/text_scrub)
from core.text_scrub import scrub_fabricated_numbers as _scrub_fabricated_numbers


def _load_profile() -> dict:
    try:
        return yaml.safe_load(_PROFILE_PATH.read_text()) or {}
    except Exception:
        return {}


def _candidate_brief(P: dict) -> str:
    """Compact, factual summary of the candidate for grounding — no fabrication. Mirrors the brief
    the autofill flow uses, kept local so this module doesn't depend on the FastAPI api package."""
    per = P.get("personal", {}) or {}
    defs = P.get("application_defaults", {}) or {}
    sk = P.get("skills", {}) or {}
    tgt = P.get("target", {}) or {}
    lines = [
        f"Name: {per.get('name', '')}",
        f"Current role: {defs.get('current_title', '')} at {defs.get('current_company', '')}",
        f"Years of experience: {P.get('experience_years', defs.get('years_of_experience', ''))}",
        f"Core skills: {', '.join(sk.get('primary', [])[:10])}",
        f"AI/ML systems: {', '.join(sk.get('ai_systems', [])[:8])}",
        f"MLOps/Platform: {', '.join(sk.get('mlops', [])[:8])}",
        f"Target domains: {', '.join(tgt.get('domains', [])[:6])}",
    ]
    for job in (P.get("work_history", []) or [])[:2]:
        if isinstance(job, dict):
            title = job.get("title") or job.get("role") or ""
            comp = job.get("company") or ""
            highs = job.get("highlights") or job.get("achievements") or []
            h = highs[0] if isinstance(highs, list) and highs else (job.get("summary") or "")
            if title or comp:
                lines.append(f"Experience: {title} @ {comp} — {str(h)[:160]}")
    return "\n".join(l for l in lines if l.rsplit(": ", 1)[-1].strip())


def _scope_notes(limit: int = 2500) -> str:
    """Concatenate any present hand-written scope/context docs (e.g. docs/chase_scope_context.md)."""
    parts: list[str] = []
    for p in _SCOPE_DOCS:
        try:
            if p.exists():
                parts.append(p.read_text())
        except Exception:
            continue
    return "\n\n".join(parts)[:limit]


async def generate_prep(job: dict, profile: dict | None = None) -> str:
    """Produce one interview-prep brief for `job` ({company,title,description}) and write it under
    data/interview_prep/. Returns the file path (str). Raises on LLM failure — callers decide."""
    P = profile if profile is not None else _load_profile()
    company = (job.get("company") or job.get("company_name") or "Company").strip()
    title = (job.get("title") or job.get("role_title") or "Role").strip()
    jd = (job.get("description") or "")[:2500]

    # Same JD-matched, git-verified stories the résumé/cover/autofill use.
    bank = ""
    try:
        from agents.application.resume_tailor import _select_bank_stories
        bank = (_select_bank_stories({"title": title, "description": jd}, []) or "")[:2400]
    except Exception:
        bank = ""

    brief = _candidate_brief(P)
    scope = _scope_notes()

    user = (
        f"CANDIDATE BRIEF:\n{brief}\n\n"
        f"REAL EXPERIENCE (production unless a line says personal/side project — draw concrete "
        f"specifics from here, do NOT inflate):\n{bank or '(use the brief above)'}\n\n"
        f"SCOPE NOTES (hand-written context on team / ownership / scale):\n{scope or '(none)'}\n\n"
        f"COMPANY: {company}\nROLE: {title}\n"
        f"JOB DESCRIPTION:\n{jd or '(not available)'}\n\n"
        f"Write the prep brief for THIS interview now."
    )

    llm = get_llm("interview_prep", model="claude-opus-4-8")
    md = await llm.chat(system=_SYSTEM, messages=[{"role": "user", "content": user}], max_tokens=4096)

    md, scrubbed = _scrub_fabricated_numbers((md or "").strip())
    if scrubbed:
        logger.info("interview_prep_scrubbed_fabrication", company=company, role=title, sentences=scrubbed)

    _PREP_DIR.mkdir(parents=True, exist_ok=True)
    path = _PREP_DIR / f"{_slug(company)}_{_slug(title)}_prep.md"
    path.write_text(md + "\n")
    logger.info("interview_prep_generated", company=company, role=title, path=str(path))
    return str(path)


def _existing_prep_path(company: str, title: str) -> Path:
    return _PREP_DIR / f"{_slug(company)}_{_slug(title)}_prep.md"


def _meeting_has_prep(state: dict, company: str, prep_path: str) -> bool:
    for m in state.get("meetings_scheduled", []) or []:
        if (m.get("company_name") or "").strip().lower() == company.strip().lower() \
                and m.get("prep_doc_path") == prep_path:
            return True
    return False


def _job_for_app(state: dict, app: dict) -> dict:
    """Build the {company,title,description} the generator needs, preferring the full job_listing
    (it has the JD) and falling back to the application's own fields."""
    jid = app.get("job_id")
    listing = next((j for j in state.get("job_listings", []) or [] if j.get("id") == jid), None)
    return {
        "company": app.get("company_name") or (listing or {}).get("company_name") or "Company",
        "title": app.get("role_title") or (listing or {}).get("title") or "Role",
        "description": (listing or {}).get("description") or "",
    }


async def prep_new_interviews(state: dict) -> int:
    """For every application in 'interview' status that doesn't already have a prep doc, generate one
    and upsert its MeetingScheduled.prep_doc_path. Mutates `state` in place (caller saves), idempotent
    (skips when the prep file already exists), never raises. Returns the number generated."""
    profile = _load_profile()
    state.setdefault("meetings_scheduled", [])
    generated = 0
    for app in state.get("applications", []) or []:
        if app.get("status") not in _INTERVIEW_STATUSES:
            continue
        company = app.get("company_name") or "Company"
        title = app.get("role_title") or "Role"
        prep_path = str(_existing_prep_path(company, title))
        # Idempotent: skip if the brief already exists on disk and a meeting already points to it.
        if Path(prep_path).exists() and _meeting_has_prep(state, company, prep_path):
            continue
        try:
            if not Path(prep_path).exists():
                prep_path = await generate_prep(_job_for_app(state, app), profile)
                generated += 1
        except Exception as e:
            logger.warning("interview_prep_failed", company=company, role=title, error=str(e)[:140])
            continue
        # Upsert the meeting record so the path is wired even if generation was skipped-but-missing.
        existing = next((m for m in state["meetings_scheduled"]
                         if (m.get("company_name") or "").strip().lower() == company.strip().lower()
                         and not m.get("prep_doc_path")), None)
        if existing:
            existing["prep_doc_path"] = prep_path
        else:
            state["meetings_scheduled"].append({
                "id": str(uuid.uuid4()),
                "company_name": company,
                "interviewer_name": "",
                "proposed_times": [],
                "confirmed_time": None,
                "calendar_event_id": None,
                "status": "proposed",
                "prep_doc_path": prep_path,
            })
    if generated:
        logger.info("interview_prep_batch_done", generated=generated)
    return generated
