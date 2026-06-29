# Codex — operating guide: what we use it for

> Companion to the Codex pitch in [`modules.md`](./modules.md). The pitch says *what it
> is*; this says *when to reach for it and when not to*. Settled 2026-06-29 (see
> knowledge#487). The mechanics (`index`, `codex_search`, `codex_blast_radius`) live in
> the pitch and the CLI `--help`.

## What Codex actually is

Strip the "code" label off and the engine is just **typed nodes + typed edges + full-text
search + reachability/blast-radius, in a throwaway SQLite file.** Nothing about that is
inherently about code. **"Symbols + call edges via tree-sitter" is adapter #1, not its
identity.** The durable mental model is: *an ephemeral relationship-mapping engine.*

A corollary worth holding onto: the **engine** generalizes; the tree-sitter **extraction**
(~90% of the complexity) is code-specific and does not. "Using Codex for something new"
means writing a thin loader for that domain's nodes+edges, then reusing the same
`search` / `reachable` / `blast_radius` core.

## The one test for reaching for it

> Use Codex only when a graph is **too big to hold in your head**, *or* needs **transitive
> queries humans do poorly by eye** (deep reachability, cycles, full blast-radius).

For everything below that bar — ad-hoc "where is X handled", small graphs, a quick
"what calls this" on a file you're already reading — **Grep / Read / memory win**: they're
always fresh and zero-setup. A graph you have to *remember to query and remember to
refresh* loses to Grep and gets abandoned. That abandonment is the failure mode this guide
exists to prevent.

## Where it is NOT the tool

- **Starting a greenfield project.** Nothing to index. The two legs there are the
  **architect corpus** (reusable patterns) + **techne memory** (past decisions/experience).
- **Cross-project "assemble something new."** The architect corpus owns this. *Abstractions
  transfer across projects; raw call edges don't* — edges are the most project-local layer.
  The intuition that an old project's *connections* help build a new one is backwards: lift
  the **pattern**, not the wiring. (If you want the concrete implementation a pattern points
  to, index *that one project on-demand* and carve the unit out with `blast_radius` — a
  transient extraction, not a maintained corpus.)
- **Ad-hoc code Q&A.** Grep wins on friction.

## Where it earns its place

1. **Intra-project / brownfield structural mapping.** Extending a codebase that has grown
   past what we reliably hold in head — "what does this shared interface touch", "is this
   reachable", "which existing subsystem should I mirror". A hedge against our own
   forgetting as a codebase ages and the design docs drift from the code. (Planning
   *tolerates staleness* — you query stable structure, not a specific diff — so an
   occasional re-index is fine here, unlike review.)
2. **Non-code relationship graphs too big to eyeball** — the generalization. Candidates we
   already navigate by hand: **issue/dependency graphs** ("if I do #X first, what
   unblocks?"), **doc/knowledge cross-references**, the **architect corpus's own
   `[[wikilink]]` composition**, **planning-time service-dependency sketches**. Each is a
   thin adapter onto the same engine.

## Disciplines (so it stays useful instead of becoming a dead artifact)

- **Keep every instance honestly throwaway.** Task-scoped db, no maintenance, no sync, no
  resolver-perfection. A task-scoped graph only needs the edges that task cares about.
  Ephemerality *is* what removes the staleness/abandonment problem — there's nothing to
  keep alive.
- **Generalize the engine only when a *second* concrete problem pulls it out** — not on
  spec. Write the non-code adapter, see if `search`/`reachable`/`blast_radius` serve it
  unchanged. Two genuinely different adapters riding the engine cleanly = the
  generalization proved itself.

## Shelved (do not build on spec)

- **The edge resolver** (`edges.to_symbol` resolution — see the sketch in knowledge/issue
  history). Revisit *only* if a recurring **auto-consumer** appears (e.g. always-on
  blast-radius-on-diff wired into a review loop with auto-indexing) or we hit real
  brownfield pain. A task-scoped, throwaway graph doesn't need a perfect global resolver.
- **A standing cross-project graph.** The architect corpus is the correct cross-project
  store; code edges don't transfer.

**Next step is a test, not a build:** the next genuinely graph-shaped problem we hit that's
too big to hold in head is where we try the *existing* engine (or a thin new adapter on it).
If it earns its place, that experience — not speculation — tells us whether further
investment (resolver, auto-indexing) is worth it.
