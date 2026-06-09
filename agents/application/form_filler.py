"""
Form Filler Agent
Submits tailored job applications through ATS portals using Playwright.

ATS detection order:
  greenhouse.io / boards.greenhouse.io → Greenhouse
  jobs.lever.co / lever.co             → Lever
  myworkdayjobs.com / workday.com      → Workday (best-effort)
  *                                    → Generic field-selector approach

HUMAN GATE: Every submission requires explicit approval via checkpoints.py.
Prepares a preview, pauses until the user approves in dashboard/CLI,
then submits and captures the confirmation URL.
"""

import asyncio
import email as _email_mod
import imaplib
import json
import re
import uuid
import yaml
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from core.state import Application, JobListing, JobSearchState
from core.config import get_settings
from core.logger import get_logger
from core.llm import get_llm
from core.checkpoints import request_approval, await_decision
from tools.browser import BrowserClient
from tools.pdf_parser import markdown_to_pdf

logger = get_logger("form_filler")

_SYSTEM_PROMPT = Path("prompts/system/form_filler.txt").read_text()
_APPLIED_LOG   = Path("data/applied_jobs.json")
_ATS_COMPAT_FILE = Path("data/ats_compatibility.json")
_SCREENSHOT_DIR = Path("data/screenshots")
_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
_PROFILE_PATH  = Path("data/user_profile.yaml")


def _load_ats_compat() -> dict:
    """Load ATS compatibility history: company → {ats, auto_submitted_count, manual_count}."""
    if _ATS_COMPAT_FILE.exists():
        try:
            return json.loads(_ATS_COMPAT_FILE.read_text())
        except Exception:
            pass
    return {}


def _record_ats_outcome(company: str, ats: str, submitted: bool) -> None:
    """Persist whether a company's ATS was auto-submittable."""
    compat = _load_ats_compat()
    entry = compat.setdefault(company, {"ats": ats, "auto": 0, "manual": 0})
    entry["ats"] = ats
    if submitted:
        entry["auto"] += 1
    else:
        entry["manual"] += 1
    _ATS_COMPAT_FILE.write_text(json.dumps(compat, indent=2))


# ── ATS Detection ─────────────────────────────────────────────────────────────

def _phone_digits(phone: str) -> str:
    """Return phone as 10 digits only (strips +1 country code and formatting).
    Greenhouse/Lever/Ashby render the country-code in a separate dropdown,
    so the number field expects exactly 10 digits with no punctuation."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]   # strip leading country code 1
    return digits


def detect_ats(url: str) -> str:
    """Identify which ATS system the apply URL points to."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    # Tier-1: exact hostname matches (most reliable)
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "workday" in host or "myworkdayjobs" in host:
        return "workday"
    if "ashbyhq.com" in host or "ashby.com" in host:
        return "ashby"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "icims.com" in host:
        return "icims"
    if "bamboohr.com" in host:
        return "bamboohr"
    if "rippling.com" in host and ("jobs" in host or "jobs" in path or "career" in path):
        return "rippling"
    if "news.ycombinator.com" in host or "ycombinator.com" in host:
        return "manual"  # HN Who's Hiring — no ATS form

    # Tier-2: path/query hints for company-hosted embeds
    if "greenhouse" in path or "gh_jid" in url:
        return "greenhouse"
    if "lever" in path:
        return "lever"
    if "ashby" in path:
        return "ashby"
    if "smartrecruiters" in path:
        return "smartrecruiters"

    return "generic"


# ── Work-authorization (country-aware) ─────────────────────────────────────────

# Country names / hints that indicate a role is NOT in the United States.
# Work-authorization answers in the profile are US-centric, so for a non-US role
# the candidate is NOT authorized and DOES require sponsorship.
_NON_US_COUNTRIES = (
    "canada", "united kingdom", "uk", "england", "scotland", "ireland", "germany",
    "france", "spain", "italy", "netherlands", "switzerland", "sweden", "poland",
    "australia", "new zealand", "india", "singapore", "japan", "china", "brazil",
    "mexico", "argentina", "israel", "uae", "dubai", "portugal", "belgium",
    "denmark", "norway", "finland", "austria", "czech", "romania", "ukraine",
)
_US_HINTS = ("united states", "usa", "u.s.", "u.s.a", " us ", ", us", "remote - us",
             "remote, us", "us-remote", "us remote", "america")


def _role_country(job: "JobListing", question: str = "") -> str:
    """
    Best-effort: return 'us' or a non-US country slug for the role this
    authorization question is about. The question text (e.g. "...work in Canada?")
    takes priority over the job's location field.
    """
    q = (question or "").lower()
    for c in _NON_US_COUNTRIES:
        if c in q:
            return c
    if any(h in q for h in _US_HINTS):
        return "us"
    loc = (job.get("location", "") or "").lower()
    for c in _NON_US_COUNTRIES:
        if c in loc:
            return c
    # Default to US (profile is US-authorized) when nothing else matches.
    return "us"


def _work_auth_answer(question: str, job: "JobListing", work_auth: dict, kind: str) -> str:
    """
    Country-aware work-authorization answer.

    kind = 'authorized'  → "Are you legally authorized to work in <country>?"
    kind = 'sponsorship' → "Do you (now/future) require sponsorship in <country>?"

    For a US role we use the profile's US answers. For a role in a country where
    the candidate is NOT authorized, authorized→No and sponsorship→Yes — answering
    a blanket "Yes" to authorization abroad is false and risks the application.
    """
    country = _role_country(job, question)
    if country == "us":
        if kind == "sponsorship":
            return work_auth.get("sponsorship_answer", "Yes")
        return work_auth.get("authorized_answer", "Yes")
    # Non-US role: not authorized, needs sponsorship.
    return "No" if kind == "authorized" else "Yes"


# ── Work-authorization sentence-option picker (shared across ATS fillers) ────────
# Some ATS render the work-auth question as a dropdown of full SENTENCES
# ("...due to my nationality" vs "...on a visa that needs sponsorship") instead of
# Yes/No. A plain "Yes" matches nothing, so the dropdown logic picks the first/wrong
# option. These helpers score the option text against the candidate's TRUE status —
# authorized to work (on a visa) but DOES require sponsorship — so any filler can pick
# the truthful option (or leave it blank rather than commit a false legal answer).

def _is_workauth_label(label_lower: str) -> bool:
    """True for a work-authorization question (not the separate sponsorship Yes/No)."""
    return (("authoriz" in label_lower or "authoris" in label_lower)
            and ("work" in label_lower or "employ" in label_lower)
            and "sponsor" not in label_lower)


def _score_workauth_option(text: str) -> int:
    """Higher = more truthful for a visa holder who IS authorized but NEEDS sponsorship."""
    t = (text or "").lower()
    if any(p in t for p in ("not authorized", "not currently authorized",
                            "do not have authorization", "unauthorized",
                            "not eligible", "not legally")):
        return -100
    if any(p in t for p in ("nationality", "citizen", "permanent resident", "green card")):
        return -50
    has_sponsor = "sponsor" in t
    negated = has_sponsor and any(n in t for n in (
        "do not need", "don't need", "dont need", "do not require", "don't require",
        "dont require", "not need", "not require", "no need", "without sponsor",
        "no sponsor", "won't need", "will not need", "does not require"))
    if negated:
        return -40
    s = 0
    if any(p in t for p in ("authorized", "authorised", "eligible", "permitted")):
        s += 2
    if any(p in t for p in ("visa", "work permit", "work authorization",
                            "employment authorization", "ead")):
        s += 1
    if has_sponsor and any(p in t for p in ("need", "require", "will need", "do need")):
        s += 5
    return s


def _pick_truthful_workauth(option_texts: list[str]) -> int | None:
    """Index of the truthful work-auth option, or None to leave blank.

    Returns None when options are basically Yes/No (let the caller's existing logic
    answer "Yes"), or when no option scores positively (don't commit a false answer)."""
    texts = [(t or "").strip().lower() for t in option_texts]
    nonempty = [t for t in texts if t]
    if not nonempty:
        return None
    if all(len(t) <= 6 for t in nonempty) or {t for t in nonempty} <= {"yes", "no", "n/a"}:
        return None
    best_i, best_s = None, 0
    for i, t in enumerate(texts):
        if not t:
            continue
        sc = _score_workauth_option(t)
        if sc > best_s:
            best_s, best_i = sc, i
    return best_i


def _workauth_outcome(texts: list[str]) -> tuple[str, int | None]:
    """Decide what to do with a work-auth option list:
      ('select', idx) → pick that truthful option
      ('blank',  None) → sentence options but none truthful → leave blank for the human
      ('defer',  None) → plain Yes/No menu → let the caller's existing logic answer."""
    idx = _pick_truthful_workauth(texts)
    if idx is not None:
        return ("select", idx)
    nonempty = [t for t in (texts or []) if t and t.strip()]
    is_sentences = bool(nonempty) and not (
        all(len(t) <= 6 for t in nonempty)
        or {t.lower() for t in nonempty} <= {"yes", "no", "n/a"})
    return ("blank" if is_sentences else "defer", None)


async def _select_truthful_workauth_native(sel_el) -> bool:
    """Pick the truthful option on a native <select> work-auth field. Returns True if
    handled (selected or deliberately left blank), False to defer to Yes/No logic."""
    try:
        texts = await sel_el.evaluate(
            "el => Array.from(el.options).map(o => o.textContent.trim())")
    except Exception:
        return False
    action, idx = _workauth_outcome(texts)
    if action == "defer":
        return False
    if action == "blank":
        return True
    try:
        await sel_el.select_option(label=texts[idx])
        return True
    except Exception:
        try:
            await sel_el.select_option(index=idx)
            return True
        except Exception:
            return False


async def _select_truthful_workauth_radios(radio_label_pairs) -> bool:
    """Pick the truthful option in a work-auth RADIO group.
    radio_label_pairs: list of (radio_locator, label_text). Returns True if handled."""
    texts = [lbl for _, lbl in radio_label_pairs]
    action, idx = _workauth_outcome(texts)
    if action == "defer":
        return False
    if action == "blank":
        return True
    try:
        await radio_label_pairs[idx][0].check()
        return True
    except Exception:
        return False


async def _select_truthful_workauth_react(contexts) -> bool:
    """Pick the truthful option in an OPEN React-Select work-auth menu (.select__option)
    across the given frame/page contexts. Returns True if handled."""
    opts = []
    for ctx in contexts:
        if ctx is None:
            continue
        try:
            els = await ctx.locator(".select__menu .select__option, [class*='__menu'] [class*='__option']").all()
            for e in els:
                t = (await e.inner_text()).strip()
                if t:
                    opts.append((e, t))
            if opts:
                break
        except Exception:
            pass
    if not opts:
        return False
    action, idx = _workauth_outcome([t for _, t in opts])
    if action == "defer":
        return False
    if action == "blank":
        return True
    el = opts[idx][0]
    try:
        await el.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        await el.click()
        return True
    except Exception:
        return False


async def _answer_workauth_radios(page) -> None:
    """Find work-authorization RADIO groups anywhere on the page and pick the truthful
    option. Groups radios by `name`; only acts on a group whose question label is a
    work-auth question and that isn't already answered. Shared across ATS fillers."""
    try:
        radios = await page.locator("input[type='radio']:visible").all()
    except Exception:
        return
    groups: dict = {}
    order: list = []
    for r in radios:
        try:
            nm = await r.get_attribute("name") or ""
        except Exception:
            nm = ""
        if nm not in groups:
            groups[nm] = []
            order.append(nm)
        groups[nm].append(r)
    for nm in order:
        rlist = groups[nm]
        if len(rlist) < 2:
            continue
        try:
            qlabel = await rlist[0].evaluate("""el => {
                const fs = el.closest('fieldset');
                if (fs) { const lg = fs.querySelector('legend'); if (lg && lg.innerText.trim().length>5) return lg.innerText.trim(); }
                let p = el;
                for (let i=0;i<9;i++){ p=p.parentElement; if(!p) break;
                    const l = p.querySelector('label,legend,[class*="label"],[class*="question"]');
                    if (l && l.innerText.trim().length>8 && !l.querySelector('input')) return l.innerText.trim(); }
                return ''; }""") or ""
        except Exception:
            qlabel = ""
        if not _is_workauth_label(qlabel.lower()):
            continue
        already = False
        for r in rlist:
            try:
                if await r.is_checked():
                    already = True
                    break
            except Exception:
                pass
        if already:
            continue
        pairs = []
        for r in rlist:
            lbl = ""
            try:
                rid = await r.get_attribute("id")
                if rid:
                    le = page.locator(f"label[for='{rid}']")
                    if await le.count() > 0:
                        lbl = (await le.first.inner_text()).strip()
                if not lbl:
                    lbl = (await r.evaluate(
                        "el => { const p = el.closest('label'); return p ? p.textContent.trim() : ''; }")) or ""
            except Exception:
                pass
            pairs.append((r, lbl))
        if any(lbl for _, lbl in pairs):
            try:
                handled = await _select_truthful_workauth_radios(pairs)
                if handled:
                    logger.info("workauth_radio_handled", question=qlabel[:70])
            except Exception:
                pass


# ── EEO / demographic answers ──────────────────────────────────────────────────
# The candidate's self-identification. Forms vary wildly in option wording, so each
# field carries an ORDERED list of acceptable option texts — the filler picks the
# first that EXACTLY matches an option on the form (exact-only avoids "Asian"
# matching "East Asian", or "Man" matching "Woman"). Coarse standard categories come
# first (e.g. plain "Asian"), then the granular variant that fits the candidate
# (e.g. "South Asian"). Order matters.
_DEMOGRAPHIC_CANDIDATES: dict[str, list[str]] = {
    "gender":      ["Male", "Man", "Cisgender man", "Cisgender Man"],
    "race":        ["Asian", "South Asian"],
    "veteran":     ["I am not a protected veteran", "I have never served in the military",
                    "I am not", "Not a protected veteran", "No"],
    "disability":  ["No, I don't have a disability",
                    "No, I do not have a disability and have not had one in the past",
                    "No, I don't have a disability and have not had one in the past",
                    "No", "I don't have a disability"],
    "orientation": ["Heterosexual", "Straight", "Heterosexual/Straight"],
    "lgbtq":       ["No"],
    "hispanic":    ["No", "Not Hispanic or Latino", "Not Hispanic or Latino/a"],
}


def _demographic_candidates(label_lower: str) -> list[str] | None:
    """Return the ordered option-text candidates for a demographic field, or None."""
    if "pronoun" in label_lower:
        return None                                   # pronouns handled separately (He/Him)
    if "lgbtq" in label_lower:
        return _DEMOGRAPHIC_CANDIDATES["lgbtq"]
    if any(k in label_lower for k in ("hispanic", "latino", "latina", "latinx")):
        return _DEMOGRAPHIC_CANDIDATES["hispanic"]
    if "gender" in label_lower:
        return _DEMOGRAPHIC_CANDIDATES["gender"]
    if any(k in label_lower for k in ("race", "ethnic")):
        return _DEMOGRAPHIC_CANDIDATES["race"]
    if any(k in label_lower for k in ("veteran", "military")):
        return _DEMOGRAPHIC_CANDIDATES["veteran"]
    if "disabilit" in label_lower:
        return _DEMOGRAPHIC_CANDIDATES["disability"]
    if "orientation" in label_lower:
        return _DEMOGRAPHIC_CANDIDATES["orientation"]
    return None


# ── LLM answer for custom questions ───────────────────────────────────────────

async def _answer_question(question: str, job: JobListing, llm) -> dict:
    """Use LLM to answer a custom application question."""
    messages = [{
        "role": "user",
        "content": (
            f"Question: {question}\n\n"
            f"Job Title: {job['title']}\n"
            f"Company: {job['company_name']}\n\n"
            f"Job Description (excerpt):\n{job['description'][:1000]}\n\n"
            "Keep the answer concise — at most ~120 words for open questions."
        ),
    }]
    try:
        # Larger budget so long open-ended answers ("Why do you want to work here?")
        # aren't truncated mid-string, which previously broke JSON parsing.
        return await llm.chat_json(_SYSTEM_PROMPT, messages, max_tokens=1024, temperature=0.1)
    except Exception as e:
        # Salvage: a truncated/invalid-JSON response still has a usable answer. Fall
        # back to a plain-text completion rather than dropping the field entirely.
        logger.warning("custom_question_json_failed", question=question[:80], error=str(e)[:120])
        try:
            text = await llm.chat(
                "Answer this job-application question for the candidate in 2-4 sentences, "
                "first person, plain text only (no JSON, no preamble).",
                messages, max_tokens=400, temperature=0.3,
            )
            text = (text or "").strip()
            if text:
                return {"answer": text, "field_type": "textarea", "confidence": 0.6}
        except Exception as e2:
            logger.warning("custom_question_failed", question=question[:80], error=str(e2)[:120])
        return {"answer": "", "field_type": "text", "confidence": 0.0}


_cover_letter_cache: dict[str, str] = {}

async def _generate_cover_letter(job: JobListing, profile: dict, llm) -> str:
    """Generate a concise, tailored 3-paragraph cover letter for this job."""
    cache_key = job["id"]
    if cache_key in _cover_letter_cache:
        return _cover_letter_cache[cache_key]

    p = profile["personal"]
    d = profile.get("application_defaults", {})
    wh = profile.get("work_history", [{}])[0]

    system = (
        "You are a professional cover letter writer. Write a concise 3-paragraph cover letter "
        "(under 200 words total). No placeholders, no brackets. Use the candidate's real details. "
        "Do NOT start with 'Dear Hiring Manager'. Start directly with a strong opening sentence. "
        "Tone: confident, specific, human — not robotic."
    )
    messages = [{
        "role": "user",
        "content": (
            f"Candidate: {p['name']}, {d.get('current_title','Senior Software Engineer')} "
            f"at {d.get('current_company','JPMorgan Chase')}, {d.get('years_of_experience','6')} years experience.\n"
            f"Role: {job['title']} at {job['company_name']}\n\n"
            f"Job Description (excerpt):\n{job['description'][:1500]}\n\n"
            "Write the cover letter now. Plain text only, no markdown."
        ),
    }]
    try:
        resp = await llm.chat(system, messages, max_tokens=400, temperature=0.4)
        text = resp.strip() if isinstance(resp, str) else str(resp)
        _cover_letter_cache[cache_key] = text
        return text
    except Exception as e:
        logger.warning("cover_letter_generation_failed", error=str(e))
        return ""


# ── Greenhouse Filler ──────────────────────────────────────────────────────────

