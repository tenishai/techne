# Welcome to your new memory

*A first-timer's guide to techne — read this before the [README](../README.md).*

You just installed a memory. Not a database you have to babysit, not a note-taking
app you have to remember to open — a memory that fills itself in while your agent
works, tidies itself while it's idle, and hands the important parts back the next
time they're needed. This guide is the tour: what you actually got, the good bits
you'd never find on your own, and the five things worth turning on.

The [README](../README.md) is the reference manual — every flag, every command.
This is the brochure. Start here.

---

## The one-paragraph pitch

Your agent starts every session cold. It re-derives the same facts, re-learns your
preferences, re-discovers the decision it made last week, forgets the thing it swore
it would remember. techne fixes that with **one local SQLite file** (`techne.db`) —
yours, offline, `sqlite3`-inspectable — that records *what the agent did*, *what it
learned*, and *who it works with*. The clever part isn't the storage. It's that
techne keeps the memory clean **using the model already running your agent** — no API
key, no second model, no extra inference bill. It reflects, dedupes, and fact-checks
itself on your own tokens.

---

## Don't read this — ask it

This guide is a snapshot; your install is the truth. techne can tell you what it does
**on your machine**, using your own store:

```
techne capabilities
```

Or just ask your agent — *"what can techne do?"* — and it calls `techne_capabilities`
and talks you through it.

It answers the question a document cannot: not "what does techne have", but **what is
switched off here, and what would turning it on buy you**, with the numbers from your
own ledger attached. A fresh install reports **8 of 8 capabilities off**, because
nearly everything below is opt-in by design. That is the honest starting point, and
this guide exists to tell you which of the eight are worth it.

It also reports the bundled workflows that aren't on your disk — something no config
flag can see.

---

## Your memory, in four verbs

Everything techne does reduces to a loop. Learn the loop and you understand the tool.

| Verb | When | What happens |
|---|---|---|
| **Recall** | Before assuming | The agent asks its memory before it guesses. `techne_recall(topic)` at the start of a task; `techne_resume` at the start of a session. |
| **Store** | When it learns something durable | A fact, a decision, a correction, hard-won reasoning — `techne_store` writes the *conclusion*, not the transcript. |
| **Reflect** | When wrapping up | At session end, techne distills each thread of work into candidate knowledge — the agent answers its own reflection prompts with its own model. [Full guide](./reflect-guide.md). |
| **Sleep** | While idle | Dedup overlapping entries (*consolidate*), surface non-obvious connections (*dream*), and check that merges didn't drop load-bearing claims (*faithfulness*). |

The thing to internalize: **you never have to run the loop by hand.** Sessions are
captured by hooks. Threads are detected automatically at session end. The hygiene
passes are prompts your agent answers between tasks. Your job is to review what
lands in the candidate queue and promote what's worth keeping (`techne reflect
review`) — memory is *yours*, so nothing touches the live store without a nod.

---

## And four functions — who you ask

The four verbs are what your memory *does*. The four **functions** are who you *ask*.
Everything techne knows sits behind one of four names, and you address each one by
name:

| | The question it answers |
|---|---|
| **archivist** | *What does the team know?* |
| **librarian** | *What is written down?* |
| **architect** | *What is worth borrowing?* |
| **codex** | *What does the code look like?* |

```bash
techne bindings
```

```
  archivist  local        resolves to this instance's own knowledge — no second store
  librarian  local        documents at the default librarian.db (created on first write)
  architect  local        patterns at the default architect.db (created on first write)
  codex      absent       no index for the active repo — `techne codex index <repo>`
```

That is a genuinely fresh install, and **three of the four say `local` while the codex
says `absent` — both are correct answers rather than degraded ones.**

`local` means *"this resolves to something here"*. The archivist resolving to your own
knowledge is what *"what does the team know"* means when the team is you. The same call keeps working when a
team store appears — the answer's `binding` and `target` change, and it *tells you*
they did, so you never have to guess which store you just read.

`absent` means *"there is deliberately nothing here"*, and **which domains can say it
is decided by the domain, not by you**. On the codex it means *you have not indexed this
repo yet* — a fact about where you are standing rather than a fault, and the row hands you
the command. On the archivist it means *you configured `archivist: none`*. **The librarian
and architect have no `absent` at all** — they always resolve somewhere, so the only
question they can raise is whether that somewhere is readable. (Full statement:
[reading the answer](four-functions.md#reading-the-answer).)

That is the whole reason to learn four names instead of four file paths: `librarian.db`
is where documents happen to live today; **the librarian is the function you are
asking.** One of those survives your setup growing. The other does not.

You will rarely call them by hand at first — `techne_recall` escalates across all four
for you, and that is the right default. Reach for a name when you already know which
function you want.

**→ [The four functions](four-functions.md)** — the tutorial: ask each one, read the
`binding`, and watch the same call answer from a different store.

---

## The good bits you'd never find on your own

These are the features a first-time downloader has no way to discover — the ones that
make techne feel less like a log and more like a colleague who remembers.

### 🔎 grep-assist — your own Grep, augmented with the code graph

When you Grep for a symbol, techne can **silently inject the matching code-graph
symbols** (definitions, signatures, the file they live in) into your agent's context
— *before* the Grep even runs. You search for `authenticate`; your agent also learns
there's a `class AuthMiddleware` in `auth/middleware.py` with this signature, whether
or not the grep text hit it.

It's a `PreToolUse` hook, and it's built to be invisible when it has nothing to add:
it never blocks the tool, degrades to a no-op on any error, and scopes symbols to the
repo you're actually searching (never bleeds another project's code in). **Opt-in and
experimental** — off by default. Turn it on:

```yaml
# ~/.techne/config.yaml
hooks:
  codex_grep_assist: true       # default: false
  codex_grep_assist_limit: 8    # max symbols injected per Grep
