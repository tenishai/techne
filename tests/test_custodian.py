"""Headless custodian — model-bound emit→ingest rails (techne#555).

THE FINDING THIS SUITE GUARDS: on the operator's own store, the automatic reflect rail
had produced ZERO candidates since 2026-06-20 while `check_maintenance_rails` reported
it LIVE — because that check keys on `knowledge_candidates.origin` and the RESIDENT
route was filling the table the whole time. Every failure here is therefore tested for
what it RECORDS, not only for what it returns: a counter nobody reads is what let this
run for two months.
"""

from __future__ import annotations

import pytest

from techne.core.custodian import (
    MARKER_PREFIX,
    RAILS,
    Rail,
    clear_rail_marker,
    effective_limit,
    effective_model,
    effective_rails,
    record_rail_marker,
    resolve_generate_fn,
    run_custodian,
    run_rail,
)


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config
    from techne.core.database import init_db

    cfg = Config.load()
    init_db(cfg.db_path)
    return cfg


def _bind(config):
    """Give the rail a model, because techne#735 made the verdict depend on it.

    These tests all say "a rail someone switched on" and none of them switched one on:
    the `config` fixture is UNBOUND, so before #735 they asserted the outcome->verdict
    mapping against a config under which the rail could not have run at all. The
    mapping is what they exist to pin and it is unchanged; this makes the fixture
    express the state each docstring already claims.
    """
    config.custodian.model = "ollama/test-model"
    config.reflect.enabled = True
    config.reflect.model = "ollama/test-model"


def _marker(config, rail):
    from techne.core.database import connect
    from techne.core.extension_state import get_state

    conn = connect(config.db_path)
    try:
        return get_state(conn, f"{MARKER_PREFIX}:{rail}:last_run") or {}
    finally:
        conn.close()


@pytest.fixture()
def fake_rail(monkeypatch):
    """A rail whose emit/ingest are controllable, so the RUNNER can be tested apart
    from any real rail's data-setup requirements. The real rails' wiring is tested
    separately below — that is where key-name mistakes actually live."""
    state = {
        "emitted": [{"prompt": "p1", "id": 1}, {"prompt": "p2", "id": 2}],
        "ingested": [],
        "producers": [],
    }

    def emit(config):
        return list(state["emitted"])

    def ingest(config, item, response, model_used):
        state["ingested"].append((item["id"], response))
        state["producers"].append(model_used)
        return {"written": 1}

    monkeypatch.setitem(
        RAILS, "faketest", Rail(name="faketest", emit=emit, ingest=ingest, what="test rail")
    )
    return state


# ---- the binding, and the fallback that must NOT exist ------------------------


def test_unbound_by_default(config):
    assert resolve_generate_fn(config) is None


def test_does_not_fall_back_to_config_model(config):
    """THE CENTRAL INVARIANT OF #555. Inheriting `Config.model` is exactly how the
    automatic reflect rail came to be bound to a credential plane the operator does not
    hold, then failed silently on every session for two months. An unset custodian must
    read as ABSENT — which runs nothing and reports dormant — never as bound to
    something unreachable."""
    config.model = "claude-haiku-4-5-20251001"
    config.custodian.model = ""

    assert resolve_generate_fn(config) is None, (
        "custodian inherited Config.model — this is the #555 regression: an unbound "
        "custodian silently bound to the session model's credential plane"
    )


def test_per_rail_model_overrides_the_default(config, monkeypatch):
    captured = {}

    def fake_make(cfg):
        captured["model"] = cfg.model
        return lambda prompt: "x"

    monkeypatch.setattr("techne.core.thread_orchestrator._make_generate_fn", fake_make)
    config.custodian.model = "ollama/small"
    config.custodian.rail_models = {"faithfulness": "vertex_ai/big"}

    resolve_generate_fn(config, rail="faithfulness")
    assert captured["model"] == "vertex_ai/big"

    resolve_generate_fn(config, rail="consolidate")
    assert captured["model"] == "ollama/small"


def test_resolving_does_not_mutate_the_caller_config(config, monkeypatch):
    """The scoped override is a copy. If it were not, binding the custodian would
    silently repoint the SESSION model — and every other consumer of config.model with
    it."""
    monkeypatch.setattr(
        "techne.core.thread_orchestrator._make_generate_fn", lambda cfg: (lambda p: "x")
    )
    config.model = "session-model"
    config.custodian.model = "ollama/llama3"

    resolve_generate_fn(config)

    assert config.model == "session-model"


# ---- the runner ---------------------------------------------------------------


def test_run_rail_answers_and_ingests_each_prompt(config, fake_rail):
    result = run_rail(config, "faketest", generate_fn=lambda prompt: f"answer-to-{prompt}")

    assert result["emitted"] == 2
    assert result["answered"] == 2
    assert result["failed"] == 0
    assert result["counts"] == {"written": 2}
    assert fake_rail["ingested"] == [(1, "answer-to-p1"), (2, "answer-to-p2")]


def test_unbound_rail_runs_nothing_and_is_not_an_error(config, fake_rail):
    """Unbound is the DEFAULT posture, not a failure — the rail is answered by a
    resident agent, which is the path that has always worked."""
    result = run_rail(config, "faketest")

    assert result["bound"] is False
    assert result["emitted"] == 0
    assert fake_rail["ingested"] == []
    assert _marker(config, "faketest") == {}, "an unbound rail wrote a liveness marker"


def test_one_bad_prompt_does_not_abort_the_rail(config, fake_rail):
    """A model that refuses one cluster must not cost the other nineteen."""

    def flaky(prompt):
        if prompt == "p1":
            raise RuntimeError("model said no")
        return "ok"

    result = run_rail(config, "faketest", generate_fn=flaky)

    assert result["answered"] == 1
    assert result["failed"] == 1
    assert fake_rail["ingested"] == [(2, "ok")]


def test_limit_caps_the_batch(config, fake_rail):
    result = run_rail(config, "faketest", generate_fn=lambda p: "ok", limit=1)
    assert result["emitted"] == 1
    assert result["answered"] == 1


def test_unknown_rail_raises(config):
    with pytest.raises(KeyError):
        run_rail(config, "no-such-rail", generate_fn=lambda p: "ok")


# ---- what it RECORDS — the half the finding was about -------------------------


def test_a_successful_run_records_ok(config, fake_rail):
    run_rail(config, "faketest", generate_fn=lambda p: "ok")
    marker = _marker(config, "faketest")
    assert marker["outcome"] == "ok"
    assert "2/2" in marker["detail"]


def test_a_total_failure_records_error_with_the_reason(config, fake_rail):
    """THE ONE THAT MATTERS. Emitting 20 prompts and answering 0 must not be
    summarised as 'ran'. The marker must carry WHY — the exception TYPE is what tells
    an operator 'this needs a credential' rather than 'this is broken'."""

    def always_fails(prompt):
        raise RuntimeError("Missing API Key")

    result = run_rail(config, "faketest", generate_fn=always_fails)

    assert result["answered"] == 0
    marker = _marker(config, "faketest")
    assert marker["outcome"] == "error", "a rail that answered nothing recorded success"
    assert "RuntimeError" in marker["detail"]
    assert "Missing API Key" in marker["detail"]


def test_a_partial_run_is_recorded_as_partial_not_ok(config, fake_rail):
    """Answered-some must be distinguishable from answered-all, or a rail degrading
    over time reads healthy until it hits zero."""

    def flaky(prompt):
        if prompt == "p1":
            raise RuntimeError("nope")
        return "ok"

    run_rail(config, "faketest", generate_fn=flaky)
    marker = _marker(config, "faketest")
    assert marker["outcome"] == "partial"
    assert "1/2" in marker["detail"]


def test_nothing_to_do_is_recorded_as_a_noop_not_silence(config, monkeypatch):
    """'Ran and found nothing' must stay distinct from 'did not run' — that
    indistinguishability is the whole subsystem's reason for existing."""
    monkeypatch.setitem(
        RAILS,
        "emptyrail",
        Rail(name="emptyrail", emit=lambda c: [], ingest=lambda c, i, r, m: {}, what="empty"),
    )
    run_rail(config, "emptyrail", generate_fn=lambda p: "ok")
    assert _marker(config, "emptyrail")["outcome"] == "noop:nothing-to-do"


