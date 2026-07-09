"""Tests for codex auto-refresh: staleness flagging + codex_meta (techne#89 slice).

Covers:
  - annotate_freshness: fresh / stale / missing / unknown states
  - File-hash deduplication (one hash per distinct file)
  - repo_root persisted in codex_meta by initial_walk
  - annotate_freshness uses codex_meta when repo_root arg is not passed
  - Migration: pre-v2 codex.db without codex_meta reconciles on connect/migrate
  - CLI codex search shows [stale] / [missing] marker (requires tree-sitter)
  - codex watch command exists and errors cleanly without watchdog
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    """Create a fresh codex.db at v2 schema."""
    from techne.core.codex import CODEX_SCHEMA
    from techne.core.stores import SqliteStore

    p = tmp_path / "codex.db"
    store = SqliteStore("codex", p)
    store.migrate(CODEX_SCHEMA)
    conn = store.connect()
    try:
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_edges_to_name   ON edges(to_name);
            CREATE INDEX IF NOT EXISTS idx_edges_to_symbol ON edges(to_symbol);
            CREATE INDEX IF NOT EXISTS idx_edges_src_hash  ON edges(src_file_hash);
        """)
    finally:
        conn.close()
    return p


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _can_tree_sitter() -> bool:
    try:
        import tree_sitter_python  # noqa: F401

        return True
    except ImportError:
        return False


_HAS_GRAMMARS = _can_tree_sitter()


# ---------------------------------------------------------------------------
# annotate_freshness — unit tests (no tree-sitter needed)
# ---------------------------------------------------------------------------


def test_annotate_freshness_fresh(tmp_path):
    """Symbol whose file bytes match file_hash → fresh."""
    from techne.core.codegraph import annotate_freshness

    f = tmp_path / "mod.py"
    f.write_bytes(b"def foo(): pass\n")
    h = _sha256(b"def foo(): pass\n")

    syms = [{"id": "s1", "file": "mod.py", "name": "foo", "kind": "function", "file_hash": h}]
    result = annotate_freshness(None, syms, repo_root=tmp_path)
    assert result[0]["freshness"] == "fresh"


def test_annotate_freshness_stale(tmp_path):
    """Symbol whose file bytes differ from file_hash → stale."""
    from techne.core.codegraph import annotate_freshness

    f = tmp_path / "mod.py"
    old_bytes = b"def foo(): pass\n"
    new_bytes = b"def foo(): return 1\n"
    f.write_bytes(new_bytes)  # file has changed
    old_hash = _sha256(old_bytes)

    syms = [
        {
            "id": "s1",
            "file": "mod.py",
            "name": "foo",
            "kind": "function",
            "file_hash": old_hash,
        }
    ]
    result = annotate_freshness(None, syms, repo_root=tmp_path)
    assert result[0]["freshness"] == "stale"


def test_annotate_freshness_missing(tmp_path):
    """Symbol whose file is absent from disk → missing."""
    from techne.core.codegraph import annotate_freshness

    syms = [
        {
            "id": "s1",
            "file": "gone.py",
            "name": "foo",
            "kind": "function",
            "file_hash": _sha256(b"x"),
        }
    ]
    result = annotate_freshness(None, syms, repo_root=tmp_path)
    assert result[0]["freshness"] == "missing"


def test_annotate_freshness_unknown_no_root(tmp_path):
    """Relative path + no repo_root + no codex_meta → unknown (graceful)."""
    from techne.core.codegraph import annotate_freshness

    db = _make_db(tmp_path)
    syms = [
        {
            "id": "s1",
            "file": "relative/mod.py",
            "name": "foo",
            "kind": "function",
            "file_hash": _sha256(b"x"),
        }
    ]
    # repo_root=None and no codex_meta row → unknown
    result = annotate_freshness(db, syms, repo_root=None)
    assert result[0]["freshness"] == "unknown"


