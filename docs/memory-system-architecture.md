# The Memory System — Architecture

**Status:** design synthesis. Captures the decisions from the 2026-07-04/05 design
sessions (Rich + Poe). Mixes **[BUILT]**, **[EXISTS]**, and **[NORTH-STAR]** — read
the status tags; most of the federation layer is not built yet, and that's on purpose
(design against something concrete before building it).

---

## 1. Core principles

These are the invariants everything else falls out of.

1. **Memory sovereignty.** techne is the *agent's* memory, controlled by the agent.
   techne never autonomously ships, routes, or exports anything. It produces and
   remembers; the agent decides.
2. **Capture ≠ distillation.** Raw is captured cheaply and durably; distillation
   happens *after the fact*, out-of-band, by a separate distiller. Nothing waits on
   an LLM in the hot path.
3. **Backing ≠ sharing.** *Backing* is durability — nothing is lost, everything is
   re-derivable. *Sharing* (federation) is a separate, narrower thing. You can back
   raw without ever sharing it.
4. **Advise, don't deliver.** techne can tell the agent *what destinations exist* and
   *what hasn't been sent* — but the agent chooses the destination and performs the
   send. techne never holds a transport.
5. **Two planes.** **Memory** (sovereign, raw-private, distilled-federated) and
   **Telemetry** (typed operational signals, ops-shared). They never merge — the
   moment telemetry lets you read an agent's raw thinking, it's a surveillance log.

---

## 2. The pipeline (one shape, everywhere)

```
   raw in                3rd-party distiller              distilled
 (fast, durable,   ─────► (async, reusable = the ─────►  (backed AND
  private-scoped)         drain; not in hot path)         federated)
```

- **Raw** — observations/findings. Written fast, backed for durability, **scoped
  private**. Never a federated domain.
- **Distiller** — reads pending raw, produces durable lessons, marks raw processed
  (idempotent via a fingerprint/ledger). Runs on a cadence or on-demand, on whatever
  model. It is a **third role** — neither the producer's nor the store's synchronous
  job.
- **Distilled** — knowledge / patterns / code-graph. Backed, and the only thing that
  federates out to other agents.

Two things feed the same pipe; the only variable is *where the raw came from*:
1. **A self-distilling agent** (own techne) captures raw locally, distills locally,
   federates only the conclusions. Its raw never leaves.
2. **An ephemeral producer** (a sub-agent with no persistent techne) *deposits* raw to
   the Archivist. Intake is a fast durable write; the same distiller processes it
   asynchronously. The deposit doesn't block on distillation.

> **[BUILT]** The distiller is `drain.py` (Prudence, 2026-07-04): fire-up + close,
> idempotent via a `.drained.json` fingerprint, answers reflect prompts with
> `claude -p --setting-sources ''` (keychain OAuth, hooks off, no API key), gated so
> a never-export/noop agent distills nothing wastefully. It's parameterized by store,
> so it generalizes to "distiller reading a store it doesn't own."

---

## 3. techne is multi-domain, and a "role" is a config

**[EXISTS]** A single techne instance already hosts several stores side by side:

| Domain | Store | Query |
|---|---|---|
| core knowledge | `knowledge` | `techne_recall` / `techne_search` |
| architecture patterns | `architect.db` | `pattern_search` |
| code graph | `codex.db` | `codex_search` |
| raw capture | `observations` | (internal; feeds the distiller) |

A **role** is just *which domains a techne instance runs, and whether it's shared /
git-backed.* This is the unifying idea:

- **Working agent** (Prudence) = local techne; shared domains off or local-only; raw
  private.
- **Archivist role** = a git-backed, *shared* techne with the team-level domains on:
  team `knowledge` **+** patterns **+** code-graph. It **backs it all** (raw included,
  for durability) but **federates only the distilled**.
- **Librarian role** = a techne in a `static`, read-mostly reference role.

Pillars (Archivist / Architect / Librarian / Codex) become *roles a shared techne
plays* — or consumers of them — rather than four separate systems.

---

## 4. techne as the federator (opt-in, generic)

