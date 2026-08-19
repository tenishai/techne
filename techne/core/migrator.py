"""Unified schema-migration engine (techne#136).

ONE place for the lock + backup + versioned step-runner and the additive schema
reconciler that every db-per-domain store routes through. Extracted from
techne.db's mature machinery (`techne/core/database.py`); the domain stores
(codex / architect / librarian / generic) replace their hand-rolled, divergent
copies by calling these helpers.

Design contract (load-bearing — see techne#136):
  * The engine does NOT own version-writing. `run_steps` writes the version —
    core inserts a `schema_version` row per step (its append-log + partial-resume
    semantics); domains call their `VersionIO.write` at each step's end. This is
    why `guarded_migrate` takes `read_version` but never writes.
  * The fcntl lock is the ONLY serialization. Migrations are not wrapped in an
    outer transaction (`executescript` auto-commits; steps issue their own
    COMMITs). The in-lock double-check makes a racing loser a true no-op.
  * The reconciler is ADDITIVE only (tables / columns / triggers / fts-backfill).
    Destructive changes (DROP/rebuild) go through a numbered `run_steps`
    migration and must keep a STRUCTURAL idempotency guard (column-presence) —
    never gate a destructive step on the version number: a retrofit-versioned
    domain reads `user_version = 0` on every existing file, so version cannot
    distinguish fresh / old-shape / already-migrated.
"""

from __future__ import annotations

import glob as _glob
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

try:
    import fcntl  # POSIX only; absent on Windows
except ImportError:  # pragma: no cover - techne targets macOS/Linux
    fcntl = None


# --- version accessors ------------------------------------------------------


@dataclass(frozen=True)
class VersionIO:
    """How a db reads + writes its schema version. Pluggable so core keeps its
    audited `schema_version` table while domains use the zero-ceremony
    `PRAGMA user_version` (techne#136 — the version store is a parameter, not a
    fifth mechanism)."""

    read: Callable[[sqlite3.Connection], int]
    write: Callable[[sqlite3.Connection, int], None]


# `PRAGMA user_version` — a single int baked in the SQLite header. The default
# for domain stores (codex/architect/librarian); fresh files read 0.
user_version_io = VersionIO(
    read=lambda conn: conn.execute("PRAGMA user_version").fetchone()[0],
    write=lambda conn, v: conn.execute(f"PRAGMA user_version = {int(v)}"),
)


# --- backup -----------------------------------------------------------------

# Both knobs are module constants with env overrides rather than config.yaml
# entries: migrator.py deliberately imports nothing from techne (see the module
# docstring — it is the low-level seam other stores reuse), so reaching for
# Config here would introduce the cycle that separation exists to avoid.

#: Reuse an existing backup for the same (from_v, to_v) if it is younger than
#: this. A FAILING migration retries on every hook invocation, and each retry
#: used to copy the whole database first — measured at 12 full copies of one
#: v18→v19 transition (#574), 11 of them from attempts that never wrote a
#: version row. Within the window the earlier backup is also the BETTER rollback
#: point: it predates every failed attempt, so it cannot contain partially
#: migrated state. Beyond the window a fresh copy is taken, because the live DB
#: has moved on and restoring a stale snapshot would discard real work.
def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back loudly rather than dying.

    `int(os.environ.get(...))` at module scope makes a TYPO IN A VALUE FATAL TO
    THE WHOLE PACKAGE: this module is the low-level seam every store imports, so
    `TECHNE_BACKUP_KEEP=three` raised ValueError at import and techne became
    wholly unimportable. An operator knob must not be able to do that
    (agents#578 r2).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            f"[techne] ignoring {name}={raw!r} — not an integer; using {default}",
            file=sys.stderr,
        )
        return default


BACKUP_REUSE_WINDOW_S = _env_int("TECHNE_BACKUP_REUSE_WINDOW_S", 3600)

#: How many backups to keep per database. One is written per migration RUN (not
#: per step), so this is "the last N upgrade events". Nothing pruned these
#: before: 1.4 GB had accumulated here, oldest 6 weeks (#574).
BACKUP_KEEP = _env_int("TECHNE_BACKUP_KEEP", 3)

