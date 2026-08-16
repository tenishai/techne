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
    if not wanted:
        return set()
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,lstart=", "-p", ",".join(str(p) for p in wanted)],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if out.returncode != 0:
        return set()

    same: set[int] = set()
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0])
            actual = int(time.mktime(time.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y")))
            claims = wanted[pid]
        except (ValueError, OverflowError, KeyError):
            continue
        for claimed in claims:
            try:
                if actual == int(calendar.timegm(time.strptime(claimed, "%a %b %d %H:%M:%S %Y"))):
                    same.add(pid)
                    break
            except (ValueError, OverflowError):
                continue
    return same


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
            (from_session, from_name, recipient, thread_id, body, _now_iso()),
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


def _unread_rows(conn, session_id: str, channels: list[str], ident: tuple[str | None, str]):
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
    # NULL from_name (pre-#376 rows, or a sender with no -n alias) must not match
    # anyone's name — SQL's `!=` on NULL is NULL, i.e. the row is filtered out, so
    # the comparison is written to leave those rows visible.
    author_clause = "AND from_session != ?" + (
        " AND (from_name IS NULL OR from_name != ?)" if name else ""
    )
    author_params = (session_id, name) if name else (session_id,)
    return conn.execute(
        f"SELECT id, from_session, from_name, recipient, thread_id, body, created_at"
        f"  FROM coord_messages"
        f" WHERE recipient IN ({placeholders})"
        f"   {author_clause}"
        f"   AND id NOT IN (SELECT message_id FROM coord_reads WHERE reader_session = ?)"
        f" ORDER BY id",
        (*channels, *author_params, key),
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
    """
    from techne.core.database import connect

    chans = _channels_for(session_id, channels)
    ident = _identity(session_id)  # one sidecar read for the whole call
    conn = connect(db_path)
    try:
        rows = _unread_rows(conn, session_id, chans, ident)
        msgs = [dict(r) for r in rows]
        if mark_read and msgs:
            now = _now_iso()
            key = ident[1]
            conn.executemany(
                "INSERT OR IGNORE INTO coord_reads (message_id, reader_session, read_at)"
                " VALUES (?, ?, ?)",
                [(m["id"], key, now) for m in msgs],
            )
            conn.commit()
        return msgs
    finally:
        conn.close()


def unread_count(db_path: Path, session_id: str, *, channels: list[str] | None = None) -> int:
    """How many unread messages ``session_id`` has. Cheap — for the banner/hook."""
    from techne.core.database import connect

    chans = _channels_for(session_id, channels)
    ident = _identity(session_id)
    conn = connect(db_path)
    try:
        return len(_unread_rows(conn, session_id, chans, ident))
    finally:
        conn.close()


def mark_read(
    db_path: Path,
    session_id: str,
    message_ids: list[int] | None = None,
    *,
    channels: list[str] | None = None,
) -> int:
    """Mark messages read for ``session_id``. With ``message_ids`` given, marks
    exactly those; otherwise marks everything currently unread. Returns the number
    of new read receipts written (idempotent — re-marking is a no-op)."""
    from techne.core.database import connect

    ident = _identity(session_id)
    conn = connect(db_path)
    try:
        if message_ids is None:
            chans = _channels_for(session_id, channels)
            ids = [r["id"] for r in _unread_rows(conn, session_id, chans, ident)]
        else:
            ids = list(message_ids)
        if not ids:
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
        # os.kill raises before probing anything. Pre-existing, but newly REACHABLE
        # since present_session_ids dropped the TTL that used to discard such records
        # before they got here (techne#581 round 2). Fail open, per "never reap on
        # ambiguity" above — an unrepresentable pid is unverifiable, not dead.
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

    SO A WOUND-DOWN SESSION CAN STILL BE REPORTED PRESENT, FOUR WAYS — enumerated
    because an earlier revision of this docstring said "that is the one way" and was
    wrong three times over (techne#581 round 2):
      1. a MISSING or non-integer pid — ``_pid_alive`` cannot disprove it, fails open;
      2. ``pid: 0`` or a NEGATIVE pid — these ARE integers, and ``pid <= 0`` takes the
         same fail-open branch, so category 1's "non-integer" framing missed them;
      3. a RECYCLED pid — a live integer pid owned by an unrelated process. Outside
         the fail-open categories entirely: the probe succeeds and is simply wrong.
         ``_same_process_pids`` is the existing remedy; see below for why it is not
         called here;
      4. any of the above outliving its session, because there is NO TTL here to bound
         them the way ``live_sessions`` does.
    Measured on this machine 2026-08-16: zero false-presents in practice. That is the
    current state, not a guarantee.

    NOT HARDENED WITH ``_same_process_pids`` ON PURPOSE, and this is a cost call, not
    an oversight: it shells out to ``ps``, and this function runs on every coord MCP
    invocation that resolves identity. Tracked in techne#587 with the measurements.

    The weaker claim is still the RIGHT DIRECTION for a guard that REFUSES on absence:
    a false present costs only the old silent behaviour, while a false absent would
    refuse a session that is genuinely working. A caller needing the stronger claim
    wants ``live_sessions`` (TTL) or ``_same_process_pids`` (identity).
    """
    return {
        sid
        for rec in (_session_records() if records is None else records)
        if (sid := rec.get("sessionId")) and _pid_alive(rec.get("pid"))
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
