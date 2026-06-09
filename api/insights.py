"""
Learning-insights routes — surface the conversion analytics + outcome learnings
produced by `agents/learning/feedback_loop.py` (data/learning_insights.json).

Read-only: the file is regenerated every email_monitor tick (capture → learn).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/insights", tags=["insights"])

_REPORT_JSON = Path(__file__).parent.parent / "data" / "learning_insights.json"


@router.get("")
def get_insights() -> dict:
    """Return the latest learning-insights report, or an empty shell if the
    learning loop hasn't run yet (so the UI can render a friendly empty state)."""
    if not _REPORT_JSON.exists():
        return {
            "available": False,
            "generated_at": None,
            "totals": {
                "submitted": 0, "responded": 0, "interviews_or_offers": 0,
                "ghosted": 0, "pending": 0, "ghosted_this_run": 0,
                "response_rate": 0.0, "interview_rate": 0.0,
                "has_response_signal": False,
            },
            "by_company": {}, "by_role": {}, "by_fit_band": {},
            "by_source": {}, "by_ats": {}, "learnings_written": [],
        }
    try:
        report = json.loads(_REPORT_JSON.read_text())
        report["available"] = True
        return report
    except Exception as e:
        return {"available": False, "error": str(e)}
