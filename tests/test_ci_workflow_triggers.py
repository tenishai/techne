"""The CI workflow must arm every PR, not only those based on main (techne#530).

WHY THIS IS A TEST AND NOT A COMMENT: the failure it guards is invisible by
construction. A `pull_request: branches: [main]` filter matches the PR's BASE, so
a stacked PR matches no trigger and gets NO run — no red X, no skipped row, just
an EMPTY Checks tab that reads exactly like "nothing to report". Nothing in CI
can report a CI run that never started, and branch protection cannot backstop it:
required status checks are unavailable on this repo's plan (403 "Upgrade to
GitHub Pro or make this repository public"). The trigger is the only mechanism,
so it gets a guard that runs in the suite the trigger would have run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "test.yml"


def _triggers() -> dict:
    """The workflow's `on:` block.

    YAML 1.1 parses a bare `on` key as the BOOLEAN True, not the string "on" —
    which is why this looks up both. Getting that wrong yields a KeyError, not a
    silent pass, but it is the kind of thing a reader trips on.
    """
    doc = yaml.safe_load(WORKFLOW.read_text())
    return doc["on"] if "on" in doc else doc[True]


def test_pull_request_has_no_base_branch_filter():
    """THE INVARIANT. A base filter here silently disarms every stacked PR.

    Asserted as "the key is absent or carries no `branches`", not as equality
    against a whole expected dict, so adding an unrelated `pull_request` option
    (e.g. `types:`) does not fail spuriously — the guard should bind the ONE
    property that is load-bearing and stay quiet about the rest.
    """
    on = _triggers()
    assert "pull_request" in on, "the workflow must still run on pull_request at all"

    pr = on["pull_request"]
    if pr is None:  # `pull_request:` with no options — the shipped form
        return
    assert "branches" not in pr, (
        "pull_request must NOT filter on `branches`: the filter matches the PR's "
        "BASE, so a stacked PR (base = a feature branch) would match no trigger "
        "and get zero CI, with an empty Checks tab that looks like success. "
        f"Found: {pr}"
    )


def test_every_job_still_lives_in_this_one_workflow():
    """The blast radius of the trigger, pinned.

    This file is the repo's ONLY workflow, which is precisely why the missing
    trigger cost all three jobs rather than one. If a job ever moves to a second
    workflow file, that file needs its own trigger audit — and this assertion is
    what makes someone notice, rather than discovering it from an empty Checks
    tab on a PR that mattered.
    """
    workflows = sorted(p.name for p in WORKFLOW.parent.glob("*.yml"))
    assert workflows == ["test.yml"], (
        f"a second workflow appeared ({workflows}); audit its `on:` block for the "
        "techne#530 base-filter trap before adding it here"
    )

    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    assert set(jobs) == {"test", "eval-vec", "arch-cards"}, (
        f"job set changed: {sorted(jobs)}. All three ride this one trigger."
    )


def test_push_filter_is_still_scoped_to_main():
    """The counterpart, so removing the PR filter is not mistaken for removing
    both. `push` SHOULD stay scoped: without it every branch push would run the
    full matrix on a two-runner self-hosted pool, on top of the PR run. Measured
    2026-08-08: a base=main PR gets exactly one pull_request run per sha, and
    push fires only on main.
    """
    assert _triggers()["push"] == {"branches": ["main"]}
