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
architecture) run fine on a small model; judgment passes deserve a better one; work the
operator is present for should stay resident and cost nothing.

MEASURE THAT TIERING ON THE INFERENCE HOST, NOT THE DEV BOX. `llm_api_base` points at
wherever the model actually runs (a LAN ollama host, a proxy), and the difference is not
a detail: the same real `conflict` prompt answers in 11.6s on a 9B served properly and
had not returned after 240s on a laptop CPU. A latency number taken on a machine that
cannot serve a model is not evidence about the model — measuring that way once already
produced a confidently wrong claim about this exact tiering.

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
from pathlib import Path
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
    #: Optional: what this rail RESOLVED for the run, recorded in the marker.
    #: Exists because "found nothing" and "looked in the wrong place" are the same
    #: empty batch (Prudence, #709). Only `memory_claims` needs it today — it resolves
    #: its project from ambient cwd, which headless has no reason to get right.
    context: Callable | None = None
    #: Optional: a rail-specific `noop:<reason>` for an empty batch, so the
    #: discriminator lives in the MACHINE-READABLE field rather than in free text.
    empty_outcome: Callable | None = None


def _claims_slug(config) -> str:
    """Which project this rail will resolve, from ambient cwd.

    SURFACED BECAUSE HEADLESS HAS NO REASON TO GET IT RIGHT (Prudence, #709).
    `claims_emit` falls through to `Path.cwd()` when given no `cwd`, and a scheduled
    run's cwd is whatever the scheduler hands it. When the slug does not match a
    project with curated memory, the batch is empty and the rail records
    `noop` — indistinguishable from the healthy "nothing to reconcile" case.
    Measured: from a non-project cwd the rail made ZERO model calls and the liveness
    check still reported LIVE, which is techne#555's own shape reproduced inside the
    check written to close it.

    This does not FIX the resolution — binding the project properly is a design
    question about where a headless run learns its project, and it is not this PR's
    to answer. It makes the ambiguity VISIBLE, which is the part that must not wait.
    """
    try:
        from techne.platforms.claude_code.paths import memory_project_slug

        return memory_project_slug(Path.cwd())
    except Exception:  # noqa: BLE001
        return "unknown"


def _claims_emit(config):
    """Normalise claims_emit's single-dict return into the list shape.

    `memory_present=False` means there is no curated memory for THE RESOLVED PROJECT —
    which is a legitimate no-op when that project is the one you meant, and a silent
    misfire when it is not. The rail cannot tell those apart, so it reports which
    project it resolved (see `_claims_slug`) rather than pretending the distinction
    does not exist.
    """
    from techne.core.memory_claims import claims_emit

    report = claims_emit(config)
    if not report.get("memory_present") or not report.get("prompt"):
        return []
    return [{"prompt": report["prompt"], "prompt_hash": report.get("prompt_hash", "")}]


def _claims_empty_outcome(config) -> str:
    """Why THIS rail's batch was empty, as a machine-readable outcome.

    `outcome` is the field the liveness check keys on; `detail` is free text. Putting
    the discriminator only in `detail` made the ambiguity visible to a READER and not
    to a PROGRAM, so the follow-up design work would have had to parse prose to act on
    it. `noop:no-memory-for-project` costs nothing today — every `noop:*` still maps to
    LIVE — and gives that work something to key on. (Prudence, #709 round 2.)
    """
    return "noop:no-memory-for-project"


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
        context=_claims_slug,
        empty_outcome=_claims_empty_outcome,
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


def clear_rail_marker(config, rail: str) -> None:
    """Retract a rail's marker because the rail is no longer bound to a model.

    THE MARKER'S CONTRACT IS "the last model-bound run, and how it went". Once a rail
    has no binding there is no such run to describe, so the payload stops being a
    statement about now.

    THE ORIGINAL REASON GIVEN HERE HAS EXPIRED — READ THIS BEFORE DELETING THIS
    FUNCTION (techne#735). It used to say that leaving the payload in place makes
    `check_model_bound_rails` report a months-old failure as the CURRENT STATE, and
    that unbinding must return the rail to DORMANT "which the check reads off the
    ABSENCE of a marker". Both are now false: the check asks `_rail_binding` FIRST, so
    an unbound rail reads DORMANT off the BINDING, marker or no marker. Measured:

        unbound + outcome='error'  ->  dormant, ok=True

    So this retraction is no longer what makes the doctor honest, and a maintainer
    weighing "can it go?" must not weigh it on the sentence that used to be here.

    THE CONCLUSION SURVIVES ON A DIFFERENT REASON, and it is worth writing down because
    nobody had (Prudence, #743 r2). The retraction must be a DELETE rather than a fresh
    payload saying "unbound" — not because of what happens while unbound, but because
    of what happens when the operator RE-BINDS. `unbound` is not a recognised outcome,
    so it trips the #709 fail-closed arm the moment the rail is bound again. Measured:

        retracted-as-payload ('unbound'), then RE-BOUND  ->  unknown
        retracted-as-DELETE,              then RE-BOUND  ->  dormant

    The open question is whether the retraction should exist at all: the unbound path
    DELETES the payload while the disabled path KEEPS it, so "what did the last bound
    run do?" is answerable on one path and not the other. Deliberately not decided in
    techne#735 — it changes just-merged #732 behaviour.

    Best-effort and never raises, for the same reason as `record_rail_marker`: this is
    bookkeeping, and bookkeeping must not fail the teardown it runs inside.
    """
    from techne.core.database import connect
    from techne.core.extension_state import clear_state

    try:
        conn = connect(config.db_path)
        try:
            clear_state(conn, f"{MARKER_PREFIX}:{rail}:last_run")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.warning("custodian: could not clear marker for rail %s", rail, exc_info=True)


