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
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
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
    _seed(
        "Postgres was chosen over MongoDB for join semantics.", type="decision", confidence="high"
    )
    out = _run(monkeypatch, capsys, {"prompt": "why postgres"})
    assert out == ""


def test_flag_on_matching_injects_context(home, monkeypatch, capsys):
    """Flag ON + prompt matching a stored fact → protocol JSON injection."""
    _write_config(home, per_turn_recall=True)
    _seed(
        "Postgres was chosen over MongoDB for join semantics.",
        type="decision",
        tags="storage",
        confidence="high",
    )
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
        type="decision",
        tags="storage",
        confidence="high",
    )
    out = _run(
        monkeypatch,
        capsys,
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
        type="decision",
        confidence="high",
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
    _seed(
        "Postgres connection pooling is handled by pgbouncer.",
        type="fact",
        tags="storage",
        confidence="high",
    )
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
    _seed(
        "Postgres connection pooling is handled by pgbouncer.",
        type="fact",
        tags="storage",
        confidence="high",
    )
    _run(monkeypatch, capsys, {"prompt": "postgres pooling", "session_id": "s-561"})

    rows = _injection_log_rows("per_turn_recall")
    assert len(rows) == 1
    ids = json.loads(rows[0]["hit_ids"])
    assert pid not in ids
    assert rows[0]["hit_count"] == len(ids)


def test_document_card_is_not_pushed(home, monkeypatch, capsys):
    """Arch cards are pull-only on THIS rail too (techne#561, decided 2026-08-13).

    This replaces a test that pinned the opposite behaviour while the question was
    open. What decided it: `_build_context` truncates every row to
    `hooks.per_turn_recall_chars` (200 by default), and an arch card's value is
    its full body — a slice chosen by embedding similarity is a pointer at best.
    techne#826 made the cap configurable and the cut boundary-aware; neither
    changes this argument, because a slice is a pointer at any size. #449's
    edit-fired rail, matching on `anchor:` paths exactly, is the delivery
    mechanism cards are supposed to get.
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
                {
                    "id": 1,
                    "source_table": "knowledge",
                    "knowledge_type": "decision",
                    "content": "d",
                },
                {
                    "id": 2,
                    "source_table": "knowledge",
                    "knowledge_type": "procedure",
                    "content": "p",
                },
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
            {"id": i, "source_table": "knowledge", "knowledge_type": k, "content": f"body-{i}"}
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
            [{"id": 9, "source_table": "knowledge", "knowledge_type": "pattern", "content": "x"}]
        )
        assert "[pattern]" in out

    def test_non_knowledge_rows_fall_back_to_the_table(self):
        """Threads and observations carry no `knowledge_type`; for those the table
        IS the most honest label available, so the fallback must survive."""
        out = self._ctx(
            [
                {"id": 3, "source_table": "observation", "content": "o"},
                {"id": 4, "source_table": "thread", "content": "t"},
            ]
        )
        assert "[observation]" in out
        assert "[thread]" in out

    def test_a_row_with_neither_is_labelled_note(self):
        assert "[note]" in self._ctx([{"id": 5, "content": "c"}])

    def test_the_content_truncation_is_pinned(self):
        """The CAP, still 200 by default, and it was pinned by nothing — removing
        the slice entirely left the suite green (#566 r2) while a recorded
        `document_card` decision rests on it. An unbounded line here is a per-turn
        injection whose size is set by whatever the longest matching row happens to
        be.

        This row is 500 unbroken `x`, so there is no boundary for techne#826's
        boundary-seeking to find and the cut is hard at the cap — which is what
        keeps this assertion a test of the CAP rather than of the cut point. The
        boundary behaviour is pinned separately below."""
        out = self._ctx(
            [{"id": 1, "source_table": "knowledge", "knowledge_type": "fact", "content": "x" * 500}]
        )
        assert "x" * 200 in out
        assert "x" * 201 not in out

    def test_a_cut_row_never_ends_mid_word(self):
        """techne#826, the measured defect: over one real session 58 of 58 injected
        facts ended mid-word — a cross-reference severed at `CORRECTS techne#`, a
        remedy command severed mid-path. The agent receives something that looks
        like the rule, is topically correct, and is missing the operative half.
        """
        body = (
            "THE VERSION STRING CANNOT IDENTIFY THE RUNNING CODE. Two independent "
            "defects, both measured 2026-08-13. CORRECTS techne#1078 and supersedes "
            "the guidance recorded there, which nobody has re-read since. The rest "
            "of this row exists to carry it past the cap so the cut actually fires."
        )
        assert len(body) > 200, "fixture must exceed the cap or it tests nothing"
        out = self._ctx([{"id": 1, "knowledge_type": "fact", "content": body}])
        rendered = out.splitlines()[-1]
        assert rendered.endswith(" …")

        # THE ASSERTION IS "the cut is not inside a word", not "the cut is a full
        # stop". This fixture's only sentence end sits below the 40% floor, so the
        # correct behaviour here is the WORD-boundary fallback — an earlier draft
        # of this test demanded `.!?` and failed against working code.
        visible = rendered.split("] ", 1)[1].removesuffix(" …")
        assert body.startswith(visible), "the cut text must be a prefix of the row"
        assert body[len(visible)] in " \t\n", (
            f"cut lands mid-word: ...{visible[-30:]!r} then {body[len(visible)]!r}"
        )

    def test_a_cut_row_announces_itself_and_an_uncut_one_does_not(self):
        """A truncated row that looks complete is the whole defect. The marker is
        what lets a reader tell "I have this" from "I have the first sentence of
        this", and the header names the knob so the remedy is in the block."""
        long_out = self._ctx([{"id": 1, "knowledge_type": "fact", "content": "word " * 200}])
        assert " …" in long_out
        assert "per_turn_recall_chars" in long_out

        short_out = self._ctx([{"id": 2, "knowledge_type": "fact", "content": "short row"}])
        assert " …" not in short_out
        assert "per_turn_recall_chars" not in short_out, (
            "an uncut block must not carry a truncation notice — a warning that "
            "fires when nothing was cut trains the reader to ignore it"
        )

    def test_boundary_seeking_falls_back_rather_than_gutting_the_row(self):
        """THE 40% FLOOR IS THE POINT. Without it, a row whose only sentence end
        sits at char 12 would be cut to 12 characters in the name of tidiness —
        boundary-seeking made strictly worse than the bare slice it replaced.
        """
        from techne.hooks.user_prompt_submit import _truncate_for_injection

        body = "Yes. " + "a" * 400
        out, was_cut = _truncate_for_injection(body, 200)
        assert was_cut
        assert len(out) > 150, f"gutted to {len(out)} chars at the early full stop"

    def test_the_cap_is_read_from_config_not_hardcoded(self):
        """techne#826 direction (3): this was the only fixed number in the rail's
        tuning surface, while `per_turn_recall_limit` beside it was already a knob.
        """
        from techne.hooks.user_prompt_submit import _build_context

        rows = [{"id": 1, "knowledge_type": "fact", "content": "word " * 200}]
        assert len(_build_context(rows, 500)) > len(_build_context(rows, 200))

    def test_the_line_shape_is_pinned_including_the_id_prefix(self):
        """The `⟦techne#id⟧` prefix was pinned by nothing — deleting it left the
        suite green (#566 r1 Nit). It is what makes an injected line citable, and
        citation is what `recall_attribution` measures, so losing it silently
        would take the attribution rail's only signal with it."""
        out = self._ctx(
            [{"id": 42, "source_table": "knowledge", "knowledge_type": "fact", "content": "z"}]
        )
        assert "- ⟦techne#42⟧ [fact] z" in out


# ---------------------------------------------------------------------------
# Startup directive (techne#643) — once per session, gated on pending work.
#
# The hook-level contract is what's covered here: WHEN the block fires, that it
# fires at most once per session, and that it stays silent when there is nothing
# to drain. `pending_reflection` is patched rather than fixtured because it has
# its own tests — what would drift undetected is the gate around it, not it.
# ---------------------------------------------------------------------------


def _patch_pending(monkeypatch, result):
    """Point the startup block's signal at `result` (a list of session dicts)."""
    monkeypatch.setattr("techne.core.reflect.pending_reflection", lambda config, **kw: result)


def _state(name: str):
    """Read one extension_state payload from the TECHNE_HOME db."""
    from techne.config import Config
    from techne.core.database import connect
    from techne.core.extension_state import get_state

    conn = connect(Config.load().db_path)
    try:
        return get_state(conn, name)
    finally:
        conn.close()


def test_startup_directive_off_by_default(home, monkeypatch, capsys):
    """Default-OFF, like every other injecting block on this hook."""
    _write_config(home, per_turn_recall=False, per_turn_seed=False)
    _patch_pending(monkeypatch, [{"session_id": "s1", "observations": 40}])
    out = _run(monkeypatch, capsys, {"prompt": "hello", "session_id": "s1"})
    assert out == ""


def test_startup_directive_fires_on_first_prompt_when_work_pending(home, monkeypatch, capsys):
    _write_config(home, startup_directive=True)
    _patch_pending(monkeypatch, [{"session_id": "old", "observations": 28}])
    out = _run(monkeypatch, capsys, {"prompt": "hello", "session_id": "s1"})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "startup drain" in ctx
    assert "~28 unreflected observation" in ctx
    # The two constraints that make the drain correct rather than merely present.
    assert "techne_reflect_emit" in ctx
    assert "#646" in ctx


def test_startup_directive_fires_at_most_once_per_session(home, monkeypatch, capsys):
    """The whole point of the extension_state gate."""
    _write_config(home, startup_directive=True)
    _patch_pending(monkeypatch, [{"session_id": "old", "observations": 28}])
    first = _run(monkeypatch, capsys, {"prompt": "one", "session_id": "s1"})
    assert "startup drain" in first
    second = _run(monkeypatch, capsys, {"prompt": "two", "session_id": "s1"})
    assert second == ""


def test_a_new_session_re_arms_the_directive(home, monkeypatch, capsys):
    """Session-scoped key, so a new session needs no cleanup pass to re-arm."""
    _write_config(home, startup_directive=True)
    _patch_pending(monkeypatch, [{"session_id": "old", "observations": 28}])
    _run(monkeypatch, capsys, {"prompt": "one", "session_id": "s1"})
    out = _run(monkeypatch, capsys, {"prompt": "one", "session_id": "s2"})
    assert "startup drain" in out


def test_no_pending_work_emits_nothing_but_still_closes_the_gate(home, monkeypatch, capsys):
    """Silence is the common case, and it must not cost a DB read every turn.

    The marker is written even though nothing was injected — "once per session",
    not "once per session that had work". Without it a quiet session re-runs the
    pending query on every single prompt.
    """
    _write_config(home, startup_directive=True)
    _patch_pending(monkeypatch, [])
    out = _run(monkeypatch, capsys, {"prompt": "hello", "session_id": "s1"})
    assert out == ""
    assert _state("startup_directive:s1") == {"fired": False, "sessions": 0}


def test_missing_session_id_stays_silent(home, monkeypatch, capsys):
    """No session id means no gate is possible; an ungated directive would fire
    on EVERY prompt, which is the failure mode this feature exists to avoid."""
    _write_config(home, startup_directive=True)
    _patch_pending(monkeypatch, [{"session_id": "old", "observations": 28}])
    out = _run(monkeypatch, capsys, {"prompt": "hello"})
    assert out == ""


def test_startup_failure_never_costs_the_turn(home, monkeypatch, capsys):
    """A chore nudge is the least important thing this hook does."""
    _write_config(home, startup_directive=True, per_turn_seed=True)

    def _boom(config, **kw):
        raise RuntimeError("signal unavailable")

    monkeypatch.setattr("techne.core.reflect.pending_reflection", _boom)
    out = _run(monkeypatch, capsys, {"prompt": "hello", "session_id": "s1"})
    # Exit 0 is asserted inside _run; the seed block must survive the failure.
    assert "seed check" in out
    assert "startup drain" not in out


def test_drop_pull_only_ignores_rows_from_other_tables(tmp_path):
    """techne#608: the id lookup reads `knowledge`, and ids are PER-TABLE.

    Correct today only because the single caller hardcodes table="knowledge".
    Widen that to "all" — plausible, since recall already spans tables — and an
    OBSERVATION with id 42 gets dropped because KNOWLEDGE row 42 happens to be a
    pull-only kind. Silent, wrong, and it would read as the guard working.

    This drives the widened shape directly rather than waiting for the caller to
    change, because "a guard whose correctness depends on a condition held
    somewhere else and written down nowhere" is the exact class this milestone
    keeps surfacing.
    """
    from techne.core.database import init_db
    from techne.core.knowledge import store
    from techne.hooks.user_prompt_submit import _drop_pull_only

    db = tmp_path / "t.db"
    init_db(db).close()
    # `document` is pull-only AND storable in personal memory; `pattern` is
    # refused here by design (techne#137, it belongs in architect.db).
    doc_id = store(db, "a pull-only document row", type="document")

    rows = [
        {"source_table": "knowledge", "id": doc_id, "content": "the document"},
        # An observation that merely SHARES the id. Not this guard's business.
        {"source_table": "observation", "id": doc_id, "content": "an unrelated observation"},
    ]
    kept = _drop_pull_only(db, rows)

    kept_kinds = {(r["source_table"], r["id"]) for r in kept}
    assert ("knowledge", doc_id) not in kept_kinds, "the pull-only knowledge row must go"
    assert ("observation", doc_id) in kept_kinds, (
        "an observation was dropped because a KNOWLEDGE row shares its id (#608)"
    )


# ---------------------------------------------------------------------------
# techne#576 — the per-turn rail had no memory of what it already said.
# ---------------------------------------------------------------------------


def _injection_row(db, session_id: str, hit_ids: list[int], *, when: str | None = None):
    """Record a delivery the way log_context_injection does."""
    import json as _json

    from techne.core.database import connect
    from techne.hooks._injection_log import _DDL

    conn = connect(db)
    try:
        # The table is created lazily by log_context_injection, not by init_db —
        # so build it the same way rather than hand-rolling a copy that could drift.
        conn.executescript(_DDL)
        if when:
            conn.execute(
                "INSERT INTO context_injections "
                "(source, session_id, timestamp, hit_ids, hit_count, approx_tokens) "
                "VALUES ('per_turn_recall', ?, ?, ?, ?, 0)",
                (session_id, when, _json.dumps(hit_ids), len(hit_ids)),
            )
        else:
            conn.execute(
                "INSERT INTO context_injections "
                "(source, session_id, hit_ids, hit_count, approx_tokens) "
                "VALUES ('per_turn_recall', ?, ?, ?, 0)",
                (session_id, _json.dumps(hit_ids), len(hit_ids)),
            )
        conn.commit()
    finally:
        conn.close()


def test_rows_already_sent_this_session_are_reported(home):
    """The measurement this fixes: 46.1% of 7,004 live deliveries went to a session
    that had already received that exact row (re-measured 2026-08-18; #208 alone
    accounts for 223 redundant re-sends of a note dated 2026-06-10)."""
    from techne.core.database import init_db
    from techne.core.state import ensure_session
    from techne.hooks.user_prompt_submit import _already_sent_this_session

    db = home / "t.db"
    init_db(db).close()
    ensure_session(db, "s-576")
    _injection_row(db, "s-576", [208, 728])
    _injection_row(db, "s-576", [198])

    assert _already_sent_this_session(db, "s-576") == {208, 728, 198}
    assert _already_sent_this_session(db, "other-session") == set()
    assert _already_sent_this_session(db, None) == set(), "no session id → suppress nothing"


def test_a_compaction_resets_the_suppression_window(home):
    """THE BOUNDARY THAT MAKES SUPPRESSION SAFE.

    After a compact, previously injected text may no longer be in the model's
    context. Suppressing on the strength of a delivery that PREDATES the compact
    would withhold something the agent no longer has — turning a waste fix into a
    correctness bug. Only deliveries since the last checkpoint count.
    """
    from techne.core.database import connect, init_db
    from techne.core.state import ensure_session
    from techne.hooks.user_prompt_submit import _already_sent_this_session

    db = home / "t.db"
    init_db(db).close()
    ensure_session(db, "s-576b")

    _injection_row(db, "s-576b", [111], when="2026-08-18 10:00:00")
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO checkpoints (session_id, trigger, data_json, created_at) "
            "VALUES (?, 'auto', '{}', '2026-08-18 11:00:00')",
            ("s-576b",),
        )
        conn.commit()
    finally:
        conn.close()
    _injection_row(db, "s-576b", [222], when="2026-08-18 12:00:00")

    sent = _already_sent_this_session(db, "s-576b")
    assert 222 in sent, "a delivery AFTER the compact is still in the window"
    assert 111 not in sent, (
        "a delivery from BEFORE the compact must not suppress — that content may "
        "have left the context window, and withholding it would be a correctness bug"
    )


def test_suppression_failure_degrades_to_resending_not_to_silence(home):
    """Suppression is an optimisation. If it breaks, the rail must fall back to the
    old (wasteful) behaviour rather than to injecting nothing."""
    from techne.hooks.user_prompt_submit import _already_sent_this_session

    assert _already_sent_this_session(home / "does-not-exist.db", "s") == set()


# ---------------------------------------------------------------------------
# techne#414 — "inject nothing" becomes a legal, logged outcome.
# ---------------------------------------------------------------------------


def _vec_row(id_: int, distance: float) -> dict:
    """A vec-channel hit as search() returns it on the union path."""
    return {
        "source_table": "knowledge",
        "id": id_,
        "knowledge_type": "fact",
        "content": "x",
        "distance": distance,
        "match_source": "vec",
    }


def _fts_row(id_: int, rank: float) -> dict:
    """An FTS-only hit. `match_source` is deliberately absent — search() stamps it
    only on the union path, so an FTS-only fallback reads None (see the NB in
    _injection_log._retrieval_signal)."""
    return {
        "source_table": "knowledge",
        "id": id_,
        "knowledge_type": "fact",
        "content": "x",
        "rank": rank,
    }


def _cfg(**floors):
    from techne.config import Config

    c = Config()
    for k, v in floors.items():
        setattr(c.hooks, k, v)
    return c


def test_no_floor_configured_changes_nothing(home):
    """The SHIPPED default. #414 says the threshold must come from the replay
    harness (#415), which is unbuilt — so this ships the mechanism with the value
    unset, and behaviour is identical until someone sets it from evidence."""
    from techne.hooks.user_prompt_submit import _apply_relevance_floor

    rows = [_vec_row(1, 0.6), _vec_row(2, 1.34)]
    assert _apply_relevance_floor(rows, _cfg()) == rows


def test_a_vec_hit_beyond_the_floor_is_dropped(home):
    """Measured anchor: 6,457 live delivered hits span distance 0.546-1.351,
    mean 0.99, worst decile 1.168-1.351. A floor of 1.168 rejects about that
    worst decile."""
    from techne.hooks.user_prompt_submit import _apply_relevance_floor

    rows = [_vec_row(1, 0.75), _vec_row(2, 1.30)]
    kept = _apply_relevance_floor(rows, _cfg(per_turn_recall_floor_vec_distance=1.168))

    assert [r["id"] for r in kept] == [1], "the far hit must not clear a 1.168 floor"


def test_the_floor_can_empty_the_block_entirely(home):
    """THE WHOLE POINT (#414): across all 1,430 live per_turn_recall rows the
    zero-hit rate was 0% — the rail never declined to inject. It must be able to."""
    from techne.hooks.user_prompt_submit import _apply_relevance_floor

    rows = [_vec_row(1, 1.30), _vec_row(2, 1.35)]
    assert _apply_relevance_floor(rows, _cfg(per_turn_recall_floor_vec_distance=1.0)) == []


def test_the_two_scales_are_thresholded_separately(home):
    """BM25 rank (more-negative = better) and vec distance (lower = nearer) are
    OPPOSITE-SIGNED. #402's review rejected merging them into one comparable
    column; a floor that shared a threshold would silently mis-judge one channel."""
    from techne.hooks.user_prompt_submit import _apply_relevance_floor

    rows = [_vec_row(1, 0.7), _fts_row(2, -8.0), _fts_row(3, -0.5)]
    kept = _apply_relevance_floor(
        rows, _cfg(per_turn_recall_floor_vec_distance=1.0, per_turn_recall_floor_bm25=-1.0)
    )

    ids = [r["id"] for r in kept]
    assert 1 in ids, "a near vec hit is unaffected by the bm25 floor"
    assert 2 in ids, "rank -8.0 is a STRONG bm25 hit and clears a -1.0 floor"
    assert 3 not in ids, "rank -0.5 is weak and must not clear it"


def test_a_hit_with_no_readable_signal_is_kept(home):
    """A floor rejects what it can JUDGE. Dropping the unmeasurable would make the
    floor look effective by discarding its own blind spot — and session-start and
    edit-rail rows legitimately carry no retrieval score at all."""
    from techne.hooks.user_prompt_submit import _apply_relevance_floor

    rows = [{"source_table": "knowledge", "id": 9, "content": "x"}]
    kept = _apply_relevance_floor(rows, _cfg(per_turn_recall_floor_vec_distance=0.1))
    assert [r["id"] for r in kept] == [9]


# ---------------------------------------------------------------------------
# techne#562 — the session-start context waits for a prompt that may never come.
# ---------------------------------------------------------------------------


def _stash(db, session_id: str, summary: str):
    from techne.core.database import connect
    from techne.core.extension_state import set_state
    from techne.hooks.session_start import DEFERRED_CONTEXT_KEY

    conn = connect(db)
    try:
        set_state(conn, f"{DEFERRED_CONTEXT_KEY}:{session_id}", {"summary": summary})
    finally:
        conn.close()


def test_the_first_prompt_claims_the_deferred_context(home):
    """The saving comes from PLACEMENT, not detection. Measured: 132,177 of
    177,302 session_start tokens (74.5%) went to sessions producing nothing, and
    94% of those never submit a prompt. Arriving here means this session asked
    something, so it earns the context."""
    from techne.core.database import init_db
    from techne.core.state import ensure_session
    from techne.hooks.user_prompt_submit import _claim_deferred_session_context

    db = home / "t.db"
    init_db(db).close()
    ensure_session(db, "s-562")
    _stash(db, "s-562", "## techne\n> session state")

    assert _claim_deferred_session_context(db, "s-562") == "## techne\n> session state"


def test_it_is_claimed_once_not_read_repeatedly(home):
    """CLAIMED, not read. A second prompt must not re-inject it — that would trade
    the old waste for a worse one, which is the exact defect #576 fixed on the
    per-turn rail (46% of deliveries were re-sends)."""
    from techne.core.database import init_db
    from techne.core.state import ensure_session
    from techne.hooks.user_prompt_submit import _claim_deferred_session_context

    db = home / "t.db"
    init_db(db).close()
    ensure_session(db, "s-562b")
    _stash(db, "s-562b", "once only")

    assert _claim_deferred_session_context(db, "s-562b") == "once only"
    assert _claim_deferred_session_context(db, "s-562b") is None, "re-injected on turn 2"


def test_a_session_that_never_prompts_costs_nothing(home):
    """THE 94%. The stash is inert until claimed — no injection, no tokens. This
    asserts the absence, because the whole saving IS the absence."""
    from techne.core.database import connect, init_db
    from techne.core.state import ensure_session
    from techne.hooks._injection_log import _DDL

    db = home / "t.db"
    init_db(db).close()
    ensure_session(db, "s-562c")
    _stash(db, "s-562c", "never claimed")

    conn = connect(db)
    try:
        conn.executescript(_DDL)
        n = conn.execute(
            "SELECT COUNT(*) FROM context_injections WHERE session_id = ?", ("s-562c",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0, "a session that never prompted was still charged for an injection"


def test_no_stash_is_not_an_error(home):
    """Deferral off, or a session that predates it: the claim is simply empty."""
    from techne.core.database import init_db
    from techne.hooks.user_prompt_submit import _claim_deferred_session_context

    db = home / "t.db"
    init_db(db).close()
    assert _claim_deferred_session_context(db, "no-such-session") is None
    assert _claim_deferred_session_context(db, None) is None


def test_the_rail_drops_the_reserved_lexical_row(home, monkeypatch, capsys):
    """THE RAIL DOES NOT DELIVER techne#827's RESERVED SLOT (#829 r4 Important 3).

    THIS IS THE PIN THAT ACTUALLY DRIVES THE RAIL. `test_search_hybrid_reserves_
    the_tail_slot_outside_the_first_k` in tests/test_search_rrf.py asserts the
    same shape from `search()`'s own output, which CANNOT observe this: Prudence
    rewrote user_prompt_submit.py:656 to preserve the lexical row — #831 fixed
    the rail-side way — and every test in both files stayed green. A pin named
    for a rail behaviour has to execute the rail.

    THE MECHANISM. `per_turn_recall_limit` is 5, but the rail asks `search()` for
    `k * _RESEND_OVERFETCH` = 15 (over-fetching so session-dedup cannot thin
    delivery below k, techne#576) and then cuts to k itself at :656. The #827
    floor reserves the page TAIL, so the lexical row is index 14 of 15 and the
    rail's own `[:k]` drops it before the session sees anything.

    INVERT, DO NOT DELETE, WHEN #831 LANDS. Either fix — placing the reserved row
    inside the first k, or teaching the rail to keep it — should make this fail.
    That failure is the fix arriving: assert the lexical row IS in the block.
    """
    _write_config(home, per_turn_recall=True)

    from techne.config import Config

    k = Config.load().hooks.per_turn_recall_limit
    page = k * user_prompt_submit._RESEND_OVERFETCH

    # Real rows, so _drop_pull_only's type lookup and _build_context's rendering
    # run against the database rather than against a fixture's imagination.
    # A FULL vec channel — `range(page)`, not `page - 1` (#829 r5 Important 1).
    # One row short and the reserved slot lands at the tail by SLACK rather than
    # by reservation, which makes the index assertion below witness nothing: with
    # `_fts_floor` mutated to 0 the short version still passes.
    vec_ids = [_seed(f"vector channel row {i}", type="fact") for i in range(page)]
    lex_id = _seed("lexical only row, reachable by identifier XYZZY-827", type="fact")

    def _rows(ids):
        import sqlite3

        conn = sqlite3.connect(str(Config.load().db_path))
        try:
            q = ",".join("?" * len(ids))
            cur = conn.execute(
                f"SELECT id, type, content FROM knowledge WHERE id IN ({q})", ids
            )
            by_id = {r[0]: r for r in cur.fetchall()}
        finally:
            conn.close()
        return [
            {
                "source_table": "knowledge",
                "id": i,
                "knowledge_type": by_id[i][1],
                "content": by_id[i][2],
            }
            for i in ids
        ]

    fake_vec = [dict(r, distance=0.1 * n) for n, r in enumerate(_rows(vec_ids))]
    fake_fts = [dict(r, rank=-1.0) for r in _rows([lex_id] + vec_ids)]
    monkeypatch.setattr("techne.core.search.search_fts", lambda *a, **kw: fake_fts)
    monkeypatch.setattr("techne.core.search.search_vec", lambda *a, **kw: fake_vec)

    out = _run(monkeypatch, capsys, {"prompt": "XYZZY-827", "session_id": "rail-1"})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]

    # Full delimiters: `techne#1` is a prefix of `techne#15` (#829 r5 nit 4).
    assert f"⟦techne#{vec_ids[0]}⟧" in ctx, "the rail delivered nothing — fixture is wrong"
    assert f"⟦techne#{lex_id}⟧" not in ctx, (
        "the reserved lexical row now reaches the session — if that is deliberate, "
        "techne#831 has been fixed and this pin should be INVERTED, not deleted"
    )
    # And say WHY it is absent, so a future failure is diagnosable: search() put
    # it on the page, at the tail, outside the k rows the rail keeps.
    from techne.core.search import search

    full = search(Config.load().db_path, "XYZZY-827", table="knowledge", limit=page)
    assert [i for i, r in enumerate(full) if r["id"] == lex_id] == [page - 1]