async def _fill_greenhouse(page: Page, profile: dict, resume_pdf: Path, job: JobListing, llm) -> bool:
    """Fill a Greenhouse application form. Returns True if form ready to submit."""
    # Some companies embed Greenhouse via iframe (SoFi, Databricks, Upstart, Abnormal Security…).
    # Strategy:
    #  1. Wait for iframe with greenhouse.io/embed in URL — covers standard embeds.
    #  2. If no iframe yet and the apply_url has a gh_jid= param, navigate directly to the
    #     Greenhouse board URL so the form loads as a first-class page rather than an iframe.
    #     This covers company-domain career pages like databricks.com?gh_jid=85210.
    # Save the outer page so we can access page.context (Frame objects lack .context).
    from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs
    outer_page = page
    await asyncio.sleep(2.0)  # let iframes finish loading

    def _find_greenhouse_frame():
        for f in outer_page.frames:
            if "greenhouse.io/embed" in f.url or "greenhouse.io" in f.url:
                return f
        return None

    gh_frame = _find_greenhouse_frame()
    if gh_frame:
        page = gh_frame  # type: ignore[assignment]
        await asyncio.sleep(1.0)
    else:
        # No iframe — check if the apply_url embeds a gh_jid query param
        # e.g. databricks.com/careers/job?gh_jid=85210  or  careers.upstart.com/jobs?gh_jid=7821481
        _parsed = _urlparse(job.get("apply_url", ""))
        _qs = _parse_qs(_parsed.query)
        _gh_jid = (_qs.get("gh_jid") or [""])[0]
        if _gh_jid and "greenhouse.io" not in _parsed.netloc:
            # Navigate directly to the embed form — bypasses the company landing page
            _embed_url = f"https://boards.greenhouse.io/embed/job_app?token={_gh_jid}"
            try:
                await outer_page.goto(_embed_url, wait_until="domcontentloaded", timeout=20_000)
                await asyncio.sleep(2.0)
                gh_frame = _find_greenhouse_frame()
                page = gh_frame if gh_frame else outer_page  # type: ignore[assignment]
            except Exception:
                pass  # fall through and fill whatever is loaded

    # Frame objects don't have .keyboard in Playwright — always use the outer page's
    # keyboard so keystrokes reach whichever element currently has focus (incl. iframe inputs).
    keyboard = outer_page.keyboard

    p    = profile["personal"]
    defs = profile.get("application_defaults", {})
    work_auth = profile.get("work_authorization", {})
    name_parts = p.get("name", "").split()
    first_name = name_parts[0] if name_parts else ""
    last_name  = " ".join(name_parts[1:]) or "."
    # Phone as 10 digits — Greenhouse has a separate +1 country-code dropdown
    phone_digits = _phone_digits(p.get("phone", ""))

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _fill(selector: str, value: str) -> bool:
        try:
            el = page.locator(selector).first
            if await el.count() > 0:
                await el.fill(value)
                await asyncio.sleep(0.2)
                return True
        except Exception:
            pass
        return False

    async def _select(selector: str, *labels: str) -> bool:
        try:
            el = page.locator(selector).first
            if await el.count() > 0:
                for label in labels:
                    try:
                        await el.select_option(label=label)
                        await asyncio.sleep(0.2)
                        return True
                    except Exception:
                        pass
        except Exception:
            pass
        return False

    async def _check(selector: str) -> bool:
        try:
            el = page.locator(selector).first
            if await el.count() > 0 and not await el.is_checked():
                await el.check()
                await asyncio.sleep(0.2)
                return True
        except Exception:
            pass
        return False

    # ── Standard fields ───────────────────────────────────────────────────────
    await _fill("#first_name",     first_name)
    await _fill("#last_name",      last_name)
    await _fill("#preferred_name", first_name)   # required on many Greenhouse forms
    await _fill("#email",          p.get("email", ""))
    await _fill("#phone",          phone_digits)
    await _fill("#phone_number",   phone_digits)
    await _fill("input[id*='linkedin']",   p.get("linkedin_url", ""))
    await _fill("input[name*='linkedin']", p.get("linkedin_url", ""))
    await _fill("input[id*='github']",     p.get("github_url", ""))
    await _fill("input[name*='website']",  p.get("github_url", ""))

    # Country — Greenhouse uses a text-input autocomplete for country
    for country_sel in ["#job_application_location", "input[id*='country']",
                        "input[name*='country']", "input[placeholder*='Country']",
                        "input[placeholder*='country']"]:
        if await _fill(country_sel, "United States"):
            await asyncio.sleep(0.5)
            try:
                suggestion = page.locator("li[role='option'], .autocomplete-option, [data-value*='United States']").first
                if await suggestion.count() > 0:
                    await suggestion.click()
                    await asyncio.sleep(0.3)
            except Exception:
                pass
            break

    # (location filled at end of function, after all React Select fills)

    # ── Resume upload ──────────────────────────────────────────────────────────
    # Verify the file actually attached (the input reports a file, or the form shows
    # the filename / clears its "required" error) and retry once — a too-early
    # screenshot was showing "Resume/CV is required" even when the upload was pending.
    async def _resume_attached(el) -> bool:
        try:
            cnt = await el.evaluate("n => (n.files && n.files.length) || 0")
            return bool(cnt)
        except Exception:
            return False

    resume_uploaded = False
    for upload_sel in ["input[type='file'][name*='resume']",
                       "input[type='file'][id*='resume']",
                       "input[type='file']"]:
        try:
            el = page.locator(upload_sel).first
            if await el.count() > 0:
                for attempt in range(2):
                    await el.set_input_files(str(resume_pdf))
                    await asyncio.sleep(2.0)
                    if await _resume_attached(el):
                        resume_uploaded = True
                        break
                if resume_uploaded:
                    logger.info("resume_uploaded", ats="greenhouse", path=str(resume_pdf))
                    break
        except Exception:
            pass
    if not resume_uploaded:
        logger.warning("resume_upload_failed", ats="greenhouse")

    # ── Cover letter ──────────────────────────────────────────────────────────
    cover_letter_text = ""
    if defs.get("cover_letter") == "auto":
        cover_letter_text = await _generate_cover_letter(job, profile, llm)
    for cl_sel in ["#cover_letter", "textarea[name*='cover']", "textarea[id*='cover']"]:
        if cover_letter_text:
            if await _fill(cl_sel, cover_letter_text):
                break

    # Wait for all dynamic content to load before scanning custom questions
    await asyncio.sleep(2.0)

    # ── Custom questions: label → known answer lookup ─────────────────────────
    # Maps label substrings → answer. Checked top-to-bottom, first match wins.
    LABEL_ANSWERS: list[tuple[tuple[str, ...], str]] = [
        # Personal
        (("first name",),                                   first_name),
        (("last name",),                                    last_name),
        (("preferred name", "preferred first"),             first_name),
        (("pronunciation", "name pronun", "personal preference", "pronounce your name"), "Rah-ool"),
        (("pronoun",),                                      defs.get("pronouns", "He/Him")),
        # Contact / location — home address fields (matched before generic city/zip)
        (("home address line 1", "address line 1", "street address"), "[REDACTED ADDRESS]"),
        (("address from which you plan on working", "address where you will work",
          "address you plan on working"), "[REDACTED ADDRESS]"),
        (("home address line 2", "address line 2", "apt,", "suite,", "unit,"), ""),
        (("city",),                                         "McKinney"),
        (("state", "province"),                             "Texas"),
        (("country",),                                      "United States"),
        (("postal", "zip"),                                 "00000"),
        # Work history
        (("current company", "current employer"),           defs.get("current_company", "JPMorgan Chase")),
        (("current title", "current role", "current position"), defs.get("current_title", "Senior Software Engineer")),
        (("years of experience", "years experience"),       defs.get("years_of_experience", "6")),
        # Spoken languages (scoped to human languages, not "programming languages")
        (("languages you speak", "languages do you speak", "spoken language",
          "which languages do you speak", "what languages do you speak",
          "fluent in", "native language", "languages spoken"),
         defs.get("languages", "English, Hindi, Telugu, Urdu")),
        # Social / portfolio
        (("linkedin",),                                     p.get("linkedin_url", "")),
        (("github",),                                       p.get("github_url", "")),
        (("website", "portfolio"),                          p.get("website_url", p.get("github_url", ""))),
        # Application specifics
        (("hear about", "learn about", "how did you find", "referral source", "how did you first"), defs.get("referral_source", "LinkedIn")),
        (("start date", "available to start", "earliest start", "when can you start",
          "earliest you would want to start", "start working with us"), defs.get("start_date", "Immediately")),
        (("deadlines or timeline", "timeline considerations"),      ""),
        (("salary", "compensation", "pay expectation", "desired pay"), defs.get("salary_display", "$150,000 – $200,000")),
        (("cover letter",),                                 cover_letter_text),
        # Work authorization
        (("legally authorized", "authorized to lawfully work", "authorized to work", "work in the united states", "eligible to work"), "Yes"),
        (("require sponsorship", "visa sponsorship", "need sponsorship", "now or in the future require"), "Yes"),
        (("previously employed", "previously worked", "worked at", "former employee"), "No"),
        # Date fields
        (("today's date", "date of application", "application date"), datetime.now().strftime("%m/%d/%y")),
        # Company-specific compliance questions (SoFi / Galileo)
        (("worked at or been a consultant for sofi", "consultant for sofi", "sofi entity", "galileo financial"), "No"),
        (("currently employed with or have been employed by deloitte", "deloitte"), "No"),
        (("currently a sofi", "sofi employee", "galileo or technisys employee", "galileo or technisys"), "No"),
        (("marketing communications about careers", "career at sofi", "marketing communications"), "No"),
        (("receive text messages from sofi", "sms policy", "text messages from sofi"), "Yes"),
        (("what finra license", "finra license(s), if any"), "N/A"),   # checkbox group: check N/A
        (("finra license", "series 7", "series 24"), "No"),             # yes/no question
        # Export compliance (Twilio / others)
        (("cuba", "iran", "north korea", "syria", "crimea"),  "No"),
        # EEOC — self-identified answers
        (("gender identity", "gender",),                   "Prefer not to say"),
        (("race", "ethnicity"),                             "Asian"),
        (("hispanic", "latino"),                            "No"),
        (("veteran",),                                      "I am not a protected veteran"),
        (("disability",),                                   "No"),
        (("sexual orientation",),                           "Straight"),
    ]

    def _known_answer(label_lower: str) -> str | None:
        for keywords, answer in LABEL_ANSWERS:
            if any(k in label_lower for k in keywords):
                return answer
        return None

    async def _fill_block(block) -> bool:
        """Fill one li.field block. Returns True if filled."""
        label_el = block.locator("label").first
        label_text = (await label_el.inner_text()).strip() if await label_el.count() > 0 else ""
        label_lower = label_text.lower()
        if not label_lower:
            return False

        answer = _known_answer(label_lower)

        # Text / textarea
        text_el = block.locator("input[type='text'], input[type='number'], textarea").first
        if await text_el.count() > 0:
            # Don't re-fill a field a prior pass already populated (e.g. the country
            # autocomplete) — fill() on a React-controlled input can APPEND, producing
            # garbage like "United StatesUnited S".
            try:
                if (await text_el.input_value()).strip():
                    return True
            except Exception:
                pass
            if answer is not None:
                if answer:
                    await text_el.fill(answer)
                return True
            # LLM fallback
            result = await _answer_question(label_text, job, llm)
            ans = result.get("answer", "")
            if ans:
                await text_el.fill(ans)
            return True

        # Select
        sel_el = block.locator("select").first
        if await sel_el.count() > 0:
            # Work-auth dropdowns with sentence options: pick the truthful one.
            if _is_workauth_label(label_lower) and await _select_truthful_workauth_native(sel_el):
                return True
            val = answer or ""
            if val:
                for method in ("label", "value"):
                    try:
                        if method == "label":
                            await sel_el.select_option(label=val)
                        else:
                            await sel_el.select_option(value=val)
                        return True
                    except Exception:
                        pass
            # Try "Prefer not to say" / "Decline" as EEOC fallback
            for fallback in ("Prefer not to say", "Decline to self-identify", "I don't wish to answer",
                             "I am not a protected veteran"):
                try:
                    await sel_el.select_option(label=fallback)
                    return True
                except Exception:
                    pass
            return False

        # Radio
        radios = await block.locator("input[type='radio']").all()
        if radios:
            val = (answer or "").lower()
            for r in radios:
                try:
                    rid = await r.get_attribute("id")
                    lbl = ""
                    if rid:
                        lel = page.locator(f"label[for='{rid}']")
                        if await lel.count() > 0:
                            lbl = (await lel.first.inner_text()).strip().lower()
                    if val and (lbl == val or val[:3] in lbl or lbl[:3] in val):
                        await r.check()
                        return True
                except Exception:
                    pass
            return False

        # Checkbox
        checks = await block.locator("input[type='checkbox']").all()
        if checks:
            # Check the first option that matches the answer label
            val = (answer or "").lower()
            for c in checks:
                try:
                    cid = await c.get_attribute("id")
                    lbl = ""
                    if cid:
                        lel = page.locator(f"label[for='{cid}']")
                        if await lel.count() > 0:
                            lbl = (await lel.first.inner_text()).strip().lower()
                    # Also try wrapping <label> parent (Greenhouse embed style)
                    if not lbl:
                        lbl = await c.evaluate("""el => {
                            let p = el.parentElement;
                            for (let i = 0; i < 5; i++) {
                                if (!p) break;
                                if (p.tagName === 'LABEL') return p.innerText.trim().toLowerCase();
                                p = p.parentElement;
                            }
                            const sib = el.nextSibling;
                            if (sib && sib.nodeType === 3) return sib.textContent.trim().toLowerCase();
                            return '';
                        }""")
                    if val and (val == lbl or val in lbl):
                        await c.check()
                        return True
                except Exception:
                    pass

        return False

    # li.field = classic Greenhouse; div.field = embed; [class*='field'] catches new job-boards.greenhouse.io
    question_blocks = await page.locator(
        "li.field, div.field, [class*='application-field'], [class*='question-field'], "
        "[class*='form-field'], [data-qa*='field']"
    ).all()
    for block in question_blocks:
        try:
            await _fill_block(block)
        except Exception as e:
            logger.warning("custom_field_error", error=str(e)[:100])

    # ── Standalone text inputs not inside li.field ─────────────────────────────
    # Some Greenhouse forms render questions as bare inputs with an associated label.
    all_inputs = await page.locator("input[type='text']:not([id='first_name']):not([id='last_name'])"
                                    ":not([id='email']):not([type='hidden'])").all()
    for inp in all_inputs:
        try:
            inp_id = await inp.get_attribute("id") or ""
            inp_name = await inp.get_attribute("name") or ""
            inp_ph = await inp.get_attribute("placeholder") or ""
            # Find associated label
            label_text = ""
            if inp_id:
                lel = page.locator(f"label[for='{inp_id}']")
                if await lel.count() > 0:
                    label_text = (await lel.first.inner_text()).strip()
            label_lower = (label_text or inp_ph or inp_name).lower()
            if not label_lower:
                continue
            # Skip already-filled standard fields
            if any(k in label_lower for k in ("first name", "last name", "email", "phone",
                                               "resume", "search")):
                continue
            answer = _known_answer(label_lower)
            if answer:
                current_val = await inp.input_value()
                if not current_val:  # only fill if empty
                    await inp.fill(answer)
                    await asyncio.sleep(0.2)
        except Exception:
            pass

    # ── Standalone textareas not inside li.field/div.field ───────────────────────
    # New Greenhouse Job Boards (job-boards.greenhouse.io) renders open questions as
    # bare <textarea> elements outside li.field — e.g. "Why Anthropic?", "Why this role?"
    all_textareas = await page.locator(
        "textarea:not([id='cover_letter']):not([id='cover-letter']):not([name*='cover'])"
    ).all()
    for ta in all_textareas:
        try:
            cur = await ta.input_value()
            if cur:
                continue  # already filled
            ta_id = await ta.get_attribute("id") or ""
            ta_ph = await ta.get_attribute("placeholder") or ""
            label_text = ""
            if ta_id:
                lel = page.locator(f"label[for='{ta_id}']")
                if await lel.count() > 0:
                    label_text = (await lel.first.inner_text()).strip()
            if not label_text:
                label_text = await ta.evaluate("""el => {
                    let p = el;
                    for (let i = 0; i < 8; i++) {
                        p = p.parentElement;
                        if (!p) break;
                        const lbl = p.querySelector('label');
                        if (lbl && lbl.innerText.trim().length > 2) return lbl.innerText.trim();
                    }
                    return '';
                }""")
            label_lower = (label_text or ta_ph or "").lower()
            if not label_lower:
                continue
            answer = _known_answer(label_lower)
            if answer is not None:
                if answer:
                    await ta.fill(answer)
                    await asyncio.sleep(0.2)
            else:
                # LLM fallback for open questions like "Why this company?"
                result = await _answer_question(label_text or label_lower, job, llm)
                ans = result.get("answer", "")
                if ans:
                    await ta.fill(ans)
                    await asyncio.sleep(0.2)
        except Exception:
            pass

    # ── Standalone native <select> fields not caught by li.field/_fill_block ────
    # Covers fields like "home address country" which use <select> outside li.field.
    all_selects = await page.locator("select").all()
    for sel_el in all_selects:
        try:
            # Skip if already has a non-default value
            current = await sel_el.input_value()
            if current and current not in ("", "0", "-1"):
                continue
            sel_id = await sel_el.get_attribute("id") or ""
            label_text = ""
            if sel_id:
                lel = page.locator(f"label[for='{sel_id}']")
                if await lel.count() > 0:
                    label_text = (await lel.first.inner_text()).strip()
            if not label_text:
                label_text = await sel_el.evaluate("""el => {
                    let p = el.parentElement;
                    for (let i = 0; i < 10; i++) {
                        if (!p) break;
                        const lbl = p.querySelector('label:not([for]), legend');
                        if (lbl && lbl.innerText.trim().length > 2) return lbl.innerText.trim();
                        p = p.parentElement;
                    }
                    return '';
                }""")
            if not label_text:
                continue
            label_lower = label_text.lower()
            if any(k in label_lower for k in ("first name", "last name", "email", "phone")):
                continue
            # Work-auth dropdowns with sentence options: pick the truthful one.
            if _is_workauth_label(label_lower) and await _select_truthful_workauth_native(sel_el):
                continue
            answer = _known_answer(label_lower)
            if not answer:
                continue
            for method in ("label", "value", "partial"):
                try:
                    if method == "label":
                        await sel_el.select_option(label=answer)
                    elif method == "value":
                        await sel_el.select_option(value=answer)
                    else:
                        # partial match: find option whose text contains the answer
                        opts = await sel_el.locator("option").all()
                        for o in opts:
                            txt = (await o.inner_text()).strip()
                            if answer.lower() in txt.lower():
                                val = await o.get_attribute("value") or txt
                                await sel_el.select_option(value=val)
                                break
                    break
                except Exception:
                    pass
        except Exception:
            pass

    # ── Checkboxes: "How did you hear?" multi-select + consent ────────────────
    # Twilio and others show a checklist — tick the matching source
    source = defs.get("referral_source", "LinkedIn")
    all_checkboxes = await page.locator("input[type='checkbox']").all()
    for cb in all_checkboxes:
        try:
            cid = await cb.get_attribute("id") or ""
            lbl_text = ""
            # 1. Try label[for=id] association
            if cid:
                lel = page.locator(f"label[for='{cid}']")
                if await lel.count() > 0:
                    lbl_text = (await lel.first.inner_text()).strip()
            # 2. Try wrapping <label> parent (common in Greenhouse EEOC section)
            if not lbl_text:
                lbl_text = await cb.evaluate("""el => {
                    let p = el.parentElement;
                    for (let i = 0; i < 5; i++) {
                        if (!p) break;
                        if (p.tagName === 'LABEL') return p.innerText.trim();
                        p = p.parentElement;
                    }
                    // Also check next sibling text
                    const sib = el.nextSibling;
                    if (sib && sib.nodeType === 3) return sib.textContent.trim();
                    return '';
                }""")
            lbl_lower = (lbl_text or "").lower()

            # Source checkbox (e.g. LinkedIn, Careers Website)
            if source.lower() in lbl_lower:
                await cb.check()
                continue
            # Consent / acknowledge checkboxes — always check
            if any(k in lbl_lower for k in ("consent", "acknowledge", "agree", "i have read",
                                             "certify", "attest", "collecting", "storing")):
                await cb.check()
                continue
            # GDPR / data processing
            if any(k in lbl_lower for k in ("data processing", "privacy", "gdpr")):
                await cb.check()
                continue
            # LABEL_ANSWERS-driven: check "N/A"/"None" checkbox in known question groups
            # (e.g. "What FINRA license(s)..." → check "N/A")
            if lbl_lower in ("n/a", "none", "none of the above"):
                in_known_group = await cb.evaluate("""el => {
                    let p = el;
                    for (let i = 0; i < 20; i++) {
                        if (!p) break;
                        const txt = (p.innerText || '').toLowerCase();
                        if (txt.includes('finra')) return true;
                        p = p.parentElement;
                    }
                    return false;
                }""")
                if in_known_group and not await cb.is_checked():
                    await cb.check()
        except Exception:
            pass

    # Truthful, profile-driven answers (relocation preference + citizenship status).
    _reloc = "Yes" if profile.get("target", {}).get("relocation") else "No"
    _citizen = "No" if profile.get("work_authorization", {}).get("requires_sponsorship") else "Yes"

    # ── Global radio scan (Greenhouse embed custom yes/no questions) ────────────────
    # _fill_block only processes li.field/div.field containers; this catches radio groups
    # in any container (e.g. "reasonable commute" questions in SoFi embedded forms).
    _radio_answers: list[tuple[tuple[str, ...], str]] = [
        (("citizen or green card", "citizen or permanent resident", "u.s. citizen or",
          "us citizen or", "a citizen or", "citizen/green card"), _citizen),
        (("reasonable commute", "located within", "planning to relocate"), "Yes"),
        (("authorized to lawfully work", "legally authorized", "eligible to work"), "Yes"),
        (("require sponsorship", "visa sponsorship", "now or in the future require"), "Yes"),
        (("worked at or been a consultant for sofi", "sofi entity", "galileo financial"), "No"),
        (("currently employed with or have been employed by deloitte", "deloitte"), "No"),
        (("currently a sofi", "sofi, galileo or technisys employee"), "No"),
        (("marketing communications about careers",), "No"),
        (("receive text messages from sofi", "sms policy"), "Yes"),
        (("hold any finra licenses", "finra licenses if employed"), "No"),
        # Anthropic-specific
        (("open to working in-person", "25% of the time"),         "Yes"),
        (("ai policy for application", "ai policy"),               "Yes"),
        (("terminated at anthropic", "previously terminated at"),  "No"),
        (("interviewed at anthropic", "previously interviewed at anthropic"), "No"),
        (("open to relocation", "willing to relocate"),            _reloc),
    ]
    try:
        from collections import defaultdict as _dd
        radio_groups: dict[str, list] = _dd(list)
        for r in await page.locator("input[type='radio']").all():
            rname = (await r.get_attribute("name")) or ""
            radio_groups[rname].append(r)
        for group_name, radios in radio_groups.items():
            try:
                if not radios or any([await r.is_checked() for r in radios]):
                    continue
                group_label = await radios[0].evaluate("""el => {
                    let p = el.parentElement;
                    for (let i = 0; i < 15; i++) {
                        if (!p) break;
                        // Try non-for label (question text), legend, or .label element
                        for (const sel of ['legend', 'label:not([for])', '.label', 'p.question']) {
                            const lbl = p.querySelector(sel);
                            if (lbl && lbl.innerText.trim().length > 5)
                                return lbl.innerText.trim().toLowerCase();
                        }
                        p = p.parentElement;
                    }
                    return '';
                }""")
                if not group_label:
                    continue
                answer = None
                for keywords, ans in _radio_answers:
                    if any(k in group_label for k in keywords):
                        answer = ans
                        break
                if not answer:
                    continue
                val = answer.lower()
                for r in radios:
                    r_lbl = await r.evaluate("""el => {
                        let p = el.parentElement;
                        for (let i = 0; i < 5; i++) {
                            if (!p) break;
                            if (p.tagName === 'LABEL') return p.innerText.trim().toLowerCase();
                            p = p.parentElement;
                        }
                        if (el.id) {
                            const l = document.querySelector('label[for="' + el.id + '"]');
                            if (l) return l.innerText.trim().toLowerCase();
                        }
                        return '';
                    }""")
                    if val and (val == r_lbl or val[:3] in r_lbl):
                        await r.check()
                        break
            except Exception:
                pass
    except Exception:
        pass

    async def _select_truthful_workauth(ctrl) -> bool:
        """Work-auth React Select with sentence options: pick the truthful option via the
        shared helper (the menu is already open). Returns True if handled."""
        return await _select_truthful_workauth_react((page, outer_page))

    # ── All React Select dropdowns (covers standard required fields + EEOC) ──────
    # Greenhouse uses React Select (custom component, not native <select>) for many fields.
    # We find every .select__control, read its label, and fill with the known answer.
    _rs_map: list[tuple[tuple[str, ...], str]] = [
        # Required application fields
        (("pronoun",),                                              "He/"),
        # Work authorization — must come before generic "authorized to work" and before "country"
        # Label: "Are you authorized to lawfully work in the country where this role is located?"
        # Note: "authorized to work" alone doesn't match because "lawfully" breaks the substring.
        (("authorized to lawfully work",),                         "Yes"),
        (("legally authorized", "legally authorised", "authorized to work", "authorised to work"), "Yes"),
        (("require sponsorship", "visa sponsorship", "now or in the future"), "Yes"),
        (("previously been employed", "previously employed at"),   "I have not"),
        (("cuba", "iran", "north korea", "syria", "crimea"),       "No"),
        (("learn about", "hear about", "how did you first"),       "LinkedIn"),
        (("state", "province"),                                    "Texas"),
        # Education
        (("degree",),                                             "Master's Degree"),
        (("discipline", "field of study", "major"),              "Computer Information Systems"),
        # EEOC — must come BEFORE ("location", "city") because Greenhouse EEOC labels
        # sometimes include "location" in regulatory fine print, causing false matches.
        (("gender identity",),                                     "Male"),
        (("race/ethnicity", "race", "ethnicity"),                  "Asian"),
        (("how do you identify",),                                 "Male"),
        (("hispanic", "latino"),                                   "No"),
        (("veteran",),                                             "I am not"),
        (("disability",),                                          "No, I do not"),
        (("sexual orientation", "orientation"),                    "Heterosexual"),
        (("gender",),                                              "Male"),
        # Commute / relocation — must come before ("location", "city") since "locations" is a
        # substring of the commute question: "...any of this job's listed hiring locations?"
        # "open to relocation" also comes before ("location", "city") because "relocation"
        # contains "location" as a substring, so the more-specific entry must match first.
        (("reasonable commute", "located within", "planning to relocate"), "Yes"),
        (("open to relocation", "willing to relocate"),                    _reloc),
        # Citizenship / green-card status — distinct from "authorized to work" (must
        # come before the generic ("country",) entry). Truthful per work_authorization.
        (("citizen or green card", "citizen or permanent resident", "u.s. citizen or",
          "us citizen or", "a citizen or", "citizen/green card"),          _citizen),
        # Location fallback (for embedded forms where geosuggest doesn't apply)
        (("location", "city"),                                     "Plano"),
        # Residence country — "Which country do you currently reside in?"
        (("currently reside in", "country do you currently reside", "which country do you reside"), "United States"),
        # Home address country — specific enough to not match "in the country where this role..."
        (("home address country", "address country"),              "United States"),
        # Datadog-specific required fields
        (("certify that the information provided", "certify that the above"),  "Yes"),
        (("candidate privacy policy", "privacy policy"),                       "Yes"),
        (("languages you speak fluently", "speak fluently", "languages do you speak"), "English"),
        (("cities are you available to work", "cities available"),             "New York"),
        # Company-specific compliance questions (SoFi / Galileo)
        (("worked at or been a consultant for sofi", "sofi entity", "galileo financial"), "No"),
        (("currently employed with or have been employed by deloitte", "deloitte"), "No"),
        (("currently a sofi", "sofi, galileo or technisys employee"), "No"),
        (("marketing communications about careers",),              "No"),
        (("receive text messages from sofi", "sms policy"),        "Yes"),
        (("hold any finra licenses", "finra licenses if employed"), "No"),
        (("what finra license", "finra license(s), if any"),       "N/A"),
        # Anthropic-specific required fields
        (("open to working in-person", "25% of the time"),         "Yes"),
        (("ai policy for application", "ai policy"),               "Yes"),
        (("terminated at anthropic", "previously terminated at"),  "No"),
        (("interviewed at anthropic", "previously interviewed at anthropic"), "No"),
        (("built or owned deployment", "deployment/release infrastructure", "deployment infrastructure at a large-scale"), "Yes"),
        # MongoDB-specific
        (("worked at mongodb", "worked at mongodb before", "ever worked at mongodb"), "No"),
        # Robinhood-specific compliance — options are granular sentences, not Yes/No;
        # the correct choice is the "never worked" full option text.
        (("worked for robinhood", "worked at robinhood", "employee, contractor, or vendor"),
         "I have never worked at Robinhood"),
        (("personal/familial relationship", "personal or familial", "relationship with anyone at robinhood"), "No"),
        (("adheres to applicable laws", "equal employment opportunity", "eeo policy"), "I Agree"),
        # Office / in-person attendance
        (("willing to work from the office", "work from the office", "work in-person", "work on-site", "office attendance"), "Yes"),
        (("preferred office location", "which office location", "office preference"), "New York"),
        # Country — plain "Country*" label on Quick Apply forms (must come AFTER work-auth entries
        # above so "authorized to work in the country where..." matches work-auth first)
        (("country",), "United States"),
    ]

    # Labels that trigger the geocode-autocomplete path (Greenhouse geosuggest).
    # Use startswith-style substrings so "...hiring locations?" doesn't match.
    _location_keywords = ("location (city", "location*", "location :")

    # Scroll through the full form before React Select loop so all sections render
    try:
        for scroll_y in range(0, 8000, 600):
            await page.evaluate(f"window.scrollTo(0, {scroll_y})")
            await asyncio.sleep(0.15)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)
    except Exception:
        pass

    # React Select: try .select__control first (standard), then broader BEM selector
    _rs_sel = ".select__control, [class*='__control'][class*='select']"
    controls = await page.locator(_rs_sel).all()
    # Also check outer_page for any controls not in the frame
    if outer_page is not page:
        try:
            outer_ctrls = await outer_page.locator(_rs_sel).all()
            controls = controls + [c for c in outer_ctrls if c not in controls]
        except Exception:
            pass
    for ctrl in controls:
        try:
            # Skip if already has a value selected
            has_val = await ctrl.locator(".select__single-value, .select__multi-value").count()
            if has_val > 0:
                continue
            # Get the label — check aria-labelledby first, then walk DOM for label/p/div
            label_lower = await ctrl.evaluate("""el => {
                // Placeholder text returned by aria-labelledby — not useful as a label
                const PLACEHOLDERS = ['select...', 'please select', 'choose...', 'select one', '- select -'];
                // React Select's built-in ARIA accessibility text (e.g. Datadog embedded Greenhouse forms)
                const RS_NOISE = ['type to refine list', 'press down to open the menu'];
                const isPlaceholder = t => PLACEHOLDERS.some(p => t.startsWith(p)) || RS_NOISE.some(p => t.includes(p));

                // 1. aria-labelledby on the inner input (skip generic placeholder elements)
                const inp = el.querySelector('input');
                if (inp) {
                    const lby = inp.getAttribute('aria-labelledby') || '';
                    for (const id of lby.split(' ')) {
                        const ref = id && document.getElementById(id);
                        if (ref) {
                            const txt = ref.innerText.trim().toLowerCase();
                            if (txt.length > 5 && !isPlaceholder(txt))
                                return txt;
                        }
                    }
                    // Also check aria-label directly on the input
                    const al = (inp.getAttribute('aria-label') || '').trim().toLowerCase();
                    if (al.length > 5 && !isPlaceholder(al))
                        return al;
                }
                // 2. Walk up looking for label, then any non-input text block
                let p = el;
                for (let i = 0; i < 14; i++) {
                    p = p.parentElement;
                    if (!p) break;
                    const lbl = p.querySelector('label');
                    if (lbl && lbl.innerText.trim().length > 2)
                        return lbl.innerText.trim().toLowerCase();
                    // fallback: div/p that has question text but no nested select
                    for (const sel of ['legend', 'div.question', 'p.question', '.question-label',
                                       '[class*="label"]', '[class*="question-text"]', '[class*="field-label"]']) {
                        const q = p.querySelector(sel);
                        if (q && q.innerText.trim().length > 8 && !q.querySelector('.select__control')) {
                            const txt = q.innerText.trim().toLowerCase();
                            if (!isPlaceholder(txt)) return txt;
                        }
                    }
                }
                return '';
            }""")
            if not label_lower:
                continue
            answer = None
            for keywords, ans in _rs_map:
                if any(k in label_lower for k in keywords):
                    answer = ans
                    break
            if not answer:
                continue
            is_location = any(k in label_lower for k in _location_keywords)
            # Demographic / EEO fields: company option labels are granular
            # ("Cisgender man", "East Asian", "South Asian") and rarely equal the
            # profile's coarse value, so a blind ArrowDown+Enter commits FALSE
            # protected-class data. For these we only accept an exact option match
            # and otherwise leave the field blank for the human to complete.
            is_demographic = ("pronoun" not in label_lower) and any(
                k in label_lower for k in (
                    "gender", "race", "ethnic", "veteran", "disabilit",
                    "orientation", "lgbtq", "hispanic", "latino"))
            await ctrl.click()
            await asyncio.sleep(0.4)

            # Get the inner input for direct key dispatch (works in cross-origin iframes)
            rs_input = ctrl.locator("input").first

            # Work-authorization dropdowns sometimes use full-SENTENCE options
            # (e.g. "...due to my nationality." vs "...require visa sponsorship")
            # instead of Yes/No. The answer "Yes" then matches nothing and the generic
            # logic picks the FIRST option ("...nationality"), which is FALSE for a visa
            # holder who needs sponsorship. Pick the truthful option (or leave blank).
            _is_workauth = (("authoriz" in label_lower or "authoris" in label_lower)
                            and ("work" in label_lower or "employ" in label_lower)
                            and "sponsor" not in label_lower)
            if _is_workauth and await _select_truthful_workauth(ctrl):
                try:
                    if await rs_input.count() > 0:
                        await rs_input.press("Escape")
                    else:
                        await keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(0.2)
                continue

            if is_location:
                # Location fields use async geocoding APIs.
                # After typing, React Select auto-focuses the first matching option.
                # Press Enter directly to select it (ArrowDown would skip to the second).
                if await rs_input.count() > 0:
                    await rs_input.type("Plano")
                else:
                    await keyboard.type("Plano")
                await asyncio.sleep(2.0)  # wait for geocoding API to return suggestions
                if await rs_input.count() > 0:
                    await rs_input.press("Enter")
                else:
                    await keyboard.press("Enter")
                await asyncio.sleep(0.3)
            else:
                # Scope to .select__menu to avoid stale options from previously-closed dropdowns
                import re as _re
                async def _find_rs_opt(text: str):
                    for ctx in (page, outer_page):
                        try:
                            # Exact text match via regex to avoid "Male" matching "Female"
                            o = ctx.locator(".select__menu .select__option").filter(
                                has_text=_re.compile(rf'^\s*{_re.escape(text)}\s*$', _re.IGNORECASE)
                            ).first
                            if await o.count() > 0:
                                return o
                        except Exception:
                            pass
                        # Substring fallback (get_by_text exact=False) is UNSAFE for
                        # demographics — "Asian" would match "East Asian"/"South Asian".
                        # Restrict it to non-protected fields.
                        if is_demographic:
                            continue
                        try:
                            # Fallback: get_by_text handles apostrophes that break CSS :has-text
                            o = ctx.locator(".select__menu").get_by_text(text, exact=False).first
                            if await o.count() > 0:
                                return o
                        except Exception:
                            pass
                    return None

                async def _first_open_opt():
                    for ctx in (page, outer_page):
                        try:
                            o = ctx.locator(".select__menu .select__option").first
                            if await o.count() > 0:
                                return o
                        except Exception:
                            pass
                    return None

                # Extra wait for the menu to render after click
                await asyncio.sleep(0.3)
                opt = await _find_rs_opt(answer)
                if opt is None or await opt.count() == 0:
                    # Type to filter then re-search — use element-level type for cross-origin iframe
                    if await rs_input.count() > 0:
                        await rs_input.type(answer)
                    else:
                        await keyboard.type(answer)
                    await asyncio.sleep(0.6)
                    opt = await _find_rs_opt(answer)
                    if (opt is None or await opt.count() == 0) and not is_demographic:
                        # Non-demographic only: a blind first-option pick is acceptable for
                        # Yes/No/Country-style fields but never for protected-class fields.
                        opt = await _first_open_opt()
                        if opt is None or await opt.count() == 0:
                            # Typed text filtered the list to nothing — clear and take first option
                            try:
                                if await rs_input.count() > 0:
                                    await rs_input.fill("")
                                    await asyncio.sleep(0.3)
                                opt = await _first_open_opt()
                            except Exception:
                                pass

                if opt and await opt.count() > 0:
                    try:
                        await opt.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    await opt.click()
                    await asyncio.sleep(0.4)
                    # Verify the value was committed — some React Select versions need
                    # keyboard confirmation to trigger onChange (e.g. MongoDB/Datadog Greenhouse)
                    committed = await ctrl.locator(".select__single-value").count() > 0
                    if not committed and not is_demographic:
                        # Reopen and select via keyboard (more reliable React onChange trigger).
                        # Skipped for demographics: ArrowDown picks an arbitrary protected-class
                        # value (e.g. "Cisgender woman") that would be false.
                        await ctrl.click()
                        await asyncio.sleep(0.3)
                        if await rs_input.count() > 0:
                            await rs_input.type(answer[:8])
                        else:
                            await keyboard.type(answer[:8])
                        await asyncio.sleep(0.5)
                        if await rs_input.count() > 0:
                            await rs_input.press("ArrowDown")
                            await asyncio.sleep(0.1)
                            await rs_input.press("Enter")
                        else:
                            await keyboard.press("ArrowDown")
                            await asyncio.sleep(0.1)
                            await keyboard.press("Enter")
                        await asyncio.sleep(0.3)
                elif not is_demographic:
                    # Last resort: keyboard nav — use element-level press for cross-origin iframes.
                    # Demographics are left blank rather than blind-selected.
                    if await rs_input.count() > 0:
                        await rs_input.press("ArrowDown")
                        await asyncio.sleep(0.3)
                        await rs_input.press("Enter")
                    else:
                        await keyboard.press("ArrowDown")
                        await asyncio.sleep(0.3)
                        await keyboard.press("Enter")
                    await asyncio.sleep(0.2)
                else:
                    # Demographic field with no exact option match — close menu, leave blank.
                    try:
                        await keyboard.press("Escape")
                    except Exception:
                        pass
            # Close any remaining open dropdown
            try:
                if await page.locator(".select__menu").count() > 0:
                    if await rs_input.count() > 0:
                        await rs_input.press("Escape")
                    else:
                        await keyboard.press("Escape")
                    await asyncio.sleep(0.1)
            except Exception:
                pass
            await asyncio.sleep(0.3)
        except Exception:
            try:
                await keyboard.press("Escape")
            except Exception:
                pass

    # Native <select> fallback — handles Quick Apply and older Greenhouse forms that
    # use <select> elements instead of React Select for country, work-auth, and EEOC.
    _native_select_map = [
        # Country
        (["select[name*='country']", "select[id*='country']", "select[aria-label*='Country']",
          "select[aria-label*='country']"],
         ["United States", "United States of America", "US"]),
        # Work authorization
        (["select[name*='authorized']", "select[id*='authorized']",
          "select[name*='work_auth']", "select[aria-label*='authorized']",
          "select[aria-label*='legally']"],
         ["Yes"]),
        (["select[name*='sponsor']", "select[id*='sponsor']",
          "select[aria-label*='sponsorship']", "select[aria-label*='visa']"],
         [work_auth.get("sponsorship_answer", "Yes")]),
        # EEOC
        (["select[id*='gender']", "select[name*='gender']", "select[aria-label*='gender']"],
         [defs.get("eeoc_gender", "Male"), "Man", "Male", "Prefer not to say"]),
        (["select[id*='race']", "select[name*='race']", "select[id*='ethnicity']",
          "select[aria-label*='race']", "select[aria-label*='ethnicity']"],
         [defs.get("eeoc_race", "Asian"), "Asian", "Prefer not to say"]),
        (["select[id*='veteran']", "select[name*='veteran']", "select[id*='military']",
          "select[aria-label*='veteran']", "select[aria-label*='military']"],
         [defs.get("eeoc_veteran", "I am not a protected veteran"),
          "I am not a protected veteran", "I am not", "Prefer not to say"]),
        (["select[id*='disability']", "select[name*='disability']",
          "select[aria-label*='disability']"],
         [defs.get("eeoc_disability", "No"),
          "No, I do not have a disability",
          "No, I do not have a disability and have not had one in the past",
          "Prefer not to say"]),
    ]
    for selectors, options in _native_select_map:
        _is_wa_group = any(k in " ".join(selectors) for k in ("authorized", "work_auth", "legally"))
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() == 0:
                    continue
                # Work-auth selects may use sentence options — pick the truthful one
                # instead of blindly trying "Yes" (which matches nothing → wrong/blank).
                if _is_wa_group and await _select_truthful_workauth_native(el):
                    break
                for opt in options:
                    try:
                        await el.select_option(label=opt)
                        await asyncio.sleep(0.2)
                        break
                    except Exception:
                        try:
                            await el.select_option(value=opt.lower())
                            await asyncio.sleep(0.2)
                            break
                        except Exception:
                            pass
                break
            except Exception:
                pass

    # Also call shared _fill_dropdowns to catch any native selects not matched above
    await _fill_dropdowns(page, profile)

    # ── Location (City) ────────────────────────────────────────────────────────────
    # Greenhouse's #candidate-location uses a geosuggest component that requires a
    # real selection from its autocomplete to pass validation.
    # Skip this block if the React Select loop above already committed a location value.
    _location_rs_filled = False
    try:
        # Check if any React Select near a location/city label already has a value
        rs_vals = await page.locator(".select__single-value").all()
        for rv in rs_vals:
            try:
                lbl = await rv.evaluate("""el => {
                    let p = el;
                    for (let i = 0; i < 10; i++) {
                        p = p.parentElement; if (!p) break;
                        const l = p.querySelector('label');
                        if (l) return l.innerText.toLowerCase();
                    }
                    return '';
                }""")
                if any(k in lbl for k in ("location", "city")):
                    _location_rs_filled = True
                    break
            except Exception:
                pass
    except Exception:
        pass

    if not _location_rs_filled:
        for loc_sel in ["#candidate-location", "input[id*='city']"]:
            el = page.locator(loc_sel).first
            if await el.count() == 0:
                continue
            try:
                # Grant mock geolocation so "Locate me" returns Plano, TX coordinates
                # Use outer_page.context — Frame objects don't have .context directly.
                await outer_page.context.grant_permissions(["geolocation"])
                await outer_page.context.set_geolocation({"latitude": 33.0198431, "longitude": -96.6988856})

                locate_btn = page.locator("a:has-text('Locate me'), button:has-text('Locate me')").first
                if await locate_btn.count() > 0:
                    await locate_btn.click()
                    # Wait up to 5 s for the component to reverse-geocode and fill the field
                    for _ in range(10):
                        await asyncio.sleep(0.5)
                        val = await el.input_value()
                        if val and len(val) > 3:
                            break
                    val = await el.input_value()
                    if val and len(val) > 3:
                        break  # Geolocation approach worked — stop here

                # Fallback: type the location and wait for autocomplete suggestions
                await el.click()
                await asyncio.sleep(0.2)
                # Type slowly to trigger the geosuggest API
                for char in "Plano, TX":
                    await keyboard.type(char)
                    await asyncio.sleep(0.15)
                _suggestion_sel = (
                    ".pac-item, "
                    "[class*='geosuggest__item'], "
                    "[class*='suggestion-item'], "
                    "[class*='autocomplete-item'], "
                    ".location-suggestion, "
                    "[data-testid*='suggestion']"
                )
                # Google Places Autocomplete appends its suggestions to the outer page's
                # <body> (not inside the iframe), so search in outer_page, not page.
                for _ in range(10):
                    await asyncio.sleep(0.5)
                    opts = await outer_page.locator(_suggestion_sel).all()
                    if opts:
                        await opts[0].click()
                        break
            except Exception:
                pass
            break

    # ── get_by_label fallback for Quick Apply dropdowns ───────────────────────
    # Quick Apply on job-boards.greenhouse.io may use a different React Select
    # class name. Use get_by_label() to find the hidden input by label text,
    # then click its parent container to open the dropdown and select an option.
    _label_dropdown_map: list[tuple[str, str]] = [
        ("Country",               "United States"),
        ("Resident Country",      "United States"),
        ("Country of Residence",  "United States"),
        ("authorized to work",    work_auth.get("authorized_answer", "Yes")),
        ("legally authorized",    work_auth.get("authorized_answer", "Yes")),
        ("require sponsorship",   work_auth.get("sponsorship_answer", "Yes")),
        ("visa sponsorship",      work_auth.get("sponsorship_answer", "Yes")),
        ("willing to work from the office", "Yes"),
        ("preferred office location",       "New York"),
        ("worked for",  "No"),
        ("worked at",   "No"),
        ("relationship", "No"),
        # NOTE: EEO/demographic fields are deliberately NOT listed here. This map's
        # last-resort is a blind "press Enter to accept first filtered option", which
        # for protected-class fields commits a FALSE value (e.g. "Cisgender woman"
        # when the answer is "Male"). Demographics are handled only by the main
        # React-Select loop / force pass, which require an exact option match and
        # otherwise leave the field blank for the human to complete.
    ]

    async def _try_label_dropdown(label_fragment: str, option_text: str) -> bool:
        """Find control by label fragment, click its container, then select option."""
        try:
            el = page.get_by_label(label_fragment, exact=False)
            if await el.count() == 0:
                return False
            inp = el.first
            # If it's a native select, use select_option
            tag = await inp.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                for opt in [option_text, option_text.split()[0]]:
                    try:
                        await inp.select_option(label=opt)
                        return True
                    except Exception:
                        pass
                return False

            # React Select: click the parent container (which opens the dropdown)
            container = await inp.evaluate_handle(
                "el => { let p = el; for(let i=0;i<5;i++){p=p.parentElement;"
                "if(!p) break; if(p.className&&(p.className.includes('control')||"
                "p.className.includes('select'))) return p;} return el.parentElement; }"
            )
            if container:
                await container.as_element().click()
                await asyncio.sleep(0.5)
            else:
                await inp.click()
                await asyncio.sleep(0.5)

            # Type to filter
            await keyboard.type(option_text[:8])
            await asyncio.sleep(0.6)

            # Click first visible option matching our text
            # Exclude ITI phone-country picker items (iti__country / iti__divider)
            for ctx in (page, outer_page):
                opt_el = ctx.locator(
                    "[role='option']:not(.iti__country):not(.iti__divider),"
                    ".select__option,"
                    "[class*='option']:not([class*='iti'])"
                ).filter(has_text=option_text[:12]).first
                if await opt_el.count() > 0:
                    await opt_el.click()
                    await asyncio.sleep(0.3)
                    return True
            # Fallback: press Enter to accept first filtered option
            await keyboard.press("Enter")
            await asyncio.sleep(0.3)
            return True
        except Exception:
            return False

    for label_frag, option_val in _label_dropdown_map:
        try:
            await asyncio.wait_for(_try_label_dropdown(label_frag, option_val), timeout=6.0)
        except (asyncio.TimeoutError, Exception):
            pass

    # ── Final force-commit pass for React Select dropdowns ──────────────────────
    # Newer Greenhouse "remix-css" React Select (e.g. Robinhood boards.greenhouse.io)
    # renders options under classes the menu-click path doesn't match, so values
    # never commit. Typing the answer then ArrowDown+Enter reliably commits — do a
    # final sweep over every still-empty .select__control matched to _rs_map.
    _LABEL_JS = """el => {
        const PH=['select...','please select','choose...','select one','- select -'];
        const NOISE=['type to refine list','press down to open the menu'];
        const isP=t=>PH.some(p=>t.startsWith(p))||NOISE.some(p=>t.includes(p));
        const inp=el.querySelector('input');
        if(inp){const lby=inp.getAttribute('aria-labelledby')||'';
          for(const id of lby.split(' ')){const ref=id&&document.getElementById(id);
            if(ref){const t=ref.innerText.trim().toLowerCase(); if(t.length>3&&!isP(t))return t;}}
          const al=(inp.getAttribute('aria-label')||'').trim().toLowerCase(); if(al.length>3&&!isP(al))return al;}
        let p=el;
        for(let i=0;i<14;i++){p=p.parentElement;if(!p)break;
          const lbl=p.querySelector('label'); if(lbl&&lbl.innerText.trim().length>2)return lbl.innerText.trim().toLowerCase();}
        return '';}"""
    try:
        force_ctrls = await page.locator(".select__control").all()
        if outer_page is not page:
            try:
                force_ctrls += await outer_page.locator(".select__control").all()
            except Exception:
                pass
        for ctrl in force_ctrls:
            try:
                if await ctrl.locator(".select__single-value, .select__multi-value").count() > 0:
                    continue  # already committed
                label_lower = await ctrl.evaluate(_LABEL_JS)
                if not label_lower:
                    continue
                # Demographic fields are handled by their candidate table even when
                # _rs_map has no entry (e.g. "military status" isn't in _rs_map).
                dem_cands = _demographic_candidates(label_lower)
                is_demographic = dem_cands is not None
                answer = None
                if is_demographic:
                    answer = dem_cands[0]
                else:
                    for keywords, ans in _rs_map:
                        if any(k in label_lower for k in keywords):
                            answer = ans
                            break
                if not answer:
                    continue
                is_location = any(k in label_lower for k in _location_keywords)
                await ctrl.scroll_into_view_if_needed()
                await ctrl.click()
                await asyncio.sleep(0.3)
                rs_input = ctrl.locator("input").first

                if is_location:
                    if await rs_input.count() > 0:
                        await rs_input.type("Plano")
                    else:
                        await keyboard.type("Plano")
                    await asyncio.sleep(1.2)
                    # Async geocode: first filtered option is the right city — accept it.
                    (await rs_input.press("Enter")) if await rs_input.count() > 0 else await keyboard.press("Enter")
                    await asyncio.sleep(0.3)
                else:
                    # SAFE match: click only an option that EXACTLY matches a candidate.
                    # Never blind-ArrowDown — that commits wrong values (e.g. "Cisgender
                    # woman" when typing "Male"). For demographics, try an ordered candidate
                    # list (e.g. "Asian" then "South Asian") so we map to whatever the form
                    # offers; non-demographics use the single _rs_map answer with a
                    # whole-word fallback. If nothing matches, leave blank for the human.
                    import re as _re2
                    candidates = (_demographic_candidates(label_lower) or [answer]) \
                        if is_demographic else [answer]
                    matched = False
                    for cand in candidates:
                        word = _re2.escape(cand.strip())
                        # clear any previously typed filter, then type this candidate
                        try:
                            if await rs_input.count() > 0:
                                await rs_input.fill("")
                                await asyncio.sleep(0.2)
                                await rs_input.type(cand)
                            else:
                                await keyboard.type(cand)
                        except Exception:
                            pass
                        await asyncio.sleep(0.6)
                        _patterns = (rf'^\s*{word}\s*$',)
                        if not is_demographic:
                            _patterns = (rf'^\s*{word}\s*$', rf'(^|\W){word}(\W|$)')
                        for ctx in (page, outer_page):
                            if matched:
                                break
                            for pat in _patterns:
                                try:
                                    opt = ctx.locator(
                                        "[role='option'], .select__option, [id*='-option-']"
                                    ).filter(has_text=_re2.compile(pat, _re2.IGNORECASE)).first
                                    if await opt.count() > 0:
                                        await opt.scroll_into_view_if_needed()
                                        await opt.click()
                                        matched = True
                                        break
                                except Exception:
                                    pass
                        if matched:
                            logger.info("eeoc_committed", field=label_lower[:40], value=cand)
                            break
                    await asyncio.sleep(0.25)
                    if not matched:
                        try:
                            await keyboard.press("Escape")
                        except Exception:
                            pass
                # Close any stray open menu
                try:
                    await keyboard.press("Escape")
                except Exception:
                    pass
            except Exception:
                continue
    except Exception:
        pass

    # ── Repair autocomplete append artifacts ────────────────────────────────────
    # React-controlled autocomplete inputs (e.g. "Your country of residence") can end
    # up with appended garbage like "United StatesUnited S". Runs last so it overrides
    # whatever produced the duplication: clear via keyboard, then set the clean value.
    try:
        for sel in ("input[aria-label*='country of residence']",
                    "input[aria-label*='Country of residence']",
                    "input[aria-label*='country' i]"):
            el = page.locator(sel).first
            if await el.count() == 0:
                continue
            v = (await el.input_value() or "").strip()
            if v and v.lower() != "united states" and "united states" in v.lower():
                await el.click()
                try:
                    await el.press("ControlOrMeta+a")
                    await el.press("Delete")
                except Exception:
                    pass
                await el.fill("")
                await el.fill("United States")
                await asyncio.sleep(0.3)
                # If an autocomplete menu opened, commit the exact match.
                try:
                    opt = page.locator("li[role='option'], .select__option").filter(
                        has_text=re.compile(r'^\s*United States\s*$', re.I)).first
                    if await opt.count() > 0:
                        await opt.click()
                    else:
                        await el.press("Escape")
                except Exception:
                    pass
                logger.info("country_residence_repaired", was=v)
            break
    except Exception:
        pass

    return True