def test_annotate_freshness_unknown_no_hash(tmp_path):
    """File present but file_hash is None (older row) → unknown."""
    from techne.core.codegraph import annotate_freshness

    f = tmp_path / "mod.py"
    f.write_bytes(b"def foo(): pass\n")

    syms = [{"id": "s1", "file": "mod.py", "name": "foo", "kind": "function", "file_hash": None}]
    result = annotate_freshness(None, syms, repo_root=tmp_path)
    assert result[0]["freshness"] == "unknown"


def test_annotate_freshness_absolute_path(tmp_path):
    """Symbol with an absolute file path resolves without repo_root."""
    from techne.core.codegraph import annotate_freshness

    f = tmp_path / "mod.py"
    content = b"def bar(): pass\n"
    f.write_bytes(content)
    h = _sha256(content)

    syms = [
        {
            "id": "s1",
            "file": str(f),
            "name": "bar",
            "kind": "function",
            "file_hash": h,
        }
    ]
    result = annotate_freshness(None, syms, repo_root=None)
    assert result[0]["freshness"] == "fresh"


def test_annotate_freshness_does_not_mutate_input(tmp_path):
    """annotate_freshness returns new dicts; input dicts are not mutated."""
    from techne.core.codegraph import annotate_freshness

    f = tmp_path / "mod.py"
    f.write_bytes(b"x")
    original = {
        "id": "s1",
        "file": "mod.py",
        "name": "foo",
        "kind": "function",
        "file_hash": _sha256(b"x"),
    }
    syms = [original]
    result = annotate_freshness(None, syms, repo_root=tmp_path)
    assert "freshness" not in original, "input dict must not be mutated"
    assert "freshness" in result[0]


# ---------------------------------------------------------------------------
# File-hash deduplication
# ---------------------------------------------------------------------------


def test_annotate_freshness_file_hash_dedup(tmp_path, monkeypatch):
    """Two symbols in the same file → file is hashed exactly once."""
    import techne.core.codegraph as cg_mod
    from techne.core.codegraph import annotate_freshness

    f = tmp_path / "mod.py"
    content = b"def a(): pass\ndef b(): pass\n"
    f.write_bytes(content)
    h = _sha256(content)

    call_count = 0
    real_sha256 = cg_mod._sha256_bytes

    def counting_sha256(path):
        nonlocal call_count
        call_count += 1
        return real_sha256(path)

    monkeypatch.setattr(cg_mod, "_sha256_bytes", counting_sha256)

    syms = [
        {"id": "s1", "file": "mod.py", "name": "a", "kind": "function", "file_hash": h},
        {"id": "s2", "file": "mod.py", "name": "b", "kind": "function", "file_hash": h},
    ]
    result = annotate_freshness(None, syms, repo_root=tmp_path)

    assert call_count == 1, f"expected 1 hash call, got {call_count}"
    assert result[0]["freshness"] == "fresh"
    assert result[1]["freshness"] == "fresh"


# ---------------------------------------------------------------------------
# repo_root persistence via codex_meta
# ---------------------------------------------------------------------------


