"""The evidence gate can confirm a linter PASSED, not merely that it ran (techne#939).

`tests_green` was the only claim type that checked an OUTCOME, and the only
outcome it could parse was a test runner's. A lint or type-check claim had to go
through `ran_action`, which matches a command substring in the ledger and
answers "did this appear", never "what did it return". An agent that runs
`ruff check`, gets 14 errors, and then writes "lint is clean" produced a BACKED
verdict. This repo's own PRs are full of "ruff and mypy are clean".

OUTCOME IS READ FROM THE TOOL'S OWN SUMMARY LINE, BECAUSE THERE IS NOTHING ELSE
TO READ. The issue proposes the exit code ("sufficient for ruff / mypy / tsc").
THE LEDGER HAS NO EXIT CODE. Surveyed across 52 live-captured Bash responses,
the union of every key the harness supplies is:

    backgroundCwdHint, backgroundTaskId, gitOperation, interrupted,
    isImage, noOutputExpected, stderr, stdout

That is the whole design constraint, and it is why the silent-on-success tools
below are `unverifiable` rather than green.
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
    """A tool reporting errors is not redeemed by a second tool reporting none."""
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
