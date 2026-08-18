---
allowed-tools: Bash(techne:*), Bash(du:*), Bash(ls:*), Bash(find:*), Bash(sqlite3:*), Bash(python3:*), Bash(gzip:*), Bash(rm:*), Read, Write, Edit
description: Periodic hygiene pass on techne's local state — drain the rails, sweep the outbox, prune backups/snapshots, reclaim disk, reschedule itself. Dry-run first, destructive steps gated behind an explicit preview. Run when the "self-care" reminder fires or when a session feels noisy.
disable-model-invocation: false
---

You are running techne's periodic hygiene pass. The goal is a store that is **smaller, current, and honest** — not a green checkmark. An honest "step 4 skipped, here's why" is a successful run.

Adapted from the Rook `lean-and-mean` procedure (fubo-era, `~/.claude/memory` + `sessions.db`). Roughly half of that procedure transferred; the steps below are the parts that actually apply to *this* stack, plus three leaks Rook's version never looked for.

## Argument

`$ARGUMENTS` is optional. Empty = full pass, dry-run posture throughout. Pass a step name (`outbox`, `rails`, `transcripts`, `backups`, `vacuum`) to run just that one. Pass `--apply` to allow the destructive halves after you have shown the preview.

## Standing rules for this pass

1. **Preview before every destructive op.** Show the operator what would be deleted, with counts and bytes, and get a yes. Never fold the preview and the apply into one call.
2. **Coordinate first.** VACUUM takes an exclusive lock and peer sessions are usually live. Announce on the bus and check for acks before any locking step.
3. **Never make data unreadable to reclaim space.** Step 4 exists as a live example of this rule.
4. **Report failures as failures.** If a step errors or you skip it, say so in the summary. Do not quietly drop it.

## Step 0 — Pre-flight

```
techne status
techne doctor
du -sh ~/.techne ~/.techne/backups ~/.techne/snapshots ~/.claude/projects
sqlite3 ~/.techne/techne.db "PRAGMA page_count; PRAGMA freelist_count;"
```

Record: observation count, stale rails, outbox pending/dead, freelist pages, and the four directory sizes. This is the before-picture; the summary compares against it. The stale rails and the reflection-debt banner are your work list for step 2.

Then announce, and read what came back:

```
techne_send(body="/self-care starting — will run outbox sweep + possible VACUUM (exclusive lock). Speak up if you're mid-write.", to="coord")
techne_inbox
```

## Step 1 — Outbox sweep

Already built; this step is cadence, not new machinery.

```
techne outbox sweep --retain-days 7            # dry run, prints scope not just a total
techne outbox sweep --retain-days 7 --apply    # only after showing the operator the dry run
```

`sweep_delivered` threads the dry run's boundary into the apply so the two cannot describe different row sets. It never touches `pending`. It *can* drop a `dead` row when a live row carries the same content hash — that is intended, but if dead rows are accumulating, **stop and find the root cause before sweeping**; they are a debugging signal, not garbage.

Expect the apply to delete slightly *more* than the dry run you showed the operator (first run: 4,402 → 4,447). The boundary is threaded within a single process, not across the two invocations, so rows that aged past the cutoff in between are included. That is the design working; report the real number, not the previewed one.

`techne outbox purge` is a different, blunter command that deletes *pending* rows. Not part of this pass. Don't reach for it.

## Step 2 — Drain the rails

The banner names what is stale. Work it down:

