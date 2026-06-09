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
    kit = {"company": company, "role": role, "search_urls": search_urls(company, role),
           "connect_note": "", "referral_ask": ""}
    if not draft:
        return kit

    profile = yaml.safe_load(_PROFILE.read_text()) if _PROFILE.exists() else {}
    pers = profile.get("personal", {}) or {}
    name = pers.get("name") or pers.get("full_name") or ""
    summary = (profile.get("summary") or "")[:400]
    llm = get_llm("referral", model="claude-haiku-4-5")   # cheap; not the on-demand-tailor exemption
    system = (
        "You write concise, genuine LinkedIn outreach for a job seeker. No fluff, no buzzwords, "
        "no fabrication. Return STRICT JSON: {\"connect_note\": str (<=280 chars, a connection "
        "request note), \"referral_ask\": str (3-4 sentences, a polite referral-ask DM referencing "
        "the specific role)}. Warm, specific, respectful of their time.")
    user = (f"Candidate: {name}. Background: {summary}\n"
            f"Target role: {role} at {company}.\n"
            "Write outreach to a current employee/recruiter at the company.")
    try:
        raw = await llm.chat(system, [{"role": "user", "content": user}], max_tokens=400, temperature=0.5)
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        kit["connect_note"] = data.get("connect_note", "")
        kit["referral_ask"] = data.get("referral_ask", "")
    except Exception as e:
        logger.warning("referral_draft_failed", company=company, error=str(e)[:90])
    return kit
