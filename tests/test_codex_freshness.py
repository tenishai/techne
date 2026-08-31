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
    hits = [s for s in foo_hits if s["name"] == "foo"] + [s for s in bar_hits if s["name"] == "bar"]
    annotated = annotate_freshness(db, hits)
    by_name = {s["name"]: s["freshness"] for s in annotated}

    assert by_name.get("foo") == "stale", f"expected stale for foo, got {by_name.get('foo')}"
    assert by_name.get("bar") == "fresh", f"expected fresh for bar, got {by_name.get('bar')}"


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
            for row in conn2.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
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
    assert "[stale]" in result.output, f"expected [stale] in output:\n{result.output}"


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
    assert result.exit_code != 0 or "error" in output_lower or "watchdog" in output_lower, (
        f"Expected a clean error about watchdog:\n{result.output}"
    )


# ---- techne#604 v3: a polluted index must not survive the fix ----------------


def test_v3_migration_purges_vendored_rows_and_their_edges(tmp_path):
    """A POLLUTED INDEX SURVIVED THE FIX AND THEN CERTIFIED ITSELF (#605 r1).

    Indexing is incremental and the only DELETE is scoped to the file being
    re-indexed, so vendored rows written before `site-packages` was skipped are
    never revisited — nothing walks them, so nothing removes them. Worse,
    `index_freshness` then reports `stale: 0, missing: 0, coverage: "complete"`:
    every one of those files really is present and unchanged on disk, so the
    surface built to stop the index lying (techne#595) vouches for the pollution.

    The v2→v3 step deletes exactly the excluded rows, and their edges with them —
    an orphaned edge is a dangling FK that outlives the symbol it described.
    """
    from techne.core.codegraph import add_edge, add_symbol
    from techne.core.codex import _apply_codex_migration, _codex_v2_to_v3
    from techne.core.stores import plain_connect

    db = tmp_path / "codex.db"
    _apply_codex_migration(db)
    from techne.core.codex import CODEX_SCHEMA

    conn = plain_connect(db)
    conn.executescript(CODEX_SCHEMA)
    conn.commit()
    conn.close()

    add_symbol(
        db, id="pkg/real.py::mine::function::1", file="pkg/real.py", name="mine", kind="function"
    )
    vend = ".venv-ci-3.13/lib/python3.13/site-packages/aiohttp/web.py"
    add_symbol(
        db, id=f"{vend}::application::function::1", file=vend, name="application", kind="function"
    )
    add_edge(db, from_symbol="pkg/real.py::mine::function::1", to_name="application", kind="call")
    add_edge(db, from_symbol=f"{vend}::application::function::1", to_name="mine", kind="call")

    conn = plain_connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 2

        _codex_v2_to_v3(conn)
        conn.commit()

        files = [r[0] for r in conn.execute("SELECT file FROM symbols")]
        assert files == ["pkg/real.py"], f"vendored rows survived: {files}"
        # The vendored symbol's OUTGOING edge goes with it; the repo symbol's own
        # edge survives rather than being collateral.
        froms = [r[0] for r in conn.execute("SELECT from_symbol FROM edges")]
        assert froms == ["pkg/real.py::mine::function::1"]
    finally:
        conn.close()


def test_v3_purge_spares_a_repo_path_merely_containing_the_words(tmp_path):
    """The purge is by PATH SEGMENT. A source file named `site_packages_util.py`,
    or a directory `my-site-packages-tool/`, must survive — deleting real rows to
    clean up junk ones is a worse failure than the junk."""
    from techne.core.codegraph import add_symbol
    from techne.core.codex import CODEX_SCHEMA, _apply_codex_migration, _codex_v2_to_v3
    from techne.core.stores import plain_connect

    db = tmp_path / "codex.db"
    _apply_codex_migration(db)
    conn = plain_connect(db)
    conn.executescript(CODEX_SCHEMA)
    conn.commit()
    conn.close()

    for f in ("pkg/site_packages_util.py", "my-site-packages-tool/main.py"):
        add_symbol(db, id=f"{f}::fn::function::1", file=f, name="fn", kind="function")

    conn = plain_connect(db)
    try:
        _codex_v2_to_v3(conn)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 2
    finally:
        conn.close()