#: How long a `.db.bak.partial` may live before it is treated as an orphan
#: rather than a copy in flight. Deliberately much shorter than the reuse window:
#: a copy either completes or dies quickly, and an hour of accumulation during a
#: retry loop is the growth this module exists to bound.
BACKUP_PARTIAL_TTL_S = _env_int("TECHNE_BACKUP_PARTIAL_TTL_S", 300)


def _prune_backups(backup_dir: Path, stem: str, keep: int, protect: Path) -> list[Path]:
    """Delete all but the newest ``keep`` backups for ``stem``. Returns the deleted.

    KNOWN RESIDUAL (#574 review): a future-dated file sorts newest here and so
    occupies a keep slot, evicting a genuine recent backup. It costs a SLOT only
    — backup_db skips skewed candidates rather than stopping at them, so reuse
    still works. Left deliberately: treating future mtimes as junk risks deleting
    a real backup written under clock skew, which is the worse error.

    ``protect`` is never removed even if ``keep`` is 0 — deleting the backup the
    caller is about to rely on would be the failure this whole mechanism exists
    to prevent. Failures to unlink are swallowed deliberately: a migration must
    not die because housekeeping could not remove a file.

    A file that does NOT match the shape below is never pruned, and that is
    deliberate rather than an oversight to fix later: it was not produced by
    this naming scheme, so the pruner cannot know what it is or whether anything
    depends on it, and refusing to delete it is the right default for
    destructive code. The cost is that such a file is immortal, which is
    acceptable while nothing is still *producing* unmatched names.
    """
    found = sorted(
        # SHAPE match, not prefix match. `{stem}-v*` also matches a sibling store
        # whose own name starts with "<stem>-v" — a domain called `techne-vault`
        # had its backups deleted by `techne`'s prune. Domains are
        # user-configurable strings (config.stores), so requiring digits either
        # side of "-to-v" narrows it sharply. NOT airtight: a store literally
        # named `techne-v2` produces `techne-v2-v1-to-v2-*.db.bak`, which still
        # matches `techne`'s glob. Filename-shape scoping cannot fully separate
        # stores whose names collide by construction; doing that properly means
        # a per-store subdirectory, which is a bigger change than this fix.
        (
            p
            for p in backup_dir.glob(f"{_glob.escape(stem)}-v[0-9]*-to-v[0-9]*-*.db.bak")
            if p.is_file()
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    deleted: list[Path] = []
    for stale in found[max(keep, 0) :]:
        if stale == protect:
            continue
        try:
            stale.unlink()
            deleted.append(stale)
        except OSError:
            continue
    return deleted


def _sweep_partials(backup_dir: Path, stem: str, ttl_s: int) -> list[Path]:
    """Reclaim `.db.bak.partial` orphans for one database, across ALL transitions.

    Lives here rather than in `backup_db` because this is where the naming scheme
    already lives, and because it must be scoped by STEM rather than by
    transition: an orphan from a v18->v19 attempt is invisible to a later
    v19->v20 run, so a per-transition sweep leaves it immortal (agents#578 r3).

    The rename in `backup_db` keeps a failed copy out of the reuse glob and out
    of the keep slots — both verified — at the cost of making it invisible to the
    pruner too. In a change whose subject is bounding growth that converts
    dangerous-but-bounded litter into safe-but-unbounded litter, on the exact
    precondition the docstring below names: the disk fills BECAUSE backups
    accumulated.

    TTL rather than unconditional, because a young partial may belong to a copy
    still in flight. The migration lock serialises hooks, but nothing stops an
    operator running a second process by hand. Failures are swallowed:
    housekeeping must not kill a migration.
    """
    swept: list[Path] = []
    now = datetime.now().timestamp()
    for dead in backup_dir.glob(f"{_glob.escape(stem)}-v[0-9]*-to-v[0-9]*-*.db.bak.partial"):
        try:
            if dead.is_file() and (now - dead.stat().st_mtime) > ttl_s:
                dead.unlink()
                swept.append(dead)
        except OSError:
            continue
    return swept


def backup_db(conn: sqlite3.Connection, db_path: Path, from_v: int, to_v: int) -> Path | None:
    """Snapshot the DB to ``<dir>/backups/`` before a migration rewrites it.

    Uses SQLite's online backup API so the copy is consistent even in WAL mode
    (a plain file copy can miss un-checkpointed WAL frames). Returns the backup
    path, or ``None`` when there's nothing worth saving (absent/empty DB).
    Migrations are forward-only, so this backup is the rollback path (#78).

    Two bounds on growth (#574), because a backup that fills the disk stops being
    a safety net: a recent backup for the SAME transition is reused rather than
    re-copied, and older backups beyond :data:`BACKUP_KEEP` are pruned. Both
    announce on stderr — this module already narrates the migration there, and a
    deletion the operator cannot see is worse than one they can.

    Reuse cannot mask partial progress, and the reason is subtle enough to state
    here rather than leave in a commit message: the caller reads ``from_v``
    under the migration lock, so if any step succeeded, ``from_v`` has advanced
    and the ``(from_v, to_v)`` key changes — which forces a fresh copy. Reuse
    therefore happens only when *no* version row was written, which is exactly
    the case where the earlier file is the better rollback point.
    """
    if not db_path.exists() or db_path.stat().st_size == 0:
        return None
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # BEFORE the copy, and unconditionally. `_prune_backups` is called only after
    # a successful reuse match or a successful replace — so if `conn.backup()`
    # raises, neither runs, and on a full disk each attempt adds another orphan
    # while the reclamation that would free space never executes (agents#578 r3).
    _sweep_partials(backup_dir, db_path.stem, BACKUP_PARTIAL_TTL_S)

    # Reuse before copying. Same transition, still fresh → the existing file is
    # both cheaper and a strictly better rollback point (it predates any failed
    # attempt, so it holds no partially-migrated state).
    # TWO STRINGS, BECAUSE THERE ARE TWO JOBS. `glob.escape` is an identity on
    # ordinary stems only by luck: for a stem containing `*`, `?` or `[` it
    # REWRITES THE BYTES, and one string was being used both as a glob PATTERN
    # and as the literal NAME written to disk. Measured: stem `data[ab]` wrote
    # `data[[]ab]-v1-to-v2-….db.bak`, and the next call's pattern then matched the
    # literal `data[ab]-…` — a name nothing produces. Neither reuse nor prune
    # fired, silently, for exactly the stems the escape was added to protect
    # (agents#578 r3). My r3 nit fix introduced this.
    prefix = f"{db_path.stem}-v{from_v}-to-v{to_v}-"                      # names
    pattern = f"{_glob.escape(db_path.stem)}-v{from_v}-to-v{to_v}-"       # globs
    now = datetime.now().timestamp()
    # is_file() BEFORE stat(), matching _prune_backups ten lines up. That
    # asymmetry was a real defect: is_file() returns False for a dangling
    # symlink where .stat() RAISES, and nothing between here and connect()
    # catches it — so one dangling symlink in backups/ meant the store would not
    # open at all. A generator also re-stats lazily, so a file removed between
    # the glob and the sort cannot abort the migration either.
    for existing in sorted(
        (q for q in backup_dir.glob(f"{pattern}*.db.bak") if q.is_file()),
        key=lambda q: q.stat().st_mtime,
        reverse=True,
    ):
        # Three-way, and the ordering of the branches is the whole point.
        #
        # A future mtime makes `now - mtime` NEGATIVE, which satisfies an upper
        # bound at any magnitude — so `age <= WINDOW` alone reuses a skewed file
        # FOREVER. But clamping to `0 <= age <= WINDOW` and falling through to
        # the break is the opposite failure: a future-dated file sorts NEWEST,
        # fails the lower bound, and the break then hides every valid in-window
        # backup behind it — so one skewed file permanently disables reuse and
        # restores the 12-copy behaviour this exists to prevent.
        #
        # `continue` on skew keeps the early-exit for the genuinely-stale case
        # while letting the scan step past it. Note the two residuals compound if
        # you get this wrong: prune keeps the skewed file (it sorts newest and
        # holds a keep slot), and a retained skewed file would keep reuse off.
        age = now - existing.stat().st_mtime
        if age < 0:
            continue  # clock skew — older candidates may still be valid
        if age <= BACKUP_REUSE_WINDOW_S:
            print(
                f"[techne] reusing backup for v{from_v}→v{to_v}: {existing.name}",
                file=sys.stderr,
            )
            # PRUNE ON THIS PATH TOO. `return existing` short-circuited the
            # function and the prune call sits after the copy, so in the exact
            # incident this PR was written for — v18→v19 failing on every hook
            # invocation, twelve copies already on disk — twelve went in and
            # twelve came out. And if that migration then SUCCEEDS, which is the
            # hoped-for outcome, prune does not run again for this database
            # until the next schema bump, which could be months. The 1.4 GB
            # stays (agents#578 r2).
            reused_pruned = _prune_backups(
                backup_dir, db_path.stem, BACKUP_KEEP, protect=existing
            )
            if reused_pruned:
                print(
                    f"[techne] pruned {len(reused_pruned)} old backup(s), "
                    f"keeping {BACKUP_KEEP}: "
                    + ", ".join(q.name for q in reused_pruned),
                    file=sys.stderr,
                )
            return existing
        # Pure optimisation, and deliberately NOT pinned by a test: with valid
        # mtimes, if the newest candidate is out of window every older one is
        # too, so removing this changes cost and not outcome. Mutation confirms
        # it survives the suite — that is correct here rather than a gap.
        break

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{prefix}{ts}.db.bak"

    # WRITTEN UNDER A .partial NAME AND RENAMED, so a file only ever bears the
    # real name once the copy has RETURNED. `sqlite3.connect(dest)` creates the
    # destination BEFORE `conn.backup()` writes a page — measured, connect-only
    # leaves it at 0 bytes — so any failure between the two (full disk, SIGTERM,
    # hook timeout, source locked) used to leave a 0-byte file whose name matched
    # the reuse glob, with an mtime of now.
    #
    # THE REUSE BRANCH IS WHAT MADE THAT DANGEROUS. Before it, every invocation
    # took a fresh copy and a leftover partial was inert litter. With reuse, that
    # empty file sorts newest, sits inside the window, and is PREFERRED — then
    # announced to the operator as their rollback point. Restoring from it raises
    # "no such table", discovered only after run_steps has already moved the
    # schema forward, and migrations are forward-only (#78), so there is no
    # second rollback path. The precondition is the one in this docstring: the
    # disk fills BECAUSE backups accumulated (agents#578 r2).
    #
    # The rename is stronger than a size check because it also keeps a partial
    # out of the pruner: it can neither be reused nor consume a keep slot, and
    # no new predicate has to stay correct.
    partial = backup_dir / f"{prefix}{ts}.db.bak.partial"
    dest = sqlite3.connect(str(partial))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    os.replace(partial, backup)

    pruned = _prune_backups(backup_dir, db_path.stem, BACKUP_KEEP, protect=backup)
    if pruned:
        # NAME them. A bare count is visible but not useful: an operator reading
        # "pruned 1 old backup(s)" cannot tell whether it took the one they were
        # about to restore from. This is the rollback path, so the line is worth
        # its length — and a long one only happens on the first prune of a
        # directory that was never pruned before.
        print(
            f"[techne] pruned {len(pruned)} old backup(s), keeping {BACKUP_KEEP}: "
            + ", ".join(q.name for q in pruned),
            file=sys.stderr,
        )
    return backup


# --- guarded migration ------------------------------------------------------


def guarded_migrate(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    target_version: int,
    read_version: Callable[[sqlite3.Connection], int],
    run_steps: Callable[[sqlite3.Connection, int, int], None],
    backup: bool = True,
    label: str = "schema",
) -> None:
    """Run pending migrations exactly once, even under concurrent processes.

    Claude Code fires session_start + post_tool_use near-simultaneously, so on
    the first open after a code bump several processes can all see a stale
    version and race into the migration body. A cross-process file lock
    serializes them; the loser re-reads the version under the lock and no-ops.
    We back up first and announce on stderr (#78).

    The lock is filesystem-level (not a SQLite transaction) because a migration's
    ``executescript`` calls issue their own COMMITs and can't be held inside one
    outer transaction. Per-machine locking is sufficient — each machine has its
    own db file. ``run_steps(conn, current, target)`` owns version-writing.
    """
    lock_path = db_path.with_suffix(db_path.suffix + ".migrate.lock")

    def _do() -> None:
        # Double-checked: a process we raced may have migrated while we waited.
        current = read_version(conn)
        if current == target_version:
            return
        if current > target_version:
            # THE STORE CLAIMS A SHAPE THE CODE HAS NEVER DEFINED (techne#676).
            #
            # This was folded into `current >= target_version` and returned in
            # silence. For `==` that is the correct no-op; for `>` it is not a
            # no-op at all, it is an INCONSISTENCY — and it was found the
            # expensive way. architect.db has read `user_version = 4` since at
            # least 2026-08-13 while every revision of the code declared
            # ARCHITECT_VERSION = 3 and no v3→v4 step has ever existed. Six days
            # of migrations were skipped with no error, no warning, no log line.
            #
            # WE WARN RATHER THAN RAISE, deliberately. This runs on the capture
            # hot path via connect(); raising would take every session down over
            # a condition that is inconsistent but not actively destructive. The
            # observable half belongs in `techne doctor`, where a human reads it
            # — see check_schema_versions.
            #
            # THE REAL DANGER IS NOT THIS SKIP, IT IS THE HOLE IT LEAVES. A store
            # stamped ahead has rungs of the ladder that never ran. When the code
            # later passes it, migration resumes from the STAMP rather than from
            # the shape, so those rungs stay unrun forever. Structural
            # idempotency guards (column-presence, never version-presence) are
            # what actually protect against that — which is exactly why this
            # module's own contract insists on them, and why a step gated only on
            # `current < N` is not enough on a store whose N cannot be trusted.
            print(
                f"[techne] WARNING: {label} at v{current} is AHEAD of this code's "
                f"v{target_version} ({db_path.name}). Migrations are being SKIPPED. "
                "No step above the code's version has ever run, so the version "
                "stamp does not describe this store's real shape. "
                "Run `techne doctor` for the full picture.",
                file=sys.stderr,
            )
            return
        note = ""
        if backup:
            bkp = backup_db(conn, db_path, current, target_version)
            note = f" (backup: {bkp})" if bkp else ""
        print(f"[techne] migrating {label} v{current} → v{target_version}{note}", file=sys.stderr)
        run_steps(conn, current, target_version)

    if fcntl is None:  # no file locking available — run unguarded (legacy behaviour)
        _do()
        return
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            _do()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


# --- schema reconciler (additive convergence toward a DDL string) -----------
#
# Generalized from core's reconciler: the four passes take the target DDL as an
# argument instead of reading a module global, so any domain can converge its db
# toward its own schema. Order is load-bearing: tables → triggers → fts_backfill
# → columns (a trigger may reference a not-yet-created table; backfill needs the
# triggers in place for future writes). Index reconciliation lands last in a
# follow-up (techne#138).


def _strip_sql_comment(line: str) -> str:
    """Drop an inline ``-- …`` comment from a DDL line.

    Column lines may carry a trailing comment (e.g.
    ``status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'synced'``). The
    reconciler copies the column definition verbatim into an ``ALTER TABLE … ADD
    COLUMN``; leaving the comment (and the mid-line comma before it) in produces
    invalid DDL and crashes the universal ``connect()`` (techne#132).

    Naive line-level strip: assumes no column's ``DEFAULT``/``CHECK`` carries a
    literal ``--`` inside a string (e.g. ``CHECK (x <> 'a--b')``). Revisit if that
    ever changes.
    """
    return re.sub(r"\s*--.*$", "", line)


def _schema_table_statements(schema_sql: str) -> list[tuple[str, str]]:
    """Parse *schema_sql* and return (table_name, create_sql) pairs.

    Each pair includes the CREATE [VIRTUAL] TABLE statement plus any
    immediately-following CREATE INDEX / CREATE UNIQUE INDEX lines that belong to
    that table (i.e. appear before the next CREATE TABLE). Virtual tables (FTS5)
    are included. Associated triggers live in the triggers DDL, not here. Index
    reconciliation on already-existing tables is out of scope here (techne#138).
    """
    table_re = re.compile(
        r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.IGNORECASE
    )

    results: list[tuple[str, str]] = []
    current_name: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        if current_name and current_lines:
            results.append((current_name, "\n".join(current_lines).strip()))

    # Statement boundaries are the CREATE TABLE lines themselves (and EOF): every
    # line after a CREATE TABLE accumulates into that table's block — its body,
    # its following CREATE INDEX lines, and any interleaved comments — until the
    # next CREATE TABLE. A comment is never a boundary, so a `--` anywhere in the
    # body cannot split a statement (techne#56 — the prior comment-based flush
    # could prematurely close a block on a mid-body comment).
    for line in schema_sql.splitlines():
        m = table_re.match(line.strip())
        if m:
            flush()  # close the previous table's block
            current_name = m.group(1)
            current_lines = [line]
        elif current_name is not None:
            current_lines.append(line)

    flush()
    return results


def _schema_columns(table: str, schema_sql: str) -> set[str]:
    """Parse column names for *table* out of *schema_sql* using regex."""
    pattern = rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.+?)\n\);"
    m = re.search(pattern, schema_sql, re.DOTALL)
    if not m:
        return set()
    block = m.group(1)
    cols = set()
    for line in block.splitlines():
        line = _strip_sql_comment(line).strip().rstrip(",")
        if not line or line.upper().startswith(
            ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "REFERENCES", "--")
        ):
            continue
        col_name = line.split()[0]
        cols.add(col_name)
    return cols


