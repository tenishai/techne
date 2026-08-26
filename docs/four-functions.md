# The four functions

techne has four knowledge domains, and you address each one **by name**:

| | The question it answers |
|---|---|
| **archivist** | *What does the team know?* |
| **librarian** | *What is written down?* |
| **architect** | *What is worth stealing?* |
| **codex** | *What does the code look like?* |

That is the whole vocabulary. This page walks you through asking each one, reading
what comes back, and — the part that matters most — watching the **same call** answer
from a different place when the infrastructure underneath it changes.

**Who types what.** `archivist_search(...)` and its three siblings are calls your *agent*
makes over MCP — you will normally ask in English and it will pick one. Anything shown as
a shell command (`techne bindings`, `techne librarian search`) is something *you* type.
Each section below gives both.

> **Why learn four names instead of one search box?** Because the names outlive this
> tool. An operator learns *archivist, librarian, architect, codex* in techne and keeps
> them when the work grows into liminal and cabal, where a local librarian may become a
> persona and later a deployed service. The call you type does not change. What it
> resolves to does, and it tells you.

---

## Before you start

You need a techne install with something in it. If you just ran `techne init`, do a
little work first — the domains answer from what you have, and an empty store is an
honest but boring teacher.

```bash
techne bindings
```

```
  archivist  local        resolves to this instance's own knowledge — no second store
  librarian  local        documents at the default librarian.db
  architect  local        patterns at the default architect.db
  codex      local        index at ~/.techne/codex/techne-5e47adfd.db
```

Four rows, always. This is the map: **what each name currently resolves to**. Come
back to it whenever an answer surprises you.

**Yours will differ, and that is expected** — the block above is a populated install. A
fresh one shows the codex `absent` and the lazily-created dbs flagged; see
[the welcome guide](welcome.md) for that output.

---

## 1. The archivist — what does the team know?

The archivist is the one you reach for when the question is *"have we decided this
already?"*

```
archivist_search("surveyed_at")
```

```json
{
  "domain": "archivist",
  "binding": "local",
  "state": "readable",
  "target": "/Users/you/.techne/techne.db",
  "count": 2,
  "results": [
    {
      "content": "MEASURED 2026-08-24 — \"THE SCHEMA CANNOT TELL YOU\" IS NOT \"THE SYSTEM ...",
      "knowledge_type": "evaluation",
      "source_table": "knowledge",
      "id": 1355
    }
  ]
}
```

CLI equivalent: `techne archivist query` runs one read-only `SELECT`, and
`techne archivist schema` shows you what you can select from.

**On a solo install the archivist is bound `local`** — it resolves to your own
knowledge, and there is no second store. That is not a degraded mode. It is the
correct answer to *"what does the team know"* when the team is you, and it is why the
call works identically before and after you have a team.

## 2. The librarian — what is written down?

Documents: your session transcripts, indexed markdown, the cards that point at them.

```
librarian_search("prudence")
```

```json
{
  "domain": "librarian",
  "binding": "local",
  "state": "readable",
  "target": "/Users/you/.techne/librarian.db",
  "count": 2,
  "results": [
    {
      "title": "we were working on a cleanse of our code bases yesterday, where did we ...",
      "content": "## User\n\nwe were working on a cleanse of our code bases yesterday, ...",
      "id": 190
    }
  ]
}
```

CLI: `techne librarian search`, `techne librarian show <id>`, `techne librarian card <id>`.

The role is the *question* — "what is written down" — not a particular file set. That
is what lets the corpus grow (transcripts today, a design-docs directory tomorrow)
without the call changing. See [Using the Librarian](librarian-guide.md) for getting
your transcripts in.

## 3. The architect — what is worth stealing?

Architectural patterns lifted from other people's code.

```
architect_search("memory")
```

```json
{
  "domain": "architect",
  "binding": "local",
  "state": "readable",
  "target": "/Users/you/.techne/architect.db",
  "count": 2,
  "results": [
    {
      "name": "LLM-agency memory: self-edit tools writing persistent memory mid-task",
      "id": "87e470e32bdee4ea"
    }
  ]
}
```

CLI: `techne architect search`, `techne architect ingest`.

