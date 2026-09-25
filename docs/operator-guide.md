# Operator guide

*Habits we learned running techne on our own machines — read this after
[welcome.md](welcome.md), alongside the [README](../README.md).*

The [welcome guide](welcome.md) tours what techne does. This guide is narrower:
the recommended hook configuration, and five things that cost us real time
before we wrote them down. None of it is techne-internal — it is how to run an
agent that has techne, without the traps.

---

## Recommended configuration

These hooks ship **on by default**. Each one is a small, best-effort injection
into your agent's context; none of them block a turn, and all of them degrade
to a no-op on error.

| Hook | What it does for you | What it costs |
|---|---|---|
| `hooks.per_turn_recall` | On every prompt, searches memory against what you typed and injects the most relevant durable facts. This is the one that makes memory feel present — without it, the agent has to decide to call `techne_recall` itself, and in practice it mostly doesn't (see [the recall rule](#the-recall-rule-and-its-limit) below). | Up to `per_turn_recall_limit × per_turn_recall_chars` extra context per turn (default 5 × 1000 = 5,000 characters), plus one search inside the hook's 5-second budget. |
| `hooks.capture_bash` | Records Bash calls live, during the turn, so `techne_check_evidence` can verify a claim like "tests passed" in the same turn it is made. The transcript backfill captures the same calls later either way — this setting is about *when* they become visible, not whether they get captured at all. | A bigger observation ledger. `capture_read` stays off by default for the same reason: Read fires far more often than Bash, and capturing it would inflate the ledger without a matching gain. |
| `hooks.codex_grep_assist` | When the agent greps for a symbol, it also gets the matching code-graph symbols (definition, signature, file) injected before the grep result comes back. | Only fires in repos you have indexed with `techne codex index <repo>`. |
| `hooks.codex_edit_rail` | After an edit, shows what depends on the changed symbol — "what breaks if I change this." Fires only when there are at least 3 dependents, so leaf edits stay silent. | An injection per non-leaf edit. Needs an indexed repo. |
| `hooks.codex_reindex_on_edit` | Re-indexes just the file you edited, so the two codex rails above don't answer from a stale graph between sessions. | A reindex after each edit. |
| `hooks.per_turn_recall_chars` | Default is 1000, not the code's historical 450. Longer injected facts mean fewer get cut before the part that matters — a truncated row still ends in `…`, so you can always tell. | Raises the per-prompt ceiling above (1000 vs 450 chars per row, at the same row limit). |

**Two more capabilities exist and are worth knowing, but stay opt-in** — turn
them on if the shape fits your work, not by default:

| Hook | What it does | Why it isn't default |
|---|---|---|
| `hooks.per_turn_seed` | Nudges the agent to write down a durable fact or decision the moment it emerges, instead of waiting for the end-of-session reflect pass to reconstruct it from the transcript. | Narrower payoff than `per_turn_recall`, and easy to get chatty if the agent over-triggers it. |
| `hooks.startup_directive` | On your first prompt of a session, tells the agent how to use techne's tools — once, then silent for the rest of the session. | Most agents pick this up from `AGENTS.md`/`CLAUDE.md` already; the hook is for clients that don't load one. |

Run `techne capabilities` to see this list against **your own store** — what's
on, what's off, and (for what's off) the actual numbers from your ledger that
make the case for turning it on. Run `techne capabilities --json` for the same
report as data.

The report also checks three things that are not hook flags, by looking at the
machine: `vectors_backend` (is an embedding backend installed, or is search
keyword-only), `vectors_model` (is its model downloaded), and
`single_memory_layer` (is Claude Code's auto-memory running beside techne; not
checked under `spool-primary`). Each comes with the command or setting that
fixes it.

**Leaving something off on purpose** is a legitimate choice, so you can say so:
`techne capabilities ack per_turn_seed --reason "too chatty"`. The item stays in
the report, marked as your choice, and stops counting toward the session-start
"N capabilities are off" line. Acknowledgements don't expire; `techne
capabilities unack <flag>` takes one back. An agent can record the same thing
through the `techne_capabilities` tool, but only after you've said so.

**Turning any of this off** is one line in `~/.techne/config.yaml`:

```yaml
hooks:
  per_turn_recall: false
  capture_bash: false
  codex_grep_assist: false
  codex_edit_rail: false
  codex_reindex_on_edit: false
