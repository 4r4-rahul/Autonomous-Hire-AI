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


# Known staffing/consulting firm NAMES (the company_name signal). The DB stores a coarse JobSource
# ENUM that DROPS the staffing source prefix (a Phenom/Workday/Kforce role becomes `careers_page`),
# so the UI/API can't classify from source alone — but company_name survives. Keep this in sync with
# the discovery scanners (kforce/insight_global/phenom_scanner.PHENOM_HOSTS/workday_scanner.
# WORKDAY_TENANTS) + staffing board-sponsors. Lowercased, exact match. JD-language stays the backstop.
STAFFING_COMPANIES = {
    "kforce", "insight global", "teksystems", "aston carter", "actalent", "ntt data", "genpact",
    "accenture", "vaco", "mastech digital", "system soft technologies", "system soft", "stellent it",
    "robert half", "randstad", "apex systems", "collabera", "judge group", "aditi consulting",
    "artech", "cognizant", "infosys", "wipro", "hcl", "sunrise systems",
    "sgs technologie", "softworld", "nous infosystems", "synechron", "fractal analytics",
    # browser-gated vendors reached via LinkedIn syndication (data/browser_gated_vendors.json)
    "diverse lynx", "net2source", "intelliswift", "mitchell martin", "russell tobin",
    "talentburst", "compunnel", "mindlance", "cynet systems", "ampcus",
}


def classify_employment_for_job(source: str = "", company: str = "",
                                title: str = "", description: str = "") -> str:
    """Company-aware classify for the UI/API path where the DB has lost the staffing source prefix.
    Treats a KNOWN staffing firm (by company_name) as a staffing source, then defers to the
    source-first classifier (so an explicit W2/direct-hire JD can still override to w2)."""
    if (company or "").strip().lower() in STAFFING_COMPANIES:
        source = "staffing:" + (source or "")
    return classify_employment(source, title, description)


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
