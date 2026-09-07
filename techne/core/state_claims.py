"""State-claim gate (techne#584) — the conversational half of the evidence gate.

``techne/core/evidence.py`` checks **deeds**: "I committed", "tests pass", "I
wrote X", all verifiable against the observation ledger.  It scopes out the
other half of an agent's output — **assertions about project state**, made in
conversation, where there is no diff, no suite, and no verdict for Prudence to
read.

The measurement that motivated this (techne#584, one session, 2026-08-16): four
wrong state claims in a day, **three caught by the operator**.  Every one was
cheaply queryable at the moment it was made — ownership from the coord bus,
issue/PR state from ``gh``, "we already do X" from our own source.  None needed
judgement; each needed one query that was not run.  So the fix is an external
check, not a firmer rule: the "never guess" instruction was in context the whole
time and was violated anyway.

Advisory only — flag, never block, exactly as ``check_claims()`` is today.

THE LOAD-BEARING RULE, inherited from evidence.py's ``bash_captured`` handling:
**absence of evidence is not disproof.**  A claim the bus never mentions, an
issue ``gh`` could not reach, a file that is not there — all resolve to
``unverifiable``, never ``unbacked``.  ``unbacked`` means the source was read
and it CONTRADICTS the claim.  Collapsing those two is how a gate starts
crying wolf, and a gate the reader learns to skip is worse than none.

External access is injected rather than imported so this module stays testable
without a network or a repo: see ``StateContext.gh`` and ``StateContext.read``.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Bound the bus scan.  Ownership is announced when work is claimed, so an
# unbounded history mostly adds stale claimants; the newest claim wins anyway.
COORD_SCAN_LIMIT: int = 500

# Bound what a capability check will read, so a claim can never pull a large
# file into a verdict detail.
CAPABILITY_READ_MAX_BYTES: int = 512_000

# `gh` is network I/O on the critical path of a turn.  A slow check that blocks
# an answer is worse than an absent one, so it fails to `unverifiable` fast.
GH_TIMEOUT_S: int = 10

VALID_STATES: frozenset[str] = frozenset({"open", "closed", "merged"})

STATE_CLAIM_TYPES: frozenset[str] = frozenset({"ownership", "artifact_state", "capability"})


def _run_gh(argv: list[str]) -> tuple[int, str]:
    """Default `gh` runner.  Returns (returncode, combined output)."""
    if shutil.which("gh") is None:
        return (127, "gh not on PATH")
    try:
        p = subprocess.run(
            ["gh", *argv],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return (124, f"gh timed out after {GH_TIMEOUT_S}s")
    except OSError as exc:  # pragma: no cover - defensive
        return (126, f"could not execute gh: {exc}")
    return (p.returncode, (p.stdout or "") + (p.stderr or ""))


def _read_text(path: Path) -> str | None:
    """Default file reader.  Returns None when unreadable — never raises."""
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > CAPABILITY_READ_MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


@dataclass
class StateContext:
    """Everything a state claim can be checked against.

    ``gh`` and ``read`` are injected so the checks are exercisable without a
    network or a checkout — the same reason ``Evidence`` is a plain projection
    rather than a live query.
    """

    db_path: Path | None = None
    repo_root: Path | None = None
    gh: Callable[[list[str]], tuple[int, str]] = _run_gh
    read: Callable[[Path], str | None] = _read_text


def _verdict(claim: dict, status: str, detail: str) -> dict:
    return {"claim": claim, "status": status, "detail": detail}


# --------------------------------------------------------------------------
# ownership — "that issue/branch/PR is mine"
# --------------------------------------------------------------------------
def _check_ownership(claim: dict, ctx: StateContext) -> dict:
    """Check a claimed owner against the coord bus.

    Claim fields: ``subject`` (branch name, ``repo#N``, worktree path — any
    string the claim was announced with) and ``owner`` (who the agent says owns
    it).  Announcements are free prose ("Claiming branch fix/51-... in ..."), so
    the match is a case-insensitive substring over ``coord_messages.body``.

    MOST RECENT CLAIM WINS, and that is the point rather than an implementation
    detail: the failure this exists for was "techne#570 is mine" when the work
    had come by handoff and the reassignment was on the bus the whole time.
    Taking the newest claimant is what makes a handoff visible.
    """
    subject = str(claim.get("subject", "")).strip()
    owner = str(claim.get("owner", "")).strip()
    if not subject or not owner:
        return _verdict(
            claim,
            "unverifiable",
            "ownership claims need both 'subject' and 'owner'.",
        )
    if ctx.db_path is None:
        return _verdict(claim, "unverifiable", "no db_path — the coord bus is unreadable.")

    try:
        conn = sqlite3.connect(f"file:{ctx.db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return _verdict(claim, "unverifiable", f"could not open the coord bus: {exc}")
    try:
        rows = conn.execute(
            "SELECT from_name, from_session, body, created_at FROM coord_messages "
            "ORDER BY id DESC LIMIT ?",
            (COORD_SCAN_LIMIT,),
        ).fetchall()
    except sqlite3.Error as exc:
        return _verdict(claim, "unverifiable", f"could not read the coord bus: {exc}")
    finally:
        conn.close()

    needle = subject.lower()
    hits = [r for r in rows if needle in (r[2] or "").lower()]
    if not hits:
        # Absence is not disproof. The bus is where claims are ANNOUNCED, and
        # plenty of real work is never announced there.
        return _verdict(
            claim,
            "unverifiable",
            f"the coord bus never mentions {subject!r} in the last "
            f"{COORD_SCAN_LIMIT} messages, so ownership cannot be established "
            "either way.",
        )

    newest = hits[0]
    claimant = (newest[0] or newest[1] or "").strip()
    if claimant.lower() == owner.lower():
        return _verdict(
            claim,
            "backed",
            f"the most recent coord message mentioning {subject!r} is from "
            f"{claimant!r} ({newest[3]}); {len(hits)} mention(s) total.",
        )
    others = sorted({(r[0] or r[1] or "").strip() for r in hits})
    return _verdict(
        claim,
        "unbacked",
        f"the coord bus says {claimant!r} most recently claimed {subject!r} "
        f"({newest[3]}), not {owner!r}. Claimants seen: {', '.join(others)}.",
    )


# --------------------------------------------------------------------------
# artifact_state — "that PR is merged", "that issue is closed"
# --------------------------------------------------------------------------
_REF_RE = re.compile(r"^(?:([\w.-]+)/([\w.-]+)|([\w.-]+))?#(\d+)$")


def _parse_ref(claim: dict) -> tuple[str | None, int | None, str | None]:
    """Return (repo, number, error). Accepts explicit fields or a ``ref``."""
    repo = str(claim.get("repo", "")).strip()
    number = claim.get("number")
    if repo and number is not None:
        try:
            return repo, int(number), None
        except (TypeError, ValueError):
            return None, None, f"'number' is not an integer: {number!r}"

    ref = str(claim.get("ref", "")).strip()
    if not ref:
        return (
            None,
            None,
            "artifact_state claims need 'ref' (e.g. 'owner/repo#12') or 'repo'+'number'.",
        )
    m = _REF_RE.match(ref)
    if not m:
        return None, None, f"could not parse ref {ref!r}; want 'owner/repo#12' or 'repo#12'."
    owner, name, bare, num = m.groups()
    if owner and name:
        return f"{owner}/{name}", int(num), None
    if bare:
        # A repo name with no owner. Guessing an org is exactly the kind of
        # inference this gate exists to stop, so refuse rather than assume.
        return (
            None,
            None,
            (
                f"ref {ref!r} names a repo but no owner; pass 'owner/repo#N' or an "
                "explicit 'repo' field."
            ),
        )
    return None, None, f"ref {ref!r} has no repo; pass 'owner/repo#N' or 'repo'+'number'."


def _check_artifact_state(claim: dict, ctx: StateContext) -> dict:
    """Check an issue/PR's state against GitHub.

    Claim fields: ``ref`` (``owner/repo#N``) or ``repo``+``number``, and
    ``state`` — one of open / closed / merged.  ``merged`` is checked
    separately from ``closed`` because GitHub reports a merged PR as closed,
    and treating them as the same is how "it's closed" hides "it never landed".
    """
    want = str(claim.get("state", "")).strip().lower()
    if want not in VALID_STATES:
        return _verdict(
            claim,
            "unverifiable",
            f"artifact_state needs 'state' in {sorted(VALID_STATES)}; got {want!r}.",
        )
    repo, number, err = _parse_ref(claim)
    if err:
        return _verdict(claim, "unverifiable", err)

    rc, out = ctx.gh(
        [
            "api",
            f"repos/{repo}/issues/{number}",
            "--jq",
            "{state:.state, merged:(.pull_request.merged_at != null), pr:(.pull_request != null)}",
        ]
    )
    if rc != 0:
        # Could not reach the source. NOT a contradiction of the claim.
        return _verdict(
            claim,
            "unverifiable",
            f"could not read {repo}#{number} from GitHub (rc={rc}): {out.strip()[:200]}",
        )
    text = out.strip()
    actual = (
        "closed"
        if '"state":"closed"' in text.replace(" ", "")
        else ("open" if '"state":"open"' in text.replace(" ", "") else "")
    )
    if not actual:
        return _verdict(
            claim,
            "unverifiable",
            f"could not parse GitHub's answer for {repo}#{number}: {text[:200]}",
        )
    is_pr = '"pr":true' in text.replace(" ", "")
    merged = '"merged":true' in text.replace(" ", "")
    if merged:
        actual = "merged"

    kind = "PR" if is_pr else "issue"
    if actual == want:
        return _verdict(claim, "backed", f"{repo}#{number} is a {kind} and is {actual}.")
    if want == "closed" and actual == "merged":
        # Defensible but worth naming rather than silently passing.
        return _verdict(
            claim,
            "backed",
            f"{repo}#{number} is a merged {kind} (GitHub also reports it closed).",
        )
    return _verdict(
        claim,
        "unbacked",
        f"{repo}#{number} is a {kind} and is {actual}, not {want}.",
    )


# --------------------------------------------------------------------------
# capability — "we already do X" / "X is missing"
# --------------------------------------------------------------------------
def _check_capability(claim: dict, ctx: StateContext) -> dict:
    """Check a 'we already do X' / 'X is missing' claim against our own source.

    Claim fields: ``path`` (repo-relative or absolute), ``pattern``, optional
    ``present`` (default True — the claim is that the thing IS there), and
    optional ``regex`` (default False; substring otherwise, because a claim
    should not have to be a well-formed regex to be checkable).
    """
    pattern = str(claim.get("pattern", ""))
    path_s = str(claim.get("path", "")).strip()
    if not pattern or not path_s:
        return _verdict(
            claim,
            "unverifiable",
            "capability claims need both 'path' and 'pattern'.",
        )
    present = claim.get("present", True)
    if not isinstance(present, bool):
        return _verdict(claim, "unverifiable", f"'present' must be a boolean; got {present!r}.")

    path = Path(path_s)
    if not path.is_absolute():
        if ctx.repo_root is None:
            return _verdict(
                claim,
                "unverifiable",
                f"{path_s!r} is relative and no repo_root is set.",
            )
        path = ctx.repo_root / path

    text = ctx.read(path)
    if text is None:
        # The file may simply not exist here. That is not evidence about the
        # capability — a missing file cannot disprove "we do X".
        return _verdict(
            claim,
            "unverifiable",
            f"could not read {path} — nothing can be concluded about the claim.",
        )

    if claim.get("regex", False):
        try:
            found = re.search(pattern, text) is not None
        except re.error as exc:
            return _verdict(claim, "unverifiable", f"bad regex {pattern!r}: {exc}")
    else:
        found = pattern in text

    if found == present:
        shape = "present" if present else "absent"
        return _verdict(claim, "backed", f"{pattern!r} is {shape} in {path}, as claimed.")
    if present:
        return _verdict(
            claim, "unbacked", f"{pattern!r} is NOT in {path}, but the claim says it is."
        )
    return _verdict(claim, "unbacked", f"{pattern!r} IS in {path}, but the claim says it is not.")


_CHECKERS: dict[str, Callable[[dict, StateContext], dict]] = {
    "ownership": _check_ownership,
    "artifact_state": _check_artifact_state,
    "capability": _check_capability,
}


def check_state_claims(claims: list[dict], ctx: StateContext) -> list[dict]:
    """Check state claims, returning evidence.py's verdict shape.

    Verdicts are ``{claim, status, detail}`` with status backed / unbacked /
    unverifiable, so a caller can merge these with ``check_claims()`` output
    and count unbacked across both without special-casing.
    """
    verdicts: list[dict] = []
    for claim in claims:
        checker = _CHECKERS.get(str(claim.get("type", "")))
        if checker is None:
            verdicts.append(
                _verdict(claim, "unverifiable", f"Unknown state claim type {claim.get('type')!r}.")
            )
            continue
        try:
            verdicts.append(checker(claim, ctx))
        except Exception as exc:  # pragma: no cover - the gate must never raise
            # A gate that can crash a turn is worse than a gate that abstains.
            verdicts.append(
                _verdict(claim, "unverifiable", f"check raised {type(exc).__name__}: {exc}")
            )
    return verdicts
