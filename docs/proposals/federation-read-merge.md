# Federation read-merge — design proposal

**Issue:** [#222](https://github.com/tenishai/techne/issues/222) · **Epic:** [#212](https://github.com/tenishai/techne/issues/212)
**Companion:** [memory-system-architecture.md](../memory-system-architecture.md) §4/§11.3 — this is the concrete mechanism for that doc's read-merge (its self-declared "*this rule is the product*").
**Depends on (all merged):** #213 (contradiction detection), #214 (decayed ranking), #220 (schema fork → **techne↔techne**).
**Author:** Poe (Opus 4.8) + Richard, 2026-07-05.

---

## TL;DR

`recall(topic)` today queries one store. This makes it **fan out** over local + configured `team`/`static` connections and return **one** ranked, deduped, **conflict-resolved** answer. The merge is not content-hash dedup — it calls the coherence layer's `run_conflict_check` + decayed ranking at the merge boundary (**the second call site** the pure functions in `conflict.py` were built for). Because #220 resolved to techne↔techne, a `team` store is just another techne db opened **read-only** — no schema translation.

**Invariant that shapes everything:** a read never mutates a foreign store, and never auto-applies a cross-store supersession. Cross-store contradictions are *surfaced as reviewable candidates in the local store* — `recall` returns prompts; a separate ingest tool stages them. Sovereignty holds.

---

## 1. What's already built (the payoff)

- **`conflict.py` was designed for this.** Its module docstring names the two-call-site design; `select_conflict_candidates(db_path, row)` and `run_conflict_check(focus, comparison_set)` are pure (`(db_path, dict)` in, verdicts out) precisely so the federation call site reuses them unchanged.
- **Cross-store ranking is already solved.** `_rerank`'s per-`type` BM25 normalization (#56, `search.py:219-235`) normalizes within each type bucket across the *pooled* set — a team "knowledge" row and a local "knowledge" row get normalized together. Different corpus sizes → different BM25 magnitudes → normalization makes them comparable within a query. Same problem #56 already fixed for cross-*table*; cross-*store* is the same shape.
- **`content_fingerprint()`** (`knowledge.py`) is ready for content-hash dedup.
- **`effective_confidence()`** (#214) is the tiebreak for non-conflicting ranking.

Genuinely absent: a `connections` concept in `Config`, a federation module, and a read-only remote-open path.

## 2. Connections config (opt-in, noop-shaped)

New in `Config` — bare techne with no `connections` stays local-only, byte-identical to today:

```python
@dataclass
class FederationConnectionSpec:
    role: str        # "team" | "static"
    db_path: str     # path to the remote techne.db (opened read-only)
    label: str = ""  # human label, shown in surfaced conflicts

# on Config:
connections: dict[str, FederationConnectionSpec] = field(default_factory=dict)
```
```yaml
connections:
  team-archivist:
    role: team
    db_path: /Users/shared/archivist/techne.db
    label: "Team Archivist"
```
`_from_dict` defaults the key to `{}` (no migration; existing configs unaffected); `save()` serializes only when non-empty (mirrors the `stores` block). **The gate:** `if not config.connections: <existing local-only recall, unchanged>`.

## 3. Fan-out + merge (`techne/core/federation.py`, new)

Pure functions (dicts + `Path`, no `Config` coupling — same discipline as `conflict.py`). Five steps:

**A — Fan out.** Local via existing `search(local_db, …, table="all")`. Each remote via `search_fts(remote, …, table="knowledge")` — **knowledge-layer only**: threads/observations are private raw capture and *never* federate (arch §2). Tag every row `_store` + `_role` for provenance. Remotes opened with a dedicated read-only helper:

```python
def _open_remote_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # writes physically impossible
    conn.execute("PRAGMA busy_timeout=3000")
    conn.row_factory = sqlite3.Row
    return conn
```
> **Do NOT** use `database.connect()` for remote opens — it creates WAL + runs `_ensure_initialized()`, which would *write schema into the foreign store's directory*. `?mode=ro` is the sovereignty guard.

**B — Content-hash dedup** (`content_fingerprint`): drop remote rows whose fingerprint exactly matches a local row. True duplicates are cheap to catch here; running this *before* the contradiction check keeps identical content out of the LLM (the dedup≠contradiction distinction is load-bearing — coherence-layer §4b¶1).

**C — Cross-store contradiction detection.** For each remaining remote knowledge row `r`: copy it, set `id=None`, call `select_conflict_candidates(local_db, r_proxy)` (finds entity-overlapping *local* rows by shared tags + same type; the `id != -1` exclusion is a no-op for the foreign row). If matches, `run_conflict_check(r_proxy, local_matches)` builds the prompt. **This `id=None` is the only adaptation** — signature unchanged, db_path is the local db. One implementation, two call sites, as designed.

**D — Merge + rank.** Pool local + deduped-remote, run `_rerank(pooled, …)`. Per-type normalization does the cross-store work; recency + decay are the tiebreaks. Return top-N.

**E — Where contradictions land (sovereignty).** `recall` **returns** the conflict prompts as `federation_conflicts` — it does **not** write. A separate `techne_federation_conflict_ingest` tool (the agent answers the prompt with its own model, same emit/ingest shape as #213) stages a **pending** candidate into the **local** `knowledge_candidates` with `origin='federation-conflict'`:
- SUPERSEDE + local older (team has the newer fact, later `event_at`) → stage a `retire` candidate targeting the **local** row.
- SUPERSEDE + local newer (team is stale) → stage a `discarded` informational marker; **no push to the team store** (we don't write foreign stores).
- The foreign id (`"team-archivist:42"`) + content ride in `decision_reason`; `target_ids` holds **local ids only** (so `promote_candidate` is unmodified and there's no foreign-id lookup).

## 4. Sovereignty invariants (the checklist Prudence verifies)

1. Remote dbs opened `?mode=ro` — writing to a foreign store is physically impossible.
2. `recall` is **read-only** — it detects + returns prompts; the separate ingest tool is the only writer, and it writes the **local** db only.
3. No auto-apply — every federation-conflict candidate is `status='pending'` for operator review.
4. Raw never federates — remote reads are `table="knowledge"` only.
5. Federation is opt-in — no `connections` → local-only, unchanged.
6. techne stands alone — no cabal/agents-repo dependency in this path.

## 5. Minimal viable cut (satisfies the full DoD)

**Build now:** `team` connections · knowledge-layer · FTS-only remote · `recall` fan-out · `techne_federation_conflict_ingest` · tests.
**Defer (trivial fast-follows):** `static` role (metadata-only, same code path) · N team stores (already a `dict`) · remote **vector** channel (FTS-only avoids fastembed on the remote path) · `techne_search` federation (recall-only MVP).

**Done when:** with a `team` connection, `recall` returns one merged ranked set; the sqlite-vs-postgres cross-store contradiction surfaces (not silently duplicated); no connection → local-only unchanged; tested.

## 6. Risk flags (what to watch in build + review)

- **Read-only WAL open (new I/O path, MEDIUM):** test against a **WAL-mode** team db fixture — WAL allows concurrent readers without blocking a writer. **Correction (verified in #241):** `?mode=ro` guarantees the team's *memory is never modified* — writes to the db raise, and the content + schema are byte-identical before/after (sha256-verified) — but reading a live-WAL store **does** leave transient `-wal`/`-shm` read-scratch in the foreign dir (a read-only connection can't checkpoint them away). That is SQLite read coordination, not a memory mutation; for a git-backed team store these sidecars are gitignored. `immutable=1` avoids the sidecars entirely but asserts the file won't change while open — **unsafe for a live, concurrently-written team store** (torn/stale snapshot), so we keep `?mode=ro`. The invariant that matters (foreign *memory* never modified) holds; the earlier "no files touched" framing was too strong.
- **Id-namespace gotcha (MEDIUM):** the foreign focus id must **never** be `_fetch_row`'d from the local db — assert this in a test.
- **Per-recall detection cost (LOW):** `select_conflict_candidates` is O(N) local per remote row; fine at MVP scale, and the anti-re-proposal hash — keyed on `(content_fingerprint(remote), sorted_local_ids)` — stops re-surfacing the same pair every recall.
- **Cross-store BM25 (LOW — already solved):** never merge raw BM25 across stores without `_rerank`'s normalization; that was the old cross-table bug (#56), already fixed.
- **Config migration (NONE):** absent `connections` key → empty dict → unchanged behavior.

## 7. Shape of the build

One PR (~450 lines: `config.py` connections + `federation.py` + `_techne_recall` branch + the ingest tool + `tests/test_federation_recall.py`). The pieces are too coupled to split usefully — a config field with no consumer is dead code until the fan-out lands. Tests mirror `test_conflict.py`: local-only-unchanged · merges-team-results · detects-cross-store-contradiction · ingest-stages-pending-candidate (never auto-applies) · read-only-remote-under-WAL.

## 8. What this unblocks

- **#227** — relocate cabal's Archivist code (cabal becomes host, not owner): the Archivist is now "a techne with a `team` connection," so its recall already federates.
- **static/search fast-follows** above.
- The federation write path (`store(scope=team)` handing off to the Archivist's git push) — a separate brick; this proposal is **reads only**.
