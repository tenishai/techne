"""Tests for the memory-attribution probe (techne#94).

Unit tests use a fake adapter — no real `claude` session is spawned, the live db
is never touched (tmp_path fixtures only). The integration test that spawns a
real session is marked `probe` and excluded from default runs (it costs tokens).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from techne.config import Config
from techne.core.database import init_db
from techne.core.knowledge import store
from techne.probe.canary import (
    canary_in_injection,
    evaluate_output,
    new_codeword,
    run_probe,
    seed_canary,
)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    db = tmp_path / "probe_test.db"
    init_db(db).close()
    cfg = Config()
    cfg.db_path = db
    return cfg


class FakeAdapter:
    """Returns a scripted output and records how it was called."""

    name = "fake"

    def __init__(self, output: str):
        self.output = output
        self.calls: list[dict] = []

    def fresh_session(self, prompt, *, disable_native, allow_techne_tools):
        self.calls.append(
            {
                "prompt": prompt,
                "disable_native": disable_native,
                "allow_techne_tools": allow_techne_tools,
            }
        )
        return self.output.format(codeword=self._codeword_from_prompt())

    # The fake "knows" the codeword because the test wires it; real adapters never do.
    def _codeword_from_prompt(self):
        return getattr(self, "codeword", "")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_new_codeword_unique_and_prefixed():
    c1, l1 = new_codeword()
    c2, l2 = new_codeword()
    assert c1.startswith("TECHNE-PROBE-") and c2.startswith("TECHNE-PROBE-")
    assert c1 != c2 and l1 != l2


def test_evaluate_output_presence_and_provenance():
    assert evaluate_output("the codeword is ABC-123", "ABC-123") == (True, False)
    assert evaluate_output("- ⟦techne#5⟧ ABC-123", "ABC-123") == (True, True)
    assert evaluate_output("I don't have it, MISSING", "ABC-123") == (False, False)


def test_seed_lands_in_injection_slice(config):
    """A freshly-seeded high-confidence canary must reach the Tier-0 slice — the
    recency reservation (techne#93) is what makes the push test possible."""
    # Crowd the store with feedback so only the reservation can surface the canary.
    for n in range(15):
        store(
            config.db_path,
            f"feedback rule {n} " + "z" * 120,
            type="fact",
            tags="memory-type:feedback",
            confidence="high",
        )
    codeword, label = new_codeword()
    seed = seed_canary(config, codeword, label)
    assert canary_in_injection(config, seed)


# ---------------------------------------------------------------------------
# Orchestration (fake adapter)
# ---------------------------------------------------------------------------


def test_run_probe_pass_offtest(config):
    adapter = FakeAdapter("{codeword}")
    # Make the fake echo whatever codeword run_probe seeds: patch after seeding by
    # intercepting via the db — simplest is to have the adapter read the seeded row.
    verdict = _run_with_echo(config, adapter, mode="off-test")
    assert verdict.passed
    assert verdict.surfaced
    assert verdict.provenance == "techne"
    assert adapter.calls[0]["disable_native"] is True
    assert adapter.calls[0]["allow_techne_tools"] is False


def test_run_probe_fail_when_absent(config):
    adapter = FakeAdapter("MISSING")
    verdict = run_probe(config, adapter, mode="off-test", cleanup=True)
    assert not verdict.passed
    assert verdict.provenance == "absent"


def test_run_probe_cleans_up_canary(config):
    adapter = FakeAdapter("MISSING")
    verdict = run_probe(config, adapter, mode="off-test", cleanup=True)
    # The canary id is not exposed on the verdict; assert no probe rows linger.
    from techne.core.knowledge import list_entries

    rows = list_entries(config.db_path, limit=100)
    assert not any("PROBE CANARY" in r["content"] for r in rows)
    assert not verdict.passed  # sanity: this run failed, cleanup still ran


def test_run_probe_keep_leaves_canary(config):
    adapter = FakeAdapter("MISSING")
    run_probe(config, adapter, mode="off-test", cleanup=False)
    from techne.core.knowledge import list_entries

    rows = list_entries(config.db_path, limit=100)
    assert any("PROBE CANARY" in r["content"] for r in rows)


def test_run_probe_rejects_unknown_mode(config):
    with pytest.raises(ValueError):
        run_probe(config, FakeAdapter(""), mode="bogus")


def _run_with_echo(config, adapter, *, mode):
    """Helper: wrap run_probe so the fake adapter echoes the just-seeded codeword.

    run_probe generates the codeword internally, so we let the adapter discover it
    by reading the freshly seeded canary row from the db at call time — exactly the
    surface a real injected session would see.
    """
    orig = adapter.fresh_session

    def echo(prompt, **kw):
        adapter.calls.append({"prompt": prompt, **kw})
        from techne.core.knowledge import list_entries

        for r in list_entries(config.db_path, limit=20):
            if "PROBE CANARY" in r["content"]:
                # Mimic an injected line carrying the ⟦techne⟧ provenance marker.
                cw = r["content"].split("codeword is ", 1)[1].split(".", 1)[0].strip()
                return f"- ⟦techne#{r['id']}⟧ {cw}"
        return "MISSING"

    adapter.fresh_session = echo
    try:
        return run_probe(config, adapter, mode=mode, cleanup=True)
    finally:
        adapter.fresh_session = orig


# ---------------------------------------------------------------------------
# Integration — spawns a real session. Opt-in only.
# ---------------------------------------------------------------------------


@pytest.mark.probe
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")
def test_real_offtest_probe_against_live_techne():
    """End-to-end #93 acceptance: seed → fresh CC session (native+pull off) → the
    egress push must carry the codeword. Runs against the real db; cleans up."""
    from techne.probe import ClaudeCodeAdapter, run_probe

    cfg = Config.load()
    verdict = run_probe(cfg, ClaudeCodeAdapter(timeout=240), mode="off-test", cleanup=True)
    assert verdict.passed, verdict.render()
    assert verdict.provenance == "techne"
