"""Tests for the curated memory drain (techne#107 PR2).

Covers:
  - ingest_memory_file idempotency (3-outcome content-hash supersede dance)
  - live_curated_memory query + slug scoping
  - curated_memory additive schema reconcile (drop table, re-run init_db)
  - CCMemorySource.initial_walk, health(), provides
  - CLI smoke test for `memory sync`
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from techne.core.curated import ingest_memory_file, live_curated_memory
from techne.core.database import connect, init_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    init_db(p).close()
    return p


@pytest.fixture
def project(tmp_path):
    """A tmp project cwd + a redirected CC config dir (hermetic discovery)."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    config_dir = tmp_path / "ccconfig"
    return cwd, config_dir


def _write_memory(config_dir: Path, cwd: Path, text: str, filename: str = "MEMORY.md") -> Path:
    """Create a memory file under the redirected CC config dir."""
    from techne.platforms.claude_code.paths import project_slug

    mem_dir = config_dir / "projects" / project_slug(cwd) / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    f = mem_dir / filename
    f.write_text(text, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# ingest_memory_file -- 3-outcome idempotency
# ---------------------------------------------------------------------------


def test_ingest_fresh_insert(db, tmp_path):
    """No prior row -> INSERT, stored=True."""
    f = tmp_path / "MEMORY.md"
    f.write_text("# hello\nworld\n")

    row_id, stored = ingest_memory_file(db, f, slug="myslug")
    assert stored is True
    assert row_id > 0


def test_ingest_same_content_noop(db, tmp_path):
    """Same content on second ingest -> no-op, same id returned, stored=False."""
    f = tmp_path / "MEMORY.md"
    f.write_text("# hello\nworld\n")

    id1, s1 = ingest_memory_file(db, f, slug="myslug")
    id2, s2 = ingest_memory_file(db, f, slug="myslug")

    assert s1 is True
    assert s2 is False
    assert id1 == id2

    # Exactly one live row for this path.
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT id FROM curated_memory WHERE source_path=? AND superseded_by IS NULL",
            (str(f),),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1


def test_ingest_changed_content_supersede(db, tmp_path):
    """Content change -> old row superseded, new row inserted, stored=True."""
    f = tmp_path / "MEMORY.md"
    f.write_text("# original content\n")

    id1, s1 = ingest_memory_file(db, f, slug="myslug")
    assert s1 is True

    f.write_text("# changed content\n")
    id2, s2 = ingest_memory_file(db, f, slug="myslug")
    assert s2 is True
    assert id2 != id1

    conn = connect(db)
    try:
        # Old row has superseded_by = new id.
        old_row = conn.execute(
            "SELECT superseded_by FROM curated_memory WHERE id=?", (id1,)
        ).fetchone()
        assert old_row["superseded_by"] == id2

        # Exactly one live row for this source_path.
        live_rows = conn.execute(
            "SELECT id FROM curated_memory WHERE source_path=? AND superseded_by IS NULL",
            (str(f),),
        ).fetchall()
        assert len(live_rows) == 1
        assert live_rows[0]["id"] == id2
    finally:
        conn.close()


def test_concurrent_ingest_of_changed_file_keeps_one_live_row(db, tmp_path):
    """Two concurrent ingests of the same changed path must leave exactly ONE live row.
    BEGIN IMMEDIATE serializes the read→write so the second writer re-reads the
    freshly-superseded row instead of capturing a stale old_id (techne#107 / #3)."""
    import threading

    f = tmp_path / "MEMORY.md"
    f.write_text("# v1\n")
    ingest_memory_file(db, f, slug="s")  # establish the live v1 row
    f.write_text("# v2\n")  # now changed — both workers will ingest this

    barrier = threading.Barrier(2)
    errors: list = []

    def worker():
        try:
            barrier.wait()  # maximize the race
            ingest_memory_file(db, f, slug="s")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    conn = connect(db)
    try:
        live = conn.execute(
            "SELECT COUNT(*) FROM curated_memory "
            "WHERE source_path=? AND superseded_by IS NULL",
            (str(f),),
        ).fetchone()[0]
    finally:
        conn.close()
    assert live == 1


def test_ingest_content_kwarg_bypasses_disk(db, tmp_path):
    """Passing content= directly skips disk read (useful for in-memory callers)."""
    f = tmp_path / "no_such_file.md"  # does NOT exist on disk

    row_id, stored = ingest_memory_file(db, f, content="direct content\n")
    assert stored is True
    assert row_id > 0


def test_ingest_missing_file_returns_neg1(db, tmp_path):
    """OSError on disk read returns (-1, False) cleanly."""
    f = tmp_path / "ghost.md"  # does not exist

    row_id, stored = ingest_memory_file(db, f)
    assert row_id == -1
    assert stored is False


# ---------------------------------------------------------------------------
# live_curated_memory
# ---------------------------------------------------------------------------


def test_live_curated_memory_excludes_superseded(db, tmp_path):
    """live_curated_memory returns only the current (non-superseded) row."""
    f = tmp_path / "MEMORY.md"
    f.write_text("v1")
    id1, _ = ingest_memory_file(db, f, slug="s")

    f.write_text("v2")
    id2, _ = ingest_memory_file(db, f, slug="s")

    live = live_curated_memory(db)
    ids = [r["id"] for r in live]
    assert id1 not in ids
    assert id2 in ids


def test_live_curated_memory_slug_scoping(db, tmp_path):
    """slug= kwarg filters rows to that project only."""
    fa = tmp_path / "A.md"
    fb = tmp_path / "B.md"
    fa.write_text("project A memory")
    fb.write_text("project B memory")

    ingest_memory_file(db, fa, slug="slug-a")
    ingest_memory_file(db, fb, slug="slug-b")

    rows_a = live_curated_memory(db, slug="slug-a")
    rows_b = live_curated_memory(db, slug="slug-b")
    all_rows = live_curated_memory(db)

    assert len(rows_a) == 1
    assert rows_a[0]["slug"] == "slug-a"
    assert len(rows_b) == 1
    assert len(all_rows) == 2


# ---------------------------------------------------------------------------
# Schema additive reconcile
# ---------------------------------------------------------------------------


def test_curated_memory_additive_reconcile(tmp_path):
    """Drop curated_memory, re-run init_db, confirm the table and indexes reappear."""
    db_path = tmp_path / "reconcile.db"
    conn = init_db(db_path)
    conn.close()

    # Verify the table exists after init.
    conn = connect(db_path)
    try:
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='curated_memory'"
        ).fetchone()
        assert table_exists is not None, "curated_memory table should exist after init_db"

        # Drop it to simulate an old db. Reconcile is gated on a sentinel (techne#140),
        # so also clear the stamp → the next connect treats the db as un-reconciled and
        # runs the reconcile passes (rather than skipping them on the hot path).
        from techne.core.database import _RECONCILE_SENTINEL

        conn.execute("DROP TABLE curated_memory")
        conn.execute("DELETE FROM extension_state WHERE name = ?", (_RECONCILE_SENTINEL,))
        conn.commit()

        gone = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='curated_memory'"
        ).fetchone()
        assert gone is None
    finally:
        conn.close()

    # Re-open unstamped (triggers reconciliation via _ensure_initialized -> _reconcile_tables).
    conn2 = connect(db_path)
    try:
        restored = conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='curated_memory'"
        ).fetchone()
        assert restored is not None, "curated_memory table should be restored by reconcile"

        # Check the three indexes.
        for idx in (
            "idx_curated_memory_path",
            "idx_curated_memory_slug",
            "idx_curated_memory_superseded",
        ):
            row = conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (idx,)
            ).fetchone()
            assert row is not None, f"index {idx} should be restored by reconcile"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# CCMemorySource
