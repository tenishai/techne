"""The local-only policy in ARCHITECTURE.md, made falsifiable (#453, #537).

ARCHITECTURE.md states that four things are deliberately not covered by CI.
That is a claim about the repo, and a claim nobody can fail is documentation
that drifts. These assertions fail the moment the repo and the policy disagree
in EITHER direction: if someone wires one of these into CI without updating the
card, or removes an exclusion so a suite starts running in the default job.

WHAT THIS DOES NOT PIN, stated so the coverage is not overread: the per-suite
test COUNTS quoted in the card (3 probe, 1 eval_llm, 1 eval_scale, 2 LSP-gated) are
descriptive, not asserted — pinning them would fail on every added test and
teach people to edit the number rather than think. What is asserted is the
load-bearing half: these do not run in CI.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
#: BOTH suffixes — GitHub Actions honours `.yaml` as well as `.yml`, and a
#: single `probe.yaml` defeated three of these assertions at once.
WORKFLOWS = sorted(
    p for ext in ("*.yml", "*.yaml") for p in (REPO / ".github" / "workflows").glob(ext)
)

#: Markers the card documents as deliberately uncovered. Kept here rather than
#: parsed out of the prose: the card is written for humans, and a regex over
#: English is a worse contract than a list the test owns.
LOCAL_ONLY_MARKERS = ("probe", "eval_llm", "eval_scale")


def test_workflows_exist_at_all():
    """Guards the guard: an empty glob would make every assertion below vacuous."""
    assert WORKFLOWS, "no workflow files found — the scans below would prove nothing"


def _effective_markexpr(pytestconfig) -> str:
    """The marker expression pytest will ACTUALLY SELECT WITH.

    Four earlier sources were wrong, each reading better than the last:
      * grepping pyproject.toml's raw text — a COMMENT stood in for the setting;
      * parsing pyproject.toml — a `pytest.ini` overrides it outright, and the
        "ignoring pytest config in pyproject.toml" warning is suppressed under
        `-q`, which is how CI runs, so the failure was SILENT;
      * `getini("addopts")` — does not see `PYTEST_ADDOPTS` or a command-line
        `-m`, both of which win. (Observed, not explained: an earlier version of
        this docstring claimed pytest prepends the env var's args ahead of the
        ini. That could not be reproduced — repeated `-m` is last-wins, so args
        landing ahead would LOSE, yet PYTEST_ADDOPTS demonstrably wins. The
        observable fact stands; the mechanism was a guess and is gone.)
      * and none of them see IN-PROCESS reconfiguration: a `pytest_configure`
        hook in a conftest, or a plugin loaded via `-p` / `PYTEST_PLUGINS`, can
        set `config.option.markexpr = ""` and drop every exclusion while the ini
        stays pristine. No workflow edit is required, so no static scan can
        catch it.

    `option.markexpr` is the effective expression and was truthful on every one
    of those routes when measured. It is public API — deliberately not
    `_pytest.mark.expression`, which would answer selection exactly at the cost
    of a private-API dependency in a guard meant to still work in a year.

    STILL RUN-DEPENDENT, in the safe direction: this reads the run, so
    `pytest -m probe` (which the card TELLS you to run locally) fails these two
    assertions. Loud and self-explanatory, and far cheaper than the false green
    it replaced. The workflow-side assertions below are NOT redundant with this
    one — a run-side check only sees the run it is in, so a widening confined to
    a CI step that never collects this file is a static-only catch.
    """
    return pytestconfig.option.markexpr or ""


@pytest.mark.parametrize("marker", LOCAL_ONLY_MARKERS)
def test_marker_is_excluded_from_the_default_run(marker, pytestconfig):
    assert f"not {marker}" in _effective_markexpr(pytestconfig), (
        f"`{marker}` is no longer excluded from the default pytest run, so it now executes "
        f"in the standard job (markexpr in force: {_effective_markexpr(pytestconfig)!r}, "
        f"config: {pytestconfig.inipath}) — "
        f"ARCHITECTURE.md still calls it local-only"
    )


def _executable_text(workflow: Path) -> str:
    """Everything a workflow actually RUNS — `run:` bodies and `env:` values.

    DELIBERATELY NOT CAUGHT, because an honest boundary beats a fragile
    parser: `sh -c "pytest -m ..."` (nested quoting) and `-m"not slow"` (value
    attached to the flag; pytest does honour it). Also, an env-var-interpolated
    interpreter such as `$PY -m pytest -m "not slow"` DOES trip the blessing
    assertion — safe direction — but reports `pytest` as an unblessed
    expression too, because the `python` check cannot see through `$PY`.

    NOT the raw file. Grepping the text matched `# CLI -m overrides the addopts
    exclusion. eval_llm stays out (needs a key)` — a COMMENT explaining why the
    marker is absent — and reported it as an invocation. Same defect as
    substring-matching a systemd unit: comments are inert to the thing that
    acts, so a check that cannot tell code from prose about code is not
    checking the contract.
    """
    doc = yaml.safe_load(workflow.read_text()) or {}
    parts: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "run" and isinstance(value, str):
                    # Join `\`-continuations FIRST. shlex raises on a trailing
                    # backslash, and the old `except ValueError: continue` then
                    # discarded the whole line — so a long command split across
                    # lines, which is simply how CI commands are written, hid its
                    # `-m` from every assertion here including the pre-existing
                    # marker one. Not an evasion; the normal case.
                    parts.append(value.replace("\\\n", " "))
                elif key == "env" and isinstance(value, dict):
                    parts.extend(f"{k}={v}" for k, v in value.items())
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return "\n".join(parts)


@pytest.mark.parametrize("marker", LOCAL_ONLY_MARKERS)
def test_no_workflow_invokes_the_marker(marker):
    """Exclusion from the default run is NOT the same as uncovered.

    `eval`, `eval_gate` and `eval_vec` are excluded too, and each has a
    dedicated CI job. The distinction the card draws is exactly this one, so it
    is the thing worth asserting.
    """
    invokers = [
        w.name
        for w in WORKFLOWS
        if re.search(rf"-m\s+['\"]?[^'\"\n]*\b{re.escape(marker)}\b", _executable_text(w))
    ]
    assert invokers == [], (
        f"`{marker}` is now invoked by {invokers} — it is covered by CI, and "
        f"ARCHITECTURE.md's local-only policy is out of date"
    )


def test_lsp_integration_is_not_enabled_in_ci():
    enablers = [w.name for w in WORKFLOWS if "RUN_LSP_INTEGRATION" in _executable_text(w)]
    assert enablers == [], (
        f"RUN_LSP_INTEGRATION is now set in {enablers} — the LSP integration tests run in CI, "
        f"and ARCHITECTURE.md still calls them local-only"
    )


def test_the_card_still_carries_the_policy():
    """The other direction: code stays uncovered, prose quietly disappears."""
    card = (REPO / "ARCHITECTURE.md").read_text()
    assert "DELIBERATELY LOCAL-ONLY" in card
    for token in (*LOCAL_ONLY_MARKERS, "RUN_LSP_INTEGRATION"):
        assert token in card, f"ARCHITECTURE.md no longer names `{token}` as local-only"


#: Marker expressions the workflows are deliberately allowed to pass to pytest.
#: A FOURTH expression is not necessarily wrong — it is a deliberate edit that
#: should make someone check whether it widens selection, which is the whole
#: point of tripping here rather than silently allowing it.
#: `crash` blessed 2026-08-17 (techne#388): the fault-injection gate is excluded
#: from the default run because it spawns subprocesses, and the `crash-recovery`
#: job's whole purpose is to run it — which is the case this allowlist exists to
#: distinguish from a suite nothing executes.
BLESSED_MARKER_EXPRESSIONS = {"eval or eval_gate", "eval_vec", "crash"}


def _pytest_marker_expressions(workflow: Path) -> list[str]:
    """`-m` values passed to PYTEST, not `python -m <module>` invocations.

    `python -m pytest` and `python -m ruff` both contain `-m`; neither is a
    marker expression. Distinguished by the preceding token rather than by
    pattern-matching the value, because the value is arbitrary text.
    """
    found: list[str] = []
    for line in _executable_text(workflow).splitlines():
        try:
            tokens = shlex.split(line)
        except ValueError:  # unbalanced quotes in a shell line we do not own
            continue
        for i, tok in enumerate(tokens):
            if tok != "-m" or i + 1 >= len(tokens):
                continue
            prev = tokens[i - 1] if i else ""
            if prev.endswith(("python", "python3")) or "python" in Path(prev).name:
                continue  # `python -m <module>`
            found.append(tokens[i + 1])
    return found


def test_no_workflow_sets_pytest_addopts():
    """`PYTEST_ADDOPTS` routes around the ini entirely.

    Setting it to `-m "not eval"` drops every other exclusion while
    `getini("addopts")` still reports the pristine value. Measured: 7 tests the
    policy says never run entered the run with every assertion green. The
    ordering that produces this is NOT stated here — see `_effective_markexpr`,
    where the same guess was retracted; only the observed behaviour is claimed.

    THIS GUARD CANNOT FIRE IN ITS OWN WORST CASE, and the reason is circular: a
    `PYTEST_ADDOPTS` whose marker expression SELECTS the local-only suites
    necessarily DESELECTS this file, because these tests carry no markers.
    Measured with the value actually IN FORCE, not merely present in a workflow:

        PYTEST_ADDOPTS="-m 'not eval'"  -> guard RUNS and FAILS (caught)
        PYTEST_ADDOPTS="-m probe"       -> 3 probe tests collected (policy
                                           violated) AND guard 9 deselected
        PYTEST_ADDOPTS="-k <no match>"  -> guard 9 deselected

    So the WIDENING case that merely drops other exclusions is caught; the blind
    spot is a value selecting `probe`/`eval_llm` directly — exactly the state
    this assertion exists to detect. Marking these tests cannot close it:
    matching `-m probe` would require carrying the `probe` marker, which would
    make them local-only too. Closing it needs a check running OUTSIDE pytest's
    selection (a CI step, or the arch-cards gate); out of scope here and stated
    rather than left to be discovered. (Blind spot found by poe_1; the
    measurements above are mine.)
    """
    setters = [w.name for w in WORKFLOWS if "PYTEST_ADDOPTS" in _executable_text(w)]
    assert setters == [], (
        f"{setters} set PYTEST_ADDOPTS, which wins over the ini's addopts — "
        f"the local-only exclusions can be dropped there with every assertion here green"
    )


def test_workflow_marker_expressions_are_blessed():
    """A command-line `-m` beats `addopts` outright.

    `test.yml` says so itself four lines above the job that relies on it. So a
    workflow running `pytest -m "not slow"` names no local-only marker, trips no
    other assertion here, and (measured) collects every deselected test.
    """
    seen = {expr for w in WORKFLOWS for expr in _pytest_marker_expressions(w)}
    unexpected = sorted(seen - BLESSED_MARKER_EXPRESSIONS)
    assert unexpected == [], (
        f"unblessed pytest marker expression(s) in a workflow: {unexpected}. A CLI `-m` "
        f"overrides addopts, so this may widen selection past the local-only policy. "
        f"If it is correct, add it to BLESSED_MARKER_EXPRESSIONS deliberately."
    )
