"""End-of-session reflection.

For each freshly detected thread, ask an LLM whether anything in it is worth
keeping across sessions (durable facts, decisions, procedures about the project;
durable identity claims about the operator). Proposals are written to
`knowledge_candidates` for human review; while config.reflect.write_to is
'candidates' the live knowledge / identity tables are never touched.

Opt-in (off by default; enable via reflect.enabled) — see config.ReflectConfig.
The hook contract still applies: any failure here exits 0 with stderr noise;
thread detection and session teardown must complete regardless.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from techne.config import Config

log = logging.getLogger(__name__)
from techne.core._resident import extract_json_array_or_none
from techne.core._resident import prompt_hash as _prompt_hash
from techne.core.database import connect
from techne.core.knowledge import VALID_TYPES as KNOWLEDGE_TYPES
from techne.core.redaction import redact_credentials
from techne.core.threads import Thread, _make_generate_fn

MAX_OBS_FOR_PROMPT = 40
MAX_OBS_CONTENT_CHARS = 200

# Actions that require a faithfulness check before promotion (techne#252, 2026-07-06).
# Must stay in sync with faithfulness_emit's SQL filter (action IN ('merge','supersede')).
_FAITHFULNESS_GATED_ACTIONS = frozenset({"merge", "supersede"})


class FaithfulnessGateError(ValueError):
    """Raised by promote_candidate when an explicitly-unfaithful merge or supersede
    candidate is promoted without ``force=True``.

    Subclasses ValueError so existing ``except ValueError`` callers (e.g. the CLI)
    continue to work without change. The message cites the stored
    ``faithfulness_warning`` so the operator knows exactly what the concern was.
    """


PROMPT_TEMPLATE = """You are reviewing a thread of work an AI agent just finished.
Decide what (if anything) should be saved for the agent's future sessions.

# Thread label
{label}

# Thread summary
{summary}

# Observations (truncated; ordered by time)
# Each line is `- [tool|kind] text`; `kind` is an inferred shape. Weight
# `decision-shaped` and `fact-shaped` observations most — they most often carry
# the durable decisions/facts worth keeping.
{observations}

# What the agent already knows (do NOT restate any of these)
{existing}

# Your task

Return ONLY a JSON array. Each item:

  {{
    "scope":      "knowledge" | "identity",
    "type":       <see types below>,
    "content":   "<one sentence, durable, evidence-anchored>",
    "tags":      "<comma-separated short tags>",
    "confidence": "low" | "medium" | "high"
  }}

For scope=knowledge, type must be one of: {knowledge_types}.
For scope=identity, type is a dotted key path (e.g. "user.prefers.terse_responses").

Be strict. Return [] when nothing in this thread is worth keeping. Skip:
  - Anything implementation-ephemeral (a one-off bug fix, a now-deleted file).
  - Anything already in "What the agent already knows".
  - Anything that would be obvious from re-reading the code.
Prefer fewer, sharper items over many vague ones. Hard cap: {max_items} items.

Output the JSON array and nothing else.
"""


@dataclass
class Candidate:
    scope: str  # 'knowledge' | 'identity'
    type: str
    content: str
    tags: str
    confidence: str


def _render_observations(observations: list[dict]) -> str:
    """Render a thread's observations into a compact, prompt-safe block."""
    if not observations:
        return "(no observations)"
    chunk = observations[:MAX_OBS_FOR_PROMPT]
    lines = []
    for o in chunk:
        tool = o.get("tool_name") or "?"
        # Part A's inferred kind (techne#33) is the second field — it tells the model
        # which observations are decision-/fact-shaped and worth extracting (techne#19).
        kind = o.get("kind") or ""
        label = f"{tool}|{kind}" if kind else tool
        content = (o.get("content") or "").replace("\n", " ")
        if len(content) > MAX_OBS_CONTENT_CHARS:
            content = content[:MAX_OBS_CONTENT_CHARS] + "…"
        lines.append(f"- [{label}] {content}")
    if len(observations) > MAX_OBS_FOR_PROMPT:
        lines.append(f"… ({len(observations) - MAX_OBS_FOR_PROMPT} more elided)")
    return "\n".join(lines)


def _existing_knowledge_block(db_path: Path, query: str, k: int) -> str:
    """Top-K existing knowledge entries relevant to the thread, for dedup."""
    if k <= 0:
        return "(dedup disabled)"
    try:
        from techne.core.search import search_fts

        hits = search_fts(db_path, query, table="knowledge", limit=k)
    except Exception:
        return "(no existing knowledge available)"
    if not hits:
        return "(none yet)"
    lines = []
    for h in hits:
        content = (h.get("content") or "")[:200]
        lines.append(f"- {content}")
    return "\n".join(lines)


def new_ingest_counts() -> dict:
    """The per-thread counts shape, defined ONCE.

    THREE HAND-MAINTAINED COPIES OF THIS DICT EXISTED — `ingest_reflection_response`,
    `reflect_on_session`, and `techne reflect ingest` in cli.py — all summed with
    `result[key] += val`. Adding a key to the first meant finding the other two,
    and I found one: the CLI crashed with `KeyError: 'parse_failures'` on every
    non-empty response, through 2381 passing tests, because no test exercises
    that command at all.

    A comment saying "keep these in sync" was the previous fix and it is what
    failed. This is the version that cannot drift: a new key is added here and
    every aggregator inherits it. (techne#465, Prudence's review of #483.)
    """
    return {
        "candidates_proposed": 0,
        "candidates_written": 0,
        "candidates_promoted": 0,
        "errors": 0,
        # Counts, not reason strings — these dicts are SUMMED, so a str-valued
        # key breaks the aggregation. The reasons go to the log.
        "parse_failures": 0,
        "candidates_dropped": 0,
        "candidates_below_confidence": 0,
    }


class ParsedResponse(NamedTuple):
    """What a reflection response actually was, not just what survived it.

    THREE OUTCOMES SHARE ONE SHAPE AND MUST NOT. Before this, `parse_response`
    returned a bare list and "returns [] on any parse failure (caller decides
    whether to surface that)" — the caller did not surface it, so ingest reported
    `{"candidates_proposed": 0, ..., "errors": 0}` for BOTH a discarded
    reflection and a correct pass where the model genuinely had nothing to save.
    A whole session's reflection could be thrown away and reported as success,
    with no log line, no counter, and no signal to go looking (techne#465).

    The realistic triggers are not exotic: a truncated response hitting an output
    limit, a raw newline inside a JSON string, or a model that explains in prose
    instead of emitting the array.

    - ``failure`` — no array could be extracted at all. THE PASS DID NOT HAPPEN.
    - ``dropped`` — the array parsed and N items were REJECTED by validation
      (bad scope, unknown type, empty content). The model produced candidates and
      every one was discarded, which is its own kind of silence: distinct from a
      parse failure and equally invisible before this.
    - both zero with an empty ``candidates`` — the honest "nothing worth saving".
    """

    candidates: list[Candidate]
    failure: str | None = None
    dropped: int = 0