def test_an_emit_failure_is_recorded_not_swallowed(config, monkeypatch):
    def boom(config):
        raise RuntimeError("emit exploded")

    monkeypatch.setitem(
        RAILS, "badrail", Rail(name="badrail", emit=boom, ingest=lambda c, i, r, m: {}, what="bad")
    )
    result = run_rail(config, "badrail", generate_fn=lambda p: "ok")

    assert "emit failed" in result["error"]
    assert _marker(config, "badrail")["outcome"] == "error"


def test_marker_failure_never_breaks_the_run(config, fake_rail, monkeypatch):
    """Bookkeeping must not be able to fail the work it describes."""
    monkeypatch.setattr(
        "techne.core.extension_state.set_state",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    result = run_rail(config, "faketest", generate_fn=lambda p: "ok")
    assert result["answered"] == 2


# ---- the real rails' wiring ---------------------------------------------------


def test_every_registry_key_matches_its_rail_name(config):
    for key, rail in RAILS.items():
        assert key == rail.name, f"registry key {key!r} does not match Rail.name {rail.name!r}"


def test_every_rail_declares_what_it_does(config):
    for rail in RAILS.values():
        assert rail.what.strip(), f"rail {rail.name} has no description for `custodian status`"


#: Rails that WRITE a candidate row and therefore must label its producer. The other
#: two take the label and ignore it by design: `faithfulness` annotates an EXISTING
#: candidate, and `memory_claims` returns an ephemeral report that persists nothing —
#: neither has a `model_used` column of its own to set.
_RAILS_THAT_LABEL_ROWS = {"consolidate", "dream", "conflict"}


@pytest.mark.parametrize(
    "rail_name,module,fn_name,item,expected_args",
    [
        (
            "consolidate",
            "techne.core.sleepdream",
            "consolidate_ingest",
            {"prompt": "p", "target_ids": [1, 2], "cluster_hash": "ch"},
            ([1, 2], "ch", "resp"),
        ),
        (
            "dream",
            "techne.core.sleepdream",
            "dream_ingest",
            {"prompt": "p", "group_ids": [3], "group_hash": "gh"},
            ([3], "gh", "resp"),
        ),
        (
            "faithfulness",
            "techne.core.faithfulness",
            "faithfulness_ingest",
            {"prompt": "p", "candidate_id": 7},
            (7, "resp"),
        ),
        (
            "conflict",
            "techne.core.conflict",
            "conflict_check_ingest",
            {"prompt": "p", "focus_id": 9, "comparison_ids": [1], "check_hash": "hh"},
            (9, [1], "hh", "resp"),
        ),
        (
            "memory_claims",
            "techne.core.memory_claims",
            "claims_ingest",
            {"prompt": "p", "prompt_hash": "ph"},
            ("ph", "resp"),
        ),
    ],
)
def test_each_adapter_passes_the_right_args_to_its_ingest(
    config, monkeypatch, rail_name, module, fn_name, item, expected_args
):
    """WHERE THE BUGS ACTUALLY LIVE. The five rails do not agree on key names
    (`target_ids` vs `group_ids` vs `candidate_id`), so each adapter re-states a
    mapping that nothing else checks. A wrong key is a TypeError at 3am in an
    unattended run — or worse, a plausible value passed to the wrong parameter."""
    captured = {}

    def fake(config_arg, *args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"written": 1}

    monkeypatch.setattr(f"{module}.{fn_name}", fake)
    RAILS[rail_name].ingest(config, item, "resp", "ollama/probe")

    assert captured["args"] == expected_args
    # AND THE PRODUCER LABEL, which is passed as a KEYWORD. Asserting only on `args`
    # left this forward unguarded — mutation caught it: dropping `model_used=model_used`
    # from the conflict adapter kept the whole suite green.
    if rail_name in _RAILS_THAT_LABEL_ROWS:
        assert captured["kwargs"].get("model_used") == "ollama/probe", (
            f"{rail_name} adapter dropped the producer label: {captured['kwargs']}"
        )
    else:
        assert "model_used" not in captured["kwargs"], (
            f"{rail_name} writes no row of its own, so it must not pass a producer label"
        )


def test_claims_emit_normalises_absent_memory_to_an_empty_batch(config, monkeypatch):
    """`memory_present=False` is a legitimate no-op, not a failure."""
    monkeypatch.setattr(
        "techne.core.memory_claims.claims_emit",
        lambda c: {"memory_present": False, "prompt": "", "prompt_hash": ""},
    )
    assert RAILS["memory_claims"].emit(config) == []


# ---- run_custodian ------------------------------------------------------------


def test_run_custodian_rejects_an_unknown_rail(config):
    with pytest.raises(KeyError):
        run_custodian(config, rails=["nope"])


def test_run_custodian_runs_the_configured_selection(config, fake_rail):
    report = run_custodian(config, rails=["faketest"], generate_fn=lambda p: "ok")
    assert [r["rail"] for r in report["rails"]] == ["faketest"]
    assert report["bound"] is True


# ---- config -------------------------------------------------------------------


def test_config_round_trips(config):
    from techne.config import Config

    cfg = Config._from_dict(
        {
            "custodian": {
                "model": "ollama/llama3",
                "rails": ["consolidate"],
                "rail_models": {"faithfulness": "vertex_ai/x"},
                "max_items_per_rail": 5,
            }
        }
    )
    again = Config._from_dict(cfg.to_yaml_dict())
    assert again.custodian == cfg.custodian, (
        "custodian config did not survive a save/load round trip — a section that is "
        "read but never written is techne#620's exact shape"
    )


def test_a_typod_rail_name_is_rejected_at_config_load(config):
    """A typo'd rail is otherwise a rail that silently never runs — techne#555's own
    failure class, reproduced inside its own config."""
    from techne.config import Config

    with pytest.raises(ValueError) as e:
        Config._from_dict({"custodian": {"rails": ["consolidat"]}})
    assert "consolidat" in str(e.value)


def test_a_typod_rail_in_rail_models_is_also_rejected(config):
    from techne.config import Config

    with pytest.raises(ValueError):
        Config._from_dict({"custodian": {"rail_models": {"faithfullness": "x"}}})


# ---- the liveness check -------------------------------------------------------


def test_no_marker_reads_dormant_not_broken(config):
    """An instance that only uses the resident route never writes a marker, and that is
    the DEFAULT and correct posture. Crying wolf on every such install is how a check
    gets trained into noise."""
    from techne.core.checks import Verdict, check_model_bound_rails

    results = check_model_bound_rails(config)
    assert results, "check produced nothing at all"
    assert all(r.verdict is Verdict.DORMANT for r in results)


def test_an_error_marker_reads_broken(config):
    """A rail someone switched on that is not working. This is the finding."""
    from techne.core.checks import Verdict, check_model_bound_rails

    _bind(config)
    record_rail_marker(config, "reflect", outcome="error", detail="AuthenticationError: no key")

    results = {r.name: r for r in check_model_bound_rails(config)}
    reflect = results["rail:reflect:bound"]
    assert reflect.verdict is Verdict.BROKEN
    assert "AuthenticationError" in reflect.detail


def test_a_partial_marker_also_reads_broken(config):
    """Answered-some is not healthy. A rail degrading toward zero must be visible
    before it gets there."""
    from techne.core.checks import Verdict, check_model_bound_rails

    _bind(config)
    record_rail_marker(config, "consolidate", outcome="partial", detail="answered 1/9")

    results = {r.name: r for r in check_model_bound_rails(config)}
    assert results["rail:consolidate:bound"].verdict is Verdict.BROKEN


def test_an_ok_marker_reads_live(config):
    from techne.core.checks import Verdict, check_model_bound_rails

    _bind(config)
    record_rail_marker(config, "dream", outcome="ok", detail="answered 3/3")

    results = {r.name: r for r in check_model_bound_rails(config)}
    assert results["rail:dream:bound"].verdict is Verdict.LIVE


def test_the_check_covers_reflect_which_is_not_a_custodian_rail(config):
    """`reflect` has its own runner and is not in RAILS, but it is the rail the whole
    finding came from — it must not be omitted by virtue of living elsewhere."""
    from techne.core.checks import check_model_bound_rails

    names = {r.name for r in check_model_bound_rails(config)}
    assert "rail:reflect:bound" in names


# ---- techne#735: a marker is not authoritative once the rail is unbound --------


def _reflect(config):
    from techne.core.checks import check_model_bound_rails

    return next(r for r in check_model_bound_rails(config) if r.name == "rail:reflect:bound")


def test_a_disabled_reflect_rail_does_not_read_live(config):
    """THE FILED DEFECT (techne#735). #732 made the UNBOUND path retract its marker;
    the DISABLED path was left alone, and it is the path an operator is more likely to
    take. `lifecycle.py` gates the call site on `reflect.enabled`, so when the feature
    is off `reflect_on_session` is never invoked and NO code path can clear the marker
    — it is permanent, not "readable for an afternoon".

    The direction is why this is Important rather than a nit: unbound + stale `error`
    read BROKEN, which is noisy, wrong and self-announcing, so someone fixes it.
    Disabled + stale `ok` read LIVE — silent, wrong and comfortable.
    """
    from techne.core.checks import Verdict

    _bind(config)
    record_rail_marker(config, "reflect", outcome="ok", detail="3 thread(s) reflected")
    assert _reflect(config).verdict is Verdict.LIVE, "precondition: bound and healthy"

    config.reflect.enabled = False

    assert _reflect(config).verdict is Verdict.DORMANT


def test_an_unbound_reflect_rail_does_not_read_live(config):
    """#732's retraction happens at WRITE time, inside `reflect_on_session`. Reading
    the check with a config that has just been unbound — before any session has ended
    — still saw the stale marker. A write-time retraction cannot cover a read that
    happens first."""
    from techne.core.checks import Verdict

    _bind(config)
    record_rail_marker(config, "reflect", outcome="ok", detail="3 thread(s) reflected")

    config.reflect.model = ""

    assert _reflect(config).verdict is Verdict.DORMANT


def test_an_unbound_custodian_rail_does_not_read_live(config):
    """THE HOLE techne#735 DOES NOT MENTION, and it is four rails wider than the issue.
    `clear_rail_marker` exists but reflect is its only caller, so an unbound custodian
    rail keeps its last marker permanently — the same shape, with no retraction
    anywhere at all."""
    from techne.core.checks import Verdict, check_model_bound_rails

    _bind(config)
    record_rail_marker(config, "dream", outcome="ok", detail="answered 3/3")
    live = {r.name: r for r in check_model_bound_rails(config)}
    assert live["rail:dream:bound"].verdict is Verdict.LIVE, "precondition"

    config.custodian.model = ""

    results = {r.name: r for r in check_model_bound_rails(config)}
    assert results["rail:dream:bound"].verdict is Verdict.DORMANT


def test_an_unbound_rail_still_shows_what_its_last_run_did(config):
    """WHAT #732 WAS PROTECTING SURVIVES. The marker is kept and its payload is shown;
    it just stops driving the verdict. If this fails, the fix has bought honesty by
    destroying the evidence, which is the trade #732 explicitly refused."""
    _bind(config)
    record_rail_marker(config, "reflect", outcome="ok", detail="3 thread(s) reflected")

    config.reflect.enabled = False

    detail = _reflect(config).detail
    assert "3 thread(s) reflected" in detail, "the last bound run must stay readable"
    assert "reflect.enabled = false" in detail, "and it must say WHY it is not authoritative"


def test_an_unbound_rail_with_a_garbled_marker_still_reads_a_sentence(config):
    """The fail-closed path accepts an empty or unrecognised outcome, so a GARBLED marker
    reaches the unbound branch too. The naive interpolation rendered "last bound run :
    garbled", and with both fields empty it trailed off at "last bound run ". Cosmetic,
    and operator-facing — which is the combination this card's own invariant says rots.
    (Prudence, #743 r1 nit 5.)"""
    _bind(config)
    record_rail_marker(config, "reflect", outcome="", detail="garbled")

    config.reflect.enabled = False

    detail = _reflect(config).detail
    assert "last bound run <no outcome recorded>: garbled" in detail, detail
    assert "run : " not in detail, f"the empty outcome leaked a stray colon: {detail}"


def test_an_unbound_rail_with_an_error_marker_reads_dormant_not_broken(config):
    """THE DOWNGRADE IS DELIBERATE — do not "fix" this back. Reading BROKEN off a stale
    payload is wrong in the same way as reading LIVE: both derive a claim about now
    from a configuration that is gone. BROKEN is merely wrong in the SAFE direction,
    which is exactly what would let it survive unexamined. Nothing is hidden — the
    outcome still rides in the detail, asserted below."""
    from techne.core.checks import Verdict

    _bind(config)
    record_rail_marker(config, "reflect", outcome="error", detail="AuthenticationError: no key")
    assert _reflect(config).verdict is Verdict.BROKEN, "precondition: bound and broken"

    config.reflect.enabled = False

    result = _reflect(config)
    assert result.verdict is Verdict.DORMANT
    assert "AuthenticationError" in result.detail


# ---- reflect's own marker -----------------------------------------------------


def test_reflect_records_a_marker_when_its_model_is_unreachable(config, monkeypatch):
    """THE TWO-MONTH BUG, pinned. reflect_on_session caught the exception, incremented
    `errors`, and returned — leaving nothing an operator or a check could read."""
    from techne.core import reflect as R
    from techne.core.thread_orchestrator import Thread

    config.reflect.enabled = True
    config.reflect.model = "ollama/llama3"  # bound — the unbound path never calls out

    def boom(cfg):
        raise RuntimeError("AuthenticationError: Missing Anthropic API Key")

    monkeypatch.setattr(R, "_make_generate_fn", boom)

    threads = [Thread(thread_num=1, label="t", summary="s")]
    R.reflect_on_session(config, "sess-1", threads, [{"content": "x"}])

    marker = _marker(config, "reflect")
    assert marker, "reflect failed to build its model and recorded NOTHING — the #555 bug"
    assert marker["outcome"] == "error"
    assert "Missing Anthropic API Key" in marker["detail"]


def test_reflect_unbound_never_calls_a_model(config, monkeypatch):
    """THE OTHER HALF OF #555. The rail was bound to `config.model` by fallback, so an
    operator who never chose a headless model still got an unattended call to whatever
    the session used — and on this operator's box that call could not authenticate.
    Enabled with no `reflect.model` must run nothing at all."""
    from techne.core import reflect as R
    from techne.core.thread_orchestrator import Thread

    config.reflect.enabled = True
    config.reflect.model = ""
    config.model = "claude-haiku-4-5-20251001"  # the id the old fallback would inherit

    def boom(cfg):
        raise AssertionError("unbound reflect must not build a model callable")

    monkeypatch.setattr(R, "_make_generate_fn", boom)

    threads = [Thread(thread_num=1, label="t", summary="s")]
    result = R.reflect_on_session(config, "sess-unbound", threads, [{"content": "x"}])

    assert result["errors"] == 0, "skipping an unbound pass is not an error"
    assert result["threads_reflected"] == 0


def test_reflect_unbound_reads_as_dormant_not_broken(config, monkeypatch):
    """Unbound must leave NO marker. `check_model_bound_rails` reads DORMANT off the
    marker's ABSENCE, and dormant is the truth for a rail answered by a resident
    agent — a `noop:` payload would green it as LIVE, which it is not."""
    from techne.core import reflect as R
    from techne.core.thread_orchestrator import Thread

    config.reflect.enabled = True
    config.reflect.model = ""
    monkeypatch.setattr(R, "_make_generate_fn", lambda cfg: (lambda p: "{}"))

    R.reflect_on_session(config, "sess-unbound", [Thread(thread_num=1, label="t", summary="s")], [])

    assert not _marker(config, "reflect"), "unbound must not write a liveness marker"


def test_unbinding_retracts_a_stale_error_marker(config, monkeypatch):
    """THE OPERATOR'S ACTUAL STATE, pinned. The marker was written by a bound-and-broken
    run; once the rail is unbound that payload describes a run that can no longer happen.
    Unbinding retracts it.

    THE ORIGINAL REASON FOR THIS TEST HAS EXPIRED — READ THIS BEFORE DECIDING THE
    RETRACTION CAN GO (techne#735). It used to end "and `doctor` would report it as
    BROKEN forever", which was true and is now false: `check_model_bound_rails` asks
    `_rail_binding` before trusting any marker, so an unbound rail reads DORMANT
    regardless of outcome — pinned by
    `test_an_unbound_rail_with_an_error_marker_reads_dormant_not_broken` twenty lines
    below. The doctor no longer needs this retraction to tell the truth.

    So this test now pins TIDINESS, not honesty, and there is a live question about
    whether it should exist at all: the unbound path DELETES the payload while the
    disabled path KEEPS it, so "what did the last bound run do?" is answerable on one
    path and not the other. That decision is deliberately NOT made here — it changes
    just-merged #732 behaviour — but whoever makes it must not make it on the expired
    premise this docstring used to state. (Prudence, #743 r1 Important 2.)
    """
    from techne.core import reflect as R
    from techne.core.thread_orchestrator import Thread

    record_rail_marker(config, "reflect", outcome="error", detail="RuntimeError: needs litellm")
    assert _marker(config, "reflect")["outcome"] == "error", "precondition"

    config.reflect.enabled = True
    config.reflect.model = ""
    monkeypatch.setattr(R, "_make_generate_fn", lambda cfg: (lambda p: "{}"))

    R.reflect_on_session(config, "sess-2", [Thread(thread_num=1, label="t", summary="s")], [])

    assert not _marker(config, "reflect"), "a retracted rail must not keep reporting BROKEN"


def test_clear_rail_marker_is_idempotent(config):
    """Clearing a rail that has no marker is a no-op, not an error — the unbound path
    runs on every teardown and must not care whether it is the first one."""
    clear_rail_marker(config, "dream")
    assert not _marker(config, "dream")

    record_rail_marker(config, "dream", outcome="ok", detail="answered 3/3")
    clear_rail_marker(config, "dream")
    clear_rail_marker(config, "dream")
    assert not _marker(config, "dream")


def test_disabled_reflect_leaves_the_marker_alone(config):
    """RETRACTION IS THE UNBOUND PATH'S JOB, NOT THE DISABLED PATH'S. `enabled=False`
    returns before any binding question is asked, and an operator who switches the
    feature off for an afternoon should still be able to read what the last bound run
    did. Only a pass that positively establishes "this rail has no binding" retracts.

    WHERE THE SAFETY COMES FROM CHANGED IN techne#735, and this test no longer carries
    it. It used `outcome="error"` — the one residual that happened to be safe, because
    a stale `error` reads BROKEN. Substitute `outcome="ok"` and the same test used to
    document a rail reporting LIVE while switched off. The marker is still kept, which
    is what this test pins; what makes keeping it safe is now
    `check_model_bound_rails` asking whether the rail is bound BEFORE trusting the
    payload. See `test_a_disabled_reflect_rail_does_not_read_live`."""
    from techne.core import reflect as R
    from techne.core.thread_orchestrator import Thread

    record_rail_marker(config, "reflect", outcome="error", detail="RuntimeError: needs litellm")
    config.reflect.enabled = False

    R.reflect_on_session(config, "sess-3", [Thread(thread_num=1, label="t", summary="s")], [])

    assert _marker(config, "reflect")["outcome"] == "error"


# ---- the CLI ------------------------------------------------------------------


def test_cli_run_unbound_exits_zero(config, monkeypatch):
    """Unbound is the default posture; a scheduler must not see it as a failure."""
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)
    result = CliRunner().invoke(main, ["custodian", "run"])
    assert result.exit_code == 0
    assert "No model bound" in result.output


