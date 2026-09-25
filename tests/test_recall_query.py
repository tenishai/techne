"""techne#1018 part 1: per-turn recall searches pasted content on its question."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from techne.hooks._recall_query import (
    build_recall_query,
    is_task_notification,
    strip_own_injections,
)

LIMIT = 600
QUESTION = "is it legit for us to build that on a max account?"
NOISE = "⏺ " + ("LiteLLM translates one call format into many provider APIs. " * 30)
HOOKS = Path(__file__).resolve().parents[1] / "techne" / "hooks"


def _paste(body: str, pid: str = "ab12") -> str:
    return f'<pasted_content id="{pid}">\n{body}\n</pasted_content id="{pid}">'


# --- typed prompts come back unchanged ------------------------------------------

TYPED = [
    QUESTION,
    "why does <pasted_content> parsing strip my text in the hook?",
    "why did you cite ⟦techne#225⟧ for the licensing question on the max account?",
    "## techne install notes\nthe venv is stale again, what do I rebuild?",
    "> quoting the doc: recall is a rail\nis that still the plan?",
    # Long, with a shell prompt drawn as ❯ (starship, pure): not a transcript.
    (
        "The migration failed again after the rebase. "
        + ("It stopped at step 24 with the observations table missing. " * 12)
        + "\n❯ techne doctor\n❯ pytest tests/test_install.py\n"
        "Here is what I ran."
    ),
    # Long, multi-line, with a question line that differs from the whole.
    "Please " + ("refactor the recall hook carefully. " * 30) + f"\n{QUESTION}\nKeep tests.",
]


@pytest.mark.parametrize("typed", TYPED)
def test_a_typed_prompt_is_searched_unchanged(typed):
    assert build_recall_query(typed, LIMIT) == typed


# --- pasted content is searched on its question ---------------------------------


def test_a_long_paste_is_searched_on_its_question_line():
    prompt = "can you check this?\n" + _paste(f"{NOISE}\n\n❯ {QUESTION}\n\n{NOISE}")
    assert build_recall_query(prompt, LIMIT) == QUESTION


def test_a_generic_wrapper_does_not_dilute_the_pasted_question():
    prompt = "what does this mean?\n" + _paste(QUESTION)
    assert build_recall_query(prompt, LIMIT) == QUESTION


def test_the_wrapper_is_the_query_when_the_paste_holds_no_question():
    prompt = "what changed in the licensing ruling\n" + _paste(NOISE)
    assert build_recall_query(prompt, LIMIT) == "what changed in the licensing ruling"


def test_long_transcript_text_without_paste_tags_uses_its_question_lines():
    prompt = f"{NOISE}\n❯ {QUESTION}\n{NOISE}"
    assert build_recall_query(prompt, LIMIT) == QUESTION


def test_a_long_wrapper_contributes_only_its_question_lines():
    long_wrapper = ("context I am adding by hand. " * 30) + "\ndoes the ruling still hold?"
    prompt = long_wrapper + "\n" + _paste(f"{NOISE}\n❯ {QUESTION}")
    assert build_recall_query(prompt, LIMIT) == f"does the ruling still hold? {QUESTION}"


def test_a_long_wrapper_with_no_question_is_not_the_fallback():
    long_wrapper = "context I am adding by hand. " * 30
    prompt = long_wrapper + "\n" + _paste(NOISE)
    assert build_recall_query(prompt, LIMIT) == NOISE.strip()[:LIMIT]


def test_a_short_wrapper_is_searched_when_the_paste_is_only_techne_output():
    prompt = "what does this mean for us\n" + _paste("- ⟦techne#225⟧ [fact] Max OAuth only")
    assert build_recall_query(prompt, LIMIT) == "what does this mean for us"


def test_a_quoted_line_in_a_paste_is_not_a_question():
    prompt = _paste(f"> see the runbook for the flip steps\n{NOISE}\n❯ {QUESTION}")
    assert build_recall_query(prompt, LIMIT) == QUESTION


def test_a_paste_with_no_question_or_wrapper_falls_back_to_its_opening():
    assert build_recall_query(_paste(NOISE), LIMIT) == NOISE.strip()[:LIMIT]


@pytest.mark.parametrize(
    "heading",
    ["## techne install notes", "## Codex — notes I wrote", "## techne setup"],
)
def test_a_pasted_document_heading_is_not_mistaken_for_a_rail(heading):
    prompt = _paste(f"{heading}\nwhat is the blast radius of search?")
    assert build_recall_query(prompt, LIMIT) == "what is the blast radius of search?"


def test_nested_paste_tags_never_reach_the_query():
    # With a question, and without one (the fallback searches the body itself).
    with_q = build_recall_query(_paste("outer " + _paste("what is x?", pid="b") + " tail"), LIMIT)
    assert "pasted_content" not in with_q
    assert "what is x?" in with_q
    no_q = build_recall_query(_paste("outer " + _paste("inner words", pid="b") + " tail"), LIMIT)
    assert "pasted_content" not in no_q
    assert "inner words" in no_q


# --- techne's own rail output, rendered by the rails themselves, is stripped ----


def _rail_outputs() -> dict[str, str]:
    from techne.hooks import post_tool_use, pre_tool_use, user_prompt_submit

    return {
        "per_turn_recall": user_prompt_submit._build_context(
            [{"id": 225, "type": "fact", "content": "Is Max OAuth the only auth?"}], 1000
        ),
        "codex_edit_rail": post_tool_use._build_edit_rail_context(
            {"name": "search", "file_path": "techne/core/search.py"},
            [],
            3,
        ),
        "codex_grep_assist": pre_tool_use._build_context("search", []),
        "coord": "📬 poe2 [coord]: are you on #1018?",
        "reminder": "⏰ Reminder: is the floor measured yet?",
        "session_banner": (
            "## techne ⚠️\n> v0.17.0 · checkout 96e7c49\n\n"
            "## Coordination (techne#376)\n> 2 live session(s)\n\n"
            "## techne — pending reflection\n> Session abcd has unreflected work?\n\n"
            "## Key Knowledge\n- ⟦techne#237⟧ [fact] who is the user?\n"
        ),
        "session_banner_healthy": "## techne ✅\n> 51966 obs · is it current?\n",
        # session_start.py builds these inline in main(), so they are written out
        # here in the shape it renders (session_start.py:433-460).
        "threads": (
            "## Active Threads\n\n**Session work (Bash)** [claude-code]\n"
            "Resume: is the floor measured yet?\n\n"
            "## Recent Threads (48h)\n\n- **licensing** [claude-code] — should we build on Max?\n"
        ),
    }


@pytest.mark.parametrize("rail", sorted(_rail_outputs()))
def test_pasted_rail_output_is_stripped(rail):
    block = _rail_outputs()[rail]
    assert block.strip(), f"{rail} rendered nothing to test against"
    leftover = strip_own_injections(block).strip()
    assert "?" not in leftover, f"{rail} left a question line behind: {leftover!r}"
    query = build_recall_query(_paste(f"{block}\n⏺ I updated the licensing check."), LIMIT)
    assert query == "⏺ I updated the licensing check."


def test_every_own_header_is_bound_by_a_rail_sample():
    from techne.hooks._recall_query import _OWN_HEADERS

    lines = [ln for out in _rail_outputs().values() for ln in out.splitlines()]
    for header in _OWN_HEADERS:
        assert any(ln.startswith(header) for ln in lines), f"no rail sample renders {header!r}"


def test_the_session_banner_headers_match_what_session_start_emits():
    source = (HOOKS / "session_start.py").read_text()
    for header in (
        "## techne ⚠️",
        "## Coordination (techne#",
        "## techne — pending reflection",
        "## Active Threads",
        "## Recent Threads",
    ):
        assert header in source, f"session_start no longer emits {header!r}"
    assert "## Key Knowledge" in (HOOKS.parent / "core" / "egress.py").read_text()


def test_the_hand_written_rail_samples_match_their_source():
    """Samples not rendered by real code are pinned to the literals that build them."""
    health = (HOOKS.parent / "core" / "health.py").read_text()
    assert 'mark = "✅" if' in health and 'f"\\n## techne {mark}"' in health
    post = (HOOKS / "post_tool_use.py").read_text()
    for literal in (
        'f"📬 {who}',
        'f"⏰ Reminder: ',
        '"Unread message(s) from a peer Claude session',
    ):
        assert literal in post, f"post_tool_use no longer renders {literal!r}"


def test_a_paste_of_only_techne_output_searches_nothing():
    injected = (
        "## techne — durable facts relevant to this prompt\n"
        "> Per-turn recall (techne#109); recalled memory, not ground truth.\n"
        "- ⟦techne#225⟧ [fact] Anthropic subscription is Max OAuth?\n"
        "<system-reminder>\nIs this a reminder?\n</system-reminder>"
    )
    assert build_recall_query(_paste(injected), LIMIT) == ""


# --- the hook searches on the built query ---------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    monkeypatch.setenv("TECHNE_HOOK_TIMEOUT", "0")
    return tmp_path


def _run_hook(monkeypatch, prompt: str) -> list[str]:
    """Run the hook and return the queries it passed to search()."""
    from techne.core import search as search_mod
    from techne.hooks import user_prompt_submit

    seen: list[str] = []
    real = search_mod.search

    def spy(db_path, query, *a, **kw):
        seen.append(query)
        return real(db_path, query, *a, **kw)

    monkeypatch.setattr(search_mod, "search", spy)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": prompt})))
    with pytest.raises(SystemExit):
        user_prompt_submit.main()
    return seen


def _logged_triggers() -> list[str]:
    import sqlite3

    from techne.config import Config

    con = sqlite3.connect(Config.load().db_path)
    try:
        # The log table is created on first write; a hook that searched nothing
        # may never have written one.
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "context_injections" not in tables:
            return []
        return [
            r[0]
            for r in con.execute(
                "SELECT trigger_snippet FROM context_injections "
                "WHERE source = 'per_turn_recall' ORDER BY id"
            )
        ]
    finally:
        con.close()


@pytest.mark.usefixtures("home")
def test_the_hook_searches_a_long_paste_on_its_question(monkeypatch):
    prompt = "can you check this?\n" + _paste(f"{NOISE}\n\n❯ {QUESTION}\n\n{NOISE}")
    assert _run_hook(monkeypatch, prompt) == [QUESTION]
    assert _logged_triggers()[-1].startswith(QUESTION[:40])


@pytest.mark.usefixtures("home")
def test_the_hook_does_not_search_a_paste_of_only_its_own_output(monkeypatch):
    prompt = _paste("- ⟦techne#225⟧ [fact] Anthropic subscription is Max OAuth")
    assert _run_hook(monkeypatch, prompt) == []
    assert _logged_triggers()[-1].startswith("[no query")


# --- techne#1045: a background-task notification is not a question ----------------

# The shape Claude Code 2.1.282 delivers to UserPromptSubmit, copied from a
# session transcript (attachment.commandMode == "task-notification").
NOTIFICATION = (
    "<task-notification>\n"
    "<task-id>bl1x8z5uc</task-id>\n"
    "<tool-use-id>toolu_01R6JLMHwaRafpJZnYsPPcjy</tool-use-id>\n"
    "<output-file>/private/tmp/claude-501/proj/sess/tasks/bl1x8z5uc.output</output-file>\n"
    "<status>completed</status>\n"
    '<summary>Background command "Reindex the techne codex graph" completed '
    "(exit code 0)</summary>\n"
    "</task-notification>"
)


@pytest.mark.parametrize(
    "prompt",
    [NOTIFICATION, f"  {NOTIFICATION}\n", f"{NOTIFICATION}\n{NOTIFICATION}"],
    ids=["one", "padded", "two"],
)
def test_a_prompt_of_only_notifications_is_one(prompt):
    assert is_task_notification(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        QUESTION,
        f"{NOTIFICATION}\n{QUESTION}",
        "why does <task-notification> fire recall?",
        "<task-notification>\n<task-id>x</task-id>",  # unclosed: not Claude Code's shape
        "",
    ],
    ids=["typed", "notification-plus-question", "typed-mention", "unclosed", "empty"],
)
def test_anything_else_is_not(prompt):
    assert not is_task_notification(prompt)


@pytest.mark.usefixtures("home")
def test_the_hook_does_not_search_a_task_notification(monkeypatch):
    assert _run_hook(monkeypatch, NOTIFICATION) == []
    assert _logged_triggers() == [], "nothing was searched, so nothing is logged as a recall"


@pytest.mark.usefixtures("home")
def test_the_hook_still_searches_a_question_sent_with_a_notification(monkeypatch):
    assert _run_hook(monkeypatch, f"{NOTIFICATION}\n{QUESTION}") != []