def parse_response(raw: str, max_items: int) -> ParsedResponse:
    """Parse the LLM's JSON array into validated Candidate objects.

    Tolerant of common LLM noise: leading/trailing prose, fenced code blocks.

    RETURNS A ParsedResponse, NOT A BARE LIST, and deliberately leaves no lossy
    entry point beside it. A back-compat wrapper returning just the list was the
    obvious smaller change and was rejected: there is exactly one production
    caller, and a second, lossier signature is how this bug gets reintroduced by
    the next person who reaches for the simpler-looking one.
    """
    items = extract_json_array_or_none(raw)
    if items is None:
        # Distinguish an EMPTY response from an unparseable one. A model that
        # returns nothing at all has still failed to reflect, but it is a
        # different failure from one that emitted prose or truncated JSON, and
        # the operator's next move differs.
        reason = "empty response" if not (raw or "").strip() else "no JSON array found"
        return ParsedResponse([], failure=reason, dropped=0)

    out: list[Candidate] = []
    # THE FOURTH PLACE A CANDIDATE VANISHES, and the last one where the number
    # lost was unknowable. `dropped` is counted against `considered`, i.e. AFTER
    # this slice — so eight candidates against a cap of five reported
    # proposed=5 with every counter at zero. Unlike the other three this is not
    # byte-identical to honest-empty, so the operator is not actively misled; but
    # "we discarded three things you produced and cannot tell you how many" is
    # still the wrong answer, and the cap is config (max_candidates_per_thread),
    # so it is tuned blind without this. (Prudence, #483 review.)
    considered = items[:max_items]
    truncated = max(0, len(items) - len(considered))
    for item in considered:
        if not isinstance(item, dict):
            continue
        scope = item.get("scope")
        if scope not in ("knowledge", "identity"):
            continue
        type_ = item.get("type")
        content = item.get("content")
        if not isinstance(type_, str) or not type_:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        if scope == "knowledge" and type_ not in KNOWLEDGE_TYPES:
            continue
        confidence = item.get("confidence", "medium")
        if confidence not in ("low", "medium", "high"):
            confidence = "medium"
        tags = item.get("tags") or ""
        if not isinstance(tags, str):
            tags = ""
        out.append(
            Candidate(
                scope=scope,
                type=type_,
                content=content.strip(),
                tags=tags.strip(),
                confidence=confidence,
            )
        )
    return ParsedResponse(out, failure=None, dropped=(len(considered) - len(out)) + truncated)


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _meets_min_confidence(candidate: Candidate, min_confidence: str) -> bool:
    return _CONFIDENCE_RANK[candidate.confidence] >= _CONFIDENCE_RANK[min_confidence]


def _build_prompt(
    thread: Thread,
    observations: list[dict],
    db_path: Path,
    config: Config,
) -> str:
    existing_block = "(dedup disabled)"
    if config.reflect.include_existing_knowledge:
        existing_block = _existing_knowledge_block(
            db_path,
            f"{thread.label} {thread.summary[:200]}",
            config.reflect.existing_knowledge_k,
        )
    from techne.core.prompts import render_prompt

    return render_prompt(
        config,
        "reflect",
        PROMPT_TEMPLATE,
        label=thread.label,
        summary=thread.summary[:1000],
        observations=_render_observations(observations),
        existing=existing_block,
        knowledge_types=", ".join(KNOWLEDGE_TYPES),
        max_items=config.reflect.max_candidates_per_thread,
    )


def _insert_candidate(
    db_path: Path,
    candidate: Candidate,
    *,
    session_id: str,
    thread_id: str,
    model_used: str,
    prompt_hash: str,
    raw_response: str | None,
) -> int | None:
    conn = connect(db_path)
    try:
        # FK safety net (techne#106): never let a stale/missing thread id drop a
        # candidate — fall back to a NULL FK (loses provenance) rather than raising.
        if (
            thread_id is not None
            and not conn.execute("SELECT 1 FROM threads WHERE id = ?", (thread_id,)).fetchone()
        ):
            thread_id = None
        cur = conn.execute(
            # Exactly-once ingest (techne#236): re-emitting/re-ingesting the same thread
            # used to insert a DUPLICATE pending candidate. The partial UNIQUE index on
            # (prompt_hash, content) WHERE origin='reflect' (schema v14) makes this a
            # no-op on replay. Scoped to origin='reflect' so the consolidate/dream/
            # conflict/federation passes — which share this table but carry their own
            # anti-re-proposal guards and legitimately reuse prompt_hash values — are
            # untouched. content is part of the key because one prompt yields many
            # distinct candidates (all sharing prompt_hash); prompt_hash (NOT NULL) over
            # source_thread_id (the FK safety-net can NULL it, and SQLite treats NULLs as
            # distinct, which would silently defeat the dedup).
            """INSERT INTO knowledge_candidates
                 (scope, type, content, tags, confidence,
                  source_session_id, source_thread_id,
                  model_used, prompt_hash, raw_response, origin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reflect')
               ON CONFLICT(prompt_hash, content) WHERE origin = 'reflect' DO NOTHING""",
            (
                candidate.scope,
                candidate.type,
                redact_credentials(candidate.content),
                candidate.tags,
                candidate.confidence,
                session_id,
                thread_id,
                model_used,
                prompt_hash,
                redact_credentials(raw_response) if raw_response else None,
            ),
        )
        conn.commit()
        # rowcount==0 → the ON CONFLICT fired (already ingested). lastrowid is unreliable
        # after a skipped upsert (returns the prior insert's rowid), so gate on rowcount.
        if cur.rowcount == 0:
            return None
        return cur.lastrowid
    finally:
        conn.close()


def _write_live(db_path: Path, candidate: Candidate, source: str) -> int | None:
    """Write straight to the live knowledge / identity tables. Used only when
    config.reflect.write_to='live' — intended for after the test concludes."""
    if candidate.scope == "knowledge":
        from techne.core.knowledge import store

        return store(
            db_path,
            candidate.content,
            type=candidate.type,
            tags=candidate.tags,
            confidence=candidate.confidence,
            source=source,
        )
    if candidate.scope == "identity":
        from techne.core.identity import set_value

        set_value(db_path, candidate.type, candidate.content, source=source)
        return None
    return None


def _bucket_observations(threads: list[Thread], observations: list[dict]) -> dict[int, list[dict]]:
    """Map each thread_num to its observation dicts (resolved from indices)."""
    out: dict[int, list[dict]] = {}
    for t in threads:
        out[t.thread_num] = [
            observations[i] for i in t.observation_indices if 0 <= i < len(observations)
        ]
    return out


def load_session_for_reflection(config: Config, session_id: str) -> tuple[list[Thread], list[dict]]:
    """Reconstruct (threads, observations) for a session the same way
    run_session_end does — heuristic detection, no LLM. Lets the agent-assisted
    emit/ingest path operate on any past session by id.
    """
    from techne.core.state import get_observations
    from techne.core.thread_orchestrator import detect_summarize_with_metadata

    observations = get_observations(config.db_path, session_id)
    threads, _scores = detect_summarize_with_metadata(observations, config)
    return threads, observations


