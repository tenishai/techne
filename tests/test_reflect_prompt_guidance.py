"""The reflect prompt names both failure modes, not one (techne#950).

From Grip's reflect run, 2026-09-17: 32 candidates from 13 threads, 26
promoted, 6 discarded. Four of the six discards failed the same way -- the
"durable, evidence-anchored" instruction pushes toward specific details, and
specific details are what go stale: counts a later fix changes, image tags, a
pinned Python version upstream will bump, supersession status on a ticket.

The clearest case: two candidates from one thread, one quoting the image tags
and one stating the rule. Only the rule stays true.

THE SECOND FAILURE RUNS THE OPPOSITE WAY, from Grip's blind-judged run (#951).
The judge's TOP discard reason was `obvious` (4 of 9), ahead of decaying
specifics (2):

    "A Job uses the CronJob spec as it stood at creation time"  (standard k8s)
    "A fresh clone has no git identity"                         (standard git)
    "Re-read the repo before citing it"                         (generic hygiene)

So an instruction that only says "prefer the rule" pushes candidates straight
into the other ditch. The prompt has to describe the BAND: non-obvious to a
competent practitioner, and still true once the identifiers change.

WHAT THESE TESTS DO AND DO NOT ESTABLISH. They assert the guidance is present
and survives rendering. They do NOT establish that it improves candidate
quality -- that needs the before/after run on the same threads the issue asks
for, which is not done here. See the PR for why.
"""

from __future__ import annotations

import pytest

from techne.config import Config
from techne.core.reflect import PROMPT_TEMPLATE


@pytest.mark.parametrize(
    "phrase",
    [
        "Prefer the rule over the instance",
        "survives it changing",
        "already knows",
        "NON-OBVIOUS",
        "STILL TRUE",
    ],
)
def test_the_guidance_is_in_the_template(phrase):
    assert phrase in PROMPT_TEMPLATE, f"missing guidance: {phrase!r}"


def test_both_failure_modes_are_named_not_just_one():
    """The regression this guards: an earlier draft pushed only against specifics.

    Pushing only one way trades the stale-candidate failure for the obvious-
    candidate failure, which the blind judge ranked as the LARGER of the two.
    """
    pushes_against_specifics = "Prefer the rule over the instance" in PROMPT_TEMPLATE
    pushes_against_obvious = "competent engineer in this domain already knows" in PROMPT_TEMPLATE
    assert pushes_against_specifics and pushes_against_obvious, (
        "the prompt must push against BOTH failures; "
        f"specifics={pushes_against_specifics} obvious={pushes_against_obvious}"
    )


def test_the_guidance_survives_rendering():
    """The template carries {placeholders}; the guidance must not be eaten by format()."""
    from techne.core.prompts import render_prompt

    rendered = render_prompt(
        Config(),
        "reflect",
        PROMPT_TEMPLATE,
        label="a label",
        summary="a summary",
        observations="an observation",
        existing="(none)",
        knowledge_types="fact, decision",
        max_items=5,
    )
    assert "Prefer the rule over the instance" in rendered
    assert "competent engineer in this domain already knows" in rendered
    # NOT a bare "{" check: the JSON item shape uses `{{` and renders to a real
    # brace, so that assertion fails on correct output. Look for an unrendered
    # NAMED placeholder instead -- the actual failure being guarded.
    import re

    leaked = re.findall(
        r"\{(label|summary|observations|existing|knowledge_types|max_items)\}", rendered
    )
    assert not leaked, f"unrendered placeholders: {leaked}"
