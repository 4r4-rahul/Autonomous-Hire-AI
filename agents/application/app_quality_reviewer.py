"""
Application Quality Reviewer Agent
Reviews every tailored resume before it reaches the form filler.

Scores across 6 dimensions (total 100):
  - ATS keyword coverage  (25 pts)
  - Value story & impact  (25 pts)
  - AI/technical depth    (20 pts)
  - Role & company fit    (15 pts)
  - Factual integrity     (10 pts)
  - Formatting             (5 pts)

If total < MIN_APP_QUALITY (default 75): blocks submission and triggers a
self-healing retry — feeds specific issues back to resume_tailor for one
rewrite attempt before final pass/fail decision.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.state import Application, JobListing, JobSearchState
from core.config import get_settings
from core.logger import get_logger
from core.text_scrub import finalize_resume_md
from core.llm import get_llm

logger = get_logger("app_quality_reviewer")

_SYSTEM_PROMPT = Path("prompts/system/app_quality_reviewer.txt").read_text()
_MAX_RETRIES = 1  # one self-healing rewrite attempt before hard fail
_DB_PATH = Path("data/quality_reviews.db")


def _init_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_reviews (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            reviewed_at TEXT NOT NULL,
            company     TEXT NOT NULL,
            role        TEXT NOT NULL,
            total_score INTEGER NOT NULL,
            passed      INTEGER NOT NULL,
            ats_score   INTEGER,
            value_score INTEGER,
            ai_score    INTEGER,
            fit_score   INTEGER,
            integrity   INTEGER,
            formatting  INTEGER,
            top_deduction_1  TEXT,
            top_deduction_2  TEXT,
            top_deduction_3  TEXT,
            missing_keywords TEXT,
            verdict     TEXT,
            self_healed INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def _log_review_to_db(job: JobListing, review: dict, self_healed: bool = False) -> None:
    try:
        conn = _init_db()
        dims = review.get("dimension_scores", {})
        deductions = review.get("top_3_deductions", [])

        def _deduction_str(d: dict) -> str:
            return f"[{d.get('dimension','')} -{d.get('points_lost',0)}] {d.get('reason','')}"

        conn.execute("""
            INSERT INTO quality_reviews
            (reviewed_at, company, role, total_score, passed,
             ats_score, value_score, ai_score, fit_score, integrity, formatting,
             top_deduction_1, top_deduction_2, top_deduction_3,
             missing_keywords, verdict, self_healed)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            job.get("company_name", ""),
            job.get("title", ""),
            review.get("total_score", 0),
            1 if review.get("passed") else 0,
            dims.get("ats_keyword_coverage"),
            dims.get("value_story_impact"),
            dims.get("ai_technical_depth"),
            dims.get("role_company_fit"),
            dims.get("factual_integrity"),
            dims.get("formatting"),
            _deduction_str(deductions[0]) if len(deductions) > 0 else None,
            _deduction_str(deductions[1]) if len(deductions) > 1 else None,
            _deduction_str(deductions[2]) if len(deductions) > 2 else None,
            json.dumps(review.get("missing_keywords", [])[:10]),
            review.get("verdict", ""),
            1 if self_healed else 0,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("quality_db_log_failed", error=str(e))


# ── Core review logic ──────────────────────────────────────────────────────

async def _review(
    resume_text: str,
    job: JobListing,
    llm,
) -> dict:
    """Run the quality review. Returns the raw score dict from LLM."""
    # Strip hidden metadata footer before review — reviewer should only see the actual resume
    clean_resume = resume_text.split("<!-- TAILOR_META")[0].strip()

    messages = [{
        "role": "user",
        "content": (
            f"JOB TITLE: {job['title']}\n"
            f"COMPANY: {job['company_name']}\n\n"
            f"JOB DESCRIPTION:\n{job['description'][:3000]}\n\n"
            f"TAILORED RESUME:\n{clean_resume}"
        ),
    }]
    return await llm.chat_json(_SYSTEM_PROMPT, messages, max_tokens=4096, temperature=0.1)


