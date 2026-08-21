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

    record_rail_marker(config, "reflect", outcome="error", detail="AuthenticationError: no key")

    results = {r.name: r for r in check_model_bound_rails(config)}
    reflect = results["rail:reflect:bound"]
    assert reflect.verdict is Verdict.BROKEN
    assert "AuthenticationError" in reflect.detail


def test_a_partial_marker_also_reads_broken(config):
    """Answered-some is not healthy. A rail degrading toward zero must be visible
    before it gets there."""
    from techne.core.checks import Verdict, check_model_bound_rails

    record_rail_marker(config, "consolidate", outcome="partial", detail="answered 1/9")

    results = {r.name: r for r in check_model_bound_rails(config)}
    assert results["rail:consolidate:bound"].verdict is Verdict.BROKEN


def test_an_ok_marker_reads_live(config):
    from techne.core.checks import Verdict, check_model_bound_rails

    record_rail_marker(config, "dream", outcome="ok", detail="answered 3/3")

    results = {r.name: r for r in check_model_bound_rails(config)}
    assert results["rail:dream:bound"].verdict is Verdict.LIVE


def test_the_check_covers_reflect_which_is_not_a_custodian_rail(config):
    """`reflect` has its own runner and is not in RAILS, but it is the rail the whole
    finding came from — it must not be omitted by virtue of living elsewhere."""
    from techne.core.checks import check_model_bound_rails

    names = {r.name for r in check_model_bound_rails(config)}
    assert "rail:reflect:bound" in names


# ---- reflect's own marker -----------------------------------------------------


def test_reflect_records_a_marker_when_its_model_is_unreachable(config, monkeypatch):
    """THE TWO-MONTH BUG, pinned. reflect_on_session caught the exception, incremented
    `errors`, and returned — leaving nothing an operator or a check could read."""
    from techne.core import reflect as R
    from techne.core.thread_orchestrator import Thread

    config.reflect.enabled = True

    def boom(cfg):
        raise RuntimeError("AuthenticationError: Missing Anthropic API Key")

    monkeypatch.setattr(R, "_make_generate_fn", boom)

    threads = [Thread(thread_num=1, label="t", summary="s")]
    R.reflect_on_session(config, "sess-1", threads, [{"content": "x"}])

    marker = _marker(config, "reflect")
    assert marker, "reflect failed to build its model and recorded NOTHING — the #555 bug"
    assert marker["outcome"] == "error"
    assert "Missing Anthropic API Key" in marker["detail"]


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
    assert "unbound" in result.output


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
    from techne.security.config_trust import EGRESS_KEYS, _NOT_A_DESTINATION

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
    from techne.core.sleepdream import consolidate_ingest, dream_ingest

    for fn in (consolidate_ingest, dream_ingest, conflict_check_ingest):
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
