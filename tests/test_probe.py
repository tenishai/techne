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
    run_differential,
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


# ---------------------------------------------------------------------------
# Differential mode (fake adapter mimicking both push paths)
# ---------------------------------------------------------------------------


class DifferentialFake:
    """Mimics a runtime: techne canary arrives via injection (read from db),
    native canary via its own seeded value. Flags choose which paths 'fire'."""

    name = "diff-fake"

    def __init__(self, config, *, inject_techne, inject_native, supports_native=True, tag=True):
        self.config = config
        self.inject_techne = inject_techne
        self.inject_native = inject_native
        self._supports = supports_native
        self.tag = tag
        self.native_cw = None
        self.cleaned = False
        self.calls: list[dict] = []

    def supports_native_seed(self):
        return self._supports

    def seed_native_canary(self, codeword, label):
        from techne.probe.runtimes import NativeCanaryHandle

        self.native_cw = codeword
        return NativeCanaryHandle(path=Path("/tmp/none"), label=label, created_file=False)

    def cleanup_native_canary(self, handle):
        self.cleaned = True

    def fresh_session(self, prompt, *, disable_native, allow_techne_tools):
        self.calls.append(
            {"disable_native": disable_native, "allow_techne_tools": allow_techne_tools}
        )
        from techne.core.knowledge import list_entries

        lines = []
        if self.inject_techne:
            for r in list_entries(self.config.db_path, limit=20):
                if "PROBE CANARY" in r["content"]:
                    cw = r["content"].split("codeword is ", 1)[1].split(".", 1)[0].strip()
                    prefix = f"⟦techne#{r['id']}⟧ " if self.tag else ""
                    lines.append(f"techne={prefix}{cw}")
                    break
        else:
            lines.append("techne=MISSING")
        lines.append(f"native={self.native_cw}" if self.inject_native else "native=MISSING")
        return "\n".join(lines)


def test_differential_pass_both_paths_live(config):
    fake = DifferentialFake(config, inject_techne=True, inject_native=True)
    v = run_differential(config, fake)
    assert v.passed
    assert v.techne_surfaced and v.native_surfaced
    assert v.control_ok
    assert v.techne_provenance == "techne"
    assert fake.calls[0] == {"disable_native": False, "allow_techne_tools": False}
    assert fake.cleaned


def test_differential_shadowed_when_only_native(config):
    fake = DifferentialFake(config, inject_techne=False, inject_native=True)
    v = run_differential(config, fake)
    assert not v.passed
    assert v.native_surfaced and not v.techne_surfaced
    assert "shadowed" in v.detail


def test_differential_advisory_when_control_absent(config):
    fake = DifferentialFake(config, inject_techne=True, inject_native=False)
    v = run_differential(config, fake)
    assert v.passed  # techne carried
    assert not v.control_ok
    assert "advisory" in v.detail


def test_differential_provenance_unconfirmed_without_tag(config):
    fake = DifferentialFake(config, inject_techne=True, inject_native=True, tag=False)
    v = run_differential(config, fake)
    assert v.passed
    assert v.techne_provenance == "techne?"


def test_differential_without_native_support(config):
    fake = DifferentialFake(config, inject_techne=True, inject_native=False, supports_native=False)
    v = run_differential(config, fake)
    assert v.passed
    assert not v.control_ok
    assert "no native control" in v.detail
    assert not fake.cleaned  # nothing to clean


def test_differential_cleans_up_techne_canary(config):
    fake = DifferentialFake(config, inject_techne=True, inject_native=True)
    run_differential(config, fake, cleanup=True)
    from techne.core.knowledge import list_entries

    rows = list_entries(config.db_path, limit=100)
    assert not any("PROBE CANARY" in r["content"] for r in rows)