def _score_to_label(score: int) -> str:
    if score >= 85:  return "excellent"
    if score >= 75:  return "good — ready to submit"
    if score >= 65:  return "needs minor fixes"
    if score >= 50:  return "needs significant rework"
    return "do not submit"


# ── Self-healing rewrite ───────────────────────────────────────────────────

async def _attempt_fix(
    app: Application,
    job: JobListing,
    review: dict,
    llm,
) -> str | None:
    """
    Feed quality issues back to resume_tailor for one targeted rewrite.
    Returns the new resume text if successful, None otherwise.
    """
    import yaml
    from pathlib import Path as P
    from agents.application.resume_tailor import _research_company, _extract_ats_keywords, _tailor_resume, _render_markdown

    issues = review.get("critical_issues", [])
    missing_kw = review.get("missing_keywords", [])
    # The SME pass says WHAT to do, not just what is wrong. Feed the actionable half — the
    # requirements the candidate demonstrably has but that this version omits or buries, plus what
    # to cut. genuine_gap items are deliberately excluded: the correct action there is to leave the
    # gap visible, and passing them to a rewriter is how a résumé starts claiming things.
    actionable = [r for r in (review.get("requirements_review") or [])
                  if r.get("status") in ("weak", "absent_but_available") and r.get("action")]
    removals = review.get("remove") or []
    if not issues and not missing_kw and not actionable:
        return None

    logger.info(
        "quality_fix_attempt",
        title=job["title"],
        company=job["company_name"],
        issues=len(issues),
        missing_keywords=missing_kw[:5],
        actionable=len(actionable),
        removals=len(removals),
        genuine_gaps=sum(1 for r in (review.get("requirements_review") or [])
                         if r.get("status") == "genuine_gap"),
    )

    profile = yaml.safe_load(P("data/user_profile.yaml").read_text())

    # Build a fake fit_score with the quality issues embedded as missing_skills
    fix_fit_score = {
        "job_id": job["id"],
        "score": 70,
        "reasoning": "Quality review fix attempt",
        "match_highlights": review.get("strengths", [])[:3],
        "missing_skills": (
            issues[:3]
            + [f"Missing keyword: {kw}" for kw in missing_kw[:3]]
            + [f"{r['requirement']} — {r['action']}" for r in actionable[:4]]
            + [f"REMOVE: {r.get('content','')[:90]} ({r.get('why','')[:60]})" for r in removals[:2]]
        ),
        "passed_threshold": True,
        "scored_at": "",
    }

    # Add explicit fix instructions to the job description context
    fix_context = f"\n\n[QUALITY REVIEWER FEEDBACK — MUST FIX]\n"
    fix_context += "Critical issues:\n" + "\n".join(f"- {i}" for i in issues[:5])
    if missing_kw:
        fix_context += f"\nMissing keywords to inject: {', '.join(missing_kw[:10])}"
    fix_context += f"\nCurrent score: {review.get('total_score', 0)}/100. Target: 75+."

    patched_job = dict(job)
    patched_job["description"] = job["description"] + fix_context

    try:
        company_research, ats_keywords = await asyncio.gather(
            _research_company(job["company_name"], llm),
            _extract_ats_keywords(job["description"], llm),
        )
        tailored = await _tailor_resume(patched_job, fix_fit_score, company_research, ats_keywords, profile, llm)
        if not tailored:
            return None
        return _render_markdown(tailored, job, profile)
    except Exception as e:
        logger.error("quality_fix_failed", error=str(e))
        return None


# ── Main entry point ───────────────────────────────────────────────────────