def test_a_v2_index_is_purged_by_the_migration_chain_not_just_the_step(tmp_path):
    """PINS THE WIRING, NOT THE STEP.

    The test above calls `_codex_v2_to_v3` directly, so removing it from
    `_run_codex_steps` left the whole suite green — an identity check on an import
    proving the import and not the use, in a test written to pin a fix. This one
    stamps a db at v2, puts vendored rows in it, and runs the real
    `_apply_codex_migration`: the purge must happen because the CHAIN runs it.
    """
    from techne.core.codegraph import add_symbol
    from techne.core.codex import CODEX_SCHEMA, CODEX_VERSION, _apply_codex_migration
    from techne.core.stores import plain_connect

    db = tmp_path / "codex.db"
    conn = plain_connect(db)
    conn.executescript(CODEX_SCHEMA)
    conn.execute("PRAGMA user_version = 2")  # a store written before #604
    conn.commit()
    conn.close()

    vend = ".venv-ci-3.13/lib/python3.13/site-packages/aiohttp/web.py"
    add_symbol(
        db, id=f"{vend}::application::function::1", file=vend, name="application", kind="function"
    )
    add_symbol(
        db, id="pkg/real.py::mine::function::1", file="pkg/real.py", name="mine", kind="function"
    )

    _apply_codex_migration(db)

    conn = plain_connect(db)
    try:
        files = [r[0] for r in conn.execute("SELECT file FROM symbols")]
        assert files == ["pkg/real.py"], f"the migration chain did not purge: {files}"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CODEX_VERSION
    finally:
        conn.close()


def test_v3_purge_matches_the_walkers_semantics_not_sqls(tmp_path):
    """THE PURGE RULE MUST BE THE WALK RULE (#604 r2).

    The first cut used SQL `LIKE`: case-INSENSITIVE, matching SUBSTRINGS of the
    WHOLE stored path. Both halves were wrong. `vendor/SITE-PACKAGES/x.py` was
    purged though `_walk_repo` — case-SENSITIVE, on path PARTS — would index it.
    And an ABSOLUTE path under an ancestor named `site-packages` matched, so a
    repo living under such an ancestor would lose EVERY row. `source.py:530-534`
    guards against exactly that and explains why; the migration threw the guard
    away and kept the hazard.
    """
    from techne.core.codex import _v3_should_purge

    root = "/home/me/proj"
    # Real vendored code — relative and absolute — is purged.
    assert _v3_should_purge("vendor/site-packages/aiohttp/web.py", root)
    assert _v3_should_purge(f"{root}/.venv/lib/python3.13/site-packages/x.py", root)
    assert _v3_should_purge("usr/lib/python3/dist-packages/yaml.py", root)

    # Case differences are NOT the walker's rule, so they must survive.
    assert not _v3_should_purge("vendor/SITE-PACKAGES/x.py", root)
    assert not _v3_should_purge("vendor/Site-Packages/x.py", root)

    # A segment merely CONTAINING the word survives; parts, not substrings.
    assert not _v3_should_purge("my-site-packages-tool/main.py", root)
    assert not _v3_should_purge("pkg/site_packages_util.py", root)

    # THE ANCESTOR HAZARD: the repo itself lives under a `site-packages` dir.
    # Relative rows are judged on the repo-relative path, so nothing matches.
    nested = "/opt/site-packages/proj"
    assert not _v3_should_purge("pkg/real.py", nested)
    assert not _v3_should_purge(f"{nested}/pkg/real.py", nested)

    # An absolute path with no repo_root cannot be made relative — decline.
    assert not _v3_should_purge("/somewhere/site-packages/x.py", None)


