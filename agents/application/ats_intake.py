"""
ATS intake — the "apply on the company ATS, not LinkedIn" plugin.

Takes discovered jobs (company + title, from ANY source — the LinkedIn radar OR the board
scanners) and, for each, with ZERO LinkedIn navigation:
  1. resolves the real ATS apply URL + JD from the company's public board API (resolve_ats_for)
  2. scores fit (Haiku)
  3. Opus-tailors a résumé
  4. quality-reviews it
  5. PARKS it in the review queue (status=pending_review) with the tailored résumé attached

The result: every discovered job becomes a real ATS application form with a tailored résumé,
ready for the extension's 1-tap OR the auto-submit safe lane. No browser, no account risk.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from core.config import get_settings
from core.logger import get_logger
from core.llm import get_llm
from core.state import JobSearchState
from agents.discovery.apply_url_resolver import resolve_ats_for, is_listing_url, posting_age_days

logger = get_logger("ats_intake")

_PROFILE = Path("data/user_profile.yaml")
# Job-board trackers / gig platforms — never a real ATS application form.
_JUNK_ATS = ("jometer.com", "jobleads.com", "outlier.ai", "zipapply",
             "indeed.com", "joinhandshake.com", "remotehub.com")

_ACTIVE = ("submitted", "pending_review", "awaiting_confirmation", "approved",
           "pending_manual_submit")
# The per-company cap counts only roles STILL AWAITING YOUR ACTION (in the queue / in flight),
# NOT ones already applied to. So once you apply to 3 roles at a company, those slots free up and
# new matched roles there can surface — you just never see more than 3 at once (the anti-spam point).
# Dedup still uses _ACTIVE, so a role you've already applied to is never re-queued.

# Per-company application caps now live in data/company_caps.json (core.company_caps): a rolling
# WINDOW cap (default 4 per 30 days) that respects employer application-limit policies — e.g. Roblox
# auto-closes the 6th application in 30 days. Counts PENDING + SUBMITTED-within-window, so a submitted
# app no longer frees an unlimited slot (the old bug that let us submit 6× to Roblox). Staffing
# agencies stay exempt. See core.company_caps.
from core.company_caps import cap_for as _cap_for, count_against_cap as _counts_against_cap

# Freshness gate: skip postings older than this when the age is known. Measured from the more
# recent of posted/updated (see apply_url_resolver._newer_iso), so an actively-maintained req isn't
# dropped. 30d is the hard floor; the queue is sorted freshest-first so recency is prioritized too.
_MAX_AGE_DAYS = 30

# Location relevance — Rahul is US-based + needs US sponsorship. Drop clearly-foreign roles.
_FOREIGN = ("france", "paris", "marseille", "lyon", "germany", "munich", "berlin", "hamburg",
            "united kingdom", "london", "manchester", "ireland", "dublin", "singapore",
            "australia", "sydney", "melbourne", "netherlands", "amsterdam", "sweden",
            "stockholm", "spain", "madrid", "barcelona", "canada", "toronto", "vancouver",
            "india", "bangalore", "hyderabad", "bengaluru", "tokyo", "japan", "poland",
            "warsaw", "portugal", "lisbon", "switzerland", "zurich", "emea", "apac", "latam",
            "brazil", "mexico", "israel", "tel aviv", "dubai", "uae", "ontario", "quebec",
            "europe", "korea", "seoul", "china", "shanghai", "beijing")
_US = ("remote", "united states", "usa", "u.s.", "u.s ", "anywhere", " us ", "us-", "-us",
       "new york", "san francisco", "bay area", "austin", "seattle", "boston", "chicago",
       "denver", "atlanta", "los angeles", "dallas", "houston", "washington", "texas",
       "california", "new jersey", "remote (us", "us remote", "north america")


def _contract_only() -> bool:
    """True when the pipeline is in CONTRACT mode — intake then keeps only C2C/C2H/contract roles."""
    try:
        from core.config import get_settings
        return (get_settings().pipeline_mode or "fulltime").lower() == "contract"
    except Exception:
        return False


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _is_relevant_location(*texts) -> bool:
    """True unless the role is clearly foreign-only (foreign signal AND no US/remote signal)."""
    t = " ".join((x or "") for x in texts).lower()
    has_foreign = any(s in t for s in _FOREIGN)
    has_us = any(s in t for s in _US)
    return has_us or not has_foreign   # unknown → keep (don't over-filter)


async def intake_jobs(discovered: list[dict], state: JobSearchState,
                      *, limit: int | None = None, tailor: bool = True) -> tuple[JobSearchState, dict]:
    """Resolve → (tailor) → park each discovered job (off LinkedIn).

    discovered: list of dicts with keys: company, title, and optionally description,
                location, source, fallback_url (a known direct ATS URL if resolution fails).
    tailor=True:  Opus-tailor a résumé for each kept role now (costs ~$0.085 each).
    tailor=False: park the role scored + resolved but WITHOUT a résumé (status pending_review,
                  tailored_resume_path=None) — near-free. The résumé is generated on demand when
                  the user actually engages the role (tailor-on-demand → spend only on real ones).
    Returns (updated_state, report). Never raises on a single bad job — it's skipped.
    """
    from agents.application.resume_tailor import tailor_one
    from agents.application.linkedin_apply_pipeline import _score_job
    from agents.application.app_quality_reviewer import review_application

    import copy
    cfg = get_settings()
    profile = yaml.safe_load(_PROFILE.read_text()) if _PROFILE.exists() else {}

    # Work on a copy so existing scanner leads can be UPDATED in place (apply_url → ATS URL)
    # rather than duplicated. A discovered item may carry a `job_id` of an existing lead.
    updated = copy.deepcopy(dict(state))
    jobs_by_id = {j.get("id"): j for j in updated.get("job_listings", [])}

    # Dedup: skip a job already queued/applied (by company+title) or by ATS URL.
    active = {(_norm(a.get("company_name")), _norm(a.get("role_title")))
              for a in updated.get("applications", []) if a.get("status") in _ACTIVE}
    existing_urls = {j.get("apply_url") for j in updated.get("job_listings", [])}
    # Per-company cap: count only roles STILL AWAITING ACTION (not already-applied) at each company,
    # so applying to your 3 frees the slots for fresh matched roles there (this run adds to it).
    from collections import Counter
    # Window-aware per-company count: PENDING + SUBMITTED-within-window (NOT all-time pending-only —
    # that let submitted apps free unlimited slots, so we hit Roblox 6×). See core.company_caps.
    company_counts = Counter(_norm(a.get("company_name"))
                             for a in updated.get("applications", [])
                             if _counts_against_cap(a, company=a.get("company_name")))
    # Reuse the learned quality gates: low-value companies (avg fit < 40 over ≥10 jobs) +
    # zero-pass title patterns (forward deployed, solutions engineer, sales…) + sponsorship
    # knock-outs. Keeps Palantir/Cloudflare/Veeva noise out of the queue.
    from agents.discovery.fit_analyst import (
        _build_low_value_companies, _quick_prefilter, _min_required_years, _MAX_REQUIRED_YEARS)
    low_value = _build_low_value_companies(updated)

    # Persistent all-time dedup: never tailor a role we've ever tailored (survives ID/URL churn
    # and re-discovery across runs) — saves the ~$0.085 Opus cost of every redundant tailoring.
    from agents.application.dedup_ledger import Ledger
    ledger = Ledger()

    llm_score = get_llm("ats_intake", model="claude-haiku-4-5")
    llm_tailor = get_llm("ats_intake")                       # Opus (config default)
    llm_cheap = get_llm("ats_intake", model="claude-haiku-4-5")
    llm_review = get_llm("ats_intake", model="claude-haiku-4-5")

    report = {"in": len(discovered), "resolved": 0, "queued": 0, "skipped_dup": 0,
              "skipped_no_ats": 0, "skipped_fit": 0, "skipped_cap": 0,
              "skipped_location": 0, "skipped_prefilter": 0, "skipped_stale": 0,
              "skipped_not_contract": 0, "errors": 0}
    new_jobs, new_apps, new_fits = [], [], []

    def _mkjob(d, ats_url, desc):
        """Build (or copy) the JobListing for scoring/tailoring. Reuses an existing lead's
        fields if a job_id was supplied (committed in place in Phase 2)."""
        # Canonical greenhouse job URLs (job-boards.greenhouse.io/{slug}/jobs/{id}) 302-redirect to
        # the company's branded SPA (careers.x.com / careerpuck), which the extension can't fill —
        # normalize to the EMBED form, which never redirects off-site.
        try:
            from agents.application.url_refresher import _greenhouse_embed_url
            if ats_url and "greenhouse.io" in ats_url and "embed/job_app" not in ats_url:
                ats_url = _greenhouse_embed_url(ats_url) or ats_url
        except Exception:
            pass
        lead = jobs_by_id.get(d.get("job_id"))
        base = dict(lead) if lead else {"id": str(uuid.uuid4()), "company_id": ""}
        base.update({
            "company_name": (d.get("company") or "").strip(), "title": (d.get("title") or "").strip(),
            "description": desc or base.get("description") or f"{d.get('title')} at {d.get('company')}",
            "apply_url": ats_url, "remote": True,
            "location": d.get("location") or base.get("location", "Remote"),
            "source": d.get("source", base.get("source", "ats_intake")),
            "discovered_at": base.get("discovered_at", datetime.now(timezone.utc).isoformat()),
        })
        base.setdefault("required_skills", []); base.setdefault("nice_to_have_skills", [])
        base.setdefault("salary_min", None); base.setdefault("salary_max", None)
        # is_new = True when this is a FRESH job (no existing lead) → its JobListing must be appended.
        # (Was inverted: fresh board-discovered roles got is_new=False, so their job was never added
        #  and the application was orphaned → silently dropped by dedup. That's why topups netted 0.)
        return base, (lead is None)

    # ── PHASE 1 (concurrent, cheap): resolve + score every lead. The slow I/O (ATS API) and
    #    Haiku scoring run in parallel (semaphore-bounded) so filtering 100+ leads takes seconds.
    sem = asyncio.Semaphore(8)

    async def _filter(d):
        company = (d.get("company") or "").strip()
        title = (d.get("title") or "").strip()
        if not company or not title:
            return None, "skip"
        if (_norm(company), _norm(title)) in active:
            return None, "skipped_dup"
        if ledger.seen(company, title):                          # all-time dedup (across runs)
            return None, "skipped_dup"
        if company_counts[_norm(company)] >= _cap_for(company):  # existing-app cap precheck
            return None, "skipped_cap"
        if not _is_relevant_location(title, d.get("location"), (d.get("description") or "")[:200]):
            return None, "skipped_location"
        # Learned quality gate: low-value company / zero-pass title / sponsorship knock-out.
        ok, _reason = _quick_prefilter(
            {"title": title, "company_name": company,
             "description": d.get("description") or "", "salary_max": None}, profile, low_value)
        if not ok:
            return None, "skipped_prefilter"
        async with sem:
            try:
                res = await resolve_ats_for(company, title, us_only=True)   # STRICT US-only
            except Exception:
                res = None
        ats_url = res["url"] if res else (
            d["fallback_url"] if d.get("fallback_url") and not is_listing_url(d["fallback_url"]) else None)
        desc = (res.get("description") if res else "") or d.get("description", "")
        if not ats_url or any(j in ats_url.lower() for j in _JUNK_ATS):
            return None, "skipped_no_ats"
        # CONTRACT-ONLY MODE: reject anything that isn't C2C/C2H/contract. Classified from the source
        # (staffing/vendor prefix), the company (known staffing firm), and the full resolved JD
        # (c2c / corp-to-corp / contract-to-hire / 1099 language). Full-time W2 roles are dropped.
        # Classify employment ONCE (cheap), preferring the ATS's OWN structured flag (resolved role
        # first, then the discovered lead) — cleanest contract signal, falls back to source/company/JD.
        # Persisted on the app so the /contract vs /approvals desks split consistently (the DB otherwise
        # loses the structured flag). In contract mode this also gates: only C2C/C2H/contract survives.
        from core.employment import classify_employment_for_job
        _et = (res.get("employment_type") if res else "") or d.get("employment_type", "")
        _employ = classify_employment_for_job(d.get("source", ""), company, title, desc, _et)
        if _contract_only() and _employ != "contract":
            return None, "skipped_not_contract"
        # When we fell back (no board resolution), the fallback URL/role hasn't been US-verified —
        # drop it if the discovered location/title looks foreign. (Resolved hits are already US.)
        if not res and not _is_relevant_location(title, d.get("location"), (d.get("description") or "")[:200]):
            return None, "skipped_location"
        if ats_url in existing_urls:
            return None, "skipped_dup"
        # Freshness gate: callback odds fall sharply after a few days. If we KNOW the posting age
        # and it's older than the cutoff, skip it (don't waste an Opus tailor on a stale role).
        posted_at = (res.get("posted_at") if res else "") or d.get("posted_at", "")
        age = posting_age_days(posted_at)
        if age is not None and age > _MAX_AGE_DAYS:
            return None, "skipped_stale"
        # Experience ceiling on the FULL resolved JD — the lead's description may have been thin when
        # the prefilter ran above, so re-check now that we have the real posting text.
        _min_yrs = _min_required_years(desc)
        if _min_yrs and _min_yrs > _MAX_REQUIRED_YEARS:
            return None, "skipped_prefilter"
        job, is_new = _mkjob(d, ats_url, desc)
        if res and res.get("location"):
            job["location"] = res["location"]
        if posted_at:
            job["posted_at"] = posted_at
        async with sem:
            try:
                fit = await _score_job(job, llm_score)
            except Exception:
                return None, "errors"
        cand = {"d": d, "job": job, "is_new": is_new, "fit": fit, "employment": _employ,
                "ats": (res["ats"] if res else "fallback"), "resolved": bool(res)}
        return cand, ("ok" if fit.get("passed_threshold") else "skipped_fit")

    results = await asyncio.gather(*[_filter(d) for d in discovered])
    candidates = []
    for cand, status in results:
        if status in ("skip",):
            continue
        if cand and cand.get("resolved"):
            report["resolved"] += 1
        if cand:
            new_fits.append(cand["fit"])
        if status == "ok":
            candidates.append(cand)
        elif status in report:
            report[status] += 1

    # ── PHASE 2 (sequential, bounded): Opus-tailor exactly the top `limit` keepers, applying
    #    the per-company cap + URL dedup as we commit. Only `limit` expensive tailorings happen.
    committed_urls = set()
    for cand in candidates:
        if limit and report["queued"] >= limit:
            break
        job = cand["job"]
        company, title, ats_url = job["company_name"], job["title"], job["apply_url"]
        if company_counts[_norm(company)] >= _cap_for(company):
            report["skipped_cap"] += 1
            continue
        if ats_url in committed_urls or ats_url in existing_urls:
            report["skipped_dup"] += 1
            continue
        if ledger.seen(company, title, ats_url):   # all-time dedup, incl. by ATS job id in URL
            report["skipped_dup"] += 1
            continue
        try:
            if tailor:
                app = await tailor_one(job, cand["fit"], llm_tailor, profile, llm_cheap=llm_cheap)
                if not app:
                    report["errors"] += 1
                    continue
                try:
                    app, _ = await review_application(app, job, llm_review)
                except Exception:
                    pass
            else:
                # Cheap-park: scored + resolved, NO résumé (tailored on demand when engaged).
                app = {
                    "id": str(uuid.uuid4()), "job_id": job["id"],
                    "company_name": company, "role_title": title,
                    "status": "pending_review", "tailored_resume_path": None,
                    "quality_score": None, "fit_score": cand["fit"].get("score"),
                    "needs_tailoring": True, "employment_type": cand.get("employment", ""),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            # Commit the JobListing (fresh → append; existing lead → update in place).
            if cand["is_new"]:
                new_jobs.append(job)
            else:
                lead = jobs_by_id.get(cand["d"].get("job_id"))
                if lead:
                    lead["apply_url"] = ats_url
                    if job.get("description"):
                        lead["description"] = job["description"]
            new_apps.append(app)
            committed_urls.add(ats_url)
            ledger.add(company, title, ats_url)   # record so we never re-tailor this role
            if app.get("status") == "blocked":
                continue
            app["status"] = "pending_review"
            active.add((_norm(company), _norm(title)))
            company_counts[_norm(company)] += 1
            report["queued"] += 1
            logger.info("ats_intake_queued", company=company, title=title[:40],
                        ats=cand["ats"], fit=cand["fit"].get("score"))
        except Exception as e:
            report["errors"] += 1
            logger.warning("ats_intake_job_failed", company=company, error=str(e)[:90])
            continue

    ledger.save()   # persist the all-time dedup ledger
    # `updated` is the deep copy: leads already updated in place; append only the fresh ones.
    updated["job_listings"] = updated.get("job_listings", []) + new_jobs
    updated["applications"] = updated.get("applications", []) + new_apps
    updated["fit_scores"] = updated.get("fit_scores", []) + new_fits
    logger.log_decision(action="ats_intake_complete", outcome="success", details=report)
    return updated, report
