"""Archivist query surface — guarded read-only SQL against the team store (techne#554).

The Archivist could hand you things but you could not interrogate it: `archivist.db`
was reachable ONLY through federation fan-out inside recall, which pushes results at
you in response to a topic string. "Which decisions touch cabal and have no issue?"
and "what did we learn last week that superseded something?" are plain reads, and
every one of them was impossible without opening the sqlite file by hand.

This is the access half of team memory; techne#553 (writer provenance) is the schema
half. A query surface without provenance answers "what do we know" but never "who
says so", which is the half that makes the first half trustworthy.

WHY SQL AND NOT A FILTER DSL. A structured filter surface answers the questions we
thought of when we wrote it. The whole complaint in #554 is questions nobody wrote an
endpoint for, so a bespoke-verb-per-question surface reproduces the gap it is meant
to close. The cost of SQL is that it must be GUARDED, which is the rest of this file.

=== THE GUARD STACK ===

Four independent layers. Each is enforced by SQLite itself, not by convention, and
none of them is load-bearing alone:

  1. ?mode=ro          — writes to the db CONTENT are physically impossible.
  2. set_authorizer    — checked by SQLite during prepare. Denies ATTACH, all but one
                         PRAGMA, and every read outside ALLOWED_TABLES.
  3. progress handler  — aborts a runaway query. Read-only does not mean cheap.
  4. single statement  — one SELECT, executed through a parameterless execute().

THE AUTHORIZER IS THE LOAD-BEARING ONE, and it is why this file does not "parse the
SQL and check the table names". A parse-and-inspect allow-list is a regex over SQL
that loses to the first sub-select nobody thought of; measured, the authorizer denies
`SELECT id FROM knowledge WHERE id IN (SELECT id FROM observations)` on the inner
read, with no parsing on our side at all.

ATTACH IS THE ONE TO UNDERSTAND. A read-only connection can still ATTACH another
database and read it — so without an ATTACH deny, this "team store" surface is a path
to read the operator's PERSONAL techne.db out through it. mode=ro does not stop that;
only the authorizer does.

=== FOUR MEASURED FACTS THAT A NAIVE VERSION OF THIS FILE GETS WRONG ===

All of them fail SILENTLY or read as a bad query rather than as a broken guard, which is
why they are recorded here rather than left to the next reader to rediscover. Probed
against a copy of the real 25MB archivist.db, python 3.14.7 / sqlite 3.53.4.

1. FTS5 NEEDS `PRAGMA data_version`. Querying `knowledge_fts` issues
   SQLITE_PRAGMA('data_version') inside the vtab constructor and SQLITE_READ against
   the shadow tables. Under a blanket PRAGMA deny every MATCH query dies with
   "vtable constructor failed" — which looks like malformed SQL, not like a guard
   that is switched too far up. Hence _PRAGMA_ALLOWED, deliberately a set of exactly
   one, and hence the shadow tables in ALLOWED_TABLES.

2. A CTE'S TRANSIENT TABLE IS ITSELF A SQLITE_READ, and its name is not a real table,
   so an allow-list check on the name alone denies legitimate queries. The
   discriminator is arg3, the database name: a real table read carries 'main', a
   transient one carries None. A plain non-recursive CTE needs NO exemption — its
   reads are attributed to the real underlying table (dbname='main', CTE name in
   arg4), so the allow-list still governs what it can reach. Recursive CTEs
   additionally need SQLITE_RECURSIVE.

3. READ-ONLY DOES NOT BOUND COST. `WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT
   x+1 FROM c WHERE x<200000000)` and a three-way cross join over `knowledge` both run
   unbounded on a connection that cannot write a byte. The progress handler is the
   only thing that stops them: measured, interrupted at 0.64s and 0.31s respectively.

4. THE AUTHORIZER'S DEFAULT BRANCH MUST RECORD WHAT IT DENIED, not merely deny it.
   Without that, ATTACH — the single most important thing here — escaped as a bare
   `sqlite3.DatabaseError: not authorized` traceback rather than a QueryDenied naming
   the surface. The guard worked; the REPORTING of it did not, and the tests could not
   tell the difference because they accepted either exception type. Found by running
   the CLI once. See `_authorizer`.

=== WHAT IS NOT HERE, ON PURPOSE ===

NO WRITE VERBS, EVER, for any caller. The write path is `sink:archivist` with its own
gates (see archivist.py); a query surface that can mutate is a second, ungoverned
write path into team memory. That is #554's second design constraint and it is
enforced twice over — the connection cannot write, and the authorizer denies it
before the connection is asked to.

DISTILLED ONLY. `observations` — the raw thread-summary tier, 17,756 rows on the
operator's box — is NOT readable here, verified denied both directly and through
`observations_fts`. Raw team observations are the widest egress surface in the system
and this path landed BEFORE the distilled-only egress decision, not after it; opening
the raw tier is a decision to take deliberately, on its own, not to inherit from a
query surface that happened to ship first.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

#: The readable surface: distilled team knowledge and nothing else. `knowledge_fts`
#: cannot be read without its shadow tables (finding 1 above) — they hold the same
#: content in an index encoding, so including them widens nothing.
ALLOWED_TABLES = frozenset(
    {
        "knowledge",
        "knowledge_deps",
        "knowledge_fts",
        "knowledge_fts_config",
        "knowledge_fts_data",
        "knowledge_fts_docsize",
        "knowledge_fts_idx",
    }
)

#: The tables a caller is meant to think in terms of — what `schema` reports and what
#: a denial message names. The shadow tables are an implementation detail of FTS5 and
#: listing them as though they were part of the interface invites queries against
#: their internal encoding, which is not a stable surface.
PUBLIC_TABLES = ("knowledge", "knowledge_deps", "knowledge_fts")

#: EXACTLY ONE pragma. `data_version` is read-only, carries no memory content, and
#: FTS5 does not work without it (finding 1). Everything else is denied — including
#: the pragma_* table-valued functions, which the authorizer sees as reads of a
#: `pragma_<name>` table that is not in ALLOWED_TABLES.
_PRAGMA_ALLOWED = frozenset({"data_version"})

#: Action codes worth naming in a denial message. An explicit map, NOT a reverse
#: lookup over sqlite3's SQLITE_* constants — those collide (SQLITE_DENY and
#: SQLITE_CREATE_INDEX are both 1), so a generated map reports the wrong verb with
#: total confidence. Anything unmapped degrades to "action <n>", which is honest.
_ACTION_NAMES = {
    sqlite3.SQLITE_ATTACH: "ATTACH",
    sqlite3.SQLITE_DETACH: "DETACH",
    sqlite3.SQLITE_INSERT: "INSERT",
    sqlite3.SQLITE_UPDATE: "UPDATE",
    sqlite3.SQLITE_DELETE: "DELETE",
    sqlite3.SQLITE_DROP_TABLE: "DROP TABLE",
    sqlite3.SQLITE_DROP_VIEW: "DROP VIEW",
    sqlite3.SQLITE_DROP_INDEX: "DROP INDEX",
    sqlite3.SQLITE_CREATE_TABLE: "CREATE TABLE",
    sqlite3.SQLITE_CREATE_INDEX: "CREATE INDEX",
    sqlite3.SQLITE_ALTER_TABLE: "ALTER TABLE",
    sqlite3.SQLITE_TRANSACTION: "transaction control",
}

#: Rows returned unless the caller asks for fewer. Deliberately small: this surface
#: answers questions, and a question whose answer is 5,000 rows was asked wrong.
DEFAULT_LIMIT = 100

#: The ceiling a caller cannot raise. `knowledge` is ~1,100 rows on the operator's
#: store, so this reads the whole distilled tier and still bounds the response.
MAX_LIMIT = 2000

#: Progress-handler budget, in units of 1,000 VDBE ops (so ~50M ops). Calibrated from
#: the measurement in finding 3: the runaway recursive CTE trips this at ~0.64s, while
#: every legitimate query measured against the real store — including FTS MATCH with
#: bm25 ranking and a GROUP BY over all of `knowledge` — completes in well under 1,000
#: units. A budget, not a timeout: it bounds WORK, so it does not punish a query that
#: is merely waiting on a busy writer.
OP_BUDGET = 50_000
#: Largest string or blob a guarded query may build or return (techne#1256).
#: Archivist rows are text a few KB long; 1 MB, the issue's figure, leaves room.
MAX_VALUE_BYTES = 1_000_000
#: Total size of the string and blob values one query may return.
MAX_RESULT_BYTES = 16_000_000
#: Longest SQL text the surface accepts.
MAX_SQL_BYTES = 100_000

#: Outcome codes. THREE, not two, and the third is the point (#554 constraint 1).
#: An empty result must be distinguishable from an unasked question, and BOTH must be
#: distinguishable from "there is no team store bound here" — otherwise a standalone
#: install reports that the team knows nothing, which is the exact failure mode the
#: recall ladder's `loose-match-only` exists to prevent. Measured three times in one
#: session before #550/#552: no-results read as nothing-exists, and a duplicate issue
#: got filed off the back of it.
OUTCOME_ROWS = "rows"
OUTCOME_NO_ROWS = "no-rows"
OUTCOME_STORE_ABSENT = "store-absent"
OUTCOME_SCHEMA = "schema"

_OUTCOME_SUMMARY = {
    OUTCOME_ROWS: "Rows returned.",
    OUTCOME_NO_ROWS: (
        "The query ran and matched nothing. This is an ANSWER — the Archivist holds no "
        "row satisfying it — and not a failure or an empty store. Before concluding "
        "nothing exists, consider that the store is distilled: a thing we did may be "
        "recorded in different words than you searched for."
    ),
    OUTCOME_STORE_ABSENT: (
        "No Archivist is bound on this instance, so nothing was queried. This is NOT "
        "'the team knows nothing' — no store was opened at all. A standalone instance "
        "is not bound to a separate store; set archivist_db_path, or archivist: store."
    ),
    OUTCOME_SCHEMA: (
        "No sql given — this is the readable schema, not a result set. Write a SELECT "
        "against these tables and call again."
    ),
}


class QueryDenied(Exception):
    """The query asked for something outside the guarded surface.

    A distinct type because a denial is a STATEMENT ABOUT THE INTERFACE, not a
    database error: the caller should learn the surface and re-ask, where a genuine
    OperationalError means their SQL was wrong. Callers that flatten both into one
    error string teach neither.
    """


def _jsonable(row: dict) -> dict:
    """Render a row so it survives JSON serialisation.

    BLOBS ARE REACHABLE ON THE PERMITTED SURFACE, which is why this is not defensive
    programming: `knowledge_fts_data.block` is a BLOB and that table is in
    ALLOWED_TABLES, so `SELECT * FROM knowledge_fts_data` is an ALLOWED query that
    succeeded and then had its rows thrown away — the MCP dispatcher serialises with
    `json.dumps(..., indent=2)` and no `default=`, so `bytes` raised TypeError, its
    blanket `except Exception` caught it, and the caller got
    `Error: Object of type bytes is not JSON serializable`.

    That is a THIRD failure category in a surface whose contract has two (QueryDenied =
    learn the interface; sqlite3.Error = your SQL is wrong), and it reads to a caller as
    a server bug. Exactly the shape of this module's own finding 4: the guard worked,
    the reporting did not.

    FIXED HERE, IN THE SEAM THAT OWNS THE ROWS, rather than in the dispatcher — that
    response path is shared by 34 other tools and techne#695 spent three review rounds
    establishing how expensive changing it is. Coercing at the source also fixes the CLI
    and any future consumer at once. A marker rather than a decoded string on purpose:
    an index block is not text, and mojibake would be less useful to a caller than its
    size. (Prudence, #707 review.)
    """
    return {k: _cell(v) for k, v in row.items()}


def _cell(v):
    """Coerce one value to something a CONFORMANT JSON parser will accept.

    TWO CASES, and the second is worse than the first even though it looks milder.

    BLOBS raise on the way out — loud, catchable, and the caller at least learns
    something went wrong (see `_jsonable`).

    NON-FINITE FLOATS DO NOT. `json.dumps` emits bare `Infinity` / `NaN` quite happily,
    so no exception is raised and `isError` is never set — but those tokens are NOT
    valid JSON (RFC 8259) and a conformant client rejects the WHOLE response. Python's
    own `json.loads` is lenient, which is exactly why a Python-side test does not see
    it; parsing with `parse_constant` set to raise gives
    `ValueError: Infinity is not valid JSON`. So the failure travels all the way to a
    non-Python client and presents there as a corrupt response from us.

    Reaching it takes an out-of-range literal (`SELECT 1e400`) — no honest question
    does that, and `confidence_numeric` is bounded — so this is a nit, not a bug. It is
    fixed anyway because it is the same family as the BLOB case, its reporting is
    strictly worse, and this is now the obvious home for it. (Prudence, #707 round 2.)
    """
    import math

    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<blob, {len(v)} bytes>"
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)  # "inf" / "-inf" / "nan" — a string is valid JSON; the token was not
    return v


def _archivist_path(config) -> Path | None:
    """The Archivist db path, or None when this instance has no team store.

    Resolves WITHOUT creating the file — `archivist_db()` would init an empty store,
    which turns "no Archivist here" into "an Archivist with nothing in it" and
    collapses OUTCOME_STORE_ABSENT into OUTCOME_NO_ROWS. Reuses archivist.py's
    resolver so the path this reads is by construction the path the sink writes.
    """
    from techne.core.archivist import ARCHIVIST_STORE, _resolve_archivist_path, archivist_binding

    # Only a SEPARATE store is queryable here. Under `local` the "team" knowledge is
    # this instance's own `knowledge` table, which `techne_search` and `techne_recall`
    # already answer — offering a second surface onto the same rows, labelled as the
    # team store, would report one source as two (techne#552).
    if archivist_binding(config) != ARCHIVIST_STORE:
        return None

    path = _resolve_archivist_path(config)
    return path if path.exists() else None


def _authorizer(denied: list[str], ops: list[str]):
    """Build the authorizer callback. *denied* collects refused TABLES and *ops*
    refused OPERATIONS, so the caller can be told what it actually asked for rather
    than just "not authorized".

    THE DEFAULT BRANCH MUST RECORD. It did not, in the first version of this file,
    and the result was that ATTACH — the single most important thing the authorizer
    blocks — escaped as a bare `sqlite3.DatabaseError: not authorized` traceback
    instead of a QueryDenied that says what the surface is. The tests did not catch
    it because they accepted either exception type; running the CLI did. If you
    widen this, keep both lists fed.

    SQLite calls this during prepare, once per object the statement touches. Default
    is DENY: a new action code added by a future SQLite is refused rather than waved
    through, which is the correct direction for a guard to fail.
    """

    def authorize(action, arg1, _arg2, dbname, _trigger):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
            # SQLITE_FUNCTION is allowed unconditionally, which INVERTS this callback's
            # default-DENY principle, so the assumption it rests on is stated rather
            # than left implicit: THERE ARE NO APPLICATION-DEFINED FUNCTIONS ON THIS
            # CONNECTION. Verified — nothing registers `create_function` /
            # `create_aggregate` on the `_open_remote_readonly` path, and SQLite itself
            # refuses `load_extension` (Python leaves extension loading disabled). But
            # that opener belongs to federation, not to this module: if federation ever
            # registers a helper on it, this surface inherits it silently. If you add
            # one, this branch needs an allow-list. (Prudence, #707 review.)
            #
            # SECOND INHERITED ASSUMPTION, same opener (Prudence, #707 round 2):
            # `_open_remote_readonly` sets neither `detect_types` nor `text_factory`,
            # so sqlite3 can only ever hand back NoneType/int/float/str/bytes — which
            # is why `_cell` has exactly two cases to handle. If federation ever adds
            # `detect_types=PARSE_DECLTYPES`, columns declared TIMESTAMP arrive as
            # `datetime` objects and pass straight through uncoerced.
            return sqlite3.SQLITE_OK
        # Recursive CTEs are allowed; their cost is bounded by OP_BUDGET, not by
        # forbidding them. `knowledge_deps` is a graph and walking a supersession
        # chain is exactly the kind of question this surface exists to answer.
        if action == sqlite3.SQLITE_RECURSIVE:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA:
            if arg1 in _PRAGMA_ALLOWED:
                return sqlite3.SQLITE_OK
            denied.append(f"PRAGMA {arg1}")
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            # dbname is None for a transient table — a CTE's own name, or a subquery
            # alias. It is not a database object and cannot be checked against the
            # allow-list (finding 2). Reads of REAL data always carry dbname='main'
            # and are governed below, so this exemption cannot launder a denied
            # table: a recursive CTE selecting FROM observations is denied on the
            # observations read itself, which is pinned by a test.
            if dbname is None:
                return sqlite3.SQLITE_OK
            if arg1 in ALLOWED_TABLES:
                return sqlite3.SQLITE_OK
            denied.append(arg1)
            return sqlite3.SQLITE_DENY
        ops.append(_ACTION_NAMES.get(action, f"action {action}"))
        return sqlite3.SQLITE_DENY

    return authorize


def _connect(path: Path, denied: list[str], ops: list[str]) -> sqlite3.Connection:
    """A guarded read-only connection to the Archivist.

    Opened via federation's `_open_remote_readonly` rather than a second ?mode=ro
    opener of our own. That function carries the measured WAL-sidecar caveat in its
    docstring, and a parallel opener drifts from it the moment either side learns
    something — the same copy-not-mechanism shape #518 found five times.
    """
    from techne.core.federation import _open_remote_readonly

    conn = _open_remote_readonly(path)
    conn.set_authorizer(_authorizer(denied, ops))
    # Set AFTER the authorizer: both are per-connection, but the ordering makes the
    # read obvious — nothing may run before both guards are installed.
    state = {"n": 0}

    def _tick():
        state["n"] += 1
        return 1 if state["n"] > OP_BUDGET else 0

    conn.set_progress_handler(_tick, 1000)
    # The op budget counts VM steps, and a C function runs as one: zeroblob(5e7)
    # or a replace() over hex() took ~590 MB in a single step (techne#1256). Cap
    # the size of any string or blob a query can build, and of the SQL text.
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
    return conn


def readable_schema(config) -> dict:
    """The tables and columns a caller may read, for the no-sql discovery call.

    Read on a SEPARATE plain connection, not the guarded one — the guard denies
    PRAGMA and sqlite_master by design, and relaxing it so the surface can describe
    itself would punch a hole for the sake of documentation. This is internal code
    reading fixed table names, never caller input.
    """
    path = _archivist_path(config)
    if path is None:
        return {
            "outcome": {
                "code": OUTCOME_STORE_ABSENT,
                "summary": _OUTCOME_SUMMARY[OUTCOME_STORE_ABSENT],
            },
            "tables": {},
        }
    from techne.core.federation import _open_remote_readonly

    conn = _open_remote_readonly(path)
    tables: dict[str, list[str]] = {}
    try:
        for name in PUBLIC_TABLES:
            cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
            if cols:
                tables[name] = [c["name"] for c in cols]
    finally:
        conn.close()
    return {
        "outcome": {"code": OUTCOME_SCHEMA, "summary": _OUTCOME_SUMMARY[OUTCOME_SCHEMA]},
        "tables": tables,
        "row_limit": {"default": DEFAULT_LIMIT, "max": MAX_LIMIT},
    }


def _too_big(e: Exception) -> QueryDenied:
    return QueryDenied(
        f"A value in this query passed the {MAX_VALUE_BYTES:,}-byte limit ({e}). The "
        "limit bounds the size of any one value a query builds (techne#1256); select "
        "less, or a substr() of it."
    )


def query(config, sql: str | None = None, limit: int | None = None) -> dict:
    """Run one guarded read-only SELECT against the Archivist (techne#554).

    With no *sql*, returns the readable schema — the discovery call. An agent cannot
    write SQL against a schema it cannot see, so without this the first query is
    always a guess; coding it as OUTCOME_SCHEMA rather than as an empty result keeps
    "I did not ask" distinct from "the answer is nothing".

    Returns columns, rows, row_count, a truncation flag, and an outcome. TRUNCATION IS
    OBSERVED, NOT INFERRED: we fetch limit+1 and report the flag, because `len(rows)
    == limit` is indistinguishable between "exactly that many" and "more, withheld",
    and a caller that guesses wrong concludes it has the whole answer.
    """
    if sql is None or not sql.strip():
        return readable_schema(config)

    path = _archivist_path(config)
    if path is None:
        return {
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "outcome": {
                "code": OUTCOME_STORE_ABSENT,
                "summary": _OUTCOME_SUMMARY[OUTCOME_STORE_ABSENT],
            },
        }

    try:
        cap = DEFAULT_LIMIT if limit is None else max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError) as e:
        # The inputSchema says `integer`, but NOTHING IN THIS SERVER VALIDATES ARGUMENTS
        # AGAINST IT — the schema is documentation to the client, not a gate. So a
        # caller that sends "abc" gets int()'s message verbatim, which describes our
        # internals rather than their mistake. Same class as the ValueError escaping
        # the CLI below: a third failure mode in a surface whose contract says two.
        raise QueryDenied(
            f"limit must be a whole number, got {limit!r}. "
            f"Omit it for {DEFAULT_LIMIT}, or pass 1-{MAX_LIMIT}."
        ) from e
    denied: list[str] = []
    ops: list[str] = []
    conn = _connect(path, denied, ops)
    try:
        try:
            cur = conn.execute(sql)
        except sqlite3.ProgrammingError as e:
            # sqlite3 refuses multiple statements itself. Re-raised as a denial
            # because "you may send one statement" is a fact about this surface.
            raise QueryDenied(f"{e} This surface takes exactly one SELECT.") from e
        except sqlite3.DatabaseError as e:
            if isinstance(e, sqlite3.DataError):
                raise _too_big(e) from e
            if ops:
                refused = sorted(set(ops))
                detail = (
                    # Said only when it applies. ATTACH needs its reason spelled out
                    # because "read-only" sounds like it should already cover it and
                    # does not; DELETE does not need to be told why it is refused.
                    " A read-only connection CAN otherwise attach another database and "
                    "read the personal store out through the team surface — which is "
                    "why this is denied and not merely impossible."
                    if "ATTACH" in refused
                    else " The write path is sink:archivist, with its own gates."
                )
                raise QueryDenied(
                    f"Refused: {', '.join(refused)}. This surface is READ-ONLY and "
                    f"cannot reach outside the Archivist (techne#554).{detail}"
                ) from e
            if denied:
                raise QueryDenied(
                    f"Not readable here: {', '.join(sorted(set(denied)))}. "
                    f"This surface reads {', '.join(PUBLIC_TABLES)} and nothing else — "
                    "the raw observation tier and the personal store are deliberately "
                    "out of reach (techne#554)."
                ) from e
            raise
        # Iterate rather than fetchmany: each value is capped by MAX_VALUE_BYTES,
        # but up to MAX_LIMIT rows of them were materialised at once, ~600 MB at
        # 200 rows (Prudence, PR 1311). The running total stops a query at
        # MAX_RESULT_BYTES, before the rows behind it are fetched.
        rows, total = [], 0
        for r in cur:
            total += sum(len(v) for v in r if isinstance(v, (str, bytes)))
            if total > MAX_RESULT_BYTES:
                raise QueryDenied(
                    f"The result passed the {MAX_RESULT_BYTES:,}-byte total limit after "
                    f"{len(rows)} row(s) (techne#1256). Select fewer rows or columns, "
                    "or a substr() of the large ones."
                )
            rows.append(_jsonable(dict(r)))
            if len(rows) > cap:
                break
        columns = [d[0] for d in (cur.description or [])]
    except sqlite3.DataError as e:
        raise _too_big(e) from e
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e).lower():
            raise QueryDenied(
                "Query exceeded the work budget and was stopped. It is read-only, so "
                "nothing was harmed — but an unbounded join or recursion can still "
                "run forever. Narrow it, or add a LIMIT inside the query."
            ) from e
        raise
    finally:
        # Drop the authorizer before close so the callback cannot be re-entered by a
        # connection teardown, and so a leaked reference to `conn` is inert.
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
        conn.close()

    truncated = len(rows) > cap
    if truncated:
        rows = rows[:cap]
    code = OUTCOME_ROWS if rows else OUTCOME_NO_ROWS
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "outcome": {"code": code, "summary": _OUTCOME_SUMMARY[code]},
    }
