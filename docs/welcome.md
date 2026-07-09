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

## Your memory, in four verbs

Everything techne does reduces to a loop. Learn the loop and you understand the tool.

| Verb | When | What happens |
|---|---|---|
| **Recall** | Before assuming | The agent asks its memory before it guesses. `techne_recall(topic)` at the start of a task; `techne_resume` at the start of a session. |
| **Store** | When it learns something durable | A fact, a decision, a correction, hard-won reasoning — `techne_store` writes the *conclusion*, not the transcript. |
| **Reflect** | When wrapping up | At session end, techne distills each thread of work into candidate knowledge — the agent answers its own reflection prompts with its own model. |
| **Sleep** | While idle | Dedup overlapping entries (*consolidate*), surface non-obvious connections (*dream*), and check that merges didn't drop load-bearing claims (*faithfulness*). |

The thing to internalize: **you never have to run the loop by hand.** Sessions are
captured by hooks. Threads are detected automatically at session end. The hygiene
passes are prompts your agent answers between tasks. Your job is to review what
lands in the candidate queue and promote what's worth keeping (`techne reflect
review`) — memory is *yours*, so nothing touches the live store without a nod.

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
- **[`docs/modules.md`](./modules.md)** — a short sales-pitch for each module (what it
  does, why it's different, how to enable it).
- **[`docs/codex-usage.md`](./codex-usage.md)** — when to reach for the code graph and,
  just as important, when *not* to.
- **[`docs/memory-system-architecture.md`](./memory-system-architecture.md)** — how the
  recall / store / reflect machinery actually works under the hood.

Welcome aboard. Your agent has a memory now — go give it something worth remembering.
