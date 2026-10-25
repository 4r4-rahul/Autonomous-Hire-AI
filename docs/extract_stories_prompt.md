# VS Code prompt — mine your work repos into résumé/interview "stories"

**How to use:** On your company laptop, open the VS Code workspace that contains all your repos
(the folder with JPMorgan / Insight Global / Alignment / Ameriprise / side projects). Open the
Chat panel, make sure it can see the whole workspace, and paste **everything inside the box below**.
When it finishes, copy its YAML output and send it to me — I'll merge it into the experience bank
and the résumé tailor will start drawing differentiated bullets from it automatically.

> **Safety / confidentiality:** the prompt tells the agent to extract *your transferable engineering
> decisions* — NOT proprietary code, secrets, client names, or internal system names. Read the output
> before sharing; redact anything your employer would consider confidential. Stories must be things
> you can defend in an interview as your own work.

---

```
You have access to all the repositories in this workspace. These are codebases I (the engineer)
have worked on across my career. I'm rebuilding my résumé and prepping for interviews, and I need
you to mine these repos for REAL, differentiated engineering stories — the kind that show HOW I
solved hard problems, not just what I worked on.

STEP 1 — ATTRIBUTE THE WORK TO ME.
- My git author identity may appear under my personal name, personal email aliases, or my company email.
- Use `git log --author`, `git blame`, and commit history to find the code *I* actually wrote/owned.
- Only build stories from work I authored or clearly led. Skip code that's purely someone else's.
- If you can't attribute something to me, leave it out rather than guess.

STEP 2 — FIND THE DIFFERENTIATED DECISIONS (the "how").
For each repo, look for places where I made a non-obvious engineering call. Strong signals:
- Performance/cost work: query/Spark tuning, partitioning, caching, batching, indexing, parallelism.
- Reliability/correctness: retries, idempotency, locking/concurrency, fault tolerance, data-quality
  gates, schema/contract validation, backfills, exactly-once handling.
- Architecture choices: why one design over the obvious alternative; tradeoffs I accepted.
- Hard debugging / root-cause fixes: a subtle bug, what the symptom was, how I traced it.
- Scale: volumes handled, throughput, latency targets, SLAs.
- Migrations/platform work: on-prem→cloud, IaC, CI/CD, multi-cloud, orchestration.
Read commit messages, PR descriptions, READMEs, config, and the code itself for evidence.

STEP 3 — WRITE ONE STORY PER NON-OBVIOUS DECISION, using EXACTLY this YAML shape:

entries:
  - source: "<Company or Project> — <my role/title if known> (<years if known>)"
    repo: "<repo name>"
    tech: [<the real stack: languages, frameworks, cloud services, data tools>]
    stories:
      - problem: "<the hard problem / constraint — why it mattered>"
        what_didnt_work: "<the obvious approach that failed or wasn't enough — omit if none>"
        decision: "<the specific non-obvious thing I did — the 'how', be concrete and technical>"
        tradeoff: "<what I gave up / accepted to get the win — omit if genuinely none>"
        impact: "<the result; use a real number ONLY if the repo/commit/README supports it,
                 otherwise describe the outcome qualitatively — do NOT invent metrics>"
        tech: [<the specific tools used in THIS story>]
        shows: [<which themes this proves, from: scale, cost, latency, reliability, concurrency,
                data integrity, correctness, architecture, debugging, migration, ai, mlops,
                api integration, security, observability, automation, dx>]
        evidence: "<repo/file path or commit hash — for MY reference only>"

STEP 4 — RULES (important):
- TRUTH ONLY. Every story must be something I can defend in an interview. Never invent a metric,
  a tradeoff, or a technique the code doesn't actually show. A plain true story beats an impressive
  fake one.
- CONFIDENTIALITY. Do NOT include proprietary source code, secrets/credentials, internal hostnames,
  client/customer names, or confidential business metrics. Describe my engineering decisions in
  transferable terms (e.g. "a regulated financial dataset" not the real system name).
- NUMBERS. If a real number exists (a config value, a documented SLA, a benchmark in the repo, a
  commit that says "reduced X from A to B"), use it and cite it in `evidence`. If not, stay
  qualitative and don't fabricate a percentage.
- DEPTH OVER BREADTH. 2–4 genuinely strong, distinct stories per repo beats ten generic ones.
  Each story should make a recruiter think "this person solved something real."
- COVER MY RANGE. I target data-platform/data-engineering, ML/AI-platform, AI/LLM-engineering,
  backend/systems, and solutions-engineering roles. If a repo demonstrates more than one of these,
  note it via the `shows` tags so each can be surfaced for the right job.

STEP 5 — AFTER THE YAML, add a short section titled "INTERVIEW NOTES" with, per story, a 2–3 sentence
spoken-form version (STAR: situation, what I did, result) I can use to answer "tell me about a time…".
Keep these truthful and in the first person.

Output the YAML first (ready to paste into a file), then the INTERVIEW NOTES.
```

---

## When you get the output back

1. Read it; redact anything confidential.
2. Send it to me (or paste it under `entries:` in `data/experience_bank.yaml`, after the AutonomousHire entry).
3. The tailor picks up the file automatically on next restart — every résumé then draws JD-matched,
   real "how" bullets from your actual work. The `INTERVIEW NOTES` section is yours for prep (not used
   by the system).