# ── Lever Filler ───────────────────────────────────────────────────────────────

async def _fill_lever(page: Page, profile: dict, resume_pdf: Path, job: JobListing, llm) -> bool:
    """Fill a Lever application form."""
    p         = profile["personal"]
    defs      = profile.get("application_defaults", {})
    work_auth = profile.get("work_authorization", {})

    # Navigate to the /apply page if we're on the job listing page
    current_url = page.url
    if "jobs.lever.co" in current_url and not current_url.rstrip("/").endswith("/apply"):
        apply_url = current_url.rstrip("/") + "/apply"
        await page.goto(apply_url, wait_until="domcontentloaded")
        await asyncio.sleep(2.0)

    # Dismiss cookie consent banners before filling
    for cookie_btn_sel in [
        "button:has-text('Accept')", "button:has-text('Accept all')",
        "button:has-text('I agree')", "[id*='cookie'] button",
        ".cookie-banner button", "#accept-cookies",
    ]:
        try:
            btn = page.locator(cookie_btn_sel).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(0.5)
                break
        except Exception:
            pass

    async def _try_fill(selector: str, value: str) -> bool:
        try:
            el = page.locator(selector).first
            if await el.count() > 0:
                await el.fill(value)
                await asyncio.sleep(0.3)
                return True
        except Exception:
            pass
        return False

    field_map = {
        "input[name='name']":            p.get("name", ""),
        "input[name='email']":           p.get("email", ""),
        "input[name='phone']":           _phone_digits(p.get("phone", "")),
        "input[name='org']":             defs.get("current_company", "JPMorgan Chase"),
        "input[name='urls[LinkedIn]']":  p.get("linkedin_url", ""),
        "input[name='urls[GitHub]']":    p.get("github_url", ""),
        "input[name='urls[Portfolio]']": p.get("github_url", ""),
        "textarea[name='comments']":     "",  # will fill with cover letter below
    }

    for selector, value in field_map.items():
        if value:
            await _try_fill(selector, value)

    # Cover letter → Lever's "Additional information" / comments field
    if defs.get("cover_letter") == "auto":
        cl = await _generate_cover_letter(job, profile, llm)
        if cl:
            for cl_sel in ["textarea[name='comments']", "textarea[id*='cover']",
                           "textarea[placeholder*='cover']", "textarea[placeholder*='letter']"]:
                try:
                    el = page.locator(cl_sel).first
                    if await el.count() > 0:
                        await el.fill(cl)
                        break
                except Exception:
                    pass

    # Resume upload
    for upload_selector in ["input[type='file'][name='resume']", "input[type='file']"]:
        try:
            el = page.locator(upload_selector).first
            if await el.count() > 0:
                await el.set_input_files(str(resume_pdf))
                await asyncio.sleep(1.0)
                logger.info("resume_uploaded", ats="lever", path=str(resume_pdf))
                break
        except Exception:
            pass

    await asyncio.sleep(2.0)

    # ── Deterministic pass: language checkboxes + smart <select> dropdowns ───────
    # Runs BEFORE the slow per-question LLM loop so it never gets cut off by the
    # fill timeout. Handles control types that the LLM-text path can't map to
    # fixed options: multi-select language checkboxes, university / years-of-
    # experience / "how did you hear" dropdowns, and EEO selects.
    langs = [l.strip() for l in str(defs.get("languages", "English")).split(",") if l.strip()]
    schools = [str(e.get("school", "")).strip() for e in profile.get("education", []) if e.get("school")]

    # Language Skill(s) — check every box matching a spoken language (handles
    # "English (ENG)", "Hindi (HIN)" style labels).
    try:
        for cb in await page.locator("input[type='checkbox']").all():
            # Option text may live in the value attribute, a label[for=id], or a
            # wrapping <label> (Lever uses value + wrapping label, no id).
            lbl = (await cb.get_attribute("value") or "").strip()
            if not lbl:
                cid = await cb.get_attribute("id") or ""
                if cid:
                    le = page.locator(f"label[for='{cid}']")
                    if await le.count() > 0:
                        lbl = (await le.first.inner_text()).strip()
            if not lbl:
                try:
                    lbl = (await cb.evaluate(
                        "el => { const l = el.closest('label'); return l ? l.textContent.trim() : ''; }")) or ""
                except Exception:
                    lbl = ""
            if not lbl:
                continue
            ll = lbl.lower()
            # match a spoken language ("English (ENG)", "Hindi (HIN)", "English"…)
            if any(lang.lower() == ll or f"{lang.lower()} (" in ll or ll.startswith(lang.lower() + " ")
                   or ll == lang.lower() for lang in langs):
                if not any(k in ll for k in ("consent", "agree", "linkedin", "indeed", "glassdoor")):
                    try:
                        if not await cb.is_checked():
                            await cb.check(); await asyncio.sleep(0.15)
                            logger.info("language_checked", value=lbl[:30])
                    except Exception:
                        pass
    except Exception:
        pass

    async def _smart_select(sel_el) -> bool:
        """Pick the right option for a known dropdown category. Returns True if set."""
        try:
            cur = await sel_el.input_value()
            if cur and cur not in ("", "0", "-1"):
                return False
        except Exception:
            pass
        try:
            label = (await sel_el.evaluate("""el => {
                if (el.id){const l=document.querySelector('label[for="'+el.id+'"]');if(l)return l.textContent.trim();}
                let p=el; for(let i=0;i<8;i++){p=p.parentElement;if(!p)break;
                    const c=p.querySelector('.application-label,label,legend'); if(c)return c.textContent.trim();}
                return el.getAttribute('aria-label')||el.name||'';}""")) or ""
        except Exception:
            label = ""
        ll = label.lower()
        opts = []
        for o in await sel_el.locator("option").all():
            try:
                opts.append((await o.inner_text()).strip())
            except Exception:
                pass

        def pick(cands, allow_partial=True):
            for c in cands:
                for ot in opts:
                    if ot and ot.lower() == c.lower():
                        return ot
            if allow_partial:
                for c in cands:
                    for ot in opts:
                        if ot and (c.lower() in ot.lower() or ot.lower() in c.lower()):
                            return ot
            return None

        chosen = None
        dem = _demographic_candidates(ll)
        if dem:
            chosen = pick(dem)
        elif any(k in ll for k in ("university", "college you", "school you", "last attend", "currently attending")):
            chosen = pick(schools, allow_partial=True) or pick(
                ["Other (School Not Listed)", "Other (Not Listed)", "Other", "School Not Listed"])
        elif "years" in ll and ("experience" in ll or "work" in ll):
            yrs = str(defs.get("years_of_experience", "6"))
            chosen = pick([yrs, f"{yrs}+", "6", "7", "5"])
        elif any(k in ll for k in ("hear about", "heard about", "how did you", "referral", "source")):
            chosen = pick([defs.get("referral_source", "LinkedIn"), "LinkedIn"])
        if not chosen:
            return False
        try:
            await sel_el.select_option(label=chosen); await asyncio.sleep(0.2)
            logger.info("smart_select", field=ll[:40], value=chosen[:40])
            return True
        except Exception:
            return False

    try:
        for sel_el in await page.locator("select").all():
            await _smart_select(sel_el)
    except Exception:
        pass

    # EEO self-identification SIGNATURE block — a standalone "Name" text field and a
    # date field (placeholder MM/DD/YYYY) at the bottom of many EEO sections. The
    # identity map fills the main name; this catches the second signature name + date.
    try:
        from datetime import datetime as _dt
        today = _dt.now().strftime("%m/%d/%Y")
        for inp in await page.locator("input[type='text'], input:not([type])").all():
            try:
                if (await inp.input_value() or "").strip():
                    continue
                nm = (await inp.get_attribute("name") or "").lower()
                ph = (await inp.get_attribute("placeholder") or "").lower()
                iid = await inp.get_attribute("id") or ""
                lbl = ""
                if iid:
                    le = page.locator(f"label[for='{iid}']")
                    if await le.count() > 0:
                        lbl = (await le.first.inner_text()).strip().lower()
                ctx = f"{lbl} {ph} {nm}"
                if "mm/dd" in ph or "mm / dd" in ph or (lbl in ("date", "signature date") and "update" not in lbl):
                    await inp.fill(today); await asyncio.sleep(0.1)
                    logger.info("eeo_signature", field="date", value=today)
                elif ("enter your full name" in ph or lbl in ("name", "full name", "signature")
                      or ph in ("full name", "your name")):
                    if not any(k in ctx for k in ("first", "last", "company", "user", "file")):
                        await inp.fill(p.get("name", "")); await asyncio.sleep(0.1)
                        logger.info("eeo_signature", field="name", value=p.get("name", ""))
            except Exception:
                pass
    except Exception:
        pass

    # Custom card questions — two strategies:
    # 1. CSS-class selector (.application-card etc.) for standard Lever
    # 2. name-attribute selector (cards[GUID][fieldN]) for Veeva/enterprise variants

    _SALARY_KEYS = ("salary", "compensation", "desired pay", "pay expectation",
                    "expected pay", "expected salary", "salary expectation",
                    "comp expectation", "expected compensation")

    _reloc = "Yes" if profile.get("target", {}).get("relocation") else "No"
    _citizen = "No" if work_auth.get("requires_sponsorship") else "Yes"
    _KNOWN_CARD = [
        (("hear about", "referral", "source"),              defs.get("referral_source", "LinkedIn")),
        (("start date", "earliest", "when can you start"),  defs.get("start_date", "Immediately")),
        (("willing to relocate", "open to relocation"),     _reloc),
        (("citizen or green card", "citizen or permanent resident", "u.s. citizen or",
          "us citizen or", "a citizen or"),                 _citizen),
        (("languages you speak", "spoken language", "fluent in"), defs.get("languages", "English")),
        (("linkedin",),                                     p.get("linkedin_url", "")),
        (("github", "portfolio", "website"),                p.get("github_url", "")),
    ]

    async def _answer_card_field(label_text: str, match_hint: str = "") -> str:
        # match_hint may add the field's own attributes (name/id/placeholder) so
        # salary/auth detection still fires when the DOM label walk grabs wrong text.
        ll = (label_text + " " + match_hint).strip().lower()
        # Salary first — broadened keywords; never let it fall through to the LLM
        # (which writes an experience pitch into the salary box).
        if any(k in ll for k in _SALARY_KEYS):
            return defs.get("salary_display", "$150,000 – $200,000")
        # Citizen / green-card status — distinct from work-authorization (answer before it).
        if any(k in ll for k in ("citizen or green card", "citizen or permanent resident",
                                  "u.s. citizen or", "us citizen or", "are you a citizen")):
            return _citizen
        # Work authorization — country-aware (a Toronto role is NOT a "Yes" for US auth)
        if any(k in ll for k in ("require sponsorship", "visa sponsorship", "need sponsorship")):
            return _work_auth_answer(label_text, job, work_auth, "sponsorship")
        if any(k in ll for k in ("authorized to work", "legally authorized",
                                  "authorised to work", "legally authorised",
                                  "eligible to work", "right to work")):
            return _work_auth_answer(label_text, job, work_auth, "authorized")
        for keys, val in _KNOWN_CARD:
            if any(k in ll for k in keys):
                return val
        result = await _answer_question(label_text, job, llm)
        return result.get("answer", "") if isinstance(result, dict) else str(result)

    async def _fill_card_input(el, label_text: str) -> None:
        # Enrich matching (not the LLM prompt) with the field's own attributes so
        # salary/auth detection still fires when the deep label walk misses.
        try:
            attrs = await el.evaluate(
                "el => [el.name||'', el.id||'', el.placeholder||'', el.getAttribute('aria-label')||''].join(' ')"
            )
        except Exception:
            attrs = ""
        answer = await _answer_card_field(label_text, attrs)
        if not answer:
            return
        try:
            tag = await el.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                # Work-auth selects may use sentence options — pick the truthful one.
                if _is_workauth_label(label_text.lower()) and await _select_truthful_workauth_native(el):
                    return
                for opt in [answer, answer.split()[0]]:
                    try:
                        await el.select_option(label=opt)
                        return
                    except Exception:
                        pass
            elif await el.get_attribute("type") in ("radio", "checkbox"):
                val = (await el.get_attribute("value") or "").lower()
                if val in answer.lower() or answer.lower() in val:
                    await el.check()
            else:
                # Numeric fields (type=number / inputmode numeric) reject currency
                # strings like "$150,000 – $200,000" — coerce to the first plain number.
                etype = (await el.get_attribute("type") or "").lower()
                imode = (await el.get_attribute("inputmode") or "").lower()
                if etype == "number" or imode in ("numeric", "decimal"):
                    import re as _re3
                    m = _re3.search(r"\d[\d,]*", answer)
                    if m:
                        answer = m.group(0).replace(",", "")
                await el.fill(answer)
                await asyncio.sleep(0.2)
        except Exception:
            pass

    # Strategy 1: standard card selector
    for card in await page.locator(".application-card, .custom-question, [data-qa='custom-question']").all():
        try:
            label_el = card.locator("label, .question-text, p").first
            label_text = (await label_el.inner_text()).strip() if await label_el.count() > 0 else ""
            if not label_text:
                continue
            for input_el in await card.locator("input:not([type=hidden]), textarea, select").all():
                await _fill_card_input(input_el, label_text)
        except Exception as e:
            logger.warning("lever_card_error", error=str(e)[:80])

    # Strategy 2: inputs named cards[GUID][fieldN] — common in enterprise Lever forms
    card_inputs = await page.locator(
        "input[name^='cards[']:not([type=hidden]):not([type=radio]):not([type=checkbox]),"
        "textarea[name^='cards['],"
        "select[name^='cards[']"
    ).all()
    for el in card_inputs:
        try:
            existing = await el.input_value()
            if existing.strip():
                continue
            # Deep label extraction — searches up to 10 DOM levels
            label_text = await el.evaluate("""el => {
                // Junk placeholders that are NOT real question labels
                const JUNK = ['type your response','your answer','your response',
                              'enter your answer','please specify','select...','answer'];
                const isJunk = t => !t || JUNK.some(j => t.trim().toLowerCase() === j);
                // 0. Lever's real question label (.application-label) on the enclosing card —
                //    must win over the field's placeholder (e.g. "Type your response").
                const card = el.closest('.application-question, li, .application-card');
                if (card) {
                    const al0 = card.querySelector('.application-label, label');
                    if (al0) {
                        const t = al0.textContent.trim().slice(0,150);
                        if (!isJunk(t)) return t;
                    }
                }
                // 1. label[for=id]
                if (el.id) {
                    const l = document.querySelector('label[for="' + el.id + '"]');
                    if (l && !isJunk(l.textContent)) return l.textContent.trim().slice(0,150);
                }
                // 2. aria-label / placeholder (skip junk placeholders)
                const al = el.getAttribute('aria-label') || el.placeholder || '';
                if (al.length > 5 && !isJunk(al)) return al;
                // 3. Walk up DOM: look for any visible text element that is a sibling
                //    or ancestor of the input, NOT containing other inputs
                let p = el;
                for (let i = 0; i < 10; i++) {
                    p = p.parentElement;
                    if (!p) break;
                    for (const tag of ['legend','h1','h2','h3','h4','p','span','strong','div']) {
                        for (const el2 of p.querySelectorAll(tag)) {
                            const txt = (el2.textContent || '').trim();
                            if (txt.length > 8 && txt.length < 200 &&
                                !el2.querySelector('input,select,textarea,button')) {
                                return txt.slice(0,150);
                            }
                        }
                    }
                }
                return el.getAttribute('name') || '';
            }""")
            await _fill_card_input(el, label_text or "")
        except Exception:
            pass

    # Strategy 3: LLM fallback for ALL remaining empty text/textarea fields (catches "Type your response" etc.)
    await _answer_open_questions(page, job, llm, profile)

    # Handle radio buttons within cards
    # Group radios by name so we process each question once
    seen_radio_names: set[str] = set()
    for radio in await page.locator("input[type='radio'][name^='cards[']").all():
        try:
            rname = await radio.get_attribute("name") or ""
            if rname in seen_radio_names:
                continue
            seen_radio_names.add(rname)

            # Get the question text for this radio group via deep DOM search
            question_text = await radio.evaluate("""el => {
                let p = el;
                for (let i = 0; i < 8; i++) {
                    p = p.parentElement; if (!p) break;
                    for (const tag of ['legend','p','h3','h4','strong','span','div']) {
                        for (const c of p.querySelectorAll(tag)) {
                            const t = (c.textContent||'').trim();
                            if (t.length > 8 && !c.querySelector('input,select,textarea'))
                                return t.slice(0,120).toLowerCase();
                        }
                    }
                }
                return '';
            }""")

            group_radios = await page.locator(f"input[type='radio'][name='{rname}']").all()

            # EEO / demographic radio groups (veteran, disability, gender, race…):
            # match the candidate self-identification by exact/contained option label.
            dem_cands = _demographic_candidates(question_text)
            if dem_cands:
                matched = False
                for cand in dem_cands:
                    cl = cand.lower()
                    for r in group_radios:
                        rid = await r.get_attribute("id")
                        opt = ""
                        if rid:
                            lbl = page.locator(f"label[for='{rid}']")
                            if await lbl.count() > 0:
                                opt = (await lbl.first.inner_text()).strip().lower()
                        if opt and (opt == cl or cl in opt or opt in cl):
                            await r.check()
                            await asyncio.sleep(0.2)
                            logger.info("eeoc_committed", field=question_text[:40], value=cand)
                            matched = True
                            break
                    if matched:
                        break
                continue  # demographic group handled (or intentionally left blank)

            # Determine the correct answer from question text (country-aware)
            if any(k in question_text for k in ("require sponsorship", "visa", "now or in the future")):
                target_answer = _work_auth_answer(question_text, job, work_auth, "sponsorship").lower()
            elif any(k in question_text for k in ("authorized", "legally authorized", "eligible to work", "right to work")):
                target_answer = _work_auth_answer(question_text, job, work_auth, "authorized").lower()
            elif any(k in question_text for k in ("citizen or green card", "citizen or permanent resident",
                                                   "u.s. citizen or", "us citizen or", "are you a citizen")):
                target_answer = _citizen.lower()
            elif "security clearance" in question_text or ("clearance" in question_text and "secret" in question_text):
                # US security clearances require citizenship; candidate needs visa
                # sponsorship → neither holds nor is eligible for one. Answer "No".
                target_answer = "no"
            else:
                # LLM answer or default Yes for unknown yes/no questions
                if question_text:
                    llm_ans = await _answer_question(question_text, job, llm)
                    target_answer = (llm_ans.get("answer", "yes") if isinstance(llm_ans, dict) else "yes").lower()
                else:
                    target_answer = "yes"

            # Now check all radios in this group
            for r in group_radios:
                val = (await r.get_attribute("value") or "").lower()
                # Get option label
                rid = await r.get_attribute("id")
                opt_label = ""
                if rid:
                    lbl = page.locator(f"label[for='{rid}']")
                    if await lbl.count() > 0:
                        opt_label = (await lbl.first.inner_text()).strip().lower()
                # Check if this option matches target
                if (val == target_answer or opt_label == target_answer or
                        val.startswith(target_answer[:3]) or target_answer in val):
                    await r.check()
                    await asyncio.sleep(0.2)
                    break
        except Exception:
            pass

    # ── EEO / demographic <select> dropdowns (veteran, disability, gender, race) ──
    for sel_el in await page.locator("select[name^='cards['], select").all():
        try:
            cur = await sel_el.input_value()
            if cur and cur not in ("", "0", "-1"):
                continue
            label = await sel_el.evaluate("""el => {
                if (el.id) { const l = document.querySelector('label[for="'+el.id+'"]'); if (l) return l.textContent.trim(); }
                let p = el;
                for (let i=0;i<8;i++){ p=p.parentElement; if(!p) break;
                    const c = p.querySelector('.application-label, label, legend'); if (c) return c.textContent.trim(); }
                return el.getAttribute('aria-label') || el.name || '';
            }""")
            cands = _demographic_candidates((label or "").lower())
            if not cands:
                continue
            opts = [(await o.inner_text()).strip() for o in await sel_el.locator("option").all()]
            chosen = None
            for cand in cands:
                for ot in opts:
                    if ot and (ot.lower() == cand.lower() or cand.lower() in ot.lower()):
                        chosen = ot
                        break
                if chosen:
                    break
            if chosen:
                try:
                    await sel_el.select_option(label=chosen)
                    logger.info("eeoc_committed", field=(label or "")[:40], value=chosen)
                except Exception:
                    pass
        except Exception:
            pass

    return True