def test_v3_purge_converges_inbound_edges_on_fresh_index_state(tmp_path):
    """An inbound edge is NOT deleted: the migration converges on the state a FRESH
    v3 walk produces, which stores exactly `to_symbol=NULL, status='unresolved'`.
    A migrated v2 db is byte-identical to a re-indexed one in `symbols` and
    `edges`; deleting these edges would make the two disagree.

    THIS TEST DOES NOT ASSERT THE RESULTING GRAPH IS SOUND. It is not — a NULL
    `to_symbol` arms codegraph's name fallback, so `callers()` will claim a
    repo-local call that never happened when a local symbol shares the bare name.
    That is #606, it reproduces on a fresh index with no migration involved, and
    it is out of scope here. The earlier name for this test
    ("..._rather_than_leaving_them_armed") asserted the opposite and was wrong.

    The two asserts below are load-bearing for DIFFERENT reasons, and an earlier
    version of this docstring gave the same (wrong) reason for both:

      - `to_symbol IS NULL` is load-bearing BEHAVIOURALLY. It had no test, and
        removing it raises `IntegrityError: FOREIGN KEY constraint failed` on
        real data (#604 r2).
      - `resolution_status = 'unresolved'` is load-bearing as an INVARIANT ONLY.
        It changes no query result: codegraph reads that column only in
        `add_edge`'s INSERT, and all four fallback sites gate on
        `to_symbol IS NULL` alone, so 'lsp' and 'unresolved' are indistinguishable
        to `callees`/`callers`/`reachable` (#604 r3 — the claim that the reset
        prevents a fallback from re-arming was disproved by A/B through the real
        accessors). It stays because 'lsp' with a NULL `to_symbol` is a state the
        v1 contract says cannot exist.
    """
    from techne.core.codegraph import add_edge, add_symbol
    from techne.core.codex import CODEX_SCHEMA, _apply_codex_migration, _codex_v2_to_v3
    from techne.core.stores import plain_connect

    db = tmp_path / "codex.db"
    _apply_codex_migration(db)
    conn = plain_connect(db)
    conn.executescript(CODEX_SCHEMA)
    conn.commit()
    conn.close()

    vend = "vendor/site-packages/aiohttp/web.py"
    add_symbol(db, id="pkg/a.py::mine::function::1", file="pkg/a.py", name="mine", kind="function")
    add_symbol(
        db, id=f"{vend}::application::function::1", file=vend, name="application", kind="function"
    )
    add_edge(
        db,
        from_symbol="pkg/a.py::mine::function::1",
        to_name="application",
        kind="call",
        to_symbol=f"{vend}::application::function::1",
        resolution_status="lsp",
    )

    conn = plain_connect(db)
    try:
        _codex_v2_to_v3(conn)
        conn.commit()
        row = conn.execute(
            "SELECT to_symbol, resolution_status FROM edges "
            "WHERE from_symbol = 'pkg/a.py::mine::function::1'"
        ).fetchone()
        assert row is not None, "the inbound edge was deleted; it records a real call"
        assert row[0] is None
        assert row[1] == "unresolved", (
            "'lsp' with a NULL to_symbol is a state the v1 contract says cannot "
            "exist; the reset restores the invariant and changes no query result"
        )
    finally:
        conn.close()


def test_v3_purge_binds_are_bounded_through_the_real_migration(tmp_path, monkeypatch):
    """The purge must not bind one SQL parameter per doomed symbol (#604 r3 Critical).

    `doomed` is unbounded — indexing this repo's own venv produced 114,410 of them
    — so a single `IN (?,?,…)` blows SQLite's SQLITE_LIMIT_VARIABLE_NUMBER and
    raises `OperationalError: too many SQL variables`. That raise lands BEFORE the
    version write, so `user_version` stays at 2 and EVERY later `codex_store()`
    re-raises: codex_search, codex_blast_radius and `techne codex index` are down
    permanently, stranding the user in the self-certifying state v3 exists to end.

    Rather than materialise 250k rows, this lowers the limit on the connection the
    migration actually uses. It drives `_apply_codex_migration` — NOT `_codex_v2_to_v3`
    directly — because the bug is only reachable through the real chain, and a
    helper-level test would pass against a migration that was never wired in.
    """
    import sqlite3

    from techne.core import codex as codex_mod
    from techne.core.codex import CODEX_SCHEMA, CODEX_VERSION, _apply_codex_migration
    from techne.core.stores import plain_connect

    limit, doomed_count = 600, 1500
    assert doomed_count > limit, "fixture must exceed the limit or it proves nothing"

    real_connect = codex_mod.plain_connect

    def _capped_connect(*a, **kw):
        conn = real_connect(*a, **kw)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, limit)
        return conn

    db = tmp_path / "codex.db"
    _apply_codex_migration(db)
    conn = plain_connect(db)
    conn.executescript(CODEX_SCHEMA)
    conn.executemany(
        "INSERT INTO symbols (id, file, name, kind) VALUES (?, ?, ?, 'function')",
        [
            (
                f"vendor/site-packages/pkg/m{i}.py::f{i}::function::1",
                f"vendor/site-packages/pkg/m{i}.py",
                f"f{i}",
            )
            for i in range(doomed_count)
        ],
    )
    from techne.core.migrator import user_version_io

    user_version_io.write(conn, 2)
    conn.commit()
    conn.close()

    monkeypatch.setattr(codex_mod, "plain_connect", _capped_connect)
    # Unchunked, this raises OperationalError: too many SQL variables.
    _apply_codex_migration(db)

    conn = plain_connect(db)
    try:
        # CODEX_VERSION, not a literal: this asserts the chain REACHED ITS END,
        # not that the end is any particular number. Hardcoding 3 made it fail on
        # the v4 addition (#679) for no reason connected to what it tests.
        assert user_version_io.read(conn) == CODEX_VERSION, (
            "migration did not reach the version write — a mid-purge raise leaves "
            "user_version at 2 and every later codex_store() re-raises"
        )
        assert conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="needs tree-sitter grammars to walk a real repo")
