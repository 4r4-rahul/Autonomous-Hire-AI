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

# TITLE explicitly naming a contract engagement — the employer's OWN label for the role, a strong
# signal on any source. Guard against "smart contract" (blockchain FTE roles are full-time).
_CONTRACT_TITLE = re.compile(
    r"\b(c2c|corp[- ]to[- ]corp|1099|(?<!smart )contract|contractor|"
    r"contract[- ]to[- ]hire|temp[- ]to[- ]perm|temporary)\b", re.I)

# UNAMBIGUOUS C2C/staffing language — a real full-time product-company posting never contains these,
# so they flip even a direct-employer source to contract.
_CONTRACT_HARD = re.compile(
    r"\b(c2c|corp[- ]to[- ]corp|1099|w2 contract|contract[- ]to[- ]hire|contract to hire|"
    r"\d+[- ]months?[- ]contract|temp[- ]to[- ]perm)\b", re.I)

# SOFT contract words that appear INCIDENTALLY in full-time JDs ("work with contractors", "no
# temporary fixes"). These must NOT override a real direct-employer source — only used as a
# last-ditch signal when there's no source at all. (This was the full-time-leak bug.)
_CONTRACT_SOFT = re.compile(
    r"\b(contractor|contract position|contract role|contract opportunity|"
    r"months? contract|temporary|staff aug|staffing)\b", re.I)

# JD language that explicitly marks direct FULL-TIME/W2 employment (overrides a staffing-looking source).
_W2_JD = re.compile(
    r"\b(full[- ]time (employee|permanent|position)|w-?2 (employee|employment)|"
    r"direct hire|direct[- ]hire|permanent (position|role|employee)|fte\b|"
    r"salaried employee|benefits (package|eligible))\b", re.I)


def _is_staffing_source(source: str) -> bool:
    s = (source or "").lower()
    return any(s.startswith(p) or f":{p}" in s or f"_{p}" in s for p in _STAFFING_SOURCE_PREFIXES)


# The ATS's OWN structured employment-type field (Lever categories.commitment, Ashby employmentType,
# SmartRecruiters typeOfEmployment, Workable employment_type). This is the CLEANEST contract signal —
# the employer declaring the role's type — so it beats JD-language guessing (no "block"/"applied
# systems" false positives). Returns "contract" | "w2" | "" (unknown/absent → fall back to heuristics).
def _structured_class(employment_type: str) -> str:
    t = (employment_type or "").strip().lower()
    if not t:
        return ""
    if any(k in t for k in ("contract", "contractor", "c2c", "1099", "temporary", "temp",
                            "fixed term", "fixed-term", "seasonal", "freelance")):
        return "contract"
    if any(k in t for k in ("full", "permanent", "fte", "regular", "direct")):
        return "w2"
    return ""


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
    # C2C staffing/consulting firms discovered via source-discovery harvest (real US IT staffing
    # shops posting corp-to-corp/1099 SWE/data roles on public ATS boards). Keep in sync with ingests.
    "atria group", "atria group llc", "damcosoft", "damcosoft inc", "satnam data systems",
    "trident consulting", "north star staffing solutions", "north star staffing", "codurance",
    "computer futures", "ricefw technologies", "jobsbridge", "computech corporation", "computech",
    "iris software", "m9 solutions", "photon", "infovision", "tanisha systems", "aptask",
    "v-soft consulting", "vsoft", "deegit", "nityo infotech", "pyramid consulting", "eteam",
    "empower professionals", "spar information systems", "amaze systems", "spar", "e-solutions",
    "spectraforce", "yochana", "kellton", "acs solutions", "american cybersystems",
    # State/local government IT staff-augmentation C2C vendors (place H1B/C2C consultants on state
    # DOT/health/tax/DMV projects — NO clearance/citizenship, unlike federal contractors). These are
    # the sponsorship-viable slice of "government jobs"; the fit_analyst clearance filter drops the
    # federal-cleared ones. Add more as source-discovery validates them.
    "22nd century technologies", "22nd century technologies inc", "tscti", "trigyn",
    "trigyn technologies", "computer aid", "computer aid inc", "cai", "nam info", "nam info inc",
    "softpath system", "softpath", "sganalytics", "kani solutions", "progilytics", "prutech",
    "prutech solutions", "zillion technologies", "intellyk", "tekfortune", "generis tek",
    "generistek", "ask consulting", "ask staffing", "datasoft technologies", "knowledge builders",
    "amerit consulting", "systematix", "prodware solutions", "ivy tech solutions",
    # H1B-sponsoring C2C IT staffing firms confirmed on SmartRecruiters (source-discovery harvest).
    "consultadd", "american technology consulting", "atc", "dellfor technologies", "dellfor",
    "collabera", "artech information system", "us corp2corp", "uscorp2corp", "north star staffing",
    "buzzclan", "prosum", "nityo infotech", "technology navigators", "dataintellect", "tms",
}


def classify_employment_for_job(source: str = "", company: str = "", title: str = "",
                                description: str = "", employment_type: str = "") -> str:
    """Company-aware classify for the UI/API path where the DB has lost the staffing source prefix.
    Treats a KNOWN staffing firm (by company_name) as a staffing source, then defers to the
    source-first classifier. `employment_type` = the ATS's structured flag (cleanest signal)."""
    if (company or "").strip().lower() in STAFFING_COMPANIES:
        source = "staffing:" + (source or "")
    return classify_employment(source, title, description, employment_type)


def classify_employment(source: str = "", title: str = "", description: str = "",
                        employment_type: str = "") -> str:
    """Return "w2" | "contract" | "unknown".
    PRIORITY: (1) the ATS's OWN structured employment-type field (cleanest — employer-declared);
    (2) explicit JD language (c2c / contract-to-hire / 1099); (3) staffing source/company; (4) a real
    company ATS = W2. The structured flag wins so direct-client CONTRACT roles are caught precisely
    and product-co full-time roles aren't false-flagged by stray JD words."""
    structured = _structured_class(employment_type)
    if structured:
        return structured

    title_l = (title or "").lower()
    body = (description or "")[:4000]
    staffing = _is_staffing_source(source)

    # (2) TITLE names a contract engagement (verve "Data Engineer (Remote / Contract)") — strong.
    if _CONTRACT_TITLE.search(title_l):
        return "contract"
    # (3) UNAMBIGUOUS C2C language in the body — real full-time postings don't carry these.
    if _CONTRACT_HARD.search(body):
        return "contract"
    # (4) Staffing source/company → contract, unless it advertises a direct-hire placement.
    if staffing:
        if _W2_JD.search(f"{title_l}\n{body}"):
            return "w2"
        return "contract"
    # (5) A real direct-employer ATS / careers page / board = W2. SOFT body words ("contractor",
    #     "temporary") do NOT override the source — that leaked product-co full-time roles (e.g. a
    #     "Senior SWE" JD mentioning contractors) onto the contract desk.
    if source:
        return "w2"
    # (6) No source signal at all — last-ditch soft language.
    if _CONTRACT_SOFT.search(body):
        return "contract"
    return "unknown"


def is_w2(source: str = "", title: str = "", description: str = "") -> bool:
    return classify_employment(source, title, description) == "w2"
