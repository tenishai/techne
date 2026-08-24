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
stale.** The codex is the one domain that will confidently tell you about code you
deleted last week if you have not re-indexed. See [codex usage](codex-usage.md) for
when to reach for it and when not to.

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

### A domain you have switched off says so

```json
{ "domain": "archivist", "binding": "absent", "state": "readable", "target": null, "count": 0 }
```

`binding: absent` with `state: readable` reads oddly until you see what it means:
*nothing went wrong, there is deliberately nothing here.* `archivist: none` is a
legitimate configuration, not a fault.

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
  "target": "/var/folders/.../archivist.db", "count": 1 }
```

```
first result: the team decided surveyed_at carries two facts
```

Nothing about the call changed. Not the name, not the arguments, not the shape of the
answer. What changed is `binding` and `target` — and the answer *told you*, so you
never have to guess which store you are reading.

That is the escalation story, and it is the reason to learn the names rather than the
file paths: **`librarian.db` is where documents happen to live today. The librarian is
the function you are asking.** One of those survives your infrastructure growing; the
other does not.

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

**The codex is experimental and manually indexed.** It does not watch your
filesystem. `techne codex index` is a thing you run, and between runs the graph is a
snapshot with a date on it.

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
