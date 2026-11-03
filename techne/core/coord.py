"""Inter-session coordination mailbox (techne#376).

Lets concurrent Claude Code sessions converse without a human router. A message
targets a **channel** (default ``'coord'``, a broadcast everyone watches) or a
specific ``session_id`` (direct). Delivery is push: the post_tool_use hook injects
unread messages into the agent's context on its next tool call, and the
session-start banner surfaces unread + who's live.

Identity is the **self-asserted** session_id (``CLAUDE_CODE_SESSION_ID`` or a
caller-supplied value), consistent with the rest of techne — NO bearer token. The
trust boundary is the local machine: anything that can write to techne.db can do
anything already, so a token would defend a door in a wall that isn't there.

Presence is read-only: ``live_sessions()`` scans Claude Code's own per-session
sidecars (``~/.claude/sessions/<pid>.json``) — no state of our own to keep in sync.

Timestamp convention: ISO text ("YYYY-MM-DD HH:MM:SS"), matching every other
TIMESTAMP column in techne.db.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from techne.core.redaction import redact_credentials

# A Claude Code session_id is a v4 UUID. Used to tell a real (possibly offline but
# deliverable) session_id target from an unresolved display-name (a dead letter).
_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

# The default broadcast channel. For the common <=2-instance case, every session
# watches 'coord' and nobody needs to know anyone else's session_id to be heard.
DEFAULT_CHANNEL = "coord"

# A session sidecar older than this (by updatedAt) is treated as not-live for
# presence. Generous — CC stamps updatedAt on activity; a truly dead session's
# file lingers with a stale timestamp.
_PRESENCE_FRESH_SECONDS = 90 * 60


def _now_iso() -> str:
    """UTC now as an ISO text timestamp matching the TIMESTAMP column convention."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


#: Namespace prefixes for ``coord_reads.reader_session``. The column holds two
#: KINDS of identity, and an unprefixed value cannot say which it is — so they are
#: tagged rather than left to be inferred from shape (techne#446).
READER_NAME = "name:"
READER_UUID = "uuid:"

#: How many of the newest broadcast messages a reader with NO receipts sees on
#: its first read (techne#1274). Without a floor such a reader was shown the whole
#: bus back to message 1: 832 messages and 873,339 bytes of body text in one
#: post_tool_use injection on 2026-10-12. Messages addressed to the session
#: directly are never floored.
FIRST_READ_CATCHUP = 20

#: Body characters one post_tool_use injection carries before the rest waits for
#: a later tool call or an explicit techne_inbox (techne#1274). A total, not a
#: per-message cap; the per-message and count caps are issue 1252.
INJECT_MAX_CHARS = 8000
#: Messages and body characters per message one injection shows (techne#1252).
#: The rest waits for a later tool call or techne_inbox, which reads them whole.
INJECT_MAX_MESSAGES = 5
INJECT_MAX_BODY = 1500


def _same_process_pids(recs: list[dict]) -> set[int]:
    """Which of these sidecars' pids are still the process the sidecar describes?

    Tells a dormant twin (alive, same process) from a corpse whose pid was
    recycled (alive, different process) — the guard that makes a TTL-free rival
    check safe instead of restoring #446.

    COMPARE AS EPOCHS: procStart is UTC, `ps -o lstart=` prints local; the two
    renderings of one instant differ by the UTC offset. ONE ps for all
    candidates — per-candidate calls cost ~6ms each on the post_tool_use path.
    Unknown/unparseable -> absent, i.e. pre-#492 behaviour. DST fall-back hour:
    mktime guesses tm_isdst and can miss by 3600s; fails safe (absent).
    """
    # pid -> the procStart(s) claimed for it. A SET, not a scalar: two same-name
    # sidecars can carry one pid (a corpse whose pid was recycled onto a running
    # twin), and a dict comprehension keeps only the last write — which can be
    # the corpse, losing the twin and reinstating #492 for the exact case this
    # exists to catch.
    wanted: dict[int, set[str]] = {}
    for r in recs:
        if isinstance(r.get("pid"), int) and r["pid"] > 0 and r.get("procStart"):
            wanted.setdefault(r["pid"], set()).add(r["procStart"])
    return {pid for pid, verdict in _classify_pids(wanted).items() if verdict is True}


def _recycled_pids(recs: list[dict]) -> set[int]:
    """Pids we have POSITIVE evidence are no longer the process the sidecar names.

    THE COMPLEMENT OF `_same_process_pids` IS NOT THIS SET, and the difference is
    the whole reason this exists (techne#587). "Not verified same" lumps together
    a genuine mismatch with every unknown — no `procStart` recorded, `ps` silent
    about the pid, an unparseable `lstart`, DST ambiguity. Demoting on that union
    would turn each unknown into a FALSE ABSENT, and a false absent is the worse
    error here: it refuses a session that is genuinely working, where a false
    present merely restores the older, weaker behaviour.

    So this returns only pids where `ps` gave us a start time, the sidecar claimed
    one, and they DISAGREE. Everything else is absent from the result and stays
    trusted.
    """
    wanted: dict[int, set[str]] = {}
    for r in recs:
        if isinstance(r.get("pid"), int) and r["pid"] > 0 and r.get("procStart"):
            wanted.setdefault(r["pid"], set()).add(r["procStart"])
    return {pid for pid, verdict in _classify_pids(wanted).items() if verdict is False}


