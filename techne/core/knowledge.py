"""Knowledge store — durable facts, decisions, patterns, evaluations, procedures."""

import hashlib
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from techne.core.database import connect, like_escape
from techne.core.redaction import redact_credentials

log = logging.getLogger(__name__)

VALID_TYPES = (
    "fact",
    "decision",
    "evaluation",
    "procedure",
    # Documents — third-party authoritative content mirrored into techne
    # by the Librarian persona (cabal#5). Tags carry source-specific
    # metadata (e.g. `source:confluence,kind:runbook`).
    "document",
    # Document card (Librarian Option D, techne#91) — a compact GIST + pointer to
    # a row in the `documents` table; full content is fetched on demand, never
    # injected. Pull-only (recall-able, never auto-injected). tags carry
    # doc:/ver:/backing:/pending.
    "document_card",
    # Agent self-lessons (techne#1195). Each row has a lesson_meta row beside it,
    # so they are written only through core/lessons.py: store() refuses them.
    # `lesson` is the agent's own (tier principle or entry), `grant` a standing
    # permission and `charter` a trust boundary, both operator-written.
    "lesson",
    "grant",
    "charter",
)
VALID_CONFIDENCE = ("high", "medium", "low")

# Team-domain types that must NOT be written to personal techne.db (techne#137).
# `pattern` lives in the Architect store (architect.db); writing it here leaked team
# patterns into personal recall (recall doesn't filter by type). store() rejects
# these with a pointer to the right store, so the boundary is a write-time invariant,
# not something a periodic cleanup has to mop up after.
REDIRECTED_TYPES = {
    "pattern": "the Architect pattern store (architect.db) — use `techne architect ingest`",
}

# Types that are reference material — recall-able (pull), but never auto-injected
# into session-start context (push). egress filters these out. `pattern` stays
# listed so any legacy rows on not-yet-cleaned installs are still pull-only.
PULL_ONLY_TYPES = ("pattern", "document", "document_card")

# Types that need a lesson_meta row (techne#1195). Written only by core/lessons.py,
# injected only by the principle band (never the general tier-0 bands), and kept
# out of consolidate/dream/conflict candidate selection: a merge would mint a row
# without its lesson_meta, and lessons are retired by the operator, not by a rail.
LESSON_TYPES = ("lesson", "grant", "charter")

# Lesson types only the operator writes or retires (techne#1195). No agent-facing
# path (MCP store, reflection promote) may create, supersede, retire or merge one.
OPERATOR_TYPES = ("grant", "charter")

# The types an agent or a reflection rail may name in a plain store. Reflection
# prompts and the CLI's --type choices use this, not VALID_TYPES, so neither
# offers a type that store() would refuse.
GENERIC_TYPES = tuple(t for t in VALID_TYPES if t not in LESSON_TYPES)

# SQL fragment excluding lesson types, for candidate selection that must skip them.
LESSON_TYPES_SQL = "(" + ",".join(f"'{t}'" for t in LESSON_TYPES) + ")"

# Mapping the legacy text enum to canonical numeric values (0..1).
# The migration in core/database.py uses the same values for backfill.
CONFIDENCE_NUMERIC = {"high": 0.9, "medium": 0.6, "low": 0.3}

# Default half-life for confidence decay (days). Operators can override per
# call site; agents reading via recall use config.knowledge_confidence_half_life_days.
DEFAULT_CONFIDENCE_HALF_LIFE_DAYS = 30.0

# Down-rank multiplier applied to an `uncertain` entry's effective confidence
# (techne#117 recall gate). An uncertain row is live and still surfaced, but a
# dependency it rests on was superseded — so it must rank below an otherwise-
# equal `current` row until reconfirmed. 0.5 halves its weight (one decay
# half-life's worth), enough to sink it without erasing it.
UNCERTAIN_CONFIDENCE_PENALTY = 0.5


def card_type_or_fact(ktype: str, tags: str | None) -> str:
    """Refuse to mint a `document_card` that has no `doc:<id>` pointer (#633, #1300).

    `document_card` is a MANAGED type: the card summary pass owns it and supplies
    the librarian pointer. A writer with no document in hand (reflect-promote,
    consolidate-merge, live-mode reflect, an agent's store) can only mint a card
    with nothing behind it, and fsck's dangling_cards check goes red.

    Demote rather than reject: the content is real prose about a document and worth
    keeping, it just isn't a card. store() and _merge_in_conn() (under merge_into()
    and the promote merge dispatch) apply this; reflect's promote-add path INSERTs
    directly and calls it itself. The #633 fix guarded only the two promote paths,
    and live-mode reflect went through store() unguarded (#1300).
    """
    from techne.core.cards import CARD_TYPE, card_doc_id

    if ktype == CARD_TYPE and card_doc_id(tags or "") is None:
        log.warning(
            "refusing to mint an unbacked %s (no doc: pointer in tags); storing as fact "
            "instead — tenishai/techne#633",
            CARD_TYPE,
        )
        return "fact"
    return ktype


