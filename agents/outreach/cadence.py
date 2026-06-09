"""
LinkedIn Outreach Cadence — 6-stage state machine.

Stages:
  0  none              — contact just added, no action yet
  1  connect_sent      — connection request sent; wait for acceptance
  2  connected         — accepted; compose + send thanks message
  3  thanks_sent       — thanks sent; wait 3 days then send ask
  4  ask_sent          — ask sent; wait 7 days for reply before followup
  5  followup_sent     — final followup sent; wait 7 more days then archive
  6  archived          — done (replied, declined, timed out)

Transitions are driven by cadence_stage and next_action_at datetime.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from core.llm import get_llm
from core.logger import get_logger
from core.state import LinkedInContact, LinkedInMessage

logger = get_logger("cadence")

_PROMPTS = {
    "connection_request": Path("prompts/outreach/connection_request.txt").read_text(),
    "thanks_message":     Path("prompts/outreach/thanks_message.txt").read_text(),
    "ask_message":        Path("prompts/outreach/ask_message.txt").read_text(),
    "followup_message":   Path("prompts/outreach/followup_message.txt").read_text(),
}

# Delays between stages (in hours, with ±20% jitter applied at call site)
_STAGE_DELAYS_HOURS = {
    1: 0,     # send connection immediately once scheduled
    2: 0,     # send thanks as soon as connection accepted
    3: 72,    # 3 days after thanks before ask
    4: 168,   # 7 days after ask before followup
    5: 168,   # 7 days after followup then archive
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future_iso(hours: float) -> str:
    import random
    jittered = hours * random.uniform(0.8, 1.2)
    dt = datetime.now(timezone.utc) + timedelta(hours=jittered)
    return dt.isoformat()


def _is_due(contact: LinkedInContact) -> bool:
    """True if next_action_at is in the past (or not set)."""
    nat = contact.get("next_action_at")
    if not nat:
        return True
    try:
        due = datetime.fromisoformat(nat)
        return datetime.now(timezone.utc) >= due
    except ValueError:
        return True


def _first_name(contact: LinkedInContact) -> str:
    return (contact["full_name"] or "").split()[0] or "there"


async def draft_message(
    contact: LinkedInContact,
    stage_name: str,
    extra_context: dict | None = None,
) -> str:
    """Generate a message for the given stage using Haiku."""
    llm = get_llm("cadence", model="claude-haiku-4-5")
    ctx = extra_context or {}

    inputs_map = {
        "connection_request": {
            "contact_name": _first_name(contact),
            "contact_title": contact["title"],
            "company": contact["company"],
            "mutual_context": ctx.get("mutual_context", f"your work on the {contact['company']} data platform"),
        },
        "thanks_message": {
            "contact_name": _first_name(contact),
            "contact_title": contact["title"],
            "company": contact["company"],
            "one_interesting_thing": ctx.get("interesting_thing", f"the scale of {contact['company']}'s infrastructure"),
        },
        "ask_message": {
            "contact_name": _first_name(contact),
            "company": contact["company"],
            "job_title": ctx.get("job_title", ""),
            "prior_topic": ctx.get("prior_topic", "your engineering culture"),
        },
        "followup_message": {
            "contact_name": _first_name(contact),
            "company": contact["company"],
            "original_ask": ctx.get("original_ask", "a quick 15-min call to learn about the team"),
        },
    }

    inputs = inputs_map.get(stage_name, {})
    user_content = "\n".join(f"{k}: {v}" for k, v in inputs.items())

    result = await llm.chat_json(
        _PROMPTS[stage_name],
        [{"role": "user", "content": user_content}],
        max_tokens=512,
    )
    return result.get("note") or result.get("message") or ""


def advance_stage(
    contact: LinkedInContact,
    new_stage: int,
    message_body: str = "",
    stage_label: str = "",
    status: str = "drafted",
) -> LinkedInContact:
    """
    Return a mutated copy of contact advanced to new_stage.
    Appends message to history if body provided.
    """
    updated = dict(contact)
    updated["cadence_stage"] = new_stage
    updated["last_action_at"] = _now_iso()

    # Set next_action_at based on stage delay
    delay_h = _STAGE_DELAYS_HOURS.get(new_stage + 1, 0)
    updated["next_action_at"] = _future_iso(delay_h) if delay_h else None

    if message_body:
        msg: LinkedInMessage = {
            "stage": stage_label or f"stage_{new_stage}",
            "body": message_body,
            "sent_at": None,   # filled in by outreach_runner after actual send
            "status": status,
        }
        updated["messages"] = list(contact.get("messages", [])) + [msg]

    return updated


def should_archive(contact: LinkedInContact) -> bool:
    """True if contact has timed out at the followup stage."""
    if contact["cadence_stage"] != 5:
        return False
    return _is_due(contact)


def contacts_ready_for_action(
    contacts: list[LinkedInContact],
) -> list[LinkedInContact]:
    """Filter to contacts that are due for their next action."""
    return [
        c for c in contacts
        if c["cadence_stage"] < 6 and _is_due(c)
    ]
