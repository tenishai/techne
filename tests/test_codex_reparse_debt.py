"""techne#864 — a migration that adds a parse-derived column must force a reparse.

`codex index` skips files whose SHA-256 is unchanged. Adding a column changes no
file's bytes, so on a repo nobody edited the re-index skips everything, the new
column stays NULL forever, and the run prints a success line and exits 0.

Measured on the live v3->v4 rollout: four of nine repos got zero `to_module`
values, and `index-all` reported `reindexed …: 0 symbols; resolved 0 calls` for
each — which reads as "already current".

These drive the real indexer over a real repo rather than asserting on the
migration in isolation, because the defect is precisely the SEAM between the two:
the migration was correct, the indexer was correct, and the pair was not.
"""

import sqlite3

import pytest

from techne.core.codex import CODEX_SCHEMA, CODEX_VERSION


def _new_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(CODEX_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("import os\n\n\ndef alpha():\n    return os.getcwd()\n")
    (root / "pkg" / "b.py").write_text("from pkg import a\n\n\ndef beta():\n    return a.alpha()\n")
    return root


def _index(db, root, **kw):
    from techne.codex.source import RepoSource

    src = RepoSource(db, root, languages=["python"])
    n = src.initial_walk(**kw)
    return src, n


@pytest.mark.parametrize("via", ["debt", "flag"])
def test_a_parse_derived_column_gets_populated_after_a_migration(tmp_path, via):
    """The regression, end to end, by BOTH routes that must close it."""
    root = _repo(tmp_path)
    db = _new_db(tmp_path / "codex.db")
    _index(db, root)

    conn = sqlite3.connect(db)
    populated = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE to_module IS NOT NULL"
    ).fetchone()[0]
    assert populated, "precondition: a fresh index populates to_module"

    # Simulate the pre-v4 state the migration inherits: the column exists but
    # every row is NULL, exactly as ALTER TABLE ADD COLUMN leaves it.
    conn.execute("UPDATE edges SET to_module = NULL")
    conn.commit()
    conn.close()

    if via == "debt":
        # What the migration now records.
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO codex_meta (key, value) VALUES ('reparse_owed_since', '4') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        conn.commit()
        conn.close()
        src, _ = _index(db, root)
        assert src.last_forced_by == "4", (
            "the run must record WHICH schema version forced it, or the CLI cannot "
            "explain why an untouched repo re-parsed"
        )
    else:
        src, _ = _index(db, root, force=True)

    conn = sqlite3.connect(db)
    after = conn.execute("SELECT COUNT(*) FROM edges WHERE to_module IS NOT NULL").fetchone()[0]
    owed = conn.execute(
        "SELECT COUNT(*) FROM codex_meta WHERE key = 'reparse_owed_since'"
    ).fetchone()[0]
    conn.close()

    assert after == populated, (
        f"to_module populated on {after} edges, expected {populated} — the re-index "
        "skipped unchanged files, so the migrated column is still empty (techne#864)"
    )
    assert owed == 0, "the debt must be discharged by the run that paid it"


def test_without_the_debt_an_unchanged_repo_is_still_skipped(tmp_path):
    """The guard on the fix. Forcing ALWAYS would make every index a full reparse
    and silently delete the incremental behaviour this indexer is built on — a
    fix that costs more than the bug. Nothing owed, nothing re-read."""
    root = _repo(tmp_path)
    db = _new_db(tmp_path / "codex.db")
    _index(db, root)

    src, n = _index(db, root)
    assert n == 0, f"unchanged repo re-parsed {n} symbols — the incremental skip is gone"
    assert src.last_forced_by is None


def test_a_schema_present_but_empty_store_owes_nothing(tmp_path):
    """A store with TABLES but no ROWS has nothing to re-parse.

    The interesting case is not a bare file — `_apply_codex_migration` stamps
    that and never runs the v4 step at all, so it exercises no guard. It is a db
    that HAS the schema and is empty, which is what an initialised-but-unindexed
    repo looks like. Recording a debt there forces a full walk on the first
    index — the walk it was already going to do — and then clears a flag that
    never meant anything, so the flag stops meaning "rows predate this column".
    """
    from techne.core.codex import _apply_codex_migration

    db = _new_db(tmp_path / "empty.db")
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 0
    conn.execute(f"PRAGMA user_version = {CODEX_VERSION - 1}")
    conn.commit()
    conn.close()

    _apply_codex_migration(db)

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT COUNT(*) FROM codex_meta WHERE key = 'reparse_owed_since'"
    ).fetchone()[0]
    conn.close()
    assert rows == 0, (
        "an empty store recorded a reparse debt it cannot owe — every future first "
        "index will report itself as a migration reparse"
    )


def test_the_debt_survives_a_run_that_failed_files(tmp_path, monkeypatch):
    """A debt cleared by a half-finished run is the original bug with an extra step.

    Files that fail roll back and KEEP their previous rows — the exact rows the
    reparse exists to replace — so clearing the flag would strand them NULL with
    nothing left to say they are owed.
    """
    from techne.codex import source as src_mod

    root = _repo(tmp_path)
    db = _new_db(tmp_path / "codex.db")
    _index(db, root)

    conn = sqlite3.connect(db)
    conn.execute("UPDATE edges SET to_module = NULL")
    conn.execute(
        "INSERT INTO codex_meta (key, value) VALUES ('reparse_owed_since', '4') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    conn.commit()
    conn.close()

    def _always_fails(*a, **kw):
        raise src_mod.IndexFailure("simulated")

    monkeypatch.setattr(src_mod, "_index_file", _always_fails)
    src, _ = _index(db, root)
    assert src.last_failed, "precondition: the run must have failed files"

    conn = sqlite3.connect(db)
    still = conn.execute(
        "SELECT value FROM codex_meta WHERE key = 'reparse_owed_since'"
    ).fetchone()
    conn.close()
    assert still is not None and still[0] == "4", (
        "the reparse debt was cleared by a run that indexed nothing successfully — "
        "those files keep their pre-migration rows with no record that they are owed"
    )


def test_the_migration_itself_records_the_debt(tmp_path):
    """Pins the seam at its source: v3 -> v4 on a db WITH rows must leave the flag."""
    from techne.core.codex import _apply_codex_migration

    root = _repo(tmp_path)
    db = _new_db(tmp_path / "codex.db")
    _index(db, root)

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM codex_meta WHERE key = 'reparse_owed_since'")
    conn.execute(f"PRAGMA user_version = {CODEX_VERSION - 1}")
    conn.commit()
    conn.close()

    _apply_codex_migration(db)

    conn = sqlite3.connect(db)
    owed = conn.execute(
        "SELECT value FROM codex_meta WHERE key = 'reparse_owed_since'"
    ).fetchone()
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert ver == CODEX_VERSION
    assert owed is not None, (
        "migrating a populated db to v4 recorded no reparse debt — the new column "
        "will stay NULL through every future incremental index (techne#864)"
    )
