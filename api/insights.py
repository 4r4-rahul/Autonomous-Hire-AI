"""
Learning-insights routes — surface the conversion analytics + outcome learnings
produced by `agents/learning/feedback_loop.py` (data/learning_insights.json).

Read-only: the file is regenerated every email_monitor tick (capture → learn).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/insights", tags=["insights"])

_REPORT_JSON = Path(__file__).parent.parent / "data" / "learning_insights.json"
_PREP_DIR = (Path(__file__).parent.parent / "data" / "interview_prep").resolve()


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
            "by_source": {}, "by_ats": {}, "learnings_written": [], "interviews": [],
        }
    try:
        report = json.loads(_REPORT_JSON.read_text())
        report["available"] = True
        return report
    except Exception as e:
        return {"available": False, "error": str(e)}


@router.get("/prep", response_class=PlainTextResponse)
def get_prep(company: str) -> str:
    """Serve the interview-prep brief for a company as markdown text. The file path is looked up
    server-side from meetings_scheduled (never taken from the client) and confined to
    data/interview_prep/, so there's no path-traversal surface."""
    from core.state_store import load_state
    co = (company or "").strip().lower()
    meeting = next((m for m in load_state().get("meetings_scheduled", []) or []
                    if (m.get("company_name") or "").strip().lower() == co and m.get("prep_doc_path")), None)
    if not meeting:
        raise HTTPException(status_code=404, detail="No prep brief for this company")
    path = Path(meeting["prep_doc_path"]).resolve()
    # Defense in depth: the stored path must live under data/interview_prep/ and exist.
    if _PREP_DIR not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Prep brief file not found")
    return path.read_text()