#: How long one `ps` snapshot may be reused. See `_classify_pids`.
_PS_CACHE_TTL_S = 2.0
_ps_cache: dict[tuple[int, ...], tuple[float, dict[int, int]]] = {}


def _ps_starts(pids: tuple[int, ...]) -> dict[int, int]:
    """pid -> process start time as an epoch, for the pids `ps` actually reports.

    ONE ps FOR ALL CANDIDATES, AND CACHED, because cost is why hardening
    `present_session_ids` was declined once already rather than an oversight
    (techne#587). Measured on this host: `present_session_ids()` 0.005 ms against
    one `_same_process_pids()` at 31.9 ms — a ~6,400x step, on a path that runs
    for every coord MCP invocation resolving identity. A 2s window collapses a
    burst to a single `ps` while staying far shorter than any interval over which
    a pid could plausibly be recycled, so it buys the cost back without weakening
    the claim in any way that matters.

    A pid ABSENT from the result is UNKNOWN, NOT DEAD — `ps` may not list it, or
    its `lstart` may not parse. Callers must never read absence as evidence; that
    is the false-absent trap `_recycled_pids` exists to avoid.
    """
    if not pids:
        return {}
    now = time.monotonic()
    hit = _ps_cache.get(pids)
    if hit and now - hit[0] < _PS_CACHE_TTL_S:
        return hit[1]
    starts: dict[int, int] = {}
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,lstart=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return {}  # NOT cached: a failed probe must not pin an empty answer.
    if out.returncode != 0:
        return {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            starts[int(parts[0])] = int(
                time.mktime(time.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y"))
            )
        except (ValueError, OverflowError):
            continue
    _ps_cache[pids] = (now, starts)
    return starts


def _classify_pids(wanted: dict[int, set[str]]) -> dict[int, bool]:
    """pid -> True (same process), False (recycled), or ABSENT (unknown).

    Three-valued on purpose. Collapsing unknown into either answer is how this
    check goes wrong in one direction or the other — see `_recycled_pids`.
    """
    if not wanted:
        return {}
    starts = _ps_starts(tuple(sorted(wanted)))
    verdicts: dict[int, bool] = {}
    for pid, claims in wanted.items():
        actual = starts.get(pid)
        if actual is None:
            continue  # ps said nothing — unknown, not an answer
        # Parse first, judge second. An UNPARSEABLE claim proves nothing, so it
        # must not count as a disagreement — that would manufacture a recycled
        # verdict out of a formatting problem.
        parsed: list[int] = []
        for claimed in claims:
            try:
                parsed.append(int(calendar.timegm(time.strptime(claimed, "%a %b %d %H:%M:%S %Y"))))
            except (ValueError, OverflowError):
                continue
        if not parsed:
            continue  # nothing comparable — unknown
        verdicts[pid] = actual in parsed
    return verdicts


def _durable_name(session_id: str, records: list[dict] | None = None) -> str | None:
    """This session's display name, but ONLY when it identifies it UNIQUELY.

    AMBIGUITY IS THE LIMIT OF THIS WHOLE IDEA, and it is not theoretical:
    ``resolve_recipient``'s own docstring says duplicate ``-n`` names are
    reachable. A name is a durable identity ACROSS TIME (the point of #446) but
    not necessarily ACROSS CONCURRENCY — and merging identity IS merging
    cardinality when two LIVE sessions answer to one name. The first cut of #446
    merged them: one ``poe_1`` marking a message read emptied the other's inbox
    silently, and because the authorship filter matched on the name too, two
    same-named peers were completely invisible to each other. That is a
    REGRESSION — distinct UUIDs used to work — and it was the one case that
    contradicted the invariant this change claimed to hold (#490 review).

    So: shared name → no durable identity → fall back to the UUID. The cost is
    that two concurrent same-named sessions each re-read the backlog, which is
    the OLD, noisy behaviour. That is the right direction to fail. Noise is
    visible and self-correcting; a silently swallowed broadcast on a channel
    whose whole purpose is "tell the other session what I am about to do" is not.

    ``records`` lets a caller pass sidecar records it has already read, so one
    read serves both the receipt key and the authorship filter (see
    :func:`_identity`).
    """
    recs = _session_records() if records is None else records
    mine = next((r for r in recs if r.get("sessionId") == session_id), None)
    name = mine.get("name") if mine else None
    if not name:
        return None
    # Another LIVE session answering to the same name makes it ambiguous. Only
    # live ones count: a dead session's sidecar lingers (presence is TTL+reap,
    # never deregistered), and treating those as rivals would permanently demote
    # a name that is in fact unique right now.
    # Reuse the records already read — live_sessions() would otherwise re-glob and
    # re-parse the whole directory, which is the SECOND scan that made the "read
    # the sidecars once" fix untrue on the named path (see :func:`_identity`).
    live_ids = {s["session_id"] for s in live_sessions(records=recs)}
    same_name = [r for r in recs if r.get("name") == name and r.get("sessionId") != session_id]
    # Rival = heartbeat-fresh OR dormant-but-running. live_sessions() drops on
    # updatedAt before checking the pid, so a twin idle past the TTL was
    # invisible (#492). Do NOT simplify to a pid-only check: a recycled pid on a
    # lingering sidecar would then demote the name forever, which is #446.
    dormant = [
        r for r in same_name if r.get("sessionId") not in live_ids and _pid_alive(r.get("pid"))
    ]
    still_running = _same_process_pids(dormant) if dormant else set()
    rivals = [
        r for r in same_name if r.get("sessionId") in live_ids or r.get("pid") in still_running
    ]
    return None if rivals else name


def _identity(session_id: str) -> tuple[str | None, str]:
    """``(durable_name_or_None, reader_key)`` — resolved ONCE per public call.

    WHY THIS EXISTS AT ALL: the first cut called ``session_name()`` 2-3 times per
    ``inbox()``/``unread_count()``/``mark_read()``, each one re-globbing and
    re-parsing every sidecar in ``~/.claude/sessions`` with no cache. That is
    ~6ms of filesystem work per tool call on the post_tool_use hot path, landing
    in the commit right after the one that took hooks from 100ms to 50ms (#490
    review). Resolving once and threading the result down removes the duplication
    outright rather than caching around it.
    """
    records = _session_records()
    name = _durable_name(session_id, records)
    key = f"{READER_NAME}{name}" if name else f"{READER_UUID}{session_id}"
    return name, key


def reader_key(session_id: str) -> str:
    """The identity a read receipt is filed under — DURABLE where one exists.

    THE BUG THIS EXISTS TO FIX (techne#446): receipts were keyed on the session
    UUID, which Claude Code mints fresh per launch. A relaunched session had zero
    receipts and re-read the entire backlog, so the unread count only ever grew:
    97 messages spanning the bus's whole life (2026-07-17 onward) reported as
    "unread" on a machine where they had all been read, repeatedly, by earlier
    incarnations of the same agent. Measured before the fix: 291 receipt rows
    across 10 reader sessions, none of which helped its successor.

    The display name (the CLI ``-n`` alias, e.g. ``poe_1``) survives relaunch, so
    it is the right key when present. FOUR CONSEQUENCES, each a real choice:

    1. A RECYCLED NAME INHERITS RECEIPTS. A fresh ``poe_2`` skips what the
       previous ``poe_2`` read but it never saw. Deliberate: the name denotes a
       persistent ROLE, not a process — that is exactly why it is stable enough to
       key on. The cost is bounded (a missed broadcast on a channel whose messages
       are announcements, not commands); the alternative is the bug above.
    2. AN UNNAMED SESSION KEEPS THE OLD BEHAVIOUR. No ``-n`` means no durable
       identity to offer, so it falls back to the UUID and re-reads on each
       launch. Stated here rather than discovered.
    3. RECEIPTS DO NOT TRANSFER when a session gains a name it did not have
       before; the pre-fix UUID rows stay UUID-keyed. The backlog drains once more
       per name, then stops. See the migration note in database.py step 21.
    4. A NAME SHARED BY TWO LIVE SESSIONS IS NOT DURABLE — see
       :func:`_durable_name`. Both fall back to the UUID rather than merging into
       one reader.

    Public and standalone for callers that want just the key; the internal paths
    use :func:`_identity` so one sidecar read serves the whole call.
    """
    return _identity(session_id)[1]


def resolve_recipient(to: str, records: list[dict] | None = None) -> str:
    """Resolve a send target to the value stored in ``recipient`` (techne#376 phase 2).

    Resolution order:
    1. the broadcast channel ``'coord'`` — passes through unchanged;
    2. a live session's display name (the CLI ``-n`` alias, e.g. 'claude_2') — resolved
       to that session's ``session_id`` for a direct message;
    3. a literal session_id (v4 UUID shape) — passes through; it's deliverable when that
       session next reads its inbox, even if currently offline.

    Anything else — a name that matches no live session and isn't a session_id — is a
    **dead letter**: a bare name never matches any inbox filter, so the message could
    never be read. Rather than store it and return a misleading id, we RAISE. (A single
    mistyped name like 'claude2' for 'claude_2' would otherwise vanish silently — this
    repo's "No Silent Failures" rule.) Newest-active session wins a name tie (first
    match over the activity-sorted list), which is only reachable via duplicate -n names.

    ``records`` lets a caller that already read the sidecars hand them in (#494);
    it must be the COMPLETE unfiltered set, since `live_sessions` is the filter.
    """
    if to == DEFAULT_CHANNEL:
        return to
    for s in live_sessions(records=records):
        if s.get("name") == to:
            return s["session_id"]
    if _SESSION_ID_RE.match(to):
        return to
    raise ValueError(
        f"coord: no live session named {to!r} (and it is not a session_id or a channel). "
        f"Use 'coord' to broadcast, or a session_id to reach a peer that's currently offline."
    )


def _channels_for(session_id: str, channels: list[str] | None) -> list[str]:
    """Recipients a session's inbox covers: its watched channels plus itself
    (direct messages). Defaults to the single broadcast channel."""
    chans = list(channels) if channels is not None else [DEFAULT_CHANNEL]
    if session_id not in chans:
        chans = chans + [session_id]
    return chans


def send(
    db_path: Path,
    from_session: str,
    body: str,
    *,
    to: str = DEFAULT_CHANNEL,
    from_name: str | None = None,
    thread_id: str | None = None,
    reply_to: int | None = None,
) -> int:
    """Post a message and return its id.

    ``to`` is a channel name (e.g. ``'coord'``), a live session's display-name alias,
    or a target session_id. ``from_name`` is a display-name snapshot (resolved via
    ``session_name()`` if omitted).

    Threading (techne#376 phase 2): pass ``reply_to=<message_id>`` to attach this
    message to that message's conversation — it inherits the parent's ``thread_id``,
    or, if the parent had none, both are grouped under a new thread keyed on the
    parent's id. An explicit ``thread_id`` takes precedence over ``reply_to``.

    ``body`` is passed through ``redact_credentials`` before storage (techne#1037):
    it is free text, and every live session's inbox nudge replays it.
    """
    from techne.core.database import connect

    # ONE sidecar read for both lookups (#494). They answer DIFFERENT questions —
    # "what is MY name?" vs "who is THAT name?" — so this threads the read, it
    # does not share a result. Skipped entirely when neither needs it.
    records = None
    if from_name is None or to != DEFAULT_CHANNEL:
        records = _session_records()
    if from_name is None:
        from_name = session_name(from_session, records)
    # NOT _identity()'s durable name: that is None when a rival holds the same
    # name, which would blank from_name on a twin's rows and change the
    # authorship filter. send snapshots what this session IS CALLED.
    recipient = resolve_recipient(to, records)  # channel passthrough, or name -> session_id
    conn = connect(db_path)
    try:
        if reply_to is not None and thread_id is None:
            parent = conn.execute(
                "SELECT thread_id FROM coord_messages WHERE id = ?", (reply_to,)
            ).fetchone()
            if parent is not None:
                thread_id = parent["thread_id"] or str(reply_to)
                if parent["thread_id"] is None:
                    # Pull the standalone parent into the new thread so the pair groups.
                    # Race-safe by construction (intentional — no lock): the derived
                    # thread_id is deterministic (str(reply_to)), so two sessions replying
                    # to the same parent at once write the identical value, and this only
                    # fires while the parent is still NULL — concurrent re-replies are no-ops.
                    conn.execute(
                        "UPDATE coord_messages SET thread_id = ? WHERE id = ?",
                        (thread_id, reply_to),
                    )
        cur = conn.execute(
            "INSERT INTO coord_messages (from_session, from_name, recipient, thread_id, body, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (from_session, from_name, recipient, thread_id, redact_credentials(body), _now_iso()),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def threads(
    db_path: Path, session_id: str, *, channels: list[str] | None = None, limit: int = 20
) -> list[dict]:
    """Conversation threads (messages sharing a thread_id) visible to ``session_id``,
    most-recently-active first (techne#376 phase 2).

    Only messages the session could see are included (its channels, direct-to-it, or
    from it), so a thread surfaces with exactly the turns this session is party to.
    Each thread: ``{thread_id, count, last_at, messages: [...]}`` — note ``count`` is
    the number of VISIBLE turns, which can be fewer than the thread's true length if
    some replies were directed to a different recipient the reader can't see.
    """
    from techne.core.database import connect

    chans = _channels_for(session_id, channels)
    placeholders = ",".join("?" for _ in chans)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT id, from_session, from_name, recipient, thread_id, body, created_at"
            f"  FROM coord_messages"
            f" WHERE thread_id IS NOT NULL"
            f"   AND (recipient IN ({placeholders}) OR from_session = ?)"
            f" ORDER BY id",
            (*chans, session_id),
        ).fetchall()
    finally:
        conn.close()

    by_thread: dict[str, list[dict]] = {}
    for r in rows:
        by_thread.setdefault(r["thread_id"], []).append(dict(r))
    out = [
        {"thread_id": tid, "count": len(msgs), "last_at": msgs[-1]["created_at"], "messages": msgs}
        for tid, msgs in by_thread.items()
    ]
    out.sort(key=lambda t: t["last_at"], reverse=True)
    return out[:limit]


def _floor_key(reader: str) -> str:
    return f"coord:floor:{reader}"


def _author_filter(session_id: str, name: str | None) -> tuple[str, tuple]:
    """SQL excluding messages this reader wrote (see :func:`_unread_rows`)."""
    # NULL from_name (pre-#376 rows, or a sender with no -n alias) must not match
    # anyone's name — SQL's `!=` on NULL is NULL, i.e. the row is filtered out, so
    # the comparison is written to leave those rows visible.
    by_name = " AND (from_name IS NULL OR from_name != ?)" if name else ""
    clause = "AND from_session != ?" + by_name
    return clause, ((session_id, name) if name else (session_id,))


def _reader_floor(
    conn, session_id: str, channels: list[str], ident: tuple[str | None, str], *, persist: bool
) -> int:
    """Lowest broadcast message id this reader can count as unread; 0 means none.

    A READER WITH NO RECEIPTS STARTS NEAR NOW (techne#1274). Its first read sets
    the floor so that only the newest ``FIRST_READ_CATCHUP`` broadcast messages
    not written by it are above it, and stores it under ``coord:floor:<reader>``.
    The floor is computed once and kept, which is what makes it stable: once the
    reader marks those messages read it HAS receipts, and a floor recomputed from
    "no receipts" would vanish and hand it the whole backlog one call later.

    A reader that already has receipts gets no floor, so its unread set is exactly
    what it was before. Reaching this state takes no name (session started without
    ``-n``), a shared name (``reader_key`` consequence 4), or a hook replayed after
    the session's sidecar is gone. ``persist=False`` computes the floor the next
    read would set without storing it, for the read-only ``unread_count``.
    """
    from techne.core.extension_state import get_state, set_state

    key = ident[1]
    st = get_state(conn, _floor_key(key)) or {}
    if isinstance(st.get("min_id"), int):
        return st["min_id"]
    if conn.execute(
        "SELECT 1 FROM coord_reads WHERE reader_session = ? LIMIT 1", (key,)
    ).fetchone():
        return 0
    broadcast = [c for c in channels if c != session_id]
    floor = 0
    if broadcast:
        author_clause, author_params = _author_filter(session_id, ident[0])
        placeholders = ",".join("?" for _ in broadcast)
        row = conn.execute(
            f"SELECT id FROM coord_messages WHERE recipient IN ({placeholders}) {author_clause}"
            f" ORDER BY id DESC LIMIT 1 OFFSET ?",
            (*broadcast, *author_params, FIRST_READ_CATCHUP - 1),
        ).fetchone()
        floor = row["id"] if row else 0
    if persist:
        set_state(conn, _floor_key(key), {"min_id": floor}, commit=False)
    return floor


def _unread_rows(
    conn,
    session_id: str,
    channels: list[str],
    ident: tuple[str | None, str],
    *,
    persist_floor: bool = True,
):
    """Unread = on a watched channel, not authored by me, no receipt from me.

    THE SESSION UUID APPEARED TWICE HERE AND MEANT TWO DIFFERENT THINGS — which is
    why the fix is not a blanket substitution:

      * the RECEIPT subquery asks "have I read this?", which must survive relaunch
        → keyed on :func:`reader_key`;
      * the AUTHORSHIP filter asks "did I write this?", which is a property of the
        MESSAGE ROW and stays a UUID comparison.

    But authorship had the SAME ephemeral-identity bug, and it was the larger half
    of it: `from_session != <my new uuid>` is true of every message my previous
    incarnation sent, so a relaunched `poe_1` was shown its own back catalogue as
    unread. On the live store that was 58 of 97 messages — the majority of the
    noise, for the most active facet. `coord_messages.from_name` is a permanent
    snapshot taken at send time, so when this session has a name, exclude by name
    as well. Same durable-identity rule, same recycled-name caveat as
    :func:`reader_key` (techne#446).
    """
    placeholders = ",".join("?" for _ in channels)
    name, key = ident
    author_clause, author_params = _author_filter(session_id, name)
    # techne#1274: a reader with no receipts sees nothing below its floor except
    # messages addressed to it directly.
    floor = _reader_floor(conn, session_id, channels, ident, persist=persist_floor)
    return conn.execute(
        f"SELECT id, from_session, from_name, recipient, thread_id, body, created_at"
        f"  FROM coord_messages"
        f" WHERE recipient IN ({placeholders})"
        f"   {author_clause}"
        f"   AND (id >= ? OR recipient = ?)"
        f"   AND id NOT IN (SELECT message_id FROM coord_reads WHERE reader_session = ?)"
        f" ORDER BY id",
        (*channels, *author_params, floor, session_id, key),
    ).fetchall()


def inbox(
    db_path: Path,
    session_id: str,
    *,
    channels: list[str] | None = None,
    mark_read: bool = False,
) -> list[dict]:
    """Unread messages for ``session_id`` — those on a watched channel or addressed
    to it directly, not authored by it, with no read receipt yet. Oldest first.

    With ``mark_read=True`` the returned messages are marked read in the same call.

    Every call records the highest id it returned as seen by this reader, which
    bounds a bare ``mark_read`` (techne#1243). The post_tool_use injection reads
    through :func:`inbox_page`, so an injected message counts as seen.

    A reader with no receipts sees at most the newest ``FIRST_READ_CATCHUP``
    broadcast messages plus its direct messages (techne#1274, :func:`_reader_floor`).
    """
    return inbox_page(db_path, session_id, channels=channels, mark_read=mark_read)[0]


def inbox_page(
    db_path: Path,
    session_id: str,
    *,
    channels: list[str] | None = None,
    mark_read: bool = False,
    exclude_ids: set[int] | frozenset[int] = frozenset(),
    max_chars: int | None = None,
    max_messages: int | None = None,
) -> tuple[list[dict], int]:
    """:func:`inbox`, limited, returning ``(messages, not_returned)``.

    ``exclude_ids`` drops messages the caller already has (the post_tool_use
    surfaced set). ``max_chars`` stops adding messages, oldest first, once their
    bodies would pass that many characters; the first message is always
    returned, whatever its size, so a caller that renders it must bound it.
    ``max_messages`` stops after that many. ``not_returned`` counts the unread
    messages either limit left out.

    The seen mark is the highest id RETURNED, so a bare ``mark_read`` never
    covers a message cut here (techne#1243). Every unread id below it was either
    returned now or excluded as already surfaced.
    """
    from techne.core.database import connect

    chans = _channels_for(session_id, channels)
    ident = _identity(session_id)  # one sidecar read for the whole call
    conn = connect(db_path)
    try:
        rows = _unread_rows(conn, session_id, chans, ident)
        candidates = [dict(r) for r in rows if r["id"] not in exclude_ids]
        msgs = candidates if max_messages is None else candidates[:max_messages]
        if max_chars is not None:
            kept, used = [], 0
            for m in msgs:
                size = len(m["body"] or "")
                if kept and used + size > max_chars:
                    break
                kept.append(m)
                used += size
            msgs = kept
        if msgs:
            _note_seen(conn, ident[1], max(m["id"] for m in msgs))
        if mark_read and msgs:
            now = _now_iso()
            key = ident[1]
            conn.executemany(
                "INSERT OR IGNORE INTO coord_reads (message_id, reader_session, read_at)"
                " VALUES (?, ?, ?)",
                [(m["id"], key, now) for m in msgs],
            )
        conn.commit()
        return msgs, len(candidates) - len(msgs)
    finally:
        conn.close()


def unread_count(db_path: Path, session_id: str, *, channels: list[str] | None = None) -> int:
    """How many unread messages ``session_id`` has. Cheap — for the banner/hook."""
    from techne.core.database import connect

    chans = _channels_for(session_id, channels)
    ident = _identity(session_id)
    conn = connect(db_path)
    try:
        return len(_unread_rows(conn, session_id, chans, ident, persist_floor=False))
    finally:
        conn.close()


def _seen_key(reader: str) -> str:
    return f"coord:seen:{reader}"


def _seen_max(conn, reader: str) -> int | None:
    """Highest message id ``reader`` has been shown, or None if never shown any."""
    from techne.core.extension_state import get_state

    st = get_state(conn, _seen_key(reader)) or {}
    v = st.get("max_id")
    return v if isinstance(v, int) else None


def _note_seen(conn, reader: str, max_id: int) -> None:
    """Raise ``reader``'s seen high-water mark to ``max_id``. Caller commits.

    Ids are monotonic and ``inbox()`` returns every unread message at once, so a
    single high-water mark is exact: an unread id below it was in some earlier
    result this reader received."""
    from techne.core.extension_state import set_state

    cur = _seen_max(conn, reader)
    if cur is None or max_id > cur:
        set_state(conn, _seen_key(reader), {"max_id": max_id}, commit=False)


def mark_read(
    db_path: Path,
    session_id: str,
    message_ids: list[int] | None = None,
    *,
    channels: list[str] | None = None,
) -> int:
    """Mark messages read for ``session_id``. With ``message_ids`` given, marks
    exactly those; otherwise marks the unread messages this reader has been shown
    (by ``inbox()`` or the post_tool_use injection), and nothing that arrived
    after. Returns the number of new read receipts written (idempotent —
    re-marking is a no-op).

    The bound is techne#1243: injected messages carried no ids, so agents called
    this bare, and "everything currently unread" also swallowed any message a peer
    sent while the agent was thinking. That message was never injected and never
    seen, yet read."""
    from techne.core.database import connect

    ident = _identity(session_id)
    conn = connect(db_path)
    try:
        chans = _channels_for(session_id, channels)
        if message_ids is None:
            seen = _seen_max(conn, ident[1])
            if seen is None:
                return 0
            ids = [r["id"] for r in _unread_rows(conn, session_id, chans, ident) if r["id"] <= seen]
        else:
            # The first receipt ends "no receipts", so fix the floor before
            # writing one (techne#1274); a later read keeps it.
            _reader_floor(conn, session_id, chans, ident, persist=True)
            ids = list(message_ids)
        if not ids:
            conn.commit()
            return 0
        now = _now_iso()
        key = ident[1]
        cur = conn.executemany(
            "INSERT OR IGNORE INTO coord_reads (message_id, reader_session, read_at)"
            " VALUES (?, ?, ?)",
            [(mid, key, now) for mid in ids],
        )
        conn.commit()
        return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(ids)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Presence — read-only view over Claude Code's own per-session sidecars.
# ---------------------------------------------------------------------------


def _sessions_dir() -> Path:
    from techne.platforms.claude_code.paths import claude_config_dir

    return claude_config_dir() / "sessions"


def _session_records() -> list[dict]:
    out: list[dict] = []
    d = _sessions_dir()
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    return out


def session_name(session_id: str, records: list[dict] | None = None) -> str | None:
    """The display name (the CLI ``-n`` name, e.g. 'claude_1') for a session_id,
    read from its sidecar. None if unknown.

    ``records`` lets a caller that already read the sidecars hand them in (#494).
    """
    for rec in _session_records() if records is None else records:
        if rec.get("sessionId") == session_id:
            return rec.get("name")
    return None


def _pid_alive(pid: object) -> bool:
    """True if ``pid`` is a live process on THIS host (techne#380).

    All coord sessions are same-host — the roster is ``~/.claude/sessions`` on this
    machine — so ``os.kill(pid, 0)``, a permission-less existence probe, is valid
    for every row. ``ProcessLookupError`` = the process is gone; ``PermissionError``
    = it exists but is owned by another user (still alive). A missing/non-int pid
    can't be disproven, so treat it as alive and let the freshness TTL bound it
    (fail open — never reap on ambiguity).
    """
    if not isinstance(pid, int) or pid <= 0:
        return True  # unverifiable → don't reap; the TTL still applies
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        # PermissionError (alive, not ours) or anything odd → fail open.
        # OverflowError is NOT an OSError: a pid >= 2**31 does not fit a C int and
        # os.kill raises before probing anything. This was ALWAYS reachable, not newly
        # so — live_sessions checks the TTL BEFORE calling here (see the ordering the
        # _durable_name comment above already relies on), so a FRESH sidecar with such
        # a pid always raised; only STALE ones were discarded first. present_session_ids
        # widened the door rather than opening it (techne#581 round 3 corrected the
        # opposite claim). Fail open, per "never reap on ambiguity" above — an
        # unprobeable pid is unverifiable, not dead.
        return True
    return True


def live_sessions(
    *,
    exclude_session: str | None = None,
    fresh_seconds: int = _PRESENCE_FRESH_SECONDS,
    records: list[dict] | None = None,
) -> list[dict]:
    """Sessions Claude Code currently considers live, newest-activity first.

    Reads ``~/.claude/sessions/*.json`` and keeps records whose ``updatedAt`` is
    within ``fresh_seconds`` AND whose ``pid`` is a live process (techne#380 —
    heartbeat freshness alone advertised dead PIDs as routable sessions during the
    TTL window; a caller would route to a corpse). Returns dicts:
    ``{session_id, name, cwd, status, pid, updated_at_ms}``.

    ``records`` lets a caller that has ALREADY read the sidecars hand them in, so
    the directory is globbed and parsed once per call rather than once per reader
    (see :func:`_identity`). Default ``None`` reads them itself — every existing
    caller is unaffected. IT MUST BE THE COMPLETE, UNFILTERED ``_session_records()``
    OUTPUT: this function IS the filter, so a pre-filtered roster does not raise —
    it silently yields a NARROWER liveness verdict. Hand it only same-cwd records
    and ``_durable_name`` sees no rivals and wrongly promotes an ambiguous name.
    """
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    live: list[dict] = []
    for rec in _session_records() if records is None else records:
        sid = rec.get("sessionId")
        if not sid or sid == exclude_session:
            continue
        updated = rec.get("updatedAt") or 0
        if not isinstance(updated, (int, float)):
            continue  # malformed sidecar (non-numeric updatedAt) — treat as not-live
        if now_ms - updated > fresh_seconds * 1000:
            continue
        if not _pid_alive(rec.get("pid")):
            continue  # reap-on-read (techne#380): dead PID inside the TTL window
        live.append(
            {
                "session_id": sid,
                "name": rec.get("name"),
                "cwd": rec.get("cwd"),
                "status": rec.get("status"),
                "pid": rec.get("pid"),
                "updated_at_ms": updated,
            }
        )
    live.sort(key=lambda r: r["updated_at_ms"], reverse=True)
    return live


def present_session_ids(records: list[dict] | None = None) -> set[str]:
    """Session ids observably present on this host — WITH OR WITHOUT a heartbeat.

    DELIBERATELY NOT :func:`live_sessions`, and the difference is not cosmetic.
    ``live_sessions`` requires a FRESH ``updatedAt``, which Claude Code writes only
    for ``entrypoint: "cli"`` (interactive) sessions. A headless ``claude -p`` run
    (``entrypoint: "sdk-cli"`` — every agent persona on this box) writes a sidecar
    with a valid pid and ``updatedAt: null``, so ``live_sessions`` scores it 0 and
    drops it. Measured 2026-08-16 in an agent's own config dir: ``_session_records``
    returned 3 while ``live_sessions`` returned ``[]``, permanently. Any caller using
    ``live_sessions`` as an EXISTENCE test is therefore blind to every agent on the
    machine (found in techne#581's review).

    THE TWO ANSWER DIFFERENT QUESTIONS, so both are kept:
      ``live_sessions``      — "who can I route to?" Freshness matters, because
                               delivering to a session that stopped reading is a
                               silent drop.
      ``present_session_ids`` — "does this id name a session that exists right now?"
                               A pid is observable when a heartbeat is absent.

    Presence = the sidecar exists AND its pid is alive. Both predicates come from
    the same primitives ``live_sessions`` uses (:func:`_session_records`,
    :func:`_pid_alive`) rather than a second copy of the pid logic — a
    reimplementation is a second definition and it will differ.

    THIS IS TTL-FREE **AND** PID-ONLY, which is the combination :func:`_durable_name`
    explicitly warns against above ("do NOT simplify to a pid-only check") — it pays
    for ``_same_process_pids`` (a ``procStart`` match) precisely because a recycled
    pid on a lingering sidecar would otherwise read as alive. That warning applies
    here too, and this function accepts the weaker claim rather than escaping it.

    SO A WOUND-DOWN SESSION CAN STILL BE REPORTED PRESENT. The enumeration below is
    phrased by WHAT THE PROBE DOES rather than by what the pid looks like, because
    every shape-based version of it has been wrong: three revisions of this docstring
    claimed "the one way", then four ways, and each time a case fell outside the
    categories — the last one introduced by the very commit that fixed the previous
    miss (techne#581 rounds 2 and 3).
      1. THE PROBE CANNOT RUN — ``_pid_alive`` returns True because it has nothing to
         go on. Covers a MISSING pid, a NON-INTEGER pid, and a pid too large for a C
         int (>= 2**31, where ``os.kill`` raises OverflowError before probing
         anything). Unverifiable is not dead, so all of these fail open;
      2. THE PROBE IS SKIPPED — ``pid: 0`` or a negative pid take the ``pid <= 0``
         short-circuit. These ARE integers, which is what a "non-integer" framing kept
         missing;
      3. THE PROBE RUNS AND IS WRONG — a RECYCLED pid, live and owned by an unrelated
         process. Nothing about the value gives it away. ``_same_process_pids`` is the
         existing remedy; see below for why it is not called here;
      4. NOTHING BOUNDS ANY OF THE ABOVE, because there is no TTL here the way
         ``live_sessions`` has one.
    1-3 ARE EXHAUSTIVE BY CONSTRUCTION, which is the whole reason for phrasing them by
    outcome: ``_pid_alive`` returns True from exactly three places — the
    ``not isinstance(pid, int) or pid <= 0`` short-circuit (2, and the missing-pid half
    of 1), the ``except (OSError, OverflowError)`` handler (the rest of 1, plus
    PermissionError), and the fall-through after ``os.kill`` succeeds (3) — and returns
    False from one, ``ProcessLookupError``. So the list is CHECKABLE AGAINST CONTROL
    FLOW instead of being another guess at what a bad pid looks like. The four previous
    versions were guesses, and each missed a case.
    Measured on this machine 2026-08-16: zero false-presents in practice. That is the
    current state, not a guarantee.

    CASE 3 IS NOW CLOSED (techne#587). A recycled pid is demoted via
    :func:`_recycled_pids`, so the docstring above describes what ``_pid_alive``
    alone can tell you, not what this function returns. Cases 1, 2 and 4 stand.

    IT IS DEMOTED ONLY ON POSITIVE EVIDENCE — ``ps`` reported a start time, the
    sidecar claimed one, and they DISAGREE. The complement of
    ``_same_process_pids`` is deliberately NOT used, because that set lumps a real
    mismatch together with every unknown (no ``procStart``, ``ps`` silent, an
    unparseable ``lstart``), and demoting on the union manufactures FALSE ABSENTS.
    The direction still matters exactly as before: a false present costs only the
    older, weaker behaviour, while a false absent refuses a session that is
    genuinely working.

    THE COST OBJECTION WAS REAL AND IS NOW PAID FOR. This previously declined to
    harden because ``ps`` is expensive on a path that runs for every coord MCP
    invocation resolving identity — measured on this host at 0.005 ms here against
    31.9 ms for one ``_same_process_pids``, a ~6,400x step. ``_ps_starts`` caches
    the snapshot for :data:`_PS_CACHE_TTL_S`, which collapses a burst to a single
    ``ps`` while staying far shorter than any plausible pid-recycling interval.
    """
    recs = _session_records() if records is None else records
    recycled = _recycled_pids(recs)
    return {
        sid
        for rec in recs
        if (sid := rec.get("sessionId"))
        and _pid_alive(rec.get("pid"))
        and rec.get("pid") not in recycled
    }


def _git_worktree(cwd: str | None) -> dict | None:
    """Resolve a cwd to its git working tree: ``{toplevel, branch, is_primary}``.

    One ``git rev-parse`` call. ``is_primary`` is False for a linked worktree (its
    git-dir lives under ``.git/worktrees/``) — that's how we tell "the shared main
    checkout" from an isolated per-session worktree. Returns None if cwd isn't a git
    working tree (the sidecar cwd is the launch dir, so this can legitimately miss).
    """
    import subprocess

    if not cwd:
        return None
    try:
        r = subprocess.run(
            [
                "git",
                "-C",
                cwd,
                "rev-parse",
                "--show-toplevel",
                "--git-dir",
                "--git-common-dir",
                "--abbrev-ref",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    parts = [p.strip() for p in r.stdout.splitlines()]
    if len(parts) < 4:
        return None
    toplevel, git_dir, common_dir, branch = parts[0], parts[1], parts[2], parts[3]
    # A linked worktree's git-dir (…/.git/worktrees/<name>) differs from its
    # common-dir (…/.git); the primary checkout's two are equal. Robust vs a repo
    # whose path merely contains '/worktrees/'.
    is_primary = os.path.normpath(git_dir) == os.path.normpath(common_dir)
    return {"toplevel": toplevel, "branch": branch, "is_primary": is_primary}


def working_tree_collisions(session_id: str) -> list[dict]:
    """Live peers that are a genuine collision risk under the worktree SOP (techne#376
    phase 2) — NOT merely "same repo" (isolated worktrees of one repo are the whole
    point). Two cases are flagged:

    1. A peer sharing THIS session's exact git working tree (same ``toplevel``) — same
       HEAD, the commit-bleed the SOP exists to prevent.
    2. A peer doing feature work in the PRIMARY (non-worktree) checkout — i.e. on the
       shared checkout, off the default branch — an SOP violation.

    Best-effort: a peer (or self) whose sidecar cwd isn't a git working tree is
    skipped, since that cwd is the launch dir and may not reflect a later ``cd``.
    Each result: ``{session_id, name, branch, toplevel, reason}``.
    """
    sessions = {s["session_id"]: s for s in live_sessions()}
    me = sessions.get(session_id)
    if not me:
        return []

    # Memoize by cwd: many sessions can share one working tree (that's the very
    # collision we're after), so we probe each distinct cwd with git at most once.
    _wt_cache: dict = {}

    def _wt(cwd):
        if cwd not in _wt_cache:
            _wt_cache[cwd] = _git_worktree(cwd)
        return _wt_cache[cwd]

    mine = _wt(me.get("cwd"))
    if not mine:
        return []
    out: list[dict] = []
    for sid, s in sessions.items():
        if sid == session_id:
            continue
        wt = _wt(s.get("cwd"))
        if not wt:
            continue
        if wt["toplevel"] == mine["toplevel"]:
            reason = "same git working tree (shared HEAD — commit-bleed risk)"
        elif wt["is_primary"] and wt["branch"] not in (None, "", "main", "master", "HEAD"):
            reason = "feature work in the shared (non-worktree) checkout"
        else:
            continue
        out.append(
            {
                "session_id": sid,
                "name": s.get("name"),
                "branch": wt["branch"],
                "toplevel": wt["toplevel"],
                "reason": reason,
            }
        )
    return out
