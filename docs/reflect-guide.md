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
techne reflect discard <id> --by "<who>" --reason "<why>"
```

**Keeping the claim, not the wrapper.** A candidate that states something durable but
carries counts, ids or versions that will go stale doesn't have to be discarded:

```sh
techne reflect promote <id> --content "<the durable version>" --by "<who>" --reason "<what you cut>"
```

The edited text is what lands in knowledge; the original proposal stays on the candidate, and
`techne reflect review` shows both. Only plain `add` candidates can be revised: a merge or
supersede was checked for faithfulness against its original text. `--content-file` reads the
text from a file. `techne reflect stats` counts revisions separately (`revised`).

`--by` and `--reason` are provenance. Leaving `--by` off records `unspecified`, so nobody can
tell afterwards whether a person or an agent's bulk drain made the call.

---

## Draining a backlog

**`emit` hands you a slice, not the session.** This is the thing to internalise before
running it. Each call delivers the next batch, advances a per-session cursor
(`reflect:emitted:<session>`) *when it delivered something*, and tells you what is left in
`truncated`, `remaining` and `note`. Two limits shape the slice, both configurable:
`reflect.emit_max_threads` (25) and `reflect.emit_max_chars` (45,000), whichever binds
first. The char budget is measured on the whole result as the MCP server sends it
(indent=2 JSON, notes included), not on the prompts alone (techne#993).

So draining a session is a **loop**, not a command:

```sh
# repeat until the batch comes back empty — OR comes back `oversized`
techne reflect emit --session <session-id> -o /tmp/batch.json
# your agent reads each item's `prompt` and writes its JSON answer into `raw_response`
techne reflect ingest /tmp/batch.json
```

Run it once against a 31-thread backlog and you drain at most 25, with nothing in the
procedure telling you to go again. The CLI does echo `note` to stderr — read it.

**Backfilling an import: run `techne import` first, and re-run it on an old store.** Emit tracks
what it has delivered by persisted thread id. Before techne#954, `techne import` wrote
observations but no threads, so every imported thread fell back to a key that changes whenever
knowledge changes: promote anything and the next emit re-offers threads you already answered.
`import` now persists threads. A store imported earlier has observations and no threads;
re-running `techne import` over the same transcripts fills them in without re-importing
anything.

**That repair re-offers, once, every thread you answered before it.** Those answers were stored
with no thread id, so nothing links them to the threads the repair persists, and the new ids read
as unpaid. Answer them again or decline them; the consolidate pass (`techne_consolidate_emit` /
`techne_consolidate_ingest` over MCP, or `techne custodian run --rail consolidate`) catches
duplicate candidates.
Upgrading before you backfill avoids it. (`--force` is different: it appends observations, so it
re-detects even a session that already has threads. That is correct, and already-paid threads
keep their payment.)

**Declining a thread is allowed, and it ends the loop rather than spinning it.** Leave an
item's `raw_response` blank and `ingest` skips it without draining: the debt stays owed, on
purpose. Since techne#901 that refusal is *recorded*, so `emit` offers the thread once and
then stops offering it — otherwise #900's re-offer and this decline chase each other and
"until empty" never arrives. The row stays visible as `declined` in `reconcile`; answer the
prompt you already hold whenever you want to pay it, and the record retires itself.

**Watch the placeholder, because it is the same literal.** `emit` writes
`raw_response: ""` into every item, so an item you simply have not filled yet reads as a
refusal once you run `ingest`. That is survivable — `techne reflect undecline --session
<id>` puts the thread back in the offer — but if you lose the batch file before noticing,
the debt is owed with nothing offering it until you retract.

**The one case where "until empty" never arrives.** A single thread whose rendered prompt
alone exceeds `emit_max_chars` sets `oversized`, and the cursor is deliberately *not*
advanced — so every further pass returns that same one item and the batch never empties.
Stop on `oversized` and say so: that thread cannot be drained until per-thread chunking
lands (the #269 follow-up), and looping on it just re-answers the same prompt forever.

**Read stderr, not just the file.** `-o` writes the *bare batch array* — `truncated`,
`remaining` and `oversized` live on the result object and never reach it. The CLI prints
`note` to **stderr**, and on an oversized batch that note says so explicitly. An agent that
reads only `/tmp/batch.json` cannot see any of the three signals this loop turns on.

**Prefer the `-o` route over the MCP tool for large sessions**, for two reasons:

- The batch goes to **disk instead of into the model's context**, so the agent reads it
  thread by thread rather than carrying tens of kB of rendered prompts in-window.
- **MCP results have a hard ceiling, and emit used to default above it.** The 14 emit calls
  this store had refused by the client's tool-result cap ran 56,974–98,755 characters —
  every one of them *inside* the old 100,000 budget, which was also measured on compact JSON
  while the server sends indent=2 (techne#993). The default is now 45,000, measured on the
  result as sent, about 20% under the smallest refusal. A refusal is not silent data loss (the
  harness spills the payload to a file and hands back the path), but it does cost you the
  round trip.

  What that sample can and cannot tell you is worth being precise about, because refusals
  are one-sided evidence. The smallest refusal bounds the ceiling **from above**: it is at
  or below 56,974 characters. Nothing here bounds it from *below* — that would take the
  largest result the client *accepted*, which this store does not record. So if refusals
  still happen, the honest advice is **set `emit_max_chars` under the smallest refusal you
  have observed, with margin**, rather than any particular number.

  And the budget is not an absolute bound in any case: the trim loop keeps the first item
  unconditionally, so a lone thread larger than the budget is still delivered whole.
  Lowering `emit_max_chars` does nothing for a single-item batch.

`techne_reflect_ingest` is small and fine over either route.

### The debt number is more than one population

`emit` may report fewer prompts than the queue has rows, and say so:

```json
{ "count": 0, "unemittable_queued": 7, "unemittable_superseded": 2,
  "unemittable_below_threshold": 5, "unemittable_note": "7 queued thread(s)… 2 superseded… 5 below…" }
