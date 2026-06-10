"""Tests for hardened _import_contacts markdown parsing in identity.py."""

import sqlite3
import textwrap
from pathlib import Path

import pytest

from techne.core import identity
from techne.core.database import connect


def _setup_db(tmp_path: Path) -> Path:
    """Create a minimal identity table in a temp SQLite DB."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE identity (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'config',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.commit()
    conn.close()
    return db_path


def _all_keys(db_path: Path) -> dict:
    """Return all identity rows as {key: value}."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT key, value FROM identity ORDER BY key").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def test_import_contacts_well_formed(tmp_path):
    """Well-formed contacts.md produces correct identity keys."""
    db_path = _setup_db(tmp_path)
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()

    contacts = textwrap.dedent("""\
        ## Alice Smith

        - **email**: alice@example.com
        - **role**: SRE
        - **slack**: @alice
    """)
    (identity_dir / "contacts.md").write_text(contacts)

    identity.sync_from_markdown(db_path, identity_dir)

    keys = _all_keys(db_path)
    assert keys.get("identity.contacts.alice_smith.email") == "alice@example.com"
    assert keys.get("identity.contacts.alice_smith.role") == "SRE"
    assert keys.get("identity.contacts.alice_smith.slack") == "@alice"
    # Top-level blob also stored
    assert "identity.contacts" in keys


def test_import_contacts_whitespace_tolerance(tmp_path):
    """Parser handles varied whitespace in bullet lines."""
    db_path = _setup_db(tmp_path)
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()

    contacts = textwrap.dedent("""\
        ## Bob Jones

        **email**:bob@example.com
        **role**:  DevOps
        - **slack**:@bob
    """)
    (identity_dir / "contacts.md").write_text(contacts)

    identity.sync_from_markdown(db_path, identity_dir)

    keys = _all_keys(db_path)
    assert keys.get("identity.contacts.bob_jones.email") == "bob@example.com"
    assert keys.get("identity.contacts.bob_jones.role") == "DevOps"
    assert keys.get("identity.contacts.bob_jones.slack") == "@bob"


def test_import_contacts_malformed_lines_skipped(tmp_path):
    """Malformed lines are silently skipped; valid lines still parse."""
    db_path = _setup_db(tmp_path)
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()

    contacts = textwrap.dedent("""\
        ## Carol Chen

        - **email**: carol@example.com
        - **broken
        just some orphan text
        - **role**: Engineer
        - **multi:colon**: value:with:colons
    """)
    (identity_dir / "contacts.md").write_text(contacts)

    identity.sync_from_markdown(db_path, identity_dir)

    keys = _all_keys(db_path)
    # Valid lines parsed
    assert keys.get("identity.contacts.carol_chen.email") == "carol@example.com"
    assert keys.get("identity.contacts.carol_chen.role") == "Engineer"
    # Orphan text should not appear as a key
    assert not any(k.startswith("identity.contacts.carol_chen.just") for k in keys)
    assert not any(k.startswith("identity.contacts.carol_chen.broken") for k in keys)


def test_import_contacts_no_lstrip_bug(tmp_path):
    """
    A line like `- *email**: x` (single leading asterisk) should NOT parse.
    With the old lstrip('- **') approach, the leading `-` and `*` would be
    stripped character-by-character, potentially producing a spurious match.
    The regex requires a full **key** pattern, so this line must be rejected.
    """
    db_path = _setup_db(tmp_path)
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()

    contacts = textwrap.dedent("""\
        ## Dave Drake

        - *email**: dave@example.com
    """)
    (identity_dir / "contacts.md").write_text(contacts)

    identity.sync_from_markdown(db_path, identity_dir)

    keys = _all_keys(db_path)
    # No field-level key should be created for this entity
    field_keys = [k for k in keys if k.startswith("identity.contacts.dave_drake.")]
    assert field_keys == [], f"Expected no field keys, got: {field_keys}"


def test_import_contacts_value_with_colon(tmp_path):
    """Value containing colons is captured fully, not truncated at first colon."""
    db_path = _setup_db(tmp_path)
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()

    contacts = textwrap.dedent("""\
        ## Eve Easton

        - **slack_dm**: https://example.com:8080/path
    """)
    (identity_dir / "contacts.md").write_text(contacts)

    identity.sync_from_markdown(db_path, identity_dir)

    keys = _all_keys(db_path)
    assert keys.get("identity.contacts.eve_easton.slack_dm") == "https://example.com:8080/path"


def test_import_yaml_malformed_logs_error(tmp_path, capsys):
    """A malformed YAML file logs a parse error to stderr and does not raise."""
    db_path = _setup_db(tmp_path)
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()

    # Write a malformed YAML file
    (identity_dir / "broken.yaml").write_text("key: [unclosed")

    identity.sync_from_markdown(db_path, identity_dir)

    captured = capsys.readouterr()
    assert "broken.yaml" in captured.err or "failed to parse" in captured.err
