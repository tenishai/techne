# Proposal: Outbox sinks — typed, registered, schema-validated delivery targets

**Status**: Draft.
**Author**: Raven (Opus 4.7) + Richard, paired session 2026-06-15.
**Tracking issue**: [#22](https://github.com/dimartin/techne/issues/22).
**Motivating consumer**: `cabal/docs/proposals/investigation-engine.md` PR-E (Archivist receiving sub-agent findings). The Archivist is the first sink; this proposal designs the substrate so future sinks (Slack courier, email, TUI notifier, etc.) plug in the same way.

---

## What exists today

`techne/sync/outbox.py` is a real durable queue:

- `outbox` table: `(id, target, data_json, content_hash, status, error_message, created_at, synced_at)`.
- `enqueue(target, data, content_hash?)` — INSERT OR IGNORE by `content_hash` (dedup); status `pending`.
- `flush(adapter)` — drains up to 100 pending rows in one batch, calls `adapter.push(entries)`, marks each entry `synced` or `dead` based on the `PushResult` contract.
- Statuses: `pending` → `synced` | `dead`. `dead` is terminal; `list_dead()` for inspection.
- One configured `SyncAdapter` consumes the *entire* queue regardless of `target`. The adapter is meant for **team backends** (filesystem, future remote sync), where `target` is a free-form string the adapter chooses how to interpret.

## The gap

Three properties the current shape doesn't have:

1. **Target validation.** `enqueue("archivist", data)` succeeds today even if nothing in the world knows how to consume that target. Rows accumulate forever. This is exactly the foot-gun that motivates this proposal — once we add a `target="archivist"` enqueue path, every misconfigured runtime starts piling up orphan deliveries.
2. **Payload schema.** `data_json` is a free-form dict. The consumer discovers a missing field at delivery time, not at enqueue time — far from the producer that made the mistake.
3. **Per-target delivery.** Today's single-adapter flush is fine for "sync everything to a team backend" but wrong for "deliver `archivist` payloads to the Archivist *and* `slack-courier` payloads to Slack." We need to dispatch by target.

The `SyncAdapter` abstraction stays as-is — it's the right shape for team-backend sync. Sinks are a parallel concept: **typed, in-process, per-target delivery handlers.**

---

## Design

### `OutboxSink`

```python
# techne/sync/sinks.py  (new module)

@dataclass(frozen=True)
class OutboxSink:
    name: str                                       # matches outbox.target column
    payload_schema: type                            # dataclass; required field shape
    handler: Callable[[dict], "DeliveryResult"]     # called with validated payload
    description: str = ""                           # human-readable; for `techne sinks list`


@dataclass
class DeliveryResult:
    """Returned by sink handlers; drives outbox status transition."""
    ok: bool
    error: str | None = None
    # If ok=True  → row marked synced
    # If ok=False → row stays pending; error stored on the row for diagnostics;
    #               retry on next flush.
    # Handler exceptions → caught by techne; treated as ok=False with the
    #               exception repr as error. Handler authors don't need to
    #               wrap every call in try/except.
```

### Registry

Module-global, process-scoped, mutable. Lives in `techne/sync/sinks.py`:

```python
_REGISTRY: dict[str, OutboxSink] = {}

def register_sink(sink: OutboxSink) -> None:
    """Register a sink. Idempotent on identical re-registration; raises on
    conflict (same name, different schema or handler)."""

def unregister_sink(name: str) -> None:
    """Remove a sink. Pending rows for this target stay queued — they'll
    be retried if the sink is re-registered, otherwise drained by a future
    `prune` step (see Migration)."""

def list_sinks() -> list[OutboxSink]:
    ...

def get_sink(name: str) -> OutboxSink | None:
    ...
```

Why module-global instead of dependency-injected: a sink is a process-wide fact ("agent X lives here"), not a per-call concern. The cabal startup wires sinks once; everything else queries the registry.

### Tightened `enqueue`

```python
def enqueue(db_path, target, data, content_hash=None, *, deliver=True):
    sink = get_sink(target)
    if sink is None:
        raise UnknownSinkError(
            f"No sink registered for target={target!r}. "
            f"Available: {[s.name for s in list_sinks()]}"
        )

    # Validate payload against the sink's declared schema.
    # The schema is a dataclass; we materialize it to surface type errors.
    try:
        sink.payload_schema(**data)
    except TypeError as e:
        raise InvalidPayloadError(target, str(e)) from e

    # ... existing INSERT OR IGNORE ...

    if deliver:
        _try_deliver_now(db_path, sink, inserted_id, data)
```

`_try_deliver_now` is the **sync-after-enqueue** path (transactional-outbox pattern):

```python
def _try_deliver_now(db_path, sink, row_id, data):
    """Best-effort immediate delivery. Failures leave the row pending
    for the next flush cycle."""
    try:
        result = sink.handler(data)
    except Exception as e:
        result = DeliveryResult(ok=False, error=repr(e))
    _update_row_status(db_path, row_id, result)
```

The producer that called `enqueue` sees nothing of this — the call is fire-and-forget from their POV. The outbox row is durable evidence the message exists; immediate delivery is an optimization for the happy path.

The `deliver=False` opt-out exists for tests and for bulk-import scenarios where you want to enqueue without firing handlers. Even with `deliver=False`, the target must be registered and the payload validated — no orphan rows can be created.

### Multiplexed flush

```python
def flush(db_path) -> dict[str, tuple[int, int]]:
    """Flush pending rows, grouped by target, to each sink's handler.

    Returns {target: (synced, failed)} for diagnostics.
    """
    conn = connect(db_path)
    rows_by_target = _load_pending_grouped_by_target(conn)

    summary = {}
    for target, rows in rows_by_target.items():
        sink = get_sink(target)
        if sink is None:
            # Sink unregistered while rows still pending. Leave for now;
            # the prune step decides whether to retire.
            summary[target] = (0, 0)
            continue

        synced = failed = 0
        for row in rows:
            try:
                result = sink.handler(json.loads(row["data_json"]))
            except Exception as e:
                result = DeliveryResult(ok=False, error=repr(e))

            if result.ok:
                _mark_synced(conn, row["id"])
                synced += 1
            else:
                _mark_pending_with_error(conn, row["id"], result.error)
                failed += 1
        summary[target] = (synced, failed)
    return summary
```

Note: this is **not** the same shape as `flush(adapter)` from today. Today's flush is for the team-backend `SyncAdapter`; that function stays. The two coexist:

- `flush_sinks(db_path)` — new; multiplexes pending rows to registered sinks.
- `flush(db_path, adapter)` — existing; pushes to the single configured team backend.

A row's `target` determines which path it takes. To avoid ambiguity:

- Sink targets MUST be registered before `enqueue` accepts them — see Migration for legacy rows.
- Adapter-bound targets (rows destined for `flush(adapter)` rather than a live handler) MUST also be registered. Their sink handler returns `DeliveryResult(ok=False)` so the row stays `pending`; `flush(adapter)` drains it on the next sync cycle. `lifecycle.py` bootstraps these registrations at startup via `_register_lifecycle_sinks()`.

This split means: existing filesystem-sync flows keep working unchanged. New per-target delivery is additive, and every enqueue path is validated.

### Transport-agnosticism

The sink handler is a Python callable. techne does not import or know about A2A, MCP, HTTP, or any specific transport. **What the handler does** is the consumer's choice:

- **In-process agent consumer** (current cabal Archivist): handler is a thin Python function that calls the agent's typed method directly, or wraps an in-process A2A client call.
- **Out-of-process agent consumer** (future remote Archivist): same handler shape; wraps an A2A HTTP client call.
- **Non-agent consumer** (hypothetical local file dumper): handler writes to disk.

The canonical pattern for cabal-side sinks is A2A delivery — sender names the receiver, fire-and-forget, receiver has its own pipeline. But that's a cabal convention, not a techne mandate.

### Schema discovery

Sinks declare their payload schema as a dataclass. Two reasons:

1. **Producer-side type checking.** `DepositFindingPayload(addressee="rook-primus", sender="investigator", raw_content="...")` fails at construction if a field is missing. The producer sees the error before `enqueue` is called.
2. **Server-side introspection.** `list_sinks()` returns the schema types; a future `techne sinks describe <name>` CLI command can render the expected payload shape for an operator debugging an integration.

Schemas are *not* JSON Schema — that would force a runtime dep and a second source of truth alongside the dataclass. The dataclass is canonical; serialization to JSON Schema can come later if external tooling needs it.

---

## Concrete API surface

### Public

```python
from techne.sync.sinks import (
    OutboxSink,
    DeliveryResult,
    register_sink,
    unregister_sink,
    list_sinks,
    get_sink,
    UnknownSinkError,
    InvalidPayloadError,
)
from techne.sync.outbox import (
    enqueue,         # existing, tightened
    flush_sinks,     # new
    flush,           # existing, unchanged shape
    pending_count,   # existing
    dead_count,      # existing
    list_dead,       # existing
    stats,           # existing
    prune_orphans,   # new — see Migration
)
```

### CLI

```
techne sinks list                      # name, description, schema field summary
techne sinks describe <name>           # full schema field list with types
techne outbox stats                    # existing; now also breaks down by target
techne outbox prune-orphans            # delete pending rows for unregistered targets
techne outbox flush-sinks              # manual trigger (the lifecycle hook is auto)
```

### Lifecycle integration

`flush_sinks` runs at the same hook points as the existing `flush`:

- `techne/hooks/session_start.py` — flush before the session begins.
- `techne/hooks/session_end.py` — flush as the session winds down.

No new lifecycle entry points. The transactional-outbox sync-after-enqueue path means most deliveries don't wait for a flush anyway; the flush is the retry mechanism.

---

## Migration

Existing `outbox` rows have arbitrary `target` strings — they predate sink validation. Two cases:

### Case 1: rows with targets a registered sink claims

These are normal pending rows. The first `flush_sinks` call picks them up and delivers.

### Case 2: rows with unregistered targets ("orphan rows")

Today's `NoopAdapter` returns `synced=0` for these (correct — nothing was actually delivered) but they remain pending. Two options:

- **Leave them pending.** If a sink is registered later for that target, they get delivered. Safe; no data loss.
- **Drain via `prune_orphans`.** Operator-invoked CLI command marks pending rows whose target isn't currently registered as `dead` with error `"no sink registered for target"`. Useful when a target name was changed and the old rows are now junk.

Default behavior: leave pending. Operators must explicitly call `prune-orphans` to drain.

`enqueue` itself raises `UnknownSinkError` going forward, so no NEW orphans accumulate. Migration is only about the historical backlog.

---

## Tests

`techne/tests/test_sinks.py` (new):

- `register_sink` + `enqueue` + immediate delivery → row ends `synced`
- `enqueue` to unregistered target → `UnknownSinkError`, no row inserted
- `enqueue` with missing schema field → `InvalidPayloadError`, no row inserted
- Handler returns `ok=False` → row stays `pending` with error_message set; next `flush_sinks` retries
- Handler raises exception → caught; row stays `pending` with exception repr as error
- Re-register same sink name with identical schema/handler → no-op (idempotent)
- Re-register same sink name with different handler → raises (conflict)
- `flush_sinks` multiplexes correctly: two sinks, two batches of pending rows, each goes to the right handler
- `prune_orphans` only touches pending rows for unregistered targets; doesn't disturb registered-target pending rows
- `deliver=False` enqueue skips the immediate-delivery path; row stays pending until `flush_sinks` runs

`techne/tests/test_outbox.py` (existing, additive):

- After tightening, `enqueue(target="any-old-string", data={})` now raises `UnknownSinkError`
- Test fixtures register a `noop` sink with an empty schema where needed

---

## What this enables

The motivating consumer is cabal's `investigation-engine.md` PR-E. Once this lands, cabal can:

```python
# cabal/agents/personas/archivist.py (startup)
from techne.sync.sinks import OutboxSink, register_sink, DeliveryResult

@dataclass
class DepositFindingPayload:
    addressee: list[str]    # one or many; tag-based fan-out downstream
    sender: str             # subordinate role or persona name
    raw_content: str        # freeform sub-agent output
    parent_task: str        # links to the spawning task
    hints: dict | None = None  # optional pre-shaped fields; bypasses distill if complete

def deliver_to_archivist(payload: dict) -> DeliveryResult:
    """Sink handler — A2A delivery to the in-process Archivist agent."""
    try:
        a2a_client.send_message(
            target="archivist",
            method="deposit_finding",
            params=payload,
        )
        return DeliveryResult(ok=True)
    except A2AError as e:
        return DeliveryResult(ok=False, error=str(e))

register_sink(OutboxSink(
    name="archivist",
    payload_schema=DepositFindingPayload,
    handler=deliver_to_archivist,
    description="Sub-agent findings → distilled corpus entries via the Archivist",
))
```

Sub-agent producer code is then:

```python
# anywhere in cabal — sub-agent finishes, deposits via outbox
from techne.sync.outbox import enqueue

enqueue(
    db_path=techne_db,
    target="archivist",
    data={
        "addressee": ["rook-primus"],
        "sender": "investigator",
        "raw_content": "<finding>",
        "parent_task": task_id,
    },
)
# Returns immediately. Outbox row durable. Handler called sync-after-enqueue;
# if Archivist is down, retried on next flush_sinks. Producer doesn't care.
```

Future sinks follow the same shape — `slack-courier`, `email-out`, `tui-notifier`. Each registers one `OutboxSink`, owns one handler.

---

## Out of scope

- **A new MCP server for techne sink admin.** techne already has an MCP server; sink admin tools can land there in a follow-up if needed. Not blocking.
- **Per-sink retry/backoff policies.** Today's flush is "try again next time the hook fires." Per-sink backoff schedules, exponential delays, etc. — defer until a real sink demands it.
- **Schema versioning / migration.** If a sink's `payload_schema` changes shape, currently-pending rows for that sink fail validation on retry. For MVP: operator handles by `prune-orphans` + redeploy. Real schema migration is a v2 problem.
- **Distributed sinks / network transport in techne.** techne stays in-process. Handlers that need network transport own that themselves (the cabal A2A handler example above).
- **Sink dependencies / ordering between sinks.** Each sink is independent. If two sinks both need to fire for one logical event, the producer enqueues to both targets.

---

## Open design questions

### 1. Should `enqueue` raise or no-op on unregistered targets?

Current draft: raise `UnknownSinkError`. Forces the producer to find out at call-time.

Alternative: silent no-op + log warning. Lower friction for partially-configured deploys (e.g. tests that don't register every sink).

**Recommendation**: raise. Silent failures are exactly the foot-gun this proposal exists to eliminate. Tests can register a no-op sink as a fixture; the friction is appropriate.

### 2. Where does the sink registry live during testing?

The module-global `_REGISTRY` is fine for prod but makes test isolation harder. Test A registers a sink; test B sees it.

**Recommendation**: provide a `with use_registry(<dict>):` context manager that swaps the global for test-local state. Fixtures use it; prod code doesn't.

### 3. Should sink handlers be `async`?

The motivating consumer (Archivist A2A delivery) is sync today. But future sinks (HTTP-based, Slack) want async.

**Recommendation**: V1 sync only. Add an `async_handler` field on `OutboxSink` later, with the flush loop awaiting it where present. Don't pre-design.

### 4. Dedup window for `content_hash`

Today's index is `UNIQUE WHERE status = 'pending'` — same content can be re-enqueued once the prior copy is synced. For idempotency at the sink level (e.g. archivist shouldn't see the same finding twice even if the prior one was already promoted), the dedup window might want to extend.

**Recommendation**: leave dedup at the substrate as today. Sink-level idempotency is the handler's responsibility. The Archivist's own dedup pipeline (L0 hash + L1 same-resource overlap) handles this for free.

---

## References

- `techne/sync/outbox.py` — current outbox implementation
- `techne/sync/adapter.py` — `SyncAdapter` protocol (the parallel concept this proposal does NOT replace)
- `techne/core/database.py:94` — `CREATE TABLE outbox` schema
- `cabal/docs/proposals/investigation-engine.md` — motivating consumer (PR-E reframed as the sink consumer once this substrate lands)