`architect_search` and `pattern_search` read the **same corpus**. The difference is
which way you are thinking: `architect_search` addresses the *domain*,
`pattern_search` names the *mechanism*. Use whichever matches the question in your
head — they are not two stores.

## 4. The codex — what does the code look like?

Symbols and the edges between them. Two calls rather than one, because the graph
answers two different questions.

```
codex_search("search_vec")
```

```json
{
  "domain": "codex",
  "binding": "local",
  "state": "readable",
  "target": "/Users/you/.techne/codex/techne-5e47adfd.db",
  "count": 2,
  "results": [
    { "name": "search_vec", "id": "techne/core/search.py::search_vec::function::552" }
  ]
}
```

Then `codex_blast_radius("techne/core/search.py::search_vec::function::552")` answers
*"what breaks if I change this"*.

CLI: `techne codex index <repo>` first — **the index is built by hand and can be
stale.**

**And it resolves from your working directory, which is a silent argument.** `codex_search`
takes the repo from `repo` *or your cwd*, walking up to the repo root — and a linked
worktree resolves to its **primary**, which is one more way cwd is a silent argument. So the identical
call answers differently depending on where you are standing — and the dangerous case is not
the miss, it is the **confident hit against the wrong repo**. Measured, two indexed repos both
defining `shared_symbol_name`, one call:

```
cwd = tinyrepo  (indexed) : {"binding":"local","count":1,"target":".../tinyrepo-750d265f.db"}
cwd = otherrepo (indexed) : {"binding":"local","count":1,"target":".../otherrepo-7e3d1fa9.db"}
cwd = no repo             : {"binding":"absent","count":0,"target":null}
```

Rows 1 and 2 are the same `binding`, the same `count`, and a same-shaped `id`. **Only `target`
differs.** `core/bindings.py` says it plainly: *a codex bound to some other repo's index would
answer confidently about the wrong code.* The `absent` in row 3 is the honest failure; row 2 is
the one that will mislead you. **Pass `repo` explicitly when it matters, and read
`target`.**

**And separately from *which* repo — how *old* it is.** The codex is the one domain that
will confidently tell you about code you deleted last week if you have not re-indexed.
Two different axes: the wrong repo above, a stale snapshot here. See
[codex usage](codex-usage.md) for when to reach for it and when not to.

---

## Reading the answer

Every domain answers in **one shape**, and four of its fields are the same four every
time:

| field | what it tells you |
|---|---|
| `domain` | which of the four answered |
| `binding` | `absent` · `local` · `store` — *what kind of thing* it resolved to |
| `state` | `readable` · `unavailable` — whether it could actually be read |
| `target` | the concrete path, so you can go look |

Four shapes for four domains would put the burden of knowing which one you got back
onto you. That is exactly the seam this vocabulary removes.

> **A resolver that throws does not degrade — it raises.** `techne bindings` catches per
> domain, so a failed resolver becomes a row carrying the reason verbatim — whichever domain
> it was. Plain `techne bindings` renders that row as `unavailable`; the literal
> `binding: null` is what you see under `--json`. The *search* calls do not catch: `archivist_search`,
> `librarian_search` and `architect_search` let a resolver failure out at you. The one you can
> actually trigger today is the archivist's — point `archivist_db_path` at your personal db and
> `archivist_search` raises. The librarian and architect have no known misconfiguration that
> reaches it; they simply would not catch one. **`codex_search` is the exception** — its
> resolver catches internally, so it returns `binding: null` with a `notice`. Handle both: a
> try/except *and* a `notice` handler.

### An unreadable domain answers with a reason, not an empty list

Point the archivist at a store that is not there:

```json
{
  "domain": "archivist",
  "binding": "store",
  "state": "unavailable",
  "target": "/nope/does-not-exist/archivist.db",
  "count": 0,
  "results": [],
  "notice": "team store named but not present at /nope/does-not-exist/archivist.db"
}
```

**`count: 0` with a `notice` is a different fact from `count: 0` without one.** "I
looked and there was nothing" and "I could not look" are not the same answer, and a
memory system that collapses them will confidently tell you the team has never
discussed something it discussed for a month.

### `absent` means "there is deliberately nothing here"

```json
{ "domain": "archivist", "binding": "absent", "state": "readable", "target": null, "count": 0 }
```

