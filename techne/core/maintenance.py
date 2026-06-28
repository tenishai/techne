"""Cross-DB maintenance: fsck + all-DBs-together backup/restore (techne#139).

techne is db-per-domain (``techne.db`` personal memory, ``librarian.db`` documents,
``codex.db`` code graph, ``architect.db`` patterns). Two SQLite files can't share a
transaction, so cross-DB invariants — a document_card's ``doc:<id>`` resolving in
librarian.db, say — are kept by after-the-fact reconciliation, not guaranteed. Two
operational consequences this module makes first-class:

1. **fsck** — ``run_fsck`` runs every cross-DB consistency check as one pass and returns a
   structured report (``ok`` False if anything is inconsistent), so an operator/CI can
   verify the whole constellation with one command.

2. **All-or-nothing backup/restore** — because the invariants span files, a backup or
   restore of ONE domain alone desyncs the pointers. ``backup_all_dbs`` snapshots every
   present domain DB together (consistent online copy per file) into one timestamped set;
   ``restore_all_dbs`` writes a set back over the live DBs as a unit, taking a pre-restore
   snapshot first. RESTORING A SINGLE DB BY HAND IS THE THING THIS EXISTS TO PREVENT —
   always back up and restore the whole set.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from techne.config import Config
from techne.core.stores import store_path

# The domains and how their db path is resolved from config WITHOUT instantiating the store
# (store creation would migrate/CREATE an absent DB — fsck/backup must not do that).
_DOMAINS = ("librarian", "codex", "architect")

# The sidecars a WAL-mode SQLite db leaves alongside the main file. On restore they MUST be
# removed so a stale WAL isn't replayed over the freshly-copied database.
_WAL_SIDECARS = ("-wal", "-shm")

_MANIFEST_NAME = "manifest.json"


def domain_db_paths(config: Config) -> dict[str, Path]:
    """Map every domain → its resolved db path (existing or not). ``techne`` is the
    personal-memory db (``config.db_path``); the rest resolve via ``store_path`` so an
    absent domain is just a non-existent path, never created here."""
    paths = {"techne": Path(config.db_path)}
    for domain in _DOMAINS:
        paths[domain] = store_path(config, domain)
    return paths


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# fsck — cross-DB consistency
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    ok: bool
    summary: str
    details: dict = field(default_factory=dict)


@dataclass
class FsckReport:
    ok: bool
    checks: list[CheckResult]
    db_present: dict[str, bool]


def _check_dangling_cards(config: Config, present: dict[str, bool]) -> CheckResult:
    """The one true cross-DB invariant today: each active document_card in techne.db points
    (``doc:<id>``) at a row that resolves in librarian.db, and the doc still hashes to the
    card's ``ver:``. ``find_dangling_cards`` reports both dangling (unresolvable) and stale.
    Skipped (ok) when librarian.db doesn't exist — no documents domain, nothing to dangle."""
    if not present.get("librarian"):
        return CheckResult("dangling_cards", True, "skipped — no librarian.db")
    from techne.core.cards import find_dangling_cards

    rep = find_dangling_cards(config)
    n_dangling, n_stale = len(rep["dangling"]), len(rep["stale"])
    ok = n_dangling == 0 and n_stale == 0
    summary = (
        f"{rep['total']} cards: {n_dangling} dangling, {n_stale} stale"
        if not ok
        else f"{rep['total']} cards: all resolve"
    )
    return CheckResult("dangling_cards", ok, summary, details=rep)


# Registry of cross-DB checks. Each is (config, present) -> CheckResult. Add codex/pattern
# cross-references here as they gain genuine cross-DB pointers (today codex/architect
# "unresolved" edges are an intra-DB, by-design v1 state, NOT an inconsistency).
_CHECKS = (_check_dangling_cards,)


def run_fsck(config: Config) -> FsckReport:
    """Run every cross-DB consistency check. ``ok`` is False iff any check failed."""
    present = {name: p.exists() for name, p in domain_db_paths(config).items()}
    checks = [check(config, present) for check in _CHECKS]
    return FsckReport(ok=all(c.ok for c in checks), checks=checks, db_present=present)


# ---------------------------------------------------------------------------
# Backup — all present domain DBs, together
# ---------------------------------------------------------------------------


