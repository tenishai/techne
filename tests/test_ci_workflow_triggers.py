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


# --- vector coverage in the test leg (techne#535) --------------------------


def _job_steps(job: str) -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"][job]["steps"]


def _run_lines(job: str) -> str:
    return "\n".join(str(s.get("run", "")) for s in _job_steps(job))


def test_test_leg_installs_the_vectors_extra():
    """#535: without it, vector tests execute NOWHERE.

    This leg runs the whole suite but had no vector backend, so they SKIPPED;
    `eval-vec` has the backend but runs `-m eval_vec` only, so they were never
    SELECTED. Six files fell through: test_inference, test_search,
    test_embeddings, test_cli_vectors, test_checks, test_documents — none carry
    the marker. A skip that reads as green is worse than a documented gap
    (#453's argument, applied to the leg that hides it).
    """
    # Parsed out of the install SPEC, not matched as a substring of the whole
    # step. A bare `"vectors" in runs` would pass on an `echo vectors` or a stray
    # mention and could not tell an installed extra from a named one — poe_3's
    # #523 finding, where a substring match could not tell 7 from 78 or a comment
    # from a live directive. The comparison has to be as strict as the contract.
    import re

    specs = re.findall(r"-e\s+\"?\.\[([a-z0-9,._-]+)\]\"?", _run_lines("test"))
    assert specs, f"no editable install spec found in the `test` leg: {_run_lines('test')!r}"
    extras = {e.strip() for spec in specs for e in spec.split(",")}
    assert "vectors" in extras, (
        f"the `test` leg installs extras {sorted(extras)} — it must include "
        "`vectors`, or every vector test silently skips while the job reports green"
    )


def test_test_leg_prefetches_the_model_so_tests_do_not_skip_differently():
    """The extra alone is NOT the fix, and this is the assertion that says so.

    `backend_available()` only checks IMPORTABILITY, so installing the extra
    un-skips the tests — and the ones that matter then embed FOR REAL
    (test_cli_vectors.py:56: "exercises the real backfill_vectors() path, not a
    mock"). Without the pinned model on disk, each job re-downloads it.

    So the guard binds BOTH halves. Asserting only the extra would let someone
    delete the pre-fetch and still pass, which converts a silent skip into a
    silent per-job download — a different failure wearing the same green.
    """
    runs = _run_lines("test")
    assert "fastembed" in runs.lower(), (
        "the `test` leg must pre-fetch its embedding model, or the un-skipped "
        "tests download it on every job — a silent per-job fetch replacing a "
        "silent skip"
    )


def test_each_leg_prefetches_the_backend_ITS_python_actually_gets():
    """THE ONE THAT COST A RED BUILD, so it is pinned rather than remembered.

    `[vectors]` is split at the 3.14 boundary (techne#601, pyproject.toml:38/45):
    fastembed below, model2vec at-or-above, because onnxruntime has no 3.14
    wheel. The `test` matrix is 3.12/3.13 -> fastembed. `eval-vec` pins 3.14 ->
    model2vec.

    Copying eval-vec's model2vec pre-fetch into the test leg looked obviously
    right and failed instantly with ModuleNotFoundError, because the resolver had
    installed the OTHER backend. A pre-fetch must name the backend its own
    interpreter gets.
    """
    test_runs = _run_lines("test").lower()
    evalvec_runs = _run_lines("eval-vec").lower()

    assert "model2vec" not in test_runs, (
        "the `test` leg (python 3.12/3.13) resolves `[vectors]` to FASTEMBED — "
        "model2vec is not installed there (pyproject.toml:45 gates it to >=3.14), "
        "so a model2vec pre-fetch raises ModuleNotFoundError"
    )
    assert "model2vec" in evalvec_runs, (
        "the eval-vec leg pins python 3.14, which gets model2vec; if that changes, "
        "its pre-fetch and cache key need revisiting too"
    )


def test_the_two_legs_do_not_share_a_cache_key():
    """Different backends, different artifacts — so DIFFERENT keys, deliberately.

    This asserts the opposite of what an earlier draft of this file asserted, and
    the reversal is the point: sharing one key was correct only while both legs
    used model2vec. With fastembed on the matrix, a shared key would let a 3.14
    model2vec cache satisfy a 3.12 fastembed restore, reporting a cache hit while
    leaving the real download to happen anyway.
    """
    keys = {
        job: [
            s["with"]["key"]
            for s in _job_steps(job)
            if isinstance(s.get("with"), dict) and "key" in s.get("with", {})
        ]
        for job in ("test", "eval-vec")
    }
    assert keys["test"] and keys["eval-vec"], f"both legs need a cache step, got {keys}"
    assert keys["test"] != keys["eval-vec"], (
        f"the two legs cache DIFFERENT backends and must not share a key: {keys}"
    )
