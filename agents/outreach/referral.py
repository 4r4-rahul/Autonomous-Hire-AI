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
    job_url = (jl.get(app.get("job_id")) or {}).get("apply_url", "")
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
        '  "mutual_intro": 2 short sentences to a MUTUAL ('"'"'[Mutual]'"'"') asking for an intro to '
        "someone at the company.")
    user = (f"CANDIDATE (the sender): {name} — currently at {cur_co}.\n"
            f"Candidate background: {summary}\nCandidate core skills: {skills}\n"
            f"Target company: {company} · Target role: {role}\nJob link: {job_url}\n"
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