def _empty_outcome(rail, config) -> str:
    """The `noop:` reason for an empty batch — rail-specific when it can say, generic
    otherwise. Never raises; a diagnostic must not fail the run it describes.

    TAKES `config`, LIKE ITS SIBLING `context` HOOK. The two are advertised side by side
    on the `Rail` dataclass, so a hook author will reasonably write one that reads
    config — and passing None would raise AttributeError, be swallowed by the except
    below, and silently degrade to the generic reason. Losing the discriminator quietly
    is the exact failure this hook exists to prevent. (Prudence, #709 round 3.)
    """
    if rail.empty_outcome is None:
        return "noop:nothing-to-do"
    try:
        return rail.empty_outcome(config) or "noop:nothing-to-do"
    except Exception:  # noqa: BLE001
        log.warning("custodian: empty-outcome probe failed for %s", rail.name, exc_info=True)
        return "noop:nothing-to-do"


def effective_rails(config, rails: list[str] | None) -> list[str]:
    """Which rails run — THE ONE PLACE THAT DECIDES, matching `effective_limit`.

    Round 2 gave the COUNT axis one owner and left the SELECTION axis with two. The
    CLI's dry-run branch fell back to every registered rail while the runner honours
    `config.custodian.rails` — the documented way to choose what the headless runner
    drives. Measured with `custodian.rails = ["conflict"]` and no `--rail`: dry-run
    promised five rails' worth of prompts, the runner sent one.

    Quantitatively the larger over-report of the two (5 rails vs 1, against the cap's
    50 vs 5), and invisible to the round-2 guard test BY CONSTRUCTION: that test pins
    both paths to the same explicit `--rail`, so the one axis where they still diverged
    was the axis it held constant. (Prudence, #709 round 3.)

    THREE CASES, and the middle one is easy to collapse by accident:
      rails=None  → the configured selection, else every registered rail.
      rails=[]    → NOTHING. An explicit empty list is a decision, not an absence.
      rails=[...] → exactly those, validated.
    """
    # `rails is not None`, NOT `if rails` — AN EXPLICIT EMPTY LIST MEANS "RUN NOTHING".
    # Moving the selection logic here in round 3 silently flipped that: `rails=[]` went
    # from running zero rails to running every registered one. No in-tree caller reaches
    # it (the CLI passes `list(rail_names) or None`), so it was latent — but it is
    # fail-open in the TOKEN-SPENDING direction, which is the wrong way for a headless
    # runner to be wrong: a programmatic caller that filters its rail list down to empty
    # would have run the full set against the bound model. (Prudence, #709 round 4.)
    selected = rails if rails is not None else (config.custodian.rails or sorted(RAILS))
    unknown = [r for r in selected if r not in RAILS]
    if unknown:
        raise KeyError(
            f"unknown rail(s): {', '.join(unknown)} (known: {', '.join(sorted(RAILS))})"
        )
    return list(selected)