def _trigger_statements(triggers_sql: str) -> list[tuple[str, str]]:
    """Parse *triggers_sql* and return (trigger_name, create_sql) pairs.

    Each statement spans ``CREATE TRIGGER IF NOT EXISTS <name>`` through ``END;``.
    """
    pattern = re.compile(
        r"(CREATE\s+TRIGGER\s+IF\s+NOT\s+EXISTS\s+(\w+).+?END\s*;)",
        re.IGNORECASE | re.DOTALL,
    )
    return [(m.group(2), m.group(1).strip()) for m in pattern.finditer(triggers_sql)]


def _trigger_source_table(trigger_name: str, triggers_sql: str) -> str | None:
    """Return the content-table *trigger_name* fires on (``AFTER … ON <table>``)."""
    for name, create_sql in _trigger_statements(triggers_sql):
        if name != trigger_name:
            continue
        m = re.search(r"AFTER\s+(?:INSERT|UPDATE|DELETE)\s+ON\s+(\w+)", create_sql, re.IGNORECASE)
        return m.group(1) if m else None
    return None


def _normalize_ddl(sql: str) -> str:
    """Normalize a CREATE statement for drift comparison: drop the optional
    ``IF NOT EXISTS`` (sqlite_master never stores it), collapse whitespace, and
    strip a trailing ``;``. A semantically-identical live/canonical pair then
    compares equal, while a real body change (e.g. an FTS trigger going from
    content-only to content+tags+kind) does not."""
    s = re.sub(r"\bIF\s+NOT\s+EXISTS\b", " ", sql, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s.rstrip(";").strip()


def _warn_drift(kind: str, name: str) -> None:
    """Surface a drifted schema object (techne#360). Reconcile is deliberately
    additive — it won't heal a redefinition — so a body change under an existing
    name would otherwise apply SILENTLY never (the #355 trigger-drift trap). This
    makes it visible; the fix is a numbered migration that DROP+CREATEs it."""
    print(
        f"[techne] reconcile: WARNING {kind} '{name}' has DRIFTED — its live "
        f"definition differs from SCHEMA. Additive reconcile cannot heal a "
        f"redefinition; ship a numbered migration to DROP+CREATE it (techne#360).",
        file=sys.stderr,
    )


def reconcile_tables(conn: sqlite3.Connection, schema_sql: str) -> None:
    """Create any tables present in *schema_sql* but absent from the live DB.

    Runs BEFORE reconcile_columns so newly-created tables don't then need a
    separate column pass. Virtual tables (FTS5) are handled alongside regular ones.
    """
    existing = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow')")
    }
    for name, create_sql in _schema_table_statements(schema_sql):
        if name not in existing:
            conn.executescript(create_sql)
            conn.commit()
            print(f"[techne] reconcile: CREATE TABLE {name}", file=sys.stderr)


