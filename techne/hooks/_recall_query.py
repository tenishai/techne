"""Build the per-turn recall query from a prompt (techne#1018, part 1).

Per-turn recall used to search on the whole prompt. That works for a typed
question and fails for pasted content: a question buried in a few kilobytes of
pasted transcript is drowned out by the text around it.

Measured 2026-09-24 on 30 real per_turn_recall turns (method and results in
PR #1038 and knowledge 3426): each typed question was embedded in the transcript
around it and wrapped as a paste, and the top five knowledge rows were compared
with what the typed question alone returned. Searching the whole paste kept
0.09 of them. This module's policy (question lines, with the operator's wrapper
only when the paste holds no question) kept 0.79; adding the generic wrapper
("what does this mean?") to the question lines as well dropped that to 0.60.
On the motivating case (bac6ba10's licensing question, ~3 KB of pasted
transcript) knowledge 225 and 935 went from absent to ranks 4 and 5, the ranks
the typed question gets.

Typed prompts are returned unchanged, however long: nothing measured says the
question-line query serves them better. Only pasted content is rewritten, and
only pasted content is stripped of techne's own injected lines, since a pasted
transcript often carries recalled rows and rail notices, and searching on them
makes recall select what it already said (architect pattern 8994b936).
"""

from __future__ import annotations

import re
import textwrap

# Claude Code wraps a paste as <pasted_content id="…">…</pasted_content id="…">.
# Claude Code always writes the id; requiring it keeps a typed mention of the tag
# ("why does <pasted_content> parsing…") from splitting the prompt.
_PASTE_TAG = re.compile(r'<(/?)pasted_content id="[^"]*">')
_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

# techne#1045 — Claude Code delivers a finished background task as a turn whose
# whole prompt is <task-notification>…</task-notification> (task id, tool-use id,
# output path, status, summary). It reaches UserPromptSubmit like a typed prompt,
# but nobody asked anything: searching on it injected unrelated rows on 36% of
# per-turn recall turns (496 of 1,377 over 14 days on the maintainer's machine).
_TASK_NOTIFICATION = re.compile(r"<task-notification>.*?</task-notification>", re.DOTALL)

# techne#1080 — other harness frames that reach UserPromptSubmit as the prompt.
# Checked against the 4,690 per_turn_recall triggers in the 2026-09-28 snapshot:
# <agent-message> (153, from 2026-09-15) and <cross-session-message> (29,
# 2026-08-09..10) lead a trigger; <system-reminder>, <local-command-*> and
# <command-name|message|args> never do. Those last are transcript records: a
# slash command reaches the hook as the typed "/name args".
#
# A subagent hand-back: the tag, a column-zero frame ("[Subagent hand-back] The
# text below is the final report ... The report follows:"), then the report with
# every line indented by the harness. The frame's tokens (agent-message,
# hand-back) are rare and identifier-shaped, so they steered recall to rows about
# hand-backs and tripped #1078's identifier head. A peer session's message: the
# tag carries a socket path, a name and a mode; the body is the peer's text.
#
# The harness writes these tags at the start of a line and always with from="…",
# and its closing tag at column zero; the report is indented, so a closing tag the
# report quotes cannot end the frame. Requiring all of that keeps a typed or
# quoted mention of a tag from framing the prompt.
_FRAME_OPEN = re.compile(
    r'(?m)^(?P<agent><agent-message from="[^"]*"[^>]*>)'
    r'|^(?P<peer><cross-session-message from="[^"]*"[^>]*>)'
    r"|(?P<note><task-notification>)"
)
_FRAME_CLOSE = {
    "agent": re.compile(r"(?m)^</agent-message>"),
    "peer": re.compile(r"(?m)^</cross-session-message>"),
    "note": re.compile(r"</task-notification>"),
}
_PASTE_CLOSE = re.compile(r'</pasted_content id="[^"]*">')
_HANDBACK_FRAME = "[Subagent hand-back]"

# What techne's rails render into context, by shape. tests/test_recall_query.py
# renders the recall and codex rails with their real code; the session-start,
# coord and reminder output is built inline in their hooks, so those samples are
# written in the rendered shape and each marker is checked against the source.
# Every entry below must match some sample, or a test fails.
#   a recalled or Key Knowledge row: "- ⟦techne#N⟧ [type] …"
_OWN_ROW = re.compile(r"^- ⟦techne#\d+⟧ \[")
#   a rail's block header: the block runs to the next "## " header or the next
#   transcript turn (❯/⏺), taking its notices, rows and trailers with it
_OWN_HEADERS = (
    # Exactly what the rails render, so a pasted document heading such as
    # "## techne install notes" or "## Codex — notes I wrote" is kept.
    "## techne —",  # per-turn recall, seed, drain, reflection notices
    "## techne ⚠️",  # session-start banner and its failure notices
    "## techne ✅",  # session-start banner, healthy (core/health.py)
    "## Codex — editing `",  # codex edit rail
    "## Codex — symbols matching `",  # codex grep assist
    "## Coordination (techne#",  # session-start coord banner
    "## Active Threads",
    "## Recent Threads",
    "## Key Knowledge",
)
#   single-line deliveries from post_tool_use
_OWN_LINE_PREFIXES = (
    "📬 ",  # a peer coord message
    "⏰ Reminder: ",  # a due reminder
    "Unread message(s) from a peer Claude session",  # the coord preamble
)

