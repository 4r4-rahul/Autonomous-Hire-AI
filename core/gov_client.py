"""State/local-government CLIENT detection for the contract (C2C) lane.

The C2C volume in US IT staffing is dominated by *public-sector staff augmentation*: a vendor
(22nd Century, Trigyn, TechnoGen, Tanisha, SPAR, …) holds a master contract with a state agency —
a DOT, a Medicaid/MMIS program, a Department of Revenue — and posts the individual requirements on
its own public board. Those reqs are corp-to-corp/W2-contract by construction, run 12-36 months,
and (unlike FEDERAL contracts) usually carry no clearance or citizenship bar.

This module answers three questions about a posting, deterministically and with no LLM:

  1. `detect_gov_client()`  — is a state/local government the END CLIENT, and which state/agency?
  2. `is_federal_client()`  — is it a FEDERAL contract instead? (dead end for a visa candidate:
                              clearance / public trust / "US person" bars, so we drop those)
  3. `usc_gc_only()`        — does the posting restrict to citizens/green-card holders? C2C boards
                              encode this in the TITLE as compact tags — "(USC/GC/TN/CTH)",
                              "Green Card/US Citizen Only" — which prose-based knock-out lists miss.

Used by agents/discovery/gov_scanner.py to filter + tag, and by fit_analyst's sponsorship gate.
"""
from __future__ import annotations

import re

# ── States ────────────────────────────────────────────────────────────────────────────────────
STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC",
    # US territories. Not states, but they run their own public-sector boards and post IT roles —
    # the national sitemap carries Northern Mariana Islands and Virgin Islands postings, which were
    # going untagged. Territory employers hire under US work-authorization rules like any state.
    "puerto rico": "PR", "guam": "GU", "virgin islands": "VI", "american samoa": "AS",
    "northern mariana islands": "MP",
}
_STATE_NAMES = "|".join(sorted(STATE_ABBR, key=len, reverse=True))

# "State of Texas", "Commonwealth of Virginia", "State of NC"
_STATE_OF = re.compile(rf"\b(?:state|commonwealth)\s+of\s+({_STATE_NAMES})\b", re.I)
# Agency acronyms that embed the state: NCDOT, TXDOT, CalTrans, MassDOT, PennDOT
_STATE_ACRONYM = re.compile(
    r"\b(?:(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|"
    r"NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)"
    r"(?:DOT|DHS|DOH|DOR|DMV|DPS|HHS|DCS|DEQ|OTS))\b")
_NAMED_ACRONYM = {"caltrans": "CA", "penndot": "PA", "massdot": "MA", "njtransit": "NJ",
                  "mtanyc": "NY", "hhsc": "TX", "dshs": "WA", "twc": "TX"}
_NAMED_RX = re.compile(r"\b(" + "|".join(_NAMED_ACRONYM) + r")\b", re.I)
# Bare state NAME — only consulted once a gov client is already confirmed (for a state/county
# client the worksite state and the client state are the same), never as a client signal itself.
_BARE_STATE = re.compile(rf"\b({_STATE_NAMES})\b", re.I)

# ── Public-sector END-CLIENT markers ──────────────────────────────────────────────────────────
# Deliberately narrow: each phrase names a government BUYER, not merely a regulated industry.
_STATE_MARKERS = re.compile(
    r"\b(state\s+of\s+\w+|commonwealth\s+of\s+\w+|state\s+(agency|agencies|government|client|"
    r"contract|dot|department)|department\s+of\s+(transportation|human\s+services|health\s+and\s+"
    r"human\s+services|revenue|motor\s+vehicles|public\s+safety|corrections|workforce|children|"
    r"labor|education|environmental\s+quality|natural\s+resources|administrative\s+services)|"
    r"medicaid|mmis|child\s+support\s+enforcement|unemployment\s+insurance|"
    r"health\s+benefit\s+exchange|dmv\b|hhsc\b|dshs\b|dhhs\b|"
    r"office\s+of\s+information\s+technology|office\s+of\s+technology\s+services|"
    r"department\s+of\s+information\s+technology|statewide\s+contract|staff\s+augmentation)\b", re.I)
_LOCAL_MARKERS = re.compile(
    r"\b(county\s+of\s+\w+|\w+\s+county\s+government|city\s+of\s+\w+|municipal(ity)?|"
    r"school\s+district|transit\s+authority|water\s+district|public\s+utility\s+district)\b", re.I)
_PUBLIC_SECTOR = re.compile(
    r"\b(public\s+sector|government\s+(client|agency|sector|project|contract)|"
    r"govt\s+client|state\s*/\s*local|sled\b)\b", re.I)

# ── FEDERAL markers — a dead end for a sponsorship-needing candidate ─────────────────────────
# Federal prime/sub work carries clearance, Public Trust, or "US person" bars almost universally.
# Matched conservatively (each token is unambiguous in a job posting).
_FEDERAL = re.compile(
    r"\b(federal\s+(government|agency|contract|client|civilian)|"
    r"department\s+of\s+(defense|homeland\s+security|veterans\s+affairs|state|energy|justice|"
    r"the\s+treasury|the\s+interior)|"
    r"dod\b|dhs\b|\bva\s+(medical|hospital|health\s+care\s+system)|"
    r"usda\b|nasa\b|\birs\b|\bgsa\b|\bfaa\b|\bfda\b|\bnih\b|\bcdc\b|\bssa\b|\bcbp\b|\bice\b|"
    r"us\s+(army|navy|air\s+force|marine\s+corps|coast\s+guard|space\s+force)|"
    r"gov\s?con\b|govcon\b|prime\s+contractor\s+to\s+the\s+federal)\b", re.I)