def test_differential_cleans_techne_canary_when_native_seed_raises(config):
    """techne#170: the techne canary is seeded BEFORE the native one. If seed_native_canary
    raises, the cleanup finally must still run — else the techne canary leaks into
    production knowledge. (The native seed was moved inside the try.)"""
    from techne.core.knowledge import list_entries

    class _RaisingFake(DifferentialFake):
        def seed_native_canary(self, codeword, label):
            raise RuntimeError("native seed boom")

    fake = _RaisingFake(config, inject_techne=True, inject_native=True)
    with pytest.raises(RuntimeError, match="native seed boom"):
        run_differential(config, fake)

    rows = list_entries(config.db_path, limit=100)
    assert not any("PROBE CANARY" in r["content"] for r in rows), (
        "techne canary leaked into knowledge after native seed failure"
    )


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter native-seed file mechanics (no session spawned)
# ---------------------------------------------------------------------------


def test_native_seed_appends_and_cleanup_removes(tmp_path):
    from techne.probe.runtimes import ClaudeCodeAdapter

    mem = tmp_path / "MEMORY.md"
    mem.write_text("# Memory Index\n\n- existing entry\n", encoding="utf-8")
    adapter = ClaudeCodeAdapter(native_memory_path=mem)
    handle = adapter.seed_native_canary("NATIVE-CW-1", "lbl-1")
    body = mem.read_text(encoding="utf-8")
    assert "NATIVE-CW-1" in body
    assert not handle.created_file
    adapter.cleanup_native_canary(handle)
    after = mem.read_text(encoding="utf-8")
    assert "NATIVE-CW-1" not in after
    assert "existing entry" in after  # original content preserved


def test_native_seed_deletes_file_it_created(tmp_path):
    from techne.probe.runtimes import ClaudeCodeAdapter

    mem = tmp_path / "sub" / "MEMORY.md"
    adapter = ClaudeCodeAdapter(native_memory_path=mem)
    handle = adapter.seed_native_canary("NATIVE-CW-2", "lbl-2")
    assert mem.exists() and handle.created_file
    adapter.cleanup_native_canary(handle)
    assert not mem.exists()


# ---------------------------------------------------------------------------
# OpencodeAdapter (no real opencode session spawned)
# ---------------------------------------------------------------------------


def test_opencode_no_native_seed():
    from techne.probe import OpencodeAdapter

    a = OpencodeAdapter()
    assert a.supports_native_seed() is False
    with pytest.raises(NotImplementedError):
        a.seed_native_canary("cw", "lbl")


def test_opencode_injected_context_includes_canary(config):
    from techne.probe import OpencodeAdapter

    codeword, label = new_codeword()
    seed_canary(config, codeword, label)
    block = OpencodeAdapter().injected_context(config)
    assert "<techne-memory>" in block
    assert codeword in block  # fresh high-confidence canary lands in `techne context`


def test_opencode_fresh_session_gates_tools(monkeypatch):
    from techne.probe import runtimes

    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "techne=ANSWER"
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        agent_file = Path(kw["cwd"]) / ".opencode" / "agent" / "techne-readonly.md"
        captured["agent_exists"] = agent_file.exists()
        captured["agent_body"] = agent_file.read_text() if agent_file.exists() else ""
        return FakeProc()

    monkeypatch.setattr(runtimes.subprocess, "run", fake_run)
    out = runtimes.OpencodeAdapter(model="opencode/big-pickle").fresh_session(
        "q", disable_native=True, allow_techne_tools=False
    )
    assert out == "techne=ANSWER"
    assert captured["argv"][:2] == ["opencode", "run"]
    assert "--agent" in captured["argv"] and "techne-readonly" in captured["argv"]
    assert "opencode/big-pickle" in captured["argv"]
    assert captured["agent_exists"]
    assert "techne_*: false" in captured["agent_body"]


def test_opencode_fresh_session_pull_allowed_no_gating(monkeypatch):
    from techne.probe import runtimes

    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(runtimes.subprocess, "run", fake_run)
    runtimes.OpencodeAdapter().fresh_session("q", disable_native=False, allow_techne_tools=True)
    assert "--agent" not in captured["argv"]  # no read-only agent when pull is allowed


