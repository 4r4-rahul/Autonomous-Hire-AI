"""
Autofill resolver + endpoint for the browser extension.

The extension scrapes the application form's fields from the page the USER has open
(in their own logged-in browser) and POSTs them here; we return the answer for each
using the same "brain" the automation used — profile data, the label→answer maps, and
the truthful work-authorization picker. The extension fills the DOM; the user reviews,
solves any CAPTCHA, and submits natively. No automation browser, no anti-bot fight.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter
from pydantic import BaseModel

from core.logger import get_logger

logger = get_logger("autofill")

# Reuse the truthful work-auth picker (pure functions) from the filler.
from agents.application.form_filler import _is_workauth_label, _pick_truthful_workauth

router = APIRouter(prefix="/autofill", tags=["autofill"])

# Background tailor tasks — kept referenced so the event loop can't GC them mid-flight (a bare
# asyncio.create_task can be garbage-collected, silently killing the tailoring the extension is
# polling for). add_done_callback logs failures (otherwise they vanish) and drops the ref.
_TAILOR_TASKS: set = set()


def _spawn_tailor(job_id: str) -> bool:
    """Fire on-demand tailoring in the background, tracked + error-logged. Returns True if started."""
    import asyncio
    from agents.application.on_demand_tailor import tailor_application
    try:
        task = asyncio.create_task(tailor_application(job_id=job_id))
    except RuntimeError:
        return False   # no running loop (shouldn't happen in a request handler)
    _TAILOR_TASKS.add(task)

    def _done(t: "asyncio.Task"):
        _TAILOR_TASKS.discard(t)
        try:
            exc = t.exception()
            if exc:
                logger.error("background_tailor_failed", job_id=job_id, error=str(exc)[:160])
            else:
                res = t.result()
                if isinstance(res, dict) and res.get("error"):
                    logger.warning("background_tailor_error", job_id=job_id, error=res["error"][:160])
        except Exception:
            pass
    task.add_done_callback(_done)
    return True

_ROOT = Path(__file__).parent.parent
_PROFILE = _ROOT / "data" / "user_profile.yaml"
_STATE = _ROOT / "data" / "pipeline_state.json"


# ── Request / response models ───────────────────────────────────────────────
class FieldIn(BaseModel):
    id: str                       # opaque id the extension assigns (to map the answer back)
    label: str = ""
    type: str = "text"            # text|email|tel|url|textarea|select|radio|checkbox
    options: list[str] = []       # for select/radio/checkbox


class ResolveIn(BaseModel):
    url: str = ""
    referrer: str = ""            # parent-page URL (the form is often in a cross-origin iframe)
    origin_url: str = ""          # URL the extension opened this tab with (survives Apply-Now nav)
    fields: list[FieldIn] = []


class FillOut(BaseModel):
    id: str
    kind: str                     # "text" | "select" | "value" | "skip"
    value: str = ""               # text to type, or the option text to choose
    note: str = ""                # shown to the user (esp. for skips / sensitive fields)


# ── Profile helpers ─────────────────────────────────────────────────────────
def _load_profile() -> dict:
    try:
        return yaml.safe_load(_PROFILE.read_text()) or {}
    except Exception:
        return {}


def _norm_label(s: str) -> str:
    """Normalize a field label for keyword matching: lowercase, smart-quotes → plain,
    hyphens/underscores/slashes → spaces, collapse whitespace. Without this, 'ideal
    start-date' never matched the 'start date' rule (hyphen vs space)."""
    s = (s or "").lower().strip()
    s = s.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"[-_/]+", " ", s)
    # Strip "(required)" / "(optional)" markers and the asterisk — they inflate length and
    # broke the short-label guards (e.g. "Email (required) 1fb4ed12" was > 24 chars → "no rule").
    s = re.sub(r"\(?\s*(required|optional)\s*\)?|\*", " ", s)
    # Strip trailing field-id hashes (hex tokens that contain a digit, e.g. "1fb4ed12",
    # "d70cf5ba") — Greenhouse/Ashby append these. Keep pure-letter words like "facade".
    s = re.sub(r"\b[0-9a-f]{6,12}\b",
               lambda m: " " if any(c.isdigit() for c in m.group()) else m.group(), s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _us_phone(raw: str) -> str:
    """US national format with NO country code — "+1 555-010-0100" → "555-010-0100". The "+1 "
    prefix / 11th digit clears masked US phone fields (Kforce et al. expect 10 digits), so we feed
    the bare 10-digit number (dashed: mask-friendly AND readable on plain fields)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return (raw or "").strip()   # unrecognized format → leave untouched


def _match_option(answer: str, options: list[str]) -> Optional[str]:
    """Pick the option text that best matches `answer` (exact → contains → first word)."""
    if not options or not answer:
        return None
    al = answer.strip().lower()
    for o in options:
        if o.strip().lower() == al:
            return o
    for o in options:
        ol = o.strip().lower()
        if ol and (ol.startswith(al) or al in ol or ol in al):
            return o
    aw = (al.split() or [al])[0]
    if len(aw) >= 2:  # guard: a 1-char first word ("i", "a") matches almost anything
        for o in options:
            if aw and aw in o.strip().lower():
                return o
    return None


# EEO answers we'll select when no truthful option matches — a legitimate, non-protected
# choice so the question still gets filled rather than left for the user every time.
_DECLINE_HINTS = ("don't wish", "do not wish", "dont wish", "prefer not", "decline",
                  "not to answer", "not to disclose", "not to identify", "rather not",
                  "wish not", "choose not", "i do not wish")


def _decline_option(options: list[str]) -> Optional[str]:
    for o in options:
        if any(h in o.lower() for h in _DECLINE_HINTS):
            return o
    return None


def _match_eeo_option(cands: list[str], options: list[str]) -> Optional[str]:
    """Match a truthful EEO answer to a verbose option, exact first then strong containment.
    Avoids the generic matcher's single-token fallback (which could pick 'I don't wish to
    answer' off a stray letter). 'No' still matches 'No, I am not a veteran…' via prefix."""
    if not options:
        return None
    for a in cands:
        al = (a or "").strip().lower()
        if not al:
            continue
        for o in options:                      # exact
            if o.strip().lower() == al:
                return o
        for o in options:                      # answer is a prefix/substring of the option
            ol = o.strip().lower()
            if ol.startswith(al + ",") or ol.startswith(al + " ") or ol.startswith(al + " (") or ol == al:
                return o
            # answer ⊂ option as a WHOLE WORD (e.g. "asian" ⊂ "Asian (Not Hispanic or
            # Latino)") — word-boundary so "asian" does NOT match "Cauc-asian". NEVER
            # option ⊂ answer ("hispanic or latino" ⊂ "Not Hispanic or Latino" → Hispanic box).
            if len(al) >= 5 and re.search(r"\b" + re.escape(al) + r"\b", ol):
                return o
    return None


def _first_option_containing(needle: str, options: list[str]) -> Optional[str]:
    """First option whose text contains `needle` (case-insensitive) — for picking a safe
    'None of the above' / 'Not applicable' option on compliance questions."""
    n = (needle or "").lower()
    for o in options:
        if n and n in o.lower():
            return o
    return None


def _match_years_range(years: int, options: list[str]) -> Optional[str]:
    """Map a plain experience count (6) to a RANGE option ('6-9 years', '5+ years', 'More than 5
    years', 'Less than 1 year'). Picks the bucket whose [low, high] contains `years`. Returns None
    when the options aren't ranges (caller then falls back to an exact match / typing the number).
    This is the fix for the react-select that hung open because we typed bare '6' into a field whose
    only options were ranges like '6-9 years' (the Coalition 'stuck' case)."""
    for o in options:
        s = o.lower()
        nums = [int(n) for n in re.findall(r"\d+", s)]
        if not nums:
            continue
        if len(nums) >= 2:                                              # "6-9 years", "3 to 5 years"
            lo, hi = nums[0], nums[1]
        elif any(x in s for x in ("+", "more than", "at least", "over", "greater", "or more")):
            lo, hi = nums[0], 10**6                                     # "5+ years", "more than 5"
        elif any(x in s for x in ("less than", "under", "fewer", "up to", "below", "or less")):
            lo, hi = 0, nums[0] - 1                                     # "less than 1 year"
        else:
            lo, hi = nums[0], nums[0]                                   # exact "6 years"
        if lo <= years <= hi:
            return o
    return None


def _is_yesno(options: list[str]) -> bool:
    # Strip leading icons/symbols (Workable renders "✓ YES" / "✗ NO" toggle buttons).
    ne = [re.sub(r"^[^a-z0-9]+", "", o.strip().lower()).strip() for o in options if o.strip()]
    return bool(ne) and (all(len(o) <= 6 for o in ne) or set(ne) <= {"yes", "no", "n/a"})


# Sensitive demographic fields — only fill on an exact option match, else leave for the
# human (never blind-pick a protected-class value).
_DEMO_KEYS = ("gender", "race", "ethnic", "veteran", "disabilit", "orientation",
              "hispanic", "latino", "lgbtq", "military", "armed forces")


def _resolve_one(f: FieldIn, P: dict) -> FillOut:
    personal = P.get("personal", {})
    defs = P.get("application_defaults", {})
    wa = P.get("work_authorization", {})
    home = personal.get("home_address", {}) or {}

    ll = _norm_label(f.label)
    opts = f.options or []
    ftype = (f.type or "text").lower()
    is_choice = ftype in ("select", "radio", "checkbox") or bool(opts)

    name = personal.get("name", "")
    parts = name.split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""

    def text(v, note=""):
        return FillOut(id=f.id, kind="text", value=str(v or ""), note=note)

    def choose(answer, note=""):
        if not is_choice:
            return text(answer, note)
        opt = _match_option(answer, opts)
        if opt is None:
            return FillOut(id=f.id, kind="skip", value="",
                           note=note or f"couldn't match '{answer}' to an option — please pick")
        return FillOut(id=f.id, kind="value", value=opt, note=note)

    def loc(answer, note=""):
        # Location fields are often type-ahead/geosuggest with NO preset options — hand
        # the value to the extension to type + select rather than skipping on no match.
        if not is_choice:
            return text(answer, note)
        opt = _match_option(answer, opts)
        return FillOut(id=f.id, kind="value", value=(opt or answer),
                       note=note or "type-ahead location — verify the suggestion")

    def skip(note):
        return FillOut(id=f.id, kind="skip", value="", note=note)

    if not ll:
        return skip("no label")

    # Type-based identity FIRST — a tel/email input is unambiguous and robust against
    # polluted labels (Workable jams the whole country-code list into the phone label,
    # which broke the length-guarded "phone" rule and mis-filled it with the city).
    if ftype == "tel":
        return text(_us_phone(personal.get("phone", "")))
    if ftype == "email":
        return text(personal.get("email", ""))

    # ── Work authorization (handle before generic identity/country) ──────────
    # "Authorized WITHOUT sponsorship / a visa?" → No (you require sponsorship). Must
    # come first so the generic eligibility/sponsorship checks don't answer Yes.
    if any(k in ll for k in ("without sponsorship", "without requiring sponsorship",
                             "authorized without", "not require sponsorship",
                             "without a visa", "without needing sponsorship")):
        return choose(wa.get("authorized_without_sponsorship", "No"),
                      "authorized WITHOUT sponsorship: No (you require sponsorship)")
    # Eligibility / authorization to work — Yes (you're authorized), or pick the truthful
    # sentence option. Covers "eligible to work", "right to work", "legally able to work".
    _elig = (("eligible" in ll or "right to work" in ll or "legally able" in ll
              or "lawfully" in ll) and "work" in ll and "sponsor" not in ll)
    # Staffing apply forms label this "Employment Eligibility" / "Work Eligibility" / "Work
    # Authorization (Status)" with no "work"+"eligible" combo — route those to the truthful picker.
    _elig = _elig or any(k in ll for k in ("employment eligibility", "work eligibility",
                                           "eligibility to work", "work authorization",
                                           "authorization to work", "employment authorization",
                                           "eligibility")) and "benefit" not in ll
    if _is_workauth_label(ll) or _elig:
        if is_choice and not _is_yesno(opts):
            idx = _pick_truthful_workauth(opts)
            if idx is not None:
                return FillOut(id=f.id, kind="value", value=opts[idx],
                               note="work auth: authorized on a visa, requires sponsorship")
            return skip("work authorization — please choose the option that matches your status")
        return choose(wa.get("authorized_answer", "Yes"), "authorized/eligible to work: Yes")
    # Any sponsorship / immigration-status question → Yes (you require sponsorship).
    # Catches "Do you need … any immigration sponsorship …" and bare "sponsorship".
    if "sponsor" in ll or ("immigration" in ll and any(k in ll for k in ("need", "require", "status"))):
        return choose(wa.get("sponsorship_answer", "Yes"), "requires sponsorship: Yes")
    if any(k in ll for k in ("citizen or green card", "citizen or permanent resident",
                             "u.s. citizen", "us citizen")):
        return choose("No", "not a citizen/permanent resident")

    # Conditional follow-ups ("If yes, …") — correctly left blank (the gating answer was
    # No). kind="blank" so the UI shows it as deliberate, not an action item.
    if (ll.startswith("if yes") or ll.startswith("if so") or ll.startswith("if no")
            or ll.startswith("if you answered") or ll.startswith("if applicable")
            or ll.startswith("if selected") or "if you answered \"yes\"" in ll
            or "if you answered yes" in ll or "if you selected yes" in ll):
        return FillOut(id=f.id, kind="blank",
                       note="conditional — correctly left blank (previous answer was No)")

    # ── Universal essay gate (runs BEFORE every keyword rule) ─────────────────
    # Root-cause fix for the "preferred approach to STATE MANAGEMENT in React" → "Texas" class:
    # a genuine free-text essay must never be hijacked by a downstream keyword rule that merely
    # shares a word (state/city/company/github/...). A field WITH options is never an essay; cover
    # letters and "anything else" boxes keep their dedicated handling later, so we exclude them.
    # Work-auth / sponsorship / conditional are handled above and still win.
    if not is_choice and _is_strong_essay(ll, ftype):
        return FillOut(id=f.id, kind="generate", value=f.label, note="essay")

    # ── Identity ─────────────────────────────────────────────────────────────
    if "preferred name" in ll or "preferred first" in ll:
        return text(first)
    if "first name" in ll:
        return text(first)
    if "last name" in ll or "family name" in ll or "surname" in ll:
        return text(last)
    if ("full name" in ll or ll in ("name", "your name", "legal name")) and "user" not in ll:
        return text(name)
    # Short labels only — so a long question that merely mentions "email"/"phone"
    # (e.g. "…if yes, provide your manager's email") isn't treated as the field itself.
    if ("email" in ll or "e mail" in ll) and len(ll) <= 24:
        return text(personal.get("email", ""))
    if ("phone" in ll or "mobile" in ll) and len(ll) <= 24:
        return text(_us_phone(personal.get("phone", "")))
    # Length-guarded like email/phone: a SHORT label ("LinkedIn", "GitHub URL") is the profile
    # field; a long question that merely mentions the platform ("your philosophy on using GitHub?")
    # is not — let it fall through (the essay gate above usually already claimed it).
    if "linkedin" in ll and len(ll) <= 30:
        return text(personal.get("linkedin_url", ""))
    if "github" in ll and len(ll) <= 30:
        return text(personal.get("github_url", ""))
    if any(k in ll for k in ("website", "portfolio", "personal site")) and len(ll) <= 30:
        return text(personal.get("github_url", ""))

    # ── Address ──────────────────────────────────────────────────────────────
    # Word boundaries: "unit" must not match "UNITed States", "apt" not "adAPTable".
    if "address line 2" in ll or re.search(r"\b(apt|apartment|suite|ste|unit)\b", ll):
        return text("")
    if any(k in ll for k in ("address line 1", "street address", "home address")):
        return text(home.get("line1", "[REDACTED ADDRESS]"))
    # Citizenship / birth / nationality → India (Rahul is an Indian national on a visa),
    # NOT his US residence. MUST come before the generic residence-country rule.
    if any(k in ll for k in ("citizenship", "nationality", "country of birth",
                             "birth country", "country of origin", "country of citizenship")):
        nat = "Indian" if "national" in ll and "country" not in ll else \
              personal.get("citizenship_country", "India")
        return choose(nat, "citizenship/birth country: India")
    if "country" in ll:
        return choose("United States", "residence country")
    # Word-boundary so "city" doesn't match "ethniCITY" and "state" doesn't match
    # "STATEment" / "real eState". PLUS a context guard: address fields are SHORT — a long essay
    # that merely contains "state"/"city"/"location" ("preferred approach to STATE MANAGEMENT in
    # React") is NOT an address field and must fall through to the open-ended handler, not get
    # answered "Texas".
    _addr_ctx = any(k in ll for k in ("address", "residen", "reside", "you live", "based in",
                                      "mailing", "located", "hometown", "where are you", "home "))
    _not_addr = any(k in ll for k in ("state management", "state machine", "application state",
                                      "global state", "component state", "manage state", "react",
                                      "redux", "state of the", "approach", "describe", "explain",
                                      "experience", "preferred approach"))
    _addr_ok = (len(ll) <= 28 or _addr_ctx) and not _not_addr
    if (re.search(r"\bcity\b", ll) or ("location" in ll and "relocat" not in ll)) and _addr_ok:
        return loc(home.get("city", "Plano"))
    if (re.search(r"\bstate\b", ll) or "province" in ll) and _addr_ok:
        return choose(home.get("state", "Texas"))
    if any(k in ll for k in ("zip", "postal")):
        return text(home.get("zip", "00000"))

    # ── Education (ATS School/Degree/Discipline widget) ──────────────────────
    # MUST come before the job "start date → Immediately" rule: education uses
    # "Start date month/year" + "End date month/year" which would otherwise collide.
    edu = P.get("education_primary", {})
    if edu and ("start date" in ll or "end date" in ll) and ("month" in ll or "year" in ll):
        is_end = "end date" in ll
        key = ("end_" if is_end else "start_") + ("year" if "year" in ll else "month")
        val = edu.get(key, "")
        return choose(val, "education date") if val else skip("education date — please fill")
    if edu and ((re.search(r"\bschool\b", ll) or "university" in ll or "institution" in ll
                 or re.search(r"\bcollege\b", ll)) and "high school" not in ll
                and "after school" not in ll):
        return loc(edu.get("school", ""), "school — type-ahead, verify suggestion")
    if edu and "degree" in ll and "degree of" not in ll and "to what degree" not in ll:
        return choose(edu.get("degree", "Master's Degree"), "degree")
    if edu and any(k in ll for k in ("discipline", "major", "field of study",
                                     "concentration", "area of study", "course of study")):
        cands = edu.get("discipline_alternatives") or [edu.get("discipline", "Computer Science")]
        if is_choice:
            for c in cands:
                opt = _match_option(c, opts)
                if opt is not None:
                    return FillOut(id=f.id, kind="value", value=opt, note="discipline")
            return choose(cands[0], "discipline")
        return text(cands[0], "discipline")

    # ── Application specifics ────────────────────────────────────────────────
    # "source" must be the specific phrase — bare "source" collided with "open source".
    if any(k in ll for k in ("hear about", "learn about", "how did you find",
                             "referral source", "how did you first", "how did you hear")):
        return choose(defs.get("referral_source", "LinkedIn"))
    if any(k in ll for k in ("start date", "available to start", "earliest start",
                             "when can you start", "notice period")):
        return choose(defs.get("start_date", "Immediately"))
    if any(k in ll for k in ("salary", "compensation", "pay expectation", "desired pay")):
        return text(defs.get("salary_display", "$150,000 – $200,000"))
    # Relocation / onsite-willingness ("Our HQ is in San Mateo and this role is not remote. Are
    # you …?") → Yes (open to relocating / working onsite). Gated on a willingness phrasing so it
    # can't hijack a neutral "is this role remote?" description.
    if is_choice and any(k in ll for k in (
            "willing to relocate", "open to relocation", "able to relocate", "relocate",
            "willing to work onsite", "willing to work on-site", "able to work onsite",
            "able to work on-site", "comfortable working onsite", "comfortable working in",
            "willing to commute", "able to commute", "work from the office", "report to the office",
            "work onsite", "work on-site", "work in-office", "work in the office")) \
            and any(w in ll for w in ("you", "are you", "willing", "able", "comfortable", "open to")):
        return choose(defs.get("relocation_answer", "Yes"), "willing to relocate / work onsite: Yes")
    if any(k in ll for k in ("years of experience", "years experience", "years of relevant",
                             "years of professional", "how many years")):
        yrs = int(re.sub(r"\D", "", str(defs.get("years_of_experience", "6"))) or "6")
        if is_choice and opts:
            # Range dropdowns ("6-9 years") need the bucket, not the bare number — typing "6"
            # filters but never commits, leaving the menu hung open.
            opt = _match_years_range(yrs, opts) or _match_option(str(yrs), opts)
            if opt:
                return FillOut(id=f.id, kind="value", value=opt, note="years of experience (range)")
            return skip(f"years of experience ({yrs}) — pick the matching range")
        return text(str(yrs))
    if any(k in ll for k in ("current company", "current employer")):
        return text(defs.get("current_company", "JPMorgan Chase & Co."))
    if any(k in ll for k in ("current title", "current role", "current position")):
        return text(defs.get("current_title", "Senior Software Engineer"))
    if "pronoun" in ll:
        pref = defs.get("pronouns", "He/Him")
        if is_choice:
            opt = _match_option(pref, opts)
            if opt is None:
                # "He/Him" won't exact/prefix-match "He/Him/His" or "He / Him / His" — match the
                # masculine option by token (has 'he', not 'she'/'they').
                for o in opts:
                    toks = re.findall(r"[a-z]+", o.lower())
                    if "he" in toks and "she" not in toks and "they" not in toks:
                        opt = o
                        break
            if opt:
                return FillOut(id=f.id, kind="value", value=opt, note="pronouns: he/him")
            return skip("pronouns — please pick")
        return text(pref)
    if any(k in ll for k in ("languages you speak", "languages do you speak",
                             "spoken language", "fluent in", "native language")):
        return text(defs.get("languages", "English, Hindi, Telugu, Urdu"))

    # ── EEO / demographics ────────────────────────────────────────────────────
    # Pick the TRUTHFUL option (forms vary: "Man" vs "Male", verbose Ashby sentences like
    # "No, I am not a veteran or active member"). If no truthful option matches, select the
    # always-present "decline to self-identify" option so the question still gets filled —
    # never blind-pick a protected-class value, never leave it stuck on the user.
    # ── Age eligibility — "At the time of application, are you 18+ years of age?" → Yes ──
    if (("18" in ll or "eighteen" in ll or "age of majority" in ll or "legal working age" in ll)
            and any(k in ll for k in ("age", "older", "old", "18+", "eighteen", "legal working",
                                      "majority"))):
        return choose("Yes", "18+ years of age: Yes")

    # NOTE: check "transgender" FIRST — it contains "gender" and must not take the gender path.
    if any(k in ll for k in _DEMO_KEYS):
        cands: list[str] = []
        is_race = False
        if "transgender" in ll:
            cands = ["No"]
        elif "gender" in ll or "how do you identify" in ll:
            cands = [defs.get("eeoc_gender", "Man"), "Man", "Male", "Cisgender man", "Cis man"]
        elif ("race" in ll or "ethnic" in ll) and "hispanic" not in ll and "latino" not in ll:
            is_race = True
            # Rahul is Indian → South Asian. Cover granular OMB-style sets (Asian Indian /
            # Indian subcontinent), federal verbose ("Asian (Not Hispanic or Latino)"),
            # the Asian/Pacific-Islander grouping, then plain "Asian". NOT bare "Indian"
            # (it substring-matches "American Indian or Alaska Native").
            cands = ["South Asian", "Asian Indian", "Indian subcontinent",
                     "Asian (Not Hispanic or Latino)", "Asian or Pacific Islander",
                     defs.get("eeoc_race", "Asian"), "Asian"]
        elif "hispanic" in ll or "latino" in ll:
            cands = [defs.get("eeoc_hispanic", "No"), "Not Hispanic or Latino",
                     "No, I am not Hispanic or Latino", "No"]
        elif "veteran" in ll or "military" in ll or "armed forces" in ll:
            # "Have you ever served in the military?" is a veteran self-ID — Rahul has not served.
            cands = ["No, I am not a veteran or active member", "I am not a protected veteran",
                     "Not a protected veteran", "I am not a veteran",
                     "No, I am not a protected veteran", "No, I have not served",
                     "Have not served", "No"]
        elif "disabilit" in ll:
            cands = ["No, I do not have a disability and have not had one in the past",
                     "No, I do not have a disability", "No, I don't have a disability", "No"]
        elif "orientation" in ll:
            cands = [defs.get("eeoc_sexual_orientation", "Straight"), "Heterosexual", "Straight",
                     "Heterosexual / Straight"]
        elif "lgbtq" in ll:
            # "Do you identify as part of the LGBTQ+ community?" — truthful No; if it's an
            # orientation picker instead of Yes/No, the straight option matches.
            cands = ["No", "No, I do not identify", "Not a member",
                     defs.get("eeoc_sexual_orientation", "Straight"), "Heterosexual", "Straight"]
        if cands and is_choice:
            opt = _match_eeo_option(cands, opts)
            # Race guard: the chosen race option MUST contain "asian" (Rahul is South Asian).
            # Defends against any fuzzy match ever returning Black / White / Native / Hispanic /
            # Pacific-Islander-only — if it's not an Asian-family option, fall to decline instead.
            if opt is not None and is_race and "asian" not in opt.lower():
                opt = None
            if opt is not None:
                return FillOut(id=f.id, kind="value", value=opt, note="EEO (truthful answer)")
            decline = _decline_option(opts)
            if decline is not None:
                return FillOut(id=f.id, kind="value", value=decline,
                               note="EEO — selected 'decline to answer' (no truthful option matched)")
            return skip("sensitive (EEO) — no matching option; please choose")
        if cands:
            return text(cands[0], "EEO")

    # ── Export-compliance / sanctions (U.S. OFAC) ────────────────────────────
    # Rahul is an Indian national — NONE of the sanctioned-country statuses apply. Pick the
    # safe "none / not applicable" option; never tick a sanctioned-country box. Handles the
    # common two-part pattern: Q1 "(do any apply?)" → None of the above; Q2 "(if you picked
    # other than none, which also applies?)" → Not applicable.
    _SANCTION = ("sanction", "export control", "ofac", "cuba", "iran", "north korea",
                 "syria", "crimea", "belarus")
    _followup = ("prior question" in ll or "previous question" in ll
                 or ll.startswith("if you selected"))
    if is_choice and (any(k in ll for k in _SANCTION) or _followup):
        for safe in ("not applicable", "none of these apply", "none of the above",
                     "none apply", "does not apply", "none of these"):
            opt = _first_option_containing(safe, opts)
            if opt:
                return FillOut(id=f.id, kind="value", value=opt,
                               note="export-compliance: none of these apply to you")
        if _match_option("No", opts):
            return choose("No", "export-compliance: none apply")
        return skip("compliance — confirm 'none of the above' applies")

    # ── Conflict-of-interest / outside-activity / government-official disclosures → No ──
    # Finance-sector forms (Robinhood et al.) ask broad Yes/No conflict & anti-bribery
    # disclosures. Rahul has none to report — answer No; any "if yes, explain" follow-up is
    # then correctly left blank by the conditional rule above.
    if is_choice and any(k in ll for k in (
            "personal/familial relationship", "personal/ familial", "familial relationship",
            "outside business activit", "conflict of interest", "intellectual property ownership",
            "investment that is greater", "government official", "bribery", "corruption",
            "politically exposed", "public function")):
        return choose("No", "compliance disclosure: No (nothing to report)")

    # ── Prior employment at THIS company (not "ever worked WITH <tech>") ──────
    if (any(k in ll for k in ("previously employed", "former employee", "worked here before",
                              "previously worked", "currently or have you previously",
                              "worked for ", "ever worked for", "rehire", "boomerang",
                              "previously been employed", "worked at this", "work for us",
                              "been employed by", "been employed at", "ever been employed",
                              "been employed with"))
            and "worked with" not in ll and "experience with" not in ll):
        if is_choice:
            for neg in ("No, I have never", "No - I have never", "Never worked",
                        "No, I have not", "No"):
                opt = _match_option(neg, opts)
                if opt:
                    return FillOut(id=f.id, kind="value", value=opt, note="not a former employee")
        return choose("No", "not a former employee")
    if any(k in ll for k in ("currently an employee", "current employee", "currently employed by",
                             "employee or contractor at", "current contractor",
                             "related to anyone", "family member", "relative who works")):
        return choose("No")

    # ── Acknowledgment / consent dropdowns ("I acknowledge", "I agree", privacy/data consent) ──
    # Required gating dropdowns whose affirmative option is worded "I acknowledge" / "I agree",
    # NOT "Yes" — so we pick by that wording. (Sponsorship/work-auth are handled far above, so this
    # can't hijack them.) Left unfilled, these hang the form on a "needs you" item.
    if is_choice and any(k in ll for k in (
            "acknowledge", "i agree", "agree to", "consent to", "i consent", "i have read",
            "privacy policy", "privacy notice", "candidate privacy", "data privacy",
            "data protection", "terms and conditions", "processing of my", "use of my data")):
        for needle in ("i acknowledge", "acknowledge", "i agree", "i accept", "i consent",
                       "agree", "accept", "consent", "yes"):
            opt = _first_option_containing(needle, opts)
            if opt:
                return FillOut(id=f.id, kind="value", value=opt, note="acknowledgment/consent: affirmative")
        return choose("Yes", "acknowledgment/consent")

    # ── Self-attestation ("confirm everything is true and your own", "I certify") → Yes ──
    if is_choice and any(k in ll for k in (
            "confirm that everything", "confirm everything", "true and your own",
            "is true and", "i certify", "certify that", "i attest", "attest that",
            "confirm the information", "information is accurate", "above is accurate",
            "confirm that the", "everything in this application")):
        return choose("Yes", "attestation: Yes")

    # Instruction/attention checks we genuinely cannot answer for the user (depends on what
    # they actually wrote) — flag clearly instead of a useless "no rule".
    if is_choice and ("exact phrase" in ll or ("did you start" in ll and "answer" in ll)):
        return skip("⚠️ instruction check — read the JD for the required opening phrase, then answer Yes")

    # ── Open-ended / essay questions → draft with the LLM (grounded in profile+job) ──
    # ONLY for genuine free-text — a field WITH options is a choice, never an essay (long
    # compliance/checkbox labels were wrongly generating "None of the above." text).
    # These are "Why are you interested in <company>?", cover letters, free-text textareas.
    if not is_choice and _is_open_ended(ll, ftype):
        # Truly optional "anything else / accommodations" → leave blank unless the user wants it.
        if any(k in ll for k in ("anything else", "additional information", "additional comments",
                                 "accommodation", "anything you")):
            return FillOut(id=f.id, kind="generate", value=f.label, note="optional")
        return FillOut(id=f.id, kind="generate", value=f.label, note="essay")

    return skip("no rule — please fill")


# Markers that make a free-text field UNAMBIGUOUSLY an essay (opinion / narrative / explanation) —
# none of these appear in identity/address/short-datum labels, so it's safe to match these BEFORE
# the keyword rules. (The broader _is_open_ended below — incl. "what is your", bare "?" — stays at
# the END as a fallback, because those CAN be short-datum questions a keyword rule should claim.)
_ESSAY_MARKERS = (
    "why are you", "why do you", "why this", "why join", "why work", "why our", "why should",
    "describe ", "explain ", "walk us through", "walk me through", "tell us about", "tell me about",
    "in your own words", "your approach", "approach to", "thoughts on", "perspective on",
    "elaborate", "experience with", "what excites", "what interests", "what motivat",
    "what draws you", "what attracts", "good fit", "right fit", "best fit", "a time when",
    "a time you", "give an example", "share an example", "how do you", "how would you",
    "how have you", "how has", "what would you build", "what are your thoughts", "what makes you",
)


def _is_strong_essay(ll: str, ftype: str) -> bool:
    """Conservative, runs-early essay test: only True for things that could NEVER be a short datum
    (a textarea, a very long prompt, or an explicit explanation marker). Excludes cover letters and
    'anything else' boxes, which have dedicated handling downstream."""
    if "cover letter" in ll or "coverletter" in ll or "motivation" in ll:
        return False
    if any(k in ll for k in ("anything else", "additional information", "additional comments",
                             "accommodation", "anything you")):
        return False
    if ftype == "textarea" or len(ll) > 80:
        return True
    return any(m in ll for m in _ESSAY_MARKERS)


def _is_open_ended(ll: str, ftype: str) -> bool:
    """True if this is a free-text question we should draft an answer for. By the time we reach
    here the field is NOT a choice and NOT a standard structured field (name/email/phone/links are
    handled earlier), so a question-form prompt is almost always a free-text essay."""
    if ftype == "textarea":
        return True
    if "cover letter" in ll or len(ll) > 70:
        return True
    # Any reasonably-long question-form prompt ('?' anywhere, not a tiny "URL?" field) is free-text.
    # Catches "How has AI changed the way you approach your work?", "What would you build?", etc.
    if "?" in ll and len(ll.strip()) > 18:
        return True
    return any(k in ll for k in (
        "why are you interested", "why do you want", "why this", "why are you applying",
        "why join", "why work", "tell us about", "tell us why", "what makes you",
        "what excites you", "what interests you", "describe your", "what attracts",
        "good fit", "right fit", "best fit", "in your own words", "motivat",
        # broader open-ended stems (incl. 'how' questions and prompt-style asks)
        "how has", "how do you", "how would you", "how have you", "how did you",
        "what is your", "what are your", "what's your", "describe a", "describe an",
        "share an", "share a", "give an example", "walk us through", "experience with",
        "approach to", "your approach", "thoughts on", "perspective on", "elaborate", "explain",
    ))


# ── Open-ended answer generation ──────────────────────────────────────────────
def _candidate_brief(P: dict) -> str:
    """Compact, factual summary of the user for grounding open-ended answers — no
    fabrication; the LLM may only draw from these real facts."""
    per = P.get("personal", {})
    defs = P.get("application_defaults", {})
    sk = P.get("skills", {})
    tgt = P.get("target", {})
    lines = [
        f"Name: {per.get('name','')}",
        f"Current role: {defs.get('current_title','')} at {defs.get('current_company','')}",
        f"Years of experience: {P.get('experience_years', defs.get('years_of_experience',''))}",
        f"Core skills: {', '.join(sk.get('primary', [])[:10])}",
        f"AI/ML systems: {', '.join(sk.get('ai_systems', [])[:8])}",
        f"MLOps/Platform: {', '.join(sk.get('mlops', [])[:8])}",
        f"Target domains: {', '.join(tgt.get('domains', [])[:6])}",
    ]
    # A couple of real work-history / project highlights for specificity.
    wh = P.get("work_history", []) or []
    for job in wh[:2]:
        if isinstance(job, dict):
            title = job.get("title") or job.get("role") or ""
            comp = job.get("company") or ""
            highs = job.get("highlights") or job.get("achievements") or []
            h = highs[0] if isinstance(highs, list) and highs else (job.get("summary") or "")
            if title or comp:
                lines.append(f"Experience: {title} @ {comp} — {str(h)[:160]}")
    projs = P.get("projects", []) or []
    for pr in projs[:2]:
        if isinstance(pr, dict):
            nm = pr.get("name") or pr.get("title") or ""
            ds = pr.get("description") or pr.get("summary") or ""
            if nm:
                lines.append(f"Project: {nm} — {str(ds)[:140]}")
    return "\n".join(l for l in lines if l.rsplit(": ", 1)[-1].strip())


_OPEN_SYSTEM = """You are drafting an application answer AS the candidate (first person, "I").
You will get the candidate's factual brief, the company + role + job description, and ONE
application question. Write the candidate's answer.

Rules — follow exactly:
- Sound like a real, specific human engineer wrote it in 60 seconds. NOT marketing copy.
- Ground every claim in the candidate brief and REAL EXPERIENCE. NEVER invent employers,
  projects, numbers, or skills not given. If you lack a specific fact, stay general but honest.
- For "describe / tell me about / a time when / hardest problem / most interesting / proudest /
  what have you built" questions: reframe a CONCRETE story from REAL EXPERIENCE (the non-obvious
  decision + what it solved), never a generic "I have X years and these skills."
- TRUTH GUARD: distinguish PRODUCTION work (built at the employer, real scale) from PERSONAL/SIDE
  projects. NEVER call a personal project "production" or claim scale it doesn't have. Do NOT claim
  ML model training/serving/drift unless REAL EXPERIENCE shows it. A referrer/interviewer will probe.
- ROLE-MATCH: for data/backend/systems roles lead with production engineering; for AI/LLM roles the
  agent/RAG projects are relevant (labeled as personal projects).
- For "why are you interested in <company>": connect something concrete the company/role
  actually does (from the job description) to the candidate's real experience. One genuine
  reason beats three generic ones.
- 2-4 sentences for "why" questions; a short tight paragraph for cover letters. Be concise.
- For a short yes/no or one-line factual answer, give JUST the answer with NO trailing period
  ("Yes", not "Yes."; "No to all three", not "No to all three.") — these often feed dropdowns where
  a trailing period stops the option from matching.
- Plain words and punctuation only. No em dashes, no semicolons-as-style, no bullet symbols.
- BANNED words/phrases (sound AI/templated): leverage, spearheaded, seamless, robust,
  passionate, thrilled, excited to, deeply, unlock, synergy, cutting-edge, fast-paced,
  "I am writing to", "I would be a great fit", "perfect candidate", "wealth of experience".
- Do not restate the question. Do not add a greeting or signature. Output ONLY the answer.
- If the question asks for something we genuinely cannot answer truthfully, return an empty string."""


async def _generate_open_ended(questions: list[str], job: Optional[dict], P: dict) -> list[str]:
    """Batch-draft answers for open-ended questions in ONE LLM call. Returns answers aligned
    to `questions` (empty string where it should stay blank). Never raises — returns [] on error."""
    if not questions:
        return []
    try:
        from core.llm import get_llm
        brief = _candidate_brief(P)
        company = (job or {}).get("company") or "this company"
        title = (job or {}).get("title") or "this role"
        jd = ((job or {}).get("description") or "")[:1500]
        # Ground answers in the JD-matched experience bank (real, git-verified stories) — the same
        # source the résumé/cover/referral use — so "describe a hard problem" pulls a concrete story,
        # not a generic skills recap. Empty/missing bank is fine (falls back to the brief).
        bank = ""
        try:
            from agents.application.resume_tailor import _select_bank_stories
            bank = (_select_bank_stories({"title": title, "description": jd}, []) or "")[:1800]
        except Exception:
            bank = ""
        numbered = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
        user = (
            f"CANDIDATE BRIEF:\n{brief}\n\n"
            f"REAL EXPERIENCE (production work unless a line says personal/side project — draw "
            f"concrete specifics from here, do NOT inflate):\n{bank or '(use the brief above)'}\n\n"
            f"COMPANY: {company}\nROLE: {title}\n"
            f"JOB DESCRIPTION (for grounding 'why' answers):\n{jd or '(not available)'}\n\n"
            f"QUESTIONS (answer each, same order):\n{numbered}\n\n"
            f'Return JSON only: {{"answers": ["<answer 1>", "<answer 2>", ...]}} — '
            f"exactly {len(questions)} strings, same order. Use \"\" to leave one blank."
        )
        llm = get_llm("autofill", model="claude-opus-4-8")
        result = await llm.chat_json(
            system=_OPEN_SYSTEM,
            messages=[{"role": "user", "content": user}],
            max_tokens=1400,
            temperature=0.6,
        )
        answers = result.get("answers") if isinstance(result, dict) else None
        if not isinstance(answers, list):
            return [""] * len(questions)
        # Pad/truncate to match the question count.
        def _tidy(a: str) -> str:
            a = str(a or "").strip()
            # Strip a trailing period on SHORT single-statement answers ("Yes." -> "Yes", "No." ->
            # "No") so they match dropdown options and read naturally. Leave multi-sentence essays
            # (which contain a mid-string ". ") untouched.
            if a.endswith(".") and ". " not in a and len(a) <= 64:
                a = a[:-1].rstrip()
            return a
        answers = [_tidy(a) for a in answers]
        answers += [""] * (len(questions) - len(answers))
        return answers[:len(questions)]
    except Exception as e:
        logger.warning("open_ended_generation_failed", error=str(e)[:120])
        return [""] * len(questions)


# ── Endpoint ─────────────────────────────────────────────────────────────────
def _match_job(url: str, referrer: str = "", origin: str = "") -> Optional[dict]:
    """Return {id, company, title} for the job matching `url`, `referrer`, or `origin`.
    Embedded application forms live in a cross-origin iframe, so the iframe URL is the
    ATS embed (not our stored apply_url) — we also match on the parent referrer and, as
    a fallback, on a shared long numeric token (gh_jid / job id present in both URLs).
    `origin` is the URL the extension OPENED this tab with (e.g. the job page it reopened in
    incognito) — it survives an 'Apply Now' navigation that drops the job id from url/referrer."""
    try:
        st = json.loads(_STATE.read_text())
    except Exception:
        return None
    cands = [u for u in (url, referrer, origin) if u]
    if not cands:
        return None
    bases = [u.split("?")[0] for u in cands]
    tokens = set(re.findall(r"\d{6,}", " ".join(cands)))
    jobs = st.get("job_listings", [])

    def _alnum_ids(s: str) -> set:
        # ATS shortcodes: alphanumeric tokens ≥8 chars with BOTH a digit and a letter
        # (Workable "CEACA06A2D", Lever/Ashby ids). Excludes words like "huggingface".
        return {t for t in re.findall(r"[A-Za-z0-9]{8,}", s)
                if any(c.isdigit() for c in t) and any(c.isalpha() for c in t)}

    url_alnum = _alnum_ids(" ".join(cands))

    def _ret(j):
        return {"id": j.get("id"), "company": j.get("company_name"), "title": j.get("title"),
                "description": j.get("description") or ""}

    # The SAME posting can be stored as two listings (e.g. boards. vs job-boards.greenhouse.io),
    # and only ONE carries the application + tailored résumé. When several listings match, prefer
    # the candidate that actually has a usable résumé, then any application, then the first — so we
    # don't return the empty duplicate and 404 the résumé that's sitting right there.
    apps = {a.get("job_id"): a for a in st.get("applications", [])}

    def _score(j):
        a = apps.get(j.get("id"))
        if not a:
            return 0
        trp = a.get("tailored_resume_path")
        if trp:
            p = Path(trp)
            if not p.is_absolute():
                p = _ROOT / p
            return 3 if p.exists() else 2
        return 1

    def _best(matches):
        return _ret(max(matches, key=_score)) if matches else None  # max keeps the FIRST on ties

    # 1) PRECISE first: shared gh_jid / job-id token. All roles at one company share the same
    #    URL base (careers.x.com/jobs), so a base match alone returns the WRONG role — the
    #    numeric job id is what pins the exact posting. Must win over the loose base match.
    if tokens:
        m = [j for j in jobs if tokens & set(re.findall(r"\d{6,}", j.get("apply_url") or ""))]
        if m:
            return _best(m)
    # 1b) alphanumeric ATS shortcode (Workable/Lever/Ashby) — the stored URL and the form URL
    #     can differ in slug ("/j/CODE/apply" vs "/slug/j/CODE/apply"); the code pins it.
    if url_alnum:
        m = [j for j in jobs if url_alnum & _alnum_ids(j.get("apply_url") or "")]
        if m:
            return _best(m)
    # 2) exact URL / base equality
    m = [j for j in jobs
         if (j.get("apply_url") or "").split("?")[0]
         and ((j.get("apply_url") or "").split("?")[0] in cands
              or (j.get("apply_url") or "").split("?")[0] in bases)]
    if m:
        return _best(m)
    # 3) host + path-prefix match (last resort). NOT bare substring — that attached the WRONG
    #    job's résumé when a short stored URL happened to be a substring of an unrelated form URL.
    #    Require same host AND a non-trivial shared path; otherwise return None so the extension
    #    prompts the user rather than silently attaching a possibly-wrong résumé.
    from urllib.parse import urlparse
    cand_parsed = [urlparse(u) for u in cands if u]
    m = []
    for j in jobs:
        ja = j.get("apply_url") or ""
        if not ja:
            continue
        pj = urlparse(ja.split("?")[0])
        if not pj.netloc or len(pj.path) < 6:
            continue
        for pc in cand_parsed:
            if pc.netloc == pj.netloc and (pj.path.startswith(pc.path) or pc.path.startswith(pj.path)) \
               and min(len(pj.path), len(pc.path)) >= 6:
                m.append(j)
                break
    if m:
        return _best(m)
    return None


def _resume_path_for_job(job_id: str) -> Optional[Path]:
    """The tailored resume markdown path for the application matching this job, if any."""
    try:
        st = json.loads(_STATE.read_text())
    except Exception:
        return None
    app = next((a for a in st.get("applications", []) if a.get("job_id") == job_id), None)
    if not app or not app.get("tailored_resume_path"):
        return None
    p = Path(app["tailored_resume_path"])
    if not p.is_absolute():
        p = _ROOT / p
    return p if p.exists() else None


def _cover_md_for_job(job_id: str) -> Optional[str]:
    """The reviewed, bank-grounded cover letter text for this job's application, if one exists, so
    the extension fills a cover-letter field with the SAME letter the user saw — not a fresh draft."""
    try:
        st = json.loads(_STATE.read_text())
    except Exception:
        return None
    app = next((a for a in st.get("applications", []) if a.get("job_id") == job_id), None)
    if not app or not app.get("cover_letter_path"):
        return None
    p = Path(app["cover_letter_path"])
    if not p.is_absolute():
        p = _ROOT / p
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except Exception:
        return None


def _cover_pdf_for_job(job_id: str) -> Optional[Path]:
    """Render the reviewed cover letter as a clean business-letter PDF (letterhead + date + body)
    so it's presentable as a file attachment. Returns the PDF path, or None if no letter exists."""
    body = _cover_md_for_job(job_id)
    if not body:
        return None
    P = _load_profile()
    per = P.get("personal", {}) or {}
    name = per.get("name") or "Rahul Varikuppala"
    contact = "  |  ".join(x for x in (
        per.get("location", ""), per.get("phone", ""), per.get("email", ""),
        (per.get("linkedin_url", "") or "").replace("https://", "").replace("http://", ""),
    ) if x)
    from datetime import datetime
    today = datetime.now().strftime("%B %d, %Y")
    letter_md = f"# {name}\n{contact}\n\n{today}\n\n{body}\n"
    out_md = _ROOT / "data" / "cover_letters" / f"{job_id}_letter.md"
    out_md.write_text(letter_md, encoding="utf-8")
    try:
        from agents.application.form_filler import markdown_to_pdf
        return markdown_to_pdf(out_md)
    except Exception as e:
        logger.warning("cover_pdf_render_failed", job_id=job_id, error=str(e)[:90])
        return None


def _saved_answers_for_job(job_id: str) -> dict:
    """Open-ended answers previously drafted for this job's application, keyed by normalized question
    text → {question, answer, updated_at}. Lets a re-opened form REUSE the exact answer (no
    re-generation, no re-pay, identical wording) and gives the user a record of what was drafted."""
    try:
        st = json.loads(_STATE.read_text())
    except Exception:
        return {}
    app = next((a for a in st.get("applications", []) if a.get("job_id") == job_id), None)
    return (app or {}).get("form_answers", {}) or {}


def _save_answers_for_job(job_id: str, new_answers: dict) -> None:
    """Write-through: persist newly drafted answers under the application (race-safe, locked merge)."""
    if not new_answers:
        return
    try:
        from core.state_store import mutate_state
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        with mutate_state() as st:
            app = next((a for a in st.get("applications", []) if a.get("job_id") == job_id), None)
            if app is None:
                return
            fa = app.setdefault("form_answers", {})
            for nq, d in new_answers.items():
                fa[nq] = {"question": d["question"], "answer": d["answer"], "updated_at": ts}
    except Exception as e:
        logger.warning("form_answers_save_failed", job_id=job_id, error=str(e)[:90])


@router.post("/resolve")
async def resolve(body: ResolveIn) -> dict:
    P = _load_profile()
    job = _match_job(body.url, body.referrer, body.origin_url)
    resolved = [_resolve_one(f, P) for f in body.fields]

    # Cover-letter fields → fill with the REVIEWED bank-grounded letter (the one in the UI), not a
    # fresh draft, so the form gets exactly what the user approved. Falls back to drafting if none.
    _label_by_id = {f.id: (f.label or "") for f in body.fields}
    cover_md = _cover_md_for_job(job["id"]) if job else None
    gen_idx = [i for i, x in enumerate(resolved) if x.kind == "generate"]
    if cover_md:
        for i in list(gen_idx):
            if "cover letter" in _label_by_id.get(resolved[i].id, "").lower():
                resolved[i] = FillOut(id=resolved[i].id, kind="text", value=cover_md,
                                      note="your reviewed tailored cover letter — edit if you like")
                gen_idx.remove(i)

    # Open-ended questions: REUSE a saved answer if we drafted one for this role before (no
    # re-generation, no re-pay, identical wording), draft only the uncached ones in a single call,
    # then SAVE them under the application — so re-opening reuses them and you have a record.
    if gen_idx:
        saved = _saved_answers_for_job(job["id"]) if job else {}
        fresh_idx = []
        for i in gen_idx:
            nq = _norm_label(resolved[i].value)
            hit = saved.get(nq) if nq else None
            if hit and hit.get("answer"):
                resolved[i] = FillOut(id=resolved[i].id, kind="text", value=hit["answer"],
                                      note="saved answer (reused) — edit if you like")
            else:
                fresh_idx.append(i)
        if fresh_idx:
            questions = [resolved[i].value for i in fresh_idx]
            answers = await _generate_open_ended(questions, job, P)
            new_to_save = {}
            for i, ans in zip(fresh_idx, answers):
                fid = resolved[i].id
                q = _label_by_id.get(fid, "") or resolved[i].value   # capture question BEFORE reassign
                if ans:
                    resolved[i] = FillOut(id=fid, kind="text", value=ans,
                                          note="AI-drafted, saved for this role — review before submitting")
                    nq = _norm_label(q)
                    if nq:
                        new_to_save[nq] = {"question": q, "answer": ans}
                else:
                    resolved[i] = FillOut(id=fid, kind="skip", value="",
                                          note="open-ended — your call")
            if job and new_to_save:
                _save_answers_for_job(job["id"], new_to_save)

    fills = [x.model_dump() for x in resolved]
    filled = sum(1 for x in fills if x["kind"] in ("text", "value"))
    blank = sum(1 for x in fills if x["kind"] == "blank")
    needs = sum(1 for x in fills if x["kind"] == "skip")
    has_resume = bool(job and _resume_path_for_job(job["id"]))

    # Tailor-on-demand: opening a role's apply form is the "real one" signal — if it's parked but
    # not yet tailored, kick off Opus tailoring in the BACKGROUND now so the résumé is ready by the
    # time the user attaches it. The extension polls /resume (or re-resolves) until has_resume=true.
    tailoring = False
    if job and not has_resume:
        try:
            from agents.application.on_demand_tailor import tailor_application
            tailoring = _spawn_tailor(job["id"])
        except Exception:
            pass

    # DEBUG dump — OFF by default. It would otherwise write the user's filled VALUES (name, email,
    # phone, address, EEO + work-auth answers) to a world-readable /tmp file on every resolve. Only
    # when AH_DEBUG=1, write to a 0600 file under data/, and REDACT values (labels/kind/note only).
    try:
        if os.getenv("AH_DEBUG") == "1":
            dbg = {
                "url": body.url, "referrer": body.referrer,
                "job": (job or {}).get("company"),
                "fields": [{"label": f.label, "type": f.type, "options": f.options} for f in body.fields],
                "fills": [{"label": next((f.label for f in body.fields if f.id == x["id"]), ""),
                           "kind": x["kind"], "note": x.get("note", "")} for x in fills],  # NO value
                "summary": {"total": len(fills), "filled": filled, "needs_you": needs},
            }
            p = Path("data/ah_autofill_debug.json")
            p.write_text(json.dumps(dbg, indent=2))
            try:
                os.chmod(p, 0o600)
            except Exception:
                pass
        logger.info("autofill_resolve", total=len(fills), filled=filled, needs_you=needs,
                    job=(job or {}).get("company"))
    except Exception:
        pass

    return {
        "job": {"company": job["company"], "title": job["title"]} if job else None,
        "has_resume": has_resume,         # tells the extension a tailored resume is available
        "has_cover": bool(cover_md),      # a reviewed cover letter was used for a cover-letter field
        "tailoring": tailoring,           # résumé being generated on demand — poll /resume until ready
        "fills": fills,
        "summary": {"total": len(fills), "filled": filled, "needs_you": needs, "blank": blank},
    }


class ReportField(BaseModel):
    label: str = ""
    kind: str = ""             # widget kind: text|select|radio|checkbox|react
    type: str = ""             # scraped field type
    options_count: int = 0
    value_kind: str = ""       # resolver fill kind: value|text|skip
    committed: bool = False
    reason: str = ""


class ReportIn(BaseModel):
    url: str = ""
    host: str = ""
    job: Optional[dict] = None
    total: int = 0
    filled: int = 0
    needs_you: int = 0
    fields: list[ReportField] = []   # the failures + resolver-skips (committed=False)


_REPORTS = _ROOT / "data" / "autofill_reports.jsonl"


@router.post("/report")
async def report(body: ReportIn) -> dict:
    """Telemetry sink. After filling, the extension VERIFIES each field against the live DOM and
    posts the outcome here so we can fix CLASSES of forms from data instead of one screenshot at a
    time. Privacy: only labels / widget kinds / reasons are sent — NEVER the user's filled values.
    Appended to data/autofill_reports.jsonl (gitignored, 0600)."""
    from datetime import datetime, timezone
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "host": body.host,
        "url": body.url,
        "job": body.job,
        "counts": {"total": body.total, "filled": body.filled, "needs_you": body.needs_you},
        "failures": [f.model_dump() for f in body.fields],
    }
    try:
        _REPORTS.parent.mkdir(parents=True, exist_ok=True)
        with _REPORTS.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        try:
            os.chmod(_REPORTS, 0o600)
        except Exception:
            pass
    except Exception as e:
        logger.warning("autofill_report_write_failed", error=str(e)[:120])
    logger.info("autofill_report", host=body.host, total=body.total, filled=body.filled,
                needs_you=body.needs_you, failures=len(rec["failures"]))
    return {"ok": True, "recorded": len(rec["failures"])}