def _marker_has_unemitted_threads(marker: dict | None) -> bool:
    """Is a session still pending reflection given its emit-cursor marker (techne#269)?

    The ``reflect:emitted:<id>`` marker is a per-thread emit cursor: ``emitted`` is the
    list of prompt_hashes already delivered and ``total`` the session's reflectable
    thread count. A session is pending when threads remain (``len(emitted) < total``),
    so the SessionStart auto-trigger keeps nudging a large session until it's drained
    instead of the first partial emit silently marking the whole thing done.

    Backward-compat: markers written by techne#268 carry {count,total,remaining} with
    NO ``emitted`` list. Those predate the cursor; treat them as fully emitted (NOT
    pending) — re-surfacing a session the operator already saw once would be a
    surprising regression, and a #268-truncated marker deliberately recorded a nonzero
    ``remaining`` while still considering itself done.
    """
    if marker is None:
        return True  # never emitted → pending
    emitted = marker.get("emitted")
    if emitted is None:
        return False  # #268-era marker (no per-thread cursor) → treat as fully emitted
    return len(emitted) < marker.get("total", len(emitted))


def pending_reflection(config: Config, *, limit: int = 5) -> list[dict]:
    """Recent sessions that look reflectable but aren't fully emitted yet (techne#19).

    A cheap SessionStart heuristic for the auto-trigger: a session with at least
    ``reflect.min_observations_per_thread`` observations whose ``reflect:emitted:<id>``
    cursor still shows unemitted threads (techne#269). It deliberately COUNTS
    observations rather than running full thread detection (too costly for the bounded
    hook) and derives "threads remaining" straight from the marker payload — never a
    fresh detection pass. The emit round-trip self-corrects: ``techne_reflect_emit``
    sets the marker even when it yields an empty batch, so a non-reflectable session is
    nudged at most once; a session larger than the emit cap keeps surfacing (its
    ``emitted`` list grows each pass) until every thread is drained.

    Returns the still-pending sessions among the ``limit`` most-recent (newest first),
    each ``{"session_id", "observations"}`` — the marker filter runs *after* the
    LIMIT, so a genuinely-old pending session past that window is intentionally not
    surfaced (newest-unreflected is what the nudge cares about; explicit emit always
    works). Empty when nothing is pending.
    """
    from techne.core.extension_state import get_state

    min_obs = config.reflect.min_observations_per_thread
    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            """SELECT s.id AS session_id, COUNT(o.id) AS n
                 FROM sessions s
                 JOIN observations o ON o.session_id = s.id
             GROUP BY s.id
               HAVING n >= ?
             ORDER BY s.started_at DESC
                LIMIT ?""",
            (min_obs, limit),
        ).fetchall()
        return [
            {"session_id": r["session_id"], "observations": r["n"]}
            for r in rows
            if _marker_has_unemitted_threads(get_state(conn, f"reflect:emitted:{r['session_id']}"))
        ]
    finally:
        conn.close()


def emittable_thread_ids(
    config: Config,
    session_id: str,
    threads: list[Thread],
    observations: list[dict],
    *,
    conn=None,
) -> list[str]:
    """Persisted ids of the threads emit will actually produce a prompt for.

    THE SINGLE SOURCE OF TRUTH for "is this thread owed reflection". enqueue used
    to queue every thread while emit skipped any under
    min_observations_per_thread (default 8), so sub-threshold threads were queued,
    never emittable, and therefore never drainable — the banner accumulated rows
    nothing could clear (#527 r4). The threads table has no observation linkage,
    so the predicate cannot be expressed in SQL; both callers must come here.

    Pass an open *conn* when calling from inside a transaction that has just
    written the threads: the id lookup must see those uncommitted rows or every
    thread resolves to None and the caller queues nothing (#527 r5).
    """
    obs_by_thread = _bucket_observations(threads, observations)
    from techne.core.thread_orchestrator import persisted_thread_id

    ids: list[str] = []
    for t in threads:
        if len(obs_by_thread.get(t.thread_num, [])) < config.reflect.min_observations_per_thread:
            continue
        tid = persisted_thread_id(config.db_path, session_id, t, conn=conn)
        if tid:
            ids.append(tid)
    return ids


def enqueue_reflection_debt(
    db_path: Path, session_id: str, *, thread_ids: list[str], conn=None
) -> int:
    """Durably record that *session_id*'s threads are owed reflection (techne#514).

    THE GAP THIS CLOSES: `session_end` counted the debt and enqueued nothing. It
    even logged "403 obs, 19 threads", so the runtime KNEW — which is worse than
    not knowing, because the instrumentation made the rail read as healthy while
    the work evaporated. Reflection survived exactly one exit path, the polite
    one, and the impolite exits are the common case here (techne#490: Remote
    Control exits after a ~10min network outage).

    THREAD IDS, NOT RENDERED PROMPTS. A stored prompt embeds a snapshot that
    drifts from the ledger it describes; the drainer re-renders from
    observations, which are append-only and never deleted.

    IDEMPOTENT BY THE SCHEMA, not by a check-then-insert: `thread_id` is UNIQUE,
    so INSERT OR IGNORE makes re-running session_end (it fires once per turn) and
    a peer enqueueing the same thread both no-ops. No read-modify-write, so two
    sessions racing cannot double-queue.

    *thread_ids* IS REQUIRED, and the "queue every thread in this session" branch
    that used to stand in for it is GONE (#527 r5). It had no production caller
    left after r4 — both callers pass `emittable_thread_ids` — but its comment
    ("kept for callers that have no observation list") read as a sanctioned
    option, and its behaviour was precisely the r4 bug: queueing sub-threshold
    threads that emit will never produce a prompt for, so they can never be
    drained and the banner accumulates rows nothing can clear. A default that
    reintroduces a fixed bug is worse than no default. If you have no observation
    list, you cannot apply the threshold, and you are not in a position to say
    what is owed — get the observations.

    Does NOT filter on whether a thread was already reflected: the drainer
    re-checks the `reflect:emitted:` marker, which is the existing
    self-correcting cursor, and duplicating that here would let the two disagree.
    The emittability threshold is a different question and is applied by the
    caller, because it needs observations this function does not have.

    Returns the number of rows newly queued. Takes an optional open *conn* so the
    caller can fold this into its own transaction.
    """
    if not thread_ids:
        return 0
    own_conn = conn is None
    c = connect(db_path) if own_conn else conn
    try:
        cur = c.executemany(
            "INSERT OR IGNORE INTO reflection_queue (session_id, thread_id) VALUES (?, ?)",
            [(session_id, t) for t in thread_ids],
        )
        if own_conn:
            c.commit()
        return cur.rowcount or 0
    finally:
        if own_conn:
            c.close()


