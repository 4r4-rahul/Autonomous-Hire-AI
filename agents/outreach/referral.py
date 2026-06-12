"""
Referral kit — surface the warm-intro path for a queued role.

Cold ATS applications convert ~1-3%; referrals ~10-30%. For a sponsorship-needing candidate
competing in the same ATS pile, a referral is the single biggest callback lever. We can't read the
user's LinkedIn connection graph (and the account is rate-limited), so instead we make the referral
channel ONE CLICK actionable per role:
  • LinkedIn people-search URLs scoped to the company (1st/2nd-degree connections + recruiters +
    the role's team) — zero cost, works immediately.
  • Drafted, role-specific outreach (Haiku, on demand): a <300-char connection note and a short
    referral-ask message the user can paste to a connection or recruiter.

No LinkedIn automation, no account risk — the user sends from their own session.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote
from pathlib import Path

from core.llm import get_llm
from core.logger import get_logger

logger = get_logger("referral")

_STATE = Path("data/pipeline_state.json")
_PROFILE = Path("data/user_profile.yaml")
_PEOPLE = "https://www.linkedin.com/search/results/people/?keywords={kw}"


def _people_search(keywords: str, *, network: list[str] | None = None) -> str:
    url = _PEOPLE.format(kw=quote(keywords))
    if network:
        url += "&network=" + quote(json.dumps(network))
    return url


def search_urls(company: str, role: str) -> dict:
    """LinkedIn people-search links (no LLM, instant)."""
    return {
        "your_connections": _people_search(company, network=["F", "S"]),   # 1st + 2nd degree at company
        "recruiters": _people_search(f"{company} recruiter talent"),
        "team": _people_search(f"{company} {role}"),
    }


async def build_referral_kit(app_id: str = "", *, draft: bool = True) -> dict:
    """Build the referral kit for a queued application. draft=True adds Haiku-drafted messages."""
    import yaml
    state = json.loads(_STATE.read_text())
    app = next((a for a in state.get("applications", []) if a.get("id") == app_id), None)
    if not app:
        return {"error": "application not found"}
    company, role = app.get("company_name", ""), app.get("role_title", "")
    jl = {j.get("id"): j for j in state.get("job_listings", [])}
    jobrec = jl.get(app.get("job_id")) or {}
    job_url = jobrec.get("apply_url", "")
    jd = jobrec.get("description", "")
    kit = {"company": company, "role": role, "job_url": job_url,
           "search_urls": search_urls(company, role),
           "connect_note": "", "referral_ask": "", "why_me": "", "mutual_intro": ""}
    if not draft:
        return kit

    profile = yaml.safe_load(_PROFILE.read_text()) if _PROFILE.exists() else {}
    pers = profile.get("personal", {}) or {}
    name = pers.get("name") or pers.get("full_name") or ""
    summary = (profile.get("summary") or "")[:500]
    skills = ", ".join((profile.get("skills", {}) or {}).get("primary", [])[:8]) \
        if isinstance(profile.get("skills"), dict) else ""
    wh = profile.get("work_history") or []
    current = (wh[0] if wh else {}) if isinstance(wh, list) else {}
    cur_co = current.get("company") or (profile.get("application_defaults", {}) or {}).get("current_company", "")

    # The EXPERT flow: a connect note with NO ask (leading with an ask lowers acceptance), a
    # separate post-accept referral ask that makes the YES effortless, a 2-line "why me" the
    # referrer can paste into the internal referral form, and a warm-intro template for 2nd-degree.
    llm = get_llm("referral", model="claude-haiku-4-5")
    system = (
        "The CANDIDATE is the SENDER. Write every message in first person AS the candidate, to a "
        "TARGET EMPLOYEE (use '[Name]'). Never address the candidate by name or praise them as the "
        "recipient. Use ONLY the facts given — no fabrication.\n\n"
        "TRUTH GUARD — a referrer who vouches for an overclaim gets embarrassed, so be exact:\n"
        "  • Draw specifics from REAL EXPERIENCE below; never invent a metric, scale, or system.\n"
        "  • Distinguish PRODUCTION work (built at the employer, real users/scale) from PERSONAL/SIDE\n"
        "    projects. NEVER call a personal project 'production' or claim it runs 'at scale'.\n"
        "  • Lead with the REAL production experience that matches THIS role (see EMPHASIS). Mention\n"
        "    side projects only as side projects, and only when the role is actually about that.\n"
        "  • Use the candidate's REAL current title VERBATIM; NEVER morph it to match the target role\n"
        "    (don't call them 'Data Engineer' just because the role is one — keep their real title).\n"
        "  • NEVER invent a number ('hundreds of concurrent jobs', a percentage, a scale figure) that\n"
        "    isn't in REAL EXPERIENCE.\n"
        "  • Only claim model serving / training / drift / inference if REAL EXPERIENCE shows it;\n"
        "    otherwise describe the data-platform / backend / infra work accurately. No ML-depth bluffing.\n\n"
        "VOICE — this is the whole point: write like a real, busy senior engineer typing on their "
        "phone. Plain, direct, a little understated. Short sentences. Contractions. Confident, not "
        "eager. Every word earns its place. It must NOT read like AI or a template.\n"
        "HARD BANS (these are dead giveaways — never use any): 'I hope this finds you well', 'I came "
        "across/stumbled on your profile', 'caught my eye', 'stood out', 'I'd love to connect', 'I'm "
        "reaching out', 'passionate about', 'excited about the opportunity', 'thrilled', 'genuinely', "
        "'truly', 'really' (as filler), 'leverage', 'utilize', 'synergy', 'spearheaded', 'robust', "
        "'cutting-edge', 'deep dive', 'Looking forward to...', 'As a ___ with X years'. NO em-dashes "
        "or en-dashes (use commas/periods). NO smart quotes. NO three-item parallel lists. Don't open "
        "with 'Hi [Name], I hope'. Mention ONE concrete specific, not a resume dump.\n\n"
        "Return STRICT JSON:\n"
        '  "connect_note": <=280 chars connection note. Who the candidate is in a few words + ONE real '
        "reason to connect, with a literal '[their work/post]' slot to personalize. NO ask.\n"
        '  "referral_ask": 3-4 short sentences to send AFTER they accept. One concrete thing the '
        "candidate built, then a low-pressure ask that makes yes easy (offer resume + a ready blurb, "
        "couple minutes, fine if not, even a pointer helps).\n"
        '  "why_me": exactly 2 plain sentences ABOUT the candidate (third person) the employee can '
        'paste into the internal "why refer them" box.\n'
        '  "mutual_intro": 2 short sentences, STILL first-person AS the candidate, asking a MUTUAL '
        "(addressed as [Mutual]) to introduce the candidate to someone at the company. Do NOT write "
        "it as a third party describing the candidate by name; the candidate is asking their OWN "
        "contact for help. Start with Hey [Mutual], and ask them to intro YOU to the team.")
    # Lead with the facts that match THIS role's domain (data-platform vs AI/ML), not always AI —
    # and ground the specifics in the candidate's REAL experience bank (production work), not the
    # generic AI-heavy summary that made referrals overclaim side projects.
    try:
        from agents.application.resume_tailor import role_domain as _rd, _select_bank_stories
        domain = _rd(role, jd)
        bank = (_select_bank_stories({"title": role, "description": jd}, []) or "")[:1700]
    except Exception:
        domain, bank = "general", ""
    # The shared classifier over-tags ai_engineer; a 'Backend/Software Engineer' title with no AI
    # words is a backend role — lead with real production engineering, not the AI side projects.
    _tl = role.lower()
    if domain in ("ai_engineer", "ai_ml") and \
       any(k in _tl for k in ("backend", "software engineer", "platform", "systems", "infrastructure")) and \
       not any(k in _tl for k in ("ai ", "ai,", "ml ", "ml,", "llm", "genai", "prompt", "machine learning",
                                  "applied ai", "ai engineer", "ai/ml", "ai platform")):
        domain = "backend"
    cur_title = ((profile.get("application_defaults", {}) or {}).get("current_title")
                 or current.get("title") or "Senior Software Engineer")
    emphasis = {
        "data_platform": "DATA-PLATFORM / data-engineering role. Lead with the candidate's REAL production "
            "data work (pipelines, orchestration, warehouse, data integrity, scale) from REAL EXPERIENCE. "
            "Do NOT lead with LLM/RAG/AI; if you mention AI projects at all, one phrase, clearly as a "
            "personal side project.",
        "ai_ml": "AI/ML modeling role. Lead with the candidate's REAL data-platform/infra production work "
            "from REAL EXPERIENCE (the data foundation ML runs on: pipelines, orchestration, scale). Do "
            "NOT claim model serving, training, fine-tuning, or drift unless REAL EXPERIENCE shows it — if "
            "the candidate hasn't done core ML, be honest: strong data/infra foundation, moving toward ML. "
            "Personal LLM projects support, labeled as side projects.",
        "ai_engineer": "AI/LLM-engineering role. HERE the AI projects ARE relevant - lead with the "
            "multi-agent platform and the RAG system, but call them PERSONAL projects (never 'production', "
            "never 'at scale'), and pair them with the candidate's real production engineering for "
            "credibility.",
        "solutions_eng": "SOLUTIONS / forward-deployed / customer-facing role. Lead with turning complex "
            "production systems into outcomes and working across teams, from REAL EXPERIENCE.",
        "systems_eng": "SYSTEMS / infrastructure / platform role. Lead with the candidate's REAL production "
            "distributed-systems, reliability, and infrastructure work from REAL EXPERIENCE (e.g. the "
            "hard backend/concurrency/security calls), NOT side projects.",
        "backend": "BACKEND engineering role. Lead with the candidate's REAL production backend work from "
            "REAL EXPERIENCE (APIs, distributed systems, concurrency, reliability), NOT the AI side "
            "projects. Mention AI only if the role asks, clearly as a side project.",
        "hybrid": "Blended role. Balance the relevant areas; lead with real production work.",
        "general": "Lead with the candidate's REAL production work from REAL EXPERIENCE that most directly "
            "matches this role; side projects are clearly labeled and never inflated.",
    }.get(domain, "Lead with the real production work that most directly matches this role; never inflate "
                  "side projects.")
    user = (f"CANDIDATE (the sender): {name} — REAL current title: {cur_title} at {cur_co} "
            "(use this title accurately).\n"
            f"Candidate core skills: {skills}\n\n"
            f"REAL EXPERIENCE (draw concrete specifics from here; this is production work unless a line "
            f"says personal/side project — do NOT inflate):\n{bank or summary}\n\n"
            f"PERSONAL/SIDE PROJECTS (label as such, never 'production'/'at scale'): a multi-agent "
            f"job-search automation platform and a RAG question-answering project.\n"
            f"HARD RULE: the candidate's JPMorgan work is DATA-PLATFORM / BACKEND production (Snowflake, "
            f"distributed systems, pipelines, cloud infra, security). RAG, multi-agent, vector databases, "
            f"and embeddings are PERSONAL PROJECTS ONLY — NEVER attribute them to JPMorgan or call them "
            f"'production'. Don't presume the target works on LLMs/agents unless the JD says so.\n\n"
            f"Target company: {company} · Target role: {role}\nJob link: {job_url}\n"
            f"EMPHASIS: {emphasis}\n"
            "Write the four messages now, in the candidate's voice.")
    try:
        raw = await llm.chat(system, [{"role": "user", "content": user}], max_tokens=700, temperature=0.6)
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        for k in ("connect_note", "referral_ask", "why_me", "mutual_intro"):
            kit[k] = _humanize(data.get(k, ""))
    except Exception as e:
        logger.warning("referral_draft_failed", company=company, error=str(e)[:90])
    return kit


def _humanize(t: str) -> str:
    """Strip the visual AI tells the model still leaks: em/en-dashes and smart quotes → plain. Keeps
    the text reading like something a person typed, not generated."""
    if not t:
        return t
    for a, b in (("—", ", "), ("–", "-"), ("’", "'"), ("‘", "'"),
                 ("“", '"'), ("”", '"'), ("…", "...")):
        t = t.replace(a, b)
    return re.sub(r"\s+,", ",", re.sub(r" {2,}", " ", t)).strip()
