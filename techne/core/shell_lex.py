"""One model of shell quoting for every reader of a Bash command (techne#1339).

The capture filter (`hooks/post_tool_use.py`), the evidence gate
(`core/evidence.py`) and grep-assist (`hooks/pre_tool_use.py`) all need to know
which characters of a command the shell treats as code. They used to keep four
scanners each with its own idea of quoting, and when one learned backslash
escapes and the others did not, a heredoc body read as commands and the gate
backed a test run that never happened (PR 1353 review). Everything here derives
from `classify`, so the readers cannot drift apart again.

Not a shell parser: no expansion, no `case`, no nested quoting inside `$(...)`.
It answers one question per character, and errs toward "code" where bash is
ambiguous, because every caller is safer seeing a command than missing one.
"""

from __future__ import annotations

import re

CODE = "c"  # unquoted, unescaped, outside a comment
SINGLE = "s"  # inside '...', both quotes included
ANSI = "a"  # inside $'...', the $ and both quotes included
DOUBLE = "d"  # inside "...", both quotes included
ESCAPE = "e"  # a backslash escape, both characters
CONT = "x"  # a backslash-newline line continuation, both characters
COMMENT = "k"  # an unquoted # at the start of a word, to the end of the line

# Characters after which an unquoted `#` starts a comment.
_WORD_START = frozenset(" \t\n;|&()<>")


def classify(text: str, state: str | None = None) -> tuple[str, str | None]:
    """A class letter per character of *text*, and the quote open at its end.

    *state* is the quote open where *text* starts (None, ``"'"``, ``'"'`` or
    ``"$'"``), or ``"\\"`` when the previous line ended in a line continuation,
    so a caller can thread it line by line and get the same answer as scanning
    the whole text. A comment never carries over: it ends at a newline or at the
    end of *text*.
    """
    out: list[str] = []
    # After a continuation the line starts mid-word: `x\<newline># y` is `x# y`.
    continued = state == "\\"
    q = None if continued else state
    comment = False
    trailing_escape = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if comment:
            if ch == "\n":
                comment = False
                out.append(CODE)
            else:
                out.append(COMMENT)
            i += 1
            continue
        if q == "'":
            out.append(SINGLE)
            if ch == "'":
                q = None
            i += 1
            continue
        if q == "$'":
            if ch == "\\" and i + 1 < n:
                out += [ANSI, ANSI]  # `\'` does not close $'...'
                i += 2
                continue
            out.append(ANSI)
            if ch == "'":
                q = None
            i += 1
            continue
        if ch == "\\":
            if text[i + 1 : i + 2] == "\n":
                out += [CONT, CONT]
                i += 2
            elif q == '"' and text[i + 1 : i + 2] not in ("$", "`", '"', "\\"):
                out.append(DOUBLE)  # inside double quotes most escapes are literal
                i += 1
            elif i + 1 < n:
                out += [ESCAPE, ESCAPE]
                i += 2
            else:
                out.append(ESCAPE)  # a trailing backslash: a continuation if a line follows
                trailing_escape = q is None
                i += 1
            continue
        if q == '"':
            out.append(DOUBLE)
            if ch == '"':
                q = None
            i += 1
            continue
        if ch == "'":
            q = "'"
            out.append(SINGLE)
        elif ch == '"':
            q = '"'
            out.append(DOUBLE)
        elif ch == "$" and text[i + 1 : i + 2] == "'":
            q = "$'"
            out += [ANSI, ANSI]
            i += 2
            continue
        elif ch == "#" and (
            (i == 0 and not continued)
            or (i > 0 and out[i - 1] == CODE and text[i - 1] in _WORD_START)
        ):
            comment = True
            out.append(COMMENT)
        else:
            out.append(CODE)
        i += 1
    return "".join(out), ("\\" if trailing_escape else q)


def split_segments(command: str) -> list[str]:
    """Top-level segments of a command, split on ``&&`` ``||`` ``|&`` ``;`` ``|``
    newline and a lone ``&`` where those are code. Comments and line
    continuations are dropped. A lone ``&`` next to a redirect (``2>&1``,
    ``&>f``, ``<&3``) does not split.
    """
    cls, _ = classify(command)
    segs: list[str] = []
    buf: list[str] = []
    i, n = 0, len(command)
    while i < n:
        ch, c = command[i], cls[i]
        if c in (COMMENT, CONT):
            i += 1
            continue
        if c == CODE:
            two = command[i : i + 2]
            if two in ("&&", "||", "|&") and cls[i + 1 : i + 2] == CODE:
                segs.append("".join(buf))
                buf = []
                i += 2
                continue
            lone_amp = (
                ch == "&"
                and (i == 0 or command[i - 1] not in "<>")
                and command[i + 1 : i + 2] != ">"
            )
            if ch in (";", "|", "\n") or lone_amp:
                segs.append("".join(buf))
                buf = []
                i += 1
                continue
        buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return [s.strip() for s in segs if s.strip()]


