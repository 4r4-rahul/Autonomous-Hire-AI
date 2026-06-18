"""
Gmail email monitor — watches for interview invites, rejections, and offers,
then updates application pipeline state automatically.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from core.config import get_settings
from core.llm import get_llm
from core.logger import get_logger
from core.state import JobSearchState, EmailEvent

logger = get_logger("email_monitor")

# ── Auto-learn per-company application caps from "application limit" / auto-close emails ──────────
# Some employers cap applications per rolling window and auto-close the excess (Roblox: 5 per 30
# days). When such an email arrives we LEARN the limit and record it in data/company_caps.json so
# the pipeline stops over-applying there — without imposing a cap on companies that don't limit.
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_LIMIT_RE = re.compile(
    r"(?:maximum of|limit(?:ed)?(?:\s+\w+){0,3}?\s+to(?:\s+a)?(?:\s+maximum of)?|"
    r"limit of|no more than|more than|up to)\s+"
    r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+application",
    re.I)
# Email/ATS sender domains that are NOT the hiring company — don't infer a company name from these.
_NON_COMPANY_DOMAINS = {"greenhouse", "lever", "ashbyhq", "workable", "smartrecruiters", "icims",
                        "myworkdayjobs", "workday", "jobvite", "gmail", "googlemail", "google",
                        "careerpuck", "phenompeople", "myworkday", "hire", "us", "eu"}


def _detect_application_limit(subject: str, body: str) -> Optional[int]:
    """If the email states an application-limit / auto-close policy, return the limit N, else None."""
    text = f"{subject}\n{body}".lower()
    if not any(k in text for k in ("application limit", "maximum of", "limit candidates",
                                   "limit applicants", "automatically closed", "limit of",
                                   "applications within", "no more than", "up to", "we limit",
                                   "limit the number", "more than")):
        return None
    for m in _LIMIT_RE.finditer(text):
        tok = m.group(1)
        n = int(tok) if tok.isdigit() else _WORD_NUM.get(tok)
        if n and 1 <= n <= 20:
            return n
    return None


def _company_from_sender(sender: str) -> Optional[str]:
    """Best-effort company name from an email sender domain (no-reply@roblox.com → 'roblox')."""
    m = re.search(r"@([\w.-]+)", sender or "")
    if not m:
        return None
    labels = [p for p in m.group(1).lower().split(".") if p]
    # registrable label = second-to-last (roblox.com → roblox; jobs.roblox.com → roblox)
    cand = labels[-2] if len(labels) >= 2 else (labels[0] if labels else "")
    return cand if cand and cand not in _NON_COMPANY_DOMAINS else None

_CATEGORIZE_SYSTEM = """You are an email classifier for a job search pipeline.
Given an email subject and body, extract:
- category: one of "interview_invite", "rejection", "offer", "application_received", "info_request", "follow_up_needed", "other"
  · "application_received" = an automated confirmation that an application was received
    (e.g. "Thank you for applying", "We've received your application", ATS receipt from
    Greenhouse/Lever/Workday). NOT a rejection, interview, or offer.
- company_name: the hiring company name, or null if unclear
- summary: one concise sentence describing what the email says

