"""Deterministic synthetic corpus for the recall-effectiveness eval (#68).

Builds a small, frozen techne store with known gold evidence + distractors so
retrieval scoring is reproducible and portable — never the live
~/.techne/techne.db (which mutates every session and carries real content).

`seed()` returns a mapping of logical evidence keys -> "type:id" gold
references, which recall_baseline.yaml resolves via each task's `gold_keys`.
Pinning gold by returned id (not a hardcoded number) keeps the baseline robust
if insertion order ever changes.
"""

from pathlib import Path

from techne.core.database import connect, init_db
from techne.core.knowledge import store

_SESSION = "eval-sess"


def _add_observation_raw(conn, content: str, tool_name: str = "Edit") -> int:
    """Insert an observation via direct SQL (bypasses embedding inference in
    add_observation) so the corpus is deterministic and dependency-free. The
    observations_fts index is populated by the table's INSERT trigger."""
    cur = conn.execute(
        "INSERT INTO observations (session_id, content, tool_name) VALUES (?, ?, ?)",
        (_SESSION, content, tool_name),
    )
    return cur.lastrowid


def seed(db_path: Path) -> dict[str, str]:
    """Build the frozen eval corpus. Returns logical-key -> 'type:id' gold map."""
    init_db(db_path).close()
    gold: dict[str, str] = {}

    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES (?, CURRENT_TIMESTAMP)",
            (_SESSION,),
        )
        conn.commit()
    finally:
        conn.close()

    # --- GOLD knowledge -----------------------------------------------------
    gold["cabal14"] = "knowledge:%d" % store(
        db_path,
        "cabal#14 settled the archivist outbox sink as a receiver-side distill "
        "with mailbox-as-tag-slice delivery; chosen over per-target fan-out "
        "because tag-slicing reuses the existing outbox table.",
        type="decision",
        tags="cabal,outbox",
        source="eval",
    )
    gold["reflect_candidates"] = "knowledge:%d" % store(
        db_path,
        "The end-of-session reflect pass writes proposals to the "
        "knowledge_candidates table for human review; it is off by default and "
        "never blocks session teardown.",
        type="pattern",
        tags="reflect,candidates",
        source="eval",
    )

    # --- DISTRACTOR knowledge (share words with the gold queries) -----------
    store(db_path, "cabal#11 AbortGraph handles transitive parent->child "
          "cancellation across nested dispatch.", type="decision", tags="cabal", source="eval")
    store(db_path, "cabal#12 covers A2A streaming foundations: message/stream and "
          "tasks/resubscribe with cursor-based replay.", type="decision", tags="cabal", source="eval")
    store(db_path, "The MMR reranker adds diversity to search results via a greedy "
          "second pass over BM25 candidates.", type="pattern", tags="search", source="eval")
    store(db_path, "Outbox routing should close the flush/flush_sinks race via "
          "enqueue-time target namespacing.", type="decision", tags="outbox", source="eval")

    # --- GOLD + DISTRACTOR observations -------------------------------------
    conn = connect(db_path)
    try:
        gold["adapter_touch"] = "observation:%d" % _add_observation_raw(
            conn,
            "Edited: techne/sync/adapter.py — relocate SourceAdapter Protocol "
            "to adapter.py (closes #32)",
        )
        _add_observation_raw(
            conn, "Edited: techne/core/search.py — OR fallback for empty "
            "implicit-AND FTS queries"
        )
        _add_observation_raw(conn, "Ran pytest tests/test_search.py — 44 passed", tool_name="Bash")
        _add_observation_raw(conn, "Read: techne/core/redaction.py credential patterns", tool_name="Read")
        conn.commit()
    finally:
        conn.close()

    return gold