Rather than a separate federator service in front of three backends, **build
federation into techne** — it's already the memory MCP the agent talks to, and it
already owns the connection/adapter machinery. **[NORTH-STAR]**

Two rules keep this from turning techne into a god-object:

1. **Federation is opt-in, via configured connections.** Bare techne — no connections
   — is local-only (the "pure" thing; the default). Add a `team` or `static`
   connection and it federates. Same shape as the `noop` gate.
2. **techne knows generic *roles*, not personas.** It knows "a `team` store at address
   X over transport Y," never "the Archivist persona." Archivist/Librarian are
   *instances* of roles.

Behaviour:

- **Reads fan out and merge.** `recall(topic)` queries local + connected `team` +
  `static`, returns one ranked, deduped answer. *This composition is the whole value*
  — a proxy that just forwards would earn nothing. **The merge is not just dedup:**
  cross-store entries can *contradict* (my local "we switched to sqlite" vs. a stale
  team "use postgres"), and content-hash dedup cannot catch that. The conflict + rank
  rule is specified by the coherence layer — see
  [`proposals/coherence-layer.md`](proposals/coherence-layer.md) §4b; the fan-out
  merge calls the same contradiction-detection + decayed-ranking functions.
  **Concrete design:** [`proposals/federation-read-merge.md`](proposals/federation-read-merge.md)
  (#222) — connections config, the fan-out algorithm, and the read-only/candidate-staging
  sovereignty boundary.
- **Writes route by scope, explicitly.** `store(..., scope=local|team|static)`.
  Default `local` (techne). `team` hands off to the Archivist (who owns the git push —
  techne never touches git). Nothing leaks local→team unless the agent says so.

**Resolved (2026-07-05, [#220](https://github.com/tenishai/techne/issues/220)): techne↔techne — one schema everywhere, git-backed.**
`team` memory is the *same techne schema*, shared and git-backed; the Archivist is
"just techne with git-backing on." Rationale: the read-merge reuses the coherence
layer's pure conflict functions directly on team rows (same shape — one implementation,
two call sites); far less code (no per-store translation adapter); and it fits the
target scale — for solo/small-team at agent cadence the **git-sync model, not SQLite
size, is the ceiling**, and it's far off (a single techne instance is comfortable to
~hundreds-of-thousands of embedded rows; the shared-write concurrency wall is where a
Postgres archivist would be needed, which git handles fine for a handful of writers).
This does **not** burn the adapter bridge: a future foreign store (e.g. a Postgres
archivist rebuilt from the precursor) is normalized to techne schema at the connection
edge, keeping the core one-schema. The proven two-engine precursor (local SQLite +
foreign ClickHouse/AlloyDB team store) is deliberately simplified here for independent scale.

---

## 5. Export: a ledger, not an outbox duplicate

For exporting *knowledge*, the outbox would be a content duplicate of the `knowledge`
table + a status column. Instead, track export state with a thin **ledger**
**[NORTH-STAR]**:

```
knowledge_exports(knowledge_id, sink_name, pushed_at)   -- references, doesn't copy
```

- **Unpushed to sink X** = `knowledge WHERE superseded_by IS NULL AND id NOT IN (ledger for X)`.
- Push → insert `(id, X, now)`. Idempotent, **per-sink**, no content dup.
- Supersede/re-distill mints a new id → picked up next pass; old id stays marked. Same
  high-water-mark pattern as the drain fingerprint.

techne's export surface (advise, don't deliver) is three MCP tools:
`techne_sinks_list` (catalog) · `techne_unpushed(sink)` (ledger) ·
`techne_mark_pushed(id, sink)`. The agent reads the menu, **does the send itself**,
marks it pushed.

> **The noop is the sentinel.** **[EXISTS]** `lifecycle.py:22` gates enqueue on
> `config.sync.adapter != "noop"`; the sink lane self-gates (`enqueue` to an
> unregistered sink raises). With a ledger, the never-export case is *inherently*
> clean: no sink → empty ledger → nothing to push → zero orphan/duplicate rows.

