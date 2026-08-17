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
    """A recall that injects is recorded in context_injections with the injected
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


# ---- techne#561: pull-only types must not ride the PUSH rail ----------------


def _seed_raw(content: str, ktype: str) -> int:
    """Insert a knowledge row of ANY type, bypassing store()'s validation.

    `store()` rejects `pattern` outright (REDIRECTED_TYPES, techne#137) — which is
    the point: the rows that matter here are LEGACY ones that predate that gate and
    survive on un-cleaned installs, exactly the population PULL_ONLY_TYPES still
    lists `pattern` for. They cannot be created through the public API.
    """
    import sqlite3

    from techne.config import Config
    from techne.core.database import init_db

    db_path = Config.load().db_path
    init_db(db_path)  # store() creates the schema lazily; a raw INSERT must not rely on that
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO knowledge (content, type, confidence) VALUES (?, ?, ?)",
            (content, ktype, "high"),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_pull_only_pattern_row_is_not_pushed(home, monkeypatch, capsys):
    """A legacy `pattern` row must never be auto-injected by the per-turn rail.

    Fail-without-fix: this rail calls search() directly and search() applies no
    type filter, so before techne#561 the pattern row rode straight into context —
    the leak REDIRECTED_TYPES describes ("leaked team patterns into personal
    recall"), on a push path PULL_ONLY_TYPES' egress-only enforcement never saw.
    """
    _write_config(home, per_turn_recall=True)
    _seed_raw("Postgres was chosen over MongoDB for join semantics.", "pattern")
    out = _run(monkeypatch, capsys, {"prompt": "postgres join decision"})
    assert out.strip() == "", f"a pull-only row must not be pushed: {out!r}"


def test_pull_only_filter_does_not_starve_a_normal_recall(home, monkeypatch, capsys):
    """The filter drops ONLY pull-only rows — a matching fact still injects."""
    _write_config(home, per_turn_recall=True)
    _seed_raw("Postgres was chosen over MongoDB for join semantics.", "pattern")
    _seed("Postgres connection pooling is handled by pgbouncer.",
          type="fact", tags="storage", confidence="high")
    out = _run(monkeypatch, capsys, {"prompt": "postgres pooling"})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "pgbouncer" in ctx
    assert "join semantics" not in ctx  # search() labels every row [knowledge], so match on content


def test_filtered_row_is_not_logged_as_injected(home, monkeypatch, capsys):
    """context_injections must record what was INJECTED, not what was retrieved.

    Otherwise the #560 attribution panel would credit encounters that never
    reached the agent, and hit_count would overstate the token cost.
    """
    _write_config(home, per_turn_recall=True)
    pid = _seed_raw("Postgres was chosen over MongoDB for join semantics.", "pattern")
    _seed("Postgres connection pooling is handled by pgbouncer.",
          type="fact", tags="storage", confidence="high")
    _run(monkeypatch, capsys, {"prompt": "postgres pooling", "session_id": "s-561"})

    rows = _injection_log_rows("per_turn_recall")
    assert len(rows) == 1
    ids = json.loads(rows[0]["hit_ids"])
    assert pid not in ids
    assert rows[0]["hit_count"] == len(ids)


def test_document_card_is_not_pushed(home, monkeypatch, capsys):
    """Arch cards are pull-only on THIS rail too (techne#561, decided 2026-08-13).

    This replaces a test that pinned the opposite behaviour while the question was
    open. What decided it: `_build_context` truncates every row to 200 chars, and
    an arch card's value is its full body — a 200-char slice chosen by embedding
    similarity is a pointer at best. #449's edit-fired rail, matching on `anchor:`
    paths exactly, is the delivery mechanism cards are supposed to get.
    """
    _write_config(home, per_turn_recall=True)
    _seed_raw("Postgres was chosen over MongoDB for join semantics.", "document_card")
    out = _run(monkeypatch, capsys, {"prompt": "postgres join decision"})
    assert out.strip() == "", f"an arch card must not be pushed: {out!r}"


def test_push_filter_is_pull_only_types_itself(home, monkeypatch, capsys):
    """The rail excludes exactly PULL_ONLY_TYPES — not a second, drifting copy.

    A restated subset is how the original constant came to be enforced on one rail
    only; this asserts the hook shares the definition rather than mirroring it.
    """
    from techne.core.knowledge import PULL_ONLY_TYPES

    _write_config(home, per_turn_recall=True)
    for i, ktype in enumerate(PULL_ONLY_TYPES):
        _seed_raw(f"Postgres join semantics note {i}.", ktype)
    out = _run(monkeypatch, capsys, {"prompt": "postgres join semantics"})
    assert out.strip() == "", f"no PULL_ONLY_TYPES row may be pushed: {out!r}"


# ---- techne#566: the injected label must be the row's kind, not its table ----


class TestInjectedTypeLabel:
    """`_build_context` labels each recalled row. That label must be the row's
    KIND (fact / decision / procedure / …), not the table it came out of.

    THE BUG: `search()` sets `type` to the SOURCE TABLE and returns the row's own
    kind separately as `knowledge_type`. Reading `type` labelled every injected
    row `[knowledge]`.

    WHY IT SURVIVED A MONTH: this rendering mirrors `egress._render_line`, which
    reads `row["type"]` — correctly, because egress reads knowledge rows straight
    from the table, where `type` IS the kind. Same field name, two shapes. The
    SessionStart block showed `[fact]`/`[procedure]` while the per-turn block
    showed `[knowledge]` for the same rows, in plain sight, every turn.
    """

    def _ctx(self, rows):
        from techne.hooks.user_prompt_submit import _build_context

        return _build_context(rows)

    def test_the_row_kind_is_shown_not_the_table(self):
        out = self._ctx(
            [
                {"id": 1, "type": "knowledge", "knowledge_type": "decision", "content": "d"},
                {"id": 2, "type": "knowledge", "knowledge_type": "procedure", "content": "p"},
            ]
        )
        assert "[decision]" in out
        assert "[procedure]" in out
        assert "[knowledge]" not in out

    def test_distinct_kinds_stay_distinguishable(self):
        """The point of the label. A `decision` and an `evaluation` warrant
        different weight; collapsing them removes the distinction exactly where
        the agent decides how much to trust the line.

        `document_card` and `pattern` are included as RENDERER inputs, not as a
        claim they reach this rail — `_drop_pull_only` filters both upstream."""
        kinds = ["fact", "decision", "evaluation", "procedure", "document_card", "pattern"]
        # Content deliberately NOT equal to the kind: with `content=k` the assertion
        # could not tell a correct render from one that swapped label and content,
        # because both strings were the same (#566 r1 Nit).
        rows = [
            {"id": i, "type": "knowledge", "knowledge_type": k, "content": f"body-{i}"}
            for i, k in enumerate(kinds)
        ]
        out = self._ctx(rows)
        for i, k in enumerate(kinds):
            assert f"[{k}] body-{i}" in out, f"{k} was collapsed or rendered out of position"

    def test_a_pattern_row_is_visibly_a_pattern(self):
        """techne#561's month-long miss: a legacy `pattern` row rode the push rail
        and was visually indistinguishable from a fact.

        PRECISION THIS TEST OWES (#566 r2): `pattern` and `document_card` are in
        `PULL_ONLY_TYPES`, and `_drop_pull_only` runs BEFORE the renderer — so
        today they cannot reach this rail at all. #561 predates that filter. This
        pins `_build_context` as a pure function of the row it is handed, which is
        what makes it correct for whatever the filter's contents become; it is not
        evidence about what this rail currently emits."""
        out = self._ctx(
            [{"id": 9, "type": "knowledge", "knowledge_type": "pattern", "content": "x"}]
        )
        assert "[pattern]" in out

    def test_non_knowledge_rows_fall_back_to_the_table(self):
        """Threads and observations carry no `knowledge_type`; for those the table
        IS the most honest label available, so the fallback must survive."""
        out = self._ctx(
            [
                {"id": 3, "type": "observation", "content": "o"},
                {"id": 4, "type": "thread", "content": "t"},
            ]
        )
        assert "[observation]" in out
        assert "[thread]" in out

    def test_a_row_with_neither_is_labelled_note(self):
        assert "[note]" in self._ctx([{"id": 5, "content": "c"}])

    def test_the_content_truncation_is_pinned(self):
        """200 chars, and it was pinned by nothing — removing the slice entirely
        left the suite green (#566 r2) while a recorded `document_card` decision
        rests on it. An unbounded line here is a per-turn injection whose size is
        set by whatever the longest matching row happens to be."""
        out = self._ctx(
            [{"id": 1, "type": "knowledge", "knowledge_type": "fact", "content": "x" * 500}]
        )
        assert "x" * 200 in out
        assert "x" * 201 not in out

    def test_the_line_shape_is_pinned_including_the_id_prefix(self):
        """The `⟦techne#id⟧` prefix was pinned by nothing — deleting it left the
        suite green (#566 r1 Nit). It is what makes an injected line citable, and
        citation is what `recall_attribution` measures, so losing it silently
        would take the attribution rail's only signal with it."""
        out = self._ctx([{"id": 42, "type": "knowledge", "knowledge_type": "fact", "content": "z"}])
        assert "- ⟦techne#42⟧ [fact] z" in out
