"""
Resume Tailor Agent
For each job that passed fit_analyst, produces a tailored resume by:

  1. Researching the company's actual tech stack, cloud provider, AI initiatives
  2. Extracting exact ATS keywords from the job description
  3. Rewriting resume bullets as value stories (impact + context, not task lists)
  4. Injecting relevant AI skills from the candidate's ai_adjacent profile
  5. Matching cloud depth to what the company actually uses

Output: tailored .md resume saved to data/resumes/{job_id}.md
"""

import asyncio
import json
import re
import uuid
import yaml
from pathlib import Path
from datetime import datetime

from core.state import JobListing, FitScore, Application, JobSearchState
from core.memory import Memory
from core.llm import get_llm
from core.logger import get_logger
from tools.search import get_search_client

logger = get_logger("resume_tailor")

_SYSTEM_PROMPT    = Path("prompts/system/resume_tailor.txt").read_text()
_BASE_RESUME      = Path("data/resume_base.md").read_text()
_PROFILE_PATH     = Path("data/user_profile.yaml")
_RESUME_DIR       = Path("data/resumes")
_RESUME_DIR.mkdir(parents=True, exist_ok=True)
_COVER_DIR        = Path("data/cover_letters")
_COVER_DIR.mkdir(parents=True, exist_ok=True)


async def _cover_letter(job: dict, profile: dict, llm) -> str:
    """A short, truthful, role-matched cover letter (Haiku). Proactively frames work authorization
    — for a sponsorship-needing candidate, surfacing 'US-authorized, needs H1B transfer' up front
    preempts the silent auto-reject and is a genuine callback lever. Never fabricates."""
    wa = profile.get("work_authorization", {}) or {}
    pers = profile.get("personal", {}) or {}
    name = pers.get("name") or pers.get("full_name") or "the candidate"
    summary = (profile.get("summary") or "")[:600]
    skills = ", ".join((profile.get("skills", {}) or {}).get("primary", [])[:8]) \
        if isinstance(profile.get("skills"), dict) else ""
    sponsor_line = ("I am authorized to work in the U.S. and will require H1B sponsorship/transfer."
                    if wa.get("requires_sponsorship") else "I am authorized to work in the U.S.")
    system = (
        "Write a concise, professional cover letter (120-170 words, 3 short paragraphs). "
        "Truthful — use ONLY the candidate facts provided; never invent experience, employers, or "
        "metrics. Plain text, no markdown headers. Tone: direct, senior, specific to the role. "
        "Include exactly one sentence stating work authorization verbatim as given.")
    user = (
        f"Candidate: {name}\nBackground: {summary}\nCore skills: {skills}\n"
        f"Work authorization (use this sentence): {sponsor_line}\n\n"
        f"Role: {job.get('title')} at {job.get('company_name')}\n"
        f"Job description (for matching only):\n{(job.get('description') or '')[:1500]}\n\n"
        "Write the cover letter now.")
    return await llm.chat(system, [{"role": "user", "content": user}], max_tokens=500, temperature=0.4)


def _load_profile() -> dict:
    return yaml.safe_load(_PROFILE_PATH.read_text())


# ── Step 1: Company Research ───────────────────────────────────────────────

_RESEARCH_SYSTEM = """You are a technical research assistant.
Given search results about a company, extract:
1. Their primary cloud provider (AWS / Azure / GCP / Multi-cloud)
2. Key technologies in their stack (languages, frameworks, data tools, ML tools)
3. Any known AI/ML initiatives or products
4. Engineering culture signals (open source, scale, regulated industry, etc.)

Return JSON:
{
  "cloud_primary": "AWS|Azure|GCP|Multi-cloud|Unknown",
  "tech_stack": ["list of technologies"],
  "ai_initiatives": ["known AI/ML products or projects"],
  "culture_signals": ["scale, regulated, open-source, startup-speed, etc."],
  "summary": "2-3 sentence company tech profile"
}
Only return valid JSON."""

_research_cache: dict[str, dict] = {}  # company_name → research result (per process)


