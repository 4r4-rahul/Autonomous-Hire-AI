"""
Fit Analyst Agent
Scores every JobListing against the user's profile on a 0–100 scale.
Uses Claude for semantic matching + pulls past learnings from memory to
calibrate scores (rejection patterns, what worked).

Only jobs scoring >= config.MIN_FIT_SCORE (default: 65) are passed forward.
"""

import asyncio
import re
import uuid
import yaml
import json
from datetime import datetime
from pathlib import Path

from core.state import JobListing, FitScore, JobSearchState
from core.memory import Memory
from core.llm import get_llm
from core.config import get_settings
from core.logger import get_logger

logger = get_logger("fit_analyst")

_SYSTEM_PROMPT = Path("prompts/system/fit_analyst.txt").read_text()
_PROFILE_PATH = Path("data/user_profile.yaml")


def _load_profile() -> dict:
    return yaml.safe_load(_PROFILE_PATH.read_text())


def _build_profile_summary(profile: dict) -> str:
    """Condense profile into a prompt-friendly text block."""
    target = profile.get("target", {})
    skills = profile.get("skills", {})
    work = profile.get("work_history", [{}])[0]

    lines = [
        f"Candidate: {profile['personal']['name']}",
        f"Experience: {profile.get('experience_years', 6)} years",
        f"Current role: {work.get('title', '')} at {work.get('company', '')}",
        f"Location: {profile['personal']['location']} | Remote OK: yes",
        f"Target roles: {', '.join(target.get('roles', []))}",
        f"Target domains: {', '.join(target.get('domains', []))}",
        f"Salary min: ${target.get('salary_min_usd', 0):,}",
        "",
        "PRIMARY SKILLS:",
        *[f"  - {s}" for s in skills.get("primary", [])],
        "",
        "SECONDARY SKILLS:",
        *[f"  - {s}" for s in skills.get("secondary", [])],
        "",
        "MLOPS & AI SKILLS:",
        *[f"  - {s}" for s in skills.get("mlops", []) + skills.get("ai_adjacent", [])],
    ]

    # Add top work highlights
    lines.append("\nKEY EXPERIENCE:")
    for job in profile.get("work_history", [])[:2]:
        lines.append(f"  {job.get('title')} @ {job.get('company')}")
        for h in job.get("highlights", [])[:3]:
            lines.append(f"    • {h}")

    return "\n".join(lines)


def _build_job_summary(job: JobListing) -> str:
    lines = [
        f"Title: {job['title']}",
        f"Company: {job['company_name']}",
        f"Location: {job['location']} | Remote: {job['remote']}",
    ]
    if job.get("salary_min") or job.get("salary_max"):
        lines.append(f"Salary: ${job.get('salary_min', '?'):,} – ${job.get('salary_max', '?'):,}")
    if job.get("required_skills"):
        lines.append(f"Required skills: {', '.join(job['required_skills'][:15])}")
    if job.get("nice_to_have_skills"):
        lines.append(f"Nice to have: {', '.join(job['nice_to_have_skills'][:10])}")
    lines.append(f"\nJob Description:\n{job['description'][:2500]}")
    return "\n".join(lines)


_NO_SPONSORSHIP_PHRASES = [
    "unable to sponsor",
    "cannot sponsor",
    "will not sponsor",
    "does not sponsor",
    "no sponsorship",
    "not able to sponsor",
    "sponsorship is not",
    "without sponsorship",
    "no visa sponsor",
    "not provide visa",
    "citizen or permanent resident",
    "must be authorized to work",
    "must be legally authorized",
    "authorized to work without",
    "no work visa",
    "us citizens only",
    "green card",          # often paired with "must have green card"
    # Clearance / citizenship knock-outs — auto-reject for a visa-needing candidate.
    "active security clearance",
    "security clearance required",
    "ability to obtain a security clearance",
    "ts/sci",
    "secret clearance",
    "polygraph",
    "must be a u.s. citizen",
    "must be a us citizen",
    "citizenship is required",
    "u.s. citizenship required",
    # More federal-contractor phrasings (state/local govt C2C is fine; federal cleared roles are
    # dead-ends for a visa candidate — 22nd Century/Trigyn state work has none of these).
    "public trust",
    "us citizen or green card",
    "u.s. citizen or green card",
    "usc or green card",
    "usc/gc only",
    "gc/usc only",
    "must be a us person",
    "must be a u.s. person",
    "eligible for a security clearance",
    "able to obtain a security clearance",
    "clearance is required",
    "dod clearance",
]