```

Then `techne init` to (re-)register the hook. It only fires for repos you've indexed
with Codex (below). *Claude Code loads hooks at session start — restart the session
after enabling.*

### 💬 Per-turn recall — the right memory, right when you ask

At session start, techne injects a **stable** block of durable knowledge once. But
what's relevant to *this* prompt changes turn to turn. With per-turn recall on, a
`UserPromptSubmit` hook searches your knowledge for each prompt and injects the
handful of facts most relevant to it — the **dynamic** complement to the stable
session-start block. So "how did we decide the auth flow?" pulls the actual decision
into context the moment you ask, without you running a recall by hand.

Best-effort by design: it never blocks or delays your turn, and stays silent when
nothing relevant turns up. **Opt-in, experimental** — off by default:

```yaml
# ~/.techne/config.yaml
hooks:
  per_turn_recall: true       # default: false
  per_turn_recall_limit: 5    # max facts injected per prompt
```

Then `techne init` and restart the session to load the hook.

### 🗺️ Codex — a code graph, one per repo

Codex is a code graph — typed **symbols** plus call / import / def-use **edges** — in
a SQLite file. It answers the transitive questions humans do badly by eye: *what calls
this*, *is this reachable*, *what's the full blast radius if I change it*.

The part worth knowing: **each repo gets its own graph.** Index three projects and you
get three independent databases at `~/.techne/codex/<repo>-<hash>.db` — indexing a
second repo can never collide with, overwrite, or bleed into the first. The path is
derived deterministically from the repo's absolute root, so same-named repos in
different locations stay separate.

```bash
pip install -e ".[codex]"                 # tree-sitter extraction (Python + TS)
techne codex index /path/to/repo          # build this repo's graph
techne codex index-all                    # refresh every repo you've indexed
techne codex stats --all                  # every indexed repo and its counts
techne codex search "handleRequest"       # find symbols
techne codex blast-radius <symbol-id>     # what a change ripples into
```

**Experimental, and honest about it:** indexing is manual, so the graph goes stale as
code changes — re-run `codex index` to refresh. Every symbol injected by grep-assist
carries a freshness marker if the graph is behind the working tree, so your agent
knows when to double-check. (Reach for Codex when a graph is *too big to hold in your
head* or needs *deep reachability* — for a quick "where's X" on a file you're already
reading, plain Grep still wins. The full when-to-use-it philosophy is in
[`codex-usage.md`](./codex-usage.md).)

### ⏰ Reminders that survive a restart

Your agent can leave itself a note for a *future* session — `techne_reminder_add`
with a due time — and it resurfaces automatically when it comes due, even after a full
restart. This is how a multi-session task ("after the restart, verify the hook
actually fires") carries its own follow-up forward instead of relying on you to
remember. `techne_reminder_list` / `techne_reminder_dismiss` manage them.

### 🩺 A memory that notices when it's misconfigured

`techne status` doesn't just print counts — it **checks that the hooks you enabled in
config are actually wired into Claude Code**, and warns if they drifted apart:

```
⚠ Hook registration drift — run 'techne init' to reconcile:
    ...