# A Claude Code transcript starts its lines with turn markers: ❯ opens a user
# turn, ⏺ an assistant one. A shell prompt (starship, pure, spaceship) also draws
# ❯, but a shell never prints ⏺, so text counts as transcript only when some line
# starts with each.
_USER_TURN = "❯"
_ASSISTANT_TURN = "⏺"


def strip_own_injections(text: str) -> str:
    """Remove system reminders and the lines techne's rails render (see above)."""
    text = _SYSTEM_REMINDER.sub(" ", text)
    kept = []
    in_block = False
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("## ") or s.startswith((_USER_TURN, _ASSISTANT_TURN)):
            in_block = s.startswith(_OWN_HEADERS)
            if in_block:
                continue
        elif in_block:
            continue
        if _OWN_ROW.match(s) or s.startswith(_OWN_LINE_PREFIXES):
            continue
        kept.append(line)
    return "\n".join(kept)


def is_task_notification(prompt: str) -> bool:
    """True when the prompt is only Claude Code task notifications, one or more.

    A prompt with any other text alongside a notification is not one: whatever
    the operator typed still deserves recall.
    """
    stripped, n = _TASK_NOTIFICATION.subn("", prompt)
    return n > 0 and not stripped.strip()


def _question_lines(text: str) -> list[str]:
    """A pasted user turn (❯) or any line ending in "?"."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(_USER_TURN):
            s = s[len(_USER_TURN) :].strip()
            if s:
                out.append(s)
        elif s.endswith("?"):
            out.append(s)
    return out


def _is_transcript(text: str) -> bool:
    starts = [line.lstrip()[:1] for line in text.splitlines()]
    return _USER_TURN in starts and _ASSISTANT_TURN in starts


def _split_paste(prompt: str) -> tuple[str, str] | None:
    """Split a prompt into (the operator's text, the pasted text), or None.

    Tags are counted by depth, so a paste inside a paste stays pasted rather than
    closing the outer one early and leaking its tail into the operator's text.
    An unclosed tag pastes everything after it; a stray closing tag is dropped.
    """
    tags = list(_PASTE_TAG.finditer(prompt))
    if not any(not m.group(1) for m in tags):
        return None
    outside, inside, depth, pos = [], [], 0, 0
    for m in tags:
        (inside if depth else outside).append(prompt[pos : m.start()])
        depth = max(0, depth - 1) if m.group(1) else depth + 1
        pos = m.end()
    (inside if depth else outside).append(prompt[pos:])
    return "".join(outside), "\n".join(inside)


def _handback_report(body: str) -> str:
    """The report inside an <agent-message>, without the harness frame.

    The harness indents every report line, so the column-zero lines are its frame.
    The "[Subagent hand-back]" frame line always goes, so a hand-back cut off
    before its report searches nothing; any other body with no indented line is
    kept, rather than dropped on a format this code has not seen.
    """
    lines = [line for line in body.splitlines() if not line.startswith(_HANDBACK_FRAME)]
    if not any(line[:1].isspace() for line in lines if line.strip()):
        return "\n".join(lines)
    return textwrap.dedent("\n".join(line for line in lines if not line or line[:1].isspace()))


def _unclosed_handback_end(prompt: str, start: int, limit: int) -> int:
    """Where a hand-back with no closing tag ends: after its frame line and the
    indented report, at the first column-zero line that follows. What comes after
    is text the harness did not indent, so it goes back to the operator's words."""
    pos = prompt.find("\n", start, limit)
    if pos < 0:
        return limit
    pos += 1
    while pos < limit and prompt.startswith(_HANDBACK_FRAME, pos):
        nl = prompt.find("\n", pos, limit)
        pos = limit if nl < 0 else nl + 1
    while pos < limit:
        nl = prompt.find("\n", pos, limit)
        line = prompt[pos : limit if nl < 0 else nl]
        if line.strip() and not line[:1].isspace():
            break
        pos = limit if nl < 0 else nl + 1
    return pos


def _split_harness_frames(prompt: str) -> tuple[str, list[str]] | None:
    """Split a prompt into (text outside harness frames, the frames' bodies), or None.

    None when the prompt holds no frame, so it takes the pre-#1080 path unchanged.
    A <task-notification> is dropped whole (ids, output path, status: nothing
    asked, per #1045). An <agent-message> keeps its report and a
    <cross-session-message> its body; both are model output rather than the
    operator's words, so they are returned apart, in text order, and searched like
    inbound text.

    The operator's words are never dropped. Frames are read left to right, and a
    frame is never read past the closing tag of the paste it opens in, nor into
    another frame's body. A hand-back with no closing tag ends after its indented
    report (a logged trigger is cut at 200 characters, so the store holds such
    prompts); a peer message or notification with no closing tag is not taken as
    a frame, since nothing marks where the peer's text stops: only the peer's
    opening tag is dropped.
    """
    outside: list[str] = []
    bodies: list[str] = []
    pos, found = 0, False
    while m := _FRAME_OPEN.search(prompt, pos):
        kind = m.lastgroup
        paste_close = _PASTE_CLOSE.search(prompt, m.end())
        limit = paste_close.start() if paste_close else len(prompt)
        close = _FRAME_CLOSE[kind].search(prompt, m.end(), limit)
        if close is None and kind == "note":
            outside.append(prompt[pos : m.end()])
            pos = m.end()
            continue
        outside.append(prompt[pos : m.start()] + "\n")
        found = True
        if close is not None:
            body, pos = prompt[m.end() : close.start()], close.end()
        elif kind == "agent":
            end = _unclosed_handback_end(prompt, m.end(), limit)
            body, pos = prompt[m.end() : end], end
        else:  # an unclosed peer message: drop the tag, keep its text
            body, pos = "", m.end()
        if kind == "agent":
            bodies.append(_handback_report(body))
        elif kind == "peer" and body:
            bodies.append(body)
    if not found:
        return None
    outside.append(prompt[pos:])
    return "".join(outside).strip(), bodies


def build_recall_query(prompt: str, long_prompt_chars: int) -> str:
    """Return the text per-turn recall should search on.

    Harness frames come off first (techne#1080): a task notification is dropped,
    and a hand-back's report or a peer's message is searched the way inbound
    recall searches a tool result (`build_inbound_query`), after whatever the
    operator wrote around it. A prompt with no frame takes the path below as is.

    A typed prompt is returned unchanged. Pasted content (a <pasted_content>
    block, or text longer than ``long_prompt_chars`` with a line starting with
    each transcript marker) is stripped of techne's own injected lines and
    searched on its question lines; failing those, on the operator's text around
    the paste; failing that, on its opening ``long_prompt_chars`` characters.

    An operator wrapper longer than ``long_prompt_chars`` is treated as pasted
    too: its question lines join the query, and it is never used whole. An empty
    result means the paste held only techne's own injected context and there was
    no usable wrapper; with a short wrapper, the wrapper is searched instead.
    """
    framed = _split_harness_frames(prompt)
    if framed is not None:
        outside, bodies = framed
        parts = [_build_unframed(outside, long_prompt_chars) if outside else ""]
        parts += [build_inbound_query(b, long_prompt_chars) for b in bodies]
        return " ".join(p for p in parts if p)
    return _build_unframed(prompt, long_prompt_chars)


def _build_unframed(prompt: str, long_prompt_chars: int) -> str:
    split = _split_paste(prompt)
    if split is None and not (len(prompt) > long_prompt_chars and _is_transcript(prompt)):
        return prompt

    # The operator's own words around the paste are never stripped.
    wrapper, pasted = split if split is not None else ("", prompt)
    wrapper = wrapper.strip()
    body = strip_own_injections(pasted).strip()

    questions = _question_lines(body)
    if wrapper and len(wrapper) > long_prompt_chars:
        # A long wrapper is itself pasted-like: take only its questions.
        questions = _question_lines(wrapper) + questions
        wrapper = ""
    parts = list(dict.fromkeys(questions)) or ([wrapper] if wrapper else [])
    query = " ".join(parts).strip()
    return query or body[:long_prompt_chars]


def build_inbound_query(text: str, max_chars: int) -> str:
    """The query for inbound recall on a tool result (techne#1018 part 3).

    Tool output is treated as pasted content always: stripped of techne's own
    injected lines (a hand-back often quotes them), then searched on its
    question lines, else on its opening text. Unlike `build_recall_query` the
    result is capped at ``max_chars`` either way, because a fetched page can run
    to 100 KB and there is no typed prompt here to pass through whole. That
    policy was measured on pasted transcripts (see the module docstring), not on
    hand-backs or pages; it is reused, not re-measured. "" when only techne's
    own context was there.
    """
    body = strip_own_injections(text).strip()
    questions = list(dict.fromkeys(_question_lines(body)))
    return (" ".join(questions) or body)[:max_chars].strip()
