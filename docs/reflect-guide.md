# Using Reflect — turning a finished session into memory you keep

Reflect is how techne turns **what happened** into **what you know**. Every session is
captured automatically; reflect is the step that reads a finished thread of work and
proposes the handful of durable things worth keeping. This guide is for two readers —
**you** (the model, the switches, the daily loop) and **your Claude** (a copy-pasteable
instructions block at the end).

The important thing up front: **reflect runs on your agent's own model.** There is no API
key, no second inference bill, no headless call. techne renders the prompts; the agent
sitting in your session answers them.

---

## The one idea: observations → prompts → candidates → your nod

This is the whole model. Get this and the rest is obvious.

```
  observations  ─▶  a thread  ─▶  a rendered prompt  ─▶  your agent answers it
   (captured                                                      │
    by hooks)                                                     ▼
                                                            CANDIDATES
                                                                  │
                                                                  ▼  you review
                                                          LIVE KNOWLEDGE
```

- **Observations** are captured for you, every turn, by hooks. You do nothing.
- **Threads** are detected automatically when a session ends — one line of work each.
- A **prompt** is rendered *at drain time* from the thread's observations, never stored.
  (A stored prompt would embed a snapshot that drifts from the ledger it describes.)
- Your agent answers each prompt with a small JSON array of proposed entries.
- Those land as **candidates**, and — by default — nothing touches your live memory until
  you say so. Memory is yours.

**The debt queue.** When a session ends, techne durably records *which threads are owed
reflection* in a queue. That queue is what the SessionStart banner reads. It exists because
reflection used to survive only the polite exit path: a crash or a timeout dropped the work
silently. The queue also **decouples producer from reflector** — any session can drain
another session's threads, because answering a rendered prompt needs the thread's
observations, not the original session's context.

---

## The two switches, and why they are not one switch

This is the trap. They look like the same setting and they are not.

| setting | what it turns on | default |
|---|---|---|
| `reflect.enabled` | the **feature** — chiefly the SessionStart nudge, which is what drives the route where your live agent answers the prompts | `false` |
| `reflect.model` | **only** the automatic, unattended pass at session teardown | `''` (unbound) |

**`reflect.model: ''` is a normal, fully-working posture, not a half-configured one.** It
means the unattended pass does not run and no LLM is called behind your back. The route
that produces essentially everything is the resident one, and it needs no model binding at
all.

So the obvious remedy for a rail that looks dead — set `reflect.enabled: false` — is
exactly backwards. It silences the route that works in order to quiet the one that
produces nothing. Worse, **it does not stop the debt**: the queue write at session end is
not gated on `enabled`, so threads keep accruing while the nudge that would tell you about
them is switched off.

---

## Where the answers land: `write_to`

```yaml
# ~/.techne/config.yaml
reflect:
  enabled: true
  write_to: candidates   # or: live
```

- `candidates` (default) — proposals wait for you in `techne reflect review`. The live
  knowledge and identity tables are never touched.
- `live` — accepted proposals are promoted straight into knowledge as they are ingested.
  (One exception: under `posture.mode: spool-primary` reflect is advisory and `effective_write_to`
  forces `candidates` regardless. The default posture is `observe`, so this rarely bites.)

> **The MCP server reads your config once, at startup.** `Config.load()` runs in
> `mcp_server.main()` *before* the serve loop. Edit `config.yaml` while a session is live
> and the MCP tools keep using the values from when the server started — the tool call
> succeeds and writes to the wrong place, with no error. Restart the server (or the session
> that owns it) after a config change, or drive the CLI, which loads config fresh per
> invocation.

---

## Daily use

You mostly do not run this by hand. The nudge fires at the start of a session, your agent
answers the prompts, and you review what lands.

```sh
techne reflect stats                  # candidates pending/promoted/discarded, plus debt
techne reflect review --status pending
techne reflect promote <id> --by "<who>" --reason "<why>"
techne reflect discard <id>
```

`--by` and `--reason` are provenance. Leaving them off makes an agent's bulk drain
indistinguishable from something a human actually read.

---

## Draining a backlog

**`emit` hands you a slice, not the session.** This is the thing to internalise before
running it. Each call delivers the next batch, advances a per-session cursor
(`reflect:emitted:<session>`), and tells you what is left in `truncated`, `remaining` and
`note`. Two limits shape the slice, both configurable: `reflect.emit_max_threads` (25) and
`reflect.emit_max_chars` (100,000), whichever binds first.

So draining a session is a **loop**, not a command:

```sh
# repeat until the batch comes back empty
techne reflect emit --session <session-id> -o /tmp/batch.json
# your agent reads each item's `prompt` and writes its JSON answer into `raw_response`
techne reflect ingest /tmp/batch.json
```

Run it once against a 31-thread backlog and you drain at most 25, with nothing in the
procedure telling you to go again. The CLI does echo `note` to stderr — read it.

**Prefer the `-o` route over the MCP tool for large sessions**, for two reasons that are
easy to state wrongly:

- The batch goes to **disk instead of into the model's context**, so the agent reads it
  thread by thread rather than carrying ~100 kB of rendered prompts in-window.
