"""Crash-recovery fault injection — SIGKILL mid-write, then prove the store (techne#388).

WHY THIS EXISTS. techne is a stateful daemon over SQLite: a persistent MCP server
plus a hook lifecycle, all writing one file. The interesting bugs live in
INTERRUPTED TRANSITIONS — a half-written transaction, a migration killed between
its work and its version stamp — and unit tests never exercise them, because a
unit test always gets to finish. Resilience here was untested at the "what happens
when the process dies" level.

THE KILL IS DETERMINISTIC, AND THAT IS THE WHOLE DESIGN. The obvious construction
— start a worker, sleep, SIGKILL from the parent — races the scheduler, and this
repo already carries one timing flake that a loaded runner loses
(tests/test_watch.py:85, techne#1155). A crash test that fails randomly is worse
than no crash test: it trains people to re-run CI until it is green, which is
exactly how a real regression gets waved through.

So the CHILD KILLS ITSELF at a chosen line — `os.kill(os.getpid(), SIGKILL)` —
after doing precisely the work each scenario needs. There is no timing window, no
sleep, and no scheduler dependency; the process dies at a point the test names.
SIGKILL specifically, not SIGTERM: it cannot be caught, so no cleanup, no atexit,
no context manager unwind runs. That is the crash being modelled.

WHAT IS ACTUALLY BEING TESTED is our USAGE, not SQLite. SQLite's own atomicity is
not in question and this suite would be worthless as a test of it. What can be
wrong is ours: a multi-statement write that is not wrapped in one transaction, a
migration that stamps its version before its work is durable, an outbox that marks
a row delivered before the delivery lands. Each test below names the way OUR code
could be wrong, and would still pass on a correct SQLite if our code were right.

MARKED `crash` AND EXCLUDED FROM THE DEFAULT RUN, then run by a dedicated CI job.
That distinction is load-bearing here (ARCHITECTURE.md makes it): exclusion alone
does not say whether a suite is covered elsewhere or covered nowhere. These
spawn subprocesses and are slower than a unit test, so they do not belong in the
per-PR default run — but a suite nothing executes is a suite that decays, so the
`crash-recovery` job exists to run them.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.crash

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_until_it_kills_itself(body: str, db: Path) -> subprocess.CompletedProcess:
    """Run `body` in a child that SIGKILLs itself, and assert it really died that way.

    THE ASSERTION ON THE EXIT STATUS IS NOT CEREMONY. A child that raised, or that
    exited cleanly because the code under test moved, would leave the database in
    a perfectly consistent state and every assertion afterwards would pass — a
    green test that injected no fault at all. -SIGKILL (-9) is the proof the crash
    happened.
    """
    script = textwrap.dedent(f"""
        import os, signal, sqlite3, sys
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        DB = {str(db)!r}
        {textwrap.indent(textwrap.dedent(body), " " * 8).lstrip()}
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == -9, (
        f"child was expected to die by SIGKILL, got returncode={proc.returncode}. "
        f"No fault was injected, so anything asserted after this is meaningless.\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return proc


def _integrity_ok(db: Path) -> bool:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


@pytest.fixture()
def db(tmp_path) -> Path:
    from techne.core.database import init_db

    path = tmp_path / "techne.db"
    init_db(path).close()
    return path


# ---------------------------------------------------------------------------
# Interrupted writes
# ---------------------------------------------------------------------------


def test_an_uncommitted_batch_leaves_no_trace(db):
    """A write killed before COMMIT must leave NOTHING behind — not some rows.

    THE DEFECT THIS WOULD CATCH IS OURS, not SQLite's: a batch written with one
    `execute` per row and no enclosing transaction is N separate commits, so a
    crash halfway leaves a HALF-INGESTED session. The ledger would then be
    internally consistent and factually wrong, which is the worst combination —
    nothing downstream can tell it happened.
    """
    _run_until_it_kills_itself(
        """
        conn = sqlite3.connect(DB)
        conn.execute("BEGIN")
        for i in range(50):
            conn.execute(
                "INSERT INTO observations (session_id, tool_name, content) VALUES (?,?,?)",
                ("crash-session", "Bash", f"row-{i}"),
            )
        os.kill(os.getpid(), signal.SIGKILL)
        """,
        db,
    )

    conn = sqlite3.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE session_id = 'crash-session'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert n == 0, f"a killed transaction left {n} rows behind — the write was not atomic"
    assert _integrity_ok(db)


def test_a_committed_batch_survives(db):
    """The other half, and it is what makes the test above meaningful.

    Without this, `test_an_uncommitted_batch_leaves_no_trace` would also pass if
    SIGKILL simply destroyed the file, or if writes never landed at all. Committed
    means durable, even through a signal that cannot be caught.
    """
    _run_until_it_kills_itself(
        """
        conn = sqlite3.connect(DB)
        conn.execute("BEGIN")
        for i in range(50):
            conn.execute(
                "INSERT INTO observations (session_id, tool_name, content) VALUES (?,?,?)",
                ("kept-session", "Bash", f"row-{i}"),
            )
        conn.commit()
        os.kill(os.getpid(), signal.SIGKILL)
        """,
        db,
    )

    conn = sqlite3.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE session_id = 'kept-session'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert n == 50, f"committed rows did not survive the crash: {n}/50"
    assert _integrity_ok(db)


def test_a_crash_with_a_hot_wal_still_reads_back(db):
    """A crash leaves the -wal and -shm files behind; the next opener must replay
    them rather than reporting an empty or corrupt store.

    techne runs in WAL mode, so this is the NORMAL crash state, not an exotic one.
    Asserting the sidecar files exist before reopening keeps the test honest — it
    would otherwise pass on a store that had already checkpointed, proving nothing.
    """
    _run_until_it_kills_itself(
        """
        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content) VALUES (?,?,?)",
            ("wal-session", "Bash", "written into the wal"),
        )
        conn.commit()
        os.kill(os.getpid(), signal.SIGKILL)
        """,
        db,
    )

    assert Path(str(db) + "-wal").exists(), (
        "no -wal file after the crash, so this test is not exercising WAL recovery"
    )

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT content FROM observations WHERE session_id = 'wal-session'"
        ).fetchall()
    finally:
        conn.close()

    assert [r[0] for r in rows] == ["written into the wal"]
    assert _integrity_ok(db)