def _requires_sponsorship_blocked(job: JobListing, profile: dict) -> tuple[bool, str]:
    """
    Returns (blocked, reason) if the candidate needs sponsorship but the job
    explicitly states they won't sponsor.
    Only runs when profile.work_authorization.requires_sponsorship is true.
    """
    work_auth = profile.get("work_authorization", {})
    if not work_auth.get("requires_sponsorship", False):
        return False, ""

    # Read the TITLE as well as the description: staffing/C2C boards put the bar in the title
    # ("Sr .Net Developer (USC/GC Only)", "DevOps Engineer - Green Card/US Citizen Only"), and a
    # SmartRecruiters listing carries no description at all — title-only was a blind spot.
    desc = f"{job.get('title') or ''}\n{job.get('description') or ''}".lower()
    for phrase in _NO_SPONSORSHIP_PHRASES:
        if phrase in desc:
            return True, f"no sponsorship offered: '{phrase}'"

    # Compact citizenship tags the prose list above can't express ("(USC/GC/TN/CTH)", "USC only").
    from core.gov_client import usc_gc_only
    bar = usc_gc_only(job.get("title") or "", job.get("description") or "")
    if bar:
        return True, f"no sponsorship offered: '{bar}'"

    return False, ""


_COMPANY_SKIP_THRESHOLD = 40   # avg fit score below this → skip company
_COMPANY_MIN_SAMPLES    = 10   # need at least this many scored jobs before auto-skipping


def _build_low_value_companies(state: "JobSearchState") -> set[str]:
    """
    Derive skip list from scored jobs in state.
    Any company with ≥ MIN_SAMPLES jobs and avg fit score < THRESHOLD is skipped.
    This grows automatically as the pipeline scores more jobs.
    """
    from collections import defaultdict
    jobs_by_id = {j["id"]: j for j in state.get("job_listings", [])}
    company_scores: dict[str, list[int]] = defaultdict(list)
    for fs in state.get("fit_scores", []):
        j = jobs_by_id.get(fs["job_id"], {})
        co = j.get("company_name")
        if co:
            company_scores[co].append(fs["score"])
    return {
        co for co, scores in company_scores.items()
        if len(scores) >= _COMPANY_MIN_SAMPLES
        and sum(scores) / len(scores) < _COMPANY_SKIP_THRESHOLD
    }

# Title substring patterns that have 0% pass rate across 10+ observed jobs.
# Each is a confirmed signal the role is a bad fit for this profile.
_EXCLUDE_TITLE_PATTERNS: list[str] = [
    # Original non-tech blocklist
    "sales", "marketing", "recruiter", "finance", "legal",
    "customer success", "account executive", "business development",
    # NOTE: "forward deployed", "solutions engineer/architect", "site reliability"/"sre" are NO
    # LONGER excluded — they're now stated TARGETS (solutions-engineering + systems-engineering
    # sides). "infrastructure engineer" also kept (AI Infra / Platform Engineer is a target). Let
    # Haiku score these rather than hard-excluding the candidate's own targets.
    " manager",           # eng managers, product managers (still excluded — IC candidate)
    "product manager",
    "security engineer",  # infosec — not a stated target
    "full-stack", "full stack",
    "front-end", "frontend",
    "java developer",
    "support engineer",
    " intern",            # internships
    "clinical",           # life sciences
    "government",
    "opendata",           # Veeva OpenData (Java-heavy)
]


# Drop roles that REQUIRE more years of experience than the candidate has (Rahul ≈ 6-7 yrs). Staff/
# Principal reqs of 8/10/12+ are a poor fit and waste an application. Overridable via
# profile target.max_required_years.
_MAX_REQUIRED_YEARS = 7
_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:to|-|–|—)?\s*(?:\d{1,2})?\s*\+?\s*years?", re.I)