# ── Workday Filler ─────────────────────────────────────────────────────────────

def _workday_answer(label_lower: str, profile: dict) -> list[str] | None:
    """Ordered candidate option texts for a Workday dropdown/radio, or None."""
    wa = profile.get("work_authorization", {})
    defs = profile.get("application_defaults", {})
    dem = _demographic_candidates(label_lower)
    if dem:
        return dem
    # Any "do you require sponsorship?" phrasing → Yes (Rahul needs H1B). Matching the
    # bare word 'sponsorship' covers slashed forms like "Visa/Sponsorship" and keeps
    # this ahead of the generic 'country' check (the label often says "within the
    # country …"). Exclude the inverted "without sponsorship" phrasing.
    # Sponsorship — including phrasings that never use the word "sponsorship"
    # ("require employer support to obtain/maintain authorization", "work permit",
    # "require a visa"). Rahul needs H1B → Yes. Exclude the inverted "without" forms.
    _sponsor = ("sponsorship" in label_lower or "work permit" in label_lower
                or ("employer support" in label_lower
                    and ("authoriz" in label_lower or "work" in label_lower or "visa" in label_lower))
                or ("require" in label_lower and "visa" in label_lower))
    if _sponsor and not any(
            k in label_lower for k in ("without sponsorship", "not require sponsorship",
                                       "do not require sponsorship", "without requiring sponsorship",
                                       "without requiring", "not require employer support",
                                       "without employer support")):
        return [wa.get("sponsorship_answer", "Yes"), "Yes"]
    # "Authorized to work WITHOUT sponsorship / a visa?" → No (Rahul needs sponsorship).
    # Must precede the generic authorized check, which would otherwise answer Yes.
    if "authoriz" in label_lower and any(
            k in label_lower for k in ("without sponsorship", "without requiring sponsorship",
                                       "without employer support", "without a visa",
                                       "without requiring employer", "without need for sponsorship")):
        return ["No"]
    if any(k in label_lower for k in ("legally authorized", "authorized to work", "eligible to work",
                                       "right to work")):
        return [wa.get("authorized_answer", "Yes"), "Yes"]
    if "citizen" in label_lower or "green card" in label_lower or "permanent resident" in label_lower:
        return ["No"]
    if "security clearance" in label_lower or "clearance" in label_lower:
        return ["No"]
    # Phone fields (Workday "My Information") — must precede the generic country check.
    if "phone" in label_lower and ("device type" in label_lower or "phone type" in label_lower):
        return ["Mobile", "Cell", "Home"]
    if ("country phone code" in label_lower or "phone code" in label_lower
            or ("country" in label_lower and "code" in label_lower)):
        return ["United States of America (+1)", "United States (+1)", "United States", "+1"]
    if "country" in label_lower and "code" not in label_lower:
        return ["United States of America", "United States", "USA"]
    if "state" in label_lower or "province" in label_lower:
        return ["Texas", "TX"]
    if any(k in label_lower for k in ("hear about", "heard about", "source", "referral", "how did you")):
        # Ordered fallbacks — if a tenant's taxonomy lacks a LinkedIn leaf, try other
        # near-universal sources before the handler's last-resort "pick any leaf".
        return [defs.get("referral_source", "LinkedIn"), "LinkedIn", "Indeed", "Glassdoor",
                "Company Website", "Website", "Job Board", "Online", "Other"]
    if any(k in label_lower for k in ("previously worked", "worked for this organization",
                                       "previously been employed", "former employee", "worked here")):
        return ["No"]
    # Company-history screening questions (Workday Application Questions). Rahul has
    # no prior relationship with these employers → No. (The sponsorship/authorization
    # checks above already returned for those, so 'employed' here is unambiguous.)
    if any(k in label_lower for k in ("currently being considered", "recently applied",
                                       "currently employed", "employed you", "applied at",
                                       "applied to", "previously applied", "ever worked",
                                       "ever been employed", "in the past")):
        return ["No"]
    # Conflict-of-interest / relationship screening (Workday Application Questions) —
    # an external applicant with no ties to the employer answers No to all of these.
    if any(k in label_lower for k in ("license user", "licence user", "procuring",
                                       "negotiating", "influencing contract", "expenditures",
                                       "non-competition", "non competition", "non-solicitation",
                                       "non solicitation", "non-compete", "noncompete",
                                       "conflict of interest", "family member", "relative",
                                       "related to anyone", "currently subject to")):
        return ["No"]
    if "degree" in label_lower or "education level" in label_lower:
        return ["Master's Degree", "Master's", "Masters", "Graduate Degree"]
    if any(k in label_lower for k in ("field of study", "major", "discipline", "area of study")):
        return ["Computer Information Systems", "Computer Science", "Information Systems",
                "Computer Engineering", "Engineering"]
    if any(k in label_lower for k in ("school", "university", "college", "institution")) \
            and "high school" not in label_lower:
        return ["New England College"]
    # Language skill row (Workday "Languages" section). The bare "Language" dropdown →
    # English; proficiency dropdowns → highest available.
    if any(k in label_lower for k in ("proficiency", "fluency", "comprehension",
                                       "reading", "speaking", "writing")):
        return ["Native or Bilingual Proficiency", "Full Professional Proficiency",
                "Professional Working Proficiency", "Fluent", "Native", "Advanced", "Expert"]
    if label_lower.strip().rstrip("*").strip() == "language" or (
            "language" in label_lower and "proficiency" not in label_lower
            and not any(k in label_lower for k in ("hear", "speak", "how did", "preferred"))):
        return ["English", "English (ENG)"]
    if "relocate" in label_lower or "relocation" in label_lower:
        return ["Yes"] if profile.get("target", {}).get("relocation") else ["No"]
    return None


async def _wd_coord_click(page, locator) -> bool:
    """Click a Workday button at its center coordinates so the overlaying
    `click_filter` div (which intercepts pointer events) receives the trusted click.
    Clicking the inner button directly silently no-ops on Workday."""
    try:
        if await locator.count() == 0:
            return False
        try:
            await locator.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        box = await locator.bounding_box()
        if box:
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            return True
        await locator.click(timeout=3000, force=True)
        return True
    except Exception:
        try:
            await locator.evaluate("el => el.click()")
            return True
        except Exception:
            return False


async def _wd_fill_input(page, auto_ids: list[str], labels: list[str], value: str) -> bool:
    """Fill a Workday text input robustly.

    Workday puts the data-automation-id on the *wrapper div* in some tenants and on
    the <input> itself in others, so we try input-scoped selectors both ways. Labels
    are matched non-exact (the accessible name often carries a trailing '*' or a
    'Legal Name - ' prefix). Returns True once a fillable, empty input is found.
    """
    locs = []
    for aid in auto_ids:
        locs.append(page.locator(f"input[data-automation-id='{aid}']"))
        locs.append(page.locator(f"[data-automation-id='{aid}'] input"))
        locs.append(page.locator(f"[data-automation-id='{aid}'] textarea"))
    for lbl in labels:
        locs.append(page.get_by_label(lbl, exact=False))
        locs.append(page.get_by_role("textbox", name=lbl, exact=False))
    for loc in locs:
        try:
            el = loc.first
            if await el.count() == 0:
                continue
            try:
                if await el.input_value():   # already populated — leave it
                    return True
            except Exception:
                pass
            await el.fill(value)
            await asyncio.sleep(0.2)
            return True
        except Exception:
            continue
    return False


async def _workday_exp_date(root, field_aid: str, ym: str) -> None:
    """Fill a From/To date section (month/year) within a work-experience panel."""
    parts = str(ym).split("-")
    if len(parts) != 2:
        return
    yr, mo = parts[0], parts[1].zfill(2)
    try:
        fld = root.locator(f"[data-automation-id='{field_aid}']").first
        droot = fld if await fld.count() > 0 else root
        mi = droot.locator("input[data-automation-id='dateSectionMonth-input']").first
        yi = droot.locator("input[data-automation-id='dateSectionYear-input']").first
        if await mi.count() > 0 and not await mi.input_value():
            await mi.fill(mo); await asyncio.sleep(0.15)
        if await yi.count() > 0 and not await yi.input_value():
            await yi.fill(yr); await asyncio.sleep(0.15)
    except Exception:
        pass


async def _workday_fill_experience(page, profile: dict) -> None:
    """Fill the Workday Work Experience section with EVERY role in work_history (Job
    Title, Company, From/To dates, 'I currently work here'), clicking 'Add Another' to
    reveal each additional panel. Each row is scoped to its `workExperience-N` panel so
    rows don't overwrite each other. Best-effort + idempotent (skips already-filled)."""
    wh = profile.get("work_history") or []
    if not wh:
        return

    async def _click_exp_add() -> bool:
        try:
            return await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll("button[data-automation-id='add-button']"));
                for (const b of btns){ let p=b;
                    for(let i=0;i<12;i++){p=p.parentElement;if(!p)break;
                        const h=p.querySelector('h2,h3,h4,[id*=header],[id*=Header]');
                        if(h && /work experience|^\\s*experience/i.test(h.innerText||'')){ b.click(); return true; }}}
                return false; }""")
        except Exception:
            return False

    # Reveal the first row if the section is collapsed/Add-gated.
    try:
        has_field = await page.locator(
            "input[data-automation-id='jobTitle'], [data-automation-id='formField-jobTitle']").count() > 0
        if not has_field:
            if await _click_exp_add():
                await asyncio.sleep(1.2); logger.info("workday_experience_add_clicked")
    except Exception:
        pass
    # Guard: only proceed on the Work Experience page.
    if await page.locator(
            "input[data-automation-id='jobTitle'], [data-automation-id='formField-jobTitle']").count() == 0:
        return

    filled = 0
    for idx, exp in enumerate(wh[:5]):
        title = exp.get("title", "")
        company = exp.get("company", "")
        if not (title or company):
            continue
        # Ensure a panel exists for this index; click "Add Another" for rows 2+.
        panel = page.locator(f"[data-automation-id='workExperience-{idx + 1}']").first
        if idx > 0 and await panel.count() == 0:
            if not await _click_exp_add():
                break   # can't add more rows — stop rather than overwrite a filled one
            await asyncio.sleep(1.2)
            panel = page.locator(f"[data-automation-id='workExperience-{idx + 1}']").first
        # Scope to the panel when present; else (single-row layouts) use the page.
        root = panel if await panel.count() > 0 else page
        ft = await _wd_fill_input(root, ["jobTitle"], ["Job Title"], title)
        fc = await _wd_fill_input(root, ["companyName", "company"], ["Company"], company)
        if not (ft or fc):
            if idx == 0:
                return   # not actually on the experience page
            break
        filled += 1
        logger.info("workday_experience_filled", n=idx + 1, title=title[:24], company=company[:24])

        start = str(exp.get("start", "")); end = str(exp.get("end", "present"))
        current = end.lower() in ("present", "current", "now", "")
        if current:
            for cbsel in ("input[data-automation-id='currentlyWorkHere']",
                          "[data-automation-id='formField-currentlyWorkHere'] input"):
                try:
                    cb = root.locator(cbsel).first
                    if await cb.count() > 0 and not await cb.is_checked():
                        await cb.click(force=True); await asyncio.sleep(0.4); break
                except Exception:
                    pass
        await _workday_exp_date(root, "formField-startDate", start)
        if not current:
            await _workday_exp_date(root, "formField-endDate", end)
    if filled:
        logger.info("workday_experience_count", filled=filled)


async def _workday_fill_education(page, profile: dict) -> None:
    """Fill the Workday Education section. Degree / Field of Study / School are searchable
    prompts handled by _workday_set_dropdowns (via _workday_answer); here we fill GPA and
    the free-text fallbacks, and reveal an Add-gated section ONLY when education is
    actually required (an Errors panel names degree/school) — never fabricating dates."""
    edu = (profile.get("education") or [{}])[0]
    if not edu:
        return
    has_edu_field = await page.locator(
        "[data-automation-id='degree'], [data-automation-id='formField-degree'], "
        "[data-automation-id='school'], [data-automation-id='formField-school']").count() > 0
    if not has_edu_field:
        # Section collapsed/Add-gated — reveal it by clicking the Education "Add" so it
        # gets filled (it otherwise stays EMPTY on optional configs). Fires only on the
        # My Experience page (the heading match finds no button elsewhere).
        clicked = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll("button[data-automation-id='add-button']"));
            for (const b of btns){ let p=b;
                for (let i=0;i<12;i++){ p=p.parentElement; if(!p) break;
                    const h=p.querySelector('h2,h3,h4,[id*=header],[id*=Header]');
                    if (h && /education/i.test(h.innerText||'')){ b.click(); return true; } } }
            return false; }""")
        if not clicked:
            return
        await asyncio.sleep(1.2)
        logger.info("workday_education_add_clicked")

    gpa = str(edu.get("gpa", "")).split("/")[0].strip()
    if gpa:
        await _wd_fill_input(page, ["gradeAverage", "gpa", "formField-gradeAverage"],
                             ["Overall Result (GPA)", "Grade Average", "GPA"], gpa)
    # Free-text fallbacks (when the field is an input, not a searchable prompt).
    await _wd_fill_input(page, ["fieldOfStudy", "formField-fieldOfStudy"],
                         ["Field of Study"], "Computer Information Systems")
    school = edu.get("school", "")
    if school:
        await _wd_fill_input(page, ["schoolName", "formField-schoolName"],
                             ["School or University", "School Name"], school)
    # NB: education First/Last-Year-Attended dates are intentionally NOT auto-filled —
    # we have no graduation year in the profile and fabricating it risks a background-
    # check mismatch. If a tenant requires them, the auto-iterate parks for the human.


async def _workday_fill_typed_questions(page, profile: dict) -> None:
    """Fill Workday Application-Question fields that are typed/numeric/date rather than
    Yes/No or free-text essays (which _answer_open_questions deliberately skips):
    salary expectation, years of experience, notice period, and an availability date."""
    defs = profile.get("application_defaults", {})
    rules = [
        (("salary expectation", "expected salary", "desired salary", "salary requirement",
          "expected compensation", "desired compensation", "compensation expectation",
          "base salary"), str(defs.get("salary_min", 150000))),
        (("years of experience", "years experience", "total years", "how many years",
          "years of relevant"), str(defs.get("years_of_experience", "6"))),
        (("notice period", "period of notice", "notice (in days)"), "2 weeks"),
        (("available to start", "availability", "earliest start", "when can you start",
          "start date"), defs.get("start_date", "Immediately")),
    ]

    async def _label_of(el) -> str:
        try:
            eid = await el.get_attribute("id")
            if eid:
                lbl = page.locator(f"label[for='{eid}']")
                if await lbl.count() > 0:
                    return (await lbl.first.inner_text()).strip()
            return (await el.get_attribute("aria-label")
                    or await el.get_attribute("placeholder")
                    or await el.evaluate("e => { const p=e.closest('[data-automation-id^=formField]');"
                                         " const l=p&&p.querySelector('label,legend'); "
                                         "return l?l.innerText.trim():''; }") or "")
        except Exception:
            return ""

    # 1) Text/number inputs
    try:
        els = await page.locator("input[type='text']:visible, input[type='number']:visible").all()
    except Exception:
        els = []
    for el in els:
        try:
            if (await el.input_value()).strip():
                continue
            ll = (await _label_of(el)).lower()
            if len(ll) < 4:
                continue
            for keys, val in rules:
                if any(k in ll for k in keys):
                    await el.fill(str(val)); await asyncio.sleep(0.15)
                    logger.info("workday_typed_question", label=ll[:34], value=str(val)[:20])
                    break
        except Exception:
            pass

    # 2) An availability/start DATE field (month/day/year sub-inputs) → today.
    try:
        for fld in await page.locator("[data-automation-id^='formField']").all():
            lbl = (await fld.evaluate("e => { const l=e.querySelector('label,legend'); "
                                      "return l?l.innerText.trim():''; }") or "").lower()
            if not any(k in lbl for k in ("available", "start date", "availability")):
                continue
            mi = fld.locator("input[data-automation-id='dateSectionMonth-input']").first
            if await mi.count() == 0 or await mi.input_value():
                continue
            now = datetime.now(timezone.utc)
            await mi.fill(now.strftime("%m"))
            yi = fld.locator("input[data-automation-id='dateSectionYear-input']").first
            di = fld.locator("input[data-automation-id='dateSectionDay-input']").first
            if await di.count() > 0:
                await di.fill(now.strftime("%d"))
            if await yi.count() > 0:
                await yi.fill(now.strftime("%Y"))
            logger.info("workday_availability_date", value=now.strftime("%m/%d/%Y"))
            break
    except Exception:
        pass


async def _workday_fill_skills(page, profile: dict) -> None:
    """Add skill tags to a Workday Skills multiselect if one is present (and not already
    populated). Uses short keyword skills — Workday skill pickers want tags, not the
    long descriptive phrases in the profile's primary-skills list."""
    import re as _re
    # Locate the skills container by automation-id or a 'skill' label (NOT the
    # How-Did-You-Hear / source multiselect).
    async def _find_skills():
        for sel in ("[data-automation-id='formField-skills']", "[data-automation-id='skills']"):
            if await page.locator(sel).count() > 0:
                return page.locator(sel).first
        try:
            for ms in await page.locator("[data-automation-id='multiselectInputContainer']").all():
                lbl = (await ms.evaluate("""el => { let p=el; for(let i=0;i<8;i++){p=p.parentElement;
                    if(!p)break; const l=p.querySelector('label,legend,[id*=label]');
                    if(l&&(l.innerText||'').trim())return l.innerText.trim();} return ''; }""") or "").lower()
                if "skill" in lbl:
                    return ms
        except Exception:
            pass
        return None

    cont = await _find_skills()
    if cont is None:
        # Skills section may be Add-gated — reveal it, then re-find.
        try:
            clicked = await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll("button[data-automation-id='add-button']"));
                for (const b of btns){ let p=b;
                    for(let i=0;i<12;i++){p=p.parentElement;if(!p)break;
                        const h=p.querySelector('h2,h3,h4,[id*=header],[id*=Header]');
                        if(h && /skill/i.test(h.innerText||'')){ b.click(); return true; }}}
                return false; }""")
            if clicked:
                await asyncio.sleep(1.0)
                cont = await _find_skills()
        except Exception:
            pass
    if cont is None:
        return
    try:
        if await cont.locator("[data-automation-id='selectedItem']").count() >= 3:
            return  # already has skills
    except Exception:
        pass
    clean = ["Python", "AWS", "Kubernetes", "Terraform", "Apache Spark", "SQL",
             "Docker", "Kafka", "Snowflake", "MLOps"]

    async def _pill_count() -> int:
        try:
            return await cont.locator("[data-automation-id='selectedItem']").count()
        except Exception:
            return 0

    added = 0
    for sk in clean[:8]:
        try:
            before = await _pill_count()
            await cont.click(); await asyncio.sleep(0.3)
            ib = page.locator("input[data-automation-id='searchBox'], "
                              "[data-automation-id='formField-skills'] input, "
                              "[data-automation-id='skills'] input, "
                              "[data-automation-id='multiselectInputContainer'] input").first
            if await ib.count() == 0:
                break
            await ib.fill(sk); await asyncio.sleep(0.8)
            opt = page.locator("[data-automation-id='promptOption'], "
                               "[data-automation-id='promptLeafNode'], [role='option']").filter(
                has_text=_re.compile(rf'^\s*{_re.escape(sk)}', _re.IGNORECASE)).first
            if await opt.count() > 0:
                await opt.click()
            else:
                await ib.press("Enter")   # tenants that accept free-text tags
            await asyncio.sleep(0.5)
            if await _pill_count() > before:   # count clicked OR free-text that stuck
                added += 1
        except Exception:
            pass
    logger.info("workday_skills_added", count=added)


async def _workday_fill_cover_letter(page, profile: dict, job, llm) -> None:
    """Fill a Workday cover-letter TEXTAREA with a generated letter (paste form only;
    optional file-upload cover letters are left for the human to avoid mis-uploading)."""
    ta = None
    for sel in ("textarea[data-automation-id*='overLetter']",
                "[data-automation-id*='overLetter'] textarea"):
        if await page.locator(sel).count() > 0:
            ta = page.locator(sel).first; break
    if ta is None:
        try:
            cand = page.get_by_label("Cover Letter", exact=False)
            if await cand.count() > 0:
                el = cand.first
                if (await el.evaluate("e => e.tagName")).lower() == "textarea":
                    ta = el
        except Exception:
            pass
    if ta is None:
        return
    try:
        if await ta.input_value():
            return
    except Exception:
        return
    text = await _generate_cover_letter(job, profile, llm)
    if text:
        try:
            await ta.fill(text); await asyncio.sleep(0.2)
            logger.info("workday_cover_letter_filled", chars=len(text))
        except Exception:
            pass


async def _workday_add_language(page, profile: dict) -> None:
    """Reveal + fill the Workday Languages section (Add-gated). Clicks the Add button
    inside the 'Languages' section, then the Language dropdown + proficiency are set by
    the dropdown handler via _workday_answer."""
    # Skip if a language row already exists (a language/proficiency field present).
    try:
        if await page.locator(
                "[data-automation-id='formField-language'], "
                "[data-automation-id='languageProficiency'], "
                "[data-automation-id*='language-']").count() > 0:
            return
    except Exception:
        pass
    clicked = False
    try:
        clicked = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll("button[data-automation-id='add-button']"));
            for (const b of btns) {
                let p=b;
                for (let i=0;i<12;i++){ p=p.parentElement; if(!p) break;
                    const h = p.querySelector('h2,h3,h4,[id*=header],[id*=Header]');
                    if (h && /language/i.test(h.innerText||'')){ b.click(); return true; } }
            }
            return false;
        }""")
    except Exception:
        return
    if not clicked:
        return
    await asyncio.sleep(1.2)
    logger.info("workday_language_add_clicked")

    # Select Language = English in the revealed row.
    import re as _re
    try:
        lb = page.locator(
            "[data-automation-id='formField-language'] button[aria-haspopup='listbox'], "
            "[data-automation-id='formField-language'] [aria-haspopup='listbox'], "
            "[data-automation-id='formField-language'] [data-automation-id='selectinput']").first
        if await lb.count() > 0:
            await lb.click(); await asyncio.sleep(0.6)
            opt = page.locator("[role='option'], [data-automation-id='promptOption']").filter(
                has_text=_re.compile(r'^\s*English\s*$', _re.IGNORECASE)).first
            if await opt.count() > 0:
                await opt.click(); await asyncio.sleep(0.6)
                logger.info("workday_language_set", value="English")
    except Exception:
        pass

    # Check "I am fluent in this language." — declares native fluency and clears the
    # otherwise-required Reading/Speaking/Writing/Comprehension proficiency grid.
    try:
        fcb = page.locator(
            "[data-automation-id='formField-native'] input[type='checkbox'], "
            "input[data-automation-id='native'], "
            "[data-automation-id='formField-native'] input").first
        if await fcb.count() > 0 and not await fcb.is_checked():
            await fcb.click(force=True); await asyncio.sleep(0.5)
            logger.info("workday_language_fluent_checked")
    except Exception:
        pass


async def _workday_set_dropdowns(page, profile: dict) -> None:
    """Fill Workday button[aria-haspopup] prompt dropdowns by matching label → option."""
    try:
        buttons = await page.locator("button[aria-haspopup='listbox'], "
                                     "[data-automation-id='selectinput'], "
                                     "div[aria-haspopup='listbox']").all()
    except Exception:
        buttons = []
    for btn in buttons:
        try:
            # Skip if already has a non-placeholder value
            cur = (await btn.inner_text() or "").strip().lower()
            if cur and cur not in ("select one", "select", "search", ""):
                continue
            label = await btn.evaluate("""el => {
                let p = el;
                for (let i=0;i<8;i++){ p=p.parentElement; if(!p) break;
                    const l = p.querySelector('label,[id*="label"],legend'); if (l) return l.innerText.trim(); }
                return el.getAttribute('aria-label') || '';
            }""")
            cands = _workday_answer((label or "").lower(), profile)
            if not cands:
                continue
            await btn.scroll_into_view_if_needed()
            await btn.click()
            await asyncio.sleep(0.6)
            picked = False
            for cand in cands:
                import re as _re
                # Long lists (e.g. Country Phone Code, ~200 entries) virtualize their
                # options — type into the prompt's search box to bring the match into
                # the DOM before clicking.
                try:
                    sb = page.locator("input[data-automation-id='searchBox'], "
                                      "[data-automation-id='multiselectInputContainer'] input").first
                    if await sb.count() > 0 and await sb.is_visible(timeout=500):
                        await sb.fill(cand); await asyncio.sleep(0.8)
                except Exception:
                    pass
                for osel in ("[role='option']", "[data-automation-id='promptOption']", "li[role='option']"):
                    # Exact match first, then a relaxed 'contains' for decorated labels
                    # like "United States of America (+1)".
                    for pat in (rf'^\s*{_re.escape(cand)}\s*$', _re.escape(cand)):
                        try:
                            opt = page.locator(osel).filter(
                                has_text=_re.compile(pat, _re.IGNORECASE)).first
                            if await opt.count() > 0:
                                await opt.click(); await asyncio.sleep(0.3); picked = True; break
                        except Exception:
                            pass
                    if picked:
                        break
                if picked:
                    break
            if not picked:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
            else:
                logger.info("workday_dropdown", field=(label or "")[:40], value=cands[0])
        except Exception:
            pass


