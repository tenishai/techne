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

    def test_multi_line_command_is_kept_whole(self, db_path):
        """Issue #713: `cd <repo>` on line 1 hid everything after it from ran_action."""
        _insert_session(db_path, "sess-ml")
        _insert_obs(
            db_path,
            "sess-ml",
            "Bash",
            "Ran: cd /repo\ngh pr merge 709 --squash --delete-branch \\\n  --admin"
            "\nOutput: merged",
        )
        ev = query_turn_evidence(db_path, "sess-ml")
        assert len(ev.actions) == 1
        assert "gh pr merge 709" in ev.actions[0]
        assert "merged" not in ev.actions[0], "output must never enter an action"
        verdict = check_claims([{"type": "ran_action", "match": "gh pr merge"}], ev)[0]
        assert verdict["status"] == "backed"

    def test_a_heredoc_body_is_data_not_an_action(self, db_path):
        """Prudence, PR #1061: a PR body describing a merge must not back the merge."""
        _insert_session(db_path, "sess-hd")
        _insert_obs(
            db_path,
            "sess-hd",
            "Bash",
            "Ran: cd /repo\ngh pr create --body-file - <<'EOF'\nthen run gh pr merge 1061\nEOF"
            "\nOutput: https://github.com/o/r/pull/1061",
        )
        ev = query_turn_evidence(db_path, "sess-hd")
        assert "gh pr create" in ev.actions[0]
        verdict = check_claims([{"type": "ran_action", "match": "gh pr merge 1061"}], ev)[0]
        assert verdict["status"] == "unbacked"

    def test_output_marker_in_the_command_never_pulls_output_into_the_action(self, db_path):
        """Two markers: the cut is at the first, so the action is a command prefix."""
        _insert_session(db_path, "sess-m2")
        _insert_obs(
            db_path,
            "sess-m2",
            "Bash",
            "Ran: git commit -m 'x\nOutput: fake'\nOutput: gh pr merge printed here",
        )
        ev = query_turn_evidence(db_path, "sess-m2")
        assert ev.actions == ["git commit -m 'x"]
        verdict = check_claims([{"type": "ran_action", "match": "gh pr merge"}], ev)[0]
        assert verdict["status"] == "unbacked"

    def test_bash_commit_sha_parsed(self, db_path):
        _insert_session(db_path, "sess-git")
        _insert_obs(
            db_path,
            "sess-git",
            "Bash",
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
                db_path,
                "sess-dup-sha",
                "Bash",
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
            db_path,
            "sess-mix",
            "Bash",
            "Ran: git commit -m 'wip'\nOutput: [main deadbeef] wip",
        )
        ev = query_turn_evidence(db_path, "sess-mix")
        assert "/proj/a.py" in ev.artifacts
        assert "/proj/b.py" in ev.artifacts
        assert "deadbeef" in ev.commits
        assert "git commit -m 'wip'" in ev.actions
        assert ev.any_tool_call is True
        assert ev.active_tool_call is True  # Write/Edit/Bash are deeds
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

    def test_passive_tool_does_not_set_active_tool_call(self, db_path):
        """Read/Grep/Glob are passive inspection: they set any_tool_call but NOT
        active_tool_call, so a look-only turn cannot back completion (techne#105)."""
        _insert_session(db_path, "sess-passive")
        _insert_obs(db_path, "sess-passive", "Read", "Tool: Read\nInput: {}\nOutput: ...")
        _insert_obs(db_path, "sess-passive", "Grep", "Tool: Grep\nInput: {}\nOutput: ...")
        ev = query_turn_evidence(db_path, "sess-passive")
        assert ev.any_tool_call is True
        assert ev.active_tool_call is False

    def test_non_passive_tool_sets_active_tool_call(self, db_path):
        """A tool outside the passive set (e.g. an MCP call) counts as a deed — an
        unknown/non-inspection tool is treated as active (safe direction).
        NOTE: read-only MCP lookups (e.g. techne_recall) also land here as 'active' —
        a known, documented limitation (PASSIVE_TOOLS lists only built-in inspection
        tools); it fails in the safe direction (false-back, not false-flag)."""
        _insert_session(db_path, "sess-active")
        _insert_obs(
            db_path,
            "sess-active",
            "mcp__techne__techne_store",
            "Tool: mcp__techne__techne_store\nInput: {}\nOutput: ok",
        )
        ev = query_turn_evidence(db_path, "sess-active")
        assert ev.active_tool_call is True


# ---------------------------------------------------------------------------
# since_iso window filtering
# ---------------------------------------------------------------------------


class TestSinceIsoWindow:
    def test_observations_before_cutoff_excluded(self, db_path):
        _insert_session(db_path, "sess-win")
        _insert_obs(
            db_path,
            "sess-win",
            "Write",
            "Wrote: /old.py\nContent: ...",
            timestamp="2026-01-01T00:00:00",
        )
        _insert_obs(
            db_path,
            "sess-win",
            "Write",
            "Wrote: /new.py\nContent: ...",
            timestamp="2026-06-01T00:00:00",
        )
        ev = query_turn_evidence(db_path, "sess-win", since_iso="2026-03-01T00:00:00")
        assert "/new.py" in ev.artifacts
        assert "/old.py" not in ev.artifacts
        assert ev.observation_count == 1

    def test_since_iso_none_returns_all(self, db_path):
        _insert_session(db_path, "sess-all")
        _insert_obs(
            db_path,
            "sess-all",
            "Write",
            "Wrote: /old.py\nContent: ...",
            timestamp="2026-01-01T00:00:00",
        )
        _insert_obs(
            db_path,
            "sess-all",
            "Write",
            "Wrote: /new.py\nContent: ...",
            timestamp="2026-06-01T00:00:00",
        )
        ev = query_turn_evidence(db_path, "sess-all")
        assert ev.observation_count == 2
        assert "/old.py" in ev.artifacts
        assert "/new.py" in ev.artifacts

    def test_cutoff_at_boundary_is_exclusive(self, db_path):
        """timestamp > since_iso, so an observation exactly at the cutoff is excluded."""
        _insert_session(db_path, "sess-bnd")
        _insert_obs(
            db_path,
            "sess-bnd",
            "Write",
            "Wrote: /exact.py\nContent: ...",
            timestamp="2026-06-01T12:00:00",
        )
        ev = query_turn_evidence(db_path, "sess-bnd", since_iso="2026-06-01T12:00:00")
        assert ev.observation_count == 0


# ---------------------------------------------------------------------------
# SuiteEvidence: test result parsing
# ---------------------------------------------------------------------------