# ---------------------------------------------------------------------------


def test_cc_memory_source_initial_walk(project, db, monkeypatch):
    """initial_walk discovers and ingests memory files; returns stored count."""
    from techne.config import Config
    from techne.sync.cc_memory import CCMemorySource

    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    cfg = Config()
    cfg.db_path = db

    _write_memory(config_dir, cwd, "# Memory entry one\nUsed foo.py\n", "ONE.md")
    _write_memory(config_dir, cwd, "# Memory entry two\nUsed bar.py\n", "TWO.md")

    src = CCMemorySource(cfg, cwd=cwd)
    stored = src.initial_walk()
    assert stored == 2

    live = live_curated_memory(db)
    assert len(live) == 2
    titles = {r["title"] for r in live}
    assert titles == {"ONE.md", "TWO.md"}


def test_cc_memory_source_initial_walk_idempotent(project, db, monkeypatch):
    """Re-running initial_walk on unchanged files returns 0 (all no-ops)."""
    from techne.config import Config
    from techne.sync.cc_memory import CCMemorySource

    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    cfg = Config()
    cfg.db_path = db

    _write_memory(config_dir, cwd, "# unchanged\n")

    src = CCMemorySource(cfg, cwd=cwd)
    assert src.initial_walk() == 1
    assert src.initial_walk() == 0  # second run is a no-op


def test_cc_memory_source_initial_walk_empty(project, db, monkeypatch):
    """initial_walk returns 0 when no memory files exist."""
    from techne.config import Config
    from techne.sync.cc_memory import CCMemorySource

    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    cfg = Config()
    cfg.db_path = db

    src = CCMemorySource(cfg, cwd=cwd)
    assert src.initial_walk() == 0


def test_cc_memory_source_health(project, db, monkeypatch):
    """health() is True when files exist, False when dir is absent or empty."""
    from techne.config import Config
    from techne.sync.cc_memory import CCMemorySource

    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    cfg = Config()
    cfg.db_path = db

    src = CCMemorySource(cfg, cwd=cwd)
    assert src.health() is False  # no files yet

    _write_memory(config_dir, cwd, "# memory\n")
    assert src.health() is True