- **Reflection debt — CAP THIS, don't drain it here.** Measure it (`SELECT substr(session_id,1,8), COUNT(*), SUM(drained_at IS NULL) FROM reflection_queue GROUP BY 1`) and report the number. Do **at most one session**, and only if it emits a handful of prompts. Beyond that, hand it to `/techne-promote`, which is purpose-built for it. Measured on the first run: one session emitted 24 prompts / 193 KB, and the full backlog was 63 threads across 7 sessions — that is a session's work, not a step in a pass. A hygiene pass that turns into an all-day promotion run is a mis-scoped pass.
- **Consolidate** — `techne_consolidate_emit` / `_ingest` (dedup), then `techne_faithfulness_emit` / `_ingest` to check that the staged merges preserve each source's load-bearing claims.
- **Dream** — `techne_dream_emit` / `_ingest`. Speculative and low-confidence by design; low priority when time is short.
- **Vectors** — `techne vectors status`, then `techne vectors backfill` if coverage has drifted.
- **Librarian transcript index** — the audit-log search layer (`source_type='claude-code-transcript'`), and the *only* index with no automatic feeder: `techne library index-transcripts` is a manual command wired to no hook or cron. Left alone it silently decays, and a 4%-coverage search looks exactly like a working one.

  **Its default `--dir` is the CURRENT CWD's project dir**, not all of `~/.claude/projects`. Run it from the wrong directory and it indexes the wrong project while reporting success. Always pass `--dir` explicitly. The glob is also non-recursive, so nested `subagents/` and `workflows/` transcripts are correctly skipped — only top-level session transcripts count. Safe to re-run: dedup is by `source_url` + content hash (`stored` vs `unchanged` in the output), and cards upsert rather than duplicate.

  Check coverage against what's actually on disk, per project dir:
  ```
  python3 - <<'EOF'
  import sqlite3
  from pathlib import Path
  proj = Path.home()/".claude"/"projects"
  con = sqlite3.connect(str(Path.home()/".techne"/"librarian.db"))
  idx = {r[0].split(":",1)[1] for r in con.execute(
      "SELECT source_url FROM documents WHERE source_type='claude-code-transcript' AND superseded_by IS NULL")}
  for d in sorted(p for p in proj.iterdir() if p.is_dir()):
      fs = {f.stem for f in d.glob("*.jsonl")}
      if fs: print(f"{len(fs & idx):>4}/{len(fs):<4} {d.name}")
  EOF
  ```
  Backfill the real project dirs; **skip the `/tmp` scratch ones** (permcheck, gate-test, prudence-review-TEST, stashcheck) — they're throwaway test sessions and each one adds a `document_card` to knowledge that rides recall.

  Two things to know before wiring this to a cron: each transcript adds ~1 card to `knowledge` (90 transcripts took it 971 → 1,062), and a *changed* transcript supersedes rather than updates — the old row is retained in full. Re-indexing live, still-growing sessions daily would append a complete copy each run. Index sessions that are done.
- **Coord inbox** — read with `techne_inbox`, clear with `techne_mark_read`. Unread coordination messages pile into every session's banner, so this is the cheapest noise reduction in the pass.
- **Thread aging** — `techne compact` ages completed/stale threads.

## Step 3 — Transcript compression (GATED, OFF BY DEFAULT)

The tempting step, and the one that will bite. Rook gzips `~/.claude/projects/**/*.jsonl` older than 14 days for ~6x. Here that is **currently unsafe**:

`transcript_library.py` (`self.transcripts_dir.glob("*.jsonl")`) and `transcript_import.py` (`directory.glob("*.jsonl")`, `PROJECTS_DIR.glob(f"*/{session_id}.jsonl")`) match `*.jsonl` only, and there is no gzip handling anywhere in `~/.techne/runtime/techne/`. Gzipping would silently make those sessions invisible to backfill, session recovery, and `recover_transcripts.sh`. Silent invisibility is the worst failure mode in a memory system.

So: **measure, report, do not compress** unless one of these is true:

- `jsonl_records.py` has learned to open `.gz` transparently (re-check — grep for `gzip` in the runtime before trusting this doc), **or**
- the operator explicitly accepts the tradeoff for a specific, already-imported set.

Measure with the exact mtime predicate — BSD `find -mtime +14` rounds and misses files 14–15 days old:

```
python3 - <<'EOF'
from datetime import datetime
from pathlib import Path
cut = datetime.now().timestamp() - 14*86400
p = Path.home()/".claude"/"projects"
allf = list(p.rglob("*.jsonl")); old = [f for f in allf if f.stat().st_mtime < cut]
mb = lambda fs: sum(f.stat().st_size for f in fs)/1024/1024
print(f"total {len(allf)} files / {mb(allf):.1f} MB; older than 14d: {len(old)} / {mb(old):.1f} MB")
EOF
```

Also note in the report: `cleanupPeriodDays: 90` in `~/.claude/settings.json` means Claude Code prunes its own transcripts at 90 days. Whether its cleanup matches `.gz` files is **unverified** — if it doesn't, gzipping silently opts those sessions out of that retention, which may be a feature or a slow leak. Verify before relying on either reading.

## Step 4 — Backup and snapshot retention

Rook's procedure has no step for this; here it is the single biggest reclaim. Three distinct problems, each needing a preview before deletion:

