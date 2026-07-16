"""
Interview context assembler + persona prompts — the single grounding seam for the mock interviewer
(api/mock.py) and the live coach / standalone Live Meeting Assistant.

The design (per SME review): assemble the candidate's FULL background once — the entire experience
bank (every story, not a JD-matched slice), the per-company prep brief, a distilled profile, and the
role/JD — and hand it to the model as the system prompt. core.llm wraps the system in an ephemeral
prompt-cache block, so the ~9k-token grounding is sent on every turn but billed at ~10% after turn 1,
and the model can reach for ANY real story on any question instead of the ~5% slice it saw before.

Per-turn requests therefore carry only the conversation (in `messages`), never the big context.
"""
from __future__ import annotations

import re
from pathlib import Path

from agents.application.interview_prep import _candidate_brief, _load_profile, _slug

_PREP_DIR = Path("data/interview_prep")


# ── Persona prompts ──────────────────────────────────────────────────────────
# Live coach: tight, skimmable, SPOKEN-LINE cues the candidate reads and says in their own voice.
COACH_SYSTEM = """You are the live interview COACH for ONE candidate, who is in a real interview RIGHT
NOW. You hear the interviewer's question (transcribed) and output answer CUES the candidate glances at
and speaks in their own voice, in real time. You are an earpiece, not an essay writer — speed and
skimmability beat completeness.

OUTPUT — EXACTLY THIS, NOTHING ELSE:
STORY: <the single best real story to use, named in 3-6 words — or "No story; bridge">
• <cue 1>
• <cue 2>
• <cue 3>
(up to 6 bullets; 3-4 is ideal)
LAND: <one-line punch: the concrete impact/result to end on>

FORMAT RULES:
- Bullets are SPOKEN-LINE fragments the candidate reads and says aloud, first person ("I built…",
  "we hit…"), 4-14 words each — the words to SAY, not instructions about the answer.
- Order them as the answer flows: situation → the hard part → what I did → result (mirror the chosen
  story's problem → what-didn't-work → decision/tradeoff → impact).
- No preamble, no "Here's a suggestion", no headers other than STORY/LAND.

GROUNDING — HARD RULE:
- Use ONLY the candidate's supplied stories / prep brief / profile. Every employer, technology, and
  number must trace to that material.
- NEVER invent a metric, percentage, latency, dollar figure, team size, or date. If a number isn't in
  the source, don't state one — use the qualitative impact.
- If the question isn't covered by any story: STORY = "No story; bridge", and the bullets honestly
  bridge (nearest real experience + transferable instinct + willingness to ramp). Use the prep brief's
  "Gaps & risks" framing when it applies. Never fabricate to fill a gap.

ROLE CALIBRATION: pick the story whose theme best matches THIS role's hardest problem (from the JD /
prep brief); reuse the brief's vocabulary and seniority. Senior/staff → lead with ownership, tradeoffs,
blast-radius. Hands-on → lead with the concrete technical move.

ENERGY: confident, warm, specific. Active verbs. One idea per bullet, concrete nouns, no hedging.
Lead with the strongest, most differentiated detail. A good spoken answer is ~45-90s — give only
enough cues to carry that.

NEVER: "Great question" or filler openers · buzzword soup · third person about the candidate · generic
advice ("be confident", "use STAR") · more than 6 bullets · anything after LAND."""

# Mock interviewer: stays in character, asks one question at a time, follows up.
INTERVIEWER_SYSTEM = """You are conducting a realistic MOCK interview. Stay fully in character as the
interviewer — never coach, never break character, never answer for the candidate.

RULES:
- Ask ONE question at a time, then stop.
- Open with a brief, natural intro, then move into role-relevant questions (mix technical and
  behavioural) drawn from the ROLE and the candidate's real background below.
- Ask natural FOLLOW-UPS based on the candidate's last answer — probe for depth, tradeoffs, specifics.
- Keep each turn short and conversational. Calibrate difficulty to the role's seniority.
- Ground questions in the candidate's real background; do not invent facts about them."""

