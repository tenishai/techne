"""The evaluator must agree with GitHub, or it is worthless (techne#590).

`tests/fixtures/github/expression_truth_table.json` holds values GitHub's OWN
evaluator produced under a real `pull_request` run — the file this repo bought
after deriving these semantics wrongly TWICE from the reference pages. Replaying
those expressions through `gha_expr.evaluate` under the fixture's own recorded
context is the only thing that makes the evaluator evidence rather than opinion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from gha_expr import evaluate  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "github" / "expression_truth_table.json"


def _context(recorded: dict) -> dict:
    """Rebuild the run context the fixture was measured in.

    `head.repo` deliberately holds ONLY the properties GitHub actually sent, so a
    `<absent>` lookup resolves to None exactly as it did in the real run.
    """
    return {
        "github": {
            "event_name": recorded["event_name"],
            "repository": recorded["repository"],
            "event": {
                "pull_request": {
                    "head": {
                        "repo": {
                            "full_name": recorded["head.repo.full_name"],
                            "fork": recorded["head.repo.fork"],
                        }
                    }
                }
            },
        }
    }


def _expand(expr: str) -> str:
    """The fixture's shorthand -> real expression text."""
    return (
        expr.replace("OLD_GUARD  ", "")
        .replace("NEW_GUARD  ", "")
        .replace("FORK_ONLY  ", "")
        .replace("head.repo.<absent>", "github.event.pull_request.head.repo.absent_prop")
        .replace("head.repo.fork", "github.event.pull_request.head.repo.fork")
        .replace("head.repo.full_name", "github.event.pull_request.head.repo.full_name")
        .replace("event_name", "github.event_name")
        .replace("github.github.", "github.")
    )


def _cases():
    doc = json.loads(FIXTURE.read_text())
    ctx = _context(doc["_context"])
    return [(expr, expected, ctx) for expr, expected in doc["expressions"].items()]


@pytest.mark.parametrize("expr,expected,ctx", _cases())
def test_evaluator_reproduces_githubs_measured_values(expr, expected, ctx):
    assert evaluate(_expand(expr), ctx) == expected, (
        f"the evaluator disagrees with GitHub on {expr!r}: "
        f"got {evaluate(_expand(expr), ctx)}, GitHub measured {expected}. "
        "The fixture is the authority — fix the evaluator, never the fixture."
    )


def test_an_absent_property_is_null_and_equals_false():
    """The semantic everything else rests on, and the one I got wrong writing this.

    The fixture records `absent_property_value: null` (NOT the empty string) and
    `<absent> == false: true`. My first version of this test asserted that second
    one was FALSE — Python's answer — and the fixture caught it. GitHub casts to a
    number when `==` compares different types, so `null == false` is `0 == 0`.

    THIS IS WHY `head.repo.fork == false` IS NOT A USABLE FORK GUARD: a typo'd or
    absent property equals false, so such a guard fails OPEN. An evaluator using
    Python equality would have pronounced it safe.
    """
    ctx = {"github": {"event": {}}}
    assert evaluate("${{ github.event.pull_request.head.repo.fork }}", ctx) is None
    assert evaluate("${{ github.event.pull_request.head.repo.fork == false }}", ctx) is True
    assert evaluate("${{ !github.event.pull_request.head.repo.fork }}", ctx) is True