@router.get("/resume")
def resume(url: str = "", referrer: str = "", origin: str = ""):
    """Serve the tailored resume PDF for the job whose apply_url matches `url`/`referrer`/`origin`.
    The extension attaches it to the form's file input (different resume per application)."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    job = _match_job(url, referrer, origin)
    if not job:
        raise HTTPException(404, "no job matched this URL")
    md = _resume_path_for_job(job["id"])
    if not md:
        raise HTTPException(404, "no tailored resume for this job")
    # Convert the tailored markdown → PDF (cached next to the .md by markdown_to_pdf).
    try:
        from agents.application.form_filler import markdown_to_pdf
        pdf = markdown_to_pdf(md)
    except Exception as e:
        raise HTTPException(500, f"resume render failed: {e}")
    P = _load_profile()
    safe = (P.get("personal", {}).get("name", "Resume") or "Resume").replace(" ", "_")
    # inline so the extension's "View attached resume" link opens it in a tab (the form
    # submits whatever the extension attached — this is the same bytes).
    return FileResponse(str(pdf), media_type="application/pdf",
                        filename=f"{safe}_Resume.pdf", content_disposition_type="inline")


@router.get("/cover")
def cover(url: str = "", referrer: str = "", origin: str = ""):
    """Serve the reviewed cover letter as a formatted PDF for the job matching `url`/`referrer`/`origin`.
    The extension attaches it to a cover-letter file input (same bytes the form submits)."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    job = _match_job(url, referrer, origin)
    if not job:
        raise HTTPException(404, "no job matched this URL")
    pdf = _cover_pdf_for_job(job["id"])
    if not pdf:
        raise HTTPException(404, "no cover letter for this job")
    P = _load_profile()
    safe = (P.get("personal", {}).get("name", "Cover") or "Cover").replace(" ", "_")
    return FileResponse(str(pdf), media_type="application/pdf",
                        filename=f"{safe}_Cover_Letter.pdf", content_disposition_type="inline")


