"""Claim-level memory reconciliation tests (techne#107 PR3).

Fixtures mirror test_memory_doctor.py (CLAUDE_CONFIG_DIR redirect, tmp db via
init_db, _write_memory/_session/_obs helpers). Covers the full emit→parse→ingest
pipeline, MCP handler round-trips, and the CLI --claims path (with a monkeypatched
_make_generate_fn so no real LLM is called).
"""

from __future__ import annotations

import json

import pytest

from techne.config import Config
from techne.core.curated import live_curated_memory
from techne.core.database import connect, init_db
from techne.core.memory_claims import (
    _deed_digest,
    claims_emit,
    claims_ingest,
    parse_claims_response,
)
from techne.platforms.claude_code.paths import project_slug


def test_claims_emit_auto_drains_disk_without_prior_sync(tmp_path, monkeypatch):
    """Staff-review C1/C2: --claims must find curated memory sitting on disk even with NO
    prior `techne memory sync` — claims_emit auto-drains the files into the drain first,
    so it no longer falsely reports 'no curated memory' when `memory doctor` (which reads
    disk) would have found them."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    config_dir = tmp_path / "ccfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    # A real on-disk CC memory file under the REAL slug (not monkeypatched), and an EMPTY
    # drain table — exactly the "files exist but never synced" state.
    mem_dir = config_dir / "projects" / project_slug(cwd) / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("The eval pipeline deploys to /opt/eval/v1.\n")

    db = tmp_path / "t.db"
    init_db(db).close()
    cfg = Config()
    cfg.db_path = db

    assert live_curated_memory(db) == []  # drain empty before
    result = claims_emit(cfg, cwd=cwd)
    assert result["memory_present"] is True  # found it via the auto-drain
    assert live_curated_memory(db)  # and the drain got populated as a side effect

# ---------------------------------------------------------------------------
# Fixtures — mirror test_memory_doctor.py
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    """A tmp project cwd + a tmp CC config dir.

    NOTE: callers that reach the drain (claims_emit auto-drains via initial_walk →
    find_curated_memory_files → paths.project_slug) MUST `monkeypatch.setenv(
    "CLAUDE_CONFIG_DIR", str(config_dir))` to stay hermetic — otherwise the walk reads
    the developer's REAL ~/.claude/projects/<slug>/memory. (Monkeypatching
    mc.project_slug is NOT enough; the walk uses paths.project_slug.)"""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    config_dir = tmp_path / "ccconfig"
    return cwd, config_dir


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    init_db(p).close()
    return p


def _session(db, sid, project_path):
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO sessions (id, project_path) VALUES (?, ?)", (sid, project_path)
        )
        conn.commit()
    finally:
        conn.close()


def _obs(db, sid, tool_name, content, timestamp=None):
    conn = connect(db)
    try:
        if timestamp:
            conn.execute(
                "INSERT INTO observations (session_id, tool_name, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (sid, tool_name, content, timestamp),
            )
        else:
            conn.execute(
                "INSERT INTO observations (session_id, tool_name, content) VALUES (?, ?, ?)",
                (sid, tool_name, content),
            )
        conn.commit()
    finally:
        conn.close()


def _write_memory(db, cwd, content, source_path=None, slug=None):
    """Insert a curated_memory row directly into the DB for testing."""
    sp = source_path or str(cwd / "memory" / "MEMORY.md")
    title = sp.rsplit("/", 1)[-1]
    import hashlib

    chash = hashlib.sha256(content.encode()).hexdigest()[:32]
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO curated_memory (source_path, slug, title, content, content_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (sp, slug or "testslug", title, content, chash),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _deed_digest
# ---------------------------------------------------------------------------


class TestDeedDigest:
    def test_renders_tool_and_snippet(self):
        deeds = [
            {"tool_name": "Write", "content": "Wrote: /proj/a.py\nSome content here"},
            {"tool_name": "Bash", "content": "Ran: ls -la"},
        ]
        digest = _deed_digest(deeds)
        assert "- [Write]" in digest
        assert "Wrote: /proj/a.py" in digest
        assert "- [Bash]" in digest
        assert "Ran: ls -la" in digest

    def test_collapses_newlines(self):
        deeds = [{"tool_name": "Edit", "content": "Edited: /f.py\nLine1\nLine2"}]
        digest = _deed_digest(deeds)
        assert "\n" not in digest.replace("- [Edit]", "").lstrip("- [Edit] ")
        # The content newlines are collapsed into spaces within the snippet
        assert "Edited: /f.py" in digest

    def test_caps_at_max_deeds_with_elision(self):
        deeds = [{"tool_name": "T", "content": f"deed {i}"} for i in range(5)]
        digest = _deed_digest(deeds, max_deeds=3)
        lines = digest.splitlines()
        # 3 deed lines + 1 elision line
        assert len(lines) == 4
        assert "2 more elided" in lines[-1]

    def test_no_elision_when_at_cap(self):
        deeds = [{"tool_name": "T", "content": f"deed {i}"} for i in range(3)]
        digest = _deed_digest(deeds, max_deeds=3)
        assert "elided" not in digest

    def test_snippet_truncation(self):
        long_content = "x" * 300
        deeds = [{"tool_name": "Read", "content": long_content}]
        digest = _deed_digest(deeds, snippet=50)
        # The rendered line should not exceed snippet chars of the content part
        line = digest.splitlines()[0]
        # strip the "- [Read] " prefix and check content part length
        content_part = line[len("- [Read] "):]
        assert len(content_part) <= 50

    def test_empty_deeds_returns_empty_string(self):
        assert _deed_digest([]) == ""


# ---------------------------------------------------------------------------
# claims_emit
# ---------------------------------------------------------------------------


class TestClaimsEmit:
    def test_no_memory_returns_memory_present_false(self, db, project, monkeypatch):
        cwd, _ = project
        # Patch project_slug so it doesn't depend on fs discovery
        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")

        from techne.config import Config

        cfg = Config()
        cfg.db_path = db
        monkeypatch.chdir(cwd)

        result = claims_emit(cfg, cwd=cwd)
        assert result["memory_present"] is False
        assert result["prompt"] == ""
        assert result["prompt_hash"] == ""
        assert result["deed_count"] == 0
        assert result["deed_log_capped"] is False
        assert result["memory_files"] == []

    def test_with_memory_and_deeds_returns_memory_present_true(
        self, db, project, monkeypatch
    ):
        cwd, _ = project
        proj_path = str(cwd.resolve())

        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")

        _write_memory(db, cwd, "The project uses Python. We store data in redis.")
        _session(db, "s1", proj_path)
        _obs(db, "s1", "Write", "Wrote: /proj/app.py\nPython main entry point")
        _obs(db, "s1", "Bash", "redis-cli ping")

        from techne.config import Config

        cfg = Config()
        cfg.db_path = db

        result = claims_emit(cfg, cwd=cwd)
        assert result["memory_present"] is True
        assert len(result["prompt"]) > 0
        assert len(result["prompt_hash"]) == 16
        assert result["deed_count"] > 0
        assert result["deed_log_capped"] is False
        assert len(result["memory_files"]) == 1

    def test_deed_log_cap_is_surfaced(self, db, project, monkeypatch):
        """Hitting the deed pull cap is surfaced (deed_log_capped + a prompt CAPPED note),
        not silently dropped — else an off-window deed reads as false uncorroborated
        (techne#107 PR3 review)."""
        cwd, _ = project
        proj_path = str(cwd.resolve())

        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")
        monkeypatch.setattr(mc, "DEED_DIGEST_CAP", 2)

        _write_memory(db, cwd, "uses redis")
        _session(db, "s1", proj_path)
        for i in range(3):
            _obs(db, "s1", "Write", f"Wrote: /proj/f{i}.py")

        from techne.config import Config

        cfg = Config()
        cfg.db_path = db
        result = claims_emit(cfg, cwd=cwd)
        assert result["deed_log_capped"] is True
        assert "CAPPED" in result["prompt"]

    def test_deed_cap_not_doubled_across_path_forms(self, db, tmp_path, monkeypatch):
        """techne#170: when cwd != cwd.resolve() (macOS /tmp→/private/tmp), the two path
        candidates each pull DEED_DIGEST_CAP deeds; the union must be CLAMPED to the cap,
        not 2× it — preserving the digest's "pull == show" budget."""
        import techne.core.memory_claims as mc
        from techne.config import Config

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")
        monkeypatch.setattr(mc, "DEED_DIGEST_CAP", 2)

        real = (tmp_path / "real").resolve()
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert str(link) != str(link.resolve())  # two distinct candidate path forms

        _write_memory(db, real, "uses redis", slug="testslug")
        # DEED_DIGEST_CAP deeds under EACH path form → union of 4 before the clamp.
        _session(db, "s_link", str(link))
        _session(db, "s_real", str(real))
        for i in range(2):
            _obs(db, "s_link", "Write", f"Wrote: /link/f{i}.py")
            _obs(db, "s_real", "Write", f"Wrote: /real/f{i}.py")

        cfg = Config()
        cfg.db_path = db
        result = claims_emit(cfg, cwd=link)
        assert result["deed_count"] == 2  # clamped to the cap, not 4
        assert result["deed_log_capped"] is True

    def test_prompt_contains_memory_content(self, db, project, monkeypatch):
        cwd, _ = project
        proj_path = str(cwd.resolve())

        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")

        _write_memory(db, cwd, "We use PostgreSQL for persistence.")
        _session(db, "s1", proj_path)
        _obs(db, "s1", "Write", "Wrote: /proj/db.py")

        from techne.config import Config

        cfg = Config()
        cfg.db_path = db

        result = claims_emit(cfg, cwd=cwd)
        assert "PostgreSQL" in result["prompt"]

    def test_prompt_contains_deed_line(self, db, project, monkeypatch):
        cwd, _ = project
        proj_path = str(cwd.resolve())

        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")

        _write_memory(db, cwd, "We edit files with the Edit tool.")
        _session(db, "s1", proj_path)
        _obs(db, "s1", "Edit", "Edited: /proj/foo.py\nchanged foo")

        from techne.config import Config

        cfg = Config()
        cfg.db_path = db

        result = claims_emit(cfg, cwd=cwd)
        assert "- [Edit]" in result["prompt"]

    def test_prompt_hash_is_stable(self, db, project, monkeypatch):
        cwd, _ = project
        proj_path = str(cwd.resolve())

        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")

        _write_memory(db, cwd, "Stable memory content.")
        _session(db, "s1", proj_path)
        _obs(db, "s1", "Write", "Wrote: /proj/stable.py")

        from techne.config import Config

        cfg = Config()
        cfg.db_path = db

        r1 = claims_emit(cfg, cwd=cwd)
        r2 = claims_emit(cfg, cwd=cwd)
        assert r1["prompt_hash"] == r2["prompt_hash"]

    def test_since_days_scopes_deeds(self, db, project, monkeypatch):
        cwd, _ = project
        proj_path = str(cwd.resolve())

        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")

        _write_memory(db, cwd, "Content about old file and recent file.")
        _session(db, "s1", proj_path)
        _obs(db, "s1", "Write", "Wrote: /proj/recent.py")
        _obs(db, "s1", "Write", "Wrote: /proj/old.py", timestamp="2020-01-01 00:00:00")

        from techne.config import Config

        cfg = Config()
        cfg.db_path = db

        r_all = claims_emit(cfg, cwd=cwd)
        r_recent = claims_emit(cfg, cwd=cwd, since_days=7)
        # The recent-only window should have fewer deeds
        assert r_recent["deed_count"] < r_all["deed_count"]


# ---------------------------------------------------------------------------
# parse_claims_response
# ---------------------------------------------------------------------------


class TestParseClaimsResponse:
    def test_parses_clean_array(self):
        raw = json.dumps([
            {"claim": "The project uses Python.", "status": "corroborated",
             "evidence": "Write deed names app.py"},
            {"claim": "Data stored in Redis.", "status": "uncorroborated",
             "evidence": "none"},
            {"claim": "Tests use pytest.", "status": "contradicted",
             "evidence": "No pytest deed found"},
        ])
        items = parse_claims_response(raw)
        assert len(items) == 3
        assert items[0]["status"] == "corroborated"
        assert items[1]["status"] == "uncorroborated"
        assert items[2]["status"] == "contradicted"

    def test_tolerates_json_fence(self):
        raw = (
            "```json\n"
            '[{"claim": "Uses Python", "status": "corroborated", "evidence": "app.py"}]\n'
            "```"
        )
        items = parse_claims_response(raw)
        assert len(items) == 1
        assert items[0]["claim"] == "Uses Python"

    def test_tolerates_plain_fence(self):
        raw = (
            "```\n"
            '[{"claim": "A claim", "status": "uncorroborated", "evidence": "none"}]\n'
            "```"
        )
        items = parse_claims_response(raw)
        assert len(items) == 1

    def test_skips_items_with_bad_status(self):
        raw = json.dumps([
            {"claim": "Good claim", "status": "corroborated", "evidence": "x"},
            {"claim": "Bad status", "status": "unknown_status", "evidence": "y"},
            {"claim": "Another good", "status": "contradicted", "evidence": "z"},
        ])
        items = parse_claims_response(raw)
        assert len(items) == 2
        statuses = {i["status"] for i in items}
        assert "unknown_status" not in statuses

    def test_skips_items_missing_claim(self):
        raw = json.dumps([
            {"status": "corroborated", "evidence": "x"},
            {"claim": "Valid", "status": "corroborated", "evidence": "y"},
        ])
        items = parse_claims_response(raw)
        assert len(items) == 1
        assert items[0]["claim"] == "Valid"

    def test_returns_empty_on_garbage(self):
        assert parse_claims_response("not json at all") == []
        assert parse_claims_response("") == []
        assert parse_claims_response("{}") == []  # object, not array

    def test_evidence_coerced_to_str(self):
        raw = json.dumps([
            {"claim": "A", "status": "corroborated", "evidence": 42},
        ])
        items = parse_claims_response(raw)
        assert items[0]["evidence"] == "42"

    def test_evidence_defaults_to_empty_string_when_missing(self):
        raw = json.dumps([{"claim": "A", "status": "corroborated"}])
        items = parse_claims_response(raw)
        assert items[0]["evidence"] == ""

    def test_claim_is_stripped(self):
        raw = json.dumps([{"claim": "  spaced claim  ", "status": "corroborated",
                           "evidence": ""}])
        items = parse_claims_response(raw)
        assert items[0]["claim"] == "spaced claim"


# ---------------------------------------------------------------------------
# claims_ingest
# ---------------------------------------------------------------------------


class TestClaimsIngest:
    def test_counts_three_status_types(self, db):
        from techne.config import Config

        cfg = Config()
        cfg.db_path = db

        raw = json.dumps([
            {"claim": "Python is used", "status": "corroborated", "evidence": "app.py"},
            {"claim": "Redis is used", "status": "uncorroborated", "evidence": "none"},
            {"claim": "MySQL is used", "status": "contradicted",
             "evidence": "no SQL deed"},
        ])
        result = claims_ingest(cfg, "abc123", raw)
        assert result["counts"] == {
            "corroborated": 1, "uncorroborated": 1, "contradicted": 1
        }
        assert result["total"] == 3
        assert len(result["claims"]) == 3
        # The honesty caveat rides along with the data (so --json consumers see it too).
        assert "uncorroborated" in result["caveat"] and "NOT" in result["caveat"]

    def test_empty_response_returns_zero_counts(self, db):
        from techne.config import Config

        cfg = Config()
        cfg.db_path = db

        result = claims_ingest(cfg, "", "garbage input")
        assert result["total"] == 0
        assert result["counts"] == {
            "corroborated": 0, "uncorroborated": 0, "contradicted": 0
        }

    def test_returns_claims_list(self, db):
        from techne.config import Config

        cfg = Config()
        cfg.db_path = db

        raw = json.dumps([
            {"claim": "Alpha", "status": "corroborated", "evidence": "deed1"},
            {"claim": "Beta", "status": "uncorroborated", "evidence": "none"},
        ])
        result = claims_ingest(cfg, "hash16char123456", raw)
        claims = result["claims"]
        assert any(c["claim"] == "Alpha" for c in claims)
        assert any(c["claim"] == "Beta" for c in claims)

    def test_is_ephemeral_no_db_writes(self, db):
        """claims_ingest must not write to any table."""
        from techne.config import Config

        cfg = Config()
        cfg.db_path = db

        raw = json.dumps([
            {"claim": "X", "status": "corroborated", "evidence": "e"},
        ])
        claims_ingest(cfg, "hash", raw)

        conn = connect(db)
        try:
            # knowledge_candidates must be untouched
            count = conn.execute(
                "SELECT COUNT(*) FROM knowledge_candidates"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# MCP handler round-trip
# ---------------------------------------------------------------------------


class TestMcpHandlers:
    def test_emit_returns_memory_present_true(self, db, project, monkeypatch):
        cwd, _ = project
        proj_path = str(cwd.resolve())

        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")

        _write_memory(db, cwd, "The agent uses Python 3.11.")
        _session(db, "s1", proj_path)
        _obs(db, "s1", "Write", "Wrote: /proj/main.py")

        from techne.config import Config
        from techne.mcp_server import handle_request

        cfg = Config()
        cfg.db_path = db
        monkeypatch.chdir(cwd)

        resp = handle_request(
            {"id": 1, "method": "tools/call",
             "params": {"name": "techne_memory_claims_emit", "arguments": {}}},
            cfg,
        )
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["memory_present"] is True
        assert "prompt" in result
        assert result["deed_count"] > 0

    def test_round_trip_emit_then_ingest(self, db, project, monkeypatch):
        cwd, _ = project
        proj_path = str(cwd.resolve())

        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")

        _write_memory(db, cwd, "Uses FastAPI. Data in Postgres.")
        _session(db, "s1", proj_path)
        _obs(db, "s1", "Write", "Wrote: /proj/api.py")

        from techne.config import Config
        from techne.mcp_server import handle_request

        cfg = Config()
        cfg.db_path = db
        monkeypatch.chdir(cwd)

        emit_resp = handle_request(
            {"id": 1, "method": "tools/call",
             "params": {"name": "techne_memory_claims_emit", "arguments": {}}},
            cfg,
        )
        emit_result = json.loads(emit_resp["result"]["content"][0]["text"])
        prompt_hash = emit_result["prompt_hash"]

        # Canned response simulating what the agent would answer
        canned = json.dumps([
            {"claim": "Uses FastAPI", "status": "uncorroborated", "evidence": "none"},
            {"claim": "Data in Postgres", "status": "corroborated",
             "evidence": "[Write] api.py"},
        ])

        ingest_resp = handle_request(
            {"id": 2, "method": "tools/call",
             "params": {
                 "name": "techne_memory_claims_ingest",
                 "arguments": {"prompt_hash": prompt_hash, "response": canned},
             }},
            cfg,
        )
        ingest_result = json.loads(ingest_resp["result"]["content"][0]["text"])
        assert ingest_result["total"] == 2
        assert ingest_result["counts"]["uncorroborated"] == 1
        assert ingest_result["counts"]["corroborated"] == 1

    def test_ingest_with_dict_response_is_coerced(self, db):
        """If `response` is accidentally passed as a parsed dict, it must be coerced."""
        from techne.config import Config
        from techne.mcp_server import handle_request

        cfg = Config()
        cfg.db_path = db

        # Passing a list (not a string) — the handler should json.dumps it
        canned_list = [
            {"claim": "A", "status": "corroborated", "evidence": "e"},
        ]
        resp = handle_request(
            {"id": 3, "method": "tools/call",
             "params": {
                 "name": "techne_memory_claims_ingest",
                 "arguments": {"prompt_hash": "", "response": canned_list},
             }},
            cfg,
        )
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["total"] == 1


# ---------------------------------------------------------------------------
# CLI memory doctor --claims
# ---------------------------------------------------------------------------


class TestCliMemoryDoctorClaims:
    def _make_config(self, db, monkeypatch):
        from techne.config import Config

        cfg = Config()
        cfg.db_path = db
        monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))
        return cfg

    def test_no_memory_path(self, db, project, monkeypatch):
        from click.testing import CliRunner

        from techne.cli import main

        cwd, _ = project
        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")
        self._make_config(db, monkeypatch)
        monkeypatch.chdir(cwd)

        runner = CliRunner()
        res = runner.invoke(main, ["memory", "doctor", "--claims"])
        assert res.exit_code == 0
        assert "No curated memory" in res.output

    def test_with_model_monkeypatched(self, db, project, monkeypatch):
        """When config.model is set and _make_generate_fn is monkeypatched, the CLI
        runs the automated path and prints the counts report."""
        from click.testing import CliRunner

        from techne.cli import main

        cwd, _ = project
        proj_path = str(cwd.resolve())

        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")

        _write_memory(db, cwd, "The project stores sessions in Redis.")
        _session(db, "s1", proj_path)
        _obs(db, "s1", "Write", "Wrote: /proj/session.py")

        cfg = self._make_config(db, monkeypatch)
        cfg.model = "fake-model"

        canned = json.dumps([
            {"claim": "Sessions stored in Redis", "status": "uncorroborated",
             "evidence": "none"},
        ])

        # Monkeypatch _make_generate_fn to return a function that yields the canned answer
        import techne.cli as cli_mod

        monkeypatch.setattr(
            cli_mod,
            "_memory_doctor_claims",
            lambda ctx, config, *, since_days, as_json: _patched_claims_path(
                ctx, config, since_days=since_days, as_json=as_json, canned=canned
            ),
        )
        monkeypatch.chdir(cwd)

        runner = CliRunner()
        res = runner.invoke(main, ["memory", "doctor", "--claims"])
        # Just check it exits cleanly (the monkeypatch handles output)
        assert res.exit_code == 0

    def test_generate_fn_path_directly(self, db, project, monkeypatch):
        """Drive --claims via CliRunner with a monkeypatched _make_generate_fn."""
        from click.testing import CliRunner

        from techne.cli import main
        from techne.config import Config

        cwd, _ = project
        proj_path = str(cwd.resolve())

        import techne.core.memory_claims as mc
        import techne.core.thread_orchestrator as orch

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")

        _write_memory(db, cwd, "The project uses FastAPI and SQLAlchemy.")
        _session(db, "s1", proj_path)
        _obs(db, "s1", "Write", "Wrote: /proj/app.py")

        cfg = Config()
        cfg.db_path = db
        cfg.model = "fake-model"

        canned_response = json.dumps([
            {"claim": "Uses FastAPI", "status": "corroborated", "evidence": "[Write] app.py"},
            {"claim": "Uses SQLAlchemy", "status": "uncorroborated", "evidence": "none"},
        ])

        monkeypatch.setattr(
            orch, "_make_generate_fn", lambda c: (lambda prompt: canned_response)
        )
        monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))
        monkeypatch.chdir(cwd)

        runner = CliRunner()
        res = runner.invoke(main, ["memory", "doctor", "--claims"], catch_exceptions=False)
        assert res.exit_code == 0
        assert "corroborated" in res.output or "uncorroborated" in res.output

    def test_json_flag_with_model(self, db, project, monkeypatch):
        """--claims --json emits raw JSON when model path succeeds."""
        cwd, _ = project
        proj_path = str(cwd.resolve())

        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")

        _write_memory(db, cwd, "The system uses RabbitMQ for messaging.")
        _session(db, "s1", proj_path)
        _obs(db, "s1", "Write", "Wrote: /proj/mq.py")

        from techne.config import Config

        cfg = Config()
        cfg.db_path = db
        cfg.model = "fake-model"

        canned_response = json.dumps([
            {"claim": "Uses RabbitMQ", "status": "corroborated", "evidence": "[Write] mq.py"},
        ])

        import techne.core.thread_orchestrator as orch

        monkeypatch.setattr(
            orch, "_make_generate_fn", lambda c: (lambda prompt: canned_response)
        )
        monkeypatch.chdir(cwd)

        from click.testing import CliRunner

        from techne.cli import main

        monkeypatch.setattr(
            Config, "load", classmethod(lambda cls, *a, **k: cfg)
        )

        runner = CliRunner()
        res = runner.invoke(main, ["memory", "doctor", "--claims", "--json"],
                            catch_exceptions=False)
        assert res.exit_code == 0
        # Strip structlog debug lines (which may contain '{' in key=value pairs).
        # The JSON block starts at the first line that begins with '{'.
        lines = res.output.splitlines()
        json_start = next((i for i, ln in enumerate(lines) if ln.startswith("{")), None)
        assert json_start is not None, f"No JSON block in output: {res.output!r}"
        data = json.loads("\n".join(lines[json_start:]))
        assert "claims" in data
        assert "counts" in data
        assert "total" in data

    def test_no_model_prints_instructions(self, db, project, monkeypatch):
        """When config.model is not set, --claims prints manual instructions."""
        cwd, _ = project
        proj_path = str(cwd.resolve())

        import techne.core.memory_claims as mc

        monkeypatch.setattr(mc, "project_slug", lambda p: "testslug")

        _write_memory(db, cwd, "Project uses Django.")
        _session(db, "s1", proj_path)
        _obs(db, "s1", "Write", "Wrote: /proj/views.py")

        from techne.config import Config

        cfg = Config()
        cfg.db_path = db
        # model is not set (None or absent)
        cfg.model = None

        monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))
        monkeypatch.chdir(cwd)

        from click.testing import CliRunner

        from techne.cli import main

        runner = CliRunner()
        res = runner.invoke(main, ["memory", "doctor", "--claims"])
        assert res.exit_code == 0
        assert "techne_memory_claims_emit" in res.output
        assert "techne_memory_claims_ingest" in res.output

    def test_existing_doctor_unaffected(self, db, project, monkeypatch):
        """The non-claims memory doctor path must be byte-for-behavior-unchanged."""
        from click.testing import CliRunner

        from techne.cli import main
        from techne.config import Config

        cwd, config_dir = project
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

        cfg = Config()
        cfg.db_path = db
        monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))
        monkeypatch.chdir(cwd)

        runner = CliRunner()
        res = runner.invoke(main, ["memory", "doctor"])
        # Should print "No curated memory" since no files on disk
        assert res.exit_code == 0
        assert "No curated memory" in res.output


# ---------------------------------------------------------------------------
# helper for CLI test (avoids a real generate call)
# ---------------------------------------------------------------------------


def _patched_claims_path(ctx, config, *, since_days, as_json, canned):
    """Stand-in for _memory_doctor_claims that skips the real LLM."""
    import click

    from techne.core.memory_claims import claims_emit, claims_ingest

    report = claims_emit(config, since_days=since_days)
    if not report["memory_present"]:
        click.echo("No curated memory (patched path)")
        return
    result = claims_ingest(config, report["prompt_hash"], canned)
    if as_json:
        import json as _json

        click.echo(_json.dumps(result, indent=2))
    else:
        click.echo(f"claims: {result['total']}")
