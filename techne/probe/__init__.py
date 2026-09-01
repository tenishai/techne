"""Memory-attribution probe (techne#94).

A differential-canary harness that proves *which* memory system feeds the agent
(techne vs the runtime's native memory) on a fresh session — so "techne is the
lead" is machine-checkable rather than a vibe. It is the per-runtime acceptance
test for the egress adapter (techne#93).

Public surface:
    run_probe(config, adapter, *, mode, cleanup) -> ProbeVerdict
    ClaudeCodeAdapter — the Claude Code runtime adapter
    new_codeword() / ProbeVerdict / CanarySeed
"""

from techne.probe.canary import (
    CanarySeed,
    DifferentialVerdict,
    ProbeVerdict,
    new_codeword,
    run_differential,
    run_probe,
)
from techne.probe.runtimes import (
    ClaudeCodeAdapter,
    NativeCanaryHandle,
    OpencodeAdapter,
    PiAdapter,
    RuntimeAdapter,
)

__all__ = [
    "CanarySeed",
    "ProbeVerdict",
    "DifferentialVerdict",
    "RuntimeAdapter",
    "ClaudeCodeAdapter",
    "OpencodeAdapter",
    "PiAdapter",
    "NativeCanaryHandle",
    "new_codeword",
    "run_probe",
    "run_differential",
]
