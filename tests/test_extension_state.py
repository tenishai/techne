"""Tests for techne.core.extension_state."""

import time

import pytest

from techne.core.database import connect
from techne.core.extension_state import get_state, set_state


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.db"
    c = connect(db)
    yield c
    c.close()


def test_round_trip(conn):
    payload = {"cursor": 42, "flags": ["a", "b"]}
    set_state(conn, "my_ext", payload)
    result = get_state(conn, "my_ext")
    assert result == payload


def test_overwrite_updates_payload_and_timestamp(conn):
    set_state(conn, "ext", {"v": 1})
    t_before = int(time.time())
    set_state(conn, "ext", {"v": 2})
    result = get_state(conn, "ext")
    assert result == {"v": 2}
    row = conn.execute("SELECT updated_at FROM extension_state WHERE name = ?", ("ext",)).fetchone()
    assert row["updated_at"] >= t_before


def test_missing_name_returns_none(conn):
    assert get_state(conn, "nonexistent") is None


def test_schema_reconciler_creates_table_on_fresh_db(tmp_path):
    """Fresh DB via connect() picks up extension_state without a manual migration."""
    db = tmp_path / "fresh.db"
    c = connect(db)
    try:
        # Table must exist — a real write + read must succeed.
        set_state(c, "probe", {"ok": True})
        assert get_state(c, "probe") == {"ok": True}
    finally:
        c.close()


def test_multiple_keys_isolated(conn):
    set_state(conn, "a", {"x": 1})
    set_state(conn, "b", {"x": 99})
    assert get_state(conn, "a") == {"x": 1}
    assert get_state(conn, "b") == {"x": 99}
