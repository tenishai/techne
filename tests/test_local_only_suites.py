"""The local-only policy in ARCHITECTURE.md, made falsifiable (#453, #537).

ARCHITECTURE.md states that three things are deliberately not covered by CI.
That is a claim about the repo, and a claim nobody can fail is documentation
that drifts. These assertions fail the moment the repo and the policy disagree
in EITHER direction: if someone wires one of these into CI without updating the
card, or removes an exclusion so a suite starts running in the default job.

WHAT THIS DOES NOT PIN, stated so the coverage is not overread: the per-suite
test COUNTS quoted in the card (3 probe, 1 eval_llm, 2 LSP-gated) are
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
LOCAL_ONLY_MARKERS = ("probe", "eval_llm")


def test_workflows_exist_at_all():
    """Guards the guard: an empty glob would make every assertion below vacuous."""
    assert WORKFLOWS, "no workflow files found — the scans below would prove nothing"


def _addopts(pytestconfig) -> str:
    """The addopts pytest is ACTUALLY USING — asked, not parsed.

    Two simpler implementations were wrong before this one, and BOTH read
    better than it does — which is why the reasons live here:
      * grepping pyproject.toml's raw text let a COMMENT mentioning `not probe`
        stand in for the setting;
      * parsing pyproject.toml was still wrong, because a `pytest.ini` overrides
        it outright and the "ignoring pytest config in pyproject.toml" warning
        is suppressed under `-q`, which is how CI runs — so the failure was
        silent rather than merely wrong.

    WHERE THIS ANSWER STOPS, which matters more than the history: `getini` does
    NOT see `PYTEST_ADDOPTS` (pytest prepends the env var's args AHEAD of the
    ini value, so the ini is never reached) and does not see a command-line
    `-m`, which beats `addopts` outright. Both route around this function
    entirely, which is why the workflow-side assertions below exist — they are
    not belt-and-braces, they cover cases this cannot.

    ALSO RUN-DEPENDENT, in the safe direction: this reads the config of the RUN,
    so `pytest -o addopts=` or a stray `pytest.ini` at the root makes it fail on
    a perfectly correct repo. Loud and self-explanatory (the message prints
    `inipath`), and far cheaper than the false green it replaced.
    """
    return " ".join(pytestconfig.getini("addopts"))


@pytest.mark.parametrize("marker", LOCAL_ONLY_MARKERS)
def test_marker_is_excluded_from_the_default_run(marker, pytestconfig):
    assert f"not {marker}" in _addopts(pytestconfig), (
        f"`{marker}` is no longer excluded from the default pytest run, so it now executes "
        f"in the standard job (config in force: {pytestconfig.inipath}) — "
        f"ARCHITECTURE.md still calls it local-only"
    )


def _executable_text(workflow: Path) -> str:
    """Everything a workflow actually RUNS — `run:` bodies and `env:` values.

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
                    parts.append(value)
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
BLESSED_MARKER_EXPRESSIONS = {"eval or eval_gate", "eval_vec"}


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

    pytest prepends the env var's args AHEAD of `addopts`, so setting it to
    `-m "not eval"` drops every other exclusion while `getini` still reports the
    pristine value. Measured in review: 7 tests the policy says never run
    entered the run, and all assertions stayed green.
    """
    setters = [w.name for w in WORKFLOWS if "PYTEST_ADDOPTS" in _executable_text(w)]
    assert setters == [], (
        f"{setters} set PYTEST_ADDOPTS, which overrides addopts before pytest reads it — "
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
