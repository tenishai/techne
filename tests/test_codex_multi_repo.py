"""Codex multi-project support — db-per-repo router (techne#209).

Covers the Phase-1 foundation: the pure repo_root→db_path derivation, per-repo
store preparation, cross-repo isolation (no bleed), enumeration, and the
one-time legacy-db migration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from techne.config import Config
from techne.core import codex
from techne.core.codegraph import add_symbol, search_symbols
from techne.core.codex import (
    codex_db_for_repo,
    list_codex_repos,
    migrate_legacy_codex_db,
    repo_db_path,
    set_meta,
)


def _config(tmp_path: Path) -> Config:
    return Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)


# ---------------------------------------------------------------------------
# Pure derivation
# ---------------------------------------------------------------------------


def test_repo_db_path_is_pure_and_stable(tmp_path):
    """repo_root → db_path is deterministic and normalizes trailing slashes."""
    cfg = _config(tmp_path)
    root = tmp_path / "myrepo"
    root.mkdir()

    p1 = repo_db_path(cfg, root)
    p2 = repo_db_path(cfg, Path(str(root) + "/"))  # trailing slash
    assert p1 == p2, "derivation not stable across trailing slash"
    assert p1.parent == tmp_path / "codex"
    assert p1.name.startswith("myrepo-") and p1.name.endswith(".db")
    # No I/O side effect — deriving a path must not create anything.
    assert not p1.exists()
    assert not p1.parent.exists()


def test_repo_db_path_collision_safe_for_same_basename(tmp_path):
    """Two repos with the same directory name resolve to different db files."""
    cfg = _config(tmp_path)
    a = tmp_path / "a" / "techne"
    b = tmp_path / "b" / "techne"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    pa = repo_db_path(cfg, a)
    pb = repo_db_path(cfg, b)
    assert pa != pb, "same-basename repos collided onto one db"
    assert pa.name.startswith("techne-") and pb.name.startswith("techne-")


def test_repo_slug_sanitizes_awkward_names(tmp_path):
    cfg = _config(tmp_path)
    root = tmp_path / "Weird Repo!!name"
    root.mkdir()
    name = repo_db_path(cfg, root).name
    # Slug is lower-cased and stripped of filesystem-awkward characters.
    assert " " not in name and "!" not in name
    assert name.startswith("weird-repo-name-")


# ---------------------------------------------------------------------------
# Per-repo store preparation
# ---------------------------------------------------------------------------


def test_codex_db_for_repo_creates_and_migrates(tmp_path):
    """codex_db_for_repo yields a ready-to-use db: dir created, schema applied."""
    cfg = _config(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()

    db = codex_db_for_repo(cfg, root)
    assert db.exists()
    assert db == repo_db_path(cfg, root)

    # Schema is present: symbols + codex_meta tables exist.
    from techne.core.stores import plain_connect

    conn = plain_connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"symbols", "edges", "codex_meta"} <= tables


# ---------------------------------------------------------------------------
# Isolation — the headline acceptance: two repos, no bleed
# ---------------------------------------------------------------------------


def test_two_repos_no_bleed(tmp_path):
    """Symbols indexed for repo A never surface when querying repo B's db."""
    cfg = _config(tmp_path)
    repo_a = tmp_path / "alpha"
    repo_b = tmp_path / "beta"
    repo_a.mkdir()
    repo_b.mkdir()

    db_a = codex_db_for_repo(cfg, repo_a)
    db_b = codex_db_for_repo(cfg, repo_b)
    assert db_a != db_b

    add_symbol(
        db_a,
        id="alpha.py::alpha_only::function::1",
        file="alpha.py",
        name="alpha_only",
        kind="function",
        signature="def alpha_only()",
    )
    add_symbol(
        db_b,
        id="beta.py::beta_only::function::1",
        file="beta.py",
        name="beta_only",
        kind="function",
        signature="def beta_only()",
    )

    a_hits = {h["name"] for h in search_symbols(db_a, "alpha_only")}
    b_hits = {h["name"] for h in search_symbols(db_b, "beta_only")}
    assert "alpha_only" in a_hits
    assert "beta_only" in b_hits
    # The whole point of #209: no cross-repo bleed.
    assert "beta_only" not in {h["name"] for h in search_symbols(db_a, "beta_only")}
    assert "alpha_only" not in {h["name"] for h in search_symbols(db_b, "alpha_only")}