def has_substitution(command: str) -> bool:
    """A ``$(...)`` or backtick the shell would run (code or double-quoted, not
    escaped), or a ``<(``/``>(`` in code. ``$((`` is arithmetic, not a command."""
    cls, _ = classify(command)
    for i, ch in enumerate(command):
        c = cls[i]
        if c not in (CODE, DOUBLE):
            continue
        if ch == "`":
            return True
        if command[i : i + 2] == "$(" and command[i : i + 3] != "$((":
            return True
        if c == CODE and command[i : i + 2] in ("<(", ">(") and cls[i + 1] == CODE:
            return True
    return False


def words(seg: str) -> list[str]:
    """Shell words of one segment: split on unquoted whitespace, quotes removed,
    escapes resolved, continuations and comments dropped. No expansion."""
    cls, _ = classify(seg)
    out: list[str] = []
    cur: list[str] = []
    have = False  # a word is open, even if empty ('' is a word)
    ansi_open = False
    i, n = 0, len(seg)
    while i < n:
        ch, c = seg[i], cls[i]
        if c in (COMMENT, CONT):
            i += 1
            continue
        if c == CODE and ch.isspace():
            if have:
                out.append("".join(cur))
                cur, have = [], False
            i += 1
            continue
        have = True
        if c == ESCAPE:
            if ch == "\\" and i + 1 < n and cls[i + 1] == ESCAPE:
                cur.append(seg[i + 1])
                i += 2
            else:
                cur.append(ch)
                i += 1
            continue
        if c == SINGLE and ch == "'":
            i += 1
            continue
        if c == DOUBLE and ch == '"':
            i += 1
            continue
        if c == ANSI:
            if not ansi_open:
                ansi_open = True
                i += 2  # the opening $'
                continue
            if ch == "\\" and i + 1 < n:
                cur.append(seg[i + 1])
                i += 2
                continue
            if ch == "'":
                ansi_open = False  # the closing quote
                i += 1
                continue
        cur.append(ch)
        i += 1
    if have:
        out.append("".join(cur))
    return out


def mask_line(line: str, state: str | None) -> tuple[str, str | None]:
    """*line* with every non-code character replaced by NUL, and the quote state
    at its end. Used to LOCATE syntax (a `<<` that is code opens a heredoc; one
    inside quotes or a comment does not) while matching on the raw line."""
    cls, state = classify(line, state)
    return "".join(ch if c == CODE else "\x00" for ch, c in zip(line, cls)), state


# `<<` or `<<-`, an optionally quoted or backslash-escaped tag, and a tag charset
# wide enough for real delimiters (`PY-1`, `EOF.2`). The lookarounds keep a
# herestring (`<<<word`) from matching: without them the scan finds the `<<` at
# offset 1 of `<<<` and strips to end of command, swallowing a real run below.
HEREDOC_OPEN = re.compile(
    r"(?<!<)<<(-?)(?!<)\s*(?:\\|(['\"]))?([A-Za-z_][A-Za-z0-9_.\-]*)(?(2)\2|)"
)


def strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc bodies, keeping the lines that are actually commands.

    Quote-awareness is not enough here. ``split_segments`` splits on ``\\n``,
    and a heredoc body is not quoted, so every line of it becomes a segment and a
    body line beginning with a runner name becomes a command head. A script
    written to disk with ``cat > probe.py <<'PY'`` whose text contains a
    ``pytest`` line would then read as a test run, and whatever count-shaped text
    that script printed would be admitted as test evidence.

    Quote state is threaded across KEPT lines only, so a stray apostrophe in a
    body (``# don't remove this``) cannot open a quote in the commands after it.
    """
    lines = command.split("\n")
    kept: list[str] = []
    i = 0
    state: str | None = None
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        # Match on the RAW line, because a heredoc tag is usually quoted (`<<'PY'`)
        # and masking would blank the tag. The mask only locates: a `<<` that is
        # not code (`echo "a << b"`, `# cat <<EOF`) opens nothing.
        masked, state = mask_line(line, state)
        opens = [
            (m.group(1) == "-", m.group(3))
            for m in HEREDOC_OPEN.finditer(line)
            if masked[m.start()] != "\x00"
        ]
        i += 1
        # A line may open several heredocs; their bodies follow in order.
        for dash, tag in opens:
            # For plain `<<TAG` the terminator must be the whole line; only `<<-TAG`
            # permits leading tabs. Being lenient would end a body early.
            while i < len(lines) and (lines[i].lstrip("\t") if dash else lines[i]) != tag:
                i += 1
            i += 1  # skip the terminator itself
    return "\n".join(kept)