def test_v3_migrated_db_is_identical_to_a_fresh_index(tmp_path):
    """THE OUTCOME, not the mechanism (#604 r4).

    Twice on this change the recorded *reason* for unresolving inbound edges was
    wrong while the code was right, and both times the reason was argued rather
    than measured. This pins what the migration is actually FOR: a v2 db that
    indexed vendored code must end up byte-identical to a repo indexed fresh with
    the v3 skip list — same symbols, same edges, and the same answer through the
    real accessor.

    It is also the test that would fail if someone "fixed" #606 by deleting
    inbound edges here: migration and re-index would then disagree.
    """
    from techne.codex.source import RepoSource
    from techne.core.codegraph import add_symbol, callers
    from techne.core.codex import CODEX_SCHEMA, _apply_codex_migration
    from techne.core.migrator import user_version_io
    from techne.core.stores import plain_connect

    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    vend_dir = root / "vendor" / "site-packages" / "aiohttp"
    vend_dir.mkdir(parents=True)
    (root / "app" / "util.py").write_text("def get():\n    return 1\n")
    (vend_dir / "web.py").write_text("def get():\n    return 2\n")
    (root / "app" / "main.py").write_text(
        "from aiohttp.web import get\n\ndef handler():\n    return get()\n"
    )

    def _new_db(name):
        db = tmp_path / name
        _apply_codex_migration(db)
        c = plain_connect(db)
        c.executescript(CODEX_SCHEMA)
        c.commit()
        c.close()
        return db

    def _dump(db):
        c = plain_connect(db)
        try:
            syms = [tuple(r) for r in c.execute("SELECT * FROM symbols ORDER BY id")]
            edges = [
                tuple(r)
                for r in c.execute(
                    "SELECT from_symbol, to_name, to_symbol, resolution_status FROM edges "
                    "ORDER BY from_symbol, to_name"
                )
            ]
        finally:
            c.close()
        return syms, edges

    fresh = _new_db("fresh.db")
    RepoSource(db_path=fresh, repo_root=root, languages=["python"]).initial_walk()

    # A v2-era db: the walker indexed site-packages AND resolved the call into it.
    polluted = _new_db("polluted.db")
    RepoSource(db_path=polluted, repo_root=root, languages=["python"]).initial_walk()
    vend = "vendor/site-packages/aiohttp/web.py"
    add_symbol(polluted, id=f"{vend}::get::function::1", file=vend, name="get", kind="function")
    add_symbol(
        polluted, id=f"{vend}::<module>::module::0", file=vend, name="<module>", kind="module"
    )
    c = plain_connect(polluted)
    c.execute(
        "UPDATE edges SET to_symbol = ?, resolution_status = 'lsp' "
        "WHERE to_name = 'get' AND from_symbol LIKE 'app/main.py::handler%'",
        (f"{vend}::get::function::1",),
    )
    user_version_io.write(c, 2)
    c.commit()
    c.close()

    _apply_codex_migration(polluted)

    assert _dump(polluted) == _dump(fresh), (
        "a migrated v2 db must converge exactly on fresh-index state; if it does "
        "not, `techne codex index` and the migration disagree about the same repo"
    )
    target = "app/util.py::get::function::1"
    assert [x["id"] for x in callers(polluted, target)] == [
        x["id"] for x in callers(fresh, target)
    ], "the real accessor must agree too, not merely the stored rows"
