#!/usr/bin/env python3
"""PreToolUse hook — graph-augment the agent's own Grep with Codex (techne#206).

When the agent issues a Grep tool call AND the opt-in flag
``hooks.codex_grep_assist`` is ON, this hook runs a Codex symbol search for the
search pattern and injects the matching symbols into the model's context via
``hookSpecificOutput.additionalContext``.

Discipline (same fail-safe posture as post_tool_use / session_start):
  - NEVER blocks the tool. EVERY path exits 0.
  - stdout is ONLY ever the PreToolUse protocol JSON; when there's nothing to
    add, nothing is printed. ALL logging/errors go to stderr.
  - Any error (config load, missing codex.db, empty search) degrades to a no-op.

Protocol shape (confirmed: PreToolUse honors additionalContext without blocking):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "allow",
                          "additionalContext": "..."}}
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

# Tools whose pattern is a symbol-ish search worth augmenting with the code graph.
# Glob is excluded: its "pattern" is a file glob (e.g. **/*.py), not a symbol name,
# so feeding it to the symbol FTS yields noise/nothing (techne#206 dogfood).
#
# BASH IS HERE BECAUSE MATCHING ONLY `Grep` MADE THIS RAIL UNREACHABLE IN PRACTICE
# (techne#596). The rail was enabled, registered, and never fired once across a
# full session of searching this codebase — because every search was `grep` inside
# a `Bash` call, which is the natural reach when you are already running shell
# commands. That is not evasion: the guard's precondition was "the agent chose the
# Grep tool", and falsifying it was free, accidental and invisible. The same shape
# as the defects in #570/#581/#582, sitting in our own enforcement layer.
ASSIST_TOOLS = {"Grep", "Bash"}

#: Shell commands whose first non-flag argument is a search PATTERN. Deliberately
#: not the whole read-only set from post_tool_use — `find`/`ls`/`cat` take paths,
#: and feeding a path to the symbol FTS is the Glob mistake in a new costume.
_SEARCH_HEADS = frozenset({"grep", "egrep", "fgrep", "rg", "ag"})

#: Options that consume the NEXT token as their value, so that token is NOT the
#: pattern. Measured omission (#596 r1): `grep -D skip search_symbols .` injected
#: symbols for `skip`, and `--exclude-dir node_modules search_symbols` lost the
#: real pattern entirely. Attached forms (`-A5`, `--include=*.py`) consume nothing
#: extra and are handled by the plain `-` prefix test.
_VALUE_OPTS_SHORT = frozenset("ABCDdfme")
_VALUE_OPTS_LONG = frozenset(
    {
        "--after-context",
        "--before-context",
        "--context",
        "--devices",
        "--directories",
        "--exclude",
        "--exclude-dir",
        "--exclude-from",
        "--file",
        "--include",
        "--label",
        "--max-count",
        "--binary-files",
        "--color",
        "--colour",
        "--group-separator",
        "--regexp",
    }
)

#: A pattern worth sending to the symbol index: a bare identifier, 3+ chars.
#: CONSERVATIVE ON PURPOSE — this rail's failure mode is noise, and noise is what
#: got the #206 original scoped down. Anything with a metacharacter, slash or dot
#: is a regex/path/glob, not a symbol name, so it is left alone. A missed
#: augmentation costs nothing; a wrong one trains the reader to ignore the block.
_SYMBOLISH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")


def _takes_a_value(tok: str) -> bool:
    """True if `tok` is an option whose value is the NEXT token."""
    if tok.startswith("--"):
        return tok in _VALUE_OPTS_LONG  # `--opt=value` never reaches here
    # A short cluster consumes a value only if its LAST letter takes one:
    # `-rn` does not, `-rm` does (the `m` of --max-count).
    return len(tok) > 1 and tok[-1] in _VALUE_OPTS_SHORT


def _bash_search_pattern(command: str) -> str | None:
    """The symbol-shaped search pattern in a shell command, or None.

    Reuses the hardened shell handling rather than re-deriving it:
    ``_strip_heredoc_bodies`` (so a `grep` inside a heredoc BODY is not read as a
    command — techne#544's finding) and ``_split_bash_segments`` (quote-aware, so
    a `grep "a|b"` alternation is not split mid-pattern). Both are already shared
    cross-module by ``core/evidence.py``; a second copy of either would drift from
    the one the evidence gate depends on.

    A LATER PIPELINE SEGMENT WITH NO PATH OPERAND IS FILTERING OUTPUT, NOT
    SEARCHING A TREE, and declining those is what makes this rail usable at all.
    Measured in #596's round-1 review against a real 1237-symbol graph: 11 of 11
    everyday `<cmd> | grep <word>` calls injected context — `git branch -a | grep
    main` put 8 symbols and 1039 characters into the model's context, `env | grep
    PATH` another 8 — while the two genuine symbol searches injected 1 and 2. THE
    NOISE FIRED HARDER THAN THE SIGNAL. The discriminator below was scored, not
    guessed: 0 of 11 noise cases fire, 0 of 8 real searches are lost.
    """
    try:
        from techne.core.evidence import _strip_heredoc_bodies
        from techne.hooks.post_tool_use import _split_bash_segments

        segments = _split_bash_segments(_strip_heredoc_bodies(command))
    except Exception:  # noqa: BLE001 — augmentation is best-effort, never fatal
        return None

    for index, seg in enumerate(segments):
        try:
            tokens = shlex.split(seg)
        except ValueError:  # unbalanced quotes — not parseable, skip
            continue
        if not tokens or os.path.basename(tokens[0]) not in _SEARCH_HEADS:
            continue

        pattern: str | None = None
        operands = 0
        rest = tokens[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok in ("-e", "--regexp") and i + 1 < len(rest):
                pattern = rest[i + 1]  # named explicitly; everything else is a path
                i += 2
                continue
            if tok.startswith("-"):
                i += 2 if (_takes_a_value(tok) and i + 1 < len(rest)) else 1
                continue
            if pattern is None:
                pattern = tok
            else:
                operands += 1  # a file/dir argument — this searches a TREE
            i += 1

        if pattern is None or not _SYMBOLISH.match(pattern):
            return None
        if index > 0 and operands == 0:
            return None  # piped filter, not a search — see the docstring
        return pattern
    return None


def _bash_cd_target(command: str) -> str | None:
    """The directory a leading literal ``cd`` moves to, or None.

    WITHOUT THIS THE HOOK INJECTS THE WRONG REPO'S SYMBOLS (#596 r1, measured):
    with the session cwd in repo alpha, `cd <beta> && grep -rn <alpha symbol> .`
    emitted ALPHA's symbols for a search run in BETA — the techne#206 signal/noise
    defect that techne#209's db-per-repo split exists to prevent, reintroduced
    because Bash carries no `path` field and `cd` was never parsed.

    LITERAL TARGETS ONLY. A path containing `$VAR`, a substitution, or `~` is left
    alone rather than expanded here: guessing a directory wrong scopes the graph to
    an unrelated repo, which is the failure being fixed, and the caller falls back
    to the session cwd — the pre-existing behaviour — when this returns None.
    """
    try:
        from techne.core.evidence import _strip_heredoc_bodies
        from techne.hooks.post_tool_use import _split_bash_segments

        segments = _split_bash_segments(_strip_heredoc_bodies(command))
    except Exception:  # noqa: BLE001
        return None
    if not segments:
        return None
    try:
        tokens = shlex.split(segments[0])
    except ValueError:
        return None
    if len(tokens) != 2 or tokens[0] != "cd":
        return None
    target = tokens[1]
    if any(ch in target for ch in ("$", "`", "~", "*")):
        return None
    return target