async def _workday_set_multiselects(page, profile: dict) -> None:
    """Fill Workday multiselect prompt widgets (e.g. 'How Did You Hear About Us?').

    These are NOT button[aria-haspopup] dropdowns — they're click-to-open prompt
    containers whose options live in a (often hierarchical) tree. Click to open, type
    into the search box to flatten/filter the tree, then click the matching leaf.
    """
    import re as _re
    try:
        conts = await page.locator(
            "[data-automation-id='multiselectInputContainer'], "
            "[data-automation-id='multiSelectContainer']").all()
    except Exception:
        conts = []
    for cont in conts:
        try:
            # Already has a selected pill/charm? Skip.
            if await cont.locator(
                    "[data-automation-id='selectedItem'], [data-automation-id*='DELETE']").count() > 0:
                continue
            label = await cont.evaluate("""el => {
                let p = el;
                for (let i=0;i<8;i++){ p=p.parentElement; if(!p) break;
                    const l = p.querySelector('label,[id*="label"],legend'); if (l) return l.innerText.trim(); }
                return el.getAttribute('aria-label') || '';
            }""")
            cands = _workday_answer((label or "").lower(), profile)
            if not cands:
                continue
            await cont.scroll_into_view_if_needed()
            await cont.click()
            await asyncio.sleep(0.6)

            async def _selected() -> bool:
                """A selection actually registered for THIS field (a pill/charm appeared in
                its container or the wrapping form field) — scoped to avoid cross-field
                contamination from another multiselect's default value."""
                try:
                    if await cont.locator(
                            "[data-automation-id='selectedItem'], "
                            "[data-automation-id*='DELETE']").count() > 0:
                        return True
                    return await cont.evaluate(
                        "el => { const f = el.closest('[data-automation-id^=formField]') || el.parentElement;"
                        " return !!(f && f.querySelector("
                        "\"[data-automation-id='selectedItem'],[data-automation-id*='DELETE']\")); }")
                except Exception:
                    return False

            async def _click_leaf(value: str) -> bool:
                """Click a selectable leaf node matching value and CONFIRM it stuck — some
                tenants render category rows as role=option, so a click can 'succeed'
                without selecting anything. Only return True once a pill appears."""
                for osel in ("[data-automation-id='promptLeafNode']", "[role='option']"):
                    try:
                        opt = page.locator(osel).filter(
                            has_text=_re.compile(rf'^\s*{_re.escape(value)}', _re.IGNORECASE)).first
                        if await opt.count() > 0 and await opt.is_visible(timeout=500):
                            await opt.click(); await asyncio.sleep(0.4)
                            if await _selected():
                                return True
                    except Exception:
                        pass
                return False

            picked = False
            # The LinkedIn leaf lives under different parents per tenant (Gartner: "Social
            # Media/Internet"; NVIDIA: "Job Board"), so drill EVERY top-level category —
            # preferred ones first — reopening between tries to return to the top.
            parent_pref = ["job board", "social media", "internet", "professional",
                           "networking", "online", "website", "search", "referral"]

            async def _top_categories():
                try:
                    return await page.evaluate(
                        "() => Array.from(document.querySelectorAll("
                        "\"[data-automation-id='promptOption']\")).map(e=>(e.innerText||'').trim())"
                        ".filter(Boolean)")
                except Exception:
                    return []

            def _rank(c):
                cl = c.lower()
                return next((i for i, p in enumerate(parent_pref) if p in cl), len(parent_pref))

            cats = sorted(set(await _top_categories()), key=_rank)
            for cand in cands:
                # 1) flat-search tenants: type then click the leaf directly.
                try:
                    sb = page.locator("input[data-automation-id='searchBox'], "
                                      "[data-automation-id='multiselectInputContainer'] input").first
                    if await sb.count() > 0 and await sb.is_visible(timeout=500):
                        await sb.fill(cand); await asyncio.sleep(0.9)
                except Exception:
                    pass
                if await _click_leaf(cand):
                    picked = True; break
                # 2) hierarchical: drill each category, reopening to return to top.
                for catname in cats:
                    try:
                        try: await page.keyboard.press("Escape")
                        except Exception: pass
                        await asyncio.sleep(0.25)
                        await cont.click(); await asyncio.sleep(0.5)
                        sb = page.locator("input[data-automation-id='searchBox'], "
                                          "[data-automation-id='multiselectInputContainer'] input").first
                        if await sb.count() > 0 and await sb.is_visible(timeout=300):
                            await sb.fill(""); await asyncio.sleep(0.25)
                        cat = page.locator("[data-automation-id='promptOption']").filter(
                            has_text=_re.compile(rf'^\s*{_re.escape(catname)}\s*$', _re.IGNORECASE)).first
                        if await cat.count() == 0:
                            continue
                        await cat.click(); await asyncio.sleep(0.6)
                        if await _click_leaf(cand):
                            picked = True; break
                    except Exception:
                        pass
                if picked:
                    break
            # Last resort for a REQUIRED source field whose taxonomy matched none of the
            # candidates: drill the first category and pick its first real leaf, so a
            # required multiselect never blocks the whole application.
            via_fallback = False
            if not picked and "*" in (label or "") and cats:
                try:
                    try: await page.keyboard.press("Escape")
                    except Exception: pass
                    await asyncio.sleep(0.25)
                    await cont.click(); await asyncio.sleep(0.5)
                    cat = page.locator("[data-automation-id='promptOption']").filter(
                        has_text=_re.compile(rf'^\s*{_re.escape(cats[0])}\s*$', _re.IGNORECASE)).first
                    if await cat.count() > 0:
                        await cat.click(); await asyncio.sleep(0.6)
                    leaf = page.locator("[data-automation-id='promptLeafNode']").first
                    if await leaf.count() > 0 and await leaf.is_visible(timeout=500):
                        picked_text = (await leaf.inner_text()).strip()
                        await leaf.click(); await asyncio.sleep(0.4)
                        if await _selected():
                            picked = via_fallback = True
                            logger.info("workday_multiselect_fallback",
                                        field=(label or "")[:40], value=picked_text[:30])
                except Exception:
                    pass
            if picked and not via_fallback:
                logger.info("workday_multiselect", field=(label or "")[:40], value=cands[0])
            elif not picked:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
        except Exception:
            pass


async def _workday_fill_self_id(page, profile: dict) -> None:
    """Fill the Workday 'Self Identify' (Voluntary Self-Identification of Disability)
    page: the Name e-signature, today's Date, and the disability-status choice.
    Guarded to this page so the generic 'Name' label can't catch other pages."""
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        return
    if not any(k in body for k in ("self-identification", "self identify",
                                   "please check one of the boxes")):
        return
    p = profile.get("personal", {})
    await _wd_fill_input(page, ["name", "selfIdentifiedDisabilityData_name", "personName"],
                         ["Name"], p.get("name", ""))
    # Date (today). Workday date spinbuttons commit only when you focus the Month
    # segment and type the digits CONTINUOUSLY (MMDDYYYY) — the widget auto-advances
    # month→day→year and fires onChange. Per-segment .fill() shows a value but leaves it
    # un-committed ("Date is required" despite showing MM/DD/YYYY). Clear, type, blur.
    try:
        now = datetime.now(timezone.utc)
        mi = page.locator("input[data-automation-id='dateSectionMonth-input']").first
        di = page.locator("input[data-automation-id='dateSectionDay-input']").first
        yi = page.locator("input[data-automation-id='dateSectionYear-input']").first
        # Re-type the FULL date unless all three segments are already correct (a prior
        # partial fill may have left only the year set → field still errors).
        m_ok = (await mi.input_value()).strip() == now.strftime("%m") if await mi.count() > 0 else False
        d_ok = (await di.input_value()).strip() == now.strftime("%d") if await di.count() > 0 else False
        y_ok = (await yi.input_value()).strip() == now.strftime("%Y") if await yi.count() > 0 else False
        if await mi.count() > 0 and not (m_ok and d_ok and y_ok):
            # The segments are React spinbuttons hidden behind a styled display. JS-setting
            # the value is treated as UNTRUSTED and reverts on Save&Continue. The reliable
            # way is real keystrokes: focus EACH segment, clear it, type its digits (typing
            # one segment at a time avoids the year-scramble that continuous typing causes).
            for aid, val in (("dateSectionMonth-input", now.strftime("%m")),
                             ("dateSectionDay-input", now.strftime("%d")),
                             ("dateSectionYear-input", now.strftime("%Y"))):
                el = page.locator(f"input[data-automation-id='{aid}']").first
                if await el.count() == 0:
                    continue
                try:
                    await el.focus(); await asyncio.sleep(0.08)
                    for _ in range(5):
                        await page.keyboard.press("Backspace")   # clear segment
                    await page.keyboard.type(val, delay=70)      # trusted keystrokes
                    await asyncio.sleep(0.12)
                except Exception:
                    pass
            await page.keyboard.press("Tab"); await asyncio.sleep(0.3)  # blur → commit
            committed = (await yi.input_value()) if await yi.count() > 0 else ""
            logger.info("workday_selfid_date", value=now.strftime("%m/%d/%Y"),
                        committed_year=committed)
    except Exception as e:
        logger.warning("workday_selfid_date_err", error=str(e)[:120])
    # Disability status → "No, I do not have a disability" (matches profile eeoc_disability=No)
    try:
        inputs = await page.locator("input[type='radio'], input[type='checkbox']").all()
    except Exception:
        inputs = []
    for inp in inputs:
        try:
            rid = await inp.get_attribute("id")
            lbl = ""
            if rid:
                le = page.locator(f"label[for='{rid}']")
                if await le.count() > 0:
                    lbl = (await le.first.inner_text()).strip().lower()
            if not lbl:
                lbl = (await inp.evaluate(
                    "el => { let p=el; for(let i=0;i<6;i++){ p=p.parentElement; if(!p) break; "
                    "const t=p.innerText; if(t && t.trim().length>5) return t; } return ''; }")
                       ).strip().lower()
            if "do not have a disability" in lbl or "don't have a disability" in lbl:
                checked = await inp.is_checked()
                for _ in range(3):
                    if checked:
                        break
                    try:
                        await inp.click(force=True); await asyncio.sleep(0.4)
                        checked = await inp.is_checked()
                    except Exception:
                        pass
                    if not checked and rid:
                        # Workday hides the native input behind a styled label — clicking
                        # the label is what actually toggles it.
                        try:
                            await page.locator(f"label[for='{rid}']").first.click()
                            await asyncio.sleep(0.4); checked = await inp.is_checked()
                        except Exception:
                            pass
                logger.info("workday_disability_selfid", choice=lbl[:40], checked=checked)
                break
        except Exception:
            pass


async def _workday_check_consents(page) -> None:
    """Tick required affirmative-consent checkboxes (Terms & Conditions, privacy,
    accuracy certifications). The human still reviews and clicks Submit at the gate,
    which ratifies these — we only complete the form up to that point."""
    consent_kw = ("consent", "i agree", "i have read", "terms & conditions",
                  "terms and conditions", "privacy", "i certify", "i acknowledge",
                  "accurate", "true and complete", "read and accept")
    try:
        boxes = await page.locator("input[type='checkbox']").all()
    except Exception:
        boxes = []
    for cb in boxes:
        try:
            if await cb.is_checked():
                continue
            rid = await cb.get_attribute("id")
            lbl = ""
            if rid:
                le = page.locator(f"label[for='{rid}']")
                if await le.count() > 0:
                    lbl = (await le.first.inner_text()).strip().lower()
            if not lbl:
                lbl = (await cb.evaluate(
                    "el => { let p=el; for(let i=0;i<6;i++){ p=p.parentElement; if(!p) break; "
                    "const t=p.innerText; if(t && t.trim().length>8) return t; } return ''; }")
                       ).strip().lower()
            if any(k in lbl for k in consent_kw):
                await cb.click(force=True); await asyncio.sleep(0.3)
                logger.info("workday_consent_checked", label=lbl[:50])
        except Exception:
            pass


async def _workday_set_radios(page, profile: dict) -> None:
    """Answer Workday radio buttons (work-auth, EEO, yes/no) by candidate matching.

    Per-radio rather than grouped-by-name: some Workday tenants render Yes/No radios
    with NO name attribute, which would merge every nameless radio into one bogus group.
    For each unchecked radio we read its OWN option label + the nearest question text,
    and tick it only when its label matches the desired answer for that question.
    """
    try:
        radios = await page.locator("input[type='radio']").all()
    except Exception:
        radios = []
    for r in radios:
        try:
            if await r.is_checked():
                continue
            # The question this radio belongs to (nearest ancestor text > 4 chars,
            # which skips the short "Yes"/"No" option labels).
            q = await r.evaluate("""el => {
                let p=el; for(let i=0;i<8;i++){p=p.parentElement;if(!p)break;
                    for(const s of ['legend','label','[id*="label"]','p']){const c=p.querySelector(s);
                        if(c && c.innerText.trim().length>4) return c.innerText.trim();}}
                return '';}""")
            cands = _workday_answer((q or "").lower(), profile)
            if not cands:
                continue
            # This radio's own option label.
            rid = await r.get_attribute("id")
            own = ""
            if rid:
                le = page.locator(f"label[for='{rid}']")
                if await le.count() > 0:
                    own = (await le.first.inner_text()).strip().lower()
            if not own:
                own = (await r.evaluate(
                    "el => { const l = el.closest('label'); return l ? l.innerText.trim() : "
                    "(el.getAttribute('aria-label')||''); }") or "").strip().lower()
            if not own:
                continue
            if any(own == c.lower() or c.lower() in own or own in c.lower() for c in cands):
                await r.check(); await asyncio.sleep(0.2)
                logger.info("workday_radio", q=(q or "")[:40], value=own[:20])
        except Exception:
            pass


async def _wd_step_index(page) -> str:
    """A signature of the current wizard step that changes only when the page actually
    advances. Uses Workday's 'current step N of M' a11y text (present in body innerText);
    falls back to the prominent step heading."""
    import re as _re
    try:
        body = await page.inner_text("body")
    except Exception:
        return ""
    m = _re.search(r'current step (\d+) of (\d+)', body, _re.IGNORECASE)
    if m:
        return f"step{m.group(1)}of{m.group(2)}"
    # Fallback: the step title rendered as the page's heading element.
    try:
        h = await page.evaluate(
            "() => { for (const s of ['[data-automation-id=\"pageHeader\"]','h2','h3']) {"
            " const e=document.querySelector(s); if (e&&(e.innerText||'').trim()) "
            "return (e.innerText||'').trim().slice(0,40); } return ''; }")
        if h:
            return h
    except Exception:
        pass
    return body[:60]


async def _wd_has_errors(page) -> bool:
    """True if Workday's validation surfaced an 'Errors Found' panel / inline errors."""
    try:
        if await page.locator("[data-automation-id='errorMessage']").count() > 0:
            return True
        body = (await page.inner_text("body")).lower()[:1800]
        return "errors found" in body or "error - " in body
    except Exception:
        return False


async def _wd_on_signin_page(page) -> bool:
    """True if Workday is still showing an auth gate (sign-in / create-account) instead
    of the application form — i.e. authentication did not resolve."""
    try:
        # Any application-form field present ⇒ authenticated, past the gate.
        if await page.locator(
                "[data-automation-id='legalNameSection_firstName'], "
                "[data-automation-id='firstName'], "
                "[data-automation-id='fileUploadDropZone'], "
                "[data-automation-id='multiselectInputContainer']").count() > 0:
            return False
        gate = await page.locator(
            "[data-automation-id='GoogleSignInButton'], "
            "[data-automation-id='SignInWithEmailButton'], "
            "[data-automation-id='signInSubmitButton'], "
            "[data-automation-id='createAccountSubmitButton'], "
            "button:has-text('Sign in with Google'), button:has-text('Sign in with email')").count()
        return gate > 0
    except Exception:
        return False


async def _wd_sso_available(page) -> bool:
    """True if the auth gate offers a federated SSO button (Google/LinkedIn/Apple),
    which can bypass the reCAPTCHA-gated create-account form."""
    try:
        return await page.locator(
            "[data-automation-id='GoogleSignInButton'], "
            "button:has-text('Sign in with Google'), a:has-text('Sign in with LinkedIn'), "
            "button:has-text('Sign in with LinkedIn'), button:has-text('Sign in with Apple')"
        ).count() > 0
    except Exception:
        return False


async def _fill_workday(page: Page, profile: dict, resume_pdf: Path, job: JobListing, llm) -> bool:
    """
    Workday multi-page wizard: sign-in/account → loop pages (My Information →
    My Experience → Questions → Voluntary Disclosures) filling each, clicking
    "Save and Continue", until the Review/Submit page. Stops before final submit
    (the caller's submit phase clicks Submit). Best-effort; Workday is highly dynamic.
    """
    p = profile["personal"]
    defs = profile.get("application_defaults", {})
    name_parts = p.get("name", "").split()
    first = name_parts[0] if name_parts else ""
    last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    await asyncio.sleep(3.0)

    # ── Dismiss cookie-consent banners (block clicks/typing on Workday) ─────────
    for ck in ["button:has-text('Accept Cookies')", "button:has-text('Accept All')",
               "button:has-text('Accept')", "[data-automation-id='legalNoticeAcceptButton']",
               "#onetrust-accept-btn-handler", "button:has-text('I Agree')"]:
        try:
            cb = page.locator(ck).first
            if await cb.count() > 0 and await cb.is_visible(timeout=1000):
                await cb.click(); await asyncio.sleep(0.5); break
        except Exception:
            pass

    # ── Resume an existing in-progress application (drafted earlier) ───────────
    # When the candidate already started this req, the posting shows "Manage
    # Application" / "Continue" instead of "Apply" — clicking it jumps straight into
    # the saved wizard step (no apply modal, no re-auth). Try this BEFORE Apply.
    for resume_sel in ("a[data-automation-id='manageApplication']",
                       "button[data-automation-id='manageApplication']",
                       "button:has-text('Manage Application')", "a:has-text('Manage Application')",
                       "button:has-text('Continue Application')", "a:has-text('Continue Application')",
                       "button:has-text('Continue your application')",
                       "button:has-text('Continue Your Application')"):
        try:
            rb = page.locator(resume_sel).first
            if await rb.count() > 0 and await rb.is_visible(timeout=1200):
                await _wd_coord_click(page, rb); await asyncio.sleep(3.0)
                logger.info("workday_resumed_draft", via=resume_sel[:30])
                break
        except Exception:
            pass

    # ── Sign-in / account handling ───────────────────────────────────────────
    # On a base posting page the apply options only appear after clicking "Apply".
    for apply_text in ["Apply", "Apply Now"]:
        try:
            b = page.locator(f"a[data-automation-id='adventureButton'], "
                             f"button:has-text('{apply_text}'), a:has-text('{apply_text}')").first
            if await b.count() > 0 and await b.is_visible(timeout=1200):
                await b.click(); await asyncio.sleep(2.0); break
        except Exception:
            pass
    # After "Apply", Workday shows a modal: Autofill with Resume / Apply Manually /
    # Use My Last Application. Click the GUEST path by its automation-id (text-match
    # alone misses Workday's button markup). Then the Sign In page renders.
    for guest_sel in ["[data-automation-id='applyManually']",
                      "[data-automation-id='autofillWithResume']",
                      "button:has-text('Continue your application')",
                      "button:has-text('Apply Manually')", "a:has-text('Apply Manually')",
                      "button:has-text('Continue as Guest')"]:
        try:
            b = page.locator(guest_sel).first
            if await b.count() > 0 and await b.is_visible(timeout=1500):
                try: await b.click(timeout=4000)
                except Exception: await b.evaluate("el => el.click()")
                await asyncio.sleep(3.0); break
        except Exception:
            pass
    # ── Federated Google SSO (preferred) ────────────────────────────────────────
    # "Sign in with Google" works across ALL Workday tenants once the profile holds a
    # Google session, and skips the reCAPTCHA-gated create-account form. WORKDAY_GOOGLE_LOGIN:
    #   "interactive" → click it and WAIT (~5 min) for a one-time human Google login;
    #   "1"/"auto"    → click it and rely on the existing session (short wait).
    import os as _os
    _g_mode = _os.environ.get("WORKDAY_GOOGLE_LOGIN", "").lower()
    if _g_mode:
        try:
            gbtn = page.locator("[data-automation-id='GoogleSignInButton'], "
                                "button:has-text('Sign in with Google')").first
            if await gbtn.count() > 0 and await gbtn.is_visible(timeout=1500):
                box = await gbtn.bounding_box()
                if box:
                    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                else:
                    await gbtn.click(force=True)
                interactive = _g_mode == "interactive"
                if interactive:
                    logger.info("workday_google_login_wait",
                                note="Sign into Google (the applicant Gmail account) in the popup window")
                # auto: a generous-enough window for the Google OAuth round-trip +
                # Workday redirect (15s occasionally timed out → spurious needs_auth).
                max_wait, waited, signed = (300 if interactive else 30), 0, False
                while waited < max_wait:
                    await asyncio.sleep(5); waited += 5
                    try:
                        on_form = await page.locator(
                            "[data-automation-id='legalNameSection_firstName'], "
                            "[data-automation-id='firstName'], "
                            "[data-automation-id='fileUploadDropZone']").count()
                    except Exception:
                        on_form = 0
                    if on_form > 0:
                        logger.info("workday_google_signed_in", waited_s=waited)
                        signed = True; break
                # #3 — clicked Google but never reached the form ⇒ the saved Google
                # session has expired (auto mode) or the human didn't finish (interactive).
                if not signed and await _wd_on_signin_page(page):
                    logger.warning(
                        "workday_google_session_expired", mode=_g_mode,
                        note="Sign in with Google did not authenticate — re-login needed "
                             "(run once with WORKDAY_GOOGLE_LOGIN=interactive).")
        except Exception as e:
            logger.warning("workday_google_signin_error", error=str(e)[:100])

    # Account handling. SIGN-IN FIRST with the stored credential (reliable once the
    # account exists); only fall back to account creation if sign-in doesn't advance.
    # (Full unattended account *creation* is brittle: React-gated acknowledgment
    # checkbox + email-link verification — best done once collaboratively.)
    from core.config import get_settings as _gs
    pw = _gs().workday_password or defs.get("workday_password") or f"{first}{last}!{defs.get('location_zip','00000')}Wd"
    try:
        # Reveal the email form (skip SSO buttons like "Sign in with Google").
        for reveal in ("button:has-text('Sign in with email')", "a:has-text('Sign in with email')",
                       "button:has-text('Sign In with Email')"):
            rb = page.locator(reveal).first
            if await rb.count() > 0 and await rb.is_visible(timeout=1000):
                await rb.click(); await asyncio.sleep(1.5); break

        # The account already exists, so SIGN IN is the reliable path. Workday often
        # renders the Create Account view by default (a verifyPassword field present);
        # switch to the Sign In view via its toggle link before attempting, otherwise
        # the verify_present gate below skips sign-in and we'd try to re-create the
        # account (which fails on the duplicate email and strands us on auth).
        verify_present = await page.locator("[data-automation-id='verifyPassword']").count() > 0
        if verify_present:
            for to_signin in ("a[data-automation-id='signInLink']",
                              "button[data-automation-id='signInLink']",
                              "[data-automation-id='goToSignInLink']",
                              "a:has-text('Sign In')", "button:has-text('Sign In')"):
                try:
                    sl = page.locator(to_signin).first
                    if await sl.count() > 0 and await sl.is_visible(timeout=1000):
                        await sl.click(); await asyncio.sleep(1.5)
                        verify_present = (
                            await page.locator("[data-automation-id='verifyPassword']").count() > 0)
                        if not verify_present:
                            logger.info("workday_switched_to_signin")
                            break
                except Exception:
                    pass

        # 1) Attempt SIGN IN (Sign-In view shows email + password, no verify field).
        signed_in = False
        email_in = page.locator("[data-automation-id='email'], input[type='email']").first
        pw_in = page.locator("[data-automation-id='password'], input[type='password']").first
        if await email_in.count() > 0 and await pw_in.count() > 0 and not verify_present:
            await email_in.fill(p.get("email", "")); await asyncio.sleep(0.2)
            await pw_in.fill(pw); await asyncio.sleep(0.2)
            for bsel in ("[data-automation-id='signInSubmitButton']", "button:has-text('Sign In')"):
                bb = page.locator(bsel).first
                if await bb.count() > 0:
                    await _wd_coord_click(page, bb)   # click_filter overlay needs a coordinate click
                    await asyncio.sleep(3.0); break
            # Advanced past the sign-in step?
            try:
                body_si = (await page.inner_text("body")).lower()
                signed_in = not any(k in body_si for k in (
                    "sign in", "create account", "invalid", "incorrect", "doesn't match",
                    "no account"))
            except Exception:
                signed_in = False
            if signed_in:
                logger.info("workday_signed_in", email=p.get("email"))

        # 2) Fall back to CREATE ACCOUNT only if sign-in didn't get us in.
        if not signed_in:
            for mk in ("a:has-text('Create Account')", "button:has-text('Create Account')",
                       "[data-automation-id='createAccountLink']"):
                cab = page.locator(mk).first
                if await cab.count() > 0 and await cab.is_visible(timeout=1000):
                    try: await cab.click(timeout=2500)
                    except Exception:
                        try: await cab.evaluate("el => el.click()")
                        except Exception: pass
                    await asyncio.sleep(1.2); break
            email_in = page.locator("[data-automation-id='email'], input[type='email']").first
            pw_in = page.locator("[data-automation-id='password'], input[type='password']").first
            if await email_in.count() > 0 and await pw_in.count() > 0:
                await email_in.fill(p.get("email", "")); await asyncio.sleep(0.2)
                await pw_in.fill(pw); await asyncio.sleep(0.2)
                vpw = page.locator("[data-automation-id='verifyPassword']").first
                if await vpw.count() > 0:
                    await vpw.fill(pw); await asyncio.sleep(0.2)
                for cbsel in ("[data-automation-id='createAccountCheckbox']", "input[type='checkbox']"):
                    cb = page.locator(cbsel).first
                    if await cb.count() > 0:
                        try:
                            await cb.scroll_into_view_if_needed(timeout=2000)
                        except Exception:
                            pass
                        # A real (force) click toggles the native checkbox AND fires
                        # Workday's trusted-event validation — JS .checked=true does not,
                        # which silently fails the create-account submit.
                        try:
                            await cb.click(timeout=3000, force=True)
                        except Exception:
                            try:
                                await cb.evaluate("el => { el.checked = true; "
                                                  "el.dispatchEvent(new Event('click', {bubbles:true})); "
                                                  "el.dispatchEvent(new Event('change', {bubbles:true})); }")
                            except Exception:
                                pass
                        await asyncio.sleep(0.4)
                        break
                for bsel in ("[data-automation-id='createAccountSubmitButton']",
                             "button:has-text('Create Account')"):
                    bb = page.locator(bsel).first
                    if await bb.count() > 0:
                        # click_filter overlay → coordinate click (inner button no-ops)
                        if await _wd_coord_click(page, bb):
                            await asyncio.sleep(3.0)
                            logger.info("workday_account", email=p.get("email"),
                                        password_hint=pw[:3] + "***", note="create-account submitted (coord)")
                            break
    except Exception as e:
        logger.warning("workday_account_error", error=str(e)[:100])

    # ── Email-verification link (Workday blocks the form until verified) ────────
    try:
        import time as _t
        body0 = (await page.inner_text("body")).lower()
        if any(k in body0 for k in ("verify your email", "check your email", "verification email",
                                     "confirm your email", "we sent")):
            logger.info("workday_email_verify_required", note="fetching verify link from Gmail")
            ts = _t.time() - 30
            link = None
            for delay in (4, 6, 10, 16, 26):
                await asyncio.sleep(delay)
                link = _fetch_workday_verify_link(not_before=ts)
                if link:
                    break
            if link:
                await page.goto(link, wait_until="domcontentloaded")
                await asyncio.sleep(3.0)
                logger.info("workday_email_verified", link=link[:80])
            else:
                logger.warning("workday_verify_link_not_found",
                               note="account created; verify link not in Gmail yet")
    except Exception as e:
        logger.warning("workday_verify_error", error=str(e)[:100])

    # ── Ensure signed in ────────────────────────────────────────────────────────
    # If we're now on a Sign In page (account already existed → create redirected to
    # sign-in, or we started on sign-in), complete the sign-in with stored creds.
    try:
        for _attempt in range(2):
            has_verify = await page.locator("[data-automation-id='verifyPassword']").count() > 0
            ei = page.locator("[data-automation-id='email'], input[type='email']").first
            pi = page.locator("[data-automation-id='password'], input[type='password']").first
            body_si = (await page.inner_text("body")).lower()
            on_signin = (not has_verify and await ei.count() > 0 and await pi.count() > 0
                         and "sign in" in body_si and "my information" not in body_si)
            if not on_signin:
                break
            await ei.fill(p.get("email", "")); await asyncio.sleep(0.2)
            await pi.fill(pw); await asyncio.sleep(0.2)
            sb = page.locator("[data-automation-id='signInSubmitButton']").first
            if await sb.count() > 0:
                await _wd_coord_click(page, sb); await asyncio.sleep(3.5)
                logger.info("workday_signin_attempt", email=p.get("email"), attempt=_attempt)
            else:
                break
    except Exception as e:
        logger.warning("workday_signin_error", error=str(e)[:100])

    # ── Auth resolution check (#2/#3) ────────────────────────────────────────
    # If we're still on a sign-in/create-account gate, authentication failed. Emit a
    # precise, actionable reason so the caller surfaces "needs login" instead of
    # parking with a half-filled form. _wd_auth_blocked is read by collab_submit.
    if await _wd_on_signin_page(page):
        has_sso = await _wd_sso_available(page)
        if has_sso:
            reason = ("Federated SSO available but session not authenticated — run once "
                      "with WORKDAY_GOOGLE_LOGIN=interactive to (re)login.")
            logger.warning("workday_auth_blocked", kind="sso_needs_login", reason=reason)
        else:
            reason = ("No SSO (Google/LinkedIn) on this tenant and the account isn't "
                      "signed in — create-account is reCAPTCHA-gated; needs a one-time "
                      "human account setup.")
            logger.warning("workday_auth_blocked", kind="no_sso", reason=reason)
        try:
            page._wd_auth_blocked = {"sso": has_sso, "reason": reason}  # read by caller
        except Exception:
            pass

    # ── Page loop ────────────────────────────────────────────────────────────
    # Mutable state shared across re-fill passes (closure can't rebind plain locals).
    state = {"uploaded": False, "linkedin_logged": False}

    async def _fill_current_page() -> None:
        """Fill every field type on whatever Workday page is currently shown. Safe to
        call repeatedly — each handler skips already-filled fields, so re-passes only
        touch fields that newly appeared (staged reveals) or failed validation."""
        # Identity text fields — bare data-automation-ids (wrapper-div OR input both
        # tried by the helper), then non-exact aria-label fallbacks.
        _id_fields = [
            (["legalNameSection_firstName", "firstName", "name--legalName--firstName"],
             ["First Name", "Legal Name - First Name"], first),
            (["legalNameSection_lastName", "lastName", "name--legalName--lastName"],
             ["Last Name", "Legal Name - Last Name"], last),
            (["phone-number", "phoneNumber", "phone--phoneNumber"],
             ["Phone Number"], _phone_digits(p.get("phone", ""))),
            (["addressSection_addressLine1", "address--addressLine1"],
             ["Address Line 1"], defs.get("location_address", "[REDACTED ADDRESS]")),
            (["addressSection_city", "address--city"], ["City"], defs.get("location_city", "McKinney")),
            (["addressSection_postalCode", "address--postalCode"], ["Postal Code"],
             defs.get("location_zip", "00000")),
        ]
        for aids, labels, value in _id_fields:
            if not value:
                continue
            await _wd_fill_input(page, aids, labels, value)

        # Resume upload (My Experience page) — REPLACE, don't append. Workday keeps the
        # in-progress application server-side per account, so retries/re-runs would
        # otherwise stack duplicate resumes. When files are already attached there is
        # NO visible upload input (only the file-upload-item list + delete buttons), so
        # detect the resume area by EITHER the input or an existing attachment.
        if not state["uploaded"]:
            on_resume_page = await page.locator(
                "[data-automation-id='file-upload-input'], "
                "[data-automation-id='attachments-FileUpload'], "
                "[data-automation-id='file-upload-item'], "
                "input[type='file']").count() > 0
            if on_resume_page:
                deleted = 0
                for _ in range(10):
                    d = page.locator("button[data-automation-id='delete-file']").first
                    if await d.count() == 0:
                        break
                    try:
                        await d.click(); await asyncio.sleep(1.0); deleted += 1
                    except Exception:
                        break
                if deleted:
                    logger.info("workday_resume_deleted_existing", count=deleted)
                await asyncio.sleep(0.5)
                for usel in ["[data-automation-id='file-upload-input']",
                             "[data-automation-id='attachments-FileUpload'] input[type='file']",
                             "input[type='file'][accept*='pdf']", "input[type='file']"]:
                    try:
                        el = page.locator(usel).first
                        if await el.count() > 0:
                            await el.set_input_files(str(resume_pdf)); await asyncio.sleep(3.0)
                            logger.info("resume_uploaded", ats="workday", path=str(resume_pdf))
                            state["uploaded"] = True; break
                    except Exception:
                        pass

        # LinkedIn URL — normalize to canonical https://www.linkedin.com/... (Workday
        # rejects the bare host).
        li_raw = p.get("linkedin_url", "").strip()
        if li_raw:
            li_url = li_raw if li_raw.startswith("http") else "https://" + li_raw.lstrip("/")
            li_url = li_url.replace("http://", "https://")
            if "://linkedin.com" in li_url:
                li_url = li_url.replace("://linkedin.com", "://www.linkedin.com")
            li_ok = await _wd_fill_input(
                page, ["formField-linkedInAccount", "linkedInAccount", "linkedin"],
                ["LinkedIn", "LinkedIn Account", "LinkedIn URL"], li_url)
            if li_ok and not state["linkedin_logged"]:
                logger.info("workday_linkedin_filled", url=li_url[:45]); state["linkedin_logged"] = True

        # Section handlers (each guarded/idempotent to its own page).
        await _workday_fill_experience(page, profile)
        await _workday_fill_education(page, profile)
        await _workday_fill_skills(page, profile)
        await _workday_fill_cover_letter(page, profile, job, llm)
        await _workday_add_language(page, profile)
        await _workday_set_dropdowns(page, profile)
        await _workday_set_multiselects(page, profile)
        await _fill_dropdowns(page, profile)
        await _workday_set_radios(page, profile)
        await _workday_fill_self_id(page, profile)
        await _workday_check_consents(page)
        await _workday_fill_typed_questions(page, profile)
        await _answer_open_questions(page, job, llm, profile)

    # ── Iterate: fill the current page, try to advance, and ONLY move on once the step
    # actually changes. Same-step after a Save&Continue means validation errors or a
    # staged reveal (e.g. picking a language unlocks proficiency dropdowns) — re-fill
    # and retry. Park for the human only after several genuinely-stuck passes. ──────
    stuck = 0
    for _it in range(40):  # safety cap; stuck-detection ends the loop in practice
        await asyncio.sleep(1.5)

        # Reached the review/submit page? Stop — caller submits.
        try:
            done_btn = page.locator(
                "[data-automation-id='bottom-navigation-done-button'], "
                "button:has-text('Submit Application'), button:has-text('Submit')").first
            body0 = (await page.inner_text("body")).lower()
            if ((await done_btn.count() > 0 and await done_btn.is_visible())
                    or "review" in body0[:400]) and "submit" in body0[:2000]:
                logger.info("workday_reached_review")
                break
        except Exception:
            pass

        step = await _wd_step_index(page)
        await _fill_current_page()

        # Advance (click_filter overlay → coordinate click).
        for sel in ["[data-automation-id='bottom-navigation-next-button']",
                    "button:has-text('Save and Continue')", "button:has-text('Continue')",
                    "button:has-text('Next')"]:
            try:
                b = page.locator(sel).first
                if await b.count() > 0 and await b.is_visible(timeout=1000):
                    if await _wd_coord_click(page, b):
                        await asyncio.sleep(2.5); break
            except Exception:
                pass

        new_step = await _wd_step_index(page)
        if new_step and step and new_step != step:
            logger.info("workday_advanced", frm=step, to=new_step)
            stuck = 0
            continue
        # Same step → errors or a staged reveal we just filled; re-pass next iteration.
        stuck += 1
        if stuck >= 4:
            logger.info("workday_no_advance", step=step,
                        errors=await _wd_has_errors(page), attempts=stuck)
            break

    if not state["uploaded"]:
        logger.warning("workday_resume_not_uploaded", note="Could not find resume upload field")
    return True


