"""Deterministic truth/quality scrubs for generated résumé + cover text.

Prompt-only guards do NOT reliably stop two model habits (worse on DeepSeek than Opus, but present
on both), so we post-process the output and drop offending sentences:

  1. Vague-magnitude fabrication — "processes millions of records daily" (an invented throughput the
     model pattern-completes even when the prompt forbids that exact phrase).
  2. Meta-commentary / cover-letter language in the RÉSUMÉ BODY — "exactly what Acme needs", "ready to
     bring that discipline to Acme", "excited to join", "perfect fit" (Rule 3: the résumé states
     evidence; it never pitches the company — that's the cover letter's job).

Single source of truth: used by the résumé tailor, cover letters (fabrication only), and interview
prep, so a violation can't leak through any generation path. See feedback_truth_guard_scrub and
feedback_resume_quality_rules (Rule 3).
"""
from __future__ import annotations

import re

_FAB_VOLUME = re.compile(
    r"\b(millions|billions|hundreds of millions|tens of millions|hundreds of thousands|thousands)\s+of\s+"
    r"(?:[a-z]+(?:-[a-z]+)?\s+){0,3}"   # up to 3 adjectives, e.g. "millions of [daily] [concurrent] requests"
    r"(records|rows|requests|events|transactions|messages|users|customers|queries|data ?points|"
    r"jobs|API calls|api calls|operations|writes|reads|documents|files|payments|trades|signals|logs)\b",
    re.I,
)

# Cover-letter / meta-commentary that must not appear in the résumé body (Rule 3). Sentence-scoped:
# a matching sentence is dropped whole. Patterns are chosen to be specific enough not to hit ordinary
# résumé prose ("brought the system to production" is safe; "ready to bring that to Acme" is not).
_META = re.compile(
    r"(?:\b(?:ready|eager|excited|thrilled|keen|poised|looking\s+forward)\s+to\s+"
    r"(?:bring|apply|join|contribute|help|deliver|make|work|tackle|drive))"
    r"|(?:\b(?:genuinely|truly|really)\s+excited\b)"
    r"|(?:\bpassionate\s+about\b)"
    r"|(?:\bperfect\s+(?:fit|match)\s+for\b)"
    r"|(?:\bwell[-\s]positioned\s+to\b)"
    r"|(?:\bexactly\s+what\b.{0,50}?\b(?:need|needs|looking\s+for|want|wants)\b)"
    r"|(?:\bthe\s+exact\s+(?:kind|type)\b.{0,60}?\b(?:need|needs|looking\s+for)\b)"
    r"|(?:\bthe\s+(?:kind|type)\s+of\s+(?:engineer|person|candidate|infrastructure|systems?)\b.{0,50}?\b(?:need|needs|looking\s+for)\b)"
    r"|(?:\bbring(?:ing)?\s+that\s+same\b)"
    r"|(?:\bwhich\s+is\s+(?:exactly\s+)?(?:what|the\s+(?:kind|type))\b.{0,50}?\b(?:need|needs|looking\s+for)\b)",
    re.I,
)


def _drop_sentences(md: str, rx: re.Pattern) -> tuple[str, int]:
    """Drop any sentence matching rx, line by line (preserves layout). Returns (clean, n_removed)."""
    removed = 0
    out: list[str] = []
    for line in md.splitlines():
        parts = re.split(r"(?<=\.)\s+", line)   # split into sentences, keeping terminal periods
        kept = [p for p in parts if not rx.search(p)]
        removed += len(parts) - len(kept)
        out.append(" ".join(kept).rstrip())
    return "\n".join(out), removed


def scrub_fabricated_numbers(md: str) -> tuple[str, int]:
    """Drop any sentence containing an ungrounded vague-magnitude volume claim. Returns (clean, n)."""
    return _drop_sentences(md, _FAB_VOLUME)


def scrub_meta_commentary(md: str) -> tuple[str, int]:
    """Drop any résumé-body sentence that pitches the company / uses cover-letter language. (clean, n).
    Do NOT run on cover letters — addressing the company is exactly their job."""
    return _drop_sentences(md, _META)


# ── Quality detectors (feed the résumé quality-review fix loop) ──────────────────────────────────

