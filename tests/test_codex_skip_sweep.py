"""Tests for the indexer's skip-list sweep (techne#613).

#605 purged vendored rows in a MIGRATION, which runs exactly once when
`user_version` moves 2→3. That left a hole observed on a real database, not
constructed: a db stamped v3 by a worktree running the PR code, then re-indexed
from a tree that predated the skip-list change, came back holding 114,410
vendored symbols at `user_version = 3`. The purge can never fire again, and
`index_freshness` certifies the result as `stale: 0, coverage: "complete"`
because those files genuinely are present and unchanged on disk.

So enforcement moved to the indexer, where a skip-list change heals itself.
These tests pin the two properties that distinguish the sweep from the migration:

  - it reads the LIVE `_SKIP_DIRS`, so a future entry needs no new migration
  - it matches the WALKER's semantics (repo-relative, exact, case-sensitive
    segments), so it cannot delete rows a fresh index would keep

Covers: sweep correctness, live-list tracking, lookalike survival, absolute
paths, bind bounding, honest zero, and the end-to-end heal through
`initial_walk` (grammars required).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _can_tree_sitter() -> bool:
    try:
        import tree_sitter_python  # noqa: F401

        return True
    except ImportError:
        return False


_HAS_GRAMMARS = _can_tree_sitter()


def _v3_db(tmp_path: Path, name: str = "codex.db") -> Path:
    """A codex.db at the CURRENT version with the full schema applied."""
    from techne.core.codex import CODEX_SCHEMA, _apply_codex_migration
    from techne.core.stores import plain_connect

    db = tmp_path / name
    _apply_codex_migration(db)
    conn = plain_connect(db)
    try:
        conn.executescript(CODEX_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return db


def _files(db: Path) -> list[str]:
    from techne.core.stores import plain_connect

    conn = plain_connect(db)
    try:
        return sorted(r[0] for r in conn.execute("SELECT file FROM symbols"))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------


def test_sweep_removes_vendored_rows_a_stamped_v3_db_still_holds(tmp_path):
    """THE #613 SCENARIO. `user_version` is already 3, so the migration is spent —
    and the rows are still there. The sweep is the only thing left that can remove
    them, and it must, without touching the repo's own rows."""
    from techne.codex.source import sweep_excluded_rows
    from techne.core.codegraph import add_symbol
    from techne.core.stores import plain_connect

    repo = tmp_path / "repo"
    repo.mkdir()
    db = _v3_db(tmp_path)

    conn = plain_connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3, (
            "precondition: the migration is already spent"
        )
    finally:
        conn.close()

    vend = ".venv-ci-3.13/lib/python3.13/site-packages/aiohttp/web.py"
    add_symbol(
        db, id="pkg/real.py::mine::function::1", file="pkg/real.py", name="mine", kind="function"
    )
    add_symbol(
        db, id=f"{vend}::application::function::1", file=vend, name="application", kind="function"
    )

    assert sweep_excluded_rows(db, repo) == 1
    assert _files(db) == ["pkg/real.py"]


def test_sweep_takes_outbound_edges_and_unresolves_inbound(tmp_path):
    """Edge handling must match the migration's, because a db healed by one route
    and a db healed by the other have to be indistinguishable — that is the whole
    reason `purge_symbol_ids` is shared rather than reimplemented here."""
    from techne.codex.source import sweep_excluded_rows
    from techne.core.codegraph import add_edge, add_symbol
    from techne.core.stores import plain_connect

    repo = tmp_path / "repo"
    repo.mkdir()
    db = _v3_db(tmp_path)

    vend = "venv/lib/python3.13/site-packages/aiohttp/web.py"
    mine = "pkg/real.py::mine::function::1"
    theirs = f"{vend}::application::function::1"
    add_symbol(db, id=mine, file="pkg/real.py", name="mine", kind="function")
    add_symbol(db, id=theirs, file=vend, name="application", kind="function")
    # Repo symbol calls into vendored code (INBOUND to the doomed symbol) …
    add_edge(db, from_symbol=mine, to_name="application", kind="call")
    # … and vendored code calls back (OUTBOUND from the doomed symbol).
    add_edge(db, from_symbol=theirs, to_name="mine", kind="call")

    conn = plain_connect(db)
    try:
        conn.execute(
            "UPDATE edges SET to_symbol = ?, resolution_status = 'lsp' "
            "WHERE to_name = 'application'",
            (theirs,),
        )
        conn.commit()
    finally:
        conn.close()

    assert sweep_excluded_rows(db, repo) == 1

    conn = plain_connect(db)
    try:
        rows = list(conn.execute("SELECT from_symbol, to_symbol, resolution_status FROM edges"))
        # The vendored symbol's own outbound edge is gone with it.
        assert [r[0] for r in rows] == [mine]
        # The repo symbol's edge SURVIVES, unresolved — the call really happened;
        # dropping it would erase a true fact (#604 r5).
        assert rows[0][1] is None
        assert rows[0][2] == "unresolved"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The property that makes this a fix rather than another one-shot
