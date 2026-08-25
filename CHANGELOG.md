# Changelog

All notable changes to techne are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic versioning.

## [Unreleased]

### Added

- **`techne backup --keep N` — retention** (#742). `backup` wrote a full set every run
  and pruned nothing, which made it safe to run by hand and unsafe to put on a timer: a
  nightly job with no ceiling is a disk-filling cron job dressed as a backup story.
  `--keep N` deletes all but the newest N sets in the destination, **after** the new one
  lands, ordered by each set's manifest `created_at` rather than by mtime — reading a
  backup directory rewrites mtimes and would evict a recent set because something *read*
  it. A directory is only a set if it carries a manifest listing databases, the same
  identity test `restore` applies; anything else under `backups/` is left alone.

  **Retention failure exits 3**, distinct from a failed backup (`1`) and from success
  (`0`). A timer surfaces exit status and nothing else, so at `0` a permanently broken
  prune reads green while the disk fills — the failure `--keep` exists to prevent, one
  level up.

- **Scheduling that ships** (#742). `deploy/techne-backup.{timer,service}` and
  `deploy/ai.tenish.techne-backup.plist`, both passing `--keep 14`, with install steps in
  `docs/deployment.md`. The systemd path documents `loginctl enable-linger` as required
  and says plainly that the verification step will not tell you it is missing — without
  lingering the user manager stops at logout and the timer never fires, while
  `list-timers` shows it healthy for as long as you stay logged in.

- **The four knowledge domains are addressable by name** (#724). `archivist_search`,
  `librarian_search`, `architect_search` and `codex_search` — the archivist, librarian,
  architect and codex answer to their own names, in one envelope carrying the `binding`
  each resolved to.

  The recall ladder has answered across all four for months; what was missing was
  *address*. The librarian had no tool at all (a test asserted its absence) and the
  architect answered only to `pattern_search`, the mechanism rather than the function —
  so operators learned `techne_*`, the one vocabulary that does not survive the move to
  liminal and cabal.

  **Every answer says what it resolved to**, which is the point rather than a nicety:
  the same call reports `local` today and would report `store` — or whatever a cabal
  persona or a deployed service resolves to later — without the caller changing. An
  unreadable domain returns its *reason*, not an empty list.

  `techne_recall` is unchanged and remains the default for "what do we know about X";
  it escalates across all four rungs. These calls are for when you already know which
  function you want. No liminal dependency is added — dot-namespacing stays liminal's
  job.


- **`techne bindings` — one question, four domains** (#552 part 4). What is the
  archivist / librarian / architect / codex bound to, and can I reach it, asked
  identically of each and answered in one shape. This is the "same feel" ask, answered
  on the axis the sibling projects answer it on: liminal's `Target` makes transport a
  field and never changes the call shape, and cabal already ships `patterns`, `code`,
  `documents` and `archivist` as peer handlers. Neither fuses its domains into one
  query.

  **It deliberately does not make patterns and codex recall rungs**, which is what #552
  part 4 literally proposes. Measured first: pattern FTS falls back to OR and answers
  "archivist binding" with "Late-binding task definitions"; the codex index was 83 of
  249 files stale as of 2026-08-21 (a snapshot — a reindex moves it) and returned zero
  hits for a symbol merged that day; the durable finding is dilution, 45% of hits on
  realistic topics being test symbols. A rung that returns noise on every recall
  is worse than no rung, and open-sourcing freezes the response shape. Revisit when
  #550 and codex freshness make them answerable — `test_the_recall_ladder_is_unchanged`
  is the guard, and says so.


### Changed

- **BREAKING: `techne backup --out DIR` now names the backups *root*, not the set**
  (#742). `--out`'s help text has always called it the root; the code treated the path
  you passed *as* the timestamped set. The two disagreed for as long as the flag has
  existed, and it stayed harmless only because nothing else read that path — until
  `--keep` did.

  **If you script `backup --out X` followed by `restore X`, that now needs
  `restore X/backup-<stamp>`.** `--out` yields a directory containing timestamped sets;
  `restore` takes one set, as it always has.

  The mismatch is what turned `--keep` into a deletion: `--keep` prunes
  `res.dest.parent`, which under the old reading was the directory *above* the one you
  named. `backup --out ~/techne-backup --keep 1` was reproduced deleting an unrelated
  sibling project directory that happened to contain a `manifest.json`. The same
  mismatch also made `--keep` useless where it was aimed — every `--out` run wrote to
  the same directory, so there was never more than one set there to prune. Fixing the
  behaviour to match the documentation was the choice over bolting a guard onto
  `--keep`, because a flag whose documented meaning and behaviour disagree is what
  produced the deletion.

- **The archivist is a call, not a deployment mode** (#552). `standalone` was a *lane
  selector*: it forked both halves of the loop — write at
  `lifecycle._thread_summary_target`, read at `archivist_connection` — so a default
  install neither read nor wrote an archivist, which is the opposite of the intent.
  Six call sites each decided for themselves what the archivist was, and the read path
  and write path could disagree.

  One function decides now: `core.archivist.archivist_binding(config)` returns
  `local` / `store` / `absent`, and read, write, the query surface and the recall
  ladder all ask it. The vocabulary is not new — #695 introduced it as a *reporting*
  concept derived inside `_techne_recall`; this gives it authority. `standalone` is
  deprecated but still read, and still means what it meant: it describes a **topology**
  ("I am a lone agent"), not an absence — and a lone agent still makes the archivist
  call. A named `archivist_db_path` now outranks it, so the previously contradictory
  config (a path set while `standalone: true`) does what it says.

- **`local` resolves to the knowledge you already have — it does not copy.** Measured:
  the local archivist was a 98.9% mirror of local knowledge (1,104 live rows against
  1,109, 1,105 byte-identical by content). #695 measured that mirror and deliberately
  stopped *reading* it; writing it is pointless by the same argument and was not free —
  `push_knowledge_to_archivist`'s own docstring records the copy as **5.15s of a 5.52s
  session teardown** (760 enqueues, 2,306 SQLite connections) against a 5.0s hook
  budget it then blew. On the author's box this change takes that teardown from ~1,100
  enqueues to **0**. Nothing depended on the copy: the egress path reads local
  `knowledge`, never the mirror.

### Fixed

- **A codex read from inside a `git worktree` resolves to the primary index** (#719,
  tenishai/agents#53). `db-per-repo` silently meant *db-per-checkout*: a linked worktree
  has its own `.git`, so every query from a review worktree or a worktree-per-session
  checkout answered "no codex index for the active repo". Measured on a review agent's
  own history: its impact pass ran on 6% of reviews, because its instructions correctly
  say to fall back to grep when the codex returns nothing — it was following them
  against a tool pointed at a path that had never been indexed. Read path only; the
  write path still resolves to the worktree, or a PR branch's symbols would land in the
  base graph other sessions query.

- **`codex_blast_radius` no longer exceeds the response token ceiling** (#722). A hot
  symbol returned 500 nodes / ~175,000 characters — about 44k tokens — and came back as
  an error rather than an impact set. Capped at 50 closest-by-depth, with `returned`,
  `walked` and `truncated` stating what was cut, including the *depth* of the cut
  ("450 more at depth 1" is the difference between a truncated radius you can reason
  about and one you cannot). It also reports `walked` as a **floor** when the graph walk
  hits its own 500-node bound, which had always been indistinguishable from a complete
  walk.

- **Raw operational memory no longer crosses the team seam** (#552). A thread summary
  is raw; the Archivist federates only the distilled, but nothing enforced that at
  write time, so `sink:archivist` carried raw summaries across for every non-standalone
  instance. Measured before the change: `archivist.db` held **17,819 observations
  against 1,104 knowledge rows — raw was 94% of the team store.** Summaries now always
  take the sync lane. The sink is *not* deleted and registration is *unconditional*:
  11,442 delivered rows sit in that lane on the author's box, and an unregistered
  target is what `outbox prune-orphans` marks permanently dead. The gate belongs on
  enqueue, not on registration.

  **Operator-visible after upgrade:** `techne doctor` grows an
  `outbox:sink:archivist — dormant — retired by techne#552` line on any box that ran
  `standalone: false`. That line is the fix, not a symptom: the lane's delivered rows are
  kept readable on purpose, and marking it retired is what stops it reporting a dead
  producer forever with no operator action able to clear it.

- **Lane retirement follows the binding, not just the release** (#715). The same false
  red existed one lane over: `sink:archivist_knowledge` is fed only under a `store`
  binding, so under `local` — the new default — it has no producer either, but it is not
  globally retired and a static set could not express it. Measured on the author's box
  minutes after #714 merged: 2,208 delivered rows, `pending=0`, due to report `STALE`
  seven days later. `retired_producer_targets(config)` is a predicate for that reason;
  retirement-by-release is the special case where the answer is the same for every
  config. The `pending == 0` gate is kept — retirement suppresses the *producer*
  question and nothing else, so an undrained backlog still reports.

  The same question, asked once per *binding* rather than once per lane, turns up a
  **third**: `sync:thread_summary`'s only producer is gated on `sync.adapter != "noop"`,
  and `noop` is the default. Including it also resolves a self-contradiction the check
  carried — the declared-but-never-enqueued branch asked "does this lane have a
  producer" and the discovered-rows loop did not, so the *same lane under the same
  config* reported `dormant/ok` with zero rows and `stale/ok=False` with three drained
  ones. Both branches now answer from one predicate.


### Added

- **`techne archivist query` and the `techne_archivist_query` MCP tool** (#554) — a
  guarded read-only SQL surface over team memory. The Archivist could hand you things
  but you could not interrogate it: `archivist.db` was reachable only through
  federation fan-out inside recall, which pushes results at you for a topic string.
  "Which decisions touch cabal?", "what was written last week that superseded
  something?", "who wrote what?" are plain reads, and every one was impossible without
  opening the sqlite file by hand. Pairs with #553 — a query surface without writer
  provenance answers *what do we know* but never *who says so*.

  **Distilled only**: `knowledge`, `knowledge_deps`, `knowledge_fts`. The raw
  observation tier is not readable here, deliberately and separately from whatever the
  distilled-only egress decision concludes. Enforced by SQLite itself, not by
  convention — a `?mode=ro` connection, a `set_authorizer` callback checked during
  prepare (which denies `ATTACH`, because a read-only connection can otherwise attach
  and read the *personal* store out through the team surface), a work budget that
  stops a runaway query, and one statement per call. No write verbs on this path ever,
  for any caller.

  Three outcomes are distinguishable on purpose: `rows`, `no-rows` (the query ran and
  matched nothing — an answer), and `store-absent` (no team store is bound — *not*
  "the team knows nothing"). Collapsing the third is how a standalone install comes to
  report that the team knows nothing; it is the same failure the recall ladder's
  `loose-match-only` exists to prevent.
- **`techne custodian run|status` — the emit→ingest rails can run headless** (#555). The
  passes that are actually custodial — dedup, merge/supersede, the faithfulness check on
  rewrites, contradiction classification — all sat on the resident path, which requires an
  agent sitting there to answer emitted prompts. A custodian that only works while you are
  watching is not a custodian. Rails now take an optional model binding and run unattended;
  unbound (the default) every rail behaves exactly as before.

  Provider-agnostic by construction: the binding is a `str -> str` callable resolved through
  the same `_make_generate_fn` as the operator-model path, so any LiteLLM identifier works —
  local Ollama, Vertex via ADC, a proxy. The credential question lives in config, not in the
  code path.

### Fixed

- **The automatic session-end reflect rail had never produced a single candidate** (#555).
  Not since a regression — *ever*, since 2026-06-20. Measured on the author's store: all 444
  candidates carry `model_used='agent-assisted'` (the resident route); zero came from the
  operator-model path, which was raising `AuthenticationError` on every session because
  `config.model` is an Anthropic id and the operator holds no Console API key.
  `reflect_on_session` caught it, incremented a counter nobody reads, and let teardown
  proceed — for two months.

  It stayed invisible because `check_maintenance_rails` reported the rail **live** the whole
  time: that check keys on `knowledge_candidates.origin`, the rail's *output*, and `origin`
  does not record which of the two producers wrote the row. The liveness subsystem built to
  stop "an absent result is indistinguishable from a working one" contained that exact hole.

  Every automatic and headless run now records `rail:<name>:last_run` — ok / partial / error /
  noop, with the exception *type*, because `AuthenticationError` says *needs a credential it
  was never given* where a generic failure says only *broken*. New `check_model_bound_rails`
  reads those markers. No marker reads dormant, not broken: an install that only uses the
  resident route has not opted in and must not be nagged.

- **Candidate rows now name the model that judged them** (#555). Three insert sites
  hardcoded `model_used='agent-assisted'` in their SQL, so the first working headless run
  wrote six rows claiming a resident agent had judged them — the feature would have
  manufactured, for five rails at once, the exact blindness it exists to fix. The label is
  now a required keyword on the private inserters (a missed forward is a `TypeError`, not a
  silent lie) and defaults to the resident value on the public entry points, so the resident
  path is unchanged. Making it required immediately surfaced a third producer nobody had
  threaded: `federation_conflict_ingest` has six insert sites of its own.

- **`_make_generate_fn` accepts an optional per-call timeout**, unset for every existing
  caller. A call that never returns is not an exception, so a per-item `try/except` cannot
  catch it — an unattended runner would hang forever and never report. The defect holds at any
  latency; a CPU-bound dev box merely made it visible (a real conflict prompt had not returned
  after 240s there, against 11.6s for the same prompt on the inference host).


## [0.15.0] — 2026-08-19

The release about what the store is willing to claim. 0.14.0 stopped the instruments
lying about whether rails *ran*; this one goes after what they assert when they do —
a search result that reported its source table as its kind, a memory we were on record
as not believing that got ranked rather than filtered, a call graph asserting 19,345
edges it had resolved none of, a version stamp certifying migrations that never ran,
and a health check that had been red since June with no way to clear it. Plus the half
that makes any of it reach you: techne can now introduce itself. **29 changes.**

### Added

- **`techne capabilities` and the `techne_capabilities` MCP tool** (#638) — what is
  switched on *on this install*, what each thing off would buy you, and the exact
  config line, grounded in counts from your own store. Measured on a fresh install:
  **8 of 8 capabilities report OFF**, because nearly everything is opt-in. It also
  reports bundled workflows absent from disk, which no config flag can see. A
  one-line invitation fires at session start **once per version change** and never
  again — deliberately, since #562 measured how fast a recurring banner becomes noise.
- **`self-care` and `techne-promote` ship with techne** (#641) — installed by
  `techne init`, versioned *with* the release because they drive its CLI and know its
  store layout. `techne skills install` / `status`; never overwrites a copy you edited.
- **First-prompt startup directive** (#643), gated on pending work and once per session.
- **Relevance floor for per-turn recall** (#414) — "inject nothing" is a legal outcome
  now. Ships the mechanism with the **value deliberately unset**: the threshold should
  come from the replay harness (#415), which is unbuilt, so behaviour is unchanged
  until someone sets it from evidence. The config comment carries the measured anchor.
- **`techne cards repair`** (#684) and a `remedy` field on fsck's results.
- **Rescue@10 eval gate** (#583) — does a supersede actually reach the ranker? Includes
  a test pinning the *known limit*: a rewrite that drops the original cue terms is not
  rescued.
- **`docs/mcp-tools.md`** — the complete agent-facing surface, pinned by a test so a
  tool cannot ship undocumented.

### Fixed

- **`search()` reported the source TABLE as `row['type']`** (#579, #566) — every
  consumer reading it was misled. The row now says which table *and* which kind.
- **`status='uncertain'` ranked instead of filtering** (#501) — we were injecting what
  we are on record as not believing. "Written down but not trusted" is now expressible.
- **The per-turn rail re-sent what the session already had** (#576) — **46.1%** of
  deliveries (3,227 of 7,004, re-measured 2026-08-18; #576 was filed at 45.4%), with
  compaction-aware suppression now in place.
- **SessionStart spent 74.5% of its budget on sessions that produce nothing** (#562 —
  re-measured 2026-08-18; the issue title's 76% is the original, smaller sample).
  No classifier can spot them in advance — project path, identity and lifetime were all
  measured and rejected — but 94% never submit a prompt, so the fix is **placement**:
  the model's copy waits for a first prompt that may never come. The operator banner is
  unaffected; it rides a separate carrier.
- **The code graph asserted calls it could not resolve** (#606). All 19,345 call edges
  have `to_symbol IS NULL`; 57% name-match some symbol and 99 ambiguous names cover
  4,177 edges. Rows now carry `resolution: exact | name-inferred`, `only_proven` returns
  what can be proved, and blast radius reports the split. The default stays permissive
  because flipping it would empty the call graph on every store today — the fix is
  honesty, not suppression. The indexer-side fix is #679.
- **A store ahead of its code migrated silently** (#676) — `current >= target` folded
  "already there" together with "ahead of the code". `architect.db` sat at v4 against a
  code constant of 3 for six days, **skipping every migration with no error, no warning,
  no log line**. Now loud on stderr, plus a `techne doctor` check per store.
- **fsck was red since June with no way to clear it** (#684) — #633 fixed the producer
  and left four rows behind. `maintenance.CheckResult` had no `remedy` field, so fsck
  could name a fault and offer nothing. A check that can never go green trains you to
  ignore it.
- **The applies graph could not see new patterns** (#674) — 141 of 505 patterns linked,
  a ceiling rather than a lag. Plus the `limenal`/`liminal` split, canonicalised at
  extraction and migrated.
- **461 silent codex index failures** (#610), a disk walk that swallowed errors (#599,
  #600), grep-assist answering a stale index with silence (#602).
- **The CLI reflect path left no record** (#642, #644, #645, #646) — ingest never
  drained the queue, emit never set the cursor.
- Also: #575, #590, #591, #593, #607, #608, #609, #620, #626, #629, #630, #631, #632,
  #635, #637.

### Docs

- **`docs/welcome.md`** gains an up-front "Don't read this — ask it" section pointing
  at `techne capabilities`, because a guide is a snapshot and your install is the
  truth. Adds the shipped workflows and this release's honesty features.
- **`docs/mcp-tools.md`** is new; the README's CLI reference gained `capabilities`,
  `audit`, `profile`, `prompts`, `deposit` and `cards repair`.
- **The counterfactual is #682** — four source sites claimed it was tracked, by two
  different numbers, neither of which was it. One was in `techne audit`'s
  operator-facing output.
- **Corrected the import-model claim from #606** (#679): from-imported *names* are
  recorded; what is missing is the source **module**, and therefore the binding.

### Upgrading

`pipx install --force "techne[recommended] @ git+https://github.com/tenishai/techne.git@v0.15.0"`
— the `--force` is required; a plain `pipx install` over an existing techne refuses.

**Core schema is unchanged — `SCHEMA_VERSION` stays at 24, so from 0.14.0 there is no
techne.db migration.** Coming from 0.11/0.12 you still cross the 0.14.0 ladder; see that
release's notes, which remain accurate.

**`architect.db` migrates 3 → 4** — one step, collapsing the misspelled `limenal` target
into `liminal`. It is `INSERT OR IGNORE` + `DELETE`, not `UPDATE`, because the primary key
is `(pattern_id, target)` and a straight update collides on every pattern carrying both
spellings. `guarded_migrate` takes a backup first.

**If your `architect.db` is ALREADY stamped at 4, the step is skipped and the `limenal`
rows survive.** `guarded_migrate` returns at `current == target` before any step runs, so
a store that reached 4 by another route never sees this one — and because the code
constant has now caught up, `techne doctor` will report `schema:architect` **healthy**,
because the stamp finally matches. That is the honest state of the stamp, not of the data.
Check with `sqlite3 ~/.techne/architect.db "SELECT target, count(*) FROM pattern_applies
GROUP BY target"`; if `limenal` is present, reset the stamp to 3 and re-open techne to let
the step run. See #676.

**A merged fix is not a deployed fix.** If you run techne from an editable checkout
(`~/.techne/runtime`), `git pull` it too — otherwise the CLI keeps running the old
extractor and re-derives the rows this step just removed. That happened on the machine
that cut this release; see #690.

**If `techne doctor` reports `schema:<store>` BROKEN after upgrading, read it.** That
check is new in this release and it can only fire on a store *ahead* of its code —
meaning migrations are being skipped. Behind is normal and reports healthy.

**Run `techne skills install` after upgrading.** An upgrade does not re-run `techne init`,
so the bundled workflows are not refreshed automatically.


## [0.14.0] — 2026-08-17

The release where the instruments stopped lying. Nearly every fix below is the same shape:
a rail that was enabled, registered, and reporting healthy while doing nothing — a
grep-assist that never once fired, a code index that certified its own pollution, a
zero-result search that couldn't describe its own reliability, a CI guard that failed open
on a typo, a completeness check satisfied by a prefix, a glob that skipped files without
saying so. None of them looked broken. That is what made them worth a release. 24 changes.

**Upgrading:** `pipx install --force "techne[recommended] @ git+https://github.com/tenishai/techne.git@v0.14.0"`
— the `--force` is required; a plain `pipx install` over an existing techne refuses. What
you cross depends on where you start:

| from | schema | what runs |
|---|---|---|
| 0.13.0 | 24 → 24 | **Nothing.** Pure code upgrade, no migration. |
| 0.11.0 / 0.12.0 | 18 → 24 | Six steps, including one history rewrite — see below. |
| 0.9.0 / 0.10.0 | 17 → 24 | The above, plus step 18. |

From **0.11/0.12** the six steps are: **19** adds `knowledge.vec_stale` (additive, defaults
0); **20** builds a covering index on `outbox` — no data change, but its cost scales with
accumulated synced rows, so the oldest store pays the most; **21** namespaces existing coord
read receipts under `uuid:` and switches new ones to `name:<display-name>`, so a relaunched
**named** session stops re-reading the backlog — *after draining it one final time*, because
old UUID-keyed receipts deliberately do not transfer (the UUID→name map lived in per-session
sidecars that are gone, and inventing one would fabricate the very "who read what" claim a
receipt exists to record); **22** bumps the version so the additive `reflection_queue` table
actually reaches existing databases — the bump *is* the load-bearing part, since an additive
schema change at an unchanged version reaches new databases only; **23 rewrites
`observations.timestamp` and `.event_at` in place across your entire history**, normalising
three concurrently-written formats to one — the only step that touches existing data at
scale; **24** backfills historical `sessions.ended_at` NULLs from last known activity.

Coming from **0.9/0.10** you also cross **18**, which redeploys the `observations_*` FTS
triggers in their content+tags+kind form and **rebuilds `observations_fts`** over your whole
history — on those databases `tags`/`kind` were silently never indexed.

**That one-time backlog drain in step 21 is expected and is indistinguishable from a fault
unless you know.** If a named session re-reads the coord backlog once after upgrading, that
is the migration working as designed, not a failure.

**IF YOU RUN A LOCKED OPERATOR LAYER AND SET `sources` IN `config.yaml`, MOVE IT INTO THE
OPERATOR LAYER BEFORE UPGRADING.** As of #585 `sources` is a governed destination, so a
locked layer now *discards* agent-supplied source paths instead of honouring them. **The
drop is silent** — nothing logs it — so the symptom is "techne stopped ingesting my
transcripts" with nothing pointing back at the upgrade. Installs with no operator layer
(the default) are unaffected.

The migrator takes an automatic backup before migrating (`guarded_migrate(backup=True)`),
and since #574 that backup is reused within a transition and pruned afterward rather than
accumulating. Taking your own copy of `~/.techne/` first is belt-and-braces, not required.

### Added

- **Citation-attribution for the knowledge rails** (#329, #560) — closes the last open
  finding of the utilization work: whether an injected memory was actually *used*, not merely
  retrieved. Without it, every change to what gets retrieved is unfalsifiable — there is no
  way to tell an improvement from a regression. Behavioral proxy first, deliberately: free,
  deterministic, no model key, CI-runnable.

- **Executable predicate for the evidence length test** (#541, #567) — the gate asserted a
  property it described in prose. It now runs the probe it claims to run.

- **Archived `.jsonl.gz` transcripts are readable** (#621) — gzipping old transcripts is a
  4.5x saving (14.0 MB → 3.1 MB measured on a real one, byte-identical round trip) against
  366 MB of transcripts older than a fortnight. But a `.gz` was **invisible** to both
  transcript globs — no error, just skipped — and the reader died on one with a
  `UnicodeDecodeError`. So the corpus could shrink silently: archive to save space, and the
  archived sessions quietly stop existing as far as import and search are concerned. The
  reader now opens `.gz` transparently and both globs match it, so archiving is no longer a
  form of deletion.

- **Crash-recovery fault-injection gate** (#388) — techne is a stateful daemon over SQLite,
  and the interesting bugs live in *interrupted transitions*, which unit tests never reach
  because a unit test always gets to finish. A new `crash` suite SIGKILLs a child mid-write
  and then proves the store: an uncommitted batch leaves no trace, a committed one survives,
  a hot WAL replays, an interrupted migration does not stamp its version *and converges on
  re-run*, and an interrupted flush never marks an undelivered row synced. **The child kills
  itself at a chosen line** rather than racing a timer from the parent — a crash test that
  fails randomly teaches people to re-run CI until it is green, which is how a real
  regression gets waved through. What is tested is *our* usage, not SQLite's atomicity. Run
  by a dedicated `crash-recovery` job, because a suite excluded from the default run and
  executed by nothing decays quietly.

- **A go-public runbook** (#556) — `docs/go-public-runbook.md`. Not yet executed; the flip is
  deliberately paused. Its load-bearing finding: **stopping the runner does not shorten the
  dangerous window between "public" and "approval policy applied", it closes it** — a job
  whose labels match no *online* runner queues rather than executes.

### Fixed

- **grep-assist was unreachable in practice, and granted permission when it did fire**
  (#596, #601) — the rail was enabled, registered, and fired *zero times* across a full
  session of searching this codebase, because its matcher was `Grep` while every real search
  was `grep` inside a `Bash` call. The guard's precondition was "the agent chose the Grep
  tool", and falsifying it was free, accidental and invisible. Widening the matcher to
  `Grep|Bash` is one line; the rest is parsing a shell command safely enough to deserve it —
  per-head option tables measured against the actual binaries, unlisted options declining
  rather than guessing. **Also removed an unconditional `permissionDecision: "allow"`**: dormant
  and near-harmless while the matcher was `Grep` alone, a live hole the moment it matched
  Bash, where a grant covers the entire command line while the parser only has to like one
  segment. A test had pinned that grant as *intended*.

- **The code index certified its own pollution** (#604, #605, #613, #615) — virtualenv
  contents were indexed, so 96% of this repo's graph was vendored code: 114,412 foreign
  symbols against 5,137 real ones, with `codex_blast_radius` returning `aiohttp/web.py` as a
  dependent of a techne hook. Indexing is incremental and its only DELETE is scoped to the
  file being re-indexed, so the polluted rows were never revisited — and `index_freshness`
  reported `coverage: complete` over them, because those files really were present and
  unchanged on disk. They were not stale; they should not have been there. Skip-list
  enforcement now runs **on every index** rather than once in a migration, so a skip-list
  change heals itself instead of needing a new migration each time.

- **A zero-result code search described itself as authoritative** (#595, #598) — a found
  result carried a staleness flag and a refresh notice; a zero result carried nothing. "I
  cannot see it" rendered as "it is not there".

- **grep-assist option tables** (#611, #616) — `--group-separator` now declines rather than
  asserting an arity that differs between ugrep and GNU grep, where it would consume the
  pattern. Provenance recorded for the table whose missing provenance caused the defect.

- **Per-turn recall showed the source table as the row's kind** (#603), and the per-turn PUSH
  rail injected pull-only rows (#561, #565).

- **Health reported a guess about the install kind** instead of the revision (#568, #569).

- **Migration backups grew without bound** (#574, #578) — now reused within a transition and
  pruned after.

- **MCP accepted a stale env-derived session identity** (#573, #581), and the reflect-ingest
  surface pre-judged an empty response (#485, #563).

- **Architect pattern search never got the OR fallback** (#70, #557, #558).

- **A capture-rail test was measuring the CI runner** rather than the rail (#543, #564).

### Security

- **`sources` is now a governed destination — THE ONE ENTRY HERE THAT ASKS SOMETHING OF YOU**
  (#585). `sources.claude_code_transcripts.paths` names arbitrary filesystem paths whose
  *contents* are ingested into the observation ledger, and the rationale that already
  governed `connections` applies to it word for word. **This changes enforcement, silently:**
  under a locked operator layer, governed keys in `config.yaml` are *dropped*, so a locked
  install now discards agent-supplied source paths where it previously honoured them.
  Nothing logs the drop. **Action: if you run a locked operator layer with `sources` in
  `config.yaml`, move it into the operator layer before upgrading** — see "Upgrading" above.
  Default installs (no operator layer) are unaffected.

- **Unlisted sync adapters are refused by default** (#389) — `load_adapter` would import any
  class registered under the `techne.sync_adapters` entry-point group and hand it every
  outbox entry, i.e. full session and memory content. Loading arbitrary registered code that
  receives memory is an egress decision, not a plugin lookup. Now gated behind
  `sync.allow_unlisted_adapter`, and the refusal *precedes* the import, because import-time
  side effects are code execution. Within one OS user this is not a wall — it makes an
  unlisted adapter impossible to load *silently*. Filed here rather than under Added: the
  audit it came from found techne has **zero network-capable imports**, so there was no POST
  to gate and this is hardening, not a feature.

- **Fork PRs could reach the self-hosted runner** (#556, #570) — `pull_request` plus a
  self-hosted pool is remote code execution on the runner host. The guard now fails *closed*;
  the previous one failed open on a typo'd property, and for `pull_request` GitHub evaluates
  the workflow from the PR's own ref, so an in-workflow guard is attacker-editable. Forking
  is disabled at both repo and org level as the load-bearing control.

- **Every Config field now forces a destination judgment** (#509, #582) — egress keys were
  opt-out by omission, so a new field defaulted to leaving the machine.

- **A dotted-key prefix was accepted as that judgment** (#588) — `reflect.model` in
  `EGRESS_KEYS` marked the *entire* `reflect` block classified, so every other field inside
  it forced no judgment at all. Measured: adding a nested LLM endpoint override left the
  suite byte-identical. Classification is now exact and recursive.

- **The completeness guard checked a surface the gate does not use** (#586) — it resolved
  entries against the *dataclass* surface while the gate acts on the mapping the loader
  walks; the two agree only by convention. It is now behavioural: load a config with each
  key and without it, and require the result to differ. Found on the way and filed
  separately (#620): `Config.load` reads a `conflict` block that `save` never writes, so a
  hand-written one is dropped by any load→save cycle.

- **A recycled pid read as a present session** (#587) — presence was TTL-free and pid-only,
  the exact combination the neighbouring code warns against. Demotion now requires *positive*
  evidence of a mismatch, because the obvious implementation would manufacture false absents,
  and a false absent refuses a session that is genuinely working. The `ps` snapshot is cached
  (measured: 31.9 ms → 0.041 ms warm), since cost was why this was deferred rather than an
  oversight.

### Docs

- Architecture cards for the **utilization seam** (#597) and the **codex** store, the latter
  now anchored to both files it makes claims about (#617). The card gate catches drift only
  where an anchor exists, so an unanchored claim is an unguarded one. The cost is stated at
  the anchor site: `source.py` churns for reasons unrelated to the card, so the gate will
  fire on edits that do not concern it — **bump the sha, do not delete the anchor to make it
  quiet.**

- The PreToolUse protocol shape is **observed rather than inferred** (#612, #614) — settled by
  dogfood, because telemetry structurally cannot see it: `record_hook_run` fires on printing,
  not delivery, so a rail emitting into the void still reads healthy.

## [0.13.0] — 2026-08-11

The milestone where the memory rails got their integrity checked, the evidence gate learned
to read the commands operators actually type, and CI started telling the truth about what it
ran. 39 issues closed. Three of the fixes below were only reachable *because* another fix in
this same release landed first — those interactions are called out where they matter.

**Upgrading:** schema goes 21 → 24 in three steps, applied automatically on first `connect()`.
Step 22 creates `reflection_queue`, 23 normalises every on-disk timestamp to one shape, 24
derives `ended_at` for sessions that died unclean. The steps are ordered and gated; a store
that has run any of them will not re-run it.

### Added

- **Durable reflection debt** (#514) — `session_end` counted the debt, logged "403 obs, 19
  threads", and enqueued nothing, so reflection survived exactly one exit path: the polite
  one. Every crash, kill or Remote Control timeout dropped it silently, and the
  instrumentation made the rail read as healthy while the work evaporated. One
  `reflection_queue` row per *emittable* thread now records what is owed, and the
  SessionStart banner reads that ledger instead of re-deriving from recency — so debt that
  ages past the recency window is no longer permanently invisible. Idempotent by schema
  (`UNIQUE(thread_id)`), so two sessions racing cannot double-queue.

- **Subsystem liveness — `techne doctor`** (#468 steps 1–2, #469, #487, #491) — "is techne
  installed" and "is techne WORKING" are different questions and only the first had an
  answer. Each check reports LIVE / DORMANT / STALE / UNOBSERVABLE **with a remedy the
  operator can act on**; a verdict nobody can clear teaches people to ignore the check, so
  UNOBSERVABLE is reserved for a rail that *should* be emitting and isn't, never one
  correctly idle. Includes `runtime:loaded`, which compares a long-lived process's start time
  against the deployed HEAD — a conservative staleness warning, not a tear detector, and it
  says so.

- **Operator-owned config layer for egress-selecting keys** (#393) — see README
  §"Operator-owned config". Honoured only while provably not writable by the process reading
  it; a writable layer **fails closed** to built-in defaults rather than deferring to
  `config.yaml`. Installs that deploy no layer are unaffected.

- **`techne architect ingest --verdict`, `reverdict`, `sweep-legacy-verdicts`** (#428) —
  record the operator's actual judgment (`skip` / `skip-with-flag` / `patterns-only` /
  `full-eval`) instead of a hardcoded constant. `reverdict` leaves `surveyed_at` alone:
  correcting a judgment is not a re-survey.

- **Retrieval-correctness gates** (#385, #399, #398/#403) — a regression gate for retrieval,
  a hybrid/vector gate, and deterministic eval legs per PR plus a py3.14 `eval-vec` job.

- **Per-hit retrieval provenance** (#413) and **capture / per-turn-recall rail
  instrumentation** (#479) — recorded in `context_injections`, so "is what we inject
  worthwhile?" becomes answerable from data rather than argued.

- **Banner honesty** (#488, #452/#457) — deferred (expensive) checks are named rather than
  silently omitted, and hook-registration drift is surfaced at session start.

### Fixed

- **Outbox retention that preserves the dedup ledger** (#518, #484) — 130,266 synced rows
  were never pruned. The obvious fix is the dangerous one: synced rows on the
  `archivist_knowledge` lane **are** the de-facto export ledger (the content-hash pre-filter
  reads them), so deleting them re-enqueues the entire knowledge base on the next sweep. That
  lane is now *collapsed* to one row per hash; the plain `archivist` lane ages out. Two
  tombstones keep one row per target so `COUNT`/`MAX(synced_at)` stay honest and a healthy
  lane cannot report "never synced".

- **One on-disk timestamp shape** (#515) — `observations.timestamp` held three formats
  written concurrently across the whole history. TEXT columns compare lexically, so `T`
  (0x54) sorted after `' '` (0x20) and every ISO row read as later than every space-separated
  row on the same date. Measured on the live store: a range query returned 782 rows where
  770 were correct — 12 phantom rows, ~1.5%, silently. Backfilled, and every
  writer normalises at the boundary — including the public `event_at` parameter, which the
  MCP tool schema and `--event-at` flag both document as ISO-8601, i.e. the contract *asked*
  for the shape being eliminated.

- **`sessions.ended_at` for sessions that died unclean** (#516) — 79% NULL and degrading
  (52% of June, 19% of July, 7% of August). With `ended_at` NULL the interval is open-ended,
  so "which session was live at T" matched every session ever started before T. Derived from
  last activity, with `ended_at_source` ('clean' | 'derived') as the discriminator — because
  after this change "has an ended_at" no longer means "ended cleanly", and counting derived
  ends would report a healthy store as STALE.

- **`env` is a transparent prefix, not a read-only utility** (#524) — `env` headed the
  read-only set, so `env … git commit` was dropped as navigation noise and, worse, an
  unrecognised command discarded the whole ledger row: a real 2,620-test run with one failure
  was thrown away while a 5-test run certified the suite green and *backed* a `tests_green`
  claim.

- **The evidence gate reads what actually ran** (#437, #439, #444, #445, #441) — keeps the
  tail of tool output so verdicts survive capture; stops reading a missing failure count as
  zero failures; reads totals only from commands that really run tests; sees through
  `bash -c` without changing what is captured; and treats a redacted legacy row as bounded,
  since `<redacted:…>` can shrink a row below the length threshold and an unflagged truncated
  row can otherwise contribute to a green verdict.

- **Vector hygiene** (#408, #382) — `threads_vec` accumulated one orphaned embedding per
  summary update, unbounded, because threads rotate rowid on `INSERT OR REPLACE`; and
  content-edited rows are re-embedded via a `vec_stale` flag instead of silently keeping a
  stale vector. `vectors:coverage` also stopped counting a population its remedy could not
  act on (#496).

- **CI ran fewer tests than it appeared to** (#530, #535) — a stacked PR got **no CI at
  all**, and six vector-dependent files ran in **neither** leg: they skipped in the job that
  would select them and were deselected in the job that could run them. Both closed, with
  static guards against the workflow edits that would silently reintroduce either.

- **`techne watch` no longer crash-loops forever on a missing `[watch]` extra** (#498) — a
  configuration error was being retried as though transient, every 5 seconds, forever. Exits
  78 (`EX_CONFIG`); the shipped unit sets `RestartPreventExitStatus=78`. **Both halves are
  required** — either alone still loops.

- **Coordination** (#492, #494, #446, #380) — a DORMANT twin is now a rival for the name;
  `send(to=<name>)` reads the sidecars once; read receipts key on the durable identity rather
  than the per-launch UUID; dead PIDs are reaped from `live_sessions`.

- **`fastconfig` import guards are a cost allowlist, not a techne blocklist** (#489) — the
  guard enumerated modules that must not be imported, which cannot catch a new expensive one.

- Also: atomic liveness guard on knowledge supersede/retire (#390); codex OR-fallback for
  multi-term symbol search (#392); vec-first union replacing RRF fusion (#401); archivist no
  longer enqueues redirected types into a lane that rejects them (#450); real git-worktree
  detection instead of a path convention (#434); interpreter validated before being baked
  into `~/.claude` (#458); MCP server registered through the self-healing launcher (#370); a
  discarded reflection no longer reports as a successful empty one (#465); `techne doctor`
  asks for `--posture auto` on a machine that was never initialised (#480).

### Performance

- **Hook fast path — 100ms → 50ms per hook** (#470) — stdlib-only config read.
- **Archivist stops re-enqueuing the whole knowledge base every teardown** (#475).
- **Suite runs under `pytest-xdist` — 654s → 317s of runner time** (#463), with isolation
  dirs carved out of `tmp_path` rather than `mktemp` (#466), and the suite no longer writes
  into the operator's live store (#477).
- **CI moved onto the self-hosted pool** (#424, #461).

### Documentation

- **Three suites are documented as deliberately local-only, and the claim is testable**
  (#453, #456, #546; part of #537, which stays OPEN) — `probe`, `eval_llm` and the two
  `RUN_LSP_INTEGRATION`-gated LSP
  tests each need a toolchain CI does not carry, and each would SKIP rather than fail if
  wired up naively, buying a green result that means less than it looks.
  `tests/test_local_only_suites.py` fails if repo and policy drift apart in either direction.
  Also records why `hooks.capture_read` is off: volume, not maturity. **#537 is only half
  closed and deliberately stays open**: its LSP-documentation half is addressed here, but
  its other half — the `model2vec` backend branch being exercised by no CI leg — is
  untouched by this release.
- **Cold-storage tier proposal** (#405) — graduate aged knowledge to Librarian documents.
- The arch-card freshness gate is wired into CI (#383).

### Removed

- **`search.mmr_lambda` config knob** (#420) — it defaulted to 0.3, round-tripped through
  `config.yaml`, and was never passed to any search function, so every live path ran at 0.0
  (MMR off) and setting it was a silent no-op. Removed rather than plumbed: turning it on
  would have been an unmeasured retrieval change on installs whose 0.3 was a serialized
  default nobody chose. Existing configs carrying the key still load; it is ignored, as it
  was in effect before.

<details>
<summary>Earlier detail from this cycle, kept verbatim</summary>

### Added

- **Operator-owned config layer for egress-selecting keys** (#393) — the keys that choose where memory can GO (`sync`, `connections`, `llm_api_base`, `llm_api_key`, `model`, `reflect.model`, `conflict.model`, `archivist_db_path`, `standalone`, `stores`) can now be pinned in a `config.operator.yaml` that techne honors only while the file is provably not writable by the process reading it. Resolved from `/etc/techne/config.operator.yaml` → `$TECHNE_OPERATOR_CONFIG` → `<techne_home>/config.operator.yaml`, gated once in `Config.load()`. A layer that exists but is writable by us FAILS CLOSED to the built-in default destinations rather than deferring to `config.yaml` — and `config.yaml` keeps its own values on disk either way, so an enforcing layer never destroys the file it overrules. Only `/etc/techne/config.operator.yaml` is a real lock; the other two tiers are conveniences an agent can bypass from its own environment — `$TECHNE_OPERATOR_CONFIG` with `env -u`, and the `<techne_home>` tier via `TECHNE_HOME` — and the check names the tier in force together with the lever that actually applies to it. **Operators pinning a layer must declare the WHOLE set** — an omitted key reverts to its built-in value rather than falling back to `config.yaml`. Watch `stores` in particular: an omitted store DSN does not error, it opens a new empty database at `<data_dir>/<domain>.db`, so domain memory looks intact and is not. **Installs with no operator layer are unaffected** — `config.yaml` keeps full authority, and the new `config:trust` check reports that state rather than implying a boundary nobody deployed.

- **`techne architect ingest --verdict`** (#428) — record the operator's actual judgment (`skip` / `skip-with-flag` / `patterns-only` / `full-eval`) for the URLs an ingest covers.
- **`techne architect reverdict URL --verdict X`** (#428) — correct one investigation's verdict without re-ingesting its pattern block. Leaves `surveyed_at` alone: correcting a judgment is not a re-survey.
- **`techne architect sweep-legacy-verdicts --before DATE [--apply]`** (#428) — retire pre-#428 auto-assigned verdicts to `unknown`. Dry-run by default and date-scoped, so a genuine post-fix verdict is never clobbered.

### Removed

- **`search.mmr_lambda` config knob** (#420) — it defaulted to 0.3, round-tripped through `config.yaml`, and was never passed to any search function, so every live path ran at `search_fts`'s own 0.0 (MMR off). Setting it was a silent no-op. Removed rather than plumbed because turning it on would have been an unmeasured retrieval change: every live caller passes λ=0, so `_mmr_reorder` returns at its λ=0 guard and never reaches the scoring loop. The deterministic `eval`/`eval_gate` legs install `.[dev,codex]` and lack numpy, so they cannot observe MMR at any λ — but the `eval-vec` leg CAN (it installs `[vectors]` on 3.14 with `potion-base-8M` pre-cached, and its `search(use_vectors=True)` path runs through `_mmr_reorder`), which is where a re-plumbing measurement belongs. Plumbing it would therefore have been an unmeasured retrieval change, on installs whose 0.3 was a serialized default nobody chose. Existing configs carrying the key still load; it is ignored, as it was in effect before. The `mmr_lambda=` parameter on `search_fts`/`_mmr_reorder` is unchanged and still tested.

### Documentation

- **Three suites are now documented as deliberately local-only, and the claim is testable** (#453, #456, #537) — `probe` (3 tests, #94 memory-attribution), `eval_llm` (1 test, #118) and the two `RUN_LSP_INTEGRATION`-gated LSP tests each need a toolchain CI does not carry (the `claude`/`opencode` CLI, a reachable model, `pyright-langserver` i.e. Node for two tests), and each would SKIP rather than fail if wired up naively — so covering them buys a green result that means less than it looks. ARCHITECTURE.md now states this as a decision rather than a "gotcha", and `tests/test_local_only_suites.py` fails if the repo and the policy drift apart in either direction. Also records WHY `hooks.capture_read` is off (`config.py`): volume, not maturity — `Read` is far more frequent than `Bash`, so capturing it inflates the ledger without a matching gain. It is shipped and symmetric with `capture_bash`; the only defect was that the reason lived in someone's head.

### Fixed

- **`techne watch` no longer crash-loops forever on a missing `[watch]` extra** (#498) — `deploy/techne-watch.service` shipped `Restart=on-failure` + `RestartSec=5s` with no `RestartPreventExitStatus`, while `watch()` raised a plain `RuntimeError` for the missing `watchdog` import. An operator who installed techne without the extra got a respawn every 5 seconds forever, and a journal full of tracebacks that read like a runtime fault when the fix was one `pip install`. A configuration error was being retried as though it were transient, and it never becomes non-transient. `techne watch` now exits `78` (`sysexits.h` `EX_CONFIG`) on a startup precondition that restarting cannot satisfy, and the shipped unit sets `RestartPreventExitStatus=78`, so systemd stops on the FIRST failure — no retry counter, no backoff ladder, no coordination between app and supervisor beyond one agreed constant. **Both halves are required**: either line alone still crash-loops, which is why the test derives the unit's value from `techne/exit_codes.py` rather than retyping `78`. The new `StartupConfigError` subclasses `RuntimeError` deliberately, so existing `except RuntimeError` callers are unaffected and the exit contract is opt-in at the seam that can act on it.

- **`runtime:loaded` — a long-running process holding pre-deploy modules is no longer reported as healthy** (#491) — `runtime:revision` reads git HEAD from disk, so it rendered a torn MCP server as LIVE while `techne_reflect_emit` died on an `ImportError` for a symbol that was present in both the repo and the runtime. The new check compares each long-lived techne process's start time against the deployed HEAD's committer time and reports STALE with a restart remedy. Deliberately a **conservative staleness warning, not a tear detector**: a runtime pull under a live server was measured NOT tearing it, so the check reports the ordering it observed and explicitly disclaims the inference.

- **The investigation verdict is no longer hardcoded** (#428) — `ingest_patterns_md` wrote `full-eval` for any URL group carrying patterns and `skip-with-flag` for a no-lift bullet, and the CLI could not set one, so the column recorded *whether the markdown had patterns* rather than a judgment and `patterns-only` was unreachable by any code path. The live corpus had 202 `full-eval` / 70 `skip-with-flag` / 0 `patterns-only` against 19 eval docs on disk. Verdicts are now validated against a vocabulary, and an ingest that does not state one records `patterns-only` (the documented modal outcome) instead of `full-eval`.

</details>

## [0.12.0] — 2026-07-24

### Added

- **Inter-session coordination mailbox** (#376, #378, #379) — live Claude sessions on one host can message each other over a `coord` bus (`techne_send` / `techne_inbox` / `techne_threads`): broadcast + direct addressing, session aliases, threaded replies, and a collision warning when two sessions claim the same branch or worktree. Phase 1 (core mailbox) + Phase 2 (aliases, threads, collision warning).
- **Architecture cards** (#383, #384) — an in-repo repo card (`ARCHITECTURE.md`) plus per-seam cards (`docs/arch/*.md`) with a git-anchored freshness gate and `AGENTS.md` wiring, so an agent grasps a subsystem from its card before reading source.
- **Session-end Bash backfill** (#371, #375) — capture can backfill Bash activity at session end behind a shared read-only denylist, restoring partial visibility for otherwise-uncaptured Bash-only sessions.

### Changed

- **Honest edit-rail attribution** (#377) — utilization attributes edits at symbol granularity, turn-windowed, for truthful read-vs-write accounting.

## [0.11.0] — 2026-07-16

### Added

- **Base Archivist — closed team-memory loop** (#346 PR 2) — a non-standalone
  instance now egresses live durable knowledge to the Archivist (`sink:archivist_knowledge`)
  at session end, and recall automatically federates over its `archivist.db` as a
  `team` connection. So a fact stored by one agent is read back as team memory by
  another sharing the Archivist — locally, no cabal, no git. Re-pushing the same
  fact doesn't duplicate it (the handler skips one already live in the Archivist).
  Known v1 gaps, both closed by the #223 export ledger: superseded facts leave a
  stale revision live in the Archivist (append-only, no supersession propagation),
  and every session re-enqueues all live knowledge (the outbox's partial unique
  index only collapses still-pending duplicates).

- **Base Archivist — write target + topology lane selector** (#346 PR 1, #224) — a
  `standalone` config flag selects the outbox lane for thread summaries: `true`
  (default, behaviour-preserving) keeps the `sync:` adapter lane; `false` routes to
  `sink:archivist`, a techne-native Archivist that writes pushed team memory into a
  techne-schema `archivist.db` (`archivist_db_path`, default `<data_dir>/archivist.db`).
  `_has_live_consumer` is now lane-aware (sync adapter vs. registered sink). techne
  never touches git in either lane. The federation read-back loop is PR 2.

- **Operator-editable maintenance prompts** (#219, completes the Coherence track of
  #212) — the reflect / consolidate / dream prompts that drive knowledge maintenance
  are now overridable per-install: drop a file at `<data_dir>/prompts/<name>.md` and
  the change is picked up on the next run — no code edit, no restart. A broken
  override (bad `{placeholder}`) falls back to the built-in default with a warning,
  so it can never break the maintenance loop. `techne prompts export` scaffolds the
  defaults to edit from; `techne prompts path` prints the directory. Contract in
  `docs/prompts.md`.

- **Codex edit-rail telemetry hygiene** (#342, #343) — two tuning fixes so the
  edit-rail data is trustworthy for the fix-or-cut read (#338). (1) Per-symbol
  -per-session throttle: a symbol's blast radius is injected at most once per
  session, so re-edits don't spam context or inflate utilization counts. (2) The
  injection log now records `total_hits` — the true pre-cap blast-radius size —
  alongside `hit_count` (the shown/cost count), so `techne audit` shows a
  `radius` column distinguishing a 252-dependent hub from a 10-dependent leaf.
  `context_injections` self-migrates the new nullable column.

- **Recall returns memory + librarian documents** (#340) — `techne_recall` now
  folds the top library documents (from `librarian.db`, resolved locally) into
  its result set alongside memory. Every row is labelled `origin` (`memory` vs
  `library`) so results stay clearly sourced; documents come as capped snippets
  with `source_url` for full retrieval. Capped by `search.recall_documents_limit`
  (default 3; 0 disables). Best-effort — a missing/broken `librarian.db` degrades
  to memory-only. New `librarian.recall_documents()`. Previously the 62-document
  store was reachable only via the `techne library` CLI, invisible to recall.

- **`techne audit`** (#329) — a utilization audit with two descriptive panels:
  the injection read-path (per-source events / hits / tokens / sessions /
  empty-fires over `context_injections`) and the ledger read/write ratios per
  memory domain (store-vs-recall, codex-reads-vs-edits, architect + librarian).
  `--since N` windows to the last N days; `--json` for machine output.
  Attribution ("was the injected item used") is out of scope — see #105.

- **Codex edit rail** (#335) — an opt-in PostToolUse hook (`hooks.codex_edit_rail`,
  default off) that, after an Edit/MultiEdit, maps the edit to its enclosing
  symbol and injects that symbol's blast-radius (transitive dependents — "what
  breaks if I change this") into context. Threshold-gated
  (`codex_edit_rail_min_deps`, default 3) so low-risk leaf edits stay silent, and
  capped (`codex_edit_rail_limit`, default 10, closest-first by depth). Logs every
  injection to `context_injections` (`source=codex_edit_rail`) so its utilization
  is measurable against the other read-paths (#329). New `codegraph.symbol_at()`
  maps a file+line to the innermost enclosing symbol via its span.

- **Config-driven tag inference threshold — tag revival** (#352) — `tag_threshold`
  is now a `Config` field (default `0.42`). The old hardcoded `0.6` sat above the
  model2vec (potion-base-8M) cosine ceiling (~0.59), so `infer()` tagged 0% of
  rows — the embedding stack was revived (#356) but tags stayed empty. `infer()`
  resolves the threshold from config when none is passed explicitly; the ceiling is
  backend-dependent, so it's a dial. Re-running `vectors backfill --tags` took the
  live store from 147 to 8.4k / 11.4k tagged. (Prototype over-fire on `todo` /
  `context` is tracked in #368.)

- **Per-category memory fill-state in the banner** (#334) — the session-start banner
  breaks its opaque `knowledge` total into per-type counts (+ identity) on a
  `memory:` line, so the agent can see WHERE memory accumulates and self-regulate
  what it writes. `db_stats` gains `knowledge_by_type`; non-canonical types are
  appended after the canonical ones so the line always accounts for the full total.

- **`techne profile`** (#359) — a repeatable hot-path profiling command (read-only,
  never mutates). `profile_import_cost()` measures the cold `import techne.config`
  price every hook pays before any work (#351); `profile_thread_detection()` times
  heuristic detection over the largest session and surfaces the top functions by
  cumulative time (#350). Latency-at-scale had no harness; now it does.

- **Hook budget-burn is observable** (#362) — `bounded_hook` hard-killed on timeout
  and otherwise exited 0 with no signal, so a hook creeping toward (or blowing) its
  budget was invisible — the #350 pathology where thread detection sat at 4.45s
  under a 5s budget and silently dropped detection every turn. It now warns on
  stderr at `warn_ratio` (default 0.7) of budget, and more loudly on an actual
  timeout, naming the hook + elapsed/budget.

- **Vector-channel degradation in the banner** (#358) — a missing embedding backend
  silently downgrades recall to FTS-only and a backend/model swap leaves the vec
  index stale, both with zero signal (this is how the embedding stack sat dead in
  prod, #352). `collect_health` folds `vector_channel_status` into a `vectors`
  summary and the banner alarms (⚠️) only when recall has actually degraded —
  stored vectors but no usable backend, or a stale index after a swap.

- **Schema-drift detector + v18 migration heal** (#364) — reconcile is deliberately
  additive (`CREATE IF NOT EXISTS`), so a redefinition under an existing name
  applies silently never and drift goes unnoticed. A detector now compares live
  trigger/index definitions (from `sqlite_master`) against canonical via DDL
  normalization and reports mismatches, and a v18 migration heals the two drifts
  surfaced in the perf review — without overturning the additive convention.

### Fixed

- **Codex blast-radius direction + fixture noise** (#325, PR #336) — `blast-radius`
  now defaults to dependents (`direction=in`, "what breaks if I change this")
  instead of dependencies, and `tests/fixtures/**` name-collision phantom nodes are
  excluded by default (`--include-fixtures` / `include_fixtures` to opt back in).

- **Evidence gate: passive inspection can't back a completion claim** (#105, PR #354)
  — the completion check backed on `any_tool_call`, set by ANY captured tool
  including read-only inspection (Read/Grep/Glob/WebFetch), so an agent that only
  *looked* at a file and claimed "done" got a green "backed" — the gate affirming
  work that never happened, the worst failure direction for a "no unbacked word"
  check. Split out `active_tool_call` (a deed — anything outside a new
  `PASSIVE_TOOLS` set) and back completion on `active_tool_call or artifacts`.

- **Federation: relative connection db_path resolves against TECHNE_HOME** (#332) —
  a federation connection's `db_path` had to be absolute, making a committed
  `config.yaml` machine-specific. A relative path now resolves against
  `techne_home()` at open time (absolute paths and `~` unchanged), so a shared
  config can carry `db_path: team-mirror.db` and each machine resolves it locally.
  Resolved at the open site, not config load, so the portable string survives
  save round-trips.

### Performance

- **Hook hot-path fixes + embedding/tag revival** (#356, staff review) — steady-state
  hook cost: `find_repo()` gained a per-pass path cache (thread detection was making
  ~126k `stat()` calls on a 1695-obs session by re-walking the same recurring
  paths), and feature extraction now runs once per pass. Also restored the embedding
  stack — the pipx runtime was missing `model2vec` / `sqlite-vec`, so recall had
  silently degraded to FTS-only and observations stored `tags=NULL`; vectors are
  backfilled here (the tag half lands via #352).

## [0.10.0] — 2026-07-13

### Added

- **Codex LSP-grounded call graph** — a reusable LSP client foundation (#320, PR
  #326) plus pyright-resolved call edges so callers are ground-truth rather than
  heuristic (#321, PR #327). Symbol resolution now accepts bare names and errors
  hard on an unresolvable symbol id (#300, PR #302), applied to the MCP
  blast-radius tool as well (#303, PR #304). The codex index reindexes the edited
  file on Write/Edit for mid-session freshness (#301, PR #314).

- **Architect curation** — topic-index + supersession curation (#308, PR #313),
  validity-windowed supersession (#306, PR #317), and the relation graph rendered
  as Mermaid/DOT (#305, PR #316).

- **Per-turn memory-seed nudge (#277, PR #309)** — closes the write half of the
  recall loop begun in 0.9.0. SessionStart injection is instrumented and unified
  with per-turn recall into a single `context_injections` surface, and per-turn
  recall injections are logged so the READ path is auditable (#329, PRs #330/#331).

- **Memory/skill eval harness — engine + seed corpus (#118, Phase 1, PR #312).**

- **Content-addressed spawn contracts (prototype, #318)** — handoff drift
  detection between spawner and spawned agent.

### Fixed

- architect: create the topic index in the migration, not the base schema (#315).

### Docs

- README "run vs develop" install note documenting the pinned pipx path (#297).

## [0.9.0] — 2026-07-10

### Added

- **`techne doctor` — core-DB health audit (#295, PR #296)** — a new top-level
  command that flags silent-accumulation bugs (duplicate rows a table shouldn't
  have) and reports reclaimable freelist space, with `--vacuum` to reclaim it in
  place. Exits non-zero on duplication debt, so it's safe to run in CI or a
  scheduled audit. Extensible via a `_DEDUP_CHECKS` registry of `(table, key)`.

- **Per-turn recall injection (#109 / #277)** — an opt-in `UserPromptSubmit` hook
  (`hooks.per_turn_recall`, default off) that runs a knowledge search scoped to each
  submitted prompt and injects the most relevant durable facts via
  `hookSpecificOutput.additionalContext` — the **dynamic** per-turn complement to the
  **stable** SessionStart injection. Strictly best-effort: never blocks the turn
  (always exit 0), no-ops on flag-off / empty prompt / no match / timeout, and
  excludes soft-archived rows (mirrors egress discipline). `per_turn_recall_limit`
  (default 5) caps rows per turn. The stdin prompt-field name is undocumented in the
  CC hooks reference, so the hook defensively accepts `prompt` / `user_prompt` / `text`
  and no-ops safely otherwise. (Note: whether Claude Code exploits Anthropic prompt
  caching across the stable/dynamic boundary is harness-controlled and undocumented —
  this delivers the per-turn recall capability regardless; any cache benefit is a bonus
  techne can't set from a hook.)

- **Federation brick #1: deposit-and-drain (#221)** — the first step toward
  one-distiller-many-sources memory federation.

  **Pattern:** an ephemeral producer (a short-lived agent, a CI job, a sidecar)
  deposits raw observations into a techne store it doesn't own by calling
  `record_observation(config, ...)` with a `Config` whose `db_path` points at
  the foreign store.  A single distiller (`drain.py`) then drains that store
  asynchronously — no LLM call, no blocking on the deposit path (accept-fast /
  distill-async).

  **Primitives:** the deposit and drain primitives pre-existed; this brick
  proves the round-trip with an acceptance test suite and adds a discoverability
  CLI surface.

  **Idempotency (two signals):**
  - *Signal A* — `knowledge_candidates.source_session_id`: once a session has
    been distilled (candidates written), drain.py's `NOT IN` worklist query
    excludes it on subsequent fire-ups.  Tested end-to-end.
  - *Signal B* — `.drained.json` fingerprint: drain.py filesystem state for
    empty-yield sessions (no candidates written).  Handled by drain.py; noted
    in the test comments.

  **Sovereignty:** deposit writes raw observations only; distillation stays
  the distiller's async job.  The local caller's store is never touched.

  **New CLI command:** `techne deposit --target <foreign techne home>
  --session-id <sid> --content "..."` — the explicit "deposit into a store you
  don't own" surface.  Mirrors `techne observe` in option shape; targets an
  independent foreign store.

  **Follow-up:** candidates land in the foreign store; Archivist-promotion
  (surfacing them to the owning agent's curated memory) is tracked in #222.

- **Codex db-per-repo (#209, PR #286)** — each indexed repo now gets its **own**
  code-graph db at `<data_dir>/codex/<slug>-<hash8>.db`, derived deterministically
  from the repo's resolved absolute root, replacing the single shared `codex.db`.
  Indexing a second repo can no longer collide with, overwrite, or bleed into the
  first. A pre-#209 single `codex.db` migrates to its per-repo home automatically
  (WAL-checkpointed move) the first time any consumer — CLI, hook, or MCP — resolves
  a codex db. New API: `repo_db_path` / `codex_db_for_repo` /
  `resolve_repo_db_for_query` / `list_codex_repos` / `migrate_legacy_codex_db`. New
  CLI: `techne codex index-all` (refresh every indexed repo) and
  `techne codex stats --all|--repo`. Read consumers (MCP query tools, grep-assist)
  scope to the repo under cwd, or `--repo`.

- **grep-assist — graph-augment the agent's own Grep (#206, PR #208)** — an opt-in
  `PreToolUse` hook (`hooks.codex_grep_assist`, default off) that runs a Codex symbol
  search for a Grep's pattern and injects the matching symbols (name, signature, file,
  freshness marker) into the model's context via `additionalContext`. Never blocks the
  tool; degrades to a no-op on any error, a missing/empty index, or a wall-clock
  timeout. Scopes symbols to the repo actually being searched (via the #209 db-per-repo
  router), fixing the single-global-graph signal/noise problem. `codex_grep_assist_limit`
  (default 8) caps injected symbols. Claude Code loads hooks at session start, so it
  fires from the next session after `techne init`.

- **Hook-registration drift check (#287, PR #289)** — `check_hook_drift(config)`
  compares the techne hooks enabled in config against what's actually wired into Claude
  Code's `settings.json`; `techne status` surfaces a `⚠ Hook registration drift — run
  'techne init' to reconcile` warning when they diverge (e.g. grep-assist ON in config
  but the `PreToolUse` group missing after a re-init or stray edit).

- **Test suite no longer clobbers the real `~/.claude` (#288, PR #289)** — a
  `conftest.py` guard points hook-registration tests at a throwaway home, so running the
  suite can't strip or rewrite a developer's real `settings.json`.

### Fixed

- **`thread_boundaries` duplicate-row debt (#295, PR #296)** — a schema **v17**
  migration collapses the duplicate `(session_id, obs_index_a, obs_index_b)` rows
  (keeps the most recent per triple) that had accumulated because `run_session_end`
  re-inserts a session's full boundary set every turn, and no backfill had shipped
  after the delete-before-insert write-path fix. A new `UNIQUE` index makes any future
  non-atomic regression fail loudly instead of silently re-accumulating; the redundant
  prefix index is dropped (the unique index serves `load_boundaries` as a prefix). On
  the reference DB this collapsed 76,343 rows → 9,438 and reclaimed ~26% after `VACUUM`.

## [0.8.0] — 2026-06-28

The "memory that maintains itself, honestly" release: a self-curating memory loop
that runs on the agent's own model, a matured db-per-domain architecture, a full
data-integrity audit, and a leaner, self-teaching install.

### Highlights
- **Memory hygiene on the agent's own model** — reflect, consolidate ("sleep"), and
  claim-evidence reconciliation now run via a render-prompt / answer / ingest loop using
  the model already in the room. No API key, no extra inference.
- **litellm is now optional** (`[llm]` extra) — the minimal `pip install techne` drops from
  ~100 MB to ~10–15 MB.
- **MCP self-teaching** — the server now ships an `instructions` block (the memory loop) so
  any MCP client's agent knows *when* to store/recall, not just that the tools exist.
- **Full data-integrity + robustness audit** (#165–#170).

### Added
- **Memory hygiene loop (agent's own model, no key):** resident-model fact extraction over
  MCP (#19), reflect candidate-model foundation + decision provenance (#108), consolidate
  "sleep" dedup/merge pass (#108), claim-level evidence reconciliation (#107).
- **Curated-memory drain (#107):** ephemeral memory doctor (file-reference reconciliation),
  persistent drain, and an init coexistence posture (`spool-primary`).
- **Knowledge freshness:** 4-state graded staleness for git-anchored memories (#90);
  cascade-to-uncertain dependency staleness + a recall gate that annotates and down-ranks
  uncertain rows (#117).
- **Codex code-graph domain — EXPERIMENTAL (#89):** tree-sitter symbol/edge extraction
  (Python + TypeScript), `callers`/`callees`/reachability/blast-radius graph queries, and
  `codex_search` / `codex_blast_radius` MCP tools. Indexing is manual (the graph can go
  stale; re-run `codex index`). Behind the `[codex]` extra.
- **Architect:** pattern relation/applies graph + `pattern_search` / `pattern_related` MCP
  tools (#87).
- **Operations:** cross-DB `fsck` + all-DBs-together `backup`/`restore` (#139).
- **Security:** shell-execution gates — command-chaining block + fail-open CVE pre-install
  audit (#120, `[security]` extra).
- **Eval harness:** deterministic MeME task taxonomy + observability plumbing, plus an LLM
  answerer/judge for negative-polarity tasks (#118).
- **MCP server `instructions`** (the four-step memory loop) + trigger-led `store`/`recall`/
  `search` tool descriptions.
- **sessions.platform read path (#81):** `techne status` platform breakdown and per-thread
  provenance surfaced at injection (cross-platform by design — surfaced, never filtered).
- **Deploy/runtime:** Dockerfile + `.dockerignore` (#123), systemd user unit + inotify
  watch-limit docs (#122), `TECHNE_HOME` to externalize the root (#114), honor
  `CLAUDE_CONFIG_DIR` when reading host files (#115).
- `docs/modules.md` (per-module tour) and `docs/context-architecture.md` (CC-coupling /
  durable-context decision record).

### Changed
- **litellm → optional `[llm]` extra** (was a core dependency). The default hygiene loop uses
  the agent's own model; `[llm]` is only for autonomous/headless operator-model runs. Dropped
  the unused `[anthropic]` extra. Minimal install ~100 MB → ~10–15 MB.
- **db-per-domain matured:** unified migration engine across core/librarian/architect/codex
  (#136, closes #138); `StoreAdapter` is SQLite-only — dead portability surface removed
  (#135, closes #87); personal/team data boundary enforced at write time (#137).
- Retired the hand-edited markdown patterns corpus in favor of `architect.db` (#121).
- Shared resident-model JSON-parse + prompt-hash util `core/_resident.py` (#164);
  `_get_embedding_model` moved to a neutral `core/embeddings.py`.
- Holistic README rewrite (technē framing, full extras map, agent's-own-model model section).

### Fixed
- **Data-integrity + robustness audit (#165–#170):** thread-ID construction unified through
  `thread_row_id` (#165); hook/process hardening against config + input failures (#166);
  `config.save` security + injection/staleness persistence (#167); retrieval robustness —
  empty-FTS, superseded-vec over-fetch, egress band, prototype cache (#168); migration
  idempotency + redeployed COALESCE knowledge FTS triggers (#169); codex edge corruption +
  three SELECT→INSERT TOCTOUs + sync correctness (#170).
- Empty-FTS-query crash closed in one shared guard (#143).
- CC auto-memory dir resolved via git-root slug + `autoMemoryDirectory` (#175).
- Migration cross-process race + anchor too-large degradation, now tested (#141).
- Outbox: bare pending targets migrated to the `sync:` namespace (#80); v10 migration guarded
  when the outbox table is absent (#127).
- Hooks: bounded SIGALRM timeout + exit-0 degradation on all sidecars (#126).
- `enable_load_extension` guard broadened to catch AttributeError/sqlite3 errors (#125);
  reflect persisted-thread FK + candidate-insert guard (#106); config thread fallbacks read
  dataclass defaults (#82); health capture-gap alarm keyed on last observation; staff-review
  bug sweep — supersede, reconciler, FTS, redaction (#131–#134).
- **Low-severity backlog cleanups (#188):** removed dead `hook_error`/`session_id`; documented
  the process-global `_migrated` memo; cached the fastembed availability check in `infer()`;
  `format_resume_context` trust-boundary note + closing-tag neutralisation; filled two test
  gaps (dead-row purge, mixed-batch flush). Trackers #140/#56/#83 triaged.

### Security
- `shell_guard`: anti-command-chaining gate plus an optional `pip-audit` CVE pre-install audit
  that fails open (the basic gate holds even without the auditor) — #120.

### Known limitations
- **Codex is experimental:** indexing is manual; the graph goes stale as code changes
  (auto-refresh is planned). Cross-file edges resolve for ordinary calls; some remain
  unresolved.
- **Vector/semantic recall** requires `[vectors]`; on Python 3.14 it is not yet installable
  (no `onnxruntime` wheel) and degrades to FTS-only.
- The curated-memory drain (#107) is inert when Claude Code auto-memory is disabled.

---

## Earlier releases

Condensed; see the linked issues/PRs and git history for detail.

- **v0.7.0** — db-per-domain: one db file per domain behind a `StoreAdapter` (librarian/
  architect/codex), store registry (#98), Option-D document cards, staff-review batch
  (#100–#104).
- **v0.6.2** — reliability + recall: FTS5 hardening + Porter stemming (#74/#77), SessionStart
  health banner (#76), flock-serialized WAL-safe migrations (#78), outbox gate + purge (#79),
  eval-discipline harness (#68).
- **v0.6.1** — correctness/hygiene (Fable review): FTS double-quote crash fix, search
  provenance separation, supersede guard, atomic redetect/session-end, public-API contract
  tests, `schema_version` race fix, `llm_api_key` chmod 0600.
- **v0.6.0** — embedding techne in custom runtimes: `llm_api_base`/`llm_api_key`,
  caller-supplied session IDs, `init_session_for_external_id`, `cabal` platform.
- **v0.5.1** — review pass: 2 blockers + 5 high + 8 medium fixes, +22 tests.
- **v0.5.0** — first public release: JSONL backfill, dual-column session summary (schema v4),
  user-prompt-anchored thread detection, `threads redetect`, platform package consolidation.
- **v0.4.1** — hooks rewritten to Claude Code's wrapped `{matcher, hooks:[...]}` shape.
- **v0.4.0** — heuristic thread detection (no `ANTHROPIC_API_KEY` for the default install);
  schema v3 `thread_boundaries`.
- **v0.3.1** — `init` writes `sys.executable`; `load_adapter` drops `None` kwargs.
- **v0.3.0** — argument-order standardization on `should_capture`/`add_observation` (breaking).
- **v0.2.2** — `capture_read` / `post_tool_use=False` fixes; symmetric `FilesystemAdapter.pull()`.
- **v0.2.1** — hook test coverage, `contacts.md` parser hardening, public API surface.
- **v0.2.0** — LiteLLM as default LLM router, lazy schema init, `techne.lifecycle` module.
- **v0.1.0** — initial release.

[0.8.0]: https://github.com/tenishai/techne/releases/tag/v0.8.0
