"""Bench marketing — put the résumé in front of state-government vendor recruiters.

In the C2C market you don't apply, you get *represented*: a vendor holding a public-sector master
contract submits you against a state requirement. Being on the right benches is most of the game,
and the way onto a bench is a direct note to the recruiter with a résumé attached.

Scope, deliberately narrow: this only ever emails recruiters who **emailed Rahul a requirement
first** (harvested from his own inbox by vendor_harvest). Every send is therefore a reply into an
existing thread of contact, not a cold blast to a scraped address, and every message carries a
one-line opt-out. No address from any other source is ever added.

Volume rules, both learned the hard way:
  * DAILY_CAP is 400, not the ~500 Gmail actually allows. A 520-send blast once consumed the whole
    daily quota and left Rahul unable to reply to a real recruiter for 24 hours. The remaining 100
    stay in reserve for actual conversations.
  * Sends are paced, not bursted, and each one is appended to data/outreach_sent.jsonl and skipped
    forever after — so a re-run continues down the list instead of re-mailing the top of it.

Nothing here sends until `dry_run=False` is passed explicitly.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import random
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import yaml

from core.logger import get_logger
from agents.outreach.vendor_harvest import load_contacts, _NEVER_MAIL, _PERSONAL_MATTER

logger = get_logger("vendor_blast")

_ROOT = Path(__file__).parent.parent.parent
_PROFILE = _ROOT / "data" / "user_profile.yaml"
_SENT = _ROOT / "data" / "outreach_sent.jsonl"

# Gmail consumer accounts allow ~500 sends/day. Bulk outreach caps at 400 so 100 stay available for
# on-demand replies — see the module docstring for why this ceiling is not raised.
DAILY_CAP = 400
_RESERVE = 100


def _profile() -> dict:
    return yaml.safe_load(_PROFILE.read_text()) if _PROFILE.exists() else {}


def _sent_today() -> int:
    if not _SENT.exists():
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    n = 0
    for line in _SENT.read_text().splitlines():
        try:
            if (json.loads(line).get("sent_at") or "").startswith(today):
                n += 1
        except Exception:
            continue
    return n


def _sent_ever() -> set[str]:
    out = set()
    if not _SENT.exists():
        return out
    for line in _SENT.read_text().splitlines():
        try:
            out.add((json.loads(line).get("email") or "").strip().lower())
        except Exception:
            continue
    return out


# First tokens that are never a person's name. Recruiter From-headers are routinely set to a role or
# a company ("US IT Recruiter", "Staffing - ABC Inc", "Talent Acquisition"), and "Hi US," or
# "Hi Staffing," is worse than no name at all.
_NOT_A_NAME = {
    "us", "it", "hr", "usa", "sr", "jr", "mr", "ms", "mrs", "dr", "the", "team", "recruiter",
    "recruiting", "recruitment", "staffing", "talent", "hiring", "careers", "career", "jobs",
    "job", "info", "admin", "support", "sales", "consulting", "consultant", "solutions", "inc",
    "llc", "corp", "group", "technologies", "technology", "systems", "services", "resource",
    "resources", "global", "national", "american", "india", "resume", "resumes", "bench",
}


def _safe_recipient(contact: dict) -> bool:
    """Second gate, at send time, on top of the one in vendor_harvest.

    The contacts file can be stale or hand-edited, and the cost of one bad address here is not a
    wasted send — it's mailing a bench pitch to Rahul's own immigration attorney, or to a newsletter
    address that damages sender reputation for the whole campaign. Both were sitting in the ranked
    list at positions 26-50. Cheap check, so it runs again right before the send loop.
    """
    if _NEVER_MAIL.search(contact.get("email", "")):
        return False
    # Every remembered subject being personal paperwork means there's no staffing thread here at all.
    subjects = contact.get("subjects") or []
    return not (subjects and all(_PERSONAL_MATTER.search(s) for s in subjects))


def _first_name(contact: dict) -> str:
    name = (contact.get("name") or "").strip()
    first = name.split()[0] if name else ""
    if not first.isalpha() or not (2 <= len(first) <= 14):
        return "there"
    if first.lower() in _NOT_A_NAME:
        return "there"
    # An all-caps short token is an acronym, not a name (US, IT, ABC, XYZ).
    if first.isupper() and len(first) <= 4:
        return "there"
    return first


def compose(contact: dict, profile: dict, *, work_auth: str, availability: str,
            resume_name: str) -> tuple[str, str]:
    """Return (subject, body).

    Every claim here has to be literally true. These are long-lived relationships and a vendor that
    catches an inflated line drops you permanently — so the public-sector angle is framed as
    *transferable regulated-industry work* (healthcare claims/member data at a Medicare Advantage
    plan, financial systems at two banks), never as state-government experience he doesn't have.
    """
    personal = profile.get("personal", {})
    states = [s for s in (contact.get("states") or []) if s]
    state_line = ""
    if states:
        pretty = ", ".join(states[:3])
        state_line = (f"I've seen {pretty} public-sector requirements come from you — that's exactly "
                      f"the kind of engagement I'm targeting.\n\n")

    subject = ("Senior Data / Backend Engineer (8 yrs — Java, Python, AWS) — "
               "available for state & local government contracts")

    body = f"""Hi {_first_name(contact)},

You've sent me requirements before, so I wanted to put my current profile in front of you directly and ask to be added to your bench list.