# ── Citizenship bars, incl. the compact C2C TITLE tags ────────────────────────────────────────
# "(USC/GC/TN/CTH)", "USC GC only", "Green Card/US Citizen Only", "GC-EAD/USC", "No H1B".
_USC_GC_ONLY = re.compile(
    r"(\busc\b\s*[/&,+ ]\s*\bgc\b|\bgc\b\s*[/&,+ ]\s*\busc\b|"
    r"\busc\b\s*(only|/\s*tn|/\s*cth)|\bgc\b\s*[/ ]\s*(usc|citizen)|"
    r"(us|u\.s\.)\s*citizen[s]?\s*(only|/\s*gc|or\s+green\s+card|and\s+green\s+card)|"
    r"green\s*card\s*[/,]?\s*(only|holders?\s+only|/\s*us\s*citizen|or\s+us\s+citizen)|"
    r"citizens?\s+and\s+green\s+card\s+holders?\s+only|"
    r"no\s+h1\s?b|h1\s?b\s+(not|candidates\s+will\s+not)|only\s+usc\b|only\s+gc\b)", re.I)

# "No C2C" / "W2 only" is an ENGAGEMENT-STRUCTURE preference, NOT a citizenship bar — a vendor that
# hires on its own W2 can still transfer an H1B. Kept separate so it can inform ranking without ever
# feeding the sponsorship knock-out, where it would have wrongly killed viable W2-contract roles.
_W2_ONLY = re.compile(r"(\bw-?2\s+only\b|no\s+c2c|not?\s+corp\s*[-\s]?to\s*[-\s]?corp|"
                      r"\bw-?2\s+(candidates|consultants|employees)\s+only\b)", re.I)

# Explicitly visa-FRIENDLY language — a strong positive on a C2C req (the vendor will transfer H1B).
_VISA_OK = re.compile(
    r"(\bh1\s?b\b(?!\s*(not|no\b))|h-1b|all\s+visas?|any\s+visa|"
    r"visa\s+transfer|h1\s?b\s+transfer|opt\b|cpt\b|\bead\b|"
    r"sponsorship\s+(available|provided|offered)|we\s+sponsor|"
    r"\bc2c\b|corp\s*[-\s]?to\s*[-\s]?corp)", re.I)


def _state_from(text: str) -> str:
    """Best-effort 2-letter state for the END CLIENT (not the worksite)."""
    m = _STATE_OF.search(text or "")
    if m:
        return STATE_ABBR.get(m.group(1).lower(), "")
    m = _STATE_ACRONYM.search(text or "")
    if m:
        return m.group(1)[:2].upper()
    low = (text or "").lower()
    for name, abbr in _NAMED_ACRONYM.items():
        if re.search(rf"\b{name}\b", low):
            return abbr
    m = _BARE_STATE.search(text or "")
    return STATE_ABBR.get(m.group(1).lower(), "") if m else ""


def detect_gov_client(title: str = "", description: str = "", company: str = "",
                      location: str = "") -> dict | None:
    """Return {"level": "state"|"local"|"public", "state": "TX"|"", "signal": "<matched text>"}
    when a state/local government is the end client, else None. FEDERAL postings return None —
    see `is_federal_client()`; they're filtered out, not tagged."""
    text = " ".join(x for x in (title, description[:6000] if description else "", company) if x)
    if not text.strip():
        return None
    if is_federal_client(text):
        return None
    for level, rx in (("state", _STATE_MARKERS), ("state", _STATE_ACRONYM), ("state", _NAMED_RX),
                      ("local", _LOCAL_MARKERS), ("public", _PUBLIC_SECTOR)):
        m = rx.search(text)
        if m:
            return {"level": level, "state": _state_from(text) or _state_from(location or ""),
                    "signal": m.group(0)[:60]}
    return None


def is_federal_client(text: str) -> bool:
    """True when the posting names a FEDERAL end client — for a visa-needing candidate these are
    dead ends (clearance / Public Trust / US-person bars), so the gov lane drops them."""
    return bool(_FEDERAL.search(text or ""))


def usc_gc_only(title: str = "", description: str = "") -> str:
    """Return the matched citizenship-bar phrase (truthy) when the posting restricts to US citizens
    / green-card holders, else "". Reads the TITLE too — C2C boards put the bar there as a compact
    tag ("Sr .Net Developer (USC/GC Only)") that prose knock-out lists never see."""
    for chunk in (title or "", (description or "")[:6000]):
        m = _USC_GC_ONLY.search(chunk)
        if m:
            # "H1B transfer OK, GC/USC also welcome" style postings are visa-friendly, not barred:
            # only treat it as a bar when no explicit visa-friendly language sits alongside it.
            if _VISA_OK.search(chunk) and "only" not in m.group(0).lower():
                continue
            return m.group(0).strip()[:60]
    return ""


def visa_friendly(title: str = "", description: str = "") -> bool:
    """True when the posting explicitly welcomes visa candidates / C2C (H1B transfer, all visas)."""
    text = f"{title or ''}\n{(description or '')[:6000]}"
    return bool(_VISA_OK.search(text)) and not usc_gc_only(title, description)


def w2_only(title: str = "", description: str = "") -> bool:
    """True when the posting rules out corp-to-corp and requires the vendor's own W2. Not a
    citizenship bar — see the note on _W2_ONLY — so it never blocks a role, it only describes it."""
    return bool(_W2_ONLY.search(f"{title or ''}\n{(description or '')[:6000]}"))
