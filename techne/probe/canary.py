"""Differential-canary memory-attribution probe — core logic (techne#94).

Mechanism (proof by construction):
  1. Seed a unique codeword ONLY in techne, from this script — never via an agent
     turn that might also hand-write it to native memory (the exact leak that
     built the shadow, techne#92). The seed is fresh + high-confidence so the
     Tier-0 recency reservation (techne#93) lands it in the injected slice.
  2. Spawn a genuinely FRESH session on the target runtime with native memory
     disabled. In the default `off-test` mode techne's *pull* tools are disabled
     too, so the SessionStart egress injection is the ONLY channel that can carry
     the codeword into context.
  3. Ask for the codeword (by its probe label — never the value, so the prompt
     can't leak it).
  4. Codeword in the output ⟹ techne reached the agent. With native + pull both
     off, that can only be the egress push path — a clean #93 acceptance.

Modes:
  - off-test: native OFF, techne pull tools OFF → tests the egress PUSH path
    specifically. The strongest #93 acceptance. (run_probe)
  - pull: native OFF, techne pull tools ON → tests that techne is reachable at
    all (recall), not specifically the push path. (run_probe)
  - differential: native ON + a native-only negative control → proves techne is
    the LEAD (reaches the agent without disabling native), the routine
    runs-anywhere gate. (run_differential)

Deferred (MVP-cut, documented for the next pass):
  - Adapters for other AI client runtimes techne runs under (Aider, openwebui,
    MCP-only — the #57 portability set) — see techne/probe/runtimes.py.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from techne.config import Config
from techne.core.egress import select_tier0_injection
from techne.core.knowledge import delete, store

logger = logging.getLogger(__name__)

# Marker that egress prefixes every injected line with (techne#93). Its presence
# in the agent's answer is direct provenance that the value came via techne.
TECHNE_PROVENANCE_MARKER = "⟦techne"


@dataclass
class CanarySeed:
    """A canary seeded into techne for one probe run."""

    knowledge_id: int
    codeword: str
    label: str  # human-readable probe label, e.g. "techne-probe-2026-06-20-ab12cd"


@dataclass
class ProbeVerdict:
    """Outcome of a single probe run."""

    passed: bool
    surfaced: bool  # codeword appeared in the agent's output
    provenance: str  # "techne" | "techne?" | "absent" | "setup-failed"
    codeword: str
    label: str
    mode: str
    output: str
    detail: str

    def render(self) -> str:
        status = "PASS ✅" if self.passed else "FAIL ❌"
        return (
            f"probe[{self.mode}] {status}\n"
            f"  codeword : {self.codeword}\n"
            f"  surfaced : {self.surfaced}\n"
            f"  provenance: {self.provenance}\n"
            f"  detail   : {self.detail}"
        )


@dataclass
class DifferentialVerdict:
    """Outcome of a differential probe — native ON, both canaries seeded.

    passed: techne carried its codeword into context (techne is in the loop, not
        masked by native memory). The native codeword is the negative control.
    """

    passed: bool
    techne_surfaced: bool
    native_surfaced: bool  # negative control — proves the native push path works
    techne_provenance: str  # "techne" | "techne?" | "absent"
    control_ok: bool  # native control surfaced (so a techne miss would be meaningful)
    techne_codeword: str
    native_codeword: str
    output: str
    detail: str

    def render(self) -> str:
        status = "PASS ✅" if self.passed else "FAIL ❌"
        ctrl = "ok" if self.control_ok else "UNAVAILABLE — verdict is advisory"
        return (
            f"probe[differential] {status}  (native memory ON)\n"
            f"  techne canary : surfaced={self.techne_surfaced} "
            f"provenance={self.techne_provenance}\n"
            f"  native control: surfaced={self.native_surfaced} ({ctrl})\n"
            f"  detail        : {self.detail}"
        )


def new_codeword() -> tuple[str, str]:
    """Return (codeword, label) — both unique per call.

    The codeword is what the agent must surface; the label is how we *ask* for it
    without ever putting the codeword in the prompt.
    """
    token = uuid.uuid4().hex[:12].upper()
    codeword = f"TECHNE-PROBE-{token}"
    label = f"techne-probe-{datetime.now():%Y-%m-%d}-{token[:6].lower()}"
    return codeword, label


def seed_canary(config: Config, codeword: str, label: str) -> CanarySeed:
    """Seed the codeword into techne ONLY, from this script.

    High-confidence + freshly written so the Tier-0 recency reservation surfaces
    it in the injected slice. Tagged ephemeral so it is unmistakably a probe row.
    """
    content = (
        f"TECHNE PROBE CANARY ({label}): the techne probe codeword is {codeword}. "
        "Seeded ONLY in techne for the memory-attribution off-test (techne#94); "
        "never written to native memory. Safe to delete after the probe run."
    )
    kid = store(
        config.db_path,
        content,
        type="evaluation",
        tags=f"probe,canary,ephemeral,techne-only,name:{label},techne#94",
        confidence="high",
        source="probe",
    )
    logger.debug("probe: seeded canary knowledge#%d (%s)", kid, label)
    return CanarySeed(knowledge_id=kid, codeword=codeword, label=label)


def canary_in_injection(config: Config, seed: CanarySeed) -> bool:
    """Precondition for off-test mode: is the canary actually in the Tier-0 slice?

    If it isn't, the push path can't deliver it and a FAIL would be a setup
    artifact, not a real egress failure — so we check and report distinctly.
    """
    rows = select_tier0_injection(
        config.db_path,
        char_budget=config.injection.char_budget,
        recent_reserve_frac=config.injection.recent_reserve_frac,
    )
    return any(r["id"] == seed.knowledge_id for r in rows)


def _adapter_injected_context(adapter, config) -> str | None:
    """Best-effort fetch of what the adapter's runtime injects at session start.

    Returns None when the adapter doesn't implement injected_context (e.g. test
    fakes) so the caller skips the precondition rather than failing spuriously.
    """
    fn = getattr(adapter, "injected_context", None)
    if fn is None:
        return None
    return fn(config)


def evaluate_output(output: str, codeword: str) -> tuple[bool, bool]:
    """Return (surfaced, provenance_tag_present).

    surfaced: the exact codeword appears in the output.
    provenance_tag_present: the ⟦techne…⟧ attribution marker appears, corroborating
        that the value arrived via techne's injection rather than some other path.
    """
    surfaced = codeword in output
    tag_present = TECHNE_PROVENANCE_MARKER in output
    return surfaced, tag_present


def run_probe(config, adapter, *, mode: str = "off-test", cleanup: bool = True) -> ProbeVerdict:
    """Run one differential-canary probe and return a verdict.

    mode:
        "off-test" — native OFF, techne pull tools OFF (tests egress push).
        "pull"     — native OFF, techne pull tools ON (tests reachability).
    cleanup: delete the canary row afterwards (default True) so it doesn't linger
        in the live knowledge table / future injections.
    """
    if mode not in ("off-test", "pull"):
        raise ValueError(f"unknown probe mode: {mode!r}")

    codeword, label = new_codeword()
    seed = seed_canary(config, codeword, label)
    try:
        # Push mode can only work if the canary is actually in what this runtime
        # injects at session start. Each adapter knows its own push surface; if it
        # can't tell us (None), we skip the precondition rather than guess.
        if mode == "off-test":
            injected = _adapter_injected_context(adapter, config)
            if injected is not None and codeword not in injected:
                return ProbeVerdict(
                    passed=False,
                    surfaced=False,
                    provenance="setup-failed",
                    codeword=codeword,
                    label=label,
                    mode=mode,
                    output="",
                    detail=(
                        "canary not in the runtime's injected context — for CC raise "
                        "injection.char_budget/recent_reserve_frac; push can't be tested otherwise"
                    ),
                )

        prompt = (
            f"What is the techne probe codeword seeded under the label '{label}'? "
            "Reply with the codeword only; if you don't have it, say MISSING."
        )
        allow_pull = mode == "pull"
        logger.debug("probe: spawning fresh session (mode=%s, allow_pull=%s)", mode, allow_pull)
        output = adapter.fresh_session(
            prompt,
            disable_native=True,
            allow_techne_tools=allow_pull,
        )

        surfaced, tag_present = evaluate_output(output, codeword)
        # In off-test mode the channel is techne by construction (native + pull off).
        if surfaced:
            provenance = "techne" if (mode == "off-test" or tag_present) else "techne?"
        else:
            provenance = "absent"

        if surfaced and mode == "off-test":
            detail = "codeword surfaced with native + pull disabled — egress push proven"
        elif surfaced:
            detail = "codeword surfaced via techne reachability (pull allowed)"
        else:
            detail = "codeword did not surface — techne did not reach the agent"

        return ProbeVerdict(
            passed=surfaced,
            surfaced=surfaced,
            provenance=provenance,
            codeword=codeword,
            label=label,
            mode=mode,
            output=output,
            detail=detail,
        )
    finally:
        if cleanup:
            deleted = delete(config.db_path, seed.knowledge_id)
            logger.debug(
                "probe: cleanup canary knowledge#%d deleted=%s", seed.knowledge_id, deleted
            )


def run_differential(config, adapter, *, cleanup: bool = True) -> DifferentialVerdict:
    """Non-destructive differential probe: native memory stays ON.

    Seeds a techne-only canary and (if the adapter supports it) a native-only
    canary, then spawns a fresh session with native ON and techne pull OFF so the
    two PUSH paths compete in one context. Because each codeword lives in exactly
    one store, presence attributes it; the ⟦techne⟧ marker corroborates.

    Passes when techne carries its codeword — proving techne reaches the agent
    without disabling native memory (the routine "is techne the lead, not the
    shadow?" gate). The native canary is the negative control: if it doesn't
    surface, the run can't distinguish a real techne miss from a broken session,
    so the verdict is marked advisory.
    """
    techne_cw, techne_label = new_codeword()
    native_cw, native_label = new_codeword()
    seed = seed_canary(config, techne_cw, techne_label)

    # native_handle is pre-bound so the finally can reference it even if the native
    # seed below raises. The native seed is INSIDE the try (techne#170): if it raised
    # outside it, the finally never ran and the techne canary already seeded above
    # leaked into production knowledge.
    native_handle = None
    try:
        has_control = adapter.supports_native_seed()
        if has_control:
            native_handle = adapter.seed_native_canary(native_cw, native_label)
        prompt = (
            "Two probe codewords were seeded before this session. "
            f"What is the techne probe codeword (label '{techne_label}') and the native "
            f"probe codeword (label '{native_label}')? Reply on two lines exactly as "
            "`techne=<codeword>` and `native=<codeword>`; write MISSING for any you don't have."
        )
        logger.debug("probe: differential session (control=%s)", has_control)
        output = adapter.fresh_session(prompt, disable_native=False, allow_techne_tools=False)

        techne_surfaced, tag_present = evaluate_output(output, techne_cw)
        native_surfaced = native_cw in output
        control_ok = native_surfaced if has_control else False

        if techne_surfaced:
            techne_provenance = "techne" if tag_present else "techne?"
        else:
            techne_provenance = "absent"

        if not techne_surfaced and native_surfaced:
            detail = "techne shadowed — native carried its codeword, techne did not"
        elif techne_surfaced and not has_control:
            detail = "techne carried; adapter has no native control, so masking is untested"
        elif techne_surfaced and not control_ok:
            detail = "techne carried, but native control didn't surface — verdict advisory"
        elif techne_surfaced and control_ok:
            detail = "techne carried alongside live native memory — techne is in the loop"
        else:
            detail = "neither codeword surfaced — both push paths failed or session broke"

        return DifferentialVerdict(
            passed=techne_surfaced,
            techne_surfaced=techne_surfaced,
            native_surfaced=native_surfaced,
            techne_provenance=techne_provenance,
            control_ok=control_ok,
            techne_codeword=techne_cw,
            native_codeword=native_cw,
            output=output,
            detail=detail,
        )
    finally:
        if cleanup:
            delete(config.db_path, seed.knowledge_id)
            if native_handle is not None:
                adapter.cleanup_native_canary(native_handle)