> **[EXISTS but bypassed]** techne has a full outbox + sinks substrate
> (`techne/sync/{outbox,sinks,adapter}.py`, techne#22) with two lanes: `sync:` →
> `SyncAdapter` (direct-to-storage) and `sink:` → `OutboxSink` handler (to a
> consumer). Today distilled knowledge **never reaches the outbox** — the current
> `sync.sh` scrapes the `knowledge` table directly and pushes to git, reaching around
> the whole boundary. That direct push is **retired** under this design.

The outbox keeps its niche: producers that **aren't** the knowledge table (raw
deposits before distillation, thread summaries) — those genuinely need a queue.

---

## 6. Two topologies, identical techne behaviour

The invariant: **techne → (its chosen route) → the Archivist → GitHub.** techne never
touches GitHub. Only *where the Archivist runs* changes.

- **Topology A — cabal + thin personas.** The Archivist is an in-process cabal persona;
  a persona's techne routes to it; the Archivist owns the git push. **[EXISTS]** — cabal
  registers `sink:archivist` (`agents/personas/archivist.py`).
- **Topology B — Archivist graduated to a standalone agent.** No cabal. techne behaves
  *identically*; the Archivist is its own agent (own techne + git connector + an intake
  endpoint). **[NORTH-STAR]**

**The `standalone` flag is a lane selector** over the two existing outbox lanes:
- `standalone=false` → `sink:archivist` → the Archivist owns the push.
- `standalone=true` → `sync:<storage>` → a techne git adapter pushes directly (lone
  agent, no Archivist). Caveat: skips the Archivist's curation — fine for a
  content-addressed git corpus.

---

## 7. Discovery for graduated agents **[NORTH-STAR]**

Today an agent's location is recorded **nowhere**: cabal personas are in-process, and
`agent-card.json` has no `url`/`endpoint` field. Graduating to a standalone host opens
this gap.

