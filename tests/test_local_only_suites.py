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

    Three sources have now been wrong here, each one closer than the last.
    Grepping pyproject.toml's raw text let a comment mentioning `not probe`
    stand in for the setting. Parsing pyproject.toml fixed that and was STILL
    wrong: pyproject is only one of five places pytest looks and is not the
    highest-precedence one — a `pytest.ini` overrides it outright, and the
    "ignoring pytest config in pyproject.toml" warning is suppressed under
    `-q`, which is how CI runs. Asking the live config ends the regress: it
    reports whatever file actually won, and returns a list, so the list-form
    `addopts = ["-m", "..."]` spelling stops being a false alarm.
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
