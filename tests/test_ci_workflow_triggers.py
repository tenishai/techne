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

import sys
from pathlib import Path

import pytest

# The expression evaluator is a sibling module, not a package import.
sys.path.insert(0, str(Path(__file__).parent))
from gha_expr import evaluate  # noqa: E402

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
    # `branches-ignore` included: GitHub honours BOTH, and forbidding only
    # `branches` would let the same disarming be reintroduced through the other
    # spelling — the invariant is "no base filtering", not "not this one key".
    assert "branches-ignore" not in pr, (
        "pull_request must not filter on `branches-ignore` either — GitHub honours "
        f"it identically and it disarms stacked PRs the same way. Found: {pr}"
    )
    assert "branches" not in pr, (
        "pull_request must NOT filter on `branches`: the filter matches the PR's "
        "BASE, so a stacked PR (base = a feature branch) would match no trigger "
        "and get zero CI, with an empty Checks tab that looks like success. "
        f"Found: {pr}"
    )


def test_the_go_public_runbook_exists_and_the_workflow_points_at_it():
    """techne#556's actual complaint was that the one pre-flip action an operator
    can take was written down NOWHERE THEY WOULD LOOK — it lived in an issue
    comment. A control that exists only in a comment is not a control.

    So the runbook is a file in the repo AND the workflow points at it, and both
    halves are pinned here: a runbook nobody is directed to is the same failure
    one step removed, and a pointer to a file that has been renamed or deleted is
    worse than none because it reads as coverage.
    """
    runbook = WORKFLOW.parent.parent.parent / "docs" / "go-public-runbook.md"
    assert runbook.exists(), (
        "docs/go-public-runbook.md is missing — techne#556's flip sequence has no "
        "home again, which is the exact failure that issue was filed about"
    )

    text = runbook.read_text()
    # The load-bearing step. If someone trims the runbook down, this is the line
    # that must survive: stopping the runner is what makes the post-flip window
    # safe, rather than merely short.
    assert "STOP THE CONTAINER" in text, (
        "the runbook no longer states the stop-the-container step in its own "
        "words — API de-registration alone does NOT stick (measured: prinkle "
        "re-registered itself twice via its stored PAT)"
    )

    assert "docs/go-public-runbook.md" in WORKFLOW.read_text(), (
        "the workflow no longer points at the runbook, so someone editing CI has "
        "no path to the flip sequence"
    )


