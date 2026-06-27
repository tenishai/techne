"""LLM answerer for the Stage-2 memory eval (#118).

Provides:
  resolve_eval_generate_fn() -- finds a reachable LLM, no key required;
                                 None → caller should pytest.skip.
  answer(question, context_rows, generate_fn) -- answers using ONLY the
                                                  provided memory context.

THE HARD CONTRACT: no required API key, no required network, NEVER fail CI
for lack of a model — SKIP. Resolver order:
  1. Local Ollama (probes localhost:11434/api/tags with 1s timeout).
  2. Operator model (only if ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY
     is already set in env — never provisions or reads a key file).
  3. None.

All litellm/network imports are lazy so this module is import-safe with no
litellm installed. A returned fn is VERIFIED-working (smoke-tested); None
means nothing reachable.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Callable

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _probe_ollama() -> tuple[bool, str]:
    """Probe local Ollama at localhost:11434/api/tags with a 1s timeout.

    Returns (reachable, model_name_or_empty).  Fails silently on any network
    or OS error — the caller treats (False, "") as "not available".
    """
    try:
        with urllib.request.urlopen(
            "http://localhost:11434/api/tags", timeout=1.0
        ) as resp:
            if resp.status != 200:
                return False, ""
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, socket.timeout):
        return False, ""

    override = os.environ.get("TECHNE_EVAL_OLLAMA_MODEL")
    if override:
        return True, override
    models = data.get("models") or []
    if not models:
        return False, ""
    name = models[0].get("name", "")
    return (True, name) if name else (False, "")


def _make_ollama_generate(model_name: str) -> Callable:
    """Build a generate_fn that calls Ollama via litellm (lazy import)."""

    def generate(prompt: str, max_tokens: int = 512) -> str:
        import litellm  # lazy

        resp = litellm.completion(
            model=f"ollama/{model_name}",
            api_base="http://localhost:11434",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content.strip()

    return generate


def _smoke_test(fn: Callable) -> bool:
    """Verify fn replies without raising on a trivial prompt.  Returns True iff ok."""
    try:
        result = fn("Reply with the single word: ok", max_tokens=5)
        return bool(result)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_eval_generate_fn() -> Callable | None:
    """Find the first reachable + verified LLM for Stage-2 eval.

    Returns a verified generate_fn(prompt, max_tokens=N) -> str, or None if
    nothing is reachable (caller should pytest.skip).

    Resolver order:
      1. Local Ollama: probe localhost:11434/api/tags; take first model (or
         TECHNE_EVAL_OLLAMA_MODEL if set).
      2. Operator model: first of ANTHROPIC_API_KEY / OPENAI_API_KEY /
         GEMINI_API_KEY found in env.  Only fires if the env-var key is
         already set — never provisions or reads a key file.
      3. None.

    Each candidate is smoke-tested before return.  If the smoke test raises,
    fall through to the next tier.  A returned fn is VERIFIED-working.
    All litellm imports are lazy — import-safe with no litellm installed.
    """
    # --- Tier 1: local Ollama (no key required) ---
    try:
        reachable, model_name = _probe_ollama()
        if reachable and model_name:
            fn = _make_ollama_generate(model_name)
            if _smoke_test(fn):
                return fn
    except ImportError:
        pass  # litellm not installed — skip tier

    # --- Tier 2: operator model — only if key env-var is already set ---
    _PROVIDERS = [
        ("ANTHROPIC_API_KEY", "claude-haiku-4-5-20251001"),
        ("OPENAI_API_KEY", "gpt-4o-mini"),
        ("GEMINI_API_KEY", "gemini/gemini-1.5-flash"),
    ]
    for env_var, model in _PROVIDERS:
        if not os.environ.get(env_var):
            continue
        try:
            from techne.config import Config
            from techne.core.thread_orchestrator import _make_generate_fn

            cfg = Config()
            cfg.model = model
            base_fn = _make_generate_fn(cfg)
            # Wrap to accept max_tokens kwarg (the orchestrator fn is
            # generate(prompt)->str; max_tokens is hardcoded there at 4096).
            def _wrapped(
                prompt: str, max_tokens: int = 512, _fn: Callable = base_fn
            ) -> str:
                return _fn(prompt)

            if _smoke_test(_wrapped):
                return _wrapped
        except Exception:
            pass  # litellm absent or model build failed — fall through to None
        break  # only attempt the first matching key

    return None


def answer(question: str, context_rows: list[dict], generate_fn: Callable) -> str:
    """Answer *question* using ONLY the provided memory *context_rows*.

    context_rows=[] (bare arm) → the prompt contains no memory entries → the
    model should respond "I don't know" for everything.

    Rows annotated with ``uncertainty`` (added by annotate_uncertain()) are
    flagged in the prompt so the model reports uncertainty rather than
    asserting the value as current.

    Returns the model's text response.
    """
    if context_rows:
        ctx_lines: list[str] = []
        for i, row in enumerate(context_rows, start=1):
            text = (
                row.get("content")
                or row.get("summary")
                or row.get("label")
                or ""
            )
            # annotate_uncertain() adds an `uncertainty` key with a nested dict
            # (state/tag/note); also handle a plain status='uncertain' field.
            is_uncertain = bool(row.get("uncertainty")) or (
                row.get("status") == "uncertain"
            )
            if is_uncertain:
                ctx_lines.append(
                    f"[{i}] [UNCERTAIN — basis may have changed; reconfirm"
                    f" before trusting] {text}"
                )
            else:
                ctx_lines.append(f"[{i}] {text}")
        ctx_block = "\n".join(ctx_lines)
        prompt = (
            "You are answering from a memory store. Answer the question using ONLY "
            "the memory entries below. Do NOT use outside knowledge.\n\n"
            "Rules:\n"
            "- If the memory does not contain enough information to answer, reply "
            "with exactly: I don't know.\n"
            "- If any entry is marked [UNCERTAIN], say the information may be stale "
            "or needs reconfirmation — do NOT assert it as confidently current.\n\n"
            f"Memory entries:\n{ctx_block}\n\n"
            f"Question: {question}\n\n"
            "Answer:"
        )
    else:
        prompt = (
            "You are answering from a memory store. There are NO memory entries "
            "available for this question.\n\n"
            "Since there is no memory to draw from, reply with exactly: "
            "I don't know.\n\n"
            f"Question: {question}\n\n"
            "Answer:"
        )
    return generate_fn(prompt)
