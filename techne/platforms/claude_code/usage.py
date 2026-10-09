"""Per-session API token usage from Claude Code transcripts (#689).

Every ``type: "assistant"`` record in a transcript carries ``message.usage``.
Two traps decide the shape of this module:

- Claude Code writes one record PER CONTENT BLOCK of an API message, each with
  the same usage, so a naive sum counts a message once per block (measured:
  1104 assistant records, 553 unique ``message.id``). Usage is keyed on
  ``message.id`` (``requestId`` as fallback) and a repeated key keeps the
  field-wise max, never a second add.
- Subagents write their own transcripts under ``<session_id>/subagents/`` next
  to the main one. That is real spend in the same session, so it is summed into
  the same columns (decided by Richard, 2026-10-08: no separate subagent column).

Reading a whole transcript is ~100 ms per 10 MB, so this runs at SessionEnd and
from ``techne usage-backfill``, never on the per-turn Stop path (issue 1218).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from techne._lazylog import get_logger
from techne.hooks._timeout import HookTimeout

if TYPE_CHECKING:
    from techne.config import Config

log = get_logger(__name__)

#: ``sessions`` column -> ``message.usage`` field.
USAGE_FIELDS = (
    ("tokens_in", "input_tokens"),
    ("tokens_out", "output_tokens"),
    ("tokens_cache_write", "cache_creation_input_tokens"),
    ("tokens_cache_read", "cache_read_input_tokens"),
)


def _as_int(value) -> int:
    # bool is an int subclass; a usage field is never legitimately a bool.
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def sum_usage(paths: Iterable[Path]) -> dict | None:
    """Sum ``message.usage`` over the assistant records of every transcript in
    *paths*, each API message counted once.

    Returns ``{column: total}`` for the four ``USAGE_FIELDS`` columns, or None
    when no record carried usage at all (so the caller can leave the columns NULL,
    meaning unknown, rather than writing a measured zero).
    """
    from techne.sync.jsonl_records import iter_jsonl_records

    per_message: dict[str, tuple[int, ...]] = {}
    anonymous: list[tuple[int, ...]] = []
    for path in paths:
        for rec in iter_jsonl_records(path):
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            row = tuple(_as_int(usage.get(field)) for _, field in USAGE_FIELDS)
            key = msg.get("id") or rec.get("requestId")
            if not key:
                # No id to dedupe on: count it, since dropping it would undercount.
                anonymous.append(row)
                continue
            prev = per_message.get(key)
            per_message[key] = row if prev is None else tuple(map(max, prev, row))
    rows = list(per_message.values()) + anonymous
    if not rows:
        return None
    return {col: sum(r[i] for r in rows) for i, (col, _) in enumerate(USAGE_FIELDS)}


def session_transcript_paths(session_id: str, jsonl_path: Path | None = None) -> list[Path]:
    """The main transcript of *session_id* plus its subagent transcripts.

    *jsonl_path* is the main transcript when the caller already knows it (the
    hook payload's ``transcript_path``); otherwise it is located with the same
    search ``backfill_uncaptured`` uses. Subagent files are
    ``<dir>/<session_id>/subagents/agent-*.jsonl`` (or ``.jsonl.gz``); their
    ``.meta.json`` siblings are not transcripts and the glob excludes them.
    Returns [] when the main transcript cannot be found.
    """
    if jsonl_path is None:
        from techne.platforms.claude_code.transcript_import import _session_jsonl

        jsonl_path = _session_jsonl(session_id)
    if jsonl_path is None or not jsonl_path.is_file():
        return []
    sub_dir = jsonl_path.parent / session_id / "subagents"
    subs = sorted(sub_dir.glob("agent-*.jsonl*")) if sub_dir.is_dir() else []
    return [jsonl_path, *[p for p in subs if p.suffix in (".jsonl", ".gz")]]


def record_session_usage(config: Config, session_id: str, jsonl_path: Path | None = None) -> dict:
    """Recompute *session_id*'s token usage from its transcripts and SET it on the
    ``sessions`` row. Idempotent: a repeat call writes the same values, never adds.

    Writes nothing when no transcript is found, no record carries usage, or the
    session row does not exist; the result then has ``recorded: False`` and a
    ``reason``. Never raises, except ``HookTimeout``: any other error is logged at
    debug and returned as the reason.
    """
    try:
        paths = session_transcript_paths(session_id, jsonl_path)
        if not paths:
            return {"recorded": False, "reason": "transcript not found"}
        totals = sum_usage(paths)
        if totals is None:
            return {"recorded": False, "reason": "no usage records"}

        from techne.core.database import connect

        conn = connect(config.db_path)
        try:
            cols = ", ".join(f"{col} = ?" for col, _ in USAGE_FIELDS)
            cur = conn.execute(
                f"UPDATE sessions SET {cols}, usage_updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*(totals[col] for col, _ in USAGE_FIELDS), session_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return {"recorded": False, "reason": "no session row"}
        finally:
            conn.close()
        log.debug("session usage: %s from %d file(s): %s", session_id, len(paths), totals)
        return {"recorded": True, "files": len(paths), **totals}
    except HookTimeout:
        raise  # the hook's wall-clock alarm must reach bounded_hook, not be swallowed
    except Exception as exc:  # never break a hook or a batch backfill
        log.debug("session usage: %s failed: %s", session_id, exc)
        return {"recorded": False, "reason": f"{type(exc).__name__}: {exc}"}