# Liveness key namespace: `hook:codex_grep_assist:last_run` (techne#468). Named
# for the RAIL rather than the hook file, because that is the thing an operator
# turns on in config and therefore the thing they expect a status for.
HOOK_NAME = "codex_grep_assist"


def _build_context(pattern: str, results: list[dict]) -> str:
    """Render matching symbols into a compact additionalContext block.

    Mirrors cli.py's `codex search` formatting. Freshness honesty (techne#89):
    any row whose freshness is NOT fresh/unknown carries a visible marker so the
    model knows the graph may be behind the working tree.
    """
    lines = [
        f"## Codex — symbols matching `{pattern}`",
        "> Experimental graph-assist (techne#206); the graph may be stale — verify before relying.",
        "",
    ]
    for s in results:
        freshness = s.get("freshness", "unknown")
        marker = f" [{freshness}]" if freshness not in ("fresh", "unknown") else ""
        lines.append(f"  {s['id']}  [{s['kind']}] {s['name']}  {s.get('signature') or ''}{marker}")
        lines.append(f"    {s['file']}")
    return "\n".join(lines)


def main() -> None:
    # ------------------------------------------------------------------
    # Parse stdin. Malformed → no-op.
    # ------------------------------------------------------------------
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    if tool_name not in ASSIST_TOOLS:
        sys.exit(0)

    # ------------------------------------------------------------------
    # FAST PATH (techne#470). This rail is DEFAULT-OFF, so the overwhelmingly
    # common case is "decide there is no work and exit". Doing that via
    # `import techne.config` cost 100ms of a 145ms invocation, on a hook that
    # fires on every matching tool call.
    #
    # The stdlib-only cache answers the gate question in ~0ms of import, so the
    # disabled case now costs bare interpreter startup and nothing more. A miss
    # (absent / stale / malformed) falls through to the real Config below, which
    # regenerates the cache as a side effect — so this is an optimisation that
    # cannot be wrong, only occasionally slow.
    # ------------------------------------------------------------------
    try:
        from techne.fastconfig import get_bool, load_fast

        fast = load_fast()
        if fast is not None and not get_bool(fast, "hooks.codex_grep_assist"):
            sys.exit(0)
    except Exception:  # noqa: BLE001 — the fast path must never break the hook
        pass

    # ------------------------------------------------------------------
    # Config load can raise (corrupt/unreadable config). A PreToolUse hook that
    # throws would surface as a tool-call failure — degrade to a no-op instead.
    # ------------------------------------------------------------------
    try:
        from techne.config import Config

        config = Config.load()
    except Exception as e:  # noqa: BLE001
        print(f"techne: config load failed, skipping grep-assist: {e}", file=sys.stderr)
        sys.exit(0)

    # Default-off gate: this is opt-in, experimental. Deliberately NOT recorded
    # as a liveness event — a disabled rail is DORMANT by configuration, not a
    # rail that ran and did nothing, and conflating the two is what makes
    # "enabled" indistinguishable from "working" (techne#468).
    if not config.hooks.codex_grep_assist:
        sys.exit(0)

    # From here on the rail is ON, so every exit path names itself. Before this
    # (techne#468) all five returned a silent exit 0 and wrote nothing anywhere,
    # which made this hook's liveness undecidable rather than merely unknown.
    from techne.hooks._liveness import record_hook_run

    # NB: deliberately NOT named `db_path` — that name is rebound below to the
    # per-repo CODEX db, and liveness belongs in the main techne db.
    techne_db = config.db_path

    tool_input = data.get("tool_input", {})
    if tool_name == "Bash":
        # A Bash call carries a whole command line, not a pattern field, and most
        # of them are not searches at all. Extract conservatively or decline —
        # declining is the common case and must stay cheap and silent.
        pattern = _bash_search_pattern(tool_input.get("command", "")) or ""
        if not pattern:
            record_hook_run(techne_db, HOOK_NAME, outcome="noop:not-a-symbol-search")
            sys.exit(0)
    else:
        pattern = tool_input.get("pattern", "")
        if not pattern:
            record_hook_run(techne_db, HOOK_NAME, outcome="noop:no-pattern")
            sys.exit(0)

    # Resolve which repo the agent is actually working in: the Grep's target
    # `path` when given, else the session cwd. This scopes the injected symbols
    # to the repo being searched (techne#209 db-per-repo) — the fix for the
    # #206 signal/noise problem, where a single global graph injected an
    # unrelated repo's symbols.
    #
    # Bash has no `path` field. A leading literal `cd` is parsed because without
    # it the hook emitted the WRONG REPO'S symbols: with the session cwd in repo
    # alpha, `cd <beta> && grep -rn <alpha symbol> .` injected alpha's graph for a
    # search run in beta — techne#206's defect, reintroduced through the door
    # techne#209 was built to close (#596 r1, measured).
    #
    # Everything else still falls through to the session cwd, which is the
    # pre-existing behaviour for a Grep with no `path`. A search pointed at another
    # repo by a bare path operand is NOT followed: resolving that would mean
    # picking one of several operands and guessing, and guessing wrong is the very
    # failure above.
    cd_target = _bash_cd_target(tool_input.get("command", "")) if tool_name == "Bash" else None
    start_path = tool_input.get("path") or cd_target or data.get("cwd") or os.getcwd()

    from techne.hooks._timeout import bounded_hook

    # ------------------------------------------------------------------
    # Resolve the per-repo codex db and run the query inside the wall-clock
    # budget. No indexed db for this repo → no-op. Any failure (search_symbols
    # raises; slow SQLite → HookTimeout) is swallowed and degrades to emitting
    # nothing.
    # ------------------------------------------------------------------
    try:
        from techne.core.codegraph import annotate_freshness, search_symbols
        from techne.core.codex import resolve_repo_db_for_query

        db_path = resolve_repo_db_for_query(config, start_path)
        if db_path is None:
            # The single most likely reason this rail looks "on but does
            # nothing": the repo being searched was never indexed.
            record_hook_run(
                techne_db, HOOK_NAME, outcome="noop:no-repo-db", detail=str(start_path)
            )
            sys.exit(0)
        with bounded_hook(config.hooks.timeout_s, label="pre_tool_use"):
            results = annotate_freshness(
                db_path,
                search_symbols(db_path, pattern, limit=config.hooks.codex_grep_assist_limit),
            )
    except Exception as e:  # noqa: BLE001  — catches HookTimeout too
        print(f"techne: grep-assist degraded: {e}", file=sys.stderr)
        record_hook_run(
            techne_db,
            HOOK_NAME,
            outcome="noop:degraded",
            error={"stage": "search", "msg": f"{type(e).__name__}: {e}"},
        )
        sys.exit(0)

    if not results:
        record_hook_run(techne_db, HOOK_NAME, outcome="noop:no-results")
        sys.exit(0)

    # ------------------------------------------------------------------
    # Emit the protocol JSON (allow + additionalContext) and exit 0.
    # ------------------------------------------------------------------
    try:
        context = _build_context(pattern, results)
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "additionalContext": context,
                    }
                }
            )
        )
    except Exception as e:  # noqa: BLE001
        print(f"techne: grep-assist emit failed: {e}", file=sys.stderr)
        record_hook_run(
            techne_db,
            HOOK_NAME,
            outcome="noop:emit-failed",
            error={"stage": "emit", "msg": f"{type(e).__name__}: {e}"},
        )
        sys.exit(0)

    # The one path where the rail actually did its job. `detail` carries the
    # symbol count so `techne status` can show utilization, not just liveness.
    record_hook_run(techne_db, HOOK_NAME, outcome="ok", detail=f"{len(results)} symbols")
    sys.exit(0)


if __name__ == "__main__":
    main()
