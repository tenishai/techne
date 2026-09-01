"""Runtime adapters for the memory-attribution probe (techne#94).

The probe core (techne/probe/canary.py) is runtime-neutral. Each runtime only
needs to answer one question: "spawn a genuinely fresh session, with native
memory optionally disabled and techne's pull tools optionally blocked, ask this
prompt, and give me back what the agent said."

Implemented: ClaudeCodeAdapter (CC headless `claude -p`), OpencodeAdapter
(`opencode run`), PiAdapter (`pi --print`). Deferred (same Protocol): adapters
for other AI *client* runtimes techne runs under — e.g. Aider, openwebui, or
any MCP-only client (the #57 portability set). Each wraps that runtime's
headless/one-shot invocation + its native-memory + tool-gating knobs. (Not the
platform's own "Codex" code-storage persona — that's a domain agent, not a
client to run techne under.)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
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
    ) -> str: ...

    def supports_native_seed(self) -> bool: ...

    def seed_native_canary(self, codeword: str, label: str) -> NativeCanaryHandle: ...

    def cleanup_native_canary(self, handle: NativeCanaryHandle) -> None: ...

    def injected_context(self, config) -> str | None:
        """Return the text this runtime injects at session start (for the
        off-test precondition: 'is the canary actually in the pushed surface?').
        Runtimes inject via different selectors, so each adapter answers for its
        own. Return None if the adapter can't compute it (precondition skipped).
        """
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
        """The project's CC auto-memory MEMORY.md, resolved by ``auto_memory_dir``: the
        default ``~/.claude/projects/<git-root-slug>/memory/`` or a user-set
        ``autoMemoryDirectory`` override (techne#175)."""
        # Resolve at call time (unlike the import-time snapshots in hooks.py /
        # transcript_import.py): the probe is a longer-lived differential-test
        # harness that may target different runtimes within one process, so it
        # re-reads CLAUDE_CONFIG_DIR per call. The deferred local import also
        # avoids a probe->platforms import cycle.
        from techne.platforms.claude_code.paths import auto_memory_dir

        return auto_memory_dir(cwd) / "MEMORY.md"

    def injected_context(self, config) -> str:
        """CC's push surface is the SessionStart hook's Tier-0 ## Key Knowledge
        block — exactly what select_tier0_injection renders."""
        from techne.core.egress import render_tier0_block, select_tier0_injection

        rows = select_tier0_injection(
            config.db_path,
            char_budget=config.injection.char_budget,
            recent_reserve_frac=config.injection.recent_reserve_frac,
        )
        return render_tier0_block(rows)

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


class PiAdapter:
    """Drive a fresh headless pi session via `pi --print` (techne#57/#94).

    pi has no native auto-memory — techne IS its memory — so:
      - disable_native is a no-op (nothing to turn off).
      - supports_native_seed() is False (no native store → no differential
        control; differential degrades to a push/reachability test).

    techne's push on pi is the capture extension's `before_agent_start` hook,
    which appends a techne-memory block built by `techne context`. To test that
    push in isolation (pull off) we run with `--no-tools` — the model needs
    zero tools to answer the probe prompt, and this closes the whole class of
    pull channels (built-ins, extension tools, the pi-mcp-adapter gateway) at
    once instead of enumerating a denylist against a moving target. Extensions
    still load, so the capture extension's injection still fires.

    Runs in a throwaway temp workspace so the probe never touches a real repo;
    techne's durable knowledge is not project-scoped, so the canary still
    injects. Uses whatever model pi is configured with (override with `model`).
    """

    name = "pi"

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout: int = 240,
        pi_bin: str = "pi",
    ):
        self.model = model
        self.timeout = timeout
        self.pi_bin = pi_bin

    # pi has no native auto-memory — the negative control isn't applicable.
    def supports_native_seed(self) -> bool:
        return False

    def seed_native_canary(self, codeword: str, label: str) -> NativeCanaryHandle:
        raise NotImplementedError("pi has no native auto-memory to seed")

    def cleanup_native_canary(self, handle: NativeCanaryHandle) -> None:  # pragma: no cover
        return None

    def injected_context(self, config) -> str:
        """pi's push surface = the extension's `techne context` block, computed
        directly. The extension runs it with --project-path <workspace>; the
        probe's workspace is a throwaway tempdir, so the threads slice is
        project-empty — but the durable-knowledge slice (where the canary
        lives) is global, which is what matters for the precondition."""
        from techne.core.state import format_resume_context, get_resume_context

        data = get_resume_context(config.db_path)
        return format_resume_context(data)

    def fresh_session(
        self,
        prompt: str,
        *,
        disable_native: bool,
        allow_techne_tools: bool,
    ) -> str:
        # disable_native is intentionally ignored: pi has no native memory.
        with tempfile.TemporaryDirectory(prefix="techne-probe-pi-") as workdir:
            cwd = Path(workdir)
            argv = [self.pi_bin, "--print", "--mode", "text"]
            if self.model:
                argv += ["--model", self.model]
            if not allow_techne_tools:
                # No tools at all — the model answers from context only, so the
                # extension's push injection is the sole channel (see class doc).
                argv += ["--no-tools"]
            argv.append(prompt)

            logger.debug(
                "PiAdapter: pi --print (allow_techne_tools=%s, model=%s)",
                allow_techne_tools,
                self.model or "default",
            )
            env = dict(os.environ)
            # The capture extension shells out to `techne` for the injection; make
            # it resolvable from this process regardless of the operator's PATH.
            # The techne entry point sits next to the venv python running the probe.
            techne_bin = Path(sys.executable).with_name("techne")
            if techne_bin.exists():
                env["TECHNE_BIN"] = str(techne_bin)
            try:
                proc = subprocess.run(
                    argv,
                    env=env,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"`{self.pi_bin}` not found — the probe needs the pi CLI on PATH"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"probe session timed out after {self.timeout}s") from exc

            if proc.returncode != 0:
                logger.warning(
                    "PiAdapter: non-zero exit %d: %s", proc.returncode, proc.stderr
                )
            return proc.stdout or ""


