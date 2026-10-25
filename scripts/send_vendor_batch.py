"""Send a small, hand-built batch of vendor emails from one manifest. Dry run unless --send.

One invocation sends the whole batch, by design: the 2026-10-13 incident came from chaining two
send commands on one shell line, which re-ran a 28-email batch. Looping inside a single process
removes that shape entirely. Guards match the daily sender - duplicate check against
data/outreach_sent_log.jsonl before each send, append after, and ~8s pacing.
"""
import argparse, base64, json, mimetypes, sys, time
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
    ap.add_argument("manifest")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    items = json.loads(Path(a.manifest).read_text())

    svc = None
    if a.send:
        from agents.monitoring.email_monitor import _build_gmail_service
        svc = _build_gmail_service()

    for i, it in enumerate(items, 1):
        to, subject = it["to"], it["subject"]
        body, att = Path(it["body"]).read_text(), Path(it["attach"])
        print(f"\n[{i}/{len(items)}] {to}\n  subject: {subject}\n  attach:  {att.name}")
        if not att.exists():
            print("  SKIP - attachment missing"); continue
        if already_sent(to, subject):
            print("  SKIP - already sent this subject to this address"); continue
        if not a.send:
            print(f"  DRY RUN - body {len(body.split())} words"); continue

        msg = EmailMessage()
        msg["To"], msg["From"], msg["Subject"] = to, _sender(), subject
        msg.set_content(body)
        ctype, _ = mimetypes.guess_type(att.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(att.read_bytes(), maintype=maintype, subtype=subtype, filename=att.name)
        sent = svc.users().messages().send(userId="me", body={
            "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}).execute()
        with LOG.open("a") as f:
            f.write(json.dumps({"to": to, "subject": subject,
                                "sent_at": datetime.now(timezone.utc).isoformat(),
                                "message_id": sent.get("id"), "attachment": att.name}) + "\n")
        print(f"  SENT - {sent.get('id')}")
        if i < len(items):
            time.sleep(8)
    print("\n" + ("done" if a.send else "[DRY RUN - pass --send to send]"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