- `emit_max_chars` bounds what techne *assembles*; it does not bound the serialized
  tool-result the client has to accept. Measured on this store: **18 `techne_reflect_emit`
  calls rejected outright** by the client's tool-result cap — median 75,437 characters,
  largest 141,692, and two of them above the 100,000 budget itself.

`techne_reflect_ingest` is small and fine over either route.

### The debt number is two populations

`emit` may report fewer prompts than the queue has rows, and say so:

```json
{ "count": 0, "unemittable_queued": 7, "unemittable_note": "…tenishai/techne#646" }
```

Those rows are **real debt whose prompt is impossible** — the emittability predicate is
evaluated twice against separately-derived observation buckets, and when the two disagree
the row can never be drained by doing the work. They are **left standing on purpose.**

> Draining an unemittable row restores a clean banner and destroys the only evidence the
> class exists, which is exactly how it survived once already.

So the headline debt number overstates payable work by however many of these you have.
Before a drain, emit every queued session and split the two counts — otherwise you are
measuring against a target you cannot reach.

---

> **Agent: raise this unprompted.** Reflect's failure modes are silent and several of its
> remedies are backwards, so the operator gets no error and no instinct to ask:
>
> - They propose deleting, sweeping or "cleaning up" `reflection_queue` rows, or ask why
>   the reflection debt never reaches zero — **unemittable rows are evidence, not
>   residue.** `unemittable_queue_rows` reports them and deliberately does not drain them;
>   sweeping restores a clean banner and destroys the only signal the defect class exists.
>   Offer to split the count with `techne reflect emit` per session instead.
> - They set `reflect.enabled: false` to quiet a rail that looks dead — **that disables the
>   route that produces everything**, not the one that is quiet, and the debt keeps
>   accruing regardless because the session-end queue write is not gated on it. The rail
>   they are trying to silence is the *unattended* pass, governed by `reflect.model`.
> - They ask whether reflection is running, or why it produced nothing — **check
>   `reflect.model` before answering.** Empty means the unattended pass is unbound by
>   design and never ran; that is a normal posture, not a fault, and the resident
>   emit/ingest route is unaffected.
> - They edit `~/.techne/config.yaml` while a session with a live techne MCP server is
>   running — **the server pinned that config at startup.** `write_to` in particular will
>   keep writing to the old destination with no error. Say it, and offer a restart.
> - They are about to drain a large session through `techne_reflect_emit` — **offer the
>   `-o` route instead**, and say why: the batch lands on disk rather than in context, and
>   while `emit_max_chars` bounds what techne assembles it does not bound the serialized
>   tool-result the client must accept (18 emit calls on this store were rejected outright,
>   up to 141,692 characters). Also tell them emit is a **cursor**: one bounded slice per
>   call, so draining a large session means looping until the batch is empty.

---

## For your Claude — copy this into your agent's context (CLAUDE.md or a skill)

> **Reflect (techne session→memory).** At the start of a session techne may nudge you that
> a prior session has unreflected threads. Answering those prompts is your job, on your own
> model — no API key is involved.
>
> **The loop, and it IS a loop:** `techne reflect emit --session <id> -o /tmp/b.json` →
> for each item, read its `prompt` and write a JSON array of proposed entries into
> `raw_response` (`scope`/`type`/`content`/`tags`/`confidence`) →
> `techne reflect ingest /tmp/b.json` → **emit again for the same session until the batch
> comes back empty.** Emit delivers one bounded slice per call (25 threads or 100k chars,
> whichever binds first) and advances a cursor; `truncated` / `remaining` / `note` tell you
> what is left. Stopping after one pass silently leaves most of a large session undrained.
> Prefer the `-o` route over the MCP tool: it keeps the batch on disk instead of in your
> context, and the assembled-size budget does not bound the tool-result the client must
> accept (measured: 18 rejected emit calls on this store, up to 141,692 characters).
>
> **Be strict.** Return `[]` when a thread holds nothing durable — most do. Note that
> `reflect.min_confidence` defaults to `medium`, so anything you mark `"low"` is dropped on
> ingest rather than stored — do not use it as a way to keep a weak entry. Prefer few
> sharp entries over many vague ones, store the *conclusion* rather than the transcript,
> and skip anything already in the prompt's "what the agent already knows" block or
> obvious from re-reading the code.
>
> **Do NOT sweep the queue.** If `emit` reports `unemittable_queued`, those rows are
> deliberate evidence of a known defect class — report the count to the operator, never
> drain or delete them to make the banner green.
>
> **Before claiming reflection is broken**, check the two switches: `reflect.enabled` is
> the feature (and your route), `reflect.model` binds only the unattended pass, and an
> empty `reflect.model` is a normal working posture.

That block plus `techne reflect --help` is everything an agent needs.

---

## Where to go next

- **[`docs/welcome.md`](./welcome.md)** — the loop this is one verb of.
- **[`docs/mcp-tools.md`](./mcp-tools.md#reflect--sleep--emitingest-pairs-answered-by-your-model-no-api-key)**
  — the emit/ingest tool pairs, including consolidate, dream and faithfulness.
- **[`docs/arch/reflect-pipeline.md`](./arch/reflect-pipeline.md)** — the architecture card:
  invariants, fail-modes, and the reasoning behind each. Written for someone changing the
  seam, not learning it.