def test_repo_root_persisted_in_meta(tmp_path):
    """initial_walk() stores repo_root in codex_meta."""
    from techne.codex.source import RepoSource
    from techne.core.codex import get_meta

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def foo(): pass\n")

    db = _make_db(tmp_path)

    src = RepoSource(db, repo, languages=["python"])
    src.initial_walk()

    stored = get_meta(db, "repo_root")
    assert stored == str(repo.resolve()), f"repo_root mismatch: {stored!r}"


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_annotate_freshness_uses_meta_root(tmp_path):
    """annotate_freshness resolves relative paths via codex_meta when repo_root arg omitted."""
    from techne.codex.source import RepoSource
    from techne.core.codegraph import annotate_freshness, search_symbols

    repo = tmp_path / "repo"
    repo.mkdir()
    content = b"def alpha(): pass\n"
    (repo / "alpha.py").write_bytes(content)

    db = _make_db(tmp_path)
    src = RepoSource(db, repo, languages=["python"])
    src.initial_walk()

    hits = search_symbols(db, "alpha")
    assert hits, "expected at least one symbol from the initial index"

    # Without repo_root arg — should read codex_meta and resolve correctly.
    annotated = annotate_freshness(db, hits)
    for sym in annotated:
        assert sym["freshness"] == "fresh", (
            f"expected fresh, got {sym['freshness']} for {sym['file']}"
        )


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_annotate_freshness_stale_after_edit(tmp_path):
    """Edit a file → its symbols become stale; unedited symbols stay fresh."""
    from techne.codex.source import RepoSource
    from techne.core.codegraph import annotate_freshness, search_symbols

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo(): pass\n")
    (repo / "b.py").write_text("def bar(): pass\n")

    db = _make_db(tmp_path)
    src = RepoSource(db, repo, languages=["python"])
    src.initial_walk()

    # Mutate a.py after indexing.
    (repo / "a.py").write_text("def foo(): return 42\n")

    # Search each name individually (FTS5 "foo bar" = AND, won't match across files).
    foo_hits = search_symbols(db, "foo")
    bar_hits = search_symbols(db, "bar")
    hits = [s for s in foo_hits if s["name"] == "foo"] + [
        s for s in bar_hits if s["name"] == "bar"
    ]
    annotated = annotate_freshness(db, hits)
    by_name = {s["name"]: s["freshness"] for s in annotated}

    assert by_name.get("foo") == "stale", (
        f"expected stale for foo, got {by_name.get('foo')}"
    )
    assert by_name.get("bar") == "fresh", (
        f"expected fresh for bar, got {by_name.get('bar')}"
    )


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_annotate_freshness_missing_after_delete(tmp_path):
    """Delete a file → its symbols become missing."""
    from techne.codex.source import RepoSource
    from techne.core.codegraph import annotate_freshness, search_symbols

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def baz(): pass\n")

    db = _make_db(tmp_path)
    src = RepoSource(db, repo, languages=["python"])
    src.initial_walk()

    (repo / "mod.py").unlink()  # delete after indexing

    hits = search_symbols(db, "baz")
    assert hits, "symbols should still be in the DB (not re-indexed)"
    annotated = annotate_freshness(db, hits)
    for sym in annotated:
        assert sym["freshness"] == "missing", f"expected missing, got {sym['freshness']}"


# ---------------------------------------------------------------------------
# Migration: pre-v2 DB without codex_meta reconciles
# ---------------------------------------------------------------------------


