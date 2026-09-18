"""The evidence gate can confirm a linter PASSED, not merely that it ran (techne#939).

`tests_green` was the only claim type that checked an OUTCOME, and the only
outcome it could parse was a test runner's. A lint or type-check claim had to go
through `ran_action`, which matches a command substring in the ledger and answers
"did this appear", never "what did it return". An agent that runs `ruff check`,
gets 14 errors, and then writes "lint is clean" produced a BACKED verdict.

THE SHAPE IS REAL, THE QUOTE WAS NOT. An earlier version of this docstring said
this repo's PRs are "full of 'ruff and mypy are clean'". Measured over the last
40 PR bodies: that exact phrase appears ZERO times -- this repo has no mypy
(issue #896), so it could not -- while a lint-or-typecheck-is-clean claim appears
in 21 of them. The claim type earns its place on those 21.

OUTCOME IS READ FROM THE TOOL'S OWN SUMMARY LINE, specifically from the OUTPUT
half of the row via `_bash_output`. Passing the whole row let the COMMAND text
satisfy the success regex -- `ruff check -q . ; echo "All checks passed!"` parsed
as green while ruff printed failures -- which is the same trap the test-totals
path already closes.

THE LEDGER HAS NO EXIT CODE, which is why the summary line is all there is.
Surveyed across 52 live-captured Bash responses, the union of every key the
harness supplies is:

    backgroundCwdHint, backgroundTaskId, gitOperation, interrupted,
    isImage, noOutputExpected, stderr, stdout

That is the whole design constraint, and it is why the silent-on-success tools
below are `unverifiable` rather than green -- and why a readable tool may not
vouch for an unread one.
"""

from __future__ import annotations

import pytest

from techne.core.evidence import (
    CheckEvidence,
    Evidence,
    _classify_check_command,
    _parse_check_output,
    check_claims,
)


@pytest.mark.parametrize(
    "command,expected_head,readable",
    [
        ("ruff check techne/", "ruff", True),
        (".venv/bin/ruff check techne/ tests/", "ruff", True),
        ("env MYPYPATH=. mypy techne/", "mypy", True),
        ("pyright src/", "pyright", True),
        ("go vet ./...", "go", False),
        ("cargo clippy -- -D warnings", "cargo", False),
        ("tsc --noEmit", "tsc", False),
    ],
)
def test_verification_commands_are_recognised(command, expected_head, readable):
    assert _classify_check_command(command) == (expected_head, readable)


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "fix the ruff warning"',
        'echo "run mypy later"',
        "npm run build -- lint",
    ],
)
def test_a_tool_named_inside_an_argument_is_not_a_check(command):
    """THE MATCH IS ON THE COMMAND HEAD, NEVER A SUBSTRING.

    pi-warden's equivalent is a \\b-bounded regex over the whole command string,
    so `git commit -m "fix the ruff warning"` classifies as a check there. We
    borrowed its vocabulary and kept our own quote-aware head matching, which is
    the same distinction `_command_runs_tests` already documents for pytest.
    """
    assert _classify_check_command(command) == (None, False)


@pytest.mark.parametrize(
    "head,output,expected",
    [
        ("ruff", "All checks passed!", True),
        ("ruff", "Found 2 errors.\n[*] 2 fixable with the `--fix` option.", False),
        ("ruff", "1 file already formatted", True),
        ("ruff", "3 files already formatted", True),
        ("ruff", "1 file would be reformatted", False),
        ("pyright", "0 errors, 0 warnings, 0 informations", True),
        ("pyright", "1 error, 0 warnings, 0 informations", False),
        ("pyright", "12 errors, 0 warnings, 0 informations", False),
        ("mypy", "Success: no issues found in 3 source files", True),
        ("mypy", "Found 2 errors in 1 file (checked 3 source files)", False),
    ],
)
def test_summary_lines_are_parsed(head, output, expected):
    """Every ruff and pyright case here was produced by running the real tool."""
    assert _parse_check_output(head, output) is expected


def test_a_zero_count_success_line_is_not_read_as_failure():
    """THE BUG THAT MADE BOTH SIDES REGEXES.

    pyright's SUCCESS line is "0 errors, 0 warnings, 0 informations", which
    contains the substring "errors,". With substring markers the failure side
    matched its own success line, both fired, and a clean run degraded to
    unverifiable. Caught by running the tool, not by re-reading the patterns.
    """
    assert _parse_check_output("pyright", "0 errors, 0 warnings, 0 informations") is True


def test_conflicting_markers_are_unverifiable_not_a_guess():
    """Two invocations in one row: refuse rather than pick."""
    assert _parse_check_output("ruff", "All checks passed!\nFound 2 errors.") is None


def _claim(ev):
    return check_claims([{"type": "checks_green"}], ev)[0]


def test_a_clean_run_is_backed():
    ev = Evidence(checks=CheckEvidence(ran=True, green=True, tools=["ruff"]))
    assert _claim(ev)["status"] == "backed"


