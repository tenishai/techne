"""Ephemeral memory reconciliation — `techne memory doctor` (techne#107 PR1).

Read-only file-reference diff of curated memory (CC's memory/*.md) against the raw
deed-log. No persistence; these tests seed the ledger + a tmp memory file and assert
the omission/unbacked/corroborated split, plus the deed_log cross-session query.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from techne.core.database import connect, init_db
from techne.core.memory_doctor import (
    _deed_touched_files,
    find_curated_memory_files,
    reconcile,
)
from techne.core.state import deed_log
from techne.platforms.claude_code.paths import project_slug


@pytest.fixture
def project(tmp_path):
    """A tmp project cwd + a redirected CC config dir, so discovery is hermetic."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    config_dir = tmp_path / "ccconfig"
    return cwd, config_dir


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    init_db(p).close()
    return p


def _session(db, sid, project_path):
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO sessions (id, project_path) VALUES (?, ?)", (sid, project_path)
        )
        conn.commit()
    finally:
        conn.close()


def _obs(db, sid, tool_name, content, timestamp=None):
    conn = connect(db)
    try:
        if timestamp:
            conn.execute(
                "INSERT INTO observations (session_id, tool_name, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (sid, tool_name, content, timestamp),
            )
        else:
            conn.execute(
                "INSERT INTO observations (session_id, tool_name, content) VALUES (?, ?, ?)",
                (sid, tool_name, content),
            )
        conn.commit()
    finally:
        conn.close()


def _write_memory(config_dir: Path, cwd: Path, text: str) -> Path:
    mem_dir = config_dir / "projects" / project_slug(cwd) / "memory"
    mem_dir.mkdir(parents=True)
    f = mem_dir / "MEMORY.md"
    f.write_text(text, encoding="utf-8")
    return f


# ---- deed_log -------------------------------------------------------------


def test_deed_log_spans_sessions_and_scopes_project(db):
    _session(db, "s1", "/proj")
    _session(db, "s2", "/proj")
    _session(db, "other", "/elsewhere")
    _obs(db, "s1", "Write", "Wrote: /proj/a.py")
    _obs(db, "s2", "Edit", "Edited: /proj/b.py")
    _obs(db, "other", "Write", "Wrote: /elsewhere/z.py")

    all_proj = deed_log(db, project_path="/proj")
    assert {d["session_id"] for d in all_proj} == {"s1", "s2"}  # cross-session, scoped
    assert len(deed_log(db)) == 3  # unscoped sees everything


def test_deed_log_windows_by_since_days(db):
    _session(db, "s1", "/proj")
    _obs(db, "s1", "Write", "Wrote: /proj/recent.py")
    _obs(db, "s1", "Write", "Wrote: /proj/old.py", timestamp="2020-01-01 00:00:00")
    recent = deed_log(db, since_days=7)
    contents = " ".join(d["content"] for d in recent)
    assert "recent.py" in contents and "old.py" not in contents


# ---- touched-file extraction ----------------------------------------------


def test_only_write_edit_count_as_touched(db):
    deeds = [
        {"tool_name": "Write", "content": "Wrote: /p/a.py\nContent: x"},
        {"tool_name": "Edit", "content": "Edited: /p/b.py\nChange: ..."},
        {"tool_name": "Bash", "content": "Ran: cat /p/c.py"},  # mention, not a touch
        {"tool_name": "Read", "content": "Read /p/d.py"},
    ]
    assert _deed_touched_files(deeds) == {"a.py", "b.py"}


def test_multiedit_notebookedit_touched_via_json_file_path(db):
    """MultiEdit/NotebookEdit render 'Tool: …\\nInput: {json}' (no Wrote:/Edited: prefix),
    so the path is read from the json's file_path — else they'd falsely read as unbacked."""
    deeds = [
        {"tool_name": "MultiEdit",
         "content": 'Tool: MultiEdit\nInput: {"file_path": "/p/multi.py", "edits": [...]}'},
        {"tool_name": "NotebookEdit",
         "content": 'Tool: NotebookEdit\nInput: {"file_path": "/p/nb.ipynb", "cell": 1}'},
    ]
    assert _deed_touched_files(deeds) == {"multi.py", "nb.ipynb"}


# ---- discovery ------------------------------------------------------------


def test_find_curated_memory_files(project, monkeypatch):
    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    assert find_curated_memory_files(cwd) == []  # none yet
    f = _write_memory(config_dir, cwd, "# memory\n")
    assert find_curated_memory_files(cwd) == [f]


# ---- the reconcile diff ---------------------------------------------------


def test_reconcile_splits_omission_unbacked_corroborated(project, db, monkeypatch):
    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    proj = str(cwd.resolve())
    _session(db, "s1", proj)
    _session(db, "s2", proj)
    _obs(db, "s1", "Write", "Wrote: /proj/a.py\nContent: ...")   # touched
    _obs(db, "s1", "Edit", "Edited: /proj/b.py\nChange: ...")    # touched
    _obs(db, "s2", "Write", "Wrote: /proj/d.py\nContent: ...")   # touched
    _write_memory(config_dir, cwd, "Worked on a.py and planned c.py.")  # names a.py, c.py

    report = reconcile(db, cwd=cwd)
    assert report["memory_present"] is True
    assert report["corroborated"] == ["a.py"]            # in memory AND touched
    assert report["unbacked"] == ["c.py"]                # in memory, never touched
    assert set(report["omissions"]) == {"b.py", "d.py"}  # touched, not in memory