```

This exists because a hook can be flipped on in config but silently missing from
`settings.json` (a re-init, a stray edit, a test run). The check turns a
mystery ("why isn't grep-assist firing?") into a one-line fix. Run `techne status`
whenever something feels off.

### 🧾 The honesty features

techne is opinionated that a confident wrong memory is worse than no memory:

- **Evidence gate** (`techne evidence <session>`) — before your agent claims it
  committed / tested / shipped something, it can check that assertion against the
  observation ledger of what it *actually did*.
- **Faithfulness check** — when the sleep cycle merges two entries, it verifies the
  merged text didn't quietly drop a load-bearing claim before you promote it.
- **Freshness markers** — recalled memory anchored to a file that has since changed,
  moved, or been deleted comes back flagged *verify before relying*, not as fact.
- **Uncertain memory is filtered, not merely ranked down** — an entry marked
  `status='uncertain'` is something techne is on record as *not believing*. It stays
  out of what gets injected rather than being quietly pushed to the bottom, so
  "we wrote this down but don't trust it" is a state the store can actually express.
- **The code graph marks its guesses** — when a call can't be resolved to a real
  symbol, techne matches it by bare name, which can attach a call to an unrelated
  function that happens to share it. Those rows now come back tagged
  `resolution: name-inferred`, and `codex_blast_radius` tells you how many of its
  results rest on a guess. Ask for only what it can prove with `only_proven`.
- **Checks that can go green** — `techne doctor` and `techne fsck` name a remedy
  command instead of just reporting a fault, because a check that can never pass
  trains you to ignore it. `techne fsck` also refuses to guess: it will repair the
  faults with one safe answer and hand you the ones that need a judgment call.

### 🧹 Two workflows that ship with the tool

Installed into `~/.claude/commands/` by `techne init`, and versioned **with** techne
rather than through a skills marketplace — they drive its CLI and know the store
layout of the release they came from, so a procedure written for 0.12 can be wrong
for 0.15.

- **`/self-care`** — the periodic hygiene pass. Sweeps the outbox, drains the rails,
  prunes backups and snapshots, reclaims disk, and reschedules itself. Dry-run first;
  the destructive halves are gated behind an explicit preview. One early run swept
  4,447 outbox rows and reclaimed 843 MB.
- **`/techne-promote`** — turns accumulated observations into durable knowledge. Your
  agent answers techne's reflection prompts with its own model, so no API key is
  involved. This is the one that grows the store.

**An upgrade does not re-run `techne init`**, so after upgrading run `techne skills
install` to refresh them. `techne skills status` tells you where you stand, and it
never overwrites a copy you have edited yourself.

---

## What we set up — and why you should too

If you're reading this because someone handed you a machine with techne already
running, here's the shape of a healthy setup, and why each piece is there:

1. **`techne init --platform claude-code`** wires the lifecycle hooks (session start,
   end, post-tool-use, pre/post-compact) and registers the MCP server. From then on
   every session is captured and thread-summarized with zero effort.
2. **Turn on grep-assist + index your active repos with Codex.** This is the
   difference between "memory of past sessions" and "memory that also understands the
   code in front of you." Index the repos you actually work in; skip the ones you
   don't.
3. **Restart the session after changing hooks.** Claude Code loads hooks at session
   *start*, so a newly-registered hook (like grep-assist) doesn't fire until the next
   session. Enable it, restart, then confirm with a real Grep that symbols get
   injected — that's the go-live signal, not just "the hook is registered."
4. **Run `techne status` after any hook change.** If it reports drift, `techne init`
   reconciles it. No drift warning = you're live.
5. **Review the candidate queue periodically** (`techne reflect review`). The sleep
   cycle proposes; you dispose. Promote what's true, discard what's stale — this is
   the one recurring bit of curation the tool asks of you, and it's what keeps the
   store sharp instead of bloated.

Why bother with all of it? Because the payoff compounds. A memory you set up and walk
away from is a log. A memory with the loop running, the code graph indexed, and the
queue reviewed is an agent that *actually gets better at your work over time* — which
was the whole point.

> **Contributor note:** running the test suite is safe — techne's tests are guarded so
> they operate on a throwaway home and never clobber your real `~/.claude` settings.

---

## Where to go next

- **[README](../README.md)** — the full reference: every CLI command, config key, and
  the library API for embedding techne in a non-Claude-Code runtime.
- **`techne capabilities`** — what is on, what is off, and what each would buy you,
  measured against *your* store. Start here rather than with any document, this one
  included.
- **[`docs/modules.md`](./modules.md)** — a short sales-pitch for each module (what it
  does, why it's different, how to enable it).
- **[`docs/threads.md`](./threads.md)** — sessions vs threads: why the split exists, the
  signals that make it, and how a line of work survives a `/clear`.
- **[`docs/four-functions.md`](./four-functions.md)** — the four domains, addressed by
  name: what each answers, how to read the `binding` it resolved to, and the same call
  against a different store.
- **[`docs/mcp-tools.md`](./mcp-tools.md)** — every MCP tool your agent can call,
  grouped by purpose. The complete agent-facing surface in one place.
- **[`docs/codex-usage.md`](./codex-usage.md)** — when to reach for the code graph and,
  just as important, when *not* to.
- **[`docs/reflect-guide.md`](./reflect-guide.md)** — the Reflect verb in full: the two
  switches people confuse, where candidates land, and how to drain a backlog.
- **[`docs/memory-system-architecture.md`](./memory-system-architecture.md)** — the
  multi-domain / federation model: how techne behaves as one store among several.

Welcome aboard. Your agent has a memory now — go give it something worth remembering.