def _min_required_years(desc: str) -> int | None:
    """Smallest HARD (non-'preferred') years-of-experience requirement stated in the JD, or None.
    Uses the MIN of stated hard requirements so a role with a low bar plus a high 'total' mention
    isn't wrongly dropped — only roles whose lowest required bar exceeds the ceiling are filtered."""
    d = (desc or "").lower()
    hard = []
    # Work per clause (sentence / bullet / comma boundary): a "preferred"/"a plus" qualifier
    # disqualifies its OWN clause only, so it can't downgrade a hard "X+ years required" elsewhere.
    for clause in re.split(r"[.;\n••]|,(?=\s)", d):
        if any(w in clause for w in ("preferred", "nice to have", "a plus", "bonus",
                                     "ideally", "desirable")):
            continue
        # Count only clauses that are about experience OR are explicit requirements.
        if not (any(w in clause for w in ("experience", "exp.", "background"))
                or any(w in clause for w in ("require", "minimum", "at least", "must have"))):
            continue
        for m in _YEARS_RE.finditer(clause):
            n = int(m.group(1))
            if 1 <= n <= 25:
                hard.append(n)
    return min(hard) if hard else None


def _quick_prefilter(
    job: JobListing,
    profile: dict,
    low_value_companies: set[str] | None = None,
) -> tuple[bool, str]:
    """
    Fast keyword pre-filter before spending an LLM call.
    Returns (should_score, reason_if_skipped).
    """
    title = job["title"].lower()
    desc = (job.get("description") or "").lower()

    # Skip companies with empirically low fit scores (auto-derived from history)
    if low_value_companies and job.get("company_name") in low_value_companies:
        return False, f"low-value company (avg score < {_COMPANY_SKIP_THRESHOLD}): {job['company_name']}"

    # Hard-exclude title patterns (non-tech + learned zero-pass patterns)
    if any(kw in title for kw in _EXCLUDE_TITLE_PATTERNS):
        return False, f"excluded title pattern: {job['title']}"

    # Experience ceiling — drop roles requiring more years than the candidate has (+ small buffer).
    max_years = int(profile.get("target", {}).get("max_required_years", _MAX_REQUIRED_YEARS))
    min_req = _min_required_years(job.get("description") or "")
    if min_req and min_req > max_years:
        return False, f"requires {min_req}+ yrs experience (> {max_years})"

    # Sponsorship hard block — skip before wasting an LLM call
    blocked, reason = _requires_sponsorship_blocked(job, profile)
    if blocked:
        return False, reason

    # Salary hard floor (if the job explicitly states salary below minimum)
    salary_max = job.get("salary_max") or 0
    salary_min_req = profile.get("target", {}).get("salary_min_usd", 0)
    if salary_max and salary_max < salary_min_req * 0.75:
        return False, f"salary too low: ${salary_max:,}"

    # Must match at least one primary skill keyword in title OR description
    skill_keywords = ["python", "data", "platform", "engineer", "ml", "cloud",
                      "aws", "spark", "pyspark", "kubernetes", "terraform", "llm", "ai"]
    combined = title + " " + desc[:500]
    if not any(kw in combined for kw in skill_keywords):
        return False, "no matching skill keywords"

    return True, ""


