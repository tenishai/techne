# Using the Librarian — documents, cards, and getting your transcripts in

The Librarian is techne's **document memory**: it remembers the things you read and the
conversations you have, and makes them recallable later. This guide is for two readers —
**you** (setup + daily use) and **your Claude** (a copy-pasteable instructions block at the
end so the agent drives it for you).

---

## The one idea: documents vs cards

This is the whole model. Get this and the rest is obvious.

- A **document** is the *full content* — a whole session transcript, a whole markdown file.
  It can be big. It lives in its own db (`librarian.db`), **not** in your personal memory.
- A **card** is a *tiny gist + a pointer* to that document. It lives in your personal memory
  (`techne.db`), and it's what shows up in normal recall and search.

```
   recall / search  ─▶  CARD (gist + pointer)        ← small, in techne.db, always recallable
                              │
                              ▼  fetch on demand
                         DOCUMENT (full content)      ← large, in librarian.db
```

It works the way human memory does: you remember *that* a thing exists and *roughly* what it
said (the card), and you go pull the details only when you actually need them (the document).
So your context never gets flooded with full transcripts — you recall the gist, then fetch.

- **One card per document.** Cards are *pull-only*: they ride recall but are never auto-dumped
  into your session at startup.
- Recall the gist: `techne search "<topic>" --table knowledge` (cards live in knowledge).
- Fetch the full thing: `techne librarian card <CARD_ID>` (resolves the pointer to the document)
  or `techne librarian show <DOC_ID>`.
- Search full document *content* directly: `techne librarian search "<text>"`.

---

## Setup (one-time, per machine)

The Librarian is per-machine — it indexes *your* local transcripts and docs into *your*
`~/.techne/librarian.db`. Nothing leaves your machine.

### 1. Get your Claude Code transcripts in (the jsonl)

Claude Code writes every session as a JSONL file under `~/.claude/projects/<project-slug>/`.
The Librarian reads the **conversation prose** out of those (what was said — the decisions,
not just the tool calls) and stores each session as a document with a gist card.

```sh
# from any techne-initialized checkout:
techne librarian index-transcripts                 # indexes the CURRENT project's transcripts
techne librarian index-transcripts --dir ~/.claude/projects/<slug>   # a specific project
```

- **Idempotent** — re-run anytime; only new/changed sessions are rewritten.
- **Redacted** — credentials are stripped from content *and* titles before storage.
- Each transcript's card gets a **thread index** gist automatically (a session spans many
  topics — "Session (date): thread A; thread B; …"), built from techne's thread detection.
  No summarization step needed for transcripts.

### 2. Get your markdown in (docs, design notes, anything)

Any directory of `.md` files can be indexed — design docs, runbooks, notes, a repo's `docs/`.

```sh
techne librarian index-markdown ./docs --source-type docs
techne librarian index-markdown ~/notes --source-type notes
```

- Title comes from the first `#` heading (else the filename).
- Single-topic docs start with a *pending* card (gist = title). Fill real gists agent-side:
  `techne librarian cards emit -o /tmp/b.json` → your Claude writes a 1–2 sentence gist into
  each `summary` → `techne librarian cards ingest /tmp/b.json`. (No API key — see below.)

### What can go in the Librarian?

Today: **Claude Code transcripts** and **markdown directories**. The store is generic — any
"document source" (other clients' transcripts, Jira/Confluence pages, web captures) is a small
adapter away (tracked in techne#96; the contract is `core/transcripts.py::TranscriptSource` /
`core/library.py::index_document`). Use it for anything worth recalling later by gist.

---

## Daily use

```sh
techne search "outbox backpressure" --table knowledge   # find the gist card
techne librarian card 268                                  # gist + fetch the full doc
techne librarian search "transactional outbox pattern"     # search full document text
techne librarian                                           # counts
```

---

## How it's stored (one line)

`techne.db` = your personal memory (sessions/threads/knowledge/identity + the **cards**).
`librarian.db` = the **documents** (full content), a separate file beside it. This is
techne's *db-per-domain* model — see [`db-per-domain.md`](db-per-domain.md). You don't have
to think about the two files; the commands route to the right one.

---

## For your Claude — copy this into your agent's context (CLAUDE.md or a skill)

> **Librarian (techne document memory).** techne stores *documents* (full transcripts/docs in
> `librarian.db`) and *cards* (gist + pointer in `techne.db` knowledge). Recall returns the
> **card** (gist); fetch the full document **on demand** — never dump whole transcripts into
> context.
>
> **Set it up (once per machine):** run `techne librarian index-transcripts` to index this
> project's Claude Code transcripts, and `techne librarian index-markdown <dir> --source-type
> <name>` for any docs the operator points you at. Both are idempotent — safe to re-run at the
> start of a session to pick up new material.
>
> **Use it during work:**
> - To recall a past decision/discussion: `techne search "<topic>" --table knowledge` (finds
>   the gist card), then `techne librarian card <id>` to pull the full transcript/doc *only if*
>   you need the detail.
> - To search document *content* (not just gists): `techne librarian search "<text>"`.
> - **Fill gists (no API key needed):** for pending document cards (single-topic docs),
>   `techne librarian cards emit -o /tmp/cards.json`, write a sharp 1–2 sentence gist into each
>   item's `summary` field (read each item's `prompt`, which contains the document), then
>   `techne librarian cards ingest /tmp/cards.json`. Transcript cards are auto-gisted (thread
>   index) — don't prose-summarize a long multi-topic session; that's dishonest.
> - Be honest in gists: if a session was trivial (a failed login, a one-line question), say so.
>
> **Don't** write the operator's notes into your own scratch files — index them into the
> Librarian so they're recallable. **Don't** paste whole documents into context — recall the
> card, fetch on demand.

That block plus `techne librarian --help` (and `cards`/`search`/`card`/`show` subcommands) is
everything an agent needs.
