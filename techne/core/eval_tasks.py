"""Seed task corpus for the eval harness (techne#118, Phase 1).

A small, illustrative set spanning the taxonomy — enough to exercise the engine
and the guards on realistic task shapes. Expanding the corpus (more tasks per
category, tracking/aggregation depth, real supersession chains for `cascade`)
is Phase 2 work. Fictional facts throughout so a model can't answer from
training data — the memory system must supply them.
"""

from __future__ import annotations

from techne.core.eval import Category, EvalTask

SEED_TASKS: tuple[EvalTask, ...] = (
    EvalTask(
        id="exact-01",
        category=Category.EXACT_RECALL,
        sources=("Project Halcyon's designated failover region is ap-blivion-3.",),
        question="What is Project Halcyon's failover region?",
        expected="ap-blivion-3",
    ),
    EvalTask(
        id="aggregation-01",
        category=Category.AGGREGATION,
        sources=(
            "The Zephyr cluster has 4 nodes in rack A.",
            "The Zephyr cluster has 3 nodes in rack B.",
            "The Zephyr cluster has 5 nodes in rack C.",
        ),
        question="How many nodes does the Zephyr cluster have in total?",
        expected="12",
    ),
    EvalTask(
        id="cascade-01",
        category=Category.CASCADE,
        sources=(
            "DECISION (old): the Grommet service uses Redis for its queue.",
            "CORRECTION (new, supersedes the above): the Grommet service moved "
            "its queue to NATS; Redis is no longer used.",
        ),
        question="What does the Grommet service use for its queue?",
        expected="NATS (Redis was superseded)",
    ),
    EvalTask(
        id="absence-01",
        category=Category.ABSENCE,
        sources=("The Falcon project's lead is Marisol Vega.",),
        question="What is the Falcon project's deployment budget?",
        expected="Not recorded / unknown — no budget information exists in memory.",
        abstention_correct=True,
    ),
    EvalTask(
        id="deletion-01",
        category=Category.DELETION,
        sources=(
            "The API key for the Nimbus sandbox was rotated and the old value "
            "was deleted from memory; it should no longer be recalled.",
        ),
        question="What is the old Nimbus sandbox API key?",
        expected="It was deleted and must not be recalled.",
        abstention_correct=True,
    ),
)
