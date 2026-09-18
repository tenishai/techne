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
        # Each of these is NEW in this change. "already knows" was in the earlier
        # parametrisation and witnessed nothing: origin/main's template already
        # contained it twice ("What the agent already knows"), so that case passed
        # on the unchanged prompt and would survive deleting every line added here.
        "TOO OBVIOUS",
        "TOO SPECIFIC",
        "survives it changing",
        "NON-OBVIOUS",
        "STILL TRUE",
    ],
)
def test_the_guidance_is_in_the_template(phrase):
    assert phrase in PROMPT_TEMPLATE, f"missing guidance: {phrase!r}"


def test_both_failures_are_named_before_they_are_referenced():
    """THE PRE-PASS FINDING: "those two failures" had no antecedent.

    The earlier wording said "Aim for the band between those two failures" while
    the text above it named none. The Skip list's last two bullets were BOTH the
    obviousness failure, and the too-specific one arrived as an instruction in a
    LATER paragraph -- so the nearest reading of "those two" was two adjacent
    bullets about the same failure. A model cannot aim between two things it has
    not been told.
    """
    obvious_at = PROMPT_TEMPLATE.find("TOO OBVIOUS")
    specific_at = PROMPT_TEMPLATE.find("TOO SPECIFIC")
    band_at = PROMPT_TEMPLATE.find("Aim between them")

    assert obvious_at != -1 and specific_at != -1, "a failure mode is not named"
    assert band_at != -1, "the band sentence is missing"
    assert obvious_at < band_at and specific_at < band_at, (
        "the band sentence refers back to failures that have not been named yet; "
        f"TOO OBVIOUS@{obvious_at} TOO SPECIFIC@{specific_at} band@{band_at}"
    )


def test_both_failure_modes_are_named_not_just_one():
    """The regression this guards: an earlier draft pushed only against specifics.

    Pushing only one way trades the stale-candidate failure for the obvious-
    candidate failure, which the blind judge ranked as the LARGER of the two.
    """
    pushes_against_specifics = "TOO SPECIFIC" in PROMPT_TEMPLATE
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
    assert "TOO SPECIFIC" in rendered
    assert "TOO OBVIOUS" in rendered
    # NOT a bare "{" check: the JSON item shape uses `{{` and renders to a real
    # brace, so that assertion fails on correct output. Look for an unrendered
    # NAMED placeholder instead -- the actual failure being guarded.
    import re

    leaked = re.findall(
        r"\{(label|summary|observations|existing|knowledge_types|max_items)\}", rendered
    )
    assert not leaked, f"unrendered placeholders: {leaked}"


def test_an_operator_override_replaces_the_guidance_entirely(tmp_path, monkeypatch):
    """W1: the commit claimed the tests cover the override path. They did not.

    `test_the_guidance_survives_rendering` writes no override file, so
    `_load_override` returns None and it renders the DEFAULT -- a useful check,
    but not of the override path. This exercises the real one, and records the
    consequence: an operator with a reflect override gets NONE of this guidance,
    because an override replaces the template rather than extending it.
    """
    from techne.config import Config
    from techne.core.prompts import render_prompt

    cfg = Config()
    cfg.data_dir = tmp_path
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "reflect.md").write_text("OVERRIDDEN. max={max_items}\n")

    rendered = render_prompt(
        cfg,
        "reflect",
        PROMPT_TEMPLATE,
        label="l",
        summary="s",
        observations="o",
        existing="(none)",
        knowledge_types="fact",
        max_items=5,
    )

    assert "OVERRIDDEN" in rendered, "the override was not loaded; this tests nothing"
    assert "TOO OBVIOUS" not in rendered, (
        "an override REPLACES the template — if this ever fails, overrides changed "
        "to extend, and the guidance story for override users changes with it"
    )
