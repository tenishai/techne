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
import re

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


def test_the_two_declared_defaults_cannot_diverge():
    """`_build_context`'s signature default must equal the config default.

    The cap is declared twice — `HookConfig.per_turn_recall_chars` and
    `_build_context`'s `char_limit` parameter. `main()` always passes the cap
    explicitly, so the signature default governs no production path and reads as
    harmless; that is exactly what let the two drift when the config default moved
    200 -> 450 on 2026-08-31, silently capping every test that calls the renderer
    without an explicit cap at the stale value. A vestige that can disagree with
    the real setting is worse than one that cannot.
    """
    import inspect

    from techne.config import HookConfig
    from techne.hooks.user_prompt_submit import _build_context

    sig_default = inspect.signature(_build_context).parameters["char_limit"].default
    assert sig_default == HookConfig().per_turn_recall_chars, (
        f"_build_context defaults char_limit={sig_default} but the shipped config "
        f"default is {HookConfig().per_turn_recall_chars}; change both or neither"
    )


def test_the_shipped_default_is_actually_deliverable():
    """The default cap must be a value the rail can honour (techne#826/#842).

    `search()` pre-truncates knowledge rows to `_SEARCH_ROW_CEILING` before the
    hook ever sees them, so a default at or above it cannot be delivered — and
    worse, an unclamped cap there makes a severed row look whole and suppresses
    the `…` marker. This pins the invariant, not the number: raise the default
    freely, but never to a value the ceiling forbids.

    WHY 450 AND NOT 200: measured 2026-08-31 over 2,468 live injectable rows,
    median length 476 chars. A 200-cap WOULD sever 2,442 of them (98%) where 450
    severs 1,305 (52.88%) — properties of the STORE, not counts of rows the rail
    cut; it injects five per turn by similarity, so it never severed 2,442 of
    anything (#852 r1). The old default would have cut a typical entry in half.
    """
    from techne.config import HookConfig
    from techne.hooks.user_prompt_submit import _SEARCH_ROW_CEILING

    default = HookConfig().per_turn_recall_chars
    assert 0 < default < _SEARCH_ROW_CEILING, (
        f"shipped default {default} is not deliverable: search() truncates at "
        f"{_SEARCH_ROW_CEILING}, so the cap must sit strictly below it"
    )