# AI-tell buzzwords a real engineer wouldn't write (résumé Rule 8). Whole-word matched so "unlock"
# doesn't hit "unlocked-in-a-name" etc. Multi-word phrases matched literally.
_AI_TELLS = [
    "leveraged", "leveraging", "spearheaded", "utilized", "utilize", "utilizing", "seamlessly", "seamless",
    "robust", "cutting-edge", "state-of-the-art", "best-in-class", "passionate", "synergy",
    "synergies", "unlock", "unlocking", "game-changing", "results-driven", "proven track record",
    "wear many hats", "deep dive", "drive impact", "world-class", "hit the ground running",
    "think outside the box", "detail-oriented", "self-starter", "go-getter", "team player",
]
_AI_TELL_RX = re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in _AI_TELLS) + r")(?![\w-])", re.I)


def ai_tells(md: str) -> list[str]:
    """Return the distinct AI-tell buzzwords present in the résumé (empty = clean). Case-insensitive,
    order-preserving. These read as machine-written / filler and weaken credibility (Rule 8)."""
    seen, out = set(), []
    for m in _AI_TELL_RX.finditer(md or ""):
        w = m.group(1).lower()
        if w not in seen:
            seen.add(w)
            out.append(m.group(1))
    return out


# Plain-English swaps for the AI tells — grammar-safe inline replacements so the sentence still reads
# naturally after the swap (a real engineer's word, not a marketing bot's). Phrases first (longest
# match), then single words. Applied deterministically so no LLM slip can leave a buzzword in.
_AI_TELL_SWAPS: list[tuple[str, str]] = [
    ("proven track record", "track record"), ("hit the ground running", "ramp up fast"),
    ("think outside the box", "find creative solutions"), ("wear many hats", "take on varied work"),
    ("passionate about", "focused on"), ("drive impact", "deliver results"),
    ("results-driven", "outcome-focused"), ("game-changing", "major"),
    ("cutting-edge", "modern"), ("state-of-the-art", "modern"), ("best-in-class", "leading"),
    ("world-class", "leading"), ("deep dive", "close look"), ("detail-oriented", "thorough"),
    ("self-starter", "proactive"), ("go-getter", "proactive"), ("team player", "collaborative"),
    ("leveraging", "using"), ("leveraged", "used"), ("utilizing", "using"), ("utilized", "used"), ("utilize", "use"),
    ("spearheaded", "led"), ("seamlessly", "smoothly"), ("seamless", "smooth"),
    ("robust", "reliable"), ("passionate", "focused"), ("synergies", "efficiencies"),
    ("synergy", "collaboration"), ("unlocking", "enabling"), ("unlock", "enable"),
]


def _match_case(original: str, repl: str) -> str:
    """Carry the original's capitalization onto the replacement so a sentence-start swap stays capped."""
    if original[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def scrub_ai_tells(md: str) -> tuple[str, int]:
    """Replace AI-tell buzzwords with plain-engineer equivalents. Returns (clean, n_replaced).
    Deterministic — guarantees the résumé never ships a buzzword, no LLM dependency (B1)."""
    n = 0
    out = md or ""
    for tell, repl in _AI_TELL_SWAPS:
        rx = re.compile(r"(?<![\w-])(" + re.escape(tell) + r")(?![\w-])", re.I)
        def _sub(m):
            nonlocal n
            n += 1
            return _match_case(m.group(1), repl)
        out = rx.sub(_sub, out)
    return out, n


_HAS_NUMBER = re.compile(r"\d")


def roles_missing_quantified_lead(md: str) -> list[str]:
    """Return role/section headings whose FIRST bullet carries no number/metric. Recruiters scan the
    lead bullet of each role for impact — a quantified lead is the single biggest scannability win."""
    missing: list[str] = []
    cur_heading: str | None = None
    lead_seen_for: set[str] = set()
    in_experience = False
    for line in (md or "").splitlines():
        s = line.strip()
        if s.startswith("## "):                       # top-level section
            in_experience = "EXPERIENCE" in s.upper()
            cur_heading = None
            continue
        if in_experience and s.startswith("### "):     # a role
            cur_heading = s[4:].strip()
            continue
        if in_experience and cur_heading and s.startswith("- ") and cur_heading not in lead_seen_for:
            lead_seen_for.add(cur_heading)             # this is the role's LEAD bullet
            if not _HAS_NUMBER.search(s):
                missing.append(cur_heading)
    return missing


def promote_quantified_lead(md: str) -> tuple[str, int]:
    """Deterministic B3 gate: within each experience role, if the LEAD bullet has no number but a
    LATER bullet does, move the first numbered bullet to the front. Truthful — only REORDERS existing
    bullets, never invents a figure — so every role that *has* a metric leads with it (recruiter
    scannability). Returns (md, n_roles_promoted)."""
    if not md:
        return md, 0
    lines = md.splitlines()
    out: list[str] = []
    n = 0
    in_exp = False
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("## "):
            in_exp = "EXPERIENCE" in s.upper()
            out.append(lines[i]); i += 1; continue
        if in_exp and s.startswith("- "):
            block = []
            j = i
            while j < len(lines) and lines[j].strip().startswith("- "):
                block.append(lines[j]); j += 1
            if block and not _HAS_NUMBER.search(block[0]):
                for k in range(1, len(block)):
                    if _HAS_NUMBER.search(block[k]):
                        block.insert(0, block.pop(k))   # promote first numbered bullet to lead
                        n += 1
                        break
            out.extend(block)
            i = j
            continue
        out.append(lines[i]); i += 1
    return "\n".join(out), n


def clean_typography(s: str) -> str:
    """Normalize résumé/cover markdown to clean, human plain text — removes the typographic symbols
    that read as machine-generated (middle dots, bullets, em/en dashes, arrows, smart quotes) without
    disturbing markdown structure or newlines."""
    if not s:
        return s
    s = re.sub(r"[ \t]*[·•‧∙▪●‣⁃⁌]+[ \t]*", ", ", s)
    s = re.sub(r"[ \t]*—[ \t]*", " - ", s)             # em dash -> spaced hyphen
    s = s.replace("–", "-")                              # en dash -> hyphen
    s = re.sub(r"[‐‑‒−]", "-", s)   # hyphen / non-breaking hyphen / figure dash / minus -> ASCII "-"
    for a, b in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
                 ("…", "..."), ("→", " to "), (" ", " "), ("​", "")):
        s = s.replace(a, b)
    s = re.sub(r"[ \t]{2,}", " ", s)                    # collapse runs of spaces
    s = re.sub(r"[ \t]+([,.;:])", r"\1", s)             # no space before punctuation
    s = re.sub(r",[ \t]*,", ", ", s)                    # no double commas
    s = re.sub(r"\n{3,}", "\n\n", s)                    # no big blank gaps
    return s