# ── Ashby Filler ───────────────────────────────────────────────────────────────

async def _fill_ashby(page: Page, profile: dict, resume_pdf: Path, job: JobListing, llm) -> bool:
    """
    Fill Ashby HQ application forms (app.ashbyhq.com).
    Ashby uses a clean React form with data-testid attributes and standard name/label patterns.
    """
    p = profile["personal"]
    defs = profile.get("application_defaults", {})
    name_parts = p.get("name", "").split()
    first = name_parts[0] if name_parts else ""
    last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    await asyncio.sleep(1.5)

    # ── Standard identity fields ──────────────────────────────────────────
    # Ashby's hosted forms use a SINGLE full-name field (_systemfield_name); some
    # embedded variants split into first/last. Fill whichever is present.
    field_map = [
        (["input[name='_systemfield_name']", "input[id='_systemfield_name']",
          "input[name='name']", "input[placeholder*='Full name']",
          "input[placeholder*='full name']"], p.get("name", "")),
        (["[data-testid='first-name-input']", "input[name='firstName']", "input[name*='first']",
          "input[placeholder*='First']", "input[id*='first']"], first),
        (["[data-testid='last-name-input']", "input[name='lastName']", "input[name*='last']",
          "input[placeholder*='Last']", "input[id*='last']"], last),
        (["input[name='_systemfield_email']", "[data-testid='email-input']",
          "input[name='email']", "input[type='email']",
          "input[placeholder*='mail']"], p.get("email", "")),
        (["[data-testid='phone-input']", "input[name='phone']", "input[type='tel']",
          "input[placeholder*='Phone']", "input[placeholder*='phone']"], _phone_digits(p.get("phone", ""))),
        (["input[name*='linkedin']", "input[placeholder*='LinkedIn']",
          "input[placeholder*='linkedin']", "input[id*='linkedin']"], p.get("linkedin_url", "")),
        (["input[name*='github']", "input[name*='portfolio']", "input[name*='website']",
          "input[placeholder*='GitHub']", "input[placeholder*='Portfolio']",
          "input[placeholder*='Website']"], p.get("github_url", "")),
        (["input[name*='salary']", "input[placeholder*='salary']",
          "input[placeholder*='compensation']"], defs.get("salary_display", "")),
    ]
    for selectors, value in field_map:
        if not value:
            continue
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(value)
                    await asyncio.sleep(0.2)
                    break
            except Exception:
                pass

    # ── Resume upload ──────────────────────────────────────────────────────
    for sel in ["input[type='file'][accept*='pdf']", "input[type='file'][name*='resume']",
                "input[type='file'][name*='cv']", "input[type='file']"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.set_input_files(str(resume_pdf))
                await asyncio.sleep(2.0)
                logger.info("resume_uploaded", ats="ashby", path=str(resume_pdf))
                break
        except Exception:
            pass

    # ── Cover letter ──────────────────────────────────────────────────────
    if defs.get("cover_letter") == "auto":
        cl = await _generate_cover_letter(job, profile, llm)
        if cl:
            for sel in ["textarea[name*='cover']", "textarea[placeholder*='cover']",
                        "textarea[placeholder*='letter']", "textarea[name*='message']"]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.fill(cl)
                        await asyncio.sleep(0.3)
                        break
                except Exception:
                    pass

    # ── Custom / dynamic questions ─────────────────────────────────────────
    # Ashby renders questions as labeled input/select/textarea blocks
    await _answer_open_questions(page, job, llm, profile)
    await _fill_dropdowns(page, profile)

    return True


# ── SmartRecruiters Filler ──────────────────────────────────────────────────────

async def _fill_smartrecruiters(page: Page, profile: dict, resume_pdf: Path, job: JobListing, llm) -> bool:
    """
    Fill SmartRecruiters application forms (careers.smartrecruiters.com).
    SmartRecruiters uses data-test attributes for reliable targeting.
    """
    p = profile["personal"]
    defs = profile.get("application_defaults", {})
    work_auth = profile.get("work_authorization", {})
    name_parts = p.get("name", "").split()
    first = name_parts[0] if name_parts else ""
    last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    await asyncio.sleep(2.0)

    # ── Identity fields ───────────────────────────────────────────────────
    field_map = [
        (["[data-test='input-firstName']", "input[name='firstName']",
          "input[id*='first']", "input[placeholder*='First']"], first),
        (["[data-test='input-lastName']", "input[name='lastName']",
          "input[id*='last']", "input[placeholder*='Last']"], last),
        (["[data-test='input-email']", "input[name='email']", "input[type='email']"], p.get("email", "")),
        (["[data-test='input-phone']", "input[name='phone']", "input[type='tel']"], _phone_digits(p.get("phone", ""))),
        (["input[name*='linkedin']", "input[placeholder*='LinkedIn']"], p.get("linkedin_url", "")),
        (["input[name*='website']", "input[placeholder*='website']",
          "input[placeholder*='Portfolio']"], p.get("github_url", "")),
    ]
    for selectors, value in field_map:
        if not value:
            continue
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(value)
                    await asyncio.sleep(0.2)
                    break
            except Exception:
                pass

    # ── Resume upload ──────────────────────────────────────────────────────
    for sel in ["[data-test='btn-upload-resume'] + input", "input[name='resume']",
                "input[type='file'][accept*='pdf']", "input[type='file']"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.set_input_files(str(resume_pdf))
                await asyncio.sleep(2.0)
                logger.info("resume_uploaded", ats="smartrecruiters", path=str(resume_pdf))
                break
        except Exception:
            pass

    # ── Work authorization / sponsorship radio ─────────────────────────────
    try:
        sponsorship_answer = work_auth.get("sponsorship_answer", "No")
        radios = page.locator("input[type='radio']")
        count = await radios.count()
        for i in range(count):
            r = radios.nth(i)
            rid = await r.get_attribute("id") or ""
            label_el = page.locator(f"label[for='{rid}']")
            label_text = ""
            if await label_el.count() > 0:
                label_text = (await label_el.first.inner_text()).strip().lower()
            rname = (await r.get_attribute("name") or "").lower()
            if any(k in rname or k in label_text for k in ("sponsor", "authorized", "legally")):
                if label_text == sponsorship_answer.lower():
                    await r.check()
    except Exception:
        pass

    # ── Custom questions ───────────────────────────────────────────────────
    await _answer_open_questions(page, job, llm, profile)
    await _fill_dropdowns(page, profile)

    return True


# ── iCIMS Filler ───────────────────────────────────────────────────────────────

async def _fill_icims(page: Page, profile: dict, resume_pdf: Path, job: JobListing, llm) -> bool:
    """
    Fill iCIMS application forms. iCIMS uses a multi-step wizard with
    standard HTML form fields and some iframe embedding.
    """
    p = profile["personal"]
    defs = profile.get("application_defaults", {})
    name_parts = p.get("name", "").split()
    first = name_parts[0] if name_parts else ""
    last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    await asyncio.sleep(2.0)

    # iCIMS: try "Apply Now" or "Continue" button to reach the form
    for btn_text in ["Apply Now", "Continue", "Next"]:
        try:
            btn = page.locator(f"button:has-text('{btn_text}'), a:has-text('{btn_text}')").first
            if await btn.count() > 0 and await btn.is_visible(timeout=2000):
                await btn.click()
                await asyncio.sleep(2.0)
                break
        except Exception:
            pass

    field_map = [
        (["input[id*='firstName']", "input[name*='firstName']", "input[placeholder*='First']"], first),
        (["input[id*='lastName']", "input[name*='lastName']", "input[placeholder*='Last']"], last),
        (["input[id*='email']", "input[name*='email']", "input[type='email']"], p.get("email", "")),
        (["input[id*='phone']", "input[name*='phone']", "input[type='tel']"], _phone_digits(p.get("phone", ""))),
        (["input[id*='linkedin']", "input[name*='linkedin']"], p.get("linkedin_url", "")),
        (["input[id*='city']", "input[name*='city']", "input[placeholder*='City']"],
         defs.get("location_city", "Plano")),
        (["input[id*='state']", "input[name*='state']", "input[placeholder*='State']"],
         defs.get("location_state", "TX")),
    ]
    for selectors, value in field_map:
        if not value:
            continue
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(value)
                    await asyncio.sleep(0.2)
                    break
            except Exception:
                pass

    # Resume upload
    for sel in ["input[type='file'][accept*='pdf']", "input[type='file'][name*='resume']",
                "input[type='file']"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.set_input_files(str(resume_pdf))
                await asyncio.sleep(2.0)
                logger.info("resume_uploaded", ats="icims", path=str(resume_pdf))
                break
        except Exception:
            pass

    await _answer_open_questions(page, job, llm, profile)
    await _fill_dropdowns(page, profile)
    return True


# ── BambooHR Filler ────────────────────────────────────────────────────────────

async def _fill_bamboohr(page: Page, profile: dict, resume_pdf: Path, job: JobListing, llm) -> bool:
    """
    Fill BambooHR application forms. BambooHR has a clean, standard HTML form.
    """
    p = profile["personal"]
    defs = profile.get("application_defaults", {})
    work_auth = profile.get("work_authorization", {})
    name_parts = p.get("name", "").split()

    await asyncio.sleep(1.5)

    field_map = [
        (["input[id='firstName']", "input[name='firstName']", "input[placeholder*='First']"],
         name_parts[0] if name_parts else ""),
        (["input[id='lastName']", "input[name='lastName']", "input[placeholder*='Last']"],
         " ".join(name_parts[1:]) if len(name_parts) > 1 else ""),
        (["input[id='email']", "input[name='email']", "input[type='email']"], p.get("email", "")),
        (["input[id='phone']", "input[name='phone']", "input[type='tel']"], _phone_digits(p.get("phone", ""))),
        (["input[name*='linkedin']", "input[placeholder*='LinkedIn']"], p.get("linkedin_url", "")),
        (["input[name*='address']", "input[placeholder*='Address']",
          "input[placeholder*='City']"], defs.get("location_city", "Plano, TX")),
        (["input[name*='salary']", "input[placeholder*='salary']"],
         defs.get("salary_display", "")),
    ]
    for selectors, value in field_map:
        if not value:
            continue
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(value)
                    await asyncio.sleep(0.2)
                    break
            except Exception:
                pass

    # Resume upload
    for sel in ["input[type='file']"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.set_input_files(str(resume_pdf))
                await asyncio.sleep(2.0)
                logger.info("resume_uploaded", ats="bamboohr", path=str(resume_pdf))
                break
        except Exception:
            pass

    # BambooHR work authorization checkboxes
    try:
        auth_value = work_auth.get("authorized_us", True)
        for checkbox_sel in ["input[name*='authorized']", "input[name*='workAuth']",
                              "input[id*='authorized']"]:
            el = page.locator(checkbox_sel).first
            if await el.count() > 0:
                if auth_value:
                    await el.check()
                break
    except Exception:
        pass

    await _answer_open_questions(page, job, llm, profile)
    await _fill_dropdowns(page, profile)
    return True


# ── Rippling Filler ────────────────────────────────────────────────────────────

async def _fill_rippling(page: Page, profile: dict, resume_pdf: Path, job: JobListing, llm) -> bool:
    """
    Fill Rippling job application forms (jobs.rippling.com).
    Rippling uses React with data-testid attributes.
    """
    p = profile["personal"]
    defs = profile.get("application_defaults", {})
    name_parts = p.get("name", "").split()
    first = name_parts[0] if name_parts else ""
    last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    await asyncio.sleep(2.0)

    field_map = [
        (["input[data-testid='first-name']", "input[name='firstName']",
          "input[placeholder*='First']", "input[id*='first']"], first),
        (["input[data-testid='last-name']", "input[name='lastName']",
          "input[placeholder*='Last']", "input[id*='last']"], last),
        (["input[data-testid='email']", "input[name='email']", "input[type='email']"], p.get("email", "")),
        (["input[data-testid='phone']", "input[name='phone']", "input[type='tel']"], _phone_digits(p.get("phone", ""))),
        (["input[name*='linkedin']", "input[placeholder*='LinkedIn']"], p.get("linkedin_url", "")),
        (["input[name*='website']", "input[placeholder*='Website']",
          "input[placeholder*='GitHub']"], p.get("github_url", "")),
    ]
    for selectors, value in field_map:
        if not value:
            continue
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(value)
                    await asyncio.sleep(0.2)
                    break
            except Exception:
                pass

    for sel in ["input[type='file'][accept*='pdf']", "input[type='file'][name*='resume']",
                "input[type='file']"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.set_input_files(str(resume_pdf))
                await asyncio.sleep(2.0)
                logger.info("resume_uploaded", ats="rippling", path=str(resume_pdf))
                break
        except Exception:
            pass

    await _answer_open_questions(page, job, llm, profile)
    await _fill_dropdowns(page, profile)
    return True


# ── Shared helpers for dynamic questions ───────────────────────────────────────

async def _answer_open_questions(page: Page, job: JobListing, llm, profile: dict | None = None) -> None:
    """
    Find all visible text inputs and textareas that look like open questions
    (have an associated label or placeholder) and answer them with the LLM.
    Skips fields that already have a value. When `profile` is supplied, also runs
    the field-type edge-case pass (dates, cover-letter file, etc.).
    """
    # Collect all labeled text inputs and textareas
    for selector in [
        "input[type='text']:visible",
        "input[type='number']:visible",
        "textarea:visible",
    ]:
        try:
            els = page.locator(selector)
            count = await els.count()
            for i in range(count):
                el = els.nth(i)
                try:
                    # Skip if already filled
                    existing = await el.input_value()
                    if existing.strip():
                        continue

                    # Get question context from label or placeholder
                    label_text = ""
                    eid = await el.get_attribute("id")
                    if eid:
                        lbl = page.locator(f"label[for='{eid}']")
                        if await lbl.count() > 0:
                            label_text = (await lbl.first.inner_text()).strip()
                    if not label_text:
                        label_text = await el.get_attribute("placeholder") or ""
                    if not label_text:
                        label_text = await el.get_attribute("aria-label") or ""
                    if not label_text:
                        # Try nearest visible ancestor label text
                        try:
                            label_text = await el.evaluate(
                                "el => { const p = el.closest('label,fieldset,div[class*=\"field\"],div[class*=\"question\"]'); "
                                "return p ? p.textContent.trim().slice(0,120) : ''; }"
                            )
                        except Exception:
                            pass

                    # Skip obvious non-question fields already handled by identity map
                    skip_keywords = ("name", "email", "phone", "linkedin", "github",
                                     "website", "salary", "start date", "company",
                                     "title", "address", "city", "state", "zip",
                                     "postal", "country", "source", "referral")
                    if any(k in label_text.lower() for k in skip_keywords):
                        continue
                    if len(label_text) < 5:
                        continue

                    result = await _answer_question(label_text, job, llm)
                    answer = result.get("answer", "") if isinstance(result, dict) else str(result)
                    answer = str(answer).strip()
                    if answer:
                        # Respect the field's maxlength so long answers aren't rejected/truncated.
                        try:
                            ml = await el.get_attribute("maxlength")
                            if ml and ml.isdigit() and len(answer) > int(ml):
                                answer = answer[: int(ml)].rstrip()
                        except Exception:
                            pass
                        await el.fill(answer)
                        await asyncio.sleep(0.2)
                        logger.info("open_question_answered", question=label_text[:80], answer=answer[:60])
                except Exception:
                    pass
        except Exception:
            pass

    # ── Rich-text / contenteditable answers (cover-letter & essay editors) ───────
    try:
        for ce in await page.locator("[contenteditable='true']:visible").all():
            try:
                if (await ce.inner_text()).strip():
                    continue
                lbl = await ce.evaluate("""el => {
                    let p=el; for(let i=0;i<8;i++){p=p.parentElement;if(!p)break;
                        const l=p.querySelector('label,legend,[class*="label"]');
                        if(l && l.innerText.trim().length>4) return l.innerText.trim();}
                    return el.getAttribute('aria-label')||'';}""") or ""
                if len(lbl) < 5:
                    continue
                res = await _answer_question(lbl, job, llm)
                ans = (res.get("answer", "") if isinstance(res, dict) else str(res)).strip()
                if ans:
                    await ce.click()
                    await ce.fill(ans) if hasattr(ce, "fill") else await page.keyboard.type(ans)
                    await asyncio.sleep(0.2)
                    logger.info("contenteditable_answered", question=lbl[:60], answer=ans[:50])
            except Exception:
                pass
    except Exception:
        pass

    # Field-type edge cases (dates, cover-letter file upload) when profile available.
    if profile is not None:
        try:
            await _fill_special_fields(page, job, profile, llm)
        except Exception:
            pass


async def _fill_special_fields(page: Page, job: JobListing, profile: dict, llm) -> None:
    """
    Field-type edge cases: native date pickers and an optional cover-letter file
    upload. (Conditional "Other, please specify" text and char-limit truncation are
    handled in _answer_open_questions; multi-select languages in the per-ATS fillers.)
    """
    defs = profile.get("application_defaults", {})
    # ── Date inputs (input[type=date]) — only fill when we can answer truthfully ─
    try:
        from datetime import datetime as _dt, timedelta as _td
        start = (_dt.now() + _td(days=14)).strftime("%Y-%m-%d")
        for el in await page.locator("input[type='date']:visible").all():
            try:
                if await el.input_value():
                    continue
                lbl = (await el.evaluate("""el => {
                    if (el.id){const l=document.querySelector('label[for="'+el.id+'"]');if(l)return l.textContent;}
                    return el.getAttribute('aria-label')||el.placeholder||'';}""") or "").lower()
                # Start/availability dates → ~2 weeks out. Skip graduation/DOB (can't guess safely).
                if any(k in lbl for k in ("start", "available", "availability", "earliest")):
                    await el.fill(start); await asyncio.sleep(0.15)
                    logger.info("date_filled", field=lbl[:40], value=start)
            except Exception:
                pass
    except Exception:
        pass

    # ── Optional cover-letter FILE upload (separate from resume) ─────────────────
    if defs.get("cover_letter") == "auto":
        try:
            cl_input = page.locator(
                "input[type='file'][name*='cover'], input[type='file'][id*='cover'], "
                "input[type='file'][aria-label*='cover' i]").first
            if await cl_input.count() > 0:
                cl_text = await _generate_cover_letter(job, profile, llm)
                if cl_text:
                    md = _SCREENSHOT_DIR.parent / f"_coverletter_{job['id']}.md"
                    md.write_text(f"# Cover Letter\n\n{cl_text}\n", encoding="utf-8")
                    try:
                        cl_pdf = markdown_to_pdf(md)
                    except Exception:
                        cl_pdf = md
                    await cl_input.set_input_files(str(cl_pdf)); await asyncio.sleep(1.5)
                    logger.info("cover_letter_uploaded", path=str(cl_pdf))
        except Exception:
            pass


async def _fill_dropdowns(page: Page, profile: dict) -> None:
    """Fill native <select> dropdowns with common known answers."""
    work_auth = profile.get("work_authorization", {})
    defs = profile.get("application_defaults", {})

    selects = page.locator("select:visible")
    count = await selects.count()
    for i in range(count):
        sel_el = selects.nth(i)
        try:
            sel_name = (await sel_el.get_attribute("name") or "").lower()
            sel_id = (await sel_el.get_attribute("id") or "").lower()
            context = sel_name + " " + sel_id

            # Work authorization
            if any(k in context for k in ("sponsor", "authorized", "workauth", "visa")):
                # Sentence-option work-auth selects: pick the truthful option (the helper
                # defers on plain Yes/No menus, so this is safe to try first).
                if any(k in context for k in ("authorized", "workauth", "visa")) and \
                        await _select_truthful_workauth_native(sel_el):
                    continue
                answer = work_auth.get("sponsorship_answer", "No")
                try:
                    await sel_el.select_option(label=answer)
                except Exception:
                    try:
                        await sel_el.select_option(value=answer.lower())
                    except Exception:
                        pass
                continue

            # Country
            if "country" in context:
                for opt in ["United States", "US", "USA"]:
                    try:
                        await sel_el.select_option(label=opt)
                        break
                    except Exception:
                        pass
                continue

            # How did you hear
            if any(k in context for k in ("source", "referral", "hear", "found")):
                for opt in ["LinkedIn", "Job Board", "Other"]:
                    try:
                        await sel_el.select_option(label=opt)
                        break
                    except Exception:
                        pass
        except Exception:
            pass

    # ── React-Select work-auth (Ashby & other React forms) ──────────────────────
    # Native <select> work-auth is handled above; React Select renders as
    # .select__control. Only act on still-empty work-auth controls (skips Greenhouse's
    # own already-filled ones), pick the truthful sentence option.
    try:
        for ctrl in await page.locator(
                ".select__control, [class*='__control'][class*='select']").all():
            try:
                if await ctrl.locator(".select__single-value, .select__multi-value").count() > 0:
                    continue
                label = (await ctrl.evaluate("""el => {
                    let p = el;
                    for (let i=0;i<12;i++){ p=p.parentElement; if(!p) break;
                        const l = p.querySelector('label,legend');
                        if (l && l.innerText.trim().length>3) return l.innerText.trim().toLowerCase(); }
                    const inp = el.querySelector('input');
                    return inp ? (inp.getAttribute('aria-label')||'').toLowerCase() : ''; }""")) or ""
                if not _is_workauth_label(label):
                    continue
                await ctrl.click()
                await asyncio.sleep(0.4)
                await _select_truthful_workauth_react((page,))
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            except Exception:
                pass
    except Exception:
        pass

    # ── Radio-group work-auth (SmartRecruiters, iCIMS, generic, etc.) ───────────
    try:
        await _answer_workauth_radios(page)
    except Exception:
        pass


async def _workday_validate_review(page, profile: dict) -> dict:
    """Validate a Workday application on its Review page BEFORE submit — an automated QA
    gate. Confirms we're genuinely at Review (Submit button, no error panel) and that the
    summary contains the expected, correct data (identity, work auth, experience,
    education, EEO, source). Returns {valid, checks:[{name, ok, detail}], at_review}.
    Each check is a hard gate: valid only if ALL pass."""
    p = profile.get("personal", {})
    defs = profile.get("application_defaults", {})
    try:
        body = await page.inner_text("body")
    except Exception:
        return {"valid": False, "at_review": False,
                "checks": [{"name": "page", "ok": False, "detail": "could not read page"}]}
    low = body.lower()
    checks: list[dict] = []

    def chk(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1) Genuinely on Review with a Submit control and no outstanding validation errors.
    try:
        has_submit = await page.locator(
            "[data-automation-id='bottom-navigation-done-button'], "
            "button:has-text('Submit')").count() > 0
    except Exception:
        has_submit = False
    chk("at_review_page", has_submit and "review" in low[:800], "Submit button on a Review page")
    chk("no_validation_errors",
        "errors found" not in low and "is required and must have a value" not in low,
        "no 'Errors Found' / required-field errors")

    # 2) Identity.
    name = (p.get("name", "") or "").strip()
    chk("name_present", bool(name) and name.lower() in low, name)
    pdig = _phone_digits(p.get("phone", ""))
    chk("phone_present", bool(pdig) and pdig[-4:] in re.sub(r"\D", "", body), "phone on file")
    chk("address_present",
        any(t and t in low for t in (str(defs.get("location_city", "mckinney")).lower(),
                                     str(defs.get("location_zip", "00000")))),
        "address city/zip")

    # 3) Work authorization — must NOT be missing/contradictory. Rahul requires sponsorship.
    #    Flag if the page asserts "without sponsorship" was answered Yes (a dangerous mistake).
    chk("work_auth_present",
        any(k in low for k in ("authorized", "sponsorship", "work permit", "visa")),
        "work-authorization question answered")
    chk("sponsorship_not_contradicted",
        not ("authorized to work without sponsorship" in low and
             "without sponsorship\nyes" in low),
        "did not answer 'authorized without sponsorship = Yes'")

    # 4) Experience + education present (the sections that were silently empty before).
    company = str(defs.get("current_company", "JPMorgan")).split()[0].lower()
    chk("work_experience_present", company in low or "experience" in low and "jpmorgan" in low,
        defs.get("current_company", ""))
    edu = (profile.get("education") or [{}])[0]
    school = str(edu.get("school", "")).split()[0].lower()
    chk("education_present", (school and school in low) or "master" in low or "degree" in low,
        edu.get("school", ""))

    # 5) Source + EEO present.
    chk("how_did_you_hear_present",
        "how did you hear" in low and "select one" not in low,
        defs.get("referral_source", "LinkedIn"))
    chk("eeo_present",
        any(k in low for k in ("asian", "male", "man", "veteran", "disability")),
        "voluntary disclosures answered")

    valid = all(c["ok"] for c in checks)
    fails = [c["name"] for c in checks if not c["ok"]]
    logger.info("workday_validate_review", valid=valid, failed=fails)
    return {"valid": valid, "at_review": has_submit, "checks": checks}


async def _review_form(page, job: "JobListing", ats: str) -> dict:
    """
    Capture every visible form field value, flag empty required fields,
    and log a structured review before submit is clicked.
    Returns a dict with 'filled', 'empty_required', 'warnings'.
    """
    filled: list[str] = []
    empty_required: list[str] = []
    warnings: list[str] = []

    try:
        # Collect all input/textarea/select values.
        # Radio/checkbox groups are evaluated by NAME: a group counts as filled if
        # ANY member is checked — otherwise the unchecked sibling of an answered
        # Yes/No group is falsely reported as an empty required field.
        field_data = await page.evaluate("""
            () => {
                const results = [];
                const seenGroups = new Set();
                document.querySelectorAll('input, textarea, select').forEach(el => {
                    if (el.type === 'hidden' || el.type === 'file') return;
                    const label = (() => {
                        if (el.id) {
                            const l = document.querySelector('label[for="' + el.id + '"]');
                            if (l) return l.textContent.trim().slice(0,60);
                        }
                        return el.placeholder || el.name || el.getAttribute('aria-label') || '';
                    })();
                    const required = el.required || el.getAttribute('aria-required') === 'true';

                    if ((el.type === 'radio' || el.type === 'checkbox') && el.name) {
                        if (seenGroups.has(el.name)) return;   // process each group once
                        seenGroups.add(el.name);
                        const members = document.getElementsByName(el.name);
                        const anyChecked = Array.from(members).some(m => m.checked);
                        const anyRequired = Array.from(members).some(
                            m => m.required || m.getAttribute('aria-required') === 'true');
                        results.push({
                            label, type: el.type, required: anyRequired,
                            value: anyChecked ? 'checked' : 'unchecked',
                        });
                        return;
                    }

                    // React Select: the committed value lives in a hidden input, while
                    // the visible combobox input stays empty. Read the control's
                    // .select__single-value so committed dropdowns aren't flagged empty.
                    const ctrl = el.closest('.select__control');
                    if (ctrl) {
                        if (ctrl.dataset._rvSeen) return;   // one row per control
                        ctrl.dataset._rvSeen = '1';
                        const sv = ctrl.querySelector('.select__single-value, .select__multi-value');
                        results.push({
                            label, type: 'react-select',
                            required: el.required || el.getAttribute('aria-required') === 'true',
                            value: sv ? sv.textContent.trim() : '',
                        });
                        return;
                    }

                    const value = (el.value || '').trim();
                    results.push({label, value, required, type: el.type});
                });
                return results;
            }
        """)

        for f in (field_data or []):
            label = f.get("label", "?")[:50]
            value = f.get("value", "")
            req   = f.get("required", False)
            ftype = f.get("type", "")

            if ftype in ("submit", "button", "reset", "image"):
                continue

            if value and value != "unchecked":
                filled.append(f"{label}: {value[:60]}")
            elif req:
                empty_required.append(label)
                warnings.append(f"REQUIRED field empty: '{label}'")

    except Exception as e:
        warnings.append(f"Form review error: {e}")

    review = {"filled": filled, "empty_required": empty_required, "warnings": warnings}
    logger.log_decision(
        action="form_review",
        outcome="warning" if empty_required else "ok",
        details={
            "ats": ats,
            "job": job.get("title", ""),
            "company": job.get("company_name", ""),
            "filled_count": len(filled),
            "empty_required": empty_required,
            "warnings": warnings,
        },
    )
    if empty_required:
        logger.warning("form_empty_required_fields",
                       fields=empty_required, ats=ats, company=job.get("company_name"))
    return review


# ── Generic Filler ─────────────────────────────────────────────────────────────

async def _fill_generic(page: Page, profile: dict, resume_pdf: Path, job: JobListing, llm) -> bool:
    """Fallback generic form filler using common field patterns."""
    p         = profile["personal"]
    defs      = profile.get("application_defaults", {})
    work_auth = profile.get("work_authorization", {})
    name_parts = p.get("name", "").split()

    # ── Standard text/email/tel fields ────────────────────────────────────
    patterns = [
        (["input[name='candidate.name']", "input[name*='full_name']", "input[name*='fullName']",
          "input[placeholder*='Full name']", "input[placeholder*='full name']"], p.get("name", "")),
        (["input[name*='first']", "input[id*='first']", "input[placeholder*='First']"], name_parts[0] if name_parts else ""),
        (["input[name*='last']", "input[id*='last']", "input[placeholder*='Last']"], " ".join(name_parts[1:]) if len(name_parts) > 1 else ""),
        (["input[name='candidate.email']", "input[name*='email']", "input[id*='email']", "input[type='email']"], p.get("email", "")),
        (["input[name='candidate.phone']", "input[name*='phone']", "input[id*='phone']", "input[type='tel']"], _phone_digits(p.get("phone", ""))),
        (["input[name*='linkedin']", "input[id*='linkedin']", "input[placeholder*='LinkedIn']",
          "input[placeholder*='Linkedln']"], p.get("linkedin_url", "")),
        (["input[name*='github']", "input[id*='github']", "input[placeholder*='GitHub']"], p.get("github_url", "")),
        # Additional common fields
        (["input[name*='salary']", "input[id*='salary']", "input[placeholder*='salary']",
          "input[placeholder*='Salary']"], defs.get("salary_display", "$150,000 – $200,000")),
        (["input[name*='start_date']", "input[id*='start']", "input[placeholder*='start date']",
          "input[placeholder*='Start Date']"], defs.get("start_date", "Immediately")),
        (["input[name*='current_company']", "input[id*='current_company']",
          "input[placeholder*='Current company']"], defs.get("current_company", "JPMorgan Chase")),
        (["input[name*='current_title']", "input[id*='current_title']",
          "input[placeholder*='Current title']"], defs.get("current_title", "Senior Software Engineer")),
        (["input[name*='source']", "input[id*='source']", "input[placeholder*='How did you hear']",
          "input[name*='referral']"], defs.get("referral_source", "LinkedIn")),
        (["textarea[name*='cover']", "textarea[id*='cover_letter']",
          "textarea[placeholder*='cover letter']"], ""),  # filled below
    ]

    for selectors, value in patterns:
        if not value:
            continue
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(value)
                    await asyncio.sleep(0.2)
                    break
            except Exception:
                pass

    # ── Cover letter ──────────────────────────────────────────────────────
    if defs.get("cover_letter") == "auto":
        cl = await _generate_cover_letter(job, profile, llm)
        if cl:
            for cl_sel in ["textarea[name*='cover']", "textarea[id*='cover_letter']",
                           "textarea[placeholder*='cover']", "textarea[placeholder*='letter']",
                           "textarea[name*='message']", "textarea[id*='message']"]:
                try:
                    el = page.locator(cl_sel).first
                    if await el.count() > 0:
                        await el.fill(cl)
                        break
                except Exception:
                    pass

    # ── Radio buttons: work authorization / yes-no questions ──────────────
    # Find all radio groups by iterating labels
    radio_inputs = page.locator("input[type='radio']")
    radio_count  = await radio_inputs.count()
    for i in range(radio_count):
        radio = radio_inputs.nth(i)
        try:
            radio_name  = await radio.get_attribute("name") or ""
            radio_label = ""
            # Get label text for context
            rid = await radio.get_attribute("id")
            if rid:
                lbl = page.locator(f"label[for='{rid}']")
                if await lbl.count() > 0:
                    radio_label = (await lbl.first.inner_text()).strip().lower()

            # Work authorization / sponsorship — choose answer based on question type
            if any(kw in radio_name.lower() or kw in radio_label
                   for kw in ("sponsor", "visa sponsorship", "require sponsorship",
                               "now or in the future")):
                # "Do you require sponsorship?" → Yes
                answer = work_auth.get("sponsorship_answer", "Yes")
                if radio_label in (answer.lower(), answer.lower()[:3]):
                    await radio.check()
                    logger.info("radio_answered", question="sponsorship_required", answer=answer)
                continue

            if any(kw in radio_name.lower() or kw in radio_label
                   for kw in ("without sponsorship", "authorized without")):
                # "Authorized WITHOUT sponsorship?" → No
                answer = work_auth.get("authorized_without_sponsorship", "No")
                if radio_label in (answer.lower(), answer.lower()[:3]):
                    await radio.check()
                    logger.info("radio_answered", question="authorized_without", answer=answer)
                continue

            if any(kw in radio_name.lower() or kw in radio_label
                   for kw in ("authorized to work", "legally authorized", "eligible to work",
                               "work in the united states", "authorization")):
                # "Are you legally authorized to work in the US?" → Yes
                answer = work_auth.get("authorized_answer", "Yes")
                if radio_label in (answer.lower(), answer.lower()[:3]):
                    await radio.check()
                    logger.info("radio_answered", question="authorized_to_work", answer=answer)
                continue

            # Generic yes/no — default Yes
            if radio_label in ("yes", "no"):
                if radio_label == "yes":
                    await radio.check()
        except Exception:
            pass

    # ── Open text questions (LLM-answered for ALL unlabeled/unknown inputs) ─
    await _answer_open_questions(page, job, llm, profile)

    # ── Resume upload ──────────────────────────────────────────────────────
    resume_selectors = [
        "input[name='candidate.cv']",
        "input[type='file'][accept*='pdf']",
        "input[type='file'][accept*='.pdf']",
        "input[type='file'][name*='resume']",
        "input[type='file'][name*='cv']",
        "input[type='file']",
    ]
    for sel in resume_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.set_input_files(str(resume_pdf))
                await asyncio.sleep(1.0)
                logger.info("resume_uploaded", ats="generic", path=str(resume_pdf))
                break
        except Exception:
            pass

    # ── Video / media uploads (e.g. intro video questions) ────────────────
    video_path = job.get("video_answer_path") or profile.get("preferences", {}).get("intro_video_path")
    if video_path and Path(video_path).exists():
        video_selectors = [
            "input[type='file'][accept*='video']",
            "input[type='file'][accept*='mp4']",
            "input[type='file'][accept*='mov']",
        ]
        for sel in video_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.set_input_files(str(video_path))
                    await asyncio.sleep(1.5)
                    logger.info("video_uploaded", path=str(video_path))
                    break
            except Exception:
                pass
    elif video_path:
        logger.warning("video_file_not_found", path=str(video_path))

    # ── Native select dropdowns ───────────────────────────────────────────
    await _fill_dropdowns(page, profile)

    return True


# ── Screenshot helper ──────────────────────────────────────────────────────────

async def _screenshot(page: Page, job_id: str) -> str | None:
    """Take a screenshot of the filled form for approval preview."""
    path = _SCREENSHOT_DIR / f"{job_id}_filled.png"
    try:
        await page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception as e:
        logger.warning("screenshot_failed", error=str(e))
        return None


# ── Confirmation capture ───────────────────────────────────────────────────────

async def _capture_confirmation(page: Page, job_id: str) -> str | None:
    """Try to capture confirmation URL and take a screenshot."""
    confirm_path = _SCREENSHOT_DIR / f"{job_id}_confirmation.png"
    try:
        await page.screenshot(path=str(confirm_path))
    except Exception:
        pass

    # Try to extract a confirmation/tracking ID from the page
    try:
        text = await page.inner_text("body")
        patterns = [
            r"application\s+(?:id|#|number)[:\s]+([A-Z0-9\-]+)",
            r"confirmation\s+(?:number|code)[:\s]+([A-Z0-9\-]+)",
            r"reference[:\s]+([A-Z0-9\-]{6,})",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return f"confirmation:{m.group(1)}"
    except Exception:
        pass

    return page.url


# ── Fill tracking ─────────────────────────────────────────────────────────────

def _build_fill_tracking(ats: str, profile: dict, job: JobListing) -> tuple[list[str], list[str]]:
    """
    Returns (filled_fields, manual_steps) describing what the automation
    will fill vs. what the user must do manually for the given ATS.
    """
    p = profile["personal"]
    work_auth = profile.get("work_authorization", {})
    filled: list[str] = []
    manual: list[str] = []

    # Fields filled on every ATS
    if p.get("name"):
        filled.append(f"Name — {p['name']}")
    if p.get("email"):
        filled.append(f"Email — {p['email']}")
    if p.get("phone"):
        filled.append(f"Phone — {p['phone']}")
    filled.append("Resume — PDF uploaded")

    if ats == "greenhouse":
        if p.get("linkedin_url"):
            filled.append(f"LinkedIn — {p['linkedin_url']}")
        if p.get("github_url"):
            filled.append(f"GitHub/Website — {p['github_url']}")
        filled.append("Custom questions — AI-answered")

    elif ats == "lever":
        if p.get("linkedin_url"):
            filled.append(f"LinkedIn — {p['linkedin_url']}")
        filled.append("Company — JPMorgan Chase")
        filled.append("Custom questions — AI-answered")

    elif ats == "workday":
        filled.append("Name, email, phone, address (multi-step)")
        filled.append("Resume — PDF uploaded to My Experience")
        filled.append("Work authorization questions answered")

    elif ats in ("ashby", "smartrecruiters", "icims", "bamboohr", "rippling"):
        if p.get("linkedin_url"):
            filled.append(f"LinkedIn — {p['linkedin_url']}")
        filled.append("Custom questions — AI-answered")

    elif ats == "generic":
        if p.get("linkedin_url"):
            filled.append(f"LinkedIn — {p['linkedin_url']}")
        if work_auth.get("requires_sponsorship"):
            answer = work_auth.get("sponsorship_answer", "No")
            filled.append(f"Work authorization (sponsorship) — answered '{answer}'")
        filled.append("Open text questions — AI-answered")

    if ats == "manual":
        manual.append(f"Navigate to: {job.get('apply_url', '')}")
        manual.append("Apply directly through the company's job portal")
        manual.append("Attach tailored resume from data/resumes/")
        return filled, manual

    # Video upload tracking
    video_path = job.get("video_answer_path") or profile.get("preferences", {}).get("intro_video_path")
    if video_path:
        video_name = Path(video_path).name
        if ats == "generic":
            # Recruitee and similar use custom video widgets, not standard file inputs
            manual.append(
                f"Upload intro video manually — click 'Upload file' in the video box and select: {video_path}"
            )
        else:
            filled.append(f"Intro video — {video_name}")

    return filled, manual


# ── Applied jobs log ───────────────────────────────────────────────────────────

def _log_applied(record: dict) -> None:
    """Upsert a submission record in data/applied_jobs.json (dedup by apply_url)."""
    _APPLIED_LOG.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    if _APPLIED_LOG.exists():
        try:
            entries = json.loads(_APPLIED_LOG.read_text())
        except Exception:
            entries = []
    key = record.get("apply_url") or record.get("job_id") or record.get("application_id")
    entries = [e for e in entries if (e.get("apply_url") or e.get("job_id") or e.get("application_id")) != key]
    entries.append(record)
    _APPLIED_LOG.write_text(json.dumps(entries, indent=2))


# ── Phase helpers ─────────────────────────────────────────────────────────────

async def _phase_fill_and_screenshot(
    job: JobListing,
    profile: dict,
    resume_pdf: Path,
    app_id: str,
    ats: str,
    llm,
    headless: bool,
) -> str | None:
    """
    Phase 1: Open browser, fill the form, take a screenshot, close browser.
    Returns screenshot path or None on failure.
    Browser is closed before we wait for human approval so we don't hold
    an idle browser open for potentially hours.
    """
    async with BrowserClient(headless=headless) as browser:
        try:
            page = await browser.new_page(job["apply_url"])
            await asyncio.sleep(2.0)

            filler = {
                "greenhouse":      _fill_greenhouse,
                "lever":           _fill_lever,
                "workday":         _fill_workday,
                "ashby":           _fill_ashby,
                "smartrecruiters": _fill_smartrecruiters,
                "icims":           _fill_icims,
                "bamboohr":        _fill_bamboohr,
                "rippling":        _fill_rippling,
                "generic":         _fill_generic,
            }.get(ats, _fill_generic)

            # Cap form-filling at 3 minutes to prevent infinite hangs
            await asyncio.wait_for(filler(page, profile, resume_pdf, job, llm), timeout=180)
            await asyncio.sleep(1.0)

            return await _screenshot(page, app_id)

        except asyncio.TimeoutError:
            logger.warning("form_fill_timeout", url=job["apply_url"], note="Form fill exceeded 3 min — taking screenshot anyway")
            try:
                return await _screenshot(page, app_id)
            except Exception:
                return None
        except PlaywrightTimeout as e:
            logger.error("form_load_timeout", url=job["apply_url"], error=str(e))
            return None
        except Exception as e:
            logger.error("form_fill_error", error=str(e))
            return None


def _fetch_workday_verify_link(not_before: float | None = None) -> str | None:
    """Fetch a Workday account email-verification LINK from the applicant inbox.
    Returns the verify URL or None. Workday requires email verification before the
    application form proceeds."""
    try:
        import httpx, base64, re as _re
        from core.config import get_settings
        cfg = get_settings()
        if not cfg.gmail_refresh_token:
            return None
        tok = httpx.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "refresh_token", "refresh_token": cfg.gmail_refresh_token,
            "client_id": cfg.gmail_client_id, "client_secret": cfg.gmail_client_secret}, timeout=10)
        at = tok.json().get("access_token")
        if not at:
            return None
        H = {"Authorization": f"Bearer {at}"}
        q = "newer_than:1h in:anywhere (workday OR \"verify your email\" OR \"confirm your email\" OR \"account\")"
        sr = httpx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages",
                       params={"q": q, "maxResults": 10}, headers=H, timeout=10)
        for msg in sr.json().get("messages", []):
            md = httpx.get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}",
                           params={"format": "full"}, headers=H, timeout=10).json()
            if not_before is not None and int(md.get("internalDate", 0)) / 1000 < not_before:
                continue
            body = ""
            pl = md.get("payload", {})
            for part in (pl.get("parts") or [pl]):
                data = (part.get("body") or {}).get("data", "")
                if data:
                    try: body += base64.urlsafe_b64decode(data + "==").decode("utf-8", "ignore")
                    except Exception: pass
                for sub in (part.get("parts") or []):
                    d2 = (sub.get("body") or {}).get("data", "")
                    if d2:
                        try: body += base64.urlsafe_b64decode(d2 + "==").decode("utf-8", "ignore")
                        except Exception: pass
            # Verify links point at the Workday tenant with a confirmation/register token.
            m = _re.search(r'https://[^"\s>]*myworkdayjobs[^"\s>]*(?:verif|confirm|register|activate)[^"\s>]*', body, _re.I) \
                or _re.search(r'https://[^"\s>]*(?:verif|confirm|activate)[^"\s>]*', body, _re.I)
            if m:
                return m.group(0).replace("&amp;", "&")
    except Exception:
        pass
    return None


def gmail_token_healthy() -> bool:
    """Quick check that the Gmail OAuth refresh token still works (for verification
    code fetch). Returns False if missing/expired — caller should prompt re-auth."""
    try:
        import httpx
        from core.config import get_settings
        cfg = get_settings()
        if not cfg.gmail_refresh_token:
            return False
        r = httpx.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "refresh_token", "refresh_token": cfg.gmail_refresh_token,
            "client_id": cfg.gmail_client_id, "client_secret": cfg.gmail_client_secret,
        }, timeout=10)
        return r.status_code == 200 and bool(r.json().get("access_token"))
    except Exception:
        return False


