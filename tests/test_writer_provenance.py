"""Writer provenance on federated knowledge (techne#553)."""

from __future__ import annotations

import sqlite3

import pytest

from techne.config import Config
from techne.core.archivist import (
    push_knowledge_to_archivist,
    register_base_archivist,
)
from techne.core.database import init_db
from techne.core.knowledge import store
from techne.sync.outbox import flush_sinks


@pytest.fixture
def pair(tmp_path):
    """A local techne plus its base archivist, non-standalone."""
    cfg = Config.load()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path
    cfg.standalone = False
    cfg.archivist_db_path = str(tmp_path / "archivist.db")
    cfg.instance_name = "poe@dev04"
    cfg.connections = {}
    init_db(cfg.db_path)
    return cfg


def _push(cfg):
    register_base_archivist(cfg)
    push_knowledge_to_archivist(cfg)
    flush_sinks(cfg.db_path)
    return sqlite3.connect(cfg.archivist_db_path)


def test_the_writing_instance_is_recorded(pair):
    """The point of the issue: 1,094 rows all said `team` and nothing else, so the
    store could not name its second contributor — which blocks conflict resolution,
    trust weighting, "who should I ask", and retracting one contributor's bad week."""
    store(pair.db_path, "the deploy runbook lives in ops/deploy.md", type="fact")
    conn = _push(pair)
    try:
        row = conn.execute("SELECT source, written_by FROM knowledge").fetchone()
    finally:
        conn.close()
    assert row == ("team", "poe@dev04")


def test_local_provenance_survives_the_federated_write(pair):
    """`source` on the local row is 'agent' | 'manual' | 'reflect-promote:<sid>:<id>'
    and the handler OVERWROTE it with 'team', destroying the only record of where the
    fact came from. It is now carried across rather than discarded."""
    store(pair.db_path, "distilled by a session", type="fact", source="reflect-promote:abc123:7")
    store(pair.db_path, "typed by a human", type="fact", source="manual")
    conn = _push(pair)
    try:
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute("SELECT content, origin_source, origin_session FROM knowledge")
        }
    finally:
        conn.close()
    assert rows["distilled by a session"] == ("reflect-promote:abc123:7", "abc123")
    # `manual` encodes no session, and inventing one would be the same class of lie
    # as backfilling written_by.
    assert rows["typed by a human"] == ("manual", None)


def test_source_team_is_not_replaced(pair):
    """`source` answers "which lane", the new columns answer "which writer". The read
    edge and 1,094 existing rows depend on the first, so #553 ADDS rather than swaps."""
    store(pair.db_path, "a fact", type="fact")
    conn = _push(pair)
    try:
        assert conn.execute("SELECT source FROM knowledge").fetchone()[0] == "team"
    finally:
        conn.close()


def test_an_unnamed_instance_writes_null_not_a_guess(pair):
    """`instance_name` empty must mean NULL, never a hostname or a default label. The
    whole issue is that an unattributable row should stay honestly unattributed — a
    wrong name in the table whose job is to say who said what is worse than no name."""
    pair.instance_name = ""
    store(pair.db_path, "a fact", type="fact")
    conn = _push(pair)
    try:
        assert conn.execute("SELECT written_by FROM knowledge").fetchone()[0] is None
    finally:
        conn.close()


def test_existing_rows_are_not_backfilled(tmp_path):
    """The migration must leave pre-existing rows unattributed. Verified on a copy of
    the real store: 1,094 rows, 0 attributed after migrating v24 -> v25."""
    db = tmp_path / "old.db"
    init_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO knowledge (type, content, source) VALUES ('fact', 'legacy', 'team')")
    # Rewind to v24. A fresh db stamps only the CURRENT version, so deleting >=25
    # empties the table entirely and `MAX(version)` returns None — an impossible state
    # that fails in the migrator rather than in the assertion.
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (24)")
    conn.execute("UPDATE knowledge SET written_by = NULL")
    conn.commit()
    conn.close()

    init_db(db)  # re-migrate

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 1
        assert conn.execute("SELECT written_by FROM knowledge").fetchone()[0] is None
    finally:
        conn.close()


def test_the_columns_land_on_a_plain_techne_db_too(tmp_path):
    """One schema everywhere (techne#220): archivist.db IS a techne-schema db, so these
    columns exist on every store. NULL on a personal one, which is correct — nothing
    federated wrote it."""
    db = tmp_path / "personal.db"
    init_db(db)
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge)")}
        assert {"written_by", "origin_source", "origin_session"} <= cols
    finally:
        conn.close()
