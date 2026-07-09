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
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
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
        db_a, id="alpha.py::alpha_only::function::1", file="alpha.py",
        name="alpha_only", kind="function", signature="def alpha_only()",
    )
    add_symbol(
        db_b, id="beta.py::beta_only::function::1", file="beta.py",
        name="beta_only", kind="function", signature="def beta_only()",
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
        legacy, id="x.py::keep::function::1", file="x.py", name="keep", kind="function",
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
    add_symbol(db, id="svc.py::handler::function::1", file="svc.py", name="handler", kind="function")

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