async def _score_job(
    job: JobListing,
    profile_summary: str,
    learnings: list[str],
    llm,
) -> FitScore | None:
    """Call LLM to score one job. Returns None on failure."""
    job_summary = _build_job_summary(job)

    learnings_block = ""
    if learnings:
        learnings_block = "\nPAST LEARNINGS FROM SIMILAR APPLICATIONS:\n" + "\n".join(
            f"  - {l}" for l in learnings[:5]
        )

    messages = [
        {
            "role": "user",
            "content": (
                f"CANDIDATE PROFILE:\n{profile_summary}\n\n"
                f"JOB LISTING:\n{job_summary}"
                f"{learnings_block}"
            ),
        }
    ]

    try:
        result = await llm.chat_json(_SYSTEM_PROMPT, messages, max_tokens=1024, temperature=0.1)
        if not isinstance(result, dict) or "score" not in result:
            return None

        score = int(result.get("score", 0))
        cfg = get_settings()

        return {
            "job_id": job["id"],
            "score": score,
            "reasoning": result.get("reasoning") or "",
            "match_highlights": result.get("match_highlights") or [],
            "missing_skills": result.get("missing_skills") or [],
            "passed_threshold": score >= cfg.min_fit_score,
            "scored_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.error("fit_score_failed", job_id=job["id"], title=job["title"], error=str(e))
        return None


async def run(
    state: JobSearchState,
    batch_size: int = 7,
    max_jobs: int = 1000,
) -> JobSearchState:
    """
    Score all unscored job listings in state.
    Jobs below MIN_FIT_SCORE are logged but not passed forward in fit_scores.
    """
    profile = _load_profile()
    profile_summary = _build_profile_summary(profile)
    cfg = get_settings()

    # Auto-derive which companies to skip based on historical scoring data
    low_value_companies = _build_low_value_companies(state)
    if low_value_companies:
        logger.info("low_value_companies_skipped", companies=sorted(low_value_companies))

    # Get already-scored job IDs
    scored_ids = {fs["job_id"] for fs in state.get("fit_scores", [])}

    # Priority companies: score these first regardless of position in list
    _PRIORITY_COMPANIES = {
        "Databricks", "Anthropic", "Mistral AI", "Cohere", "Groq",
        "Modal", "Anyscale", "Weights & Biases", "Hugging Face",
        "Datadog, Inc.", "MongoDB, Inc.", "Cloudflare, Inc.",
        "Fivetran", "dbt Labs", "Airbyte", "Pinecone",
        "Arize AI", "WhyLabs", "Tecton", "Feast",
        "Cursor", "Replit", "Scale AI",
    }

    all_listings = state.get("job_listings", [])
    unscored = [j for j in all_listings if j["id"] not in scored_ids]

    # Partition: priority companies first, then the rest (preserving order within each)
    priority = [j for j in unscored if j["company_name"] in _PRIORITY_COMPANIES]
    rest = [j for j in unscored if j["company_name"] not in _PRIORITY_COMPANIES]
    jobs_to_score = (priority + rest)[:max_jobs]

    logger.info(
        "fit_analyst_start",
        total_jobs=len(state.get("job_listings", [])),
        to_score=len(jobs_to_score),
        threshold=cfg.min_fit_score,
    )

    # Pull relevant learnings from memory to calibrate scoring
    memory = Memory.get()
    learnings = memory.get_relevant_learnings(
        context="job application rejection success patterns software engineer",
        n=5,
    )

    llm = get_llm("fit_analyst", model="claude-haiku-4-5")
    new_fit_scores: list[FitScore] = []
    passed = 0
    filtered = 0
    prefiltered = 0

    # Process in batches
    for i in range(0, len(jobs_to_score), batch_size):
        batch = jobs_to_score[i:i + batch_size]

        # Pre-filter within batch
        to_score = []
        for job in batch:
            ok, reason = _quick_prefilter(job, profile, low_value_companies)
            if not ok:
                prefiltered += 1
                logger.info("job_prefiltered", title=job["title"], reason=reason)
                continue
            to_score.append(job)

        if not to_score:
            continue

        # Score the batch concurrently
        tasks = [_score_job(job, profile_summary, learnings, llm) for job in to_score]
        results = await asyncio.gather(*tasks)

        for job, fit_score in zip(to_score, results):
            if fit_score is None:
                continue

            new_fit_scores.append(fit_score)

            if fit_score["passed_threshold"]:
                passed += 1
                logger.log_decision(
                    action="job_scored",
                    outcome="passed",
                    details={
                        "title": job["title"],
                        "company": job["company_name"],
                        "score": fit_score["score"],
                        "highlights": fit_score["match_highlights"][:2],
                    },
                )
            else:
                filtered += 1
                logger.info(
                    "job_below_threshold",
                    title=job["title"],
                    company=job["company_name"],
                    score=fit_score["score"],
                    missing=fit_score["missing_skills"][:2],
                )

        # Persist passing scores to memory
        for fit_score in new_fit_scores:
            if fit_score["passed_threshold"]:
                memory.upsert(
                    "applications",
                    fit_score["job_id"],
                    fit_score["reasoning"],
                    {
                        "score": fit_score["score"],
                        "passed": True,
                        "scored_at": fit_score["scored_at"],
                    },
                )

        # Small pause between batches
        await asyncio.sleep(0.5)

    logger.log_decision(
        action="fit_analysis_complete",
        outcome="success",
        details={
            "scored": len(new_fit_scores),
            "prefiltered": prefiltered,
            "passed_threshold": passed,
            "filtered_out": filtered,
            "pass_rate_pct": round(passed / max(len(new_fit_scores), 1) * 100, 1),
        },
    )

    updated = dict(state)
    updated["fit_scores"] = state.get("fit_scores", []) + new_fit_scores
    return updated


def get_passing_jobs(state: JobSearchState) -> list[tuple[JobListing, FitScore]]:
    """
    Convenience: return (job, fit_score) pairs that passed the threshold,
    sorted by score descending. Used by resume_tailor and form_filler.
    """
    cfg = get_settings()
    passing_scores = {
        fs["job_id"]: fs
        for fs in state.get("fit_scores", [])
        if fs["passed_threshold"]
    }
    jobs_by_id = {j["id"]: j for j in state.get("job_listings", [])}

    pairs = [
        (jobs_by_id[job_id], fit_score)
        for job_id, fit_score in passing_scores.items()
        if job_id in jobs_by_id
    ]
    sorted_pairs = sorted(pairs, key=lambda x: x[1]["score"], reverse=True)

    # Deduplicate: keep only the highest-scoring entry per (company, normalized_title)
    seen: set[tuple[str, str]] = set()
    deduped: list[tuple] = []
    for job, fs in sorted_pairs:
        key = (job["company_name"], job["title"].lower().strip())
        if key not in seen:
            seen.add(key)
            deduped.append((job, fs))

    # Cap at 3 passing jobs per company to prevent score concentration
    from collections import defaultdict
    company_counts: dict[str, int] = defaultdict(int)
    capped: list[tuple] = []
    for job, fs in deduped:
        if company_counts[job["company_name"].lower()] < 3:
            capped.append((job, fs))
            company_counts[job["company_name"].lower()] += 1

    return capped


# ── CLI smoke test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    async def _test():
        # Pull real jobs from job_scanner first
        from agents.discovery.job_scanner import run as scan_jobs
        from core.state import Company

        test_companies: list[Company] = [
            {
                "id": str(uuid.uuid4()), "name": "Datadog",
                "website": "https://www.datadoghq.com", "linkedin_url": "",
                "size": "mid", "domain": "Developer Tools", "tech_stack": [],
                "open_role_count": 0, "discovered_at": datetime.utcnow().isoformat(),
            },
        ]
        base_state: JobSearchState = {
            "companies": test_companies, "job_listings": [], "fit_scores": [],
            "applications": [], "pending_approvals": [], "recruiter_contacts": [],
            "email_events": [], "meetings_scheduled": [],
            "weekly_quality_score": 0.0, "weekly_quantity_count": 0,
            "response_rate": 0.0, "interview_rate": 0.0,
            "last_run_at": None, "errors": [], "agent_statuses": {},
        }

        print("Scanning jobs...")
        scanned = await scan_jobs(base_state, max_companies=1, batch_size=1)
        print(f"Found {len(scanned['job_listings'])} job listings")

        print("\nScoring fit (first 20 jobs)...")
        # Only score first 20 to keep test fast
        scanned["job_listings"] = scanned["job_listings"][:20]
        result = await run(scanned, batch_size=5)

        print(f"\n{'─'*70}")
        print(f"Scored: {len(result['fit_scores'])}  |  "
              f"Passed: {sum(1 for fs in result['fit_scores'] if fs['passed_threshold'])}")
        print(f"{'─'*70}")

        passing = get_passing_jobs(result)
        print(f"\nTop matches:")
        for job, fs in passing[:10]:
            print(f"\n  [{fs['score']:3d}] {job['title']} @ {job['company_name']}")
            print(f"        {fs['reasoning'][:120]}...")
            if fs["match_highlights"]:
                print(f"        ✓ {', '.join(fs['match_highlights'][:2])}")
            if fs["missing_skills"]:
                print(f"        ✗ Missing: {', '.join(fs['missing_skills'][:2])}")

    asyncio.run(_test())
