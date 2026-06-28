"""Tests for cross-DB maintenance: fsck + all-DBs backup/restore (techne#139)."""

from __future__ import annotations

import json

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.maintenance import (
    RestoreError,
    backup_all_dbs,
    domain_db_paths,
    read_manifest,
    restore_all_dbs,
    run_fsck,
)


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path
    init_db(cfg.db_path).close()
    return cfg


def _add_knowledge(db_path, content):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO knowledge (type, content, confidence) VALUES ('fact', ?, 'high')",
            (content,),
        )
        conn.commit()
    finally:
        conn.close()


def _knowledge_contents(db_path):
    conn = connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT content FROM knowledge").fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# fsck
# ---------------------------------------------------------------------------


def test_fsck_skips_dangling_check_without_librarian(config):
    """A fresh install has only techne.db — the cross-DB card check is skipped (ok)."""
    report = run_fsck(config)
    assert report.ok
    assert report.db_present["techne"] is True
    assert report.db_present["librarian"] is False
    card_check = next(c for c in report.checks if c.name == "dangling_cards")
    assert card_check.ok and "skipped" in card_check.summary


def test_fsck_passes_when_card_resolves(config):
    """A card whose doc:<id> resolves in librarian.db at the recorded version → fsck ok."""
    from techne.core.cards import upsert_document_card
    from techne.core.documents import store_document
    from techne.core.knowledge import content_hash
    from techne.core.librarian import librarian_db

    content = "a real document body"
    doc_id, _ = store_document(
        librarian_db(config), source_type="github_issue", source_url="u", content=content, title="T"
    )
    upsert_document_card(
        config.db_path, source_url="u", doc_id=doc_id,
        source_version=content_hash(content), title="T",
    )
    report = run_fsck(config)
    assert report.ok
    assert report.db_present["librarian"] is True


def test_fsck_fails_on_dangling_card(config):
    """A card pointing at a doc id absent from librarian.db → dangling → fsck NOT ok."""
    from techne.core.cards import upsert_document_card
    from techne.core.documents import store_document
    from techne.core.librarian import librarian_db

    # Create librarian.db (so the check runs) with one real doc...
    store_document(librarian_db(config), source_type="x", source_url="real", content="c", title="T")
    # ...then a card that points at a NON-existent doc id.
    upsert_document_card(
        config.db_path, source_url="ghost", doc_id=999999, source_version="v", title="Ghost"
    )
    report = run_fsck(config)
    assert not report.ok
    card_check = next(c for c in report.checks if c.name == "dangling_cards")
    assert not card_check.ok
    assert len(card_check.details["dangling"]) == 1


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


def test_backup_only_present_dbs_with_manifest(config):
    _add_knowledge(config.db_path, "MARKER")
    res = backup_all_dbs(config)

    assert res.backed_up == ["techne"]
    assert set(res.skipped_absent) == {"librarian", "codex", "architect"}
    assert (res.dest / "techne.db").exists()

    manifest = json.loads((res.dest / "manifest.json").read_text())
    assert list(manifest["databases"]) == ["techne"]
    assert manifest["databases"]["techne"]["size_bytes"] > 0
    assert manifest["created_at"].endswith("Z")
    # The online-copied db is a real, queryable SQLite file.
    assert "MARKER" in _knowledge_contents(res.dest / "techne.db")


def test_backup_includes_multiple_present_domains(config):
    from techne.core.documents import store_document
    from techne.core.librarian import librarian_db

    store_document(librarian_db(config), source_type="x", source_url="u", content="c", title="T")
    res = backup_all_dbs(config)
    assert set(res.backed_up) == {"techne", "librarian"}
    assert (res.dest / "librarian.db").exists()


# ---------------------------------------------------------------------------
# restore — validation + round-trip + all-or-nothing
# ---------------------------------------------------------------------------


def test_read_manifest_rejects_non_backup_dir(tmp_path):
    with pytest.raises(RestoreError, match="not a techne backup"):
        read_manifest(tmp_path)


def test_read_manifest_rejects_incomplete_set(config, tmp_path):
    res = backup_all_dbs(config)
    (res.dest / "techne.db").unlink()  # corrupt the set: a listed db is missing
    with pytest.raises(RestoreError, match="incomplete"):
        read_manifest(res.dest)