def test_a_dirty_run_is_unbacked():
    """THE REGRESSION. Before this, the same situation produced a BACKED
    ran_action verdict for 'lint is clean'."""
    ev = Evidence(checks=CheckEvidence(ran=True, green=False, tools=["ruff"]))
    r = _claim(ev)
    assert r["status"] == "unbacked"
    assert "NOT clean" in r["detail"]


def test_a_silent_tool_that_ran_is_unverifiable_never_green():
    """go vet, tsc, eslint and cargo clippy print nothing on success.

    With no exit status in the ledger, silence is indistinguishable from output
    elided in the ledger — the same reason totals_unreliable refuses to call a
    test run green. Reporting this as backed would be the false green the whole
    claim type exists to remove.
    """
    ev = Evidence(checks=CheckEvidence(ran=True, green=None, tools=["go"], unreadable=["go"]))
    r = _claim(ev)
    assert r["status"] == "unverifiable"
    assert "no exit status" in r["detail"]


def test_ran_but_unreadable_is_distinguished_from_nothing_ran():
    """Two different facts, two different verdicts."""
    silent = _claim(
        Evidence(checks=CheckEvidence(ran=True, green=None, tools=["tsc"], unreadable=["tsc"]))
    )
    none = _claim(Evidence(checks=CheckEvidence()))
    assert silent["status"] == "unverifiable"
    assert none["status"] == "unbacked"
    assert silent["detail"] != none["detail"]


def test_bash_off_is_unverifiable():
    """Absence of a deed is not disproof when capture is off — the file's rule."""
    ev = Evidence(checks=CheckEvidence(), bash_captured=False)
    assert _claim(ev)["status"] == "unverifiable"


def test_any_failure_outweighs_any_success():
    """A stated failure is not redeemed by a stated success.

    PER-RESULT, NOT PER-TOOL. The results list cannot tell a second TOOL from a
    RERUN of the same one, so `ruff` failing and then passing after a fix still
    reports not-clean for the window. That is the safe direction and is why the
    gate is scoped to a session window rather than a single command.
    """
    from techne.core.evidence import _classify_check_command as _c

    assert _c("ruff check .")[0] == "ruff"
    ev = Evidence(checks=CheckEvidence(ran=True, green=False, tools=["ruff", "mypy"]))
    assert _claim(ev)["status"] == "unbacked"


def test_ran_action_is_left_alone():
    """The issue is explicit: ran_action is honest about what it does, it just
    should not be the only door."""
    ev = Evidence(actions=["ruff check techne/"])
    r = check_claims([{"type": "ran_action", "match": "ruff check"}], ev)[0]
    assert r["status"] == "backed"


@pytest.fixture
def ledger(tmp_path):
    """A real session in a real store, so the AGGREGATION runs, not a hand-built dict."""
    from techne.core.database import connect, init_db

    db = tmp_path / "ev.db"
    init_db(db)
    conn = connect(db)
    conn.execute(
        "INSERT INTO sessions (id, project_path, started_at) VALUES (?, ?, ?)",
        ("sess-checks", "/p", "2026-09-18T10:00:00"),
    )
    conn.commit()
    conn.close()

    def add(command, output):
        conn = connect(db)
        conn.execute(
            "INSERT INTO observations (session_id, tool_name, content, tier) "
            "VALUES (?, 'Bash', ?, 'hot')",
            ("sess-checks", f"Ran: {command}\nOutput: {output}"),
        )
        conn.commit()
        conn.close()

    return db, add


def _evidence(db):
    from techne.core.evidence import query_turn_evidence

    return query_turn_evidence(db, "sess-checks")


def test_an_unreadable_tool_blocks_green(ledger):
    """THE PRE-PASS FALSE GREEN (W2), DRIVEN THROUGH THE REAL AGGREGATION.

    `green` ignored `unreadable`, so a window with `ruff check` (states success)
    and `tsc --noEmit` (silent) produced green=True with BOTH tools listed -- and
    the detail read "Verification clean: ruff, tsc reported no problems." tsc
    reported nothing. "ruff and tsc are clean" came back BACKED on ruff alone.

    AN EARLIER VERSION OF THIS TEST HAD NO TEETH: it built CheckEvidence by hand
    with green=None, which is the value the aggregation is supposed to COMPUTE, so
    it exercised only check_claims' rendering. This one inserts real Bash rows and
    lets query_turn_evidence do the aggregating.
    """
    db, add = ledger
    add("ruff check techne/", "All checks passed!")
    add("tsc --noEmit", "")

    ev = _evidence(db)
    assert set(ev.checks.tools) == {"ruff", "tsc"}, ev.checks
    assert "tsc" in ev.checks.unreadable, ev.checks
    assert ev.checks.green is not True, (
        "a readable tool vouched for a silent one; green must not be True while "
        f"a recognised tool went unread. {ev.checks}"
    )

    r = check_claims([{"type": "checks_green"}], ev)[0]
    assert r["status"] == "unverifiable", r
    assert not r["detail"].startswith("Verification clean"), r["detail"]