async def _fetch_greenhouse_verification_code(not_before: float | None = None) -> str | None:
    """
    Poll the applicant Gmail account for a Greenhouse 8-char security code.
    Uses OAuth2 refresh token from Settings. Returns code string or None.
    not_before: Unix timestamp; skip emails received before this time (avoids reusing codes from prior apps).
    """
    try:
        import httpx, base64, re as _re
        from core.config import get_settings
        cfg = get_settings()
        if not cfg.gmail_refresh_token:
            return None

        # Exchange refresh token for access token
        token_resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type":    "refresh_token",
                "refresh_token": cfg.gmail_refresh_token,
                "client_id":     cfg.gmail_client_id,
                "client_secret": cfg.gmail_client_secret,
            },
            timeout=10,
        )
        if token_resp.status_code != 200:
            return None
        access_token = token_resp.json().get("access_token")
        if not access_token:
            return None

        headers = {"Authorization": f"Bearer {access_token}"}
        search_resp = httpx.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            params={"q": "from:greenhouse-mail.io newer_than:20m in:anywhere", "maxResults": 10},
            headers=headers,
            timeout=10,
        )
        if search_resp.status_code != 200:
            logger.warning("gmail_search_failed", status=search_resp.status_code, body=search_resp.text[:200])
            return None

        messages = search_resp.json().get("messages", [])
        logger.info("gmail_search_result", count=len(messages), not_before=not_before)

        for msg in messages:
            msg_resp = httpx.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}",
                params={"format": "full"},
                headers=headers,
                timeout=10,
            )
            if msg_resp.status_code != 200:
                continue

            msg_data = msg_resp.json()
            internal_date_ms = int(msg_data.get("internalDate", 0))
            # Skip emails received before the form was submitted (avoids stale codes from prior apps)
            if not_before is not None:
                if internal_date_ms / 1000 < not_before:
                    logger.info("gmail_msg_skipped", msg_id=msg['id'],
                                internal_date=internal_date_ms/1000, not_before=not_before,
                                age_s=not_before - internal_date_ms/1000)
                    continue

            # Decode body (walk payload parts)
            payload = msg_data.get("payload", {})
            body_text = ""
            parts = payload.get("parts") or [payload]
            for part in parts:
                data = (part.get("body") or {}).get("data", "")
                if data:
                    try:
                        body_text += base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
                    except Exception:
                        pass
                for sub in (part.get("parts") or []):
                    data2 = (sub.get("body") or {}).get("data", "")
                    if data2:
                        try:
                            body_text += base64.urlsafe_b64decode(data2 + "==").decode("utf-8", errors="ignore")
                        except Exception:
                            pass

            # Greenhouse puts the verification code in an <h1> tag, e.g. <h1>xTvcerWO</h1>
            # Fallback: look near "paste" or "code" keyword to avoid matching CSS words like "overflow"
            m = (_re.search(r'<h1>([A-Za-z0-9]{8,10})</h1>', body_text)
                 or _re.search(r'(?:paste|security code|your code)[^A-Za-z0-9]{1,80}([A-Za-z0-9]{8})\b', body_text, _re.IGNORECASE))
            logger.info("gmail_msg_checked", msg_id=msg['id'], internal_date=internal_date_ms/1000,
                        body_len=len(body_text), code_found=bool(m),
                        body_snippet=body_text[:300].replace("\n", " "))
            if m:
                return m.group(1)
    except Exception as _ex:
        logger.warning("gmail_fetch_exception", error=str(_ex))
    return None


async def _detect_captcha(page, ctx=None) -> bool:
    """True if the form is protected by reCAPTCHA / hCaptcha / Turnstile. Used by the
    auto-submit lane to AVOID submitting through a CAPTCHA (those get queued for the
    human's one-tap extension flow instead)."""
    targets = [x for x in (ctx, page) if x is not None]
    for c in targets:
        try:
            for fr in (getattr(c, "frames", []) or []):
                u = (getattr(fr, "url", "") or "").lower()
                if any(k in u for k in ("recaptcha", "hcaptcha", "/captcha", "turnstile")):
                    return True
        except Exception:
            pass
        try:
            n = await c.locator(
                ".g-recaptcha, .h-captcha, .cf-turnstile, #recaptcha, "
                "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[title*='recaptcha'], "
                "iframe[title*='captcha'], [data-sitekey]").count()
            if n > 0:
                return True
        except Exception:
            pass
    return False


async def _resume_is_attached(page) -> bool:
    """True if the form needs no résumé OR a résumé is actually attached. Used by the
    auto-submit lane to refuse sending a résumé-less application when headless file-attach
    silently failed. Checks EVERY frame (embedded ATS forms live in iframes):
      • no file input anywhere → form doesn't require an upload → OK to proceed.
      • a file input exists and ANY of them reports files.length>0 → attached → OK.
      • file input(s) exist but none holds a file → NOT attached → block.
    Fails OPEN (returns True) on any error so a detection glitch never wrongly blocks."""
    try:
        any_input = False
        for fr in [page, *(getattr(page, "frames", []) or [])]:
            try:
                inputs = fr.locator("input[type='file']")
                cnt = await inputs.count()
            except Exception:
                continue
            for i in range(cnt):
                any_input = True
                try:
                    files = await inputs.nth(i).evaluate("n => (n.files && n.files.length) || 0")
                    if files:
                        return True
                except Exception:
                    pass
        # If there were file inputs but none had a file, the résumé didn't attach.
        return not any_input
    except Exception:
        return True


async def _phase_submit(
    job: JobListing,
    profile: dict,
    resume_pdf: Path,
    app_id: str,
    ats: str,
    llm,
    headless: bool,
    abort_on_captcha: bool = False,
) -> tuple[bool, str | None]:
    """
    Phase 2: Reopen browser, refill the form, click submit.
    Returns (submitted: bool, confirmation_url: str | None).
    If abort_on_captcha and the form has a CAPTCHA, returns (False, "captcha_present")
    WITHOUT submitting — the caller queues it for the human instead.
    """
    async with BrowserClient(headless=headless) as browser:
        try:
            page = await browser.new_page(job["apply_url"])
            await asyncio.sleep(2.0)

            filler = {
                "greenhouse":      _fill_greenhouse,
                "lever":           _fill_lever,
                "workday":         _fill_workday,
                "ashby":           _fill_ashby,
                "smartrecruiters": _fill_smartrecruiters,
                "icims":           _fill_icims,
                "bamboohr":        _fill_bamboohr,
                "rippling":        _fill_rippling,
                "generic":         _fill_generic,
            }.get(ats, _fill_generic)

            await asyncio.wait_for(filler(page, profile, resume_pdf, job, llm), timeout=180)
            await asyncio.sleep(0.5)

            # Determine which context to use for submit: iframe (embedded forms) or main page
            submit_ctx = page
            for frame in page.frames:
                if "greenhouse.io/embed" in frame.url:
                    submit_ctx = frame  # type: ignore[assignment]
                    break

            submit_selectors = [
                "button[type='submit']",
                "input[type='submit']",
                "button:has-text('Submit Application')",
                "button:has-text('Submit')",
                "button:has-text('Apply Now')",
                "button:has-text('Apply')",
                "button:has-text('Send Application')",
                "button:has-text('Complete Application')",
                "[data-automation-id='bottom-navigation-next-button']",  # Workday Review step
                "[data-automation-id='bottom-navigation-done-button']",   # Workday final
                "[data-qa='btn-submit']",                                  # Lever
                "[data-test='btn-send-application']",                      # SmartRecruiters
                "[data-testid='submit-application-button']",               # Ashby
                "[data-testid='apply-button']",                            # Ashby alt
            ]

            # ── Auto-submit lane: never push through a CAPTCHA — queue for the human ──
            # MUST run BEFORE any solve attempt: clicking the reCAPTCHA checkbox can collapse the
            # widget so _detect_captcha no longer matches, letting an unattended submit slip through
            # a CAPTCHA (the exact thing this lane forbids). So abort first, and NEVER solve here.
            if abort_on_captcha:
                if await _detect_captcha(page, submit_ctx):
                    logger.info("auto_submit_captcha_queued", company=job.get("company_name"),
                                url=job.get("apply_url"))
                    return False, "captcha_present"
            else:
                # Human/assisted lane only: try the reCAPTCHA checkbox (SoFi + some Greenhouse forms).
                # reCAPTCHA renders in a sub-iframe within the Greenhouse embed frame. The user is
                # present to handle any challenge; this lane never auto-submits unattended.
                try:
                    rc_frames = submit_ctx.child_frames if hasattr(submit_ctx, "child_frames") else []
                    for rc_frame in rc_frames:
                        if "recaptcha" in rc_frame.url.lower() or "google.com/recaptcha" in rc_frame.url.lower():
                            rc_cb = rc_frame.locator("#recaptcha-anchor, .recaptcha-checkbox-checkmark, [aria-label*='robot']").first
                            if await rc_cb.count() > 0:
                                await rc_cb.click()
                                await asyncio.sleep(3.0)  # wait for reCAPTCHA to verify
                                break
                except Exception:
                    pass

            # ── Auto-submit lane: never send a résumé-less application ──
            # Headless file-attach is unreliable on some ATS (embedded Greenhouse). If the
            # form HAS a file input but nothing is attached, abort and defer to the extension
            # rather than submit without the résumé. (Human/assisted lane is unaffected — the
            # user attaches it themselves there.)
            if abort_on_captcha and not await _resume_is_attached(page):
                logger.warning("auto_submit_resume_missing", company=job.get("company_name"),
                               url=job.get("apply_url"))
                return False, "resume_not_attached"

            # ── Pre-submit form review ────────────────────────────────────────
            review = await _review_form(submit_ctx, job, ats)
            if review["empty_required"]:
                logger.warning("pre_submit_empty_fields",
                               fields=review["empty_required"], ats=ats,
                               company=job.get("company_name"))

            url_before = page.url
            url_before_ctx = submit_ctx.url
            for sel in submit_selectors:
                try:
                    btn = submit_ctx.locator(sel).first
                    if await btn.count() > 0 and await btn.is_enabled():
                        await btn.click()
                        await asyncio.sleep(7.0)   # Greenhouse/Lever need ~5-7s to process
                        url_after = page.url
                        url_after_ctx = submit_ctx.url
                        confirmation_url = await _capture_confirmation(page, app_id)

                        # For embedded iframes (e.g. SoFi), read text from the iframe context;
                        # the outer page URL won't change even on successful submission.
                        page_text = ""
                        try:
                            page_text = (await submit_ctx.inner_text("body"))[:500].lower()
                        except Exception:
                            try:
                                page_text = (await page.inner_text("body"))[:500].lower()
                            except Exception:
                                pass

                        # Full body text for verification/success detection
                        try:
                            page_text_full = (await submit_ctx.inner_text("body")).lower()
                        except Exception:
                            page_text_full = page_text
                        # Also grab outer page text in case submission landed there
                        if not page_text_full:
                            try:
                                page_text_full = (await page.inner_text("body")).lower()
                            except Exception:
                                pass

                        # Email verification step: Greenhouse sends a code to the
                        # applicant's email before finalising — treat as success
                        # (application is lodged; user just needs to click the link)
                        email_verify_page = (
                            "verification code was sent" in page_text_full
                            or "notification code was sent" in page_text_full
                            or "security code" in page_text_full
                            or "verify your email" in page_text_full
                            or "check your email" in page_text_full
                            or "confirm you're a human" in page_text_full
                            or "enter the 8-character code" in page_text_full
                            or "enter the 8 character code" in page_text_full
                        )
                        if email_verify_page:
                            logger.warning(
                                "email_verification_required",
                                url=url_after,
                                note="Greenhouse sent a verification code to the applicant Gmail account — attempting auto-fetch.",
                            )
                            # Attempt to auto-fetch the code from Gmail and enter it
                            # Subtract 30s buffer: email arrives ~0-5s after submit click,
                            # but _submit_ts is set ~3-5s later after page navigation.
                            import time as _time
                            _submit_ts = _time.time() - 30
                            code = None
                            # Exponential backoff: 3, 5, 8, 13, 21, 34s … up to 90s total
                            _backoff = [3, 5, 8, 13, 21, 34, 55]
                            for _attempt, _delay in enumerate(_backoff):
                                await asyncio.sleep(_delay)
                                code = await _fetch_greenhouse_verification_code(not_before=_submit_ts)
                                if code:
                                    break
                            if code:
                                try:
                                    # Try specific code-field selectors first, then fall back to
                                    # any visible text input on the verification page
                                    code_input = page.locator(
                                        "input[name*='code'], input[id*='code'], "
                                        "input[placeholder*='code'], input[placeholder*='Code'], "
                                        "input[name*='security'], input[id*='security'], "
                                        "input[name*='verify'], input[id*='verify'], "
                                        "input[name*='confirm'], input[id*='confirm'], "
                                        "input[type='text']:visible"
                                    ).first
                                    cnt = await code_input.count()
                                    logger.info("greenhouse_code_input_found", count=cnt)
                                    if cnt > 0:
                                        await code_input.fill(code)
                                        await asyncio.sleep(0.4)
                                        # Re-click submit — try original selectors then broader button search
                                        submitted_code = False
                                        for sub_sel in submit_selectors + ["button:visible", "input[type='button']:visible"]:
                                            sub_btn = submit_ctx.locator(sub_sel).first
                                            if await sub_btn.count() > 0 and await sub_btn.is_enabled():
                                                await sub_btn.click()
                                                await asyncio.sleep(3.0)
                                                submitted_code = True
                                                break
                                        confirmation_url = await _capture_confirmation(page, app_id)
                                        logger.info("greenhouse_code_entered", code=code, resubmit=submitted_code)
                                    else:
                                        logger.warning("greenhouse_code_input_not_found",
                                                       note="Could not locate code input field on verification page")
                                except Exception as _ce:
                                    logger.warning("greenhouse_code_entry_failed", error=str(_ce))
                            else:
                                logger.warning("greenhouse_code_not_found",
                                               note="Code not in Gmail within 30s — marking pending_manual_submit. "
                                                    "Check the applicant Gmail account for the 8-char code and re-run, "
                                                    "or set GMAIL_REFRESH_TOKEN/CLIENT_ID/CLIENT_SECRET in .env.")
                                # Code not entered → application NOT actually submitted
                                return False, "email_verification_pending"
                            return True, confirmation_url

                        success_signals = (
                            url_after != url_before
                            or url_after_ctx != url_before_ctx
                            or "thank you" in page_text_full
                            or "thanks for applying" in page_text_full
                            or "application received" in page_text_full
                            or "successfully submitted" in page_text_full
                            or "application submitted" in page_text_full
                            or "we received your" in page_text_full
                            or "we've received your" in page_text_full
                            or "we have received your" in page_text_full
                            or "your application is" in page_text_full
                            or "your application has been" in page_text_full
                            or "application is complete" in page_text_full
                            or "you have applied" in page_text_full
                            or "you've applied" in page_text_full
                            or "application complete" in page_text_full
                            or "good luck" in page_text_full
                            or "we'll be in touch" in page_text_full
                            or "we will be in touch" in page_text_full
                            or "hear from us" in page_text_full
                            or "confirm" in url_after.lower()
                            or "confirm" in url_after_ctx.lower()
                            or "success" in url_after.lower()
                            or "applied" in url_after.lower()
                        )
                        if success_signals:
                            logger.info("submission_confirmed", url_before=url_before, url_after=url_after)
                            return True, confirmation_url
                        else:
                            # Debug: screenshot + first 500 chars of page text
                            debug_path = _SCREENSHOT_DIR / f"{app_id}_submit_debug.png"
                            try:
                                await page.screenshot(path=str(debug_path), full_page=True)
                            except Exception:
                                pass
                            # Two very different "no nav" cases:
                            #  (a) the page shows validation errors → the submit did NOT go
                            #      through → genuine failure (needs the extension/manual).
                            #  (b) no errors, no success text → AMBIGUOUS. We clicked submit
                            #      with a fully-filled form; the ATS may have accepted it on a
                            #      page our text-heuristic doesn't recognize. Don't guess from
                            #      the page — defer the verdict to the confirmation email
                            #      ("clicked_unconfirmed"). The email monitor confirms it, and
                            #      the reconcile sweep re-queues it if no email ever arrives.
                            _VALIDATION_HINTS = (
                                "is required", "required field", "field is required",
                                "please enter", "please fill", "please complete",
                                "please select", "please provide", "cannot be blank",
                                "this field", "must be", "invalid ", "is not valid",
                                "are required",
                            )
                            had_errors = any(h in page_text_full for h in _VALIDATION_HINTS)
                            if had_errors:
                                logger.warning("submit_validation_error", url=url_after,
                                               page_snippet=page_text_full[:2000],
                                               note="Form shows validation errors — not submitted")
                                return False, "validation_error"
                            logger.info("submit_clicked_unconfirmed", url=url_after,
                                        note="Submit clicked, no success text — deferring verdict "
                                             "to the confirmation email")
                            return False, "clicked_unconfirmed"
                except Exception:
                    pass

            logger.warning("submit_button_not_found", ats=ats, url=job["apply_url"])
            return False, "no_submit_button"

        except Exception as e:
            logger.error("phase_submit_error", error=str(e))
            return False, "submit_error"