# ---------------------------------------------------------------------------
# PiAdapter (no real pi session spawned)
# ---------------------------------------------------------------------------


def test_pi_no_native_seed():
    from techne.probe import PiAdapter

    a = PiAdapter()
    assert a.supports_native_seed() is False
    with pytest.raises(NotImplementedError):
        a.seed_native_canary("cw", "lbl")


def test_pi_injected_context_includes_canary(config):
    from techne.probe import PiAdapter

    codeword, label = new_codeword()
    seed_canary(config, codeword, label)
    block = PiAdapter().injected_context(config)
    assert "<techne-memory>" in block
    assert codeword in block  # fresh high-confidence canary lands in `techne context`


def test_pi_fresh_session_gates_tools(monkeypatch):
    from techne.probe import runtimes

    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "techne=ANSWER"
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(runtimes.subprocess, "run", fake_run)
    out = runtimes.PiAdapter().fresh_session("q", disable_native=True, allow_techne_tools=False)
    assert out == "techne=ANSWER"
    assert captured["argv"][0] == "pi"
    assert "--print" in captured["argv"]
    # off-test gates ALL tools (not an enumerated denylist) so the extension's
    # push injection is the only channel that can carry the canary.
    assert "--no-tools" in captured["argv"]


def test_pi_fresh_session_pull_allowed_no_gating(monkeypatch):
    from techne.probe import runtimes

    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(runtimes.subprocess, "run", fake_run)
    runtimes.PiAdapter(model="provider/model").fresh_session(
        "q", disable_native=False, allow_techne_tools=True
    )
    assert "--no-tools" not in captured["argv"]  # no gating when pull is allowed
    assert "--model" in captured["argv"] and "provider/model" in captured["argv"]


@pytest.mark.probe
@pytest.mark.skipif(shutil.which("pi") is None, reason="pi CLI not on PATH")
def test_real_offtest_probe_pi():
    """End-to-end on pi: fresh `pi --print` with techne MCP tools gated off → the
    extension's push injection must carry the codeword. Runs against the real db."""
    from techne.probe import PiAdapter, run_probe

    cfg = Config.load()
    verdict = run_probe(cfg, PiAdapter(timeout=300), mode="off-test", cleanup=True)
    assert verdict.passed, verdict.render()
    assert verdict.provenance == "techne"


class _NoInjectFake:
    name = "no-inject"

    def fresh_session(self, prompt, *, disable_native, allow_techne_tools):
        return "UNREACHABLE"

    def injected_context(self, config):
        return ""  # nothing injected → off-test precondition must fail


def test_offtest_setup_failed_when_canary_not_injected(config):
    v = run_probe(config, _NoInjectFake(), mode="off-test")
    assert not v.passed
    assert v.provenance == "setup-failed"
    assert "UNREACHABLE" not in v.output  # never spawned the session


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


@pytest.mark.probe
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")
def test_real_differential_probe_against_live_techne():
    """End-to-end: native ON + native-only control. techne must carry its codeword
    (not be masked), and the control must fire so the verdict is trustworthy."""
    from techne.probe import ClaudeCodeAdapter, run_differential

    cfg = Config.load()
    verdict = run_differential(cfg, ClaudeCodeAdapter(timeout=240), cleanup=True)
    assert verdict.passed, verdict.render()
    assert verdict.control_ok, verdict.render()


@pytest.mark.probe
@pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode CLI not on PATH")
def test_real_offtest_probe_opencode():
    """End-to-end on opencode: fresh `opencode run` with techne MCP tools gated off
    → the plugin's push injection must carry the codeword. Runs against the real db."""
    from techne.probe import OpencodeAdapter, run_probe

    cfg = Config.load()
    verdict = run_probe(cfg, OpencodeAdapter(timeout=300), mode="off-test", cleanup=True)
    assert verdict.passed, verdict.render()
    assert verdict.provenance == "techne"