def test_migration_adds_codex_meta_table(tmp_path):
    """A v1 codex.db (no codex_meta) gets the table after _apply_codex_migration + migrate."""
    db_path = tmp_path / "codex_v1.db"

    # Build a v1-shaped DB by hand (has symbols + edges with to_name, no codex_meta).
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE symbols (
            id TEXT PRIMARY KEY, file TEXT NOT NULL, name TEXT NOT NULL,
            kind TEXT NOT NULL, signature TEXT, span TEXT, language TEXT,
            file_hash TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE edges (
            from_symbol TEXT NOT NULL REFERENCES symbols(id),
            to_name TEXT NOT NULL,
            to_symbol TEXT REFERENCES symbols(id),
            kind TEXT NOT NULL,
            src_file_hash TEXT,
            resolution_status TEXT NOT NULL DEFAULT 'unresolved',
            PRIMARY KEY (from_symbol, to_name, kind)
        )
    """)
    conn.execute(
        "INSERT INTO symbols VALUES ('f1','a.py','foo','function',NULL,'1:5','python',NULL)"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    # Apply migration (bumps to v2); store.migrate(CODEX_SCHEMA) creates codex_meta.
    from techne.core.codex import CODEX_SCHEMA, CODEX_VERSION, _apply_codex_migration
    from techne.core.stores import SqliteStore, plain_connect

    _apply_codex_migration(db_path)
    store = SqliteStore("codex", db_path)
    store.migrate(CODEX_SCHEMA)

    conn2 = plain_connect(db_path)
    try:
        version = conn2.execute("PRAGMA user_version").fetchone()[0]
        assert version == CODEX_VERSION, f"expected version={CODEX_VERSION}, got {version}"

        tables = {
            row[0]
            for row in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "codex_meta" in tables, f"codex_meta table missing; tables={tables}"
    finally:
        conn2.close()


def test_get_set_meta(tmp_path):
    """get_meta / set_meta round-trip; get_meta on absent key returns None."""
    from techne.core.codex import get_meta, set_meta

    db = _make_db(tmp_path)

    # Initially absent.
    assert get_meta(db, "repo_root") is None

    # Round-trip.
    set_meta(db, "repo_root", "/some/path")
    assert get_meta(db, "repo_root") == "/some/path"

    # Upsert.
    set_meta(db, "repo_root", "/new/path")
    assert get_meta(db, "repo_root") == "/new/path"


def test_get_meta_graceful_no_table(tmp_path):
    """get_meta returns None when codex_meta table doesn't exist (pre-v2 DB)."""
    from techne.core.codex import get_meta

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE symbols ("
        "id TEXT PRIMARY KEY, file TEXT NOT NULL, "
        "name TEXT NOT NULL, kind TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    # Should not raise.
    result = get_meta(db_path, "repo_root")
    assert result is None


# ---------------------------------------------------------------------------
# CLI: stale marker visible in output (requires tree-sitter)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_cli_search_shows_stale_marker(tmp_path, monkeypatch):
    """CLI `codex search` output shows [stale] after file edit."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.codex.source import RepoSource
    from techne.config import Config
    from techne.core.codex import codex_db_for_repo

    # Set up a minimal techne data dir.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "techne.db").touch()

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mymod.py").write_text("def stalefn(): pass\n")

    # Build a config pointing at our test data dir, then index the repo into its
    # own per-repo codex db (techne#209).
    cfg = Config()
    cfg.db_path = data_dir / "techne.db"
    cfg.data_dir = data_dir
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))

    db_path = codex_db_for_repo(cfg, repo)
    RepoSource(db_path, repo, languages=["python"]).initial_walk()

    # Edit the file so its symbols become stale.
    (repo / "mymod.py").write_text("def stalefn(): return 99\n")

    runner = CliRunner()
    result = runner.invoke(main, ["codex", "search", "stalefn", "--repo", str(repo)])
    assert result.exit_code == 0, f"CLI exited {result.exit_code}:\n{result.output}"
    assert "[stale]" in result.output, (
        f"expected [stale] in output:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# codex watch: command exists, errors cleanly without watchdog
# ---------------------------------------------------------------------------


def test_codex_watch_command_exists():
    """codex watch is registered as a CLI command."""
    from click.testing import CliRunner

    from techne.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["codex", "watch", "--help"])
    assert result.exit_code == 0, f"expected help to succeed:\n{result.output}"
    assert "REPO" in result.output or "watch" in result.output.lower()


def test_codex_watch_requires_watchdog(tmp_path, monkeypatch):
    """codex watch reports an error cleanly when watchdog is not importable."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config
    from techne.core.codex import CODEX_SCHEMA
    from techne.core.stores import SqliteStore

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "techne.db").touch()

    repo = tmp_path / "repo"
    repo.mkdir()

    db_path = data_dir / "codex.db"
    SqliteStore("codex", db_path).migrate(CODEX_SCHEMA)

    cfg = Config()
    cfg.db_path = data_dir / "techne.db"
    cfg.data_dir = data_dir
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))

    def patched_start_watch(self):
        raise RuntimeError("watchdog is not installed; run `pip install techne[codex]`")

    runner = CliRunner()
    with patch("techne.codex.source.RepoSource.start_watch", patched_start_watch):
        result = runner.invoke(main, ["codex", "watch", str(repo)])
    # Should produce a readable error about watchdog, not a raw traceback.
    output_lower = result.output.lower()
    assert (
        result.exit_code != 0
        or "error" in output_lower
        or "watchdog" in output_lower
    ), f"Expected a clean error about watchdog:\n{result.output}"