def test_reconcile_no_memory_present(project, db, monkeypatch):
    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    _session(db, "s1", str(cwd.resolve()))
    _obs(db, "s1", "Write", "Wrote: /proj/a.py")
    report = reconcile(db, cwd=cwd)
    assert report["memory_present"] is False
    assert report["unbacked"] == [] and report["corroborated"] == []


def test_reconcile_flags_capped_deed_log(project, db, monkeypatch):
    """When the deed-log hits the cap, the report says so — silent truncation would drop
    the oldest deeds and read as false `unbacked`."""
    from techne.core import memory_doctor as md

    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(md, "DEED_LIMIT", 2)
    proj = str(cwd.resolve())
    _session(db, "s1", proj)
    for i in range(3):
        _obs(db, "s1", "Write", f"Wrote: /proj/f{i}.py")
    _write_memory(config_dir, cwd, "# m\n")
    report = md.reconcile(db, cwd=cwd)
    assert report["deed_log_capped"] is True


def test_cli_doctor_json(project, db, monkeypatch):
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    cfg = Config()
    cfg.db_path = db
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))
    _session(db, "s1", str(cwd.resolve()))
    _obs(db, "s1", "Write", "Wrote: /proj/a.py")
    _write_memory(config_dir, cwd, "uses a.py and z.py")
    monkeypatch.chdir(cwd)

    res = CliRunner().invoke(main, ["memory", "doctor", "--json"])
    assert res.exit_code == 0
    import json as _json

    rpt = _json.loads(res.output)
    assert rpt["corroborated"] == ["a.py"] and rpt["unbacked"] == ["z.py"]


def test_reconcile_scopes_to_project(project, db, monkeypatch):
    """Deeds from a different project must not pollute this project's reconciliation."""
    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    _session(db, "mine", str(cwd.resolve()))
    _session(db, "theirs", "/some/other/project")
    _obs(db, "theirs", "Write", "Wrote: /some/other/project/foreign.py")
    _write_memory(config_dir, cwd, "# empty-ish memory\n")
    report = reconcile(db, cwd=cwd)
    assert "foreign.py" not in report["omissions"]
    assert report["deed_count"] == 0  # only this project's deeds count


# ---- techne#175: auto-memory dir resolution -------------------------------


def test_auto_memory_dir_default_is_projects_slug_memory(project, monkeypatch):
    """Lock the base dir: default = <config>/projects/<git-root-slug>/memory. This is the
    regression guard — if a future CC version moves curated memory elsewhere, this fails
    loudly instead of techne silently reconciling an empty/absent dir (techne#175)."""
    from techne.platforms.claude_code.paths import auto_memory_dir, memory_project_slug

    cwd, config_dir = project
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    expected = config_dir / "projects" / memory_project_slug(cwd) / "memory"
    assert auto_memory_dir(cwd) == expected
    # The tmp cwd is not inside a git repo, so the memory slug == the cwd (kS) slug.
    assert memory_project_slug(cwd) == project_slug(cwd)


def test_auto_memory_dir_honors_autoMemoryDirectory_override(project, monkeypatch):
    """techne#175: a user-set `autoMemoryDirectory` in settings.json relocates the dir;
    techne must read from there, not the default projects/<slug>/memory."""
    import json

    from techne.platforms.claude_code.paths import auto_memory_dir

    cwd, config_dir = project
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    custom = cwd / "custom-mem"
    (config_dir / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(custom)}))

    assert auto_memory_dir(cwd) == custom


def test_auto_memory_dir_reads_override_from_git_root_not_subdir(tmp_path, monkeypatch):
    """techne#175 (Prudence): a PROJECT-scoped `autoMemoryDirectory` lives at the git
    ROOT's .claude/, and CC keys memory on that root — so a session run from a subdir must
    still pick it up, not fall through to the default dir."""
    import json

    from techne.platforms.claude_code.paths import auto_memory_dir

    config_dir = tmp_path / "ccconfig"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".claude").mkdir()
    custom = tmp_path / "elsewhere-mem"
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(custom)})
    )

    subdir = repo / "pkg" / "deep"
    subdir.mkdir(parents=True)
    assert auto_memory_dir(subdir) == custom  # found via the repo-root .claude, not subdir


def test_memory_project_slug_uses_git_repo_root(tmp_path):
    """techne#175: CC keys auto-memory on the GIT REPO ROOT (docs: '<project> is derived
    from the git repository … subdirectories within the same repo share one'). A session
    run from a subdir must resolve the repo-root slug, not the subdir's."""
    from techne.platforms.claude_code.paths import memory_project_slug

    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)  # marks the repo root
    subdir = repo / "pkg" / "sub"
    subdir.mkdir(parents=True)

    assert memory_project_slug(subdir) == project_slug(repo)
    assert memory_project_slug(subdir) != project_slug(subdir)  # NOT the raw cwd slug


def test_memory_project_slug_handles_git_file_worktree(tmp_path):
    """techne#175: in a git worktree/submodule `.git` is a `gitdir:` POINTER FILE, not a
    directory. The root walk uses `.exists()`, which matches both — pin the file case."""
    from techne.platforms.claude_code.paths import memory_project_slug

    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")  # .git as a FILE
    subdir = repo / "src"
    subdir.mkdir()

    assert memory_project_slug(subdir) == project_slug(repo)
