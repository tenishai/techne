"""Acceptance tests for the deposit-and-drain federation brick (techne#221).

Proves the 'accept-fast deposit into a store you don't own / one distiller,
many sources / idempotent' backbone using existing primitives:

  Deposit  = record_observation(foreign_config, ...) where foreign_config.db_path
             points at a separate directory the caller does not own.
  Drain    = emit_reflection_batch + ingest_reflection_response against that
             same foreign_config.

These tests drive the primitives DIRECTLY — drain.py (agents repo, read-only
reference) is NOT invoked.  drain.py orchestrates exactly these same techne
calls with TECHNE_HOME set to the foreign dir; its two idempotency signals are:

  Signal A — knowledge_candidates.source_session_id: any session that already
             has a candidate row is excluded from the drain worklist by the
             NOT IN subquery.  Tested here.
  Signal B — .drained.json fingerprint file: covers empty-yield sessions that
             a candidate row cannot record.  This is drain.py filesystem state,
             not testable from the techne repo (noted inline).

Both signals are STORE-SCOPED: they live in / reference the foreign store that
drain was pointed at, so a distiller reading many stores never cross-contaminates
its own store's ledger.
"""
from __future__ import annotations

import json

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.reflect import (
    emit_reflection_batch,
    ingest_reflection_response,
    list_candidates,
    load_session_for_reflection,
)
from techne.core.state import end_session, record_observation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_foreign_config(tmp_path, *, subdir: str = "deposited") -> Config:
    """A Config whose db_path lives in a sub-directory the 'caller' doesn't own.

    This is the foreign store an ephemeral producer deposits into.  A distiller
    (drain.py) reads it by pointing TECHNE_HOME at that sub-directory — which
    is exactly what this config represents.
    """
    foreign_dir = tmp_path / subdir
    foreign_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.db_path = foreign_dir / "techne.db"
    cfg.reflect.enabled = True
    cfg.reflect.min_observations_per_thread = 3
    cfg.reflect.min_confidence = "low"
    cfg.reflect.max_candidates_per_thread = 5
    cfg.reflect.write_to = "candidates"
    cfg.reflect.include_existing_knowledge = False  # skip FTS in tests
    init_db(cfg.db_path).close()
    return cfg


def _mk_local_config(tmp_path, *, subdir: str = "local") -> Config:
    """The caller's own (local/default) store.  Deposit must not touch this."""
    local_dir = tmp_path / subdir
    local_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.db_path = local_dir / "techne.db"
    cfg.reflect.enabled = True
    cfg.reflect.min_observations_per_thread = 3
    cfg.reflect.min_confidence = "low"
    cfg.reflect.max_candidates_per_thread = 5
    cfg.reflect.write_to = "candidates"
    cfg.reflect.include_existing_knowledge = False
    init_db(cfg.db_path).close()
    return cfg


def _seed_session(
    config: Config,
    session_id: str,
    n: int,
    content_template: str = "Decided to use PostgreSQL for the jobs queue, observation {i}",
) -> None:
    """Deposit n observations into config's store under session_id."""
    for i in range(n):
        record_observation(
            config,
            session_id,
            content_template.format(i=i),
            tool_name="Edit",
        )


def _count_observations(db_path, session_id: str) -> int:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def _count_candidates(db_path, session_id: str) -> int:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM knowledge_candidates WHERE source_session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0]
    finally:
        conn.close()