# ---------------------------------------------------------------------------


def test_sweep_tracks_the_live_skip_list_where_the_migration_is_frozen(tmp_path, monkeypatch):
    """THE POINT OF #613. `_SKIP_DIRS` has already changed twice; under the old
    design each change needed its own migration. A name added to the live list
    must be swept with no migration and no version bump — while the migration's
    frozen segments deliberately do NOT learn it, because a migration whose
    meaning changes when someone edits a constant is not a migration."""
    import techne.codex.source as source
    from techne.core.codegraph import add_symbol
    from techne.core.codex import _V3_PURGE_SEGMENTS

    repo = tmp_path / "repo"
    repo.mkdir()
    db = _v3_db(tmp_path)

    assert "bower_components" not in _V3_PURGE_SEGMENTS, (
        "the frozen v3 segments must not grow — that is what makes them frozen"
    )
    monkeypatch.setattr(source, "_SKIP_DIRS", source._SKIP_DIRS | {"bower_components"})

    add_symbol(
        db, id="pkg/real.py::mine::function::1", file="pkg/real.py", name="mine", kind="function"
    )
    add_symbol(
        db,
        id="bower_components/jquery/jquery.py::each::function::1",
        file="bower_components/jquery/jquery.py",
        name="each",
        kind="function",
    )

    assert source.sweep_excluded_rows(db, repo) == 1
    assert _files(db) == ["pkg/real.py"]


# ---------------------------------------------------------------------------
# It must not over-purge — deleting correct rows is the worse failure
# ---------------------------------------------------------------------------


def test_sweep_spares_paths_merely_containing_a_skipped_name(tmp_path):
    """Segment matching, not substring. `my-site-packages-tool/` and
    `site_packages_util.py` are repo code and must survive."""
    from techne.codex.source import sweep_excluded_rows
    from techne.core.codegraph import add_symbol

    repo = tmp_path / "repo"
    repo.mkdir()
    db = _v3_db(tmp_path)

    keep = [
        "pkg/site_packages_util.py",
        "my-site-packages-tool/main.py",
        "vendor/SITE-PACKAGES/x.py",  # case-SENSITIVE: the walker would index this
        "buildings/plan.py",  # 'build' is skipped; 'buildings' is not
    ]
    for i, f in enumerate(keep):
        add_symbol(db, id=f"{f}::fn::function::{i}", file=f, name="fn", kind="function")

    assert sweep_excluded_rows(db, repo) == 0
    assert _files(db) == sorted(keep)


def test_sweep_relativises_absolute_paths_and_declines_foreign_ones(tmp_path):
    """`_index_file` falls back to an ABSOLUTE path when `relative_to` fails, so
    both shapes are stored. Testing the whole path would empty any repo that
    happens to live under a directory named `build` — the hazard `source.py`
    relativises to avoid, and which the first cut of the migration reintroduced."""
    from techne.codex.source import sweep_excluded_rows
    from techne.core.codegraph import add_symbol

    # The repo itself lives under an ancestor whose name IS on the skip list.
    repo = tmp_path / "build" / "repo"
    repo.mkdir(parents=True)
    db = _v3_db(tmp_path)

    inside_ok = str(repo / "pkg" / "real.py")
    inside_vendored = str(repo / ".venv" / "lib" / "site-packages" / "web.py")
    foreign = "/somewhere/else/site-packages/other.py"
    for i, f in enumerate((inside_ok, inside_vendored, foreign)):
        add_symbol(db, id=f"{f}::fn::function::{i}", file=f, name="fn", kind="function")

    # Exactly one: the repo-internal vendored row. The ancestor `build/` must not
    # drag the whole repo down, and a path outside the repo is not ours to judge.
    assert sweep_excluded_rows(db, repo) == 1
    assert _files(db) == sorted([inside_ok, foreign])


