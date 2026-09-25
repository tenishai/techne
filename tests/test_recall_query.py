"""techne#1018 part 1: per-turn recall searches on the question, not the paste."""

from __future__ import annotations

import io
import json

import pytest

from techne.hooks._recall_query import build_recall_query, strip_own_injections

LIMIT = 600
QUESTION = "is it legit for us to build that on a max account?"
NOISE = "⏺ " + ("LiteLLM translates one call format into many provider APIs. " * 30)


def _paste(body: str, pid: str = "ab12") -> str:
    return f'<pasted_content id="{pid}">\n{body}\n</pasted_content id="{pid}">'


def test_a_short_typed_prompt_is_searched_unchanged():
    assert build_recall_query(QUESTION, LIMIT) == QUESTION


def test_a_long_paste_is_searched_on_its_question_line():
    prompt = "can you check this?\n" + _paste(f"{NOISE}\n\n❯ {QUESTION}\n\n{NOISE}")
    assert build_recall_query(prompt, LIMIT) == QUESTION


def test_a_generic_wrapper_does_not_dilute_the_pasted_question():
    prompt = "what does this mean?\n" + _paste(QUESTION)
    assert build_recall_query(prompt, LIMIT) == QUESTION


def test_the_wrapper_is_the_query_when_the_paste_holds_no_question():
    prompt = "what changed in the licensing ruling\n" + _paste(NOISE)
    assert build_recall_query(prompt, LIMIT) == "what changed in the licensing ruling"


def test_a_long_typed_prompt_is_searched_unchanged():
    # Multi-line, with a question line that differs from the whole: a query
    # builder applied to typed prompts would return only that line.
    typed = (
        "Please " + ("refactor the recall hook carefully. " * 30) + "\n"
        f"> {QUESTION}\n"
        "Keep the existing tests passing."
    )
    assert len(typed) > LIMIT
    assert build_recall_query(typed, LIMIT) == typed


def test_long_transcript_text_without_paste_tags_uses_its_question_lines():
    prompt = f"{NOISE}\n❯ {QUESTION}\n{NOISE}"
    assert build_recall_query(prompt, LIMIT) == QUESTION


def test_a_long_prompt_with_no_question_falls_back_to_its_opening():
    query = build_recall_query(NOISE, LIMIT)
    assert query == NOISE.strip()[:LIMIT]


def test_techne_own_injected_rows_are_never_searched_on():
    injected = (
        "## techne — durable facts relevant to this prompt\n"
        "> Per-turn recall (techne#109); recalled memory, not ground truth — verify first.\n"
        "- ⟦techne#225⟧ [fact] Anthropic subscription is Max OAuth?\n"
        "<system-reminder>\nIs this a reminder?\n</system-reminder>"
    )
    assert strip_own_injections(injected).strip() == ""
    assert build_recall_query(_paste(injected), LIMIT) == ""


# --- the hook searches on the built query --------------------------------------


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


def test_the_hook_searches_a_long_paste_on_its_question(home, monkeypatch):
    prompt = "can you check this?\n" + _paste(f"{NOISE}\n\n❯ {QUESTION}\n\n{NOISE}")
    seen = _run_hook(monkeypatch, prompt)
    assert len(seen) == 1
    assert QUESTION in seen[0]
    assert "LiteLLM" not in seen[0]


def test_the_hook_does_not_search_a_prompt_that_is_only_its_own_injection(home, monkeypatch):
    prompt = _paste("- ⟦techne#225⟧ [fact] Anthropic subscription is Max OAuth")
    assert _run_hook(monkeypatch, prompt) == []
