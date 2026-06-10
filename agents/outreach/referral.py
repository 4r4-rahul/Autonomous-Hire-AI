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
        "The CANDIDATE is the SENDER. Write every message in the first person AS the candidate, "
        "addressed to a TARGET EMPLOYEE at the company (use '[Name]' for that employee). NEVER "
        "address the candidate by name or praise the candidate as if they're the recipient — the "
        "candidate is writing these, describing THEIR OWN background. Peer-to-peer, specific, never "
        "needy or salesy. NO fabrication — use only the candidate facts given. Return STRICT JSON:\n"
        '  "connect_note": <=280 chars LinkedIn connection note FROM the candidate TO [Name]. Lead '
        "with who the candidate is (their own background) + ONE genuine reason to connect, including "
        "a literal '[their work/post]' slot to personalize. NO ask, NO mention of referral/applying.\n"
        '  "referral_ask": 3-5 sentences FROM the candidate TO [Name], to send AFTER they accept. '
        "State ONE concrete accomplishment of the candidate, then a low-pressure optional referral "
        "ask that makes yes effortless (offer to send résumé + a ready blurb, ~2 min, 'totally fine "
        "if not — even a pointer helps').\n"
        '  "why_me": exactly 2 sentences ABOUT the candidate (third person) that [Name] can paste '
        'into the internal "why are you referring them" box.\n'
        '  "mutual_intro": 2 sentences FROM the candidate TO a MUTUAL connection ('"'"'[Mutual]'"'"'), '
        "asking them to introduce the candidate to someone at the company.")
    user = (f"CANDIDATE (the sender): {name} — currently at {cur_co}.\n"
            f"Candidate background: {summary}\nCandidate core skills: {skills}\n"
            f"Target company: {company} · Target role: {role}\nJob link: {job_url}\n"
            "Write the four messages now, in the candidate's voice.")
    try:
        raw = await llm.chat(system, [{"role": "user", "content": user}], max_tokens=700, temperature=0.5)
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        for k in ("connect_note", "referral_ask", "why_me", "mutual_intro"):
            kit[k] = data.get(k, "")
    except Exception as e:
        logger.warning("referral_draft_failed", company=company, error=str(e)[:90])
    return kit