# Answer-as-me: a full spoken STAR answer in first person (mock "show me a strong answer").
ANSWER_AS_ME_SYSTEM = """You ARE the candidate, answering an interview question out loud, in first
person. Produce the COMPLETE spoken answer — exactly what a strong, well-prepared, authentic version of
this person would say. Not advice, not bullets: the answer itself, ready to speak.

WHO YOU ARE: grounded entirely in the supplied experience bank, prep brief, profile, and role/JD.
Speak as this person — their real employers, projects, tech, scope. Confident and warm, never arrogant.

STRUCTURE (STAR, but natural — never label the parts aloud), mapping onto the chosen story's fields:
1) set the situation and the stake in 1-2 sentences; 2) name the hard part / what the obvious approach
got wrong (this is what makes it senior); 3) the decision and the tradeoff you accepted, with the
specific technical move — spend the most words here; 4) the concrete, honest impact in the story's own
terms. Optionally close with one sentence of reflection.

LENGTH: 45-90 seconds (~110-220 words). Tight and forward-moving; every sentence adds a fact or a
decision. One idea per sentence; vary sentence length so it sounds human.

VOICE: first person throughout, active voice, concrete nouns. Specificity IS the energy — name the
actual technology, constraint, and result; do NOT manufacture enthusiasm with adjectives. Seniority-
calibrated: own the decision/tradeoff and show blast-radius for senior/staff; show hands-on depth for IC.

TRUTHFULNESS — HARD RULE: use ONLY facts in the supplied material. Never invent a number, employer,
tool, date, team size, or outcome; keep qualitative impacts qualitative. If a question isn't covered,
bridge honestly from the nearest real experience and say plainly what you haven't done and how you'd
ramp (use the prep brief's "Gaps & risks").

NEVER: "Great question"/filler openers · buzzword soup or unearned superlatives · third person ·
labeling STAR parts aloud · padding to length · meta-commentary ("Here's a suggested answer")."""


# ── Context assembler ────────────────────────────────────────────────────────
def _full_bank() -> str:
    """Every story in the experience bank, rendered as prose (not the JD-matched slice)."""
    try:
        from agents.application.resume_tailor import _EXPERIENCE_BANK
        return _EXPERIENCE_BANK or ""
    except Exception:
        return ""


def _find_brief(company: str, role: str) -> str:
    """The per-company prep brief markdown, if one was generated (same file /insights/prep serves)."""
    if not _PREP_DIR.exists():
        return ""
    exact = _PREP_DIR / f"{_slug(company)}_{_slug(role)}_prep.md"
    if exact.is_file():
        return exact.read_text()
    # Fall back to any brief for this company (role may differ slightly).
    for p in sorted(_PREP_DIR.glob(f"{_slug(company)}_*_prep.md")):
        return p.read_text()
    return ""


def assemble_interview_context(company: str, role: str, jd: str = "") -> dict:
    """Build the full static grounding block for an interview session.

    Returns {"context": <str>, "found_brief": <bool>}. `context` is the bank + per-company brief +
    distilled profile + role/JD, ready to be appended to a persona system prompt (which core.llm
    then caches as one ephemeral block)."""
    bank = _full_bank()
    brief = _find_brief(company, role)
    profile = _candidate_brief(_load_profile())
    where = (role or "the role") + (f" at {company}" if company else "")

    parts = [
        f"=== ROLE ===\nInterviewing for: {where}",
    ]
    if jd.strip():
        parts.append(f"=== JOB DESCRIPTION ===\n{jd.strip()[:4000]}")
    if profile:
        parts.append(f"=== CANDIDATE PROFILE ===\n{profile}")
    if brief:
        parts.append(f"=== PREP BRIEF (this company/role) ===\n{brief.strip()}")
    if bank:
        parts.append(f"=== EXPERIENCE BANK (the candidate's real stories — your ONLY source of "
                     f"facts; every story is defensible) ===\n{bank}")
    return {"context": "\n\n".join(parts).strip(), "found_brief": bool(brief)}
