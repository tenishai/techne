"""Build the per-turn recall query from a prompt (techne#1018, part 1).

Per-turn recall used to search on the whole prompt. That works for a typed
question and fails for pasted content: a question buried in a few kilobytes of
pasted transcript is drowned out by the text around it.

MEASURED (2026-09-24, 30 real per_turn_recall turns since 2026-09-01). Each typed
question was re-embedded in the transcript around it (two turns either side,
memory refs stripped so the answer could not be read off the page), and the top
five knowledge rows were compared with what the typed question alone returned:

    search the whole pasted chunk      mean top-5 overlap 0.07
    search only its question lines     mean top-5 overlap 0.81

A frequency-ranked keyword bag and a per-paragraph search fused by RRF were also
tried on the motivating case (bac6ba10's licensing question, knowledge 225/935)
and recovered neither row. Question lines recovered both at the typed ranks.

So pasted content (a <pasted_content> block, or long text carrying Claude Code's
❯/⏺ transcript markers) is searched on its question lines. The operator's own
words around the paste are used only when the paste holds no question: with the
wrapper added, the measured overlap fell from 0.81 to 0.60. Typed prompts of any
length are searched unchanged, since nothing measured says they would do better.

The first step strips techne's own injected context. A pasted transcript often
carries recalled rows and system reminders, and searching on them makes recall
select what it already said (architect pattern 8994b936, "a memory that reads
its own output will eat it").
"""

from __future__ import annotations

import re

# Claude Code wraps a paste as <pasted_content id="…">…</pasted_content id="…">.
_PASTED = re.compile(r"<pasted_content\b[^>]*>(.*?)</pasted_content\b[^>]*>", re.DOTALL)
_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

# Lines techne itself emits into context: recalled rows carry the ⟦techne#N⟧ tag,
# and the rails' headers and notices start with these prefixes.
_OWN_LINE_MARKERS = ("⟦techne#",)
_OWN_LINE_PREFIXES = (
    "## techne",
    "> Per-turn recall",
    "> Rows ending in",
    "> Recalled memory",
)

# Claude Code transcript turn markers: ❯ opens a user turn, ⏺ an assistant one.
# A prompt carrying them is pasted transcript even without <pasted_content> tags.
_TRANSCRIPT_MARKERS = ("❯", "⏺")

# A user turn in a pasted Claude Code transcript starts with ❯; a quoted line
# starts with "> ". Either, or any line ending in "?", counts as a question line.
_QUESTION_PREFIXES = ("❯", "> ")


def strip_own_injections(text: str) -> str:
    """Remove system reminders and every line techne itself injects."""
    text = _SYSTEM_REMINDER.sub(" ", text)
    kept = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if any(m in stripped for m in _OWN_LINE_MARKERS):
            continue
        if stripped.startswith(_OWN_LINE_PREFIXES):
            continue
        kept.append(line)
    return "\n".join(kept)


def _question_lines(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(_QUESTION_PREFIXES):
            s = s.lstrip("❯>").strip()
            if s:
                out.append(s)
        elif s.endswith("?"):
            out.append(s)
    return out


def build_recall_query(prompt: str, long_prompt_chars: int) -> str:
    """Return the text per-turn recall should search on.

    After techne's own injections are stripped, a typed prompt is returned as
    is. Pasted content (a pasted block, or transcript-marked text longer than
    ``long_prompt_chars``) is searched on its question lines; failing those, on
    the operator's text around the paste; failing that, on its opening
    ``long_prompt_chars`` characters, so it never searches on nothing but tags.
    An empty result means the prompt held only techne's own injected context.
    """
    text = strip_own_injections(prompt).strip()
    has_paste = bool(_PASTED.search(text))
    is_transcript = any(line.lstrip().startswith(_TRANSCRIPT_MARKERS) for line in text.splitlines())
    if not has_paste and not (is_transcript and len(text) > long_prompt_chars):
        # A typed prompt, however long, is searched as is: 171 of 2,635 typed
        # prompts since 2026-09-01 ran over 600 characters with no paste, and
        # nothing measured says the question-line query serves them better.
        return text

    wrapper = _PASTED.sub("\n", text).strip() if has_paste else ""
    body = "\n".join(_PASTED.findall(text)) if has_paste else text
    questions = _question_lines(body)
    if wrapper and len(wrapper) > long_prompt_chars:
        # A long wrapper is itself pasted-like: take only its questions.
        questions = _question_lines(wrapper) + questions
        wrapper = ""
    # The pasted questions carry the topic; the operator's wrapper ("what does
    # this mean?") is usually generic and diluted them in measurement (0.60 vs
    # 0.81 top-5 overlap). It is the query only when the paste has no question.
    parts = list(dict.fromkeys(questions)) or ([wrapper] if wrapper else [])
    query = " ".join(parts).strip()
    if query:
        return query
    # No question anywhere: search on the opening, with the paste tags removed so
    # the markup itself is never the query.
    return _PASTED.sub(lambda m: m.group(1), text).strip()[:long_prompt_chars]