async def _research_company(company_name: str, llm) -> dict:
    """Search for the company's tech stack and engineering culture."""
    if company_name in _research_cache:
        return _research_cache[company_name]

    search = get_search_client()

    queries = [
        f"{company_name} engineering tech stack cloud AWS Azure 2024 2025",
        f"{company_name} engineering blog data platform machine learning AI",
        f"site:linkedin.com/jobs {company_name} senior engineer requirements",
    ]

    snippets: list[str] = []
    for q in queries:
        results = await search.search(q, max_results=3)
        for r in results:
            snippets.append(f"[{r.get('title','')}]\n{r.get('content','')[:400]}")
        await asyncio.sleep(0.5)

    if not snippets:
        return {"cloud_primary": "Unknown", "tech_stack": [], "ai_initiatives": [],
                "culture_signals": [], "summary": ""}

    messages = [{
        "role": "user",
        "content": f"Company: {company_name}\n\nSearch results:\n\n" + "\n\n".join(snippets[:6])
    }]

    try:
        result = await llm.chat_json(_RESEARCH_SYSTEM, messages, max_tokens=512, temperature=0.1)
        logger.info("company_researched", company=company_name,
                    cloud=result.get("cloud_primary"), stack_count=len(result.get("tech_stack", [])))
        _research_cache[company_name] = result
        return result
    except Exception as e:
        logger.warning("company_research_failed", company=company_name, error=str(e))
        fallback = {"cloud_primary": "Unknown", "tech_stack": [], "ai_initiatives": [],
                    "culture_signals": [], "summary": ""}
        _research_cache[company_name] = fallback
        return fallback


# ── Step 2: ATS Keyword Extraction ────────────────────────────────────────

_KEYWORD_SYSTEM = """Extract ATS keywords from a job description.
Return JSON array of exact phrases the ATS and hiring manager will search for.
Include: technical skills, tools, methodologies, domain terms, seniority signals.
Prioritize phrases that appear 2+ times or are in bold/requirements sections.
Return only the JSON array, no explanation.
Example: ["Python", "distributed systems", "Kubernetes", "data pipeline", "MLOps"]"""

async def _extract_ats_keywords(jd_text: str, llm) -> list[str]:
    messages = [{"role": "user", "content": f"Job description:\n{jd_text[:3000]}"}]
    try:
        result = await llm.chat_json(_KEYWORD_SYSTEM, messages, max_tokens=512, temperature=0.1)
        return result if isinstance(result, list) else []
    except Exception:
        return []


# ── Step 3: Tailoring ─────────────────────────────────────────────────────

async def _tailor_resume(
    job: JobListing,
    fit_score: FitScore,
    company_research: dict,
    ats_keywords: list[str],
    profile: dict,
    llm,
) -> dict | None:
    """Call LLM to produce the tailored resume JSON."""

    ai_skills = (
        profile.get("skills", {}).get("ai_adjacent", []) +
        profile.get("skills", {}).get("mlops", [])
    )

    # STABLE block — identical on every tailoring call (base résumé + the candidate's static
    # skills). Marked cache_control:ephemeral so calls 2..N in a batch read it from cache
    # ($0.50/Mtok) instead of paying full input ($5/Mtok on Opus). Zero quality impact —
    # same content, just a cache breakpoint. (System prompt is already cached in core/llm.)
    stable_block = f"""BASE RESUME:
{_BASE_RESUME}

CANDIDATE AI/ML SKILLS TO DRAW FROM (use aggressively for AI roles):
{json.dumps(ai_skills)}

CANDIDATE AI SYSTEMS SKILLS (production AI patterns):
{json.dumps(profile.get('skills', {}).get('ai_systems', []))}""".strip()

    # VARIABLE block — changes per job, never cached.
    variable_block = f"""JOB TITLE: {job['title']}
COMPANY: {job['company_name']}
LOCATION: {job['location']} | Remote: {job['remote']}

JOB DESCRIPTION:
{job['description'][:3500]}

COMPANY RESEARCH:
- Cloud: {company_research.get('cloud_primary', 'Unknown')}
- Stack: {', '.join(company_research.get('tech_stack', [])[:15])}
- AI initiatives: {', '.join(company_research.get('ai_initiatives', [])[:5])}
- Culture: {', '.join(company_research.get('culture_signals', [])[:5])}
- Summary: {company_research.get('summary', '')}

ATS KEYWORDS TO INJECT:
{json.dumps(ats_keywords[:30])}

FIT SCORE: {fit_score['score']}/100
MATCH HIGHLIGHTS: {json.dumps(fit_score.get('highlights', fit_score.get('match_highlights', [])))}
GAPS TO ADDRESS: {json.dumps(fit_score.get('missing_keywords', fit_score.get('missing_skills', [])))}

IS AI ROLE: {any(kw in job['title'].lower() for kw in ['ai', 'ml', 'llm', 'genai', 'machine learning', 'mlops', 'platform'])}
If IS AI ROLE is true: projects section MUST appear before professional experience in the output JSON (add "projects_first": true).""".strip()

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": stable_block, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": variable_block},
        ],
    }]

    try:
        result = await llm.chat_json(
            _SYSTEM_PROMPT, messages,
            max_tokens=4096, temperature=0.2,
        )
        return result if isinstance(result, dict) else None
    except Exception as e:
        logger.error("resume_tailoring_failed", job_id=job["id"], error=str(e))
        return None