def test_cli_run_exits_nonzero_when_a_rail_answered_nothing(config, fake_rail, monkeypatch):
    """A scheduled run whose only signal is exit status HAS to be able to say 'I did
    nothing'. Reporting success while producing nothing is #555 one layer up."""
    from click.testing import CliRunner

    from techne.cli import main

    def always_fails(prompt):
        raise RuntimeError("no key")

    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)
    monkeypatch.setattr(
        "techne.core.custodian.resolve_generate_fn", lambda cfg, rail=None: always_fails
    )

    result = CliRunner().invoke(main, ["custodian", "run", "--rail", "faketest"])

    assert result.exit_code == 1, f"a rail that answered 0 of 2 exited 0:\n{result.output}"
    assert "answered 0" in result.output


def test_cli_status_reports_never_run(config, monkeypatch):
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)
    result = CliRunner().invoke(main, ["custodian", "status"])
    assert result.exit_code == 0
    assert "never" in result.output
    # "unset", not "unbound": the header is a config echo and must not make a
    # behavioural claim about rails (Prudence, #709 round 5).
    assert "unset" in result.output


# ---- the timeout, which only running it for real revealed --------------------


def test_the_bound_model_gets_a_timeout(config, monkeypatch):
    """AN UNATTENDED CALL THAT NEVER RETURNS IS NOT AN EXCEPTION, so `run_rail`'s
    per-item try/except cannot save it — the runner would hang forever and never
    report. Found by running the real thing: a 1,748-char conflict prompt against a
    local llama3.2 had not returned after 240s while a trivial prompt took 1.3s."""
    captured = {}

    def fake_make(cfg):
        captured["timeout"] = getattr(cfg, "llm_timeout", None)
        return lambda p: "x"

    monkeypatch.setattr("techne.core.thread_orchestrator._make_generate_fn", fake_make)
    config.custodian.model = "ollama/llama3.2"
    config.custodian.timeout_seconds = 90

    resolve_generate_fn(config)

    assert captured["timeout"] == 90, "the bound model was built with no timeout"