def reconcile_triggers(conn: sqlite3.Connection, triggers_sql: str) -> None:
    """Create any triggers present in *triggers_sql* but absent from the live DB.

    Skips triggers whose source table doesn't yet exist (e.g. ``documents_ai``
    before ``documents``); the next connect() installs them after the source
    table is reconciled.
    """
    existing_tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow')")
    }
    existing = {
        row[0]: row[1]
        for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
    }
    for name, create_sql in _trigger_statements(triggers_sql):
        if name in existing:
            if _normalize_ddl(existing[name] or "") != _normalize_ddl(create_sql):
                _warn_drift("trigger", name)
            continue
        src = _trigger_source_table(name, triggers_sql)
        if src is not None and src not in existing_tables:
            continue
        conn.executescript(create_sql)
        conn.commit()
        print(f"[techne] reconcile: CREATE TRIGGER {name}", file=sys.stderr)


def reconcile_fts_backfill(
    conn: sqlite3.Connection,
    fts_backfill: dict[str, tuple[str, list[str]]],
    fts_insert_cols: dict[str, list[str]],
) -> None:
    """Backfill any FTS table whose index is empty while its source has rows.

    Uses the FTS5 shadow ``_docsize`` table to detect an empty index (a content-
    FTS5 VT reports the source COUNT(*) even when never populated, so COUNT on the
    FTS table is not a reliable signal). Safe to re-run; runs after triggers exist
    so future writes stay in sync. *fts_backfill*/*fts_insert_cols* are caller-
    supplied because the rowid source + COALESCE wrapping can't be derived from DDL.
    """
    for fts_table, (src_table, src_cols) in fts_backfill.items():
        fts_indexed = conn.execute(f"SELECT COUNT(*) FROM {fts_table}_docsize").fetchone()[0]
        if fts_indexed > 0:
            continue
        src_count = conn.execute(f"SELECT COUNT(*) FROM {src_table}").fetchone()[0]
        if src_count == 0:
            continue
        insert_cols = ", ".join(fts_insert_cols[fts_table])
        select_cols = ", ".join(src_cols)
        conn.execute(
            f"INSERT INTO {fts_table}({insert_cols}) SELECT {select_cols} FROM {src_table}"
        )
        conn.commit()
        print(
            f"[techne] reconcile: backfill {fts_table} ({src_count} rows from {src_table})",
            file=sys.stderr,
        )