async def review_application(
    app: Application,
    job: JobListing,
    llm,
    auto_fix: bool = True,
    llm_fix=None,  # separate (smarter) model for fix rewrites; defaults to llm if not set
) -> tuple[Application, dict]:
    """
    Review a single application. Returns (updated_app, quality_report).
    If score < threshold and auto_fix=True, attempts one rewrite before deciding.
    llm_fix allows using a more capable model only for the expensive fix rewrite.
    """
    if llm_fix is None:
        llm_fix = llm
    cfg = get_settings()
    resume_path = Path(app["tailored_resume_path"])

    if not resume_path.exists():
        logger.error("resume_file_missing", path=str(resume_path))
        report = {"total_score": 0, "passed": False, "verdict": "Resume file not found"}
        updated = dict(app)
        updated["quality_score"] = 0
        updated["quality_report"] = report
        updated["status"] = "blocked"
        return updated, report

    resume_text = resume_path.read_text(encoding="utf-8")

    # Initial review — retry up to 3 times on JSON parse errors
    review = None
    for _attempt in range(3):
        try:
            review = await _review(resume_text, job, llm)
            break
        except ValueError as e:
            if _attempt == 2:
                logger.error("review_json_failed_all_retries", app_id=app["id"], error=str(e))
                report = {"total_score": 0, "passed": False, "verdict": f"Review JSON parse error: {e}"}
                updated = dict(app)
                updated["quality_score"] = 0
                updated["quality_report"] = report
                updated["status"] = "blocked"
                return updated, report
            logger.warning("quality_review_json_retry", app_id=app["id"], attempt=_attempt + 1)
        except Exception as e:
            logger.error("review_failed", app_id=app["id"], error=str(e))
            report = {"total_score": 0, "passed": False, "verdict": f"Review error: {e}"}
            updated = dict(app)
            updated["quality_score"] = 0
            updated["quality_report"] = report
            updated["status"] = "blocked"
            return updated, report

    # Deterministic quality gates (B1 AI-tells, B3 quantified-lead bullets) — merged into the review
    # so the self-healing rewrite addresses them even when the LLM score passed. The prompt alone
    # doesn't reliably enforce these (esp. on DeepSeek), so we catch them in code.
    from core.text_scrub import ai_tells, roles_missing_quantified_lead
    _tells = ai_tells(resume_text)
    _noquant = roles_missing_quantified_lead(resume_text)
    det_issues: list[str] = []
    if _tells:
        det_issues.append("Rewrite to remove AI-tell buzzwords (use plain engineer language): "
                          + ", ".join(_tells))
    if _noquant:
        det_issues.append("Lead bullet has no metric for: " + "; ".join(_noquant)
                          + ". Start each role's first bullet with a concrete number where truthful.")
    if det_issues:
        review["critical_issues"] = det_issues + list(review.get("critical_issues", []))
        logger.info("quality_deterministic_issues", app_id=app["id"],
                    ai_tells=_tells, non_quant_leads=_noquant)

    score = review.get("total_score", 0)
    passed = score >= cfg.min_app_quality

    logger.log_decision(
        action="quality_review",
        outcome="passed" if passed else "failed",
        details={
            "title": job["title"],
            "company": job["company_name"],
            "score": score,
            "threshold": cfg.min_app_quality,
            "label": _score_to_label(score),
            "critical_issues": len(review.get("critical_issues", [])),
            "missing_keywords": review.get("missing_keywords", [])[:5],
            "top_3_deductions": review.get("top_3_deductions", [])[:3],
        },
    )

    # Self-healing: attempt one rewrite on a near-miss score. det_issues (B3 non-quant leads) were
    # already merged into review["critical_issues"] above, so IF a fix runs it also addresses them —
    # but we do NOT force a re-tailor for det_issues alone (too costly per résumé, and a number can't
    # be fabricated). B1 AI-tells are handled deterministically in finalize, not here. A regression
    # guard keeps the ORIGINAL résumé if the rewrite scores worse.
    if auto_fix and not passed and score >= cfg.min_app_quality - 8:
        logger.info("quality_fix_triggered", score=score, target=cfg.min_app_quality)
        orig_score, orig_review = score, review
        fixed_text = await _attempt_fix(app, job, review, llm_fix)  # use smarter model for fix

        if fixed_text:
            # Finalize the rewrite through the SAME clean+scrub as the initial save, or the fix silently
            # reintroduces typographic junk / fabrication / meta-commentary (it bypassed finalize before).
            fixed_text, _fx = finalize_resume_md(fixed_text, company=job.get("company_name", ""))
            if any(_fx.values()):
                logger.info("quality_fix_scrubbed", title=job.get("title"), **_fx)

            # Re-review the fixed version (cheap model is fine for re-check)
            try:
                new_review = await _review(fixed_text, job, llm)
                new_score = new_review.get("total_score", 0)
                # Deterministic re-check: did the fix actually clear the code-enforced issues?
                new_det = bool(ai_tells(fixed_text) or roles_missing_quantified_lead(fixed_text))
                # Keep the fix only if it doesn't regress the LLM score. Prefer it when it clears the
                # deterministic issues at an equal-or-better score; otherwise keep the original.
                if new_score >= orig_score:
                    resume_path.write_text(fixed_text, encoding="utf-8")
                    review, score = new_review, new_score
                    passed = score >= cfg.min_app_quality
                    review["self_healed"] = True
                    review["deterministic_clean"] = not new_det
                    logger.log_decision(action="quality_review_after_fix",
                                        outcome="passed" if passed else "still_failed",
                                        details={"score": score, "title": job["title"],
                                                 "det_cleared": not new_det})
                else:
                    review, score = orig_review, orig_score   # regression guard: revert to original
                    logger.info("quality_fix_reverted_regression", orig=orig_score, fixed=new_score,
                                title=job["title"])
            except Exception:
                pass

    _log_review_to_db(job, review, self_healed=review.get("self_healed", False))

    updated = dict(app)
    updated["quality_score"] = score
    updated["quality_report"] = review
    updated["status"] = "pending_review" if passed else "blocked"

    return updated, review