```

> **Two of these need `techne init` and a new session; the rest apply on the
> next tool call.** `per_turn_recall` and `codex_grep_assist` decide which hooks
> are registered in Claude Code's `settings.json`, and Claude Code loads that
> hook chain at session start, so run `techne init` and restart after changing
> either. `techne status` warns if config and the registered hooks have drifted
> apart. `capture_bash`, `codex_edit_rail` and `codex_reindex_on_edit` are read
> by the always-registered PostToolUse hook each time it runs, so they apply
> immediately, mid-session, with no init.

`techne init` also handles two related setup steps for you: it downloads the
embedding model once so vector search works without a separate step (pass
`--no-vectors` to skip the download on a slow or offline connection), and it
asks before turning off Claude Code's own auto-memory rather than doing it
silently — see [why that matters](#turn-off-claude-codes-own-auto-memory)
below.

### Turn off Claude Code's own auto-memory

Running Claude Code's own auto-memory (`autoMemoryEnabled` in
`settings.json`) alongside techne is two parallel memory layers writing and
reading independently, which causes drift between them and can double-inject
the same fact from both sources. Turn `autoMemoryEnabled` off if techne is
your memory layer. `techne init` offers to turn it off for you, and never
flips it silently. It only asks when both ends are a terminal; otherwise it
prints the recommendation. Under `spool-primary` (below) it does not offer,
because techne is then deliberately working alongside that memory.

Moving from `spool-primary` to techne alone does not bring Claude Code's
memory with you. Your `MEMORY.md` files stay where they are. `techne memory
sync` mirrors one project's files for the claim-gate only, and nothing serves
them back into context. techne writes memory of its own only with
`reflect.enabled: true` in `~/.techne/config.yaml`. To switch: turn reflect on,
set `autoMemoryEnabled: false` in `~/.claude/settings.json`, then run
`techne init --platform claude-code --posture observe`. Passing the posture
matters: a bare `techne init` re-detects the memory files and resolves back to
`spool-primary`.

This is not the same question as whether techne *detects* existing CC memory
files: `techne init` always checks the project directory for them
(`find_curated_memory_files`), and if it finds any, sets techne's own posture
to `spool-primary` — which silences techne's unattended auto-promote so it
never drifts against CC's authored memory, regardless of whether
`autoMemoryEnabled` is on. That is a safety net for coexistence, not a
substitute for turning auto-memory off. `techne status` prints the resolved
posture (`Posture: observe` or `Posture: spool-primary`) so you can see which
one is active.

### Checking it works

```bash
techne doctor          # per-subsystem liveness: each active hook reports live /
                        # dormant / stale / unobservable, with a remedy if it isn't live
techne capabilities     # what's on, what's off, and what turning on the rest would buy you
```

`techne doctor`'s liveness checks are the ground truth for "is this actually
running," not just "is it configured" — a hook can be enabled in
`config.yaml` and still be missing from Claude Code's `settings.json` (a
stray edit, an interrupted `init`), and liveness catches that the way a
config read cannot.

---

## The recall rule, and its limit

Tell your agent, in its own instructions file: **call `techne_recall` before
answering from belief.**

`per_turn_recall` (above) does this automatically, but only for what you
*typed*. It hooks `UserPromptSubmit`, so pasted content, replayed transcripts,
handed-back results from another agent, and tool output never trigger it —
nothing gets searched or injected for those. An agent that relies on the hook
alone will call `techne_recall` close to never, because the hook is already
doing the job for typed prompts and there is no remaining prompt to trigger
it on. Measured: one full session with the hook on made zero explicit
`techne_recall` calls across roughly 3,500 transcript lines — the hook
covered every turn that looked like a question, so the agent never had a
reason to call it by hand (tracked as techne#1018). When the source of truth
is something other than a typed prompt — a pasted spec, a file you just read,
another agent's report — say so and call `techne_recall` yourself.

---

## Knowledge-row vs. issue references

`techne#N` renders knowledge rows as `⟦techne#N⟧` in the context it injects.
That means a bare `#N` is ambiguous between *a GitHub issue* and *a knowledge
row* the moment both numbering spaces exist in the same conversation.