def test_the_timeout_does_not_leak_onto_the_caller_config(config, monkeypatch):
    """Set on the scoped copy only — an interactive caller of config must not silently
    inherit a headless deadline."""
    monkeypatch.setattr(
        "techne.core.thread_orchestrator._make_generate_fn", lambda cfg: (lambda p: "x")
    )
    config.custodian.model = "ollama/llama3.2"

    resolve_generate_fn(config)

    assert not getattr(config, "llm_timeout", 0)


def test_generate_fn_passes_timeout_through_to_litellm(config, monkeypatch):
    """The config field has to reach the actual call, not just exist."""
    import sys
    import types

    captured = {}

    fake_litellm = types.ModuleType("litellm")

    def completion(**kwargs):
        captured.update(kwargs)

        class R:
            choices = [types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))]

        return R()

    fake_litellm.completion = completion
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)

    from techne.core.thread_orchestrator import _make_generate_fn

    config.model = "ollama/llama3.2"
    config.llm_timeout = 45
    _make_generate_fn(config)("hello")

    assert captured.get("timeout") == 45


def test_generate_fn_omits_timeout_when_unset(config, monkeypatch):
    """Additive by construction — every existing caller must behave exactly as before."""
    import sys
    import types

    captured = {}
    fake_litellm = types.ModuleType("litellm")

    def completion(**kwargs):
        captured.update(kwargs)

        class R:
            choices = [types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))]

        return R()

    fake_litellm.completion = completion
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)

    from techne.core.thread_orchestrator import _make_generate_fn

    config.model = "ollama/llama3.2"
    _make_generate_fn(config)("hello")

    assert "timeout" not in captured


# ---- the operator-layer classification ---------------------------------------