Return valid JSON only, no markdown:
{"category": "...", "company_name": "...", "summary": "..."}"""

_NOTABLE_CATEGORIES = {"interview_invite", "rejection", "offer", "application_received"}


# ── Gmail client ───────────────────────────────────────────────────────────────

def _build_gmail_service():
    cfg = get_settings()
    creds = Credentials(
        token=None,
        refresh_token=cfg.gmail_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=cfg.gmail_client_id,
        client_secret=cfg.gmail_client_secret,
    )
    return build("gmail", "v1", credentials=creds)


def _search_query() -> str:
    # NOT is:unread — the user often reads recruiter/rejection emails before the monitor
    # runs, and read-marking can fail on a read-only Gmail scope. Dedup is handled by
    # email_events (gmail_message_id) instead, so re-scanning read mail is safe.
    after = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y/%m/%d")
    return (
        f"after:{after} "
        "(interview OR application OR offer OR rejection OR position OR role OR opportunity "
        "OR sponsorship OR candidacy OR \"next steps\" OR \"moving forward\" OR "
        "\"not moving forward\" OR \"thank you for applying\" OR unfortunately OR "
        "\"regret to inform\" OR \"other candidates\")"
    )


def _extract_text(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = _extract_text(part)
            if text:
                return text
    return ""


def _get_header(headers: list[dict], name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _fetch_unread_emails(service) -> list[dict]:
    """Return list of dicts with id, subject, sender, received_at, body."""
    try:
        result = service.users().messages().list(
            userId="me",
            q=_search_query(),
            maxResults=50,
        ).execute()
    except HttpError as e:
        logger.error("gmail_list_failed", error=str(e))
        return []

    messages = result.get("messages", [])
    if not messages:
        return []

    emails = []
    for msg_stub in messages:
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_stub["id"], format="full"
            ).execute()
            payload = msg.get("payload", {})
            headers = payload.get("headers", [])
            subject = _get_header(headers, "Subject")
            sender = _get_header(headers, "From")
            date_str = _get_header(headers, "Date")
            body = _extract_text(payload)[:1500]
            internal_date_ms = int(msg.get("internalDate", 0))
            received_at = datetime.fromtimestamp(
                internal_date_ms / 1000, tz=timezone.utc
            ).isoformat()
            emails.append({
                "id": msg_stub["id"],
                "subject": subject,
                "sender": sender,
                "received_at": received_at,
                "body": body,
            })
        except HttpError as e:
            logger.warning("gmail_get_message_failed", msg_id=msg_stub["id"], error=str(e))

    return emails


def _mark_as_read(service, msg_id: str) -> None:
    try:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
    except HttpError as e:
        # Expected when the token is read-only (gmail.readonly) by design — marking read needs the
        # gmail.modify scope we deliberately don't request. That 403 is not an error; stay silent.
        # The monitor scans read mail too, so dedup doesn't depend on marking read. Warn only on
        # genuinely unexpected failures.
        status = getattr(getattr(e, "resp", None), "status", None)
        if status == 403 or "insufficient" in str(e).lower():
            return
        logger.warning("gmail_mark_read_failed", msg_id=msg_id, error=str(e)[:160])


# ── Categorization ─────────────────────────────────────────────────────────────

async def _categorize_email(llm, subject: str, sender: str, body: str) -> dict:
    prompt = f"Subject: {subject}\nFrom: {sender}\n\nBody:\n{body}"
    try:
        result = await llm.chat_json(
            system=_CATEGORIZE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
            temperature=0.1,
        )
        return {
            "category": result.get("category", "other"),
            "company_name": result.get("company_name"),
            "summary": result.get("summary", subject),
        }
    except Exception as e:
        logger.warning("email_categorize_failed", error=str(e))
        return {"category": "other", "company_name": None, "summary": subject}


# ── Application status update (role-aware) ──────────────────────────────────────

_CO_SUFFIXES = (" inc", " inc.", " llc", " l.l.c", " corp", " corporation", " ltd",
                " limited", " technologies", " technology", " labs", " co", " company",
                " group", " holdings", " ai", ",")
# Generic words that don't distinguish one role from another at the same company —
# matching must rely on DISTINCTIVE tokens (team/product/specialization names).
_STOP = {"the", "and", "of", "for", "a", "an", "to", "at", "in", "ii", "iii", "iv",
         "sr", "jr", "senior", "staff", "principal", "lead", "manager", "engineer",
         "engineering", "software", "remote", "hybrid", "ai", "ml", "data", "platform",
         "platforms", "backend", "frontend", "fullstack", "full", "stack", "cloud",
         "infrastructure", "infra", "systems", "system", "developer", "dev", "applications",
         "application", "position", "role", "team", "group", "technical", "product",
         "machine", "learning", "intelligence", "services", "service", "experienced"}


def _norm_company(s: str) -> str:
    s = (s or "").lower().strip()
    for suf in _CO_SUFFIXES:
        s = s.replace(suf, " ")
    return re.sub(r"[^a-z0-9 ]", " ", s).split(" ")[0] if s.strip() else ""


def _role_tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOP and len(w) > 2}


def _match_application(state: JobSearchState, company: str, summary: str,
                      eligible: set, strict: bool = False) -> Optional[dict]:
    """Find the ONE application this email is about: same company (normalized) and, when
    several match, the one whose role best matches the email text. Returns it or None.
    strict=True (rejection/interview/offer): if several candidates and NO role overlap,
    return None rather than risk marking the wrong live application."""
    core = _norm_company(company)
    if not core:
        return None
    cands = [a for a in state.get("applications", [])
             if a.get("status") in eligible and core and core in _norm_company(a.get("company_name", ""))]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    # Disambiguate by role: token overlap between the email text and each app's role title.
    etoks = _role_tokens(summary)
    best, best_score = None, 0
    for a in cands:
        score = len(_role_tokens(a.get("role_title", "")) & etoks)
        if score > best_score:
            best, best_score = a, score
    if best:
        return best
    if strict:
        return None  # ambiguous — don't guess which live app to mark rejected/interviewed
    return max(cands, key=lambda a: a.get("sent_at") or a.get("submitted_at") or "")


def _update_application_status(state: JobSearchState, category: str, company_name: Optional[str], summary: str) -> None:
    if not company_name:
        return
    now = datetime.now(timezone.utc).isoformat()

    # Confirmation finalizes "submitted" — only upgrade an app we actually sent.
    if category == "application_received":
        app = _match_application(state, company_name, summary,
                                 {"awaiting_confirmation", "submitting"})
        if app:
            app["status"] = "submitted"
            app["submitted_at"] = now
            logger.info("application_confirmed_submitted",
                        company=company_name, role=app.get("role_title"))
        return

    new_status = {"interview_invite": "interview", "rejection": "rejected",
                  "offer": "offer"}.get(category)
    if not new_status:
        return
    # Attach to the right open application (not a terminal one). strict=True so an
    # ambiguous multi-role company doesn't get the wrong app marked rejected/interviewed.
    app = _match_application(state, company_name, summary,
                            {"submitted", "awaiting_confirmation", "submitting",
                             "approved", "pending_review", "pending_manual_submit"},
                            strict=True)
    if not app:
        logger.info("email_no_matching_application", company=company_name, category=category)
        return
    app["status"] = new_status
    app["outcome"] = new_status
    if category == "rejection":
        app["rejection_reason"] = summary
    logger.info("application_status_updated", company=company_name,
                role=app.get("role_title"), new_status=new_status)


def reconcile_awaiting(state: JobSearchState, hours: int = 24) -> int:
    """Close the loop on auto-submits that never produced a confirmation email.

    "Applied" is decided ONLY by the Gmail confirmation. So an auto-submit sits in
    `awaiting_confirmation` until the email lands. Two cases need a verdict after a wait:
      • submit_confidence == "click_only": we clicked submit but never saw a success
        page AND no confirmation arrived → assume it did NOT go through → re-queue it
        (pending_review) so the user can re-apply via the extension.
      • submit_confidence == "page": we have page evidence it submitted. Many companies
        never send a confirmation email, so we DON'T re-queue (that would double-apply) —
        it stays awaiting_confirmation, surfaced in the digest as "sent, unconfirmed".
    Returns the number of apps re-queued.
    """
    now = datetime.now(timezone.utc)
    requeued = 0
    for a in state.get("applications", []):
        if a.get("status") != "awaiting_confirmation":
            continue
        if a.get("submit_confidence") != "click_only":
            continue  # page-confirmed: leave it; never auto re-apply
        sent = a.get("sent_at")
        if not sent:
            continue
        try:
            age_h = (now - datetime.fromisoformat(str(sent).replace("Z", "+00:00"))).total_seconds() / 3600
        except Exception:
            continue
        if age_h < hours:
            continue
        a["status"] = "pending_review"
        a["submit_confidence"] = None
        a["rejection_reason"] = (
            f"Auto-submit clicked but no confirmation email in {int(age_h)}h — "
            "re-apply via Open & Apply (extension) to be sure"
        )
        requeued += 1
        logger.info("awaiting_reconciled_requeued", company=a.get("company_name"),
                    role=a.get("role_title"), age_h=round(age_h, 1))
    if requeued:
        logger.info("awaiting_reconcile_summary", requeued=requeued)
    return requeued


# ── Discord notification ───────────────────────────────────────────────────────

async def _post_discord(webhook: str, message: str) -> None:
    try:
        async with httpx.AsyncClient() as client:
            await client.post(webhook, json={"content": message}, timeout=5)
    except Exception:
        pass


async def _notify_discord(category: str, company: str, summary: str) -> None:
    cfg = get_settings()
    webhook = cfg.discord_webhook_review
    if not webhook:
        return

    templates = {
        "interview_invite": "🎯 Interview invite from {company} — {summary}",
        "rejection": "❌ Rejection from {company} — {summary}",
        "offer": "🎉 Offer from {company} — {summary}",
    }
    template = templates.get(category)
    if not template:
        return

    message = template.format(company=company, summary=summary)
    asyncio.ensure_future(_post_discord(webhook, message))


# ── Main run function ──────────────────────────────────────────────────────────

async def run(state: JobSearchState) -> JobSearchState:
    cfg = get_settings()

    # Always start from a working copy so the reconcile sweep can run even when there
    # are no new emails (it's time-based, not email-driven).
    updated_state = dict(state)
    updated_state["email_events"] = list(state.get("email_events", []))
    updated_state["applications"] = [dict(a) for a in state.get("applications", [])]

    def _finish(st: JobSearchState) -> JobSearchState:
        reconcile_awaiting(st)  # re-queue click-only sends that never got confirmed
        return st

    if not cfg.gmail_refresh_token or not cfg.gmail_client_id or not cfg.gmail_client_secret:
        logger.warning("gmail_credentials_missing")
        return _finish(updated_state)

    try:
        service = _build_gmail_service()
    except Exception as e:
        logger.error("gmail_auth_failed", error=str(e))
        return _finish(updated_state)

    raw_emails = _fetch_unread_emails(service)
    if not raw_emails:
        logger.info("email_monitor_no_new_emails")
        return _finish(updated_state)

    existing_ids = {ev["gmail_message_id"] for ev in state.get("email_events", [])}
    new_emails = [e for e in raw_emails if e["id"] not in existing_ids]

    if not new_emails:
        logger.info("email_monitor_all_already_processed", count=len(raw_emails))
        return _finish(updated_state)

    logger.info("email_monitor_processing", count=len(new_emails))

    llm = get_llm("email_monitor", model="claude-haiku-4-5")

    for email in new_emails:
        classification = await _categorize_email(
            llm,
            subject=email["subject"],
            sender=email["sender"],
            body=email["body"],
        )

        category = classification["category"]
        company_name = classification["company_name"]
        summary = classification["summary"]

        event: EmailEvent = {
            "id": str(uuid.uuid4()),
            "gmail_message_id": email["id"],
            "subject": email["subject"],
            "sender": email["sender"],
            "received_at": email["received_at"],
            "category": category,
            "company_name": company_name,
            "job_id": None,
            "summary": summary,
            "actioned": False,
        }
        updated_state["email_events"].append(event)

        _update_application_status(updated_state, category, company_name, summary)

        # Auto-learn a per-company application cap from limit/auto-close policy emails (Roblox etc.),
        # regardless of category. Apply a 1-app safety margin (our submission counts undercount the
        # manual extension flow), keeping the tightest limit if we've seen one before.
        limit = _detect_application_limit(email["subject"], email["body"])
        if limit:
            co = company_name or _company_from_sender(email["sender"])
            if co:
                from core.company_caps import set_cap
                applied = max(1, limit - 1)
                set_cap(co, applied, source="email",
                        note=f"policy max {limit} from auto-close email {email.get('received_at','')}")
                logger.info("application_cap_learned", company=co,
                            detected_limit=limit, applied_cap=applied, subject=email["subject"])

        if category in _NOTABLE_CATEGORIES and company_name:
            await _notify_discord(category, company_name, summary)

        _mark_as_read(service, email["id"])

        logger.info(
            "email_processed",
            gmail_id=email["id"],
            category=category,
            company=company_name,
        )

    return _finish(updated_state)