# ── Step 4: Render to Markdown ─────────────────────────────────────────────

def _is_ai_role(job: JobListing, tailored: dict) -> bool:
    ai_keywords = ["ai", "ml", "llm", "genai", "machine learning", "mlops", "platform"]
    title = job.get("title", "").lower()
    return tailored.get("projects_first", False) or any(kw in title for kw in ai_keywords)


def _render_projects_block(tailored: dict) -> list[str]:
    lines = ["## AI PROJECTS", ""]
    for proj in tailored.get("projects", []):
        lines += [f"### {proj.get('name', '')}", proj.get("description", ""), ""]
    return lines


def _render_experience_block(tailored: dict) -> list[str]:
    lines = ["## PROFESSIONAL EXPERIENCE", ""]
    for exp in tailored.get("experience", []):
        lines += [
            f"### {exp.get('company', '')} — {exp.get('title', '')}",
            f"**{exp.get('period', '')}**",
            "",
        ]
        for bullet in exp.get("bullets", []):
            lines.append(f"- {bullet}")
        lines.append("")
    return lines


def _disp_url(u: str) -> str:
    """Readable form of a URL for visible (ATS-parseable) link text."""
    return (u or "").replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/")


def _clean_md(s: str) -> str:
    """Normalize the saved resume markdown to clean, human-looking plain text — removes
    the typographic symbols that read as machine-generated (middle dots, bullets,
    em-dashes, arrows, smart quotes) without disturbing markdown structure or newlines."""
    if not s:
        return s
    s = re.sub(r"[ \t]*[·•‧∙▪●‣⁃⁌]+[ \t]*", ", ", s)
    s = re.sub(r"[ \t]*—[ \t]*", " - ", s)        # em dash -> spaced hyphen
    s = s.replace("–", "-")                         # en dash -> hyphen
    for a, b in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
                 ("…", "..."), ("→", " to "), (" ", " "), ("​", "")):
        s = s.replace(a, b)
    s = re.sub(r"[ \t]{2,}", " ", s)                    # collapse runs of spaces
    s = re.sub(r"[ \t]+([,.;:])", r"\1", s)             # no space before punctuation
    s = re.sub(r",[ \t]*,", ", ", s)                    # no double commas
    s = re.sub(r"\n{3,}", "\n\n", s)                    # no big blank gaps
    return s


