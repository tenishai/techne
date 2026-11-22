"""A pattern keeps the code it was verified against, and the commit it was read at.

Until 2026-11-22 `import_md` kept the first URL on a Source line and called the rest
"decorative": the `file:line` citation the investigating guide asks for was parsed and
thrown away, and `patterns.source_path` was NULL in every row of a 1,024-pattern store.
A line or symbol only means something at a known commit, so the commit is stored too
(`patterns.source_commit`, architect schema v9).
"""

from __future__ import annotations

import sqlite3

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.core import architect
from techne.core.architect import ARCHITECT_SCHEMA, ARCHITECT_VERSION, architect_db
from techne.core.database import init_db
from techne.core.patterns import export_patterns_md, ingest_patterns_md
from techne.core.stores import SqliteStore, plain_connect
from techne.import_md import commit_from, parse, split_source

SHA = "9c4652688590abb836b40ebd1e4725ecf9a7c7d1"


def _section(source: str, commit: str | None = None, body: str = "b") -> str:
    lines = ["## P", "", f"- **Source**: {source}", "- **Surveyed**: 2026-11-22"]
    if commit is not None:
        lines.append(f"- **Commit**: {commit}")
    return "\n".join(lines + ["", body, ""])


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "architect.db"
    SqliteStore("architect", path).migrate(ARCHITECT_SCHEMA)
    architect._reconcile_relations(path)
    return path


def _row(db, cols="source_path, source_commit"):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(f"SELECT {cols} FROM patterns").fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- parser


@pytest.mark.parametrize(
    "source,url,citation",
    [
        ("https://github.com/x/y — src/a.ts::run:12", "https://github.com/x/y", "src/a.ts::run:12"),
        ("https://github.com/x/y - `src/a.py::f`", "https://github.com/x/y", "`src/a.py::f`"),
        ("https://github.com/x/y — §3.2", "https://github.com/x/y", "§3.2"),
        (
            "(https://github.com/x/y), see spec.md::Tasks",
            "https://github.com/x/y",
            "see spec.md::Tasks",
        ),
        ("https://github.com/x/y", "https://github.com/x/y", None),
        ("https://github.com/x/y).", "https://github.com/x/y", None),
        ("no url here", None, None),
    ],
)
def test_split_source_keeps_the_citation_after_the_url(source, url, citation):
    got = split_source(source)
    if url is None:
        assert got is None
    else:
        assert got == (url, citation)


def test_a_commit_line_is_parsed_and_lowercased():
    (p,) = parse(_section("https://github.com/x/y — src/a.ts::run", commit=SHA.upper())).patterns
    assert p.source_path == "src/a.ts::run"
    assert p.source_commit == SHA


def test_a_pinned_url_supplies_the_commit_when_no_commit_line_does():
    url = f"https://github.com/x/y/blob/{SHA[:12]}/src/a.ts#L10-L20"
    (p,) = parse(_section(url)).patterns
    assert p.source_commit == SHA[:12]


@pytest.mark.parametrize("value", ["main", "abc", "commit 9c46526", "zzzzzzz", ""])
def test_a_commit_value_that_is_not_a_sha_is_not_stored(value):
    """Prose in the column would read as a commit later; store nothing instead."""
    assert commit_from(value) is None


def test_the_commit_line_does_not_leak_into_the_body():
    (p,) = parse(_section("https://github.com/x/y — f.py::g", commit=SHA, body="the body")).patterns
    assert p.body == "the body"


# --------------------------------------------------------------------------- store


def test_ingest_stores_the_citation_and_the_commit(db):
    ingest_patterns_md(db, _section("https://github.com/x/y — src/a.ts::run:12", commit=SHA))
    assert _row(db) == [("src/a.ts::run:12", SHA)]


