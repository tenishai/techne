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
    _add(db, repo, "indexed.py")                       # fresh, indexed
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
    add_symbol(db, id="ghost.py::ghost::function::1", file="ghost.py", name="ghost",
               kind="function", file_hash=None)
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
    _add(db, repo, "stale.py", indexed_hash="0" * 64)   # on disk, bytes differ
    _add(db, repo, "gone.py", on_disk=False)            # indexed, deleted from disk

    out = _codex_search({"query": "nothing_here", "repo": str(repo)}, cfg)
    idx = out["index"]

    assert (idx["stale"], idx["missing"], idx["unindexed"]) == (1, 1, 0)
    assert "2 indexed file(s) are stale or missing" in out["notice"]


def test_unindexed_ignores_directories_the_index_never_covered(tmp_path):
    """The unindexed count is SCOPED to directories that already hold indexed
    files, and this pins why.

    `_walk_repo`'s skip list does not exclude virtualenvs: on the real techne
    repo it yields 10,282 files, 10,042 of them inside `.venv-ci-3.12` and
    `.venv-ci-3.13`. An unscoped count reported "10,055 never indexed" — which
    would fire on every repo with a venv, and a warning that always fires is one
    the reader learns to skip. That is the silence this fix exists to remove,
    wearing a louder coat.

    So: a new file BESIDE indexed ones counts; a file in a directory the index
    never touched does not.
    """
    from techne.mcp_server import _codex_search

    cfg = Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)
    repo, db = _repo(cfg, tmp_path)
    _add(db, repo, "pkg.py")                                   # indexed, at repo root
    (repo / "sibling.py").write_text("def added_later(): ...\n")  # NEW, beside it

    noise = repo / ".venv-ci-3.13" / "lib"
    noise.mkdir(parents=True)
    for i in range(5):
        (noise / f"vendored_{i}.py").write_text("x = 1\n")      # never-indexed dir

    out = _codex_search({"query": "nothing_here", "repo": str(repo)}, cfg)

    assert out["index"]["unindexed"] == 1, "the 5 vendored files must not count"
    assert "1 source file(s) on disk have never been indexed" in out["notice"]
