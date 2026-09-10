"""Harvest the STATE-GOVERNMENT vendor recruiters out of the applicant's own inbox.

The C2C market runs on vendor relationships, not applications: a staffing recruiter emails you a
requirement, and from then on you're on their bench list. Rahul's mailbox already holds thousands of
those emails — which makes it, not any job board, the highest-quality vendor directory available.

This module finds the subset that matters: recruiters whose requirements name a STATE or LOCAL
government client (a Medicaid/MMIS program, a DOT, a Department of Revenue, a state capital worksite).
Those are the desks with public-sector master contracts, and they're the ones worth being on the
bench list of.

Why the inbox and not `data/past_vendors.json`: that file keeps only ~3 sampled subjects per sender,
and a keyword pass over it finds nothing — the government signal is spread across the full archive.
Searching Gmail directly reads every message a query matches.

Contacts harvested here are people who cold-emailed Rahul a requirement — replying to them is
invited contact. Nothing else is ever added to the outreach list.
"""
from __future__ import annotations

import json
import random
import re
import time
from collections import defaultdict
from pathlib import Path

from core.logger import get_logger
from core.gov_client import detect_gov_client, is_federal_client

logger = get_logger("vendor_harvest")

_ROOT = Path(__file__).parent.parent.parent
_OUT = _ROOT / "data" / "gov_vendor_contacts.json"
_SENT = _ROOT / "data" / "outreach_sent.jsonl"

# Gmail queries aimed at requirement emails naming a state/local government client, each tagged
# STRONG or WEAK. A STRONG query names a government buyer outright, so a match is itself the
# evidence — the recruiter sent a public-sector req, full stop. A WEAK query matches on worksite
# city, and state capitals are commercial tech hubs too (Austin, Phoenix, Atlanta, Denver), so those
# only count when something else corroborates.
#
# The tiering exists because the first version re-ran a phrase detector over the SUBJECT LINE and
# kept only what matched there — which discarded the Gmail query's own evidence and cut 217 real
# vendor contacts down to 10. The message matched; the phrase just lived in the body.
_QUERIES = (
    ("strong", '"State of" (developer OR engineer OR analyst OR consultant OR architect)'),
    ("strong", '(Medicaid OR MMIS OR HHSC OR DHHS OR DSHS) '
               '(developer OR engineer OR analyst OR consultant)'),
    ("strong", '"Department of" (Transportation OR Revenue OR "Motor Vehicles" OR '
               '"Human Services" OR Health) (developer OR engineer OR analyst)'),
    ("strong", '("public sector" OR "state agency" OR "state client" OR "government client" OR '
               '"statewide") (developer OR engineer OR analyst OR consultant)'),
    ("weak",   '(Harrisburg OR "Richmond, VA" OR "Columbia, SC" OR Lansing OR "Albany, NY" OR '
               'Trenton OR "Raleigh, NC" OR Tallahassee OR "Springfield, IL" OR "Baton Rouge") '
               '(developer OR engineer OR analyst OR consultant)'),
)

# Automated job-alert senders — never a person, never worth an email.
_ROBOT = re.compile(
    r"(no-?reply|donotreply|do-not-reply|@alerts?\.|alerts?@|notification|mailer|bounce|"
    r"\bjobs@|\bjobsearch@|\bjobalert|\bcareers@|\binfo@|\bsupport@|\bnews@|"
    r"careerbuilder|indeed\.com|dice\.com|linkedin\.com|ziprecruiter|monster\.com|glassdoor|"
    r"talent\.com|jobcase|techfetch|upward\.careers|smartrecruiters\.com|greenhouse\.io|"
    r"lever\.co|myworkday)", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_NAME = re.compile(r'^\s*"?([^"<@]+?)"?\s*<')

# Free/consumer mail domains — a recruiter using one isn't a firm we can identify or verify.
_FREEMAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com",
             "live.com", "msn.com", "protonmail.com", "mail.com"}


def _throttled(fn, *, tries: int = 9):
    """Gmail's per-user 'Total Query Cost' burst limit trips easily on a wide harvest. Retry with
    exponential backoff + jitter, and on exhaustion return None rather than raising — one throttled
    message must not discard a scan that already cost thousands of API calls."""
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:
            if "rateLimitExceeded" not in str(e) and "Quota exceeded" not in str(e):
                raise
            time.sleep(min(2 ** attempt, 60) + random.random() * 2)
    logger.warning("gmail_rate_limit_gave_up")
    return None