The rule that matters for anything a **machine** parses — PR bodies, commit
messages, issue bodies, review comments — is narrower than a style
preference: GitHub's closing keywords (`Closes #N`, `Closes owner/repo#N`)
match only that exact shape. A PR body once wrote `Closes 🎫techne#831` and
bound no issue at all — it merged with issue #831 still open, past several
review passes, because the text reads correctly to a person and fails
silently for the parser. Use plain `issue #N` and `knowledge #N` (or
`owner/repo#N` for a closing keyword) in anything machine-read. A decorated
or prefixed reference is fine in a chat reply or a message to another agent,
never in text GitHub or a script has to match exactly.

---

## The model-pin trap

Two independent ways a "pinned" model quietly isn't:

1. **A floating alias resolves at session start.** `"model": "opus"` in
   `~/.claude/settings.json` (Claude-Code-specific) picks whatever `opus`
   currently means the moment a session starts — not what it meant when you
   set it. A long-lived session, or a fleet you don't restart often, drifts
   onto a newer model with nobody choosing it.
2. **A `--model` launch flag beats the settings pin.** Even with a full model
   ID pinned in settings, a launcher or script that passes `--model opus` on
   every start overrides it — the alias wins at launch time regardless of
   what settings.json says. One fleet pinned a full model ID in settings but
   launched every session with `--model opus` on top of it; the pin never
   actually took effect, and every session moved to the next model the day
   it shipped, with nobody choosing it.

Pin **and** launch with a full model ID, never an alias, on both sides. To
check what a session actually ran — not what you intended — read the `model`
field in its session JSONL rather than trusting the launch command or the
settings file in isolation.

---

## Read prompting guidance, don't recall it

Per-model prompting behavior changes between model releases, and it is easy
to get wrong from memory: what worked as a steering instruction for one model
can be a no-op, or actively counterproductive, for the next. If your
toolchain ships versioned prompting guidance (Claude Code's bundled
`claude-api` skill carries `shared/prompt-audit.md` and
`shared/model-migration.md` for this reason), read it fresh rather than
relying on what you remember about a model's quirks — the guidance updates
with the tool; your memory of last quarter's advice does not. Re-check any
instruction block you wrote for steering a specific model after a model
change, rather than assuming it still applies.

A related trap: never write a model's identity or knowledge cutoff into a
long-lived instructions file (`CLAUDE.md`, `AGENTS.md`). Those files are
injected into every session, so a cutoff or model-name claim that was true
when you wrote it becomes a standing source of confusion the moment the
underlying model changes and the file doesn't. Read the model's own
self-report for that information instead.

---

## Imports can fail silently

**Claude-Code-specific.** A `CLAUDE.md` line like `@AGENTS.md` imports another
file's contents into context — and if the path doesn't resolve, the failure
is invisible: no error, no warning, just an import that silently did
nothing. That reads identically to one that worked, until an agent visibly
doesn't know something the imported file was supposed to teach it. Put a
one-line fallback after every import:

```
@AGENTS.md
(If this import did not resolve, read AGENTS.md directly.)
```

---

## Where to go next

- **[welcome.md](welcome.md)** — the first-timer tour: the memory loop, the
  four functions, the features you'd never find on your own.
- **[README](../README.md)** — the full reference: every CLI command and
  config key, including the complete `hooks:` block this guide summarizes.
- **[AGENTS.md](../AGENTS.md)** — the agent-facing version of everything in
  this guide, short and imperative, plus the doc's own silent-failure
  triggers.
- **`techne capabilities`** — what's on, what's off, and what turning on the
  rest would buy you, measured against your own store.