# ---------------------------------------------------------------------------
# Interrupted migration — the transition the issue names explicitly
# ---------------------------------------------------------------------------


def test_a_migration_killed_mid_work_does_not_claim_to_have_finished(tmp_path):
    """A MIGRATION MUST NOT STAMP ITS VERSION BEFORE ITS WORK IS DURABLE, and a
    crash is the only thing that proves the ordering.

    The codex v2→v3 purge is used because its own comment names this hazard: the
    version write lands AFTER the deletes, so an interruption must leave
    `user_version` at 2 with a store that still opens. The failure being excluded
    is the self-certifying one — a db stamped v3 whose purge never ran, which no
    later open would ever revisit (that is techne#613's shape, reached by a crash
    instead of by a stale checkout).

    Killed INSIDE the batch loop, after at least one batch has been deleted, so
    the store is genuinely mid-transition rather than untouched.
    """
    from techne.core.codegraph import add_symbol
    from techne.core.codex import CODEX_SCHEMA, _apply_codex_migration
    from techne.core.stores import plain_connect

    db = tmp_path / "codex.db"
    _apply_codex_migration(db)
    conn = plain_connect(db)
    conn.executescript(CODEX_SCHEMA)
    conn.commit()
    conn.close()

    vend = "venv/lib/python3.13/site-packages/pkg{}/mod.py"
    for i in range(1200):
        f = vend.format(i)
        add_symbol(db, id=f"{f}::fn::function::{i}", file=f, name="fn", kind="function")
    add_symbol(
        db, id="pkg/real.py::mine::function::1", file="pkg/real.py", name="mine", kind="function"
    )

    conn = plain_connect(db)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    _run_until_it_kills_itself(
        """
        from pathlib import Path
        from techne.core import codex

        real_batch = codex._v3_purge_batch
        seen = []

        def kill_after_one_batch(conn, doomed):
            real_batch(conn, doomed)
            seen.append(1)
            if len(seen) >= 1:
                os.kill(os.getpid(), signal.SIGKILL)

        codex._v3_purge_batch = kill_after_one_batch
        codex._apply_codex_migration(Path(DB))
        """,
        db,
    )

    conn = sqlite3.connect(db)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    finally:
        conn.close()

    assert version == 2, (
        f"user_version advanced to {version} on a migration that never finished — "
        "the store now certifies work that did not happen, and no later open will "
        "revisit it (techne#613's shape, reached by a crash)"
    )
    assert symbols > 0, "the store lost everything rather than rolling back"
    assert _integrity_ok(db)

    # AND IT CONVERGES ON RE-RUN. Surviving a crash is only half the property —
    # a store that is intact but permanently stuck mid-migration is still broken.
    _apply_codex_migration(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        files = [r[0] for r in conn.execute("SELECT DISTINCT file FROM symbols")]
    finally:
        conn.close()
    assert files == ["pkg/real.py"], f"re-run did not converge: {files}"


# ---------------------------------------------------------------------------
# Interrupted delivery
# ---------------------------------------------------------------------------


def test_a_crash_mid_flush_never_marks_an_undelivered_row_synced(db):
    """The outbox's dangerous direction is marking a row DELIVERED when it was not.

    A row wrongly left pending is retried — noisy, harmless. A row wrongly marked
    synced is silently lost forever, and `flush` reporting success for it makes the
    loss invisible. So the crash is injected BETWEEN the send and the bookkeeping,
    which is the only window where the two can disagree.
    """
    from techne.sync.outbox import enqueue, pending_count

    conn = sqlite3.connect(db)
    conn.close()
    for i in range(5):
        enqueue(db, "sync:knowledge", {"content": f"entry-{i}"}, deliver=False)
    assert pending_count(db) == 5

    _run_until_it_kills_itself(
        """
        conn = sqlite3.connect(DB)
        # Stand in for a delivery that succeeded and a bookkeeping write that had
        # not been committed when the process died.
        conn.execute("BEGIN")
        conn.execute("UPDATE outbox SET status = 'synced' WHERE status = 'pending'")
        os.kill(os.getpid(), signal.SIGKILL)
        """,
        db,
    )

    conn = sqlite3.connect(db)
    try:
        synced = conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'synced'").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0]
    finally:
        conn.close()

    assert synced == 0, f"{synced} rows claim delivery the crash interrupted — silent data loss"
    assert pending == 5, f"expected all 5 rows still pending for retry, got {pending}"
    assert _integrity_ok(db)


def test_the_harness_fails_when_no_fault_is_injected(db):
    """THE SUITE'S OWN GUARD. Every test above is meaningless if the child quietly
    exits 0 — the database would be consistent and every assertion would pass.

    This proves the harness rejects that case, so a future edit that stops the
    child from dying fails loudly instead of turning the whole file into
    decoration. Same shape as the mutation checks used elsewhere in this repo:
    a guard nobody has seen fail is a guard nobody knows works.
    """
    with pytest.raises(AssertionError, match="expected to die by SIGKILL"):
        _run_until_it_kills_itself("pass", db)