`binding: absent` with `state: readable` reads oddly until you see what it means:
*nothing went wrong, there is deliberately nothing here.* `archivist: none` is a
legitimate configuration, not a fault.

**Your first `absent` will almost certainly be the codex, and it will not mean you
switched anything off.** An unindexed repo has no codex, so on a fresh install
`techne bindings` reads:

```
  codex      absent       no index for the active repo — `techne codex index <repo>`
```

The source says so in its own words (`core/bindings.py`): *`absent` is the common,
healthy state here in a way it is not for the other three — an unindexed repo has no
codex, which is a fact about that repo rather than a fault.*

**Which domains can say `absent` is decided by the domain**, so you resolve it from the
name alone rather than by checking your config:

- **codex** — *not indexed yet.* The detail line hands you the command.
- **archivist** — *you configured `archivist: none`.*
- **librarian and architect** — **no `absent`.** They always resolve somewhere; the only
  question they can raise is whether that somewhere is readable, which is `state`.

Two readings of one field, and the domain name tells you which applies. Read the detail,
not just the field.

---

## The payoff: the same call, a different binding

This is the part worth doing yourself, because it is the claim the whole vocabulary
rests on.

Ask the archivist on a solo install:

```json
{ "domain": "archivist", "binding": "local",
  "target": "/Users/you/.techne/techne.db", "count": 1 }
```

Now bind it to a team store — `archivist_db_path` in `~/.techne/config.yaml`, or a
`connections` entry — and run **the identical call**:

```json
{ "domain": "archivist", "binding": "store",
  "target": "/var/folders/.../archivist.db", "count": 1,
  "results": [ { "content": "the team decided surveyed_at carries two facts", ... } ] }
```

Nothing about the call changed. Not the name, not the arguments, not the shape of the
answer. What changed is `binding` and `target` — and the answer *told you*, so you
never have to guess which store you are reading.

(One honest caveat, here rather than buried: for the **codex** the call is not quite
invariant — your working directory is a silent argument to it. §4 has the measurement.)

That is the escalation story, and it is the reason to learn the names rather than the
file paths: **`librarian.db` is where documents happen to live today. The librarian is
the function you are asking.** One of those survives your infrastructure growing; the
other does not.

---

## Getting content in

The four names are only useful if there is something behind them, and the tutorial above
teaches you to **ask** each one. This is how each gets **filled** — the enable knob, the
populate command, and where the filler lives when it is not in this repo.