def effective_limit(config, limit: int | None) -> int:
    """How many prompts a rail may send this run — THE ONE PLACE THAT DECIDES.

    Three surfaces answer "how many prompts leave the box": `run_custodian`, `run_rail`,
    and the CLI's `--dry-run` branch. They were one function short of agreeing, and the
    one that disagreed was the ONLY surface an operator consults BEFORE wiring cron:
    dry-run called `rail.emit()` directly, bypassing both runners, and reported the
    uncapped batch length. Measured: cap 5, dry-run said 50, the real run answered 5 —
    and `--limit 3` was silently inert on a flag the same command accepts.

    It over-reported, so nobody would have overspent. But "shows exactly what WOULD be
    sent" is the docstring's own claim and it was false, which is worse than a wrong
    number: it is a number you would trust. (Prudence, #709 round 2.)

    Exported rather than re-derived per caller — re-copying
    `config.custodian.max_items_per_rail` into the CLI is exactly the ownership this
    consolidates.

    AND IT VALIDATES, because deciding WHICH number without deciding WHETHER THE NUMBER
    IS USABLE is only half an owner (Prudence, #709 round 3). The config path already
    rejects `0` and negatives at load; `--limit` reached the identical `batch[:limit]`
    slice with no guard at all, and both failures are silent:

      --limit -1  → `batch[:-1]`, i.e. ALL BUT THE LAST ITEM of every batch. "-1 means
                    no cap" is a common CLI idiom, so this is a reading an operator
                    would plausibly expect to be safe. Measured: emitted 49 of 50 — and
                    since round 2 made dry-run agree with the runner, dry-run now
                    CONFIRMS the wrong number rather than contradicting it.
      --limit 0   → a rail that never runs, records `noop:` (which maps to LIVE), and
                    exits 0 because the CLI's failure check is `if run["emitted"] and
                    run["answered"] == 0`. A cron line wired with `--limit 0` is a rail
                    that never runs, reports healthy, and goes green: techne#555's own
                    failure shape, reproduced through a flag on the command written to
                    close it.
    """
    if limit is None:
        return config.custodian.max_items_per_rail
    if limit < 1:
        raise ValueError(
            f"limit must be >= 1, got {limit}. 0 means the rail never runs — and it "
            "would still record a healthy outcome and exit 0. A negative value slices "
            "batch[:limit], silently dropping items from the end of every batch; it "
            "does NOT mean 'no cap'."
        )
    return limit


def _context_note(rail, config) -> str:
    """The rail's resolved-inputs note, as a marker suffix.

    NEVER RAISES. A diagnostic that can fail the run it describes is worse than no
    diagnostic — same rule as `record_rail_marker`.
    """
    if rail.context is None:
        return ""
    try:
        value = rail.context(config)
    except Exception:  # noqa: BLE001
        log.warning("custodian: context probe failed for rail %s", rail.name, exc_info=True)
        return ""
    return f" [resolved: {value}]" if value else ""


def run_rail(config, rail_name: str, generate_fn=None, *, limit: int | None = None) -> dict:
    """Emit one rail's prompts, answer them with the bound model, ingest the answers.

    Returns ``{rail, emitted, answered, failed, counts, bound, error}``. NEVER RAISES —
    an unattended run must not die on one bad prompt, and the counts are the report.

    THE CAP IS THE CALLER'S. `limit=None` here means NO cap — `run_custodian` resolves
    it through `effective_limit` before calling in. `run_rail` is exported and directly
    tested, so this is stated rather than left to be inferred from the one in-tree
    caller that happens to do it right. (Prudence, #709 round 2.)

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
    note = _context_note(rail, config)  # computed ONCE; every marker below reuses it

    try:
        batch = rail.emit(config)
    except Exception as e:  # noqa: BLE001
        log.exception("custodian: emit failed for rail %s", rail_name)
        result["error"] = f"emit failed: {type(e).__name__}: {e}"
        # WITH the context note: if emit RAISED, "which project was it looking at" is
        # arguably at its most useful, and this was the one outcome not getting it.
        record_rail_marker(
            config, rail_name, outcome="error", detail=f"{result['error']}{note}"
        )
        return result

    if limit is not None:
        batch = batch[:limit]
    result["emitted"] = len(batch)

    if not batch:
        # Nothing to do is a real outcome and is recorded as one — `noop:` names it,
        # matching the hook vocabulary, so "ran and found nothing" stays distinct from
        # "did not run". AND, when the rail can say what it resolved, WHAT it found
        # nothing FOR: an empty batch because the project resolved wrong looks
        # identical to an empty batch because there is genuinely nothing to do
        # (Prudence, #709).
        record_rail_marker(
            config,
            rail_name,
            outcome=_empty_outcome(rail, config),
            detail=note.strip(),
        )
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
        reason = first_error or "all items failed"
        detail = f"emitted {result['emitted']}, answered 0 — {reason}{note}"
    elif result["failed"]:
        outcome = "partial"
        detail = (
            f"answered {result['answered']}/{result['emitted']} — "
            f"first error: {first_error}{note}"
        )
    else:
        outcome = "ok"
        detail = f"answered {result['answered']}/{result['emitted']}{note}"
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
    # THE CAP IS HONOURED HERE, not only in the CLI (Prudence, #709). This function's
    # own docstring calls itself "the headless entry point" and "the thing a scheduler
    # calls" — and it was the one path that ignored `max_items_per_rail`, so an
    # operator who lowered it to bound unattended spend did not get what they set,
    # on exactly the entry point the documentation points them at. Measured before the
    # fix: max_items_per_rail=5 answered 50 of 50. Bounded in practice only because
    # each rail's own emit caps itself at 10-20, which is luck, not a guarantee.
    limit = effective_limit(config, limit)
    selected = effective_rails(config, rails)

    runs = [run_rail(config, name, generate_fn, limit=limit) for name in selected]
    return {"rails": runs, "bound": any(r["bound"] for r in runs)}