class OpencodeAdapter:
    """Drive a fresh headless opencode session via `opencode run` (techne#57/#94).

    opencode has no native auto-memory — techne IS its memory — so:
      - disable_native is a no-op (nothing to turn off).
      - supports_native_seed() is False (no native store → no differential control;
        differential degrades to a push/reachability test).

    techne's push on opencode is the capture plugin's
    `experimental.chat.system.transform` hook, which prepends a <techne-memory>
    block built by `techne context`. To test that push in isolation (pull off) we
    can't use `--pure` (it kills the plugin too); instead we run under a
    project-local agent that disables the techne MCP tools (`tools: techne_*:
    false`) — verified to remove them from the model while the plugin still fires.

    Runs in a throwaway temp workspace so the gating agent file never touches a
    real repo; techne knowledge isn't project-scoped, so the canary still injects.
    Defaults to the built-in keyless model `opencode/big-pickle`.
    """

    name = "opencode"
    READONLY_AGENT = "techne-readonly"

    def __init__(
        self,
        *,
        model: str | None = "opencode/big-pickle",
        timeout: int = 240,
        opencode_bin: str = "opencode",
    ):
        self.model = model
        self.timeout = timeout
        self.opencode_bin = opencode_bin

    # opencode has no native auto-memory — the negative control isn't applicable.
    def supports_native_seed(self) -> bool:
        return False

    def seed_native_canary(self, codeword: str, label: str) -> NativeCanaryHandle:
        raise NotImplementedError("opencode has no native auto-memory to seed")

    def cleanup_native_canary(self, handle: NativeCanaryHandle) -> None:  # pragma: no cover
        return None

    def injected_context(self, config) -> str:
        """opencode's push surface = the plugin's `techne context` block. Compute
        it directly (the plugin shells the same get_resume_context/format pair)."""
        from techne.core.state import format_resume_context, get_resume_context

        data = get_resume_context(config.db_path)
        return format_resume_context(data)

    def _write_readonly_agent(self, cwd: Path) -> None:
        agent_dir = cwd / ".opencode" / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / f"{self.READONLY_AGENT}.md").write_text(
            "---\n"
            "description: techne plugin injection active; techne MCP tools disabled (probe)\n"
            "mode: primary\n"
            "tools:\n"
            "  techne_*: false\n"
            "---\n"
            "Answer concisely with only what was asked.\n",
            encoding="utf-8",
        )

    def fresh_session(
        self,
        prompt: str,
        *,
        disable_native: bool,
        allow_techne_tools: bool,
    ) -> str:
        # disable_native is intentionally ignored: opencode has no native memory.
        with tempfile.TemporaryDirectory(prefix="techne-probe-oc-") as workdir:
            cwd = Path(workdir)
            argv = [self.opencode_bin, "run", "--dir", str(cwd)]
            if self.model:
                argv += ["-m", self.model]
            if not allow_techne_tools:
                self._write_readonly_agent(cwd)
                argv += ["--agent", self.READONLY_AGENT]
            argv.append(prompt)

            logger.debug(
                "OpencodeAdapter: opencode run (allow_techne_tools=%s, model=%s)",
                allow_techne_tools,
                self.model,
            )
            try:
                proc = subprocess.run(
                    argv,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"`{self.opencode_bin}` not found — the probe needs the opencode CLI on PATH"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"probe session timed out after {self.timeout}s") from exc

            if proc.returncode != 0:
                logger.warning(
                    "OpencodeAdapter: non-zero exit %d: %s", proc.returncode, proc.stderr
                )
            return proc.stdout or ""
