"""Tests for techne#105 evidence gate — query_turn_evidence + check_claims.

Coverage:
- query_turn_evidence: artifacts, commits, actions, any_tool_call, observation_count.
- since_iso window filtering.
- SuiteEvidence: parsed counts and suite_green flag.
- check_claims: all supported types, backed/unbacked/unverifiable.
- MCP techne_check_evidence round-trip.
- CLI `techne evidence <session>` smoke test.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.evidence import (
    Evidence,
    SuiteEvidence,
    check_claims,
    query_turn_evidence,
)
from techne.mcp_server import handle_request

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "techne.db"
    conn = init_db(path)
    conn.close()
    return path


@pytest.fixture
def config(db_path):
    cfg = Config()
    cfg.db_path = db_path
    return cfg


def _insert_session(db_path, session_id: str = "sess-ev-001"):
    """Create a bare session row."""
    conn = connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, project_path) VALUES (?, ?)",
        (session_id, "/tmp/test"),
    )
    conn.commit()
    conn.close()


def _insert_obs(
    db_path, session_id: str, tool_name: str, content: str, timestamp: str | None = None
):
    """Insert a raw observation row, bypassing inference."""
    conn = connect(db_path)
    if timestamp:
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, tier, timestamp) "
            "VALUES (?, ?, ?, 'hot', ?)",
            (session_id, tool_name, content, timestamp),
        )
    else:
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, tier) "
            "VALUES (?, ?, ?, 'hot')",
            (session_id, tool_name, content),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# query_turn_evidence: basic extraction
# ---------------------------------------------------------------------------


class TestQueryTurnEvidence:
    def test_empty_session_returns_zeroed_evidence(self, db_path):
        _insert_session(db_path, "empty-sess")
        ev = query_turn_evidence(db_path, "empty-sess")
        assert ev.observation_count == 0
        assert ev.any_tool_call is False
        assert ev.artifacts == []
        assert ev.commits == []
        assert ev.actions == []
        assert ev.tests.suite_green is None

    def test_write_obs_parsed_into_artifacts(self, db_path):
        _insert_session(db_path, "sess-w")
        _insert_obs(
            db_path, "sess-w", "Write", "Wrote: /tmp/proj/foo.py\nContent: def main(): pass..."
        )
        ev = query_turn_evidence(db_path, "sess-w")
        assert "/tmp/proj/foo.py" in ev.artifacts
        assert ev.any_tool_call is True
        assert ev.observation_count == 1

    def test_edit_obs_parsed_into_artifacts(self, db_path):
        _insert_session(db_path, "sess-e")
        _insert_obs(db_path, "sess-e", "Edit", "Edited: /tmp/proj/bar.py\nChange: old → new")
        ev = query_turn_evidence(db_path, "sess-e")
        assert "/tmp/proj/bar.py" in ev.artifacts

    def test_write_and_edit_deduped(self, db_path):
        """The same path written twice should appear once."""
        _insert_session(db_path, "sess-dedup")
        _insert_obs(db_path, "sess-dedup", "Write", "Wrote: /tmp/a.py\nContent: ...")
        _insert_obs(db_path, "sess-dedup", "Edit", "Edited: /tmp/a.py\nChange: x → y")
        ev = query_turn_evidence(db_path, "sess-dedup")
        assert ev.artifacts.count("/tmp/a.py") == 1

    def test_bash_command_parsed_into_actions(self, db_path):
        _insert_session(db_path, "sess-b")
        _insert_obs(db_path, "sess-b", "Bash", "Ran: ls -la\nOutput: foo.py")
        ev = query_turn_evidence(db_path, "sess-b")
        assert "ls -la" in ev.actions

    def test_bash_commit_sha_parsed(self, db_path):
        _insert_session(db_path, "sess-git")
        _insert_obs(
            db_path, "sess-git", "Bash",
            "Ran: git commit -m 'feat: add evidence'\n"
            "Output: [main abc1234] feat: add evidence\n 1 file changed",
        )
        ev = query_turn_evidence(db_path, "sess-git")
        assert "abc1234" in ev.commits

    def test_commits_deduped(self, db_path):
        _insert_session(db_path, "sess-dup-sha")
        # Same SHA from two Bash rows (e.g. verbose git output).
        for _ in range(2):
            _insert_obs(
                db_path, "sess-dup-sha", "Bash",
                "Ran: git log --oneline\nOutput: [main abc1234] some commit",
            )
        ev = query_turn_evidence(db_path, "sess-dup-sha")
        assert ev.commits.count("abc1234") == 1

    def test_mixed_session(self, db_path):
        """Write + Edit + Bash together — all fields populated."""
        _insert_session(db_path, "sess-mix")
        _insert_obs(db_path, "sess-mix", "Write", "Wrote: /proj/a.py\nContent: ...")
        _insert_obs(db_path, "sess-mix", "Edit", "Edited: /proj/b.py\nChange: x → y")
        _insert_obs(
            db_path, "sess-mix", "Bash",
            "Ran: git commit -m 'wip'\nOutput: [main deadbeef] wip",
        )
        ev = query_turn_evidence(db_path, "sess-mix")
        assert "/proj/a.py" in ev.artifacts
        assert "/proj/b.py" in ev.artifacts
        assert "deadbeef" in ev.commits
        assert "git commit -m 'wip'" in ev.actions
        assert ev.any_tool_call is True
        assert ev.observation_count == 3

    def test_userprompt_does_not_set_any_tool_call(self, db_path):
        """UserPrompt is a non-deed meta-record and must not count as agent activity."""
        _insert_session(db_path, "sess-up")
        _insert_obs(db_path, "sess-up", "UserPrompt", "Tool: UserPrompt\nInput: hello\nOutput: ")
        ev = query_turn_evidence(db_path, "sess-up")
        assert ev.any_tool_call is False

    def test_other_tool_sets_any_tool_call(self, db_path):
        """A non-excluded tool (e.g. Read) does set any_tool_call."""
        _insert_session(db_path, "sess-read")
        _insert_obs(db_path, "sess-read", "Read", "Tool: Read\nInput: {}\nOutput: ...")
        ev = query_turn_evidence(db_path, "sess-read")
        assert ev.any_tool_call is True


# ---------------------------------------------------------------------------
# since_iso window filtering
# ---------------------------------------------------------------------------


class TestSinceIsoWindow:
    def test_observations_before_cutoff_excluded(self, db_path):
        _insert_session(db_path, "sess-win")
        _insert_obs(db_path, "sess-win", "Write",
                    "Wrote: /old.py\nContent: ...",
                    timestamp="2026-01-01T00:00:00")
        _insert_obs(db_path, "sess-win", "Write",
                    "Wrote: /new.py\nContent: ...",
                    timestamp="2026-06-01T00:00:00")
        ev = query_turn_evidence(db_path, "sess-win", since_iso="2026-03-01T00:00:00")
        assert "/new.py" in ev.artifacts
        assert "/old.py" not in ev.artifacts
        assert ev.observation_count == 1

    def test_since_iso_none_returns_all(self, db_path):
        _insert_session(db_path, "sess-all")
        _insert_obs(db_path, "sess-all", "Write",
                    "Wrote: /old.py\nContent: ...",
                    timestamp="2026-01-01T00:00:00")
        _insert_obs(db_path, "sess-all", "Write",
                    "Wrote: /new.py\nContent: ...",
                    timestamp="2026-06-01T00:00:00")
        ev = query_turn_evidence(db_path, "sess-all")
        assert ev.observation_count == 2
        assert "/old.py" in ev.artifacts
        assert "/new.py" in ev.artifacts

    def test_cutoff_at_boundary_is_exclusive(self, db_path):
        """timestamp > since_iso, so an observation exactly at the cutoff is excluded."""
        _insert_session(db_path, "sess-bnd")
        _insert_obs(db_path, "sess-bnd", "Write",
                    "Wrote: /exact.py\nContent: ...",
                    timestamp="2026-06-01T12:00:00")
        ev = query_turn_evidence(db_path, "sess-bnd", since_iso="2026-06-01T12:00:00")
        assert ev.observation_count == 0


# ---------------------------------------------------------------------------
# SuiteEvidence: test result parsing
# ---------------------------------------------------------------------------


class TestSuiteEvidence:
    def test_green_suite_parsed(self, db_path):
        _insert_session(db_path, "sess-tg")
        _insert_obs(
            db_path, "sess-tg", "Bash",
            "Ran: pytest -q\nOutput: 5 passed in 0.50s",
        )
        ev = query_turn_evidence(db_path, "sess-tg")
        assert ev.tests.passed == 5
        assert ev.tests.suite_green is True

    def test_failing_suite_parsed(self, db_path):
        _insert_session(db_path, "sess-tf")
        _insert_obs(
            db_path, "sess-tf", "Bash",
            "Ran: pytest -q\nOutput: 1 failed, 3 passed in 0.50s",
        )
        ev = query_turn_evidence(db_path, "sess-tf")
        assert ev.tests.passed == 3
        assert ev.tests.failed == 1
        assert ev.tests.suite_green is False

    def test_no_test_output_returns_none(self, db_path):
        _insert_session(db_path, "sess-nt")
        _insert_obs(db_path, "sess-nt", "Bash", "Ran: ls\nOutput: foo.py  bar.py")
        ev = query_turn_evidence(db_path, "sess-nt")
        assert ev.tests.suite_green is None
        assert ev.tests.passed is None

    def test_error_count_makes_suite_red(self, db_path):
        _insert_session(db_path, "sess-te")
        _insert_obs(
            db_path, "sess-te", "Bash",
            "Ran: pytest\nOutput: 2 passed, 1 error in 0.12s",
        )
        ev = query_turn_evidence(db_path, "sess-te")
        assert ev.tests.errors == 1
        assert ev.tests.suite_green is False  # error makes it red

    def test_max_aggregation_across_rows(self, db_path):
        """When multiple Bash rows have test output, we take the max, not the sum."""
        _insert_session(db_path, "sess-max")
        _insert_obs(db_path, "sess-max", "Bash", "Ran: pytest part1\nOutput: 10 passed in 0.1s")
        _insert_obs(db_path, "sess-max", "Bash", "Ran: pytest part2\nOutput: 3 passed in 0.05s")
        ev = query_turn_evidence(db_path, "sess-max")
        assert ev.tests.passed == 10  # max(10, 3), not 13


# ---------------------------------------------------------------------------
# check_claims
# ---------------------------------------------------------------------------


class TestCheckClaims:
    def _ev(self, **kwargs) -> Evidence:
        """Build a minimal Evidence for unit-testing check_claims directly."""
        t = SuiteEvidence(
            passed=kwargs.pop("passed", None),
            failed=kwargs.pop("failed", None),
            errors=kwargs.pop("errors", None),
            suite_green=kwargs.pop("suite_green", None),
        )
        return Evidence(tests=t, **kwargs)

    # --- Bash-capture-off honesty: Bash-derived claims are UNVERIFIABLE, not unbacked ---

    def test_commit_unverifiable_when_bash_off(self):
        """capture_bash off → a real commit isn't recorded, so 'commit' must be
        unverifiable (absent), NOT unbacked (disproven) — no false flag (techne#105)."""
        ev = self._ev(any_tool_call=True, commits=[], bash_captured=False)
        verdicts = check_claims([{"type": "commit"}], ev)
        assert verdicts[0]["status"] == "unverifiable"

    def test_tests_green_unverifiable_when_bash_off(self):
        ev = self._ev(any_tool_call=True, bash_captured=False)
        verdicts = check_claims([{"type": "tests_green"}], ev)
        assert verdicts[0]["status"] == "unverifiable"

    def test_ran_action_unverifiable_when_bash_off(self):
        ev = self._ev(any_tool_call=True, actions=[], bash_captured=False)
        verdicts = check_claims([{"type": "ran_action", "match": "pytest"}], ev)
        assert verdicts[0]["status"] == "unverifiable"

    def test_bash_off_does_not_affect_artifact_claims(self):
        """Write/Edit are captured by default, so wrote_file is unaffected by bash_captured."""
        ev = self._ev(artifacts=["/x/y.py"], bash_captured=False)
        verdicts = check_claims([{"type": "wrote_file", "path": "y.py"}], ev)
        assert verdicts[0]["status"] == "backed"

    def test_completion_backed_when_tool_called(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=[], actions=[])
        verdicts = check_claims([{"type": "completion"}], ev)
        assert verdicts[0]["status"] == "backed"

    def test_completion_unbacked_when_no_activity(self):
        ev = self._ev(any_tool_call=False, artifacts=[], commits=[], actions=[])
        verdicts = check_claims([{"type": "completion"}], ev)
        assert verdicts[0]["status"] == "unbacked"

    def test_did_work_alias(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=[], actions=[])
        verdicts = check_claims([{"type": "did_work"}], ev)
        assert verdicts[0]["status"] == "backed"

    def test_commit_backed_when_commits_present(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=["abc1234"], actions=[])
        verdicts = check_claims([{"type": "commit"}], ev)
        assert verdicts[0]["status"] == "backed"

    def test_commit_unbacked_when_no_commits(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=[], actions=[])
        verdicts = check_claims([{"type": "commit"}], ev)
        assert verdicts[0]["status"] == "unbacked"

    def test_commit_with_sha_match(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=["abc1234", "def5678"], actions=[])
        verdicts = check_claims([{"type": "commit", "sha": "abc"}], ev)
        assert verdicts[0]["status"] == "backed"

    def test_commit_with_sha_no_match(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=["def5678"], actions=[])
        verdicts = check_claims([{"type": "commit", "sha": "abc"}], ev)
        assert verdicts[0]["status"] == "unbacked"

    def test_tests_green_backed(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=[], actions=[],
                      suite_green=True, passed=5, failed=0)
        verdicts = check_claims([{"type": "tests_green"}], ev)
        assert verdicts[0]["status"] == "backed"

    def test_tests_green_unbacked(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=[], actions=[],
                      suite_green=False, passed=3, failed=1)
        verdicts = check_claims([{"type": "tests_green"}], ev)
        assert verdicts[0]["status"] == "unbacked"

    def test_tests_green_unverifiable(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=[], actions=[],
                      suite_green=None)
        verdicts = check_claims([{"type": "tests_green"}], ev)
        assert verdicts[0]["status"] == "unverifiable"

    def test_ran_action_backed(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=[], actions=["pytest -q"])
        verdicts = check_claims([{"type": "ran_action", "match": "pytest"}], ev)
        assert verdicts[0]["status"] == "backed"

    def test_ran_action_unbacked(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=[], actions=["ls -la"])
        verdicts = check_claims([{"type": "ran_action", "match": "pytest"}], ev)
        assert verdicts[0]["status"] == "unbacked"

    def test_ran_action_missing_match_field(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=[], actions=["ls"])
        verdicts = check_claims([{"type": "ran_action"}], ev)
        assert verdicts[0]["status"] == "unverifiable"

    def test_wrote_file_exact_match(self):
        ev = self._ev(any_tool_call=True, artifacts=["/proj/foo.py"], commits=[], actions=[])
        verdicts = check_claims([{"type": "wrote_file", "path": "/proj/foo.py"}], ev)
        assert verdicts[0]["status"] == "backed"

    def test_wrote_file_suffix_match(self):
        ev = self._ev(any_tool_call=True, artifacts=["/proj/foo.py"], commits=[], actions=[])
        verdicts = check_claims([{"type": "wrote_file", "path": "foo.py"}], ev)
        assert verdicts[0]["status"] == "backed"

    def test_wrote_file_unbacked(self):
        ev = self._ev(any_tool_call=True, artifacts=["/proj/other.py"], commits=[], actions=[])
        verdicts = check_claims([{"type": "wrote_file", "path": "foo.py"}], ev)
        assert verdicts[0]["status"] == "unbacked"

    def test_wrote_file_missing_path_field(self):
        ev = self._ev(any_tool_call=True, artifacts=["/proj/foo.py"], commits=[], actions=[])
        verdicts = check_claims([{"type": "wrote_file"}], ev)
        assert verdicts[0]["status"] == "unverifiable"

    def test_unknown_type_unverifiable(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=[], actions=[])
        verdicts = check_claims([{"type": "alien_claim"}], ev)
        assert verdicts[0]["status"] == "unverifiable"
        assert "alien_claim" in verdicts[0]["detail"]

    def test_multiple_claims_unbacked_count(self):
        ev = self._ev(any_tool_call=False, artifacts=[], commits=[], actions=[],
                      suite_green=None)
        claims = [
            {"type": "completion"},       # unbacked
            {"type": "tests_green"},      # unverifiable
            {"type": "wrote_file", "path": "x.py"},  # unbacked
        ]
        verdicts = check_claims(claims, ev)
        unbacked = [v for v in verdicts if v["status"] == "unbacked"]
        assert len(unbacked) == 2

    def test_claim_dict_preserved_in_output(self):
        ev = self._ev(any_tool_call=False, artifacts=[], commits=[], actions=[])
        claim = {"type": "completion", "extra": "meta"}
        verdicts = check_claims([claim], ev)
        assert verdicts[0]["claim"] == claim


# ---------------------------------------------------------------------------
# MCP techne_check_evidence
# ---------------------------------------------------------------------------


class TestMcpCheckEvidence:
    def test_round_trip_returns_correct_structure(self, config, db_path):
        _insert_session(db_path, "sess-mcp")
        _insert_obs(db_path, "sess-mcp", "Write", "Wrote: /tmp/out.py\nContent: x=1...")
        _insert_obs(
            db_path, "sess-mcp", "Bash",
            "Ran: git commit -m 'add'\nOutput: [main abc9999] add\n",
        )

        response = handle_request(
            {
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "techne_check_evidence",
                    "arguments": {
                        "session_id": "sess-mcp",
                        "claims": [
                            {"type": "wrote_file", "path": "out.py"},
                            {"type": "commit"},
                            {"type": "tests_green"},
                        ],
                    },
                },
            },
            config,
        )
        result = response["result"]
        assert "isError" not in result or result.get("isError") is False
        text = result["content"][0]["text"]
        import json
        data = json.loads(text)
        assert "evidence" in data
        assert "verdicts" in data
        assert "unbacked_count" in data

        verdicts_by_type = {v["claim"]["type"]: v["status"] for v in data["verdicts"]}
        assert verdicts_by_type["wrote_file"] == "backed"
        assert verdicts_by_type["commit"] == "backed"
        assert verdicts_by_type["tests_green"] == "unverifiable"
        assert data["unbacked_count"] == 0

    def test_unbacked_count_correct(self, config, db_path):
        _insert_session(db_path, "sess-ub")
        # No observations, and capture_bash is off by default → completion is
        # unbacked, but commit is UNVERIFIABLE (absent + Bash not recorded), not
        # unbacked — so unbacked_count is 1, not 2 (techne#105 no-false-flag).

        response = handle_request(
            {
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "techne_check_evidence",
                    "arguments": {
                        "session_id": "sess-ub",
                        "claims": [
                            {"type": "completion"},
                            {"type": "commit"},
                        ],
                    },
                },
            },
            config,
        )
        import json
        data = json.loads(response["result"]["content"][0]["text"])
        assert data["unbacked_count"] == 1
        by_type = {v["claim"]["type"]: v["status"] for v in data["verdicts"]}
        assert by_type["completion"] == "unbacked"
        assert by_type["commit"] == "unverifiable"

    def test_missing_session_id_returns_error(self, config):
        response = handle_request(
            {
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "techne_check_evidence",
                    "arguments": {"claims": []},
                },
            },
            config,
        )
        assert response["result"].get("isError") is True

    def test_tool_listed_in_tools_list(self, config):
        response = handle_request({"id": 4, "method": "tools/list"}, config)
        names = {t["name"] for t in response["result"]["tools"]}
        assert "techne_check_evidence" in names

    def test_description_leads_with_trigger(self, config):
        from techne.mcp_server import TOOLS
        by_name = {t["name"]: t["description"] for t in TOOLS}
        assert by_name["techne_check_evidence"].startswith("Call this before asserting")


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


