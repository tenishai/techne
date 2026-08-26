"""Tests for cross-DB maintenance: fsck + all-DBs backup/restore (techne#139)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.maintenance import (
    RETENTION_REFUSAL_PHRASES,
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
    """A backup set is a directory carrying a manifest THAT LISTS DATABASES.

    `databases` used to be `{}` here, which the pruner accepted and `read_manifest`
    rejects — so the fixture was asserting a set shape techne itself would refuse to
    restore from (Prudence, #755 Important 1 flagged this before the check was
    tightened). No db FILES are written: the pruner asks whether a directory is ours,
    not whether it could be restored, and those are deliberately different questions.
    """
    d = backups / name
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        json.dumps({"created_at": created_at, "databases": {"techne": {"file": "techne.db"}}})
    )
    # AND THE FILE IT LISTS, because a set that names a database it does not contain is one
    # `restore` refuses (Prudence, #755 round 8). Leaving it out made every fixture here
    # assert a shape techne would decline — the same defect as the `"databases": {}` fixture
    # in round 2, which is where the precedent was set: fix the fixture, do not weaken the
    # check to accommodate it.
    (d / "techne.db").write_bytes(b"")
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


@pytest.mark.parametrize("order", [False, True], ids=["iterdir-ascending", "iterdir-descending"])
def test_prune_is_deterministic_when_stamps_tie(tmp_path, monkeypatch, order):
    """`_now_iso()` has ONE-SECOND resolution, so ties are reachable under a scripted or
    retried `--keep` run. Without a secondary key, a stable sort leaves the survivor to
    `Path.iterdir()` — i.e. to the filesystem (pre-pass on #755: three sets, one stamp,
    keep=1, survivor was whichever iterdir listed first).

    RUN UNDER BOTH DIRECTORY ORDERS, which is the whole point and was the defect in the
    version of this test that shipped in round 2 (Prudence, #755 round 5, question D).
    That version ran in whatever order APFS gave — ascending — where the tie-break's answer
    and the filesystem's answer AGREE. It therefore passed on a machine whose filesystem
    happened to be doing the work, and it would have gone silently green on one that
    listed descending. A test for "the result does not depend on directory order" cannot
    itself run in only one directory order.

    Note the two orders are NOT symmetric: a stable `sort(reverse=True)` preserves input
    order among equal keys, so DESCENDING yields `backup-C` even unguarded. Ascending is
    the order that catches it. Keeping both is deliberate — the claim is about every order,
    and pinning only the discriminating one re-encodes today's implementation.
    """
    b = tmp_path / "backups"
    same = "2026-03-01T00:00:00Z"
    for name in ("backup-A", "backup-B", "backup-C"):
        _set(b, name, same)

    real_iterdir = Path.iterdir
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda self: iter(sorted(real_iterdir(self), key=lambda q: q.name, reverse=order))
        if self == b
        else real_iterdir(self),
    )

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

    removed, failed = prune_backup_sets(b, keep=0)

    assert offset.exists(), "pruned a set whose stamp cannot be ordered as text"
    assert removed == [canonical]
    assert [p.name for p, _ in failed] == ["backup-offset"]
    assert "cannot be ordered safely" in failed[0][1]


def test_the_pre_restore_snapshot_stays_visible_to_retention(config):
    """THE SET RETENTION CANNOT SEE IS THE ONE THAT GROWS FOREVER. Making `--out` the
    backups ROOT (#755's Critical) silently re-nested the one internal caller that named
    its own set: `restore_all_dbs`' pre-restore snapshot landed at
    `backups/pre-restore-<stamp>/backup-<stamp>/`, and `prune_backup_sets` scans only
    DIRECT children for a manifest — so `--keep` stopped seeing it and every restore
    leaked a full copy of every domain db. Pins the layout, not the call: a set under
    `backups/` must be prunable no matter which code path wrote it."""
    _add_knowledge(config.db_path, "PRE-RESTORE-MARK")
    res = backup_all_dbs(config)
    restore_all_dbs(config, res.dest, pre_restore_backup=True)

    backups = Path(config.data_dir) / "backups"
    pre = [d for d in backups.iterdir() if d.is_dir() and d.name.startswith("pre-restore-")]
    assert len(pre) == 1, f"expected one pre-restore set directly under backups/, got {pre}"
    assert (pre[0] / "manifest.json").is_file(), "pre-restore set carries no manifest at its top"

    removed, failed = prune_backup_sets(backups, keep=0)

    assert failed == []
    assert pre[0] in removed, "retention cannot see the pre-restore set"
    assert not pre[0].exists()


def test_prune_leaves_a_foreign_directory_alone(tmp_path):
    """TECHNE REFUSING TO RESTORE FROM A DIRECTORY AND BEING WILLING TO DELETE IT IS NOT A
    DEFENSIBLE PAIR (Prudence, #755 Important 1). `manifest.json` is not a techne-private
    filename and `created_at` is not a techne-private field, so a foreign project directory
    satisfied the old check — which asked only for parseable JSON and an ISO-Z stamp — while
    `read_manifest` rejected it outright. `rmtree` is not recoverable."""
    b = tmp_path / "backups"
    ours = _set(b, "backup-20260101-000000", "2026-01-01T00:00:00Z")
    foreign = b / "some_project"
    foreign.mkdir(parents=True)
    (foreign / "manifest.json").write_text(
        json.dumps(
            {"name": "my-extension", "version": "2.1.0", "created_at": "2024-03-01T09:00:00Z"}
        )
    )

    with pytest.raises(RestoreError):
        read_manifest(foreign)  # the asymmetry this test exists to close

    removed, failed = prune_backup_sets(b, keep=0)

    assert foreign.exists(), "pruned a directory techne refuses to read as a backup set"
    assert removed == [ours]
    assert failed == []


def test_prune_skips_a_set_stamped_in_the_future(tmp_path):
    """With no upper bound on a lexicographic sort, a set stamped 2099 sorts newest FOREVER,
    holds a keep slot permanently, and quietly makes retention `keep - 1` (Prudence, #755
    Nit 4). Reachable by ordinary clock skew — and `Persistent=true` fires a catch-up run
    right after boot, before NTP may have synced. Same category as the `+02:00` stamp: one
    we cannot order against real time, so it is skipped rather than trusted."""
    b = tmp_path / "backups"
    future = _set(b, "backup-future", "2099-01-01T00:00:00Z")
    real = _set(b, "backup-real", "2026-03-01T00:00:00Z")

    removed, failed = prune_backup_sets(b, keep=1)

    assert future.exists(), "a future stamp must not occupy a retention slot"
    assert real.exists(), "the newest REAL set was pruned in favour of a future-stamped one"
    assert removed == []
    # NEVER DELETED AND NEVER MENTIONED IS A LEAK (Prudence, #755 round 3 Important 1).
    assert [p.name for p, _ in failed] == ["backup-future"]
    assert "in the future" in failed[0][1]


def test_prune_names_a_symlinked_set_as_a_symlink(tmp_path):
    """`shutil.rmtree` REFUSES a symlink, which is what keeps the target safe — but it
    raises `[Errno None] None`, a string containing neither the word "symlink" nor anything
    actionable, on every run forever (Prudence, #755 Nit 5). That is the alarm-fatigue
    channel for `retention INCOMPLETE`. Pins the target-safety too, which was accidental
    rather than designed and had nothing holding it."""
    b = tmp_path / "backups"
    fresh = _set(b, "backup-fresh", "2026-03-01T00:00:00Z")
    target = _set(tmp_path / "elsewhere", "backup-target", "2026-01-01T00:00:00Z")
    (b / "backup-link").symlink_to(target, target_is_directory=True)

    removed, failed = prune_backup_sets(b, keep=1, protect=fresh)

    assert removed == []
    assert [p.name for p, _ in failed] == ["backup-link"]
    # EXACT, NOT `"symlink" in ...` — pytest derives `tmp_path` from the TEST NAME, so the
    # raw `[Errno None] None: PosixPath('.../test_prune_names_a_symlinked_s0/...')` message
    # CONTAINS the substring "symlink". A containment assertion here passes when the branch
    # under test is deleted, satisfied by the fixture's own directory name. Caught by
    # mutation-testing this guard; the test proved nothing until this line.
    assert failed[0][1] == "is a symlink — refusing to remove it or its target"
    assert target.exists(), "followed a symlink out of backups/ and deleted its target"
    assert (b / "backup-link").is_symlink()


@pytest.mark.parametrize("label", ["../escape", "a/b", "", ".hidden", "a\\b"])
def test_backup_rejects_a_label_that_could_escape_the_root(config, label):
    """AN INVARIANT, NOT A COMMENT (Prudence, #755 Nit 8). `root / f"{label}-<stamp>"` is
    safe today because of a property of the two CALLERS, not of the function — and the
    failure would be silent, because an escaped path still creates a plausible directory."""
    with pytest.raises(ValueError, match="bare directory-name fragment"):
        backup_all_dbs(config, label=label)


def test_backup_exits_3_when_retention_could_not_complete(config, monkeypatch):
    """EXIT STATUS IS THE ONLY CHANNEL A TIMER SURFACES (Prudence, #755 Important 2). Both
    units this PR ships route stderr to a place nobody reads until something is already
    wrong, so at exit 0 `systemctl status` reads green forever while retention is
    permanently broken. 3 rather than 1: the backup SUCCEEDED, and a monitor has to be able
    to tell those apart. Driven through the real CLI with a real prune failure — a
    symlinked set, which `rmtree` genuinely refuses — not a patched pruner."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
    backups = Path(config.data_dir) / "backups"
    target = _set(Path(config.data_dir) / "elsewhere", "backup-target", "2026-01-01T00:00:00Z")
    backups.mkdir(parents=True, exist_ok=True)
    (backups / "backup-link").symlink_to(target, target_is_directory=True)

    result = CliRunner().invoke(main, ["backup", "--keep", "1"])

    # The LITERAL 3, not `EX_RETENTION_INCOMPLETE`, on purpose: this pins the value that
    # crosses the process boundary into a monitor. Asserting against the constant would
    # follow it if someone changed it, which is the one change that must break a test.
    assert result.exit_code == 3, f"exit {result.exit_code}\n{result.output}"
    assert "retention INCOMPLETE" in result.output
    assert target.exists()


def test_backup_exits_0_when_retention_completes(config, monkeypatch):
    """The other half of the contract: a clean run must NOT go red, or the exit code stops
    meaning anything and the operator silences it."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
    old = _set(Path(config.data_dir) / "backups", "backup-20260101-000000", "2026-01-01T00:00:00Z")

    result = CliRunner().invoke(main, ["backup", "--keep", "1"])

    assert result.exit_code == 0, f"exit {result.exit_code}\n{result.output}"
    assert "retention INCOMPLETE" not in result.output
    # PROVE RETENTION RAN, not merely that nothing complained (Prudence, #755 round 5,
    # question D). Both of this file's exit-0 assertions survived `if keep is not None:` ->
    # `if False:` — they passed with retention DISABLED ENTIRELY, which is the state they
    # exist to distinguish from working.
    assert not old.exists(), "exit 0 with the old set still present — retention did nothing"
    assert "pruned 1 older set(s)" in result.output


def test_a_symlinked_set_never_enters_the_keep_window(tmp_path):
    """THE SCAN LOOP ADMITTED SYMLINKS AND ORDERED THEM BY THE TARGET'S STAMP (Prudence,
    #755 round 3 Important 2). The `is_symlink` guard lived in the DELETE loop only, so a
    symlink pointing at a set NEWER than a real one took a slot inside the keep window and
    pushed the real backup out of it — measured `removed: ['backup-a']`, `failed: []`: a
    genuine backup deleted, nothing reported. The round-2 test covered only the mirror case
    (target older, so the symlink sorted last and merely failed to delete), which is
    exactly why this half hid behind it."""
    b = tmp_path / "backups"
    real = _set(b, "backup-a", "2026-01-01T00:00:00Z")
    newer_target = _set(tmp_path / "elsewhere", "backup-newer", "2026-06-01T00:00:00Z")
    (b / "backup-link").symlink_to(newer_target, target_is_directory=True)

    removed, failed = prune_backup_sets(b, keep=1)

    assert real.exists(), "a real backup was deleted to make room for a symlink"
    assert removed == []
    assert [p.name for p, _ in failed] == ["backup-link"]
    assert newer_target.exists()


def test_unorderable_sets_are_reported_so_they_cannot_pile_up_silently(config, monkeypatch):
    """SKIPPING A SET IS NOT FREE ONCE IT IS UNBOUNDED. Before Nit 4's fix, an unorderable
    set was self-limiting — it got pruned once more than `keep` existed. Now it is never
    pruned, so silence means the disk fills on a schedule while the timer reads green:
    Important 2's shape, one level up, introduced by the fix for Nit 4. Driven through the
    real CLI, because the exit code is the half that matters."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
    backups = Path(config.data_dir) / "backups"
    for i in range(7):
        _set(backups, f"backup-bad-{i}", f"2026-06-0{i + 1}T00:00:00+02:00")

    result = CliRunner().invoke(main, ["backup", "--keep", "1"])

    assert result.exit_code == 3, f"exit {result.exit_code}\n{result.output}"
    assert "retention INCOMPLETE: 7 set(s)" in result.output
    assert all((backups / f"backup-bad-{i}").exists() for i in range(7))


@pytest.mark.parametrize("dbs", [{"techne": "techne.db"}, {"techne": None}, {"techne": ["x"]}])
def test_prune_leaves_a_set_restore_cannot_read_alone(tmp_path, dbs):
    """THE ONE SHAPE THAT STILL GOT THROUGH (Prudence, #755 round 3 Nit 3). A `databases`
    dict whose VALUES are not dicts satisfied "non-empty dict" while `read_manifest` could
    not read it at all. Round 2's asymmetry survived here: the pruner called it ours and
    would rmtree it while restore blew up on it. Asserted against `read_manifest` in the
    same body, so the pair is pinned rather than the symptom.

    UPDATED BY techne#759, exactly as the note below predicted. The asymmetry is closed
    from the other side now: `read_manifest` raises RestoreError for these shapes instead
    of AttributeError, so both halves of the pair agree that the set is unreadable, and
    they agree by CONTRACT rather than by coincidence."""
    b = tmp_path / "backups"
    ours = _set(b, "backup-20260101-000000", "2026-01-01T00:00:00Z")
    unreadable = b / "backup-20260201-000000"
    unreadable.mkdir(parents=True)
    (unreadable / "manifest.json").write_text(
        json.dumps({"created_at": "2026-02-01T00:00:00Z", "databases": dbs})
    )

    # ONE DOOR, NOT TWO (Prudence, #755 round 5, question D). `raises((RestoreError,
    # AttributeError))` would pass whichever happens, so it could not notice the behaviour
    # changing. It was pinned to AttributeError precisely so that #759 would have to come
    # back and change this line — and it did. Keep it pinned to ONE exception for the same
    # reason: the next change to this contract should be forced through here too.
    with pytest.raises(RestoreError):
        read_manifest(unreadable)

    removed, _ = prune_backup_sets(b, keep=0)

    assert unreadable.exists(), "pruned a set techne cannot read as a backup"
    assert removed == [ours]


def test_an_unreadable_manifest_is_reported_not_silently_skipped(config, monkeypatch):
    """AN UNREADABLE MANIFEST IS NOT AN ABSENT ONE (Prudence, #755 round 4 Important 1).
    Folding every OSError into silence meant a set techne itself wrote — canonical stamp,
    real db files — went permanently quiet and permanently unpruned the moment its manifest
    became unreadable. Round 3 shut the stamp door and left this one open, and reachability
    is symmetric: `_now_iso()` is the sole writer of `created_at` and always emits
    canonical, so a bad stamp is no more techne-producible than a chmod'd manifest. Driven
    through the real CLI with a REAL permission denial."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
    backups = Path(config.data_dir) / "backups"
    stranded = _set(backups, "backup-20260101-000000", "2026-01-01T00:00:00Z")
    (stranded / "manifest.json").chmod(0o000)
    try:
        result = CliRunner().invoke(main, ["backup", "--keep", "1"])

        assert result.exit_code == 3, f"exit {result.exit_code}\n{result.output}"
        assert "manifest is unreadable" in result.output
        assert stranded.exists()
    finally:
        (stranded / "manifest.json").chmod(0o644)


def test_an_absent_manifest_stays_silent(config, monkeypatch):
    """The other side of the same boundary: a directory under a root the operator pointed us
    at, carrying no manifest, is not ours to report OR delete. If this went to `failed` the
    exit code would go red for someone else's folder and the signal would be worthless."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
    backups = Path(config.data_dir) / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    (backups / "someone-elses-folder").mkdir()
    # A REAL SET THAT MUST BE PRUNED, so this cannot pass with retention disabled — the
    # weak-oracle problem Prudence ranked in round 5. Without it, "exit 0 and no complaint"
    # is equally true of a run that did nothing at all.
    prunable = _set(backups, "backup-20260101-000000", "2026-01-01T00:00:00Z")

    result = CliRunner().invoke(main, ["backup", "--keep", "1"])

    assert result.exit_code == 0, f"exit {result.exit_code}\n{result.output}"
    assert "retention INCOMPLETE" not in result.output
    assert (backups / "someone-elses-folder").exists(), "reported or removed a foreign folder"
    assert not prunable.exists(), "retention did not run, so the silence proves nothing"


def test_every_retention_refusal_has_a_row_in_the_docs():
    """THE MESSAGES AND THE TABLE HAVE THE SAME AUTHOR, so left unchecked they agree with
    each other rather than with reality (Prudence, #755 round 5 attack B). An operator
    reading `docs/deployment.md` at 3am because their timer went red has to be able to
    match what they see on stderr to a row. Derived rather than retyped, which is the rule
    #498 set for this repo's other cross-process contract."""
    from techne.core.maintenance import RETENTION_REFUSAL_PHRASES

    doc = (Path(__file__).parent.parent / "docs" / "deployment.md").read_text()
    table = doc[doc.index("What `3` actually means") :]

    missing = [phrase for phrase in RETENTION_REFUSAL_PHRASES if phrase not in table]

    assert not missing, f"exit-3 causes with no row in deployment.md: {missing}"


_REFUSAL_BUILDERS = {
    "is a symlink": "symlink",
    "manifest is unreadable": "unreadable",
    "is not YYYY-MM-DDTHH:MM:SSZ": "bad_stamp",
    "is in the future": "future",
    "has no manifest": "no_manifest",
    "manifest does not parse": "bad_json",
    "lists databases in a shape techne cannot read": "bad_databases",
    "lists a database file that is not there": "missing_db_file",
}


@pytest.mark.parametrize("phrase", RETENTION_REFUSAL_PHRASES)
def test_every_refusal_phrase_is_actually_emitted(tmp_path, phrase):
    """VACUOUS BEFORE, AND THEN OUT OF SYNC (Prudence, #755 rounds 6-7). The first version
    read `maintenance.py`'s SOURCE TEXT, so it passed the exact mutation its own docstring
    described — a test that greps the file it is testing is not a test. The second version
    built each condition but iterated a HAND-WRITTEN list, so the phrase added in round 7
    simply was not covered and its guard's mutation survived.

    Parametrised over `RETENTION_REFUSAL_PHRASES` itself now, which is the same
    derive-don't-retype rule the doc gate follows: add a phrase without a builder and this
    fails, rather than silently testing one fewer thing than it claims to."""
    build = _REFUSAL_BUILDERS.get(phrase)
    assert build is not None, (
        f"no builder for refusal {phrase!r} — add one rather than narrowing this test"
    )
    b = tmp_path / "backups"
    b.mkdir(parents=True)
    d = b / "backup-20260101-000000"
    if build == "symlink":
        target = _set(tmp_path / "elsewhere", "backup-t", "2026-01-01T00:00:00Z")
        d.symlink_to(target, target_is_directory=True)
    elif build == "unreadable":
        _set(b, "backup-20260101-000000", "2026-01-01T00:00:00Z")
        (d / "manifest.json").chmod(0o000)
    elif build == "bad_stamp":
        _set(b, "backup-20260101-000000", "2025-06-01T23:00:00+02:00")
    elif build == "future":
        _set(b, "backup-20260101-000000", "2099-01-01T00:00:00Z")
    elif build == "no_manifest":
        d.mkdir()
    elif build == "bad_json":
        d.mkdir()
        (d / "manifest.json").write_text("")  # what a truncating write leaves behind
    elif build == "missing_db_file":
        _set(b, "backup-20260101-000000", "2026-01-01T00:00:00Z")
        (d / "techne.db").unlink()  # the manifest still lists it
    elif build == "bad_databases":
        d.mkdir()
        (d / "manifest.json").write_text(
            json.dumps(
                {
                    "techne_version": "0.15.0",
                    "created_at": "2026-01-01T00:00:00Z",
                    "databases": {"techne": "techne.db"},  # a string, not an object
                }
            )
        )

    try:
        _removed, failed = prune_backup_sets(b, keep=0)

        assert len(failed) == 1, f"expected one refusal, got {failed}"
        assert phrase in failed[0][1], f"{phrase!r} not in {failed[0][1]!r}"
    finally:
        m = d / "manifest.json"
        if d.is_dir() and not d.is_symlink() and m.exists():
            m.chmod(0o644)


def test_an_empty_set_does_not_take_a_slot_from_a_real_one(tmp_path):
    """ISOLATES THE `holds_data` GUARD. The CLI-level regression test for round 7's Critical
    is saved by the ordering fix alone (`apply_keep=False` on a capture-nothing run), so it
    never exercises this half — mutating `holds_data` back left it green. Here the window IS
    applied and the empty set is the NEWEST, which is precisely the arrangement that
    destroyed archives: sorted by stamp it would take a slot and evict a real backup."""
    b = tmp_path / "backups"
    older = _set(b, "backup-20260101-000000", "2026-01-01T00:00:00Z")
    newer = _set(b, "backup-20260102-000000", "2026-01-02T00:00:00Z")
    empty = b / "backup-20260103-000000"
    empty.mkdir()
    (empty / "manifest.json").write_text(
        json.dumps(
            {"techne_version": "0.15.0", "created_at": "2026-01-03T00:00:00Z", "databases": {}}
        )
    )

    removed, failed = prune_backup_sets(b, keep=2)

    assert failed == []
    assert older.exists() and newer.exists(), "an empty set evicted a real backup"
    assert removed == [empty], f"the empty set was not reclaimed: {removed}"


def test_an_interrupted_backup_leaves_nothing_behind(config, monkeypatch):
    """THE MANIFEST IS WRITTEN LAST, so an interrupted run left a directory holding a full
    copy of a live domain db and no manifest — which the pruner read as "not ours" and
    ignored forever (Prudence, #755 round 5 Important 1). Measured before the fix: at
    `keep=0` the pruner removed the REAL set and left the orphan, `failed=[]`. KeyboardInterrupt
    specifically, because that is how a person stops a backup and it is not an `Exception`."""
    import techne.core.maintenance as maint

    calls = {"n": 0}
    real_copy = maint._online_copy

    def _interrupted(src, dst):
        calls["n"] += 1
        real_copy(src, dst)
        if calls["n"] == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(maint, "_online_copy", _interrupted)
    backups = Path(config.data_dir) / "backups"

    with pytest.raises(KeyboardInterrupt):
        backup_all_dbs(config)

    # `if backups.exists() else []` made "the whole backups ROOT was deleted" indistinguishable
    # from "the partial set was cleaned up" — mutating `rmtree(dest_dir)` to
    # `rmtree(dest_dir.parent)` passed all 50 tests (Prudence, #755 round 6 Important 3).
    # Cleanup that takes the root with it is a far worse bug than the one being fixed.
    assert backups.is_dir(), "cleanup removed the backups root, not just the partial set"
    leftovers = [d.name for d in backups.iterdir()]
    assert leftovers == [], f"an interrupted backup left {leftovers} behind"


def test_an_orphaned_set_we_named_is_reported(config, monkeypatch):
    """The half in-process cleanup cannot reach: a SIGKILL or a power cut runs no handler.
    The directory is still one TECHNE NAMED, so it is an interrupted backup rather than a
    foreign folder — conclusive that it is ours, conclusive that it is incomplete, and
    therefore reported rather than silently left to hold a copy of a live db forever."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
    backups = Path(config.data_dir) / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    orphan = backups / "backup-20260101-120000"
    orphan.mkdir()
    (orphan / "techne.db").write_bytes(b"a partial copy of a live database")

    result = CliRunner().invoke(main, ["backup", "--keep", "1"])

    assert result.exit_code == 3, f"exit {result.exit_code}\n{result.output}"
    assert "has no manifest" in result.output
    assert orphan.exists(), "an incomplete set must be reported, never deleted automatically"


def test_a_set_with_no_databases_is_still_ours(config, monkeypatch):
    """THE SIXTH CASE, AND IT ARRIVES THROUGH THE SUCCESS PATH. When every domain db is
    absent, `backup_all_dbs` writes a COMPLETE set with `"databases": {}` — and the
    identity test, imported wholesale from `read_manifest` in round 2, then answered "not
    ours" about a set techne had just written. Measured before the fix: five nightly runs,
    five sets on disk, `removed=[]`, `failed=[]`, exit 0. Silent unbounded growth with no
    interruption involved. `read_manifest` keys on `databases` because it is about to
    RESTORE from them; identity is a different question and `techne_version` answers it."""
    empty_home = Path(config.data_dir) / "empty"
    empty_home.mkdir()
    from techne.config import Config

    cfg = Config()
    cfg.db_path = empty_home / "techne.db"  # deliberately never created
    cfg.data_dir = empty_home

    first = backup_all_dbs(cfg)
    assert first.backed_up == [], "fixture is wrong — something was actually backed up"
    for _ in range(3):
        backup_all_dbs(cfg)
    # A REAL SET IN THE ARRANGEMENT, which is what this test was missing (Prudence, #755
    # round 7, question D). All-empty could not distinguish "reclaimed an empty set" from
    # "evicted a real one" — and evicting a real one is exactly what the fix it was written
    # for went on to do. One non-empty set here would have caught the Critical.
    real = _set(empty_home / "backups", "backup-20200101-000000", "2020-01-01T00:00:00Z")

    removed, failed = prune_backup_sets(empty_home / "backups", keep=1)

    assert failed == []
    assert real.exists(), "an empty set evicted a real backup from the keep window"
    assert len(removed) == 4, f"empty sets are not being reclaimed: {removed}"
    assert not any(p.name.startswith("backup-2026") for p in (empty_home / "backups").iterdir())


def test_backup_exits_4_when_it_captured_nothing(config, monkeypatch, tmp_path):
    """A BACKUP OF ZERO DATABASES IS NOT A BACKUP, and at exit 0 it was indistinguishable
    from a working one on the only channel a timer has. The realistic cause is a job pointed
    at the wrong store — a typo'd TECHNE_HOME, a unit installed before `techne init` — which
    means a green nightly light and no backups at all until the day someone needs to
    restore. Separate from 1: nothing failed, there was simply nothing there."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    empty = tmp_path / "wrong-store"
    empty.mkdir()
    cfg = Config()
    cfg.db_path = empty / "techne.db"  # deliberately never created
    cfg.data_dir = empty
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))

    result = CliRunner().invoke(main, ["backup", "--keep", "14"])

    assert result.exit_code == 4, f"exit {result.exit_code}\n{result.output}"
    assert "NO DATABASES WERE BACKED UP" in result.output
    assert str(empty) in result.output, "does not name the store it actually looked in"


def test_backup_exits_0_when_it_captured_something(config, monkeypatch):
    """The other side, so exit 4 cannot fire on a working backup — the failure that would
    train an operator to ignore it."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))

    result = CliRunner().invoke(main, ["backup"])

    assert result.exit_code == 0, f"exit {result.exit_code}\n{result.output}"
    assert "NO DATABASES WERE BACKED UP" not in result.output


def test_the_manifest_never_exists_half_written(config, monkeypatch):
    """`Path.write_text` opens mode `'w'`, which TRUNCATES BEFORE IT WRITES — so a kill in
    that window left a zero-byte `manifest.json`: a file that is neither absent nor valid
    (Prudence, #755 round 6 Important 1). The fix writes a temp name and `os.replace`s it,
    so the truncated state stops existing rather than being detected downstream.

    Pins the PROPERTY, not the implementation: interrupt the run at the moment of the
    swap and assert what is on disk. `manifest.json` must be wholly absent — never
    present-and-empty. Cleanup is disabled here on purpose; it would otherwise remove the
    evidence, and what is under test is the state a SIGKILL leaves, which no cleanup runs
    for."""
    import techne.core.maintenance as maint

    def _die(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(maint.os, "replace", _die)
    monkeypatch.setattr(maint.shutil, "rmtree", lambda *a, **k: None)  # keep the evidence

    with pytest.raises(KeyboardInterrupt):
        backup_all_dbs(config)

    sets = list((Path(config.data_dir) / "backups").iterdir())
    assert len(sets) == 1, sets
    manifest = sets[0] / "manifest.json"
    assert not manifest.exists(), (
        f"a half-written manifest.json survived ({manifest.stat().st_size} bytes) — "
        "the state the atomic write exists to make impossible"
    )


@pytest.mark.parametrize("populated", [True, False], ids=["with-databases", "empty"])
def test_what_backup_writes_is_recognised_as_ours(config, tmp_path, populated):
    """THE ROUND TRIP, WHICH IS THE INVARIANT THAT ACTUALLY MATTERS: whatever
    `backup_all_dbs` writes, `_classify_backup_set` must recognise as ours. It has been
    broken twice in this PR from opposite ends — round 2 taught the classifier
    `read_manifest`'s test (which rejected our empty sets), and identity now keys on
    `techne_version` (which a future rename would silently break for the sets it is the only
    evidence for — the empty ones — leaving them never pruned and never mentioned: this PR's
    own failure shape arriving via a refactor rather than a bug). Measured: renaming the
    field fails exactly the empty half of this test and not the populated one.

    Both cases, because the empty one is exactly what round 2 got wrong and a test that
    only covers the populated one would have passed throughout."""
    from techne.config import Config
    from techne.core.maintenance import _classify_backup_set

    if populated:
        cfg = config
    else:
        empty = tmp_path / "no-dbs"
        empty.mkdir()
        cfg = Config()
        cfg.db_path = empty / "techne.db"  # deliberately never created
        cfg.data_dir = empty

    res = backup_all_dbs(cfg)

    cls = _classify_backup_set(res.dest)

    assert cls.refusal is None, f"techne refuses to recognise a set it just wrote: {cls.refusal}"
    assert cls.stamp is not None, "a set we wrote is invisible to retention"
    # AND THE THIRD ANSWER, which is what round 7's Critical turned on: being ours must not
    # imply being a backup. A populated set holds data and may occupy a retention slot; an
    # empty one is ours, is reclaimable, and must never take a slot from a real archive.
    assert cls.holds_data is populated


def test_an_empty_set_never_evicts_a_real_backup(config, monkeypatch, tmp_path):
    """THE ROUND-7 CRITICAL, WHICH I CAUSED FIXING A LEAK (Prudence, #755). Round 6 made an
    empty set "ours" so retention could reclaim it — and "ours" was also what made a set
    COUNT TOWARD THE KEEP WINDOW. The empty set carries the newest stamp, so it took a slot
    and evicted a genuine backup every night. Her measurement: three real sets, `--keep 2`,
    empty source store, five nights, every real backup gone.

    The leak that fix was chasing was a directory and a small JSON file. The regression was
    the archive. Trading a leak for a deletion is the wrong direction and this pins it."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    wrong_store = tmp_path / "wrong-store"
    wrong_store.mkdir()
    cfg = Config()
    cfg.db_path = wrong_store / "techne.db"  # never created — captures nothing
    cfg.data_dir = wrong_store
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    backups = wrong_store / "backups"
    real = [
        _set(backups, f"backup-2026010{i}-030000", f"2026-01-0{i}T03:00:00Z") for i in (1, 2, 3)
    ]

    for _ in range(5):  # five nights on a misconfigured timer
        result = CliRunner().invoke(main, ["backup", "--keep", "2"])

    # PIN THE else-BRANCH WORDING TOO (Prudence, #755 round 9). Restoring the old
    # "pruned N older set(s), keeping K" line left the whole suite green — only the `if`
    # branch was covered, so the fix for a message that CLAIMED A CEILING IT DID NOT APPLY
    # had nothing asserting it. By the last run an earlier empty set is being reclaimed, so
    # this is the arrangement where that branch actually prints.
    assert "keep window NOT applied" in result.output, (
        f"the run claims a ceiling it did not apply:\n{result.output}"
    )
    assert "keeping 2" not in result.output

    survivors = [r.name for r in real if r.exists()]
    assert survivors == [r.name for r in real], (
        f"a run that captured NOTHING destroyed real backups — {survivors} left of 3"
    )


def test_exit_4_takes_precedence_over_exit_3(config, monkeypatch, tmp_path):
    """A CONTRACT WITH NO ORACLE (Prudence, #755 round 7, question D — the fifth distinct
    weak-test mechanism in this PR). Inverting the 3-vs-4 precedence in `cli.py` left all
    3491 tests green: the rule was documented in a changelog, a docstring and a doc table,
    and asserted nowhere. Both conditions hold here at once — nothing was captured AND a
    set cannot be reclaimed — and 4 must win, because an empty backup is the more serious
    of the two."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    wrong_store = tmp_path / "wrong-store"
    wrong_store.mkdir()
    cfg = Config()
    cfg.db_path = wrong_store / "techne.db"  # never created — captures nothing
    cfg.data_dir = wrong_store
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    backups = wrong_store / "backups"
    backups.mkdir(parents=True)
    stranded = _set(backups, "backup-unorderable", "2025-06-01T23:00:00+02:00")

    result = CliRunner().invoke(main, ["backup", "--keep", "1"])

    assert "retention INCOMPLETE" in result.output, "the 3-condition does not hold — bad fixture"
    assert "NO DATABASES WERE BACKED UP" in result.output, "the 4-condition does not hold"
    assert result.exit_code == 4, f"3 won over 4 — exit {result.exit_code}\n{result.output}"
    assert stranded.exists()


def test_protect_covers_the_empty_set_path_too(tmp_path):
    """THE SIXTH WEAK-TEST MECHANISM (Prudence, #755 round 8, found by mutation not opinion):
    A GUARD TESTED ONLY ON THE PATHS THAT EXISTED WHEN THE TEST WAS WRITTEN. She made
    `protect` ignore empty sets and all 65 tests passed — because the existing test uses a
    POPULATED fixture, so it covers `protect` for the keep-window path only, and round 7
    added a second deletion path (unconditional empty-set reclamation) that nothing checked.

    This is load-bearing beyond tidiness: `protect` on the empty path is the ONLY thing
    preserving the misconfiguration record. On a wrongly-pointed timer every run writes an
    empty set and reclaims the previous ones; without `protect` the current run's set goes
    too, and the operator loses the `skipped_absent` list naming which domains were absent."""
    b = tmp_path / "backups"
    b.mkdir(parents=True)
    fresh = b / "backup-20260103-000000"
    fresh.mkdir()
    fresh_manifest = {
        "techne_version": "0.15.0",
        "created_at": "2026-01-03T00:00:00Z",
        "databases": {},
        "skipped_absent": ["techne", "librarian", "codex", "architect"],
    }
    (fresh / "manifest.json").write_text(json.dumps(fresh_manifest))
    older_empty = b / "backup-20260102-000000"
    older_empty.mkdir()
    (older_empty / "manifest.json").write_text(
        json.dumps(
            {"techne_version": "0.15.0", "created_at": "2026-01-02T00:00:00Z", "databases": {}}
        )
    )

    removed, failed = prune_backup_sets(b, keep=0, protect=fresh)

    assert failed == []
    assert fresh.exists(), "protect does not cover the empty-set reclamation path"
    assert removed == [older_empty]
    assert json.loads((fresh / "manifest.json").read_text())["skipped_absent"], (
        "the surviving set no longer records which domains were absent"
    )


def test_an_unrestorable_set_never_takes_a_slot_from_a_restorable_one(tmp_path):
    """`holds_data = bool(dbs)` TRUSTED THE MANIFEST'S LIST (Prudence, #755 round 8,
    Important). `read_manifest` additionally requires every listed file to EXIST, so a set
    whose `.db` had gone still looked like a backup, took a keep slot, and — being newer —
    evicted a genuine one. Her measurement: `keep=1` removed the restorable set and kept the
    one `restore` raises `RestoreError: backup set is incomplete` on.

    The two predicates coincide for every set techne has ever written, which is why the
    whole suite could not see it; reaching this state needs an external cause — a partial
    copy, a half-finished sync, someone reclaiming space by hand."""
    b = tmp_path / "backups"
    good = _set(b, "backup-20260101-000000", "2026-01-01T00:00:00Z")
    broken = _set(b, "backup-20260102-000000", "2026-01-02T00:00:00Z")  # newer
    (broken / "techne.db").unlink()  # something outside techne took it

    with pytest.raises(RestoreError):
        read_manifest(broken)

    removed, failed = prune_backup_sets(b, keep=1)

    assert good.exists(), "an unrestorable set evicted the restorable one"
    assert removed == [], f"pruned against a set restore refuses: {removed}"
    assert [p.name for p, _ in failed] == [broken.name]
    assert "not there" in failed[0][1]
    assert broken.exists(), "deleted a set that still holds real database files"


# --- manifest SHAPE, not just validity (techne#759) ---------------------------
#
# `read_manifest` is the gate that decides whether a directory is a restorable
# backup set, and its docstring promises RestoreError on a malformed one. Valid
# JSON says nothing about shape. Each case below tracebacked before the fix.
#
# NOT A CLOSED SET, and the first attempt claimed it was. Round 1 enumerated by
# SYMPTOM -- the two `.get()` calls that raised AttributeError -- and walked past a
# third on the same line: `meta.get("file", ...)` returns a non-string happily and
# fails one operator later on `Path.__truediv__` as a TypeError. Different
# exception, so a symptom-shaped search could not see it (Prudence, #777 I1).
# Enumerate by EXPRESSION, not by traceback type, if this list ever grows again.

@pytest.mark.parametrize(
    "label,payload,expected",
    [
        ("databases dict-of-strings", {"databases": {"techne": "techne.db"}}, "'techne'"),
        ("databases dict-of-None", {"databases": {"techne": None}}, "'techne'"),
        ("databases dict-of-lists", {"databases": {"techne": ["techne.db"]}}, "'techne'"),
        # MIRROR THE TOP LEVEL. Round 2 added number/null/bool up there and did not
        # mirror them one level down, which is how a fifteen-element set was counted
        # as thirteen — and left `bool` with no witness anywhere in the suite, so
        # `isinstance(meta, (dict, bool))` passed the whole suite green
        # (Prudence, #777 r3).
        ("databases dict-of-numbers", {"databases": {"techne": 7}}, "'techne'"),
        ("databases dict-of-bools", {"databases": {"techne": True}}, "'techne'"),
        ("manifest is a JSON array", [{"databases": {}}], "expected a JSON object"),
        ("manifest is a JSON string", "techne", "expected a JSON object"),
        # The remaining top-level shapes, so all THIRTEEN that tracebacked pre-fix
        # are pinned rather than ten of them (Prudence, #777 r2 Nit 2).
        ("manifest is a JSON number", 7, "expected a JSON object"),
        ("manifest is JSON null", None, "expected a JSON object"),
        ("manifest is a JSON bool", True, "expected a JSON object"),
        # `file` present but not a string: `.get` returns it, `Path / it` explodes.
        ("file is an int", {"databases": {"techne": {"file": 7}}}, "'file' for 'techne'"),
        ("file is None", {"databases": {"techne": {"file": None}}}, "'file' for 'techne'"),
        ("file is a list", {"databases": {"techne": {"file": ["t.db"]}}}, "'file' for 'techne'"),
        ("file is a dict", {"databases": {"techne": {"file": {"n": "t"}}}}, "'file' for 'techne'"),
        ("file is a bool", {"databases": {"techne": {"file": True}}}, "'file' for 'techne'"),
        ("file is empty", {"databases": {"techne": {"file": "  "}}}, "'file' for 'techne'"),
    ],
)
def test_a_malformed_manifest_raises_restore_error_not_a_traceback(
    tmp_path, label, payload, expected
):
    (tmp_path / "manifest.json").write_text(json.dumps(payload))
    with pytest.raises(RestoreError, match=expected):
        read_manifest(tmp_path)


def test_the_malformed_manifest_message_names_what_it_found(tmp_path):
    """The message has to be actionable: which entry, and what was there instead.

    'malformed manifest' alone sends the operator to open the file and guess;
    naming the domain and the type found points at the line to fix.
    """
    (tmp_path / "manifest.json").write_text(json.dumps({"databases": {"librarian": 7}}))
    with pytest.raises(RestoreError) as excinfo:
        read_manifest(tmp_path)
    msg = str(excinfo.value)
    assert "librarian" in msg, "the failing entry is not named"
    assert "int" in msg, "the type actually found is not named"


def test_a_malformed_file_value_does_not_kill_the_whole_retention_run(tmp_path):
    """THE PAIR, ONE LEVEL DEEPER (Prudence, #777 I2).

    `_classify_backup_set` carried its own copy of `meta.get("file", ...)`, so a
    guard added only to `read_manifest` did not protect it. A non-string `file`
    raised TypeError out of `prune_backup_sets` ENTIRELY — retention died for every
    set, not just the unreadable one, routing around the `failed` channel the pruner
    built so an unprunable set is reported rather than fatal. Measured before the
    fix: with keep=0 and one bad set present, a perfectly good set was left on disk,
    which is the disk-filling cron job `prune_backup_sets`' own docstring exists to
    prevent.

    Both readers now go through `_manifest_db_filename`, so they cannot drift again.
    """
    backups = tmp_path / "backups"
    good = backups / "backup-20260101-000000"
    good.mkdir(parents=True)
    (good / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": "2026-01-01T00:00:00Z",
                "techne_version": "0.15.0",
                "databases": {"techne": {"file": "techne.db"}},
            }
        )
    )
    (good / "techne.db").write_text("x")

    bad = backups / "backup-20260201-000000"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": "2026-02-01T00:00:00Z",
                "techne_version": "0.15.0",
                "databases": {"techne": {"file": 7}},
            }
        )
    )

    removed, failed = prune_backup_sets(backups, keep=0)

    assert bad.exists(), "pruned a set techne cannot read as a backup"
    assert [p for p, _ in failed] == [bad], "the unreadable set was not REPORTED"
    assert removed == [good], "retention did not run for the readable set"
    # And the pair stays honest: restore refuses the same set.
    with pytest.raises(RestoreError, match="'file' for 'techne'"):
        read_manifest(bad)
