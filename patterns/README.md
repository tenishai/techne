# Architectural patterns — shared team corpus

`architectural-patterns.md` is the team's borrowable-patterns corpus: design patterns
lifted from other people's code, tagged by source URL + survey date, with where-to-apply
notes. It's a **plain-markdown file you can edit by hand** *and* the import/export format for
the Architect pattern store (`architect.db`, techne#87). Markdown diffs merge cleanly, so the
whole team can contribute.

## Add a pattern (anyone)

1. Open `architectural-patterns.md`, append a block using the **Template for new entries** at
   the bottom of the file:
   ```
   ## <Short pattern name>

   - **Source**: <url> — <optional file:line / §section>
   - **Surveyed**: <YYYY-MM-DD>

   <What it does — 1–2 paragraphs.>

   **Why interesting**: <the non-obvious design choice / trade-off>.

   **Where to apply**: <where in our stack it fits>.

   ---
   ```
   (A URL you investigated but found nothing to lift → a bullet under
   `## Surveyed (no patterns worth saving)`.)
2. `git commit && git push`.

Grep for a source URL before investigating it — if it's already in the file, we've seen it.

## Query it (load into your local store)

```sh
git pull
techne architect ingest patterns/architectural-patterns.md   # idempotent (content-hash dedup)
techne architect search "<topic>"
techne architect show <PATTERN_ID>
```

## Regenerating the file (`architect export`)

`architect.db` and the markdown round-trip: `architect ingest` loads the md, `architect
export` regenerates it. If you regenerate (e.g. after building patterns via an
`investigate-url` flow into your own `architect.db`), **`ingest` the current file first**, then
`export` — ingest pulls everyone's hand-added patterns into your store so the regenerate
doesn't drop them:

```sh
git pull
techne architect ingest patterns/architectural-patterns.md   # absorb others' hand-edits
# … add your own patterns to architect.db via investigate-url …
techne architect export -o patterns/architectural-patterns.md
git commit && git push
```

For casual additions, just hand-edit the file (step 1 above) — no `architect` commands needed.
