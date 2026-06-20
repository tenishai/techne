# Architectural patterns — shared team corpus

`architectural-patterns.md` is the team's borrowable-patterns corpus: design patterns
lifted from other people's code, tagged by source URL + survey date, with where-to-apply
notes. **It is a generated export** from the Architect pattern store (`architect.db`,
techne#87) — `architect.db` is canonical; this file is the shareable transport.

## Workflow — one writer, many readers

To avoid merge conflicts, the corpus has **one curator** (the architect persona / Richard)
and the rest of the team are **read-only consumers**.

**Curator (writes):**
```sh
# investigate a URL → patterns land in architect.db (via the investigate-url flow), then:
techne architect export -o patterns/architectural-patterns.md
git add patterns/architectural-patterns.md && git commit && git push
```

**Everyone else (reads):** pull, then load into your own local `architect.db`:
```sh
git pull
techne architect ingest patterns/architectural-patterns.md   # idempotent (content-hash dedup)
techne architect search "<topic>"                            # query your local copy
```

Don't hand-edit this file — edits get overwritten on the next export. New patterns go
through the curator's `architect.db`.