def pending_reflection_queue(db_path: Path, *, limit: int = 20) -> list[dict]:
    """Threads owed reflection, oldest first, across ALL sessions (techne#514).

    The queue-backed counterpart to `pending_reflection`, and the reason #514
    exists: that function scans the `limit` most-recent sessions and filters
    AFTER the LIMIT, so — by its own docstring — "a genuinely-old pending session
    past that window is intentionally not surfaced". Debt that ages out of the
    window becomes permanently invisible. This reads the ledger of what was
    actually owed instead of re-deriving it from recency.

    Returns [{queue_id, session_id, thread_id, enqueued_at}]. Empty when clear.
    """
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, session_id, thread_id, enqueued_at
                 FROM reflection_queue
                WHERE drained_at IS NULL
             ORDER BY id
                LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {
                "queue_id": r["id"],
                "session_id": r["session_id"],
                "thread_id": r["thread_id"],
                "enqueued_at": r["enqueued_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def drain_reflection_queue(db_path: Path, thread_ids: list[str]) -> int:
    """Mark *thread_ids* reflected. Returns rows affected.

    Stamps `drained_at` rather than deleting: the queue doubles as the record of
    what reflection debt EXISTED and when it was paid, which is the evidence
    #514 was filed on. A deleted row cannot show that the rail worked.

    Guarded on `drained_at IS NULL` so a second drain of the same thread does not
    rewrite the original timestamp — the first payment is the true one.
    """
    if not thread_ids:
        return 0
    conn = connect(db_path)
    try:
        placeholders = ",".join("?" * len(thread_ids))
        cur = conn.execute(
            f"""UPDATE reflection_queue
                   SET drained_at = CURRENT_TIMESTAMP
                 WHERE drained_at IS NULL AND thread_id IN ({placeholders})""",
            thread_ids,
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def emit_reflection_batch(
    config: Config,
    session_id: str,
    threads: list[Thread],
    observations: list[dict],
) -> list[dict]:
    """Build the per-thread reflection prompts WITHOUT calling an LLM.

    The agent-assisted (no-API-key) half of the reflect pass: techne renders the
    prompts here, an in-session agent (running on the operator's OAuth model)
    produces the JSON candidate arrays, and ingest_reflection_response feeds them
    back. Thread selection mirrors reflect_on_session exactly (same
    min_observations gate), so emit→agent→ingest is equivalent to the automated
    pass with the LLM step relocated into the agent loop.

    Returns [{thread_id, session_id, thread_num, prompt_hash, prompt}].
    """
    from techne.core.thread_orchestrator import persisted_thread_id

    obs_by_thread = _bucket_observations(threads, observations)
    batch: list[dict] = []
    for t in threads:
        t_obs = obs_by_thread.get(t.thread_num, [])
        if len(t_obs) < config.reflect.min_observations_per_thread:
            continue
        prompt = _build_prompt(t, t_obs, config.db_path, config)
        prompt_hash = _prompt_hash(prompt)
        batch.append(
            {
                # The real persisted FK id — resolved against the threads table, not
                # recomputed (techne#106: a re-detected thread hashes differently than
                # the saved row on legacy sessions). None → candidate gets a NULL FK.
                "thread_id": persisted_thread_id(config.db_path, session_id, t),
                "session_id": session_id,
                "thread_num": t.thread_num,
                "prompt_hash": prompt_hash,
                "prompt": prompt,
            }
        )
    return batch


def unemittable_queue_rows(config: Config, session_id: str) -> list[str]:
    """Undrained queue rows for *session_id* that no emit can produce a prompt for.

    THE #646 VISIBILITY SURFACE. `emittable_thread_ids` is the single source of
    truth for "is this thread owed reflection", but it is evaluated TWICE against
    separately-derived observation buckets — once at session_end (lifecycle) and
    again here at emit time. When the two disagree the queue row is real and the
    prompt is impossible, so the row can never be drained by doing the work.

    Deliberately does NOT drain them. Draining an unemittable row restores a clean
    banner and destroys the only evidence the class exists, which is exactly how
    it survived #527 r4. Report, don't sweep.
    """
    threads, observations = load_session_for_reflection(config, session_id)
    emittable = set(emittable_thread_ids(config, session_id, threads, observations))
    conn = connect(config.db_path)
    try:
        queued = [
            r["thread_id"]
            for r in conn.execute(
                "SELECT thread_id FROM reflection_queue "
                "WHERE session_id = ? AND drained_at IS NULL",
                (session_id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    return [t for t in queued if t not in emittable]


def reflection_debt_stats(config: Config) -> dict:
    """Queue-level counts: what is owed, what has been paid, and how old the debt is.

    Separate from candidate_stats (which counts PROPOSALS). This counts the LEDGER
    — the record #514 was filed on. `pending` is what the SessionStart banner
    reports; `unemittable` is the #646 subset of it that no amount of reflecting
    can clear.
    """
    conn = connect(config.db_path)
    try:
        row = conn.execute(
            "SELECT count(*) AS total, "
            "       sum(drained_at IS NULL) AS pending, "
            "       min(CASE WHEN drained_at IS NULL THEN enqueued_at END) AS oldest "
            "  FROM reflection_queue"
        ).fetchone()
        sessions = [
            r["session_id"]
            for r in conn.execute(
                "SELECT DISTINCT session_id FROM reflection_queue WHERE drained_at IS NULL"
            ).fetchall()
        ]
    finally:
        conn.close()
    pending = row["pending"] or 0
    stats = {
        "total": row["total"] or 0,
        "pending": pending,
        "drained": (row["total"] or 0) - pending,
        "sessions_pending": len(sessions),
        "oldest_pending": row["oldest"],
    }
    return stats


def emit_reflection_pass(
    config: Config,
    session_id: str | None = None,
    *,
    limit: int | None = None,
) -> dict:
    """Render one cursor-aware pass of a session's reflection prompts.

    THE ONE EMIT IMPLEMENTATION. Both `techne_reflect_emit` (MCP) and
    `techne reflect emit` (CLI) call this — #645 exists because they did not, and
    the CLI path silently skipped the cursor advance every one of them assumed was
    happening. A second implementation of a rail drifts on its BOOKKEEPING first,
    because that half is invisible to whoever runs it.

    Returns {session_id, count, batch} plus, when relevant, truncated/remaining/
    oversized/note/unemittable_queued.
    """
    from techne.core.extension_state import get_state, set_state

    if not session_id:
        # Default to the most-recent session that actually has unreflected work,
        # NOT simply the newest session by started_at: the canonical reflect-before-
        # restart call fires while a fresh (empty) session is the newest, so the raw
        # "ORDER BY started_at DESC" default landed on a count:0 session and forced a
        # manual hunt for the real id (tenishai/techne#267).
        pend = pending_reflection(config)
        session_id = pend[0]["session_id"] if pend else None
    if not session_id:
        return {"batch": [], "count": 0, "note": "no session with unreflected work"}

    threads, observations = load_session_for_reflection(config, session_id)
    batch = emit_reflection_batch(config, session_id, threads, observations)
    total = len(batch)

    # Per-thread emit cursor (techne#269/#236): a session with more reflectable threads
    # than the emit cap must drain losslessly across passes — the #268 marker collapsed
    # the whole session to "emitted" on the first partial emit, so threads past the cap
    # were never reflected. Load the prompt_hashes already delivered and exclude them, so
    # each pass takes the NEXT slice of unemitted threads: no overlap, no loss. The cursor
    # is keyed on prompt_hash — the stable, always-present per-thread id (thread_id can be
    # NULL for unpersisted threads) and the same key #236 dedups candidates on.
    conn = connect(config.db_path)
    try:
        marker = get_state(conn, f"reflect:emitted:{session_id}")
    finally:
        conn.close()
    already = set(marker.get("emitted", [])) if marker else set()
    # Stability invariant: prompt_hash is derived from the rendered prompt INCLUDING the
    # per-thread existing-knowledge block (reflect._build_prompt), so it must not shift
    # between cursor passes mid-drain — otherwise an already-emitted thread re-appears.
    # This holds on the DEFAULT path (write_to='candidates'): that path never mutates live
    # knowledge, and a completed session's observations are frozen, so the hash is stable.
    # KNOWN LIVE-MODE LIMITATION (write_to='live'): promoting knowledge mid-drain can alter
    # a drained thread's existing-knowledge block, changing its prompt_hash. The cursor
    # would then re-emit it, and #236's (prompt_hash, content) dedup would be sidestepped
    # (duplicate candidate / re-promotion).
    batch = [item for item in batch if item["prompt_hash"] not in already]

    # Bound the payload so a large session can't produce a batch that exceeds the MCP
    # transport's token cap — an oversized emit used to error out AND still mark the
    # session reflected, silently dropping the whole session's memory (techne#267).
    # `is not None` (not truthiness) so an explicit limit=0 is honored as "emit none"
    # rather than silently reinterpreted as the default.
    if limit is None:
        limit = config.reflect.emit_max_threads
    batch = batch[:limit]
    # Trim by serialized size to a char budget (proxy for the transport token cap),
    # keeping >=1 prompt so a lone huge thread still makes progress. Single O(n) pass
    # — accumulate a prefix that fits — rather than re-dumping the whole batch per pop.
    max_chars = config.reflect.emit_max_chars
    kept, running = [], 2  # 2 = the enclosing "[]"
    for item in batch:
        item_len = len(json.dumps(item)) + 1  # + separator headroom
        if kept and running + item_len > max_chars:
            break
        kept.append(item)
        running += item_len
    batch = kept if kept else batch[:1]

    # Cursor-relative truncation: `remaining` counts threads NOT YET emitted across all
    # passes (total minus the cumulative emitted set), so a multi-pass drain reports its
    # true shortfall and reaches 0 only when the whole session is done (techne#269).
    newly = [item["prompt_hash"] for item in batch]
    emitted_ids = sorted(already | set(newly))
    truncated = len(emitted_ids) < total
    remaining = total - len(emitted_ids)
    # A lone thread that alone exceeds the budget can't be delivered whole. Marking
    # the session emitted here would silently skip it — footgun 3, unchanged (Prudence,
    # #268). Detect it, leave the cursor UNADVANCED so the auto-trigger keeps surfacing
    # it, and flag the payload loudly.
    oversized = len(batch) == 1 and len(json.dumps(batch)) > max_chars
    delivered = bool(batch) and not oversized

    # Advance the cursor: record the cumulative set of emitted thread prompt_hashes plus
    # the session's total, so pending_reflection re-surfaces the session until every
    # thread is drained (techne#269) and re-emits skip already-delivered threads. This is
    # ALSO the exactly-once reflected-marker (techne#236 Gap 2) — one unified marker, not
    # two. We do NOT advance when nothing deliverable was produced.
    if delivered:
        conn = connect(config.db_path)
        try:
            set_state(
                conn,
                f"reflect:emitted:{session_id}",
                {
                    "emitted": emitted_ids,
                    "total": total,
                    "remaining": remaining,
                    "count": len(emitted_ids),
                },
            )
        finally:
            conn.close()

    result = {"session_id": session_id, "count": len(batch), "batch": batch}
    if truncated:
        result["truncated"] = True
        result["remaining"] = remaining
        result["note"] = (
            f"Session has {total} threads; emitted {len(batch)} this pass (cap={limit}). "
            f"{remaining} not yet emitted — re-emit with the same session_id (the "
            "per-thread cursor auto-resumes from where this pass stopped) to drain the rest."
        )
    if oversized:
        result["oversized"] = True
        result["note"] = (
            f"A single thread's serialized prompt exceeds emit_max_chars ({max_chars}) "
            "and can't be delivered whole; the session cursor is left UNADVANCED so it "
            "isn't silently skipped. Needs per-thread prompt chunking (#269 follow-up). "
            "Because the cursor drains threads IN ORDER, an oversized lone thread also "
            "blocks every thread after it in the session — the session remains pending "
            "until per-thread chunking lands (#269 follow-up)."
        )
    # #646: surface queue rows this session owes that no emit can render. Reported,
    # never drained — see unemittable_queue_rows.
    stuck = unemittable_queue_rows(config, session_id)
    if stuck:
        result["unemittable_queued"] = len(stuck)
        result["unemittable_note"] = (
            f"{len(stuck)} queued thread(s) for this session are below "
            f"reflect.min_observations_per_thread ({config.reflect.min_observations_per_thread}) "
            "at emit time, so no prompt can be rendered for them and reflecting can never "
            "drain them (tenishai/techne#646). They are left queued deliberately."
        )
    return result


def ingest_one_and_drain(
    config: Config,
    session_id: str,
    thread_id: str,
    prompt_hash: str,
    raw_response: str,
    *,
    model_used: str = "agent-assisted",
) -> dict:
    """Ingest one answered prompt AND mark its reflection debt paid.

    THE ONE INGEST-PLUS-BOOKKEEPING PATH (#644). The drain half lived only in the
    MCP tool, so `techne reflect ingest` — the path the documented /techne-promote
    skill actually runs — wrote candidates and left every queue row undrained
    forever. Both callers come here now.

    Drains ONLY on a pass that actually happened. parse_failures marks the branch
    whose own comment reads "THE PASS DID NOT HAPPEN" (see ingest_reflection_response)
    — draining there stamps a DISCARDED reflection as paid, permanently, since drain
    is guarded on drained_at IS NULL. errors covers the same ground for any other
    failure mode.
    """
    # Tolerate an inlined array: an agent calling this through MCP can hand back a
    # real JSON list rather than the string the parser expects. This normalization
    # lived in the MCP wrapper, which is precisely why it was easy to drop when the
    # two paths were unified — it belongs on the shared path, not on one caller.
    if not isinstance(raw_response, str):
        raw_response = json.dumps(raw_response) if raw_response else ""
    result = ingest_reflection_response(
        config,
        session_id,
        thread_id,
        prompt_hash,
        raw_response,
        model_used=model_used,
    )
    if thread_id and not result.get("parse_failures") and not result.get("errors"):
        result["reflection_debt_drained"] = drain_reflection_queue(config.db_path, [thread_id])
    return result


def ingest_reflection_batch(config: Config, items: list[dict]) -> dict:
    """Ingest a whole emit batch the agent has filled in. Returns summed counts.

    The file-round-trip half of the no-API-key path: `techne reflect emit -o f`,
    the agent fills each item's `raw_response`, `techne reflect ingest f`. An item
    whose raw_response is blank is a deliberately unanswered thread, not an error —
    it is skipped WITHOUT draining, so the debt stays owed.
    """
    totals = new_ingest_counts()
    totals["skipped"] = 0
    totals["drained"] = 0
    for it in items:
        raw = it.get("raw_response")
        if not isinstance(raw, str):
            raw = json.dumps(raw) if raw else ""  # tolerate an inlined array
        if not raw.strip():  # noqa: E501 - blank means the agent declined this thread
            totals["skipped"] += 1
            continue
        counts = ingest_one_and_drain(
            config,
            it.get("session_id", ""),
            it.get("thread_id", "") or "",
            it.get("prompt_hash", ""),
            raw,
            model_used=it.get("model_used", "agent-assisted"),
        )
        totals["drained"] += counts.pop("reflection_debt_drained", 0)
        for k, v in counts.items():
            totals[k] += v
    return totals


def effective_write_to(config: Config) -> str:
    """Return the effective write_to mode honoring the coexistence posture.

    When posture.mode == 'spool-primary', reflect is ADVISORY: proposals land in
    candidates, never auto-promoted into live knowledge that could drift from CC's
    curated memory. techne#107 PR4.

    Under posture 'observe' (default), returns config.reflect.write_to as-is so
    existing behaviour is preserved exactly.
    """
    posture = getattr(config, "posture", None)
    if posture is not None and posture.mode == "spool-primary":
        return "candidates"
    return config.reflect.write_to


def ingest_reflection_response(
    config: Config,
    session_id: str,
    thread_id: str,
    prompt_hash: str,
    raw_response: str,
    *,
    model_used: str = "agent-assisted",
) -> dict:
    """Process one agent-produced raw response (the JSON array parse_response
    expects) into knowledge_candidates — and live knowledge when
    config.reflect.write_to == 'live'. The single insertion path shared with
    reflect_on_session. `thread_id` is the real threads.id (from thread_row_id /
    the emit batch), so the candidates FK resolves. Returns per-thread counts.
    """
    counts = new_ingest_counts()
    parsed = parse_response(raw_response, config.reflect.max_candidates_per_thread)

    if parsed.failure:
        # THE PASS DID NOT HAPPEN, and this is the line whose absence was the bug.
        # `errors` is incremented so the count is not byte-identical to a correct
        # pass that found nothing — that identity is what let a whole session's
        # reflection be discarded and reported as success (techne#465).
        counts["errors"] += 1
        counts["parse_failures"] += 1
        log.warning(
            "reflect: response could not be parsed (%s) — this thread's reflection"
            " was DISCARDED, not empty (session=%s thread=%s, %d chars)%s",
            parsed.failure,
            session_id,
            thread_id,
            len(raw_response or ""),
            ""
            if config.reflect.log_raw_response
            else " — set reflect.log_raw_response to capture the response itself",
        )
        return counts

    if parsed.dropped:
        # The model DID produce candidates and every one of these was rejected by
        # validation. Distinct from a parse failure and equally invisible before
        # this: a prompt drifting out of contract shows up here first.
        counts["candidates_dropped"] = parsed.dropped
        log.warning(
            "reflect: %d candidate(s) rejected by validation (session=%s thread=%s)"
            " — bad scope, unknown type, or empty content",
            parsed.dropped,
            session_id,
            thread_id,
        )

    kept = [c for c in parsed.candidates if _meets_min_confidence(c, config.reflect.min_confidence)]
    below = len(parsed.candidates) - len(kept)
    if below:
        # THE SAME SILENCE, ONE LINE LATER — and on the DEFAULT config, not a
        # corner. min_confidence defaults to "medium" while the prompt invites
        # "low", so valid low-confidence candidates are dropped here and produced
        # a counts dict byte-identical to honest-empty. Drawing the boundary at
        # validation and stopping was arbitrary. (Prudence, #483 review.)
        counts["candidates_below_confidence"] = below
        log.warning(
            "reflect: %d candidate(s) below min_confidence=%s were discarded"
            " (session=%s thread=%s)",
            below,
            config.reflect.min_confidence,
            session_id,
            thread_id,
        )
    counts["candidates_proposed"] += len(kept)

    raw_for_store = raw_response if config.reflect.log_raw_response else None
    for c in kept:
        try:
            cand_id = _insert_candidate(
                config.db_path,
                c,
                session_id=session_id,
                thread_id=thread_id,
                model_used=model_used,
                prompt_hash=prompt_hash,
                raw_response=raw_for_store,
            )
            # None → the exactly-once guard skipped a duplicate (techne#236): don't
            # count it as written and don't re-promote it into live knowledge.
            if cand_id is None:
                continue
            counts["candidates_written"] += 1
            if effective_write_to(config) == "live":
                promoted_id = _write_live(
                    config.db_path, c, source=f"reflect:{thread_id or 'unknown'}"
                )
                _mark_promoted(config.db_path, cand_id, promoted_id)
                counts["candidates_promoted"] += 1
        except Exception:
            log.exception(
                "ingest_reflection_response: failed to insert/promote candidate"
                " (session=%s thread=%s)",
                session_id,
                thread_id,
            )
            counts["errors"] += 1
    return counts


def reflect_on_session(
    config: Config,
    session_id: str,
    threads: list[Thread],
    observations: list[dict],
    *,
    generate_fn=None,
) -> dict:
    """Run reflection on every qualifying thread in the just-ended session.

    Returns {threads_considered, threads_reflected, candidates_proposed,
             candidates_written, candidates_promoted, errors}.
    Never raises — failures are counted in `errors`. The caller (run_session_end)
    must still treat session teardown as successful.

    `generate_fn` is an injectable LLM callable for tests; in prod it's built
    from config.reflect.model (falling back to config.model).
    """
    result = {"threads_considered": len(threads), "threads_reflected": 0, **new_ingest_counts()}
    if not config.reflect.enabled:
        return result

    # One LLM client for the whole pass, with optional override of the
    # session-wide model (so reflection can run on a cheaper model than
    # whatever the thread-summarizer uses).
    reflect_config = config
    if config.reflect.model:
        # Shallow override of config.model for the generate fn only
        import copy

        reflect_config = copy.copy(config)
        reflect_config.model = config.reflect.model
    if generate_fn is None:
        try:
            generate_fn = _make_generate_fn(reflect_config)
        except Exception:
            log.exception(
                "reflect_on_session: could not build generate_fn (session=%s)", session_id
            )
            result["errors"] += 1
            return result

    from techne.core.thread_orchestrator import persisted_thread_id

    obs_by_thread = _bucket_observations(threads, observations)

    for t in threads:
        t_obs = obs_by_thread.get(t.thread_num, [])
        if len(t_obs) < config.reflect.min_observations_per_thread:
            continue

        prompt = _build_prompt(t, t_obs, config.db_path, config)
        prompt_hash = _prompt_hash(prompt)

        try:
            raw = generate_fn(prompt)
        except Exception:
            log.exception(
                "reflect_on_session: LLM call failed (session=%s thread_num=%s)",
                session_id,
                t.thread_num,
            )
            result["errors"] += 1
            continue
        result["threads_reflected"] += 1

        # Same insertion path as the agent-assisted ingest — one code path. Resolve
        # the ACTUAL persisted threads.id (the techne#106 drift-resistant lookup that
        # emit_reflection_batch already uses), not a recomputed thread_row_id which
        # drifts for re-detected heuristic threads → candidate FK silently NULLed
        # (techne#165). None → ingest stores a NULL FK rather than losing the candidate.
        thread_id = persisted_thread_id(config.db_path, session_id, t)
        counts = ingest_reflection_response(
            config, session_id, thread_id, prompt_hash, raw, model_used=reflect_config.model
        )
        for key, val in counts.items():
            result[key] += val

    return result


def _mark_promoted(db_path: Path, candidate_id: int, promoted_to_id: int | None) -> None:
    """Mark a candidate promoted by the write_to='live' auto-path. Stamps
    decided_by='reflect-auto' for the audit trail (techne#108) — no human reviewed it.
    A separate UPDATE from promote_candidate's dispatch (this path never applies an
    action); decision_reason is left NULL — there's no human reason on the auto-path."""
    conn = connect(db_path)
    try:
        conn.execute(
            """UPDATE knowledge_candidates
                  SET status = 'promoted',
                      promoted_to_id = ?,
                      reviewed_at = CURRENT_TIMESTAMP,
                      decided_by = 'reflect-auto'
                WHERE id = ?""",
            (promoted_to_id, candidate_id),
        )
        conn.commit()
    finally:
        conn.close()


# --- Read-side helpers used by the CLI -----------------------------------


def list_candidates(
    db_path: Path,
    status: str = "pending",
    limit: int = 50,
) -> list[dict]:
    if status not in ("pending", "promoted", "discarded", "all"):
        raise ValueError(f"Invalid status '{status}'")
    conn = connect(db_path)
    try:
        if status == "all":
            rows = conn.execute(
                """SELECT * FROM knowledge_candidates
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM knowledge_candidates
                    WHERE status = ?
                    ORDER BY created_at DESC LIMIT ?""",
                (status, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _card_type_or_fact(ktype: str, tags: str) -> str:
    """Refuse to mint a `document_card` that has no `doc:<id>` pointer (#633).

    `document_card` is a MANAGED type: the card summary pass owns it and supplies
    the librarian pointer. Neither reflect-promote nor consolidate-merge has a
    document in hand, so both could mint a card with nothing behind it — four such
    rows exist (knowledge 361, 366, 624, 625), and `fsck`'s dangling_cards check has
    been red since 2026-06-21 because of them.

    Demote rather than reject: the content is real prose about a document and worth
    keeping, it just isn't a card. This mirrors the fallback already on the same line
    at both call sites (an unrecognized type becomes `fact`).
    """
    from techne.core.cards import CARD_TYPE, card_doc_id

    if ktype == CARD_TYPE and card_doc_id(tags or "") is None:
        log.warning(
            "refusing to mint an unbacked %s (no doc: pointer in tags); storing as fact "
            "instead — tenishai/techne#633",
            CARD_TYPE,
        )
        return "fact"
    return ktype


def _apply_add(conn, row, db_path: Path) -> int | None:
    """Apply an additive candidate — the original reflect promotion path, verbatim.

    Runs inside promote_candidate's open transaction (techne#108 dispatch); a
    failure here rolls back the status flip too. Returns the new knowledge.id, or
    None for an identity entry.

    Dependency edges (techne#215): a dream-origin candidate is a new insight
    explicitly synthesized from its source entries (recorded in `target_ids`). The
    derivation relationship is genuinely known at promotion time — NOT inferred from
    similarity. Writing dep edges here, inside the same transaction as the knowledge
    insert, makes the relationship atomic and ensures the cascade can fire the moment
    any source is superseded. No edges are written for reflect/consolidate add
    candidates: those have no structural source relationship.
    """
    from techne.core.knowledge import (
        CONFIDENCE_NUMERIC,
        VALID_CONFIDENCE,
        VALID_TYPES,
        _add_dep_in_conn,
    )

    scope = row["scope"]
    source = f"reflect-promote:{row['source_session_id']}:{row['id']}"
    if scope == "knowledge":
        content = redact_credentials(row["content"])
        conf = row["confidence"] if row["confidence"] in VALID_CONFIDENCE else "medium"
        numeric = CONFIDENCE_NUMERIC[conf]
        tags = row["tags"] or ""
        ktype = _card_type_or_fact(row["type"] if row["type"] in VALID_TYPES else "fact", tags)
        cur2 = conn.execute(
            """INSERT INTO knowledge
                 (type, content, source, tags, confidence, confidence_numeric)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ktype, content, source, tags, conf, numeric),
        )
        new_id = cur2.lastrowid

        # Populate dependency edges for dream candidates (techne#215).
        # Only dream candidates carry a meaningful derived-from relationship: the
        # new insight was synthesized from the explicit source entries in target_ids.
        # Superseding any source later will cascade the insight to `uncertain` via
        # the existing _cascade_uncertain machinery — no new cascade logic needed.
        if row["origin"] == "dream":
            source_ids = json.loads(row["target_ids"] or "[]")
            for src_id in source_ids:
                _add_dep_in_conn(conn, new_id, src_id)

        return new_id
    if scope == "identity":
        from techne.core.identity import set_value

        # Use the SAME connection — a second connection would deadlock against
        # this one's open write (the candidate status UPDATE).
        set_value(db_path, row["type"], row["content"], source=source, conn=conn)
        return None
    return None


def _apply_supersede(conn, row, db_path: Path) -> int | None:
    """Apply a 'supersede' proposal (techne#108 consolidate): replace ONE existing
    row (target_ids[0]) with the proposal's corrected/clarified content, inheriting
    the old row's type/tags/anchor (supersede semantics). Returns the new knowledge.id."""
    from techne.core.knowledge import _supersede_in_conn

    targets = json.loads(row["target_ids"] or "[]")
    if len(targets) != 1:
        raise ValueError(
            f"supersede candidate {row['id']} needs exactly one target_id, got {targets}"
        )
    source = f"consolidate-supersede:{row['id']}"
    return _supersede_in_conn(conn, targets[0], redact_credentials(row["content"]), source)


def _apply_retire(conn, row, db_path: Path) -> int | None:
    """Apply a 'retire' proposal (techne#213 contradiction detection): link an EXISTING
    live row to an EXISTING live survivor via `superseded_by` — no new row minted.

    target_ids = [old_id, survivor_id] (ORDER-SIGNIFICANT — old is retired into survivor).
    Both are already live knowledge rows (the two entries the conflict check compared),
    so retirement points old_id.superseded_by at survivor_id rather than minting a
    duplicate of the survivor's fact. The candidate's `content` holds the survivor's
    (optionally LLM-refined) text for operator DISPLAY only; the applier links, it does
    not write content — apply a wording edit separately if desired. Returns survivor_id."""
    from techne.core.knowledge import _retire_in_conn

    targets = json.loads(row["target_ids"] or "[]")
    if len(targets) != 2:
        raise ValueError(
            f"retire candidate {row['id']} needs exactly two target_ids "
            f"[old, survivor], got {targets}"
        )
    old_id, survivor_id = targets[0], targets[1]
    return _retire_in_conn(conn, old_id, survivor_id)


def _apply_merge(conn, row, db_path: Path) -> int | None:
    """Apply a 'merge' proposal (techne#108 consolidate): collapse N existing rows
    (target_ids) into one merged row carrying the proposal's explicitly chosen
    type/confidence/tags. Returns the new knowledge.id."""
    from techne.core.knowledge import (
        CONFIDENCE_NUMERIC,
        VALID_CONFIDENCE,
        VALID_TYPES,
        _merge_in_conn,
    )

    targets = json.loads(row["target_ids"] or "[]")
    if len(targets) < 2:
        raise ValueError(
            f"merge candidate {row['id']} needs at least two target_ids, got {targets}"
        )
    conf = row["confidence"] if row["confidence"] in VALID_CONFIDENCE else "medium"
    ktype = _card_type_or_fact(
        row["type"] if row["type"] in VALID_TYPES else "fact", row["tags"] or ""
    )
    return _merge_in_conn(
        conn,
        targets,
        redact_credentials(row["content"]),
        ktype=ktype,
        confidence=conf,
        numeric=CONFIDENCE_NUMERIC[conf],
        tags=row["tags"] or "",
        source=f"consolidate-merge:{row['id']}",
    )


# Per-action appliers (techne#108). promote_candidate owns the transaction + status
# guard; each applier takes the open conn + the candidate row and returns the new
# knowledge.id (or None). 'add' is reflect's original path; 'supersede'/'merge' are
# the consolidate pass; 'retire' is the contradiction-detection pass (techne#213 —
# links two EXISTING live rows without minting). ('connect' / dream is the deferred PR3.)
_APPLIERS = {
    "add": _apply_add,
    "supersede": _apply_supersede,
    "merge": _apply_merge,
    "retire": _apply_retire,
}


def promote_candidate(
    db_path: Path,
    candidate_id: int,
    *,
    decided_by: str | None = None,
    decision_reason: str | None = None,
    force: bool = False,
) -> int | None:
    """Promote a candidate to live knowledge / identity. Returns the new
    knowledge.id (or None for identity entries). Raises if already reviewed.

    Atomic: a single connection performs the status guard (UPDATE WHERE
    status='pending'), the action-specific apply, and the promoted_to_id update
    inside one transaction.  Concurrent callers that both see 'pending' will
    race on the UPDATE; the loser gets rowcount==0 and raises immediately
    without writing a duplicate knowledge row.

    The `action` column (techne#108) selects how the proposal is applied; a NULL
    action (legacy rows) routes to 'add'. `decided_by`/`decision_reason` record the
    review provenance.

    Faithfulness soft gate (techne#252): for merge/supersede candidates that have
    been explicitly flagged unfaithful (faithfulness_ok == 0), promotion is refused
    and a FaithfulnessGateError (subclass of ValueError) is raised — the candidate
    status is NOT changed.  Pass ``force=True`` to override after reviewing the
    warning.  Unchecked (faithfulness_ok IS NULL) and faithful (== 1) candidates
    always promote normally; the gate only blocks explicitly-flagged-unfaithful
    merge/supersede candidates.
    """
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM knowledge_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Candidate {candidate_id} not found")

        # action is NOT NULL DEFAULT 'add' (reconciler backfills pre-#108 rows), so
        # this `or` never actually fires — defensive only.
        action = row["action"] or "add"
        applier = _APPLIERS.get(action)
        if applier is None:
            raise ValueError(f"Candidate {candidate_id} has unknown action {action!r}")

        # Faithfulness soft gate (techne#252): refuse explicitly-unfaithful
        # merge/supersede candidates before touching status. The check fires only when
        # faithfulness_ok == 0 (checked and flagged); NULL (unchecked) and 1 (faithful)
        # pass through. force=True bypasses the gate after operator review.
        if not force and action in _FAITHFULNESS_GATED_ACTIONS and row["faithfulness_ok"] == 0:
            warning = row["faithfulness_warning"] or "candidate was flagged unfaithful"
            raise FaithfulnessGateError(
                f"Candidate {candidate_id} ({action}) was flagged unfaithful: "
                f"{warning}. "
                f"Promote with force=True (CLI: --force) to override after reviewing."
            )

        # Atomic guard: only one concurrent caller can flip status from pending.
        cur = conn.execute(
            """UPDATE knowledge_candidates
                  SET status = 'promoted', reviewed_at = CURRENT_TIMESTAMP,
                      decided_by = ?, decision_reason = ?
                WHERE id = ? AND status = 'pending'""",
            (decided_by, decision_reason, candidate_id),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"Candidate {candidate_id} already {row['status']} — cannot re-promote"
            )

        # Apply on the SAME connection so a failure here rolls back the status flip.
        promoted_to_id = applier(conn, row, db_path)

        conn.execute(
            "UPDATE knowledge_candidates SET promoted_to_id = ? WHERE id = ?",
            (promoted_to_id, candidate_id),
        )
        conn.commit()
        return promoted_to_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def discard_candidate(
    db_path: Path,
    candidate_id: int,
    *,
    decided_by: str | None = None,
    decision_reason: str | None = None,
) -> None:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """UPDATE knowledge_candidates
                  SET status = 'discarded', reviewed_at = CURRENT_TIMESTAMP,
                      decided_by = ?, decision_reason = ?
                WHERE id = ? AND status = 'pending'""",
            (decided_by, decision_reason, candidate_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"Candidate {candidate_id} not found or already reviewed")
        conn.commit()
    finally:
        conn.close()


def candidate_stats(db_path: Path) -> dict:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """SELECT status, COUNT(*) AS n
                 FROM knowledge_candidates GROUP BY status"""
        ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        scope_rows = conn.execute(
            """SELECT scope, status, COUNT(*) AS n
                 FROM knowledge_candidates GROUP BY scope, status"""
        ).fetchall()
        by_scope = {(r["scope"], r["status"]): r["n"] for r in scope_rows}
        return {
            "pending": counts.get("pending", 0),
            "promoted": counts.get("promoted", 0),
            "discarded": counts.get("discarded", 0),
            "by_scope": {f"{s[0]}.{s[1]}": n for s, n in by_scope.items()},
        }
    finally:
        conn.close()
