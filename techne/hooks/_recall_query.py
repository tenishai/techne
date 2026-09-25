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

# Claude Code wraps a paste as <pasted_content id="…">…</pasted_content id="…">.
# Claude Code always writes the id; requiring it keeps a typed mention of the tag
# ("why does <pasted_content> parsing…") from splitting the prompt.
_PASTE_TAG = re.compile(r'<(/?)pasted_content id="[^"]*">')
_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

# techne#1045 — Claude Code delivers a finished background task as a turn whose
# whole prompt is <task-notification>…</task-notification> (task id, tool-use id,
# output path, status, summary). It reaches UserPromptSubmit like a typed prompt,
# but nobody asked anything: searching on it injected unrelated rows on 36% of
# per-turn recall turns (496 of 1,377 over 14 days on dev04).
_TASK_NOTIFICATION = re.compile(r"<task-notification>.*?</task-notification>", re.DOTALL)

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


def build_recall_query(prompt: str, long_prompt_chars: int) -> str:
    """Return the text per-turn recall should search on.

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