- Add **`url` to the agent-card** (the A2A-standard anchor it's missing).
- Record a **stable name, not a raw IP** (DNS / reverse-proxy / tailscale) so the IP
  can move without churning discovery.
- Use a **git-backed catalog of cards** as the v1 registry — same pattern as the memory
  repo, no new always-on daemon. Promote to a live registry service only when
  membership gets dynamic.
- **Graduation ceremony:** assign a stable address → write `card.url` → publish the card
  to the catalog.

`limenal` (MCP gateway, per-persona policy) is the connection/auth *plane* the
federator rides to reach a remote backend — it is not the router itself.

---

## 8. The telemetry plane — fleet observability without surveillance **[PARTIAL]**

Legitimate need: catch **runaways** and **correlated problems** across agents. The rule
that keeps it from becoming a surveillance log: **share signals *about* activity, not
the content of it.** A signal is "agent X hit error-class Y" or "X's tool-rate spiked
10×" — typed, derived, minimal, reconstructs nothing.

Two speeds, because the needs differ:

- **Runaways → real-time.** **[EXISTS]** cabal has doom-loop guardrails
  (`_DoomLoopError`, guard #59) and the durable activity log (techne#547). Aggregate
  those cheap health signals into a fleet view and a runaway shows up *while it's
  happening* (async distillation is too late to stop one).
- **Correlated problems → async.** The distiller, already reading the raw, *also* emits
  a typed **problem-marker** ("blocked on X," "error-class Y," "recurring finding Z") to
  the telemetry plane. A correlation pass spots "three agents, same marker → systemic."
  The raw that produced it stays private.

Discipline: signals are **typed / derived / minimal**, on a **separate plane** with its
own retention + access (even if physically co-located with the Archivist, it's a
distinct domain with distinct rules). **The test:** if you can reconstruct what an agent
was *thinking* from the fleet layer, you've built the surveillance log; if you can only
see *that* it hit a class of problem, it's telemetry.

---

## 9. What this means for cabal

The federator/roles model doesn't delete cabal's memory functions — it relocates the
backends, absorbs the routing, and leaves cabal as a *host*, not an owner.

- **Moves out of cabal** (so standalone works): `cabal.archivist.*` (`ArchivistStore`,
  `gitmemory`, `promote`, `gitsync`) → the Archivist's home (techne-adjacent under the
  techne↔techne answer). cabal imports & hosts it; doesn't own it.
- **Absorbed by techne-federator:** the sink map / `register_sink("archivist")` wiring —
  "team means the Archivist" is routing knowledge.
- **Survives, fronted:** the Archivist & Librarian personas as topology-A hosts +
  (for the Archivist) the raw-deposit intake.
- **Retired:** the techne→git direct push (`sync.sh` / scraping `knowledge`).
- **Unchanged:** A2A dispatch, orchestration, limenal's connection/policy plane.

Net: **cabal stops being where memory *code* lives and becomes an orchestration host for
topology A.**

---

## 10. Status ledger

| Piece | Status |
|---|---|
| techne MCP server (local memory) | **EXISTS** |
| outbox + sinks + two lanes (techne#22) | **EXISTS** (bypassed for knowledge) |
| sync adapters (noop/filesystem/github/…) | **EXISTS** |
| multi-domain: `architect.db`, `codex.db` | **EXISTS** |
| doom-loop guards + activity log | **EXISTS** |
| cabal Archivist/Librarian personas | **EXISTS** |
| the distiller (`drain.py`, fire-up/close, idempotent) | **BUILT** (2026-07-04) |
| `capture_bash`/`capture_read` for review agents | **BUILT** (2026-07-04) |
| techne-as-federator (roles, connections, read-merge) | **NORTH-STAR** |
| knowledge-export ledger + `unpushed`/`mark_pushed` tools | **NORTH-STAR** |
| distill → outbox/ledger wiring (today it reaches neither) | **NORTH-STAR** |
| `standalone` flag as lane selector | **NORTH-STAR** (noop gate exists) |
| standalone Archivist agent (topology B) | **NORTH-STAR** |
| discovery: agent-card `url` + git-backed catalog | **NORTH-STAR** |
| telemetry plane (fleet aggregation, problem-markers) | **PARTIAL** (substrate exists) |
| coherence layer (contradiction-supersession, decay wiring, faithfulness) — [`proposals/coherence-layer.md`](proposals/coherence-layer.md) | **PROPOSED** (prerequisite for read-merge §4/§11.3; the store-time half stands alone) |

---

## 11. Open decisions (in priority order)

1. **✅ RESOLVED (2026-07-05, #220): `team` = same techne schema (techne↔techne).**
   One schema everywhere, git-backed; Archivist = "just techne with git-backing on."
   Adapter deferred — a future foreign store normalizes to techne schema at the edge.
   Unblocks #222 (read-merge) and #227 (cabal Archivist relocation). See §4.
2. **Consolidate all shared domains under the Archivist role, or split by pillar?**
   Lean: consolidate; personas (Architect/Codex) become consumers, not stores.
3. **Read-merge ranking + dedup rule** (local vs team vs static; content-hash dedup;
   mine-beats-theirs? recency? confidence?). *This rule is the product.*
   → **Specified by** [`proposals/coherence-layer.md`](proposals/coherence-layer.md):
   contradiction detection over entity-overlapping candidates (not similarity/hash —
   which can't tell contradiction from duplicate), later `event_at` wins a supersession,
   `effective_confidence()` breaks non-conflicting ties. The coherence primitives are a
   **prerequisite** for this fan-out merge — one implementation, two call sites.
   → **Concrete build design:** [`proposals/federation-read-merge.md`](proposals/federation-read-merge.md) (#222).
4. **Distiller cadence/trigger for Archivist intake** (on-deposit nudge vs scheduled
   sweep vs the same fire-up/close cadence as the drain).

---

## 12. The first real brick

Independent of every open decision above: **the distiller reading from a store it
doesn't own.** We already have it working against an agent's own store; pointing it at
a second raw source (a deposited-raw store) is the smallest concrete step that proves
the "accept fast, distill async, one distiller / many sources" backbone — without
committing to federation, topology, or the schema fork.