def test_restore_round_trip_recovers_data(config):
    _add_knowledge(config.db_path, "MARKER-XYZ")
    res = backup_all_dbs(config)

    # Mutate the live DB away from the backup.
    conn = connect(config.db_path)
    conn.execute("DELETE FROM knowledge")
    conn.commit()
    conn.close()
    assert "MARKER-XYZ" not in _knowledge_contents(config.db_path)

    rr = restore_all_dbs(config, res.dest)
    assert rr.restored == ["techne"]
    assert rr.pre_restore_backup is not None and rr.pre_restore_backup.exists()
    assert "MARKER-XYZ" in _knowledge_contents(config.db_path)  # data recovered


def test_restore_removes_stale_wal_sidecar(config):
    _add_knowledge(config.db_path, "M")
    res = backup_all_dbs(config)
    # Simulate a live WAL sidecar present at restore time.
    wal = config.db_path.with_name(config.db_path.name + "-wal")
    wal.write_bytes(b"stale-wal")
    restore_all_dbs(config, res.dest, pre_restore_backup=False)
    assert not wal.exists()  # removed so SQLite can't replay it over the restored file


def test_restore_all_or_nothing_aborts_before_touching_live(config):
    """A corrupt set must abort in validation, with the live DB untouched."""
    _add_knowledge(config.db_path, "LIVE-INTACT")
    res = backup_all_dbs(config)
    (res.dest / "techne.db").unlink()  # incomplete → read_manifest (called first) raises

    with pytest.raises(RestoreError):
        restore_all_dbs(config, res.dest)
    # Live DB never overwritten.
    assert "LIVE-INTACT" in _knowledge_contents(config.db_path)


def test_domain_db_paths_resolves_all_without_creating(config):
    paths = domain_db_paths(config)
    assert set(paths) == {"techne", "librarian", "codex", "architect"}
    # Resolving must not CREATE absent domain DBs.
    assert not paths["librarian"].exists()
    assert not paths["codex"].exists()


# ---------------------------------------------------------------------------
# CLI: techne fsck / backup / restore
# ---------------------------------------------------------------------------


def _run_cli(config, monkeypatch, args, input=None):
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: config))
    return CliRunner().invoke(main, args, input=input)


def test_cli_fsck_clean_exit_zero(config, monkeypatch):
    res = _run_cli(config, monkeypatch, ["fsck"])
    assert res.exit_code == 0
    assert "all cross-DB checks passed" in res.output


def test_cli_fsck_inconsistent_exit_one(config, monkeypatch):
    from techne.core.cards import upsert_document_card
    from techne.core.documents import store_document
    from techne.core.librarian import librarian_db

    store_document(librarian_db(config), source_type="x", source_url="u", content="c", title="T")
    upsert_document_card(config.db_path, source_url="g", doc_id=999, source_version="v", title="G")

    res = _run_cli(config, monkeypatch, ["fsck"])
    assert res.exit_code == 1
    assert "inconsistencies found" in res.output


def test_cli_backup_then_restore_round_trip(config, monkeypatch, tmp_path):
    _add_knowledge(config.db_path, "CLI-MARKER")
    out = tmp_path / "myset"
    res = _run_cli(config, monkeypatch, ["backup", "--out", str(out)])
    assert res.exit_code == 0 and "techne" in res.output
    assert (out / "manifest.json").exists()

    conn = connect(config.db_path)
    conn.execute("DELETE FROM knowledge")
    conn.commit()
    conn.close()

    # restore with the confirmation prompt answered 'y'
    res = _run_cli(config, monkeypatch, ["restore", str(out)], input="y\n")
    assert res.exit_code == 0 and "restored" in res.output
    assert "CLI-MARKER" in _knowledge_contents(config.db_path)


def test_cli_restore_cancelled_leaves_live_untouched(config, monkeypatch, tmp_path):
    _add_knowledge(config.db_path, "KEEP-ME")
    out = tmp_path / "set"
    _run_cli(config, monkeypatch, ["backup", "--out", str(out)])
    conn = connect(config.db_path)
    conn.execute("INSERT INTO knowledge (type, content, confidence) VALUES ('fact','NEWER','high')")
    conn.commit()
    conn.close()

    res = _run_cli(config, monkeypatch, ["restore", str(out)], input="n\n")  # decline
    assert res.exit_code == 0 and "cancelled" in res.output
    assert "NEWER" in _knowledge_contents(config.db_path)  # live not overwritten


def test_cli_restore_bad_dir_aborts(config, monkeypatch, tmp_path):
    res = _run_cli(config, monkeypatch, ["restore", str(tmp_path / "nope")])
    assert res.exit_code == 1
    assert "aborted" in res.output