_SYNTHETIC_RESPONSE = json.dumps(
    [
        {
            "scope": "knowledge",
            "type": "fact",
            "content": "PostgreSQL is used for the jobs queue",
            "tags": "storage,jobs",
            "confidence": "high",
        }
    ]
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_deposit_scoped_to_foreign_store(tmp_path):
    """Deposit writes ONLY to the foreign store; the local store is not touched.

    This is the core independence invariant: an ephemeral producer calling
    `record_observation(foreign_config, ...)` does not bleed into the caller's
    own store.  The deposit path is purely a Config.db_path routing decision —
    no shared mutable state.
    """
    foreign_config = _mk_foreign_config(tmp_path)
    local_config = _mk_local_config(tmp_path)

    session_id = "deposit-scope-test"
    _seed_session(foreign_config, session_id, n=5)

    # Foreign store has the observations.
    assert _count_observations(foreign_config.db_path, session_id) == 5

    # Local store is completely untouched — different db_path means no bleed.
    assert _count_observations(local_config.db_path, session_id) == 0

    # Belt-and-suspenders: foreign and local db files are distinct on disk.
    assert foreign_config.db_path != local_config.db_path
    assert foreign_config.db_path.exists()


def test_deposit_is_synchronous_no_llm(tmp_path, monkeypatch):
    """Deposit is a fast synchronous write; it never calls the LLM.

    Proves accept-fast / distill-async: record_observation writes raw observations
    and returns immediately.  Distillation is the drain's job, running out-of-band
    on the operator's model.  If the LLM entrypoint is called, this test fails.
    """
    from techne.core import thread_orchestrator

    def _bomb(config):
        raise AssertionError(
            "deposit must NOT call the LLM — distillation is the distiller's job, "
            "not the deposit path's"
        )

    monkeypatch.setattr(thread_orchestrator, "_make_generate_fn", _bomb)

    foreign_config = _mk_foreign_config(tmp_path)
    session_id = "no-llm-test"

    N = 8
    for i in range(N):
        # Must not raise — the monkeypatched bomb is proof the LLM was never called.
        record_observation(
            foreign_config,
            session_id,
            f"Ran migration step {i}",
            tool_name="Bash",
        )

    assert _count_observations(foreign_config.db_path, session_id) == N

    # No distillation on deposit: knowledge_candidates must be empty.
    conn = connect(foreign_config.db_path)
    try:
        n_candidates = conn.execute(
            "SELECT COUNT(*) FROM knowledge_candidates"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n_candidates == 0, (
        f"deposit must not write knowledge_candidates (found {n_candidates})"
    )


def test_reflect_emit_reads_foreign_observations(tmp_path):
    """A distiller can emit reflection prompts from a foreign store it didn't produce.

    This is the 'one distiller, many sources' half: drain.py would point its
    TECHNE_HOME at the foreign dir, then call `techne reflect emit --session <id>`.
    Here we call the primitive directly with foreign_config.

    The session is explicitly ended before emission.  Real ephemeral producers
    (killed or fire-and-forget processes) rely on drain.py's staleness guard
    (observations older than DRAIN_STALE_MIN minutes with ended_at IS NULL) to
    become drainable; ending explicitly gives a deterministic drainable session
    for the test.
    """
    foreign_config = _mk_foreign_config(tmp_path)
    session_id = "emit-foreign-test"
    _seed_session(foreign_config, session_id, n=12)

    # Explicitly end the session — drain's staleness guard is drain.py state,
    # not tested here; this gives a deterministic drainable session.
    end_session(foreign_config.db_path, session_id)

    threads, observations = load_session_for_reflection(foreign_config, session_id)
    assert observations, "load_session_for_reflection must return the deposited observations"

    batch = emit_reflection_batch(foreign_config, session_id, threads, observations)

    assert batch, (
        "emit_reflection_batch must return at least one prompt for 12 observations "
        "with min_observations_per_thread=3"
    )
    item = batch[0]
    assert {"thread_id", "session_id", "prompt_hash", "prompt"} <= set(item)
    assert item["session_id"] == session_id


def test_reflect_ingest_writes_candidates_to_foreign_store(tmp_path):
    """The full distill round-trip targets the store drain was pointed at.

    After ingest, knowledge_candidates rows exist in the FOREIGN db with the
    correct source_session_id.  The local (caller's) db must have 0 candidates —
    a distiller reading foreign stores must not pollute its own store.
    """
    foreign_config = _mk_foreign_config(tmp_path)
    local_config = _mk_local_config(tmp_path)

    session_id = "ingest-foreign-test"
    _seed_session(foreign_config, session_id, n=10)
    end_session(foreign_config.db_path, session_id)

    threads, observations = load_session_for_reflection(foreign_config, session_id)
    batch = emit_reflection_batch(foreign_config, session_id, threads, observations)
    assert batch, "test setup: emit must produce at least one prompt"

    # Ingest into the foreign store via foreign_config.
    for item in batch:
        counts = ingest_reflection_response(
            foreign_config,
            item["session_id"],
            item["thread_id"],
            item["prompt_hash"],
            _SYNTHETIC_RESPONSE,
        )
        assert counts["candidates_written"] > 0

    # Foreign store has candidates with the right source_session_id.
    foreign_conn = connect(foreign_config.db_path)
    try:
        rows = foreign_conn.execute(
            "SELECT source_session_id, scope, content FROM knowledge_candidates"
        ).fetchall()
    finally:
        foreign_conn.close()

    assert rows, "knowledge_candidates must be non-empty in the foreign store after ingest"
    assert all(r["source_session_id"] == session_id for r in rows), (
        "every candidate must reference the deposited session"
    )

    # The distiller's own (local) store must have ZERO candidates — ingest targeted
    # foreign_config.db_path, not local_config.db_path.
    local_conn = connect(local_config.db_path)
    try:
        n_local = local_conn.execute(
            "SELECT COUNT(*) FROM knowledge_candidates"
        ).fetchone()[0]
    finally:
        local_conn.close()
    assert n_local == 0, (
        f"ingest must not write to the local store (found {n_local} candidates in local db)"
    )


def test_drain_idempotent_signal_a(tmp_path):
    """Signal A: a distilled session is excluded from drain.py's re-drain worklist.

    drain.py's worklist SQL:
        WHERE s.id NOT IN (SELECT source_session_id FROM knowledge_candidates)

    After one successful emit+ingest round, the session MUST appear in
    knowledge_candidates.  If it does, drain.py's NOT IN clause excludes it on
    subsequent fire-ups.  This test makes the assertion BITE: if ingest had
    written zero candidates, the NOT IN clause would NOT exclude the session,
    and drain would re-distill it on the next run — potentially accumulating
    duplicates over time.

    Signal B (.drained.json) covers the empty-yield case where no candidates
    were written; that is drain.py filesystem state and not tested from here.
    """
    foreign_config = _mk_foreign_config(tmp_path)

    session_id = "signal-a-test"
    _seed_session(
        foreign_config,
        session_id,
        n=12,
        content_template="Decided to use Redis for the session cache, observation {i}",
    )
    end_session(foreign_config.db_path, session_id)

    # --- First drain cycle (emit + ingest) ---
    threads, observations = load_session_for_reflection(foreign_config, session_id)
    batch = emit_reflection_batch(foreign_config, session_id, threads, observations)
    assert batch, "test setup: emit must produce prompts for Signal A to guard"

    for item in batch:
        ingest_reflection_response(
            foreign_config,
            item["session_id"],
            item["thread_id"],
            item["prompt_hash"],
            _SYNTHETIC_RESPONSE,
        )

    candidate_count = _count_candidates(foreign_config.db_path, session_id)
    assert candidate_count > 0, (
        "ingest must write ≥1 candidate — this is what Signal A relies on to protect the session"
    )

    # --- Second emit (drain.py would call this on the next fire-up) ---
    # emit_reflection_batch does not check for existing candidates; drain.py's
    # Signal A guard lives at the worklist level, before emit is called.
    batch2 = emit_reflection_batch(foreign_config, session_id, threads, observations)

    # For each prompt drain.py WOULD have sent (if it bypassed Signal A),
    # a knowledge_candidates row with that exact prompt_hash must already exist.
    # This is the evidence that Signal A's NOT IN subquery correctly identifies
    # the session as already-distilled.
    foreign_conn = connect(foreign_config.db_path)
    try:
        for item in batch2:
            row = foreign_conn.execute(
                """SELECT id FROM knowledge_candidates
                   WHERE source_session_id = ? AND prompt_hash = ?""",
                (session_id, item["prompt_hash"]),
            ).fetchone()
            assert row is not None, (
                f"Signal A guard broken: no candidate with prompt_hash={item['prompt_hash']!r} "
                f"for session {session_id!r}.  drain.py would re-distill this session, "
                f"accumulating duplicates."
            )

        # Signal A guard: drain.py's NOT IN worklist query must exclude this session.
        # A non-None result here means the session is STILL in the worklist (broken).
        worklist_hit = foreign_conn.execute(
            """SELECT s.id
                 FROM sessions s
                 JOIN observations o ON o.session_id = s.id
                WHERE s.id NOT IN (SELECT source_session_id FROM knowledge_candidates)
                  AND s.id = ?
                GROUP BY s.id""",
            (session_id,),
        ).fetchone()
        assert worklist_hit is None, (
            f"Signal A broken: session {session_id!r} still appears in drain.py's worklist "
            f"after a successful ingest — drain.py would re-distill it on the next fire-up"
        )
    finally:
        foreign_conn.close()

    # Candidate count is unchanged: drain.py would not call ingest again because
    # Signal A excluded the session from its worklist before it could emit+ingest.
    # (If we had called ingest a second time directly, we WOULD get duplicates —
    # Signal A is the guard that prevents that from happening in practice.)
    assert _count_candidates(foreign_config.db_path, session_id) == candidate_count


def test_deposit_empty_yield_no_candidates(tmp_path):
    """A deposited session whose reflection yields nothing writes no candidates and raises no error.

    This exercises the Signal-B path: drain.py would add the session to
    .drained.json on an empty emit batch or empty ingest response, ensuring it
    is never re-run.  The .drained.json write is drain.py filesystem state —
    not covered here (noted in drain.py's module docstring).

    The techne-side invariant: ingest with an empty/invalid response is a no-op
    that leaves knowledge_candidates clean.
    """
    foreign_config = _mk_foreign_config(tmp_path)
    session_id = "empty-yield-test"

    # Fewer observations than min_observations_per_thread → emit returns empty batch.
    # Even with exactly 2 observations, the heuristic thread detector creates a
    # thread, but emit_reflection_batch filters it (len < 3).
    _seed_session(foreign_config, session_id, n=2)
    end_session(foreign_config.db_path, session_id)

    threads, observations = load_session_for_reflection(foreign_config, session_id)
    batch = emit_reflection_batch(foreign_config, session_id, threads, observations)

    # Empty batch → nothing to distill.  drain.py would write session to .drained.json
    # (Signal B, drain.py filesystem state, not tested here).
    if not batch:
        # The expected path: not enough observations for a qualifying thread.
        assert _count_candidates(foreign_config.db_path, session_id) == 0
        return

    # If the batch is unexpectedly non-empty (thread detection + thresholds differ
    # from expectations), ingest with an empty response must still be a clean no-op.
    for item in batch:
        counts = ingest_reflection_response(
            foreign_config,
            item["session_id"],
            item["thread_id"],
            item["prompt_hash"],
            "[]",  # empty JSON array → no candidates
        )
        assert counts["candidates_written"] == 0, (
            "ingest of an empty response must write zero candidates"
        )

    assert _count_candidates(foreign_config.db_path, session_id) == 0, (
        "no candidates must exist after an empty-yield distillation"
    )