def test_re_ingest_fills_a_missing_citation_without_changing_the_id(db):
    """Old rows lost their citations at ingest; re-ingesting the original file restores
    them. The id is a hash of (name, url, body), so the citation must not be part of it."""
    ingest_patterns_md(db, _section("https://github.com/x/y"))
    (before,) = _row(db, "id")
    ingest_patterns_md(db, _section("https://github.com/x/y — src/a.ts::run", commit=SHA))
    assert _row(db, "id") == [before]
    assert _row(db) == [("src/a.ts::run", SHA)]


def test_re_ingest_never_overwrites_a_recorded_citation(db):
    ingest_patterns_md(db, _section("https://github.com/x/y — src/a.ts::run", commit=SHA))
    ingest_patterns_md(db, _section("https://github.com/x/y — src/other.ts::x", commit="1234567"))
    assert _row(db) == [("src/a.ts::run", SHA)]


def test_export_round_trips_the_citation_and_the_commit(db, tmp_path):
    ingest_patterns_md(db, _section("https://github.com/x/y — src/a.ts::run:12", commit=SHA))
    exported = export_patterns_md(db)
    assert f"- **Commit**: {SHA}" in exported
    db2 = tmp_path / "architect2.db"
    SqliteStore("architect", db2).migrate(ARCHITECT_SCHEMA)
    architect._reconcile_relations(db2)
    ingest_patterns_md(db2, exported)
    assert _row(db2) == [("src/a.ts::run:12", SHA)]


def test_a_v8_store_gains_source_commit_and_keeps_its_rows(tmp_path):
    path = tmp_path / "architect.db"
    conn = plain_connect(path)
    conn.executescript(
        """CREATE TABLE patterns (id TEXT PRIMARY KEY, name TEXT, source_url TEXT,
                                  source_path TEXT, body TEXT, status TEXT DEFAULT 'active');
           INSERT INTO patterns (id, name, source_url, body) VALUES ('p1', 'n', 'u', 'b');
           PRAGMA user_version = 8;"""
    )
    conn.commit()
    conn.close()
    architect._reconcile_relations(path)
    architect._reconcile_relations(path)  # idempotent
    conn = plain_connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ARCHITECT_VERSION == 9
        row = conn.execute("SELECT id, source_commit FROM patterns").fetchone()
        assert tuple(row) == ("p1", None)
    finally:
        conn.close()


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def cli_config(tmp_path, monkeypatch):
    from techne.config import Config

    techne_db = tmp_path / "techne.db"
    init_db(techne_db).close()
    cfg = Config()
    cfg.db_path = techne_db
    cfg.data_dir = tmp_path
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    return cfg


def _add(*args):
    return CliRunner().invoke(main, ["architect", "add", *args])


def test_add_takes_a_commit_and_show_prints_the_citation(cli_config):
    res = _add(
        "--name",
        "P",
        "--source",
        "https://github.com/x/y — src/a.ts::run",
        "--commit",
        SHA,
        "--body",
        "b",
    )
    assert res.exit_code == 0, res.output
    assert _row(architect_db(cli_config)) == [("src/a.ts::run", SHA)]
    (pid,) = _row(architect_db(cli_config), "id")[0]
    shown = CliRunner().invoke(main, ["architect", "show", pid])
    assert f"cites: src/a.ts::run @ {SHA}" in shown.output


def test_add_refuses_a_commit_that_is_not_a_sha(cli_config):
    res = _add(
        "--name", "P", "--source", "https://github.com/x/y", "--commit", "main", "--body", "b"
    )
    assert res.exit_code != 0
    assert "--commit" in res.output
    assert _row(architect_db(cli_config)) == []


def test_show_says_when_no_citation_was_recorded(cli_config):
    assert _add("--name", "P", "--source", "https://github.com/x/y", "--body", "b").exit_code == 0
    (pid,) = _row(architect_db(cli_config), "id")[0]
    assert "cites: (none recorded)" in CliRunner().invoke(main, ["architect", "show", pid]).output