**(a) Migration backup pileup.** `~/.techne/backups/` keeps one `.db.bak` per migration run with no retention. A repeated migration leaves near-identical copies (as of first run: 12 × `techne-v18-to-v19` at ~48 MB). Keep the newest per version-pair, drop the rest:

```
ls ~/.techne/backups/ | sed -E 's/-[0-9]{8}-[0-9]{6}\.db\.bak$//' | sort | uniq -c | sort -rn | head
```

**(b) Orphaned snapshot identities.** `db_snapshot.py` prunes to `keep_per_db` (14) *per identity* — so an identity that stops receiving new snapshots keeps its last 14 forever. Path renames and deleted worktrees strand whole identity families. Find them by newest-snapshot date:

```
ls -t ~/.techne/snapshots/*.zst | sed -E 's/.*\///; s/^(.*)-([0-9]{8})-[0-9]{6}\.db\.zst$/\1 \2/' \
  | sort -k1,1 -k2,2r | awk '!seen[$1]++ {print $2, $1}' | sort | head -30
```

Any identity whose newest snapshot predates the last path rename, or whose source DB no longer exists, is dead weight. Confirm the source path is really gone before deleting.

**(c) Snapshotting frozen backup dirs.** `exclude_globs` in `~/.techne/db_snapshot.json` has `*/backups/*`, which does **not** match sibling dirs like `backups-pre-runtime-pull-<stamp>/`. Those get snapshotted on every run even though their contents never change. Fix the config (add `*/backups-*/*`) *and* delete the accumulated snapshots — fixing only the config leaves the existing pile.

**(d) Top-level `.bak` files.** `ls -lh ~/.techne/*.bak*` — one-off pre-surgery copies. Keep the most recent and anything guarding an unresolved change; the rest are cheap to drop once the change they guarded has settled.

Keep the retention principle from Rook: recent backups stay. The savings from the pass dwarf what a few recent backups cost.

## Step 5 — VACUUM

Only after steps 1 and 4, and only if it will do something. `freelist_count` from step 0 tells you: 0 free pages means VACUUM reclaims nothing and just takes a lock for no reason.

Confirm on the coord bus that peers are idle, then:

```
techne doctor --vacuum
```

It reports before/after MB. Prefer this over raw `sqlite3 VACUUM` — it runs the health checks first and handles the live-agent lock case rather than dying on "database is locked".

## Step 6 — Consistency check and reschedule

```
techne fsck; echo "fsck exit=$?"
```

Run it **unpiped**. `techne fsck | tail` reports `tail`'s exit status, so a failing check reads as exit 0 — the same trap that costs time elsewhere in this stack. Use `${PIPESTATUS[0]}` if you must pipe.

On failure, check the note on the reminder that triggered this pass:

- **A new failure stops the pass.** Report it, don't paper over it, don't delete anything else until it's understood.
- **A failure already recorded as a known, triaged bug** does not stop the pass — re-confirm it is the *same* failure (same ids, same reason), note that it is still open, and continue. Re-investigating a known bug every fortnight is its own kind of waste.

Known-open as of the first run: 4 dangling `document_card` rows (knowledge ids 361, 366, 624, 625), all `reason='no doc pointer'`, minted by the `reflect-promote` and `consolidate-merge` rails. Producer bug, not corruption. If it is still unfiled when you read this, file it.

Then reschedule. There is no reminder CLI; reminders are MCP-only:

```
techne_reminder_add(note="/self-care hygiene pass — outbox sweep, rail drain, backup/snapshot prune, vacuum", due_at=<today + 14 days>)
```

Dismiss the reminder that triggered this run with `techne_reminder_dismiss`, and bank the outcome with `techne_store` (type `procedure`): what you reclaimed, what you skipped, and anything the pass taught you about the store. Next run reads that.

## Verify and report

Re-run the step 0 measurements and report as a before→after table: `~/.techne` total, backups, snapshots, `~/.claude/projects`, techne.db size, freelist, observation count, stale rails.

Then, in plain prose:

- **Reclaimed** — what and how much.
- **Skipped** — every step you didn't run, with the reason. Step 3 will normally be here.
- **Found** — anything that looks like a bug rather than accumulation (dead outbox rows, an fsck failure, a new orphan family). Accumulation is hygiene; a bug is a work item — file it.

Do not report a step as done if it errored, and do not pad the reclaim number with things you only *proposed* deleting.
