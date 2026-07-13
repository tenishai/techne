"""Tests for the UserPromptSubmit per-turn recall hook (techne#109/#277).

The hook injects durable knowledge relevant to the submitted prompt via
hookSpecificOutput.additionalContext. It is strictly best-effort: it must NEVER
block the turn (ALWAYS exit 0 — exit 2 would erase the prompt) and emit NOTHING on
flag-off, empty/missing prompt, malformed input, or no match. The stdin field that
carries the prompt text is undocumented in the CC hooks reference, so the hook
accepts multiple candidate keys — that robustness is exercised here. These tests
drive main() directly with a monkeypatched stdin and capture stdout via capsys.
"""

from __future__ import annotations

import io
import json

import pytest

from techne.hooks import user_prompt_submit


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A fresh TECHNE_HOME so Config.load() resolves db/data_dir under tmp_path."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    # Disable the SIGALRM budget so slow CI never trips the hard timeout mid-test.
    monkeypatch.setenv("TECHNE_HOOK_TIMEOUT", "0")
    return tmp_path


def _write_config(home, **hook_flags):
    """Write a config.yaml under TECHNE_HOME with the given hooks.* flags."""
    import yaml

    (home / "config.yaml").write_text(yaml.safe_dump({"hooks": hook_flags}))


def _seed(content, **kw):
    """Store a knowledge row in the TECHNE_HOME db so search() can recall it."""
    from techne.config import Config
    from techne.core import knowledge

    return knowledge.store(Config.load().db_path, content, **kw)


def _run(monkeypatch, capsys, payload: dict) -> str:
    """Run the hook main() with `payload` on stdin; return captured stdout."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as exc:
        user_prompt_submit.main()
    assert exc.value.code == 0, f"hook must always exit 0, got {exc.value.code}"
    return capsys.readouterr().out


def _injection_log_rows(source: str | None = None) -> list[dict]:
    """Read the techne#329 context_injections visibility log from the TECHNE_HOME db."""
    import sqlite3

    from techne.config import Config

    conn = sqlite3.connect(str(Config.load().db_path))
    try:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "context_injections" not in names:
            return []
        cur = conn.execute(
            "SELECT source, session_id, trigger_snippet, hit_count, hit_ids, scores, "
            "approx_tokens FROM context_injections ORDER BY id"
        )
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        return [r for r in rows if source is None or r["source"] == source]
    finally:
        conn.close()


def test_flag_off_default_emits_nothing(home, monkeypatch, capsys):
    """Flag OFF (no config.yaml → default False) → no stdout, even with a match."""
    _seed("Postgres was chosen over MongoDB for join semantics.",
          type="decision", confidence="high")
    out = _run(monkeypatch, capsys, {"prompt": "why postgres"})
    assert out == ""


def test_flag_on_matching_injects_context(home, monkeypatch, capsys):
    """Flag ON + prompt matching a stored fact → protocol JSON injection."""
    _write_config(home, per_turn_recall=True)
    _seed("Postgres was chosen over MongoDB for join semantics.",
          type="decision", tags="storage", confidence="high")
    out = _run(monkeypatch, capsys, {"prompt": "postgres join decision"})
    assert out.strip(), "expected protocol JSON on stdout"
    payload = json.loads(out)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    assert "Postgres" in hso["additionalContext"]


# ---- techne#329: recall visibility log --------------------------------------


def test_recall_injection_is_logged_with_hit_ids(home, monkeypatch, capsys):
    """A recall that injects is recorded in recall_injections with the injected
    knowledge ids + token cost — the READ path is now auditable.

    Fail-without-fix: pre-#329 the rail left no trace, so this log stays empty.
    """
    _write_config(home, per_turn_recall=True)
    _seed(
        "Postgres was chosen over MongoDB for join semantics.",
        type="decision", tags="storage", confidence="high",
    )
    out = _run(
        monkeypatch, capsys,
        {"prompt": "postgres join decision", "session_id": "sess-1"},
    )
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "Postgres" in ctx  # still injects as before

    rows = _injection_log_rows("per_turn_recall")
    assert len(rows) == 1
    r = rows[0]
    assert r["source"] == "per_turn_recall"
    assert r["session_id"] == "sess-1"
    assert r["hit_count"] >= 1
    ids = json.loads(r["hit_ids"])
    assert ids and ids[0] is not None
    # the logged ids are exactly what was injected (⟦techne#<id>⟧ in the block)
    assert f"techne#{ids[0]}" in ctx
    assert r["approx_tokens"] > 0


def test_empty_recall_is_logged_with_zero_hits(home, monkeypatch, capsys):
    """A recall that fires but matches nothing is logged with hit_count=0 —
    'fired but found nothing' is signal (wasted rail cost), not silence."""
    _write_config(home, per_turn_recall=True)
    _seed("Postgres was chosen over MongoDB.", type="decision", confidence="high")
    _run(monkeypatch, capsys, {"prompt": "zzznomatchqqq", "session_id": "sess-2"})

    rows = _injection_log_rows("per_turn_recall")
    assert len(rows) == 1
    assert rows[0]["hit_count"] == 0
    assert rows[0]["approx_tokens"] == 0


