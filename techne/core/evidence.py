"""Evidence gate (techne#105) — read-only view over the observation ledger.

Surfaces what an agent actually DID (writes, git commits, bash commands, test
results) so claims like "I committed", "tests pass", "I wrote X" can be verified
against the ledger rather than trusted on faith.  Advisory only — flag, not block.

Free-text claim extraction is explicitly OUT of scope for this slice; callers
must pass structured claim dicts.  See check_claims() for the supported types.

IMPORTANT — Bash capture dependency: commits, shell actions, and test results are
all parsed from Bash observations, but Bash capture is OFF by default in techne
(config.hooks.capture_bash=False). When it's off, those deeds aren't in the ledger
at all, so a real commit/test-run is *absent*, not *disproven*. Callers pass
``bash_captured`` (from config) so check_claims() returns ``unverifiable`` rather
than falsely flagging an honest agent as ``unbacked``. Artifacts (Write/Edit) and
any_tool_call work regardless, since those tools are captured by default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

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
    "Bash capture is off (config.hooks.capture_bash=False), so commits, test "
    "results, and shell actions are not recorded — this claim cannot be verified "
    "from the ledger (absent, not disproven). Enable capture_bash to check it."
)


@dataclass
class SuiteEvidence:
    """Best-effort parse of test runner output from Bash observations.

    CAVEAT: Bash output is bounded at 2000 chars in the ledger (see
    post_tool_use.summarize).  The preview keeps the head AND the tail, so a
    runner's trailing result line survives; only the middle of a long run is
    elided, and the preview says so.  The heuristic still only handles
    pytest/jest/vitest-style result lines, and a runner that reports its totals
    from the *middle* of its output can still be missed.
    ``None`` means "couldn't parse" — report honestly, not optimistically.

    Aggregation: when multiple Bash rows contain test output (e.g. re-runs),
    we take the *maximum* count seen, not the sum.  Max is less wrong for
    re-run scenarios; sum would double-count.  Document this in callers.
    """

    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    suite_green: bool | None = None


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
            parts = content.split("\n", 1)
            first_line = parts[0]
            rest = parts[1] if len(parts) > 1 else ""

            if first_line.startswith("Ran: "):
                cmd = first_line[len("Ran: ") :].strip()
                actions.append(cmd)

            # Strip the "Output: " prefix to get the raw bash output.
            output_text = rest[len("Output: ") :] if rest.startswith("Output: ") else rest

            # Extract git commit SHAs.
            for m in _GIT_SHA_RE.finditer(output_text):
                sha = m.group(1)
                if sha not in commits_seen:
                    commits.append(sha)
                    commits_seen.add(sha)

            # Parse test result lines.
            p, f, e = _parse_test_output(output_text)
            if p is not None or f is not None or e is not None:
                saw_test_output = True
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

    if saw_test_output:
        eff_failed = (failed or 0) + (errors or 0)
        eff_passed = passed or 0
        suite_green: bool | None = eff_failed == 0 and eff_passed > 0
    else:
        suite_green = None

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
            else:
                status = "unverifiable"
                detail = (
                    "No parseable test output in Bash observations. "
                    "Tests were not run, the runner's format is unrecognized, or "
                    "its totals fell in the elided middle of a long output."
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