def test_the_model_bindings_are_governed_as_egress_destinations():
    """THE HIGHEST-VALUE DESTINATION KEYS IN THE FILE, because this binding runs
    UNATTENDED. A compromised agent that set `custodian.model` to its own endpoint
    would ship the distilled knowledge tier out on a timer with nobody present to
    notice. `rail_models` is listed too — pinning only the default would leave the
    per-rail overrides as an unpinned way to repoint the same traffic.

    The completeness guard in test_config_trust only requires that each field be
    CLASSIFIED somehow; this one pins WHICH WAY, so the judgement cannot be quietly
    downgraded to 'not a destination' later.
    """
    from techne.security.config_trust import _NOT_A_DESTINATION, EGRESS_KEYS

    assert "custodian.model" in EGRESS_KEYS
    assert "custodian.rail_models" in EGRESS_KEYS
    assert "custodian.model" not in _NOT_A_DESTINATION

    # Volume, not destination — same line `reflect.enabled` sits on.
    for volume_key in ("custodian.rails", "custodian.max_items_per_rail",
                       "custodian.timeout_seconds"):
        assert volume_key in _NOT_A_DESTINATION
        assert volume_key not in EGRESS_KEYS


# ---- the producer label -------------------------------------------------------


def test_headless_rows_are_labelled_with_the_model_that_judged_them(config, fake_rail, monkeypatch):
    """THE FEATURE MUST NOT MANUFACTURE THE HOLE IT EXISTS TO FIX.

    `model_used` is the ONLY column recording which producer judged a candidate, and
    the whole #555 finding turned on it: the automatic reflect rail was invisible for
    two months because nothing distinguished its output from the resident route's. A
    headless run writing rows labelled 'agent-assisted' would recreate that blindness
    for all five rails at once. Caught by running the real CLI against a real model and
    reading the row it wrote — the suite was green throughout.
    """
    config.custodian.model = "ollama/llama3.2"
    run_rail(config, "faketest", generate_fn=lambda p: "ok")

    assert fake_rail["producers"] == ["ollama/llama3.2", "ollama/llama3.2"]


def test_the_per_rail_model_is_the_label_when_set(config, fake_rail):
    config.custodian.model = "ollama/small"
    config.custodian.rail_models = {"faketest": "vertex_ai/big"}

    run_rail(config, "faketest", generate_fn=lambda p: "ok")

    assert fake_rail["producers"] == ["vertex_ai/big", "vertex_ai/big"]


def test_an_injected_generate_fn_still_gets_a_producer_label(config, fake_rail):
    """A caller that injects its own callable (tests, or a future transport) must not
    produce unlabelled rows — the label falls back to a name that is at least not a
    LIE about being resident."""
    run_rail(config, "faketest", generate_fn=lambda p: "ok")

    assert fake_rail["producers"] == ["custodian", "custodian"]
    assert "agent-assisted" not in fake_rail["producers"]


def test_the_resident_default_is_one_shared_constant(config):
    """conflict imports sleepdream's constant rather than re-typing the literal — a
    second copy drifts the moment either side moves (techne#518, five times)."""
    from techne.core.conflict import RESIDENT_PRODUCER as a
    from techne.core.sleepdream import RESIDENT_PRODUCER as b

    assert a is b == "agent-assisted"


def test_resident_ingest_still_labels_rows_agent_assisted(config):
    """THE RESIDENT PATH IS UNCHANGED — the new kwarg defaults, so a caller that does
    not pass it writes exactly what it always wrote."""
    import inspect

    from techne.core.conflict import conflict_check_ingest
    from techne.core.federation import federation_conflict_ingest
    from techne.core.sleepdream import consolidate_ingest, dream_ingest

    # federation_conflict_ingest WAS THE ONE OMISSION, and it survived a full 3,240-test
    # sweep when its default was mutated (Prudence, #709). Its behaviour in-tree is
    # correct — it imports conflict's `_insert_candidate`, so the required-kwarg
    # mechanism protects its five forwards, and the headless direction is pinned
    # elsewhere in this file. What was unguarded is the RESIDENT direction: nothing
    # would have noticed if this public default drifted. In a PR whose thesis is that
    # a mislabelled producer is invisible, an unpinned public default is the gap.
    for fn in (consolidate_ingest, dream_ingest, conflict_check_ingest,
               federation_conflict_ingest):
        default = inspect.signature(fn).parameters["model_used"].default
        assert default == "agent-assisted", f"{fn.__name__} changed the resident default"


def test_a_supersede_verdict_labels_its_row_with_the_model_too(config):
    """THE PATH A REAL RUN ACTUALLY TOOK. SUPERSEDE and UPDATE verdicts write their
    candidate from inside `_handle_supersede` / `_handle_update`, NOT from the ingest
    body — two extra hops the label has to survive. Mutation proved this gap was real:
    deleting `model_used=model_used` from the handler inserts left the whole suite
    green, while the first end-to-end run against llama3.2 returned supersede=3, so
    three of the five rows written would have lied about their producer.
    """
    import json

    from techne.core.conflict import conflict_check_emit, conflict_check_ingest
    from techne.core.database import connect
    from techne.core.knowledge import store

    store(config.db_path, "we use Postgres as the primary datastore", type="fact",
          confidence="high", tags="datastore")
    store(config.db_path, "we switched to SQLite as the primary datastore", type="fact",
          confidence="high", tags="datastore")
    # A THIRD tagged entry, so the emitted item carries MORE THAN ONE comparison id.
    # With only two entries there is exactly one comparison and only the SUPERSEDE path
    # runs — which is how the ingest-body insert stayed uncovered through a full
    # mutation sweep.
    store(config.db_path, "the cache layer is Redis", type="fact",
          confidence="high", tags="datastore")

    batch = conflict_check_emit(config)
    assert batch, "no conflict pairs emitted — fixture did not produce a comparable pair"
    item = batch[0]
    target = item["comparison_ids"][0]
    # BOTH VERDICT FAMILIES IN ONE RESPONSE, because they take different insert paths:
    # SUPERSEDE writes from inside `_handle_supersede`, while DISTINCT/NOOP write their
    # `status='discarded'` triage rows straight from the ingest body. Mutation showed
    # the two paths are independently droppable — covering only one leaves the other
    # free to write rows claiming a resident agent judged them.
    verdicts = [{"id": target, "verdict": "SUPERSEDE", "content": "we switched to SQLite",
                 "reason": "directly contradicts the older entry"}]
    for other in item["comparison_ids"][1:]:
        verdicts.append({"id": other, "verdict": "NOOP", "reason": "already known"})
    response = json.dumps(verdicts)

    counts = conflict_check_ingest(
        config, item["focus_id"], item["comparison_ids"], item["check_hash"], response,
        model_used="ollama/llama3.2",
    )
    assert counts["supersede"] >= 1, f"fixture produced no supersede verdict: {counts}"
    assert counts["noop"] >= 1, (
        f"fixture exercised only the handler path, not the ingest-body insert: {counts}"
    )

    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT model_used FROM knowledge_candidates WHERE origin = 'conflict'"
        ).fetchall()
    finally:
        conn.close()
    labels = {r[0] for r in rows}
    assert labels == {"ollama/llama3.2"}, (
        f"a conflict candidate was written under the wrong producer: {labels}. "
        "SUPERSEDE/UPDATE rows are inserted from inside the handlers — the label has "
        "to be forwarded there too, or a headless run writes rows claiming a resident "
        "agent judged them (techne#555)."
    )