def test_recall_log_can_be_disabled(home, monkeypatch, capsys):
    """per_turn_recall_log=False → recall still injects, but nothing is logged."""
    _write_config(home, per_turn_recall=True, per_turn_recall_log=False)
    _seed(
        "Postgres was chosen over MongoDB for join semantics.",
        type="decision", confidence="high",
    )
    out = _run(monkeypatch, capsys, {"prompt": "postgres join decision"})
    assert "Postgres" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert _injection_log_rows() == []


def test_injection_log_helper_never_raises_on_bad_db():
    """Instrumentation must never break the turn — a bad db path is swallowed."""
    from techne.hooks._injection_log import log_context_injection

    log_context_injection(
        "/nonexistent/dir/nope.db",
        source="per_turn_recall",
        session_id="x",
        trigger="p",
        rows=[{"id": 1, "rank": -1.0}],
        injected_text="hi",
    )  # must not raise


def test_no_match_emits_nothing(home, monkeypatch, capsys):
    """Flag ON, db populated, but prompt matches nothing → no stdout."""
    _write_config(home, per_turn_recall=True)
    _seed("Postgres was chosen for join semantics.", type="decision", confidence="high")
    out = _run(monkeypatch, capsys, {"prompt": "zzznomatchqqq"})
    assert out == ""


def test_empty_prompt_emits_nothing(home, monkeypatch, capsys):
    """A whitespace-only prompt → no usable text → no stdout."""
    _write_config(home, per_turn_recall=True)
    _seed("Postgres decision.", type="decision", confidence="high")
    out = _run(monkeypatch, capsys, {"prompt": "   "})
    assert out == ""


def test_missing_prompt_key_emits_nothing(home, monkeypatch, capsys):
    """No known prompt key present → clean no-op (never fires on garbage)."""
    _write_config(home, per_turn_recall=True)
    _seed("Postgres decision.", type="decision", confidence="high")
    out = _run(monkeypatch, capsys, {"hook_event_name": "UserPromptSubmit"})
    assert out == ""


def test_malformed_stdin_emits_nothing(home, monkeypatch, capsys):
    """Non-JSON stdin → exit 0, no stdout."""
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    with pytest.raises(SystemExit) as exc:
        user_prompt_submit.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == ""


def test_alternate_prompt_key_user_prompt(home, monkeypatch, capsys):
    """Defensive design: the prompt field name is undocumented, so an alternate
    key (`user_prompt`) is honored too — the hook must not hard-depend on `prompt`."""
    _write_config(home, per_turn_recall=True)
    _seed("Postgres was chosen for join semantics.", type="decision", confidence="high")
    out = _run(monkeypatch, capsys, {"user_prompt": "postgres join"})
    payload = json.loads(out)
    assert "Postgres" in payload["hookSpecificOutput"]["additionalContext"]


# ---- Seed block (techne#277) ----------------------------------------------


def test_seed_flag_on_injects_nudge(home, monkeypatch, capsys):
    """per_turn_seed ON → the static seed nudge is injected (no DB needed)."""
    _write_config(home, per_turn_seed=True)
    out = _run(monkeypatch, capsys, {"prompt": "anything"})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "seed check" in ctx and "techne_store" in ctx


def test_seed_flag_off_no_nudge(home, monkeypatch, capsys):
    """Recall ON but seed OFF → recalled fact present, seed nudge absent."""
    _write_config(home, per_turn_recall=True)
    _seed("Postgres was chosen for join semantics.", type="decision", confidence="high")
    out = _run(monkeypatch, capsys, {"prompt": "postgres join"})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "Postgres" in ctx
    assert "seed check" not in ctx


def test_seed_and_recall_compose(home, monkeypatch, capsys):
    """Both flags ON + a matching fact → additionalContext carries BOTH the
    recalled fact and the seed nudge."""
    _write_config(home, per_turn_recall=True, per_turn_seed=True)
    _seed("Postgres was chosen for join semantics.", type="decision", confidence="high")
    out = _run(monkeypatch, capsys, {"prompt": "postgres join"})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "Postgres" in ctx and "seed check" in ctx


def test_seed_only_fires_without_a_recall_match(home, monkeypatch, capsys):
    """Seed ON, recall OFF, nothing matches → the nudge still fires (static,
    independent of any search hit)."""
    _write_config(home, per_turn_seed=True)  # per_turn_recall stays False
    out = _run(monkeypatch, capsys, {"prompt": "zzznomatchqqq"})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "techne_store" in ctx


def test_both_off_emits_nothing(home, monkeypatch, capsys):
    """Explicit config with both per-turn flags off → clean no-op."""
    _write_config(home, per_turn_recall=False, per_turn_seed=False)
    _seed("Postgres decision.", type="decision", confidence="high")
    out = _run(monkeypatch, capsys, {"prompt": "postgres"})
    assert out == ""


def test_non_dict_json_emits_nothing(home, monkeypatch, capsys):
    """Valid-but-non-object JSON on stdin ([], 5, "foo") → clean no-op, not a
    traceback (the malformed-stdin contract covers these too)."""
    _write_config(home, per_turn_seed=True)
    monkeypatch.setattr("sys.stdin", io.StringIO("[1, 2, 3]"))
    with pytest.raises(SystemExit) as exc:
        user_prompt_submit.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == ""