def reconcile_columns(conn: sqlite3.Connection, schema_sql: str) -> None:
    """Add any columns present in *schema_sql* but absent from the live table."""
    table_pattern = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\(", re.IGNORECASE)
    for m in table_pattern.finditer(schema_sql):
        table = m.group(1)
        live_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not live_cols:
            continue  # table doesn't exist yet; CREATE TABLE IF NOT EXISTS handles it
        for col in _schema_columns(table, schema_sql) - live_cols:
            block_m = re.search(
                rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.+?)\n\);",
                schema_sql,
                re.DOTALL,
            )
            col_def = col  # fallback: bare name (SQLite infers NULL affinity)
            if block_m:
                for line in block_m.group(1).splitlines():
                    stripped = _strip_sql_comment(line).strip().rstrip(",")
                    parts = stripped.split()
                    if parts and parts[0] == col:
                        col_def = stripped
                        break
            # SQLite cannot ADD a NOT NULL column without a DEFAULT to an existing
            # (possibly populated) table. Skip + warn rather than crash connect();
            # such a column needs a numbered migration, not the additive path.
            upper = col_def.upper()
            if "NOT NULL" in upper and "DEFAULT" not in upper:
                print(
                    f"[techne] reconcile: SKIP {table}.{col} — NOT NULL without DEFAULT "
                    "cannot be added to an existing table; use a numbered migration",
                    file=sys.stderr,
                )
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            conn.commit()
            print(f"[techne] reconcile: ALTER TABLE {table} ADD COLUMN {col_def}", file=sys.stderr)


