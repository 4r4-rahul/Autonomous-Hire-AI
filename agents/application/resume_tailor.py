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


def _load_bank_entries() -> list[dict]:
    """Parse the experience bank (data/experience_bank.yaml) into entries of real,
    differentiated engineering stories mined from the candidate's repos. Empty/missing file
    is fine — the tailor falls back to the base résumé alone."""
    p = Path("data/experience_bank.yaml")
    if not p.exists():
        return []
    try:
        return (yaml.safe_load(p.read_text()) or {}).get("entries", []) or []
    except Exception:
        return []


_BANK_ENTRIES = _load_bank_entries()


def _render_story(s: dict, indent: str = "  ") -> str:
    """Compact, prompt-friendly rendering of a single bank story."""
    shows = ", ".join(s.get("shows", []) or [])
    tradeoff = s.get("tradeoff", "")
    parts = [
        f"{indent}- PROBLEM: {s.get('problem','')}",
        f"{indent}  TRIED-FAILED: {s.get('what_didnt_work','')}" if s.get("what_didnt_work") else "",
        f"{indent}  DECISION: {s.get('decision','')}",
        f"{indent}  TRADEOFF: {tradeoff}" if tradeoff and tradeoff != "n/a" else "",
        f"{indent}  IMPACT: {s.get('impact','')}",
        f"{indent}  SHOWS: {shows}" if shows else "",
    ]
    return "\n".join(x for x in parts if x)


_TOK_RE = re.compile(r"[a-z0-9+]+")


def _tokens(*texts: str) -> set[str]:
    out: set[str] = set()
    for t in texts:
        out |= {w for w in _TOK_RE.findall((t or "").lower()) if len(w) >= 3}
    return out


def _score_story(story: dict, jd_tokens: set[str]) -> float:
    """How well a bank story matches THIS JD. shows-tags and tech weigh most (they encode the
    story's theme); a little credit for problem/decision token overlap. Deterministic, free."""
    score = 0.0
    for tag in story.get("shows", []) or []:
        tw = _tokens(tag)
        if tw and tw <= jd_tokens:            # whole tag present (e.g. "data integrity")
            score += 3.0
        elif tw & jd_tokens:
            score += 1.5
    for tech in story.get("tech", []) or []:
        if _tokens(tech) & jd_tokens:
            score += 2.0
    overlap = len(_tokens(story.get("problem", ""), story.get("decision", "")) & jd_tokens)
    score += min(overlap, 6) * 0.5
    return score


