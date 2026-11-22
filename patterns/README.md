# Architectural patterns — the pattern store

Patterns lifted from other people's code live in **`architect.db`** (techne#87), the
**single source of truth**. Query and write via the `techne architect` CLI.

There is **no hand-edited markdown corpus**. The old `architectural-patterns.md` transport
(and the operator's `~/.claude` copy) were retired 2026-06-25: a hand-edited file silently
drifts out of sync with the db and gets treated as canonical by accident — which is exactly
how 14 patterns ended up only in the markdown and never in the store. Don't reintroduce one.

## Add a pattern

Preferred: the `/investigate-url` skill surfaces candidates from a URL, filters, and ingests
the kept patterns straight into `architect.db`. To add one by hand, write a minimal block to a
temp file and ingest it (the **Source line must contain an `http(s)://` URL**, else it's skipped):

```sh
cat > /tmp/p.md <<'EOF'
## <Short pattern name>

- **Source**: <https-url> — <path::symbol (line optional) / §section> @ <sha the citation was read at>
- **Surveyed**: <YYYY-MM-DD>

<What it does — 1–2 paragraphs.>

**Why interesting**: <the non-obvious design choice / trade-off>.

**Where to apply in <target>**: <where in our stack it fits>.

---
EOF
techne architect ingest /tmp/p.md --verdict patterns-only   # idempotent (content-hash dedup)
```

> **The `in <target>` is load-bearing, not style.** `backfill_graph` anchors on the
> literal phrase `apply in <target>` and takes the token that follows it, so
> `**Where to apply**: techne, ...` (target after the colon) produces NO
> `pattern_applies` row — the parser documents that case as an intentional
> under-reach. The narrowness is deliberate: it is what stops `**built in techne**`
> and `...in techne (vs cabal)` manufacturing false links (PR#130). Write the
> target inside the heading. Several targets are fine when joined by `/`, `,`,
> `and` or `&` right after the first one: `**Where to apply in techne / cabal**`
> or `**Where to apply in techne, cabal and liminal**` link all of them (#693).
> Valid targets are `architect.pattern_targets` in `~/.techne/config.yaml`;
> `techne architect targets` lists them.

`--verdict` records *your judgment* about the URL — `skip` / `skip-with-flag` /
`patterns-only` / `full-eval`. Omit it and the ingest records `patterns-only`, the
modal outcome. State it when the answer is anything else, particularly `full-eval`,
which means the source forces a decision (adopt / fork / build-on-top / block) and
should be accompanied by an eval doc.

Before #428 this column was hardcoded to `full-eval` on every ingest carrying
patterns, so pre-#428 rows carry no real judgment. To correct one without
re-ingesting its pattern block:

```sh
techne architect reverdict <https-url> --verdict full-eval
```

A URL investigated with nothing to lift → a `## Surveyed (no patterns worth saving)` bullet
in the temp md: `- <url> — surveyed YYYY-MM-DD — <reason>`.

A cross-source *synthesis* (no single source URL) belongs in techne knowledge
(`techne_store`, type=pattern), not here — the pattern schema requires a real Source URL.

## Query it

```sh
techne architect search "<topic>"      # also: dedup a URL before investigating it
techne architect show <PATTERN_ID>
```

## Backup

`architect.db` is binary and lives at `~/.techne/architect.db`. Back it up by **copying the db
file** — never by exporting a markdown corpus (that reintroduces the drift foot-gun):

```sh
mkdir -p ~/.techne/backups
cp ~/.techne/architect.db ~/.techne/backups/architect.db.$(date +%Y%m%d)
```

`techne architect export -o <file>` still exists for a one-off human-readable dump, but treat
any exported md as a throwaway snapshot — never a file you ingest back, hand-edit, or commit
as canonical.