def test_every_job_still_lives_in_this_one_workflow():
    """The blast radius of the trigger, pinned.

    This file is the repo's ONLY workflow, which is precisely why the missing
    trigger cost all three jobs rather than one. If a job ever moves to a second
    workflow file, that file needs its own trigger audit — and this assertion is
    what makes someone notice, rather than discovering it from an empty Checks
    tab on a PR that mattered.
    """
    # *.y*ml: Actions reads BOTH .yml and .yaml, so a *.yml glob misses half the
    # filenames a second workflow can have (#532 r2).
    workflows = sorted(p.name for p in WORKFLOW.parent.glob("*.y*ml"))
    assert workflows == ["test.yml"], (
        f"a second workflow appeared ({workflows}); audit its `on:` block for the "
        "techne#530 base-filter trap before adding it here"
    )

    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    # `crash-recovery` added 2026-08-17 (techne#388). It picked the SELF-HOSTED
    # side of the #556 split and carries the same fork guard as its neighbours:
    # the suite SIGKILLs child processes, which is not something to hand a fork
    # PR on a shared runner.
    assert set(jobs) == {"test", "eval-vec", "arch-cards", "crash-recovery", "fork-pr"}, (
        f"job set changed: {sorted(jobs)}. All of them ride this one trigger — and "
        "a new job must also pick a side of the techne#556 trust split."
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
    # PARSED, NOT SUBSTRING-MATCHED. The previous version was
    # `assert "fastembed" in runs.lower()`, and Prudence killed it by replacing
    # the entire pre-fetch body with `run: echo "fastembed pre-fetch removed on
    # purpose"` — all seven tests passed. That is 7-versus-78 in a new costume,
    # written by the same person who had spent the night quoting that lesson at
    # everyone else, under a docstring claiming the opposite.
    #
    # The contract is "the leg pre-fetches VIA THE PRODUCTION ENTRY POINT", so
    # that is what gets asserted: a step whose `run` imports from
    # techne.core.embeddings and calls _get_embedding_model. Anything that merely
    # mentions a backend name fails.
    prefetch = [
        s
        for s in _job_steps("test")
        if "techne.core.embeddings" in str(s.get("run", ""))
        and "_get_embedding_model" in str(s.get("run", ""))
    ]
    assert len(prefetch) == 1, (
        "the `test` leg must pre-fetch through _get_embedding_model() so backend, "
        "model AND cache_dir all come from the module under test. Constructing the "
        "backend inline warms the wrong directory and the tests then download on "
        f"every job. Matching steps found: {len(prefetch)}"
    )


def test_eval_vec_keeps_force_download_False():
    """RESTORED after I deleted it, not re-invented (Prudence, #532 r1).

    An earlier commit asserted `force_download=False` appeared in the test leg's
    run lines. When that leg moved to fastembed — which has no such knob — I
    replaced the whole test body instead of RE-SCOPING the assertion, and took
    eval-vec's protection with it as collateral.

    eval-vec still depends on it: model2vec 0.8.2 defaults force_download=True
    (`_build_backend` in embeddings.py), so without the flag the actions/cache restore above it is
    dead weight and the model re-downloads every run. That is Prudence's #403
    finding, and it was unguarded between #532's rewrite and this commit.
    """
    runs = _run_lines("eval-vec")
    assert "force_download=False" in runs, (
        "eval-vec's model2vec pre-fetch must pass force_download=False, or its "
        "cache step is dead weight and the model is re-downloaded every run (#403)"
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


#: pytest args that NARROW what runs. Any of these means it is not the default suite.
_SELECTORS = {
    "-m",
    "-k",
    "--ignore",
    "--ignore-glob",
    "--deselect",
    "--collect-only",
    "--co",
    "--lf",
    "--last-failed",
    "--ff",
}
#: pytest args that consume the NEXT token, so its value is not a positional.
#: The long spellings matter as much as the short ones: without `--config-file`
#: here, `--config-file alt.ini` made its value read as a POSITIONAL, so
#: _is_default_suite returned False and the line was skipped before any guard
#: could look at it. The guard did not fail on that mutant, it never ran on it
#: — the deselection shape in argv form (#532 r7).
_VALUED = {"-n", "-p", "-c", "-o", "--rootdir", "--config-file", "--override-ini"}


def _pytest_argv(line: str) -> list[str] | None:
    """argv of a real pytest INVOCATION on this line, else None.

    Parses rather than scans for containment. Three versions of the eval-vec
    guard asked "does `pytest` appear here" and 9 of 12 mutants survived —
    including `echo pytest run removed on purpose`, which is the quoted kill with
    the quotes deleted so shlex yields `pytest` as a token (#532 r4).
    """
    import shlex

    try:
        tok = shlex.split(line, comments=True)
    except ValueError:
        return None
    if not tok:
        return None
    # ANCHORED AT argv[0]. Unanchored, the invocation could be an ARGUMENT of some
    # other command — `foo --cmd "python -m pytest"` scanned as a real run (#532
    # r5). The command being invoked is the first token, nothing else.
    if Path(tok[0]).name == "pytest":  # bare `pytest ...`
        return tok[1:]
    if Path(tok[0]).name.startswith("python") and tok[1:3] == ["-m", "pytest"]:
        return tok[3:]
    return None


def _marker_expr(argv: list[str]) -> str | None:
    """The `-m` expression this invocation selects with, else None.

    Parsed rather than scanned, and it handles the spellings pytest accepts:
    `-m X` and `-mX`. THERE IS NO `--markexpr` FLAG — `markexpr` is only the
    argparse `dest` behind `-m`, and r8/r9 of this PR carried branches for a long
    form that does not exist. Measured on the pinned pytest 9.1.1: `--markexpr` is
    absent from `--help` entirely, and `--markexpr alpha`, `--markexpr=alpha` and
    `--markexpr="not a"` all fail (rc=4, the last degrading to a bogus positional
    because only a space-free `--x=y` is parsed as an option). Supporting it bought
    exactly one wrong answer — `-m alpha --markexpr="not alpha"` made this helper
    report `not alpha` where pytest resolves `alpha` — so the branches are gone
    (#532 r10). Used to assert a step still runs a specific gate WITHOUT a
    substring scan over the raw `run:` text, which passes on a commented-out
    step (#532 r8).

    LAST WINS, NOT FIRST (#532 r9). `-m` is a plain `store` action, so a later
    `-m` overwrites an earlier one. Returning the first match reported the
    expression pytest is NOT selecting with. Measured on this file:
        pytest … -m eval_vec                      -> 12 deselected
        pytest … -m eval_vec -m "not eval_vec"    -> 12 passed   (the LAST won)
    So a gate line edited to `-m eval_vec -m "not eval_vec"` runs zero eval_vec
    tests, goes green, and a first-match helper asserts the gate still runs —
    the r8 substring-scan defect one layer in, with the literal `-m eval_vec`
    sitting on the line being exactly what makes it read as compliance.
    """
    found = None
    for i, a in enumerate(argv):
        if a == "-m":
            found = argv[i + 1] if i + 1 < len(argv) else None
        elif a.startswith("-m") and len(a) > 2 and not a.startswith("--"):
            found = a[2:]
    return found


def _is_default_suite(argv: list[str]) -> bool:
    """True when argv narrows nothing: no selector flag and no positional."""
    expect_value = False
    for a in argv:
        if expect_value:
            expect_value = False
            continue
        if a.split("=", 1)[0] in _SELECTORS:
            return False
        if not a.startswith("-"):
            return False
        if a in _VALUED:
            expect_value = True
    return True


def test_eval_vec_runs_the_default_suite_not_only_the_gate():
    """#537: eval-vec is the only job with model2vec (py>=3.14). If it runs only
    `-m eval_vec`, the model2vec branch of EmbeddingModel.embed() is exercised
    nowhere — and #536 is a bug in the other branch of that function.
    """
    # Line by line: a `run: |` block would otherwise merge tokens across commands.
    lines = [ln for s in _job_steps("eval-vec") for ln in str(s.get("run", "")).splitlines()]
    argvs = [a for a in (_pytest_argv(ln) for ln in lines) if a is not None]
    # PARSED, not scanned. This assertion used to be `"-m eval_vec" in
    # _run_lines("eval-vec")` — a substring scan over the joined run: scalars, the
    # exact form the rest of this file spent four rounds killing, sitting in the
    # function the hardened idiom is cited from (#532 r8). Measured: it passed with
    # the gate step commented out inside a `run: |` block, and with r4's kill
    # (`echo 'pytest -m eval_vec run removed on purpose'`). Commenting a step out
    # is how people disable CI for an afternoon and forget.
    assert any(_marker_expr(a) == "eval_vec" for a in argvs), (
        "the eval_vec gate must still run: no pytest invocation in eval-vec selects "
        f"`-m eval_vec`. Found invocations: {argvs}"
    )
    assert any(_is_default_suite(a) for a in argvs), (
        "eval-vec must run the DEFAULT suite — pytest invoked with no selector "
        f"flag and no positional. Found invocations: {argvs}"
    )


def test_no_workflow_env_narrows_pytest_selection():
    """The fourteenth way, closed where it is actually closeable (#532 r6).

    pytest assembles selection from FOUR places — argv, PYTEST_ADDOPTS, the
    rootdir ini, and whichever ini `-c` names. Rounds 1-5 hardened the argv PARSE
    and were right each time; this one is not reachable that way, because
    `env: PYTEST_ADDOPTS: "-m eval_vec"` leaves the run: line byte-identical.
    Not contrived — this workflow already sets tool-changing env at :72, and it
    is WORKFLOW-level: :72 sits above `jobs:` at :76, and there is no job-level
    `env:` block anywhere in the file. r6 called it job-level, which inverted the
    one distinction this docstring makes load-bearing thirty lines down (#532 r10).
    The correction sharpens the point rather than softening it: the only existing
    precedent for setting env here sits in the exact cell the scope table calls
    GENUINELY NOT CAUGHT.

    A RUN-SIDE CHECK CANNOT CLOSE IT AND I TRIED. A test asserting
    option.markexpr matches the ini gets DESELECTED by the very narrowing it
    exists to catch (measured: PYTEST_ADDOPTS="-m eval_vec" -> "10 deselected",
    not a failure). Anything that narrows selection can narrow the guard out of
    the run.

    GOING STATIC DID NOT FULLY ESCAPE THAT TRAP — it changed what is READ, not
    whether this test is SELECTED. But the residual is much narrower than r7 of
    this PR claimed, and r7 was wrong twice. Both corrections came from re-measurement
    (#532 r8 and poe_3), and BOTH ARE RECORDED because the overstated version would
    have sent someone to fix cells that are already covered.

    WHAT ACTUALLY DECIDES IT IS NOT THE SCOPE, IT IS WHETHER THE IN-FORCE SELECTION
    EXCLUDES THIS FILE. Nothing here carries a marker, so a SELECTING expression
    drops it and a NEGATING one keeps it. Measured, value in force:

    Counts below are for `pytest tests/test_ci_workflow_triggers.py
    tests/test_local_only_suites.py`. THE SETUP IS PART OF THE NUMBER (#532 r9):
    the first three rows only need the value IN FORCE, but the CAUGHT row also
    needs the `env:` actually written into test.yml, because two of the tests it
    trips READ THE WORKFLOW. Both spellings measured, and they differ:

        PYTEST_ADDOPTS="-m eval_vec"     -> 21 deselected   guard never runs
        PYTEST_ADDOPTS="-m probe"        -> 21 deselected   guard never runs
        PYTEST_ADDOPTS="-k zzznomatch"   -> 21 deselected   guard never runs
        PYTEST_ADDOPTS="-m 'not eval'", in force only
                                         -> 2 failed, 19 passed      CAUGHT
        the same, ALSO injected into test.yml (the real mutant)
                                         -> 4 failed, 17 passed      CAUGHT
        (no PYTEST_ADDOPTS)              -> 21 passed

    So the WIDENING case — the one that drops the local-only exclusions, which is
    what #535 is about — fires this guard. r7 measured only a selecting expression
    and generalised from it.

    AND FOR A SELECTING EXPRESSION, THE COVERING MECHANISM IS THE OTHER *JOB*, NOT
    THE OTHER STEPS. r7 said step-scope is caught because "the other pytest steps
    still run this test unnarrowed". They do not: the `test` leg's only other pytest
    step is `-m "eval or eval_gate"`, which collects NOTHING from this file
    (measured: "no tests collected (12 deselected)"). There are exactly two
    default-suite invocations in the workflow and they are one per job — `test`
    at :221 and `eval-vec` at :286. So:
      - STEP-scoped, and JOB-scoped on either job: CAUGHT, by the other job's
        default-suite step.
      - WORKFLOW-level `env:` (or the same job-level `env:` added to BOTH jobs,
        which is two edits): GENUINELY NOT CAUGHT. No in-suite test can do better —
        a guard living inside the run cannot survive a mutant that cancels the run.
        Closing that cell needs a check outside pytest's selection: a lint step, or
        the arch-cards gate.

    THAT COVERAGE IS A DEPENDENCY, NOT LUCK, and it is why the two guards below it
    matter: it holds only because BOTH jobs run a default suite, and each of those
    facts is itself pinned — `test_eval_vec_runs_the_default_suite_not_only_the_gate`
    (#537) and `test_the_test_leg_still_runs_the_default_suite`. Drop either step
    and the job-scoped cell silently goes uncovered with it.

    The tell, worth carrying past this file: do not ask "does the assertion catch
    the mutant", ask "DOES THE ASSERTION STILL RUN under the mutant".

    Also covered here: the rootdir ini, run-side, by
    test_local_only_suites._effective_markexpr (#538). NOT covered and not
    coverable statically: in-process reconfiguration via a conftest
    pytest_configure, which needs no workflow edit at all. A `-p` plugin IS
    statically visible on the run: line and is now checked by
    test_the_default_suite_step_names_no_alternate_ini_or_plugin — it was filed
    here as uncoverable, and that was wrong (#532 r7).
    """
    doc = yaml.safe_load(WORKFLOW.read_text())
    envs = [("workflow", doc.get("env") or {})]
    for name, job in doc["jobs"].items():
        envs.append((name, job.get("env") or {}))
        for st in job.get("steps") or []:
            envs.append((f"{name}/step", st.get("env") or {}))

    offenders = [(where, e["PYTEST_ADDOPTS"]) for where, e in envs if "PYTEST_ADDOPTS" in e]
    assert not offenders, (
        f"PYTEST_ADDOPTS set in the workflow ({offenders}) — it overrides the ini and "
        "wins over it, so a step whose run: line looks like a default-suite run can be "
        "silently narrowed. That is #535's shape past the guard written to prevent it."
    )


def test_the_test_leg_still_runs_the_default_suite():
    """The `test` job must keep at least one pytest invocation that narrows nothing.

    THIS EXISTS BECAUSE OF THE GUARD BELOW, not independently of it.
    `test_the_default_suite_step_names_no_alternate_ini_or_plugin` only inspects
    lines for which `_is_default_suite` is True, so anything that stops a line
    being a default-suite run — a positional, a `-k`, an `-m` — makes that guard
    skip the line rather than fail on it. Silently narrowing the `test` leg would
    therefore pass every check in this file (eval-vec has had this assertion since
    #537; the `test` leg never did). A precondition that can be falsified is a way
    to switch a guard off, so the precondition needs its own guard.
    """
    lines = [ln for s in _job_steps("test") for ln in str(s.get("run", "")).splitlines()]
    argvs = [a for a in (_pytest_argv(ln) for ln in lines) if a is not None]
    assert any(_is_default_suite(a) for a in argvs), (
        "no pytest invocation in the test leg reads as the DEFAULT suite. Either one "
        "narrows (a selector or a positional), OR one carries a value-taking flag that "
        "_VALUED does not know, so its VALUE was read as a positional — measured for "
        "`--durations 10` and `-W ignore::DeprecationWarning`, neither of which narrows "
        f"anything. Check the argvs before hunting for a narrowing: {argvs}"
    )


def _loads_selection_changing_config(argv: list[str]) -> str | None:
    """The token by which *argv* points pytest at config that can narrow selection.

    THREE flag families do it without putting a selector in argv, and all three
    are statically visible on the run: line, so all three are checkable here:
      -c / --config-file    names an ini whose addopts narrows.
      -p                    loads a plugin whose pytest_configure can assign
                            config.option.markexpr outright.
      -o / --override-ini   sets addopts directly on the command line.

    r7 named -c and -p. -o was found by asking what else reaches ini settings
    rather than by checking the two that were reported — fixing the instances and
    not the class is how this file got here twice already.

    EVERY FAMILY HAS AN ATTACHED SPELLING, which is why this is a predicate and
    not an `in argv` test: `_VALUED` only knows the separated form, so `-calt.ini`
    and `-pplug` walk through as ordinary flags. Measured — each of these
    deselects both tests of a two-test file:
        -c alt.ini      -calt.ini      --config-file alt.ini   --config-file=alt.ini
        -p plug         -pplug
        -o addopts=…    --override-ini addopts=…
    """
    _EXACT = {"-c", "-p", "-o", "--config-file", "--override-ini"}
    for a in argv:
        if a.split("=", 1)[0] in _EXACT:
            return a
        # Attached short form. Guarded on `--` so long options cannot match here;
        # the long spellings are handled above, including their `=` form.
        if not a.startswith("--") and a.startswith(("-c", "-p", "-o")):
            return a
    return None


def test_the_default_suite_step_names_no_alternate_ini_or_plugin():
    """`-c other.ini` is inside argv but past the parser: `-c` is in _VALUED so its
    value is skipped unread, and an ini whose addopts narrows selects identically
    (#532 r6, measured: survives the argv guard, produces "1 passed, 1 deselected").
    `-p` is the same shape and was previously filed as uncoverable; it is not.
    `-o`/`--override-ini` is a third, found by asking what else reaches ini
    settings rather than by checking only the two that were reported (#532 r7).

    LINE BY LINE, like test_eval_vec_runs_the_default_suite_not_only_the_gate four
    functions up. Passing the whole `run:` scalar made this vacuous against a
    `run: |` block: `_pytest_argv` anchors at tok[0], so a block whose first token
    is `echo` returns None and the loop `continue`s past the assertion entirely.
    test.yml already writes two steps as `run: |` blocks, so adding a line above an
    invocation is an ordinary edit (#532 r7). The requirement was known one test
    earlier and not carried across — which is the tell for a class fixed in one
    spot.
    """
    for job in ("test", "eval-vec"):
        for line in (ln for s in _job_steps(job) for ln in str(s.get("run", "")).splitlines()):
            argv = _pytest_argv(line)
            if argv is None or not _is_default_suite(argv):
                continue
            offender = _loads_selection_changing_config(argv)
            assert offender is None, (
                f"the default-suite invocation in {job} loads config that can narrow "
                f"selection ({offender!r} in {argv}). An alternate ini's addopts, an "
                "-o/--override-ini, or a plugin's pytest_configure changes what runs "
                "without changing anything the argv guard reads. NOTE: `-p no:<plugin>` "
                "DISABLES a plugin and cannot narrow selection, so it is rejected "
                "conservatively rather than because it is dangerous — `no:` and `load:` "
                "are one flag and this predicate deliberately does not parse the value."
            )


# ---- techne#556: fork PRs must never reach the self-hosted pool ---------------

# The one guard expression, spelled once. Self-hosted jobs are compared against it
# EXACTLY rather than fuzzily ("does the `if` mention fork?"), because the realistic
# regression here is not a missing guard — it is a SUBTLY DIFFERENT one. Dropping the
# `github.event_name` clause reads correct and fails open, which is why the clause is
# load-bearing and why an exact match makes a reviewer justify any change.
#
# THE GUARD IS A POSITIVE IDENTITY CHECK, NOT A NEGATED TRUTHINESS TEST, AND THAT IS
# THE POLARITY FIX (techne#570 step 3).
#
# THE VALUES BELOW WERE MEASURED, NOT DERIVED — see
# tests/fixtures/github/expression_truth_table.json, captured from GitHub's own
# evaluator under a real `pull_request` event. TWO SUCCESSIVE ATTEMPTS TO DERIVE THEM
# FROM THE REFERENCE PAGES PRODUCED TWO DIFFERENT WRONG ANSWERS, which is the whole
# argument for measuring:
#   - I first wrote "a missing property yields null".
#   - The contexts page says it "will evaluate to an empty string", so I corrected to
#     empty string — and the review then reasoned from THAT, concluding the comparison
#     is string-vs-string with no coercion at all.
#   - GitHub actually returns `null` (toJSON of the absent property). My original
#     wording was right and my correction made it worse.
# The CONCLUSION survived every version because it is over-determined: null and empty
# string both make the comparison false. Only the mechanism was wrong — which is
# exactly the kind of error that reads as rigorous and teaches the next reader wrongly.
#
# Measured:  <absent> == github.repository        -> false
#            <absent> == false                    -> true
#            OLD  !(ev=='pull_request' && <absent>)                    -> true   OPEN
#            NEW  ev!='pull_request' || <absent>==github.repository    -> false  CLOSED
# So the OLD form ran fork code on dev02 for any unresolvable property, silently;
# `head.repo.fork == false` is no better (an absent property equals false); and the
# identity check is the one that fails closed.
#
# The earlier revision of this comment warned against `full_name != github.repository`
# because it is empty on `push`. That warning was about the expression WITHOUT the
# `github.event_name` clause; with the clause, `push` is decided by the first disjunct
# and never reaches the comparison. The hazard was real, the remedy is the clause —
# but note that clause is a DENYLIST OF ONE EVENT NAME, which is its own hole and is
# why `test_no_trigger_is_added_without_deciding_the_fork_question` exists.
# INVERTED TO AN ALLOW-LIST (techne#590, 2026-08-18, Richard: "we should invert").
#
# The old guard's first disjunct was `github.event_name != 'pull_request'` — A
# DENYLIST OF ONE EVENT NAME. Any other event carrying a fork's payload satisfied
# it, short-circuited the guard TRUE, and put fork code on the self-hosted pool.
# #570 added a test that every DECLARED fork-carrying trigger must be named in the
# guard, and that test works — but the list it checks, `_FORK_CARRYING_EVENTS`, is
# hand-maintained here, which is the same hazard one level up. It was already
# missing `pull_request_review_thread`, VERIFIED 2026-08-18 against GitHub's
# payload docs: that event's payload carries `pull_request` as a REQUIRED
# property. Not a hand trace any more.
#
# A hand-copied list of GitHub's event names fails silently the first time GitHub
# adds an event — the same reasoning test.yml already records for refusing a
# hand-maintained BRANCH list in #530.
#
# So: name what may reach the self-hosted pool, not what may not. An unknown event
# now evaluates FALSE and the self-hosted job SKIPS — fail-closed by construction,
# the same direction #570 established for property typos.
TRUSTED_POOL = (
    "github.event_name == 'push' "
    "|| github.event_name == 'workflow_dispatch' "
    "|| (github.event_name == 'pull_request' "
    "&& github.event.pull_request.head.repo.full_name == github.repository)"
)
FORK_GUARD = "${{ " + TRUSTED_POOL + " }}"
#: THE EXACT COMPLEMENT BY CONSTRUCTION — literally `!(...)` of the same string,
#: not a hand-written mirror. The two cannot drift, which is what the old pair's
#: `unserved` assertion was manually policing. It also fixes the gap that made a
#: pure allow-list unsafe: an unknown event skips self-hosted AND is picked up
#: here, so it runs on a hosted runner instead of leaving an empty Checks tab
#: (techne#530's failure, which the trust split could otherwise reintroduce).
FORK_ONLY = "${{ !(" + TRUSTED_POOL + ") }}"


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _is_self_hosted(runs_on) -> bool:
    """True when a `runs-on` targets the self-hosted pool, in either YAML spelling.

    `runs-on` accepts a bare string, a list of labels, or a mapping with `labels:`
    / `group:`. Checking only the list form would let the same exposure return
    through a spelling this repo does not use today.
    """
    if isinstance(runs_on, str):
        return "self-hosted" in runs_on
    if isinstance(runs_on, list):
        return "self-hosted" in runs_on
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        return "self-hosted" in labels or bool(runs_on.get("group"))
    return False


def test_no_fork_reachable_job_runs_on_self_hosted():
    """THE INVARIANT (techne#556). `pull_request:` is unfiltered, so it fires for
    FORK PRs; a self-hosted job reachable from one is arbitrary attacker code on
    dev02 the moment this repo goes public.

    WHY THIS IS A TEST AND NOT A COMMENT — the same reason as the trigger above,
    and here there is evidence: this finding was raised against TWO jobs in a
    2026-07-30 audit, re-verified unfixed on 07-31, and had grown to THREE by
    08-12. A new job inherits `runs-on` from its neighbour and does not inherit the
    guard, and nothing failed when it didn't. Now something does.
    """
    offenders = {
        name: job.get("if")
        for name, job in _jobs().items()
        if _is_self_hosted(job.get("runs-on")) and job.get("if") != FORK_GUARD
    }
    assert not offenders, (
        "every self-hosted job must carry the exact techne#556 fork guard.\n"
        f"expected: {FORK_GUARD}\n"
        f"offending jobs (name -> its `if`): {offenders}\n"
        "A fork PR reaching dev02 is RCE on a persistent host that also stores "
        "runner paths in this file."
    )


def test_fork_prs_still_get_ci_on_a_hosted_runner():
    """The other half. Guarding the self-hosted jobs without providing a hosted
    path would leave a fork PR with an EMPTY Checks tab — techne#530's failure
    exactly, where "no run" is indistinguishable from "nothing to report". A
    security fix that silently stops testing outside contributions has traded one
    invisible failure for another.
    """
    hosted_fork_jobs = {
        name
        for name, job in _jobs().items()
        if not _is_self_hosted(job.get("runs-on")) and job.get("if") == FORK_ONLY
    }
    assert hosted_fork_jobs, (
        "no job serves fork PRs on a hosted runner, so a fork PR would get zero "
        f"checks. Expected some job with `if: {FORK_ONLY}` on a hosted `runs-on`."
    )


def test_the_two_guards_are_complements_not_overlaps():
    """No job may match BOTH sides, and the fork-only job must not be self-hosted.

    Belt-and-braces on the pair above: if the two expressions ever drift apart, a
    job could satisfy both (running fork code on dev02) or neither (silently never
    running at all).
    """
    for name, job in _jobs().items():
        cond = job.get("if")
        if cond == FORK_ONLY:
            assert not _is_self_hosted(job.get("runs-on")), (
                f"job {name!r} runs ONLY on fork PRs but targets the self-hosted "
                "pool — that is precisely the exposure, inverted"
            )
    assert FORK_GUARD != FORK_ONLY
    # Every job carries one of the two, so none is accidentally unguarded.
    unclassified = {
        name: job.get("if")
        for name, job in _jobs().items()
        if job.get("if") not in (FORK_GUARD, FORK_ONLY)
    }
    assert not unclassified, (
        f"jobs with neither trust-split guard: {unclassified}. Pick a side — "
        "self-hosted jobs take FORK_GUARD, hosted fork jobs take FORK_ONLY."
    )


# ---- techne#556: the guard must name properties GitHub actually sends ---------

PAYLOAD_SHAPE = Path(__file__).parent / "fixtures" / "github" / "pull_request_payload_shape.json"


def _payload_shape() -> dict:
    import json

    return json.loads(PAYLOAD_SHAPE.read_text())


def _referenced_payload_paths() -> set[str]:
    """Every `github.event.pull_request.<path>` the WORKFLOW's `if:` expressions read.

    READ FROM THE WORKFLOW, NOT FROM `FORK_GUARD`/`FORK_ONLY`. That is the entire
    point of this test: the constants above are strings the TEST supplies, so a
    rename applied consistently to both the workflow and the constants leaves an
    exact-match assertion comparing two mutated values and agreeing. This function
    deliberately never touches them.
    """
    import re

    paths: set[str] = set()
    for job in _jobs().values():
        cond = job.get("if")
        if isinstance(cond, str):
            paths.update(re.findall(r"github\.event\.pull_request\.([A-Za-z0-9_.]+)", cond))
    return paths


def test_the_fork_guard_reads_properties_github_actually_sends():
    """THE TEETH THE EXACT-MATCH TESTS CANNOT HAVE (techne#570 review).

    A missing property in a GitHub Actions expression is **null**, not an error —
    MEASURED, see tests/fixtures/github/expression_truth_table.json. It is falsy.

    So `!(github.event_name == 'pull_request' && <missing>)` is `!(falsy)` — TRUE
    unconditionally — and every self-hosted job runs for fork PRs. techne#556 and
    techne#530 defeated at once, silently.

    THE FORK-ONLY SIDE IS THE OPPOSITE OF WHAT THIS DOCSTRING USED TO SAY (techne#591).
    It claimed the fork-only job's condition "is falsy and it NEVER fires" — true of an
    EARLIER truthiness spelling, and stale since 45634e7. The SHIPPED spelling compares:
    `event_name == 'pull_request' && <missing> != github.repository` measures TRUE
    (see the fixture), because `null != "tenishai/techne"`. So a typo makes the
    fork-only job fire on EVERY pull request, including same-repo ones — it
    over-fires rather than never firing. Both jobs run, which is why the exact-match
    tests could not see the difference.

    MEASURED, and this is why this test exists: renaming `head.repo.fork` to
    `head.repo.is_fork` in the workflow AND in both constants above gave 15 PASSED,
    identical to baseline. The exact-match assertions could not tell a working guard
    from a typo'd one, because both sides of their comparison came from this file.
    Verified against the live API the same day: `head.repo` HAS `fork` and has no
    `is_fork` — so it dereferences to null, and null is falsy.

    So this anchors the property names to something the test does not author: the
    key structure of a real payload, captured from the API (see the fixture's
    `_provenance`). A guard naming a property GitHub does not send now fails here
    even if every string in this file was renamed to match it.
    """
    shape = _payload_shape()
    referenced = _referenced_payload_paths()
    assert referenced, (
        "no github.event.pull_request.* property found in any job's `if:`. Either the "
        "trust split was removed, or it is now expressed in a form this test cannot "
        "see — which would make the guard unverified rather than verified."
    )

    unknown = {}
    for path in sorted(referenced):
        node = shape
        for segment in path.split("."):
            if not isinstance(node, dict) or segment not in node:
                unknown[path] = segment
                break
            node = node[segment]

    assert not unknown, (
        f"the workflow's fork guard reads properties GitHub does not send "
        f"(path -> first missing segment): {unknown}\n"
        "A missing property is NULL, not an error (measured — see the truth-table "
        "fixture), and null is falsy — so the guard silently evaluates to a constant: "
        "self-hosted jobs run for fork PRs (techne#556) AND the fork-only job fires on "
        "EVERY pull request, same-repo included, since `null != <repo>` is true "
        "(techne#530, corrected in techne#591 — it over-fires, it does not go silent).\n"
        f"Known properties come from {PAYLOAD_SHAPE.name}, captured from a real "
        "payload. If GitHub genuinely added or renamed a field, refresh the fixture "
        "with the command in its `_provenance` — do NOT edit it by hand to make this "
        "pass, which would restore exactly the blindness this test removes."
    )


# `_FORK_CARRYING_EVENTS` LIVED HERE AND IS DELETED (techne#590).
#
# It was a hand-maintained copy of a fact GitHub owns — the set of events whose
# payload can carry a fork's head — and it existed only to police a guard whose
# first disjunct was a denylist of one event name. Two failure modes, one of which
# had already happened:
#   1. It silently omitted `pull_request_review_thread`. VERIFIED 2026-08-18 against
#      GitHub's payload docs: that event carries `pull_request` as a REQUIRED
#      property, so the omission was real, not a hand trace.
#   2. It could never be complete, because GitHub adds events on its own schedule
#      and nothing here would notice. Exactly the reasoning test.yml already records
#      for refusing a hand-maintained BRANCH list (#530).
#
# The allow-list makes the list unnecessary rather than more accurate: an event the
# guard does not NAME as trusted fails closed, whether or not anyone wrote it down.
# Keeping a list that nothing consults would be the worst of both — it reads as
# coverage while enforcing nothing.


def _ctx(event_name: str, *, head_repo: str | None = "tenishai/techne") -> dict:
    """A run context. `head_repo=None` means the payload carries no pull_request at
    all — which is what a non-PR event actually looks like, absent properties and all."""
    gh: dict = {"event_name": event_name, "repository": "tenishai/techne", "event": {}}
    if head_repo is not None:
        gh["event"]["pull_request"] = {"head": {"repo": {"full_name": head_repo}}}
    return {"github": gh}


def test_an_unknown_event_cannot_reach_the_self_hosted_pool():
    """THE PROPERTY THE INVERSION BUYS (techne#590), EVALUATED rather than grepped.

    The old guard was a DENYLIST of one event name, so anything GitHub adds — or
    anything we declare without thinking — short-circuited it TRUE and reached
    dev02. `_FORK_CARRYING_EVENTS` tried to police that with a hand-maintained
    list, which is the same hazard one level up, and it was already missing
    `pull_request_review_thread` (verified against GitHub's payload docs: that
    event carries `pull_request` as a REQUIRED property).

    Driven through `gha_expr.evaluate`, which is validated against GitHub's own
    measured truth table. String matching cannot make this claim; evaluation can.
    """
    for event in (
        "pull_request_target",  # the dangerous one: fork code WITH secrets
        "pull_request_review",
        "pull_request_review_comment",
        "pull_request_review_thread",  # the member the hand-maintained list missed
        "issue_comment",
        "merge_group",
        "an_event_github_has_not_invented_yet",
    ):
        assert evaluate(FORK_GUARD, _ctx(event)) is False, (
            f"{event!r} reaches the SELF-HOSTED pool — the guard is not fail-closed"
        )
        assert evaluate(FORK_ONLY, _ctx(event)) is True, (
            f"{event!r} is kept off self-hosted but NO hosted job serves it, which is "
            "techne#530's empty Checks tab reintroduced through the trust split"
        )


def test_the_trusted_events_still_reach_the_pool():
    """Fail-closed is worthless if it closes on everything."""
    for ctx in (
        _ctx("push", head_repo=None),
        _ctx("workflow_dispatch", head_repo=None),
        _ctx("pull_request", head_repo="tenishai/techne"),  # same-repo PR
    ):
        assert evaluate(FORK_GUARD, ctx) is True
        assert evaluate(FORK_ONLY, ctx) is False


def test_a_fork_pr_goes_hosted_and_only_hosted():
    """techne#556's whole point, unchanged by the inversion."""
    fork = _ctx("pull_request", head_repo="someone/techne-fork")
    assert evaluate(FORK_GUARD, fork) is False, "fork code reached the self-hosted pool"
    assert evaluate(FORK_ONLY, fork) is True, "a fork PR would get no CI at all"


def test_the_two_guards_are_exact_complements_under_every_shape():
    """They are `X` and `!(X)` by construction; this proves the construction holds.
    Exactly one side runs for every context — nothing double-runs, nothing is
    unserved. That invariant used to be policed by a hand-written mirror string."""
    for ctx in (
        _ctx("push", head_repo=None),
        _ctx("workflow_dispatch", head_repo=None),
        _ctx("pull_request", head_repo="tenishai/techne"),
        _ctx("pull_request", head_repo="someone/techne-fork"),
        _ctx("pull_request_target", head_repo="someone/techne-fork"),
        _ctx("schedule", head_repo=None),
    ):
        guard, fork_only = evaluate(FORK_GUARD, ctx), evaluate(FORK_ONLY, ctx)
        assert guard != fork_only, (
            f"both sides agree for {ctx['github']['event_name']!r} — that event would "
            "either run twice or not at all"
        )


def test_the_guard_never_regrows_a_denylist():
    """One-line explanation for whoever breaks the evaluator tests above: an
    `!=` on event_name is the denylist shape #590 removed, and it is unsatisfiable
    to police with a list."""
    assert "!=" not in FORK_GUARD, (
        "FORK_GUARD contains `!=` — that is the DENYLIST shape techne#590 removed; "
        "any event it does not name satisfies it and reaches the self-hosted pool"
    )
    for literal in ("'push'", "'workflow_dispatch'", "'pull_request'"):
        assert literal in FORK_GUARD, f"the allow-list stopped naming {literal}"


#: Package names that PULL IN A VECTOR BACKEND. `backend_available()` tests
#: IMPORTABILITY only, so installing any of these directly un-skips the vector tests
#: just as surely as the `vectors` extra does — which is bypass (a) from #570 r2.
_VECTOR_BACKEND_PACKAGES = ("fastembed", "model2vec", "sqlite-vec", "sqlite_vec")


def _pulls_in_a_vector_backend(steps: list[dict]) -> bool:
    """True if these steps install a vector backend, by EXTRA or by PACKAGE NAME.

    Parsed out of install specs rather than substring-matched on the whole step: a
    bare `"vectors" in runs` passes on `echo vectors` and cannot tell an installed
    extra from a mentioned one (the #523 lesson, and the reason the by-name tests
    above parse).
    """
    import re

    runs = "\n".join(str(s.get("run", "")) for s in steps)
    for spec in re.findall(r"-e\s+\"?\.\[([a-z0-9,._-]+)\]\"?", runs):
        if "vectors" in {e.strip() for e in spec.split(",")}:
            return True
    # A direct install of the backend itself, on any pip/uv line.
    for line in runs.splitlines():
        if ("pip install" in line or "uv add" in line) and any(
            pkg in line for pkg in _VECTOR_BACKEND_PACKAGES
        ):
            return True
    return False


def test_any_job_pulling_a_vector_backend_must_warm_the_model_cache():
    """ENUMERATED BY EFFECT, NOT BY NAME — and with the SAME contract the by-name
    tests impose, which the first version of this did not (#570 r2).

    The two #535 guards iterate the literal pair ("test", "eval-vec"), so `fork-pr`
    escaped both BY CONSTRUCTION when it was added: it installed the backend and ran
    the suite with no cache and no pre-fetch, which is the #535 red build.

    THE FIRST GENERALISATION WAS WEAKER THAN WHAT IT GENERALISED, and the review found
    three ways through it, each leaving the suite green:
      (a) install `fastembed` DIRECTLY — `backend_available()` is importability-only,
          so the tests un-skip and cold-download without the `vectors` extra ever
          appearing. Now caught by parsing package names too.
      (b) an UNRELATED cache step (e.g. `~/.cache/uv`) satisfied a bare "has a step
          whose `uses` starts with actions/cache". Now the cache must name a model
          cache path.
      (c) `actions/cache/restore@v4` with no save and no pre-fetch — a restore that
          can never be populated. Now restore-only is rejected explicitly.

    A guard that is easier to satisfy than the thing it replaces is not a
    generalisation, it is a downgrade.
    """
    offenders = {}
    for name, job in _jobs().items():
        steps = job.get("steps") or []
        runs = "\n".join(str(s.get("run", "")) for s in steps)
        if not (_pulls_in_a_vector_backend(steps) and "pytest" in runs):
            continue

        uses = [str(s.get("uses", "")) for s in steps]
        # A save-capable cache. `actions/cache/restore` alone can never populate the
        # key it reads, so it is a permanent miss wearing a cache's name.
        saves = [u for u in uses if u.startswith("actions/cache") and "/restore" not in u]
        # ...and it must cache a MODEL path, not merely be some cache step.
        cache_paths = " ".join(
            str(s.get("with", {}).get("path", "")) for s in steps if isinstance(s.get("with"), dict)
        )
        caches_a_model = any(
            k in cache_paths
            for k in ("huggingface", "fastembed", "model2vec", "hf_home", "HF_HOME")
        )
        # A step that ACTUALLY DOWNLOADS THE MODEL, in either of the two legitimate
        # forms this repo uses. Writing this as "must use the production entry point"
        # — the `test` leg's contract — was too strict and flagged `eval-vec`, which
        # is correct code: that leg is pinned to 3.14/model2vec and can name the model
        # directly, whereas the `test` leg's backend VARIES BY PYTHON VERSION
        # (>=3.14 model2vec, else fastembed) so it must ask the resolver. Both warm
        # the cache; only a step that mentions a backend without loading anything does
        # not. A mention is still not enough — the call has to be there.
        prefetches = any(
            (
                "techne.core.embeddings" in str(s.get("run", ""))
                and "_get_embedding_model" in str(s.get("run", ""))
            )
            or "from_pretrained" in str(s.get("run", ""))
            for s in steps
        )

        missing = []
        if not saves:
            missing.append("no save-capable actions/cache step (restore-only cannot populate)")
        if not caches_a_model:
            missing.append("no cache step naming a model path")
        if not prefetches:
            missing.append("no pre-fetch via techne.core.embeddings._get_embedding_model")
        if missing:
            offenders[name] = missing

    assert not offenders, (
        f"jobs that pull in a vector backend and would re-download the model on every "
        f"run: {offenders}.\n"
        "This is the #535 red build. Either give the job a save-capable cache on a "
        "model path PLUS the production-entry-point pre-fetch (see the `test` and "
        "`eval-vec` legs — they cache DIFFERENT backends and must not share a key), or "
        "do not pull in a vector backend at all, so the vector tests skip visibly "
        "under `-rs`."
    )


def test_every_default_suite_job_fetches_origin_main():
    """A JOB THAT RUNS THE DEFAULT SUITE MUST FETCH `origin/main` (techne#690).

    `doctor`'s `runtime:revision` runs `git rev-parse --short origin/main` and
    returns UNKNOWN when it fails, which breaks "All checks passed" and fails four
    `test_cli_doctor` tests. On a push the checked-out ref IS main; on a
    pull_request `actions/checkout` fetches only the merge ref at depth 1, so the
    ref has to be fetched explicitly.

    THE INVARIANT NOW LIVES IN THREE JOBS, WHICH THIS FILE ALREADY CALLS A HAZARD.
    Its own note at the fork guard says: "THE GUARD IS REPEATED THREE TIMES AND
    THAT IS A HAZARD... a new job inherits the runner and not the guard... Do not
    delete it in favour of a comment; a comment is what let this regrow twice."
    A comment is exactly what held this together until now (Prudence, #779 Nit 2).

    Derived, not hardcoded: the job list comes from which jobs actually run an
    unmarked pytest, so a new default-suite job is caught rather than a new name
    being added here by hand.
    """
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]

    def runs_default_suite(job):
        for step in job.get("steps", []):
            # PER LINE, not per step: a `run:` block is often multi-line (a pip
            # install followed by the pytest call), and treating the block as one
            # string matched a `pytest` on one line against a `-m` on another --
            # which classified crash-recovery, a marker-only job, as running the
            # default suite.
            for line in step.get("run", "").splitlines():
                # Anchor on the INVOCATION form, not the bare word. Every pytest
                # call in this workflow is `python -m pytest`; matching "pytest"
                # alone also matched `pip install -e ".[codex]" pytest`, which
                # installs the package and runs nothing.
                if "-m pytest" not in line:
                    continue
                args = line.split("-m pytest", 1)[1]
                # `-m <marker>` selects a subset; a bare pytest is the whole suite,
                # and only the whole suite reaches tests/test_cli_doctor.py.
                if " -m " not in args:
                    return True
        return False

    def fetches_main(job):
        return any(
            "refs/remotes/origin/main" in step.get("run", "")
            for step in job.get("steps", [])
        )

    needs = {name for name, job in jobs.items() if runs_default_suite(job)}
    assert needs == {"test", "eval-vec", "fork-pr"}, (
        f"the set of default-suite jobs changed: {sorted(needs)}. Each one runs "
        "tests/test_cli_doctor.py and so needs origin/main fetched."
    )

    missing = sorted(n for n in needs if not fetches_main(jobs[n]))
    assert not missing, (
        f"{missing} run(s) the default suite without fetching origin/main — "
        "doctor's runtime:revision will return UNKNOWN and fail four "
        "test_cli_doctor tests on every pull_request (techne#690)"
    )
