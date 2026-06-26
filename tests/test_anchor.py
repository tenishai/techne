"""Tests for evidence-anchor resolution + graded staleness (techne#90).

Each test that needs git uses a real temp repo (git init + a real commit) so the
write-time hash/sha capture and the recall-time classification are exercised
end-to-end, not mocked.
"""

import subprocess

import pytest

from techne.core import knowledge
from techne.core.anchor import annotate_freshness, classify, resolve_anchor
from techne.core.database import SCHEMA_VERSION, connect


def _run(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    """A temp git repo with one committed file. Returns (repo_dir, file_path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "git", "init", "-q")
    _run(repo, "git", "config", "user.email", "test@example.com")
    _run(repo, "git", "config", "user.name", "Test")
    f = repo / "module.py"
    f.write_text("def hello():\n    return 'world'\n")
    _run(repo, "git", "add", "module.py")
    _run(repo, "git", "commit", "-q", "-m", "initial")
    return repo, f


# --- resolve_anchor ---------------------------------------------------------


def test_resolve_anchor_basic(git_repo):
    repo, f = git_repo
    result = resolve_anchor(str(f))
    assert result is not None
    assert result["repo_rel_path"] == "module.py"
    assert len(result["content_hash"]) == 64  # hex sha256
    assert len(result["git_sha"]) == 40  # git sha1
    # git_sha matches what rev-parse reports
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert result["git_sha"] == head


def test_resolve_anchor_off_git_returns_none(tmp_path):
    """A file that exists but is not inside any git repo → None (no anchor)."""
    f = tmp_path / "loose.txt"
    f.write_text("not in a repo")
    assert resolve_anchor(str(f)) is None


def test_resolve_anchor_missing_file_returns_none(tmp_path):
    assert resolve_anchor(str(tmp_path / "nope.py")) is None


# --- classify ---------------------------------------------------------------


def test_classify_unchanged(git_repo):
    _repo, f = git_repo
    r = resolve_anchor(str(f))
    state, detail = classify(str(f), r["content_hash"], r["git_sha"])
    assert state == "unchanged"
    assert detail == ""


def test_classify_modified(git_repo):
    _repo, f = git_repo
    r = resolve_anchor(str(f))
    f.write_text("def hello():\n    return 'CHANGED'\n")
    state, _detail = classify(str(f), r["content_hash"], r["git_sha"])
    assert state == "modified"


def test_classify_deleted(git_repo):
    _repo, f = git_repo
    r = resolve_anchor(str(f))
    f.unlink()
    state, _detail = classify(str(f), r["content_hash"], r["git_sha"])
    assert state == "deleted"


def test_classify_moved(git_repo):
    """File removed from its path but identical content lives elsewhere in the
    repo → moved, pointing at the new path."""
    repo, f = git_repo
    r = resolve_anchor(str(f))
    moved_to = repo / "subdir" / "renamed.py"
    moved_to.parent.mkdir()
    moved_to.write_text(f.read_text())
    f.unlink()
    _run(repo, "git", "add", "-A")
    state, detail = classify(str(f), r["content_hash"], r["git_sha"])
    assert state == "moved"
    assert detail == "subdir/renamed.py"


def test_classify_unknown_on_ls_files_failure(git_repo, monkeypatch):
    """Contract: if the moved-scan's `git ls-files` itself fails (vs. completing
    empty), classify returns `unknown`, not a possibly-wrong `deleted`."""
    import techne.core.anchor as anchor

    _repo, f = git_repo
    r = resolve_anchor(str(f))
    f.unlink()  # file gone → take the moved-scan branch
    real_git = anchor._git

    def _failing_git(root, *args, **kwargs):
        if args and args[0] == "ls-files":
            return False, ""  # simulate a git failure / timeout
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(anchor, "_git", _failing_git)
    state, detail = anchor.classify(str(f), r["content_hash"], r["git_sha"])
    assert state == "unknown"
    assert detail


def test_classify_unknown_no_repo(tmp_path):
    """Anchor path outside any git repo → unknown, never falsely fresh/deleted."""
    f = tmp_path / "loose.txt"
    f.write_text("v1")
    # Hash captured, then file removed so we hit the gone-but-no-repo branch.
    import hashlib

    stored = hashlib.sha256(b"v1").hexdigest()
    f.unlink()
    state, detail = classify(str(f), stored, "deadbeef")
    assert state == "unknown"
    assert detail  # reason string present


def test_classify_unknown_missing_path(tmp_path):
    state, detail = classify(str(tmp_path / "does" / "not" / "exist.py"), "abc123", "sha")
    assert state == "unknown"
    assert detail


def test_classify_unknown_no_content_hash(git_repo):
    """A NULL stored content hash can never be graded → unknown."""
    _repo, f = git_repo
    state, detail = classify(str(f), None, "sha")
    assert state == "unknown"
    assert detail == "no content hash stored"


# --- write path -------------------------------------------------------------


def test_store_with_anchor_persists_hash_and_sha(tmp_path, git_repo):
    _repo, f = git_repo
    db = tmp_path / "techne.db"
    kid = knowledge.store(db, "hello() returns world", anchor=str(f))
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT anchor_path, anchor_symbol, content_hash, anchor_git_sha "
            "FROM knowledge WHERE id = ?",
            (kid,),
        ).fetchone()
    finally:
        conn.close()
    assert row["anchor_path"] == str(f)
    assert row["content_hash"] is not None
    assert row["anchor_git_sha"] is not None
    assert row["anchor_symbol"] is None


def test_store_with_anchor_symbol(tmp_path, git_repo):
    _repo, f = git_repo
    db = tmp_path / "techne.db"
    kid = knowledge.store(db, "the hello fn", anchor=f"{f}::hello")
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT anchor_path, anchor_symbol FROM knowledge WHERE id = ?", (kid,)
        ).fetchone()
    finally:
        conn.close()
    assert row["anchor_path"] == str(f)
    assert row["anchor_symbol"] == "hello"


def test_store_with_off_git_anchor_records_path_but_null_hash(tmp_path):
    """An anchor whose file isn't in a repo records the path but leaves hash/sha
    NULL → recall will report `unknown`, never a false `unchanged`."""
    loose = tmp_path / "loose.txt"
    loose.write_text("x")
    db = tmp_path / "techne.db"
    kid = knowledge.store(db, "note", anchor=str(loose))
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT anchor_path, content_hash, anchor_git_sha FROM knowledge WHERE id = ?",
            (kid,),
        ).fetchone()
    finally:
        conn.close()
    assert row["anchor_path"] == str(loose)
    assert row["content_hash"] is None
    assert row["anchor_git_sha"] is None


# --- recall annotation ------------------------------------------------------


def test_annotate_freshness_unchanged_no_tag(tmp_path, git_repo):
    _repo, f = git_repo
    db = tmp_path / "techne.db"
    kid = knowledge.store(db, "fact", anchor=str(f))
    rows = [{"type": "knowledge", "id": kid, "content": "fact"}]
    annotate_freshness(db, rows)
    assert "freshness" not in rows[0]  # unchanged → no special tag


def test_annotate_freshness_modified_tag(tmp_path, git_repo):
    _repo, f = git_repo
    db = tmp_path / "techne.db"
    kid = knowledge.store(db, "fact", anchor=str(f))
    f.write_text("changed\n")
    rows = [{"type": "knowledge", "id": kid, "content": "fact"}]
    annotate_freshness(db, rows)
    assert rows[0]["freshness"]["state"] == "modified"
    assert "re-read before quoting" in rows[0]["freshness"]["tag"]


def test_non_anchored_no_freshness_check(tmp_path):
    """CRITICAL hard constraint: a knowledge entry stored WITHOUT an anchor is
    never staleness-checked, never gets a freshness tag, and is never
    invalidated — even if some unrelated path on disk would be 'modified'."""
    db = tmp_path / "techne.db"
    kid = knowledge.store(db, "behavioral: prefers concise replies", tags="memory-type:feedback")
    # Confirm the row has no anchor.
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT anchor_path, content_hash FROM knowledge WHERE id = ?", (kid,)
        ).fetchone()
    finally:
        conn.close()
    assert row["anchor_path"] is None
    assert row["content_hash"] is None

    rows = [{"type": "knowledge", "id": kid, "content": "behavioral: prefers concise replies"}]
    annotate_freshness(db, rows)
    assert "freshness" not in rows[0]
    assert "anchor_path" not in rows[0]
    # The entry survives recall untouched.
    assert rows[0]["content"] == "behavioral: prefers concise replies"


# --- migration --------------------------------------------------------------


# A v10-shape knowledge table: the pre-#90 columns only, no anchor fields.
_SCHEMA_V10_KNOWLEDGE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    tags TEXT,
    confidence TEXT NOT NULL DEFAULT 'medium',
    confidence_numeric REAL NOT NULL DEFAULT 0.6,
    last_confirmed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    superseded_by INTEGER REFERENCES knowledge(id),
    event_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def test_migration_v10_to_v11(tmp_path):
    """v10 → v11 adds the four nullable anchor columns to knowledge. Anchored on
    the SCHEMA_VERSION constant, never a hardcoded integer."""
    import sqlite3

    from techne.core.database import _migrate

    db = tmp_path / "v10.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_V10_KNOWLEDGE)
    conn.execute("INSERT INTO schema_version (version) VALUES (10)")
    conn.commit()

    cols_before = {row[1] for row in conn.execute("PRAGMA table_info(knowledge)")}
    assert "anchor_path" not in cols_before

    _migrate(conn, 10, SCHEMA_VERSION)

    cols_after = {row[1] for row in conn.execute("PRAGMA table_info(knowledge)")}
    for col in ("anchor_path", "anchor_symbol", "content_hash", "anchor_git_sha"):
        assert col in cols_after
    max_v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert max_v == SCHEMA_VERSION
    conn.close()