def test_the_command_text_cannot_supply_the_success_line(ledger):
    """THE PRE-PASS FALSE GREEN (W7), DRIVEN THROUGH THE REAL ROW PARSE.

    The row is "Ran: <command>\nOutput: ...", and the whole row was handed to the
    parser -- so a command CONTAINING the summary text satisfied the success
    regex while the tool printed failures.

    The earlier version called `_parse_check_output(head, _bash_output(row))`
    directly, which is the helper, not the call site that was wrong.
    """
    db, add = ledger
    add('ruff check -q . ; echo "All checks passed!"', "a.py:1:1: F401 unused")

    ev = _evidence(db)
    assert ev.checks.green is not True, (
        "the command text satisfied the success pattern; the OUTPUT half is what "
        f"should be parsed. {ev.checks}"
    )
    assert check_claims([{"type": "checks_green"}], ev)[0]["status"] != "backed"


def test_a_clean_run_through_the_ledger_is_backed(ledger):
    """The positive control: the real path still reaches BACKED."""
    db, add = ledger
    add("ruff check techne/", "All checks passed!")

    ev = _evidence(db)
    assert ev.checks.green is True, ev.checks
    assert check_claims([{"type": "checks_green"}], ev)[0]["status"] == "backed"


def test_a_failing_run_through_the_ledger_is_unbacked(ledger):
    db, add = ledger
    add("ruff check techne/", "Found 2 errors.")

    ev = _evidence(db)
    assert ev.checks.green is False, ev.checks
    assert check_claims([{"type": "checks_green"}], ev)[0]["status"] == "unbacked"


def test_unreadable_output_is_never_green():
    """`_bash_output` returns None on an ambiguous row boundary. None is not green."""
    from techne.core.evidence import _parse_check_output

    assert _parse_check_output("ruff", None) is None
    assert _parse_check_output("ruff", "") is None


def test_an_unread_tool_does_not_hide_a_stated_failure(ledger):
    """THE ROUND-2 REGRESSION: rule 2 was applied BEFORE rule 1 and swallowed failures.

    Gating green on `not unreadable` before testing for a stated failure made
    `ruff` reporting "Found 14 errors." alongside a silent `tsc` come back
    UNVERIFIABLE instead of UNBACKED -- an unread tool HID a real one.

    A failure is knowledge; an unread tool is the absence of knowledge, and
    absence must never outrank it.
    """
    db, add = ledger
    add("ruff check techne/", "Found 14 errors.")
    add("tsc --noEmit", "")

    ev = _evidence(db)
    assert ev.checks.green is False, ev.checks
    r = check_claims([{"type": "checks_green"}], ev)[0]
    assert r["status"] == "unbacked", "a stated failure was hidden by an unread tool"

    # THE DETAIL MUST NOT SPEAK FOR THE SILENT TOOL. The earlier version of this
    # test asserted only the status, so it could not see the ledger saying
    # "ruff, tsc reported problems" when tsc reported nothing -- the same defect
    # as rule 2 in the opposite polarity, made reachable by this very fix.
    assert "tsc reported problems" not in r["detail"], r["detail"]
    assert "ruff reported problems" in r["detail"], r["detail"]
    assert "tsc" in r["detail"], "the unread tool should still be disclosed, just not blamed"


def test_a_tool_that_failed_then_went_quiet_is_still_the_one_named(ledger):
    """THE ROUND-4 FINDING: a name filter over `unreadable` blamed the CLEAN tool.

    `unreadable` is appended when ANY row for a head is unreadable, so a tool that
    FAILED in one row and was quiet in another lands in BOTH lists. Filtering the
    detail by name then dropped the real speaker. Measured before the fix, with
    ruff failing, ruff quiet, and mypy clean:

        unbacked: Verification NOT clean: mypy reported problems.
                  (ruff also ran but said nothing readable...)

    mypy stated SUCCESS. ruff stated the failure. The sentence said the reverse --
    a false accusation of the clean tool, the direction this gate must never take.

    Fixed by recording per ROW which head stated what, rather than inferring it
    from a name filter.
    """
    db, add = ledger
    add("ruff check .", "Found 3 errors.")
    add("ruff check -q .", "")
    add("mypy techne/", "Success: no issues found in 4 source files")

    ev = _evidence(db)
    assert "ruff" in ev.checks.failed, ev.checks
    assert "mypy" in ev.checks.passed, ev.checks

    r = check_claims([{"type": "checks_green"}], ev)[0]
    assert r["status"] == "unbacked", r
    assert "ruff reported problems" in r["detail"], r["detail"]
    assert "mypy reported problems" not in r["detail"], (
        f"the clean tool is named as the failing one: {r['detail']!r}"
    )


def test_a_tool_named_as_failing_is_not_also_disclosed_as_silent(ledger):
    """A head in both lists must not read as two different tools."""
    db, add = ledger
    add("ruff check .", "Found 3 errors.")
    add("ruff check -q .", "")

    r = check_claims([{"type": "checks_green"}], _evidence(db))[0]
    assert r["detail"].count("ruff") == 1, (
        f"ruff is named twice, once as failing and once as silent: {r['detail']!r}"
    )
