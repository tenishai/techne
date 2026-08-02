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
            db_path, "sess-active", "mcp__techne__techne_store",
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
            db_path, "sess-intact", "Bash",
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
            db_path, "sess-elided", "Bash",
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
            db_path, "sess-elided-zero", "Bash",
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
            db_path, "sess-elided-red", "Bash",
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
            db_path, "sess-vouch", "Bash",
            "Ran: cargo test -p small\nOutput: test result: ok. 3 passed; 0 failed",
        )
        _insert_obs(
            db_path, "sess-vouch", "Bash",
            "Ran: pytest -q\nOutput: 900 passed"
            "\n… [5000 chars elided] …\ncoverage 82%",
        )
        ev = query_turn_evidence(db_path, "sess-vouch")
        assert ev.tests.totals_unreliable is True
        assert ev.tests.suite_green is None
        assert check_claims([{"type": "tests_green"}], ev)[0]["status"] == "unverifiable"

    def test_zero_errors_does_not_substitute_for_a_missing_failure_count(self, db_path):
        """"0 errors" says nothing about failures — different categories.

        The guard originally required `errors is None`, so any parsed zero-errors
        anywhere disarmed it and returned green. `_ERRORS_RE` is loose enough to
        match prose, which widens the exposure.
        """
        _insert_session(db_path, "sess-zeroerr")
        _insert_obs(
            db_path, "sess-zeroerr", "Bash",
            "Ran: pytest -q\nOutput: 900 passed, 0 errors"
            "\n… [5000 chars elided] …\ntail",
        )
        ev = query_turn_evidence(db_path, "sess-zeroerr")
        assert ev.tests.suite_green is None
        assert check_claims([{"type": "tests_green"}], ev)[0]["status"] == "unverifiable"

    def test_positive_errors_still_makes_the_suite_red(self, db_path):
        """A row with real errors is red, not unverifiable — it can't false-green."""
        _insert_session(db_path, "sess-poserr")
        _insert_obs(
            db_path, "sess-poserr", "Bash",
            "Ran: pytest -q\nOutput: 900 passed, 4 errors"
            "\n… [5000 chars elided] …\ntail",
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
            db_path, "sess-red-wins", "Bash",
            "Ran: pytest -q\nOutput: 900 passed\n… [5000 chars elided] …\ntail",
        )
        _insert_obs(
            db_path, "sess-red-wins", "Bash",
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
            db_path, "sess-mypy", "Bash",
            "Ran: make check\nOutput: mypy: Found 0 errors in 42 files"
            "\n… [6000 chars elided] …\ndone",
        )
        _insert_obs(
            db_path, "sess-mypy", "Bash",
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
        message = (
            "git commit -F - <<EOF\nfix: thing\n\n"
            "Output: 2206 passed, 17 skipped\nEOF"
        )
        _insert_obs(
            db_path, "sess-ambig", "Bash",
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
        _insert_obs(db_path, "sess-noprefix", "Bash", "Bash: git commit\n[main abc1234] some commit")
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
            db_path, "sess-gitlog", "Bash",
            "Ran: git log -1\nOutput: commit 93db80e\n\n"
            "    Full suite: 2209 passed, 17 skipped.\n",
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

    def test_runner_name_in_a_quoted_message_is_not_a_test_run(self, db_path):
        """`git commit -m "fix the pytest flake"` names a runner but runs none.

        Guards the adjacent-pair rule: a loose token scan for "pytest" would read
        the commit message as a test run, which is the same mistake as reading a
        command's own text as its output. The commit itself is still evidence.
        """
        _insert_session(db_path, "sess-quoted")
        _insert_obs(
            db_path, "sess-quoted", "Bash",
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
            db_path, "sess-loose", "Bash",
            'Ran: echo "about to run pytest against the worktree"\nOutput: 5 passed',
        )
        ev = query_turn_evidence(db_path, "sess-loose")
        assert ev.tests.passed is None
        assert ev.tests.suite_green is None

    def test_interpreter_behind_a_variable_still_counts_as_a_runner(self, db_path):
        """`"$VENV" -m pytest` is the dominant real-world shape and must match.

        Head-only matching missed it — the head is a variable reference, not a
        recognisable interpreter. Measured against the live ledger, requiring a
        literal head refused 103 rows; the `-m <module>` pair rule cut that to 48,
        and the remainder are genuinely non-runner commands.
        """
        _insert_session(db_path, "sess-var")
        _insert_obs(
            db_path, "sess-var", "Bash",
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
            db_path, "sess-make", "Bash",
            "Ran: make check\nOutput: mypy: Found 0 errors in 42 files"
            "\n… [6000 chars elided] …\ndone",
        )
        _insert_obs(
            db_path, "sess-make", "Bash",
            "Ran: pytest tests/unit -q\nOutput: 8 passed in 1.1s",
        )
        ev = query_turn_evidence(db_path, "sess-make")
        assert ev.tests.totals_unreliable is True
        assert ev.tests.suite_green is None

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
            any_tool_call=True, active_tool_call=False, artifacts=[],
            observation_count=1, bash_captured=False,
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