def _render_markdown(tailored: dict, job: JobListing, profile: dict) -> str:
    p = profile.get("personal", {})
    ai_role = _is_ai_role(job, tailored)

    # Deterministic contact header — built verbatim from the profile (never LLM-written),
    # with the readable URL as the visible link text so both the PDF and ATS capture it.
    _li, _gh = p.get("linkedin_url", ""), p.get("github_url", "")
    _contact = " | ".join(
        f"[{_disp_url(u)}]({u})" for u in (_li, _gh) if u)

    lines = [
        f"# {p.get('name', 'Rahul Varikuppala')}",
        f"{p.get('location', '')} | {p.get('phone', '')} | {p.get('email', '')}",
        _contact,
        "",
        "---",
        "",
        "## SUMMARY",
        "",
        tailored.get("summary", ""),
        "",
        "---",
        "",
        "## CORE SKILLS",
        "",
        tailored.get("skills_section", ""),
        "",
        "---",
        "",
    ]

    # For AI roles: projects first — leads with strongest signal for AI hiring managers
    if ai_role:
        lines += _render_projects_block(tailored)
        lines += ["---", ""]
        lines += _render_experience_block(tailored)
    else:
        lines += _render_experience_block(tailored)
        lines += ["---", ""]
        lines += _render_projects_block(tailored)

    edu = profile.get("education", [])
    if edu:
        lines += ["---", "", "## EDUCATION", ""]
        for e in edu:
            lines.append(f"**{e.get('degree', '')}** — {e.get('school', '')}"
                         + (f" | GPA: {e.get('gpa', '')}" if e.get("gpa") else ""))
        lines.append("")

    # Tailor metadata footer (hidden — for dashboard only)
    lines += [
        "",
        "<!-- TAILOR_META",
        f"job_id: {job['id']}",
        f"company: {job['company_name']}",
        f"role: {job['title']}",
        f"ats_keywords: {', '.join(tailored.get('ats_keywords_used', [])[:10])}",
        f"cloud_emphasis: {tailored.get('cloud_emphasis', '')}",
        f"tailoring_notes: {tailored.get('tailoring_notes', '')}",
        "-->",
    ]

    return "\n".join(lines)


# ── Step 5: Deterministic ATS keyword injection ────────────────────────────

_KW_STOP = {"and", "the", "for", "with", "from", "into", "your", "you", "our", "their", "a", "an",
            "of", "to", "in", "on", "at", "or", "as", "by", "is", "are", "be", "experience",
            "strong", "ability", "knowledge", "understanding", "using", "across", "including"}


def _profile_text(profile: dict) -> str:
    """All free-text in the candidate profile, lowercased — the ground truth for what they have."""
    parts: list[str] = []

    def walk(d):
        if isinstance(d, dict):
            for v in d.values():
                walk(v)
        elif isinstance(d, list):
            for v in d:
                walk(v)
        elif isinstance(d, str):
            parts.append(d)
    walk(profile)
    return " ".join(parts).lower()


def _ats_coverage(md: str, ats_keywords: list[str]) -> float:
    """Deterministic % of extracted ATS keywords actually present in the résumé text. ATS rank on
    literal term coverage, so this is a cheap, measured proxy for passing the keyword filter."""
    if not ats_keywords:
        return 1.0
    low = (md or "").lower()
    hit = sum(1 for kw in ats_keywords if (kw or "").lower() in low)
    return round(hit / len(ats_keywords), 3)


def _inject_missing_keywords(md: str, ats_keywords: list[str], profile: dict) -> str:
    """Append ONLY ATS keywords the candidate GENUINELY has (grounded in their profile) that the
    tailored résumé happens to omit. Never paste raw JD phrases the candidate can't back up —
    that's keyword-stuffing/fabrication and trips ATS down-ranking + recruiter rejection.
    """
    if not ats_keywords:
        return md

    md_lower = md.lower()
    ptext = _profile_text(profile)

    def supported(kw: str) -> bool:
        k = (kw or "").lower().strip()
        if len(k) < 3:
            return False
        if k in ptext:
            return True
        toks = [t for t in re.findall(r"[a-z0-9+#.]+", k) if len(t) > 2 and t not in _KW_STOP]
        if not toks:
            return False
        hits = sum(1 for t in toks if t in ptext)
        return hits / len(toks) >= 0.8   # nearly every meaningful token is in the profile

    missing = [kw for kw in ats_keywords if kw.lower() not in md_lower and supported(kw)]
    missing = list(dict.fromkeys(missing))[:8]   # dedup, cap
    if not missing:
        return md

    injection = " · ".join(missing)
    skills_marker = "## CORE SKILLS"
    sep_marker = "\n---"
    if skills_marker in md:
        insert_pos = md.find(sep_marker, md.find(skills_marker))
        if insert_pos != -1:
            # Folded into the real skills section (these are profile-true), not a JD dump footer.
            return md[:insert_pos] + f"\n\nAlso: {injection}" + md[insert_pos:]

    return md