def test_end_to_end_index_per_repo(tmp_path):
    """codex_db_for_repo composes with RepoSource for a real tree-sitter index."""
    pytest.importorskip("tree_sitter_python")
    from techne.codex.source import RepoSource

    cfg = _config(tmp_path)
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "mod.py").write_text(
        "def helper():\n    return 1\n\ndef caller():\n    return helper()\n"
    )

    db = codex_db_for_repo(cfg, repo)
    count = RepoSource(db, repo, languages=["python"]).initial_walk()
    assert count >= 2
    names = {h["name"] for h in search_symbols(db, "helper")}
    assert "helper" in names
    # initial_walk stamps repo_root into this repo's own db.
    from techne.core.codex import get_meta

    assert get_meta(db, "repo_root") == str(repo.resolve())


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def test_list_codex_repos(tmp_path):
    cfg = _config(tmp_path)
    assert list_codex_repos(cfg) == []  # nothing indexed yet

    repo_a = tmp_path / "alpha"
    repo_b = tmp_path / "beta"
    repo_a.mkdir()
    repo_b.mkdir()
    db_a = codex_db_for_repo(cfg, repo_a)
    db_b = codex_db_for_repo(cfg, repo_b)
    set_meta(db_a, "repo_root", str(repo_a.resolve()))
    set_meta(db_b, "repo_root", str(repo_b.resolve()))

    listed = dict(list_codex_repos(cfg))
    assert listed == {
        str(repo_a.resolve()): db_a,
        str(repo_b.resolve()): db_b,
    }