async def run(state: JobSearchState) -> JobSearchState:
    """
    Review all applications that have a tailored resume but no quality score yet.
    Blocks low-quality applications from reaching the form filler.
    """
    jobs_by_id = {j["id"]: j for j in state.get("job_listings", [])}
    llm_cheap = get_llm("app_quality_reviewer", model="claude-haiku-4-5")  # initial screen
    llm_smart = get_llm("app_quality_reviewer")  # fix rewrites only
    cfg = get_settings()

    to_review = [
        a for a in state.get("applications", [])
        if a.get("tailored_resume_path") and (
            a.get("quality_score", 0) == 0  # not yet reviewed
            or (
                a.get("status") == "blocked"
                and 0 < a.get("quality_score", 0) < cfg.min_app_quality
            )  # re-review blocked apps that now meet the (possibly lowered) threshold
        )
    ]

    logger.info(
        "quality_review_start",
        to_review=len(to_review),
        threshold=cfg.min_app_quality,
    )

    updated_apps = {a["id"]: a for a in state.get("applications", [])}

    # Review concurrently (max 3 at once to avoid rate limits)
    sem = asyncio.Semaphore(3)

    async def _review_with_sem(app: Application) -> None:
        async with sem:
            job = jobs_by_id.get(app["job_id"])
            if not job:
                logger.warning("job_not_found_for_app", app_id=app["id"])
                return
            # Initial screen with Haiku; fix rewrites use Sonnet via review_application's llm arg
            reviewed_app, _ = await review_application(app, job, llm_cheap, llm_fix=llm_smart)
            updated_apps[reviewed_app["id"]] = reviewed_app

    await asyncio.gather(*[_review_with_sem(a) for a in to_review])

    # Promote any blocked app whose existing score already meets the current threshold
    # (handles threshold being lowered between runs)
    promoted = 0
    for app_id, app in updated_apps.items():
        if app.get("status") == "blocked" and app.get("quality_score", 0) >= cfg.min_app_quality:
            promoted_app = dict(app)
            promoted_app["status"] = "pending_review"
            updated_apps[app_id] = promoted_app
            promoted += 1
    if promoted:
        logger.info("blocked_apps_promoted", count=promoted, threshold=cfg.min_app_quality)

    all_apps = list(updated_apps.values())
    passed = sum(1 for a in all_apps if a.get("status") == "pending_review")
    blocked = sum(1 for a in all_apps if a.get("status") == "blocked")

    logger.log_decision(
        action="quality_review_complete",
        outcome="success",
        details={
            "reviewed": len(to_review),
            "passed": passed,
            "blocked": blocked,
            "avg_score": round(
                sum(a.get("quality_score", 0) for a in all_apps) / max(len(all_apps), 1), 1
            ),
        },
    )

    updated = dict(state)
    updated["applications"] = all_apps
    return updated