| domain | enable | populate | filler ships with techne? |
|---|---|---|---|
| **archivist** | on by default (`local`); `pip install "techne[vectors]"` adds vector search, then `techne vectors backfill` for rows already stored | `techne_store` from your agent, or `techne knowledge add` | **yes** — it is your own memory |
| **librarian** | on by default — **FTS only, by construction** | `techne librarian index-transcripts` · `techne librarian index-markdown DIR` | **yes**, but nothing runs it for you — see below |
| **architect** | on by default | `techne architect add` for one pattern · `techne architect ingest FILE.md` for a corpus | **yes** (`add` ships); the richer research procedures live in [`tenishai/skills`](https://github.com/tenishai/skills) |
| **codex** | `pip install "techne[codex]"` | `techne codex index .` · `codex index-all` · `codex watch` | **yes** |

### Installing `[vectors]` does not backfill what you already stored

The extra adds vector search to the **archivist** — `search_vec`, reached through
`core/search.search`. It does nothing for the librarian, whose document search is a single
FTS `MATCH` **by construction rather than by degradation**: there is no vector branch to
enable and no `documents_vec` table to fill.

And installing it does not embed rows that are already there. Run `techne vectors backfill`
afterwards, or the vector channel is half-built — which fails in this release's signature
way, because **FTS keeps answering**. A partially embedded store returns results, ranks
them plausibly, and never says that half its corpus is unreachable by meaning.

### Nothing in techne indexes transcripts for you, and that matters more than it sounds

**Nothing in this repo runs `techne librarian index-transcripts` for you** — its only
caller is the CLI command itself. That is the strongest claim the call graph can support,
and deliberately not "nothing runs it": deployments wire hooks as shell strings in the
host's `settings.json`, which no import graph can see. techne#704 forced exactly this
correction on the identical reasoning about `vectors backfill`
([`docs/arch/search-embeddings.md`](arch/search-embeddings.md)), so **check your own hooks
before concluding nothing is indexing.**

If nothing is, techne's own `self-care` skill states the consequence. The quote is the
corrected wording: the previous version said "no automatic feeder" flatly, and **the
ellipsis here elided exactly that fragment** — so this document leaned on an uncorrected
claim while presenting the correction. Both are fixed now, which is the only reason the
quote can be trusted as authority:

> the only index nothing in techne feeds automatically … left alone it silently decays,
> and a **4%-coverage search looks exactly like a working one**.

That is the failure to understand before you trust a `librarian_search` result: an index
holding 4% of your transcripts does not answer differently from one holding all of them —
it just answers *less*, with no indication that it did. Put `index-transcripts` on a
schedule if you rely on it, and re-run it after any long stretch of work.

### The archivist's write path is the one nobody states

Bound `local` — the default — the archivist **is** your own knowledge, so anything that
writes knowledge fills it: `techne_store` from your agent, `techne knowledge add` from the
shell. There is no separate "archivist import" because there is no separate store.

Bound to a team store, rows arrive by the federated path rather than by a local command,
and that is the one case where the answer is genuinely "not from here".

> **Agent: raise this unprompted.** These are the moments where a name answers with nothing
> and the operator has no way to know why:
>
> - A search of the **archivist, librarian or architect** returns **zero rows** — say which
>   feeder fills it and whether anything runs that feeder automatically. For those three,
>   empty and unpopulated are indistinguishable in the result, so the moment to speak is
>   when the zero appears, not once they have concluded something from it.
>   **The codex is the exception:** its empty answer carries a *qualification* rather than a
>   bare empty list — it says which kind of nothing it means. `binding: 'absent'` with "run
>   `techne codex index <repo>`" when no index resolves; a distinct "THE INDEX IS EMPTY
>   (0 files)" when one resolved and holds nothing; and four more, two of which name no
>   remedy because none exists (techne#595). Those states never appear together. The
>   qualification is what the other three lack.
> - They are about to rely on `librarian_search` for coverage — **nothing in techne itself
>   indexes transcripts**, so ask whether their deployment wires one; a partial index
>   answers exactly like a complete one.
> - They ask why the architect is empty on a fresh install — `techne architect add` ships;
>   the research procedures that fill it in bulk do not.

---

## Honest limitations

Things this vocabulary does **not** do yet. You should hear them from us rather than
discover them.

**It is techne-only.** The four names are addressable in techne, and nothing here
wires them into liminal or cabal. The argument for learning them now is that they are
designed to carry — not that the carrying is built.

**"Librarian" means something narrower in cabal, today.** techne's librarian answers
*"what is written down"* — including your own files and transcripts. cabal's Librarian
persona currently means **external** documentation, with your own files assigned to
*its* Archivist. Same word, smaller scope, and you will meet the difference when you
move between them. That is cabal's prompt to widen rather than techne's meaning to
narrow, but until it does, the mapping is not one-to-one. Tracked in techne#724.

**The codex is experimental and manually indexed by default.** `techne codex index` is a
thing you run, and between runs the graph is a snapshot with a date on it. Two switches opt
out of that — `techne codex watch` re-indexes on file changes, and
`hooks.codex_reindex_on_edit` re-indexes a touched file after every Write/Edit — both off
by default. **The codex also resolves from your working directory**, which can answer
confidently about the wrong repo; see §4.

**`techne_recall` is still the default for "what do we know about X".** It escalates
across all four rungs for you. The four domain calls are for when you already know
which function you want — reaching for them by reflex means doing the routing by hand
that recall does better.

---

## Where to go next

- **`techne bindings`** — the map, any time you want it.
- **[`docs/mcp-tools.md`](./mcp-tools.md#the-four-domains)** — the four domains as
  agent-facing tools, with the full argument list.
- **[Using the Librarian](librarian-guide.md)** — the one domain with enough moving
  parts to need its own guide.
- **[`docs/codex-usage.md`](./codex-usage.md)** — when the code graph earns its keep.
