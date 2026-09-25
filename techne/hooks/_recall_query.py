"""Build the per-turn recall query from a prompt (techne#1018, part 1).

Per-turn recall used to search on the whole prompt. That works for a typed
question and fails for pasted content: a question buried in a few kilobytes of
pasted transcript is drowned out by the text around it.

Measured 2026-09-24 on 30 real per_turn_recall turns (method and results in
PR #1038 and knowledge 3426): each typed question was embedded in the transcript
around it and wrapped as a paste, and the top five knowledge rows were compared
with what the typed question alone returned. Searching the whole paste kept
0.09 of them; searching its question lines kept 0.79. Adding the operator's
generic wrapper ("what does this mean?") to the question lines dropped that to
0.60, so the wrapper is used only when the paste holds no question.

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

# What techne's rails render into context, by shape. tests/test_recall_query.py
# renders the rails themselves and checks each shape here still matches, so a
# rail whose output changes, or a new rail, fails a test instead of leaking in.
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

# A Claude Code transcript carries both turn markers: ❯ opens a user turn, ⏺ an
# assistant one. A shell prompt (starship, pure, spaceship) also draws ❯, but a
# shell never prints ⏺, so text counts as transcript only when it has both.
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
    block, or text longer than ``long_prompt_chars`` carrying both transcript
    markers) is stripped of techne's own injected lines and searched on its
    question lines; failing those, on the operator's text around the paste;
    failing that, on its opening ``long_prompt_chars`` characters. An empty
    result means the paste held only techne's own injected context.
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
