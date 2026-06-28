# Context architecture: CC coupling, durable context, and the #109/#107 findings

**Status:** Decision record (Richard + Claude, 2026-06-27). Captures a strategic
investigation that reshaped #109 and surfaced a latent question about what techne is *for*.
**Confidence:** facts flagged inline; HIGH = official docs / live code / measured.

---

## TL;DR

- **#109 item-1 (stable/dynamic injection split for prompt caching) is dead** as specced.
  Two premises failed verification: (1) hooks cannot touch the system prompt or set cache
  breakpoints; (2) CC auto-loads *only* `MEMORY.md` by exact name — a `techne-context.md`
  in the auto-memory dir is ignored at session start. Item-1 was dropped; the file-write
  implementation was reverted.
- **CC auto-memory is OFF on this machine** (`autoMemoryEnabled: false`, dir absent). So CC
  reads/writes nothing there, and **#107's curated-memory drain is currently dormant** for us.
- **We are far *less* CC-locked than it feels.** The CC-specific surface is ~1,500 LOC
  isolated in two named layers; core (~10.5k LOC) and the MCP server are client-agnostic.
- **techne.db is a *lossy ledger*, not a transcript.** "Rebuild full context" from techne.db
  alone is not possible today; the verbatim record lives in CC's JSONL files.
- **Open strategic question:** should techne become a *durable context store* (Model B) that
  owns a complete, indexed copy of history independent of CC — or stay a *curated memory
  layer* (Model A) on top of CC's transcripts? This doc frames it; the decision is deferred.
- **#109 item-2 (replaceability score) is secretly blocked on Model B** — you can't score how
  well a summary replaces a raw result you didn't keep. Don't build it before the floor.

---

## 1. The findings that triggered this (the #109 investigation)

#109 proposed a "cheap, no-key, highest-ROI" split: put slow-changing context in a cacheable
*stable* block and per-turn recall in a *dynamic* block, to exploit provider prompt caching.
Verification (CC docs, HIGH) falsified the mechanism twice:

1. **Hooks cannot place content in the system prompt and cannot set `cache_control`
   breakpoints.** Caching is 100% CC-internal. `SessionStart`/`UserPromptSubmit` inject into
   the *conversation* layer only. (techne also does no per-turn push injection today —
   `techne_recall` is pull-only — so there is no per-turn cache-busting to "fix".)
2. **CC auto-loads exactly `MEMORY.md` by name** from the auto-memory dir. Topic files
   (`debugging.md`, `techne-context.md`, anything else) are "not loaded at startup" — Claude
   only reads them on demand with file tools. The 200-line/25 KB cap is `MEMORY.md`-specific.
   So writing `techne-context.md` into that dir reaches session-start context **nowhere**.

The only working ways to land stable content in the cached, compaction-surviving
project-context layer are all worse than #109 assumed: **CLAUDE.md `@import`** (works, but
techne would edit your hand-authored CLAUDE.md and *deepen* CC coupling), or **write into
`MEMORY.md`** (races CC's own auto-authoring; violates #107's spool-primary). Both rejected.

**Plus:** `autoMemoryEnabled: false` is set in `~/.claude/settings.json`, and the project's
memory dir does not exist. Per docs: *"When false, Claude does not read from or write to the
auto memory directory."* So on this machine the entire CC-auto-memory avenue is moot, and the
#107 drain (`CCMemorySource`) has no input — it reads files CC would author, but CC authors
none. #107 is correct but **dormant here**; it activates only if auto-memory is enabled or
for other clients/users.

---

## 2. How tied are we to Claude Code? (the lock-in map, HIGH — measured from the live tree)

| Layer | LOC | CC coupling |
|---|---|---|
| `core/` | 10,471 | **Portable.** CC mentions are soft (docstrings, a `platform="claude-code"` default column value, a comment example). No CC API shapes. |
| `mcp_server.py` | large | **Portable.** MCP is cross-client; `techne_store/recall/search` work in opencode, Aider, Cursor, Zed today. |
| `sync/` | 1,699 | **Abstracted.** `SourceAdapter`/`SyncAdapter` Protocols; CC is two impls beside `github_issues`, `filesystem`, `noop`. |
| `platforms/claude_code/` | 645 | **CC-locked, quarantined** (slug formula, hook registration, transcript paths). |
| `hooks/` | 864 | **CC-locked** (lifecycle entry points; thin — format + delegate into core). |

