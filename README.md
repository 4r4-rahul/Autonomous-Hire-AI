# AutonomousHire

Multi-agent job-search automation for engineers (built for a sponsorship-needing Senior Data/ML/Platform Engineer). It **discovers roles on companies' own ATS boards**, keeps a **fresh, US-only** review queue, **tailors résumés on demand** with Claude under a hard cost cap, and lets you **one-tap apply** through a Chrome extension — with a human in the loop on every submission.

## Core idea: discover on boards, apply on the company ATS

LinkedIn bans accounts for *applying* too much, not for *browsing*. So the two are decoupled:

| | Where | Account risk |
|---|---|---|
| **Discover** a job exists | public ATS board APIs (Greenhouse / Lever / Ashby / Workable) | none |
| **Apply** to it | the company's own ATS form, via a Chrome extension in your browser | none |

Application volume never touches the LinkedIn account.

## Pipeline

```
Board scan (sponsor ATS APIs)
  → US-only + freshness filter (<30d, real posted_at)
  → fit score (Haiku) + sponsorship/recency ranking
  → cheap-park in the review queue          ← no LLM spend yet
        │
   you engage a role
        ▼
  → tailor on demand: Opus résumé + cover letter (work-auth framing)
  → quality review + ATS keyword-coverage check
  → review queue (freshest-first) → Open & Apply
  → Chrome extension autofills the ATS form → you review CAPTCHA + submit
  → email-as-truth outcome tracking → learning loop
```

## Key design points

- **Tailor-on-demand** — the queue is filled cheaply (Haiku scoring only); Opus tailoring runs *only* on roles you actually pursue. "Résumés created" ≈ "jobs you apply to."
- **Hard daily cost cap** — all LLM spend is metered and capped per day (cross-process, atomic); see `core/budget_guard.py`.
- **US-only, strict** — word-boundary location classification + sponsorship matching; never queues foreign-variant postings.
- **Freshness-gated** — captures real `posted_at` from each ATS; skips stale reqs (the early-applicant edge).
- **Truthful résumés** — keyword injection is grounded in the candidate profile (no JD-phrase stuffing); a quality reviewer flags fabrication.
- **Safe data store** — single JSON source of truth with cross-process file locking, atomic writes, union-merge, corrupt-state guard, and a DB-sync prune tripwire (`core/state_store.py`).
- **Human-in-the-loop** — nothing is submitted unattended by default; an opt-in, CAPTCHA-free auto-submit lane exists behind explicit gates.
- **Referral surfacing** — the biggest callback lever: per-role LinkedIn connection-search links in the dashboard and daily digest.

## Stack

- **Backend**: Python · FastAPI · APScheduler (macOS launchd) · Postgres (UI mirror) · ChromaDB
- **LLM**: Claude (Opus for tailoring, Haiku for scoring/review) with prompt caching
- **Frontend**: Next.js + React (review-queue dashboard)
- **Apply**: Chrome MV3 extension (form autofill + résumé attach)

## Layout

```
agents/        discovery (board scanner, ATS resolver, fit), application (tailor, intake,
               on-demand tailor, dedup ledger), outreach (referral), monitoring, learning
core/          state_store, budget_guard, llm, config
api/           FastAPI app (applications, autofill resolver for the extension)
frontend/      Next.js dashboard
extension/     Chrome autofill extension
orchestration/ scheduler (launchd) + CLI runner
tests/         data-integrity + autofill resolver unit tests
```

## Setup

1. `python -m venv .venv && .venv/bin/pip install -r requirements.txt`
2. Copy `.env.example` → `.env` and fill in keys (Anthropic, Gmail OAuth, etc.).
3. `frontend/`: `npm install && npm run dev`
4. API: `uvicorn api.main:app --host 127.0.0.1 --port 8000`
5. Scheduler: `python -m orchestration.scheduler` (or the launchd agent).

> Secrets live in `.env`; all runtime state and PII (`data/`) is git-ignored.

## Status

Personal project, single-user. Not affiliated with any of the companies whose public ATS boards it reads.
