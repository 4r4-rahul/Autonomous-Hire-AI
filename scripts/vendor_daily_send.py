#!/usr/bin/env python3
"""Send the next daily batch of the vendor "open to opportunities" email.

    python scripts/vendor_daily_send.py            # dry run: shows what today's batch would be
    python scripts/vendor_daily_send.py --send     # sends it

Reads data/vendor_queue.json (warm contacts first, then cold) and protects the inbox:
  - never re-mails anyone in data/outreach_sent_log.jsonl (recipient + subject);
  - stops at DAILY_BULK_CAP sends per rolling 24h, so Rahul keeps 140+/day for manual email;
  - before sending, reads new bounce notices: "address not found" drops that address,
    "message blocked" drops the whole domain;
  - refuses to send when bounces over the last 24h exceed 10% of sent mail.
Run from the repo root (core.config reads .env relative to the working directory).
"""
from __future__ import annotations

import base64
import json
import re
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

DAILY_BULK_CAP = 360          # Gmail allows ~500/24h; 140 stay free for manual sends
MAX_BOUNCE_RATE = 0.10
PAUSE_SECONDS = 8
QUEUE = Path("data/vendor_queue.json")
LOG = Path("data/outreach_sent_log.jsonl")
SUPPRESSION = Path("data/email_suppression.json")   # never mail these (bounced/blocked)
import os
RESUME = Path(os.environ["VENDOR_RESUME_PATH"]) if os.environ.get("VENDOR_RESUME_PATH") else Path("data/vendor_resume.pdf")  # local path, set in .env

def _profile() -> dict:
    import yaml
    return yaml.safe_load(Path("data/user_profile.yaml").read_text())["personal"]

def _subject(per: dict) -> str:
    return f"Senior Data Engineer available for new opportunities - {per['name']} (H1B, 10+ Yrs)"

def _body(per: dict) -> str:
    return f"""Hi,

I'm currently looking for new opportunities and would be a strong fit for Senior Data Engineer roles you're working on.

I'm a Senior Data Engineer at JPMorgan Chase with 10+ years building data platforms in banking, financial regulation, healthcare and state government (Georgia Department of Banking and Finance). I was technical lead for a team of 12 engineers.

- Core stack: AWS (S3, EMR, Glue, Lambda, Step Functions, Redshift, Athena), Snowflake, Databricks and Delta Lake, PySpark, Kafka, Airflow, dbt and Terraform
- Also: Azure (Data Factory, Databricks) and GCP (BigQuery, Dataflow), Informatica, DataStage and Oracle PL/SQL
- GenAI: RAG, LLM pipelines and agentic workflows on top of these platforms
- Visa: H1B | Location: {per.get('location', 'Plano, TX')}, open to relocation, onsite, hybrid or remote | C2C or W2

My résumé is attached. Please keep me in mind for Senior Data Engineer, AWS, Snowflake or Databricks roles, and check with me before submitting me anywhere so we avoid duplicate submissions.

Thanks,
{per['name']}
{per['phone']} | {per['email']}
{per.get('linkedin', '')}

If you'd rather not receive emails like this, just reply and I won't contact you again.
"""

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_BOUNCE_Q = ('(from:mailer-daemon OR from:postmaster) newer_than:1d (subject:"Delivery Status Notification" '
             'OR subject:Undeliverable OR subject:"Mail delivery failed" OR subject:"Address not found")')


def _count(svc, q: str) -> int:
    n, page = 0, None
    while True:
        r = svc.users().messages().list(userId="me", q=q, maxResults=500, pageToken=page).execute()
        n += len(r.get("messages", [])); page = r.get("nextPageToken")
        if not page:
            return n


def _sent_pairs() -> set[tuple[str, str]]:
    if not LOG.exists():
        return set()
    return {(d["to"].lower(), d["subject"]) for d in map(json.loads, LOG.read_text().splitlines()) if d}


def _absorb_bounces(svc, queue: dict) -> int:
    """Turn today's bounce notices into dead addresses / blocked domains. Returns bounce count."""
    msgs = svc.users().messages().list(userId="me", q=_BOUNCE_Q, maxResults=500).execute().get("messages", [])
    dead, blocked = set(queue["dead_addresses"]), set(queue["blocked_domains"])
    for m in msgs:
        snip = svc.users().messages().get(userId="me", id=m["id"], format="metadata").execute().get("snippet", "")
        addrs = [a.lower() for a in _EMAIL.findall(snip)]
        if not addrs:
            continue
        if "blocked" in snip.lower():
            blocked.add(addrs[0].split("@")[1])
        else:
            dead.add(addrs[0])
    queue["dead_addresses"], queue["blocked_domains"] = sorted(dead), sorted(blocked)
    return len(msgs)


def main() -> int:
    send = "--send" in sys.argv
    import yaml
    from agents.monitoring.email_monitor import _build_gmail_service

    per = _profile(); sender = per["email"]
    SUBJECT, BODY = _subject(per), _body(per)
    assert RESUME.exists(), RESUME
    queue = json.loads(QUEUE.read_text())
    svc = _build_gmail_service()

    bounces = _absorb_bounces(svc, queue)
    QUEUE.write_text(json.dumps(queue, indent=1))
    sent_24h = _count(svc, "in:sent newer_than:1d")
    rate = bounces / sent_24h if sent_24h else 0.0
    print(f"last 24h: sent {sent_24h}, bounces {bounces} ({rate:.0%}); "
          f"blocked domains {len(queue['blocked_domains'])}, dead addresses {len(queue['dead_addresses'])}")
    if rate > MAX_BOUNCE_RATE:
        print("STOP: bounce rate above 10%. Review the bounces before sending more.")
        return 1

    done = _sent_pairs()
    # The permanent do-not-send list (bounced or blocked addresses) always wins over the queue.
    sup = json.loads(SUPPRESSION.read_text()) if SUPPRESSION.exists() else {"addresses": {}, "blocked_domains": []}
    skip_dom = tuple("@" + d for d in set(queue["blocked_domains"]) | set(sup["blocked_domains"]))
    dead = set(queue["dead_addresses"]) | set(sup["addresses"])
    pending = [e for e in queue["warm"] + queue["cold"]
               if (e, SUBJECT) not in done and e not in dead and not e.endswith(skip_dom)]
    room = max(0, DAILY_BULK_CAP - sent_24h)
    batch = pending[:room]
    print(f"pending {len(pending)} | room today {room} (cap {DAILY_BULK_CAP}, 140+ kept for manual) | "
          f"this batch {len(batch)} | left after today {len(pending) - len(batch)}")
    if not send or not batch:
        print("dry run" if not send else "nothing to send today")
        return 0

    pdf = RESUME.read_bytes()
    for i, to in enumerate(batch, 1):
        m = EmailMessage(); m["To"] = to; m["From"] = sender; m["Subject"] = SUBJECT
        m.set_content(BODY)
        m.add_attachment(pdf, maintype="application", subtype="pdf", filename=RESUME.name)
        try:
            r = svc.users().messages().send(
                userId="me", body={"raw": base64.urlsafe_b64encode(m.as_bytes()).decode()}).execute()
            with LOG.open("a") as f:
                f.write(json.dumps({"to": to, "subject": SUBJECT,
                                    "sent_at": datetime.now(timezone.utc).isoformat()}) + "\n")
            print(f"[{i}/{len(batch)}] SENT {to} {r.get('id')}", flush=True)
        except Exception as e:      # a timeout may still have delivered; it is NOT retried automatically
            print(f"[{i}/{len(batch)}] FAIL {to} {e}", flush=True)
        time.sleep(PAUSE_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