@dataclass
class BackupResult:
    dest: Path
    backed_up: list[str]
    skipped_absent: list[str]


def _online_copy(src: Path, dst: Path) -> None:
    """Consistent copy of a (possibly live) SQLite db via the online backup API — produces
    a single self-contained file with no WAL sidecar, even under concurrent writes."""
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def backup_all_dbs(config: Config, dest_dir: Path | None = None) -> BackupResult:
    """Snapshot every PRESENT domain DB together into one timestamped directory.

    Each db is copied via the online backup API (consistent even if written concurrently),
    and a manifest records the set so a restore can validate it. Absent domains are skipped
    (recorded in the manifest), not created. Returns the destination + which domains landed.
    """
    if dest_dir is None:
        dest_dir = Path(config.data_dir) / "backups" / f"backup-{_now_stamp()}"
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    paths = domain_db_paths(config)
    backed_up: list[str] = []
    skipped: list[str] = []
    manifest_dbs: dict[str, dict] = {}
    for domain, src in paths.items():
        if not src.exists():
            skipped.append(domain)
            continue
        out = dest_dir / f"{domain}.db"
        _online_copy(src, out)
        backed_up.append(domain)
        manifest_dbs[domain] = {"file": out.name, "size_bytes": out.stat().st_size}

    from techne import __version__

    manifest = {
        "techne_version": __version__,
        "created_at": _now_iso(),
        "databases": manifest_dbs,
        "skipped_absent": skipped,
    }
    (dest_dir / _MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")
    return BackupResult(dest=dest_dir, backed_up=backed_up, skipped_absent=skipped)


# ---------------------------------------------------------------------------
# Restore — write a backup set back over the live DBs, as a unit
# ---------------------------------------------------------------------------


class RestoreError(Exception):
    """A backup set could not be validated or restored."""


@dataclass
class RestoreResult:
    restored: list[str]
    pre_restore_backup: Path | None


def read_manifest(src_dir: Path) -> dict:
    """Load + validate a backup set's manifest. Raises RestoreError if absent/malformed or a
    listed db file is missing — so restore validates the WHOLE set before touching anything."""
    src_dir = Path(src_dir)
    mpath = src_dir / _MANIFEST_NAME
    if not mpath.exists():
        raise RestoreError(f"no {_MANIFEST_NAME} in {src_dir} — not a techne backup set")
    try:
        manifest = json.loads(mpath.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RestoreError(f"unreadable manifest: {e}") from e
    dbs = manifest.get("databases")
    if not isinstance(dbs, dict) or not dbs:
        raise RestoreError("manifest lists no databases")
    for domain, meta in dbs.items():
        f = src_dir / meta.get("file", f"{domain}.db")
        if not f.exists():
            raise RestoreError(f"backup set is incomplete: {f.name} missing for '{domain}'")
    return manifest


def restore_all_dbs(
    config: Config, src_dir: Path, *, pre_restore_backup: bool = True
) -> RestoreResult:
    """Write a backup set back over the live domain DBs, as a unit.

    ALL-OR-NOTHING: the set is validated up front (``read_manifest``) before any live file is
    touched, so a bad set aborts with the live DBs untouched. A pre-restore snapshot of the
    CURRENT live state is taken first (unless disabled) so a restore is itself reversible.
    For each db: copy the backup file over the live path, then delete the live ``-wal``/
    ``-shm`` sidecars so SQLite can't replay a stale WAL over the restored file.

    Caller MUST ensure no other techne process holds the DBs open during a restore.
    """
    src_dir = Path(src_dir)
    manifest = read_manifest(src_dir)  # validates the whole set first

    pre: Path | None = None
    if pre_restore_backup:
        pre = backup_all_dbs(
            config, Path(config.data_dir) / "backups" / f"pre-restore-{_now_stamp()}"
        ).dest

    live = domain_db_paths(config)
    restored: list[str] = []
    for domain, meta in manifest["databases"].items():
        if domain not in live:
            # A backup from a future techne with a new domain — skip rather than guess a path.
            continue
        target = live[domain]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_dir / meta.get("file", f"{domain}.db"), target)
        for sidecar in _WAL_SIDECARS:
            wal = target.with_name(target.name + sidecar)
            if wal.exists():
                wal.unlink()
        restored.append(domain)
    return RestoreResult(restored=restored, pre_restore_backup=pre)