def get_review_summary(state: JobSearchState) -> list[dict]:
    """Return a dashboard-friendly summary of all application quality scores."""
    jobs_by_id = {j["id"]: j for j in state.get("job_listings", [])}
    summary = []
    for app in sorted(
        state.get("applications", []),
        key=lambda a: a.get("quality_score", 0),
        reverse=True,
    ):
        job = jobs_by_id.get(app["job_id"], {})
        report = app.get("quality_report", {})
        summary.append({
            "company": app["company_name"],
            "role": app["role_title"],
            "score": app.get("quality_score", 0),
            "label": _score_to_label(app.get("quality_score", 0)),
            "status": app.get("status", "unknown"),
            "verdict": report.get("verdict", ""),
            "strengths": report.get("strengths", [])[:2],
            "critical_issues": report.get("critical_issues", [])[:2],
            "missing_keywords": report.get("missing_keywords", [])[:5],
            "self_healed": report.get("self_healed", False),
            "resume_path": app.get("tailored_resume_path", ""),
        })
    return summary


# ── CLI smoke test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    async def _test():
        import uuid
        from datetime import datetime, timezone
        from agents.discovery.job_scanner import run as scan_jobs
        from agents.discovery.fit_analyst import run as score_jobs
        from agents.application.resume_tailor import run as tailor
        from core.state import Company, JobSearchState

        companies: list[Company] = [{
            "id": str(uuid.uuid4()), "name": "Databricks",
            "website": "https://www.databricks.com", "linkedin_url": "",
            "size": "large", "domain": "Data Infrastructure", "tech_stack": [],
            "open_role_count": 0, "discovered_at": datetime.now(timezone.utc).isoformat(),
        }]
        base: JobSearchState = {
            "companies": companies, "job_listings": [], "fit_scores": [],
            "applications": [], "pending_approvals": [], "recruiter_contacts": [],
            "email_events": [], "meetings_scheduled": [],
            "weekly_quality_score": 0.0, "weekly_quantity_count": 0,
            "response_rate": 0.0, "interview_rate": 0.0,
            "last_run_at": None, "errors": [], "agent_statuses": {},
        }

        print("1/4  Scanning jobs...")
        scanned = await scan_jobs(base, max_companies=1, batch_size=1)
        ai_kw = ["platform", "ml", "ai", "mlops", "data engineer", "backend", "infrastructure"]
        scanned["job_listings"] = [
            j for j in scanned["job_listings"]
            if any(k in j["title"].lower() for k in ai_kw)
        ][:15]
        print(f"     {len(scanned['job_listings'])} relevant jobs")

        print("2/4  Scoring fit...")
        scored = await score_jobs(scanned, batch_size=5)
        from agents.discovery.fit_analyst import get_passing_jobs
        passing = get_passing_jobs(scored)
        print(f"     {len(passing)} passed threshold")
        if not passing:
            print("     No passing jobs. Exiting.")
            return

        print("3/4  Tailoring resume...")
        tailored_state = await tailor(scored, max_applications=1)
        apps = tailored_state.get("applications", [])
        print(f"     {len(apps)} applications created")

        print("4/4  Quality review...")
        reviewed = await run(tailored_state)
        summary = get_review_summary(reviewed)

        print(f"\n{'═'*65}")
        for s in summary:
            status_icon = "✓" if s["status"] == "pending_review" else "✗"
            healed = " (self-healed)" if s["self_healed"] else ""
            print(f"\n{status_icon} [{s['score']:3d}/100] {s['role']} @ {s['company']}{healed}")
            print(f"  Label:   {s['label']}")
            print(f"  Verdict: {s['verdict']}")
            if s["strengths"]:
                print(f"  ✓ {s['strengths'][0]}")
            if s["critical_issues"]:
                print(f"  ✗ {s['critical_issues'][0]}")
            if s["missing_keywords"]:
                print(f"  Missing: {', '.join(s['missing_keywords'])}")

    asyncio.run(_test())
