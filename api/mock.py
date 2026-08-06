"""
Mock-interview routes — the embedded "AI Interviewer" surface of the dashboard (/mock), plus a
shared context endpoint the standalone Live Meeting Assistant pulls from.

Native to AutonomousHire (uses core.llm + the experience bank) so it works without the LMA backend.
Grounding = the FULL experience bank + per-company prep brief + profile + role/JD, assembled by
agents.application.interview_context and passed as the system prompt (core.llm caches it as one
ephemeral block, so it's reused cheaply across every turn).
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from agents.application.interview_context import (
    ANSWER_AS_ME_SYSTEM,
    INTERVIEWER_SYSTEM,
    LINUX_INTERVIEWER_SYSTEM,
    SYSTEM_DESIGN_INTERVIEWER_SYSTEM,
    assemble_interview_context,
)
from core.logger import get_logger

router = APIRouter(prefix="/mock", tags=["mock"])
logger = get_logger("mock")


class MockRequest(BaseModel):
    company: str = ""
    role: str = ""
    jd: str = ""
    history: str = ""   # "Interviewer: …\nCandidate: …" transcript so far (capped client-side)
    mode: str = "behavioral"   # "behavioral" (mixed) | "system_design" (architecture) | "linux" (troubleshooting)
    fault: str = ""            # linux mode only: the hidden root cause to simulate (steers away from the
                               # model's cert-fault prior). Picked once per session client-side, sent every turn.


class ContextResponse(BaseModel):
    context: str
    found_brief: bool


@router.get("/context", response_model=ContextResponse)
def context(company: str = "", role: str = "", jd: str = "") -> dict:
    """The full assembled interview grounding for a company/role — bank + brief + profile + role/JD.
    The standalone LMA fetches this once per session and passes it to its coach/interviewer."""
    return assemble_interview_context(company, role, jd)


@router.post("/interviewer")
async def interviewer(req: MockRequest) -> dict:
    """The interviewer's next turn (one question), grounded in the candidate's full background."""
    ctx = assemble_interview_context(req.company, req.role, req.jd)["context"]
    persona = {
        "system_design": SYSTEM_DESIGN_INTERVIEWER_SYSTEM,
        "linux": LINUX_INTERVIEWER_SYSTEM,
    }.get(req.mode, INTERVIEWER_SYSTEM)
    # Steer the linux sim's hidden fault (the model otherwise always picks the cert/config fault). The
    # client picks one at random per session and sends it every turn so the fault stays fixed.
    if req.mode == "linux" and req.fault.strip():
        persona += (
            f"\n\nFOR THIS SESSION, use EXACTLY this hidden root cause: {req.fault.strip()}. Do not pick "
            f"another; keep every command's output consistent with it; never reveal or name it directly — "
            f"the candidate must diagnose it. Commands that wouldn't expose this fault return healthy output."
        )
    system = persona + "\n\n" + ctx
    # Linux mode plays the terminal too — journalctl/df dumps need more room than a single question.
    max_tokens = 700 if req.mode == "linux" else 400
    prompt = (
        "Give your next turn: if the candidate's last line is a shell command, return the box's terminal "
        "output for it (raw text, consistent with the hidden fault); otherwise respond as the interviewer."
        if req.mode == "linux"
        else "Give your next interviewer turn (one question)."
    )
    user = f"CONVERSATION SO FAR:\n{req.history or '(none yet — open the interview)'}\n\n{prompt}"
    from core.llm import get_llm
    llm = get_llm("mock_interviewer", model="claude-sonnet-4-6")
    text = await llm.chat(system=system, messages=[{"role": "user", "content": user}], max_tokens=max_tokens)
    return {"question": (text or "").strip()}


@router.post("/answer")
async def answer(req: MockRequest) -> dict:
    """Model a strong first-person 'answer-as-me' to the latest interviewer question — shows the
    candidate what a great grounded answer sounds like. `history` should end with the question."""
    ctx = assemble_interview_context(req.company, req.role, req.jd)["context"]
    system = ANSWER_AS_ME_SYSTEM + "\n\n" + ctx
    user = (
        f"CONVERSATION SO FAR:\n{req.history or '(the question is below)'}\n\n"
        f"Answer the interviewer's most recent question as the candidate, out loud, in first person."
    )
    from core.llm import get_llm
    llm = get_llm("mock_interviewer", model="claude-sonnet-4-6")
    text = await llm.chat(system=system, messages=[{"role": "user", "content": user}], max_tokens=600)
    return {"answer": (text or "").strip()}