def test_document_card_is_not_pushed(home, monkeypatch, capsys):
    """Arch cards are pull-only on THIS rail too (techne#561, decided 2026-08-13).

    This replaces a test that pinned the opposite behaviour while the question was
    open. What decided it: `_build_context` truncates every row to
    `hooks.per_turn_recall_chars` (450 by default), and an arch card's value is
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
        """The CAP, and it was pinned by nothing — removing
        the slice entirely left the suite green (#566 r2) while a recorded
        `document_card` decision rests on it. An unbounded line here is a per-turn
        injection whose size is set by whatever the longest matching row happens to
        be.

        This row is 500 unbroken `x`, so there is no boundary for techne#826's
        boundary-seeking to find and the cut is hard at the cap — which is what
        keeps this assertion a test of the CAP rather than of the cut point. The
        boundary behaviour is pinned separately below."""
        # AGAINST THE PRODUCTION DEFAULT, NOT THE RENDERER'S SIGNATURE (#839 r4
        # Important 3). Deriving `cap` from `_build_context`'s parameter made this a
        # TAUTOLOGY — changing that default to 400 left the test green, where the old
        # hardcoded 200 would have failed. Worse, `main()` always passes the cap
        # explicitly, so the signature default governs no production path at all: the
        # derived version pinned a vestige while the CHANGELOG's bolded promise that
        # "the 200-character default is unchanged" was pinned by nothing anywhere.
        from techne.config import HookConfig

        cap = HookConfig().per_turn_recall_chars
        assert cap == 450, (
            "the shipped default moved; #808 measures per-session context as already "
            "the dominant cost, so this is a deliberate decision, not a tuning tweak"
        )
        # MOVED 200 -> 450 on 2026-08-31, and this tripwire is why it was a decision
        # rather than a drift: it failed, and the number changed only after measuring.
        #
        # THE MEASUREMENT IS OF THE STORE, NOT THE RAIL (#852 r1 Important 3). Of 2,468
        # live injectable rows (median 476), a 200-cap WOULD sever 2,442 (98%) and 450
        # WOULD sever 1,305 (52%) — if each were injected. The rail injects five rows a
        # turn chosen by embedding similarity and BM25, so it never severed 2,442 of
        # anything. #826's field figure (58/58 at 200) matches the corpus number, but at
        # 200 nearly everything severs however it was chosen; the agreement carries no
        # information about 450, where 47% of the store sits below the cap and selection
        # bias finally has room. docs/roadmap.md:87 records that BM25 length-norm ranks
        # longer rows BELOW shorter ones, which would push true severance under 52% —
        # unmeasured either way, so 52% is a bound, not a prediction. #853 adds the
        # telemetry that settles it.
        #
        # Cost: ~312 tokens/turn TYPICAL on a five-row push, ~412 TYPICAL-WORST. 312 is
        # the full-delta figure (5 × 250 / 4). It is NOT a ceiling, and calling it one
        # was wrong (#852 r2): boundary-seeking floors at int(limit * 0.6), which
        # applies to the OLD cap too — int(200*0.6)=120 against int(450*0.6)=270 — so
        # it shrinks the old emission as well and WIDENS the worst-case delta to
        # 450-120=330 chars/row, i.e. 5 × 330 / 4 = 412 tokens. Counting the floor
        # against the new cap only was the same overstatement r1 raised, reintroduced
        # by the word chosen to fix it.
        # AND 412 IS STILL NOT A BOUND (#852 r3 nit): the floor floors the CUT INDEX
        # and `.rstrip()` runs after it, so a whitespace run ending at 120 emits 116
        # against a new 452 — a 336-char delta, 420 tokens. Constructed against the
        # real function, not argued. Read 412 as TYPICAL-WORST; rstrip can push a
        # little past it. Third numeric claim in this PR and the third to need
        # narrowing, which is the argument for stating deltas rather than bounds.
        # PER-TURN against #808's PER-SESSION block, so
        # comparable only multiplied out: 312 × 40 ≈ 12,500 over a 40-turn session.
        #
        # 450 not 500 because search() pre-truncates at _SEARCH_ROW_CEILING (#842) and
        # the cap must sit strictly below it — pinned by
        # test_the_shipped_default_is_actually_deliverable.
        out = self._ctx(
            [{"id": 1, "source_table": "knowledge", "knowledge_type": "fact", "content": "x" * 500}]
        )
        assert "x" * cap in out
        assert "x" * (cap + 1) not in out

    def test_a_cut_row_never_ends_mid_word(self):
        """techne#826, the measured defect: over one real session 58 of 58 injected
        facts ended mid-word — a cross-reference severed at `CORRECTS techne#`, a
        remedy command severed mid-path. The agent receives something that looks
        like the rule, is topically correct, and is missing the operative half.
        """
        body = (
            "X THE VERSION STRING CANNOT IDENTIFY THE RUNNING CODE. Two independent "
            "defects, both measured 2026-08-13. CORRECTS techne#1078 and supersedes "
            "the guidance recorded there, which nobody has re-read since. The rest "
            "of this row exists to carry it past the cap so the cut actually fires."
            " It also has to reach past the raised cap, so this sentence carries it "
            "beyond four hundred and fifty characters without a boundary landing exactly "
            "at the cut point itself, which is the property the fixture exists to hold."
        )
        # THE FIXTURE'S DISCRIMINATING PROPERTY, ASSERTED RATHER THAN ASSUMED.
        # An earlier version of this test could not fail: its character at the cap
        # was a SPACE, so the bare `content[:cap]` defect satisfied both assertions
        # below and the whole boundary mechanism could be deleted with the suite
        # staying green (#839 r1). The row is shifted one character so the character
        # AT the cap is non-whitespace — mid-word, in fact, which is precisely the
        # condition the bare slice cannot survive. (#839 r2 nit 1: an earlier comment
        # here claimed it was a full stop. It is not; the full stop is at index 201,
        # one later — an earlier correction said "two", which was itself off by one. The
        # guard was right and only its explanation was wrong — worth correcting
        # because this is the comment that says why the fixture can be trusted.)
        #
        # LENGTHENED 2026-08-31 when the cap moved 200 -> 450: the 282-char fixture no
        # longer exceeded the cap, and this test says so out loud rather than passing
        # on a row it never cuts — the `len(body) > cap` assertion below is what caught
        # it. The property is re-established at the NEW cap, verified empirically, not
        # counted — AND THE FIRST ATTEMPT AT THAT WAS WRONG, which is the part worth
        # keeping. It padded to `body[450] == 'w'` and called that mid-word. It is not:
        # `body[449]` was a SPACE, so 'w' opened "which", the hard cut's `rstrip()`
        # removed that space, and the assertion read the stripped position instead —
        # this guard passed with boundary-seeking deleted outright. Caught in #852 r1
        # by executing the defect rather than reasoning about it. The cap index was
        # checked and the one before it was not, which is the whole error: "verified
        # empirically" is only true of the index you actually looked at.
        # Now `body[449]='e'` and `body[450]='l'`, both interior to "itself".
        # The index-201 note above refers to the old cap and is kept as the record of
        # why the original fixture held.
        #
        # THE CAP IS DERIVED, NOT WRITTEN AS 200 (#839 r2 nit 2). `_ctx` calls
        # `_build_context` without a limit, so this fixture is coupled to that
        # DEFAULT; a hardcoded 200 would check the wrong index if the default ever
        # moved, and go quietly toothless in exactly the way this guard exists to
        # prevent.
        import inspect

        from techne.hooks.user_prompt_submit import _build_context

        cap = inspect.signature(_build_context).parameters["char_limit"].default
        assert len(body) > cap, "fixture must exceed the cap or it tests nothing"
        # NECESSARY AND SUFFICIENT, WHICH THE cap-ONLY CHECK WAS NOT (#852 r1).
        # The hard-cut path ends `window[:limit].rstrip()`, so if `body[cap - 1]` is
        # whitespace the bare-slice defect produces a string one shorter and the
        # mid-word assertion below reads the stripped space instead — passing with
        # boundary-seeking deleted. Checking only `body[cap]` cannot see that.
        # SUBSUMES the cap-only check this replaced (#852 r2 nit 2): `A and B`
        # implies `B`, so that assertion could never fire once this one holds.
        # r2 asked for the fold and r2's fix SHIPPED THE NOTE WITHOUT THE FOLD —
        # the deleted assertion sat three lines below this comment for a whole
        # round, which is a claim and its falsifier in one file, six lines apart:
        # the exact shape r2's own Important 1 was retiring, recurring inside the
        # commit that retired it (#852 r3). Actually gone now.
        assert body[cap - 1] not in " \t\n" and body[cap] not in " \t\n", (
            f"fixture is toothless: the hard cut rstrips to index "
            f"{len(body[:cap].rstrip())}, and the char there must be non-whitespace"
        )
        out = self._ctx([{"id": 1, "knowledge_type": "fact", "content": body}])
        rendered = out.splitlines()[-1]
        # Anchored to the ROW, not the block: `" …" in out` is satisfied by the
        # header's own "Rows ending in …" text, so a block-level check never
        # inspects the row at all (#839 r1).
        assert rendered.endswith(" …")

        # "the cut is not inside a word", NOT "the cut is a full stop" — whether a
        # sentence end is available depends on where it falls relative to the 40%
        # floor, and demanding `.!?` failed against working code in an earlier draft.
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
        # ROW-anchored: `" …" in long_out` also matches the header's own
        # "Rows ending in …" text, so it passed without ever inspecting the row.
        assert long_out.splitlines()[-1].endswith(" …")
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

    def test_truncate_is_pinned_directly_not_only_through_the_renderer(self):
        """`_truncate_for_injection` is pure, and reaching it only through
        `_build_context` is what let the header's own text satisfy a row-level
        claim (#839 r1). These assert the function itself.
        """
        from techne.hooks.user_prompt_submit import _truncate_for_injection as trunc

        # under the cap: untouched, not marked
        assert trunc("short", 200) == ("short", False)
        # exactly at the cap: still untouched — the cut is len > limit, not >=
        assert trunc("x" * 200, 200) == ("x" * 200, False)
        # over the cap: marked, and the emitted CONTENT never exceeds the budget
        out, cut = trunc("y" * 500, 200)
        assert cut and out.endswith(" …")
        assert len(out.removesuffix(" …")) <= 200

        # A TERMINATOR STRADDLING THE WINDOW EDGE is still found (#839 r1 nit 4):
        # full stop at 199, its space at 200, invisible to a search bounded at 200.
        #
        # THE PREFIX MUST CARRY WORD BOUNDARIES OR THIS TEST CANNOT FAIL (#839 r2
        # Important 2). An earlier fixture was `"z" * 199 + ". "`: with the peek
        # reverted the sentence branch misses, the word branch finds no space, and
        # the HARD-CUT fallback returns the same 199 characters ending in the same
        # full stop — so the assertion held on both sides of the change and the
        # whole suite stayed byte-identical with the fix removed. Spacing the prefix
        # gives the fallback somewhere to retreat to (the space at 194), so the two
        # paths now produce different text.
        straddle = ("zzzz " * 39) + "z" * 4 + ". " + "w" * 300
        assert straddle[199] == "." and straddle[200] == " ", "fixture must straddle"
        out, _ = trunc(straddle, 200)
        assert out.endswith(". …"), f"straddling terminator missed: {out[-12:]!r}"
        assert len(out.removesuffix(" …")) == 200, "the peeked char must not be emitted"

    def test_a_non_positive_cap_never_means_unbounded(self):
        """#839 r1. The first version returned the row untouched when `limit <= 0`,
        so `per_turn_recall_chars: 0` produced UNLIMITED per-row injection — while
        the sibling knob `per_turn_recall_limit: 0` means "inject nothing". An
        operator switching the cap off would have got the expensive opposite.
        """
        from techne.hooks.user_prompt_submit import _truncate_for_injection as trunc

        for limit in (0, -1, -500):
            out, cut = trunc("a" * 5000, limit)
            assert out == "", f"limit={limit} emitted {len(out)} chars"
            assert cut is True, "withheld content must still be marked as cut"

    def test_a_zero_cap_empties_the_rendered_block(self):
        """#839 r2 Important 3. Withholding content is not enough — rendering the
        withheld rows produced a block whose header promised "Rows ending in …"
        above rows that ended in nothing, contradicting itself, and cost a header
        plus one line per row of pure overhead on every prompt.

        `per_turn_recall_limit: 0` feeds `rows[:0]` and the caller's
        `if rows else ""` suppresses the block entirely. `0` must mean the same
        thing on BOTH knobs, or the CHANGELOG's claim that it does is false.
        """
        from techne.hooks.user_prompt_submit import _build_context

        rows = [
            {"id": 1, "knowledge_type": "decision", "content": "x" * 400},
            {"id": 2, "knowledge_type": "fact", "content": "y" * 400},
        ]
        assert _build_context(rows, 0) == ""
        assert _build_context(rows, -5) == ""
        # control: the same rows DO render under a normal cap
        assert _build_context(rows, 200) != ""

    def test_the_cap_parameter_is_honoured(self):
        """The RENDERER half only. Renamed, because under its old name — "is read
        from config, not hardcoded" — it asserted nothing of the kind: it passes the
        cap as a positional argument, so the assertion is satisfied by the function
        PARAMETER and the config path it claimed to pin was never exercised. Audit
        instance 5: reverting the call site to `_build_context(rows)` so the knob
        never reaches the rail left all 3889 tests green. The config path is pinned
        end-to-end by `test_the_configured_cap_reaches_the_rail` below.
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
            cur = conn.execute(f"SELECT id, type, content FROM knowledge WHERE id IN ({q})", ids)
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


def test_the_configured_cap_reaches_the_rail(home, monkeypatch, capsys):
    """END-TO-END: config.yaml -> Config.load() -> the hook -> the injected row.

    AUDIT INSTANCE 5. `per_turn_recall_chars` round-tripped through YAML (pinned by
    test_fastconfig) and was honoured as a parameter (pinned above), and NOTHING
    connected the two: reverting the call site to `_build_context(rows)` — so the
    knob is read, saved, and then ignored by the rail it exists to tune — left all
    3889 tests green. Two pinned halves with an unpinned join is the same shape as
    the write/comparison split on #840.

    A generous cap must therefore produce a LONGER injected row than the default,
    driven through `main()` rather than by calling the renderer directly.

    WHAT THE 600 ARM ACTUALLY DEMONSTRATES, corrected (#839 r3 Important 1): NOT a
    cut at 600. `search()` pre-truncates knowledge rows to 500 and the hook then
    clamps to 499, so the row is cut at 499 and marked — not at 600. (An earlier
    version of this sentence said no truncation was applied at all; that predates
    the r4 clamp, and the very next test asserts the opposite at the same cap.) The
    test still pins the join — the config value has to reach the renderer for the
    two arms to differ — but "600 works" is not what it shows, and the FTS half of the ceiling is
    pinned separately by `test_search_itself_truncates_knowledge_rows`, and the VEC
    half only by `test_both_search_sites_share_one_ceiling`.
    """
    long_fact = " ".join(f"word{i:03d}" for i in range(200))

    _write_config(home, per_turn_recall=True, per_turn_recall_chars=600)
    _seed(long_fact, type="fact")
    wide = _run(monkeypatch, capsys, {"prompt": "word001 word002 recall"})

    _write_config(home, per_turn_recall=True, per_turn_recall_chars=200)
    narrow = _run(monkeypatch, capsys, {"prompt": "word001 word002 recall"})

    assert wide.strip() and narrow.strip(), "both runs must inject something"
    assert len(wide) > len(narrow), (
        f"the configured cap never reached the rail: wide={len(wide)} narrow={len(narrow)}"
    )


def test_a_cap_at_or_above_searchs_ceiling_still_marks_the_cut(home, monkeypatch, capsys):
    """techne#842 / #839 r4 Important 1 — THE INERTNESS IS TOLERATED, THE SILENCE IS NOT.

    `search()` hands knowledge rows in already cut at 500, unmarked, at BOTH sites
    that build one — `_fts_match_rows` (FTS) and `search_vec` (VEC, which the
    vec-first union puts at the head of the page, so it serves most rows on any
    non-trivial store).

    REWRITTEN BY techne#842, WHICH FIXED BOTH HALVES rather than tolerating one.
    `search()` now takes `row_chars` and this rail passes `per_turn_recall_chars + 1`,
    so a cap above 500 delivers what it asks for AND the cut is still announced.

    THE +1 IS WHAT THIS TEST NOW PINS. `_truncate_for_injection` decides "was this
    cut?" by `len(content) > limit`, so a row `search()` sliced to EXACTLY the cap
    reads as whole and the marker is suppressed. Threading the cap unchanged would
    have spread that dishonest state from cap>=500 to EVERY cap — measured at 600,
    500 and 200, all ending mid-word with no marker. Over-fetching by one gives the
    renderer the overflow it needs to tell the two cases apart.

    The old clamp to `_SEARCH_ROW_CEILING - 1` is gone with the defect that motivated
    it, so a 600-char cap now yields a 600-char row rather than 499.
    """
    from techne.hooks.user_prompt_submit import _SEARCH_ROW_CEILING

    long_fact = " ".join(f"word{i:03d}" for i in range(200))
    assert len(long_fact) > 900, "fixture must exceed the ceiling to test it"

    _write_config(home, per_turn_recall=True, per_turn_recall_chars=600)
    _seed(long_fact, type="fact")
    out = _run(monkeypatch, capsys, {"prompt": "word001 word002 recall"})

    block = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    row = [ln for ln in block.splitlines() if ln.startswith("- ⟦techne#")]
    assert row, f"expected an injected row: {block!r}"
    body = row[0].split("] ", 1)[1]

    # BOUNDED BY THE CLAMP, NOT BY search() — AND THAT IS THE POINT OF THIS ASSERTION
    # NOW (#839 r5 Important 1). The hook caps at `_SEARCH_ROW_CEILING - 1` before
    # `_truncate_for_injection` ever sees the row, so this length is identical whether
    # search() truncates or not: deleting `_fts_match_rows`'s own knowledge slice left the whole
    # suite green. The r4 "both bounds" fix landed on an assertion that the r4 clamp
    # made inert IN THE SAME COMMIT. search()'s ceiling is witnessed directly by
    # `test_search_itself_truncates_knowledge_rows` below; what THIS pins is that the
    # clamp is applied and the marker survives.
    # BOUNDED BY THE CAP THE OPERATOR SET, not by a hidden ceiling (techne#842).
    # The configured cap is 600 and the row must reach it; before this fix the
    # clamp held it at 499 no matter what the knob said, which is the inertness
    # the issue is named for. `_SEARCH_ROW_CEILING` survives as the DEFAULT a
    # caller gets when it asks for nothing, so it is still imported and still
    # meaningful — it is just no longer a ceiling on this rail.
    assert len(body) > _SEARCH_ROW_CEILING, (
        f"row is {len(body)} chars — a 600-char cap must now beat the old 499 clamp"
    )
    assert len(body) <= 600 + len(" …"), (
        f"row is {len(body)} chars — must not exceed the cap it was given"
    )
    assert body.endswith(" …"), (
        "a row at the ceiling shipped UNMARKED — the clamp is gone and the rail is "
        "back to reading a severed row as whole"
    )
    assert "per_turn_recall_chars" in block, "the header notice must fire too"

    # AND THE "AT" HALF OF THE NAME, which only "above" was covering (#839 r6
    # Important 2). Exactly 500 is the one value where a plausible clamp goes wrong:
    # `if chars > _SEARCH_ROW_CEILING: chars = _SEARCH_ROW_CEILING - 1` passes 500
    # straight through, `_truncate_for_injection` returns `(content, False)` for a row
    # of exactly 500, and both the marker and the header notice vanish — the honesty
    # regression this PR exists to close, live at the boundary the name claims.
    _write_config(home, per_turn_recall=True, per_turn_recall_chars=_SEARCH_ROW_CEILING)
    at_out = _run(monkeypatch, capsys, {"prompt": "word001 word002 recall"})
    at_block = json.loads(at_out)["hookSpecificOutput"]["additionalContext"]
    at_row = [ln for ln in at_block.splitlines() if ln.startswith("- ⟦techne#")]
    assert at_row, f"expected an injected row at cap {_SEARCH_ROW_CEILING}: {at_block!r}"
    assert at_row[0].split("] ", 1)[1].endswith(" …"), (
        f"a cap of exactly {_SEARCH_ROW_CEILING} shipped an unmarked severed row"
    )
    assert "per_turn_recall_chars" in at_block


def test_both_search_sites_share_one_ceiling():
    """#839 r4 Important 2 — THE PIN WAS SINGLE-SITED AND THE DOCS INHERITED IT.

    `search()` fuses two channels and truncates knowledge in BOTH: `search_fts` at
    `_fts_match_rows` and `search_vec`. The end-to-end test above
    exercises whichever channel the fixture happens to hit; this one asserts the two
    sites agree, so raising one without the other cannot pass unnoticed — which is
    the failure mode that leaves `config.py`'s comment and the README quietly wrong.

    Source-level on purpose — but NOT because the deps are missing: `sqlite_vec`
    imports and loads here (vec_version reports v0.1.9) and `model2vec` imports. The
    reason is that driving the vec channel end-to-end needs a populated vector index
    and a cached model, which makes the test slow, environment-dependent, and prone
    to being skipped — and a skipped guard on the busier channel witnesses nothing.
    """
    import re
    from pathlib import Path as _P

    from techne.hooks.user_prompt_submit import _SEARCH_ROW_CEILING

    src = _P("techne/core/search.py").read_text()

    # Anchored on the knowledge row rather than on the truncation shape: matching
    # the shape alone caught the OBSERVATIONS row too (#839 r5).
    # The first version matched `[:NNN]` on any content field and caught THREE sites —
    # including the OBSERVATIONS row, which has nothing to do with this
    # knob. That was wrong in both directions, both demonstrated: deleting the knowledge
    # ceiling still left two matching sites so the test stayed GREEN (false negative on
    # the thing it exists to catch), and editing the observations row alone turned
    # it RED with a message
    # about "knowledge rows" that was not true (false positive, and the kind that gets a
    # test deleted as noise). Each knowledge block is found by its own source_table
    # marker, and the count is EXACT so future slack cannot hide a lost site.
    # TEMPERED, so a match cannot cross a `source_table` boundary. An unbounded
    # `.*?` under re.S paired the knowledge marker with the THREAD block's
    # truncation, so deleting site 416's slice still yielded two 500s and a pass.
    # This pin is the SOLE witness for the VEC site (`search_vec`) — the direct witness below
    # reaches only FTS — so measuring the wrong block is the one thing it must not do.
    blocks = re.findall(
        r'"source_table":\s*"knowledge",(?:(?!"source_table").)*?\[:([A-Za-z_0-9]+)\]',
        src,
        flags=re.S,
    )
    assert len(blocks) == 2, (
        f"expected exactly two knowledge-row truncation sites, found {blocks} — "
        "search.py's shape changed; re-derive this pin rather than widening it"
    )
    # STRONGER THAN "BOTH ARE 500" (techne#842). Both sites must slice by `_cap`,
    # the caller-aware value, rather than by any literal. A literal at either site
    # is the defect this issue fixed: it silently overrode whatever the caller
    # asked for, so `per_turn_recall_chars` governed nothing above 500 and a row
    # arriving at exactly the cap read as whole. Pinning the NAME rather than the
    # NUMBER also survives a change to the default, which a numeric pin would not.
    assert set(blocks) == {"_cap"}, (
        f"knowledge rows are sliced by {set(blocks)} — both sites must use `_cap` so "
        "the caller's `row_chars` is honoured; a literal here reintroduces techne#842"
    )
    # And the default the two fall back to is still the one the hook documents.
    from techne.core.search import _ROW_CHAR_CAP

    assert _ROW_CHAR_CAP == _SEARCH_ROW_CEILING, (
        f"search default {_ROW_CHAR_CAP} and the hook's documented ceiling "
        f"{_SEARCH_ROW_CEILING} have diverged — the comment at the knob is now wrong"
    )


def test_the_ledger_rows_are_pinned_to_the_default_at_both_sites():
    """The other half of techne#842's contract: `row_chars` moves knowledge ONLY.

    Observations and threads are the tool ledger — mostly shell echoes — so raising
    the recall cap must not drag them along, or the payload the operator paid for
    grows by transcript instead of by the curated row they asked for. There is a
    behavioural witness for the FTS ledger site, but the VEC ledger site has none:
    driving that channel needs a populated vector index and a cached model. So this
    pin is the sole guard there, exactly as the knowledge pin above is for `search_vec`.

    Written as its own test rather than folded into that one because its failure
    means the opposite thing — a ledger site drifted ONTO the caller's knob — and a
    message about "knowledge rows" would send the next reader the wrong way.
    """
    import re
    from pathlib import Path as _P

    src = _P("techne/core/search.py").read_text()

    # Same tempered shape as the knowledge pin: `(?!"source_table")` keeps a match
    # from crossing into the next block and pairing a ledger marker with the
    # knowledge slice, which would let a real drift pass green.
    blocks = re.findall(
        r'"source_table":\s*"(observation|thread)",(?:(?!"source_table").)*?\[:([A-Za-z_0-9]+)\]',
        src,
        flags=re.S,
    )
    assert len(blocks) == 4, (
        f"expected four ledger truncation sites (observation+thread across both "
        f"channels), found {blocks} — search.py's shape changed; re-derive this pin"
    )
    sliced_by = {name for _table, name in blocks}
    assert sliced_by == {"_ROW_CHAR_CAP"}, (
        f"ledger rows are sliced by {sliced_by} — every one must stay on the fixed "
        "default; a `_cap` here means raising per_turn_recall_chars now inflates the "
        "observation ledger too, which techne#842 explicitly did not do"
    )


def test_a_withheld_row_is_not_marked_sent(home, monkeypatch, capsys):
    """techne#839 r3 Important 2 — LOGGING A ROW AS DELIVERED SUPPRESSES IT.

    `log_context_injection`'s own contract is "`rows` is what was INJECTED", and the
    hook passed `rows` whether or not anything was emitted. That is not merely an
    inaccurate audit row: `_already_sent_this_session` reads `hit_ids` back with NO
    filter on whether the block was empty, so a row logged-but-never-sent is
    suppressed for the rest of the session segment.

    Two turns, one session, cap raised in between — which AGENTS.md lists as a
    modelled operator behaviour ("edits ~/.techne/config.yaml while a techne MCP
    server is live"), not a contrived one.
    """
    _write_config(home, per_turn_recall=True, per_turn_recall_chars=0)
    _seed("the fact that must survive a withheld turn", type="fact")

    first = _run(monkeypatch, capsys, {"session_id": "sup-1", "prompt": "withheld turn"})
    assert first.strip() == "", f"cap 0 must inject nothing: {first!r}"

    # THE NAME'S LITERAL CLAIM, not merely its consequence (#839 r4 nit 1). Asserting
    # only that turn two emits would go green under a future change that made
    # `_already_sent_this_session` skip empty-injection rows while still RECORDING
    # them as sent — the audit log would stay wrong and this test would not say so.
    logged = _injection_log_rows("per_turn_recall")
    assert logged and logged[0]["hit_ids"] == "[]", (
        f"a withheld row was recorded as sent: {logged[0]['hit_ids'] if logged else None}"
    )

    _write_config(home, per_turn_recall=True, per_turn_recall_chars=200)
    second = _run(monkeypatch, capsys, {"session_id": "sup-1", "prompt": "withheld turn"})

    assert second.strip(), (
        "the row was suppressed as already-sent despite never having been injected"
    )


def test_search_itself_truncates_knowledge_rows(home):
    """techne#842 / #839 r5 Important 1 — WITNESS THE CEILING WHERE IT LIVES.

    The end-to-end test above cannot see this: the hook clamps to
    `_SEARCH_ROW_CEILING - 1` BEFORE `_truncate_for_injection` runs, so its length
    assertion is satisfied by the clamp whether or not `search()` truncates at all.
    Deleting `search.py`'s own ceiling left the full suite green.

    THE FTS CHANNEL ONLY, which the name does not say on its own. Deleting the VEC
    slice at `search_vec` leaves this GREEN — that site's sole guard is
    `test_both_search_sites_share_one_ceiling`, a regex over source text. The
    distinction matters because the vec-first union puts the VEC channel at the head
    of the page, so the channel this test cannot see is the busier one on any
    non-trivial store.

    This calls `search()` directly, so the only thing that can satisfy it is
    `search()`'s own behaviour on the channel it reaches. Together with the source
    pin it makes the constant in `config.py`, the README line and the CHANGELOG line
    falsifiable — every one of them is keyed to a number that, until this pair
    existed, no test observed at its source at all.
    """
    from techne.config import Config
    from techne.core.search import search
    from techne.hooks.user_prompt_submit import _SEARCH_ROW_CEILING

    long_fact = " ".join(f"word{i:03d}" for i in range(300))
    assert len(long_fact) > _SEARCH_ROW_CEILING * 2, "fixture must exceed the ceiling"
    _seed(long_fact, type="fact")

    rows = search(Config.load().db_path, "word001 word002", table="knowledge", limit=5)
    assert rows, "fixture must be retrievable or this witnesses nothing"
    longest = max(len(r.get("content") or "") for r in rows)
    assert longest == _SEARCH_ROW_CEILING, (
        f"search() returned a {longest}-char knowledge row; the hook and its docs are "
        f"keyed to {_SEARCH_ROW_CEILING} (techne#842) and must move together"
    )


def test_no_carrier_still_documents_the_removed_ceiling():
    """techne#842 — the prose was the part that drifted, and nothing watched it.

    Three files documented the 500 ceiling as current behaviour and none was in
    the fix's diff: `config.py`'s docstring on the knob, the README's config
    block, and CHANGELOG's `[Unreleased]`. The README even cited #842 as the
    REASON for the clamp that #842 removes — a citation that had inverted.

    The two tests above advertise themselves as making those three files
    falsifiable. They do not: they pin the NUMERAL 500, which is still correct
    everywhere (it is still the default). What went stale is the SENTENCE built
    around it, and no test observed a sentence. This one does.

    Phrases, not files, because the claim can move between them. Each is a
    string that was true before #842 and is false after it, so a match means
    someone reintroduced the ceiling narrative or restored a carrier from a
    pre-#842 copy.
    """
    from pathlib import Path as _P

    # PRESENT-TENSE ASSERTIONS ONLY, and that limit is deliberate rather than an
    # oversight. The obvious entry — "clamped to 499" — is NOT here, because the
    # correction in `config.py` names the clamp in order to retract it, and a test
    # that bans the words also bans saying they were wrong. A ban list that forces
    # a correction to be silent about what it corrects is worse than no ban list:
    # absence is not a retraction for a claim that was documented at length. So
    # each phrase below is one that can only be read as a statement about how the
    # code behaves NOW.
    retracted = [
        "so no\n  default above 499 can be honoured",
        "Setting this above 500 therefore changes",
        "still pre-truncates at 500",
        "HARD CEILING OF 500 AND IT IS NOT THIS KNOB'S",
        "this is CLAMPED to 499",
        "the hook CLAMPS to 499",
    ]
    # CHANGELOG's SHIPPED release sections are historical record and must keep
    # describing what shipped — 0.16.1 really did clamp to 499. Only the live
    # carriers are checked, plus CHANGELOG above its first dated release.
    #
    # Found the FIRST DATED release rather than naming one: a hardcoded anchor
    # goes stale at the next release cut, silently widening this slice to cover
    # the block just shipped. It was pinned to `## [0.16.1]` and the 0.17.0 cut
    # made the whole 682-line shipped block read as `[Unreleased]`. Same regex
    # shape as `_RELEASED` in tests/test_changelog.py.
    changelog = _P("CHANGELOG.md").read_text()
    m = re.search(r"^## \[\d+\.\d+\.\d+\]", changelog, flags=re.M)
    assert m, "no dated release block in CHANGELOG.md — the heading format changed"
    first_release = m.start()
    carriers = {
        "techne/config.py": _P("techne/config.py").read_text(),
        "README.md": _P("README.md").read_text(),
        "CHANGELOG.md [Unreleased]": changelog[:first_release],
    }

    offenders = [
        f"{name}: {phrase!r}"
        for name, text in carriers.items()
        for phrase in retracted
        if phrase in text
    ]
    assert not offenders, (
        "a live carrier still documents the ceiling techne#842 removed:\n  "
        + "\n  ".join(offenders)
        + "\nThese read as current behaviour to anyone tuning per_turn_recall_chars."
    )

    # And the fix is positively stated where the knob is defined, so a reader who
    # half-remembers the ceiling sees it retracted rather than merely absent.
    assert "techne#842 REMOVED IT" in carriers["techne/config.py"], (
        "config.py no longer retracts the ceiling explicitly — absence is not a "
        "correction for a claim that was documented at length"
    )


def test_chars_dropped_still_moves_with_the_cap(tmp_path):
    """techne#853's metric, which techne#842 collapsed to a constant.

    #853 exists so that raising or lowering `per_turn_recall_chars` can be
    evaluated from data. It differenced against the row this rail was handed —
    but `search()` truncates first, so that was always the length of an
    already-cut row. Merely misleading before #842; useless after, because the
    rail now asks for `cap + 1` and the difference became the marker's width.
    Measured on this 3,199-char row: 2/4/2/2 at caps 200/450/1200/2000, where
    the true losses are 3000/2752/2000/1200.

    A constant is worse than a wrong number. It answers every cap identically,
    which is the one thing the metric was bought to avoid — so this asserts
    MOVEMENT across caps as well as exactness, and the movement assertion is
    the one that fails under a regression to any fixed value.
    """
    from techne.core.database import init_db
    from techne.core.knowledge import store as store_knowledge
    from techne.core.search import search
    from techne.hooks.user_prompt_submit import _build_context

    db = tmp_path / "t.db"
    init_db(db)
    body = " ".join(f"word{i:03d}" for i in range(400))
    store_knowledge(db, content=body, type="procedure", tags="probe", confidence="high")

    seen = {}
    for cap in (200, 450, 1200, 2000):
        rows = search(db, "word001", table="knowledge", limit=5, row_chars=cap + 1)
        stats: dict = {}
        block = _build_context(rows, char_limit=cap, stats=stats)
        line = next(ln for ln in block.splitlines() if "⟦techne#" in ln)
        rendered = line.split("] ", 1)[1]
        delivered = len(rendered) - 2 if rendered.endswith(" …") else len(rendered)
        assert stats["chars_dropped"] == len(body) - delivered, (
            f"cap {cap}: chars_dropped={stats['chars_dropped']} but the reader "
            f"lost {len(body) - delivered} of {len(body)} stored characters"
        )
        seen[cap] = stats["chars_dropped"]

    assert len(set(seen.values())) == len(seen), (
        f"chars_dropped is {seen} — it must differ per cap, or the telemetry "
        "cannot tell one setting from another, which is all it is for"
    )
    assert seen[200] > seen[2000], (
        f"a tighter cap must drop MORE: {seen}"
    )