def _sent_already() -> set[str]:
    out = set()
    if not _SENT.exists():
        return out
    for line in _SENT.read_text().splitlines():
        try:
            out.add((json.loads(line).get("email") or "").strip().lower())
        except Exception:
            continue
    return out


def harvest(*, max_per_query: int = 400, pause: float = 0.15) -> dict:
    """Scan the mailbox for state/local-government requirement emails and return the vendor
    recruiters behind them, keyed by email address."""
    from agents.monitoring.email_monitor import _build_gmail_service
    svc = _build_gmail_service()

    contacts: dict[str, dict] = {}
    evidence: dict[str, list] = defaultdict(list)
    scanned = 0

    for tier, query in _QUERIES:
        page, seen = None, 0
        while seen < max_per_query:
            resp = _throttled(lambda: svc.users().messages().list(
                userId="me", q=query, maxResults=100, pageToken=page).execute())
            if resp is None:
                break
            msgs = resp.get("messages", [])
            if not msgs:
                break
            for m in msgs:
                seen += 1
                scanned += 1
                detail = _throttled(lambda mid=m["id"]: svc.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["From", "Subject", "Date"]).execute())
                if detail is None:
                    continue
                headers = {h["name"]: h["value"]
                           for h in detail.get("payload", {}).get("headers", [])}
                frm, subject = headers.get("From", ""), headers.get("Subject", "")
                # The snippet is ~200 chars of body and comes back free with metadata format. The
                # requirement's client and worksite usually sit there rather than in the subject.
                snippet = detail.get("snippet", "") or ""
                match = _EMAIL.search(frm)
                if not match or _ROBOT.search(frm):
                    continue
                email = match.group(0).lower()
                domain = email.split("@")[-1]
                if domain in _FREEMAIL:
                    continue
                if is_federal_client(f"{subject}\n{snippet}"):
                    continue
                gov = detect_gov_client(subject, snippet, "", "")
                name_match = _NAME.match(frm)
                entry = contacts.setdefault(email, {
                    "email": email, "domain": domain,
                    "name": (name_match.group(1).strip() if name_match else "").title(),
                    "reqs": 0, "gov_reqs": 0, "weak_reqs": 0, "states": [], "subjects": [],
                })
                entry["reqs"] += 1
                # A STRONG query match IS the government evidence — the recruiter sent a req naming
                # a public-sector buyer. detect_gov_client only refines it (which state).
                if tier == "strong":
                    entry["gov_reqs"] += 1
                else:
                    entry["weak_reqs"] += 1
                if gov and gov.get("state") and gov["state"] not in entry["states"]:
                    entry["states"].append(gov["state"])
                if len(entry["subjects"]) < 5:
                    entry["subjects"].append(subject[:140])
                evidence[email].append(subject[:140])
                time.sleep(pause)
            page = resp.get("nextPageToken")
            if not page:
                break
        logger.info("vendor_harvest_query_done", query=query[:44], scanned=seen)

    # Rank: an explicit government client beats volume, volume breaks ties.
    ranked = sorted(contacts.values(), key=lambda c: (-c["gov_reqs"], -c["reqs"]))
    sent = _sent_already()
    for c in ranked:
        c["already_contacted"] = c["email"] in sent
    payload = {
        "_comment": "State/local-government vendor recruiters harvested from the applicant's own "
                    "inbox (they cold-emailed a requirement first — invited contact). Built by "
                    "agents/outreach/vendor_harvest.py.",
        "scanned_messages": scanned,
        "contacts": ranked,
    }
    _OUT.write_text(json.dumps(payload, indent=1) + "\n")
    logger.info("vendor_harvest_done", scanned=scanned, contacts=len(ranked),
                gov=sum(1 for c in ranked if c["gov_reqs"]),
                fresh=sum(1 for c in ranked if not c["already_contacted"]))
    return payload


def load_contacts() -> list[dict]:
    try:
        return json.loads(_OUT.read_text()).get("contacts", [])
    except Exception:
        return []