def test_a_clean_db_reports_zero_rather_than_a_false_heal(tmp_path):
    """A sweep that found nothing must say so. #604 stayed invisible precisely
    because a silent purge and a silent no-op look identical from outside."""
    from techne.codex.source import sweep_excluded_rows
    from techne.core.codegraph import add_symbol

    repo = tmp_path / "repo"
    repo.mkdir()
    db = _v3_db(tmp_path)
    add_symbol(
        db, id="pkg/real.py::mine::function::1", file="pkg/real.py", name="mine", kind="function"
    )

    assert sweep_excluded_rows(db, repo) == 0
    assert _files(db) == ["pkg/real.py"]


# ---------------------------------------------------------------------------
# Bounding — the sweep inherits the migration's chunk, and must keep it
# ---------------------------------------------------------------------------


def test_sweep_binds_are_bounded(tmp_path):
    """`doomed` is unbounded — the real db that prompted #613 held 114,410 rows.
    One bind per symbol raises `too many SQL variables`, so the sweep must chunk
    exactly as the migration does. 1,200 clears the 999-variable floor of old
    SQLite builds, which is the bound worth defending."""
    from techne.codex.source import sweep_excluded_rows
    from techne.core.codegraph import add_symbol

    repo = tmp_path / "repo"
    repo.mkdir()
    db = _v3_db(tmp_path)

    for i in range(1200):
        f = f"venv/lib/site-packages/mod{i}.py"
        add_symbol(db, id=f"{f}::fn::function::{i}", file=f, name="fn", kind="function")
    add_symbol(
        db, id="pkg/real.py::mine::function::1", file="pkg/real.py", name="mine", kind="function"
    )

    assert sweep_excluded_rows(db, repo) == 1200
    assert _files(db) == ["pkg/real.py"]


def test_sweep_chunk_is_shared_with_the_migration_not_reimplemented(tmp_path):
    """If these ever diverge, whether a db was healed by the migration or by the
    sweep becomes observable — which is the drift `purge_symbol_ids` exists to
    prevent. Pin the shared entrypoint rather than the behaviour twice."""
    import inspect

    from techne.codex import source
    from techne.core.codex import _codex_v2_to_v3, purge_symbol_ids

    assert "purge_symbol_ids" in inspect.getsource(source.sweep_excluded_rows)
    assert "purge_symbol_ids" in inspect.getsource(_codex_v2_to_v3)
    assert callable(purge_symbol_ids)


# ---------------------------------------------------------------------------
# End to end — the heal a user actually gets
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_GRAMMARS, reason="tree-sitter grammars not installed")
def test_initial_walk_heals_a_polluted_db_and_reports_the_count(tmp_path):
    """The user-facing fix: RE-INDEXING IS THE HEAL. No migration, no version
    bump, no new command — the pollution is gone after one ordinary
    `techne codex index`, and the count is reported rather than left to be
    inferred from a total that merely got smaller."""
    from techne.codex.source import RepoSource
    from techne.config import Config
    from techne.core.codegraph import add_symbol
    from techne.core.codex import codex_db_for_repo

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "real.py").write_text("def mine():\n    return 1\n")

    config = Config(db_path=tmp_path / "techne.db")
    db = codex_db_for_repo(config, repo)

    # A row written by an older tree, for a path the current walker refuses.
    vend = ".venv-ci-3.13/lib/python3.13/site-packages/aiohttp/web.py"
    src = RepoSource(db, repo)
    src.initial_walk()
    add_symbol(
        db, id=f"{vend}::application::function::1", file=vend, name="application", kind="function"
    )
    assert vend in _files(db), "precondition: the db is polluted"

    src2 = RepoSource(db, repo)
    src2.initial_walk()

    assert src2.last_swept == 1
    assert vend not in _files(db)
    assert "pkg/real.py" in _files(db), "the repo's own symbols survive the heal"