def _select_bank_stories(job: dict, ats_keywords: list[str], *,
                         per_source: int = 3, total_cap: int = 9) -> str:
    """Score every bank story against THIS JD, pick the top matches per source (so every role
    gets developed) under a global cap, and render them ranked for the per-job prompt block.
    Returns '' when the bank is empty so the writer falls back to the base résumé."""
    if not _BANK_ENTRIES:
        return ""
    jd_tokens = _tokens(job.get("title", ""), job.get("description", "")) | {
        w for k in (ats_keywords or []) for w in _tokens(k)
    }
    blocks: list[tuple[float, str]] = []
    for ent in _BANK_ENTRIES:
        scored = sorted(((_score_story(s, jd_tokens), s) for s in ent.get("stories", []) or []),
                        key=lambda x: x[0], reverse=True)
        top = [s for sc, s in scored[:per_source] if sc > 0] or [scored[0][1]] if scored else []
        if not top:
            continue
        best = scored[0][0] if scored else 0.0
        src = ent.get("source", "")
        tech = ", ".join(ent.get("tech", []) or [])
        rendered = f"### {src}" + (f"  [tech: {tech}]" if tech else "") + "\n" + \
            "\n".join(_render_story(s) for s in top)
        blocks.append((best, rendered))
    blocks.sort(key=lambda x: x[0], reverse=True)
    # Trim to the global cap by dropping lowest-scored individual stories isn't needed — per_source
    # already bounds it; cap the number of SOURCES rendered if it ever grows large.
    chosen = [b for _, b in blocks][: max(2, total_cap // per_source + 1)]
    return "\n\n".join(chosen).strip()


# Full bank (all stories) kept for the cached reference block; per-JD selection is built per call.
_EXPERIENCE_BANK = "\n\n".join(
    (f"### {e.get('source','')}" + (f"  [tech: {', '.join(e.get('tech', []) or [])}]" if e.get("tech") else "")
     + "\n" + "\n".join(_render_story(s) for s in e.get("stories", []) or []))
    for e in _BANK_ENTRIES
).strip()
_PROFILE_PATH     = Path("data/user_profile.yaml")
_RESUME_DIR       = Path("data/resumes")
_RESUME_DIR.mkdir(parents=True, exist_ok=True)
_COVER_DIR        = Path("data/cover_letters")
_COVER_DIR.mkdir(parents=True, exist_ok=True)


def _cover_letter_decision(job: dict, fit_score: dict, profile: dict) -> tuple[bool, str]:
    """Decide whether a cover letter is WORTH writing for this role (quality lever, not blanket).
    Modes (application_defaults.cover_letter): 'never' off, 'always' every role, 'auto' (default)
    = smart/when-necessary. Returns (needed, human-readable reason)."""
    mode = (profile.get("application_defaults", {}) or {}).get("cover_letter", "auto")
    if mode == "never":
        return False, ""
    if mode == "always":
        return True, "configured to always include one"
    # 'auto' — include only when it genuinely improves the application:
    desc = (job.get("description") or "").lower()
    title = (job.get("title") or "").lower()
    score = (fit_score or {}).get("score", 0) or 0
    if "cover letter" in desc or "cover letter" in title:
        return True, "the posting asks for a cover letter"
    if score >= 78:
        return True, f"strong fit ({score}) — a tailored pitch is worth it"
    dom = role_domain(job.get("title", ""), job.get("description", ""))
    if dom == "solutions_eng":
        return True, "customer-facing role — motivation and communication carry weight"
    if any(k in desc for k in ("early-stage", "early stage", "seed stage", "series a", "series b",
                               "founding", "join us early", "small team", "0 to 1", "0-to-1")):
        return True, "early-stage role — motivation and proactive sponsorship framing help"
    return False, ""


async def _cover_letter(job: dict, profile: dict, llm, ats_keywords: list[str] | None = None) -> str:
    """A short, truthful, role-matched cover letter — GROUNDED in the candidate's real experience-bank
    stories (reframed, not copied) and tied to THIS JD's hardest problem, so it reads like the
    candidate wrote it, not a template. Proactively frames work authorization (a callback lever for a
    sponsorship-needing candidate). Never fabricates."""
    wa = profile.get("work_authorization", {}) or {}
    pers = profile.get("personal", {}) or {}
    name = pers.get("name") or pers.get("full_name") or "the candidate"
    skills = ", ".join((profile.get("skills", {}) or {}).get("primary", [])[:8]) \
        if isinstance(profile.get("skills"), dict) else ""
    sponsor_line = ("I am authorized to work in the U.S. and will require H1B sponsorship/transfer."
                    if wa.get("requires_sponsorship") else "I am authorized to work in the U.S.")
    # The single most JD-relevant real story to anchor the letter (reframed, never copied verbatim).
    stories = _select_bank_stories(job, ats_keywords or [])[:1400]
    system = (
        "Write a concise, senior cover letter (140-180 words, 3 short paragraphs) that sounds like a "
        "real engineer wrote it in one focused sitting — specific, a little understated, never "
        "templated. Truthful: use ONLY the facts provided; never invent experience, employers, or "
        "metrics. Lead with ONE concrete accomplishment that maps to this role's hardest problem "
        "(reframe a provided story in your own words; do not copy it). Plain text, no markdown, no "
        "clichés ('excited to apply', 'team player', 'proven track record', 'fast-paced'). Mirror the "
        "JD's language where it fits. Include exactly one sentence stating work authorization verbatim.")
    user = (
        f"Candidate: {name}\nCore skills: {skills}\n"
        f"Work authorization (use this sentence verbatim): {sponsor_line}\n\n"
        f"Real accomplishments to draw from (reframe for this role, never copy):\n{stories}\n\n"
        f"Role: {job.get('title')} at {job.get('company_name')}\n"
        f"Job description (match to its hardest problem):\n{(job.get('description') or '')[:1500]}\n\n"
        "Write the cover letter now.")
    return await llm.chat(system, [{"role": "user", "content": user}], max_tokens=600, temperature=0.45)


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

# Keyword sets per target category. Title hits count double (strongest signal); JD breaks ties.
_DOMAIN_TERMS = {
    # ML modeling / training / serving / MLOps
    "ai_ml": ("machine learning", "ml engineer", "ml platform", "mlops", "model serving", "model training",
              "applied scientist", "deep learning", "fine-tun", "inference", "recommendation", "ranking"),
    # LLM/GenAI APPLICATION building, prompt eng, agents, Claude/Anthropic, AI coding tools
    "ai_engineer": ("ai engineer", "prompt engineer", "prompt engineering", "genai", "gen ai", "generative ai",
                    "llm", "rag", "retrieval-augmented", "agent", "agentic", "claude", "anthropic", "openai",
                    "vector database", "embeddings", "ai application", "ai product", "copilot", "ai coding",
                    "developer tool", "developer experience", "devrel", "developer advocate", "mcp", "tool-use"),
    # Data infrastructure (home turf)
    "data_platform": ("data platform", "data engineer", "data infrastructure", "data pipeline", "etl", "elt",
                      "analytics engineer", "data warehouse", "data lake", "lakehouse", "streaming", "spark",
                      "kafka", "flink", "airflow", "dagster", "dbt", "snowflake", "databricks", "bigquery",
                      "data modeling", "data quality", "data governance", "ingestion", "iceberg", "delta lake"),
    # Solutions / sales / forward-deployed / customer-facing technical
    "solutions_eng": ("solutions engineer", "solution engineer", "sales engineer", "solutions architect",
                      "pre-sales", "presales", "forward deployed", "forward-deployed", "field engineer",
                      "implementation engineer", "customer engineer", "technical account", "deployment strategist",
                      "professional services", "integration engineer"),
    # Systems / infra / platform / distributed / backend / reliability
    "systems_eng": ("systems engineer", "infrastructure engineer", "platform engineer", "distributed systems",
                    "backend engineer", "backend software", "site reliability", "reliability engineer",
                    "kubernetes", "terraform", "scalability", "high availability", "observability", "cloud engineer"),
}
_DOMAIN_ORDER = ("ai_engineer", "ai_ml", "data_platform", "solutions_eng", "systems_eng")


def role_domain(title: str, jd: str) -> str:
    """Classify a role into one of the target categories (ai_engineer / ai_ml / data_platform /
    solutions_eng / systems_eng), or 'hybrid'/'general'. Title hits count double; JD breaks ties.
    Drives which specialized résumé emphasis is used."""
    t, j = (title or "").lower(), (jd or "").lower()
    scores = {d: sum(2 for k in terms if k in t) + sum(1 for k in terms if k in j)
              for d, terms in _DOMAIN_TERMS.items()}
    top = max(scores.values())
    if top == 0:
        return "general"
    winners = [d for d in _DOMAIN_ORDER if scores[d] == top]
    if len(winners) > 1:
        return "hybrid"
    return winners[0]


def _emphasis_directive(domain: str) -> tuple[str, bool]:
    """(directive text, projects_first). The directive is the DOMINANT per-job instruction — it
    selects which specialized résumé to produce so emphasis mirrors THIS JD's category."""
    D = {
        "ai_ml": ("ROLE EMPHASIS = AI/ML (modeling, training, serving, MLOps). Weight ~70% toward model "
                  "serving, MLOps, training/inference infra, feature pipelines, evaluation. Lead with AI/ML "
                  "impact in the JD's exact terms. AI Projects come first.", True),
        "ai_engineer": ("ROLE EMPHASIS = AI ENGINEER (building LLM/GenAI applications — incl. prompt "
                        "engineering, agents, Claude/Anthropic, AI dev tools). Weight ~70% toward SHIPPING "
                        "real LLM systems: RAG pipelines, multi-agent systems, prompt engineering + "
                        "evaluation, LLM API integration (Claude/Anthropic, GPT-4), tool-use/function "
                        "calling, vector search, human-in-the-loop. Lead with the candidate's shipped AI "
                        "systems (the 20-agent autonomous platform on LangGraph + Claude + RAG, and the "
                        "production RAG system). This is APPLICATION/systems building with LLMs, NOT model "
                        "training. AI Projects come first.", True),
        "data_platform": ("ROLE EMPHASIS = DATA PLATFORM / DATA ENGINEERING. Weight ~70% toward batch + "
                          "streaming pipelines, orchestration (Airflow/dbt), warehouse/lakehouse, data "
                          "modeling, data quality/governance, reliability and scale, in the JD's exact "
                          "terms. Lead with DATA-PLATFORM impact. Keep AI/ML to at most one brief mention, "
                          "and only if the JD asks. Professional Experience before projects. Lean — only "
                          "what the JD asks for.", False),
        "solutions_eng": ("ROLE EMPHASIS = SOLUTIONS / SALES / FORWARD-DEPLOYED ENGINEERING (customer-facing "
                          "technical). Weight ~70% toward turning complex systems into business value: "
                          "stakeholder-facing delivery, integrations/implementations, technical "
                          "communication, breadth across the stack, and partnering with teams/customers. "
                          "Lead with cross-functional, delivery, and impact-on-others bullets (e.g. "
                          "'partnered with 8 downstream teams to ship X'). Show you both BUILD and "
                          "COMMUNICATE; keep deep internals brief. Professional Experience before projects.", False),
        "systems_eng": ("ROLE EMPHASIS = SYSTEMS / INFRASTRUCTURE / PLATFORM ENGINEERING. Weight ~70% "
                        "toward distributed systems, scale, reliability, infrastructure (Kubernetes, cloud, "
                        "Terraform/IaC), backend services, performance and operational excellence. Lead with "
                        "systems-level impact (throughput, latency, uptime, scale) in the JD's terms. Keep "
                        "AI/ML brief unless the JD asks. Professional Experience before projects.", False),
        "hybrid": ("ROLE EMPHASIS = HYBRID. Mirror the JD's own balance across the relevant areas; lead with "
                   "whichever it names first/most. Don't over-index on AI if the JD doesn't.", False),
    }
    return D.get(domain, (
        "ROLE EMPHASIS = match the JD exactly. Lead with the skills/experience THIS JD asks for; include "
        "only what is relevant. Do NOT force an AI angle onto a non-AI role.", False))


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
    bank_block = (f"""

EXPERIENCE BANK — REAL, DEFENSIBLE ENGINEERING STORIES (mined from the candidate's actual repos).
These are the SOURCE OF TRUTH for differentiated bullets: each is a real problem → the non-obvious
decision → the tradeoff → the measured impact. Draw the bullets that map to THIS job's hardest
problem from here. Never copy a story verbatim — reframe it for the JD. NEVER use a story whose
facts the base résumé/profile don't support.
{_EXPERIENCE_BANK}""" if _EXPERIENCE_BANK else "")

    stable_block = f"""BASE RESUME:
{_BASE_RESUME}

CANDIDATE AI/ML SKILLS TO DRAW FROM (use aggressively for AI roles):
{json.dumps(ai_skills)}

CANDIDATE AI SYSTEMS SKILLS (production AI patterns):
{json.dumps(profile.get('skills', {}).get('ai_systems', []))}{bank_block}""".strip()

    # Role-matched emphasis — the dominant instruction for THIS job. Mirrors the JD's actual focus
    # (data-platform vs AI/ML) instead of always leading with AI.
    _domain = role_domain(job.get("title", ""), job.get("description", ""))
    _directive, _projects_first = _emphasis_directive(_domain)

    # Per-JD story development: score the bank against THIS JD and surface the top matches per role
    # right here in the (uncached) per-job block, so every tailoring run DEVELOPS bullets from the
    # most relevant real stories rather than hoping the writer digs them out of the cached bank.
    _selected = _select_bank_stories(job, ats_keywords)
    _bank_directive = (f"""

DEVELOP BULLETS FROM THE EXPERIENCE BANK — every tailoring must mine the bank, not the base résumé's
generic lines. The stories below are the deterministically-ranked BEST matches for THIS JD (most
relevant first); develop each role's lead bullets from them — reframe into a crisp bullet (the
non-obvious decision + tradeoff + measured impact) in the JD's language, strongest match as the role's
first bullet. You MAY instead use a different story from the full EXPERIENCE BANK above if it fits a role
better — but a real bank story must drive the lead bullets. Never copy verbatim; never add a fact the
story doesn't contain.

PRE-RANKED MATCHES FOR THIS JD:
{_selected}""" if _selected else """

USE THE EXPERIENCE BANK above: for each role, lead with the story whose SHOWS-tags best match this JD's
hardest problem, reframed into a crisp bullet. If no story fits a role, keep the base-résumé bullet.""")

    # VARIABLE block — changes per job, never cached.
    variable_block = f"""*** {_directive} ***

JOB TITLE: {job['title']}
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

ROLE DOMAIN: {_domain}
SECTION ORDER: {'AI Projects BEFORE Professional Experience' if _projects_first else 'Professional Experience BEFORE projects'} — set "projects_first": {str(_projects_first).lower()} in the output JSON.
Follow the ROLE EMPHASIS at the top: keep the résumé lean and matched to THIS JD — include only what it asks for.
{_bank_directive}""".strip()

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

    # Cover letter — ONLY when it genuinely improves the application (posting asks, strong fit,
    # customer-facing, or early-stage). Grounded in real bank stories. Uses the stronger model
    # since it's now selective. Proactively frames work authorization (a sponsorship callback lever).
    cover_path = None
    need_cover, cover_reason = _cover_letter_decision(job, fit_score, profile)
    if need_cover:
        try:
            cover_md = await _cover_letter(job, profile, llm, ats_keywords)
            if cover_md:
                cp = _COVER_DIR / f"{job['id']}.md"
                cp.write_text(_clean_md(cover_md), encoding="utf-8")
                cover_path = str(cp)
                logger.info("cover_letter_written", company=job.get("company_name"), reason=cover_reason)
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
        "cover_letter_reason": cover_reason if cover_path else "",
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