class TestSuiteEvidence:
    def test_green_suite_parsed(self, db_path):
        _insert_session(db_path, "sess-tg")
        _insert_obs(
            db_path,
            "sess-tg",
            "Bash",
            "Ran: pytest -q\nOutput: 5 passed in 0.50s",
        )
        ev = query_turn_evidence(db_path, "sess-tg")
        assert ev.tests.passed == 5
        assert ev.tests.suite_green is True

    def test_failing_suite_parsed(self, db_path):
        _insert_session(db_path, "sess-tf")
        _insert_obs(
            db_path,
            "sess-tf",
            "Bash",
            "Ran: pytest -q\nOutput: 1 failed, 3 passed in 0.50s",
        )
        ev = query_turn_evidence(db_path, "sess-tf")
        assert ev.tests.passed == 3
        assert ev.tests.failed == 1
        assert ev.tests.suite_green is False

    def test_green_suite_still_green_when_output_is_intact(self, db_path):
        """A green run prints NO failure line at all, and that must stay green.

        This is the load-bearing case, not an edge case: pytest, jest and vitest
        all omit "N failed" entirely when nothing fails, so `failed is None` is
        what a passing suite normally looks like. The techne#438 fix must not
        reach it — treating a missing failure count as unverifiable here would
        make every ordinary green run unverifiable and undo PR #437.
        """
        _insert_session(db_path, "sess-intact")
        _insert_obs(
            db_path,
            "sess-intact",
            "Bash",
            "Ran: pytest -q\nOutput: 2189 passed, 17 skipped in 68.60s",
        )
        ev = query_turn_evidence(db_path, "sess-intact")
        assert ev.tests.totals_unreliable is False
        assert ev.tests.suite_green is True
        assert check_claims([{"type": "tests_green"}], ev)[0]["status"] == "backed"

    def test_elided_output_without_failure_count_is_not_green(self, db_path):
        """passed survived, failure count did not, and we KNOW output was cut.

        techne#438: `(failed or 0)` read the missing count as zero failures and
        certified the suite green. A green run and a run whose failure line was
        dropped are indistinguishable here, so the only honest answer is
        unverifiable.
        """
        _insert_session(db_path, "sess-elided")
        _insert_obs(
            db_path,
            "sess-elided",
            "Bash",
            "Ran: pytest -q\nOutput: 5 passed"
            "\n… [4000 chars elided] …\n"
            "  coverage: 82% of statements",
        )
        ev = query_turn_evidence(db_path, "sess-elided")
        assert ev.tests.totals_unreliable is True
        assert ev.tests.passed == 5
        assert ev.tests.suite_green is None
        verdict = check_claims([{"type": "tests_green"}], ev)[0]
        assert verdict["status"] == "unverifiable"
        assert "elided" in verdict["detail"]

    def test_surviving_zero_failures_in_bounded_output_is_not_green(self, db_path):
        """A surviving "0 failed" is NOT evidence when the output was cut.

        This test asserted the opposite until Prudence disproved it.
        ``_parse_test_output`` takes the ``max()`` of every count it finds, and
        max is only sound over a COMPLETE sample. In bounded output it runs over
        the surviving subset, so a zero that survived beats a positive that was
        elided — see test_max_over_a_bounded_sample_is_not_evidence for the real
        cargo-workspace shape that does this.

        A POSITIVE count is different and still decides the suite: nothing in the
        dropped middle can make a red run green.
        """
        _insert_session(db_path, "sess-elided-zero")
        _insert_obs(
            db_path,
            "sess-elided-zero",
            "Bash",
            "Ran: jest\nOutput: Tests: 0 failed, 5 passed"
            "\n… [4000 chars elided] …\n"
            "  coverage table",
        )
        ev = query_turn_evidence(db_path, "sess-elided-zero")
        assert ev.tests.totals_unreliable is True
        assert ev.tests.suite_green is None

    def test_elided_output_with_failures_is_still_red(self, db_path):
        """A surviving failure count keeps the suite red — elision never rescues it."""
        _insert_session(db_path, "sess-elided-red")
        _insert_obs(
            db_path,
            "sess-elided-red",
            "Bash",
            "Ran: jest\nOutput: Tests: 3 failed, 5 passed"
            "\n… [4000 chars elided] …\n"
            "  coverage table",
        )
        ev = query_turn_evidence(db_path, "sess-elided-red")
        assert ev.tests.suite_green is False

    def test_intact_row_cannot_vouch_for_a_bounded_row(self, db_path):
        """A small intact run must not supply the failure count a big cut run lost.

        `passed`/`failed` are max()'d across rows. Asking "was output cut?" of the
        union instead of per row let a 3-test `cargo test` printing "0 failed"
        disarm the guard for a separate 900-test run whose own failure line was
        dropped — the pair certified `backed`. Found by Prudence re-reviewing the
        techne#438 fix; the aggregate must never be consulted for this question.
        """
        _insert_session(db_path, "sess-vouch")
        _insert_obs(
            db_path,
            "sess-vouch",
            "Bash",
            "Ran: cargo test -p small\nOutput: test result: ok. 3 passed; 0 failed",
        )
        _insert_obs(
            db_path,
            "sess-vouch",
            "Bash",
            "Ran: pytest -q\nOutput: 900 passed\n… [5000 chars elided] …\ncoverage 82%",
        )
        ev = query_turn_evidence(db_path, "sess-vouch")
        assert ev.tests.totals_unreliable is True
        assert ev.tests.suite_green is None
        assert check_claims([{"type": "tests_green"}], ev)[0]["status"] == "unverifiable"

    def test_zero_errors_does_not_substitute_for_a_missing_failure_count(self, db_path):
        """ "0 errors" says nothing about failures — different categories.

        The guard originally required `errors is None`, so any parsed zero-errors
        anywhere disarmed it and returned green. `_ERRORS_RE` is loose enough to
        match prose, which widens the exposure.
        """
        _insert_session(db_path, "sess-zeroerr")
        _insert_obs(
            db_path,
            "sess-zeroerr",
            "Bash",
            "Ran: pytest -q\nOutput: 900 passed, 0 errors\n… [5000 chars elided] …\ntail",
        )
        ev = query_turn_evidence(db_path, "sess-zeroerr")
        assert ev.tests.suite_green is None
        assert check_claims([{"type": "tests_green"}], ev)[0]["status"] == "unverifiable"

    def test_positive_errors_still_makes_the_suite_red(self, db_path):
        """A row with real errors is red, not unverifiable — it can't false-green."""
        _insert_session(db_path, "sess-poserr")
        _insert_obs(
            db_path,
            "sess-poserr",
            "Bash",
            "Ran: pytest -q\nOutput: 900 passed, 4 errors\n… [5000 chars elided] …\ntail",
        )
        ev = query_turn_evidence(db_path, "sess-poserr")
        assert ev.tests.suite_green is False

    def test_legacy_unmarked_truncation_is_not_read_as_intact(self, db_path):
        """Pre-#437 rows were cut head-only and carry NO elision marker.

        `was_elided` alone returns False for them, so the gate read exactly the
        rows the marker was invented to describe as authoritative and returned
        `backed`. Recognised by length instead: the current writer never emits an
        unmarked output at or over budget.
        """
        _insert_session(db_path, "sess-legacy")
        legacy_output = ("900 passed" + "\n" + "." * 4000)[:2000]
        assert "elided" not in legacy_output
        _insert_obs(db_path, "sess-legacy", "Bash", f"Ran: pytest -q\nOutput: {legacy_output}")
        ev = query_turn_evidence(db_path, "sess-legacy")
        assert ev.tests.totals_unreliable is True
        assert ev.tests.suite_green is None

    def test_max_over_a_bounded_sample_is_not_evidence(self, db_path):
        """Built through the REAL summarize()/_preview(), not hand-written content.

        Every other test here writes `content` directly, in the ideal shape — the
        one shape that cannot exhibit this bug. A cargo workspace prints per-crate
        results, so a small green crate's "0 failed" lands in the head while the
        real "900 passed; 12 failed" sits in the middle that gets dropped. The
        surviving zero then wins the max() and the gate certified green with 12
        tests failing.
        """
        from techne.hooks.post_tool_use import summarize

        raw = (
            "test result: ok. 3 passed; 0 failed\n"
            + ("compiling some-crate v0.1.0\n" * 120)
            + "test result: FAILED. 900 passed; 12 failed\n"
            + ("warning: unused import\n" * 40)
        )
        content = summarize("Bash", {"command": "cargo test --workspace"}, raw)
        assert "12 failed" not in content, "fixture must drop the real failure count"
        assert "0 failed" in content, "fixture must keep the misleading zero"

        _insert_session(db_path, "sess-cargo")
        _insert_obs(db_path, "sess-cargo", "Bash", content)
        ev = query_turn_evidence(db_path, "sess-cargo")
        assert ev.tests.suite_green is None
        assert check_claims([{"type": "tests_green"}], ev)[0]["status"] == "unverifiable"

    def test_bounded_row_does_not_suppress_a_definitive_red(self, db_path):
        """A real failure count outranks doubt about what was cut.

        Regression: putting the reliability gate ahead of the red computation
        downgraded `unbacked` to `unverifiable` and emitted a detail claiming "no
        failure count" while failed=12 was parsed. Nothing in an elided middle can
        turn a red suite green, so the red must win.
        """
        _insert_session(db_path, "sess-red-wins")
        _insert_obs(
            db_path,
            "sess-red-wins",
            "Bash",
            "Ran: pytest -q\nOutput: 900 passed\n… [5000 chars elided] …\ntail",
        )
        _insert_obs(
            db_path,
            "sess-red-wins",
            "Bash",
            "Ran: pytest -q\nOutput: 12 failed, 30 passed in 3s",
        )
        ev = query_turn_evidence(db_path, "sess-red-wins")
        assert ev.tests.failed == 12
        assert ev.tests.suite_green is False
        assert check_claims([{"type": "tests_green"}], ev)[0]["status"] == "unbacked"

    def test_multiline_command_does_not_leak_into_output(self, db_path):
        """A heredoc command must not be parsed as if it were tool output.

        Splitting on the first newline left the command's remaining lines inside
        output_text, which inflated the length used to detect legacy truncation
        (measured thousands of chars against a true output of tens) and let a
        shell comment supply a test result with no test run at all.
        """
        from techne.hooks.post_tool_use import summarize

        command = 'gh pr comment 1 --body "$(cat <<EOF\nreview notes\n# expect 0 failed\nEOF\n)"'
        content = summarize("Bash", {"command": command}, "done")

        _insert_session(db_path, "sess-heredoc")
        _insert_obs(db_path, "sess-heredoc", "Bash", content)
        ev = query_turn_evidence(db_path, "sess-heredoc")
        assert ev.tests.failed is None, "a shell comment must not supply a failure count"
        assert ev.tests.suite_green is None
        # Pin the boundary directly. Asserting `totals_unreliable is False` here
        # would pass trivially, since no test-output row exists to evaluate.
        from techne.core.evidence import _bash_output

        assert _bash_output(content) == "done"

    def test_row_without_a_passed_count_still_raises_the_flag(self, db_path):
        """A bounded row with only "0 errors" must not stay silent.

        `make check` prints mypy's "Found 0 errors" before running pytest, so when
        both pytest totals land in the dropped middle the row parses as
        p=None, f=None, e=0. Returning early on `p is None` let that row say
        nothing, and a small green run elsewhere in the window then certified the
        whole thing green.
        """
        _insert_session(db_path, "sess-mypy")
        _insert_obs(
            db_path,
            "sess-mypy",
            "Bash",
            "Ran: make check\nOutput: mypy: Found 0 errors in 42 files"
            "\n… [6000 chars elided] …\ndone",
        )
        _insert_obs(
            db_path,
            "sess-mypy",
            "Bash",
            "Ran: pytest tests/unit -q\nOutput: 8 passed in 1.1s",
        )
        ev = query_turn_evidence(db_path, "sess-mypy")
        assert ev.tests.totals_unreliable is True
        assert ev.tests.suite_green is None
        assert check_claims([{"type": "tests_green"}], ev)[0]["status"] == "unverifiable"

    def test_ambiguous_output_boundary_parses_no_totals(self, db_path):
        """A command containing the output marker makes the boundary unknowable.

        Reachable by ordinary habit, not just adversarially: a commit message
        with a line reading "Output: 2206 passed, 17 skipped" made the gate report
        a green suite for a `git commit` that ran no tests. Neither split
        direction is safe, so the gate declines rather than picks one.
        """
        _insert_session(db_path, "sess-ambig")
        message = "git commit -F - <<EOF\nfix: thing\n\nOutput: 2206 passed, 17 skipped\nEOF"
        _insert_obs(
            db_path,
            "sess-ambig",
            "Bash",
            f"Ran: {message}\nOutput: [main deadbee] fix: thing",
        )
        ev = query_turn_evidence(db_path, "sess-ambig")
        assert ev.tests.passed is None, "a commit message must not supply a test count"
        assert ev.tests.suite_green is None
        # The commit itself is still evidence — only the test totals are refused.
        assert "deadbee" in ev.commits

    def test_row_without_output_prefix_keeps_its_commit(self, db_path):
        """Free-form Bash rows must not lose their SHA.

        techne_observe writes free-form content under tool_name="Bash", so
        narrowing SHA extraction to the output half turned a real commit from
        `backed` into `unbacked`. A false accusation is the one direction this
        gate must never take (techne#105).
        """
        _insert_session(db_path, "sess-noprefix")
        _insert_obs(
            db_path, "sess-noprefix", "Bash", "Bash: git commit\n[main abc1234] some commit"
        )
        ev = query_turn_evidence(db_path, "sess-noprefix")
        assert "abc1234" in ev.commits
        assert check_claims([{"type": "commit"}], ev)[0]["status"] == "backed"

    def test_non_runner_output_does_not_certify_green(self, db_path):
        """Count-shaped text from a non-test command is not test evidence.

        techne#440. Our commit convention puts a suite count in nearly every
        message, so any `git log`/`git show` in a session echoes "N passed" and
        certified a green suite with no test run. 8 live ledger rows did this.
        """
        _insert_session(db_path, "sess-gitlog")
        _insert_obs(
            db_path,
            "sess-gitlog",
            "Bash",
            "Ran: git log -1\nOutput: commit 93db80e\n\n    Full suite: 2209 passed, 17 skipped.\n",
        )
        ev = query_turn_evidence(db_path, "sess-gitlog")
        assert ev.tests.passed is None
        assert ev.tests.suite_green is None
        assert check_claims([{"type": "tests_green"}], ev)[0]["status"] == "unverifiable"

    def test_linter_error_count_does_not_make_the_suite_red(self, db_path):
        """A linter's "Found N errors" must not turn a green window red.

        The false-ACCUSATION half of techne#440 — 29 live rows would do this, and
        per techne#105 that is the direction that matters most.
        """
        _insert_session(db_path, "sess-ruff")
        _insert_obs(db_path, "sess-ruff", "Bash", "Ran: pytest -q\nOutput: 50 passed in 2s")
        _insert_obs(db_path, "sess-ruff", "Bash", "Ran: ruff check .\nOutput: Found 181 errors.")
        ev = query_turn_evidence(db_path, "sess-ruff")
        assert ev.tests.errors is None, "a linter must not contribute an error count"
        assert ev.tests.suite_green is True
        assert check_claims([{"type": "tests_green"}], ev)[0]["status"] == "backed"

    def test_commit_naming_a_runner_still_yields_its_sha(self, db_path):
        """A commit whose message names a runner is still commit evidence.

        Renamed from `..._is_not_a_test_run`, which claimed to guard the
        adjacent-pair rule and did not: `-m "fix the pytest flake"` splits to the
        token `"fix` (quote included), which can never equal `pytest`, so it
        passed under a loose scan AND under deletion of the VCS exclusion. What it
        genuinely covers is that refusing test totals does not cost the SHA.
        """
        _insert_session(db_path, "sess-quoted")
        _insert_obs(
            db_path,
            "sess-quoted",
            "Bash",
            'Ran: git commit -m "fix the pytest flake"\nOutput: [main abc1234] 3 passed',
        )
        ev = query_turn_evidence(db_path, "sess-quoted")
        assert ev.tests.passed is None
        assert "abc1234" in ev.commits, "the commit must still be evidence"

    def test_loose_runner_token_outside_a_dash_m_pair_is_not_a_test_run(self, db_path):
        """A runner NAME anywhere in a non-VCS command is not a test run.

        Isolates the adjacent-pair rule specifically. The `git commit -m "...
        pytest ..."` case is caught by the VCS-head exclusion even under a loose
        token scan, so it does not pin this; a bare `echo` naming a runner does.
        Realistic because our probe commands routinely echo their own intent.
        """
        _insert_session(db_path, "sess-loose")
        _insert_obs(
            db_path,
            "sess-loose",
            "Bash",
            'Ran: echo "about to run pytest against the worktree"\nOutput: 5 passed',
        )
        ev = query_turn_evidence(db_path, "sess-loose")
        assert ev.tests.passed is None
        assert ev.tests.suite_green is None

    def test_bare_dash_m_message_naming_a_runner_is_not_a_test_run(self):
        """`git commit -m pytest` — the case the quoted test could never reach.

        Pins `_DASH_M_IS_A_MESSAGE`, which was load-bearing but asserted by
        nothing: emptying the set flips each of these from False to True.
        """
        from techne.core.evidence import _command_runs_tests

        assert _command_runs_tests("git commit -m pytest") is False
        assert _command_runs_tests("git commit -m tox") is False
        assert _command_runs_tests("jj describe -m nox") is False
        assert _command_runs_tests("hub commit -m pytest") is False

    def test_heredoc_body_is_not_a_command(self):
        """A runner name at the start of a heredoc BODY line is not an invocation.

        Quote-awareness does not cover this: the splitter breaks on newlines and a
        heredoc body is unquoted, so each body line became a segment and its first
        word became a command head. Writing a script that contains a `pytest` line
        would have read as running one.
        """
        from techne.core.evidence import _command_runs_tests

        writes_a_script = "cat > /tmp/probe.py <<'PY'\npytest tests/ -q\nPY"
        assert _command_runs_tests(writes_a_script) is False
        # The same command really running the suite afterwards still counts.
        assert _command_runs_tests(writes_a_script + "\npytest -q") is True

    def test_non_python_runners_are_recognised(self):
        """The non-Python half of the allowlist, which no test asserted.

        A mutation harness showed that dropping every subcommand runner except
        `make`, or every plain head except `pytest`, passed the whole suite — so
        the Go/Rust/JS/JVM/Ruby paths were unpinned.
        """
        from techne.core.evidence import _command_runs_tests

        for command in (
            "go test ./...",
            "cargo test --workspace",
            "npm test",
            "npm run test",
            "yarn test",
            "pnpm test",
            "bun test",
            "mvn -q test",
            "gradle test",
            "dotnet test",
            "bundle exec rspec",
            "rake spec",
            "vitest run",
            "jest --ci",
        ):
            assert _command_runs_tests(command) is True, command

    def test_wrappers_and_modules_beyond_pytest_are_recognised(self):
        """Wrapper heads and non-pytest `-m` modules, also unpinned.

        Emptying `_TEST_RUNNER_WRAPPERS`, or reducing `_TEST_MODULES` to just
        `pytest`, passed the whole suite — so `uv run pytest` and `python -m tox`
        were carried by no assertion.
        """
        from techne.core.evidence import _command_runs_tests

        for command in (
            "uv run pytest -q",
            "poetry run pytest",
            "npx jest",
            "pipx run pytest",
            "python -m unittest discover",
            "python -m tox",
            '"$VENV" -m nox -s tests',
        ):
            assert _command_runs_tests(command) is True, command

    def test_decorated_runner_spellings_are_recognised(self):
        """Flags, goals, directories and toolchains must not hide the subcommand.

        A stricter position rule was tried and reverted. Filtering flags keeps
        their VALUES, so every one of these stopped being recognised — and in this
        gate an unrecognised row raises no unreliable flag, letting a small intact
        run elsewhere certify green. Starvation here IS a false green (techne#438),
        which is why the scan is deliberately loose up to a `--`.
        """
        from techne.core.evidence import _command_runs_tests

        for command in (
            "make -C build test",
            "make -j 4 test",
            "make V=1 test",
            "mvn clean test",
            "gradle clean test",
            "cargo +nightly test",
            "npm --prefix web test",
            "yarn --cwd web test",
            "dotnet test -v n",
        ):
            assert _command_runs_tests(command) is True, command

    def test_arguments_after_a_separator_are_not_subcommands(self):
        """`--` ends the scan: `npm run build -- test` passes `test` onward."""
        from techne.core.evidence import _command_runs_tests

        assert _command_runs_tests("npm run build -- test") is False
        assert _command_runs_tests("npm run test") is True

    def test_heredoc_edge_cases(self):
        """Delimiter spellings and terminator rules, each a real leak or over-strip.

        The first two produced `backed` on a command that runs nothing; the last
        three over-stripped and swallowed a real run below the body.
        """
        from techne.core.evidence import _command_runs_tests

        # Body stripped -> the runner line inside it is not an invocation.
        assert _command_runs_tests("cat > f <<\\PY\npytest x\nPY") is False
        assert _command_runs_tests("cat > f <<'PY-1'\npytest x\nPY-1") is False
        assert _command_runs_tests("cat f <<'A' g <<'B'\npytest x\nA\npytest y\nB") is False
        assert _command_runs_tests("cat > f <<'PY'\npytest x") is False, "unterminated"
        # NOT stripped -> a real run below the construct still counts.
        assert _command_runs_tests("grep x <<<word\npytest -q") is True, "herestring"
        assert _command_runs_tests('echo "a << b"\npytest -q') is True, "quoted <<"
        # `<<-` must be recognised as an opener at all: without it the tag `-PY`
        # never matches, nothing is stripped, and the body's `pytest` line escapes.
        # Asserted WITHOUT a trailing run, because with one both versions return
        # True and the assertion would pin nothing.
        assert _command_runs_tests("cat > f <<-PY\npytest x\n\tPY") is False
        # Quote state must carry ACROSS newlines. A multi-line quoted message
        # followed by a heredoc: with a per-line mask the second line starts
        # clean, reads the message's CLOSING quote as an opening one, hides the
        # real `<<'SH'` opener, and the body's runner line escapes.
        assert (
            _command_runs_tests(
                'git commit -m "fix: a message\n\nspanning lines." '
                "&& cat > /tmp/probe.sh <<'SH'\npytest -q\nSH"
            )
            is False
        )
        assert _command_runs_tests("cat > f <<-PY\npytest x\n\tPY\npytest -q") is True
        # An indented tag inside a PLAIN `<<` body is body text, not the end. A
        # lenient `.strip()` terminator ends the body here and lets the `pytest`
        # line below escape as a command — this asserts False specifically because
        # True is what the lenient version returns.
        assert _command_runs_tests("cat > f <<PY\n  PY\npytest x\nPY") is False
        # ...and a genuine run after the body still counts.
        assert _command_runs_tests("cat > f <<PY\n  PY\npytest x\nPY\npytest -q") is True

    def test_interpreter_behind_a_variable_still_counts_as_a_runner(self, db_path):
        """`"$VENV" -m pytest` is the dominant real-world shape and must match.

        Head-only matching missed it — the head is a variable reference, not a
        recognisable interpreter. Measured against the live ledger, requiring a
        literal head refused 103 rows; the `-m <module>` pair rule cut that to 48,
        and the remainder are genuinely non-runner commands.
        """
        _insert_session(db_path, "sess-var")
        _insert_obs(
            db_path,
            "sess-var",
            "Bash",
            'Ran: cd "$WT" && PYTHONPATH="$WT" "$VENV" -m pytest tests/ -q\n'
            "Output: 25 passed in 1.2s",
        )
        ev = query_turn_evidence(db_path, "sess-var")
        assert ev.tests.passed == 25
        assert ev.tests.suite_green is True

    def test_make_check_counts_as_a_test_run(self, db_path):
        """`make check` stays on the allowlist, and the reason is a regression.

        Excluding it looked safer, but it silently reintroduced techne#438's
        cross-row false green: a `make check` that runs the suite stopped being
        recognised, so its bounded output raised no flag and a small intact run
        elsewhere certified the window green.
        """
        _insert_session(db_path, "sess-make")
        _insert_obs(
            db_path,
            "sess-make",
            "Bash",
            "Ran: make check\nOutput: mypy: Found 0 errors in 42 files"
            "\n… [6000 chars elided] …\ndone",
        )
        _insert_obs(
            db_path,
            "sess-make",
            "Bash",
            "Ran: pytest tests/unit -q\nOutput: 8 passed in 1.1s",
        )
        ev = query_turn_evidence(db_path, "sess-make")
        assert ev.tests.totals_unreliable is True
        assert ev.tests.suite_green is None

    def test_quotes_in_a_heredoc_body_do_not_leak_into_later_commands(self):
        """A stray apostrophe in a script being written is not shell quoting.

        Heredoc body text is not subject to shell quoting at all. Masking the
        whole command up front could not express that — the mask ran over text the
        segment splitter never sees — so `# don't remove this` opened a quote that
        never closed, hid the NEXT heredoc's opener, and let its body's `pytest`
        line escape as a command head. Real bash runs nothing here; both heredocs
        write files.
        """
        from techne.core.evidence import _command_runs_tests

        assert (
            _command_runs_tests(
                "cat > probe.py <<'PY'\n# don't remove this\nPY\ncat > run.sh <<'SH'\npytest -q\nSH"
            )
            is False
        )

    def test_property_no_heredoc_body_line_is_ever_a_command(self):
        """Generated commands: a runner inside a body must never be an invocation.

        This is the invariant `_strip_heredoc_bodies` exists to guarantee, asserted
        over a generated product rather than over shapes someone thought of. Four
        rounds of review found four defects in this code and each was a shape
        nobody had enumerated — including two introduced by the fix for the
        previous one. The grammar is deliberately built from what actually breaks
        it: delimiter spellings, and body text containing unbalanced quotes.

        Deterministic (itertools product, no RNG, no new dependency) so a failure
        reproduces exactly.
        """
        import itertools

        from techne.core.evidence import _command_runs_tests

        openers = ["<<'PY'", '<<"PY"', "<<PY", "<<-PY", "<<\\PY", "<<'PY-1'"]
        terminators = {
            "<<'PY'": "PY",
            '<<"PY"': "PY",
            "<<PY": "PY",
            "<<-PY": "\tPY",
            "<<\\PY": "PY",
            "<<'PY-1'": "PY-1",
        }
        bodies = [
            "# don't remove this",  # unbalanced single quote
            'x = "oops',  # unbalanced double quote
            "it's fine",
            "echo 'balanced'",
            "plain text",
        ]
        runner_lines = ["pytest -q", "go test ./...", "npm test"]

        # A multi-line quoted argument before the heredocs. Without this the
        # grammar misses the round-3 regression entirely: verified by emulating
        # each prior implementation, the body-quote shapes alone catch round 4's
        # but not round 3's.
        prefixes = [
            "",
            'git commit -m "fix: a message\n\nspanning lines." && ',
            # PR 1353 review, Critical: an escaped quote of odd parity. The splitter
            # learned escapes and the mask did not, so the mask hid the `<<` while
            # the splitter split the body into commands. Both now share shell_lex.
            'git commit -m "bump 5\\" size"\n',
            "echo it\\'s\n",
            "echo $'it\\'s'\n",
            "cd . # don't\n",
        ]

        for prefix, opener, body, runner in itertools.product(
            prefixes, openers, bodies, runner_lines
        ):
            end = terminators[opener]
            # BOTH orderings, because each catches a different regression and
            # neither catches both. With the runner in the SECOND body, a body
            # quote leaking forward exposes it (round 4). With the runner in the
            # FIRST body, a prefix that hides that opener exposes it (round 3) —
            # verified by emulating each prior implementation.
            for command in (
                f"{prefix}cat > a.py {opener}\n{body}\n{end}\ncat > b.sh {opener}\n{runner}\n{end}",
                f"{prefix}cat > a.sh {opener}\n{runner}\n{end}\ncat > b.py {opener}\n{body}\n{end}",
            ):
                assert _command_runs_tests(command) is False, command

    def test_property_a_c_wrapped_runner_is_always_seen(self):
        """Generated: wrapping a runner in `<shell> -c` must never hide it.

        The #445 half of the grammar. Starvation in this gate is a FALSE GREEN,
        not a harmless miss — an unrecognised row raises no `totals_unreliable`
        flag, so an intact run elsewhere in the window certifies the lot.
        """
        import itertools

        from techne.core.evidence import _command_runs_tests

        shells = ["bash", "sh", "zsh", "dash", "ksh", "/bin/bash", "/usr/bin/env"]
        # `-cx` pins that c need not be LAST in the cluster; the long options pin
        # that `--norc` is not a `-c` and that `-o`'s value is not a positional.
        # Each spelling here pinned a real starvation bug found in review. The
        # clustered value options (-euo) are why the rule is LETTER-based: only
        # the last letter of a cluster may consume the next token, so a set of
        # whole tokens could never see `-euo pipefail`.
        flags = [
            "-c",
            "-lc",
            "-ec",
            "-cx",
            "--norc -c",
            "-o pipefail -c",
            "-eu -o pipefail -c",
            "-euo pipefail -c",
            "-euxo pipefail -c",
            "-O globstar -c",
            "+O globstar -c",
            "+x -c",
            # Value letter NOT last in the cluster, with `c` in the same cluster.
            # Both shells on this box run these; the last-letter-only rule
            # returned `pipefail` as the script and starved. Verified against the
            # real shell, not against the man page, which says otherwise.
            "-oc pipefail",
            "-occ pipefail",
        ]
        # Transparent prefixes: `env bash -c` really does run the script, unlike
        # `/usr/bin/env -c`, which is not a shell invocation at all.
        # `env -iu FOO` is the CLUSTERED spelling of `env -i -u FOO`. It is in
        # the grammar rather than in one example because the unclustered form
        # passed while the clustered one starved (r3 finding 1) — the same shape
        # as `-euo pipefail` above, in env's option loop instead of the shell's.
        # `-uCC` / `-iuCC` are the ATTACHED-value spellings: getopt gives the
        # first value letter the rest of the token, so they consume NOTHING
        # extra. The last-letter rule read the trailing C as a value letter,
        # skipped two tokens and ate `bash` (r4 finding 1). `-P` is BSD's
        # utilpath, which does take the next token.
        prefixes = [
            "", "env ", "env -u FOO ", "env -iu FOO ", "env -iC /tmp ",
            "env -uCC ", "env -iuCC ", "env -P /usr/local/bin ", "CI=1 ",
        ]
        quotes = ["'", '"']
        runners = ["pytest -q", "go test ./...", "npm test", "python -m pytest"]

        for prefix, shell, flag, q, runner in itertools.product(
            prefixes, shells, flags, quotes, runners
        ):
            if q in runner:
                continue
            command = f"{prefix}{shell} {flag} {q}{runner}{q}"
            # `/usr/bin/env -c '<script>'` is NOT a shell invocation — env has no
            # -c. Note this is the only shape the grammar generates for env as a
            # HEAD; env as a transparent PREFIX is covered by `prefixes` above,
            # and those must expand.
            expected = shell != "/usr/bin/env"
            assert _command_runs_tests(command) is expected, command

        # Shapes the grammar could not previously generate, each verified to work
        # and therefore worth pinning rather than holding by luck (#521 r2 (c)).
        for command in (
            "bash -c 'sh -c \"pytest -q\"'",  # nesting; pins _MAX_SHELL_C_DEPTH
            "bash -c 'cd proj && pytest -q'",  # inner segmentation
            "bash -c 'echo start; pytest -q'",
            "bash -co pipefail 'pytest -q'",  # value letter INSIDE the c cluster
        ):
            assert _command_runs_tests(command) is True, command

    def test_property_a_c_wrapper_never_invents_a_runner(self):
        """The mirror invariant, and the reason `bash` is NOT in the wrapper
        allowlist: a `-c` script that merely MENTIONS a runner, or prints
        count-shaped output, must stay False. Adding bash to
        `_TEST_RUNNER_WRAPPERS` would have matched all of these — #440 through
        the front door.
        """
        import itertools

        from techne.core.evidence import _command_runs_tests

        shells = ["bash", "sh", "zsh", "dash", "ksh"]
        # WHAT THIS GUARDS: a `-c` script that merely MENTIONS a runner is not a
        # test run. It does NOT guard the `_TEST_RUNNER_WRAPPERS` exclusion —
        # verified by adding `bash` to that set, under which these cases still
        # pass, because `_expand_shell_c` replaces the `bash -c` segment before
        # the wrapper rule sees a `bash` head. That exclusion is pinned by
        # `test_bash_is_not_a_test_runner_wrapper`, which DOES fail under the
        # same mutation.
        payloads = [
            "echo pytest passed",  # bare `pytest` token in rest
            "echo 12 passed",
            "git commit -m fix-the-pytest-flake",  # bare, unquoted
            "echo 'pytest is great'",
            "cat pytest.ini",
            "ls pytest",
        ]
        for shell, payload in itertools.product(shells, payloads):
            command = f"{shell} -c '{payload}'"
            assert _command_runs_tests(command) is False, command

    def test_bash_is_not_a_test_runner_wrapper(self):
        """The #440 exclusion, pinned where it is OBSERVABLE.

        `_TEST_RUNNER_WRAPPERS` matches a head whose ARGUMENT is a runner. With
        `bash` in that set, `bash -x pytest.log` would read as a test run. The
        `-c` shapes cannot pin this — `_expand_shell_c` consumes them first — so
        it takes a bash invocation with no `-c` AND a BARE runner token, because
        the wrapper rule scans rest-tokens for exactly that.

        The payload used to be `bash -x pytest.log`, which is False in both
        worlds (`pytest.log` is not a bare head) — so the only thing that failed
        under the mutation was the set-membership assertion, a restatement of the
        implementation that catches a literal edit and nothing arriving at the
        same place another way. `bash -x pytest` discriminates: False at
        baseline, True with `bash` in the set. (Prudence, PR #521 — she
        teeth-checked by neutralising ONLY the membership asserts, and all 91
        tests still passed.)
        """
        from techne.core.evidence import _TEST_RUNNER_WRAPPERS, _command_runs_tests

        assert "bash" not in _TEST_RUNNER_WRAPPERS
        assert "sh" not in _TEST_RUNNER_WRAPPERS
        assert _command_runs_tests("bash -x pytest") is False
        assert _command_runs_tests("sh -x pytest") is False

    def test_the_capture_side_is_untouched_by_the_gate_expansion(self):
        """#445's load-bearing constraint. `_split_bash_segments` is SHARED with
        the capture-time noise filter, and the two want opposite things: the gate
        needs to see INTO `bash -c`, while the noise filter must keep heading on
        `bash` — expanding there would head on `ls`, return True, and DROP a row
        it currently captures, against its documented fail-safe.

        So the expansion lives gate-side and this pins that the shared splitter
        kept its semantics.
        """
        from techne.hooks.post_tool_use import _bash_is_readonly_noise, _split_bash_segments

        assert _split_bash_segments("bash -c 'ls -la'") == ["bash -c 'ls -la'"]
        assert _bash_is_readonly_noise("bash -c 'ls -la'") is False, "must still CAPTURE"
        assert _bash_is_readonly_noise("ls -la") is True

    def test_unparseable_c_segments_are_left_alone(self):
        """A segment shlex cannot parse (heredoc bodies routinely leave unbalanced
        quotes) must be passed through, not guessed at."""
        from techne.core.evidence import _shell_c_script

        assert _shell_c_script("bash -c 'unterminated") is None
        assert _shell_c_script("bash -c") is None  # -c with no script
        assert _shell_c_script("bash script.sh") is None  # positional, not -c
        assert _shell_c_script("pytest -q") is None

    def test_no_test_output_returns_none(self, db_path):
        _insert_session(db_path, "sess-nt")
        _insert_obs(db_path, "sess-nt", "Bash", "Ran: ls\nOutput: foo.py  bar.py")
        ev = query_turn_evidence(db_path, "sess-nt")
        assert ev.tests.suite_green is None
        assert ev.tests.passed is None

    def test_error_count_makes_suite_red(self, db_path):
        _insert_session(db_path, "sess-te")
        _insert_obs(
            db_path,
            "sess-te",
            "Bash",
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
            totals_unreliable=kwargs.pop("totals_unreliable", False),
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

    def test_completion_unverifiable_when_empty_and_bash_off(self):
        """A pure-Bash turn under capture_bash=off leaves NO trace (count 0) — completion
        is unverifiable (absent), not unbacked (disproven). techne#105 no-false-flag."""
        ev = self._ev(any_tool_call=False, artifacts=[], observation_count=0, bash_captured=False)
        verdicts = check_claims([{"type": "completion"}], ev)
        assert verdicts[0]["status"] == "unverifiable"

    def test_completion_unverifiable_when_recorded_but_no_deed_and_bash_off(self):
        """Records exist (e.g. UserPrompts) but no deed, Bash off → UNVERIFIABLE, not
        unbacked: the real work could be uncaptured Bash, so absence isn't disproof.
        (techne#105 honesty; the no-deed+Bash-off verdict is unverifiable regardless of
        whether the recorded rows were non-deeds or passive tools.)"""
        ev = self._ev(any_tool_call=False, artifacts=[], observation_count=3, bash_captured=False)
        verdicts = check_claims([{"type": "completion"}], ev)
        assert verdicts[0]["status"] == "unverifiable"

    def test_completion_unverifiable_when_only_passive_tools_and_bash_off(self):
        """The regression Prudence caught: a captured passive tool (Read) in an otherwise
        Bash-off turn must NOT flip an honest agent's completion claim to 'unbacked'. With
        no deed and Bash off, the deeds could be uncaptured Bash → unverifiable."""
        ev = self._ev(
            any_tool_call=True,
            active_tool_call=False,
            artifacts=[],
            observation_count=1,
            bash_captured=False,
        )
        verdicts = check_claims([{"type": "completion"}], ev)
        assert verdicts[0]["status"] == "unverifiable"

    def test_completion_backed_when_active_tool_called(self):
        ev = self._ev(
            any_tool_call=True, active_tool_call=True, artifacts=[], commits=[], actions=[]
        )
        verdicts = check_claims([{"type": "completion"}], ev)
        assert verdicts[0]["status"] == "backed"

    def test_completion_unbacked_when_only_passive_tools_and_bash_on(self):
        """Bash ON + only passive inspection (Read/Grep/…): sets any_tool_call but NOT
        active_tool_call → unbacked. Looking is not doing, and with Bash captured a real
        command WOULD have been recorded, so no-deed is genuine (techne#105).
        (Contrast the Bash-off companion above, which is unverifiable.)"""
        ev = self._ev(
            any_tool_call=True, active_tool_call=False, artifacts=[], commits=[], actions=[]
        )  # bash_captured defaults True
        verdicts = check_claims([{"type": "completion"}], ev)
        assert verdicts[0]["status"] == "unbacked"

    def test_completion_backed_by_artifacts_without_active_flag(self):
        """Artifacts (Write/Edit) back completion on their own, even if the active flag
        weren't set — a written file is a deed."""
        ev = self._ev(any_tool_call=True, active_tool_call=False, artifacts=["/x/y.py"])
        verdicts = check_claims([{"type": "completion"}], ev)
        assert verdicts[0]["status"] == "backed"

    def test_completion_unbacked_when_no_activity(self):
        ev = self._ev(
            any_tool_call=False, active_tool_call=False, artifacts=[], commits=[], actions=[]
        )
        verdicts = check_claims([{"type": "completion"}], ev)
        assert verdicts[0]["status"] == "unbacked"

    def test_did_work_alias(self):
        ev = self._ev(
            any_tool_call=True, active_tool_call=True, artifacts=[], commits=[], actions=[]
        )
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
        ev = self._ev(
            any_tool_call=True,
            artifacts=[],
            commits=[],
            actions=[],
            suite_green=True,
            passed=5,
            failed=0,
        )
        verdicts = check_claims([{"type": "tests_green"}], ev)
        assert verdicts[0]["status"] == "backed"

    def test_tests_green_unbacked(self):
        ev = self._ev(
            any_tool_call=True,
            artifacts=[],
            commits=[],
            actions=[],
            suite_green=False,
            passed=3,
            failed=1,
        )
        verdicts = check_claims([{"type": "tests_green"}], ev)
        assert verdicts[0]["status"] == "unbacked"

    def test_tests_green_unverifiable(self):
        ev = self._ev(any_tool_call=True, artifacts=[], commits=[], actions=[], suite_green=None)
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
        ev = self._ev(any_tool_call=False, artifacts=[], commits=[], actions=[], suite_green=None)
        claims = [
            {"type": "completion"},  # unbacked
            {"type": "tests_green"},  # unverifiable
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
            db_path,
            "sess-mcp",
            "Bash",
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
        config.hooks.capture_bash = False  # the case under test; the default is ON since #1020
        _insert_session(db_path, "sess-ub")
        # A recorded-but-no-deed turn (a UserPrompt, no Write/Edit/Bash) with
        # capture_bash off. Under techne#105 honesty:
        #   - completion → UNVERIFIABLE: no deed, but a write/command via uncaptured
        #     Bash would leave no trace, so absence isn't disproof. (Prudence-caught:
        #     a recorded passive/non-deed row must NOT flip this to a false 'unbacked'.)
        #   - commit     → UNVERIFIABLE: absent + Bash not recorded.
        #   - wrote_file(absent) → UNBACKED: artifacts are captured regardless of Bash,
        #     so a missing path is a genuine, verifiable no.
        # unbacked_count is therefore exactly 1 — the counter isolates the genuine
        # unbacked from the two honest unverifiables (no false flags).
        _insert_obs(db_path, "sess-ub", "UserPrompt", "fix the bug")

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
                            {"type": "wrote_file", "path": "never_written.py"},
                        ],
                    },
                },
            },
            config,
        )
        import json

        data = json.loads(response["result"]["content"][0]["text"])
        by_type = {v["claim"]["type"]: v["status"] for v in data["verdicts"]}
        assert by_type["completion"] == "unverifiable"
        assert by_type["commit"] == "unverifiable"
        assert by_type["wrote_file"] == "unbacked"
        assert data["unbacked_count"] == 1

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
        _insert_obs(db, "sess-cli", "Bash", "Ran: pytest\nOutput: 7 passed in 1.2s")

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
        _insert_obs(
            db,
            "sess-cli2",
            "Write",
            "Wrote: /tmp/old.py\nContent: ...",
            timestamp="2026-01-01T00:00:00",
        )
        _insert_obs(
            db,
            "sess-cli2",
            "Write",
            "Wrote: /tmp/new.py\nContent: ...",
            timestamp="2026-06-01T00:00:00",
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["evidence", "sess-cli2", "--since", "2026-03-01T00:00:00"],
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


# ---- redaction shrinks a legacy row below the length threshold (techne#441) ----


def test_redacted_legacy_row_is_treated_as_bounded():
    """The bug: redaction runs BEFORE storage and CHANGES LENGTH.

    A pre-#437 row was cut at exactly the budget with no marker, so the only
    signal it is bounded is its length. `redact_credentials` then replaced a
    long secret with a short `<redacted:tag>` token, so the stored text lands
    BELOW the budget and reads as a complete record — the bad direction, since
    an unflagged row can carry a green verdict.
    """
    from techne.core.evidence import _output_was_bounded
    from techne.core.redaction import redact_credentials
    from techne.hooks.post_tool_use import _PREVIEW_MAX_CHARS

    secret = "sk-ant-" + "a" * 120
    legacy_cut = ("x" * (_PREVIEW_MAX_CHARS - len(secret))) + secret
    assert len(legacy_cut) == _PREVIEW_MAX_CHARS, "precondition: cut at the budget"

    stored = redact_credentials(legacy_cut)
    assert len(stored) < _PREVIEW_MAX_CHARS, (
        "precondition: redaction must SHRINK this row below the budget, or the "
        "test proves nothing about the length test"
    )

    assert _output_was_bounded(stored) is True


def test_an_unredacted_short_row_is_still_authoritative():
    """The case that must NOT regress.

    Flagging every short row as bounded would make the gate useless — it would
    never certify anything. Only redaction makes the length unknowable.
    """
    from techne.core.evidence import _output_was_bounded

    assert _output_was_bounded("3 passed in 0.1s") is False


def test_the_marker_matches_what_the_redactor_actually_writes():
    """Binds the reader's pattern to the writer's format, through REAL output.

    SCOPE, stated because the first version of this test overstated it: the
    per-tag loop below builds `f"<redacted:{tag}>"`, which IS the format string
    hardcoded a second time — so it checks the marker knows every TAG, and
    proves nothing about the format. The binding to the format comes only from
    the live redactions, so those cover several distinct patterns rather than
    one. Injecting format drift is caught by the live half, not the loop.
    """
    from techne.core.redaction import _PATTERNS, REDACTION_MARKER, redact_credentials

    # Real credential-SHAPED inputs, run through the actual redactor. Not real
    # secrets: each is a synthetic string matching one pattern's shape.
    live_samples = [
        "sk-ant-" + "a" * 30,
        "ghp_" + "b" * 30,
        "sk-proj-" + "c" * 30,
    ]
    for sample in live_samples:
        produced = redact_credentials(f"prefix {sample} suffix")
        assert produced != f"prefix {sample} suffix", f"redactor did not match {sample[:12]!r}"
        assert REDACTION_MARKER.search(produced), produced

    # Every tag the writer can interpolate is recognised. Format-independent by
    # construction, which is exactly its limit.
    for _pattern_src, tag in _PATTERNS:
        assert REDACTION_MARKER.search(f"<redacted:{tag}>"), f"marker misses tag {tag!r}"


# ---- techne#541: the length test's pressure, probed rather than narrated ------


class TestProbeLengthTestPressure:
    """`probe_length_test_pressure` replaces a prose predicate that rotted.

    The value of these tests is NOT the numbers — the live ledger's counts change
    with every run. It is that the probe's clauses stay bolted to the ones the
    gate actually applies. Every error during #441's five review rounds came from
    re-implementing a step the gate already owns; a probe that drifts the same way
    is a comment with a return type.
    """

    @staticmethod
    def _bash(db_path, session, command, output):
        from techne.core.evidence import _BASH_OUTPUT_MARKER

        _insert_obs(db_path, session, "Bash", f"Ran: {command}{_BASH_OUTPUT_MARKER}{output}")

    def test_empty_ledger_is_all_zeroes_not_a_crash(self, db_path):
        from techne.core.evidence import probe_length_test_pressure

        assert set(probe_length_test_pressure(db_path).values()) == {0}

    def test_counts_the_conjunction_the_gate_actually_applies(self, db_path):
        from techne.core.evidence import probe_length_test_pressure

        _insert_session(db_path, "s")
        # runner + parseable + failure-free + short + unmarked → the ordinary
        # green run whose verdict the redaction clause ALONE decides.
        self._bash(db_path, "s", "pytest -q", "12 passed in 0.4s")
        # runner + parseable but FAILING → excluded by the failure-free clause.
        self._bash(db_path, "s", "pytest -q", "1 failed, 3 passed in 0.4s")
        # runner but UNPARSEABLE → excluded before the length test is consulted.
        self._bash(db_path, "s", "pytest -q", "collecting ...")
        # not a runner at all, and names one only inside a quoted argument —
        # the substring-vs-command-head distinction (techne#440).
        self._bash(db_path, "s", 'git commit -m "fix the pytest flake"', "12 passed in 0.4s")

        c = probe_length_test_pressure(db_path)
        assert c["bash_rows"] == 4
        assert c["runner_rows"] == 3
        assert c["conjunction_rows"] == 1
        assert c["decided_by_redaction_alone"] == 1

    def test_marker_carrying_runner_rows_ignores_parseability_and_outcome(self, db_path):
        """The watch-observable counts runner rows carrying the marker, PERIOD.

        Scoping it to the conjunction would make it move with the suite's size —
        the stock this docstring argues against watching.
        """
        from techne.core.evidence import probe_length_test_pressure

        _insert_session(db_path, "s")
        self._bash(db_path, "s", "pytest -q", "12 passed, key <redacted:aws-access-key> in 0.4s")
        self._bash(db_path, "s", "pytest -q", "1 failed <redacted:bearer>")  # failing
        self._bash(db_path, "s", "pytest -q", "collecting <redacted:jwt>")  # unparseable
        self._bash(db_path, "s", "ls -la", "<redacted:sk>")  # not a runner

        c = probe_length_test_pressure(db_path)
        assert c["marker_carrying_runner_rows"] == 3
        assert c["runner_rows"] == 3

    def test_flips_require_all_four_clauses(self, db_path):
        """A flip is only a flip when the clauses ABOVE the redaction test did not
        already return True — which is why dropping the length clause inflates it."""
        from techne.core.evidence import probe_length_test_pressure
        from techne.hooks.post_tool_use import _PREVIEW_ELISION_MARK, _PREVIEW_MAX_CHARS

        _insert_session(db_path, "s")
        self._bash(db_path, "s", "pytest -q", "12 passed <redacted:bearer>")  # FLIP
        # already elided → the first clause returns True, so redaction decides nothing
        elided = f"12 passed\n… [4096{_PREVIEW_ELISION_MARK}<redacted:bearer>"
        self._bash(db_path, "s", "pytest -q", elided)
        # at/over budget → the length clause already returned True
        self._bash(db_path, "s", "pytest -q", "x" * _PREVIEW_MAX_CHARS + " <redacted:bearer>")
        self._bash(db_path, "s", "pytest -q", "12 passed in 0.4s")  # no marker

        assert probe_length_test_pressure(db_path)["flips"] == 1

    def test_flips_read_the_output_not_the_whole_row(self, db_path):
        """Measuring the row counts a marker in the COMMAND as a flip. It isn't
        one: the gate only ever reads `_bash_output(content)`."""
        from techne.core.evidence import probe_length_test_pressure

        _insert_session(db_path, "s")
        self._bash(db_path, "s", "pytest -q --token=<redacted:bearer>", "12 passed in 0.4s")
        c = probe_length_test_pressure(db_path)
        assert c["flips"] == 0
        assert c["marker_carrying_runner_rows"] == 0

    def test_ignores_rows_that_are_not_bash_output(self, db_path):
        from techne.core.evidence import probe_length_test_pressure

        _insert_session(db_path, "s")
        _insert_obs(db_path, "s", "Edit", "Edited: a.py\nChange: x → y")
        _insert_obs(db_path, "s", "Bash", "Ran: pytest -q")  # no Output section
        assert probe_length_test_pressure(db_path)["bash_rows"] == 0


# ---------------------------------------------------------------------------
# techne#545 — a test run behind a transparent prefix contributed no evidence
# ---------------------------------------------------------------------------


class TestTransparentPrefixes:
    """The gate saw `pytest -q` and `env bash -c 'pytest -q'` but not `timeout 60
    pytest -q`. Measured on main before the fix, every wrapped form was False.

    THE MISS IS NOT HARMLESS. An unrecognised row is discarded WHOLE, so its failures
    never reach `all_failed` and a smaller intact run elsewhere in the window certifies
    green — the #438 starvation shape, i.e. a false green rather than a missing signal.
    """

    def test_the_wrapped_forms_from_the_issue_are_recognised(self):
        from techne.core.evidence import _command_runs_tests

        for cmd in (
            "pytest -q",
            "env bash -c 'pytest -q'",
            "timeout 60 pytest -q",
            "nice -n 10 pytest -q",
            "nohup pytest -q",
            "stdbuf -o0 pytest -q",
        ):
            assert _command_runs_tests(cmd), f"{cmd!r} runs tests and must be seen"

    def test_prefixes_compose_and_carry_their_option_grammars(self):
        """Each prefix has a DIFFERENT grammar, which is why `_skip_env_prefix` could
        not simply absorb them: `nice -n` takes a value on a letter `env` does not
        know, and `timeout` carries a mandatory POSITIONAL duration before the
        command — without which the duration reads as the command head."""
        from techne.core.evidence import _command_runs_tests

        for cmd in (
            "env FOO=1 nice -n 10 timeout 60 pytest -q",
            "timeout --signal=KILL 5m pytest",
            "stdbuf -o 0 pytest -q",
            "timeout 60 make test",  # the subcommand path, not just a runner head
        ):
            assert _command_runs_tests(cmd), f"{cmd!r} must resolve to the real head"

    def test_a_bare_prefix_is_not_a_test_run(self):
        """`nohup` alone runs nothing. Left where it is, matching `_skip_env_prefix`'s
        rule for bare `env` — otherwise the skipper walks off the end and the caller
        loses the head it actually has."""
        from techne.core.evidence import _command_runs_tests

        # NOTE the UNQUOTED `pytest`. With quotes the token is the six-character
        # string `'pytest'` — `_command_runs_tests` splits on whitespace, not shlex —
        # so the assertion held for a second, accidental reason and would have kept
        # passing if the _DASH_M_IS_A_MESSAGE guard were deleted (Prudence, #737 nit 2).
        for cmd in ("nohup", "timeout 60", "nice", "timeout 60 git commit -m pytest"):
            assert not _command_runs_tests(cmd), f"{cmd!r} does not run tests"


class TestOneGetoptRule:
    """#737 Important. The transparent-prefix skipper carried a SECOND, weaker copy of
    getopt's cluster rule — `t[1] in value_letters and len(t) == 2` — 25 lines above
    the correct one, and it drifted inside the very PR whose docstring argues against
    reimplementing a grammar because "a second copy would drift".

    getopt binds a value to the first VALUE LETTER in a cluster, and consumes the
    following token only when that letter is LAST. Both spellings below are uncommon,
    but a miss here is a false green, not a harmless gap — this file's own standard."""

    def test_a_clustered_flag_before_a_value_letter_is_read_correctly(self):
        from techne.core.evidence import _command_runs_tests

        # `-vs KILL` — `v` is a flag, `s` is the value letter and is LAST, so KILL is
        # consumed. Under the drifted rule the index landed on `60`, not `pytest`.
        assert _command_runs_tests("timeout -vs KILL 60 pytest")
        assert _command_runs_tests("ionice -tc 2 pytest")

    def test_an_attached_value_consumes_only_its_own_token(self):
        from techne.core.evidence import _command_runs_tests

        assert _command_runs_tests("stdbuf -oL pytest")
        assert _command_runs_tests("stdbuf -o L pytest")

    def test_env_still_uses_the_same_rule(self):
        """The helper is shared, so `env`'s own measured cases must not regress:
        `-uCC bash` consumes one token (CC is attached), `-iu CC` consumes two."""
        from techne.hooks.post_tool_use import _skip_transparent_prefixes

        for cmd in ("env -uCC bash -c pytest", "env -iu CC bash -c pytest"):
            tokens = cmd.split()
            assert tokens[_skip_transparent_prefixes(tokens)] == "bash", cmd

    def test_the_helper_matches_stdlib_getopt_over_every_cluster(self):
        """THE RULE, EXECUTED AGAINST AN INDEPENDENT IMPLEMENTATION (Prudence, #737 r2).

        This grammar has been got wrong three times in this repo and every miss is a
        false green. Documentation is what we had; `getopt` is the same C grammar the
        real utilities use, it ships with Python, and it needs no coreutils — so the
        part most likely to go wrong is now the part most verified. She ran 11,368
        clusters across the transparent rows and `env`'s letters with 0 disagreements;
        this pins it permanently."""
        import getopt as _getopt
        import itertools

        from techne.hooks.post_tool_use import (
            _ENV_VALUE_LETTERS,
            _TRANSPARENT_PREFIXES,
            _getopt_cluster_advance,
        )

        letter_sets = [_ENV_VALUE_LETTERS] + [spec[0] for spec in _TRANSPARENT_PREFIXES.values()]
        alphabet = "ucsknoiePt0"
        checked = 0
        for value_letters in letter_sets:
            if not value_letters:
                continue
            spec = "".join(c + (":" if c in value_letters else "") for c in alphabet)
            for size in (1, 2, 3):
                for combo in itertools.product(alphabet, repeat=size):
                    token = "-" + "".join(combo)
                    # getopt consumes the next token iff it parses `token VALUE` but
                    # not `token` alone — the operational definition of "takes a value".
                    try:
                        _getopt.getopt([token], spec)
                        consumes = 1
                    except _getopt.GetoptError:
                        try:
                            _getopt.getopt([token, "VALUE"], spec)
                            consumes = 2
                        except _getopt.GetoptError:
                            continue  # not a legal cluster at all
                    assert _getopt_cluster_advance(token, value_letters) == consumes, (
                        f"{token!r} with {sorted(value_letters)}: helper says "
                        f"{_getopt_cluster_advance(token, value_letters)}, getopt says {consumes}"
                    )
                    checked += 1
        assert checked > 1000, f"the corpus must be substantial, got {checked}"

    def test_the_helper_contract_directly(self):
        """nit 3 — the docstring's own examples as assertions, so a failure names the
        RULE rather than naming a command."""
        from techne.hooks.post_tool_use import _getopt_cluster_advance as adv

        assert adv("-vs", frozenset({"s", "k"})) == 2, "value letter LAST consumes next"
        assert adv("-uCC", frozenset({"u"})) == 1, "value attached in-token"
        assert adv("-tc", frozenset({"c"})) == 2, "flag then value letter, last"
        assert adv("-i", frozenset({"u"})) == 1, "no value letter present"
