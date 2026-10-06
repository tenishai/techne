"""model_engine: how techne's own model calls go out (techne#1189)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from techne.config import Config
from techne.core import model_engine
from techne.core.checks import Verdict, check_model_engine
from techne.core.thread_orchestrator import _make_generate_fn


def _cfg(**kw) -> Config:
    c = Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture
def fake_claude(monkeypatch):
    """Put a fake `claude` on PATH and record each subprocess.run call."""
    calls: list[dict] = []
    reply = {"proc": _Proc(stdout="  the answer \n")}

    monkeypatch.setattr(model_engine.shutil, "which", lambda name: "/opt/bin/claude")

    def run(argv, **kw):
        calls.append({"argv": argv, **kw})
        if isinstance(reply["proc"], BaseException):
            raise reply["proc"]
        return reply["proc"]

    monkeypatch.setattr(model_engine.subprocess, "run", run)
    return calls, reply


# --- config ---------------------------------------------------------------


def test_default_engine_is_litellm():
    assert Config().model_engine == "litellm"


def _load(tmp_path: Path, monkeypatch, raw: dict) -> Config:
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    return Config.load()


def test_config_loads_claude_code(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch, {"model_engine": "claude-code"}).model_engine == (
        "claude-code"
    )


def test_config_refuses_an_unknown_engine(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="model_engine must be one of"):
        _load(tmp_path, monkeypatch, {"model_engine": "openai"})


def test_sparse_save_omits_the_default_and_keeps_a_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    c = Config.load()
    c.save()
    assert "model_engine" not in (tmp_path / "config.yaml").read_text()
    c.model_engine = "claude-code"
    c.save()
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["model_engine"] == "claude-code"


def test_model_engine_is_operator_governed():
    from techne.security.config_trust import EGRESS_KEYS

    assert "model_engine" in EGRESS_KEYS


# --- dispatch -------------------------------------------------------------


def test_litellm_engine_does_not_touch_claude(monkeypatch):
    monkeypatch.setattr(
        model_engine, "claude_code_generate_fn", lambda c: pytest.fail("claude engine used")
    )
    assert callable(_make_generate_fn(_cfg(model_engine="litellm")))


def test_claude_code_engine_routes_through_claude(fake_claude):
    calls, _ = fake_claude
    gen = _make_generate_fn(_cfg(model_engine="claude-code", model="haiku"))
    assert gen("hello") == "the answer"
    assert len(calls) == 1


# --- the claude -p call ---------------------------------------------------


def test_call_is_isolated_and_passes_prompt_on_stdin(fake_claude):
    calls, _ = fake_claude
    model_engine.claude_code_generate_fn(_cfg(model="sonnet"))("summarize this")
    c = calls[0]
    argv = c["argv"]
    assert argv[:2] == ["/opt/bin/claude", "-p"]
    # Every isolation flag, in order, against a literal so a dropped one fails here
    # (Prudence, PR #1190 r1: comparing against ISOLATION_ARGS itself pinned nothing).
    assert argv[2:-2] == [
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
    ]
    for flag in ("--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--settings") + 1] == '{"disableAllHooks":true}'
    assert argv[-2:] == ["--model", "sonnet"]
    # The prompt goes on stdin, never argv: argv has a size limit and shows in `ps`.
    assert c["input"] == "summarize this"
    assert "summarize this" not in argv
    # A fresh temp dir, so no project's CLAUDE.md is read.
    assert Path(c["cwd"]).name.startswith("techne-claude-")
    assert c["timeout"] == model_engine.DEFAULT_CLAUDE_TIMEOUT_S


def test_empty_model_leaves_the_choice_to_claude(fake_claude):
    calls, _ = fake_claude
    model_engine.claude_code_generate_fn(_cfg(model=""))("x")
    assert "--model" not in calls[0]["argv"]


def test_llm_timeout_overrides_the_default(fake_claude):
    calls, _ = fake_claude
    c = _cfg(model="haiku")
    c.llm_timeout = 42
    model_engine.claude_code_generate_fn(c)("x")
    assert calls[0]["timeout"] == 42


def test_missing_cli_raises_with_the_remedy(monkeypatch):
    monkeypatch.setattr(model_engine.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="not on PATH"):
        model_engine.claude_code_generate_fn(_cfg())("x")


def test_nonzero_exit_raises_with_stderr(fake_claude):
    _, reply = fake_claude
    reply["proc"] = _Proc(returncode=1, stderr="Not logged in · Please run /login")
    with pytest.raises(RuntimeError, match="exited 1: Not logged in"):
        model_engine.claude_code_generate_fn(_cfg())("x")


def test_timeout_raises(fake_claude):
    _, reply = fake_claude
    reply["proc"] = subprocess.TimeoutExpired(cmd="claude", timeout=1)
    with pytest.raises(RuntimeError, match="did not answer within"):
        model_engine.claude_code_generate_fn(_cfg())("x")


def test_empty_answer_raises(fake_claude):
    _, reply = fake_claude
    reply["proc"] = _Proc(stdout="   \n")
    with pytest.raises(RuntimeError, match="empty answer"):
        model_engine.claude_code_generate_fn(_cfg())("x")


# --- doctor ---------------------------------------------------------------


def test_doctor_litellm_is_dormant():
    r = check_model_engine(_cfg(model_engine="litellm"))
    assert (r.name, r.verdict) == ("model:engine", Verdict.DORMANT)


def test_doctor_claude_code_without_cli_is_broken_with_remedy(monkeypatch):
    monkeypatch.setattr(model_engine.shutil, "which", lambda name: None)
    r = check_model_engine(_cfg(model_engine="claude-code"))
    assert r.verdict == Verdict.BROKEN
    assert "model_engine: litellm" in r.remedy


def test_doctor_claude_code_with_cli_is_live(monkeypatch):
    monkeypatch.setattr(model_engine.shutil, "which", lambda name: "/opt/bin/claude")
    for name in model_engine.CREDENTIAL_OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    r = check_model_engine(_cfg(model_engine="claude-code"))
    assert r.verdict == Verdict.LIVE
    assert "bills" not in r.detail


@pytest.mark.parametrize("var", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_doctor_names_an_env_credential_that_claude_p_would_bill(monkeypatch, var):
    monkeypatch.setattr(model_engine.shutil, "which", lambda name: "/opt/bin/claude")
    for name in model_engine.CREDENTIAL_OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(var, "sk-not-read")
    r = check_model_engine(_cfg(model_engine="claude-code"))
    assert r.verdict == Verdict.LIVE
    assert f"{var} is set here" in r.detail
    assert "sk-not-read" not in r.detail


def test_auth_token_outranks_api_key():
    env = {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t"}
    assert model_engine.credential_override(env) == "ANTHROPIC_AUTH_TOKEN"
    assert model_engine.credential_override({}) is None


def test_child_inherits_the_environment_unchanged(fake_claude):
    """No env= override: the engine is "what `claude -p` does here", credentials and all."""
    calls, _ = fake_claude
    model_engine.claude_code_generate_fn(_cfg())("x")
    assert "env" not in calls[0]
