"""Headless custodian — run the emit→ingest rails without a live session (techne#555).

A CUSTODIAN THAT ONLY WORKS WHILE YOU ARE WATCHING IS NOT A CUSTODIAN. That is the
whole issue. techne has two model paths and they split badly:

  * the OPERATOR-MODEL path (`_make_generate_fn` → LiteLLM) drives `reflect` and the
    thread summarizer, and can run unattended;
  * the RESIDENT emit→ingest path — techne renders a prompt, the in-session agent
    answers it with its own model, techne ingests the answer — drives `consolidate`,
    `dream`, `faithfulness`, `memory_claims` and `conflict`.

Everything genuinely CUSTODIAL (dedup, merge/supersede, the faithfulness check on
rewrites, contradiction classification) is on the second path, and the second path has
no daemon. This module gives those rails an optional `generate_fn` so the same emitted
prompt can be answered by a bound model instead of a resident agent.

NOTHING ABOUT THE RESIDENT PATH CHANGES. Unbound, every rail behaves exactly as before;
borrowed inference stays the right choice for judgment work the operator is present for.
This adds a second way to answer the prompts, not a replacement for the first.

=== PROVIDER-AGNOSTIC BY CONSTRUCTION ===

The binding is a callable, `str -> str`. It resolves through the SAME
`_make_generate_fn` the operator-model path already uses, so any LiteLLM identifier
works: `ollama/llama3` (local, free, no policy surface), `vertex_ai/claude-*` via ADC,
an OpenAI id, a proxy `api_base`. THE CREDENTIAL QUESTION BELONGS IN CONFIG, NOT IN
THIS CODE PATH — five viable transports were enumerated on the issue and the right
answer differs per operator and per pass. Do not special-case a provider here; if a
transport needs bespoke handling, it needs it in `_make_generate_fn` where every other
caller gets it too.

The intended tiering, recorded so the config makes sense to a reader:
mechanical passes (dedup, contradiction classification — classification, not
architecture) run fine on a small local model; judgment passes deserve a better one;
work the operator is present for should stay resident and cost nothing.

=== THE FAILURE THIS MODULE EXISTS TO NOT REPEAT ===

MEASURED 2026-08-21, on the operator's own store: the automatic session-end reflect
rail has produced ZERO candidates in its entire history. Not zero since a regression —
zero ever, since 2026-06-20. All 444 candidates carry `model_used='agent-assisted'`,
the resident route; not one was produced by the operator-model path. The cause is
mundane (`config.model` is an Anthropic id, the operator holds no Console API key, and
`litellm.completion` raises `AuthenticationError`), and it stayed invisible for two
months because `reflect_on_session` catches the exception, increments `result["errors"]`,
logs, and lets teardown proceed — while `check_maintenance_rails` reported the rail
GREEN, because it keys on `knowledge_candidates.origin` and the resident route was
filling that table the whole time.

So: A BOUND RAIL THAT FAILS MUST LEAVE A TRACE. Every run here records a liveness
marker — success AND failure, with the reason — through the same `extension_state`
mechanism the hooks use. An unattended custodian that fails silently is strictly worse
than no custodian, because it reports success while doing nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

#: Marker key prefix, mirroring `hooks/_liveness.py`'s `hook:<name>:*` convention so
#: `techne doctor` reads one vocabulary rather than two.
MARKER_PREFIX = "rail"


@dataclass(frozen=True)
class Rail:
    """One emit→ingest rail, adapted to a uniform shape.

    THE ADAPTER EXISTS BECAUSE THE RAILS DO NOT AGREE ON KEY NAMES, and that is fine —
    they were written independently and their own callers depend on those names. What
    they DO agree on is the shape: emit renders prompts, something answers them, ingest
    takes the answer plus the item's identifying fields. The `_<rail>_emit` /
    `_<rail>_ingest` adapters below do the translating; NO RAIL IS MODIFIED to suit
    this module, which is what keeps the resident path provably unchanged.

    `emit` must return a LIST of items each carrying a "prompt" key — `claims_emit`
    returns a single dict and is normalised at the adapter, not here.
    `ingest` takes (config, item, response, model_used) and returns a counts dict.
    `model_used` LABELS THE PRODUCER on any row the rail writes — see run_rail.
    """

    name: str
    emit: Callable
    ingest: Callable
    #: Human-readable note for `techne custodian status`, so an operator can tell what
    #: they are switching on without reading the source.
    what: str


def _claims_emit(config):
    """Normalise claims_emit's single-dict return into the list shape.

    `memory_present=False` means there is no curated memory for this project — nothing
    to reconcile, which is a legitimate no-op and must not read as a failure. Returning
    an empty batch says exactly that.
    """
    from techne.core.memory_claims import claims_emit

    report = claims_emit(config)
    if not report.get("memory_present") or not report.get("prompt"):
        return []
    return [{"prompt": report["prompt"], "prompt_hash": report.get("prompt_hash", "")}]


def _claims_ingest(config, item, response, model_used):
    from techne.core.memory_claims import claims_ingest

    # claims_ingest returns an EPHEMERAL report and persists nothing, so there is no
    # row to label — the parameter is accepted for a uniform adapter signature only.
    return claims_ingest(config, item.get("prompt_hash", ""), response)


def _consolidate_emit(config):
    from techne.core.sleepdream import consolidate_emit

    return consolidate_emit(config)


def _consolidate_ingest(config, item, response, model_used):
    from techne.core.sleepdream import consolidate_ingest

    return consolidate_ingest(
        config, item["target_ids"], item["cluster_hash"], response, model_used=model_used
    )


def _dream_emit(config):
    from techne.core.sleepdream import dream_emit

    return dream_emit(config)


def _dream_ingest(config, item, response, model_used):
    from techne.core.sleepdream import dream_ingest

    return dream_ingest(
        config, item["group_ids"], item["group_hash"], response, model_used=model_used
    )


def _faithfulness_emit(config):
    from techne.core.faithfulness import faithfulness_emit

    return faithfulness_emit(config)


def _faithfulness_ingest(config, item, response, model_used):
    from techne.core.faithfulness import faithfulness_ingest

    # Annotates an EXISTING candidate rather than writing one, so it has no
    # model_used column of its own to set.
    return faithfulness_ingest(config, item["candidate_id"], response)


def _conflict_emit(config):
    from techne.core.conflict import conflict_check_emit

    return conflict_check_emit(config)


def _conflict_ingest(config, item, response, model_used):
    from techne.core.conflict import conflict_check_ingest

    return conflict_check_ingest(
        config,
        item["focus_id"],
        item["comparison_ids"],
        item["check_hash"],
        response,
        model_used=model_used,
    )


#: The rails this module can drive. A registry rather than a chain of ifs, so adding a
#: sixth rail is one entry and `techne custodian status` can enumerate them.
#:
#: IMPORTS ARE LOCAL TO EACH ADAPTER, not at module scope. `core.conflict` and
#: `core.sleepdream` pull in the knowledge/database stack; binding all five at import
#: time would make `import custodian` expensive for the CLI, which imports it merely to
#: render `custodian status`.
RAILS: dict[str, Rail] = {
    "consolidate": Rail(
        name="consolidate",
        what="dedup — propose merges for near-duplicate knowledge clusters",
        emit=_consolidate_emit,
        ingest=_consolidate_ingest,
    ),
    "dream": Rail(
        name="dream",
        what="recombine — surface non-obvious A+B→C connections as low-confidence candidates",
        emit=_dream_emit,
        ingest=_dream_ingest,
    ),
    "faithfulness": Rail(
        name="faithfulness",
        what="check that a merge/supersede preserves each source's load-bearing claims",
        emit=_faithfulness_emit,
        ingest=_faithfulness_ingest,
    ),
    "conflict": Rail(
        name="conflict",
        what="contradiction detection → supersession candidates",
        emit=_conflict_emit,
        ingest=_conflict_ingest,
    ),
    "memory_claims": Rail(
        name="memory_claims",
        what="evidence-check curated memory against the observation ledger",
        emit=_claims_emit,
        ingest=_claims_ingest,
    ),
}


def effective_model(config, *, rail: str | None = None) -> str:
    """The model id this rail would use, or "" when unbound.

    Split out from `resolve_generate_fn` because the runner needs the NAME as well as
    the callable: it labels each written candidate with the producer that judged it
    (techne#555). Deriving the name twice would let the label and the caller drift.
    """
    if rail and rail in config.custodian.rail_models:
        if config.custodian.rail_models[rail]:
            return config.custodian.rail_models[rail]
    return config.custodian.model or ""


def resolve_generate_fn(config, *, rail: str | None = None):
    """Build the bound model callable, or None when this instance has no binding.

    RESOLUTION ORDER, most specific first: the per-rail override, then
    `custodian.model`, then nothing. It deliberately DOES NOT fall back to
    `config.model`.

    That last part is the whole lesson of #555 and is worth stating plainly: falling
    back to `config.model` is exactly what the automatic reflect rail does, and it is
    why that rail has been failing since June. `config.model` is the SESSION-WIDE
    default (an Anthropic id on this operator's box) and inheriting it silently binds
    the custodian to a credential plane the operator may not hold. An unset custodian
    must be UNBOUND — which reads as dormant and runs nothing — rather than bound to a
    model it cannot reach, which reads as broken only if someone is watching the logs.
    """
    model = effective_model(config, rail=rail)
    if not model:
        return None

    import copy

    from techne.core.thread_orchestrator import _make_generate_fn

    scoped = copy.copy(config)
    scoped.model = model
    # THE TIMEOUT IS THE POINT OF BINDING HEADLESSLY AT ALL. Without it the runner has
    # no upper bound: `run_rail` catches exceptions per item, but a call that never
    # returns is not an exception, and a scheduled custodian would hang rather than
    # report. Set on the scoped copy so no other consumer of `config` inherits it.
    scoped.llm_timeout = config.custodian.timeout_seconds
    return _make_generate_fn(scoped)


def record_rail_marker(config, rail: str, *, outcome: str, detail: str = "") -> None:
    """Record what a rail run actually did, success or failure.

    BEST-EFFORT AND NEVER RAISES, for the same reason `hooks/_liveness.py` never does:
    bookkeeping must not be able to fail the work it describes. But unlike a hook, a
    custodian run that loses its marker is genuinely worse off — the run is unattended,
    so the marker is the ONLY evidence it happened at all. Hence the warning log on the
    failure path rather than a bare swallow.
    """
    from datetime import datetime, timezone

    from techne.core.database import connect
    from techne.core.extension_state import set_state

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "outcome": outcome,
        "detail": detail,
    }
    try:
        conn = connect(config.db_path)
        try:
            set_state(conn, f"{MARKER_PREFIX}:{rail}:last_run", payload)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.warning("custodian: could not record marker for rail %s", rail, exc_info=True)


def run_rail(config, rail_name: str, generate_fn=None, *, limit: int | None = None) -> dict:
    """Emit one rail's prompts, answer them with the bound model, ingest the answers.

    Returns ``{rail, emitted, answered, failed, counts, bound, error}``. NEVER RAISES —
    an unattended run must not die on one bad prompt, and the counts are the report.

    ONE FAILED PROMPT DOES NOT ABORT THE RAIL. A model that refuses or times out on a
    single cluster should not cost the other nineteen; each item is attempted, failures
    are counted, and the run reports both numbers. `answered` and `failed` are reported
    separately for the reason the whole issue exists: a rail that emitted 20 prompts and
    answered 0 must not be summarised as "ran".
    """
    rail = RAILS.get(rail_name)
    if rail is None:
        raise KeyError(f"unknown rail: {rail_name!r} (known: {', '.join(sorted(RAILS))})")

    result = {
        "rail": rail_name,
        "emitted": 0,
        "answered": 0,
        "failed": 0,
        "counts": {},
        "bound": generate_fn is not None,
        "error": "",
    }

    if generate_fn is None:
        generate_fn = resolve_generate_fn(config, rail=rail_name)
        result["bound"] = generate_fn is not None
    if generate_fn is None:
        # UNBOUND IS NOT AN ERROR. It is the default, and it means "this rail is
        # answered by a resident agent" — the behaviour that has always worked.
        result["error"] = "no model bound — rail stays resident (set custodian.model)"
        return result

    # THE PRODUCER LABEL, and it is not cosmetic. `model_used` is the ONLY column that
    # records which producer judged a candidate, and the entire techne#555 finding turned
    # on it: the automatic reflect rail was invisible for two months precisely because
    # nothing distinguished its output from the resident route's. A headless run that
    # wrote rows labelled 'agent-assisted' would recreate that blindness for all five
    # rails at once — this feature would have manufactured the very hole it exists to fix.
    producer = effective_model(config, rail=rail_name) or "custodian"

    try:
        batch = rail.emit(config)
    except Exception as e:  # noqa: BLE001
        log.exception("custodian: emit failed for rail %s", rail_name)
        result["error"] = f"emit failed: {type(e).__name__}: {e}"
        record_rail_marker(config, rail_name, outcome="error", detail=result["error"])
        return result

    if limit is not None:
        batch = batch[:limit]
    result["emitted"] = len(batch)

    if not batch:
        # Nothing to do is a real outcome and is recorded as one — `noop:` names it,
        # matching the hook vocabulary, so "ran and found nothing" stays distinct from
        # "did not run".
        record_rail_marker(config, rail_name, outcome="noop:nothing-to-do")
        return result

    totals: dict[str, int] = {}
    first_error = ""
    for item in batch:
        try:
            response = generate_fn(item["prompt"])
            counts = rail.ingest(config, item, response, producer)
        except Exception as e:  # noqa: BLE001
            log.exception("custodian: rail %s failed on one item", rail_name)
            result["failed"] += 1
            if not first_error:
                first_error = f"{type(e).__name__}: {e}"
            continue
        result["answered"] += 1
        for k, v in (counts or {}).items():
            if isinstance(v, int):
                totals[k] = totals.get(k, 0) + v

    result["counts"] = totals
    if first_error:
        result["error"] = first_error

    # THE MARKER MUST DISTINGUISH THESE THREE, because the whole #555 finding is that
    # they were indistinguishable: answered everything / answered some / answered none.
    if result["answered"] == 0:
        outcome = "error"
        detail = f"emitted {result['emitted']}, answered 0 — {first_error or 'all items failed'}"
    elif result["failed"]:
        outcome = "partial"
        detail = f"answered {result['answered']}/{result['emitted']} — first error: {first_error}"
    else:
        outcome = "ok"
        detail = f"answered {result['answered']}/{result['emitted']}"
    record_rail_marker(config, rail_name, outcome=outcome, detail=detail)
    return result


def run_custodian(
    config, rails: list[str] | None = None, generate_fn=None, *, limit: int | None = None
) -> dict:
    """Run every configured rail. The headless entry point.

    Rail selection: explicit *rails* argument, else `custodian.rails` from config, else
    every rail in the registry. Returns ``{rails: [<run_rail result>, ...], bound: bool}``.

    DELIBERATELY NOT SCHEDULED FROM HERE. This function is the thing a scheduler calls;
    installing the schedule is a separate, deliberate act by the operator, because
    switching it on means spending money (or GPU) on a timer with nobody watching.
    """
    selected = rails if rails is not None else (config.custodian.rails or sorted(RAILS))
    unknown = [r for r in selected if r not in RAILS]
    if unknown:
        raise KeyError(f"unknown rail(s): {', '.join(unknown)} (known: {', '.join(sorted(RAILS))})")

    runs = [run_rail(config, name, generate_fn, limit=limit) for name in selected]
    return {"rails": runs, "bound": any(r["bound"] for r in runs)}