def test_the_no_verdict_row_is_also_labelled(config):
    """The THIRD insert path in conflict: a response the parser finds no actionable
    verdict in still writes a `status='discarded'` marker, so the pair is not
    re-judged forever. It is the least interesting row in the system and exactly the
    kind that gets missed — mutation showed it was the last unguarded forward, and no
    test exercised the branch at all.
    """
    from techne.core.conflict import conflict_check_emit, conflict_check_ingest
    from techne.core.database import connect
    from techne.core.knowledge import store

    store(config.db_path, "we use Postgres as the primary datastore", type="fact",
          confidence="high", tags="datastore")
    store(config.db_path, "we switched to SQLite as the primary datastore", type="fact",
          confidence="high", tags="datastore")

    batch = conflict_check_emit(config)
    assert batch
    item = batch[0]

    counts = conflict_check_ingest(
        config, item["focus_id"], item["comparison_ids"], item["check_hash"],
        "the model replied with prose and no verdicts at all",
        model_used="ollama/llama3.2",
    )
    assert counts["errors"] == 0, f"the no-verdict path raised instead of recording: {counts}"

    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT model_used, decision_reason FROM knowledge_candidates WHERE origin='conflict'"
        ).fetchall()
    finally:
        conn.close()
    assert rows, "no marker row written — the pair will be re-judged on every run"
    assert {r[0] for r in rows} == {"ollama/llama3.2"}, (
        f"the no-verdict marker claims the wrong producer: {[r[0] for r in rows]}"
    )


@pytest.mark.parametrize("verdict", ["SUPERSEDE", "DISTINCT", "UPDATE", "NO_VERDICT"])
def test_federation_conflict_rows_carry_the_producer_too(config, verdict):
    """THE THIRD PRODUCER. Cross-store conflict verdicts are written by
    `federation_conflict_ingest`, which has its OWN six insert sites — and because it
    defaults to the resident label, a dropped forward there is invisible to any test
    that never passes a different one. Mutation proved exactly that: two sites survived
    a sweep the other sixteen died in.

    Parametrized over both verdict families because they take different insert paths,
    the same split that hid the gap on the local conflict rail.
    """
    import json

    from techne.core.database import connect
    from techne.core.federation import federation_conflict_ingest
    from techne.core.knowledge import store

    local_id = store(
        config.db_path, "we use Postgres as the primary datastore", type="fact",
        confidence="high", tags="datastore",
    )

    federation_conflict_ingest(
        config,
        focus_content="we switched to sqlite — much simpler",
        focus_store="team-archivist",
        focus_event_at="2030-01-01T00:00:00",
        comparison_ids=[local_id],
        check_hash="",
        # NO_VERDICT is not a verdict — it is unparseable prose, which takes a THIRD
        # insert path (the "don't re-nag" discarded marker). Mutation showed that path
        # and UPDATE were the last two unguarded forwards in the whole change.
        response=(
            "the model replied with prose and no verdicts at all"
            if verdict == "NO_VERDICT"
            else json.dumps([{"id": local_id, "verdict": verdict,
                              "content": "we switched to sqlite",
                              "reason": "cross-store contradiction"}])
        ),
        model_used="ollama/llama3.2",
    )

    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT model_used FROM knowledge_candidates WHERE origin LIKE '%conflict%'"
        ).fetchall()
    finally:
        conn.close()
    assert rows, f"{verdict} wrote no candidate at all"
    assert {r[0] for r in rows} == {"ollama/llama3.2"}, (
        f"a federation-conflict row claims the wrong producer: {[r[0] for r in rows]}"
    )


def test_the_bound_model_keeps_the_configured_api_base(config, monkeypatch):
    """THE ACTUAL DEPLOYMENT SHAPE. The model does not run on the box techne runs on —
    it is served over the LAN (or via a proxy), selected by `llm_api_base`. The scoped
    copy must carry it, or the custodian silently tries to reach a model on localhost
    and reports a connection error that looks like a broken rail.

    Verified live against a real host: ornith:9b over llm_api_base answered a real
    conflict prompt in 11.6s, where the same prompt on the dev laptop had not returned
    after 240s.
    """
    captured = {}

    def fake_make(cfg):
        captured["api_base"] = cfg.llm_api_base
        captured["model"] = cfg.model
        return lambda p: "x"

    monkeypatch.setattr("techne.core.thread_orchestrator._make_generate_fn", fake_make)
    config.llm_api_base = "http://inference-host:11434"
    config.custodian.model = "ollama/ornith:9b"

    resolve_generate_fn(config)

    assert captured["api_base"] == "http://inference-host:11434"
    assert captured["model"] == "ollama/ornith:9b"


# ---- what Prudence found in #709 ---------------------------------------------


def test_run_custodian_honours_the_configured_cap(config, monkeypatch):
    """`run_custodian` calls ITSELF the headless entry point and "the thing a scheduler
    calls" — and it was the one path that ignored `max_items_per_rail`. An operator who
    lowered it to bound unattended spend did not get what they set, on exactly the entry
    point the docs point them at. Measured before the fix: cap 5, answered 50 of 50.
    """
    monkeypatch.setitem(
        RAILS, "probe",
        Rail(name="probe", what="probe",
             emit=lambda c: [{"prompt": f"p{i}"} for i in range(50)],
             ingest=lambda c, i, r, m: {"written": 1}),
    )
    config.custodian.max_items_per_rail = 5

    report = run_custodian(config, rails=["probe"], generate_fn=lambda p: "ok")

    assert report["rails"][0]["answered"] == 5, (
        f"max_items_per_rail ignored: answered {report['rails'][0]['answered']} of 50"
    )


def test_an_explicit_limit_still_beats_the_configured_cap(config, monkeypatch):
    monkeypatch.setitem(
        RAILS, "probe",
        Rail(name="probe", what="probe",
             emit=lambda c: [{"prompt": f"p{i}"} for i in range(50)],
             ingest=lambda c, i, r, m: {"written": 1}),
    )
    config.custodian.max_items_per_rail = 5

    report = run_custodian(config, rails=["probe"], generate_fn=lambda p: "ok", limit=2)

    assert report["rails"][0]["answered"] == 2


def test_an_unrecognised_marker_outcome_fails_closed(config):
    """THE FAIL-OPEN THIS CHECK EXISTED TO NOT HAVE. Everything that was not literally
    'error' or 'partial' mapped to LIVE — including a missing, empty or garbled outcome,
    because `str(marker.get("outcome", ""))` yields "". Fail-open in the one check whose
    entire job is catching silent nothing. (Prudence, #709.)
    """
    from techne.core.checks import Verdict, check_model_bound_rails

    _bind(config)
    record_rail_marker(config, "dream", outcome="", detail="garbled")
    record_rail_marker(config, "conflict", outcome="something-new-from-the-future")

    results = {r.name: r for r in check_model_bound_rails(config)}
    assert results["rail:dream:bound"].verdict is Verdict.UNKNOWN
    assert results["rail:conflict:bound"].verdict is Verdict.UNKNOWN


def test_a_noop_is_still_healthy(config):
    """Nothing to chew on is not a fault for consolidate/dream/conflict — the
    fail-closed branch above must not sweep the legitimate steady state with it."""
    from techne.core.checks import Verdict, check_model_bound_rails

    _bind(config)
    record_rail_marker(config, "consolidate", outcome="noop:nothing-to-do")

    results = {r.name: r for r in check_model_bound_rails(config)}
    assert results["rail:consolidate:bound"].verdict is Verdict.LIVE


def test_memory_claims_records_which_project_it_resolved(config, monkeypatch, tmp_path):
    """'Found nothing' and 'looked in the wrong place' are the SAME empty batch. A
    headless run's cwd is whatever the scheduler hands it, and `claims_emit` falls
    through to `Path.cwd()`. Measured before the fix: from a non-project cwd the rail
    made ZERO model calls and the check reported LIVE — techne#555's own shape,
    reproduced inside the check written to close it. (Prudence, #709.)
    """
    monkeypatch.chdir(tmp_path)
    calls = []

    run_rail(config, "memory_claims", generate_fn=lambda p: calls.append(p) or "x")

    assert calls == [], "fixture unexpectedly had curated memory"
    marker = _marker(config, "memory_claims")
    # The reason is rail-specific now — see test_memory_claims_names_its_empty_reason.
    assert marker["outcome"] == "noop:no-memory-for-project"
    assert "resolved:" in marker["detail"], (
        f"the rail found nothing and did not say what it looked for: {marker['detail']!r}"
    )


