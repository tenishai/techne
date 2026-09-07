"""Evidence gate (techne#105) — read-only view over the observation ledger.

Surfaces what an agent actually DID (writes, git commits, bash commands, test
results) so claims like "I committed", "tests pass", "I wrote X" can be verified
against the ledger rather than trusted on faith.  Advisory only — flag, not block.

Free-text claim extraction is explicitly OUT of scope for this slice; callers
must pass structured claim dicts.  See check_claims() for the supported types.

This module covers DEEDS.  Claims about PROJECT STATE — whether a PR is merged,
whether we already do X — have no ledger record and are
handled by ``techne/core/state_claims.py`` (techne#584), which shares this
module's verdict shape and its rule that absence is ``unverifiable``, never
``unbacked``.  Both are reachable through one ``techne_check_evidence`` call.

IMPORTANT — Bash capture dependency: commits, shell actions, and test results are
all parsed from Bash observations, and live Bash capture is a config flag
(config.hooks.capture_bash). When it's off, those deeds aren't in the ledger *for
the current turn*, so a real commit/test-run is *absent*, not *disproven*. They
are not lost — transcript backfill records Bash regardless of the flag — but it
runs after the session, too late to serve an in-turn claim. Callers pass
``bash_captured`` (from config) so check_claims() returns ``unverifiable`` rather
than falsely flagging an honest agent as ``unbacked``. Artifacts (Write/Edit) and
any_tool_call work regardless, since those tools are captured by default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# The gate reads bounded projections of tool output, so it needs the writer's
# budget and marker to tell a complete record from a cut one (techne#438).
from techne.core.redaction import REDACTION_MARKER
from techne.hooks.post_tool_use import (
    _PREVIEW_MAX_CHARS,
    _skip_transparent_prefixes,
    was_elided,
)

# Tool names that are NOT agent deeds — meta-records or user-side events.
#
# Design call: the post_tool_use.py SKIP_TOOLS set ({"AskUserQuestion"}) is the
# *capture-time* blocklist — it decides what gets recorded.  This NON_DEED set
# is the *evidence-time* filter — it decides what counts as agent activity.
#
# "UserPrompt" appears in observations recorded from transcript-import and
# MCP-observe paths to mark the human's turn; it is not an agent action.
# "AskUserQuestion" is already skipped at capture time but may appear in older
# data or non-Claude-Code captures, so it is repeated here for safety.
#
# We do NOT replicate the full SKIP_TOOLS import here because that set is a
# capture-time policy whose membership may evolve independently.  The NON_DEED
# set is intentionally minimal and stable: only true non-deeds belong.
NON_DEED: frozenset[str] = frozenset({"UserPrompt", "AskUserQuestion"})

# Read-only inspection tools. A turn made up ONLY of these did not *do* anything,
# so they must not, on their own, back a "completion"/"did_work" claim (techne#105:
# "a lone Read currently backs it").  They still count as ``any_tool_call`` (they
# ARE tool calls) — the distinction is ``active_tool_call``, which drives the
# completion verdict.  Deed tools (Write/Edit/Bash/NotebookEdit) and any tool NOT
# listed here still count as activity.  This is deliberately a small, stable list
# of built-in inspection tools rather than an exhaustive passive-tool taxonomy:
# the goal is to stop pure-inspection turns from false-BACKING completion without
# risking a false 'unbacked' for genuine deeds done via less common / MCP tools
# (an unknown tool is treated as active, the safe direction for an advisory gate).
PASSIVE_TOOLS: frozenset[str] = frozenset(
    {"Read", "Grep", "Glob", "LS", "NotebookRead", "WebFetch", "WebSearch"}
)

# Regex: extract 7-40 hex chars from git's commit summary line:
#   [branch-name abc1234] commit message
# The word-boundary anchors the SHA's LEFT edge inside the brackets. This is
# heuristic, not exact: any bracketed token ending in a 7-40 hex run (some log
# formats) can match. That errs toward false-BACKING a `commit` claim, never
# false-flagging — the safe direction for an advisory tool; dedup only collapses
# repeats. Tightening (e.g. requiring a `git commit` action) is a follow-up.
_GIT_SHA_RE = re.compile(r"\[[^\]]*\b([0-9a-f]{7,40})\]")

# Test-runner result line patterns — cover pytest ("N passed"), jest/vitest
# ("N passed"), and generic "N failure(s)".
_PASSED_RE = re.compile(r"(\d+)\s+passed")
_FAILED_RE = re.compile(r"(\d+)\s+(?:failed|failure)")
_ERRORS_RE = re.compile(r"(\d+)\s+error")

# Returned for Bash-derived claims (commit / tests_green / ran_action) when Bash
# capture is off — the deeds simply aren't recorded, so absence is not disproof.
_BASH_OFF_DETAIL = (
    "Live Bash capture is off (config.hooks.capture_bash=False), so this session's "
    "commits, test results, and shell actions are not in the ledger YET — absent, "
    "not disproven. They are not lost: transcript backfill records Bash regardless "
    "of this flag (sync/jsonl_records.should_import ignores it), but it runs after "
    "the session, too late to verify a claim in the turn that makes it. Enable "
    "capture_bash for in-turn verification."
)


@dataclass
class SuiteEvidence:
    """Best-effort parse of test runner output from Bash observations.

    CAVEAT: Bash output is bounded at 2000 chars in the ledger (see
    post_tool_use.summarize).  The preview keeps the head AND the tail, so a
    runner's trailing result line survives; only the middle of a long run is
    elided, and the preview says so.  Rows written before PR #437 were cut
    head-only and carry no marker — ``_output_was_bounded`` recognises those by
    length.  The heuristic still only handles pytest/jest/vitest-style result
    lines, and a runner that reports its totals from the *middle* of its output
    can still be missed.
    ``None`` means "couldn't parse" — report honestly, not optimistically.

    Aggregation: when multiple Bash rows contain test output (e.g. re-runs),
    we take the *maximum* count seen, not the sum.  Max is less wrong for
    re-run scenarios; sum would double-count.  Document this in callers.

    Max carries a sharp caveat: it is only sound over a COMPLETE sample.  Over
    bounded output it runs on the surviving subset, so a zero that survived beats
    a positive that was elided.  ``_row_totals_unreliable`` is what stops that
    from becoming a green verdict, which is why only POSITIVE counts are treated
    as decisive anywhere in this module.
    """

    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    suite_green: bool | None = None
    # True when at least one row could certify green without evidence of zero
    # failures: a passed count survived, no failure count did, and that row's
    # output was bounded so the failure line may have been dropped rather than
    # never printed. Set per row — see _row_totals_unreliable (techne#438).
    totals_unreliable: bool = False


@dataclass
class Evidence:
    """Structured view of what an agent actually did in a session window.

    Turn-boundary convention: the observation ledger has no explicit turn
    marker — only ``session_id`` and ``timestamp``.  The window is defined by
    ``(session_id, since_iso)``:

    * ``since_iso=None``  → all observations for the session.
    * ``since_iso=<iso>`` → observations with ``timestamp > since_iso``.

    The caller is responsible for choosing a meaningful boundary (e.g. the
    ISO time just before the agent's last reply).  There is no guarantee that
    the boundary aligns with a clean agent turn; partial turns are possible.
    """

    any_tool_call: bool = False
    """True if the window contains any deed-eligible tool call (anything outside
    ``NON_DEED``), including passive inspection tools like Read/Grep. Surfaced for
    visibility; ``active_tool_call`` is what backs a completion claim."""

    active_tool_call: bool = False
    """True if the window contains a tool call that is NOT purely passive
    inspection — i.e. any tool outside ``NON_DEED`` and ``PASSIVE_TOOLS``
    (Write/Edit/Bash/NotebookEdit and non-inspection MCP tools). This is the
    signal that backs a ``completion``/``did_work`` claim: a turn that only read,
    grepped, or fetched did not *do* the work (techne#105)."""

    artifacts: list[str] = field(default_factory=list)
    """File paths written or edited (Write + Edit tools), deduped, order-preserved."""

    commits: list[str] = field(default_factory=list)
    """Git commit SHAs parsed from Bash output, deduped, order-preserved."""

    actions: list[str] = field(default_factory=list)
    """Bash commands run (text after "Ran: " on the first content line)."""

    tests: SuiteEvidence = field(default_factory=SuiteEvidence)
    observation_count: int = 0

    bash_captured: bool = True
    """Whether Bash observations are recorded for this install (config.hooks.capture_bash).

    LOAD-BEARING for honesty: commits, actions, and test results are all parsed
    from Bash output, but Bash capture is OFF by default in techne. When it's off,
    the ledger simply has no Bash rows — so a real commit/test-run is ABSENT, not
    DISPROVEN. check_claims() must therefore return ``unverifiable`` (not
    ``unbacked``) for Bash-derived claims when this is False, or the gate would
    falsely flag honest agents. The caller (MCP tool / CLI) sets this from config.
    """


def _row_totals_unreliable(output_text: str, p: int | None, f: int | None, e: int | None) -> bool:
    """True when this row could certify green without evidence of zero failures.

    Called per row, never on the aggregate — see the caller for why cross-row
    aggregation is unsafe here (techne#438 re-review).

    A row is unreliable when a passed count survived, no failure count did, and
    the output was bounded so the failure line may simply have been dropped.
    Intact output is different in kind: pytest, jest and vitest all omit the
    failure line entirely when nothing fails, so absence there is real evidence
    of zero.

    Only a POSITIVE count disarms this, never a zero. ``_parse_test_output``
    takes the ``max()`` of every count it sees, and max is only sound over a
    complete sample: in bounded output it runs over the SURVIVING subset, so a
    zero that survived beats a positive that was elided. A cargo workspace shows
    it — a small green crate reports ``0 failed`` in the head while
    ``900 passed; 12 failed`` sits in the dropped middle, and the surviving zero
    wins. A positive count needs no such care: it makes the suite red on its own,
    and nothing hiding in the elided middle can make it green again.

    An `errors` count does NOT substitute for a missing `failed` count either.
    They are different categories, so a parsed ``0 errors`` says nothing about
    failures.
    """
    if p is None and f is None and e is None:
        return False  # no counts at all -> this row says nothing about any suite
    # A row with NO passed count still matters. `make check` can print mypy's
    # "Found 0 errors" while both pytest totals sit in the dropped middle, giving
    # p=None, f=None, e=0. Returning early on `p is None` let that row stay silent,
    # so one small green run elsewhere in the window certified the whole thing.
    if f is not None and f > 0:
        return False  # a real failure count makes the suite red on its own
    if e is not None and e > 0:
        return False  # errors likewise
    return _output_was_bounded(output_text)


# Commands whose output may be read for test totals. Deliberately a denylist-free
# ALLOWLIST: anything not named here contributes no test evidence at all.
#
# Without this gate, any command whose output happens to contain a count is read
# as a test result. Both directions fired on the live ledger: `git log` echoing a
# commit message that says "2209 passed" certified a green suite with no test run
# (8 rows), and `ruff check` printing "Found 181 errors" turned a green window red
# (29 rows) — the false-accusation direction techne#105 exists to prevent.
_TEST_RUNNER_HEADS = frozenset(
    {
        "pytest",
        "py.test",
        "tox",
        "nox",
        "jest",
        "vitest",
        "mocha",
        "jasmine",
        "ava",
        "karma",
        "rspec",
        "minitest",
        "phpunit",
        "behat",
        "ctest",
        "bats",
        "gotestsum",
        "nextest",
        "unittest",
    }
)

# Heads that run tests only under a specific subcommand — `go test`, not `go build`.
_TEST_RUNNER_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "go": frozenset({"test"}),
    "cargo": frozenset({"test", "nextest"}),
    "dotnet": frozenset({"test"}),
    "swift": frozenset({"test"}),
    "mix": frozenset({"test"}),
    "npm": frozenset({"test", "t"}),
    "yarn": frozenset({"test"}),
    "pnpm": frozenset({"test"}),
    "bun": frozenset({"test"}),
    "deno": frozenset({"test"}),
    "mvn": frozenset({"test", "verify"}),
    "gradle": frozenset({"test"}),
    "gradlew": frozenset({"test"}),
    # `check` is included alongside `test` after measuring the alternative.
    # Excluding it looked safer — a lint-only `make check` printing "Found N
    # errors" is a false red — but it silently reintroduced techne#438's
    # cross-row false green: a `make check` that runs the suite stops being
    # recognised as a test run, so its bounded output no longer raises the
    # unreliable flag, and a small intact `pytest` elsewhere in the window
    # certifies the whole thing green. Losing a warning is worse than gaining a
    # too-cautious red, so `check` stays. The residual is a lint-only check
    # target reporting its own error count; that yields `unbacked` (over-
    # cautious) rather than a false green.
    "make": frozenset({"test", "tests", "check"}),
    "rake": frozenset({"test", "spec"}),
}

# Interpreters and task runners that invoke a real runner as an argument:
# `python -m pytest`, `uv run pytest`, `poetry run pytest`.
_TEST_RUNNER_WRAPPERS = frozenset(
    {
        "python",
        "python3",
        "py",
        "uv",
        "poetry",
        "pdm",
        "hatch",
        "pipenv",
        "rye",
        "npx",
        "bundle",
        "pipx",
        "pdm",
        "just",
    }
)

# Modules a `-m` flag can name to mean "run the suite".
_TEST_MODULES = frozenset({"pytest", "unittest", "nox", "tox"})

# Heads where `-m` means "commit message", not "module". Without this, a message
# reading `-m pytest` would register as a test run.
_DASH_M_IS_A_MESSAGE = frozenset({"git", "hg", "svn", "jj", "bzr", "hub", "gh"})


# `<<` or `<<-`, an optionally quoted or backslash-escaped tag, and a tag charset
# wide enough for real delimiters (`PY-1`, `EOF.2`). The lookarounds keep a
# herestring (`<<<word`) from matching: without them the scan finds the `<<` at
# offset 1 of `<<<` and strips to end of command, swallowing a real run below.
_HEREDOC_OPEN = re.compile(
    r"(?<!<)<<(-?)(?!<)\s*(?:\\|(['\"]))?([A-Za-z_][A-Za-z0-9_.\-]*)(?(2)\2|)"
)


def _mask_quoted_line(line: str, quote: str | None) -> tuple[str, str | None]:
    """Mask one line's quoted spans, carrying quote state in and out.

    `echo "a << b"` contains no heredoc, but an unmasked scan reads `b` as a
    delimiter and strips every following line hunting a terminator that never
    comes — swallowing a real test run below.

    State carries across newlines, because a quoted argument may span them and
    ``_split_bash_segments`` models it that way. A per-line mask that restarts
    clean disagrees: after a multi-line quoted commit message, the next line reads
    the message's CLOSING quote as an opening one and hides a real heredoc opener.

    But state must NOT carry across a heredoc body, which is why this is threaded
    by the caller rather than run over the whole command. Body text is not subject
    to shell quoting at all, so a stray apostrophe in a script being written —
    ``# don't remove this`` — would otherwise open a quote that never closes and
    poison every command line after it. Masking the whole command up front cannot
    express that distinction, because the mask would run over text the segment
    splitter never sees.
    """
    out: list[str] = []
    for char in line:
        if quote:
            out.append("\x00" if char != quote else char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        else:
            out.append(char)
    return "".join(out), quote


def _strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc bodies, keeping the lines that are actually commands.

    Quote-awareness is not enough here. ``_split_bash_segments`` splits on ``\\n``,
    and a heredoc body is not quoted, so every line of it becomes a segment and a
    body line beginning with a runner name becomes a command head. A script
    written to disk with ``cat > probe.py <<'PY'`` whose text contains a
    ``pytest`` line would then read as a test run, and whatever count-shaped text
    that script printed would be admitted as test evidence — the eighth instance
    of the same class this whole gate exists to close.
    """
    lines = command.split("\n")
    kept: list[str] = []
    i = 0
    # Threaded across KEPT lines only. The body-skipping loop below advances `i`
    # without touching `quote`, so a heredoc body cannot leak quote state into the
    # commands that follow it.
    quote: str | None = None
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        # Match on the RAW line — a heredoc tag is usually quoted (`<<'PY'`), so
        # scanning a masked line would blank out the tag itself. The mask is used
        # only to LOCATE: a `<<` whose own position is inside a quoted span (as in
        # `echo "a << b"`) opens nothing.
        masked, quote = _mask_quoted_line(line, quote)
        opens = [
            (m.group(1) == "-", m.group(3))
            for m in _HEREDOC_OPEN.finditer(line)
            if masked[m.start()] != "\x00"
        ]
        i += 1
        # A line may open several heredocs; their bodies follow in order.
        for dash, tag in opens:
            # Terminator rules differ, and being lenient ends a body EARLY: for
            # plain `<<TAG` the terminator must be the whole line, so a tag that
            # appears indented inside the body is body text, not the end. Only
            # `<<-TAG` permits leading tabs.
            while i < len(lines) and (lines[i].lstrip("\t") if dash else lines[i]) != tag:
                i += 1
            i += 1  # skip the terminator itself
    return "\n".join(kept)


#: Shells whose ``-c`` argument is a nested script the gate must look inside.
#: NOT added to `_TEST_RUNNER_WRAPPERS`: that set matches a head whose ARGUMENT
#: is a runner, so `bash` there would make `bash -c 'echo 12 passed'` read as a
#: test run — #440 through the front door.
_SHELL_C_WRAPPERS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})

#: Short-option LETTERS that consume the next token as a value. Letters, not
#: whole tokens, because POSIX getopt says only the LAST letter of a cluster may
#: take a value — so one rule covers `-o`, `+o`, `-O`, `+O`, `-xo`, `-euo`,
#: `-euxo` and `-co` at once. A token-based set could not see `-euo pipefail`
#: (the clustered spelling of an idiom we already handled unclustered), and no
#: amount of lengthening it would. Verified against bash 5.3.15: `bash -co
#: pipefail 'echo MARKER'` prints MARKER, i.e. `o` took the value and the script
#: is the token AFTER it. Keeping the hand-kept part to two letters is the point
#: — the risk in such a list is proportional to its length. (Prudence, #521 r2.)
#:
#: CORRECTION TO THAT DERIVATION, measured 2026-08-07 and the reason the scanner
#: counts value letters instead of testing only the last one. POSIX says only a
#: cluster's final letter may take a value; REAL BASH DOES NOT CARE. Both shells
#: on this machine run `-oc pipefail '<script>'`, and `-oc bogusopt` fails with
#: "bogusopt: invalid option name" — so `o` ate the next token from the FRONT of
#: the cluster. Measured, not read off a man page:
#:
#:   /usr/local/bin/bash 5.3.15 (the DEFAULT shell here)  -oc  -> runs
#:   /bin/sh, /bin/bash        3.2.57 (Apple)             -oc  -> runs
#:
#: r3 nit 2 said bash 5.3 rejects this spelling and filed it as a curiosity of
#: legacy /bin/sh. It does not, and it is not: under the last-letter rule this
#: was a live false green on the shell that actually runs here.
_SHELL_VALUE_LETTERS = frozenset({"o", "O"})

#: Long options taking a separate value. No clustering applies to these.
_SHELL_VALUE_LONG_OPTS = frozenset({"--rcfile", "--init-file"})

#: Bound on `-c` nesting. Depth 2 covers `bash -c 'sh -c "pytest"'`; beyond that
#: a command is pathological and the gate stops rather than recursing on input.
_MAX_SHELL_C_DEPTH = 3


def _expand_shell_c(segments: list[str], depth: int = 0) -> list[str]:
    """Replace ``bash -c '<script>'`` segments with the script's own segments.

    GATE-SIDE ONLY, and that placement is the whole design (techne#445). The
    splitter is SHARED with the capture-time noise filter, and the two want
    opposite things from a `-c` wrapper:

      gate         `bash -c 'pytest -q'` unseen -> the row raises no
                   `totals_unreliable` flag, so an intact run elsewhere in the
                   window certifies the whole thing green. Starvation IS a false
                   green here (techne#438).
      noise filter `bash -c 'ls'` currently heads on `bash`, which is not
                   read-only, so it CAPTURES. Expanding it there would head on
                   `ls` and DROP it — flipping False->True, i.e. LESS capture,
                   against that filter's documented fail-safe. Measured both
                   before choosing: the heredoc precedent (#444 r2) does not
                   transfer, because it only ever added segments.

    So the shared splitter keeps its semantics exactly and the gate expands
    afterwards.
    """
    if depth >= _MAX_SHELL_C_DEPTH:
        return segments
    out: list[str] = []
    for seg in segments:
        script = _shell_c_script(seg)
        if script is None:
            out.append(seg)
            continue
        from techne.hooks.post_tool_use import _split_bash_segments

        inner = _split_bash_segments(_strip_heredoc_bodies(script))
        out.extend(_expand_shell_c(inner, depth + 1))
    return out


def _shell_c_script(segment: str) -> str | None:
    """The script argument of a ``<shell> -c <script>`` segment, else None.

    Uses `shlex` so the quoting that HID the runner is what now reveals it: the
    script arrives already unquoted. A segment shlex cannot parse (unbalanced
    quotes, which heredoc bodies produce) is left alone rather than guessed at.
    """
    import shlex

    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None
    i = 0
    while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith("-"):
        i += 1
    i = _skip_transparent_prefixes(tokens, i)
    if i >= len(tokens) or tokens[i].rsplit("/", 1)[-1] not in _SHELL_C_WRAPPERS:
        return None
    # `-c`, including clustered SHORT flags containing c (`-lc`, `-ec`, `-cx`).
    #
    # `t[1:2] != "-"` excludes every --long option, and that guard is load-bearing
    # twice over: without it `--norc` matches (it ends in "c"), which BOTH loses
    # `bash --noprofile --norc -c 'pytest -q'` — idiomatic clean-shell in CI and
    # Makefiles — AND makes `bash --norc pytest` read the following positional as
    # a script, so a FILE named pytest reads as a suite run. Starvation and
    # invention from one bug. `"c" in t[1:]` rather than endswith, because c need
    # not be last (`bash -cx`).
    j = i + 1
    while j < len(tokens):
        t = tokens[j]
        if t in _SHELL_VALUE_LONG_OPTS:
            j += 2  # its value is not a positional
            continue
        if t.startswith("--"):
            j += 1  # a long option we do not model; it takes no separate value
            continue
        if t[:1] in ("-", "+") and len(t) > 1:
            cluster = t[1:]
            # EVERY value letter in the cluster consumes a token, not just the
            # last one. Measured against the shells on this box rather than
            # derived, because the derivation was WRONG (r3 nit 2 asserted POSIX
            # last-letter-only, and that real bash 5.3 rejects the other
            # spelling — it does not):
            #
            #   bash 5.3.15 -oc pipefail 'echo MARKER'  -> prints MARKER
            #   bash 5.3.15 -oc bogusopt 'echo MARKER'  -> "bogusopt: invalid
            #                                              option name"
            #
            # That error is the proof: `o` consumed the NEXT TOKEN while sitting
            # first in the cluster, so position is irrelevant. Under the
            # last-letter rule `bash -oc pipefail 'pytest -q'` returned
            # `pipefail` as the script — starvation, i.e. a false green, on the
            # DEFAULT shell of this machine (/usr/local/bin/bash), not on some
            # legacy /bin/sh nobody invokes.
            n_values = sum(1 for ch in cluster if ch in _SHELL_VALUE_LETTERS)
            if "c" in cluster:
                # The script sits after however many values the cluster ate:
                # `-c` -> next token; `-co pipefail` / `-oc pipefail` -> the one
                # after that; `-occ pipefail` likewise (verified: prints MARKER).
                k = j + 1 + n_values
                return tokens[k] if k < len(tokens) else None
            j += 1 + n_values
            continue
        return None  # a positional before any -c: not a -c invocation
    return None


def _command_runs_tests(command: str) -> bool:
    """True when some segment of `command` actually invokes a test runner.

    Matched on the segment's COMMAND HEAD, never as a substring of the whole
    command. That distinction is the point: `git commit -m "fix the pytest flake"`
    contains a runner name but does not run one, and substring matching would read
    the commit message as a test result — the same class of bug as reading a
    command's own text as its output. The shell splitter is quote-aware, so a
    runner named inside a quoted argument stays inside its segment and is never a
    head.
    """
    from techne.hooks.post_tool_use import _split_bash_segments

    segments = _split_bash_segments(_strip_heredoc_bodies(command))
    for segment in _expand_shell_c(segments):
        tokens = segment.split()
        i = 0
        # Skip leading `VAR=val` env assignments to find the real head.
        while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith("-"):
            i += 1
        # ...and the `env` prefix, for the same reason and in the same order as
        # the other three sites. THE FOURTH CALL SITE (r1 review): the gate saw
        # `env bash -c 'pytest -q'` only because that routes through
        # _expand_shell_c -> _shell_c_script, which already called this helper;
        # the DIRECT form `env pytest -q` fell through here. Not cosmetic — an
        # unrecognised command hits the `continue` in the row loop below, which
        # discards the row WHOLE, so its failures never reach `all_failed` and
        # _row_totals_unreliable is never consulted for it. Measured end-to-end
        # against a real ledger: `env pytest -q` -> "1 failed, 2619 passed"
        # alongside a small `pytest tests/test_small.py -q` -> "5 passed" gave
        # suite_green=True and BACKED the tests_green claim — the 2620-test run
        # discarded, the 5-test run certifying green. That is the starvation-
        # shaped false green this file names at 596-599 and techne#438.
        i = _skip_transparent_prefixes(tokens, i)
        if i >= len(tokens):
            continue
        head = tokens[i].rsplit("/", 1)[-1]  # basename of e.g. .venv/bin/pytest
        rest = tokens[i + 1 :]

        if head in _TEST_RUNNER_HEADS:
            return True
        subcommands = _TEST_RUNNER_SUBCOMMANDS.get(head)
        if subcommands:
            # Scan the tokens BEFORE a `--` separator. Everything after `--` is an
            # argument to something else, which is what distinguishes
            # `npm run test` from `npm run build -- test`.
            #
            # A stricter position rule was tried and reverted: filtering flags
            # keeps their VALUES, so `make -C build test`, `make -j 4 test`,
            # `mvn clean test`, `cargo +nightly test` and `npm --prefix web test`
            # all pushed the subcommand out of position and stopped being
            # recognised. In THIS gate that is not a harmless miss — an
            # unrecognised row raises no unreliable flag, so a small intact run
            # elsewhere in the window certifies green. Starvation here IS a false
            # green, the same shape as techne#438.
            #
            # The cost of the looser scan is `go build --tags test`, which names
            # `test` as a build tag. That match is inert: `go build` emits no
            # count-shaped output in either direction, so it can neither certify
            # nor accuse. An inert false match is the right trade for a live class
            # of false greens.
            before_separator = rest[: rest.index("--")] if "--" in rest else rest
            if any(t in subcommands for t in before_separator):
                return True
        if head in _TEST_RUNNER_WRAPPERS and any(
            t.rsplit("/", 1)[-1] in _TEST_RUNNER_HEADS for t in rest
        ):
            return True
        # `-m pytest` where the interpreter is behind a variable — `"$VENV" -m
        # pytest`, `$PY -m pytest`. The head is unrecognisable there, but the
        # module flag is not, and this is the dominant shape in real commands.
        # Matched as an ADJACENT PAIR, never as a loose token scan: `git commit
        # -m "fix the pytest flake"` puts the runner name in a quoted argument,
        # and only the pair rule keeps that from reading as a test run. VCS heads
        # are excluded outright because there `-m` means "message".
        if head not in _DASH_M_IS_A_MESSAGE:
            for flag, following in zip(rest, rest[1:]):
                if flag == "-m" and following.rsplit("/", 1)[-1] in _TEST_MODULES:
                    return True
    return False


_BASH_OUTPUT_MARKER = "\nOutput: "


def _bash_output(content: str) -> str | None:
    """Locate the output half of a ``summarize()``-shaped Bash observation.

    Returns None when the boundary cannot be located with certainty, and the
    caller then parses no test totals from the row.

    ``summarize`` writes ``"Ran: <command>\\nOutput: <preview>"`` into a single
    flat string with no escaping, so a command containing the marker is
    indistinguishable from the real boundary. That is reachable by ordinary
    habit, not just adversarially: a commit message with a line reading
    ``Output: 2206 passed, 17 skipped`` made the gate report a green suite for a
    ``git commit`` that ran no tests.

    Neither split direction is safe. Taking the FIRST occurrence pulls trailing
    command text into the output and invents counts; taking the LAST discards the
    head of a real output, elision marker included, promoting a bounded row to
    authoritative. So when the marker appears more than once we decline instead,
    which costs a verdict rather than fabricating one.

    Resolving this properly needs the command and output stored as separate
    fields rather than recovered from one string — see the artifact-store
    follow-up on techne#438.
    """
    if content.count(_BASH_OUTPUT_MARKER) != 1:
        return None
    return content.partition(_BASH_OUTPUT_MARKER)[2]


def _output_was_bounded(output_text: str) -> bool:
    """True when this output is a bounded projection rather than a full record.

    Two shapes, because the ledger predates the marker:

    * Current rows carry the explicit elision marker written by ``_preview``.
    * Rows written before PR #437 were cut with a bare ``tool_output[:2000]`` and
      carry NO marker, so ``was_elided`` alone reads a genuinely truncated legacy
      row as authoritative. They are recognisable by length: ``_preview`` returns
      text verbatim only when it is within budget, so an unmarked output at or
      over the budget could not have come from the current writer.

    Backfill re-summarises with current code at import time, so legacy rows shrink
    as a share of the ledger, but the ones already stored keep the old shape. An
    output that is genuinely complete at exactly the budget is misread as bounded;
    that costs a verdict of ``unverifiable`` instead of ``backed``, which is the
    direction this whole class of bug should fail in.

    THE LENGTH TEST IS ONLY SOUND WHEN THE STORED TEXT IS THE CUT TEXT (techne#441).
    ``redact_credentials`` runs BEFORE storage and substitutes ``<redacted:tag>``
    for each match, so a legacy row cut at exactly the budget can be stored BELOW
    it and read as authoritative — the bad direction, since an unflagged row can
    carry a green verdict. A redacted row therefore has an UNKNOWABLE original
    length, and "we cannot determine it" is a different fact from "it was under
    budget"; collapsing them is what produced this bug. Treated as bounded.

    Deliberately NOT widened by a shrinkage allowance: replacement is shorter than
    the secret by an UNBOUNDED amount, so any constant is a number nobody can
    justify — it would flag more intact rows than this does and still miss the
    large-shrinkage cases.

    IT HAS NOT FIRED YET, AND THAT RESTS ON ONE LEG, NOT FOUR. Setting the flag
    only changes a verdict when a row is simultaneously a test-runner command,
    parseable, failure-free AND redacted — but the first three are ORDINARY, not
    rare: they are what a green suite run looks like, and the redaction clause
    ALONE decides for nearly all of them. Nothing has fired because
    credential-shaped text happens to be absent from green runner output — a fact
    about CONTENT, not about structure.

    THAT PREDICATE IS EXECUTABLE — ``probe_length_test_pressure(db_path)``, at the
    foot of this module. RUN IT; DO NOT RE-DERIVE IT (techne#541). Every count it
    reports comes from the same helpers this gate calls, so there is no second
    implementation to drift from the first. That drift is not hypothetical: prose
    versions of this predicate were hand-rolled twice during review — measuring
    ``content`` where the gate reads ``_bash_output(content)``, and re-splitting
    ``Ran: `` instead of partitioning on ``_BASH_OUTPUT_MARKER`` — and both
    produced numbers that did not reproduce (an irreproducible 475 among them).
    The probe's clauses are exactly the ones ``_row_totals_unreliable`` applies
    BEFORE it consults this function.

    CREDENTIAL-SHAPED TEXT DOES REACH RUNNER OUTPUT HERE — ONE ROW TODAY. That is
    the honest form of the argument. An earlier version of this note claimed a
    cabal ``test_secrets_endpoint.py`` row was "inert only because a grep stripped
    the summary line — delete the pipe and all four legs hold". FALSE: that row's
    stored output is a pytest TRACEBACK, so it is a FAILING run, and the leg that
    disarms it is FAILURE-FREE, which no edit to the pipe can cross. Under ``-q`` a
    passing run through that row's own filter keeps nothing at all. Reaching the
    conjunction needs a different run — one that passes — not one keystroke.

    SO WATCH THE INTERSECTION, NOT THE STOCK. Re-measuring the conjunction is the
    wrong instruction: it is a stock that grows monotonically as the repo acquires
    tests, every member already known NOT to be redacted, so it moves while the
    risk does not — a watchdog barking at your own suite getting bigger. The
    observable that moves only when the hazard moves is
    ``marker_carrying_runner_rows``: runner rows whose output carries the marker,
    regardless of parseability or outcome. If it climbs, REPLACE the length test
    rather than re-justify it.

    THE PROBE HAS NOW DEMONSTRATED THAT ARGUMENT RATHER THAN ASSERTED IT. Between
    the 2026-08-10 hand measurement and a 2026-08-13 run, the stock grew with the
    suite (conjunction 413 → 493) while ``marker_carrying_runner_rows`` stayed at
    1. Exactly the predicted behaviour, and the reason to watch the second number
    and not the first — but read those figures as an illustration of the shape,
    not as a fact to maintain. The current values come from running the probe.

    (An even earlier version said to re-check "whether the conjunction has started
    occurring". Unexecutable: when it fires a verdict quietly becomes
    ``unverifiable`` — no alarm, no counter, nobody told. It read as a safeguard
    and functioned as a comment.)

    THREE THINGS MAKE IT IMPRECISE, and the third dominates the live data:

    * NO AGE DISCRIMINATOR EXISTS. A post-#437 row that was never cut carries no
      marker either, so it is indistinguishable by content from a legacy cut.
    * BACKFILL REGENERATES IT. Re-summarising runs ``_preview`` (verbatim, under
      budget, no marker) then redaction, which puts the marker back.
    * THE MARKER CANNOT TELL A TOKEN WE WROTE FROM THE SAME LITERAL ARRIVING FOR
      ANY OTHER REASON. Every flipped row inspected is a short, complete output
      that merely CONTAINS a credential shape — `.env` dumps, a JWT in a signed
      URL, a PEM block in a values.yaml, two of them output ABOUT redaction.

    THE FLIP COUNT IS ``probe_length_test_pressure``'s ``flips``, and the WHY of
    its construction is the part worth keeping in prose: it measures
    ``_bash_output(content)`` — the string the gate reads — NOT the whole
    observation row. Measuring the row inflates lengths by the
    ``Ran: …\nOutput: `` prefix, counts a marker in the COMMAND as a flip, and
    misses rows whose row exceeds the budget while their output does not. All four
    clauses participate (``was_elided`` + ``len(...) < _PREVIEW_MAX_CHARS`` +
    ``REDACTION_MARKER``, over ``_bash_output``); the length clause is NOT
    optional, because a flip is only a flip when the clauses above it did not
    already return True — dropping it inflates the count. Observed shape at the
    2026-08-10 measurement: lengths 290-1989, median 840, three within 10% of the
    budget.

    NO TRUE-POSITIVE COUNT IS QUOTED. Any estimate would come from
    proximity-to-budget, and the paragraph above rejecting a shrinkage allowance
    — shorter by an UNBOUNDED amount — is exactly the argument that this proxy is
    weak. Length cannot identify a true positive; that is the premise of the fix.

    A TIMESTAMP DISCRIMINATOR IS NOT SOUND AT DAY GRANULARITY, and this dataset
    contains the counterexample. #437 merged at 2026-08-01T20:22:17Z; against
    that INSTANT the split is 12 before / 3 after, and one flip lands at
    19:00:51Z ON THE MERGE DAY, where a date comparison yields a coin-toss
    between ``<`` and ``<=`` rather than an answer. (#515's two-format problem is
    a separate and lesser issue here — it breaks sub-day ordering at position 10
    — but three of the fifteen timestamps carry no zone at all.) The gate would
    remove three of fifteen. Not built.
    """
    if was_elided(output_text):
        return True
    if len(output_text) >= _PREVIEW_MAX_CHARS:
        return True
    return REDACTION_MARKER.search(output_text) is not None


def _parse_test_output(text: str) -> tuple[int | None, int | None, int | None]:
    """Parse pytest/jest/vitest result lines from a single output blob.

    Returns ``(passed, failed, errors)``; each is the *maximum* match found
    (not sum) so multiple result lines (e.g. verbose --tb output) don't
    inflate the count.  Returns ``None`` for a category with no match.
    """
    passed_vals = [int(m) for m in _PASSED_RE.findall(text)]
    failed_vals = [int(m) for m in _FAILED_RE.findall(text)]
    errors_vals = [int(m) for m in _ERRORS_RE.findall(text)]
    return (
        max(passed_vals) if passed_vals else None,
        max(failed_vals) if failed_vals else None,
        max(errors_vals) if errors_vals else None,
    )


def query_turn_evidence(
    db_path: Path,
    session_id: str,
    since_iso: str | None = None,
    bash_captured: bool = True,
) -> Evidence:
    """Build an Evidence view over the observation ledger for a session window.

    Queries ``observations`` for ``session_id``, optionally filtered by
    ``timestamp > since_iso``.  Parses each row's ``tool_name`` + ``content``
    using the exact formats produced by ``post_tool_use.summarize``:

    * Write  → ``"Wrote: <file_path>\\nContent: ..."``
    * Edit   → ``"Edited: <file_path>\\nChange: ..."``
    * Bash   → ``"Ran: <command>\\nOutput: <head+tail preview, ≤2000 chars>"``
    * other  → ``"Tool: <name>\\nInput: ...\\nOutput: ..."``

    CAVEAT: Bash output is bounded at 2000 chars in the ledger, keeping the head
    and the tail with the middle elided.  Commit SHA and test-result extraction
    are therefore heuristic — they see the ends of a long run (where commit
    output and runner verdicts appear) but not its middle.

    Args:
        db_path:   Path to techne.db.
        session_id: The session to query.
        since_iso: Optional ISO-8601 timestamp.  When given, only observations
                   with ``timestamp > since_iso`` are included.  This is the
                   turn-boundary approximation; the ledger has no turn marker.

    Returns:
        Populated Evidence dataclass.
    """
    from techne.core.database import connect

    conn = connect(db_path)
    try:
        if since_iso:
            rows = conn.execute(
                """SELECT tool_name, content FROM observations
                   WHERE session_id = ? AND timestamp > ?
                   ORDER BY timestamp""",
                (session_id, since_iso),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT tool_name, content FROM observations
                   WHERE session_id = ?
                   ORDER BY timestamp""",
                (session_id,),
            ).fetchall()
    finally:
        conn.close()

    observation_count = len(rows)
    any_tool_call = False
    active_tool_call = False
    artifacts: list[str] = []
    artifacts_seen: set[str] = set()
    commits: list[str] = []
    commits_seen: set[str] = set()
    actions: list[str] = []

    # Accumulate test results across Bash rows; aggregate at the end.
    all_passed: list[int] = []
    all_failed: list[int] = []
    all_errors: list[int] = []
    saw_test_output = False
    totals_unreliable = False

    for row in rows:
        tool_name: str = row[0] or ""
        content: str = row[1] or ""

        if tool_name not in NON_DEED:
            any_tool_call = True
            if tool_name not in PASSIVE_TOOLS:
                active_tool_call = True

        if tool_name == "Write":
            # "Wrote: <file_path>\nContent: ..."
            first_line = content.split("\n", 1)[0]
            if first_line.startswith("Wrote: "):
                path = first_line[len("Wrote: ") :].strip()
                if path and path not in artifacts_seen:
                    artifacts.append(path)
                    artifacts_seen.add(path)

        elif tool_name == "Edit":
            # "Edited: <file_path>\nChange: ..."
            first_line = content.split("\n", 1)[0]
            if first_line.startswith("Edited: "):
                path = first_line[len("Edited: ") :].strip()
                if path and path not in artifacts_seen:
                    artifacts.append(path)
                    artifacts_seen.add(path)

        elif tool_name == "Bash":
            # "Ran: <command>\nOutput: <output>"
            first_line = content.split("\n", 1)[0]
            if first_line.startswith("Ran: "):
                cmd = first_line[len("Ran: ") :].strip()
                actions.append(cmd)

            # SHAs are scanned across the WHOLE row, not just the output half.
            # _GIT_SHA_RE requires brackets, so it cannot be tripped by ordinary
            # command text, and scanning everything keeps rows that carry no
            # "Output: " prefix at all — techne_observe writes free-form content
            # under tool_name="Bash", and narrowing this to the output half turned
            # a real commit from `backed` into `unbacked`. A false accusation is
            # the one direction this gate must never take (techne#105).
            for m in _GIT_SHA_RE.finditer(content):
                sha = m.group(1)
                if sha not in commits_seen:
                    commits.append(sha)
                    commits_seen.add(sha)

            # Test totals need the boundary located exactly, so they use the
            # stricter reader, which declines rather than guess.
            output_text = _bash_output(content)
            if output_text is None:
                continue

            # ...and they are read ONLY from a command that actually runs tests.
            # Output alone cannot establish that a test ran: `git log` echoing a
            # commit message, or a linter reporting its own error count, both look
            # identical to a runner's summary once you are only looking at text
            # (techne#440). The command is the evidence that a suite executed.
            ran = content.partition(_BASH_OUTPUT_MARKER)[0]
            command = ran[len("Ran: ") :] if ran.startswith("Ran: ") else ran
            if not _command_runs_tests(command):
                continue

            # Parse test result lines.
            p, f, e = _parse_test_output(output_text)
            if p is not None or f is not None or e is not None:
                saw_test_output = True
                # Judged PER ROW, never across the window. `passed`/`failed` are
                # max()'d over all rows, so asking "was anything cut?" of the union
                # lets an intact row vouch for a bounded one: a 3-test `cargo test`
                # printing "0 failed" would supply the missing count for a separate
                # 900-test run whose own failure line was dropped, and the pair
                # would certify green.
                if _row_totals_unreliable(output_text, p, f, e):
                    totals_unreliable = True
                if p is not None:
                    all_passed.append(p)
                if f is not None:
                    all_failed.append(f)
                if e is not None:
                    all_errors.append(e)

    # Aggregate: max across rows, not sum (see SuiteEvidence docstring).
    passed = max(all_passed) if all_passed else None
    failed = max(all_failed) if all_failed else None
    errors = max(all_errors) if all_errors else None

    if not saw_test_output:
        suite_green = None
    else:
        eff_failed = (failed or 0) + (errors or 0)
        eff_passed = passed or 0
        if eff_failed > 0:
            # A surviving positive failure count is DEFINITIVE and outranks any
            # doubt about what was cut: nothing hiding in an elided middle can
            # turn a red suite green. Checked first because putting the
            # reliability gate ahead of it downgraded a real `unbacked` to
            # `unverifiable` — suppressing a verdict we actually had.
            suite_green = False
        elif totals_unreliable:
            # Some row could certify green without evidence of zero failures, so
            # the aggregate cannot be trusted either. `(failed or 0)` used to read
            # a missing count as zero and return green (techne#438). Decided per
            # row by _row_totals_unreliable; the aggregate is deliberately not
            # consulted, because max()-ing counts across rows lets an intact row
            # supply the number a bounded row lost.
            suite_green = None
        else:
            suite_green = eff_passed > 0

    return Evidence(
        any_tool_call=any_tool_call,
        active_tool_call=active_tool_call,
        artifacts=artifacts,
        commits=commits,
        actions=actions,
        tests=SuiteEvidence(
            passed=passed,
            failed=failed,
            errors=errors,
            suite_green=suite_green,
            totals_unreliable=totals_unreliable,
        ),
        observation_count=observation_count,
        bash_captured=bash_captured,
    )


def check_claims(claims: list[dict], evidence: Evidence) -> list[dict]:
    """Check a list of structured claims against the evidence ledger.

    Status semantics
    ----------------
    * ``"backed"``      — the ledger has positive evidence supporting the claim.
    * ``"unbacked"``    — the ledger lacks or contradicts the claim.  This is
                          the 'no unbacked word' advisory flag: the agent
                          asserted something not found in the deed log.
    * ``"unverifiable"``— the ledger cannot determine the claim (e.g. no
                          parseable test output, unknown claim type, missing
                          required field).

    Supported claim types
    ---------------------
    * ``"completion"`` / ``"did_work"``
        Backed if ``active_tool_call`` or ``artifacts`` are non-empty — i.e. a
        deed happened.  A turn of only passive inspection (Read/Grep/Glob/
        WebFetch) does NOT back completion (techne#105).  With no deed, the
        verdict is ``unverifiable`` when Bash capture is off (a Bash deed would be
        invisible, so absence isn't disproof) and ``unbacked`` when Bash is on (a
        real command would have been recorded, so no-deed is genuine).
    * ``"commit"``
        Backed if ``commits`` is non-empty.  If the optional ``sha`` field is
        given, at least one commit must start with it.
    * ``"tests_green"``
        Backed if ``tests.suite_green is True``; unbacked if ``False``;
        unverifiable if ``None`` (no parseable output).
    * ``"ran_action"``  (required field: ``match``)
        Backed if any Bash action contains ``match`` as a substring.
    * ``"wrote_file"``  (required field: ``path``)
        Backed if ``path`` matches any artifact exactly or as a suffix.
    * unknown type → ``unverifiable`` with a note.

    Args:
        claims:   List of claim dicts, each with at least ``"type"``.
        evidence: Evidence object from ``query_turn_evidence``.

    Returns:
        List of verdict dicts: ``{claim, status, detail}``.
    """
    verdicts: list[dict] = []

    for claim in claims:
        claim_type = claim.get("type", "")

        if claim_type in ("completion", "did_work"):
            if evidence.active_tool_call or evidence.artifacts:
                # A deed — a write/edit/command or any non-inspection tool. Passive
                # inspection alone (Read/Grep/Glob/WebFetch, techne#105) does NOT
                # back completion: looking at code is not doing the work.
                status = "backed"
                detail = (
                    f"Agent made {evidence.observation_count} observations; "
                    "a deed (write/edit/command or non-inspection tool call) and/or "
                    "artifacts found in the ledger."
                )
            elif not evidence.bash_captured:
                # No active deed and no artifacts, and Bash is off: a real
                # write/commit/test could have happened via uncaptured Bash, so its
                # absence here is not disproof. Report unverifiable, never a false
                # 'unbacked' on an honest agent (techne#105; matches how commit /
                # tests_green / ran_action treat Bash-off). This subsumes the
                # empty-window case AND the passive-tool-only case — the mere
                # presence of a captured Read must not flip a Bash-heavy honest turn
                # to 'unbacked' (the regression Prudence caught on the first cut).
                status = "unverifiable"
                detail = (
                    "No deed recorded and Bash capture is off — a write/command done "
                    "via Bash would leave no trace, so completion can't be confirmed "
                    "or denied. " + _BASH_OFF_DETAIL
                )
            else:
                # Bash WAS captured and still no deed (only passive inspection or
                # non-deed records) → genuinely unbacked: a real command would have
                # been recorded.
                status = "unbacked"
                detail = (
                    "No deed found in the ledger for this session window — only "
                    "passive inspection (read/grep/glob/fetch) or non-deed records, "
                    "which do not back a completion claim (Bash capture is on, so a "
                    "real command would have been recorded)."
                )

        elif claim_type == "commit":
            # Positive evidence wins regardless of capture config: if we found
            # commits, the claim is backed. The bash-off guard only downgrades the
            # ABSENT case (no commits) from unbacked → unverifiable, so a real
            # commit isn't falsely flagged when Bash isn't recorded.
            sha_filter = claim.get("sha")
            if evidence.commits and sha_filter:
                matching = [c for c in evidence.commits if c.startswith(sha_filter)]
                if matching:
                    status = "backed"
                    detail = f"Commit {matching[0]!r} found (matches sha prefix {sha_filter!r})."
                else:
                    status = "unbacked"
                    detail = (
                        f"Commits found {evidence.commits!r} but none match sha prefix "
                        f"{sha_filter!r}."
                    )
            elif evidence.commits:
                status = "backed"
                detail = f"Git commits found: {evidence.commits!r}."
            elif not evidence.bash_captured:
                status = "unverifiable"
                detail = _BASH_OFF_DETAIL
            else:
                status = "unbacked"
                detail = "No git commits found in Bash output for this session window."

        elif claim_type == "tests_green":
            t = evidence.tests
            if t.suite_green is True:
                status = "backed"
                detail = f"Tests green: passed={t.passed}, failed={t.failed}, errors={t.errors}."
            elif t.suite_green is False:
                status = "unbacked"
                detail = (
                    f"Tests NOT green: passed={t.passed}, failed={t.failed}, errors={t.errors}."
                )
            elif not evidence.bash_captured:
                status = "unverifiable"
                detail = _BASH_OFF_DETAIL
            elif t.totals_unreliable:
                status = "unverifiable"
                detail = (
                    f"Parsed passed={t.passed} but no failure count, from output that was "
                    "elided in the ledger. A green run and a run whose failure line was "
                    "dropped look identical here, so this cannot be called green."
                )
            else:
                status = "unverifiable"
                detail = (
                    "No parseable test output in Bash observations. Tests were not "
                    "run, the runner's format is unrecognized, its totals fell in "
                    "the elided middle of a long output, or the row's command/output "
                    "boundary was ambiguous."
                )

        elif claim_type == "ran_action":
            match_str = claim.get("match", "")
            if not match_str:
                status = "unverifiable"
                detail = "ran_action claim is missing the required 'match' field."
            elif any(match_str in a for a in evidence.actions):
                matching = [a for a in evidence.actions if match_str in a]
                status = "backed"
                detail = f"Found matching action: {matching[0]!r}."
            elif not evidence.bash_captured:
                status = "unverifiable"
                detail = _BASH_OFF_DETAIL
            else:
                status = "unbacked"
                detail = (
                    f"No Bash action containing {match_str!r} found. "
                    f"Actions seen: {evidence.actions[:5]!r}."
                )

        elif claim_type == "wrote_file":
            path_str = claim.get("path", "")
            if not path_str:
                status = "unverifiable"
                detail = "wrote_file claim is missing the required 'path' field."
            else:
                matching = [a for a in evidence.artifacts if a == path_str or a.endswith(path_str)]
                if matching:
                    status = "backed"
                    detail = f"Artifact {matching[0]!r} found in the ledger."
                else:
                    status = "unbacked"
                    detail = (
                        f"Path {path_str!r} not found in artifacts. "
                        f"Artifacts seen: {evidence.artifacts[:5]!r}."
                    )

        else:
            status = "unverifiable"
            detail = f"Unknown claim type {claim_type!r} — cannot check against the ledger."

        verdicts.append({"claim": claim, "status": status, "detail": detail})

    return verdicts


# ---- techne#541: the length test's pressure, as a probe rather than prose ----


def probe_length_test_pressure(db_path: Path) -> dict:
    """Measure the population `_output_was_bounded`'s length clause rests on.

    THE POINT OF THIS FUNCTION IS THAT NOBODY RE-DERIVES ITS NUMBERS BY HAND.
    `_output_was_bounded`'s note carried its figures in prose, and every error in
    five review rounds was the same shape: a predicate stated in words that a
    reader then hand-rolled to check. Twice a step the gate already owns was
    reimplemented instead of called — measuring `content` where the gate reads
    `_bash_output(content)`, and hand-rolling the `Ran: ` split — and both
    produced numbers that did not reproduce (techne#441 r5, techne#541).

    So every clause here calls the SAME helper the gate calls. There is no
    second implementation of the extraction to drift from the first.

    Returns, over the ledger's Bash rows:

    ``bash_rows``
        Rows examined.
    ``runner_rows``
        Command actually invokes a test runner (`_command_runs_tests` on the
        command head, never a substring — `git commit -m "fix the pytest flake"`
        is not a runner row).
    ``conjunction_rows``
        Runner + parseable + failure-free. The three clauses
        `_row_totals_unreliable` applies BEFORE consulting `_output_was_bounded`.
        These are ORDINARY — they are what a green suite run looks like.
    ``decided_by_redaction_alone``
        Of those, the ones short and unmarked, so the redaction clause alone
        decides the verdict.
    ``marker_carrying_runner_rows``
        **THE OBSERVABLE TO WATCH.** Runner rows whose output carries the
        redaction marker, regardless of parseability or outcome. Re-measuring
        ``conjunction_rows`` is the wrong instruction — it is a stock that grows
        monotonically as the repo acquires tests, every member already known NOT
        to be redacted, so it moves while the risk does not. This one moves only
        when the hazard moves. **If it climbs, REPLACE the length test rather
        than re-justify it.**
    ``flips``
        Rows where the redaction clause alone flips the verdict — elision-free,
        under budget, marker present. All four clauses matter: dropping the
        length clause inflates this, because a flip is only a flip when the
        clauses above it did not already return True.

    Read-only. The ledger grows, so every count is as-of, which is exactly why
    this is a function and not a comment.
    """
    from techne.core.stores import plain_connect

    counts = dict.fromkeys(
        (
            "bash_rows",
            "runner_rows",
            "conjunction_rows",
            "decided_by_redaction_alone",
            "marker_carrying_runner_rows",
            "flips",
        ),
        0,
    )
    conn = plain_connect(db_path)
    try:
        rows = conn.execute("SELECT content FROM observations WHERE tool_name = 'Bash'").fetchall()
    finally:
        conn.close()

    for (content,) in ((r["content"],) for r in rows):
        output_text = _bash_output(content or "")
        if output_text is None:
            continue
        counts["bash_rows"] += 1

        marked = REDACTION_MARKER.search(output_text) is not None
        short = len(output_text) < _PREVIEW_MAX_CHARS
        if not was_elided(output_text) and short and marked:
            counts["flips"] += 1

        # The command, extracted the way the CALLER extracts it, character for
        # character — hand-rolling this split is what produced the earlier
        # irreproducible 475. (`_skip_env_prefix` is deliberately NOT applied here:
        # it lives inside `_command_runs_tests`, and applying it again would be a
        # second implementation of a step the gate already owns.)
        ran = (content or "").partition(_BASH_OUTPUT_MARKER)[0]
        command = ran[len("Ran: ") :] if ran.startswith("Ran: ") else ran
        if not _command_runs_tests(command):
            continue
        counts["runner_rows"] += 1
        if marked:
            counts["marker_carrying_runner_rows"] += 1

        p, f, e = _parse_test_output(output_text)
        if p is None and f is None and e is None:
            continue
        if (f or 0) > 0 or (e or 0) > 0:
            continue
        counts["conjunction_rows"] += 1
        if short and not marked:
            counts["decided_by_redaction_alone"] += 1

    return counts
