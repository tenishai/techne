# MCP tools — the complete surface

Every tool techne's MCP server registers, grouped by what it is for. **Generated
against the live registry and pinned by `test_mcp_tools_doc_is_complete`**, so a
tool cannot ship without appearing here — the gap this file was written to close
(techne#647 doc pass: 10 of 33 tools appeared in no document at all).

Call these from any MCP client. They are not Claude-Code-specific; the agent-facing
surface is deliberately vendor-neutral (techne#499).

For the CLI equivalents see the [README](../README.md); for what is switched on
**on your machine**, call `techne_capabilities` or run `techne capabilities`.


## Recall — ask before you assume

### `techne_recall`

Call this before relying on an assumption about this project, person, or past decision — to retrieve context for a specific topic across all layers including identity. Git-anchored knowledge results that need attention carry an explicit `freshness` tag (modified/moved/deleted/unknown) — spend verification effort only on those rather than re-checking every result by hand. An entry with no…

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

## Interrogate — ask team memory a question nobody wrote an endpoint for

### `techne_archivist_query`

Call this to INTERROGATE team memory — ask the Archivist a question nobody wrote an endpoint for, as read-only SQL. `techne_recall` pushes results at you for a topic string; this lets you ask precisely: which decisions touch a project, what was written last week that superseded something, how many procedures are anchored to files that moved, who wrote what (`written_by`, techne#553). Call with NO `sql` first to get the readable schema.

Read-only and guarded (techne#554): one `SELECT`, over the distilled knowledge tables only — `knowledge`, `knowledge_deps`, `knowledge_fts`. The raw observation tier and the personal store are out of reach, writes and `ATTACH` are denied, and a runaway query is stopped by a work budget. Three outcomes are distinguishable on purpose: `rows`, `no-rows` (the query ran and matched nothing — an ANSWER), and `store-absent` (no team store is bound here — NOT "the team knows nothing").

## Store — write the conclusion, not the transcript

### `techne_store`

Call this when you learn a durable fact, make or are given a decision, get user feedback or a correction, or finish reasoning you'd hate to redo — to store a durable personal-memory entry (fact, decision, evaluation, procedure). Store the conclusion, not the transcript. Architectural patterns go to the Architect store via `techne architect ingest`, not here (techne#137).

### `techne_observe`

Record an observation into memory from any client. The platform-agnostic capture path — call this on each turn/tool-use when no native hook exists. Supply your own session_id; techne ensures the session and tags its platform.

## Reflect & sleep — emit/ingest pairs answered by YOUR model, no API key

### `techne_reflect_emit`

Consolidate a finished session into durable memory using YOUR OWN model (no API key, no sampling). techne renders one reflection prompt per thread of work; answer each `prompt` in the returned batch as a JSON candidate array (scope/type/content/tags/confidence), then call techne_reflect_ingest once per thread with your answer. Run this when wrapping up a chunk of work, or when the SessionStart…

### `techne_reflect_ingest`

Feed back one thread's reflection answer produced by techne_reflect_emit. `response` is the JSON candidate array (as a string) you wrote for that thread's `prompt`; `thread_id` and `prompt_hash` come from the emit batch item. Proposals land in knowledge_candidates for review (techne reflect review). Call once per answered thread.

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

Feed back your verdict on a cross-store contradiction surfaced by techne_recall (techne#222). Use the fields from a federation_conflicts item returned by techne_recall: focus_content, focus_store, focus_event_at, comparison_ids, check_hash. Answer the prompt with a JSON verdict array (same format as techne_conflict_ingest) and pass it as response. SUPERSEDE + team newer → pending 'retire'…

### `techne_memory_claims_emit`

Extract claims from curated memory and judge each against the deed-log using YOUR OWN model (techne#107 PR3 — no API key, no sampling). FIDELITY CAVEAT: the deed-log contains truncated ~500-char summaries — file paths are reliable but claim text is lossy. 'uncorroborated' means no deed evidence found, NOT that the claim is false. Returns the emit dict (prompt + metadata); answer the prompt…

### `techne_memory_claims_ingest`

Feed back the claim-judgement JSON array from techne_memory_claims_emit. `response` is your JSON array of claim objects (each with `claim`, `status`, `evidence`). Returns an ephemeral report with per-claim verdicts and counts (corroborated / uncorroborated / contradicted). Nothing is persisted.

## Honesty — check a claim before making it

### `techne_check_evidence`

Call this before asserting you did something (committed, ran tests, wrote a file) to verify the claim against the observation ledger — enforces 'no unbacked word'. Returns per-claim backed/unbacked/unverifiable verdicts by parsing what the agent actually DID (Write/Edit/Bash tool records) in the given session window. Advisory — does not block; use unbacked_count > 0 as the flag. Structured…

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

## Code graph (codex)

### `codex_search`

Search Codex symbols (functions, classes, methods) by name or signature. Requires `techne codex index <repo>` to have been run first. Scoped to one repo (db-per-repo, techne#209): pass `repo` to pick it, else the server's working directory's repo is used. Experimental: the index is built manually and may be stale — re-run `techne codex index` after code changes.

### `codex_blast_radius`

Compute the transitive reachability (blast radius) of a Codex symbol. direction defaults to `in`=what reaches this symbol (dependents) — the "what breaks if I change this" impact set; `out`=what this symbol reaches (dependencies). Uses recursive CTE BFS over the call/import graph. tests/fixtures/** nodes are excluded by default (name-collision noise in the v1 name-based edge model; set…

## Patterns (architect)

### `archivist_search`

What the TEAM knows, addressed by name. Resolves to this instance's own knowledge
when the archivist is bound `local`, or to the team store when bound to one — the
answer says which in its `binding` field.

### `librarian_search`

What is WRITTEN DOWN — documents, transcripts and cards. The role is the question
("what is written down"), not a particular file set, which is what lets the corpus
grow without the call changing.

### `architect_search`

Architectural PATTERNS — the "what is worth stealing" corpus. Same corpus as
`pattern_search`, addressed by the domain name rather than the mechanism.

## The four domains

`archivist`, `librarian`, `architect` and `codex` are techne's four knowledge
domains, and the vocabulary is deliberate: an operator learns these four names in
techne and keeps them when the infrastructure grows into liminal and cabal, where a
local librarian may become a persona and later a deployed service. Every domain
answer carries the `binding` it resolved to (`absent` / `local` / `store`), so the
call stays the same while the infrastructure underneath changes.

`techne_recall` remains the default for "what do we know about X" — it escalates
across all four rungs. The domain calls are for when you already know which function
you want. `techne bindings` reports what each name currently resolves to.

### `pattern_search`

Search the Architect's architectural-pattern corpus (architect.db) — design patterns lifted from other projects' code, the team-shared 'what's worth stealing' store. Optionally scope to patterns that apply to a project (target=agent-infra|agora|argus|cabal|daedalus|kaminos|ladon|liminal|prudence|techne); with a target and no query, lists all patterns for that project.

### `pattern_related`

Given a pattern id (from pattern_search), return the patterns it links to and from via the [[wikilink]] relation graph — outbound (incl. unresolved/dangling refs) and inbound (resolved).
