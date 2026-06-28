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
import os
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


def _unique_dir(path: Path) -> Path:
    """``path`` if free, else ``path-2``/``path-3``/… — so two auto-named backups (or
    pre-restore snapshots) in the same wall-clock second can't silently overwrite each
    other (the prior set is exactly what a restore-gone-wrong needs)."""
    if not path.exists():
        return path
    i = 2
    while (cand := path.with_name(f"{path.name}-{i}")).exists():
        i += 1
    return cand


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
    Skipped (ok) when librarian.db doesn't exist — no documents domain, nothing to dangle.

    A DANGLING card (pointer doesn't resolve) is a structural fault → fail. A STALE card
    (resolves, but the doc drifted from the card's ``ver:``) still works — it's advisory, so
    it does NOT fail fsck (else a routine doc edit flips CI red); refresh it via the card
    summary pass."""
    if not present.get("librarian"):
        return CheckResult("dangling_cards", True, "skipped — no librarian.db")
    # The present-check above is LOAD-BEARING: find_dangling_cards opens librarian_store,
    # which would CREATE+migrate an absent librarian.db. Never call it when absent.
    from techne.core.cards import find_dangling_cards

    rep = find_dangling_cards(config)
    n_dangling, n_stale = len(rep["dangling"]), len(rep["stale"])
    ok = n_dangling == 0  # stale is advisory, not a hard fail
    parts = []
    if n_dangling:
        parts.append(f"{n_dangling} DANGLING")
    if n_stale:
        parts.append(f"{n_stale} stale (advisory)")
    summary = f"{rep['total']} cards: " + (", ".join(parts) if parts else "all resolve")
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
        dest_dir = _unique_dir(Path(config.data_dir) / "backups" / f"backup-{_now_stamp()}")
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
    skipped_unknown: list[str] = field(default_factory=list)


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
    """Write a backup set back over the live domain DBs, as close to a unit as a filesystem
    allows.

    Fail-safe ordering, to keep a restore from leaving a half-applied cross-DB desync:
      1. ``read_manifest`` validates the WHOLE set before any live file is touched — a bad
         set aborts with live untouched.
      2. A pre-restore snapshot of the CURRENT live state is taken (unless disabled), so the
         restore is itself reversible.
      3. STAGE: copy every source db to a ``<target>.restore-tmp`` sibling. Copying is the
         failure-prone step (disk full, EIO); doing it ALL before touching any live file
         means such a failure aborts with live untouched (temps cleaned up).
      4. COMMIT: per db, delete the live ``-wal``/``-shm`` FIRST (so SQLite can't replay a
         stale WAL over the new file — the corruption window if done after), then
         ``os.replace`` the staged temp into place (atomic, near-instant — a failure here is
         very unlikely and is reported with the pre-restore location to recover from).

    Caller MUST ensure no other techne process holds the DBs open during a restore —
    overwriting a db under a live WAL-mode connection is undefined and that process may later
    checkpoint its WAL back over the restored file. (v1 stance: documented, not enforced.)
    """
    src_dir = Path(src_dir)
    manifest = read_manifest(src_dir)  # validates the whole set first

    pre: Path | None = None
    if pre_restore_backup:
        pre = backup_all_dbs(
            config, _unique_dir(Path(config.data_dir) / "backups" / f"pre-restore-{_now_stamp()}")
        ).dest

    live = domain_db_paths(config)
    plan: list[tuple[str, Path, Path, Path]] = []  # (domain, src_file, target, temp)
    skipped_unknown: list[str] = []
    for domain, meta in manifest["databases"].items():
        if domain not in live:  # a backup from a future techne with a new domain
            skipped_unknown.append(domain)
            continue
        target = live[domain]
        src_file = src_dir / meta.get("file", f"{domain}.db")
        plan.append((domain, src_file, target, target.with_name(target.name + ".restore-tmp")))

    # STAGE — copy all sources to temps beside their targets. Live is UNTOUCHED until commit.
    try:
        for _domain, src_file, target, temp in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, temp)
    except OSError as e:
        for *_, temp in plan:
            temp.unlink(missing_ok=True)
        msg = f"restore staging failed ({e}); live DBs untouched."
        if pre:
            msg += f" Prior state: {pre}"
        raise RestoreError(msg) from e

    # COMMIT — corruption-safe order: drop stale WAL sidecars, then atomic rename into place.
    restored: list[str] = []
    try:
        for domain, _src_file, target, temp in plan:
            for sidecar in _WAL_SIDECARS:
                target.with_name(target.name + sidecar).unlink(missing_ok=True)
            os.replace(temp, target)
            restored.append(domain)
    except OSError as e:
        msg = (
            f"restore PARTIALLY applied (committed {restored}, failed on the next db: {e}). "
            f"Cross-DB state may be inconsistent — restore from the pre-restore snapshot"
            + (f" at {pre}" if pre else " (none taken)")
            + " or re-run."
        )
        raise RestoreError(msg) from e

    return RestoreResult(
        restored=restored, pre_restore_backup=pre, skipped_unknown=skipped_unknown
    )