def test_a_context_probe_failure_cannot_break_the_run(config, monkeypatch):
    """A diagnostic that can fail the run it describes is worse than no diagnostic."""
    monkeypatch.setitem(
        RAILS, "ctxrail",
        Rail(name="ctxrail", what="probe", emit=lambda c: [],
             ingest=lambda c, i, r, m: {},
             context=lambda c: (_ for _ in ()).throw(RuntimeError("probe exploded"))),
    )
    result = run_rail(config, "ctxrail", generate_fn=lambda p: "ok")
    assert result["error"] == ""
    assert _marker(config, "ctxrail")["outcome"] == "noop:nothing-to-do"


@pytest.mark.parametrize("field,value", [
    ("max_items_per_rail", 0), ("max_items_per_rail", -1),
    ("timeout_seconds", 0), ("timeout_seconds", -5),
])
def test_bounds_that_disable_a_guard_are_rejected(field, value):
    """Both silently switch off a protection their own docstrings call load-bearing:
    a 0 cap makes every rail a permanent no-op, and `timeout_seconds: 0` is FALSY so
    `_make_generate_fn` skips the timeout kwarg and an unattended run can hang."""
    from techne.config import Config

    with pytest.raises(ValueError) as e:
        Config._from_dict({"custodian": {field: value}})
    assert field in str(e.value)


def test_cli_rejects_an_unknown_rail_without_a_traceback(config, monkeypatch):
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)
    for argv in (["custodian", "run", "--rail", "typo"],
                 ["custodian", "run", "--rail", "typo", "--dry-run"]):
        result = CliRunner().invoke(main, argv)
        assert result.exit_code != 0
        assert "Traceback" not in result.output, argv
        assert "typo" in result.output


# ---- Prudence #709 round 2 ----------------------------------------------------


def test_dry_run_reports_what_the_real_run_would_actually_send(config, monkeypatch):
    """THE ASSERTION THAT STOPS THE THREE PATHS DRIFTING APART. run_custodian, run_rail
    and the CLI's --dry-run branch each decide how many prompts leave the box, and
    dry-run was the one that disagreed — it called rail.emit() directly, bypassing both
    runners, and printed the uncapped length. It is also the ONLY surface an operator
    consults before wiring cron. Measured before the fix: cap 5, dry-run said 50, the
    real run answered 5. (Prudence, #709 round 2.)
    """
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setitem(
        RAILS, "probe",
        Rail(name="probe", what="probe",
             emit=lambda c: [{"prompt": f"p{i}"} for i in range(50)],
             ingest=lambda c, i, r, m: {"written": 1}),
    )
    config.custodian.max_items_per_rail = 5
    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)

    dry = CliRunner().invoke(main, ["custodian", "run", "--rail", "probe", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    reported = int(dry.output.split("probe: ")[1].split(" ")[0])

    real = run_custodian(config, rails=["probe"], generate_fn=lambda p: "ok")

    assert reported == real["rails"][0]["emitted"], (
        f"dry-run promised {reported} prompts, the real run emitted "
        f"{real['rails'][0]['emitted']}"
    )


def test_dry_run_honours_an_explicit_limit(config, monkeypatch):
    """--limit was silently inert on a flag the same command accepts."""
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setitem(
        RAILS, "probe",
        Rail(name="probe", what="probe",
             emit=lambda c: [{"prompt": f"p{i}"} for i in range(50)],
             ingest=lambda c, i, r, m: {"written": 1}),
    )
    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)

    result = CliRunner().invoke(
        main, ["custodian", "run", "--rail", "probe", "--dry-run", "--limit", "3"]
    )
    assert "probe: 3 prompt(s)" in result.output, result.output


def test_memory_claims_names_its_empty_reason_in_the_outcome(config, monkeypatch, tmp_path):
    """The discriminator belongs in `outcome` — the field the CHECK keys on — not only
    in free-text `detail`, or the follow-up design work has to parse prose to act on
    it. Every noop:* still maps to LIVE, so this costs nothing today."""
    monkeypatch.chdir(tmp_path)

    run_rail(config, "memory_claims", generate_fn=lambda p: "x")

    marker = _marker(config, "memory_claims")
    assert marker["outcome"] == "noop:no-memory-for-project"
    assert "resolved:" in marker["detail"]


def test_a_generic_rail_still_reports_the_plain_noop(config, monkeypatch):
    monkeypatch.setitem(
        RAILS, "emptyrail2",
        Rail(name="emptyrail2", what="e", emit=lambda c: [], ingest=lambda c, i, r, m: {}),
    )
    run_rail(config, "emptyrail2", generate_fn=lambda p: "ok")
    assert _marker(config, "emptyrail2")["outcome"] == "noop:nothing-to-do"


def test_an_emit_failure_marker_keeps_the_context_note(config, monkeypatch):
    """If emit RAISED, 'which project was it looking at' is at its most useful — and it
    was the one outcome that did not carry it."""
    monkeypatch.setitem(
        RAILS, "boomrail",
        Rail(name="boomrail", what="b",
             emit=lambda c: (_ for _ in ()).throw(RuntimeError("emit exploded")),
             ingest=lambda c, i, r, m: {},
             context=lambda c: "some-project"),
    )
    run_rail(config, "boomrail", generate_fn=lambda p: "ok")
    marker = _marker(config, "boomrail")
    assert marker["outcome"] == "error"
    assert "some-project" in marker["detail"]


def test_the_context_probe_runs_once_per_rail_run(config, monkeypatch):
    """Two Path.cwd() + slug resolutions for one marker is waste on a rail that runs
    on a timer."""
    calls = []
    monkeypatch.setitem(
        RAILS, "countrail",
        Rail(name="countrail", what="c", emit=lambda c: [], ingest=lambda c, i, r, m: {},
             context=lambda c: (calls.append(1), "proj")[1]),
    )
    run_rail(config, "countrail", generate_fn=lambda p: "ok")
    assert len(calls) == 1, f"context probe ran {len(calls)} times"


# ---- Prudence #709 round 3 ----------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, -5])
def test_an_unusable_limit_is_rejected_rather_than_sliced(config, bad):
    """`effective_limit` decides WHICH number; deciding whether the number is USABLE is
    the other half of being its owner. The config path already rejected these at load
    and `--limit` walked straight past the same guard into `batch[:limit]`.

    Both failures are silent and one is a common idiom: `--limit -1` reads as "no cap"
    and actually means "all but the last item of every batch" (measured: 49 of 50),
    while `--limit 0` is a rail that never runs, records a noop that maps to LIVE, and
    exits 0 — techne#555's shape through a flag on the command written to close it.
    """
    with pytest.raises(ValueError) as e:
        effective_limit(config, bad)
    assert ">= 1" in str(e.value)


def test_a_valid_limit_and_the_configured_default_both_resolve(config):
    config.custodian.max_items_per_rail = 7
    assert effective_limit(config, None) == 7
    assert effective_limit(config, 3) == 3


def test_the_cli_rejects_a_bad_limit_without_a_traceback(config, monkeypatch):
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)
    for argv in (["custodian", "run", "--limit", "0"],
                 ["custodian", "run", "--limit", "-1", "--dry-run"]):
        result = CliRunner().invoke(main, argv)
        assert result.exit_code != 0, argv
        assert "Traceback" not in result.output, argv
        assert ">= 1" in result.output, f"{argv}: {result.output}"