_ASSISTED_SUCCESS_PHRASES = (
    "thanks for applying", "thank you for applying", "application received",
    "successfully submitted", "application submitted", "we received your application",
    "we've received your application", "we have received your application",
    "your application has been received", "your application has been submitted",
    "you have applied", "you've applied", "application complete",
)


async def _phase_assisted_submit(
    job: JobListing,
    profile: dict,
    resume_pdf: Path,
    app_id: str,
    ats: str,
    llm,
) -> tuple[bool, str | None]:
    """Assisted submit: open a VISIBLE browser, pre-fill the form, then hand control
    to the user to review/correct fields, solve any CAPTCHA, and click Submit. Polls
    for a real success signal and captures a confirmation screenshot. Reliable on
    reCAPTCHA-protected ATS where headless auto-submit can't complete.
    Returns (submitted, confirmation_url)."""
    async with BrowserClient(headless=False) as browser:
        try:
            page = await browser.new_page(job["apply_url"])
            await asyncio.sleep(2.0)

            filler = {
                "greenhouse": _fill_greenhouse, "lever": _fill_lever, "workday": _fill_workday,
                "ashby": _fill_ashby, "smartrecruiters": _fill_smartrecruiters, "icims": _fill_icims,
                "bamboohr": _fill_bamboohr, "rippling": _fill_rippling, "generic": _fill_generic,
            }.get(ats, _fill_generic)
            try:
                await asyncio.wait_for(filler(page, profile, resume_pdf, job, llm), timeout=180)
            except Exception as e:
                logger.warning("assisted_prefill_error", error=str(e)[:120])
            await asyncio.sleep(0.5)

            submit_ctx = page
            for frame in page.frames:
                if "greenhouse.io/embed" in frame.url:
                    submit_ctx = frame  # type: ignore[assignment]
                    break

            url_before = page.url
            try:
                url_before_ctx = submit_ctx.url
            except Exception:
                url_before_ctx = url_before

            logger.info("assisted_submit_ready", company=job.get("company_name"),
                        note="visible browser open — awaiting user submit (up to 5 min)")
            print(f"\n🖐️  ACTION NEEDED — {job.get('company_name')} · {job.get('title')}")
            print(f"    A browser window opened with the form pre-filled.")
            print(f"    1) Review the fields and FIX anything wrong (name/country/work-auth).")
            print(f"    2) Solve the CAPTCHA if present.  3) Click SUBMIT.")
            print(f"    Waiting up to 5 minutes for you to submit…\n")

            import time as _t
            deadline = _t.time() + 300
            while _t.time() < deadline:
                await asyncio.sleep(3.0)
                try:
                    url_after = page.url
                    try:
                        url_after_ctx = submit_ctx.url
                    except Exception:
                        url_after_ctx = url_after
                    txt = ""
                    try:
                        txt = (await submit_ctx.inner_text("body")).lower()
                    except Exception:
                        try:
                            txt = (await page.inner_text("body")).lower()
                        except Exception:
                            txt = ""
                    if (url_after != url_before or url_after_ctx != url_before_ctx
                            or any(p in txt for p in _ASSISTED_SUCCESS_PHRASES)):
                        conf = await _capture_confirmation(page, app_id)
                        logger.info("assisted_submit_confirmed",
                                    company=job.get("company_name"), url=url_after)
                        print(f"✓  Submission detected for {job.get('company_name')} — confirmation captured.\n")
                        await asyncio.sleep(1.0)
                        return True, conf
                except Exception as e:
                    logger.info("assisted_submit_window_closed", note=str(e)[:80])
                    break

            logger.warning("assisted_submit_timeout", company=job.get("company_name"))
            print(f"⏱  No submission detected for {job.get('company_name')} — left in the queue to retry.\n")
            return False, None

        except Exception as e:
            logger.error("assisted_submit_error", error=str(e)[:160])
            return False, None


# ── Main submission logic ──────────────────────────────────────────────────────

async def submit_application(
    app: Application,
    job: JobListing,
    profile: dict,
    llm,
    headless: bool = True,
    assisted: bool = False,
    auto_safe: bool = False,
) -> Application:
    """
    Full submission flow for one application:
      Phase 1 — Fill form + screenshot (browser closed after)
      HUMAN GATE — await approval (browser closed, no idle resources held)
      Phase 2 — If approved: reopen, refill, submit, capture confirmation
    """
    updated = dict(app)
    job = dict(job)  # local copy so we can patch apply_url without mutating caller's data
    apply_url = job["apply_url"]

    # Normalize redirector URLs to direct Greenhouse board URLs
    # sofi.com/careers/job/{id}?gh_jid={gh_jid}  →  job-boards.greenhouse.io/sofi/jobs/{gh_jid}
    import re as _re
    # sofi.com career pages embed the Greenhouse form in an iframe; no URL change needed —
    # the iframe is detected inside _fill_greenhouse and used as the form context.

    # Email-only application — no browser, surface as manual step
    if apply_url.startswith("mailto:"):
        email_addr = apply_url[len("mailto:"):]
        resume_md = Path(app["tailored_resume_path"])
        resume_preview = ""
        try:
            full_text = resume_md.read_text(encoding="utf-8")
            resume_preview = full_text.split("<!-- TAILOR_META")[0].strip()
        except Exception:
            pass

        approval_payload = {
            "company":                 job["company_name"],
            "role":                    job["title"],
            "apply_url":               apply_url,
            "ats":                     "email",
            "quality_score":           app.get("quality_score", 0),
            "tailored_resume_preview": resume_preview,
            "tailored_resume_path":    str(resume_md),
            "screenshot_path":         "",
            "resume_pdf_path":         "",
            "filled_fields":           [],
            "manual_steps": [
                f"Send email to: {email_addr}",
                "Subject: Application — " + job["title"] + " @ " + job["company_name"],
                "Attach: resume PDF from data/resumes/",
                "Body: brief intro + why you're a fit",
            ],
        }
        # Auto-approved — no human gate
        logger.log_decision(
            action="email_application_auto_approved",
            outcome="approved",
            details={"company": job["company_name"], "email": email_addr},
        )
        # Mark as pending_manual_submit — user will send email manually
        updated["status"] = "pending_manual_submit"
        updated["rejection_reason"] = f"Email application — send manually to {email_addr}"
        _log_applied({
            "application_id": app["id"],
            "job_id": job["id"],
            "company": job["company_name"],
            "role": job["title"],
            "ats": "email",
            "apply_url": apply_url,
            "quality_score": app.get("quality_score", 0),
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        })
        return updated

    ats = detect_ats(apply_url)
    logger.info("form_fill_start", title=job["title"], company=job["company_name"], ats=ats)

    # HN / manual-only jobs — no ATS form; surface as a manual task with the URL
    if ats == "manual":
        resume_md = Path(app["tailored_resume_path"])
        resume_preview = ""
        try:
            full_text = resume_md.read_text(encoding="utf-8")
            resume_preview = full_text.split("<!-- TAILOR_META")[0].strip()
        except Exception:
            pass
        approval_payload = {
            "company":                 job["company_name"],
            "role":                    job["title"],
            "apply_url":               apply_url,
            "ats":                     "manual",
            "quality_score":           app.get("quality_score", 0),
            "tailored_resume_preview": resume_preview,
            "tailored_resume_path":    str(resume_md),
            "screenshot_path":         "",
            "resume_pdf_path":         "",
            "filled_fields":           [],
            "manual_steps": [
                f"Open the link to find the real apply URL: {apply_url}",
                f"Apply for: {job['title']} @ {job['company_name']}",
                "Attach your tailored resume from data/resumes/",
            ],
        }
        # Auto-approved — no human gate
        updated["status"] = "pending_manual_submit"
        updated["rejection_reason"] = f"Manual application — visit {apply_url}"
        _log_applied({
            "application_id": app["id"],
            "job_id":         job["id"],
            "company":        job["company_name"],
            "role":           job["title"],
            "ats":            "manual",
            "apply_url":      apply_url,
            "quality_score":  app.get("quality_score", 0),
            "submitted_at":   datetime.now(timezone.utc).isoformat(),
        })
        return updated

    # Convert resume markdown → PDF
    resume_md = Path(app["tailored_resume_path"])
    try:
        resume_pdf = markdown_to_pdf(resume_md)
    except Exception as e:
        logger.error("pdf_conversion_failed", error=str(e))
        resume_pdf = resume_md  # fall back to md

    from core.checkpoints import (
        _auto_approve_enabled, approval_status_for_apply_url, request_approval,
    )

    # ── Phase 1: fill and screenshot ──────────────────────────────────────
    # SKIPPED for an already-approved assisted submit: the form was filled +
    # screenshotted when it was parked, so we skip the slow headless refill and open
    # the VISIBLE browser right away (otherwise the user waits ~2 min staring at
    # nothing while a hidden browser re-fills first).
    _skip_phase1 = assisted and (
        _auto_approve_enabled() or approval_status_for_apply_url(job["apply_url"]) == "approved")
    if _skip_phase1:
        screenshot_path = app.get("screenshot_path") or str(
            _SCREENSHOT_DIR / f"{app['id']}_filled.png")
    else:
        screenshot_path = await _phase_fill_and_screenshot(
            job, profile, resume_pdf, app["id"], ats, llm, headless
        )
        if screenshot_path is None:
            updated["status"] = "blocked"
            updated["rejection_reason"] = "Form load or fill failed"
            return updated

    # ── HUMAN GATE: park after fill, submit only once approved ───────────
    # Default = review in the UI parking lot before anything is sent. The user
    # approves in /approvals (which resolves the checkpoint gate), then a submit
    # run (`runner --submit-only`) reaches this code again and proceeds to Phase 2.
    # Set AUTONOMOUSHIRE_AUTO_APPROVE=1 to skip the gate and submit immediately.
    filled_fields, manual_steps = _build_fill_tracking(ats, profile, job)
    updated["screenshot_path"] = screenshot_path
    updated["filled_fields"] = filled_fields

    # auto_safe = the auto-apply lane already gated this app; proceed to submit (but the
    # submit aborts on CAPTCHA and queues it). AUTO_APPROVE = force-submit everything.
    auto = _auto_approve_enabled() or auto_safe
    gate_status = approval_status_for_apply_url(job["apply_url"])

    if not auto and gate_status != "approved":
        if gate_status == "rejected":
            updated["status"] = "rejected"
            updated["rejection_reason"] = "Rejected during review"
            logger.log_decision(action="application_rejected", outcome="rejected",
                                details={"company": job["company_name"], "role": job["title"]})
            return updated
        # Park for review — create a pending gate only if one doesn't exist yet
        # (re-runs over an already-parked app must not pile up duplicate gates).
        if gate_status is None:
            try:
                resume_preview = Path(app["tailored_resume_path"]).read_text(
                    encoding="utf-8").split("<!-- TAILOR_META")[0].strip()
            except Exception:
                resume_preview = ""
            request_approval("submit_application", {
                "company":                 job["company_name"],
                "role":                    job["title"],
                "apply_url":               job["apply_url"],
                "ats":                     ats,
                "quality_score":           app.get("quality_score", 0),
                "tailored_resume_preview": resume_preview,
                "tailored_resume_path":    str(app.get("tailored_resume_path", "")),
                "screenshot_path":         screenshot_path,
                "resume_pdf_path":         str(resume_pdf),
                "filled_fields":           filled_fields,
                "manual_steps":            manual_steps,
            })
        updated["status"] = "pending_review"
        logger.log_decision(action="parked_for_approval", outcome="pending",
                            details={"company": job["company_name"], "role": job["title"],
                                     "screenshot": screenshot_path})
        return updated

    # Approved (or AUTO_APPROVE): proceed to submit.
    logger.log_decision(
        action="approved",
        outcome="approved",
        details={"company": job["company_name"], "role": job["title"],
                 "screenshot": screenshot_path, "auto": auto},
    )

    if assisted:
        # Visible browser: pre-fill, then the user reviews, solves CAPTCHA, and submits.
        submitted, confirmation_url = await _phase_assisted_submit(
            job, profile, resume_pdf, app["id"], ats, llm
        )
    else:
        submitted, confirmation_url = await _phase_submit(
            job, profile, resume_pdf, app["id"], ats, llm, headless,
            abort_on_captcha=auto_safe,   # auto lane never pushes through a CAPTCHA
        )

    # Auto lane hit a CAPTCHA, or the résumé failed to attach headlessly → queue for the
    # user's one-tap extension flow (don't submit). The extension fills + attaches the
    # résumé in the user's real browser, where these both work.
    if confirmation_url in ("captcha_present", "resume_not_attached"):
        _queue_reason = ("CAPTCHA detected — queued for your 1-tap submit"
                         if confirmation_url == "captcha_present"
                         else "Résumé didn't attach headlessly — queued for your 1-tap submit")
        if approval_status_for_apply_url(job["apply_url"]) is None:
            try:
                resume_preview = Path(app["tailored_resume_path"]).read_text(
                    encoding="utf-8").split("<!-- TAILOR_META")[0].strip()
            except Exception:
                resume_preview = ""
            request_approval("submit_application", {
                "company": job["company_name"], "role": job["title"],
                "apply_url": job["apply_url"], "ats": ats,
                "quality_score": app.get("quality_score", 0),
                "tailored_resume_preview": resume_preview,
                "tailored_resume_path": str(app.get("tailored_resume_path", "")),
                "screenshot_path": screenshot_path, "resume_pdf_path": str(resume_pdf),
                "filled_fields": filled_fields, "manual_steps": manual_steps,
            })
        updated["status"] = "pending_review"
        updated["rejection_reason"] = _queue_reason
        logger.log_decision(action="auto_submit_queued", outcome="queued",
                            details={"company": job["company_name"], "role": job["title"],
                                     "cause": confirmation_url})
        return updated

    # Distinguish "code needed" from generic submit failure
    if not submitted and confirmation_url == "email_verification_pending":
        updated["rejection_reason"] = (
            "Greenhouse email verification code required — "
            "set GMAIL_REFRESH_TOKEN/CLIENT_ID/CLIENT_SECRET in .env and re-run"
        )

    # "applied" is decided by the Gmail confirmation, never by our page heuristic.
    #   • page_confirmed → strong evidence the form went through (success page/text).
    #   • click_only     → we clicked submit on a filled, error-free form but couldn't
    #                      see a success page. Could be a real submit we just didn't
    #                      recognize, or a silent no-op. The email is the tie-breaker;
    #                      if none arrives, the reconcile sweep re-queues it (page-
    #                      confirmed ones are NOT re-queued — we have evidence).
    page_confirmed = submitted
    click_only = (not submitted) and confirmation_url == "clicked_unconfirmed"
    awaiting = page_confirmed or click_only

    if awaiting:
        now = datetime.now(timezone.utc).isoformat()
        # Form went out, but we don't call it "submitted" until the employer/ATS
        # confirmation email lands (email_monitor flips it). Interim = "awaiting_confirmation".
        # `sent_at` records when the form went out; the applied-log entry (with submitted_at)
        # still guards dedup + the daily cap.
        updated["status"] = "awaiting_confirmation"
        updated["sent_at"] = now
        updated["submit_confidence"] = "page" if page_confirmed else "click_only"
        updated["confirmation_url"] = confirmation_url if page_confirmed else None
        _record_ats_outcome(job["company_name"], ats, submitted=page_confirmed)

        _log_applied({
            "application_id":   app["id"],
            "job_id":           job["id"],
            "company":          job["company_name"],
            "role":             job["title"],
            "ats":              ats,
            "apply_url":        job["apply_url"],
            "quality_score":    app.get("quality_score", 0),
            "resume_path":      str(resume_pdf),
            "screenshot_path":  screenshot_path,
            "confirmation_url": (confirmation_url if page_confirmed else "") or "",
            "submit_confidence": "page" if page_confirmed else "click_only",
            "submitted_at":     now,
        })

        logger.log_decision(
            action="application_sent",
            outcome="awaiting_confirmation",
            details={
                "company":          job["company_name"],
                "role":             job["title"],
                "ats":              ats,
                "confirmation_url": confirmation_url,
            },
        )
    elif assisted:
        # User didn't complete the submit in the visible window — keep it queued so
        # they can re-approve and try again (gate stays approved). Not a real failure.
        updated["status"] = "pending_review"
        updated["rejection_reason"] = "Not submitted in the visible browser — re-approve to retry"
    else:
        updated["status"] = "pending_manual_submit"
        if not updated.get("rejection_reason"):
            _reason = {
                "validation_error": "Form rejected the submit (validation errors) — re-apply via the extension",
                "no_submit_button": "Submit button not found — manual submit required",
                "submit_error": "Submit step errored — re-apply via the extension",
            }.get(confirmation_url, "Submit button not found — manual submit required")
            updated["rejection_reason"] = _reason
        _record_ats_outcome(job["company_name"], ats, submitted=False)

    return updated


# ── Main entry point ───────────────────────────────────────────────────────────

def _load_applied_job_ids() -> set[str]:
    """Load the set of job_ids already in applied_jobs.json (file-level dedup guard)."""
    if not _APPLIED_LOG.exists():
        return set()
    try:
        entries = json.loads(_APPLIED_LOG.read_text())
        return {e["job_id"] for e in entries if e.get("job_id")}
    except Exception:
        return set()


async def run(state: JobSearchState, approved_only: bool = False, assisted: bool = False) -> JobSearchState:
    """
    Process applications that passed quality review (status = pending_review).

    Two modes:
      • approved_only=False (default, pipeline fill step): fill + screenshot + PARK
        every reviewed app for review, and submit any already approved.
      • approved_only=True (the UI approve→submit trigger): only refill + submit apps
        the user has APPROVED; never touches parked-but-unapproved apps.
    Respects the daily apply cap. AUTONOMOUSHIRE_AUTO_APPROVE=1 submits without a gate.
    """
    cfg = get_settings()
    profile = yaml.safe_load(_PROFILE_PATH.read_text())
    llm = get_llm("form_filler", model="claude-haiku-4-5")

    jobs_by_id = {j["id"]: j for j in state.get("job_listings", [])}

    # File-level guard: never resubmit a job that's already in applied_jobs.json,
    # even if the in-memory state still shows it as pending_review (e.g. after a restart).
    already_applied_job_ids = _load_applied_job_ids()

    # Decide which pending_review apps to process this run. A gate is only created
    # the first time an app is filled, so:
    #   • no gate  → needs first fill + park (creates the gate, screenshots, waits)
    #   • approved → user said go → refill + submit
    #   • pending  → already parked, awaiting the user → SKIP (don't refill)
    #   • rejected → SKIP
    # AUTONOMOUSHIRE_AUTO_APPROVE=1 processes everything (fill straight to submit).
    from core.checkpoints import approval_status_for_apply_url, _auto_approve_enabled
    _auto = _auto_approve_enabled()

    def _should_process(app: Application) -> bool:
        if _auto:
            return True
        job = jobs_by_id.get(app.get("job_id"), {})
        gate = approval_status_for_apply_url(job.get("apply_url", ""))
        if approved_only:
            return gate == "approved"          # click→submit: only what was approved
        return gate in (None, "approved")      # pipeline: first-fill (no gate) or approved

    to_submit = [
        a for a in state.get("applications", [])
        if a.get("status") == "pending_review"
        and a.get("quality_score", 0) >= cfg.min_app_quality
        and a.get("tailored_resume_path")
        and a.get("job_id") not in already_applied_job_ids  # ← dedup guard
        and _should_process(a)                              # ← fill-or-submit gate
    ]

    # Sync in-memory state: mark any pending_review apps that are already in the log as submitted
    updated_apps_pre = {}
    for a in state.get("applications", []):
        if a.get("status") == "pending_review" and a.get("job_id") in already_applied_job_ids:
            a = dict(a)
            a["status"] = "submitted"
            logger.info("dedup_skip", job_id=a["job_id"], company=a.get("company_name"))
        updated_apps_pre[a["id"]] = a

    # Respect daily apply cap
    today = datetime.now(timezone.utc).date().isoformat()
    already_submitted_today = sum(
        1 for a in state.get("applications", [])
        if a.get("status") == "submitted"
        and a.get("submitted_at", "").startswith(today)
    )
    # Also count today's entries from applied_jobs.json (in case state was reset)
    if _APPLIED_LOG.exists():
        try:
            log_entries = json.loads(_APPLIED_LOG.read_text())
            already_submitted_today = max(
                already_submitted_today,
                sum(1 for e in log_entries if e.get("submitted_at", "").startswith(today))
            )
        except Exception:
            pass
    # The daily cap limits real SUBMISSIONS, not parking. In fill+park mode
    # (approved_only=False) apps that aren't approved just get screenshotted and
    # parked — that's free, so don't truncate it. Capping applies to the submit
    # path (approved_only / AUTO_APPROVE).
    if approved_only or _auto:
        daily_remaining = cfg.apply_max_per_day - already_submitted_today
        to_submit = to_submit[:max(0, daily_remaining)]

    logger.info(
        "form_filler_start",
        to_submit=len(to_submit),
        daily_cap=cfg.apply_max_per_day,
        already_today=already_submitted_today,
    )

    if not to_submit:
        logger.info("form_filler_nothing_to_submit")
        # Still return updated state so dedup corrections are persisted
        updated_state = dict(state)
        updated_state["applications"] = list(updated_apps_pre.values())
        return updated_state

    updated_apps = updated_apps_pre  # already built above

    # Parallelize Phase 1 (fill + screenshot) up to N concurrent Playwright contexts.
    # Assisted (visible) submit must be serial — the user handles one window at a time.
    sem = asyncio.Semaphore(1 if assisted else 3)

    async def _submit_with_sem(app: Application) -> None:
        job = jobs_by_id.get(app["job_id"])
        if not job:
            logger.warning("job_not_found_for_app", app_id=app["id"])
            return
        async with sem:
            try:
                result = await submit_application(app, job, profile, llm, assisted=assisted)
                updated_apps[result["id"]] = result
            except Exception as e:
                logger.error("submit_application_error", app_id=app["id"], error=str(e))

    await asyncio.gather(*[_submit_with_sem(a) for a in to_submit])

    all_apps = list(updated_apps.values())
    submitted = sum(1 for a in all_apps if a.get("status") == "submitted")
    skipped   = sum(1 for a in all_apps if a.get("status") == "skipped")

    logger.log_decision(
        action="form_filler_complete",
        outcome="success",
        details={"submitted": submitted, "skipped": skipped},
    )

    updated = dict(state)
    updated["applications"] = all_apps

    return updated


# ── CLI smoke test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    async def _test():
        import uuid as _uuid
        from datetime import datetime as dt, timezone as tz
        from pathlib import Path as P

        # Create a minimal fake application pointing to a real resume
        resumes = sorted(P("data/resumes").glob("*.md"))
        if not resumes:
            print("No tailored resumes found in data/resumes/. Run resume_tailor first.")
            return

        resume_path = resumes[0]
        print(f"Testing with resume: {resume_path}")

        fake_app: Application = {
            "id":                   str(_uuid.uuid4()),
            "job_id":               "test-job-001",
            "company_name":         "Test Company",
            "role_title":           "ML Platform Engineer",
            "status":               "pending_review",
            "tailored_resume_path": str(resume_path),
            "cover_letter_path":    None,
            "quality_score":        82,
            "quality_report":       {},
            "submitted_at":         None,
            "confirmation_url":     None,
            "rejection_reason":     None,
        }

        fake_job: JobListing = {
            "id":                "test-job-001",
            "company_id":        "test-co-001",
            "company_name":      "Test Company",
            "title":             "ML Platform Engineer",
            "description":       "Build ML infrastructure. Python, Kubernetes, LLMs required.",
            "required_skills":   ["Python", "Kubernetes", "LLMs"],
            "nice_to_have_skills": [],
            "location":          "Remote",
            "remote":            True,
            "salary_min":        160000,
            "salary_max":        210000,
            "apply_url":         "https://boards.greenhouse.io/anthropic/jobs/4890083007",
            "source":            "greenhouse",
            "discovered_at":     dt.now(tz.utc).isoformat(),
        }

        profile = yaml.safe_load(_PROFILE_PATH.read_text())
        llm = get_llm("form_filler", model="claude-haiku-4-5")

        print(f"\nATS detected: {detect_ats(fake_job['apply_url'])}")
        print("Starting form fill + human approval gate...")
        print("(Check data/pending_approvals.json to approve)")

        result = await submit_application(fake_app, fake_job, profile, llm, headless=False)
        print(f"\nResult status: {result['status']}")
        if result.get("confirmation_url"):
            print(f"Confirmation: {result['confirmation_url']}")
        if result.get("rejection_reason"):
            print(f"Reason: {result['rejection_reason']}")

    asyncio.run(_test())
