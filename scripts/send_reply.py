#!/usr/bin/env python3
"""Reply to one recruiter email, in-thread, with an attachment.

  python scripts/send_reply.py --query "from:recruiter@example.com" \
      --body reply.md --attach resume.pdf [--send]

Threads properly (In-Reply-To / References / same threadId) so the reply lands under the original
message rather than arriving as a new email — a recruiter working a requirement has the thread open,
and a detached reply loses that context.

Dry by default; `--send` is required to actually send.
"""
from __future__ import annotations

import argparse
import base64
import mimetypes
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True, help="Gmail search selecting the message to reply to")
    ap.add_argument("--body", required=True, help="path to the reply body (markdown/plain text)")
    ap.add_argument("--attach", default="", help="path to a file to attach")
    ap.add_argument("--subject", default="", help="override the reply subject")
    ap.add_argument("--send", action="store_true", help="actually send (otherwise dry run)")
    args = ap.parse_args()

    from agents.monitoring.email_monitor import _build_gmail_service
    from agents.outreach.vendor_harvest import _throttled
    import yaml

    svc = _build_gmail_service()
    found = _throttled(lambda: svc.users().messages().list(
        userId="me", q=args.query, maxResults=1).execute())
    if not found.get("messages"):
        print(f"no message matched: {args.query}")
        return 1
    mid = found["messages"][0]["id"]
    orig = _throttled(lambda: svc.users().messages().get(
        userId="me", id=mid, format="metadata",
        metadataHeaders=["From", "Subject", "Message-ID", "References"]).execute())
    # Gmail returns headers with their original casing ("Message-Id" vs "Message-ID"), so match
    # case-insensitively — a missed Message-ID silently breaks threading.
    h = {x["name"].lower(): x["value"] for x in orig["payload"]["headers"]}

    to = h.get("from", "")
    subject = args.subject or (h["subject"] if h.get("subject", "").lower().startswith("re:")
                               else f"RE: {h.get('subject', '')}".strip())
    profile = yaml.safe_load(Path("data/user_profile.yaml").read_text())
    sender = profile.get("personal", {}).get("email", "")

    body = Path(args.body).read_text()
    # The draft file carries a header block and trailing notes-to-self; send only the letter.
    if "\n---\n" in body:
        parts = body.split("\n---\n")
        body = parts[1] if len(parts) > 2 else parts[-1]
    body = body.strip()
    # The draft is markdown; this goes out as plain text, where **bold** renders as literal
    # asterisks and reads like an unfinished template.
    import re as _re
    body = _re.sub(r"\*\*(.+?)\*\*", r"\1", body)
    body = _re.sub(r"^#+\s*", "", body, flags=_re.M)

    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = sender
    msg["Subject"] = subject
    if h.get("message-id"):
        msg["In-Reply-To"] = h["message-id"]
        msg["References"] = (h.get("references", "") + " " + h["message-id"]).strip()
    msg.set_content(body)

    if args.attach:
        f = Path(args.attach)
        ctype, _ = mimetypes.guess_type(f.name)
        maintype, subtype = (ctype or "application/pdf").split("/", 1)
        msg.add_attachment(f.read_bytes(), maintype=maintype, subtype=subtype, filename=f.name)

    print(f"To:      {to}\nSubject: {subject}\nAttach:  {args.attach or '(none)'}")
    print(f"Thread:  {orig.get('threadId')}  (in-reply-to {h.get('message-id', '?')[:44]})")
    print("-" * 70)
    print(body[:700] + ("..." if len(body) > 700 else ""))
    if not args.send:
        print("\n[dry run — pass --send to send]")
        return 0

    sent = svc.users().messages().send(userId="me", body={
        "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode(),
        "threadId": orig.get("threadId"),
    }).execute()
    print(f"\nSENT — id {sent.get('id')}, thread {sent.get('threadId')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
