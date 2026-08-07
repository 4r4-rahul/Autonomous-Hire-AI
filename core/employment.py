"""W2 (direct-employer, full-time) vs contract (staffing/C2C) classification.

Deterministic, source-first: a role's SOURCE is the reliable signal — company ATS boards
(Greenhouse/Lever/Ashby/…) are direct-employer W2; staffing-agency scanners (Kforce, TEKsystems,
Insight Global, …) are contract. JD language only overrides when it explicitly contradicts the
source (a board role advertised as a contract, or a staffing role that's a direct-hire placement).

Used to (a) TAG every role W2/contract for sort/filter, and (b) gate hands-off auto-apply to W2
only — never cold-auto-submit a contract/staffing role.
"""

import re

# Source PREFIXES that mean a staffing/contract intermediary (extend as the contract universe grows).
# Match is prefix-based on the source string (e.g. "kforce:azuresearch" -> "kforce").
_STAFFING_SOURCE_PREFIXES = (
    "kforce", "teksystems", "insightglobal", "insight_global", "roberthalf", "robert_half",
    "randstad", "apexsystems", "apex_systems", "motion_recruitment", "motionrecruitment",
    "dice", "cybercoders", "collabera", "judge", "aditi", "artech", "beacon", "signature",
    "staffing", "consulting", "contract",
)

# JD language that explicitly marks a CONTRACT engagement (overrides a W2-looking source).
_CONTRACT_JD = re.compile(
    r"\b(c2c|corp[- ]to[- ]corp|1099|w2 contract|contract[- ]to[- ]hire|contract to hire|"
    r"contract position|contract role|contract opportunity|\d+[- ]month contract|"
    r"months? contract|contractor|temp[- ]to[- ]perm|temporary|staff aug|staffing)\b", re.I)

# JD language that explicitly marks direct FULL-TIME/W2 employment (overrides a staffing-looking source).
_W2_JD = re.compile(
    r"\b(full[- ]time (employee|permanent|position)|w-?2 (employee|employment)|"
    r"direct hire|direct[- ]hire|permanent (position|role|employee)|fte\b|"
    r"salaried employee|benefits (package|eligible))\b", re.I)


def _is_staffing_source(source: str) -> bool:
    s = (source or "").lower()
    return any(s.startswith(p) or f":{p}" in s or f"_{p}" in s for p in _STAFFING_SOURCE_PREFIXES)


def classify_employment(source: str = "", title: str = "", description: str = "") -> str:
    """Return "w2" | "contract" | "unknown". Source-first, JD-language override on explicit conflict."""
    blob = f"{title or ''}\n{(description or '')[:4000]}"
    staffing = _is_staffing_source(source)

    # Explicit JD language wins when it clearly contradicts the source signal.
    if _CONTRACT_JD.search(blob):
        return "contract"
    if staffing and _W2_JD.search(blob):
        return "w2"          # staffing shop advertising a direct-hire placement

    if staffing:
        return "contract"
    if source:
        return "w2"          # a real company ATS / careers page / board = direct-employer W2
    return "unknown"


def is_w2(source: str = "", title: str = "", description: str = "") -> bool:
    return classify_employment(source, title, description) == "w2"