def test_dry_run_and_the_runner_agree_on_WHICH_RAILS_TOO(config, monkeypatch):
    """THE AXIS THE ROUND-2 GUARD HELD CONSTANT. That test passes `--rail probe` and
    `rails=["probe"]`, pinning both paths to the same explicit selection — so the one
    axis where they still diverged was the one it could not see. Measured before the
    fix: `custodian.rails = ["conflict"]`, dry-run promised five rails, the runner ran
    one. Quantitatively the larger over-report of the two. (Prudence, #709 round 3.)

    This test deliberately passes NO --rail, so the equality assertion covers selection
    and count at once.
    """
    from click.testing import CliRunner

    from techne.cli import main

    for name in ("alpha", "beta"):
        monkeypatch.setitem(
            RAILS, name,
            Rail(name=name, what=name,
                 emit=lambda c, n=name: [{"prompt": f"{n}-{i}"} for i in range(4)],
                 ingest=lambda c, i, r, m: {"written": 1}),
        )
    config.custodian.rails = ["alpha"]
    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)

    dry = CliRunner().invoke(main, ["custodian", "run", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    dry_rails = sorted(
        line.split(":")[0] for line in dry.output.strip().splitlines() if ":" in line
    )

    real = run_custodian(config, generate_fn=lambda p: "ok")
    real_rails = sorted(r["rail"] for r in real["rails"])

    assert dry_rails == real_rails, (
        f"dry-run promised rails {dry_rails}, the runner ran {real_rails}"
    )


def test_effective_rails_honours_the_configured_selection(config):
    config.custodian.rails = ["conflict"]
    assert effective_rails(config, None) == ["conflict"]
    assert effective_rails(config, ["dream"]) == ["dream"]
    config.custodian.rails = []
    assert effective_rails(config, None) == sorted(RAILS)


def test_effective_rails_rejects_an_unknown_name(config):
    with pytest.raises(KeyError):
        effective_rails(config, ["no-such-rail"])


def test_the_empty_outcome_hook_receives_config(config, monkeypatch):
    """Its sibling `context` hook gets config, and the two are advertised side by side
    on the dataclass — so a hook author will reasonably write one that reads config.
    With None it would raise AttributeError, be swallowed, and silently degrade to the
    generic reason. Losing the discriminator quietly is what the hook exists to
    prevent. (Prudence, #709 round 3.)"""
    seen = {}
    monkeypatch.setitem(
        RAILS, "cfgrail",
        Rail(name="cfgrail", what="c", emit=lambda c: [], ingest=lambda c, i, r, m: {},
             empty_outcome=lambda c: (seen.update(got=c is not None), "noop:saw-config")[1]),
    )
    run_rail(config, "cfgrail", generate_fn=lambda p: "ok")

    assert seen.get("got") is True, "the hook was handed None where its sibling gets config"
    assert _marker(config, "cfgrail")["outcome"] == "noop:saw-config"


def test_dry_run_survives_a_rail_whose_emit_raises(config, monkeypatch):
    """The real run records an error marker and continues; dry-run exited 1 with a
    traceback and left the remaining rails unreported, on a command meant for cron."""
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setitem(
        RAILS, "boom",
        Rail(name="boom", what="b",
             emit=lambda c: (_ for _ in ()).throw(RuntimeError("emit exploded")),
             ingest=lambda c, i, r, m: {}),
    )
    monkeypatch.setitem(
        RAILS, "fine",
        Rail(name="fine", what="f", emit=lambda c: [{"prompt": "p"}],
             ingest=lambda c, i, r, m: {}),
    )
    config.custodian.rails = ["boom", "fine"]
    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)

    result = CliRunner().invoke(main, ["custodian", "run", "--dry-run"])

    assert "Traceback" not in result.output
    assert "boom: emit failed" in result.output
    assert "fine: 1 prompt(s)" in result.output, "a raising rail suppressed the rest"


# ---- Prudence #709 round 4: the model axis ------------------------------------


def test_status_reports_the_model_the_runner_will_actually_use(config, monkeypatch):
    """THE FOURTH AXIS. `custodian status` is THE surface built to answer "what is
    bound", and it never read `rail_models`: a rail carrying a per-rail override
    reported the GLOBAL model and a bare `bound: yes`, while the runner would use the
    override. In this feature that is a wrong belief about `model_used` — the one column
    recording which producer judged a candidate, and the column the whole techne#555
    finding turned on. (Prudence, #709 round 4.)
    """
    from click.testing import CliRunner

    from techne.cli import main

    config.custodian.model = "vertex_ai/MODEL-A"
    config.custodian.rail_models = {"conflict": "vertex_ai/MODEL-B"}
    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)

    out = CliRunner().invoke(main, ["custodian", "status"]).output
    conflict_block = out.split("conflict\n")[1].split("consolidate")[0]

    assert "vertex_ai/MODEL-B" in conflict_block, (
        f"status named the wrong model for a rail with an override:\n{conflict_block}"
    )
    assert effective_model(config, rail="conflict") == "vertex_ai/MODEL-B"


def test_dry_run_names_the_model_it_claims_to_name(config, monkeypatch):
    """The docstring says dry-run "shows exactly what WOULD be sent, and to which
    model". It printed bound/unbound only. A claim a surface does not honour is the same
    defect as a number it gets wrong."""
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setitem(
        RAILS, "probe",
        Rail(name="probe", what="p", emit=lambda c: [{"prompt": "p"}],
             ingest=lambda c, i, r, m: {}),
    )
    config.custodian.model = "ollama/small"
    config.custodian.rail_models = {"probe": "vertex_ai/big"}
    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)

    out = CliRunner().invoke(main, ["custodian", "run", "--rail", "probe", "--dry-run"]).output

    assert "vertex_ai/big" in out, out


def test_all_three_surfaces_agree_on_the_model(config, monkeypatch):
    """The whole point of the axis enumeration: status, dry-run and the producer label
    the runner writes must name ONE model for a given rail."""
    from click.testing import CliRunner

    from techne.cli import main

    seen = []
    monkeypatch.setitem(
        RAILS, "probe",
        Rail(name="probe", what="p", emit=lambda c: [{"prompt": "p"}],
             ingest=lambda c, i, r, m: seen.append(m) or {}),
    )
    config.custodian.model = "ollama/small"
    config.custodian.rail_models = {"probe": "vertex_ai/big"}
    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)
    monkeypatch.setattr(
        "techne.core.thread_orchestrator._make_generate_fn", lambda cfg: (lambda p: "ok")
    )

    runner = CliRunner()
    status = runner.invoke(main, ["custodian", "status"]).output
    dry = runner.invoke(main, ["custodian", "run", "--rail", "probe", "--dry-run"]).output
    run_rail(config, "probe", generate_fn=lambda p: "ok")

    assert "vertex_ai/big" in status
    assert "vertex_ai/big" in dry
    assert seen == ["vertex_ai/big"], f"the runner labelled rows {seen}"


def test_an_explicit_empty_rail_list_runs_nothing(config, monkeypatch):
    """`rails=[]` IS A DECISION, NOT AN ABSENCE. Moving the selection logic in round 3
    flipped `rails is not None` to `if rails`, so an explicit empty list went from
    running zero rails to running every registered one — fail-open in the
    token-spending direction, which is the wrong way for a headless runner to be wrong.
    Latent (no in-tree caller reaches it) and caught by review, not by the suite.
    (Prudence, #709 round 4.)
    """
    monkeypatch.setitem(
        RAILS, "probe",
        Rail(name="probe", what="p", emit=lambda c: [{"prompt": "p"}],
             ingest=lambda c, i, r, m: {}),
    )
    report = run_custodian(config, rails=[], generate_fn=lambda p: "ok")

    assert report["rails"] == [], f"an explicit empty selection ran {len(report['rails'])} rail(s)"


def test_none_still_means_the_configured_selection(config):
    config.custodian.rails = ["dream"]
    assert effective_rails(config, None) == ["dream"]
    assert effective_rails(config, []) == []


def test_status_header_does_not_claim_resident_when_a_rail_override_binds(config, monkeypatch):
    """`rail_models` WITHOUT a global `custodian.model` is a legal, validated config,
    and the header said "unbound — rails stay resident" four lines above a rail
    reporting "bound: yes — vertex_ai/MODEL-B". The runner agreed with the second.
    A fact about one config key had been glossed into a behavioural claim about rails.
    (Prudence, #709 round 5.)
    """
    from click.testing import CliRunner

    from techne.cli import main

    config.custodian.model = ""
    config.custodian.rail_models = {"conflict": "vertex_ai/MODEL-B"}
    monkeypatch.setattr("techne.config.Config.load", lambda *a, **k: config)

    out = CliRunner().invoke(main, ["custodian", "status"]).output

    assert "rails stay resident" not in out, out
    assert "vertex_ai/MODEL-B" in out
