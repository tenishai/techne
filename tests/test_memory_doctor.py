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
    project_slug,
    reconcile,
)
from techne.core.state import deed_log


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
