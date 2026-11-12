---
allowed-tools: Bash(techne:*), Bash(.venv/bin/python -m techne.cli:*), Bash(sqlite3:*), Bash(gh issue view:*), Bash(gh pr view:*), Bash(git log:*), Read, Write, Edit
description: Agent-assisted promotion of techne observations into curated knowledge (the no-API-key path). Emits per-thread reflection prompts, you answer them as strict candidates, ingests them into knowledge_candidates for review. Use to grow techne's knowledge layer from accumulated session observations (techne#92/#20).
disable-model-invocation: false
---

You are the **inference step** in techne's agent-assisted promotion pipeline (techne#92). techne can't call an LLM (no API key) — but you can, on the operator's OAuth. So techne renders the prompts, **you** answer them, techne persists the results. This is how observation→knowledge promotion happens without a key.

Run from the techne repo. Use `techne` on PATH, or `.venv/bin/python -m techne.cli` if that's stale.

## Argument

`$ARGUMENTS` is an optional session id. If empty, promote the **latest substantive** session (most observations). To backfill, run once per session id.

## Procedure

1. **Pick the session.** If an id was given, use it. Otherwise find the latest substantive one:
   `sqlite3 ~/.techne/techne.db "SELECT s.id, date(s.started_at), count(o.id) obs FROM sessions s JOIN observations o ON o.session_id=s.id GROUP BY s.id ORDER BY max(o.id) DESC LIMIT 5;"` and choose the most recent with real work (skip empty/probe/temp-dir sessions).

2. **Emit** the per-thread prompts to a file:
   `techne reflect emit --session <SID> -o /tmp/techne-promote-<SID>.json`
   Each item has `{thread_id, session_id, thread_num, prompt_hash, prompt, raw_response}` with `raw_response` empty for you to fill.

3. **Answer each thread.** Read the file. For each item, read its `prompt` (thread label + summary + observations + a "What the agent already knows" dedup block) and decide what — if anything — is worth saving. Fill that item's `raw_response` with a JSON array **string** of candidates:
   ```
   [{"scope":"knowledge"|"identity","type":<type>,"content":"<one durable, evidence-anchored sentence>","tags":"<comma,tags>","confidence":"low"|"medium"|"high"}]
   ```
   - `scope=knowledge` type ∈ fact|decision|pattern|evaluation|procedure. `scope=identity` type is a dotted key (e.g. `user.prefers.terse`).
   - Edit the file in place (Edit/Write), keeping every other field unchanged. Leave `raw_response` as `""` to **skip** a thread (ingest ignores blanks).

4. **Be a skeptic — expect to keep little.** Most threads yield 0–2 candidates, many yield none. A candidate earns its place only if it is:
   - **Durable** across sessions (not a one-off bug fix, a now-deleted file, a transient command).
   - **Not already known** — skip anything in the prompt's "What the agent already knows" block, and don't restate what the code/git history already records.
   - **Evidence-anchored** to what the thread actually did, in one sharp sentence. Prefer fewer, sharper items over many vague ones.
   - **Honest** — never invent a fact the observations don't support. If a thread is pure mechanical work, return `[]`.
   - **Verified before `high`** — the prompt's observations are an index, not evidence: the first slice of the thread, each line clipped. Before writing `"confidence":"high"` on a candidate that names a number, an issue/PR title, a state, or a mechanism, check it against the source (`gh issue view`, `gh pr view`, `git log`, `sqlite3` on the store). If you can't, write it at `medium` and say what is unconfirmed, or drop it. This is the one check that has caught errors at authorship; the review queue caught none (techne#790).

5. **Ingest** the same file:
   `techne reflect ingest /tmp/techne-promote-<SID>.json`
   Reports candidates written + threads skipped. Candidates land in `knowledge_candidates` (pending) unless `reflect.write_to=live` and the posture doesn't override it: under `posture.mode: spool-primary` they always stay pending. The ingest output says which applies (#1360).

6. **Review and promote the keepers.** `techne reflect review --status pending` lists them. Promote the ones that survive a second look: `techne reflect promote <candidate_id> --by <who> --reason "<why>"` (→ live knowledge/identity). Discard noise: `techne reflect discard <candidate_id> --by <who> --reason "<why>"`. Always pass `--by`: without it the audit trail records `unspecified`.

7. **Report** to the operator: session, threads answered/skipped, candidates written, and which you promoted — in plain finding→decision form. Note any thread you skipped wholesale so coverage isn't silently truncated.

## Notes
- `reflect.write_to=candidates` (default) is the safe mode — review before live. Only the operator should flip to `live`.
- Don't fabricate to hit a count. An honest empty promotion is a valid outcome.
- The canary/probe (`techne probe`) proves injection works; this grows what there is to inject.