# ── Main entry point ───────────────────────────────────────────────────────

async def tailor_one(
    job: JobListing,
    fit_score: FitScore,
    llm,
    profile: dict,
    llm_cheap=None,
) -> Application | None:
    """
    Tailor resume for a single job. Returns an Application record.
    llm_cheap: Haiku instance for research + keyword extraction (cheap tasks).
               Defaults to llm if not provided.
    """
    if llm_cheap is None:
        llm_cheap = llm

    logger.info("tailoring_start", title=job["title"], company=job["company_name"],
                score=fit_score["score"])

    # Cheap tasks (Haiku): company research + ATS keyword extraction run in parallel.
    # Research does web searches that can hang/time-out (a flaky mojeek call once burned ~50s
    # on 3 LLM retries) — hard-bound it to 12s and fall back to a minimal dict. The JD + profile
    # already carry the bulk of the signal, so a missed research barely affects quality.
    async def _research_timed():
        try:
            return await asyncio.wait_for(_research_company(job["company_name"], llm_cheap), timeout=12)
        except Exception as e:
            logger.warning("company_research_timeout", company=job["company_name"], error=str(e)[:60])
            return {"cloud_primary": "Unknown", "tech_stack": [], "ai_initiatives": [],
                    "culture_signals": [], "summary": ""}

    company_research, ats_keywords = await asyncio.gather(
        _research_timed(),
        _extract_ats_keywords(job["description"], llm_cheap),
    )

    # Expensive task (Sonnet): actual resume bullet rewrites
    tailored = await _tailor_resume(job, fit_score, company_research, ats_keywords, profile, llm)
    if not tailored:
        return None

    # Render to markdown, inject any ATS keywords the LLM missed, then save
    md = _render_markdown(tailored, job, profile)
    md = _inject_missing_keywords(md, ats_keywords, profile)
    md = _clean_md(md)   # strip LLM-ish typographic symbols → clean, human plain text
    resume_path = _RESUME_DIR / f"{job['id']}.md"
    resume_path.write_text(md, encoding="utf-8")

    # Cover letter (cheap, Haiku) — only when configured 'auto'. Proactively frames work
    # authorization (a callback lever for a sponsorship-needing candidate) and the role fit.
    cover_path = None
    if (profile.get("application_defaults", {}) or {}).get("cover_letter") == "auto":
        try:
            cover_md = await _cover_letter(job, profile, llm_cheap)
            if cover_md:
                cp = _COVER_DIR / f"{job['id']}.md"
                cp.write_text(_clean_md(cover_md), encoding="utf-8")
                cover_path = str(cp)
        except Exception as e:
            logger.warning("cover_letter_failed", company=job.get("company_name"), error=str(e)[:90])

    ats_coverage = _ats_coverage(md, ats_keywords)   # measured JD-keyword coverage of the résumé
    logger.log_decision(
        action="resume_tailored",
        outcome="saved",
        details={
            "title": job["title"],
            "company": job["company_name"],
            "fit_score": fit_score["score"],
            "ats_keywords_injected": len(tailored.get("ats_keywords_used", [])),
            "ats_coverage": ats_coverage,
            "cloud_emphasis": tailored.get("cloud_emphasis", ""),
            "resume_path": str(resume_path),
        },
    )

    return {
        "id": str(uuid.uuid4()),
        "job_id": job["id"],
        "company_name": job["company_name"],
        "role_title": job["title"],
        "status": "pending_review",
        "tailored_resume_path": str(resume_path),
        "cover_letter_path": cover_path,
        "quality_score": 0,
        "quality_report": {},
        "ats_coverage": ats_coverage,
        "submitted_at": None,
        "confirmation_url": None,
        "rejection_reason": None,
    }