def test_list_codex_repos_skips_unstamped_db(tmp_path):
    """A db that was created but never had repo_root stamped is skipped."""
    cfg = _config(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    codex_db_for_repo(cfg, repo)  # created, but no repo_root set
    assert list_codex_repos(cfg) == []


# ---------------------------------------------------------------------------
# One-time legacy migration
# ---------------------------------------------------------------------------


def test_migrate_legacy_codex_db(tmp_path):
    """Legacy <data_dir>/codex.db moves to its per-repo path, data preserved."""
    cfg = _config(tmp_path)
    repo = tmp_path / "legacyrepo"
    repo.mkdir()

    # Build a legacy single global codex.db at <data_dir>/codex.db.
    from techne.core.codex import codex_store

    legacy = codex_store(cfg).db_path
    assert legacy == tmp_path / "codex.db"
    add_symbol(
        legacy,
        id="x.py::keep::function::1",
        file="x.py",
        name="keep",
        kind="function",
    )
    set_meta(legacy, "repo_root", str(repo.resolve()))

    dest = migrate_legacy_codex_db(cfg)
    assert dest == repo_db_path(cfg, repo)
    assert dest.exists()
    assert not legacy.exists(), "legacy db should be moved, not copied"
    # Data survived the move.
    assert "keep" in {h["name"] for h in search_symbols(dest, "keep")}

    # Idempotent: second call is a no-op.
    assert migrate_legacy_codex_db(cfg) is None


def test_migrate_legacy_no_legacy_db(tmp_path):
    """No legacy db present → no-op."""
    cfg = _config(tmp_path)
    assert migrate_legacy_codex_db(cfg) is None


def test_migrate_legacy_without_repo_root_left_in_place(tmp_path):
    """A legacy db with no stored repo_root can't be routed — leave it put."""
    cfg = _config(tmp_path)
    from techne.core.codex import codex_store

    legacy = codex_store(cfg).db_path
    add_symbol(legacy, id="x.py::f::function::1", file="x.py", name="f", kind="function")
    # No set_meta(repo_root).
    assert migrate_legacy_codex_db(cfg) is None
    assert legacy.exists()


def test_migrate_legacy_empty_husk_is_removed_quietly(tmp_path, caplog):
    """techne#875: a zero-table legacy db warned on every process, forever."""
    import logging
    import sqlite3

    cfg = _config(tmp_path)
    legacy = tmp_path / "codex.db"
    sqlite3.connect(legacy).execute("PRAGMA user_version = 0").connection.close()
    assert legacy.exists()

    with caplog.at_level(logging.DEBUG, logger="techne.core.codex"):
        assert migrate_legacy_codex_db(cfg) is None
    assert not legacy.exists(), "an empty husk carries no data and must not recur"
    assert not (tmp_path / "codex.db-wal").exists()
    assert not (tmp_path / "codex.db-shm").exists()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_migrate_legacy_with_tables_but_no_repo_root_still_warns(tmp_path, caplog):
    """The anomaly worth a warning is a db that HAS content and no repo_root."""
    import logging

    cfg = _config(tmp_path)
    from techne.core.codex import codex_store

    legacy = codex_store(cfg).db_path
    with caplog.at_level(logging.WARNING, logger="techne.core.codex"):
        assert migrate_legacy_codex_db(cfg) is None
    assert legacy.exists()
    assert any("has no repo_root" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Shared helper promotion
# ---------------------------------------------------------------------------


def test_find_repo_root_promoted_and_reexported():
    from techne.core.anchor import _find_repo_root
    from techne.core.repo import find_repo_root

    assert _find_repo_root is find_repo_root

    # It actually finds this repo's root from a nested source file.
    here = Path(codex.__file__)
    root = find_repo_root(here)
    assert root is not None
    assert (root / ".git").exists()


# ---------------------------------------------------------------------------
# CLI wiring (techne#209 Phase 2)
# ---------------------------------------------------------------------------


def _cli(monkeypatch, cfg):
    """Patch Config.load to return *cfg* and hand back a CliRunner + main."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))
    return CliRunner(), main


def test_cli_index_then_search_scoped(tmp_path, monkeypatch):
    """`codex index REPO` writes the per-repo db; `--repo` scopes the search."""
    pytest.importorskip("tree_sitter_python")
    (tmp_path / "techne.db").touch()
    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    runner, main = _cli(monkeypatch, cfg)

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "m.py").write_text("def widget():\n    return 1\n")

    r = runner.invoke(main, ["codex", "index", str(repo)])
    assert r.exit_code == 0, r.output
    assert "Indexed:" in r.output
    assert repo_db_path(cfg, repo).exists()

    r = runner.invoke(main, ["codex", "search", "widget", "--repo", str(repo)])
    assert r.exit_code == 0, r.output
    assert "widget" in r.output


def test_cli_search_unindexed_repo_errors(tmp_path, monkeypatch):
    """Querying a repo with no index fails cleanly with a run-index hint."""
    (tmp_path / "techne.db").touch()
    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    runner, main = _cli(monkeypatch, cfg)

    repo = tmp_path / "proj"
    repo.mkdir()
    r = runner.invoke(main, ["codex", "search", "anything", "--repo", str(repo)])
    assert r.exit_code != 0
    assert "no codex index" in r.output


def test_cli_stats_all(tmp_path, monkeypatch):
    """`codex stats --all` reports every indexed repo."""
    import json as _json

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    (tmp_path / "techne.db").touch()
    runner, main = _cli(monkeypatch, cfg)

    repo = tmp_path / "alpha"
    repo.mkdir()
    db = codex_db_for_repo(cfg, repo)
    add_symbol(db, id="a.py::f::function::1", file="a.py", name="f", kind="function")
    set_meta(db, "repo_root", str(repo.resolve()))

    r = runner.invoke(main, ["codex", "stats", "--all"])
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.output)
    assert str(repo.resolve()) in payload
    assert payload[str(repo.resolve())]["symbols"] == 1


def test_cli_index_all_reindexes_known_repos(tmp_path, monkeypatch):
    pytest.importorskip("tree_sitter_python")
    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    (tmp_path / "techne.db").touch()
    runner, main = _cli(monkeypatch, cfg)

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "m.py").write_text("def gadget():\n    return 1\n")
    runner.invoke(main, ["codex", "index", str(repo)])

    r = runner.invoke(main, ["codex", "index-all"])
    assert r.exit_code == 0, r.output
    assert "reindexed" in r.output
    assert str(repo) in r.output


# ---------------------------------------------------------------------------
# MCP tools — per-repo scoping (techne#209 Phase 3)
# ---------------------------------------------------------------------------


def test_mcp_codex_search_scoped_by_repo_arg(tmp_path):
    """codex_search's `repo` arg selects which repo's graph to search."""
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / ".git").mkdir()
    db = codex_db_for_repo(cfg, repo)
    add_symbol(
        db, id="svc.py::handler::function::1", file="svc.py", name="handler", kind="function"
    )

    out = _codex_search({"query": "handler", "repo": str(repo)}, cfg)
    assert out["count"] == 1
    assert out["results"][0]["name"] == "handler"


def test_mcp_codex_search_no_repo_returns_hint(tmp_path):
    """No indexed repo at the given path → empty results with a run-index hint."""
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()  # not a git repo

    out = _codex_search({"query": "anything", "repo": str(plain)}, cfg)
    assert out["count"] == 0
    assert out["results"] == []
    assert "no codex index" in out["notice"]


def test_mcp_codex_blast_radius_no_repo_returns_hint(tmp_path):
    from techne.mcp_server import _codex_blast_radius

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()

    out = _codex_blast_radius({"symbol_id": "x", "repo": str(plain)}, cfg)
    assert out["nodes"] == []
    assert "no codex index" in out["notice"]


# ---------------------------------------------------------------------------
# Phase 4 — full two-real-repo integration through the CLI.
#
# The adversarial case: both repos have the SAME relative file path (svc.py)
# AND a same-named symbol (shared) — exactly what collides/bleeds under a single
# global db. db-per-repo must keep them fully isolated end-to-end.
# ---------------------------------------------------------------------------

_REPO_A_SRC = "def alpha_handler():\n    return shared()\n\n\ndef shared():\n    return 1\n"
_REPO_B_SRC = "def beta_handler():\n    return shared()\n\n\ndef shared():\n    return 2\n"


def _symbol_lines(output: str) -> list[str]:
    """The symbol rows of `codex search` output (id is the first token)."""
    return [ln.strip() for ln in output.splitlines() if "[function]" in ln]


def test_two_real_repos_end_to_end_cli(tmp_path, monkeypatch):
    """Index two real repos via the CLI and prove full isolation + scoping."""
    pytest.importorskip("tree_sitter_python")
    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    (tmp_path / "techne.db").touch()
    runner, main = _cli(monkeypatch, cfg)

    repo_a = tmp_path / "alpha"
    repo_b = tmp_path / "beta"
    for repo, src in ((repo_a, _REPO_A_SRC), (repo_b, _REPO_B_SRC)):
        repo.mkdir()
        (repo / ".git").mkdir()  # so cwd-based resolution could also find it
        (repo / "svc.py").write_text(src)  # SAME relative path in both repos

    # 1. Index both via the CLI.
    for repo in (repo_a, repo_b):
        r = runner.invoke(main, ["codex", "index", str(repo)])
        assert r.exit_code == 0, r.output
    assert repo_db_path(cfg, repo_a) != repo_db_path(cfg, repo_b)

    # 2. stats --all reports both, each with its own symbols.
    import json as _json

    r = runner.invoke(main, ["codex", "stats", "--all"])
    stats_all = _json.loads(r.output)
    assert set(stats_all) == {str(repo_a.resolve()), str(repo_b.resolve())}
    assert stats_all[str(repo_a.resolve())]["symbols"] >= 2
    assert stats_all[str(repo_b.resolve())]["symbols"] >= 2

    # 3. Repo-only symbol is visible in its repo, INVISIBLE in the other.
    ra = runner.invoke(main, ["codex", "search", "alpha_handler", "--repo", str(repo_a)])
    assert "alpha_handler" in ra.output
    rb = runner.invoke(main, ["codex", "search", "alpha_handler", "--repo", str(repo_b)])
    assert "alpha_handler" not in rb.output  # no cross-repo bleed

    # 4. The SAME-named symbol resolves independently in each db (no collision).
    for repo in (repo_a, repo_b):
        rs = runner.invoke(main, ["codex", "search", "shared", "--repo", str(repo)])
        lines = _symbol_lines(rs.output)
        assert any("shared" in ln for ln in lines), rs.output

    # 5. blast-radius stays within a repo: shared's dependents in A include
    #    alpha_handler and NOT beta_handler.
    shared_id = next(
        s["id"]
        for s in search_symbols(repo_db_path(cfg, repo_a), "shared")
        if s["name"] == "shared"
    )
    rbr = runner.invoke(
        main, ["codex", "blast-radius", shared_id, "--repo", str(repo_a), "--direction", "in"]
    )
    assert "alpha_handler" in rbr.output
    assert "beta_handler" not in rbr.output

    # 6. index-all reindexes both (idempotent — unchanged files skipped).
    r = runner.invoke(main, ["codex", "index-all"])
    assert r.exit_code == 0
    assert str(repo_a) in r.output and str(repo_b) in r.output

    # 7. Per-repo freshness: editing A marks A stale, leaves B fresh.
    (repo_a / "svc.py").write_text(_REPO_A_SRC.replace("return 1", "return 42"))
    ra2 = runner.invoke(main, ["codex", "search", "shared", "--repo", str(repo_a)])
    assert "[stale]" in ra2.output
    rb2 = runner.invoke(main, ["codex", "search", "shared", "--repo", str(repo_b)])
    assert "[stale]" not in rb2.output  # B untouched by A's edit

    # 8. The group overview lists both indexed repos.
    r = runner.invoke(main, ["codex"])
    assert str(repo_a.resolve()) in r.output and str(repo_b.resolve()) in r.output


# ---- techne#595: a zero result must describe its own reliability -------------


def _repo(cfg, tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo, codex_db_for_repo(cfg, repo)


def _add(db, repo, rel, *, body="def existing(): ...\n", indexed_hash=None, on_disk=True):
    """Index one symbol from `rel`. `indexed_hash` mismatching the bytes = stale."""
    import hashlib

    if on_disk:
        (repo / rel).write_text(body)
    add_symbol(
        db,
        id=f"{rel}::sym_{rel.replace('.', '_')}::function::1",
        file=rel,
        name=f"sym_{rel.replace('.', '_')}",
        kind="function",
        file_hash=indexed_hash or hashlib.sha256(body.encode()).hexdigest(),
    )


def test_a_brand_new_module_is_not_reported_as_genuine_absence(tmp_path):
    """THE ROUND-1 FINDING, and it is the whole reason this fix was rewritten.

    `index_freshness` compares INDEXED files against disk. That structurally
    cannot see a file the index never saw — and a symbol added since the last
    index arrives in a NEW module at least as often as in an edited one.

    The first cut certified "genuine absence" whenever the indexed files happened
    to be fresh. Prudence's repro: one untouched indexed file, the sought symbol
    sitting in `brand_new.py` beside it, and the tool answered "the index is
    current (1 files all fresh) — so this is genuine absence". That is WORSE than
    the silence it replaced: it turns no-information into a confident wrong answer.
    """
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    _add(db, repo, "indexed.py")  # fresh, indexed
    (repo / "brand_new.py").write_text("def recall_attribution(): ...\n")  # never indexed

    out = _codex_search({"query": "recall_attribution", "repo": str(repo)}, cfg)

    assert out["count"] == 0
    assert out["index"]["unindexed"] == 1
    assert out["index"]["coverage"] == "complete"
    assert "never been indexed" in out["notice"]
    assert "genuine absence" not in out["notice"]


def test_an_empty_index_never_claims_to_be_current(tmp_path):
    """`codex_db_for_repo` creates the db BEFORE `initial_walk`, so a docs-only
    repo, a Go/Rust repo, or an install without the `codex` extra all leave a db
    with zero symbols. An EMPTY index issuing a clean bill of health is the
    strongest statement of trust from the weakest evidence."""
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, _ = _repo(cfg, tmp_path)

    out = _codex_search({"query": "anything", "repo": str(repo)}, cfg)

    assert out["count"] == 0
    assert out["index"]["files"] == 0
    assert "THE INDEX IS EMPTY" in out["notice"]
    assert "genuine absence" not in out["notice"]


def test_the_notice_reports_counts_that_can_discriminate(tmp_path):
    """Round 1 killed the first tests with THREE surviving mutants — deleting the
    unknown branch, dropping `missing` from the total, and swapping the notice's
    numbers — because the fixture made every count equal 1, so no arithmetic in
    the message could be wrong.

    This fixture makes them DISTINCT: 3 indexed files (1 fresh, 2 stale) plus 2
    never indexed, so a swapped or miscounted figure shows up as a different
    string.
    """
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    _add(db, repo, "a.py")
    _add(db, repo, "b.py", indexed_hash="0" * 64)
    _add(db, repo, "c.py", indexed_hash="1" * 64)
    (repo / "d.py").write_text("x = 1\n")
    (repo / "e.py").write_text("y = 2\n")

    out = _codex_search({"query": "nothing_here", "repo": str(repo)}, cfg)
    idx = out["index"]

    assert (idx["files"], idx["fresh"], idx["stale"], idx["unindexed"]) == (3, 1, 2, 2)
    # Unindexed outranks stale: a never-seen file is the stronger doubt.
    assert "2 source file(s) on disk have never been indexed" in out["notice"]
    assert "3 file(s) — 1 fresh, 2 stale, 0 missing, 0 unclassifiable" in out["notice"]
    assert "2 on disk never indexed" in out["notice"]


def test_genuine_absence_requires_a_complete_and_current_index(tmp_path):
    """The claim survives, but only on evidence that supports it: disk enumerated,
    nothing unindexed, everything fresh. A notice that ALWAYS hedges gets ignored,
    so this branch has to stay reachable — it just may not be reached cheaply."""
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    _add(db, repo, "only.py")

    out = _codex_search({"query": "genuinely_absent", "repo": str(repo)}, cfg)

    assert out["count"] == 0
    assert (out["index"]["stale"], out["index"]["unindexed"]) == (0, 0)
    assert "genuine absence" in out["notice"]


def test_a_hit_does_not_pay_for_the_index_scan(tmp_path):
    """The scan walks the repo AND hashes every indexed file — affordable to
    qualify a nothing, not to decorate a hit. A non-empty result must not carry
    the index summary."""
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    _add(db, repo, "only.py")

    out = _codex_search({"query": "sym_only_py", "repo": str(repo)}, cfg)

    assert out["count"] == 1
    assert "index" not in out


def test_an_unreadable_hit_is_qualified_too(tmp_path):
    """Round 1, aim point 1: the row-derived notice counted only stale/missing, so
    a hit whose file could not be resolved came back with NO qualification. An
    unverifiable hit is not a trustworthy hit."""
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    # Indexed, but the file is not on disk -> "missing"; and one with no hash at
    # all -> "unknown", which previously escaped the notice entirely.
    add_symbol(
        db,
        id="ghost.py::ghost::function::1",
        file="ghost.py",
        name="ghost",
        kind="function",
        file_hash=None,
    )
    (repo / "ghost.py").write_text("def ghost(): ...\n")

    out = _codex_search({"query": "ghost", "repo": str(repo)}, cfg)

    assert out["count"] == 1
    assert out["results"][0]["freshness"] == "unknown"
    assert "unverifiable" in out["notice"]


def test_the_stale_branch_counts_missing_files_too(tmp_path):
    """`stale + missing` must be discriminable from `stale` alone.

    The first discriminating fixture had `missing == 0`, so dropping `missing`
    from that sum survived as a mutant — the round-1 trap one variable over. Here
    one indexed file is stale and another was DELETED from disk, with nothing
    unindexed, so the stale branch is reached and the total can only be right by
    counting both.
    """
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    _add(db, repo, "stale.py", indexed_hash="0" * 64)  # on disk, bytes differ
    _add(db, repo, "gone.py", on_disk=False)  # indexed, deleted from disk

    out = _codex_search({"query": "nothing_here", "repo": str(repo)}, cfg)
    idx = out["index"]

    assert (idx["stale"], idx["missing"], idx["unindexed"]) == (1, 1, 0)
    assert "2 indexed file(s) are stale or missing" in out["notice"]


def test_unindexed_ignores_directories_the_index_never_covered(tmp_path):
    """The unindexed count is SCOPED to directories that already hold indexed
    files, and this pins why.

    HISTORICALLY — before techne#604 taught `_walk_repo` to skip `site-packages`
    — the walk on the real techne repo yielded 10,282 files, 10,042 of them inside
    `.venv-ci-3.12` and `.venv-ci-3.13`, and an unscoped count reported "10,055
    never indexed". Those figures are now 242 and 2. The venv case is fixed at its
    source; this test keeps pinning the GENERAL case, since `_SKIP_DIRS` cannot
    enumerate every uncovered tree. A warning that always fires is one
    the reader learns to skip. That is the silence this fix exists to remove,
    wearing a louder coat.

    So: a new file BESIDE indexed ones counts; a file in a directory the index
    never touched does not.

    THE NOISE DIRECTORY'S NAME IS LOAD-BEARING (techne#600). It must be a name
    `_SKIP_DIRS` does NOT filter, or `_walk_repo` removes the noise before the
    scoping rule is ever consulted and this test passes with the scoping DELETED
    — correct by accident of a directory name, and a well-meaning tidy-up to the
    conventional `.venv` would silently disarm it. The assertion below fails
    loudly if anyone renames it into the skip list.
    """
    from techne.codex.source import _SKIP_DIRS
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    _add(db, repo, "pkg.py")  # indexed, at repo root
    (repo / "sibling.py").write_text("def added_later(): ...\n")  # NEW, beside it

    noise_dir_name = ".venv-ci-3.13"
    assert noise_dir_name not in _SKIP_DIRS, (
        f"{noise_dir_name!r} is now in _SKIP_DIRS, so _walk_repo filters this noise "
        "before the scoping rule runs and this test no longer pins anything (#600)"
    )
    noise = repo / noise_dir_name / "lib"
    noise.mkdir(parents=True)
    for i in range(5):
        (noise / f"vendored_{i}.py").write_text("x = 1\n")  # never-indexed dir

    out = _codex_search({"query": "nothing_here", "repo": str(repo)}, cfg)

    assert out["index"]["unindexed"] == 1, "the 5 vendored files must not count"
    assert "1 source file(s) on disk have never been indexed" in out["notice"]


def test_a_new_subpackage_is_not_reported_as_genuine_absence(tmp_path):
    """THE ROUND-2 CRITICAL. Scoping by the file's OWN DIRECTORY was blind to any
    NEW DIRECTORY, not merely a new top-level package.

    A new sub-package under an already-indexed tree scored 0 unindexed and earned
    "genuine absence" with the symbol sitting on disk. Worse in a
    per-module-directory layout (`Foo/index.ts`, and this indexer handles `.ts`):
    every new module lands in a new directory, so the count is pinned at 0
    forever while the notice asserts certainty — the fix doing nothing, loudly.

    Top-level component fixes it: `pkg/newsub/` still belongs to `pkg/`.
    """
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    (repo / "pkg").mkdir()
    _add(db, repo, "pkg/known.py")
    (repo / "pkg" / "newsub").mkdir()
    (repo / "pkg" / "newsub" / "mod.py").write_text("def sought(): ...\n")

    out = _codex_search({"query": "sought", "repo": str(repo)}, cfg)

    assert out["index"]["unindexed"] == 1, "a new DIRECTORY under an indexed tree must count"
    assert "genuine absence" not in out["notice"]


def test_per_module_directory_layout_is_not_pinned_at_zero(tmp_path):
    """The same Critical in the layout where it is total rather than occasional.

    With `src/Foo/index.ts` per module, EVERY new module is a new directory, so
    the per-directory rule could never report anything — a fix that runs, reports
    zero, and certifies absence forever.
    """
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    (repo / "src" / "Bar").mkdir(parents=True)
    _add(db, repo, "src/Bar/index.ts", body="export function bar() {}\n")
    for name in ("Foo", "Baz"):
        (repo / "src" / name).mkdir()
        (repo / "src" / name / "index.ts").write_text("export function x() {}\n")

    out = _codex_search({"query": "nothing_here", "repo": str(repo)}, cfg)

    assert out["index"]["unindexed"] == 2
    assert "genuine absence" not in out["notice"]


def test_unenumerable_disk_is_reported_as_unknown_not_absence(tmp_path, monkeypatch):
    """PINS THE `coverage != complete` BRANCH — a surviving mutant in round 2.

    Deleting that guard reproduces the ROUND-1 DEFECT VERBATIM with a green
    suite: coverage falls through to the genuine-absence branch, and the tool
    certifies absence on a disk it never read. Both surviving mutants were
    doubt-expressing branches failing open to certainty, which is the shape of
    every defect on this PR.

    Driven through the real path: the walker raises, so `_unindexed_count`'s own
    OSError handler produces the unknown coverage.
    """
    import techne.codex.source as src
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    _add(db, repo, "only.py")

    def boom(*a, **k):
        raise OSError("cannot enumerate")
        yield  # pragma: no cover — keeps this a generator

    monkeypatch.setattr(src, "_walk_repo", boom)

    out = _codex_search({"query": "anything", "repo": str(repo)}, cfg)

    assert out["index"]["coverage"] == "unknown"
    assert out["index"]["unindexed"] is None
    assert "UNKNOWN, not absent" in out["notice"]
    assert "genuine absence" not in out["notice"]


def test_unclassifiable_files_block_the_absence_claim(tmp_path):
    """PINS THE `unknown` BRANCH of the notice — the other round-2 survivor.

    A symbol stored with no file_hash cannot be compared against disk, so the
    index's freshness is not established. Deleting this branch let such a repo
    fall through to "genuine absence" — certainty from evidence that was never
    checked. Note the same-named kill in my round-1 write-up was the HIT-ROW
    `stale_count` branch, which is separately pinned; this is the notice's.
    """
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    (repo / "opaque.py").write_text("def opaque(): ...\n")
    add_symbol(
        db,
        id="opaque.py::opaque::function::1",
        file="opaque.py",
        name="opaque",
        kind="function",
        file_hash=None,
    )

    out = _codex_search({"query": "nothing_here", "repo": str(repo)}, cfg)

    assert (out["index"]["unknown"], out["index"]["stale"]) == (1, 0)
    assert "could not be classified" in out["notice"]
    assert "genuine absence" not in out["notice"]


def test_an_unreadable_directory_blocks_the_absence_claim(tmp_path):
    """techne#599: `Path.rglob` swallows per-directory errors internally, so an
    unreadable directory simply DROPPED OUT of the walk — no exception, no signal.

    `_unindexed_count`'s `except OSError` documented a degraded result on failure
    and never fired. A `chmod 000` directory holding the sought symbol vanished
    from enumeration while coverage still said "complete" and the tool asserted
    "This is genuine absence" — the exact sentence #595 exists to make
    trustworthy. The fourth failure-open found on that PR.
    """
    import os
    import sys

    import pytest as _pytest

    if sys.platform == "win32" or os.geteuid() == 0:
        _pytest.skip("needs POSIX permissions and a non-root user")

    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    _add(db, repo, "pkg.py")

    hidden = repo / "secret"
    hidden.mkdir()
    (hidden / "target.py").write_text("def the_symbol_we_want(): ...\n")
    os.chmod(hidden, 0o000)
    try:
        out = _codex_search({"query": "the_symbol_we_want", "repo": str(repo)}, cfg)
    finally:
        os.chmod(hidden, 0o755)  # so tmp_path cleanup can remove it

    assert out["index"]["coverage"] == "unknown", (
        "a directory we could not read must not be reported as a complete enumeration"
    )
    assert "genuine absence" not in out["notice"]
    assert "UNKNOWN, not absent" in out["notice"]


def test_a_symlinked_directory_blocks_the_absence_claim(tmp_path):
    """techne#599 route 2: symlinked directories are not descended into — the
    same as before — but the hole is now observable instead of unrecorded."""
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    _add(db, repo, "pkg.py")

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "target.py").write_text("def the_symbol_we_want(): ...\n")
    (repo / "linked").symlink_to(outside, target_is_directory=True)

    out = _codex_search({"query": "the_symbol_we_want", "repo": str(repo)}, cfg)

    assert out["index"]["coverage"] == "unknown"
    assert "genuine absence" not in out["notice"]


# ---------------------------------------------------------------------------
# techne#679 — the resolver must actually RUN, not merely exist.
#
# WHY THESE EXIST. #856 shipped `edges.to_module` (schema v4) and
# `techne/codex/import_resolve.py` to consume it, and argued in its own body
# that shipping the column without a reader would be a fresh instance of the
# declared-and-never-read class #517 tracks. It was — one level up.
# `resolve_edges_by_import` had NO production caller: not the indexer, not the
# CLI, not the MCP server. Only tests. So a merged, deployed, fully re-indexed
# corpus would still have resolved zero call edges, and the PR's measured
# "5,864 resolved" was reachable only by invoking the function by hand.
#
# These assert the END STATE IN THE DB, not a line of CLI output, because the
# output is what a wiring bug leaves behind most convincingly.
# ---------------------------------------------------------------------------


def _resolvable_repo(root: Path) -> None:
    """A repo whose call edge is resolvable ONLY through an import binding."""
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "helpers.py").write_text("def helper():\n    return 1\n")
    (root / "app.py").write_text(
        "from pkg.helpers import helper\n\n\ndef run():\n    return helper()\n"
    )


def _import_resolved(db) -> int:
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM edges WHERE kind='call' "
            "AND resolution_status='import_resolved' AND to_symbol IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()


def test_codex_index_resolves_call_edges_without_being_asked(tmp_path, monkeypatch):
    """THE ACCEPTANCE CRITERION FOR #679: plain `codex index` leaves resolved edges.

    No `--lsp`, no second command, no manual call. If this passes only when a
    human remembers an extra step, the corpus stays at 1 resolved edge in
    33,081, which is the measured state this issue exists to change.
    """
    pytest.importorskip("tree_sitter_python")
    (tmp_path / "techne.db").touch()
    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    runner, main = _cli(monkeypatch, cfg)

    repo = tmp_path / "proj"
    repo.mkdir()
    _resolvable_repo(repo)

    r = runner.invoke(main, ["codex", "index", str(repo)])
    assert r.exit_code == 0, r.output
    assert _import_resolved(repo_db_path(cfg, repo)) >= 1, (
        "codex index left every call edge unresolved — the import-binding pass "
        f"did not run. Output was:\n{r.output}"
    )


def test_codex_index_all_resolves_too(tmp_path, monkeypatch):
    """The second call site, pinned separately.

    `index` and `index-all` walk repos through separate code paths, so wiring
    the pass into one and not the other is the ordinary way this regresses —
    the same declared-twice shape #852 fixed in the recall cap.
    """
    pytest.importorskip("tree_sitter_python")
    (tmp_path / "techne.db").touch()
    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    runner, main = _cli(monkeypatch, cfg)

    repo = tmp_path / "proj"
    repo.mkdir()
    _resolvable_repo(repo)

    assert runner.invoke(main, ["codex", "index", str(repo)]).exit_code == 0
    db = repo_db_path(cfg, repo)

    # THE FIXTURE MUST REACH THE CONDITION. `codex index` above already
    # resolved these edges, so without this reset `index-all` inherits a
    # resolved db and the assertion below passes whether or not the pass is
    # wired into it — which is exactly what the first draft of this test did,
    # caught by mutating the index-all call site and watching nothing go red.
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE edges SET to_symbol = NULL, resolution_status = 'unresolved' "
        "WHERE kind = 'call'"
    )
    conn.commit()
    conn.close()
    assert _import_resolved(db) == 0, "reset did not take; the test below proves nothing"

    r = runner.invoke(main, ["codex", "index-all"])
    assert r.exit_code == 0, r.output
    assert _import_resolved(db) >= 1, (
        f"index-all left every call edge unresolved — the pass is wired into "
        f"`index` but not `index-all`. Output was:\n{r.output}"
    )
