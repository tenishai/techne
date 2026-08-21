---
card: arch-card:techne-archivist-query
updated: 2026-08-21
anchor: techne/core/archivist_query.py sha=5b8eee4402ccf6d69ca7d9a2a3810727e6057965
---
# archivist-query — guarded read-only SQL over the team store (techne#554)

SEAM: `query(config, sql, limit)` + `readable_schema(config)`. The ACCESS half of team memory; techne#553 (writer provenance) is the schema half — a query surface without provenance answers "what do we know" but never "who says so".
WHY SQL, NOT A FILTER DSL: #554's complaint is questions nobody wrote an endpoint for; a verb-per-question surface reproduces the gap it closes. The price is that it must be guarded, which is the whole module.

GUARD STACK — four layers, each enforced by SQLite, none load-bearing alone:
  1. ?mode=ro (via federation._open_remote_readonly — NOT a second opener; that one carries the measured WAL-sidecar caveat)
  2. set_authorizer — the load-bearing one. Denies ATTACH, all-but-one PRAGMA, every read outside ALLOWED_TABLES. Default branch is DENY, so a future SQLite action code is refused rather than waved through.
  3. set_progress_handler (OP_BUDGET) — read-only does NOT bound cost.
  4. one statement only.
NOT BY PARSING THE SQL. A parse-and-inspect allow-list is a regex that loses to the first sub-select nobody thought of; the authorizer denies `SELECT id FROM knowledge WHERE id IN (SELECT id FROM observations)` on the inner read with no parsing at all.

ATTACH IS THE ONE TO UNDERSTAND: a ?mode=ro connection can still ATTACH the operator's PERSONAL techne.db and read it out through this "team" surface. mode=ro does not stop that; only the authorizer does.

THREE MEASURED FACTS a naive version gets wrong — all fail SILENTLY or read as bad SQL rather than as a broken guard (probed against a copy of the real 25MB archivist.db, py3.14.7/sqlite3.53.4):
  1. FTS5 needs `PRAGMA data_version` + SQLITE_READ on the knowledge_fts shadow tables (_config/_data/_docsize/_idx), issued inside the vtab constructor. Blanket PRAGMA deny ⇒ every MATCH dies "vtable constructor failed".
  2. A CTE's transient table is itself a SQLITE_READ; discriminator is arg3 dbname ('main' = real table, None = transient). Recursive CTEs also need SQLITE_RECURSIVE. A plain non-recursive CTE needs NO exemption — its reads are attributed to the real table.
  3. A runaway recursive CTE / 3-way cross join runs unbounded on a connection that cannot write a byte. Interrupted at 0.64s / 0.31s by the progress handler.

DISTILLED ONLY (Richard, 2026-08-21): knowledge, knowledge_deps, knowledge_fts (+ its shadow tables). `observations` — the raw thread-summary tier, 17,756 rows on the operator's box — is NOT readable, verified denied directly AND via observations_fts. This path landed BEFORE the distilled-only egress decision, not after it; opening the raw tier is a deliberate decision, not one inherited from whichever surface shipped first.

INVARIANTS:
- NO WRITE VERBS EVER, any caller. Enforced twice: the connection cannot write, and the authorizer denies it before the connection is asked. The write path is `sink:archivist` with its own gates (core/archivist.py). A query surface that can mutate is a second, ungoverned write path.
- THREE OUTCOMES, not two: `rows` / `no-rows` / `store-absent` (+ `schema` for the no-sql discovery call). An empty result MUST be distinguishable from an unasked question AND from "no team store is bound" — collapsing the third makes a standalone install report that the team knows nothing. Same failure the recall ladder's `loose-match-only` exists to prevent (#550/#552).
- Truncation is OBSERVED, not inferred: fetch cap+1. `len(rows)==limit` is ambiguous between "exactly that many" and "more, withheld".
- `_archivist_path` resolves WITHOUT creating the db — `archivist_db()` would init an empty store, collapsing store-absent into no-rows and erasing the cause of the misconfiguration.
CONSUMERS: mcp_server._techne_archivist_query (returns a denial as a RESULT, so the agent learns the surface instead of getting a bare "Error: not authorized"); cli.py `techne archivist query|schema`.
FAIL-MODES: QueryDenied = a statement about the interface (learn it, re-ask); sqlite3.Error = the caller's SQL is wrong. Flattening both into one string teaches neither. CLI exit codes are the enforceable form of that distinction: 0 ran, 2 refused, 1 bad SQL — and neither error path prints a traceback, which would describe OUR call stack for THEIR mistake.
THE AUTHORIZER'S DEFAULT BRANCH MUST RECORD WHAT IT DENIED (`ops`), not just deny. It did not in the first cut, so ATTACH — the most important thing here — escaped as a bare `DatabaseError: not authorized` traceback rather than a QueryDenied naming the surface. The guard worked; the REPORTING of it did not. The tests accepted `(QueryDenied, sqlite3.DatabaseError)` and so could not tell the difference; running the CLI found it in one command. Tests now assert QueryDenied specifically.
MEASURED SQLITE ORDERING: `DROP TABLE x` is refused as DELETE, not DROP TABLE — SQLite issues SQLITE_DELETE first (a drop deletes rows) and prepare aborts on the first denial. Assert that a write verb appears, never which one; the ordering is SQLite's, not our invariant.
TESTING: tests/test_archivist_query.py. Every guard is pinned by a test that FAILS WHEN THE GUARD IS REMOVED — verified by mutating all nine guards, not by review. Two mutants survived the first pass: the SQLITE_PRAGMA branch was untested (the pragma_* table-valued functions take a different branch), and `test_limit_is_capped` asserted against a 3-row fixture, so it passed with or without the cap (the do-nothing shape of #701). Both fixed. If you widen this surface, mutate it again.
