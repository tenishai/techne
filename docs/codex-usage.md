# Codex — operating guide: what we use it for

> Companion to the Codex pitch in [`modules.md`](./modules.md). The pitch says *what it
> is*; this says *when to reach for it and when not to*. Settled 2026-06-29, then corrected
> against a field survey of code-graph / knowledge-graph tools (CodeQL, Joern, Sourcegraph,
> Glean, stack-graphs, Potpie, RepoGraph, GraphRAG, Graphiti, Cognee, Kuzu). The mechanics
> (`index`, `codex_search`, `codex_blast_radius`) live in the pitch and the CLI `--help`.

## What Codex actually is

Strip the "code" label off and the engine is just **typed nodes + typed edges + full-text
search + reachability/blast-radius, in a SQLite file.** Nothing about that is inherently
about code. **"Symbols + call edges via tree-sitter" is adapter #1, not its identity.**

A corollary worth holding onto: the **engine** generalizes; the tree-sitter **extraction**
(~90% of the complexity) is code-specific and does not. "Using Codex for something new"
means writing a thin loader for that domain's nodes+edges, then reusing the same
`search` / `reachable` / `blast_radius` core.

## The real axis: who queries it, how often, and is indexing automatic

We first framed this as "ephemeral / throwaway." The survey corrected that. The tools that
*work* for LLM-context use are **persistent data + an auto-maintained index + on-demand
query** — GraphRAG (persistent Parquet), Cognee (persistent, SQLite among backends), and
Codebase-Memory (the closest analog to Codex: a tree-sitter call graph on SQLite with
file-watcher auto-sync + a fixed set of MCP tools). So persistent-vs-ephemeral is the wrong
question. The axis that decides whether a graph *lives or rots* is:

> **Who triggers the query, how often — and is the index refreshed without a human
> remembering to?**

The cautionary tale: GitHub's `stack-graphs` was **auto-indexed in CI and still got
archived** (Sep 2025) — the consumer wasn't valuable enough to justify the maintenance.
**Auto-indexing alone is not enough; a high-value recurring consumer is the thing.** CodeQL
and Sourcegraph thrive because a consumer with teeth (every security PR / daily code-nav)
calls them automatically.

## The one test for reaching for it

> Use Codex only when a graph is **too big to hold in your head**, *or* needs **transitive
> queries humans do poorly by eye** (deep reachability, cycles, full blast-radius).

For everything below that bar — ad-hoc "where is X handled", small graphs, a quick
"what calls this" on a file you're already reading — **Grep / Read / memory win**: they're
always fresh and zero-setup. Multiple LLM-agent teams tried graphs for code-nav and fell
back to grep/LSP for the simple case; don't relitigate that.

## Where it is NOT the tool

- **Starting a greenfield project.** Nothing to index. The two legs there are the
  **architect corpus** (reusable patterns) + **techne memory** (past decisions/experience).
- **Cross-project "assemble something new."** The architect corpus owns this. *Abstractions
  transfer across projects; raw call edges don't* — edges are the most project-local layer.
  (The field confirms this: even Glean/SCIP do cross-repo only by defining a symbol-identity
  *abstraction* over edges; the useful tier people actually build is intra-repo
  **cross-module**, not cross-project.) If you want the concrete implementation a pattern
  points to, index *that one project on-demand* and carve the unit out with `blast_radius`.
- **Ad-hoc code Q&A.** Grep wins on friction.

## Where it earns its place

1. **Intra-project / brownfield structural mapping.** Extending a codebase grown past what
   we reliably hold in head — "what does this shared interface touch", "is this reachable",
   "which existing subsystem should I mirror". A hedge against our own forgetting as a
   codebase ages and the docs drift from the code.
2. **Non-code relationship graphs too big to eyeball** — the generalization. Candidates we
   already navigate by hand: **issue/dependency graphs**, **doc/knowledge cross-references**,
   the **architect corpus's own `[[wikilink]]` composition**, **planning-time
   service-dependency sketches**. Each is a thin adapter onto the same engine.

## The refresh question: per-project graph rebuilt in CI?

A per-project `codex.db` rebuilt by a CI job is the **correct mechanism** for the
persistent-auto-maintained model — but it is only the *supply* side, and supply-first is the
trap `stack-graphs` fell into. Two rules if we ever build it:

- **Publish the `.db` as a CI artifact / blob — never commit it to git.** A binary index
  bloats history and reintroduces the merge conflicts the design avoids. (This is the §8b
  "derived index artifact as a blob" decision.)
- **Mind the freshness mismatch.** CI builds the *pushed / main* state, but a reviewer or
  author usually cares about the *unmerged branch*. So CI-refresh only serves an **automated
  PR-time consumer** (run the index on the PR branch, CodeQL-shaped). A human or agent on a
  local branch needs local `codex index` / `codex watch` instead — the CI main graph is
  stale for them.

| Consumer | Right refresh mechanism |
|---|---|
| Automated PR-time check (e.g. blast-radius comment on API-changing PRs) | **CI on the PR branch** |
| Human/agent doing local brownfield / planning work | local `codex index` / `codex watch` |

For the second row, 0.17.0 closes the loop that made local refresh a thing you had to
remember: `codex:freshness` on the SessionStart banner compares the index's recorded
`indexed_head` against the repo's current HEAD and names the commit it is stuck at (#937).
That does not refresh anything — it converts a silent "not found" into a visible "the index
is behind, here is the command", which is the failure this whole section is about.

## Build-vs-adopt: keep SQLite

SQLite was the right call. **Kuzu** — the obvious embedded graph DB to adopt instead — was
acquired by Apple and archived (2025); Cognee and Codebase-Memory independently chose SQLite
for the same zero-dependency, single-file, portable reasons we did. There is no embedded
graph engine worth adopting right now.

## Disciplines

- **Don't build the index pipeline or the resolver on spec — consumer first.** The unlock is
  a *product* decision: "do we want an automated PR-time graph consumer?" If yes, design that
  consumer first — it justifies the CI job, dictates that the job runs on the PR branch, and
  is also what would finally justify the edge resolver. If no, keep Codex local + on-demand.
- **Generalize the engine only when a *second* concrete problem pulls it out** — write the
  non-code adapter and see if `search`/`reachable`/`blast_radius` serve it unchanged. Two
  different adapters riding the engine cleanly = the generalization proved itself.

## Shelved (do not build on spec)

- **The edge resolver** (`edges.to_symbol` resolution). Revisit *only* once a high-value
  recurring auto-consumer exists — `stack-graphs` proves a resolution layer built ahead of
  its consumer gets thrown away.
- **A standing cross-project graph.** The architect corpus is the correct cross-project
  store; code edges don't transfer.

**Next step is a decision, not a build:** name the automatic consumer (or confirm there
isn't one). That answer — not speculation — tells us whether CI-refresh, the resolver, and
auto-indexing are worth it.
