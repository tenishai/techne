# Contributing to techne

## Getting a change in

```bash
git clone https://github.com/tenishai/techne.git
cd techne
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                  # the full suite; -n auto if you want it parallel
ruff check techne tests
```

Then: branch, commit, open a PR. Keep the suite green — CI runs pytest on 3.12
and 3.13 on every push.

A few things worth knowing before you spend effort:

- **Tests live beside the code they cover**, in `tests/`, and a behaviour change
  wants a test that fails without it. "It is covered by an existing test" is worth
  checking rather than assuming — reverting your change and watching a test fail is
  the cheap way to know.
- **Architecture cards.** Load-bearing subsystems have a card in `docs/arch/`, each
  pinned to its source file by a content hash. Change such a subsystem and you update
  its card in the same commit, hash included. `python scripts/check_arch_cards.py`
  tells you; so does CI.
- **Schema changes have their own rules**, below. Read that section before touching
  anything that creates or alters a table — picking the wrong mechanism either leaves
  existing databases broken or burns a version bump that was not needed.
- **AGENTS.md** is the context file for AI coding agents, and `CLAUDE.md` is a
  one-line bridge to it. If you add a rule an agent must follow, it belongs there,
  not only here.

Issues are the place for design discussion. A PR that changes behaviour is easier to
review when it says what it measured, not only what it intends.

## Schema evolution

Two parallel mechanisms manage the SQLite schema. Pick the right one — using the wrong one either leaves old databases broken or burns a SCHEMA_VERSION bump on a change that didn't need one.

---

### Use `_migrate()` when

- The change requires a **data backfill** (e.g., populating a new column from an old one).
- The change is **structurally destructive** — DROP TABLE, DROP COLUMN, index rebuild that requires DROP + CREATE.
- You need future migration steps to be able to **assume a structural precondition** (e.g., "column X definitely exists") that a purely-additive reconciler pass can't guarantee.

Steps:

1. Bump `SCHEMA_VERSION` at the top of `techne/core/database.py`.
2. Add a `if from_version < N and to_version >= N:` block inside `_migrate()`.
3. Update `SCHEMA_SQL` to reflect the post-migration state (so fresh databases get the right definition from the start).

Real examples:
- **v2** — `outbox` status columns + backfill from `synced_at` encoding.
- **v4** — `sessions` summary split; existing `summary` left in place (provenance indeterminate).
- **v5** — `confidence_numeric` + `last_confirmed_at` with backfill from legacy enum.
- **v8** — `observations_fts` rebuild after schema change (DROP + CREATE is destructive).

---

### Use SCHEMA_SQL only (no SCHEMA_VERSION bump) when

The change is **purely additive**: new nullable column, new table, new index, new trigger. The reconciler (`_reconcile_tables` / `_reconcile_columns` / `_reconcile_triggers`) runs on every connect and diffs SCHEMA_SQL against the live DB, so the change lands automatically the next time any code opens a connection.

**Post-#140 caveat:** connect()-time reconcile is sentinel-gated and skips databases that have already been reconciled at the current schema version. An additive SCHEMA_SQL change at an **unchanged SCHEMA_VERSION** will NOT automatically reach already-reconciled databases — they have passed the gate and won't re-run the diff. To force re-application on an existing DB, run `techne reconcile` (re-stamps the sentinel) or bump SCHEMA_VERSION (which always triggers a full reconcile pass).

Steps:

1. Add the column / table / index to `SCHEMA_SQL`. That's it.
2. If deploying to existing DBs without a version bump, remind users to run `techne reconcile` once.

Real examples:
- `extension_state` table (issue #18) — new table, no migration needed.
- `event_at` column on `observations` (issue #8) — new nullable column.
- `summary_llm_source` column on `sessions` (issue #1) — new nullable column.

---

### What `schema_version` actually tracks

`schema_version` tracks **semantic compatibility**, not structural completeness. A database at version N can have any additive structure reconciled in without a version bump. That means two databases at the same `schema_version` can differ in which additive columns / tables they have — and that's intentional.

Consequence: if you write a migration step that reads a column introduced additively (i.e., never bumped SCHEMA_VERSION for it), call `_reconcile_columns()` before reading. The reconciler is idempotent and safe to call inside a migration block.

---

### Historical inconsistency — v7

v7 (observation `tags` / `kind` columns) was a purely additive `ALTER TABLE`; it did not require a backfill or a DROP. Under the convention above it should have gone through SCHEMA_SQL only. It was bumped anyway. Leave it — the migration is harmless, the column definitions are correct, and retrofitting it would rewrite history without benefit. Don't use v7 as a model for future additive changes.

---

### Decision tree

```
Does the change touch existing data (backfill, transform, rename)?
  Yes → _migrate() + bump SCHEMA_VERSION + update SCHEMA_SQL
  No  → Is the change destructive (DROP, index rebuild)?
          Yes → _migrate() + bump SCHEMA_VERSION + update SCHEMA_SQL
          No  → SCHEMA_SQL only (reconciler picks it up on next connect)
```