def store(
    db_path: Path,
    content: str,
    type: str = "fact",
    tags: str = "",
    confidence: str = "medium",
    source: str = "session",
    anchor: str | None = None,
    provenance: dict | None = None,
    supersedes: Iterable[int] | None = None,
) -> int:
    """Store a knowledge entry. Returns the new entry's id.

    `supersedes` (techne#887) retires existing entries INTO the new one, in the
    same transaction as the insert: each id gets `superseded_by = <new id>` and
    the #117 dependency cascade, via `_retire_in_conn`. Before this, an entry could
    only say "SUPERSEDES #N" in prose, which recall never reads, so the old row
    stayed live. All-or-nothing: an unknown or already-superseded id rolls back
    the insert too, so a failed call never leaves the new row half-linked.

    `provenance` (techne#553) carries `written_by` / `origin_source` /
    `origin_session` for a federated write. IN THE SAME INSERT, deliberately: the
    first version stamped them with a follow-up UPDATE, and Prudence injected a
    "database is locked" on that second connection — the row committed
    unattributed, the retry hit the handler's content dedup, and the outbox
    recorded `synced` with no error. It degraded to NULL rather than a wrong name,
    which is the right ordering, but it made "unattributed" ambiguous between
    LEGITIMATELY UNATTRIBUTABLE and WE DROPPED IT — the exact distinction this
    column exists to draw. One statement cannot half-succeed.

    Writes both the legacy `confidence` text enum (for display + backward
    compat) and the canonical `confidence_numeric` derived from it. The
    `last_confirmed_at` is set BY THIS INSERT to CURRENT_TIMESTAMP so a fresh
    entry starts at full strength. It is no longer a schema default
    (techne#938): the CREATE TABLE could not carry one and still match what
    the v5 ALTER produces, so every writer owns the value.

    `anchor` (techne#90) optionally pins the memory to evidence in a repo. Pass
    a path, or "path::symbol" to also record the symbol. When given, the file's
    sha256 + the git HEAD sha are captured so recall can grade staleness. If the
    path doesn't resolve to a file in a git repo, the anchor_path/symbol are
    still recorded but the hash/sha stay NULL → recall reports `unknown` (off-git
    or portable host) rather than falsely asserting freshness. Omitting `anchor`
    leaves all anchor columns NULL and behaves exactly as before — non-anchored
    memory is never staleness-checked.
    """
    if type in REDIRECTED_TYPES:
        raise ValueError(
            f"knowledge type '{type}' is not stored in personal memory (techne#137); "
            f"it belongs in {REDIRECTED_TYPES[type]}"
        )
    if type in LESSON_TYPES:
        raise ValueError(
            f"knowledge type '{type}' needs lesson structure (techne#1195); "
            "write it with `techne lesson` or the techne_lesson tool"
        )
    if type not in VALID_TYPES:
        raise ValueError(f"Invalid knowledge type '{type}'. Must be one of: {GENERIC_TYPES}")
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(f"Invalid confidence '{confidence}'. Must be one of: {VALID_CONFIDENCE}")
    type = card_type_or_fact(type, tags)

    anchor_path: str | None = None
    anchor_symbol: str | None = None
    anchor_content_hash: str | None = None
    anchor_git_sha: str | None = None
    if anchor:
        from techne.core.anchor import resolve_anchor

        anchor_path, _, symbol = anchor.partition("::")
        anchor_path = anchor_path or None
        anchor_symbol = symbol or None
        if anchor_path:
            resolved = resolve_anchor(anchor_path)
            if resolved is not None:
                anchor_content_hash = resolved["content_hash"]
                anchor_git_sha = resolved["git_sha"]

    retire_ids = _normalize_ids(supersedes)

    numeric = CONFIDENCE_NUMERIC[confidence]
    conn = connect(db_path)
    try:
        if retire_ids:
            # Write lock before the liveness reads in _retire_in_conn (TOCTOU guard,
            # same as supersede()).
            conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """INSERT INTO knowledge
                 (type, content, source, tags, confidence, confidence_numeric,
                  anchor_path, anchor_symbol, content_hash, anchor_git_sha,
                  written_by, origin_source, origin_session, last_confirmed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                type,
                redact_credentials(content),
                source,
                tags,
                confidence,
                numeric,
                anchor_path,
                anchor_symbol,
                anchor_content_hash,
                anchor_git_sha,
                (provenance or {}).get("written_by"),
                (provenance or {}).get("origin_source"),
                (provenance or {}).get("origin_session"),
            ),
        )
        new_id = cur.lastrowid
        for old_id in retire_ids:
            _retire_in_conn(conn, old_id, new_id)
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_ids(ids: Iterable[int] | int | None) -> list[int]:
    """Validate a `supersedes` argument: a single id or an iterable of ids, as ints.

    Duplicates are refused rather than folded: the second retire of the same id
    would fail with "already superseded by <the row we just wrote>", which blames
    the store for a typo in the call."""
    if ids is None:
        return []
    if isinstance(ids, (int, str)):
        ids = [ids]
    out: list[int] = []
    for raw in ids:
        if isinstance(raw, bool):
            raise ValueError(f"supersedes: {raw!r} is not a knowledge id")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"supersedes: {raw!r} is not a knowledge id") from None
        if value in out:
            raise ValueError(f"supersedes: knowledge id {value} is listed twice")
        out.append(value)
    return out


def supersede(db_path: Path, old_id: int, new_content: str, source: str = "session") -> int:
    """Replace an existing knowledge entry with updated content.

    Marks the old entry as superseded and creates a new one inheriting the old
    entry's type, tags, and confidence — including `confidence_numeric` (the value
    that actually drives ranking/decay), which must stay in sync with the text
    enum (techne#131; a prior version dropped it, silently demoting every
    superseded fact to the 0.6 default). `last_confirmed_at` is intentionally NOT
    carried: superseding is a fresh confirmation, so the decay clock resets to now.

    An evidence anchor (techne#90) is carried forward to the new revision, but its
    hash/sha are RE-RESOLVED against the current file state (mirroring store()) —
    the new content is a fresh snapshot of the same anchored evidence, not an
    inheritance of the old, now-stale snapshot. Off-git/missing → NULL → `unknown`.
    """
    conn = connect(db_path)
    try:
        # Take the write lock before the liveness read (read→write TOCTOU; matches
        # curated.ingest_memory_file's guard, techne#108 staff review). The promote
        # dispatch uses _supersede_in_conn directly under its own held lock, so this
        # only guards direct public callers.
        conn.execute("BEGIN IMMEDIATE")
        new_id = _supersede_in_conn(conn, old_id, new_content, source)
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _supersede_in_conn(
    conn,
    old_id: int,
    new_content: str,
    source: str,
    *,
    operator: bool = False,
    lesson_path: bool = False,
) -> int:
    """Core of supersede() on a caller-owned connection — no commit/close — so it can
    run inside another transaction (e.g. promote_candidate's action dispatch, #108).
    Body is verbatim from the public supersede(); the caller owns the transaction.

    A lesson-type row (techne#1195) changes only through lessons.refine, which passes
    ``lesson_path`` and rewrites the meta rule; any other supersede would leave the
    band rendering the old rule. The new revision gets a copy of the lesson_meta and
    the old one's family entries are repointed at it. A grant or charter is also
    refused unless ``operator`` is set — only `techne lesson` passes it."""
    old = conn.execute(
        "SELECT type, tags, confidence, anchor_path, anchor_symbol, superseded_by, "
        "written_by, origin_source, origin_session "
        "FROM knowledge WHERE id = ?",
        (old_id,),
    ).fetchone()
    if old is None:
        raise ValueError(f"Knowledge entry {old_id} not found")
    if old["superseded_by"] is not None:
        raise ValueError(
            f"Knowledge entry {old_id} is already superseded by {old['superseded_by']}; "
            "supersede the latest revision instead"
        )
    if old["type"] in LESSON_TYPES and not lesson_path:
        _refuse_lesson_type(old["type"], old_id)
    _refuse_operator_type(old["type"], old_id, operator)

    # Re-derive numeric from the enum so the two can't drift (self-heals any
    # row a pre-#131 supersede left inconsistent). Fall back to medium if the
    # stored enum is somehow unknown.
    numeric = CONFIDENCE_NUMERIC.get(old["confidence"], CONFIDENCE_NUMERIC["medium"])

    # Re-resolve the carried anchor (if any) against the current working tree.
    # `anchor_content_hash` is the anchored FILE's hash (→ the content_hash column),
    # distinct from the content_fingerprint() of the knowledge text.
    anchor_content_hash: str | None = None
    anchor_git_sha: str | None = None
    if old["anchor_path"]:
        from techne.core.anchor import resolve_anchor

        resolved = resolve_anchor(old["anchor_path"])
        if resolved is not None:
            anchor_content_hash = resolved["content_hash"]
            anchor_git_sha = resolved["git_sha"]

    cur = conn.execute(
        """INSERT INTO knowledge
             (type, content, source, tags, confidence, confidence_numeric,
              anchor_path, anchor_symbol, content_hash, anchor_git_sha,
              written_by, origin_source, origin_session, last_confirmed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (
            old["type"],
            redact_credentials(new_content),
            source,
            old["tags"],
            old["confidence"],
            numeric,
            old["anchor_path"],
            old["anchor_symbol"],
            anchor_content_hash,
            anchor_git_sha,
            # Writer provenance is carried too (techne#1244): a revision is still
            # the same writer's row, not an unattributed one.
            old["written_by"],
            old["origin_source"],
            old["origin_session"],
        ),
    )
    new_id = cur.lastrowid

    # Atomic liveness guard (techne#390): `AND superseded_by IS NULL` so a
    # concurrent supersede that already retired old_id — between our liveness read
    # above and this write, which the Python check can't cover for _in_conn callers
    # not holding BEGIN IMMEDIATE — can't ALSO succeed and leave two live 'current'
    # rows. rowcount 0 = we lost the race: raise so the caller's transaction rolls
    # back the revision we just inserted (no orphan) rather than silently no-op.
    cur = conn.execute(
        "UPDATE knowledge SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND superseded_by IS NULL",
        (new_id, old_id),
    )
    if cur.rowcount == 0:
        won = conn.execute("SELECT superseded_by FROM knowledge WHERE id = ?", (old_id,)).fetchone()
        won_by = won["superseded_by"] if won else None
        raise ValueError(
            f"Knowledge entry {old_id} was concurrently superseded"
            + (f" by {won_by}" if won_by else "")
            + "; supersede the latest revision instead"
        )
    # Dependency cascade (techne#117): a fact that depended on the now-superseded
    # entry can no longer be trusted at face value — flip live dependents to
    # `uncertain`, atomically in this transaction. The replacement value is NOT
    # auto-propagated; a dependent stays uncertain until reconfirm()ed. supersede
    # is a deliberate update, so this is off the hot ingest path.
    _cascade_uncertain(conn, old_id)
    if old["type"] in LESSON_TYPES:
        _carry_lesson_meta(conn, old_id, new_id)
    return new_id


def _refuse_lesson_type(ktype: str, knowledge_id: int) -> None:
    raise ValueError(
        f"knowledge entry {knowledge_id} is a {ktype}: it changes only with "
        "`techne lesson refine` or `techne lesson retire` (techne#1195)"
    )


def _refuse_operator_type(ktype: str, knowledge_id: int, operator: bool) -> None:
    if ktype in OPERATOR_TYPES and not operator:
        raise ValueError(
            f"knowledge entry {knowledge_id} is a {ktype}: only the operator changes it "
            "(techne#1195), with `techne lesson`"
        )


def _carry_lesson_meta(conn, old_id: int, new_id: int) -> None:
    """Copy *old_id*'s lesson_meta to its new revision *new_id* and repoint the
    family entries that named *old_id* as their parent (techne#1195). Without this a
    refined principle would lose its structure and its recurrence evidence."""
    conn.execute(
        """INSERT OR REPLACE INTO lesson_meta
             (knowledge_id, agent_id, tier, rule, parent_id, enforced_by,
              evidence_session, loaded_when_failed)
           SELECT ?, agent_id, tier, rule, parent_id, enforced_by,
                  evidence_session, loaded_when_failed
           FROM lesson_meta WHERE knowledge_id = ?""",
        (new_id, old_id),
    )
    conn.execute("UPDATE lesson_meta SET parent_id = ? WHERE parent_id = ?", (new_id, old_id))


def _retire_in_conn(conn, old_id: int, survivor_id: int) -> int:
    """Retire *old_id* by pointing its `superseded_by` at an EXISTING live *survivor_id*
    row — without minting a new revision (techne#213 contradiction detection).

    Unlike `_supersede_in_conn`, which mints a fresh row from *new_content*, retire is
    used when BOTH the loser and the survivor already exist as live `knowledge` rows
    (the two entries a contradiction check compared). Minting here would duplicate the
    survivor's fact into a second live row with an independent confidence/decay clock
    that can drift — the integrity bug this helper avoids. The survivor keeps its own
    row, id, and clock untouched; we only repoint the loser at it.

    Bookkeeping parity with `_supersede_in_conn` MINUS the mint: sets
    `superseded_by` + `updated_at` on *old_id* and runs the #117 dependency cascade
    (live dependents of *old_id* → `uncertain`). It does NOT repoint knowledge_deps
    edges — neither does `_supersede_in_conn`: per `cascade_uncertain`, an edge
    references a specific revision and is re-pointed by re-extraction, not auto-migrated.
    It does NOT touch the survivor's `last_confirmed_at` (retire is not a reaffirmation
    of the survivor; that would be a separate deliberate act).

    Preserve-not-delete: *old_id* is retained (retrievable with include_superseded),
    never hard-deleted. Returns *survivor_id* (the id that stays live).

    A lesson-type row is refused (techne#1195): retiring a principle into another
    row would strand its family entries, so lessons retire with `techne lesson retire`.
    """
    if old_id == survivor_id:
        raise ValueError("cannot retire a knowledge entry into itself")
    old = conn.execute(
        "SELECT superseded_by, type FROM knowledge WHERE id = ?", (old_id,)
    ).fetchone()
    if old is None:
        raise ValueError(f"Knowledge entry {old_id} not found")
    if old["type"] in LESSON_TYPES:
        _refuse_lesson_type(old["type"], old_id)
    if old["superseded_by"] is not None:
        raise ValueError(
            f"Knowledge entry {old_id} is already superseded by {old['superseded_by']}; "
            "retire the latest revision instead"
        )
    survivor = conn.execute(
        "SELECT superseded_by FROM knowledge WHERE id = ?", (survivor_id,)
    ).fetchone()
    if survivor is None:
        raise ValueError(f"Survivor knowledge entry {survivor_id} not found")
    if survivor["superseded_by"] is not None:
        raise ValueError(
            f"Survivor {survivor_id} is itself superseded by {survivor['superseded_by']}; "
            "retire into a live entry"
        )
    # Atomic liveness guard (techne#390), mirroring _supersede_in_conn: the Python
    # check above is a read→write TOCTOU for callers not under BEGIN IMMEDIATE, so
    # gate the write on `AND superseded_by IS NULL` too. rowcount 0 = a concurrent
    # supersede/retire already won → raise (caller rolls back) rather than
    # double-write.
    cur = conn.execute(
        "UPDATE knowledge SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND superseded_by IS NULL",
        (survivor_id, old_id),
    )
    if cur.rowcount == 0:
        won = conn.execute("SELECT superseded_by FROM knowledge WHERE id = ?", (old_id,)).fetchone()
        won_by = won["superseded_by"] if won else None
        raise ValueError(
            f"Knowledge entry {old_id} was concurrently superseded"
            + (f" by {won_by}" if won_by else "")
            + "; retire the latest revision instead"
        )
    # #117 cascade — identical to _supersede_in_conn: live dependents of the retired
    # entry go `uncertain` until reconfirmed. The survivor is NOT auto-propagated.
    _cascade_uncertain(conn, old_id)
    return survivor_id


def retire(db_path: Path, old_id: int, survivor_id: int) -> int:
    """Retire *old_id* into an EXISTING live entry *survivor_id* (techne#887).

    Public wrapper for `_retire_in_conn`, which until now was reachable only from
    the contradiction check: the case where an entry already in the store says in
    prose that it replaces an older one, and the link was never written. Minting a
    new revision (supersede()) would duplicate the survivor's content, so this only
    repoints the loser and runs the #117 cascade. Returns *survivor_id*.

    Refuses an ARCHIVED survivor as well as a superseded one: retiring a live row
    into a row recall no longer injects would drop the fact from default context
    entirely. `_retire_in_conn` checks only `superseded_by`, so the check is here.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM knowledge WHERE id = ?", (survivor_id,)).fetchone()
        if row is not None and row["status"] == "archived":
            raise ValueError(
                f"Survivor {survivor_id} is archived; retire into a live entry "
                f"(`techne knowledge unarchive {survivor_id}` first if it should be live)"
            )
        result = _retire_in_conn(conn, old_id, survivor_id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# A replacement declared in prose (techne#887): the OPENING of an entry names the
# entry it replaces. Strict on purpose. The 2026-09-12 sweep on the live store found
# a bare LIKE '%supersede%' matched 266 rows, most of them entries discussing
# supersession, "SHARPENS #N", or "supersedes my recommendation". So this needs the
# verb, then a MARKED id straight after it (`#N`, `techne#N`, `knowledge #N`, or the
# knowledge sigil), inside the first 400 characters. A bare number is not enough:
# "corrects 2 errors" would name entry 2. "superseded by #N" is the opposite
# direction and does not match (`supersedes?` needs `:` or whitespace next), and
# other repos' refs (`cabal#12`) do not match either.
#
# `ref_word` / `ref_brain` (techne#1065) mark which form matched: a literal
# "techne#N" is AMBIGUOUS (can equally name a GitHub issue, not this row), and an
# unmarked list continuation inherits the ambiguity of the nearest marked reference
# before it — see `_ref_is_ambiguous` and the PR body for the incidents this fixes.
_REF = r"(?:(?P<ref_word>techne|knowledge)\s*#|(?P<ref_brain>\N{BRAIN})|#)\s*(?P<ref_id>\d{1,9})\b"
_DECLARATION = re.compile(
    r"\b(?P<verb>supersedes?|corrects|correction\s+(?:to|of))\s*:?\s+" + _REF,
    re.IGNORECASE,
)
# "SUPERSEDES techne#882 (old runner), #887 (its hardening) and #995": the
# list form knowledge #1155 used. A continuation must be marked too.
_DECLARATION_MORE = re.compile(r"\s*(?:\([^)]*\)\s*)?(?:,|\band\b|&)\s*" + _REF, re.IGNORECASE)
DECLARATION_WINDOW = 400

# The back-pointer a partially corrected entry carries (techne#1202). "corrects" /
# "correction to" may fix ONE claim in an older entry whose other claims still
# stand; the store's procedure is to keep that entry live and edit it to point at
# the correction ("CORRECTED by knowledge 4045 (2026-09-30): ... The rest of this
# entry stands."). Once the target carries that pointer the correction is recorded,
# so doctor must not suggest retiring the target. Searched over the WHOLE target,
# since the pointer is appended at the end. A literal `techne#N` does not count:
# it names a GitHub issue (REF_CONVENTION_DATE).
_CORRECTED_BY = re.compile(
    r"\bcorrected\s+by\s*:?\s+(?:knowledge\s*#?|\N{BRAIN}|#)\s*(?P<ref_id>\d{1,9})\b",
    re.IGNORECASE,
)


def _points_at_correction(target_content: str, declarer: int) -> bool:
    """True iff *target_content* says it was corrected by entry *declarer*."""
    return any(
        int(m.group("ref_id")) == declarer for m in _CORRECTED_BY.finditer(target_content or "")
    )


# Since this date the convention is: knowledge refs use "knowledge #N" or the brain
# sigil, and "techne#N" names a GitHub issue (techne#1065). A declarer written on or
# after this date gets no benefit of the doubt for a bare "techne#N" — it is not
# accepted as a knowledge reference at all. A declarer written before this date may
# still mean a knowledge row by it (e.g. knowledge 1155's "SUPERSEDES techne#882"),
# so it is still accepted there, just always reported as ambiguous (techne#1065).
REF_CONVENTION_DATE = "2026-08-31"


def _ref_is_marked(match: re.Match) -> bool:
    """True iff *match* carries its own marker word (`techne`/`knowledge`) or the
    brain sigil, rather than a plain `#N`."""
    return bool(match.group("ref_word")) or match.group("ref_brain") is not None


def _ref_is_ambiguous(match: re.Match, *, opener_ambiguous: bool) -> bool:
    """True iff *match* (a `_DECLARATION`/`_DECLARATION_MORE` match) is a bare,
    word-less `techne#N` — the one form a GitHub issue can hide behind.

    A continuation that carries no marker of its own (plain `#N`) is NOT its own
    fresh, confident reference: it inherits `opener_ambiguous`, the ambiguity of
    the NEAREST preceding marked reference in the list. "SUPERSEDES techne#882,
    #887 and #995" names 882, 887 and 995 the same way (techne#1065, Prudence
    review round 1), and in "SUPERSEDES knowledge #1, techne#2 and #3" the #3
    follows techne#2, not the confident opener. Only a continuation with its OWN
    `knowledge #` word or brain sigil is confident regardless of what came before.
    """
    if not _ref_is_marked(match):
        return opener_ambiguous
    return (match.group("ref_word") or "").lower() == "techne"


def _declared_refs(content: str) -> list[tuple[int, int | None, bool]]:
    """Per declared id: the `techne#N` id that makes it ambiguous (or None), and
    whether the declaring verb is `supersede(s)` rather than a correction.

    The second element is the id itself when the reference is a literal
    `techne#N`, the id of the nearest preceding `techne#N` in the list when it is
    an unmarked continuation of one, and None when the reference is confident.
    Ids are deduplicated across the whole content block: if ANY occurrence of an
    id is unambiguous (its own `knowledge #` or brain sigil), it is reported
    unambiguous — a later, confident restatement should not be shadowed by an
    earlier, ambiguous one (techne#1065 review nit; e.g. "SUPERSEDES techne#5 and
    knowledge #5"). Likewise, an id is a replacement if ANY occurrence uses
    `supersede(s)`; list continuations take the verb of their opener (techne#1202).
    """
    head = (content or "")[:DECLARATION_WINDOW]
    refs: list[tuple[int, int | None, bool]] = []
    for m in _DECLARATION.finditer(head):
        replaces = m.group("verb").lower().startswith("supersede")
        opener_ambiguous = _ref_is_ambiguous(m, opener_ambiguous=False)
        ref_id = int(m.group("ref_id"))
        via = ref_id if opener_ambiguous else None
        refs.append((ref_id, via, replaces))
        pos = m.end()
        while more := _DECLARATION_MORE.match(head, pos):
            ref_id = int(more.group("ref_id"))
            opener_ambiguous = _ref_is_ambiguous(more, opener_ambiguous=opener_ambiguous)
            if _ref_is_marked(more):
                via = ref_id if opener_ambiguous else None
            refs.append((ref_id, via, replaces))
            pos = more.end()
    seen: dict[int, tuple[int | None, bool]] = {}
    for target_id, via, replaces in refs:
        if target_id not in seen:
            seen[target_id] = (via, replaces)
        else:
            prev_via, prev_replaces = seen[target_id]
            seen[target_id] = (None if via is None else prev_via, prev_replaces or replaces)
    return [(target_id, via, replaces) for target_id, (via, replaces) in seen.items()]


def _declared_targets_with_ambiguity(content: str) -> list[tuple[int, bool]]:
    """Like `declared_targets`, but keeps, per id, whether the match is ambiguous
    (see `_declared_refs` for the rules)."""
    return [(target_id, via is not None) for target_id, via, _ in _declared_refs(content)]


def declared_targets(content: str) -> list[int]:
    """Knowledge ids that *content* says, in its opening, it replaces."""
    return [target_id for target_id, _ in _declared_targets_with_ambiguity(content)]


def find_declared_supersessions(db_path: Path) -> list[dict]:
    """Prose-declared supersessions that were never written to `superseded_by`
    (techne#887). REPORT ONLY: nothing here writes.

    A pair is reported when a live entry (the declarer) names, in its opening, an
    OLDER entry that is still live. Ids that are missing, already superseded,
    newer than the declarer, or the declarer itself are skipped: each of those
    needs a person, not a rule. When two entries claim the same target the newest
    claimant wins, since the latest declaration is the one to keep.

    Returned in ascending target order, which is the order to apply them in:
    `_retire_in_conn` needs a LIVE survivor, and in a chain a->b->c retiring b
    first would leave a nothing live to retire into.

    Each pair carries `ambiguous` (techne#1065): True when EVERY reference that
    matched the target resolves to a literal "techne#N" — the one marker that can
    equally name a GitHub issue rather than this knowledge row, and whose
    ambiguity is not decidable from the text alone (this includes an unmarked
    list continuation whose nearest preceding marked reference is a `techne#N` —
    see `_ref_is_ambiguous`). `ambiguous_via` names that `techne#N` id (the target
    itself for a direct `techne#N`), or is None when the pair is confident, so a
    caller can quote what the text actually says. If ANY occurrence of the id is instead
    unambiguous (its own "knowledge #N" or the brain sigil), that wins and the
    pair is reported unambiguous, so a later, confident restatement is never
    shadowed by an earlier, ambiguous one. A declarer written on or after
    `REF_CONVENTION_DATE` gets no ambiguous match at all (a bare "techne#N" there
    is not accepted as a knowledge reference, full stop — see
    `_declared_refs`); for an older declarer it is still
    accepted, but always comes back `ambiguous`. Callers MUST NOT render an
    ambiguous pair as a ready-to-run command — it is a check item, not a verdict
    (techne#1065; two production pairs the day this was found would have retired
    live, unrelated knowledge if pasted as printed).

    A correction ("corrects" / "correction to") is skipped once the target already
    points back at the declarer ("corrected by knowledge N"): that is a partial
    correction, recorded the way the store's procedure says, and the target's other
    claims still stand (techne#1202). A `supersede(s)` declaration is reported
    whatever the target says.
    """
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, content, superseded_by, status, created_at FROM knowledge"
        ).fetchall()
    finally:
        conn.close()
    live = {r["id"]: r for r in rows if r["superseded_by"] is None and r["status"] != "archived"}
    best: dict[int, dict] = {}
    for declarer, row in live.items():
        declarer_is_post_convention = (row["created_at"] or "") >= REF_CONVENTION_DATE
        for target, via, replaces in _declared_refs(row["content"]):
            ambiguous = via is not None
            if ambiguous and declarer_is_post_convention:
                # Post-convention, "techne#N" never means a knowledge row.
                continue
            if target >= declarer or target not in live:
                continue
            if not replaces and _points_at_correction(live[target]["content"], declarer):
                continue
            if target not in best or declarer > best[target]["declarer"]:
                best[target] = {"declarer": declarer, "ambiguous": ambiguous, "via": via}
    return [
        {
            "target": t,
            "declarer": info["declarer"],
            "ambiguous": info["ambiguous"],
            "ambiguous_via": info["via"],
            "target_preview": (live[t]["content"] or "")[:80].replace("\n", " "),
        }
        for t, info in sorted(best.items())
    ]


def merge_into(
    db_path: Path,
    old_ids: list[int],
    merged_content: str,
    *,
    type: str,
    confidence: str,
    tags: str | None = None,
    source: str = "consolidate",
) -> int:
    """Collapse N live knowledge rows into one merged row (techne#108 consolidate).

    Mints ONE new row with the *explicitly chosen* type and content, plus what its
    sources carried (techne#1244, see `_merge_in_conn`): the union of the given and
    the sources' tags, the given confidence capped at the highest source's, the anchor
    and writer provenance the sources agree on, and the newest source's
    last_confirmed_at. Then it points every old at it via `superseded_by` (N→1 is a plain per-row FK; legal and indexed). Each old
    runs the #117 cascade (its dependents go `uncertain`).

    Reversible: `UPDATE knowledge SET superseded_by=NULL WHERE superseded_by=new_id`
    then delete the merged row — the N back-pointers ARE the merge provenance, so no
    extra table. Raises if any old is missing or already superseded (no merging a
    chain). Does NOT add a knowledge_deps edge merged→olds: the olds are superseded,
    so the cascade would immediately flip the merged row to uncertain.
    """
    if not old_ids:
        raise ValueError("merge_into requires at least one old id")
    if type not in GENERIC_TYPES:
        raise ValueError(f"merge_into: invalid type {type!r}")
    numeric = CONFIDENCE_NUMERIC.get(confidence, CONFIDENCE_NUMERIC["medium"])
    conn = connect(db_path)
    try:
        # Write lock before the liveness reads (TOCTOU guard, matches supersede above).
        conn.execute("BEGIN IMMEDIATE")
        new_id = _merge_in_conn(
            conn,
            old_ids,
            merged_content,
            ktype=type,
            confidence=confidence,
            numeric=numeric,
            tags=tags,
            source=source,
        )
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _merge_in_conn(
    conn,
    old_ids: list[int],
    merged_content: str,
    *,
    ktype: str,
    confidence: str,
    numeric: float,
    tags: str | None,
    source: str,
) -> int:
    """Core of merge_into() on a caller-owned connection (no commit/close), for the
    promote dispatch (#108). Asserts every old is live, mints the merged row, repoints
    the olds, cascades each. Caller owns the transaction.

    "Live" excludes `status = 'archived'` as well as superseded (review round 2 on
    techne#1033): `delete()` can leave a retired-but-superseded_by-NULL predecessor
    behind when it archives the head of a chain, and merging that row's content into
    a freshly-minted `current` row would resurrect it under a different id — the
    same resurrection this guard already blocks for a superseded row.
    """
    ktype = card_type_or_fact(ktype, tags)
    seen: set[int] = set()
    sources: list = []
    for oid in old_ids:
        if oid in seen:
            raise ValueError(f"merge_into: duplicate old id {oid}")
        seen.add(oid)
        row = conn.execute(
            "SELECT superseded_by, status, type, tags, confidence, anchor_path, anchor_symbol, "
            "written_by, origin_source, origin_session, last_confirmed_at "
            "FROM knowledge WHERE id = ?",
            (oid,),
        ).fetchone()
        if row is None:
            raise ValueError(f"merge_into: knowledge entry {oid} not found")
        if row["type"] in LESSON_TYPES:
            raise ValueError(
                f"merge_into: entry {oid} is a {row['type']}; lessons are merged with "
                "`techne lesson refine`, which keeps their structure (techne#1195)"
            )
        if row["superseded_by"] is not None:
            raise ValueError(
                f"merge_into: entry {oid} is already superseded — merge live rows only"
            )
        if row["status"] == "archived":
            raise ValueError(f"merge_into: entry {oid} is archived — merge live rows only")
        sources.append(row)

    # WHAT THE MERGED ROW INHERITS (techne#1244). It replaces its sources, so it
    # keeps what they carried rather than only what the judge wrote:
    #   - tags: the union of the caller's and every source's, in first-seen order
    #     (tags select rows for conflict and dream, so an untagged merge dropped out);
    #   - confidence: the caller's, capped at the highest source's, so two `low`
    #     rows cannot become one `high` on the model's say-so;
    #   - anchor and writer provenance: carried when every source that has one
    #     agrees, else left NULL (honestly unattributed, as the schema intends);
    #   - last_confirmed_at: the newest source's, not "now": merging is not a
    #     confirmation of the content.
    merged_tags = _union_tags([tags or "", *(r["tags"] or "" for r in sources)])
    capped = _cap_confidence(confidence, [r["confidence"] for r in sources])
    if capped != confidence:
        confidence, numeric = capped, CONFIDENCE_NUMERIC[capped]
    anchor = _agreed(sources, ("anchor_path", "anchor_symbol"))
    written_by, origin_source, origin_session = (
        _agreed(sources, (col,))[0] for col in ("written_by", "origin_source", "origin_session")
    )
    confirmed = [r["last_confirmed_at"] for r in sources if r["last_confirmed_at"]]
    anchor_content_hash: str | None = None
    anchor_git_sha: str | None = None
    if anchor[0]:
        from techne.core.anchor import resolve_anchor

        resolved = resolve_anchor(anchor[0])
        if resolved is not None:
            anchor_content_hash = resolved["content_hash"]
            anchor_git_sha = resolved["git_sha"]

    cur = conn.execute(
        """INSERT INTO knowledge
             (type, content, source, tags, confidence, confidence_numeric,
              anchor_path, anchor_symbol, content_hash, anchor_git_sha,
              written_by, origin_source, origin_session, last_confirmed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))""",
        (
            ktype, redact_credentials(merged_content), source, merged_tags, confidence, numeric,
            anchor[0], anchor[1], anchor_content_hash, anchor_git_sha,
            written_by, origin_source, origin_session, max(confirmed) if confirmed else None,
        ),
    )
    new_id = cur.lastrowid
    for oid in old_ids:
        conn.execute(
            "UPDATE knowledge SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_id, oid),
        )
        _cascade_uncertain(conn, oid)
    return new_id


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _union_tags(tag_strings: list[str]) -> str:
    """Comma-separated tags from every string, deduplicated in first-seen order."""
    out: list[str] = []
    for tags in tag_strings:
        for tag in (t.strip() for t in tags.split(",")):
            if tag and tag not in out:
                out.append(tag)
    return ",".join(out)


def _cap_confidence(requested: str, source_confidences: list[str]) -> str:
    """`requested`, lowered to the highest of `source_confidences` if above it."""
    known = [c for c in source_confidences if c in _CONFIDENCE_RANK]
    if not known or requested not in _CONFIDENCE_RANK:
        return requested
    ceiling = max(known, key=_CONFIDENCE_RANK.__getitem__)
    return ceiling if _CONFIDENCE_RANK[requested] > _CONFIDENCE_RANK[ceiling] else requested


def _agreed(rows: list, cols: tuple[str, ...]) -> tuple:
    """The value of `cols` shared by every row that has one (any non-NULL column),
    or all-None when no row has one or two rows disagree."""
    values = {tuple(r[c] for c in cols) for r in rows if any(r[c] is not None for c in cols)}
    return values.pop() if len(values) == 1 else (None,) * len(cols)


# --- dependency-driven staleness (techne#117) -------------------------------


def _add_dep_in_conn(conn, dependent_id: int, trigger_id: int, kind: str = "derived_from") -> None:
    """Write one dependency edge inside a caller-owned transaction (no commit/close).

    Idempotent (INSERT OR IGNORE). The sole mechanism for writing dep edges — called
    only from promote_candidate's dream-promote dispatch (_apply_add in reflect.py)
    so the edge write stays in the same atomic transaction as the knowledge row insert
    (techne#215). Self-edges are silently skipped (dependent_id == trigger_id).

    Design decision (techne#251, 2026-07-06): dependency edges are created only on the
    dream-promote path. Supersession is replacement, not derivation, so no dep-edge is
    written on the conflict/supersede path. The former public add_dependency() API was
    removed as dead code (zero non-test callers); revisit if agent-declared dependencies
    become a concrete need.
    """
    if dependent_id == trigger_id:
        return  # no self-edges; silent skip mirrors INSERT OR IGNORE spirit
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_deps (dependent_id, trigger_id, kind) VALUES (?, ?, ?)",
        (dependent_id, trigger_id, kind),
    )


def _cascade_uncertain(conn, trigger_id: int) -> int:
    """Flip live (non-superseded, currently-current) dependents of *trigger_id* to
    `uncertain`. Operates on a caller-owned connection so supersede() can run it in
    its own transaction. Returns the count changed."""
    cur = conn.execute(
        """UPDATE knowledge SET status = 'uncertain', updated_at = CURRENT_TIMESTAMP
           WHERE superseded_by IS NULL AND status = 'current'
             AND id IN (SELECT dependent_id FROM knowledge_deps WHERE trigger_id = ?)""",
        (trigger_id,),
    )
    return cur.rowcount


def cascade_uncertain(db_path: Path, trigger_id: int) -> int:
    """Standalone/reconcile entry point for the dependency cascade (techne#117) —
    mark live dependents of *trigger_id* `uncertain`. supersede() runs the same
    cascade inline when it supersedes an entry. Returns the count flipped.

    Two deliberate Stage-1 boundaries (a junction table, not a graph engine):
      * ONE-HOP: flipping a dependent to `uncertain` is not itself a supersession,
        so it does not re-fire — if A depends on B depends on C, superseding C marks
        B, not A. Transitive propagation is out of scope.
      * Edges reference a specific revision id and do NOT auto-migrate across a
        supersession chain; re-extraction re-points them at the current revision.
    """
    conn = connect(db_path)
    try:
        n = _cascade_uncertain(conn, trigger_id)
        conn.commit()
        return n
    finally:
        conn.close()


def mark_uncertain(db_path: Path, knowledge_id: int) -> bool:
    """Mark an entry `uncertain` — live, retrievable on request, not established.

    The write-side counterpart to `reconfirm`, its inverse on `status` alone. Until now
    the only route from a `current` row to `uncertain` was the dependency cascade:
    something was superseded, so its dependents became doubtful. There was no way for a WRITER
    to say "this is what I concluded, and nothing has checked it."

    That is what an agent's self-authored memory is (cabal#141). It must not be
    recallable as an established fact — `search()` already excludes `uncertain`
    unless a caller opts in, which is techne#501's decision: filter from
    auto-injection, tag on explicit search — but before this there was no
    supported way to put a row into that state at write time, so callers wrote
    `current` and their own conclusions came back indistinguishable from
    curated knowledge.

    Returns True if a row changed. Idempotent: marking an already-uncertain row
    returns False rather than raising, and a superseded row is left alone —
    superseded is a stronger statement than uncertain and must not be weakened.
    """
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """UPDATE knowledge SET status = 'uncertain', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'current' AND superseded_by IS NULL""",
            (knowledge_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def reconfirm(db_path: Path, knowledge_id: int) -> bool:
    """Clear an `uncertain` flag — the dependent was re-verified (techne#117). Sets
    status back to `current` and bumps last_confirmed_at. Returns True if a row
    changed (i.e. it was uncertain)."""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """UPDATE knowledge SET status = 'current',
                 last_confirmed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'uncertain'""",
            (knowledge_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _uncertainty_payload() -> dict:
    """Build the uncertainty marker attached to a recalled `uncertain` row.

    Deliberately mirrors anchor.py's `_freshness_payload` shape (machine-readable
    `state` + human `tag` + `note`) so a consumer renders staleness and
    dependency-uncertainty through one code path rather than two vocabularies.
    """
    return {
        "state": "uncertain",
        "tag": "[uncertain — a fact this rests on changed; reconfirm before trusting]",
        "note": (
            "a dependency was superseded (techne#117); this entry is untrustworthy "
            "until reconfirmed"
        ),
    }


def annotate_uncertain(db_path, rows: Iterable[dict]) -> list[dict]:
    """Attach an `uncertainty` payload to recalled knowledge rows whose status is
    `uncertain` (techne#117 cascade-to-uncertain, Stage-2 recall gate).

    The dependency cascade (Stage 1) flips a derived row to `uncertain` when its
    basis is superseded; the row stays live (``superseded_by IS NULL``) so search
    still returns it. This annotates that subset so the surface never presents an
    uncertain row as confidently current — the recall-side mirror of #90's
    `annotate_freshness`.

    Scope guarantees (mirrors annotate_freshness):
      - Only knowledge rows with status='uncertain' are touched; `current` rows
        get no `uncertainty` field. No row is ever dropped — recall annotates.
      - The `uncertainty` key is distinct from `freshness`, so a row that is both
        anchor-stale and dependency-uncertain carries both, independently.
      - Surfacing-path only; never fires on touch/relevance updates.

    Mutates and returns *rows*.
    """
    from techne.core.database import connect

    rows = list(rows)
    knowledge_ids = [
        r["id"] for r in rows if r.get("source_table") == "knowledge" and r.get("id") is not None
    ]
    if not knowledge_ids:
        return rows

    placeholders = ",".join("?" * len(knowledge_ids))
    conn = connect(db_path)
    try:
        uncertain_ids = {
            row["id"]
            for row in conn.execute(
                f"""SELECT id FROM knowledge
                    WHERE id IN ({placeholders}) AND status = 'uncertain'""",
                knowledge_ids,
            )
        }
    finally:
        conn.close()

    if not uncertain_ids:
        return rows

    for r in rows:
        if r.get("source_table") == "knowledge" and r.get("id") in uncertain_ids:
            r["uncertainty"] = _uncertainty_payload()
    return rows


def list_entries(
    db_path: Path,
    type: str | None = None,
    tags: str | None = None,
    limit: int = 50,
    include_superseded: bool = False,
) -> list[dict]:
    """List knowledge entries with optional filters.

    ``include_superseded=False`` (the default) is the "what's currently live"
    view, so it also excludes soft-archived rows (``status = 'archived'``,
    techne#216) alongside superseded ones — otherwise a predecessor archived by
    `delete()` (review finding on techne#1033: a deleted replacement must not
    resurrect the entry it replaced) would still show up here as if current.
    ``include_superseded=True`` returns everything, archived or not, matching
    how it already returns superseded rows.
    """
    conn = connect(db_path)
    try:
        query = (
            "SELECT id, type, content, tags, confidence, source, created_at, updated_at "
            "FROM knowledge WHERE 1=1"
        )
        params: list = []

        if not include_superseded:
            query += " AND superseded_by IS NULL AND status != 'archived'"
        if type:
            query += " AND type = ?"
            params.append(type)
        if tags:
            for tag in tags.split(","):
                query += " AND tags LIKE ? ESCAPE '\\'"
                params.append(f"%{like_escape(tag.strip())}%")

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get(db_path: Path, entry_id: int) -> dict | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT id, type, content, tags, confidence, source,
                      superseded_by, created_at, updated_at
               FROM knowledge WHERE id = ?""",
            (entry_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete(db_path: Path, entry_id: int) -> bool:
    """Delete one knowledge entry, and everything that points at it.

    Two foreign keys reference `knowledge.id` with no ON DELETE clause, and every
    connection enforces them: `knowledge_deps` (an edge a dream-promote drew to or
    from this entry) and `knowledge.superseded_by` (an older entry this one
    replaced). A bare DELETE fails on either with `FOREIGN KEY constraint failed`,
    which is how a seeder retiring one stale entry crash-looped an agent at boot
    (2026-09-24).

    The predecessors this entry replaced (rows with `superseded_by = entry_id`)
    are never resurrected — `superseded_by IS NULL` is how the rest of techne
    (list_entries, FTS/vector recall, egress) recognises a live row, so simply
    nulling it would bring a retired fact back as current (review finding on
    techne#1033):
      - If `entry_id` itself has a live successor (it was later superseded), its
        predecessors are repointed at that successor — the chain just skips the
        deleted link, `superseded_by` stays non-NULL, and nothing else changes.
      - If `entry_id` is the head of its chain (nothing superseded it), its
        predecessors have no successor to point at. They are archived
        (`status = 'archived'`, techne#216) instead of left live.

        THIS IS A NEW KIND OF ARCHIVED ROW (review round 2 on techne#1033), not
        "same as any other soft-archived row": `archive_scan` only ever archives a
        row that was already `uncertain` and had decayed below the confidence
        floor, so a status-blind reader that missed the archived check did little
        harm. A row archived here can be a previously `current`, `confidence='high'`
        fact — exactly what the high-confidence readers below select. Every reader
        that decides "is this a live fact worth injecting/promoting/comparing" on
        `superseded_by IS NULL` alone had to be checked, not just the four that
        already filtered `status`. Fixed to also exclude `status = 'archived'`:
        `list_entries` (this module), `search`/`search_fts`/`search_vec` with
        `include_archived=False` (search.py, unchanged — already correct),
        Tier-0 egress (egress.py, unchanged — already correct), `get_resume_context`
        (state.py — feeds `techne_resume`/`techne context`), `consolidate_clusters`
        and `recombine_groups` (sleepdream.py — dream/consolidate candidate
        selection), `select_conflict_candidates` and `conflict_check_emit`
        (conflict.py), and `_merge_in_conn`'s live-row guard (this module — refuses
        to merge an archived row, same as it already refuses a superseded one).

        `knowledge_export.query_live_knowledge` itself is left unchanged and keeps
        returning the full set with `status` alongside each row — it has two
        consumers, and they don't agree on what "filter for itself" means. The
        export renderer (`knowledge_export.py`) does read `status` and labels the
        row. `push_knowledge_to_archivist` (archivist.py, review round 3 on
        techne#1033) did not: it iterated every row, skipped only
        `REDIRECTED_TYPES`, and `store()`d an archived predecessor into the team DB
        as a fresh `current` row — publishing the round-1 resurrection onto the one
        path that leaves this machine, then returning it through federated recall
        (`fan_out_search`). Fixed at the push call site next to the
        `REDIRECTED_TYPES` skip, for the same reason that skip lives there and not
        in `query_live_knowledge`: this function is shared with the export path and
        must keep returning the full set.

    Dependents of `entry_id` (edges where `entry_id` is the trigger) are flipped
    to `uncertain` before the edges are dropped, mirroring `supersede()`'s
    `_cascade_uncertain` (techne#117): deleting a trigger is a stronger retirement
    than superseding it, so it must not leave dependents looking more trustworthy
    than a supersede would have.

    Left alone deliberately, none of it a foreign key: `knowledge_vec` (swept
    later by `_purge_orphaned_vec`), `knowledge_candidates.promoted_to_id` /
    `.target_ids`, and `context_injections.hit_ids` (audit history).

    One transaction, so a failure part-way leaves nothing half-removed.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT superseded_by FROM knowledge WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is not None:
            successor_id = row["superseded_by"]
            if successor_id is not None:
                conn.execute(
                    "UPDATE knowledge SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE superseded_by = ?",
                    (successor_id, entry_id),
                )
            else:
                conn.execute(
                    "UPDATE knowledge SET superseded_by = NULL, status = 'archived', "
                    "updated_at = CURRENT_TIMESTAMP WHERE superseded_by = ?",
                    (entry_id,),
                )
        # Dependency cascade (techne#117), same as supersede(): flip live dependents
        # of entry_id to `uncertain` BEFORE the edges that record the relationship
        # are dropped below.
        _cascade_uncertain(conn, entry_id)
        conn.execute(
            "DELETE FROM knowledge_deps WHERE dependent_id = ? OR trigger_id = ?",
            (entry_id, entry_id),
        )
        # lesson_meta (techne#1195) references knowledge by FK like the two above:
        # drop this entry's row, and unlink family entries that named it as parent.
        conn.execute("DELETE FROM lesson_meta WHERE knowledge_id = ?", (entry_id,))
        conn.execute("UPDATE lesson_meta SET parent_id = NULL WHERE parent_id = ?", (entry_id,))
        cur = conn.execute("DELETE FROM knowledge WHERE id = ?", (entry_id,))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def content_fingerprint(content: str) -> str:
    """Stable hash of knowledge CONTENT — used for dedup + version compare.

    Renamed from ``content_hash`` (techne#self-review): that collided with the
    ``content_hash`` *column*, which stores the anchored FILE's hash (a different
    thing) — see anchor.py. The two are now lexically distinct.
    """
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ---- Decay-aware confidence ----------------------------------------------


def decay_score(
    confidence_numeric: float,
    last_confirmed_at: "str | datetime | None",
    half_life_days: float,
    status: "str | None" = None,
    *,
    now: "datetime | None" = None,
) -> float:
    """Pure decay formula — no DB access.

    Formula: ``confidence_numeric * 0.5 ** (days_since_confirmed / half_life_days)``

    When ``half_life_days <= 0`` or ``last_confirmed_at`` is absent/unparseable,
    returns ``confidence_numeric`` directly (decay disabled; no timestamp → treat
    as fresh). The ``UNCERTAIN_CONFIDENCE_PENALTY`` (×0.5) is applied afterwards
    when ``status == 'uncertain'``, regardless of whether time decay was computed
    — it is a trust penalty, not a decay penalty (techne#117).

    Args:
        confidence_numeric: The base stored confidence (0..1).
        last_confirmed_at:  ISO/space-separated timestamp of last confirmation,
                            or a ``datetime`` object, or None.
        half_life_days:     Half-life for exponential decay.  0 or negative
                            disables time decay.
        status:             Row status string; ``'uncertain'`` triggers the
                            ``UNCERTAIN_CONFIDENCE_PENALTY`` multiplier.
        now:                Reference instant for age calculation.  Defaults to
                            ``datetime.now(timezone.utc)``; pass an explicit
                            value in tests to pin time.
    """
    base = float(confidence_numeric)
    if half_life_days <= 0 or not last_confirmed_at:
        value = base
    else:
        try:
            if isinstance(last_confirmed_at, datetime):
                last = last_confirmed_at
            else:
                last = _parse_timestamp(last_confirmed_at)
            if last.tzinfo is None:
                # SQLite returns naive timestamps; treat them as UTC.
                last = last.replace(tzinfo=timezone.utc)
            _now = now or datetime.now(timezone.utc)
            if _now.tzinfo is None:
                _now = _now.replace(tzinfo=timezone.utc)
            days_since = max(0.0, (_now - last).total_seconds() / 86400.0)
            value = base * (0.5 ** (days_since / half_life_days))
        except (ValueError, TypeError):
            value = base
    # techne#117 recall gate: an uncertain row (a dependency it rests on was
    # superseded) ranks below an otherwise-equal current row until reconfirmed.
    # Applied regardless of whether time decay ran — this is a trust penalty.
    if status == "uncertain":
        value *= UNCERTAIN_CONFIDENCE_PENALTY
    return value


def effective_confidence(
    db_path: Path,
    entry_id: int,
    *,
    now: datetime | None = None,
    half_life_days: float = DEFAULT_CONFIDENCE_HALF_LIFE_DAYS,
) -> float | None:
    """Return the decay-adjusted confidence of an entry.

    Formula: ``confidence_numeric * 0.5 ** (days_since_confirmed / half_life_days)``

    A re-affirmed entry (where ``last_confirmed_at`` was bumped recently)
    holds most of its weight. An entry that hasn't been seen in
    ``half_life_days`` is at half strength; double that, quarter strength;
    etc.

    ``half_life_days=0`` disables decay entirely — useful for operators
    who want the raw stored value, or for entries (like architectural
    patterns) where staleness isn't meaningful. The `uncertain` down-rank
    (techne#117) still applies in that mode: it is a trust penalty, not decay.

    Returns None if no such entry exists.
    """
    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT confidence_numeric, last_confirmed_at, status
               FROM knowledge WHERE id = ?""",
            (entry_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return decay_score(
        row["confidence_numeric"],
        row["last_confirmed_at"],
        half_life_days,
        row["status"],
        now=now,
    )


def reaffirm(db_path: Path, entry_id: int) -> bool:
    """Bump ``last_confirmed_at`` to NOW without creating a new revision.

    Use this when an agent re-encounters a fact and finds it still true —
    refreshes the entry's effective confidence without supersession. Returns
    True iff the entry exists.
    """
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """UPDATE knowledge
               SET last_confirmed_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (entry_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---- Soft-archive lifecycle (techne#216 / coherence D2) --------------------

# Conservative defaults — archiving should be hard to trigger accidentally.
# Evidence: aggressive lifecycle costs precision; bias the floor low and the
# reaffirmation window long so only genuinely stale, uncertain entries qualify.
DEFAULT_ARCHIVE_CONFIDENCE_FLOOR = 0.1
DEFAULT_ARCHIVE_REAFFIRMATION_WINDOW_DAYS = 180


def archive_scan(
    db_path: Path,
    *,
    confidence_floor: float = DEFAULT_ARCHIVE_CONFIDENCE_FLOOR,
    reaffirmation_window_days: int = DEFAULT_ARCHIVE_REAFFIRMATION_WINDOW_DAYS,
    half_life_days: float = DEFAULT_CONFIDENCE_HALF_LIFE_DAYS,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Soft-archive scan — move stale, uncertain, unreaffirmed entries out of default recall.

    An entry qualifies for archiving when ALL THREE of these hold:
      1. status = 'uncertain'         (a dependency it rests on was superseded)
      2. effective_confidence() < confidence_floor  (decayed below the floor)
      3. last_confirmed_at older than reaffirmation_window_days (not recently reaffirmed)

    Action: sets status = 'archived'. This is a RECALL VISIBILITY change, not a
    content edit — the row is NEVER hard-deleted. Archived entries are:
      - Excluded from Tier-0 egress (select_tier0_injection — the default session-start
        injection). Bounds growth of the default working set.
      - Excluded from techne_recall (operator-topic-scoped pull).
      - Still returned by explicit knowledge search (techne_search / search_fts).
      - Fully reversible via unarchive().

    This is an EXPLICIT operator-invoked action (not a silent background sweep) because:
      - It changes recall visibility, which affects every future session.
      - It's reversible but the change has immediate operational effect.
      - The codebase precedent for reversible status changes (reconfirm, reaffirm)
        is explicit — mirrored here.

    Returns a list of archived-entry dicts: {id, content, status, effective_confidence}.
    When dry_run=True, returns the same list without writing to the DB (no-op).

    Config levers (conservative defaults):
      - confidence_floor: 0.1  (only archive deeply decayed entries)
      - reaffirmation_window_days: 180  (6 months without reaffirmation)
    """
    _now = now or datetime.now(timezone.utc)
    cutoff_dt = _now - timedelta(days=reaffirmation_window_days)
    # SQLite stores timestamps as "YYYY-MM-DD HH:MM:SS"; format the cutoff to match.
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    conn = connect(db_path)
    try:
        # Fetch all uncertain, live (non-superseded) entries whose last_confirmed_at
        # predates the reaffirmation window. We pull their decay columns so we can
        # compute effective_confidence() in Python (same formula as the public API)
        # without an extra per-row DB round-trip.
        candidates = conn.execute(
            """SELECT id, content, confidence_numeric, last_confirmed_at, status
               FROM knowledge
               WHERE superseded_by IS NULL
                 AND status = 'uncertain'
                 AND last_confirmed_at < ?""",
            (cutoff_str,),
        ).fetchall()
    finally:
        conn.close()

    # Filter by effective_confidence floor.
    to_archive: list[dict] = []
    for row in candidates:
        eff = decay_score(
            row["confidence_numeric"],
            row["last_confirmed_at"],
            half_life_days,
            row["status"],
            now=_now,
        )
        if eff < confidence_floor:
            to_archive.append(
                {
                    "id": row["id"],
                    "content": row["content"],
                    "status": row["status"],
                    "effective_confidence": eff,
                }
            )

    if not to_archive or dry_run:
        return to_archive

    # Candidates are read on one connection above and UPDATEd on a fresh one below,
    # and MCP sessions reconfirm concurrently (techne#1269). The UPDATE re-checks
    # the selection condition, so a row reconfirmed in between is left alone, and
    # only the rows it actually archived are returned.
    conn = connect(db_path)
    archived: set[int] = set()
    try:
        # One UPDATE per row, in one transaction, so rowcount says which rows were
        # still eligible (RETURNING would need SQLite 3.35).
        for r in to_archive:
            cur = conn.execute(
                """UPDATE knowledge
                   SET status = 'archived', updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?
                     AND superseded_by IS NULL
                     AND status = 'uncertain'
                     AND last_confirmed_at < ?""",
                (r["id"], cutoff_str),
            )
            if cur.rowcount:
                archived.add(r["id"])
        conn.commit()
    finally:
        conn.close()
    to_archive = [r for r in to_archive if r["id"] in archived]

    return to_archive


def unarchive(db_path: Path, entry_id: int) -> bool:
    """Reverse a soft-archive: restore an archived entry to 'uncertain' status.

    Archived entries were moved out of default recall by archive_scan(). This is
    the EXPLICIT reversal — it does not reconfirm the entry (the dependency that
    caused it to become uncertain is still unresolved). Use reconfirm() separately
    if the dependency has been resolved and the entry should return to 'current'.

    Returns True if the entry was archived and has now been restored, False if it
    was not found or was not in 'archived' status.
    """
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """UPDATE knowledge
               SET status = 'uncertain', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'archived'""",
            (entry_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _parse_timestamp(value) -> datetime:
    """Parse a SQLite timestamp value into a datetime. Accepts both string
    and datetime forms because connect() has detect_types enabled in some
    paths but not others.

    Handles all timestamp formats seen in practice:
    - Timezone-aware ISO 8601:  ``"2024-07-06T21:09:39.617094+00:00"``
    - SQLite space-separated:   ``"2024-07-06 21:09:39"`` / ``"2024-07-06 21:09:39.123456"``
    - UTC sentinel "Z" suffix:  ``"2024-07-06T21:09:39Z"`` (stripped before strptime)
    """
    if isinstance(value, datetime):
        return value
    s = str(value)
    # fromisoformat handles timezone-aware strings (e.g. "+00:00") and the
    # standard space separator; try it first before the strptime fallback.
    try:
        return datetime.fromisoformat(s.replace(" ", "T"))
    except ValueError:
        pass
    # Fallback: strip "Z" suffix and parse with explicit strptime formats.
    s_stripped = s.replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s_stripped, fmt)
        except ValueError:
            continue
    raise ValueError(f"could not parse timestamp {value!r}")