def test_cc_memory_source_provides():
    """provides class attribute declares only the 'curated' band."""
    from techne.sync.cc_memory import CCMemorySource

    assert CCMemorySource.provides == frozenset({"curated"})
    # Raw deed-ledger is NOT in scope for this adapter.
    assert "raw" not in CCMemorySource.provides


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


def test_cli_memory_sync_help():
    """memory sync --help exits cleanly (import + Click wiring check)."""
    from click.testing import CliRunner

    from techne.cli import main

    res = CliRunner().invoke(main, ["memory", "sync", "--help"])
    assert res.exit_code == 0
    assert "sync" in res.output.lower()


def test_cli_memory_sync_invocation(project, db, monkeypatch):
    """memory sync runs, ingests files, and reports the count."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    cfg = Config()
    cfg.db_path = db
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))
    monkeypatch.chdir(cwd)

    _write_memory(config_dir, cwd, "# test memory\nreferences alpha.py\n")

    res = CliRunner().invoke(main, ["memory", "sync"])
    assert res.exit_code == 0
    assert "1" in res.output  # 1 file drained

    # Second run: no-op.
    res2 = CliRunner().invoke(main, ["memory", "sync"])
    assert res2.exit_code == 0
    # "0 file(s) drained" or similar -- just confirm it ran cleanly.
    assert "0" in res2.output


def test_cli_memory_sync_no_files(project, db, monkeypatch):
    """memory sync with no files prints the 'nothing to drain' message."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    cfg = Config()
    cfg.db_path = db
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))
    monkeypatch.chdir(cwd)

    res = CliRunner().invoke(main, ["memory", "sync"])
    assert res.exit_code == 0
    assert "nothing to drain" in res.output.lower() or "no curated" in res.output.lower()


def test_curated_memory_meta_stored(db, tmp_path):
    """meta JSON contains mtime and size for a real file."""
    f = tmp_path / "META.md"
    f.write_text("# hi\n")

    row_id, _ = ingest_memory_file(db, f)

    conn = connect(db)
    try:
        row = conn.execute("SELECT meta FROM curated_memory WHERE id=?", (row_id,)).fetchone()
    finally:
        conn.close()

    meta = json.loads(row["meta"])
    assert "mtime" in meta
    assert "size" in meta
    assert meta["size"] > 0


# ---------------------------------------------------------------------------
# techne#1290 -- a deleted memory file retires its row
# ---------------------------------------------------------------------------


def _walk(cfg, cwd):
    from techne.sync.cc_memory import CCMemorySource

    return CCMemorySource(cfg, cwd=cwd).initial_walk()


def test_initial_walk_retires_deleted_file(project, db, monkeypatch):
    """Ingest two files, delete one, re-walk: only the survivor is live, and the
    deleted row is kept as history behind a tombstone."""
    import shutil

    from techne.config import Config

    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    cfg = Config()
    cfg.db_path = db
    keep = _write_memory(config_dir, cwd, "# kept\n", "KEEP.md")
    gone = _write_memory(config_dir, cwd, "# wrong, deleted\n", "GONE.md")
    assert _walk(cfg, cwd) == 2

    gone.unlink()
    _walk(cfg, cwd)
    assert [r["title"] for r in live_curated_memory(db)] == ["KEEP.md"]
    conn = connect(db)
    try:
        old = conn.execute(
            "SELECT superseded_by FROM curated_memory WHERE source_path=? AND content != ''",
            (str(gone),),
        ).fetchone()
    finally:
        conn.close()
    assert old["superseded_by"] is not None

    # Removing the whole directory retires nothing.
    shutil.rmtree(keep.parent)
    _walk(cfg, cwd)
    assert [r["title"] for r in live_curated_memory(db)] == ["KEEP.md"]


def test_retired_file_that_reappears_is_live_again(project, db, monkeypatch):
    """A tombstone must not block the file coming back. The middle assertion makes
    this fail without the fix too: on main the deleted file is never retired."""
    from techne.config import Config

    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    cfg = Config()
    cfg.db_path = db
    _write_memory(config_dir, cwd, "# a\n", "A.md")
    b = _write_memory(config_dir, cwd, "# b\n", "B.md")
    _walk(cfg, cwd)
    b.unlink()
    _walk(cfg, cwd)
    assert [r["title"] for r in live_curated_memory(db)] == ["A.md"]
    _write_memory(config_dir, cwd, "# b\n", "B.md")
    _walk(cfg, cwd)
    assert sorted(r["title"] for r in live_curated_memory(db)) == ["A.md", "B.md"]


def test_retire_is_scoped_to_the_walked_slug(db, tmp_path):
    from techne.core.curated import retire_missing_memory

    a = tmp_path / "a.md"
    a.write_text("a")
    b = tmp_path / "b.md"
    b.write_text("b")
    ingest_memory_file(db, a, slug="proj-a")
    ingest_memory_file(db, b, slug="proj-b")
    assert retire_missing_memory(db, slug="proj-a", present=set()) == 1
    assert [r["slug"] for r in live_curated_memory(db)] == ["proj-b"]
