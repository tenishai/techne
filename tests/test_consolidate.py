"""Consolidate / sleep pass over stored knowledge (techne#108 PR2).

Covers the apply primitive (merge_into / the promote dispatch), cluster selection,
the anti-re-proposal guard, and the emit→ingest→promote round-trip. The agent's role
(judging a cluster) is faked by a canned JSON verdict — no real LLM, mirroring the
reflect/consolidate emit-ingest contract.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.knowledge import (
    _add_dep_in_conn,
    get,
    merge_into,
    store,
    supersede,
)
from techne.core.reflect import promote_candidate
from techne.core.sleepdream import (
    consolidate_clusters,
    consolidate_emit,
    consolidate_ingest,
    parse_consolidation_response,
)


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "sleep.db"
    init_db(cfg.db_path)
    return cfg


def _status(db, kid):
    conn = connect(db)
    try:
        return conn.execute("SELECT status FROM knowledge WHERE id=?", (kid,)).fetchone()[0]
    finally:
        conn.close()


# ---- merge_into primitive -------------------------------------------------


def test_merge_into_collapses_n_to_one(config):
    db = config.db_path
    a = store(db, "Richard uses host-a", type="fact", confidence="high")
    b = store(db, "Primary machine is host-a", type="fact", confidence="medium")
    new = merge_into(db, [a, b], "Richard's primary machine is host-a",
                     type="fact", confidence="high")
    assert get(db, a)["superseded_by"] == new
    assert get(db, b)["superseded_by"] == new
    assert get(db, new)["superseded_by"] is None
    assert get(db, new)["confidence"] == "high"


def test_merge_into_raises_on_superseded_old(config):
    db = config.db_path
    a = store(db, "v1", type="fact")
    supersede(db, a, "v2")  # a is now superseded
    with pytest.raises(ValueError, match="already superseded"):
        merge_into(db, [a], "x", type="fact", confidence="low")


def test_merge_into_raises_on_missing_and_duplicate(config):
    db = config.db_path
    a = store(db, "real", type="fact")
    with pytest.raises(ValueError, match="not found"):
        merge_into(db, [a, 9999], "x", type="fact", confidence="low")
    with pytest.raises(ValueError, match="duplicate"):
        merge_into(db, [a, a], "x", type="fact", confidence="low")


def test_merge_into_rejects_bad_type(config):
    db = config.db_path
    a, b = store(db, "x", type="fact"), store(db, "y", type="fact")
    with pytest.raises(ValueError, match="invalid type"):
        merge_into(db, [a, b], "merged", type="not-a-type", confidence="low")


def test_merge_into_cascades_dependents_to_uncertain(config):
    """A merged-away row's live dependents go uncertain (techne#117 cascade)."""
    db = config.db_path
    a = store(db, "trigger fact", type="fact")
    b = store(db, "trigger fact dup", type="fact")
    derived = store(db, "depends on the trigger", type="fact")
    # Write dep edge directly — add_dependency() was removed as dead code in #251;
    # use _add_dep_in_conn inside a short-lived connection for test setup.
    conn = connect(db)
    try:
        _add_dep_in_conn(conn, dependent_id=derived, trigger_id=a)
        conn.commit()
    finally:
        conn.close()
    merge_into(db, [a, b], "the trigger fact", type="fact", confidence="high")
    assert _status(db, derived) == "uncertain"


# ---- promote dispatch (merge action end-to-end) ---------------------------