**CC-specific surface ≈ 1,500 LOC (~10% of ~13.7k), isolated in named layers — not smeared
through core.** Porting to client X = rewrite those ~1,500 LOC (lifecycle entry points,
paths/slug, a `TranscriptSource` for X's format — which is **#96**), not the 10,500 portable
ones. **#96 (per-client adapters) and #97 (plugin-authoring guide) are the "finish
portability" tickets.** The one genuinely CC-specific *bet* is the push-injection /
auto-memory mechanism — and #109 wanted to lean *harder* into it, which is exactly the wrong
direction given (a) auto-memory is off and (b) the portability goal.

**Architecture rule going forward:** new CC-specific code lands on the *platform* side of the
existing seam (render in core, write/inject in `platforms/claude_code`), never in core.

---

## 3. What techne actually stores (the storage reality, HIGH — read live)

- **`observations` (techne.db) = a lossy ledger, NOT a transcript.** 6,306 rows; each is a
  truncated summary (`summarize()` caps tool outputs at 2–4 KB, inputs at 100–500 chars, then
  redacts). Captures tool calls + `UserPrompt` rows, **not** Claude's prose/reasoning turns
  (the hook fires on tool use). You can reconstruct *what happened* (action sequence + your
  asks), **not a verbatim replay**.
- **CC JSONL transcripts = the verbatim truth.** `~/.claude/projects/<slug>/*.jsonl`
  (43 files for this project) — every message, full tool I/O. But these are *CC's*: its
  format, location, retention.
- **`JsonlTranscriptSource` can index them** into techne — but currently stores a truncated
  4 KB redacted preview for search, not the verbatim body. The verbatim still lives only in
  the JSONL.
- **`librarian.db` (documents domain) is built for full content** (documents + fts + vec) and
  is the natural home for a complete, indexed transcript archive.

**So "rebuild full context" = yes from CC's JSONL (which techne *can* ingest), no from
techne.db alone.** techne today is a curated/lossy layer *on top of* CC's transcripts.

---

## 4. The model question: curated layer (A) vs durable store (B)

**Model A — curated memory layer (today).** Observe → summarize lossily → thread → extract
knowledge/identity → serve *recall*. Verbatim lives in CC's JSONL. Value is curation, not
archive. "Full-context rebuild" isn't its job.

**Model B — durable context store (the pivot).** techne ingests transcripts verbatim and owns
the system of record, so history survives CC retention, auto-memory-off, client swaps, machine
moves. Recall sits on a complete substrate. (B = A + a complete archive underneath.)

**Case for B:** durability/independence (the JSONL is CC's to prune); with auto-memory off the
lossy ledger is our *only* techne-side record and it can't reconstruct a session; full content
is a better retrieval substrate than 892-char summaries.

**Case against B:** verbatim transcripts are large and mostly low-value (the ledger exists to
suppress noise); the JSONL already exists on disk (value is in *index + durability*, not the
copy); it reopens scope on a system declared "good enough to leave."

**Key coupling insight — #109 item-2 depends on B.** Item-2 scores "how well each *summary*
substitutes the *raw* result," but techne throws the raw away (keeps only `[:2000]`). You
cannot score replacement of a raw you no longer have. Item-2 is therefore either uncomputable
or must run *at capture time* (the one moment both coexist) — which presupposes you keep the
raw, i.e. Model B. **Replaceability-scoring + dream/compression (#109-2, #108) are the roof;
a verbatim layer is the floor. Don't build the roof first.**

---

## 5. Recommended direction (the middle path)

Not verbatim-everything (swamp), not lossy-ledger-only (can't reconstruct). The sweet spot,
most of whose machinery exists:

- **Finish `JsonlTranscriptSource → librarian.db (documents)`**: ingest full transcript
  *bodies* (drop the 4 KB cap on the stored body; keep a cap only on the FTS *preview*),
  incrementally, with retention you control. JSONL = cold verbatim; Librarian = warm, indexed,
  durable copy that survives CC pruning and is independent of auto-memory.
- Keep the observation ledger as the **fast curated index** — don't bloat it.
- This makes "rebuild full context" a real techne capability *without* a transcript dump and
  *without* any CC-auto-memory dependency, and it gives #109-2/#108 a verbatim layer to
  compress from.

**Honest sequencing (reverse of the roadmap):** durable transcript-ingest is the foundation;
replaceability/dream/compression sit on top.

---

## 6. Open decisions (Richard's call — deferred, not yet made)

1. **Do we want Model B at all**, or is "recall + CC's live transcripts" enough (and B is
   over-building a system just declared done)?
2. **If B:** warm-indexed-Librarian (the lean) vs verbatim-everything vs status-quo+raise-cap.
   Which bar — recall / session-replay / full-archive?
3. **Retention:** verbatim forever vs rolling window (the swamp↔durability dial).

---

## 7. Issue impact

- **#109** — item-1 (injection split) **dropped** (premises falsified; auto-memory off).
  item-2 (replaceability score) **blocked on the Model B decision** (§4). Recommend re-scoping
  or parking the issue with these findings rather than building item-2 standalone.
- **#107** — correct but **dormant on this machine** (auto-memory off → no curated source).
  No action; note it activates with auto-memory on / other clients.
- **#96 / #97** — the portability finish line; **#96 becomes the ingestion front-end** if we
  pursue Model B (per-client transcript adapters feeding the durable store).
- **#108 / #105** — consolidation/evidence-gate; sit *above* the durable-store floor.
- Unaffected & independently completable now: **#164** (dedup resident-model util — ripe after
  #107 added a 4th emit/ingest copy), **#81** (sessions.platform read path), **#140/#83/#56**
  (staff-review backlog cleanups), **#75/#118** (eval), **#89** (Codex).

---

## 8. What we're doing now

The Model B question is deferred (above). To keep momentum on **completable** work, we proceed
with issues independent of that decision — starting with **#164** (the ripe, bounded
resident-model emit/ingest dedup), then other self-contained backlog items.
