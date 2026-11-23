"""Re-apply credential redaction to rows already stored (techne#1245).

Redaction runs at write time, so widening the pattern set protects new rows
only. This walks the free-text columns that carry captured or model-written text
and rewrites any value the current patterns would change. Write-time redaction
did not cover all of them: `context_injections.trigger_snippet` and the
thread-summary payloads `run_session_end` enqueues into `outbox.data_json` were
stored raw (issue #1238) until they were fixed, so rows written before that carry
no write-time redaction and this pass is the only redaction they get.
`tests/test_redaction_coverage.py` now fails when a new write site skips redaction.
Dry run by default: it counts and never prints matched text.

What it does not reach: vector embeddings (derived, not text), FTS shadow
tables (rebuilt by the UPDATE triggers on observations, knowledge and threads),
the librarian's documents (a separate database, librarian.db), backups and
exports already written, and anything already pushed to a remote sink.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from techne.core.redaction import redact_credentials

#: (table, column, is_json). JSON columns are redacted value by value so the
#: blob stays parseable; a whole-text pass could swallow a quoted key/value pair.
TARGETS: list[tuple[str, str, bool]] = [
    ("observations", "content", False),
    ("knowledge", "content", False),
    ("sessions", "summary", False),
    ("sessions", "summary_llm", False),
    ("sessions", "summary_heuristic", False),
    ("threads", "label", False),
    ("threads", "summary", False),
    ("threads", "resume_points", False),
    ("coord_messages", "body", False),
    ("context_injections", "trigger_snippet", False),
    ("reminders", "note", False),
    ("curated_memory", "content", False),
    ("knowledge_candidates", "content", False),
    ("knowledge_candidates", "raw_response", False),
    ("knowledge_candidates", "decision_reason", False),
    ("knowledge_candidates", "revised_content", False),
    ("knowledge_candidates", "faithfulness_warning", False),
    ("lesson_meta", "rule", False),
    ("checkpoints", "data_json", True),
    ("outbox", "data_json", True),
]


def _redact_json(value):
    if isinstance(value, str):
        return redact_credentials(value)
    if isinstance(value, list):
        return [_redact_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_json(v) for k, v in value.items()}
    return value


def _redacted(text: str, is_json: bool) -> str:
    if not is_json:
        return redact_credentials(text)
    try:
        obj = json.loads(text)
    except ValueError:
        return redact_credentials(text)
    red = _redact_json(obj)
    # Re-serialising changes formatting, so only rewrite when a value changed.
    return text if red == obj else json.dumps(red)


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def scrub(db_path: Path, *, apply: bool = False) -> dict[str, int]:
    """Count (and with ``apply`` rewrite) rows the current patterns would change.

    Returns ``{"table.column": n_rows}`` for every target that exists, zeros
    included, so a clean store reads as clean rather than as unscanned.
    """
    from techne.core.database import connect

    conn = connect(db_path)
    counts: dict[str, int] = {}
    try:
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        for table, col, is_json in TARGETS:
            cols = _columns(conn, table)
            if col not in cols:
                continue
            changed: list[tuple[int, str, str]] = []
            for rowid, text in conn.execute(
                f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''"
            ):
                if not isinstance(text, str):
                    continue
                new = _redacted(text, is_json)
                if new != text:
                    changed.append((rowid, text, new))
            counts[f"{table}.{col}"] = len(changed)
            if not apply:
                continue
            for rowid, old, new in changed:
                if table == "observations" and "content_hash" in cols:
                    # Keep add_observation's invariant: content_hash IS NOT NULL iff
                    # redaction changed the content, and it hashes the raw payload.
                    # A row redacted earlier already holds the raw hash; keep it.
                    conn.execute(
                        "UPDATE observations SET content = ?,"
                        " content_hash = COALESCE(content_hash, ?) WHERE rowid = ?",
                        (new, hashlib.sha256(old.encode()).hexdigest(), rowid),
                    )
                else:
                    # outbox.content_hash is left alone on purpose: it is a dedup key
                    # under a partial unique index, and a recomputed key can collide
                    # with another pending row.
                    conn.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", (new, rowid))
        if apply:
            conn.commit()
        return counts
    except BaseException:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()