{state_line}Quick summary:
- 8 years building backend and data-platform systems: Java/Spring Boot, Python, SQL, Spark, Kafka
- Cloud across AWS, GCP and Azure (Snowflake, Databricks, Redshift, BigQuery), plus CI/CD, Terraform, Kubernetes
- Currently Senior Data Engineer at JPMorgan Chase; previously Alignment Health (Medicare Advantage healthcare data) and Ameriprise Financial (Java)
- Regulated-industry background - healthcare claims and member data, and financial systems - which maps closely onto state health, Medicaid and revenue programs

Work authorization: {work_auth}
Location: {personal.get('location', 'Plano, TX')} - open to remote, hybrid, or relocation for the right engagement
Availability: {availability}

Resume attached ({resume_name}). If you have public-sector or commercial requirements that fit, I'd welcome a call.

Best regards,
{personal.get('name', 'Rahul Varikuppala')}
{personal.get('phone', '')} | {personal.get('email', '')}
{personal.get('linkedin_url', '')}

If you'd prefer not to hear from me again, reply "remove" and I won't write back.
"""
    return subject, body


def _message(to: str, subject: str, body: str, sender: str, resume: Path) -> dict:
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    ctype, _ = mimetypes.guess_type(resume.name)
    maintype, subtype = (ctype or "application/pdf").split("/", 1)
    msg.add_attachment(resume.read_bytes(), maintype=maintype, subtype=subtype,
                       filename=resume.name)
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}


def _spread_by_firm(contacts: list[dict], per_domain: int) -> list[dict]:
    """Re-order so no firm gets more than `per_domain` emails before every other firm has had one.

    The goal is REACH — being on many benches — and five recruiters at saxonglobal.com forwarding
    each other the same pitch buys one bench, looks like a blast, and spends five sends doing it.
    Interleaving by domain turns the same budget into far more distinct firms, and the surplus
    recruiters simply stay in the queue for a later batch.
    """
    by_domain: dict[str, list[dict]] = {}
    for c in contacts:
        by_domain.setdefault(c.get("domain") or c["email"].split("@")[-1], []).append(c)
    out, round_no = [], 0
    while round_no < per_domain:
        for domain_contacts in by_domain.values():
            if len(domain_contacts) > round_no:
                out.append(domain_contacts[round_no])
        round_no += 1
    return out


def run(*, resume: str, work_auth: str, availability: str, limit: int = 50,
        gov_only: bool = True, dry_run: bool = True, pause: float = 1.5,
        per_domain: int = 2) -> dict:
    """Send the bench email to the next `limit` un-contacted vendor recruiters.

    gov_only:   only recruiters who sent a requirement naming a state/local government client.
    per_domain: at most this many recruiters per staffing firm in one batch (reach beats depth).
    dry_run:    compose and report, send nothing. Always run this first and read the sample.
    """
    resume_path = Path(resume)
    if not resume_path.exists():
        return {"error": f"resume not found: {resume}"}

    profile = _profile()
    sender = profile.get("personal", {}).get("email", "")
    already = _sent_ever()
    contacts = [c for c in load_contacts()
                if c["email"] not in already and (c.get("gov_reqs", 0) > 0 or not gov_only)
                and _safe_recipient(c)]

    remaining_today = max(0, DAILY_CAP - _sent_today())
    pool = len(contacts)
    firms = len({c.get("domain") for c in contacts})
    # The per-firm cap limits ONE batch, not the pool — recruiters it holds back stay queued for the
    # next run, so report both numbers rather than letting the cap look like the whole universe.
    contacts = _spread_by_firm(contacts, per_domain)
    batch = contacts[:min(limit, remaining_today)]
    report = {"pool": pool, "firms_in_pool": firms, "eligible_this_batch": len(contacts),
              "batch": len(batch), "sent": 0, "failed": 0,
              "firms_reached": len({c.get("domain") for c in batch}),
              "dry_run": dry_run, "remaining_today": remaining_today,
              "reserve_held": _RESERVE, "resume": resume_path.name}
    if not batch:
        return report

    if dry_run:
        subject, body = compose(batch[0], profile, work_auth=work_auth,
                                availability=availability, resume_name=resume_path.name)
        report["sample"] = {"to": batch[0]["email"], "subject": subject, "body": body}
        report["recipients"] = [{"email": c["email"], "name": c.get("name", ""),
                                 "states": c.get("states", []), "gov_reqs": c.get("gov_reqs", 0)}
                                for c in batch]
        return report

    from agents.monitoring.email_monitor import _build_gmail_service
    svc = _build_gmail_service()
    with _SENT.open("a") as log:
        for c in batch:
            subject, body = compose(c, profile, work_auth=work_auth,
                                    availability=availability, resume_name=resume_path.name)
            try:
                svc.users().messages().send(
                    userId="me", body=_message(c["email"], subject, body, sender, resume_path)
                ).execute()
            except Exception as e:
                report["failed"] += 1
                logger.warning("vendor_blast_send_failed", to=c["email"], error=str(e)[:120])
                continue
            report["sent"] += 1
            log.write(json.dumps({"email": c["email"], "agency": c.get("domain", ""),
                                  "states": c.get("states", []), "campaign": "gov_bench",
                                  "sent_at": datetime.now(timezone.utc).isoformat()}) + "\n")
            log.flush()
            # Paced, never bursted: hundreds of near-identical sends in a tight loop is what trips
            # both spam heuristics and the send quota.
            time.sleep(pause + random.random())
    logger.log_decision(action="vendor_blast_complete", outcome="success", details=report)
    return report
