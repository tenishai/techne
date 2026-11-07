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


def classify(text: str, state: tuple | None = None) -> tuple[str, tuple | None]:
    """A class letter per character of *text*, and the scanner state at its end.

    The state is ``(quote, brace_depth, continuation)``, or None at rest: the
    quote open (``"'"``, ``'"'``, ``"$'"``), how many ``${`` are open (a ``#``
    inside one is not a comment), and whether the text ended in a line
    continuation that resumes mid-word (``"mid"``: ``x\\``) or at a word start
    (``"start"``: ``x \\``). Threading it line by line gives the same answer as
    scanning the whole text. A comment never carries over: it ends at a newline
    or at the end of *text*.
    """
    q, depth, cont_in = state if state else (None, 0, None)
    out: list[str] = []
    comment = False
    cont_out = None
    i, n = 0, len(text)

    def word_start(k: int) -> bool:
        # Look back past `\<newline>` pairs: bash removes them before reading words.
        j = k - 1
        while j >= 1 and out[j] == CONT and out[j - 1] == CONT:
            j -= 2
        if j < 0:
            return cont_in != "mid"
        return out[j] == CODE and text[j] in _WORD_START

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
                if q is None:
                    cont_out = "start" if word_start(i) else "mid"
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
        elif ch == "$" and text[i + 1 : i + 2] == "{":
            depth += 1
            out += [CODE, CODE]
            i += 2
            continue
        elif ch == "}" and depth:
            depth -= 1
            out.append(CODE)
        elif ch == "#" and depth == 0 and word_start(i):
            comment = True
            out.append(COMMENT)
        else:
            out.append(CODE)
        i += 1
    end = (q, depth, cont_out)
    return "".join(out), (None if end == (None, 0, None) else end)


def _joined(text: str) -> tuple[str, str]:
    """*text* and its classes with line continuations removed, as bash reads it."""
    cls, _ = classify(text)
    keep = [k for k, c in enumerate(cls) if c != CONT]
    return "".join(text[k] for k in keep), "".join(cls[k] for k in keep)


def split_with_ops(command: str) -> list[tuple[str, str | None]]:
    """Top-level segments of a command, each with the operator that came before
    it (None for the first). Splits on ``&&`` ``||`` ``|&`` ``;`` ``|`` newline
    and a lone ``&`` where those are code. Comments and line continuations are
    dropped. A lone ``&`` next to a redirect (``2>&1``, ``&>f``, ``<&3``) does
    not split.
    """
    text, cls = _joined(command)
    out: list[tuple[str, str | None]] = []
    buf: list[str] = []
    op: str | None = None

    def close(next_op: str) -> None:
        nonlocal buf, op
        seg = "".join(buf).strip()
        if seg:
            out.append((seg, op))
            op = next_op
        elif op not in ("|", "|&"):
            op = next_op  # an empty segment; a pipe before it still pipes (`ls |<newline>grep`)
        buf = []

    i, n = 0, len(text)
    while i < n:
        ch, c = text[i], cls[i]
        if c == COMMENT:
            i += 1
            continue
        if c == CODE:
            two = text[i : i + 2]
            if two in ("&&", "||", "|&") and cls[i + 1 : i + 2] == CODE:
                close(two)
                i += 2
                continue
            lone_amp = (
                ch == "&" and (i == 0 or text[i - 1] not in "<>") and text[i + 1 : i + 2] != ">"
            )
            if ch in (";", "|", "\n") or lone_amp:
                close(ch)
                i += 1
                continue
        buf.append(ch)
        i += 1
    seg = "".join(buf).strip()
    if seg:
        out.append((seg, op))
    return out


def split_segments(command: str) -> list[str]:
    """The segments of `split_with_ops`, without their operators."""
    return [seg for seg, _ in split_with_ops(command)]


def has_substitution(command: str) -> bool:
    """A ``$(...)`` or backtick the shell would run (code or double-quoted, not
    escaped), or a ``<(``/``>(`` in code. ``$((`` is arithmetic, not a command.
    Read after continuations are removed, so ``$\\<newline>(`` counts."""
    text, cls = _joined(command)
    for i, ch in enumerate(text):
        c = cls[i]
        if c not in (CODE, DOUBLE):
            continue
        if ch == "`":
            return True
        if text[i : i + 2] == "$(" and text[i : i + 3] != "$((":
            return True
        if c == CODE and text[i : i + 2] in ("<(", ">(") and cls[i + 1] == CODE:
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


def mask_line(line: str, state: tuple | None) -> tuple[str, tuple | None]:
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
    state: tuple | None = None
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
