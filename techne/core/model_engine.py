"""How techne's model calls go out (techne#1189).

Every model call techne makes itself (LLM thread detection and summaries, session-end
reflection, the headless custodian rails) goes through
`thread_orchestrator._make_generate_fn`, and that function asks `config.model_engine`
how to send it:

* ``litellm`` (the default): LiteLLM, exactly as before this module existed.
* ``claude-code``: the user's own ``claude`` CLI, run as ``claude -p`` on the Claude
  Code login. Settings files are ignored (``--restricted``, below), so an
  ``apiKeyHelper`` or a settings ``env`` key does not apply. techne never stores or logs
  a credential.

ALWAYS THE LOGIN, NEVER AN ENVIRONMENT KEY (techne#1194). Claude Code's authentication
precedence (code.claude.com/docs/en/iam) puts ``ANTHROPIC_AUTH_TOKEN`` and then
``ANTHROPIC_API_KEY`` above the subscription login, and "in non-interactive mode
(``-p``), the key is always used when present". PR #1190 let the child inherit them, so
an exported key silently billed that key instead of the plan. The operator decided the
engine is for the login (2026-10-06), so the child's environment is a copy of techne's
WITHOUT those two variables (`child_env`). That reads no credential; it only declines to
forward one. ``check_model_engine`` still names a variable that is set, and says it is
ignored here (`credential_override`).

The engine picks the transport; the model keys (`model`, `reflect.model`,
`custodian.model`, `custodian.rail_models`) still pick the model. Under ``claude-code``
that value goes to ``--model``, so it must be a Claude alias or id. ``llm_api_base`` and
``llm_api_key`` are LiteLLM settings and are not used.

The resident path is unaffected: prompts the live session answers over MCP
(``*_emit`` / ``*_ingest``) never come through here.

THE CALL IS ISOLATED SO IT CANNOT FEED techne. A ``claude -p`` run inherits the user's
settings, and with them techne's own hooks and MCP server, so an unisolated summary call
would record observations about itself and queue more summaries. The flags below
remove that: ``--restricted`` ignores user, project and local settings (the hooks live
there) and drops the command-running tools, ``--settings`` adds
``disableAllHooks`` for any hook that arrives another way, ``--strict-mcp-config`` with
an empty ``--mcp-config`` loads no MCP server, ``--tools ""`` leaves no tool at all, and
``--no-session-persistence`` writes no transcript for techne's transcript import to pick
up later. It runs from a fresh temporary directory, so no project's CLAUDE.md is read.
Verified against Claude Code 2.1.290 on 2026-10-05: the reply came back and the
observation count did not move.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

LITELLM = "litellm"
CLAUDE_CODE = "claude-code"
MODEL_ENGINES = (LITELLM, CLAUDE_CODE)

#: A ``claude -p`` call always has a bound. LiteLLM's path leaves it unset unless the
#: caller sets ``llm_timeout`` on its config copy (only the custodian does, from
#: ``custodian.timeout_seconds``), but a subprocess that never returns would hang its
#: caller with nothing reporting it, so this engine applies a default otherwise.
DEFAULT_CLAUDE_TIMEOUT_S = 300

#: The isolation flags described in the module docstring. The test pins the exact
#: sequence against a literal of its own, so a dropped flag fails it.
ISOLATION_ARGS = (
    "--restricted",
    "--settings",
    '{"disableAllHooks":true}',
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--tools",
    "",
    "--no-session-persistence",
    "--output-format",
    "text",
)


#: Environment credentials Claude Code ranks above the subscription login under ``-p``.
#: `child_env` withholds them so the call always runs on the login (techne#1194).
CREDENTIAL_OVERRIDES = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


def claude_binary() -> str | None:
    """The ``claude`` CLI on PATH, or None."""
    return shutil.which("claude")


def child_env(env=None) -> dict:
    """A copy of *env* (default ``os.environ``) without `CREDENTIAL_OVERRIDES`."""
    env = os.environ if env is None else env
    return {k: v for k, v in env.items() if k not in CREDENTIAL_OVERRIDES}


def credential_override(env=None) -> str | None:
    """The first environment credential that a bare ``claude -p`` would use instead of the
    subscription login, or None. This engine withholds it (`child_env`); doctor reports
    it. Reads only whether the variable is set, never its value."""
    env = os.environ if env is None else env
    for name in CREDENTIAL_OVERRIDES:
        if env.get(name):
            return name
    return None


def claude_code_generate_fn(config):
    """A ``str -> str`` model callable that answers through ``claude -p``.

    Raises RuntimeError when ``claude`` is missing, times out or exits non-zero, the
    same way the LiteLLM path raises when litellm is missing or the provider refuses.
    Callers already handle that: each one decides whether to fall back or record
    the failure.
    """

    def generate(prompt: str) -> str:
        binary = claude_binary()
        if binary is None:
            raise RuntimeError(
                "model_engine is 'claude-code' but the `claude` CLI is not on PATH. "
                "Install Claude Code, or set model_engine: litellm."
            )
        argv = [binary, "-p", *ISOLATION_ARGS]
        if config.model:
            argv += ["--model", config.model]
        timeout = getattr(config, "llm_timeout", 0) or DEFAULT_CLAUDE_TIMEOUT_S
        logger.debug(
            "model_engine claude-code: model=%s timeout=%ss prompt_chars=%d",
            config.model or "(claude default)",
            timeout,
            len(prompt),
        )
        with tempfile.TemporaryDirectory(prefix="techne-claude-") as cwd:
            try:
                proc = subprocess.run(
                    argv,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                    env=child_env(),
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"`claude -p` did not answer within {timeout}s") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            raise RuntimeError(f"`claude -p` exited {proc.returncode}: {tail}")
        out = proc.stdout.strip()
        if not out:
            raise RuntimeError("`claude -p` returned an empty answer")
        return out

    return generate
