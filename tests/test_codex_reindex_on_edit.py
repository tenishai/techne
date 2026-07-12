"""Mid-session codex freshness — reindex the edited file (techne#301)."""

from __future__ import annotations

from techne.config import Config


def _config(tmp_path) -> Config:
    return Config(db_path=tmp_path / "techne.db", data_dir=tmp_path)


def _indexed_repo(tmp_path, src: str):
    """A .git-marked repo with mod.py, indexed at its DERIVED codex db path."""
    from techne.codex.source import RepoSource
    from techne.core.codex import codex_db_for_repo

    config = _config(tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)  # marker for find_repo_root
    pyfile = repo / "mod.py"
    pyfile.write_text(src)
    db = codex_db_for_repo(config, repo)
    RepoSource(db, repo, languages=["python"]).initial_walk()
    return config, repo, pyfile, db


def test_reindex_edited_file_refreshes_the_graph(tmp_path):
    from techne.core.codegraph import search_symbols
    from techne.core.codex import reindex_edited_file

    config, _repo, pyfile, db = _indexed_repo(
        tmp_path, "def original():\n    return 1\n"
    )
    assert any(h["name"] == "original" for h in search_symbols(db, "original"))
    assert not search_symbols(db, "added_later")  # not present before the edit

    # Edit: add a new function, then reindex JUST this file.
    pyfile.write_text(
        "def original():\n    return 1\n\ndef added_later():\n    return 2\n"
    )
    n = reindex_edited_file(config, str(pyfile))
    assert n is not None and n >= 1

    # The graph reflects the edit — no session restart needed.
    assert any(h["name"] == "added_later" for h in search_symbols(db, "added_later"))


def test_reindex_edited_file_skips_unindexed_repo(tmp_path):
    """A repo never indexed → no-op (None), and no empty db is spawned."""
    from techne.core.codex import reindex_edited_file, repo_db_path

    config = _config(tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    pyfile = repo / "mod.py"
    pyfile.write_text("def x():\n    return 1\n")

    assert reindex_edited_file(config, str(pyfile)) is None
    assert not repo_db_path(config, repo).exists(), "must not create a db"


def test_reindex_edited_file_skips_non_code(tmp_path):
    from techne.core.codex import reindex_edited_file

    config, _repo, _py, _db = _indexed_repo(tmp_path, "def x():\n    return 1\n")
    md = _repo / "notes.md"
    md.write_text("# notes\n")
    assert reindex_edited_file(config, str(md)) is None


def test_reindex_edited_file_no_repo(tmp_path):
    from techne.core.codex import reindex_edited_file

    config = _config(tmp_path)
    loose = tmp_path / "loose.py"  # no .git ancestor
    loose.write_text("def x():\n    return 1\n")
    assert reindex_edited_file(config, str(loose)) is None


# --- hook gate (_maybe_reindex_codex) --------------------------------------


def test_hook_gate_off_does_not_reindex(tmp_path, monkeypatch):
    from techne.hooks import post_tool_use

    called: list[str] = []
    monkeypatch.setattr(
        "techne.core.codex.reindex_edited_file", lambda c, f: called.append(f)
    )
    config = _config(tmp_path)  # codex_reindex_on_edit defaults False
    post_tool_use._maybe_reindex_codex(config, "Edit", {"file_path": "/x/y.py"})
    assert not called


def test_hook_gate_on_reindexes_only_write_edit(tmp_path, monkeypatch):
    from techne.hooks import post_tool_use

    called: list[str] = []
    monkeypatch.setattr(
        "techne.core.codex.reindex_edited_file", lambda c, f: called.append(f)
    )
    config = _config(tmp_path)
    config.hooks.codex_reindex_on_edit = True

    post_tool_use._maybe_reindex_codex(config, "Edit", {"file_path": "/x/a.py"})
    post_tool_use._maybe_reindex_codex(config, "Write", {"file_path": "/x/b.py"})
    post_tool_use._maybe_reindex_codex(config, "Grep", {"pattern": "z"})  # not an edit
    post_tool_use._maybe_reindex_codex(config, "Edit", {})  # no file_path

    assert called == ["/x/a.py", "/x/b.py"]
