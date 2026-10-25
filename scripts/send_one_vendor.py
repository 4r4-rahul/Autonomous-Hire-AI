"""Send ONE résumé to ONE named vendor contact. Dry run unless --send.

Separate from vendor_daily_send.py (which works a queue) because a single reply to a named recruiter
needs its own subject and body. Same guards as the batch sender:
  * checks data/outreach_sent_log.jsonl first - the duplicate incident on 2026-10-13 sent 28 vendors
    three copies each, so nothing goes out without a recipient+subject check;
  * appends to that log only on a real send;
  * must run from the repo root, or core.config reads the wrong .env and the refresh token "vanishes".
"""
import argparse, base64, json, mimetypes, sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

def _sender() -> str:
    """Sender address comes from the untracked profile (data/user_profile.yaml), never from code."""
    import yaml
    return yaml.safe_load(Path("data/user_profile.yaml").read_text())["personal"]["email"]

sys.path.insert(0, str(Path(__file__).parent.parent))
LOG = Path("data/outreach_sent_log.jsonl")


def already_sent(to: str, subject: str) -> bool:
    if not LOG.exists():
        return False
    for line in LOG.read_text().splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if (d.get("to") or d.get("recipient", "")).lower() == to.lower() \
           and (d.get("subject") or "") == subject:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body", required=True, help="path to the body text file")
    ap.add_argument("--attach", required=True)
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()

    body = Path(a.body).read_text()
    att = Path(a.attach)
    if not att.exists():
        print(f"attachment missing: {att}")
        return 1

    if already_sent(a.to, a.subject):
        print(f"ALREADY SENT to {a.to} with this subject - refusing to duplicate.")
        return 1

    msg = EmailMessage()
    msg["To"] = a.to
    msg["From"] = _sender()
    msg["Subject"] = a.subject
    msg.set_content(body)
    ctype, _ = mimetypes.guess_type(att.name)
    maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
    msg.add_attachment(att.read_bytes(), maintype=maintype, subtype=subtype, filename=att.name)

    print(f"To:      {a.to}\nFrom:    {_sender()}\nSubject: {a.subject}")
    print(f"Attach:  {att.name}  ({att.stat().st_size:,} bytes)")
    print("-" * 72)
    print(body)
    print("-" * 72)
    if not a.send:
        print("[DRY RUN - pass --send to actually send]")
        return 0

    from agents.monitoring.email_monitor import _build_gmail_service
    svc = _build_gmail_service()
    sent = svc.users().messages().send(userId="me", body={
        "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}).execute()
    with LOG.open("a") as f:
        f.write(json.dumps({"to": a.to, "subject": a.subject,
                            "sent_at": datetime.now(timezone.utc).isoformat(),
                            "message_id": sent.get("id"), "attachment": att.name}) + "\n")
    print(f"SENT - gmail id {sent.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
