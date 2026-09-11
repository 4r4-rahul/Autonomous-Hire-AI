"""Bounce tracking — keep a dead address from ever being mailed twice.

Learned in production, expensively: the first gov bench batch bounced 22 of 64 sends (34%). Staffing
recruiters change firms constantly, so a contact list built from a mail archive rots fast — most of
those addresses were over a year old and the person had moved on.

A bounce rate like that is not merely wasted sends. Mailbox providers read it as list-buying
behaviour and start filing everything you send into spam, which would silently kill the outreach
that *is* working. So bounces are harvested after every batch, stored permanently, and enforced as a
hard suppression list before any future send.

The real fix is upstream — mail recruiters who emailed recently (see vendor_harvest.harvest_active)
rather than whoever appears most often in the archive. This module is the safety net under that.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from core.logger import get_logger

logger = get_logger("deliverability")

_ROOT = Path(__file__).parent.parent.parent
_BOUNCED = _ROOT / "data" / "bounced_addresses.json"
_SENT = _ROOT / "data" / "outreach_sent.jsonl"

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
# Gmail surfaces a failure either as its own Mail Delivery Subsystem notice or as the receiving
# server's "Undeliverable:" bounce — plus soft variants worth suppressing (mailbox full, disabled).
_BOUNCE_QUERY = ('(from:mailer-daemon OR from:postmaster OR subject:Undeliverable OR '
                 'subject:"Delivery Status Notification" OR subject:"Returned mail" OR '
                 'subject:"Mail delivery failed")')


def load_bounced() -> set[str]:
    try:
        return {a.lower() for a in json.loads(_BOUNCED.read_text()).get("addresses", [])}
    except Exception:
        return set()


def _sent_addresses() -> set[str]:
    out = set()
    if not _SENT.exists():
        return out
    for line in _SENT.read_text().splitlines():
        try:
            out.add((json.loads(line).get("email") or "").lower())
        except Exception:
            continue
    return out


def record_bounces(*, days: int = 3) -> dict:
    """Scan recent mail for delivery failures and add the failed addresses to the suppression list.

    Only addresses we actually mailed are recorded — a bounce notice quotes several addresses
    (the sender, the postmaster, the original recipient), and suppressing all of them would
    blacklist innocent contacts.
    """
    from agents.monitoring.email_monitor import _build_gmail_service
    from agents.outreach.vendor_harvest import _throttled

    svc = _build_gmail_service()
    ours = _sent_addresses()
    known = load_bounced()
    found: set[str] = set()

    page = None
    while True:
        resp = _throttled(lambda: svc.users().messages().list(
            userId="me", q=f"newer_than:{days}d {_BOUNCE_QUERY}", maxResults=100,
            pageToken=page).execute())
        if resp is None:
            break
        for m in resp.get("messages", []):
            detail = _throttled(lambda mid=m["id"]: svc.users().messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["Subject", "From"]).execute())
            if detail is None:
                continue
            for addr in _EMAIL.findall(detail.get("snippet", "") or ""):
                low = addr.lower()
                if low in ours:
                    found.add(low)
        page = resp.get("nextPageToken")
        if not page:
            break

    addresses = sorted(known | found)
    _BOUNCED.write_text(json.dumps({
        "_comment": "Hard suppression list. Addresses that bounced — never mail these again. "
                    "Maintained by agents/outreach/deliverability.record_bounces().",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "addresses": addresses,
    }, indent=1) + "\n")
    logger.info("bounces_recorded", new=len(found - known), total=len(addresses))
    return {"new": len(found - known), "total": len(addresses),
            "sent_total": len(ours),
            "bounce_rate": round(len(addresses) / len(ours) * 100, 1) if ours else 0.0}
