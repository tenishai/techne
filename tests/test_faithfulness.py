"""Faithfulness gate tests (techne#217 / coherence-layer D4).

Covers:
- Schema columns exist after init.
- faithfulness_emit returns prompts for pending merge/supersede candidates.
- faithfulness_ingest annotates unfaithful candidates (flag + reason visible).
- faithfulness_ingest does NOT flag a faithful replacement (happy path).
- Unparseable response leaves candidate unchecked (no false-positive block).
- Already-checked candidates are skipped by emit (idempotent).
- Action='add' candidates are not included in emit (no source rows to check).
- Sovereignty: faithfulness_ingest never changes candidate status.
- CLI reflect review surfaces the warning for an unfaithful candidate.
- annotate_faithfulness direct write utility.
- Anti-re-dormancy: the pass is wired into the session-end MCP instructions.
- Redaction: a credential-shaped warning is redacted at rest.
- Batch cap: emit respects max_candidates.
"""

from __future__ import annotations

import json

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.faithfulness import (
    annotate_faithfulness,
    build_faithfulness_prompt,
    faithfulness_emit,
    faithfulness_ingest,
    parse_faithfulness_response,
)
from techne.core.knowledge import store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "faith.db"
    init_db(cfg.db_path)
    return cfg


def _insert_candidate(
    db,
    *,
    target_ids: list[int],
    content: str,
    action: str = "merge",
    status: str = "pending",
) -> int:
    """Insert a merge/supersede candidate referencing the given knowledge rows."""
    conn = connect(db)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id) VALUES ('system:sleep')"
        )
        cur = conn.execute(
            """INSERT INTO knowledge_candidates
                 (scope, type, content, confidence, source_session_id, model_used,
                  prompt_hash, action, target_ids, origin, status)
               VALUES ('knowledge', 'fact', ?, 'high', 'system:sleep', 'agent-assisted',
                       'ph', ?, ?, 'consolidate', ?)""",
            (content, action, json.dumps(sorted(target_ids)), status),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _get_candidate(db, cid: int) -> dict:
    conn = connect(db)
    try:
        return dict(
            conn.execute(
                "SELECT * FROM knowledge_candidates WHERE id=?", (cid,)
            ).fetchone()
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_has_faithfulness_columns(config):
    """faithfulness_ok and faithfulness_warning columns exist after init."""
    conn = connect(config.db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_candidates)")}
    finally:
        conn.close()
    assert "faithfulness_ok" in cols
    assert "faithfulness_warning" in cols


def test_faithfulness_ok_default_is_null(config):
    """A freshly inserted candidate has faithfulness_ok=NULL (unchecked)."""
    db = config.db_path
    a = store(db, "source fact", type="fact")
    b = store(db, "source fact dup", type="fact")
    cid = _insert_candidate(db, target_ids=[a, b], content="merged fact")
    row = _get_candidate(db, cid)
    assert row["faithfulness_ok"] is None
    assert row["faithfulness_warning"] is None


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def test_build_faithfulness_prompt_includes_sources_and_replacement():
    source_rows = [
        {"id": 1, "content": "Richard prefers Python"},
        {"id": 2, "content": "Python is used for all new scripts"},
    ]
    prompt = build_faithfulness_prompt(source_rows, "Richard uses Python for all scripting work")
    assert "#1" in prompt
    assert "Richard prefers Python" in prompt
    assert "#2" in prompt
    assert "Python is used for all new scripts" in prompt
    assert "Richard uses Python for all scripting work" in prompt


def test_build_faithfulness_prompt_truncates_long_content():
    long_content = "x" * 1000
    source_rows = [{"id": 1, "content": long_content}]
    prompt = build_faithfulness_prompt(source_rows, "short replacement")
    # Truncated at MAX_SOURCE_CHARS (300) + ellipsis
    assert "…" in prompt
    # Full content should NOT appear verbatim
    assert long_content not in prompt


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def test_parse_faithful_response():
    raw = json.dumps({"verdict": "faithful", "reason": "all claims covered", "dropped_claims": ""})
    result = parse_faithfulness_response(raw)
    assert result is not None
    assert result["verdict"] == "faithful"
    assert result["reason"] == "all claims covered"


def test_parse_unfaithful_response():
    raw = json.dumps({
        "verdict": "unfaithful",
        "reason": "dropped the Python-only constraint",
        "dropped_claims": "Python-only scripting language",
    })
    result = parse_faithfulness_response(raw)
    assert result is not None
    assert result["verdict"] == "unfaithful"
    assert "Python-only" in result["dropped_claims"]


def test_parse_garbage_returns_none():
    assert parse_faithfulness_response("not json at all") is None
    assert parse_faithfulness_response("") is None
    assert parse_faithfulness_response(json.dumps({"verdict": "maybe"})) is None


def test_parse_tolerates_code_fence():
    raw = json.dumps({"verdict": "faithful", "reason": "ok", "dropped_claims": ""})
    fenced = "```json\n" + raw + "\n```"
    result = parse_faithfulness_response(fenced)
    assert result is not None
    assert result["verdict"] == "faithful"


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------


def test_faithfulness_emit_returns_prompts_for_pending_merge(config):
    db = config.db_path
    a = store(db, "Richard uses SQLite for personal projects", type="fact")
    b = store(db, "SQLite is preferred over Postgres locally", type="fact")
    cid = _insert_candidate(db, target_ids=[a, b], content="merged content about SQLite")
    batch = faithfulness_emit(config)
    candidate_ids = [item["candidate_id"] for item in batch]
    assert cid in candidate_ids
    # Prompt contains the source content
    item = next(i for i in batch if i["candidate_id"] == cid)
    assert "SQLite" in item["prompt"]


def test_faithfulness_emit_includes_supersede_candidates(config):
    db = config.db_path
    a = store(db, "The project uses Postgres", type="fact")
    cid = _insert_candidate(db, target_ids=[a], content="The project switched to SQLite", action="supersede")
    batch = faithfulness_emit(config)
    assert any(i["candidate_id"] == cid for i in batch)


def test_faithfulness_emit_excludes_add_candidates(config):
    """action='add' has no source rows — emit must not include them."""
    db = config.db_path
    conn = connect(db)
    try:
        conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES ('system:sleep')")
        cid = conn.execute(
            """INSERT INTO knowledge_candidates
                 (scope, type, content, confidence, source_session_id, model_used,
                  prompt_hash, action, target_ids, origin)
               VALUES ('knowledge', 'fact', 'a new fact', 'medium', 'system:sleep',
                       'agent-assisted', 'ph2', 'add', NULL, 'reflect')"""
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    batch = faithfulness_emit(config)
    assert not any(i["candidate_id"] == cid for i in batch)


def test_faithfulness_emit_excludes_already_checked(config):
    """Candidates with faithfulness_ok already set are skipped (idempotent)."""
    db = config.db_path
    a = store(db, "already checked source", type="fact")
    b = store(db, "already checked source dup", type="fact")
    cid = _insert_candidate(db, target_ids=[a, b], content="already checked")
    annotate_faithfulness(db, cid, ok=True, warning=None)

    batch = faithfulness_emit(config)
    assert not any(i["candidate_id"] == cid for i in batch)


def test_faithfulness_emit_excludes_non_pending_candidates(config):
    """Promoted/discarded candidates are not re-checked."""
    db = config.db_path
    a = store(db, "promoted source", type="fact")
    b = store(db, "promoted source dup", type="fact")
    cid = _insert_candidate(db, target_ids=[a, b], content="promoted", status="promoted")
    batch = faithfulness_emit(config)
    assert not any(i["candidate_id"] == cid for i in batch)


# ---------------------------------------------------------------------------
# Ingest — unfaithful (the flag path)
# ---------------------------------------------------------------------------


def test_faithfulness_ingest_flags_unfaithful_candidate(config):
    """A merge candidate that drops a source claim is flagged at ingest."""
    db = config.db_path
    a = store(db, "Richard exclusively uses Python for scripting", type="fact")
    b = store(db, "All automation scripts are in Python", type="fact")
    # Replacement silently drops the "exclusively Python" constraint
    cid = _insert_candidate(
        db,
        target_ids=[a, b],
        content="Richard writes automation scripts",  # drops 'Python' claim
    )

    unfaithful_response = json.dumps({
        "verdict": "unfaithful",
        "reason": "drops the Python-only scripting constraint",
        "dropped_claims": "exclusively Python, all automation in Python",
    })
    result = faithfulness_ingest(config, cid, unfaithful_response)

    # Return value
    assert result["ok"] is False
    assert "Python" in result["warning"]

    # Persistent annotation
    row = _get_candidate(db, cid)
    assert row["faithfulness_ok"] == 0
    assert row["faithfulness_warning"] is not None
    assert "Python" in row["faithfulness_warning"]


def test_faithfulness_ingest_unfaithful_does_not_change_status(config):
    """Sovereignty invariant: flagging unfaithful must NOT change candidate status."""
    db = config.db_path
    a = store(db, "source A", type="fact")
    b = store(db, "source B", type="fact")
    cid = _insert_candidate(db, target_ids=[a, b], content="replacement drops something")

    unfaithful_response = json.dumps({
        "verdict": "unfaithful",
        "reason": "drops claim",
        "dropped_claims": "claim X",
    })
    faithfulness_ingest(config, cid, unfaithful_response)

    row = _get_candidate(db, cid)
    assert row["status"] == "pending", "unfaithful flag must not auto-discard the candidate"


# ---------------------------------------------------------------------------
# Ingest — faithful (happy path)
# ---------------------------------------------------------------------------


def test_faithfulness_ingest_does_not_flag_faithful_candidate(config):
    """A merge candidate that faithfully preserves all source claims is NOT flagged."""
    db = config.db_path
    a = store(db, "Richard uses Python for scripting", type="fact")
    b = store(db, "Python is the default language for new tooling", type="fact")
    # Replacement covers both claims
    cid = _insert_candidate(
        db,
        target_ids=[a, b],
        content="Richard uses Python for scripting and new tooling (the default language)",
    )

    faithful_response = json.dumps({
        "verdict": "faithful",
        "reason": "all Python-related claims preserved",
        "dropped_claims": "",
    })
    result = faithfulness_ingest(config, cid, faithful_response)

    assert result["ok"] is True
    assert result["warning"] is None

    row = _get_candidate(db, cid)
    assert row["faithfulness_ok"] == 1
    assert row["faithfulness_warning"] is None
    assert row["status"] == "pending"  # status unchanged


# ---------------------------------------------------------------------------
# Ingest — parse failure leaves unchecked (no false-positive block)
# ---------------------------------------------------------------------------


def test_faithfulness_ingest_parse_failure_leaves_unchecked(config):
    """A bad/unparseable response must NOT mark the candidate as unfaithful."""
    db = config.db_path
    a = store(db, "source", type="fact")
    b = store(db, "source dup", type="fact")
    cid = _insert_candidate(db, target_ids=[a, b], content="replacement")

    result = faithfulness_ingest(config, cid, "not json at all")

    assert result["ok"] is None  # unchecked, not unfaithful
    row = _get_candidate(db, cid)
    assert row["faithfulness_ok"] is None  # left untouched
    assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# annotate_faithfulness direct utility
# ---------------------------------------------------------------------------


def test_annotate_faithfulness_sets_ok_true(config):
    db = config.db_path
    a = store(db, "src", type="fact")
    b = store(db, "src dup", type="fact")
    cid = _insert_candidate(db, target_ids=[a, b], content="ok replacement")
    annotate_faithfulness(db, cid, ok=True, warning=None)
    row = _get_candidate(db, cid)
    assert row["faithfulness_ok"] == 1
    assert row["faithfulness_warning"] is None


def test_annotate_faithfulness_sets_ok_false_with_warning(config):
    db = config.db_path
    a = store(db, "src", type="fact")
    b = store(db, "src dup", type="fact")
    cid = _insert_candidate(db, target_ids=[a, b], content="bad replacement")
    annotate_faithfulness(db, cid, ok=False, warning="dropped claim X")
    row = _get_candidate(db, cid)
    assert row["faithfulness_ok"] == 0
    assert row["faithfulness_warning"] == "dropped claim X"


def test_annotate_faithfulness_idempotent(config):
    """Later annotate overwrites earlier; no duplicate rows."""
    db = config.db_path
    a = store(db, "src", type="fact")
    b = store(db, "src dup", type="fact")
    cid = _insert_candidate(db, target_ids=[a, b], content="replacement")
    annotate_faithfulness(db, cid, ok=False, warning="first warning")
    annotate_faithfulness(db, cid, ok=True, warning=None)  # overwrite
    row = _get_candidate(db, cid)
    assert row["faithfulness_ok"] == 1
    assert row["faithfulness_warning"] is None


# ---------------------------------------------------------------------------
# CLI reflect review surfaces the warning
# ---------------------------------------------------------------------------


def _make_cli_config(config, monkeypatch):
    """Inject config.db_path into CLI by monkeypatching Config.load."""
    from techne.config import Config as TechneConfig

    monkeypatch.setattr(TechneConfig, "load", classmethod(lambda cls: config))
    return config


def test_cli_review_shows_faithfulness_warning(config, monkeypatch):
    """reflect review must display the faithfulness warning for flagged candidates."""
    from click.testing import CliRunner
    from techne.cli import main as cli

    _make_cli_config(config, monkeypatch)
    db = config.db_path
    a = store(db, "source A claim", type="fact")
    b = store(db, "source B claim", type="fact")
    cid = _insert_candidate(
        db,
        target_ids=[a, b],
        content="replacement drops A claim",
    )
    annotate_faithfulness(db, cid, ok=False, warning="source A claim was dropped")

    runner = CliRunner()
    result = runner.invoke(cli, ["reflect", "review", "--status", "pending"])
    assert result.exit_code == 0, result.output
    assert "FAITHFULNESS WARNING" in result.output
    assert "source A claim was dropped" in result.output


def test_cli_review_does_not_warn_for_faithful_candidate(config, monkeypatch):
    """reflect review must NOT show a warning for a faithful candidate."""
    from click.testing import CliRunner
    from techne.cli import main as cli

    _make_cli_config(config, monkeypatch)
    db = config.db_path
    a = store(db, "source A", type="fact")
    b = store(db, "source B", type="fact")
    cid = _insert_candidate(
        db,
        target_ids=[a, b],
        content="faithful replacement covers A and B",
    )
    annotate_faithfulness(db, cid, ok=True, warning=None)

    runner = CliRunner()
    result = runner.invoke(cli, ["reflect", "review", "--status", "pending"])
    assert result.exit_code == 0, result.output
    assert "FAITHFULNESS WARNING" not in result.output
    assert "faithfulness: ok" in result.output


def test_cli_review_silent_for_unchecked_candidate(config, monkeypatch):
    """reflect review must NOT mention faithfulness for unchecked candidates."""
    from click.testing import CliRunner
    from techne.cli import main as cli

    _make_cli_config(config, monkeypatch)
    db = config.db_path
    a = store(db, "source A", type="fact")
    b = store(db, "source B", type="fact")
    _insert_candidate(db, target_ids=[a, b], content="unchecked replacement")

    runner = CliRunner()
    result = runner.invoke(cli, ["reflect", "review", "--status", "pending"])
    assert result.exit_code == 0, result.output
    assert "FAITHFULNESS WARNING" not in result.output
    assert "faithfulness:" not in result.output


# ---------------------------------------------------------------------------
# Anti-re-dormancy: the gate MUST be invoked by the automated session-end loop.
#
# The faithfulness pass is agent-instruction-driven (no programmatic runner
# chains consolidate → faithfulness; consolidate itself is only run via the MCP
# emit/ingest loop). So the wiring that makes the pass actually RUN is its
# presence in the MCP SERVER_INSTRUCTIONS session-end block. These tests fail if
# that wiring is removed — guarding against the pass silently going dormant.
# ---------------------------------------------------------------------------


def test_faithfulness_tools_are_registered_mcp_handlers():
    """The emit/ingest handlers must be dispatchable (registered in TOOL_HANDLERS)."""
    from techne.mcp_server import TOOL_HANDLERS

    assert "techne_faithfulness_emit" in TOOL_HANDLERS
    assert "techne_faithfulness_ingest" in TOOL_HANDLERS


def test_faithfulness_wired_into_session_end_instructions():
    """The session-end loop text must name the faithfulness pass, or the resident
    agent never runs it and every merge/supersede candidate reaches review with
    faithfulness_ok=NULL (no warning ever surfaces). This is the anti-re-dormancy
    guard — it fails if the wiring is dropped from SERVER_INSTRUCTIONS."""
    from techne.mcp_server import SERVER_INSTRUCTIONS

    assert "techne_faithfulness_emit" in SERVER_INSTRUCTIONS or (
        "techne_faithfulness_emit/ingest" in SERVER_INSTRUCTIONS
    )
    # It must be positioned as part of the SESSION END consolidation loop, after
    # consolidate (whose candidates it checks).
    idx_consolidate = SERVER_INSTRUCTIONS.find("techne_consolidate")
    idx_faith = SERVER_INSTRUCTIONS.find("techne_faithfulness")
    assert idx_consolidate != -1, "consolidate wiring missing from instructions"
    assert idx_faith != -1, "faithfulness wiring missing from instructions"
    assert idx_faith > idx_consolidate, (
        "faithfulness must be described AFTER consolidate — it checks consolidate's output"
    )


def test_faithfulness_exposed_as_mcp_tool_schemas():
    """Both tools must appear in the advertised TOOLS list (tools/list), so a client
    that reads the schema can call them."""
    from techne.mcp_server import TOOLS

    names = {t["name"] for t in TOOLS}
    assert "techne_faithfulness_emit" in names
    assert "techne_faithfulness_ingest" in names


# ---------------------------------------------------------------------------
# Redaction — the new stored free-text column must be redacted at rest.
# ---------------------------------------------------------------------------


def test_faithfulness_warning_is_redacted_at_rest(config):
    """A credential-shaped string in a warning must be redacted before storage
    (redaction.py invariant for new free-text columns)."""
    db = config.db_path
    a = store(db, "source A", type="fact")
    b = store(db, "source B", type="fact")
    cid = _insert_candidate(db, target_ids=[a, b], content="replacement")

    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890"
    annotate_faithfulness(db, cid, ok=False, warning=f"dropped the key {secret}")

    row = _get_candidate(db, cid)
    assert secret not in row["faithfulness_warning"]
    assert "<redacted:sk-ant>" in row["faithfulness_warning"]


def test_faithfulness_ingest_redacts_warning_at_rest(config):
    """The full ingest path (not just the direct utility) must redact the warning."""
    db = config.db_path
    a = store(db, "source A", type="fact")
    b = store(db, "source B", type="fact")
    cid = _insert_candidate(db, target_ids=[a, b], content="replacement drops something")

    secret = "AKIAIOSFODNN7EXAMPLE"
    response = json.dumps({
        "verdict": "unfaithful",
        "reason": f"dropped the credential {secret}",
        "dropped_claims": "",
    })
    faithfulness_ingest(config, cid, response)

    row = _get_candidate(db, cid)
    assert secret not in (row["faithfulness_warning"] or "")


# ---------------------------------------------------------------------------
# Batch cap — emit respects max_candidates (bounded like the sibling passes).
# ---------------------------------------------------------------------------


def test_faithfulness_emit_respects_max_candidates(config):
    """A large backlog must not produce an unbounded emit batch."""
    db = config.db_path
    for i in range(5):
        a = store(db, f"source A{i}", type="fact")
        b = store(db, f"source B{i}", type="fact")
        _insert_candidate(db, target_ids=[a, b], content=f"merged {i}")

    batch = faithfulness_emit(config, max_candidates=2)
    assert len(batch) <= 2


def test_faithfulness_emit_default_cap_is_bounded(config):
    """The default cap is a finite bound (not None/unbounded)."""
    from techne.core.faithfulness import DEFAULT_MAX_CANDIDATES

    assert isinstance(DEFAULT_MAX_CANDIDATES, int)
    assert DEFAULT_MAX_CANDIDATES > 0
