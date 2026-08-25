"""Tests for cross-DB maintenance: fsck + all-DBs backup/restore (techne#139)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.maintenance import (
    RestoreError,
    backup_all_dbs,
    domain_db_paths,
    prune_backup_sets,
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
    from techne.core.knowledge import content_fingerprint
    from techne.core.librarian import librarian_db

    content = "a real document body"
    doc_id, _ = store_document(
        librarian_db(config), source_type="github_issue", source_url="u", content=content, title="T"
    )
    upsert_document_card(
        config.db_path, source_url="u", doc_id=doc_id,
        source_version=content_fingerprint(content), title="T",
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


def _the_set_in(root: Path) -> Path:
    """`--out` names the backups ROOT; the set is the timestamped dir inside it.

    THIS CHANGED IN techne#742 AND THE CHANGE IS THE POINT. `--out` used to name the SET,
    which is what made `--keep` prune the root's PARENT — a directory the operator never
    named. The help text always described a root; the code disagreed, and the disagreement
    turned into an `rmtree`. Scripts doing `backup --out X` then `restore X` need
    `restore X/backup-<stamp>` now.
    """
    sets = [c for c in root.iterdir() if c.is_dir() and (c / "manifest.json").is_file()]
    assert len(sets) == 1, f"expected exactly one set under {root}, found {sets}"
    return sets[0]


def test_cli_backup_writes_a_timestamped_set_inside_out(config, monkeypatch, tmp_path):
    """`--out` is the ROOT, not the set (techne#742). Pinning it because the previous
    behaviour disagreed with the flag's own help text, and `--keep` turned that
    disagreement into a deletion one directory up."""
    out = tmp_path / "root"
    _run_cli(config, monkeypatch, ["backup", "--out", str(out)])

    assert not (out / "manifest.json").exists(), "`--out` must not itself be the set"
    assert _the_set_in(out).name.startswith("backup-")


def test_cli_backup_then_restore_round_trip(config, monkeypatch, tmp_path):
    _add_knowledge(config.db_path, "CLI-MARKER")
    root = tmp_path / "myroot"
    res = _run_cli(config, monkeypatch, ["backup", "--out", str(root)])
    assert res.exit_code == 0 and "techne" in res.output
    out = _the_set_in(root)
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
    root = tmp_path / "root"
    _run_cli(config, monkeypatch, ["backup", "--out", str(root)])
    out = _the_set_in(root)
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


# ---------------------------------------------------------------------------
# Prudence follow-up: stale-advisory, multi-domain round-trip, mid-copy safety
# ---------------------------------------------------------------------------


def test_fsck_stale_card_is_advisory_not_fail(config):
    """A card that RESOLVES but whose ver: drifted from the doc is `stale` — advisory, so
    fsck stays ok (a routine doc edit must not flip fsck red). Only dangling fails."""
    from techne.core.cards import upsert_document_card
    from techne.core.documents import store_document
    from techne.core.librarian import librarian_db

    doc_id, _ = store_document(
        librarian_db(config), source_type="x", source_url="u", content="real body", title="T"
    )
    # ver: deliberately doesn't match the doc's content hash → stale (but resolves).
    upsert_document_card(
        config.db_path, source_url="u", doc_id=doc_id, source_version="WRONG-HASH", title="T"
    )
    report = run_fsck(config)
    assert report.ok  # stale does NOT fail
    card = next(c for c in report.checks if c.name == "dangling_cards")
    assert card.ok and len(card.details["stale"]) == 1 and "advisory" in card.summary


def test_backup_restore_multi_domain_round_trip(config):
    """The loop that matters: back up techne + librarian, mutate BOTH, restore, both recover."""
    from techne.core.documents import get_document, store_document
    from techne.core.librarian import librarian_db

    lib = librarian_db(config)
    _add_knowledge(config.db_path, "T-MARK")
    doc_id, _ = store_document(lib, source_type="x", source_url="u", content="L-MARK", title="T")

    res = backup_all_dbs(config)
    assert set(res.backed_up) == {"techne", "librarian"}

    # Mutate both live DBs.
    conn = connect(config.db_path)
    conn.execute("DELETE FROM knowledge")
    conn.commit()
    conn.close()
    lconn = connect(lib)
    lconn.execute("DELETE FROM documents")
    lconn.commit()
    lconn.close()

    rr = restore_all_dbs(config, res.dest)
    assert set(rr.restored) == {"techne", "librarian"}
    assert "T-MARK" in _knowledge_contents(config.db_path)
    assert get_document(lib, doc_id)["content"] == "L-MARK"


def test_restore_staging_failure_leaves_live_untouched(config, monkeypatch):
    """Prudence #2: a copy failure mid-restore must abort with the live DBs UNTOUCHED (the
    stage-then-commit design) and surface the pre-restore snapshot location."""
    import techne.core.maintenance as maint

    _add_knowledge(config.db_path, "LIVE-OK")
    res = backup_all_dbs(config)

    calls = {"n": 0}
    real_copy = maint.shutil.copy2

    def _flaky_copy(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1 and str(dst).endswith(".restore-tmp"):
            raise OSError("disk full (simulated)")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(maint.shutil, "copy2", _flaky_copy)
    with pytest.raises(RestoreError, match="staging failed"):
        restore_all_dbs(config, res.dest)

    # Live DB never overwritten; no orphan temp left behind.
    assert "LIVE-OK" in _knowledge_contents(config.db_path)
    assert not config.db_path.with_name(config.db_path.name + ".restore-tmp").exists()


# ---------------------------------------------------------------------------
# retention — techne#742
# ---------------------------------------------------------------------------


def _set(backups: Path, name: str, created_at: str) -> Path:
    """A backup set is a directory carrying a manifest. Anything else is not ours."""
    d = backups / name
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"created_at": created_at, "databases": {}}))
    return d


def test_prune_keeps_the_newest_n_sets(tmp_path):
    """RETENTION IS WHY THIS EXISTS (techne#742). `techne backup` had none, so it was safe
    by hand and unsafe on a timer — a nightly job with no ceiling is a disk-filling cron job
    dressed as a backup story."""
    b = tmp_path / "backups"
    old = _set(b, "backup-20260101-000000", "2026-01-01T00:00:00Z")
    mid = _set(b, "backup-20260201-000000", "2026-02-01T00:00:00Z")
    new = _set(b, "backup-20260301-000000", "2026-03-01T00:00:00Z")

    removed, _ = prune_backup_sets(b, keep=2)

    assert removed == [old]
    assert not old.exists()
    assert mid.exists() and new.exists()


def test_prune_orders_by_the_manifest_not_by_mtime(tmp_path):
    """A restore, a copy, or a backup tool reading the directory rewrites mtimes. Ordering on
    them would evict a genuine recent backup BECAUSE SOMETHING READ IT. The stamp inside the
    set is the fact; the filesystem's opinion about it is not."""
    import os

    b = tmp_path / "backups"
    older = _set(b, "backup-A", "2026-01-01T00:00:00Z")
    newer = _set(b, "backup-B", "2026-03-01T00:00:00Z")
    # Make the OLDER set look freshest on disk, which is what a read would do.
    os.utime(older, (9_000_000_000, 9_000_000_000))

    removed, _ = prune_backup_sets(b, keep=1)

    assert removed == [older], "ordered by mtime, not by the manifest's created_at"
    assert newer.exists()


def test_prune_never_touches_what_is_not_a_backup_set(tmp_path):
    """Migration `.db.bak` files live under `backups/` too and are pruned by their own scheme
    in `migrator._prune_backups`. A pruner that cannot tell what it is deleting deletes
    nothing — the same rule that one states."""
    b = tmp_path / "backups"
    b.mkdir(parents=True)
    stray_file = b / "techne-v18-to-v19-20260101-000000.db.bak"
    stray_file.write_text("not a set")
    stray_dir = b / "backups-pre-runtime-pull-20260813"
    stray_dir.mkdir()
    (stray_dir / "techne.db").write_text("no manifest here")
    _set(b, "backup-20260301-000000", "2026-03-01T00:00:00Z")

    removed, _ = prune_backup_sets(b, keep=0)

    assert stray_file.exists(), "pruned a migration backup it cannot identify"
    assert stray_dir.exists(), "pruned a directory with no manifest"
    assert len(removed) == 1


def test_prune_protects_the_set_the_caller_just_wrote(tmp_path):
    """Even at keep=0. Deleting the set the caller is about to rely on is the failure this
    mechanism exists to prevent (`migrator._prune_backups` states the same rule)."""
    b = tmp_path / "backups"
    fresh = _set(b, "backup-20260301-000000", "2026-03-01T00:00:00Z")

    removed, _ = prune_backup_sets(b, keep=0, protect=fresh)

    assert removed == []
    assert fresh.exists()


def test_prune_rejects_a_negative_keep(tmp_path):
    """`keep=-1` would silently keep everything via slice semantics — a retention flag that
    quietly does nothing is the shape this whole issue is about."""
    with pytest.raises(ValueError, match="keep must be >= 0"):
        prune_backup_sets(tmp_path, keep=-1)


def test_prune_is_deterministic_when_stamps_tie(tmp_path):
    """`_now_iso()` has ONE-SECOND resolution, so ties are reachable under a scripted or
    retried `--keep` run. Without a secondary key, a stable sort leaves the survivor to
    `Path.iterdir()` — i.e. to the filesystem (pre-pass on #755: three sets, one stamp,
    keep=1, survivor was whichever iterdir listed first)."""
    b = tmp_path / "backups"
    same = "2026-03-01T00:00:00Z"
    for name in ("backup-A", "backup-B", "backup-C"):
        _set(b, name, same)

    kept = {p.name for p in b.iterdir()} - {p.name for p in prune_backup_sets(b, keep=1)[0]}

    assert kept == {"backup-C"}, "the survivor must not depend on directory order"


def test_prune_skips_a_stamp_it_cannot_order_safely(tmp_path):
    """THE SORT IS LEXICOGRAPHIC, so the format is part of the contract. Demonstrated in
    the pre-pass: `2025-06-01T23:00:00+02:00` (21:00Z) sorts ABOVE `2025-06-01T22:00:00Z`
    as text while being an hour OLDER — so the newer set gets pruned. The failure mode is
    a DELETED BACKUP, so a set we cannot order is skipped rather than guessed at."""
    b = tmp_path / "backups"
    offset = _set(b, "backup-offset", "2025-06-01T23:00:00+02:00")
    canonical = _set(b, "backup-canonical", "2025-06-01T22:00:00Z")

    removed, _ = prune_backup_sets(b, keep=0)

    assert offset.exists(), "pruned a set whose stamp cannot be ordered as text"
    assert removed == [canonical]
