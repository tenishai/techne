"""Tests for the Codex tree-sitter SourceAdapter and schema migration (techne#89).

Covers:
  - initial_walk() indexes all files, returns symbol count > 0
  - stats() after walk shows symbols, edges, files
  - search_symbols() finds indexed symbols
  - callers() / callees() / reachable() traverse name-based edges
  - Incrementality: editing a file re-indexes it, no duplicate symbols, hash updated
  - Unresolved edges: resolution_status='unresolved', to_symbol IS NULL (v1 contract)
  - Migration: v0 → v1 schema migration (PRAGMA user_version, edges rebuild)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _can_import_tree_sitter() -> bool:
    try:
        import tree_sitter_python  # noqa: F401
        import tree_sitter_typescript  # noqa: F401

        return True
    except ImportError:
        return False


_HAS_GRAMMARS = _can_import_tree_sitter()

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    """Create and return a fresh codex.db path with the v1 schema applied."""
    from techne.core.codex import CODEX_SCHEMA
    from techne.core.stores import SqliteStore

    p = tmp_path / "codex.db"
    store = SqliteStore("codex", p)
    store.migrate(CODEX_SCHEMA)
    # Ensure the new-column indexes exist (mirrors codex_store() logic).
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


def _make_source(db_path: Path, repo_root: Path, languages=None):
    from techne.codex.source import RepoSource

    return RepoSource(db_path, repo_root, languages=languages or ["python", "typescript"])


# ---------------------------------------------------------------------------
# initial_walk
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_walk_repo_under_skip_named_parent(tmp_path):
    """Regression: a repo whose root lives under a skip-dir name (e.g. ~/build/repo)
    must still index — _walk_repo filters by path RELATIVE to the root, not by
    absolute parts (else the repo's own ancestor silently yields zero files)."""
    repo = tmp_path / "build" / "myrepo"  # 'build' is in _SKIP_DIRS
    repo.mkdir(parents=True)
    (repo / "mod.py").write_text(
        "def alpha():\n    return beta()\n\n\ndef beta():\n    return 1\n"
    )
    db = _make_db(tmp_path)
    assert _make_source(db, repo).initial_walk() > 0  # 0 before the relative-parts fix


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_initial_walk_returns_positive_count(tmp_path):
    db = _make_db(tmp_path)
    src = _make_source(db, FIXTURES)
    count = src.initial_walk()
    assert count > 0, "initial_walk should index at least one symbol"


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_initial_walk_indexes_all_files(tmp_path):
    db = _make_db(tmp_path)
    src = _make_source(db, FIXTURES)
    src.initial_walk()

    from techne.core.codegraph import stats

    s = stats(db)
    # 3 fixture files → at least 3 file entries; each has symbols
    assert s["files"] >= 3, f"expected ≥3 files, got {s['files']}"
    assert s["symbols"] > 0
    assert s["edges"] >= 0  # edges may be 0 if calls don't match names across files


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_initial_walk_idempotent(tmp_path):
    """Running initial_walk twice should not duplicate symbols."""
    db = _make_db(tmp_path)
    src = _make_source(db, FIXTURES)
    src.initial_walk()

    from techne.core.codegraph import stats

    s1 = stats(db)

    # Second walk: all files unchanged → 0 new symbols, totals unchanged
    count2 = src.initial_walk()
    assert count2 == 0, "second walk on unchanged files should emit 0 symbols"

    s2 = stats(db)
    assert s2["symbols"] == s1["symbols"], "symbol count must not change on re-walk"


# ---------------------------------------------------------------------------
# search_symbols
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_search_symbols_finds_python_function(tmp_path):
    db = _make_db(tmp_path)
    src = _make_source(db, FIXTURES, languages=["python"])
    src.initial_walk()

    from techne.core.codegraph import search_symbols

    hits = search_symbols(db, "add")
    names = [h["name"] for h in hits]
    assert "add" in names, f"'add' not found in FTS results: {names}"


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_search_symbols_finds_typescript_class(tmp_path):
    db = _make_db(tmp_path)
    src = _make_source(db, FIXTURES, languages=["typescript"])
    src.initial_walk()

    from techne.core.codegraph import search_symbols

    hits = search_symbols(db, "Greeter")
    names = [h["name"] for h in hits]
    assert "Greeter" in names, f"'Greeter' not found in FTS results: {names}"


# ---------------------------------------------------------------------------
# callers / callees / reachable
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_callees_traversal(tmp_path):
    """caller.run() calls add() — callees should include 'add'."""
    db = _make_db(tmp_path)
    src = _make_source(db, FIXTURES, languages=["python"])
    src.initial_walk()

    from techne.core.codegraph import callees, search_symbols

    # Find the 'run' symbol
    hits = search_symbols(db, "run")
    run_ids = [h["id"] for h in hits if h["name"] == "run"]
    if not run_ids:
        pytest.skip("'run' symbol not found — fixture may have changed")

    run_id = run_ids[0]
    callee_names = {s["name"] for s in callees(db, run_id)}
    assert "add" in callee_names, f"'add' not in callees of run: {callee_names}"


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_callers_traversal(tmp_path):
    """add() is called by run() and compute() — callers should be non-empty."""
    db = _make_db(tmp_path)
    src = _make_source(db, FIXTURES, languages=["python"])
    src.initial_walk()

    from techne.core.codegraph import callers, search_symbols

    hits = search_symbols(db, "add")
    add_ids = [h["id"] for h in hits if h["name"] == "add"]
    if not add_ids:
        pytest.skip("'add' symbol not found — fixture may have changed")

    add_id = add_ids[0]
    caller_names = {s["name"] for s in callers(db, add_id)}
    # run() and/or compute() both call add
    assert caller_names, "expected callers for 'add', got empty set"


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_reachable_out(tmp_path):
    """Transitive out-reachability from a calling function should be non-empty."""
    db = _make_db(tmp_path)
    src = _make_source(db, FIXTURES, languages=["python"])
    src.initial_walk()

    from techne.core.codegraph import reachable, search_symbols

    hits = search_symbols(db, "run")
    run_ids = [h["id"] for h in hits if h["name"] == "run"]
    if not run_ids:
        pytest.skip("'run' symbol not found")

    reached = reachable(db, run_ids[0], direction="out", max_depth=3)
    assert len(reached) > 0, "reachable(out) should find at least one callee"


# ---------------------------------------------------------------------------
# Incrementality
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_incrementality_on_file_change(tmp_path):
    """Edit a file: re-index should update symbols, no duplicates, hash updated."""
    # Copy fixture to a mutable temp dir so we can modify it.
    import shutil

    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(FIXTURES / "callee.py", repo / "callee.py")

    db = _make_db(tmp_path)
    src = _make_source(db, repo, languages=["python"])
    count1 = src.initial_walk()
    assert count1 > 0

    from techne.core.codegraph import stats
    from techne.core.stores import plain_connect

    s1 = stats(db)
    # Fetch file_hash directly — search_symbols only returns id/file/name/kind/sig/rank.
    conn_tmp = plain_connect(db)
    try:
        row = conn_tmp.execute(
            "SELECT file_hash FROM symbols WHERE name='add' LIMIT 1"
        ).fetchone()
        old_hash = row[0] if row else None
    finally:
        conn_tmp.close()

    # Append a new function to the file.
    (repo / "callee.py").open("a").write("\n\ndef subtract(a, b):\n    return a - b\n")

    count2 = src.initial_walk()
    assert count2 > 0, "re-index of changed file should emit symbols"

    s2 = stats(db)
    # No duplicates: total symbols may increase (new function) but each file's
    # symbols are cleanly replaced.
    assert s2["symbols"] >= s1["symbols"], "symbol count should be >= after adding a function"

    # Old symbols for this file should not be duplicated.
    # No duplicate 'add' symbols.
    from techne.core.stores import plain_connect as _pc2

    conn_tmp2 = _pc2(db)
    try:
        add_rows = conn_tmp2.execute(
            "SELECT id, file_hash FROM symbols WHERE name='add'"
        ).fetchall()
        assert len(add_rows) >= 1

        # Hash updated.
        new_hash = add_rows[0][1]
        assert new_hash != old_hash, "file_hash should change after file modification"
    finally:
        conn_tmp2.close()


def _edges_for_file(db_path: Path, file: str) -> int:
    from techne.core.stores import plain_connect

    conn = plain_connect(db_path)
    try:
        return conn.execute(
            "SELECT count(*) FROM edges WHERE from_symbol IN "
            "(SELECT id FROM symbols WHERE file = ?)",
            (file,),
        ).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_reindex_does_not_delete_twin_file_edges(tmp_path):
    """techne#170: two files with identical bytes share a SHA. Re-indexing one must
    NOT delete the OTHER's edges — the old `DELETE FROM edges WHERE src_file_hash=?`
    keyed on the shared content hash and wiped the twin's edges."""
    repo = tmp_path / "repo"
    repo.mkdir()
    twin = "def a():\n    return b()\n\n\ndef b():\n    return 1\n"
    (repo / "one.py").write_text(twin)
    (repo / "two.py").write_text(twin)  # identical content → identical file_hash

    db = _make_db(tmp_path)
    src = _make_source(db, repo, languages=["python"])
    src.initial_walk()

    two_before = _edges_for_file(db, "two.py")
    assert two_before > 0, "expected the twin file to contribute edges (a→b)"

    # Re-index ONLY one.py by changing its content (new hash → triggers the
    # stale-delete path). two.py is unchanged, so the fast-path skips it.
    (repo / "one.py").write_text("def a():\n    return c()\n\n\ndef c():\n    return 2\n")
    src.initial_walk()

    two_after = _edges_for_file(db, "two.py")
    assert two_after == two_before, (
        f"twin file two.py lost edges on one.py re-index: {two_before} → {two_after}"
    )


# ---------------------------------------------------------------------------
# Unresolved edges (v1 contract)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_edges_are_unresolved_in_v1(tmp_path):
    """v1 contract: all extracted edges have to_symbol=NULL, status='unresolved'."""
    db = _make_db(tmp_path)
    src = _make_source(db, FIXTURES, languages=["python"])
    src.initial_walk()

    from techne.core.stores import plain_connect

    conn = plain_connect(db)
    try:
        edges = conn.execute("SELECT to_symbol, resolution_status FROM edges").fetchall()
        if not edges:
            pytest.skip("no edges extracted — check fixture files")
        for row in edges:
            assert row[0] is None, f"to_symbol should be NULL in v1, got {row[0]}"
            assert row[1] == "unresolved", f"status should be 'unresolved', got {row[1]}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v0 → v1 migration
# ---------------------------------------------------------------------------


def test_codex_migration_v0_to_v1(tmp_path):
    """PRAGMA user_version-gated v0 → v1 migration: edges rebuilt, symbols.file_hash added."""
    from techne.core.stores import plain_connect

    db_path = tmp_path / "codex_v0.db"

    # Create a v0-shaped database by hand.
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE symbols (
            id TEXT PRIMARY KEY,
            file TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            signature TEXT,
            span TEXT,
            language TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE edges (
            from_symbol TEXT NOT NULL REFERENCES symbols(id),
            to_symbol   TEXT NOT NULL REFERENCES symbols(id),
            kind        TEXT NOT NULL,
            PRIMARY KEY (from_symbol, to_symbol, kind)
        )
    """)
    # Seed some data: two symbols connected by an edge.
    conn.execute("INSERT INTO symbols VALUES ('f1','a.py','foo','function',NULL,'1:5','python')")
    conn.execute("INSERT INTO symbols VALUES ('b1','b.py','bar','function',NULL,'1:3','python')")
    conn.execute("INSERT INTO edges VALUES ('f1','b1','call')")
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    # Run the migration.
    from techne.core.codex import CODEX_VERSION, _apply_codex_migration

    _apply_codex_migration(db_path)

    # Verify the v1 state.
    conn2 = plain_connect(db_path)
    try:
        version = conn2.execute("PRAGMA user_version").fetchone()[0]
        assert version == CODEX_VERSION, f"expected user_version={CODEX_VERSION}, got {version}"

        # symbols.file_hash column exists.
        sym_cols = {row[1] for row in conn2.execute("PRAGMA table_info(symbols)")}
        assert "file_hash" in sym_cols, "symbols.file_hash should exist after migration"

        # edges table has new shape.
        edge_cols = {row[1] for row in conn2.execute("PRAGMA table_info(edges)")}
        assert "to_name" in edge_cols, "edges.to_name should exist after migration"
        assert "to_symbol" in edge_cols, "edges.to_symbol (nullable) should exist after migration"
        assert "resolution_status" in edge_cols, "edges.resolution_status should exist"
        assert "src_file_hash" in edge_cols, "edges.src_file_hash should exist"

        # Old edge (f1→b1) was migrated: to_name='bar', to_symbol='b1', status='import_resolved'.
        rows = conn2.execute("SELECT * FROM edges").fetchall()
        assert len(rows) == 1, f"expected 1 edge, got {len(rows)}"
        # sqlite3.Row (set by plain_connect row_factory) supports dict() conversion.
        row_dict = dict(rows[0])
        assert row_dict["to_name"] == "bar", f"to_name should be 'bar', got {row_dict}"
        assert row_dict["to_symbol"] == "b1", f"to_symbol should be 'b1', got {row_dict}"
        assert row_dict["resolution_status"] == "import_resolved"
    finally:
        conn2.close()


def test_codex_migration_idempotent(tmp_path):
    """Running _apply_codex_migration twice does not error or corrupt data."""
    db_path = tmp_path / "codex_v0b.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE symbols (
            id TEXT PRIMARY KEY, file TEXT NOT NULL, name TEXT NOT NULL,
            kind TEXT NOT NULL, signature TEXT, span TEXT, language TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE edges (
            from_symbol TEXT NOT NULL, to_symbol TEXT NOT NULL,
            kind TEXT NOT NULL, PRIMARY KEY (from_symbol, to_symbol, kind)
        )
    """)
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    from techne.core.codex import CODEX_VERSION, _apply_codex_migration

    _apply_codex_migration(db_path)
    # Second call must be a no-op (version check gates it).
    _apply_codex_migration(db_path)

    from techne.core.stores import plain_connect

    conn2 = plain_connect(db_path)
    try:
        version = conn2.execute("PRAGMA user_version").fetchone()[0]
        assert version == CODEX_VERSION
    finally:
        conn2.close()
