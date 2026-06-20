"""Runtime adapters for the memory-attribution probe (techne#94).

The probe core (techne/probe/canary.py) is runtime-neutral. Each runtime only
needs to answer one question: "spawn a genuinely fresh session, with native
memory optionally disabled and techne's pull tools optionally blocked, ask this
prompt, and give me back what the agent said."

Implemented: ClaudeCodeAdapter (CC headless `claude -p`).
Deferred (same Protocol): OpencodeAdapter, CodexAdapter — each wraps that
runtime's headless/one-shot invocation + its native-memory + tool-gating knobs.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Protocol

logger = logging.getLogger(__name__)

# techne's MCP pull tools — disabled in off-test mode so the SessionStart egress
# injection is the only channel that can carry the canary into context.
TECHNE_MCP_TOOLS = [
    "mcp__techne__techne_observe",
    "mcp__techne__techne_recall",
    "mcp__techne__techne_resume",
    "mcp__techne__techne_search",
    "mcp__techne__techne_status",
    "mcp__techne__techne_store",
    "mcp__techne__techne_threads",
]

# Local tools that could be used to pull from techne out-of-band (shell `techne
# search`, reading the db / memory files). Blocked in off-test mode too.
LOCAL_PULL_TOOLS = ["Bash", "Read", "Grep", "Glob"]


class RuntimeAdapter(Protocol):
    """Spawn a fresh session on a runtime and return the agent's textual output."""

    name: str

    def fresh_session(
        self,
        prompt: str,
        *,
        disable_native: bool,
        allow_techne_tools: bool,
    ) -> str:
        ...


class ClaudeCodeAdapter:
    """Drive a fresh headless Claude Code session via `claude -p`."""

    name = "claude-code"

    def __init__(self, *, model: str | None = None, timeout: int = 180, claude_bin: str = "claude"):
        self.model = model
        self.timeout = timeout
        self.claude_bin = claude_bin

    def fresh_session(
        self,
        prompt: str,
        *,
        disable_native: bool,
        allow_techne_tools: bool,
    ) -> str:
        argv = [self.claude_bin, "-p", prompt]
        if self.model:
            argv += ["--model", self.model]

        # In off-test mode, block every path that could PULL the canary so the
        # only remaining channel is the SessionStart egress injection (push).
        if not allow_techne_tools:
            disallowed = TECHNE_MCP_TOOLS + LOCAL_PULL_TOOLS
            argv += ["--disallowed-tools", *disallowed]

        env = dict(os.environ)
        if disable_native:
            # CC's native auto-memory off — the destructive off-test condition.
            env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"

        logger.debug(
            "ClaudeCodeAdapter: %s (disable_native=%s, allow_techne_tools=%s)",
            " ".join(argv[:3]),
            disable_native,
            allow_techne_tools,
        )
        try:
            proc = subprocess.run(
                argv,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"`{self.claude_bin}` not found — the probe needs the Claude Code CLI on PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"probe session timed out after {self.timeout}s") from exc

        if proc.returncode != 0:
            logger.warning("ClaudeCodeAdapter: non-zero exit %d: %s", proc.returncode, proc.stderr)
        return proc.stdout or ""