async def run(
    state: JobSearchState,
    max_applications: int = 10,
    concurrency: int = 3,
) -> JobSearchState:
    """
    Tailor resumes for all jobs that passed fit threshold and don't
    already have an application record.
    """
    from agents.discovery.fit_analyst import get_passing_jobs

    profile = _load_profile()
    llm       = get_llm("resume_tailor")                              # Opus (config default): bullet rewrites — KEEP Opus for max résumé quality (user decision 2026-06-08)
    llm_cheap = get_llm("resume_tailor", model="claude-haiku-4-5")    # Haiku: research + keywords

    passing = get_passing_jobs(state)

    # Skip jobs already in applications
    existing_job_ids = {a["job_id"] for a in state.get("applications", [])}
    to_tailor = [
        (job, fs) for job, fs in passing
        if job["id"] not in existing_job_ids
    ][:max_applications]

    logger.info("resume_tailor_start", to_tailor=len(to_tailor),
                passing_jobs=len(passing))

    new_applications: list[Application] = []
    sem = asyncio.Semaphore(concurrency)

    async def _tailor_with_sem(job: JobListing, fs: FitScore) -> Application | None:
        async with sem:
            return await tailor_one(job, fs, llm, profile, llm_cheap=llm_cheap)

    tasks = [_tailor_with_sem(job, fs) for job, fs in to_tailor]
    results = await asyncio.gather(*tasks)

    for result in results:
        if result:
            new_applications.append(result)

    logger.log_decision(
        action="resume_tailor_complete",
        outcome="success",
        details={
            "tailored": len(new_applications),
            "failed": len(to_tailor) - len(new_applications),
        },
    )

    updated = dict(state)
    updated["applications"] = state.get("applications", []) + new_applications
    return updated


# ── CLI smoke test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    async def _test():
        import uuid
        from datetime import datetime, timezone
        from agents.discovery.job_scanner import run as scan_jobs
        from agents.discovery.fit_analyst import run as score_jobs, get_passing_jobs
        from core.state import Company, JobSearchState

        # Use a Python/data-heavy company for better fit scores
        companies: list[Company] = [
            {
                "id": str(uuid.uuid4()), "name": "Confluent",
                "website": "https://www.confluent.io", "linkedin_url": "",
                "size": "mid", "domain": "Data Infrastructure", "tech_stack": [],
                "open_role_count": 0, "discovered_at": datetime.now(timezone.utc).isoformat(),
            },
        ]
        state: JobSearchState = {
            "companies": companies, "job_listings": [], "fit_scores": [],
            "applications": [], "pending_approvals": [], "recruiter_contacts": [],
            "email_events": [], "meetings_scheduled": [],
            "weekly_quality_score": 0.0, "weekly_quantity_count": 0,
            "response_rate": 0.0, "interview_rate": 0.0,
            "last_run_at": None, "errors": [], "agent_statuses": {},
        }

        print("Step 1: Scanning jobs...")
        scanned = await scan_jobs(state, max_companies=1, batch_size=1)
        print(f"  Found {len(scanned['job_listings'])} jobs")

        print("Step 2: Scoring fit (first 30 jobs)...")
        scanned["job_listings"] = scanned["job_listings"][:30]
        scored = await score_jobs(scanned, batch_size=5)
        passing = get_passing_jobs(scored)
        print(f"  {len(passing)} jobs passed threshold")

        if not passing:
            print("No passing jobs — try a different company")
            return

        print(f"\nStep 3: Tailoring resume for top match...")
        result = await run(scored, max_applications=1)

        apps = result.get("applications", [])
        if apps:
            app = apps[0]
            print(f"\n{'='*60}")
            print(f"Tailored for: {app['role_title']} @ {app['company_name']}")
            print(f"Resume saved: {app['tailored_resume_path']}")
            print(f"{'='*60}")
            # Print the resume
            print(Path(app["tailored_resume_path"]).read_text())
        else:
            print("No applications created")

    asyncio.run(_test())
