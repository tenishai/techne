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
import logging
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from techne.config import Config
from techne.core.stores import store_path

logger = logging.getLogger(__name__)

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
    #: The command that fixes it. Checks REPORT, they never self-heal — the same
    #: rule core/checks.py states, and until techne#684 this dataclass could only
    #: obey half of it: fsck could name a structural fault and offer the operator
    #: nothing to do about it. Four dangling cards sat red from June to August
    #: because of that, on a check documented as "usable in scripts/CI" and
    #: exiting non-zero. A check that can never go green trains the operator to
    #: ignore it — the invariant core/checks.py already carries, arrived at here
    #: from the other direction.
    remedy: str | None = None


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
    # Only the no-pointer case has a mechanical repair, so only name one when it is
    # the case in hand — offering `cards repair` for a `missing` pointer would
    # promise a fix that command deliberately refuses to perform.
    remedy = None
    if any(d.get("reason") == "no doc pointer" for d in rep["dangling"]):
        remedy = "techne cards repair   (retypes cards that never had a pointer; techne#684)"
    elif n_dangling:
        remedy = "restore librarian.db or supersede the card — a missing pointer needs a human"
    return CheckResult("dangling_cards", ok, summary, details=rep, remedy=remedy)


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


def backup_all_dbs(
    config: Config, dest_dir: Path | None = None, *, label: str = "backup"
) -> BackupResult:
    """Snapshot every PRESENT domain DB together into one timestamped directory.

    Each db is copied via the online backup API (consistent even if written concurrently),
    and a manifest records the set so a restore can validate it. Absent domains are skipped
    (recorded in the manifest), not created. Returns the destination + which domains landed.
    """
    # `dest_dir` IS THE BACKUPS ROOT, NOT THE SET — which is what `--out`'s help text has
    # always said ("Destination dir (default: <data_dir>/backups/)") and what this code did
    # not do (Prudence, #755 Critical 1). Treating a supplied path AS the set made
    # `res.dest.parent` — the directory `--keep` prunes — one the operator never named.
    # Reproduced before the fix: `--out ~/techne-backup --keep 1` rmtree'd a sibling
    # project directory that happened to contain a `manifest.json`. That is not a
    # techne-private filename and `rmtree` is not recoverable.
    #
    # It also made `--keep` USELESS where it was pointed: every `--out` run wrote into the
    # same directory, so there was only ever one set there to prune.
    #
    # BREAKING, DELIBERATELY, ON RICHARD'S CALL. A script doing `backup --out X` then
    # `restore X` now needs `restore X/backup-<stamp>`. The alternative was to keep a
    # flag whose documented meaning and behaviour disagree, which is what turned into a
    # deletion. `restore` accepts the set path it always did; `--out` now yields a root
    # containing timestamped sets, which is what makes `--out` usable on a timer at all.
    # `label` NAMES THE SET, AND IS WHAT KEEPS RETENTION ABLE TO SEE IT. Making `dest_dir`
    # the ROOT (above) broke the one internal caller that had been naming its own set:
    # `restore_all_dbs`' pre-restore snapshot became `backups/pre-restore-<stamp>/backup-
    # <stamp>/`, and `prune_backup_sets` only scans DIRECT children for a manifest — so the
    # set went invisible to `--keep` and every restore leaked a full copy of every domain db
    # that retention could never reclaim. Demonstrated: `keep=0` removed the ordinary set and
    # left the nested one. Labelling here restores the pre-break on-disk layout
    # (`backups/pre-restore-<stamp>/`, manifest at the top) under the new root semantics.
    # Internal only: not reachable from the CLI, so it is not a path-injection surface.
    # AN INVARIANT, NOT A COMMENT (Prudence, #755 Nit 8). `root / f"{label}-..."` is safe
    # today because of a property of the two CALLERS, not of this function — and a comment
    # asserting that sits where the next caller's author reads it as a description rather
    # than a constraint they must uphold. A `label` carrying a separator or a leading dot
    # escapes the root, and the failure would be SILENT because it still creates a
    # plausible directory. Cheap to make impossible instead.
    if not label or label.startswith(".") or set(label) & {"/", "\\"} or ".." in label:
        raise ValueError(f"label must be a bare directory-name fragment; got {label!r}")
    root = Path(dest_dir) if dest_dir is not None else Path(config.data_dir) / "backups"
    dest_dir = _unique_dir(root / f"{label}-{_now_stamp()}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    paths = domain_db_paths(config)
    backed_up: list[str] = []
    skipped: list[str] = []
    manifest_dbs: dict[str, dict] = {}
    # A FAILED BACKUP USED TO LEAVE A SET NOTHING WOULD EVER RECLAIM (Prudence, #755 round 5
    # Important 1). The manifest is written LAST — correctly, since a set is not a set until
    # it is complete — so a `^C` partway through left `backups/<label>-<stamp>/` holding a
    # full copy of a live domain db and no manifest. `_classify_backup_set` then read it as
    # "no manifest, not ours" and stayed silent, forever. Measured: at `keep=0` the pruner
    # removed the REAL set and left the orphan, `failed=[]`. techne wrote it, techne named
    # it, and techne would neither reclaim nor mention it.
    #
    # `BaseException`, not `Exception`: KeyboardInterrupt and SystemExit are the likely ways
    # this is interrupted and neither is an Exception. The partial set is removed and the
    # original raised — housekeeping must not swallow the reason the backup stopped.
    try:
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
        # ATOMIC, BECAUSE `write_text` TRUNCATES BEFORE IT WRITES (Prudence, #755 round 6
        # Important 1). Mode `'w'` empties the file first, so a kill inside that window left
        # a ZERO-BYTE manifest.json — which parses as neither absent nor valid, and fell
        # into a branch that answered "not ours". Writing to a temp name in the same
        # directory and `os.replace`-ing it means the manifest is either wholly absent or
        # wholly there: the truncated state stops existing rather than being detected.
        tmp_manifest = dest_dir / f"{_MANIFEST_NAME}.tmp"
        tmp_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
        os.replace(tmp_manifest, dest_dir / _MANIFEST_NAME)
    except BaseException:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return BackupResult(dest=dest_dir, backed_up=backed_up, skipped_absent=skipped)


#: The one shape `_now_iso()` emits, and the only one that sorts chronologically as
#: text. See the format note inside `prune_backup_sets`.
_CANONICAL_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

#: The shape `_unique_dir(root / f"{label}-{_now_stamp()}")` produces. Used ONLY to decide
#: whether a manifest-less directory is one WE left behind — never to decide what to delete.
_OUR_SET_NAME = re.compile(r".+-\d{8}-\d{6}(-\d+)?$")

#: THE DISTINGUISHING PHRASE OF EVERY REASON RETENTION REFUSES A SET, and the single source
#: for both the messages below and the exit-3 table in `docs/deployment.md`.
#:
#: Derived rather than retyped, which is the rule #498 set for the other cross-process
#: contract in this repo (its test reads the value from `exit_codes.py` "rather than
#: retyping 78"). The hazard here is the one Prudence named on #755: the same person writes
#: the message and the doc row, so the two agree with each other and neither is checked
#: against reality. `test_every_retention_refusal_has_a_row_in_the_docs` closes that — add a
#: refusal without a row and it fails.
#:
#: Failures from the delete itself are NOT here: those carry the OS's own `strerror`, which
#: is why the table's first row deliberately describes the class instead of naming an errno.
RETENTION_REFUSAL_PHRASES = (
    "is a symlink",
    "manifest is unreadable",
    "is not YYYY-MM-DDTHH:MM:SSZ",
    "is in the future",
    "has no manifest",
    "manifest does not parse",
    "lists databases in a shape techne cannot read",
    "lists a database file that is not there",
)



def _manifest_db_filename(domain: str, meta: object) -> tuple[str | None, str | None]:
    """Resolve one manifest entry to its db filename. Returns (filename, problem).

    ONE resolver for the TWO readers of a manifest entry — `read_manifest` and
    `_classify_backup_set` — because they carried the same expression twice and a
    guard added to one did not protect the other (techne#759, Prudence #777 I2).
    That asymmetry is the whole thing `test_prune_leaves_a_set_restore_cannot_read
    _alone` exists to prevent, so the fix is a shared resolver rather than a second
    copy of the same isinstance check.

    `meta.get("file", ...)` DOES NOT RAISE on a non-string value — it returns it,
    and the failure lands one operator later on `Path.__truediv__` as a TypeError.
    Enumerating by symptom (`AttributeError`) walked straight past it. Measured
    before the guard: int / None / list / dict / bool all reached
    `unsupported operand type(s) for /: 'PosixPath' and ...`.

    `maintenance.py`'s writer always emits `{"file": <str>, "size_bytes": <int>}`,
    so a non-string `file` means a foreign, hand-edited or corrupted manifest —
    exactly the class the surrounding guards already exist for.
    """
    if not isinstance(meta, dict):
        return None, (
            f"entry for '{domain}' should be an object, found {type(meta).__name__}"
        )
    name = meta.get("file", f"{domain}.db")
    # bool is an int subclass but not a str, so it is caught here too.
    if not isinstance(name, str) or not name.strip():
        return None, (
            f"'file' for '{domain}' should be a non-empty string, "
            f"found {type(name).__name__}"
        )
    return name, None


@dataclass(frozen=True)
class SetClass:
    """What one directory under `backups/` is, in the three terms retention needs.

    THE COLLAPSE THIS TYPE EXISTS TO PREVENT (Prudence, #755 round 7, Critical). Round 6
    made an empty set "ours" so retention could reclaim it — and "ours" was ALSO what made
    a set count toward the keep window. The empty set carries the newest stamp, so it took
    a slot and evicted a real backup, every run, on the schedule. Measured through the real
    CLI: three genuine sets, `--keep 2`, empty source store, five nights — every real
    backup destroyed. A leak traded for a deletion, which is the worse direction, and the
    leak was tiny: an empty set is a directory and a small JSON file.

      `stamp`       when it was written, if we can order it safely.
      `refusal`     why retention will not act on it, if so — surfaced to the operator.
      `holds_data`  whether it contains any database. AN EMPTY SET IS OURS AND IS NOT A
                    BACKUP: reclaimable because it holds nothing, and never eligible to
                    occupy a retention slot for the same reason.

    "Is this ours?" and "is this a backup?" are different questions. Answering them with
    one value is what caused the Critical.
    """

    stamp: str | None
    refusal: str | None
    holds_data: bool = False


def _classify_backup_set(child: Path) -> SetClass:
    """Classify one directory under `backups/` — returns a three-field `SetClass`.

    THREE OUTCOMES, AND COLLAPSING TWO OF THEM IS WHAT WENT WRONG (Prudence, #755 round 3
    Important 1). The previous version returned a bare `None` for both "not ours, say
    nothing" and "ours, but we cannot order it, so it is never reclaimed" — and only the
    first should be silent. Measured through the real CLI: seven unorderable sets under
    `--keep 1` gave exit 0, no `retention INCOMPLETE`, and eight sets on disk. That is the
    disk filling on a schedule while the timer reads green, which is the exact failure
    `--keep` exists to prevent, reintroduced one level up by the fix for Nit 4.

      (None, None)      not ours — stay silent. A foreign directory under a root the
                        operator pointed us at is not our business to report or delete.
      (None, reason)    ours (or ours-shaped) and NOT RECLAIMABLE — the caller must
                        surface it, because nothing else will and it accumulates forever.
      (stamp, None)     ours and safely orderable.

    A `logger.warning` is not a substitute for the middle case: there is no logging
    configuration anywhere in `techne/`, so those records go nowhere by default, and the
    `extra=` fields do not render into the default format even when they do.
    """
    # A SYMLINK IS REFUSED BEFORE IT IS DATED (Prudence, #755 round 3 Important 2). The
    # symlink check used to live in the delete loop only, so the scan loop still admitted
    # a symlinked set and ordered it BY ITS TARGET'S STAMP. A symlink pointing at a set
    # NEWER than a real one therefore took a slot inside the keep window and pushed a real
    # backup out of it: measured `removed: ['backup-a']`, `failed: []` — a genuine backup
    # deleted, nothing reported. The old test covered only the mirror case (target older,
    # so the symlink sorted last and merely failed to delete), which is why this half hid.
    if child.is_symlink():
        return SetClass(
            None, f"{RETENTION_REFUSAL_PHRASES[0]} — refusing to remove it or its target"
        )
    try:
        manifest = json.loads((child / _MANIFEST_NAME).read_text())
    except FileNotFoundError:
        # THE RULE THIS FUNCTION ACTUALLY FOLLOWS, stated once because the previous comment
        # justified silence with "delete nothing you cannot read" — the right answer by a
        # route that gives the WRONG one a screen further down, where round 3 already ruled
        # that reasoning insufficient (Prudence, #755 round 5, question A):
        #
        #   A CONCLUSIVE "NOT OURS" IS SILENT. AN INCONCLUSIVE TEST IS REPORTED.
        #
        # Every silent branch below is the identity test COMPLETING and answering no. Every
        # reported one is that test failing to run, or running and answering yes.
        #
        # A missing manifest is conclusive — unless we NAMED the directory. Then it is one
        # of ours that never finished: the manifest is written last, so an interrupted
        # backup leaves exactly this. In-process failures are cleaned up by
        # `backup_all_dbs`, but a SIGKILL or a power cut runs no handler, and the orphan
        # holds a full copy of a live db that nothing would otherwise reclaim or mention.
        if _OUR_SET_NAME.fullmatch(child.name):
            # SAY WHAT WAS OBSERVED, NOT WHAT IT MEANS (Prudence, #755 round 6 Important 2).
            # This message used to read "techne named this directory, so it is an interrupted
            # backup" — an identity claim a NAME REGEX CANNOT MAKE. It cannot separate three
            # cases: a backup in flight right now, one that died, and a foreign directory
            # that happens to end in a timestamp (`photos-20240101-000000` matches, measured).
            # The code deletes none of them, which is right — but the message was licensing a
            # human to, and the operator deleting a directory techne never created is
            # round 1's Critical committed by hand instead of by `rmtree`.
            return SetClass(None, (
                f"{RETENTION_REFUSAL_PHRASES[4]} — a backup in progress, an interrupted one, "
                "and a foreign directory are indistinguishable here; it is never pruned"
            ))
        return SetClass(None, None)  # not named by us, no manifest: conclusively not ours
    except ValueError:
        # THE RULE ABOVE DECIDES THIS ONE TOO, and this branch was still carrying the old
        # reasoning a line after the new rule was written (Prudence, #755 round 6 Important
        # 1). A manifest that will not parse, inside a directory whose name matches the
        # shape we create, is the identity test FAILING TO COMPLETE — the reported side.
        # The truncation window that made this reachable from our own writes is closed
        # above; what remains is corruption, which is exactly the inconclusive case.
        if _OUR_SET_NAME.fullmatch(child.name):
            return SetClass(None, (
                f"{RETENTION_REFUSAL_PHRASES[5]} — so whether this is a usable backup "
                "cannot be determined here; it is never pruned"
            ))
        return SetClass(None, None)  # unparseable, nothing about it claims to be ours
    except OSError as exc:
        # AN UNREADABLE MANIFEST IS NOT THE SAME AS AN ABSENT ONE (Prudence, #755 round 4
        # Important 1). Folding every OSError into silence meant a set TECHNE ITSELF WROTE
        # — canonical stamp, real db files — went permanently quiet and permanently
        # unpruned the moment its manifest became unreadable. Measured through the real
        # CLI: seven such sets at `--keep 1` gave exit 0, no `retention INCOMPLETE`, and
        # eight sets on disk — character-for-character the failure round 3 closed for
        # stamps. The stamp door was shut and this one left open.
        #
        # Her decisive argument, which is why this is not a judgement call: REACHABILITY IS
        # SYMMETRIC. `_now_iso()` is the sole writer of `created_at` and always emits
        # canonical, so a bad stamp is no more techne-producible than a chmod'd manifest.
        # Treating one as a leak worth reporting and the other as none of our business had
        # no basis. A permission bit is also the more likely of the two in practice.
        return SetClass(None, (
            f"{RETENTION_REFUSAL_PHRASES[1]} ({exc.strerror or exc}) — it cannot be "
            "judged, so the set is never pruned"
        ))
    if not isinstance(manifest, dict):
        return SetClass(None, None)
    dbs = manifest.get("databases")
    # AN EMPTY SET IS STILL OURS — the sixth case, and it arrives through the SUCCESS path
    # rather than any interruption (found answering my own round-6 question B, which is the
    # completeness-claim shape I got wrong in round 5). When every domain db is absent,
    # `backup_all_dbs` writes a COMPLETE set with `"databases": {}`, and the identity test
    # below then answered "not ours" about a set techne had just written. Measured: five
    # nightly runs, five sets on disk, `removed=[]`, `failed=[]`, exit 0.
    #
    # `techne_version` is the honest identity key. `read_manifest` keys on `databases`
    # because it is about to RESTORE from them, which is a different question — and the
    # round-2 fix imported its test wholesale without noticing that difference.
    #
    # SAFE FOR EVERY SET EVER WRITTEN, and checked by enumerating tags rather than by one
    # `git log -S` — which counts occurrence changes and so cannot see a rename-and-readd.
    # Every release from v0.8.0 (the first carrying `maintenance.py`) through v0.15.0 writes
    # `techne_version`; every tag before that has no backup command at all, so no manifest
    # of theirs exists to strand.
    #
    # THE STANDING HAZARD IS THE OTHER DIRECTION: rename this field and the sets it is the
    # ONLY identity evidence for — the empty ones — silently become "not ours", never pruned
    # and never mentioned. Populated sets survive such a rename via the `databases` test
    # below, so the blast radius is the empty ones specifically, not every set on disk.
    # (Measured by renaming the field and watching exactly the empty half of
    # `test_what_backup_writes_is_recognised_as_ours` fail — the claim that it strands
    # everything was wrong, and narrower is worth saying accurately: it is the case nobody
    # would think to test.)
    if "techne_version" not in manifest and (not isinstance(dbs, dict) or not dbs):
        # `read_manifest`'s identity test, for manifests that do NOT claim to be ours.
        # techne refusing to RESTORE from a directory
        # while being willing to DELETE it is not a defensible pair, and `manifest.json` +
        # `created_at` are among the least techne-private things there are — a foreign
        # `{"name": "my-extension", ...}` used to pass (round 2).
        return SetClass(None, None)
    if not isinstance(dbs, dict) or not all(
        isinstance(meta, dict) for meta in dbs.values()
    ):
        # THE ONE SHAPE THAT STILL GOT THROUGH (Prudence, #755 round 3 Nit 3). A
        # `databases` dict whose VALUES are strings/None/lists satisfied "non-empty dict"
        # here while `read_manifest` cannot read it at all — its `meta.get("file", ...)`
        # raises AttributeError rather than RestoreError. So round 2's asymmetry survived
        # in this narrow shape: the pruner called it ours and would rmtree it, restore
        # tracebacked on it. Narrow (it needs a manifest, a non-empty `databases`, AND a
        # canonical `...Z` stamp, all inside `backups/`) but the fix is one line.
        #
        # SILENT RATHER THAN REPORTED, and by the rule at the top of this function rather
        # than by "delete nothing you cannot read" — which is what this comment used to say
        # (Prudence, #755 round 5, question A). That phrasing reaches the right answer HERE
        # and the wrong one one screen up, where round 3 ruled it insufficient for an
        # unreadable manifest. The rule that covers both: this branch is the identity test
        # COMPLETING and answering no — a manifest whose `databases` values are not objects
        # never described a techne backup set, whatever else it is. Checked rather than
        # assumed: no techne version has written non-dict values (`27ac22f:202`, HEAD
        # `:240`), so this is conclusively someone else's file and not ours to report.
        #
        # `read_manifest`'s AttributeError on the same input is pre-existing and reachable
        # from `techne restore <any dir>` — filed as #759 rather than grown into this PR.
        #
        # BUT NOT SILENT WHEN IT CLAIMS TO BE OURS (Prudence, #755 round 7). A manifest
        # carrying `techne_version` with unreadable `databases` is the identity test saying
        # YES and the readability test failing — the inconclusive case, which this
        # function's own reachability-is-symmetric rule puts on the reported side. Silence
        # there contradicted the rule three screens above it.
        if "techne_version" in manifest:
            return SetClass(None, (
                f"{RETENTION_REFUSAL_PHRASES[6]} — it claims to be a techne backup and "
                "cannot be read as one; it is never pruned"
            ))
        return SetClass(None, None)
    stamp = str(manifest.get("created_at", ""))
    # THE SORT IS LEXICOGRAPHIC, SO THE FORMAT IS PART OF THE CONTRACT. `_now_iso()` is the
    # only writer and always emits `%Y-%m-%dT%H:%M:%SZ`, which sorts chronologically as
    # text. A stamp in another shape does not: "2025-06-01T23:00:00+02:00" (21:00Z) sorts
    # ABOVE "2025-06-01T22:00:00Z" while being an hour OLDER, so the newer set gets pruned.
    # The failure is a DELETED BACKUP, so a set we cannot order is never guessed at — but
    # it IS reported, because "never deleted" and "never mentioned" together is a leak.
    if not _CANONICAL_STAMP.fullmatch(stamp):
        return SetClass(None, (
            f"manifest created_at {stamp!r} {RETENTION_REFUSAL_PHRASES[2]} — it cannot "
            "be ordered safely, so it is never pruned and will accumulate"
        ))
    # A STAMP IN THE FUTURE IS THE SAME CATEGORY (round 2, Nit 4). With no upper bound on
    # the sort it sorts newest forever, holds a keep slot permanently and quietly makes
    # retention `keep - 1`. Reachable by ordinary clock skew, and `Persistent=true` fires a
    # catch-up run right after boot, before NTP may have synced.
    if stamp > _now_iso():
        return SetClass(None, (
            f"manifest created_at {stamp} {RETENTION_REFUSAL_PHRASES[3]} — it cannot be "
            "ordered against real time, so it is never pruned and holds a retention slot"
        ))
    # DELETION-SAFETY AND SLOT-ELIGIBILITY ARE STILL TWO QUESTIONS (Prudence, #755 round 8).
    # `holds_data` answers the first correctly — an empty set holds no database, so removing
    # it loses nothing. It is one value short of the second: `bool(dbs)` trusts the
    # manifest's LIST, while `read_manifest` also requires every listed file to EXIST. A set
    # whose `.db` has since gone therefore looked like a backup, took a keep slot, and —
    # being newer — evicted a genuine one. Demonstrated: `keep=1` removed the restorable set
    # and kept the one `restore` raises `RestoreError: backup set is incomplete` on.
    #
    # The two coincide for every set techne has ever written, which is why 3497 tests could
    # not see it; it needs an external cause (a partial copy, a half-finished sync, someone
    # reclaiming space by hand). Not a Critical for that reason — but it deletes a good
    # backup, which is why it is fixed here rather than filed.
    #
    # No fourth field is needed. An unrestorable set gets NO STAMP, so it can never occupy a
    # slot, and a REFUSAL, so it is reported rather than accumulating in silence.
    #
    # `holds_data=True` here is INERT TODAY and said so rather than left looking load-bearing
    # (Prudence, #755 round 9). The caller returns at `if cls.stamp is None: continue` before
    # it is read, so setting it False changes nothing observable — measured. It is written
    # because it is TRUE (the set does hold databases) and because the field's meaning is
    # "safe to delete unconditionally?", to which the honest answer here is no. A later
    # caller that reads `holds_data` without first checking `stamp` would otherwise get a
    # lie. What actually keeps this set out of the empty-set reclamation is the `None` stamp.
    # SAME RESOLVER AS `read_manifest`, deliberately. Before techne#759 this line
    # carried its own copy of `meta.get("file", ...)`, so a non-string `file` raised
    # TypeError out of `prune_backup_sets` entirely — killing retention for EVERY
    # set, not just the unreadable one, and routing around the `failed` channel this
    # function built so an unprunable set is REPORTED rather than fatal. Measured:
    # with keep=0 and one bad set present, a perfectly good set was left on disk.
    malformed: list[str] = []
    missing: list[str] = []
    for domain, meta in dbs.items():
        name, problem = _manifest_db_filename(domain, meta)
        if problem:
            malformed.append(domain)
        elif not (child / name).exists():
            missing.append(domain)
    if malformed:
        return SetClass(
            None,
            (
                f"{RETENTION_REFUSAL_PHRASES[6]} ({', '.join(sorted(malformed))}) — "
                "`restore` refuses it, so it is not counted as a backup and is never pruned"
            ),
            holds_data=True,
        )
    missing = sorted(missing)
    if missing:
        return SetClass(
            None,
            (
                f"{RETENTION_REFUSAL_PHRASES[7]} ({', '.join(missing)}) — `restore` refuses "
                "it, so it is not counted as a backup and is never pruned"
            ),
            holds_data=True,
        )
    # An empty set is OURS (so we can reclaim it) and is NOT A BACKUP (so it must never
    # occupy a retention slot).
    return SetClass(stamp, None, holds_data=bool(dbs))


def prune_backup_sets(
    backups_dir: Path, keep: int, *, protect: Path | None = None, apply_keep: bool = True
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Delete all but the newest *keep* backup sets. Returns (removed, failed).

    RETENTION IS WHY THIS EXISTS, AND WHY IT LANDS WITH THE SCHEDULING DOCS RATHER THAN
    AFTER (techne#742). `backup_all_dbs` had none, so `techne backup` was safe to run by
    hand and unsafe to put on a timer: a nightly job with no ceiling is a disk-filling
    cron job dressed as a backup story. Documenting the schedule without shipping the
    ceiling would be advice that breaks the machine it is given to.

    A SET IS ONLY A SET IF IT CARRIES A MANIFEST. Anything else under `backups/` is left
    alone — migration `.db.bak` files live there too, pruned by their own scheme in
    `migrator._prune_backups`, and a pruner that cannot tell what it is deleting should
    delete nothing. Same rule that pruner states: a file not matching the shape is never
    removed, deliberately.

    ORDERED BY THE MANIFEST'S `created_at`, NOT BY MTIME. A restore, a copy, or a backup
    tool touching the directory rewrites mtimes and would reorder the sets — evicting a
    genuine recent backup because something read it. The stamp inside the set is the fact;
    the filesystem's opinion about it is not.

    `protect` is never removed, even at keep=0 — deleting the set the caller is about to
    rely on is the failure this exists to prevent (the idiom is `migrator._prune_backups`').
    """
    if keep < 0:
        raise ValueError(f"keep must be >= 0; got {keep}")
    if not backups_dir.is_dir():
        return [], []

    protect = protect.resolve() if protect else None
    sets: list[tuple[str, Path]] = []
    empties: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for child in backups_dir.iterdir():
        if not child.is_dir():
            continue
        cls = _classify_backup_set(child)
        if cls.refusal is not None:
            # OURS AND UNRECLAIMABLE. Surfaced through the same channel as a failed
            # `rmtree`, because the operator-visible consequence is identical: a set under
            # `backups/` that retention will never remove.
            failed.append((child, cls.refusal))
        if cls.stamp is None:
            continue
        if cls.holds_data:
            sets.append((cls.stamp, child))
        else:
            # AN EMPTY SET IS OURS AND IS NOT A BACKUP (Prudence, #755 round 7, Critical).
            # It never enters `sets`, so it can never occupy a slot in the keep window —
            # which is exactly how round 6's fix destroyed real archives: the empty set
            # carried the newest stamp, took a slot, and evicted a genuine backup every
            # night. And it is reclaimed unconditionally rather than by the window, because
            # it holds no database: there is nothing in it to lose.
            empties.append(child)

    # TIE-BREAK ON NAME, BECAUSE TIES ARE REACHABLE (pre-pass on #755). `_now_iso()` has
    # ONE-SECOND resolution, so two sets written in the same second carry identical
    # stamps — reachable under a scripted or retried `--keep` run. With a stable sort and
    # no secondary key, which one survives is decided by `Path.iterdir()`, i.e. by the
    # filesystem. Demonstrated: three sets sharing a stamp at keep=1 kept whichever
    # iterdir happened to list first. The directory name carries the same stamp plus a
    # uniquifying suffix, so it is a deterministic tie-break rather than a meaningful one
    # — the point is that the answer stops depending on the filesystem.
    sets.sort(key=lambda pair: (pair[0], pair[1].name), reverse=True)
    removed: list[Path] = []
    # THE KEEP WINDOW APPLIES TO BACKUPS. Empty sets are reclaimed either way; real sets are
    # only weighed against `keep` when the caller says the window is meaningful. It is NOT
    # when the run that just finished captured nothing (`apply_keep=False`): deleting
    # archives on the strength of a run that backed up nothing is the same mistake as
    # letting an empty set hold a slot, one level up. The alarm must not follow the
    # irreversible step.
    doomed = list(empties) + ([path for _, path in sets[keep:]] if apply_keep else [])
    for path in doomed:
        if protect and path.resolve() == protect:
            continue
        if path.is_symlink():
            # A TOCTOU BACKSTOP, NOT THE PRIMARY CHECK — `_classify_backup_set` refuses
            # symlinks during the scan, so nothing symlinked reaches this loop unless the
            # directory became one in between. Kept rather than deleted because `rmtree`'s
            # own refusal raises `[Errno None] None`, a string naming neither the symlink
            # nor anything actionable, and this is the last place that can say what it is.
            # Not hypothetical under `backups/`: `migrator.py` records a dangling symlink
            # there once stopping the store from opening at all.
            failed.append(
                (path, f"{RETENTION_REFUSAL_PHRASES[0]} — refusing to remove it or its target")
            )
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            # NOT FAILING IS NOT THE SAME AS NOT TELLING (Prudence, #755 Important 2).
            # Housekeeping must not fail the backup it follows — but swallowing this
            # silently means a nightly `--keep 14` fills the disk exactly as it would with
            # no retention at all, and nothing anywhere says so. Returned to the caller to
            # report; this module does not print.
            failed.append((path, str(exc)))
            continue
        removed.append(path)
    return removed, failed


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
    # SHAPE BEFORE CONTENT, at BOTH levels (techne#759). This function is the gate
    # that decides whether a directory is a restorable set, and its contract is to
    # raise RestoreError -- a traceback here surfaces during a RESTORE, which is
    # exactly when a stack trace is least useful and most alarming. Valid JSON says
    # nothing about shape: a top-level array reached `.get` on a list, and a
    # `databases` value that was a string / null / array reached `.get` on those.
    if not isinstance(manifest, dict):
        raise RestoreError(
            f"malformed manifest: expected a JSON object, found "
            f"{type(manifest).__name__}"
        )
    dbs = manifest.get("databases")
    if not isinstance(dbs, dict) or not dbs:
        raise RestoreError("manifest lists no databases")
    for domain, meta in dbs.items():
        name, problem = _manifest_db_filename(domain, meta)
        if problem:
            raise RestoreError(f"malformed manifest: {problem}")
        f = src_dir / name
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
        # Label, do not name the directory: `dest_dir` is the backups ROOT now, so passing
        # a set path here would nest the set inside it and hide it from retention.
        pre = backup_all_dbs(config, label="pre-restore").dest

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
    finally:
        # On success every temp was consumed by os.replace (these unlinks no-op); on a
        # commit-pass failure this clears the orphan temps of the failing/unreached domains.
        for *_, temp in plan:
            temp.unlink(missing_ok=True)

    return RestoreResult(restored=restored, pre_restore_backup=pre, skipped_unknown=skipped_unknown)