def _index_statements(schema_sql: str) -> list[tuple[str, str]]:
    """Parse *schema_sql* for standalone ``CREATE [UNIQUE] INDEX IF NOT EXISTS``
    statements (multi-line supported — a negated class matches across newlines up
    to the terminating ``;``). Returns (index_name, create_sql) pairs."""
    pattern = re.compile(
        r"(CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)[^;]*;)",
        re.IGNORECASE,
    )
    return [(m.group(2), m.group(1).strip()) for m in pattern.finditer(schema_sql)]


def reconcile_indexes(conn: sqlite3.Connection, schema_sql: str) -> None:
    """Create any index present in *schema_sql* but absent from the live DB
    (techne#138). The pre-#138 reconciler only emitted a table's indexes when the
    *table* was missing, so an index added to an already-existing table never
    landed on upgraded dbs. Runs LAST — after column reconciliation — so an index
    can reference a column the column pass just added. ``CREATE INDEX IF NOT
    EXISTS`` is idempotent; we diff first only to skip work + log accurately.

    Additive by index *name*: if an index's definition changes under the same
    name it is NOT reconciled (the name is already present) — that's a redefinition
    and, per the SCHEMA_SQL-only-is-additive convention, requires a numbered
    migration + version bump, not the reconciler."""
    existing = {
        row[0]: row[1]
        for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index'")
    }
    for name, create_sql in _index_statements(schema_sql):
        if name in existing:
            if _normalize_ddl(existing[name] or "") != _normalize_ddl(create_sql):
                _warn_drift("index", name)
            continue
        conn.execute(create_sql)  # one statement per entry; matches reconcile_columns
        conn.commit()
        print(f"[techne] reconcile: CREATE INDEX {name}", file=sys.stderr)


