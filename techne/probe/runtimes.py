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
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
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


@dataclass
class NativeCanaryHandle:
    """Opaque handle returned by seed_native_canary, passed back for cleanup."""

    path: Path
    label: str
    created_file: bool  # True if we created MEMORY.md (delete it on cleanup)


class RuntimeAdapter(Protocol):
    """Spawn a fresh session on a runtime and return the agent's textual output.

    Differential mode additionally needs a contamination-proof way to seed a
    canary into the runtime's NATIVE memory (the negative control). Adapters that
    can't do that return supports_native_seed() == False and the probe runs the
    techne side without the control (flagged in the verdict).
    """

    name: str

    def fresh_session(
        self,
        prompt: str,
        *,
        disable_native: bool,
        allow_techne_tools: bool,
    ) -> str:
        ...

    def supports_native_seed(self) -> bool:
        ...

    def seed_native_canary(self, codeword: str, label: str) -> NativeCanaryHandle:
        ...

    def cleanup_native_canary(self, handle: NativeCanaryHandle) -> None:
        ...


class ClaudeCodeAdapter:
    """Drive a fresh headless Claude Code session via `claude -p`."""

    name = "claude-code"

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout: int = 180,
        claude_bin: str = "claude",
        session_cwd: Path | None = None,
        native_memory_path: Path | None = None,
    ):
        self.model = model
        self.timeout = timeout
        self.claude_bin = claude_bin
        # The fresh session runs here; CC's native memory is scoped to this dir's
        # project slug, so the native canary must be seeded into the SAME slug.
        self.session_cwd = Path(session_cwd) if session_cwd else Path.cwd()
        self.native_memory_path = native_memory_path or self._default_memory_path(self.session_cwd)

    @staticmethod
    def _default_memory_path(cwd: Path) -> Path:
        """CC stores per-project memory under ~/.claude/projects/<slug>/memory/.

        The slug is the absolute cwd with '/' and '.' replaced by '-'.
        """
        slug = re.sub(r"[/.]", "-", str(Path(cwd).resolve()))
        return Path.home() / ".claude" / "projects" / slug / "memory" / "MEMORY.md"

    # --- native-memory negative control (differential mode) ----------------

    def supports_native_seed(self) -> bool:
        return True

    def seed_native_canary(self, codeword: str, label: str) -> NativeCanaryHandle:
        """Append a sentinel-wrapped canary line to the project's MEMORY.md — the
        surface CC auto-injects at session start. Written from the script (never
        an agent turn) so it's provably native-only."""
        path = self.native_memory_path
        created = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        block = (
            f"\n<!-- techne#94 probe canary {label} START -->\n"
            f"- Native probe codeword ({label}): {codeword}\n"
            f"<!-- techne#94 probe canary {label} END -->\n"
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(block)
        logger.debug("ClaudeCodeAdapter: seeded native canary in %s (created=%s)", path, created)
        return NativeCanaryHandle(path=path, label=label, created_file=created)

    def cleanup_native_canary(self, handle: NativeCanaryHandle) -> None:
        """Remove the sentinel block (idempotent); delete the file if we made it."""
        if not handle.path.exists():
            return
        if handle.created_file:
            handle.path.unlink()
            return
        text = handle.path.read_text(encoding="utf-8")
        pattern = re.compile(
            rf"\n?<!-- techne#94 probe canary {re.escape(handle.label)} START -->.*?"
            rf"<!-- techne#94 probe canary {re.escape(handle.label)} END -->\n?",
            re.DOTALL,
        )
        handle.path.write_text(pattern.sub("", text), encoding="utf-8")

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
                cwd=self.session_cwd,
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
