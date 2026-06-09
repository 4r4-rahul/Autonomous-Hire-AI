# Assessment: move source-of-truth from JSON → Postgres (Tier 3, deferred)

## Status: NOT done in this pass — assessed + de-risked instead

A full migration of the source-of-truth from `data/pipeline_state.json` to Postgres is a large
(multi-day), high-risk refactor: every reader and writer across `agents/`, `api/`, `orchestration/`,
and `scripts/` touches the JSON file directly (16+ modules). Doing it halfway is more dangerous than
not doing it. So this pass **de-risked the JSON-as-truth model** instead of replacing it.

## What was done to neutralize the acute risks (Tier 0 + Tier 3 increment)
- **Atomic + cross-process-locked writes** — `core/state_store.py` (`filelock` + `os.replace`).
  Eliminates lost-update corruption and truncated-file crashes (the root cause of the duplicate /
  stale-row fires this cycle). All runtime writers (scheduler, on_demand_tailor) go through it.
- **Corrupt-state guard** — `load_state()` raises `StateCorruptError` on an unparseable file; the
  scheduler aborts the job instead of silently returning empty (which previously let the DB-sync
  prune-wipe everything).
- **DB-sync prune tripwire** — `db/migrate_from_json.py` refuses to prune when the incoming JSON
  has <50% of the DB's job count, so a partial state can't cascade-delete the tables.
- **Lead age-out** — `dedup_state` drops app-less leads older than 45 days to bound file growth.

## Why a full migration is still the right eventual move
- The 26 MB file is rewritten wholesale on every scheduler tick (O(file) per tick).
- Postgres is already populated with a sound schema (`db/models.py`) and the UI reads from it.
- Eliminates the entire JSON↔DB drift class and the merge-sync complexity.

## Recommended migration path (when undertaken as a dedicated effort)
1. Add a thin repository layer (`core/repo.py`) wrapping all reads/writes the pipeline needs
   (job_listings, applications, fit_scores), backed by Postgres.
2. Migrate writers one subsystem at a time behind the repo (intake → apply → on-demand → monitoring),
   keeping the JSON export as a derived artifact for any remaining consumers.
3. Move the dedup/freshness/decision-preservation invariants (currently in `state_store.dedup_state`
   and `migrate_from_json`) into DB constraints + upsert logic.
4. Delete the JSON-as-truth path once all writers are migrated; keep a periodic JSON export only if
   a script still needs it.
5. Backfill tests (the data-integrity suite in `tests/unit/test_data_integrity.py` should be ported
   to exercise the repo layer).

Effort: L (multi-day). Prerequisite: the data-integrity test coverage added this pass.
