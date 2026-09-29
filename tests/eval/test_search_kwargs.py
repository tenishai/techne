"""Unit tests for the eval harness's optional search() kwargs (techne#1086).

NOT eval-marked: these run in the default suite, like test_scoring.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from search_kwargs import (  # noqa: E402
    OPTIONAL_SEARCH_KWARGS,
    task_search_kwargs,
    unsupported_kwargs,
)

from techne.core.search import search  # noqa: E402


def _main_before_1082(
    db_path, query, *, table="all", limit=10, use_vectors=True, half_life_days=None
):
    """The shape of search() before identifier_head_slots existed (PR #1082)."""


def test_a_task_that_sets_no_optional_kwarg_passes_none():
    assert task_search_kwargs({"id": "t", "query": "q"}) == {}


def test_a_task_passes_only_what_it_sets():
    # identifier_head_slots: 0 is falsy and must still be forwarded.
    task = {"id": "t", "identifier_head_slots": 0, "unrelated": 1}
    assert task_search_kwargs(task) == {"identifier_head_slots": 0}


def test_a_base_search_without_the_kwarg_is_detected():
    assert unsupported_kwargs(_main_before_1082, {"identifier_head_slots": 0}) == [
        "identifier_head_slots"
    ]


def test_a_search_with_the_kwarg_accepts_it():
    def with_it(db_path, query, *, identifier_head_slots=None):
        pass

    assert unsupported_kwargs(with_it, {"identifier_head_slots": 0}) == []


def test_no_kwargs_is_always_supported():
    assert unsupported_kwargs(_main_before_1082, {}) == []


def test_var_keyword_accepts_anything():
    def open_ended(db_path, query, **kw):
        pass

    assert unsupported_kwargs(open_ended, {"identifier_head_slots": 0}) == []


def test_a_positional_only_name_cannot_be_passed_by_keyword():
    def pos_only(identifier_head_slots, /):
        pass

    assert unsupported_kwargs(pos_only, {"identifier_head_slots": 0}) == ["identifier_head_slots"]


def test_this_repos_search_accepts_every_declared_optional_kwarg():
    """HEAD's own search() must accept every kwarg the harness may forward.

    If it did not, the gate would fail on HEAD (the harness asserts outside the
    base leg), but this names the cause without needing the eval extras.
    """
    assert unsupported_kwargs(search, dict.fromkeys(OPTIONAL_SEARCH_KWARGS)) == []
