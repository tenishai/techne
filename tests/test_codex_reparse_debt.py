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


def test_a_language_limited_run_does_not_discharge_the_debt(tmp_path):
    """Prudence, #872 r2 — the sixth mutation, and #864 through its own fix.

    `codex index --language typescript` on a mixed repo never visits the Python
    files. Nothing raises, so `failed` is 0. Under a "nothing failed" predicate
    the one-shot debt is deleted, the Python files keep their pre-migration rows
    forever, and the NEXT full-language run reports `forced_by None` — the
    original bug reached through the mechanism that fixed it.
    """
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "helper.py").write_text("def helper():\n    return 1\n")
    # An IMPORT is what produces a to_module row — a file with none would make
    # the post-run assertion vacuous no matter which predicate shipped.
    (root / "pkg" / "a.py").write_text(
        "from pkg import helper\n\n\ndef alpha():\n    return helper.helper()\n"
    )
    (root / "web").mkdir()
    (root / "web" / "b.ts").write_text("export function beta() { return 1; }\n")

    db = _new_db(tmp_path / "codex.db")
    from techne.codex.source import RepoSource

    RepoSource(db, root, languages=["python", "typescript"]).initial_walk()

    conn = sqlite3.connect(db)
    conn.execute("UPDATE edges SET to_module = NULL")
    conn.execute(
        "INSERT INTO codex_meta (key, value) VALUES ('reparse_owed_since', '4') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    conn.commit()
    conn.close()

    ts_only = RepoSource(db, root, languages=["typescript"])
    ts_only.initial_walk()
    assert ts_only.last_failed == 0, "precondition: the ts-only run raises nothing"

    conn = sqlite3.connect(db)
    owed = conn.execute(
        "SELECT value FROM codex_meta WHERE key = 'reparse_owed_since'"
    ).fetchone()
    conn.close()
    assert owed is not None, (
        "a --language-limited run discharged the debt — pkg/a.py was never visited, "
        "so it keeps its pre-migration rows and no later run will ever re-parse it"
    )

    full = RepoSource(db, root, languages=["python", "typescript"])
    full.initial_walk()
    assert full.last_forced_by == "4", (
        "the next full run must still be forced; if it reports None the debt was "
        "spent by a run that could not pay it"
    )
    conn = sqlite3.connect(db)
    after = conn.execute("SELECT COUNT(*) FROM edges WHERE to_module IS NOT NULL").fetchone()[0]
    cleared = conn.execute(
        "SELECT COUNT(*) FROM codex_meta WHERE key = 'reparse_owed_since'"
    ).fetchone()[0]
    conn.close()
    assert after, "the full run re-parsed nothing"
    assert cleared == 0, "the full run paid the debt and it must now be discharged"


def test_a_file_that_yields_no_symbols_does_not_discharge_the_debt(tmp_path, monkeypatch):
    """The other half of the same finding: `_index_file` returns 0 WITHOUT raising
    when the grammar is absent. The delete rolls back, the file keeps its
    pre-migration rows, and `failed` stays 0 — so "nothing raised" cannot be the
    discharge condition."""
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

    # Grammar absent: the real path returns ([], []) before any insert.
    monkeypatch.setattr(src_mod, "_parse_file", lambda *a, **k: ([], []))
    src, total = _index(db, root)
    assert total == 0 and src.last_failed == 0, (
        "precondition: an empty parse yields 0 symbols and raises nothing"
    )

    conn = sqlite3.connect(db)
    owed = conn.execute(
        "SELECT value FROM codex_meta WHERE key = 'reparse_owed_since'"
    ).fetchone()
    conn.close()
    assert owed is not None, (
        "a run that re-parsed nothing discharged the debt — every file kept its "
        "pre-migration rows and the column stays empty forever"
    )


def test_a_deleted_source_file_does_not_pin_the_debt_open(tmp_path):
    """The guard on the guard. Stale rows for a file no longer on disk can never
    be re-parsed, so requiring them would make the debt permanent on any repo
    that has ever deleted a source file — trading a silent failure for a
    permanently red one, which #870 is the standing argument against."""
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

    (root / "pkg" / "b.py").unlink()  # rows survive; the file does not

    src, _ = _index(db, root)
    assert src.last_failed == 0

    conn = sqlite3.connect(db)
    cleared = conn.execute(
        "SELECT COUNT(*) FROM codex_meta WHERE key = 'reparse_owed_since'"
    ).fetchone()[0]
    conn.close()
    assert cleared == 0, (
        "stale rows for a deleted file kept the debt open — it can never be paid, "
        "so every future index re-parses the whole repo and says it was forced"
    )


def _owe_fixture(tmp_path):
    """A repo with a debt set and one file's rows deliberately left un-reparsed."""
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
    return root, db


def test_a_broken_symlink_does_not_discharge_the_debt(tmp_path):
    """Prudence, #872 r3 — ABSENT AND UNREADABLE ARE NOT THE SAME THING.

    `Path.is_file()` is False for a file that is genuinely gone, for one inside
    an unreadable directory, and for a broken symlink — and only the FIRST can
    never be re-parsed. The other two still hold pre-migration rows, so treating
    them as "gone" discharged the debt with the data still stale: techne#864
    restored through the exclusion written to prevent it.

    The symlink route needs no permissions at all — a `git checkout` that leaves
    a dangling link is enough, which is why this is the case worth pinning.
    """
    import os

    root, db = _owe_fixture(tmp_path)
    target = root / "pkg" / "b.py"
    target.unlink()
    os.symlink(root / "pkg" / "vanished.py", target)  # dangling
    assert not target.is_file() and os.path.lexists(target), "precondition: broken symlink"

    src, _ = _index(db, root)
    assert src.last_failed == 0, "precondition: a dangling link raises nothing"

    conn = sqlite3.connect(db)
    owed = conn.execute(
        "SELECT value FROM codex_meta WHERE key = 'reparse_owed_since'"
    ).fetchone()
    conn.close()
    assert owed is not None, (
        "a broken symlink discharged the debt — pkg/b.py's rows are still "
        "pre-migration and nothing will ever re-parse them"
    )


def test_an_unreadable_directory_does_not_discharge_the_debt(tmp_path):
    """The same collapse by the other route. `_walk_repo` takes no `onerror`, so
    an unreadable directory drops out of the walk with no exception (techne#599)
    — its files never enter `reparsed`, `failed` stays 0, and stat then files
    them under "gone"."""
    import os

    root, db = _owe_fixture(tmp_path)
    locked = root / "pkg"
    os.chmod(locked, 0o000)
    try:
        src, _ = _index(db, root)
        assert src.last_failed == 0
        conn = sqlite3.connect(db)
        owed = conn.execute(
            "SELECT value FROM codex_meta WHERE key = 'reparse_owed_since'"
        ).fetchone()
        conn.close()
    finally:
        os.chmod(locked, 0o755)
    assert owed is not None, (
        "an unreadable directory discharged the debt — its files kept their "
        "pre-migration rows and were counted as absent"
    )


def test_the_owed_predicate_excludes_what_this_walk_can_never_reach(tmp_path):
    """Prudence, #872 r3 — normalisation and its scope, tested at the predicate.

    Driven directly rather than through `initial_walk`, because the end-to-end
    route CANNOT express these rows: a symbol id embeds its file path, so
    rewriting only `symbols.file` leaves ids that `INSERT OR REPLACE` restores on
    the next walk. An earlier version of this test did exactly that and passed
    against a deliberately broken predicate — it was measuring the re-insert, not
    the rule.

    She flagged the in-repo absolute case and said plainly she could not
    demonstrate a live write path for it. Measured: `sweep_excluded_rows`
    relativises in-repo absolute rows during the walk, so that shape does not
    survive one. The reachable case is a row OUTSIDE the root, which this walk
    can never reach and must therefore not demand — demanding it pins the debt,
    and a pinned debt forces a full re-parse of every repo at every SessionStart.
    """
    from techne.codex.source import RepoSource

    root = _repo(tmp_path)
    db = _new_db(tmp_path / "codex.db")
    _index(db, root)
    src = RepoSource(db, root, languages=["python"])

    outside = tmp_path / "elsewhere.py"
    outside.write_text("def gamma():\n    return 1\n")
    inside_abs = str(root / "pkg" / "a.py")

    conn = sqlite3.connect(db)
    for f in (str(outside), inside_abs):
        conn.execute(
            "INSERT INTO symbols (id, file, name, kind, language) VALUES (?,?,?,?,?)",
            (f"{f}::<module>::module::0", f, "<module>", "module", "python"),
        )
    conn.commit()
    conn.close()

    owed = src._files_still_owed({"pkg/a.py", "pkg/b.py"})

    assert str(outside) not in owed, (
        "a row outside the repo root is owed — this walk can never reach it, so "
        "the debt would never discharge and every index would re-parse everything"
    )
    assert inside_abs not in owed, (
        "an in-repo ABSOLUTE row is owed despite its relative twin being re-parsed "
        "— absolute and relative spellings of one file must not count as two"
    )