```

Those rows are **real debt whose prompt is impossible**, and they come in two kinds. The
distinction decides what you should do about them, so it is worth keeping straight:

- **Superseded** — the queued thread id is not in the session's *current* detection at all.
  Thread ids derive from where boundaries fall, so a re-detection over a grown observation
  set can re-key a thread; the row then points at a thread that no longer exists. Nothing is
  unreflected: those observations are still covered by a current thread, under a different
  id. **Retire these** with `techne reflect reconcile` (dry by default, `--apply` to write).
- **Below threshold** — the id *is* current, but re-partitioning left the thread under
  `reflect.min_observations_per_thread`, so no prompt can render for it. **Leave these
  standing.** They are the population techne#646 exists to keep visible, and `reconcile`
  never deletes them. `reconcile --apply` *parks* them (techne#870): the row stays
  undrained and in the table, but the SessionStart banner stops counting it as owed and
  lists it on a separate line. A parked row un-parks by itself when its thread renders
  again (a lowered floor, or a re-detection that hands it observations back).

Two more populations are the opposite shape: their prompts render perfectly well, and
what separates them is what happens on the next `emit`.

- **Orphaned** — the id is current, above threshold, and would render today, but the emit
  cursor already counts it as delivered. The cursor advances when a batch is *rendered*, so
  a pass whose answers never came back (the common case: `techne_reflect_emit` exceeded the
  MCP token cap and spilled to a file) left the row owed and the cursor satisfied. The
  symptom is `emit` returning `count: 0` for a session the SessionStart banner still calls
  pending. **Do nothing.** Since techne#900 the next `emit` for that session detects the
  disagreement and re-offers those threads by itself; `reconcile` reports the count and
  never removes it. A nonzero `orphaned` that does not clear after an emit is a regression,
  not a chore.

- **Declined** — same shape as orphaned, opposite cause and opposite remedy. The prompt
  was offered, the agent came back, and it left `raw_response` blank. Nothing will
  re-offer it while the emit cursor claims the thread, by design (techne#901): a decline
  is a decision, and re-asking makes the drain loop spin forever. The row stays owed and visible for as long as it goes
  unanswered. **If the blank was an accident, retract it** — `emit` pre-fills
  `raw_response: ""` as its placeholder, so a partial fill and a refusal look identical
  in the file:

  ```sh
  techne reflect undecline --session <session-id>   # or --thread <id> for just one
  ```

- **Unpersisted** — a thread with no row in `threads`, so no queue row either: a session
  whose detection never ran, or one whose observations or detector moved on since it was
  saved. Since techne#1091 `emit` writes the missing rows (and their queue rows) before it
  renders, so its items carry a real `thread_id`; answers recorded under an `obs_key`,
  before or after that write, move to the new id on the next `emit` and are not offered
  again. An item still comes out with
  `thread_id: null` and an `obs_key` only if that write failed (emit logs a warning). Such a
  thread works like the others: the cursor records its answers under `obs_key`, the next
  empty `emit` re-offers one that was rendered and never answered, and `undecline` reaches
  its declines. Ingest it by passing `thread_id: ""` and the `obs_key`; over the MCP tool,
  leave out `obs_key` and its answer is not recorded.

`techne reflect reconcile` prints the split per session, which is the cheapest way to see
which kind you have:

```
c7d98cb5-b932-40  superseded=0  below_threshold=1  orphaned=0  declined=0
```

**A historical note, because the old explanation is still quoted in places.** techne#646
originally attributed all of this to the emittability predicate being "evaluated twice
against separately-derived observation buckets". That hypothesis was measured and refuted in
techne#793: the predicate is fine, and the two calls were running over *different partitions*
because `run_session_end` replaced the boundary table but not the thread table. The
"evidence, not residue" instruction still holds — but only for the below-threshold kind.
Blanket-draining is still wrong; retiring a provably-superseded row is not draining.

So until `reconcile --apply` has run, the headline debt number overstates payable work by
however many superseded and below-threshold rows you have. Before a drain, run `reconcile`
and read the split —
otherwise you are measuring against a target you cannot reach. Orphaned rows are the
exception: they *are* payable, which is why they self-heal rather than being retired.

---

> **Agent: raise this unprompted.** Reflect's failure modes are silent and several of its
> remedies are backwards, so the operator gets no error and no instinct to ask:
>
> - They propose deleting, sweeping or "cleaning up" `reflection_queue` rows, or ask why
>   the reflection debt never reaches zero — **offer `techne reflect reconcile`** (dry by
>   default) rather than a hand-rolled DELETE. It splits the undrainable rows into
>   *superseded* (thread gone from the current detection — safe to retire) and *below
>   threshold* (thread still current, under the observation floor). Retiring the first kind
>   is sanctioned; blanket-draining the second is not, because those rows are the only
>   visible evidence of a live defect class. Do not repeat techne#646's original "evaluated
>   twice" explanation — techne#793 measured and refuted it.
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
>   an MCP result the client refuses costs a round trip (lower `emit_max_chars` if refusals
>   are still happening).
> - They treat one `emit` call as having drained a session — **emit is a cursor**, not a
>   dump: one bounded slice per call, so draining means looping until the batch comes back
>   empty, or comes back `oversized`, which never empties and must stop instead.

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
> comes back empty, or comes back `oversized`.** Emit delivers one bounded slice per call
> (25 threads or 45k chars, whichever binds first) and advances a cursor;
> `truncated` / `remaining` / `note` tell you what is left. Stopping after one pass silently
> leaves most of a large session undrained — but `oversized` means a single thread exceeds
> the budget and the cursor will NOT advance, so looping re-answers that same prompt
> forever. On `oversized`, stop and tell the operator. Prefer the `-o` route over the MCP
> tool: it keeps the batch on disk instead of in your context, and an MCP emit result the
> client refuses costs a round trip. The `-o` file holds only the batch array — `truncated`, `remaining` and
> `oversized` do NOT appear in it; the CLI prints `note` to stderr, so read stderr too or
> you will never see the signal that stops the loop.
>
> **Be strict.** Return `[]` when a thread holds nothing durable — most do. Note that
> `reflect.min_confidence` defaults to `medium`, so anything you mark `"low"` is dropped on
> ingest rather than stored — do not use it as a way to keep a weak entry. Prefer few
> sharp entries over many vague ones, store the *conclusion* rather than the transcript,
> and skip anything already in the prompt's "what the agent already knows" block or
> obvious from re-reading the code.
>
> **Verify before you write `high`.** The observations in a prompt are an index, not
> evidence — the first slice of the thread, each line clipped. A `high` entry that names a
> number, an issue or PR, a state or a mechanism gets checked against the source first
> (`gh issue view`, `gh pr view`, `git log`, `sqlite3` on the store). If you cannot check
> it, write it at `medium` and say what is unconfirmed, or leave it out. This holds under
> either `write_to`: under `live` nothing reviews it after you, and under `candidates` a
> reviewer rarely has the evidence you had one command away.
>
> **Do NOT sweep the queue by hand — offer `techne reflect reconcile` instead.** If `emit`
> reports `unemittable_queued`, that count is TWO populations: rows whose thread is gone from
> the current detection (*superseded* — `reconcile` retires these, dry-run by default), and
> rows whose thread is still current but has fallen under the observation floor (*below
> threshold* — leave them, they are the only visible evidence of a known defect class).
> `reconcile` prints the split. Retiring a provably-superseded row is not the same act as
> draining an undrainable one, and a hand-rolled DELETE cannot tell them apart.
>
> **Before claiming reflection is broken**, check the two switches: `reflect.enabled` turns on
> the SessionStart nudge (your route), `reflect.model` binds only the unattended pass, and an
> empty `reflect.model` is a normal working posture. Disabling the feature does NOT stop debt
> accruing — the session-end enqueue is not gated on it — so a quiet banner is not an empty
> ledger.

That block plus `techne reflect --help` is everything an agent needs.

---

## Where to go next

- **[`docs/welcome.md`](./welcome.md)** — the loop this is one verb of.
- **[`docs/mcp-tools.md`](./mcp-tools.md#reflect--sleep--emitingest-pairs-answered-by-your-model-no-api-key)**
  — the emit/ingest tool pairs, including consolidate, dream and faithfulness.
- **[`docs/arch/reflect-pipeline.md`](https://github.com/tenishai/techne/blob/main/docs/arch/reflect-pipeline.md)** — the architecture card:
  invariants, fail-modes, and the reasoning behind each. Written for someone changing the
  seam, not learning it.