def _summary_bounds(md: str) -> tuple[int, int] | None:
    """Char span of the SUMMARY section body (after the heading, up to the next heading/---)."""
    m = re.search(r"^#{1,3}\s*SUMMARY\s*$", md, re.I | re.M)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r"^\s*(?:#{1,3}\s|\-\-\-)", md[start:], re.M)
    end = start + nxt.start() if nxt else len(md)
    return start, end


def scrub_company_from_summary(md: str, company: str) -> tuple[str, int]:
    """Drop any SUMMARY sentence that names the TARGET company — the résumé summary is about the
    candidate, never the employer they're applying to (naming it reads as cover-letter pitch, Rule 3).
    Scoped to the summary + whole-word match so a past employer in a bullet is untouched."""
    company = (company or "").strip()
    if not company or len(company) < 3:
        return md, 0
    bounds = _summary_bounds(md)
    if not bounds:
        return md, 0
    start, end = bounds
    # match the full name or its distinctive first token (>=4 chars), allowing a trailing 's
    first = company.split()[0]
    names = [company] + ([first] if len(first) >= 4 and first.lower() != company.lower() else [])
    rx = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")('s)?\b", re.I)
    body = md[start:end]
    kept_lines, removed = [], 0
    for line in body.splitlines():
        parts = re.split(r"(?<=\.)\s+", line)
        keep = [p for p in parts if not rx.search(p)]
        removed += len(parts) - len(keep)
        kept_lines.append(" ".join(keep).rstrip())
    return md[:start] + "\n".join(kept_lines) + md[end:], removed


def finalize_resume_md(md: str, company: str = "") -> tuple[str, dict]:
    """The ONE finalize step every résumé write must pass through (initial tailor AND the quality-fix
    rewrite) so a rewrite can't silently reintroduce fabrication, meta-commentary, company-pitch, or
    typographic junk. Returns (clean_md, {"fabrication": n, "meta_commentary": n, "company_pitch": n})."""
    md = clean_typography(md)
    md, fab = scrub_fabricated_numbers(md)
    md, meta = scrub_meta_commentary(md)
    md, comp = scrub_company_from_summary(md, company)
    md, tells = scrub_ai_tells(md)   # B1: guarantee no AI-tell buzzwords ship (100% human-sounding)
    md, promoted = promote_quantified_lead(md)   # B3: lead each role with a metric where one exists
    return md, {"fabrication": fab, "meta_commentary": meta, "company_pitch": comp,
                "ai_tells": tells, "lead_promoted": promoted}