def reconcile_schema(
    conn: sqlite3.Connection,
    schema_sql: str,
    *,
    triggers_sql: str = "",
    fts_backfill: dict[str, tuple[str, list[str]]] | None = None,
    fts_insert_cols: dict[str, list[str]] | None = None,
) -> None:
    """Converge a db toward *schema_sql* additively — the combined reconcile pass for
    domains whose evolution is purely additive (new table/column/index/trigger; no
    destructive change). Order is load-bearing (see module docstring):
    tables → triggers → fts_backfill → columns → indexes.

    A domain's triggers usually live in the same DDL string, so pass
    ``triggers_sql=schema_sql``. ``fts_backfill`` is only needed by a db that gained
    FTS *after* rows already existed (core techne.db) — omit it otherwise. NOT for a
    db with destructive/numbered migrations (codex/architect): those run their
    versioned step under ``guarded_migrate`` first, and a structure-diffing column
    reconcile would mangle an old-shape table the numbered step means to rebuild.
    """
    reconcile_tables(conn, schema_sql)
    if triggers_sql:
        reconcile_triggers(conn, triggers_sql)
    if fts_backfill:
        reconcile_fts_backfill(conn, fts_backfill, fts_insert_cols or {})
    reconcile_columns(conn, schema_sql)
    reconcile_indexes(conn, schema_sql)
