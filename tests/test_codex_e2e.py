"""End-to-end smoke test for the Codex index → query chain (techne#89).

Covers the integrated path: file parsing → codex.db → graph queries.
Unit/isolation coverage lives in test_codex_source.py; this test exercises the
full pipeline on a fresh single-file repo so every layer is exercised together.

Skips cleanly when tree-sitter grammars are not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Module-level skip guards — skips all tests if tree-sitter grammars are absent.
pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_python")


# ---------------------------------------------------------------------------
# Helpers (mirrored from test_codex_source.py)
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    """Create and return a fresh codex.db path with the v1 schema applied."""
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


# ---------------------------------------------------------------------------
# End-to-end smoke test
# ---------------------------------------------------------------------------

_SINGLE_FILE_SRC = """\
def helper():
    return 1


def caller():
    return helper()
"""


def test_codex_e2e_index_to_query(tmp_path):
    """Full chain: single-file repo → initial_walk → stats/search/callers/reachable.

    Uses a same-file call so the edge target is guaranteed to be present in the
    indexed symbols and name-based resolution is deterministic.
    """
    from techne.codex.source import RepoSource
    from techne.core.codegraph import callers, reachable, search_symbols, stats

    # Build a minimal repo with a single Python file.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text(_SINGLE_FILE_SRC)

    db = _make_db(tmp_path)

    # 1. Index the repo.
    count = RepoSource(db, repo, languages=["python"]).initial_walk()
    assert count >= 2, f"expected >= 2 symbols indexed, got {count}"

    # 2. Stats: at least 2 symbols, 1 edge, 1 file.
    s = stats(db)
    assert s["symbols"] >= 2, f"stats symbols: {s}"
    assert s["edges"] >= 1, f"stats edges: {s}"
    assert s["files"] >= 1, f"stats files: {s}"

    # 3. FTS: search_symbols finds both symbols.
    helper_hits = search_symbols(db, "helper")
    helper_names = [h["name"] for h in helper_hits]
    assert "helper" in helper_names, f"'helper' not in FTS results: {helper_names}"

    caller_hits = search_symbols(db, "caller")
    caller_names = [h["name"] for h in caller_hits]
    assert "caller" in caller_names, f"'caller' not in FTS results: {caller_names}"

    # 4. Resolve symbol IDs from FTS results.
    helper_id = next(h["id"] for h in helper_hits if h["name"] == "helper")
    caller_id = next(h["id"] for h in caller_hits if h["name"] == "caller")

    # 5. Call-edge: callers(helper) includes "caller".
    caller_syms = callers(db, helper_id)
    caller_sym_names = {s["name"] for s in caller_syms}
    assert "caller" in caller_sym_names, (
        f"expected 'caller' in callers(helper), got: {caller_sym_names}"
    )

    # 6. Blast-radius BFS: reachable(caller, out) includes "helper".
    reached = reachable(db, caller_id, direction="out", max_depth=3)
    reached_names = {s["name"] for s in reached}
    assert "helper" in reached_names, (
        f"expected 'helper' in reachable(caller, out), got: {reached_names}"
    )