class DomDumpIn(BaseModel):
    url: str = ""
    fields: list[dict] = []


@router.post("/domdump")
async def domdump(body: DomDumpIn) -> dict:
    """DEBUG: receive a structural snapshot of a form's fields (tag/role/class/label/outerHTML) so a
    bespoke ATS form (e.g. apply.teksystems.com's custom widgets) can be reverse-engineered. Writes
    data/ah_domdump.json (gitignored). One-shot, triggered by the extension's capture button."""
    p = Path("data/ah_domdump.json")
    try:
        p.write_text(json.dumps({"url": body.url, "count": len(body.fields), "fields": body.fields}, indent=2))
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    return {"ok": True, "saved": str(p), "fields": len(body.fields)}


class CoverGenIn(BaseModel):
    url: str = ""
    referrer: str = ""


_COVER_IN_FLIGHT: set = set()


@router.post("/cover/generate")
async def cover_generate(body: CoverGenIn) -> dict:
    """On-demand cover letter. The pipeline only pre-writes one for SOME roles (strong fit / posting
    asks / customer-facing / early-stage). When a form actually HAS a cover-letter field, that's the
    strongest signal one is wanted — so generate + persist it now, with the same bank-grounded,
    truthful writer the pipeline uses. Returns {ok, status: exists|generated|in_progress}."""
    from fastapi import HTTPException
    matched = _match_job(body.url, body.referrer)
    if not matched:
        raise HTTPException(404, "no job matched this URL")
    job_id = matched["id"]
    if _cover_md_for_job(job_id):                       # already have one — nothing to do
        return {"ok": True, "status": "exists"}
    if job_id in _COVER_IN_FLIGHT:
        return {"ok": True, "status": "in_progress"}
    _COVER_IN_FLIGHT.add(job_id)
    try:
        st = json.loads(_STATE.read_text())
        job = next((j for j in st.get("job_listings", []) if j.get("id") == job_id), None)
        app = next((a for a in st.get("applications", []) if a.get("job_id") == job_id), None)
        if not job:
            raise HTTPException(404, "job listing not found")
        if not app:
            raise HTTPException(409, "no application row to attach the cover letter to")
        from core.llm import get_llm
        from agents.application.resume_tailor import _cover_letter, _COVER_DIR, _clean_md
        llm = get_llm("on_demand_tailor")              # exempt from the budget cap (autofill path)
        cover_md = await _cover_letter(job, _load_profile(), llm, None)
        if not cover_md:
            raise HTTPException(500, "cover generation returned nothing")
        _COVER_DIR.mkdir(parents=True, exist_ok=True)
        cp = _COVER_DIR / f"{job_id}.md"
        cp.write_text(_clean_md(cover_md), encoding="utf-8")
        from core.state_store import mutate_state
        with mutate_state() as state:
            a = next((x for x in state.get("applications", []) if x.get("job_id") == job_id), None)
            if a is not None:
                a["cover_letter_path"] = str(cp)
                a["cover_letter_reason"] = "form has a cover-letter field (on-demand)"
        logger.info("cover_generated_on_demand", job_id=job_id, company=job.get("company_name"))
        return {"ok": True, "status": "generated"}
    finally:
        _COVER_IN_FLIGHT.discard(job_id)
