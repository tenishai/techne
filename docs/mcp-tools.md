# MCP tools — the complete surface

Every tool techne's MCP server registers, grouped by what it is for. **Generated
against the live registry and pinned by `test_mcp_tools_doc_is_complete`**, so a
tool cannot ship without appearing here — the gap this file was written to close
(techne#647 doc pass: 10 of 33 tools appeared in no document at all).

Call these from any MCP client. They are not Claude-Code-specific; the agent-facing
surface is deliberately vendor-neutral (techne#499).

For the CLI equivalents see the [README](https://github.com/tenishai/techne/blob/main/README.md); for what is switched on
**on your machine**, call `techne_capabilities` or run `techne capabilities`.


## Recall — ask before you assume

### `techne_recall`

Call this before relying on an assumption about this project, person, or past decision — to retrieve context for a specific topic across all layers including identity. Git-anchored knowledge results that need attention carry an explicit `freshness` tag (modified/diverged/moved/deleted/unknown) — spend verification effort only on those rather than re-checking every result by hand. An entry with no…

Each federated memory row names its source: `store` (the connection name, or `local`) and `role` (that connection's role). The same values also appear as `_store`/`_role`, which are **deprecated** and will be removed in a future release; read `store`/`role`. Unfederated recall rows carry neither (they are local by construction) (techne#1102).

### `techne_search`

Call this when you need breadth across history — search across sessions, threads, knowledge, and identity. (For focused topic context, prefer techne_recall.)

### `techne_resume`

Get the current resume state — active threads and hot facts for session start

### `techne_threads`

List threads — active, recent, or by status

### `techne_status`

System health and database statistics

### `techne_capabilities`

What techne can do on THIS install, and what is switched OFF — grounded in the user's own store. Call this when the user asks what techne is, what it can do, what changed after an upgrade, or why a feature they heard about does not seem to happen. Returns data, not prose: narrate it as a short conversation, lead with what is off and what it would buy THEM, and quote the evidence counts — they…

### `techne_guide`

Read one of techne's bundled guides. Call with no name to list them. These are the procedures techne ships: `four-functions` (which domain answers what, and how to read the `binding` in an answer), `reflect-guide` (draining a backlog), `investigating` (mining a URL the user pasted for patterns — carries the read-only-subagent sandbox and the verdict tiers), `surveying` (mapping a whole field), `db-per-domain`, `deployment`, `welcome`, `operator-guide` (recommended hook config and operating habits), `releasing`, `mcp-tools`, `AGENTS`, `self-care`, `techne-promote`. Returns the raw markdown to read into your context, not a rendered page. Exists so a client with no shell can still reach them — `techne guide <name>` is the same content from the CLI.

**Why it exists as a tool and not only a CLI command.** The server instructions tell every
connected agent that these guides ship. Reading them used to require `techne guide`, a
shell command — so a client without a shell got the pointer and could not collect what it
pointed at. Both doors call the same accessor, so there is one copy of each guide and no
second rendering path to drift.

## Interrogate — ask team memory a question nobody wrote an endpoint for

### `techne_archivist_query`

Call this to INTERROGATE team memory — ask the Archivist a question nobody wrote an endpoint for, as read-only SQL. techne_recall pushes results at you for a topic string; this lets you ask precisely: which decisions touch a project, what was written last week that superseded something, how many procedures are anchored to files that moved, who wrote what (written_by, techne#553). Call with NO…

Read-only and guarded (techne#554): one `SELECT`, over the distilled knowledge tables only — `knowledge`, `knowledge_deps`, `knowledge_fts`. The raw observation tier and the personal store are out of reach, writes and `ATTACH` are denied, and a runaway query is stopped by a work budget. Three outcomes are distinguishable on purpose: `rows`, `no-rows` (the query ran and matched nothing — an ANSWER), and `store-absent` (no team store is bound here — NOT "the team knows nothing").

## Store — write the conclusion, not the transcript

### `techne_store`

Call this when you learn a durable fact, make or are given a decision, get user feedback or a correction, or finish reasoning you'd hate to redo — to store a durable personal-memory entry (fact, decision, evaluation, procedure). Store the conclusion, not the transcript. Architectural patterns go to the Architect store via `techne architect ingest`, not here (techne#137). When this entry replaces or corrects an existing one, pass its id in `supersedes`: writing "SUPERSEDES #N" in the content does nothing, and #N keeps surfacing as current (techne#887).

### `techne_observe`

Record an observation into memory from any client. The platform-agnostic capture path — call this on each turn/tool-use when no native hook exists. Supply your own session_id; techne ensures the session and tags its platform.

## Reflect & sleep — emit/ingest pairs answered by YOUR model, no API key

### `techne_reflect_emit`

Consolidate a finished session into durable memory using YOUR OWN model (no API key, no sampling). techne renders one reflection prompt per thread of work; answer each `prompt` in the returned batch as a JSON candidate array (scope/type/content/tags/confidence), then call techne_reflect_ingest once per thread with your answer. Run this when wrapping up a chunk of work, or when the SessionStart…

### `techne_reflect_ingest`

Feed back one thread's reflection answer produced by techne_reflect_emit. `response` is the JSON candidate array (as a string) you wrote for that thread's `prompt`; `thread_id`, `prompt_hash` and `obs_key` come from the emit batch item — PASS `obs_key` WHENEVER THE ITEM HAS ONE (a thread with no persisted row), or emit keeps offering the thread you just answered. Proposals land in knowledge_candidates for review (techne reflect review). Call once per answered thread.

### `techne_consolidate_emit`

Find duplicate/overlapping knowledge entries and judge them with YOUR OWN model (techne#108 'sleep' — fights memory bloat; no API key, no sampling). Returns clusters; for each, answer the `prompt` with a JSON verdict (merge into one, or keep separate), then call techne_consolidate_ingest with the same cluster_hash + target_ids. Proposals land in the review queue — never auto-applied. Run when…

### `techne_consolidate_ingest`

Feed back one cluster's consolidation verdict from techne_consolidate_emit. `response` is your JSON verdict object; `target_ids` + `cluster_hash` come from the emit batch item. A 'merge' verdict queues a proposal for review (promoting it collapses the rows); a 'keep' verdict writes nothing.

### `techne_faithfulness_emit`

Faithfulness gate (techne#217 / coherence-layer D4): find pending merge/supersede candidates whose replacement content has NOT yet been checked for faithfulness to their source entries, and emit a prompt for each so you can judge it with YOUR OWN model (no API key, no sampling). Returns prompts; for each, answer the `prompt` and call techne_faithfulness_ingest with the same `candidate_id`. A…

### `techne_faithfulness_ingest`

Feed back one candidate's faithfulness verdict from techne_faithfulness_emit. `response` is your JSON verdict object (verdict='faithful'|'unfaithful', dropped_claims, reason). `candidate_id` comes from the emit batch item. An 'unfaithful' verdict annotates the candidate row with a warning visible at `techne reflect review` — it does NOT change the candidate status. Sovereignty invariant: the…

### `techne_dream_emit`

Recombine pass (techne#108 'dream'): find non-obvious connections across stored knowledge using YOUR OWN model — speculative, low-confidence, review-gated, abstention-first. No API key, no sampling. Returns groups; for each, answer the `prompt` with a JSON verdict (connect or none), then call techne_dream_ingest with the same group_hash + group_ids. 'none' is the easy default — most groups…

### `techne_dream_ingest`

Feed back one group's dream verdict from techne_dream_emit. `response` is your JSON verdict object (verdict='connect'|'none'); `group_ids` + `group_hash` come from the emit batch item. A 'connect' verdict queues a low-confidence pending candidate for human review (the basis facts stay live — this is a new derived insight, not a merge); a 'none' verdict writes a keep-memory marker so the group…

### `techne_conflict_emit`

Contradiction detection pass (techne#213 D1 — coherence layer): find live knowledge pairs that share entities/tags + same type and may contradict each other. Structural selection — NOT similarity (cosine AUROC ~0.59 for contradictions). PREREQUISITE: detection requires a SHARED TAG token between two entries (or an overlapping content word when an entry is untagged) — consistent tagging is a…

### `techne_conflict_ingest`

Feed back one conflict verdict from techne_conflict_emit (techne#213). `response` is your JSON verdict array; `focus_id`, `comparison_ids`, and `check_hash` come from the emit batch item. SUPERSEDE → pending 'retire' candidate linking the older entry (by event_at) into the newer survivor (no row minted; operator reviews before promote). UPDATE → pending 'retire' candidate folding the new…

### `techne_federation_conflict_ingest`

Feed back your verdict on a cross-store contradiction surfaced by techne_recall (techne#222). Use the fields from a federation_conflicts item returned by techne_recall(include_federation_conflicts=true): focus_content, focus_store, focus_event_at, comparison_ids, check_hash. Answer the prompt with a JSON verdict array (same format as techne_conflict_ingest) and pass it as response. SUPERSEDE + team newer → pending 'retire'…

### `techne_memory_claims_emit`

Extract claims from curated memory and judge each against the deed-log using YOUR OWN model (techne#107 PR3 — no API key, no sampling). FIDELITY CAVEAT: the deed-log contains truncated ~500-char summaries — file paths are reliable but claim text is lossy. 'uncorroborated' means no deed evidence found, NOT that the claim is false. Returns the emit dict (prompt + metadata); answer the prompt…

### `techne_memory_claims_ingest`

Feed back the claim-judgement JSON array from techne_memory_claims_emit. `response` is your JSON array of claim objects (each with `claim`, `status`, `evidence`). Returns an ephemeral report with per-claim verdicts and counts (corroborated / uncorroborated / contradicted). Nothing is persisted.

## Honesty — check a claim before making it

### `techne_check_evidence`

Call this before asserting either that you DID something (committed, ran tests, wrote a file) or that PROJECT STATE is a certain way (whether a PR is merged, whether we already do X) — enforces 'no unbacked word'. Deed claims are checked against the observation ledger; state claims are checked against GitHub and our own source (techne#584). State claims are the ones the operator has…

## Coordination — talk to peer sessions on this machine

### `techne_send`

Send a message to another Claude Code session on this machine — coordinate without a human router (techne#376). Post to the broadcast channel 'coord' (default; every live session sees it) or address a specific session_id. Your identity defaults to the server's env, which can be STALE (techne#573) — if the call refuses, pass your own 'session_id'. REFLEX: announce here when you claim a git…

### `techne_inbox`

Read unread coordination messages addressed to you or your channels, plus who else is live (from Claude Code's session registry). Oldest first. Pass mark_read=true to mark them read as you read them. (techne#376)

### `techne_mark_read`

Mark coordination messages read so they stop surfacing. With 'message_ids' marks exactly those; otherwise marks everything currently unread. (techne#376)

### `techne_coord_threads`

List coordination conversation threads you're party to (messages grouped by thread), most-recently-active first. Reply into one with techne_send(reply_to=<message id>). (techne#376)

## Reminders

### `techne_reminder_add`

Arm a durable reminder that will surface in future sessions via the PostToolUse hook. Provide a note (what to remember) and a due_at time. due_at accepts a relative offset (+1d, +3h, +30m) or an absolute ISO timestamp in UTC ('YYYY-MM-DD HH:MM:SS'). Returns the new reminder id and the normalised UTC due_at. (techne#207)

### `techne_reminder_list`

List all ACTIVE reminders (including future ones) for in-session management. Returns id, note, due_at, and last_surfaced_at for each. Use techne_reminder_dismiss to dismiss a reminder. (techne#207)

### `techne_reminder_dismiss`

Dismiss a reminder by id so it no longer surfaces. The row is kept as status='done' for audit; it will not appear in future hook injections. (techne#207)

## The four domains

### `archivist_search`

What the TEAM knows. Resolves to this instance's own knowledge when the archivist is bound `local`, or to the team store when bound to one — the answer says which. One of techne's four knowledge domains (archivist, librarian, architect, codex), addressable by name. Every answer carries the `binding` it resolved to, so the same call keeps working as the domain grows from a local store into a…

### `librarian_search`

What is WRITTEN DOWN — documents, transcripts and cards. Today that is the operator's own corpus; the role is the question, not the file set. One of techne's four knowledge domains (archivist, librarian, architect, codex), addressable by name. Every answer carries the `binding` it resolved to, so the same call keeps working as the domain grows from a local store into a persona or a service.…

### `architect_search`

Architectural PATTERNS — the 'what is worth borrowing' corpus. Same corpus as pattern_search, addressed by the domain name rather than the mechanism. One of techne's four knowledge domains (archivist, librarian, architect, codex), addressable by name. Every answer carries the `binding` it resolved to, so the same call keeps working as the domain grows from a local store into a persona or a…

The codex is the fourth domain and it has TWO calls rather than one, because the graph
answers two different questions: `codex_search` finds a symbol, `codex_blast_radius`
answers *"what breaks if I change this"*. They live here rather than in a section of
their own — this section is what teaches the four names, and a vocabulary section that
listed three of them taught the gap (techne#744).

### `codex_search`

Search Codex symbols (functions, classes, methods) by name or signature. Requires `techne codex index <repo>` to have been run first. Scoped to one repo (db-per-repo, techne#209): pass `repo` to pick it, else the server's working directory's repo is used. Experimental: the index is built manually and may be stale — re-run `techne codex index` after code changes.

### `codex_blast_radius`

Compute the transitive reachability (blast radius) of a Codex symbol. direction defaults to `in`=what reaches this symbol (dependents) — the "what breaks if I change this" impact set; `out`=what this symbol reaches (dependencies). Uses recursive CTE BFS over the call/import graph. tests/fixtures/** nodes are excluded by default (name-collision noise in the v1 name-based edge model; set…

`archivist`, `librarian`, `architect` and `codex` are techne's four knowledge
domains, and the vocabulary is deliberate: an operator learns these four names in
techne and keeps them when a store moves from a local file to a team service, where a
local librarian may become a shared one. Every domain
answer carries the `binding` it resolved to (`absent` / `local` / `store`), so the
call stays the same while the infrastructure underneath changes.

`techne_recall` remains the default for "what do we know about X" — it escalates
across all four rungs. The domain calls are for when you already know which function
you want. `techne bindings` reports what each name currently resolves to.

### Choosing what recall searches, and in what order

By default recall consults the four built-in rungs in ladder order: `memory`,
`archivist:local`, `archivist:remote`, `librarian`. An installed package can add
read-only sources through the `techne.recall_sources` entry-point group (each entry
point is a zero-arg callable returning a `RecallSource`: `name`, `search(topic, limit)`,
`health()`, and optionally `escalation = True` for reference material). Those are
refused unless allowed (the allow-list keys belong to the operator layer), and
config.yaml decides the order:

```yaml
recall:
  order:
    - memory
    - librarian
    - archivist:local
    - archivist:remote
    - {name: librarian:remote, fallback: true}
  allowed_sources: [remote-lib]     # entry-point names; refused by default
  fill: 10            # a fallback source runs only if the sources before it gave fewer rows
  source_limit: 5     # rows asked of each plugged-in source
  source_timeout_s: 3.0
```

Results come back grouped by source in that order, and `tiers` reports each source
in the same order. Every source, built-in or plugged in, is searched only when the
order reaches it: a source left out of `order` and a fallback that isn't needed are
never searched. Setting an order replaces the default merge, which alternates local
and remote rows, each in its own store's order; with an order, an earlier source's rows
always come first.

Each source reports one of these states:
- `hit` or `empty`: it was searched;
- `skipped`: a fallback that wasn't needed, so it wasn't searched;
- `excluded`: a built-in left out of `order`, not searched;
- `unavailable`: a plugged-in source that wasn't allowed, failed to load, timed out,
  reported unhealthy, or whose previous call is still running;
- `unconfigured`: a built-in archivist rung with nothing configured behind it.

Rows from plugged-in sources carry `origin`, `recall_source` and `external: true`, and
their text is credential-redacted. The two allow-list keys are governed by the
operator layer. `techne doctor` reports `recall:sources`, which is broken when `order`
names a source recall can't use.


## Patterns (architect)

### `pattern_search`

Search the Architect's architectural-pattern corpus (architect.db) — design patterns borrowed from other projects' code, the team-shared 'what's worth borrowing' store. Optionally scope to patterns that apply to a project (target=<one of your configured architect.pattern_targets — run `techne architect targets` to see them>); with a target and no query, lists all patterns for that project.

### `pattern_related`

Given a pattern id (from pattern_search), return the patterns it links to and from via the [[wikilink]] relation graph — outbound (incl. unresolved/dangling refs) and inbound (resolved).