def _insert_merge_candidate(db, target_ids, content, *, type="fact", confidence="high"):
    conn = connect(db)
    try:
        conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES ('system:sleep')")
        cur = conn.execute(
            """INSERT INTO knowledge_candidates
                 (scope, type, content, confidence, source_session_id, model_used,
                  prompt_hash, action, target_ids, origin)
               VALUES ('knowledge', ?, ?, ?, 'system:sleep', 'm', 'ph', 'merge', ?,
                       'consolidate')""",
            (type, content, confidence, json.dumps(sorted(target_ids))),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_promote_merge_candidate_applies_merge(config):
    db = config.db_path
    a = store(db, "fact one", type="fact", confidence="medium")
    b = store(db, "fact one again", type="fact", confidence="low")
    cid = _insert_merge_candidate(db, [a, b], "fact one, consolidated")
    new_id = promote_candidate(db, cid, decided_by="agent-triage", decision_reason="dup")
    assert new_id is not None
    assert get(db, a)["superseded_by"] == new_id
    assert get(db, b)["superseded_by"] == new_id
    assert get(db, new_id)["content"] == "fact one, consolidated"


def test_promote_merge_with_too_few_targets_raises_and_rolls_back(config):
    db = config.db_path
    a = store(db, "solo", type="fact")
    cid = _insert_merge_candidate(db, [a], "x")  # only 1 target — invalid merge
    with pytest.raises(ValueError, match="at least two"):
        promote_candidate(db, cid)
    # Rolled back: candidate still pending, row not superseded.
    conn = connect(db)
    try:
        assert conn.execute(
            "SELECT status FROM knowledge_candidates WHERE id=?", (cid,)
        ).fetchone()[0] == "pending"
    finally:
        conn.close()
    assert get(db, a)["superseded_by"] is None


# ---- cluster selection ----------------------------------------------------


def test_consolidate_clusters_finds_exact_dups(config):
    db = config.db_path
    a = store(db, "Richard prefers terse responses", type="fact")
    b = store(db, "Richard prefers terse responses", type="fact")  # exact dup
    store(db, "an unrelated fact about cabal", type="fact")
    clusters = consolidate_clusters(config)
    dup_clusters = [c for c in clusters if set(c["target_ids"]) == {a, b}]
    assert len(dup_clusters) == 1
    assert dup_clusters[0]["via"] == "hash"


def test_consolidate_clusters_skips_superseded(config):
    db = config.db_path
    store(db, "dup fact", type="fact")
    b = store(db, "dup fact", type="fact")
    supersede(db, b, "dup fact v2")  # b now superseded
    clusters = consolidate_clusters(config)
    # The superseded row is never offered for consolidation (selection is live-only).
    assert not any(b in c["target_ids"] for c in clusters)


# ---- emit / ingest round-trip + anti-re-proposal --------------------------


_MERGE_VERDICT = json.dumps(
    {"verdict": "merge", "content": "the consolidated fact", "type": "fact",
     "confidence": "high", "reason": "identical"}
)


def test_emit_ingest_promote_round_trip(config):
    db = config.db_path
    a = store(db, "duplicated knowledge", type="fact")
    b = store(db, "duplicated knowledge", type="fact")
    batch = consolidate_emit(config)
    item = next(c for c in batch if set(c["target_ids"]) == {a, b})

    counts = consolidate_ingest(config, item["target_ids"], item["cluster_hash"], _MERGE_VERDICT)
    assert counts["written"] == 1

    # The proposal is queued as a merge candidate; promoting it collapses the rows.
    conn = connect(db)
    try:
        cid = conn.execute(
            "SELECT id FROM knowledge_candidates WHERE origin='consolidate' AND status='pending'"
        ).fetchone()[0]
    finally:
        conn.close()
    new_id = promote_candidate(db, cid)
    assert get(db, a)["superseded_by"] == new_id
    assert get(db, b)["superseded_by"] == new_id


def test_keep_verdict_records_discarded_marker(config):
    """A 'keep' writes no pending proposal, but DOES record a discarded marker so the
    cluster isn't re-judged on every future run (the anti-re-proposal guard)."""
    db = config.db_path
    store(db, "dup", type="fact")
    store(db, "dup", type="fact")
    batch = consolidate_emit(config)
    counts = consolidate_ingest(
        config, batch[0]["target_ids"], batch[0]["cluster_hash"],
        json.dumps({"verdict": "keep", "reason": "actually distinct"}),
    )
    assert counts["written"] == 0 and counts["kept"] == 1
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT status, decided_by FROM knowledge_candidates WHERE origin='consolidate'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "discarded" and rows[0]["decided_by"] == "agent-triage"
    finally:
        conn.close()


def test_kept_cluster_not_re_proposed(config):
    """After a 'keep', the cluster must not surface again — no re-nagging a rejected merge."""
    db = config.db_path
    a = store(db, "same thing", type="fact")
    b = store(db, "same thing", type="fact")
    batch1 = consolidate_emit(config)
    item = next(c for c in batch1 if set(c["target_ids"]) == {a, b})
    consolidate_ingest(config, item["target_ids"], item["cluster_hash"],
                       json.dumps({"verdict": "keep"}))
    batch2 = consolidate_emit(config)
    assert not any(set(c["target_ids"]) == {a, b} for c in batch2)


def test_ingest_recomputes_cluster_hash(config):
    """ingest must store the hash derived from target_ids, not a caller-supplied one —
    a mismatched hash can't silently defeat _already_proposed (techne#108 review)."""
    from techne.core.sleepdream import _cluster_hash

    db = config.db_path
    a = store(db, "rc", type="fact")
    b = store(db, "rc", type="fact")
    consolidate_ingest(config, [a, b], "bogus-hash-from-caller", _MERGE_VERDICT)
    conn = connect(db)
    try:
        ph = conn.execute(
            "SELECT prompt_hash FROM knowledge_candidates WHERE origin='consolidate'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert ph == _cluster_hash([a, b])  # recomputed, not 'bogus-hash-from-caller'


def test_anti_re_proposal_skips_already_proposed_cluster(config):
    db = config.db_path
    a = store(db, "twice proposed", type="fact")
    b = store(db, "twice proposed", type="fact")
    batch1 = consolidate_emit(config)
    item = next(c for c in batch1 if set(c["target_ids"]) == {a, b})
    consolidate_ingest(config, item["target_ids"], item["cluster_hash"], _MERGE_VERDICT)

    # Second emit: the cluster now has a pending proposal → it must be skipped.
    batch2 = consolidate_emit(config)
    assert not any(set(c["target_ids"]) == {a, b} for c in batch2)


def test_ingest_guards_against_duplicate_proposal(config):
    """techne#170: the anti-re-proposal guard is enforced at INGEST too — a direct or
    duplicate ingest of an already-proposed cluster is skipped, not re-added."""
    db = config.db_path
    a = store(db, "dup ingest", type="fact")
    b = store(db, "dup ingest", type="fact")
    item = next(c for c in consolidate_emit(config) if set(c["target_ids"]) == {a, b})

    first = consolidate_ingest(config, item["target_ids"], item["cluster_hash"], _MERGE_VERDICT)
    assert first["written"] == 1 and first["skipped"] == 0

    # Re-ingest the SAME cluster (e.g. a retry) — must be a no-op, not a 2nd candidate.
    second = consolidate_ingest(config, item["target_ids"], item["cluster_hash"], _MERGE_VERDICT)
    assert second["skipped"] == 1
    assert second["written"] == 0 and second["proposed"] == 0

    conn = connect(db)
    try:
        n = conn.execute(
            "SELECT count(*) FROM knowledge_candidates WHERE origin='consolidate'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1, "duplicate ingest must not create a second candidate"


# ---- parser ---------------------------------------------------------------


def test_parse_merge_verdict():
    v = parse_consolidation_response(_MERGE_VERDICT)
    assert v["verdict"] == "merge" and v["content"] == "the consolidated fact"


def test_parse_keep_and_garbage_return_none():
    assert parse_consolidation_response(json.dumps({"verdict": "keep"})) is None
    assert parse_consolidation_response("not json at all") is None
    assert parse_consolidation_response(
        json.dumps({"verdict": "merge"})  # missing content
    ) is None


def test_parse_tolerates_code_fence():
    fenced = "```json\n" + _MERGE_VERDICT + "\n```"
    assert parse_consolidation_response(fenced)["verdict"] == "merge"


# ---------------------------------------------------------------------------
# #246 — silent-failure logging: consolidate_ingest swallow must now log
# ---------------------------------------------------------------------------


def test_consolidate_ingest_logs_on_insert_failure(config, caplog):
    """#246: when _insert_consolidation_candidate raises inside the try block,
    log.exception fires with context AND the errors counter still increments."""
    db = config.db_path
    a = store(db, "fact alpha", type="fact")
    b = store(db, "fact alpha dup", type="fact")

    with patch(
        "techne.core.sleepdream._insert_consolidation_candidate",
        side_effect=RuntimeError("sqlite gone"),
    ):
        with caplog.at_level(logging.ERROR, logger="techne.core.sleepdream"):
            counts = consolidate_ingest(config, [a, b], "ignored-hash", _MERGE_VERDICT)

    assert counts["errors"] == 1, "error counter must still increment (behavior preserved)"
    assert any(
        "consolidate_ingest" in r.message for r in caplog.records
    ), "log.exception must emit a record naming the function"


# ---- techne#987: Tier 1 must find a reworded duplicate ---------------------


def test_tier1_pairs_two_rows_stating_one_procedure_in_different_words(config):
    """The property #987 asks for, not "clusters is non-empty" (Tier 0 alone passes
    that). Neither row is byte-identical to the other, so only Tier 1 can pair them;
    querying with the whole row made its only hit itself."""
    db = config.db_path
    a = store(db, "To verify a test actually pins a behavior rather than passing by "
                  "accident, mutate the source on a copy and rerun the test; it must fail.",
              type="procedure")
    b = store(db, "To verify a new or changed test actually pins the behavior it claims, "
                  "apply a mutation to a copy of the source and confirm the test fails.",
              type="procedure")
    store(db, "Richard's primary machine is host-a, an Intel Mac on macOS 15.", type="fact")
    store(db, "Homebrew ships no Intel bottles, so brew upgrade may compile llvm for hours.",
          type="fact")
    store(db, "Backups are written as timestamped sets under the data dir backups folder.",
          type="fact")

    clusters = consolidate_clusters(config)
    assert {"target_ids": sorted([a, b]), "via": "fts"} in [
        {"target_ids": c["target_ids"], "via": c["via"]} for c in clusters
    ], clusters


# ---- techne#823: document_cards are pointers, not duplicates --------------


def _card(db, gist, doc_id):
    return store(db, gist, type="document_card", tags=f"doc:{doc_id},ver:v{doc_id}",
                 source=f"/docs/{doc_id}.md")


def test_document_cards_with_one_gist_are_not_clustered(config):
    """Nine cards, one gist, nine documents was the 2026-08-29 cluster. Merging them
    supersedes every pointer and mints one pointerless `fact`."""
    db = config.db_path
    cards = [_card(db, "Trivial: the session contains only an /exit command.", d)
             for d in (108, 115, 125)]
    clusters = consolidate_clusters(config)
    assert not any(set(cards) & set(c["target_ids"]) for c in clusters), clusters


def test_ingest_refuses_a_merge_over_document_cards(config):
    """The backstop: a direct ingest names its own targets, so the selection policy does
    not reach it. The merge is refused whatever the model judged, and remembered."""
    db = config.db_path
    a = _card(db, "Greeting probe: the user said hi.", 1174)
    b = _card(db, "Greeting probe: the user said say hi.", 1211)

    counts = consolidate_ingest(config, [a, b], "ignored", _MERGE_VERDICT)
    assert counts["refused"] == 1 and counts["written"] == 0, counts
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT status FROM knowledge_candidates WHERE origin = 'consolidate'"
        ).fetchall()
    finally:
        conn.close()
    assert [r["status"] for r in rows] == ["discarded"]
    assert get(db, a)["superseded_by"] is None and get(db, b)["superseded_by"] is None
