"""
Human-in-the-loop approval gates.
Agents call request_approval() and then await_decision() before taking irreversible actions.
Approvals are persisted to data/pending_approvals.json so the dashboard can surface them.
"""

import json
import asyncio
import uuid
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from core.logger import get_logger

logger = get_logger("checkpoints")

_APPROVALS_FILE = Path("data/pending_approvals.json")

# Gate definitions — timeout and default action if the user doesn't respond in time
APPROVAL_GATES: dict[str, dict] = {
    "submit_application": {
        "description": "About to submit job application",
        "display_fields": ["company", "role", "apply_url", "quality_score", "tailored_resume_preview"],
        "timeout_hours": 24,
        "default_on_timeout": "SKIP",
    },
    "send_linkedin_message": {
        "description": "About to send recruiter outreach message",
        "display_fields": ["recruiter_name", "company", "message_preview"],
        "timeout_hours": 12,
        "default_on_timeout": "SKIP",
    },
    "confirm_meeting": {
        "description": "About to confirm interview meeting",
        "display_fields": ["company", "interviewer", "proposed_time", "calendar_conflicts"],
        "timeout_hours": 6,
        "default_on_timeout": "HOLD",
    },
    "offer_received": {
        "description": "Job offer received — full user control required",
        "display_fields": ["full_offer_details", "comparison_analysis"],
        "timeout_hours": 0,      # Immediate, never auto-act
        "default_on_timeout": "HOLD",
    },
}


def _load_approvals() -> dict:
    if _APPROVALS_FILE.exists():
        try:
            return json.loads(_APPROVALS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_approvals(approvals: dict) -> None:
    _APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _APPROVALS_FILE.write_text(json.dumps(approvals, indent=2))


def _auto_approve_enabled() -> bool:
    return os.environ.get("AUTONOMOUSHIRE_AUTO_APPROVE", "0") == "1"


def request_approval(gate_type: str, payload: dict) -> str:
    """
    Register a pending approval. Returns the approval_id.
    The agent should then call await_decision(approval_id) to block until resolved.
    """
    if gate_type not in APPROVAL_GATES:
        raise ValueError(f"Unknown gate type: {gate_type}")

    gate = APPROVAL_GATES[gate_type]
    approval_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Auto-approve: skip the gate entirely, write record as already approved
    if _auto_approve_enabled() and gate_type == "submit_application":
        record = {
            "id": approval_id,
            "gate_type": gate_type,
            "description": gate["description"],
            "payload": payload,
            "created_at": now.isoformat(),
            "expires_at": None,
            "status": "approved",
            "resolved_at": now.isoformat(),
            "auto_approved": True,
        }
        approvals = _load_approvals()
        approvals[approval_id] = record
        _save_approvals(approvals)
        logger.log_decision(
            action="approval_auto_approved",
            outcome="approved",
            details={"gate_type": gate_type, "approval_id": approval_id},
        )
        return approval_id

    timeout_h = gate["timeout_hours"]
    expires_at = (now + timedelta(hours=timeout_h)).isoformat() if timeout_h > 0 else None

    record = {
        "id": approval_id,
        "gate_type": gate_type,
        "description": gate["description"],
        "payload": payload,
        "created_at": now.isoformat(),
        "expires_at": expires_at,
        "status": "pending",   # pending | approved | rejected | expired
        "default_on_timeout": gate["default_on_timeout"],
    }

    approvals = _load_approvals()
    approvals[approval_id] = record
    _save_approvals(approvals)

    logger.log_decision(
        action="approval_requested",
        outcome="pending",
        details={"gate_type": gate_type, "approval_id": approval_id},
    )
    return approval_id


async def await_decision(
    approval_id: str,
    poll_interval_seconds: float = 5.0,
) -> str:
    """
    Poll until the approval is resolved (approved / rejected / expired).
    Returns the final status string.
    """
    while True:
        approvals = _load_approvals()
        record = approvals.get(approval_id)

        if record is None:
            logger.error("approval_not_found", approval_id=approval_id)
            return "rejected"

        status = record["status"]

        if status in ("approved", "rejected"):
            logger.log_decision(
                action="approval_resolved",
                outcome=status,
                details={"approval_id": approval_id, "gate_type": record["gate_type"]},
            )
            return status

        # Check for expiry
        if record.get("expires_at"):
            expires = datetime.fromisoformat(record["expires_at"])
            if datetime.now(timezone.utc) > expires:
                record["status"] = "expired"
                approvals[approval_id] = record
                _save_approvals(approvals)
                default = record.get("default_on_timeout", "SKIP")
                logger.log_decision(
                    action="approval_expired",
                    outcome=default,
                    details={"approval_id": approval_id},
                )
                # Map default action to a status the caller can act on
                return "rejected" if default == "SKIP" else "hold"

        await asyncio.sleep(poll_interval_seconds)


def resolve_approval(approval_id: str, decision: str) -> bool:
    """
    Called by the dashboard or CLI to approve/reject a pending gate.
    decision: "approved" | "rejected"
    """
    approvals = _load_approvals()
    if approval_id not in approvals:
        return False

    approvals[approval_id]["status"] = decision
    approvals[approval_id]["resolved_at"] = datetime.now(timezone.utc).isoformat()
    _save_approvals(approvals)

    logger.log_decision(
        action="approval_manual_resolve",
        outcome=decision,
        details={"approval_id": approval_id},
    )
    return True


def resolve_approval_by_apply_url(apply_url: str, decision: str) -> str | None:
    """Resolve the pending `submit_application` gate whose payload matches `apply_url`.

    The UI/DB keys applications by `ats_url`, while checkpoints key by the payload's
    `apply_url` — this bridges a UI approve/reject to the gate the submit machinery
    actually reads. Returns the resolved approval_id, or None if no pending gate matched.
    """
    if not apply_url:
        return None
    approvals = _load_approvals()
    # Prefer the most recently created pending gate for this URL.
    candidates = [
        (aid, r) for aid, r in approvals.items()
        if r.get("status") == "pending"
        and r.get("gate_type") == "submit_application"
        and (r.get("payload") or {}).get("apply_url") == apply_url
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[1].get("created_at") or "", reverse=True)
    aid = candidates[0][0]
    resolve_approval(aid, decision)
    return aid


def approval_status_for_apply_url(apply_url: str) -> str | None:
    """Status of the most-recent `submit_application` gate for this apply_url
    ("pending" | "approved" | "rejected" | "expired"), or None if no gate exists.
    Lets the submitter decide whether to park, submit, or skip a given application."""
    if not apply_url:
        return None
    approvals = _load_approvals()
    candidates = [
        r for r in approvals.values()
        if r.get("gate_type") == "submit_application"
        and (r.get("payload") or {}).get("apply_url") == apply_url
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return candidates[0].get("status")


def get_pending_approvals() -> list[dict]:
    """Return all approvals still waiting for user action."""
    approvals = _load_approvals()
    return [r for r in approvals.values() if r["status"] == "pending"]


def get_all_approvals() -> list[dict]:
    return list(_load_approvals().values())
