"""techne#676 — a store ahead of its code must never be silent.

`guarded_migrate` folded `current > target` into `current >= target` and
returned without a word. For `==` that is the correct no-op. For `>` it is an
inconsistency: the store claims a shape the code has never defined, every
migration is skipped, and nothing says so. architect.db sat at user_version 4
against ARCHITECT_VERSION = 3 for at least six days before anyone noticed, and
only then because a migration written against it did nothing.
"""

from __future__ import annotations

import sqlite3

import pytest

from techne.core import migrator


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "thing.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    return p, conn


def _migrate(db, target, ran: list):
    path, conn = db
    migrator.guarded_migrate(
        conn,
        path,
        target_version=target,
        read_version=migrator.user_version_io.read,
        run_steps=lambda c, cur, tgt: ran.append((cur, tgt)),
        label="test schema",
        backup=False,
    )


def test_store_ahead_of_code_warns_loudly(db, capsys):
    """The defect, verbatim: stamp above the code's version, migrations skipped."""
    path, conn = db
    migrator.user_version_io.write(conn, 4)
    conn.commit()

    ran: list = []
    _migrate(db, 3, ran)

    assert ran == [], "no step can run above the code's version"
    err = capsys.readouterr().err
    assert "AHEAD" in err
    assert "v4" in err and "v3" in err
    assert "SKIPPED" in err


def test_store_at_code_version_stays_silent(db, capsys):
    """The `==` case is a genuine no-op and must not become noise — that is how a
    real warning gets trained into background."""
    path, conn = db
    migrator.user_version_io.write(conn, 3)
    conn.commit()

    ran: list = []
    _migrate(db, 3, ran)

    assert ran == []
    assert capsys.readouterr().err == ""


def test_store_behind_code_migrates_without_warning(db, capsys):
    """Behind is the ladder working, not a fault."""
    path, conn = db
    migrator.user_version_io.write(conn, 1)
    conn.commit()

    ran: list = []
    _migrate(db, 3, ran)

    assert ran == [(1, 3)]
    assert "AHEAD" not in capsys.readouterr().err


def test_ahead_does_not_raise(db):
    """Deliberate: this runs on the capture hot path via connect(). Raising would
    take every session down over a condition that is inconsistent but not
    destructive. The loud half is stderr; the readable half is `techne doctor`."""
    path, conn = db
    migrator.user_version_io.write(conn, 9)
    conn.commit()
    _migrate(db, 3, [])  # must not raise


# --- the doctor check -------------------------------------------------------


def test_check_does_not_false_positive_on_table_versioned_stores(tmp_path, monkeypatch):
    """THE REASON THE REGISTRY EXISTS. techne.db and archivist.db do not use
    PRAGMA user_version at all — reading it returns 0 because the field is
    UNUSED. A check comparing the pragma across every db would report three
    false problems on a healthy machine and be ignored within a week."""
    from techne.config import Config
    from techne.core.checks import Verdict, check_schema_versions
    from techne.core.database import SCHEMA_VERSION, init_db

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path
    init_db(cfg.db_path)

    core = [r for r in check_schema_versions(cfg) if r.name == "schema:core"]
    assert core and core[0].verdict == Verdict.LIVE
    assert f"v{SCHEMA_VERSION}" in core[0].evidence
    # And the pragma really is 0 — i.e. the check is right for the right reason.
    conn = sqlite3.connect(cfg.db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()


def test_check_flags_a_pragma_store_that_is_ahead(tmp_path, monkeypatch):
    from techne.config import Config
    from techne.core.architect import ARCHITECT_VERSION
    from techne.core.checks import Verdict, check_schema_versions

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path

    conn = sqlite3.connect(tmp_path / "architect.db")
    conn.execute(f"PRAGMA user_version = {ARCHITECT_VERSION + 1}")
    conn.commit()
    conn.close()

    arch = [r for r in check_schema_versions(cfg) if r.name == "schema:architect"]
    assert arch and arch[0].verdict == Verdict.BROKEN
    assert "AHEAD" in arch[0].detail
    assert arch[0].remedy


def test_check_treats_behind_as_healthy(tmp_path, monkeypatch):
    """A store below its code is the ladder waiting to run. Reporting it as a
    fault would make the check cry wolf on every pending migration."""
    from techne.config import Config
    from techne.core.architect import ARCHITECT_VERSION
    from techne.core.checks import Verdict, check_schema_versions

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path

    conn = sqlite3.connect(tmp_path / "architect.db")
    conn.execute(f"PRAGMA user_version = {max(0, ARCHITECT_VERSION - 1)}")
    conn.commit()
    conn.close()

    arch = [r for r in check_schema_versions(cfg) if r.name == "schema:architect"]
    assert arch and arch[0].verdict == Verdict.LIVE


def test_registry_constants_all_resolve():
    """A renamed constant must fail here, not silently read as 0 and turn every
    store into a false 'ahead'."""
    from techne.core.checks import _VERSION_REGISTRY, _code_version

    for label, _pattern, mechanism, ref in _VERSION_REGISTRY:
        if mechanism == "none":
            assert ref is None
            continue
        assert _code_version(ref) is not None, f"{label}: {ref} does not resolve"