class TestEvidenceCli:
    @pytest.fixture
    def cli_config(self, tmp_path, monkeypatch):
        db = tmp_path / "techne.db"
        cfg = Config()
        cfg.db_path = db
        monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
        return cfg

    def test_cli_prints_evidence_view(self, cli_config, db_path=None):
        """techne evidence <session> prints the key fields."""
        # Use the config's db_path (initialized by Config()).
        db = cli_config.db_path
        # init_db so the schema exists.
        conn = init_db(db)
        conn.close()
        _insert_session(db, "sess-cli")
        _insert_obs(db, "sess-cli", "Write", "Wrote: /tmp/cli.py\nContent: ...")
        _insert_obs(db, "sess-cli", "Bash",
                    "Ran: pytest\nOutput: 7 passed in 1.2s")

        runner = CliRunner()
        result = runner.invoke(main, ["evidence", "sess-cli"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        out = result.output
        assert "sess-cli" in out
        assert "Observations: 2" in out
        assert "/tmp/cli.py" in out
        assert "GREEN" in out

    def test_cli_since_option(self, cli_config):
        db = cli_config.db_path
        conn = init_db(db)
        conn.close()
        _insert_session(db, "sess-cli2")
        _insert_obs(db, "sess-cli2", "Write",
                    "Wrote: /tmp/old.py\nContent: ...",
                    timestamp="2026-01-01T00:00:00")
        _insert_obs(db, "sess-cli2", "Write",
                    "Wrote: /tmp/new.py\nContent: ...",
                    timestamp="2026-06-01T00:00:00")

        runner = CliRunner()
        result = runner.invoke(
            main, ["evidence", "sess-cli2", "--since", "2026-03-01T00:00:00"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "/tmp/new.py" in result.output
        assert "/tmp/old.py" not in result.output

    def test_cli_no_artifacts_no_commits(self, cli_config):
        db = cli_config.db_path
        conn = init_db(db)
        conn.close()
        _insert_session(db, "sess-cli3")

        runner = CliRunner()
        result = runner.invoke(main, ["evidence", "sess-cli3"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "Artifacts: (none)" in result.output
        assert "Commits: (none)" in result.output
        assert "unparsed" in result.output
