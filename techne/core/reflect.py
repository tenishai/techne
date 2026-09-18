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

import hashlib
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

A candidate can fail in one of two ways. Check both before you keep it, and
keep it only if it is neither.

  TOO OBVIOUS — a competent engineer in this domain already knows it: how a
  common tool behaves by default, or generic hygiene advice.
  "Re-read the repo before citing it" is this failure.

  TOO SPECIFIC — it is true today and false after the next change, because it
  leans on an identifier, a count, a version or a status. "The image tags are
  9efefd3 and 07c015b" is this failure; the rule those tags illustrate is not.

Aim between them: NON-OBVIOUS to a competent practitioner, and STILL TRUE once
the identifiers change. If you cite an identifier, count or version, ask whether
the claim survives it changing; if it does not, state the rule and drop the
identifier.

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


def _write_live(
    db_path: Path, candidate: Candidate, source: str, session_id: str | None = None
) -> int | None:
    """Write straight to the live knowledge / identity tables. Used only when
    config.reflect.write_to='live' — intended for after the test concludes.

    `session_id` goes into `origin_session` in the same INSERT (techne#949), the way
    reviewed promotion stamps it."""
    if candidate.scope == "knowledge":
        from techne.core.knowledge import store

        return store(
            db_path,
            candidate.content,
            type=candidate.type,
            tags=candidate.tags,
            confidence=candidate.confidence,
            source=source,
            provenance={"origin_session": session_id} if session_id else None,
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


def _marker_has_unemitted_threads(marker: dict | None, *, observations: int | None = None) -> bool:
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
    total = marker.get("total", len(emitted))
    # A SESSION THAT LATER GROWS MUST RE-OPEN (Prudence, #734 r2). Writing on a
    # completed-but-empty pass fixed the uncleanable cursor, but it also stamped
    # `total: 0` on a session that had nothing renderable YET — and this function reads
    # `total` out of the marker and never recomputes it, so `0 < 0` is false forever.
    # MEASURED against both trees: thin session, empty emit, 12 observations backfilled,
    # threads re-detected — origin/main nudged the session, the new marker returned []
    # while an explicit emit returned count: 1. Both consumers went quiet (the
    # SessionStart nudge and the #643 startup drain).
    #
    # The marker now records the observation count it was written at, so a caller that
    # knows the CURRENT count can say "this session has grown since we judged it".
    # Legacy markers carry no count and are unaffected — absent means "cannot tell",
    # which keeps the old behaviour rather than re-surfacing every historical session.
    seen_obs = marker.get("observations")
    if observations is not None and seen_obs is not None and observations > seen_obs:
        return True
    if len(emitted) > total:
        # A CORRUPT CURSOR MUST NOT READ AS "DONE" (techne#733). Recording more emitted
        # entries than the session has threads is impossible under a stable key, so this
        # marker's contents are not evidence of anything — and the arithmetic below would
        # quietly answer "fully emitted" for the sessions whose bookkeeping is WORST.
        # MEASURED on the operator's store: six sessions carried over-full cursors (one at
        # 51 entries for 24 threads) and every one of them had dropped out of the
        # SessionStart nudge, which is how 126 owed queue rows were reported as 50.
        # "We do not know" is a reason to re-surface, never to stay silent — the same
        # fail-open defect #709 closed in check_model_bound_rails.
        return True
    return len(emitted) < total


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
            if _marker_has_unemitted_threads(
                get_state(conn, f"reflect:emitted:{r['session_id']}"), observations=r["n"]
            )
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

    eligible: list[tuple[str | None, Thread, list[dict]]] = []
    for t in threads:
        t_obs = obs_by_thread.get(t.thread_num, [])
        if len(t_obs) < config.reflect.min_observations_per_thread:
            continue
        eligible.append((persisted_thread_id(config.db_path, session_id, t), t, t_obs))

    # ONE ITEM PER PERSISTED THREAD, AND THE COLLISION IS RESOLVED LOSSLESSLY
    # (techne#733; corrected by Prudence at #734 r2). Two Thread objects can resolve to
    # ONE threads.id — re-detection splits a thread whose rows still map back to the
    # saved row. Measured on session 0ae96e10: 24 items, 23 distinct ids.
    #
    # THE COLLIDING ITEMS ARE NOT THE SAME PROMPT, which is what an earlier revision of
    # this comment claimed. `_bucket_observations` keys on `thread_num`, so each side
    # carries its OWN observations and `_build_prompt` renders only what it is handed.
    # So PICKING a winner — by first-seen or by richest — silently discards the loser's
    # observations, and they never come back: measured across three trees, main emitted
    # both (3 and 11 observations), first-wins dropped the 11, richest-wins dropped the
    # 3. Against the merge base either choice converts "both reflected, one wasted
    # drain" into "one reflected, the rest discarded" — on the rail whose whole stake is
    # that dropping a thread loses the session's memory silently.
    #
    # AND THE DUPLICATE IT WAS AVOIDING WAS ALREADY HARMLESS: `drain_reflection_queue`
    # is guarded on `drained_at IS NULL`, so the second stamp is a no-op.
    #
    # UNIONING THE INDEX SETS IS THEREFORE BOTH SMALLER AND CORRECT, and the reason is
    # DETECTOR-CONDITIONAL — worth stating precisely on a change whose whole thesis is
    # that trusting a comment cost two months (Prudence, #734 r3).
    #
    # On `threads.detector: heuristic` (the default) the buckets really are disjoint BY
    # CONSTRUCTION: `split_at_boundaries` appends each index exactly once in ascending
    # order and `merge_pass` only concatenates adjacent segments, so the indices arrive
    # already sorted and unique and `sorted(set(...))` is a strict no-op for any thread
    # that did not collide. On `threads.detector: llm` — a supported, validated config
    # value — `observation_indices` comes from `_parse_threads` reading an LLM's free-text
    # `Observations: 0,1,2` line, and NOTHING validates that parse for ordering,
    # duplicates or overlap. "Every observation belongs to exactly one thread" is a
    # REQUEST IN A PROMPT there, not an invariant.
    #
    # The union is still right on that path — it reorders to chronological, drops a
    # duplicate render, and a genuine cross-thread overlap still merges correctly — but
    # it is right for a weaker reason, and `sorted(set(...))` applies to every persisted
    # tid rather than only collided ones, so the claim has to hold universally.
    merged: dict[str, list[int]] = {}
    rep: dict[str, int] = {}
    for idx, (tid, t, t_obs) in enumerate(eligible):
        if tid is None:
            continue
        keep = [i for i in t.observation_indices if 0 <= i < len(observations)]
        if tid not in merged:
            merged[tid], rep[tid] = list(keep), idx
        else:
            merged[tid].extend(keep)
            # The representative supplies label/summary only; richest bucket describes
            # the merged thread best.
            if len(keep) > len(eligible[rep[tid]][2]):
                rep[tid] = idx
    for tid in merged:
        merged[tid] = sorted(set(merged[tid]))

    batch: list[dict] = []
    for idx, (tid, t, t_obs) in enumerate(eligible):
        if tid is not None:
            if rep[tid] != idx:
                continue
            t_obs = [observations[i] for i in merged[tid]]
        prompt = _build_prompt(t, t_obs, config.db_path, config)
        prompt_hash = _prompt_hash(prompt)
        item = {
            # The real persisted FK id — resolved against the threads table, not
            # recomputed (techne#106: a re-detected thread hashes differently than
            # the saved row on legacy sessions). None → candidate gets a NULL FK.
            "thread_id": tid,
            "session_id": session_id,
            "thread_num": t.thread_num,
            "prompt_hash": prompt_hash,
            "prompt": prompt,
        }
        if tid is None:
            obs_key = _observations_key(t_obs)
            if obs_key:
                item["obs_key"] = obs_key
        batch.append(item)
    return batch


def _observations_key(observations: list[dict]) -> str | None:
    """A cursor key for an UNPERSISTED thread that does not move with knowledge (#954).

    Hashes the thread's observation ids and nothing else. `prompt_hash` covers the
    rendered prompt, which carries the existing-knowledge block, so it changes on any
    knowledge write; observation ids change only if the thread's own membership does.
    None when any observation has no id (in-memory threads), leaving the caller on
    prompt_hash.
    """
    ids = [o.get("id") for o in observations]
    if not ids or any(i is None for i in ids):
        return None
    digest = hashlib.sha256(",".join(str(i) for i in sorted(ids)).encode()).hexdigest()
    return f"obs-{digest[:16]}"


def _owed_thread_ids(config: Config, session_id: str) -> set[str]:
    """Thread ids this session still owes: queue rows with no `drained_at` stamp.

    The LEDGER half of the cursor/ledger pair. `_emitted_keys` says the two are keyed
    on the same thing so they "agree by construction"; nothing enforced that, which is
    techne#900. This is what lets a caller check it.
    """
    conn = connect(config.db_path)
    try:
        return {
            r["thread_id"]
            for r in conn.execute(
                "SELECT thread_id FROM reflection_queue "
                "WHERE session_id = ? AND drained_at IS NULL",
                (session_id,),
            ).fetchall()
        }
    finally:
        conn.close()


def _declined_keys(config: Config, session_id: str) -> set[str]:
    """Threads the agent RENDERED, came back for, and deliberately left unanswered.

    THE DISTINCTION #900's HEAL TURNS ON (Prudence, #901 r1). A blank `raw_response` is
    blessed by `ingest_reflection_batch` as a decline: skipped without draining, so the
    debt stays owed. That leaves exactly the shape the heal fires on — owed row, cursor
    claims it — so the documented "repeat until the batch comes back empty" loop never
    terminated. Measured: 6 iterations, the same thread every time.

    An orphan is a thread ingest NEVER SAW. A decline is one it saw and refused. Only the
    first is a bookkeeping accident, and only the first should be re-offered.
    """
    from techne.core.extension_state import get_state

    conn = connect(config.db_path)
    try:
        marker = get_state(conn, f"reflect:emitted:{session_id}")
    finally:
        conn.close()
    if not marker:
        return set()
    return set(marker.get("declined", []))


def _record_declines(config: Config, session_id: str, thread_ids: list[str]) -> None:
    """Add *thread_ids* to the session's declined set, preserving the rest of the marker."""
    from techne.core.extension_state import get_state, set_state

    if not session_id or not thread_ids:
        return
    conn = connect(config.db_path)
    try:
        marker = get_state(conn, f"reflect:emitted:{session_id}")
        # NEVER MINT A MARKER HERE (Prudence, #901 r2). `dict(... or {})` wrote
        # `{"declined": [...]}` with no `emitted` key, and `_marker_has_unemitted_threads`
        # reads `emitted is None` as a #268-era marker: NOT PENDING, permanently, ahead of
        # the observations-growth re-open. That silences the SessionStart nudge AND
        # `pending_reflection`, which is where emit picks its default target — so the
        # session becomes unreachable from both hooks and both emit entry points, and only
        # an explicit id can still get to it. Reachable two ways she measured: an oversized
        # emit writes no marker, and a batch can name a session that never emitted.
        #
        # Skipping is not a compromise, it is the correct answer. With no marker
        # `_emitted_keys` rebuilds from DRAINED rows, a declined row is not drained, so the
        # cursor never claims it and the heal cannot re-offer it. There is nothing to
        # suppress.
        if not marker:
            return
        marker = dict(marker)
        marker["declined"] = sorted(set(marker.get("declined", [])) | set(thread_ids))
        set_state(conn, f"reflect:emitted:{session_id}", marker)
    finally:
        conn.close()


def _clear_decline(config: Config, session_id: str, thread_id: str) -> None:
    """Drop *thread_id* from the declined set — it was answered after all.

    Without this the record would outlive the decision it describes, and a thread the
    agent came back and answered would still read as refused.
    """
    from techne.core.extension_state import get_state, set_state

    if not session_id or not thread_id:
        return
    conn = connect(config.db_path)
    try:
        marker = get_state(conn, f"reflect:emitted:{session_id}")
        if not marker or thread_id not in set(marker.get("declined", [])):
            return
        marker = dict(marker)
        marker["declined"] = sorted(set(marker["declined"]) - {thread_id})
        set_state(conn, f"reflect:emitted:{session_id}", marker)
    finally:
        conn.close()


def clear_declines(config: Config, session_id: str, thread_ids: list[str] | None = None) -> int:
    """Retract declines so `emit` offers those threads again. Returns the count cleared.

    THE WAY BACK, AND WITHOUT IT THE DECLINE IS A TRAP (Prudence, #901 r2). `techne
    reflect emit` PRE-FILLS `raw_response: ""` as a placeholder, and `ingest` reads that
    same literal as a refusal — so an ordinary partial fill is indistinguishable from a
    deliberate one. Before this PR the collision was harmless because #900's heal
    re-offered the thread; #901 r1 removed that recovery to stop the drain loop spinning,
    and removing a recovery without adding one leaves a thread owed forever and unpayable
    if its batch file is lost.

    A decline is still a decision — nothing re-offers it on its own. It is just no longer
    an irreversible one.
    """
    from techne.core.extension_state import get_state, set_state

    if not session_id:
        return 0
    conn = connect(config.db_path)
    try:
        marker = get_state(conn, f"reflect:emitted:{session_id}")
        current = set((marker or {}).get("declined", []))
        if not marker or not current:
            return 0
        drop = current if thread_ids is None else current & set(thread_ids)
        if not drop:
            return 0
        marker = dict(marker)
        marker["declined"] = sorted(current - drop)
        set_state(conn, f"reflect:emitted:{session_id}", marker)
        return len(drop)
    finally:
        conn.close()


def declined_queue_rows(config: Config, session_id: str) -> list[str]:
    """Undrained rows whose thread was rendered, returned for, and left unanswered.

    Reported separately from `orphaned` because the two make OPPOSITE promises to the
    operator: an orphan self-heals on the next emit, a decline is not re-offered while
    the cursor claims the thread. Both are real owed debt and neither is ever swept
    (#646).
    """
    owed = _owed_thread_ids(config, session_id)
    return sorted(owed & _declined_keys(config, session_id)) if owed else []


def unemittable_queue_rows(config: Config, session_id: str) -> list[str]:
    """Undrained queue rows for *session_id* that no emit can produce a prompt for.

    THE #646 VISIBILITY SURFACE, WITH ITS CAUSE CORRECTED (techne#795). This used to
    say `emittable_thread_ids` "is evaluated TWICE against separately-derived
    observation buckets" and that the disagreement was the cause. MEASURED
    2026-08-27 and refuted: the predicate is fine, and the two calls were running
    over DIFFERENT PARTITIONS because `run_session_end` replaced the boundary table
    and not the thread table. Fixed in techne#793/#797; see
    `docs/arch/reflect-pipeline.md` and `docs/reflect-guide.md`.

    WHAT THIS FUNCTION RETURNS IS STILL THE UNION OF BOTH POPULATIONS, so callers
    must not treat it as one thing:
      - SUPERSEDED — the id is absent from the current detection. Provably stale,
        nothing is unreflected, and `techne reflect reconcile` retires these. Use
        `superseded_queue_rows` to get exactly this subset.
      - BELOW THRESHOLD — the id IS current but the thread now sits under
        `min_observations_per_thread`. Two routes in: re-partitioning moved observations
        to neighbours (the #646-relevant one), or the floor itself was raised, which is
        how `test_unemittable_queue_rows_are_reported_and_never_drained` builds it. This
        is the population #646 exists to keep visible.

    Deliberately does NOT drain either. Blanket-draining restores a clean banner and
    destroys the only evidence the class exists, which is exactly how it survived
    #527 r4. Report, don't sweep — and retire the superseded subset through
    `reconcile`, which is a different act from draining.
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


def superseded_queue_rows(config: Config, session_id: str) -> list[str]:
    """Undrained queue rows whose thread id is not in the session's CURRENT detection.

    THE STRICT SUBSET OF `unemittable_queue_rows` THAT IS PROVABLY STALE (techne#646).
    Both are undrainable, but for different reasons and with different standing:

    - SUPERSEDED (here): the id does not appear in the current detection at all. It
      belongs to a partition `run_session_end` replaced. Its observations are still
      covered — by a current thread under a different id — so nothing is unreflected,
      and the row is bookkeeping about a thread that no longer exists.
    - BELOW THRESHOLD (the remainder): the id IS current, but re-partitioning moved
      observations to neighbours and left the thread under
      `min_observations_per_thread`. That is the genuine disagreement #646 was filed
      on, and it stays visible.

    The distinction is what makes reconciliation legitimate rather than sweeping. #646
    forbids draining these rows to make the banner green, because that "destroys the
    only evidence the class exists" — a rule about blanket-clearing. Retiring a row we
    can demonstrate points at a partition that no longer exists removes noise while
    leaving the signal, and the reconciler reports what it removed either way.
    """
    threads, observations = load_session_for_reflection(config, session_id)
    from techne.core.thread_orchestrator import persisted_thread_id

    current = set()
    for t in threads:
        tid = persisted_thread_id(config.db_path, session_id, t)
        if tid:
            current.add(tid)

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
    return [t for t in queued if t not in current]


def orphaned_queue_rows(config: Config, session_id: str) -> list[str]:
    """Undrained queue rows the emit cursor ALREADY counts as delivered (techne#900).

    The third population, alongside superseded and below_threshold, and the one neither
    of those can see: these rows render fine. The thread is current and above threshold.
    The cursor was advanced when the batch was RENDERED and the answers were never
    ingested, so emit excludes them and reports count:0 for a session reading pending.

    DIAGNOSTIC ONLY. `emit_reflection_pass` now re-offers these once its batch comes out
    empty, so a nonzero count here means the self-heal has not run yet for the session —
    or that it regressed. Never drained here; #646's report-don't-sweep rule applies.
    """
    from techne.core.extension_state import get_state

    owed = _owed_thread_ids(config, session_id)
    if not owed:
        return []
    # A DECLINE IS NOT AN ORPHAN. Both are owed rows the cursor claims, but only the
    # orphan self-heals, and `orphaned`'s whole promise to the operator is that it clears
    # on the next emit. `declined_queue_rows` reports the other half.
    renderable = owed - set(unemittable_queue_rows(config, session_id))
    renderable -= _declined_keys(config, session_id)
    if not renderable:
        return []
    conn = connect(config.db_path)
    try:
        marker = get_state(conn, f"reflect:emitted:{session_id}")
    finally:
        conn.close()
    # A legacy/absent marker claims nothing: `_emitted_keys` rebuilds those from the
    # drained rows, so they cannot orphan anything by construction.
    if not marker or marker.get("key") != _CURSOR_KEY:
        return []
    return sorted(renderable & set(marker.get("emitted", [])))


def reconcile_reflection_queue(config: Config, *, apply: bool = False) -> dict:
    """Find (and optionally remove) queue rows left behind by a replaced detection.

    DRY BY DEFAULT. `apply=False` reports without writing, because this deletes rows
    from the ledger the SessionStart banner reads and the operator should see the
    split before anything moves.

    WHY THIS EXISTS SEPARATELY FROM THE FIX. `persist_detection` stops new superseded
    rows by retiring the old partition through the existing `ON DELETE CASCADE`, but
    it only runs when a session sees another detection. Sessions that already ended
    never will, so their residue needs an explicit pass.

    DELETES THE QUEUE ROW, NOT THE THREAD ROW. Dropping the stale `threads` row would
    reach the same rows via the CASCADE, but it also takes any DRAINED row with it —
    losing the record that a thread was reflected — and `threads.parent_thread_id`
    links point into that table. Narrowing to undrained queue rows keeps the blast
    radius to exactly the rows that are provably meaningless.

    REPORTS TWO MORE POPULATIONS AND REMOVES NEITHER (techne#900, #901 r1). Both hold
    rows that RENDER FINE, which is what makes them invisible to the first two — those
    classify rows that cannot render at all. They differ in what happens next, and the
    difference is the whole reason they are counted apart:
      - `orphaned` — ingest never saw the thread. The next emit re-offers it, so a
        nonzero count that does not clear is a regression signal, not a work list.
      - `declined` — ingest saw it and the agent left it blank. NO EMIT RE-OFFERS IT
        WHILE THE CURSOR CLAIMS IT, which is the only honest answer to a deliberate
        refusal; the debt stays owed and visible. Read that as scoped, not absolute
        (Prudence, #901 r3): a decline recorded against a thread the cursor does NOT
        claim — a marker holding `emitted: []` — is offered normally, because the
        exclusion it would need never applies. That is the safe direction, and
        `clear_declines` is the deliberate way back.

    Returns {sessions, superseded, below_threshold, orphaned, declined, removed,
    by_session}.
    """
    conn = connect(config.db_path)
    try:
        sessions = [
            r["session_id"]
            for r in conn.execute(
                "SELECT DISTINCT session_id FROM reflection_queue WHERE drained_at IS NULL"
            ).fetchall()
        ]
    finally:
        conn.close()

    by_session: dict[str, dict] = {}
    superseded_total = below_total = orphaned_total = declined_total = 0
    for sid in sessions:
        stale = superseded_queue_rows(config, sid)
        unemittable = unemittable_queue_rows(config, sid)
        below = [t for t in unemittable if t not in set(stale)]
        orphaned = orphaned_queue_rows(config, sid)
        declined = declined_queue_rows(config, sid)
        if not stale and not below and not orphaned and not declined:
            continue
        by_session[sid] = {
            "superseded": stale,
            "below_threshold": below,
            "orphaned": orphaned,
            "declined": declined,
        }
        superseded_total += len(stale)
        below_total += len(below)
        orphaned_total += len(orphaned)
        declined_total += len(declined)

    removed = 0
    if apply and superseded_total:
        conn = connect(config.db_path)
        try:
            for sid, rows in by_session.items():
                for tid in rows["superseded"]:
                    cur = conn.execute(
                        "DELETE FROM reflection_queue "
                        "WHERE thread_id = ? AND session_id = ? AND drained_at IS NULL",
                        (tid, sid),
                    )
                    removed += cur.rowcount or 0
            conn.commit()
        finally:
            conn.close()

    return {
        "sessions": len(by_session),
        "superseded": superseded_total,
        "below_threshold": below_total,
        "orphaned": orphaned_total,
        "declined": declined_total,
        "removed": removed,
        "by_session": by_session,
    }


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


def _cursor_key(item: dict) -> str:
    """The emit cursor's key for one batch item: the persisted thread id.

    A thread with no persisted row (`thread_id` is None) keys on `obs_key`, a hash of
    its observation ids (techne#954). This used to fall back straight to `prompt_hash`
    on the reasoning that such threads were rare "unsaved/legacy" cases. They were
    not: `techne import` persisted no threads at all, so an imported corpus was 100%
    fallback, and prompt_hash moves on every knowledge write — a backfill that
    promoted as it went was re-offered every thread it had already ingested. Import
    now persists threads; this key covers whatever still arrives unpersisted.

    `prompt_hash` remains the last resort, for an item with neither (in-memory
    observations without ids).
    """
    return item.get("thread_id") or item.get("obs_key") or item["prompt_hash"]


#: Cursor format flag: "thread_id" marks a post-#733 marker. Absent = a pre-#733 marker
#: whose entries are prompt_hashes and cannot be compared against anything stable.
#: Despite the name, `emitted` is not only thread ids: since techne#954 it also holds an
#: `obs-` key (`_observations_key`) for every RENDERED thread with no persisted row,
#: answered or not — it is written at render time. `answered` is the separate ledger of
#: the ones that came back, and the empty-batch heal is only meaningful because the two
#: differ: it re-offers `obs-` keys in `emitted` that `answered` does not carry.
_CURSOR_KEY = "thread_id"


def _emitted_keys(config: Config, session_id: str, marker: dict | None) -> set[str]:
    """The set of already-delivered thread keys for this session's emit cursor.

    WHY THE KEY CHANGED (techne#733). The cursor used to store `prompt_hash`, described
    as "the stable, always-present per-thread id". IT IS NOT STABLE. `_prompt_hash`
    hashes the RENDERED prompt, and `_build_prompt` embeds a freshly-retrieved "What the
    agent already knows" block — so the hash of an unchanged thread changes whenever
    LIVE KNOWLEDGE changes, which every reflect/promote/`techne_store` call does. The
    docstring scoped that hazard to `write_to='live'`; the real trigger is any write to
    the knowledge table from any session, which is continuous.

    The consequence is not a missed exclusion, it is an ACCUMULATING one: a drifted hash
    matches nothing, so the thread is re-emitted AND a second entry is appended.
    MEASURED: session 0ae96e10 held 51 entries for 23 renderable threads, 48 of which
    matched no current render; `remaining` had gone to -27; six sessions were affected.

    `thread_id` is the persisted FK — it is what `reflection_queue` is keyed on and what
    `drain_reflection_queue` stamps, so keying here on the same thing makes the cursor
    and the ledger COMPARABLE instead of incomparable. prompt_hash stays exactly where it
    belongs: candidate dedup (#236), which WANTS to notice a re-render.

    THIS PARAGRAPH SAID "agree by construction" UNTIL techne#900, AND THAT WAS THE NEXT
    TWO WEEKS' BUG. A shared key does not make two records agree; it makes disagreement
    detectable. Nothing detected it, so a cursor advanced on RENDER kept claiming threads
    whose queue rows were still owed. `emit_reflection_pass` now checks.

    A pre-#733 marker is not migrated in place — its contents are unrecoverable — so the
    delivered set is rebuilt from the authoritative record: queue rows already drained.
    A session with no queue rows at all (pre-#514) rebuilds to empty, which re-offers its
    threads once. That is the safe direction: re-reading a thread costs a prompt, while
    dropping one loses the session's memory silently.
    """
    if marker and marker.get("key") == _CURSOR_KEY:
        return set(marker.get("emitted", []))
    conn = connect(config.db_path)
    try:
        return {
            r["thread_id"]
            for r in conn.execute(
                "SELECT thread_id FROM reflection_queue "
                "WHERE session_id = ? AND drained_at IS NOT NULL",
                (session_id,),
            ).fetchall()
        }
    finally:
        conn.close()


def _owed_first(config: Config, session_id: str, batch: list[dict]) -> list[dict]:
    """Stable-sort the batch so threads with an UNDRAINED queue row come first.

    The char budget is spent in order, so ordering decides what a capped pass can even
    reach. Without this the budget goes to whatever sits earliest in thread order —
    which on a session with paid early threads means re-rendering work already done
    while the owed rows stay unreachable pass after pass (techne#733).
    """
    owed = _owed_thread_ids(config, session_id)
    if not owed:
        return batch
    return sorted(batch, key=lambda i: i.get("thread_id") not in owed)


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
    #: Every key this session can render RIGHT NOW. The cursor is reconciled against
    #: this before it is persisted — see the write below.
    current_ids = {_cursor_key(item) for item in batch}

    # Per-thread emit cursor (techne#269/#236): a session with more reflectable threads
    # than the emit cap must drain losslessly across passes — the #268 marker collapsed
    # the whole session to "emitted" on the first partial emit, so threads past the cap
    # were never reflected. Load the keys already delivered and exclude them, so each pass
    # takes the NEXT slice of unemitted threads: no overlap, no loss.
    #
    # THE CURSOR IS KEYED ON THE PERSISTED thread_id (see `_cursor_key`). The sentence
    # that stood here until #733 said it was keyed on prompt_hash, "the stable,
    # always-present per-thread id" — and TRUSTING THAT CLAIM IS WHAT COST TWO MONTHS.
    # It is false: `_prompt_hash` hashes the RENDERED prompt and `_build_prompt` embeds a
    # freshly-retrieved knowledge block, so the hash moves whenever anyone writes
    # knowledge. prompt_hash is still the right key for #236's candidate dedup, which
    # WANTS to notice a re-render; it was never right for a cursor.
    conn = connect(config.db_path)
    try:
        marker = get_state(conn, f"reflect:emitted:{session_id}")
    finally:
        conn.close()
    already = _emitted_keys(config, session_id, marker)
    # THE "STABILITY INVARIANT" THAT USED TO BE ASSERTED HERE WAS FALSE, and stating it
    # is more useful than deleting it. It claimed prompt_hash "must not shift between
    # cursor passes mid-drain" and that this "holds on the DEFAULT path
    # (write_to='candidates'): that path never mutates live knowledge". The scoping was
    # the error — the hash moves on ANY live-knowledge write from ANY session, which is
    # continuous, and the cursor keyed on it accumulated for two months (techne#733).
    # The cursor now keys on the persisted thread_id, so no stability claim about the
    # rendered prompt is load-bearing here any more.
    rendered = batch
    batch = [item for item in rendered if _cursor_key(item) not in already]

    # THE CURSOR DOES NOT OUTRANK THE LEDGER (techne#900). `delivered` below is set on
    # RENDER, not on ingest, so a pass that renders and is never ingested — the routine
    # case, since this tool's result regularly blows the MCP token cap and spills to a
    # file — marks its threads emitted forever while their queue rows stay undrained.
    #
    # Heal only where the cursor is provably wrong: an EMPTY batch is the cursor saying
    # "nothing left" about a session whose ledger still records owed rows that render
    # today. Those were rendered and never paid. Drop them and re-offer.
    #
    # Scoped to the empty case on purpose. A non-empty batch changes nothing, so the
    # multi-pass drain and the `emit -o file` / `ingest file` round trip — which
    # legitimately renders long before it ingests — behave exactly as before.
    if not batch:
        stale = already & _owed_thread_ids(config, session_id) & current_ids
        # AN UNPERSISTED THREAD HAS NO QUEUE ROW, so the line above can never see it
        # (techne#954, Prudence #955 r1). Its ledger is the marker's `answered` list,
        # written at ingest by `_claim_answered`. Without this a thread rendered and
        # never answered was dropped with every counter reading zero. Same scope as
        # the #900 heal: only on an empty batch, so paged `emit -o` drains still work.
        stale |= {k for k in already & current_ids if k.startswith("obs-")} - _answered_keys(marker)
        stale -= _declined_keys(config, session_id)
        if stale:
            already = already - stale
            batch = [item for item in rendered if _cursor_key(item) in stale]

    batch = _owed_first(config, session_id, batch)

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
    newly = [_cursor_key(item) for item in batch]
    # RECONCILE, DO NOT JUST APPEND (Prudence, #734 r1). Before this the cursor was only
    # ever read and appended to, and THAT is what made every remaining #733 symptom
    # permanent rather than transient:
    #   * A key that no longer renders — a drifted `prompt_hash` from the None-thread_id
    #     fallback, or a queue row rebuilt for a thread that is no longer emittable —
    #     stayed in the set forever, so the set grew without bound (measured: 1,2,3,4,5,6
    #     across six passes) and `len(emitted) > total` became permanent.
    #   * The rebuild path could itself WRITE an over-full marker in the new format, so
    #     it never took the rebuild path again and recorded `remaining: 0` while reading
    #     pending — internally incoherent.
    # Intersecting with what is renderable now makes the set describe REALITY at write
    # time. A dropped key is re-offered next pass, which costs a prompt; leaving it in
    # cost a cursor nobody could clear.
    emitted_ids = sorted((already | set(newly)) & current_ids)
    truncated = len(emitted_ids) < total
    # CLAMPED. A negative `remaining` is what an unstable cursor key produced for two
    # months, and it reaches the operator through the SessionStart banner (techne#733).
    remaining = max(0, total - len(emitted_ids))
    # A lone thread that alone exceeds the budget can't be delivered whole. Marking
    # the session emitted here would silently skip it — footgun 3, unchanged (Prudence,
    # #268). Detect it, leave the cursor UNADVANCED so the auto-trigger keeps surfacing
    # it, and flag the payload loudly.
    oversized = len(batch) == 1 and len(json.dumps(batch)) > max_chars
    # A COMPLETED-BUT-EMPTY PASS MUST STILL WRITE (Prudence, #734 r1). The old guard was
    # `bool(batch) and not oversized`, so once everything was emitted the marker could
    # never be rewritten — and a session carrying a legacy or over-full cursor then read
    # as PENDING on every pass with NO OPERATION ABLE TO CLEAR IT. Measured: five
    # consecutive passes, `pending=True` every time, `key` never leaving None. That turns
    # #733's silent drop into a banner that nags forever, which is the same defect wearing
    # the opposite sign — an uncleanable warning trains the operator to ignore the rail.
    # `oversized` STILL withholds the advance: that is #268 footgun 3, and a lone thread
    # too big to deliver must keep re-surfacing.
    delivered = not oversized

    # Advance the cursor: record the cumulative set of emitted thread ids plus
    # the session's total, so pending_reflection re-surfaces the session until every
    # thread is drained (techne#269) and re-emits skip already-delivered threads. This is
    # ALSO the exactly-once reflected-marker (techne#236 Gap 2) — one unified marker, not
    # two. We do NOT advance when nothing deliverable was produced.
    if delivered:
        # THE INVARIANT, ASSERTED AT THE WRITE POINT (Prudence, #734 r1). #733 survived
        # two months because this was only ever checked by arithmetic that failed open —
        # `len(emitted) < total` quietly answers "done" for the markers that are WORST.
        # The reconciliation above makes this unreachable by construction, which is
        # exactly why it belongs here: it cannot fire today, and it turns the next
        # regression into a loud failure instead of another silent two months.
        if len(emitted_ids) > total:
            raise AssertionError(
                f"reflect cursor invariant violated for {session_id}: "
                f"{len(emitted_ids)} emitted keys > {total} renderable threads"
            )
        conn = connect(config.db_path)
        try:
            set_state(
                conn,
                f"reflect:emitted:{session_id}",
                {
                    "key": _CURSOR_KEY,
                    #: CARRIED FORWARD, not recomputed. emit rewrites this marker
                    #: wholesale, so dropping the field erases every decline on the next
                    #: pass. NOT an infinite loop -- this comment said so until #901 r3,
                    #: and the teeth-check disproves it: the heal reads `declined` BEFORE
                    #: this write, so the drain loop still terminates and the mutation is
                    #: caught by a visibility assertion instead. The cost is "re-offered
                    #: once per drain run", which is milder and still wrong.
                    "declined": sorted(_declined_keys(config, session_id)),
                    #: CARRIED FORWARD for the same reason (techne#954): the ledger of
                    #: answered unpersisted threads, which the heal reads.
                    "answered": sorted(_answered_keys(_read_marker(config, session_id))),
                    #: The session's observation count AT WRITE TIME. `total` alone
                    #: cannot re-open a session that was judged empty and later grew,
                    #: because this marker is the only thing the nudge reads (#734 r2).
                    "observations": len(observations),
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
        # NAMES THE MEASURED CAUSE AND THE REMEDY (techne#795). This used to tell the
        # operator every stuck row was below the observation threshold, which is true
        # of only one of the two populations, and to attribute it to a cause techne#793
        # measured and refuted.
        result["unemittable_note"] = (
            f"{len(stuck)} queued thread(s) for this session cannot be drained by "
            "reflecting: either the thread is absent from the session's current "
            "detection (superseded — retire these with `techne reflect reconcile`), or "
            "it is still current but now below reflect.min_observations_per_thread "
            f"({config.reflect.min_observations_per_thread}), so no prompt can render "
            "for it. `techne reflect reconcile` prints the split, across all sessions. Do "
            "not delete them by "
            "hand: the below-threshold rows are the only visible evidence of "
            "tenishai/techne#646."
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
    obs_key: str | None = None,
) -> dict:
    """Ingest one answered prompt AND mark its reflection debt paid.

    A thread with no persisted row has no queue row to drain; its emit-batch `obs_key`
    is claimed in the cursor instead, so emit stops offering it (techne#954).

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
    if not result.get("parse_failures") and not result.get("errors"):
        if thread_id:
            result["reflection_debt_drained"] = drain_reflection_queue(config.db_path, [thread_id])
            _clear_decline(config, session_id, thread_id)
        elif obs_key:
            _claim_answered(config, session_id, obs_key)
    return result


def _claim_answered(config: Config, session_id: str, obs_key: str) -> None:
    """Record an answered UNPERSISTED thread in the emit cursor's `answered` ledger
    (techne#954).

    The persisted-thread equivalent is draining its queue row: emit claims a thread on
    render, and the empty-batch heal re-offers rendered threads that were never paid.
    For an unpersisted thread "paid" means listed here. Writes into an existing
    marker only, for the reason `_record_declines` gives (#901 r2): minting one here
    would read as a #268-era marker and hide the session. Emit writes the marker when
    it delivers, so a thread that was emitted has one.
    """
    from techne.core.extension_state import get_state, set_state

    if not session_id or not obs_key:
        return
    conn = connect(config.db_path)
    try:
        marker = get_state(conn, f"reflect:emitted:{session_id}")
        if not marker:
            log.debug("_claim_answered: no marker for %s, nothing to claim", session_id)
            return
        marker = dict(marker)
        marker["emitted"] = sorted(set(marker.get("emitted", [])) | {obs_key})
        marker["answered"] = sorted(set(marker.get("answered", [])) | {obs_key})
        marker["declined"] = [k for k in marker.get("declined", []) if k != obs_key]
        set_state(conn, f"reflect:emitted:{session_id}", marker)
    finally:
        conn.close()


def _answered_keys(marker: dict | None) -> set[str]:
    """`obs-` keys of answered unpersisted threads (techne#954). See `_claim_answered`."""
    return set((marker or {}).get("answered", []))


def _read_marker(config: Config, session_id: str) -> dict | None:
    from techne.core.extension_state import get_state

    conn = connect(config.db_path)
    try:
        return get_state(conn, f"reflect:emitted:{session_id}")
    finally:
        conn.close()


def ingest_reflection_batch(config: Config, items: list[dict]) -> dict:
    """Ingest a whole emit batch the agent has filled in. Returns summed counts.

    The file-round-trip half of the no-API-key path: `techne reflect emit -o f`,
    the agent fills each item's `raw_response`, `techne reflect ingest f`. An item
    whose raw_response is blank is a deliberately unanswered thread, not an error —
    it is skipped WITHOUT draining, so the debt stays owed.

    THE DECLINE IS NOW RECORDED, AND IT HAS TO BE (Prudence, #901 r1). #900's heal
    re-offers an owed row the cursor claims, which is exactly what a decline leaves
    behind — so without this the documented drain loop never terminated. Recording it
    keeps the debt owed and visible (`declined_queue_rows`, `reflect reconcile`) while
    stopping the re-offer. Answering the thread later retires the record.
    """
    totals = new_ingest_counts()
    totals["skipped"] = 0
    totals["drained"] = 0
    declined: dict[str, list[str]] = {}
    for it in items:
        raw = it.get("raw_response")
        if not isinstance(raw, str):
            raw = json.dumps(raw) if raw else ""  # tolerate an inlined array
        if not raw.strip():  # noqa: E501 - blank means the agent declined this thread
            totals["skipped"] += 1
            # An unpersisted thread's decline is recorded by its obs_key (techne#954),
            # so emit excludes it and `undecline` can reach it.
            declined.setdefault(it.get("session_id", ""), []).append(
                it.get("thread_id") or it.get("obs_key") or ""
            )
            continue
        counts = ingest_one_and_drain(
            config,
            it.get("session_id", ""),
            it.get("thread_id", "") or "",
            it.get("prompt_hash", ""),
            raw,
            model_used=it.get("model_used", "agent-assisted"),
            obs_key=it.get("obs_key"),
        )
        totals["drained"] += counts.pop("reflection_debt_drained", 0)
        for k, v in counts.items():
            totals[k] += v
    for sid, tids in declined.items():
        _record_declines(config, sid, [t for t in tids if t])
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
                    config.db_path,
                    c,
                    source=f"reflect:{thread_id or 'unknown'}",
                    session_id=session_id,
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

    `generate_fn` is an injectable LLM callable for tests; in prod it is built from
    `config.reflect.model`, and an empty `reflect.model` means THIS PASS DOES NOT RUN.
    """
    result = {"threads_considered": len(threads), "threads_reflected": 0, **new_ingest_counts()}
    if not config.reflect.enabled:
        return result

    # THE AUTOMATIC PASS REQUIRES AN EXPLICIT BINDING, AND DOES NOT INHERIT
    # `config.model` (techne#555). This used to fall back to the session-wide model,
    # which is how the rail came to be bound to a credential plane the operator does
    # not hold: `config.model` was an Anthropic id, the operator held no Console API
    # key, every session-end pass raised, `reflect_on_session` counted the error and
    # returned, and the rail produced zero candidates from 2026-06-20 onward while
    # `check_maintenance_rails` read LIVE off the 450 the RESIDENT route had written.
    # `resolve_generate_fn` already refuses this fallback for the five custodian rails
    # and names this rail as the reason; reflect has its own runner and was left
    # inheriting. It no longer does.
    #
    # UNBOUND IS A CORRECT AND COMMON POSTURE, NOT A FAULT. `reflect.enabled` also
    # gates the SessionStart nudge — NOT the reflection-debt queue (techne#794); the
    # session_end enqueue in lifecycle.py sits outside that flag, deliberately, so
    # the ledger survives the feature being switched off and re-enabling later does
    # not have amnesia. The nudge is what drives the resident emit→ingest route —
    # the route that actually produces candidates on an instance with no headless
    # binding. So enabled-and-unbound must leave that route
    # working and simply not attempt an LLM call here, rather than failing loudly or
    # forcing the operator to disable reflection wholesale to silence it.
    #
    # An injected `generate_fn` IS a binding — the caller supplied the callable, so
    # there is nothing left to resolve.
    if generate_fn is None and not config.reflect.model:
        # Retract any marker from when this rail WAS bound.
        #
        # THE REASON LIVES IN `custodian.clear_rail_marker`'s DOCSTRING, NOT HERE
        # (techne#735). This comment used to say a stale `error` payload "would
        # otherwise report a run that can no longer happen as the rail's current
        # state" — which stopped being true when `check_model_bound_rails` started
        # asking `_rail_binding` first: unbound reads DORMANT off the BINDING, marker
        # or no marker. The retraction is still correct, on a different reason (what
        # happens on RE-BIND), and that reason is written down in exactly one place so
        # it cannot drift. Read it there before concluding this call can go.
        _clear_reflect_marker(config)
        return result

    # One LLM client for the whole pass. `reflect.model` is a full override of the
    # session-wide model rather than a tweak of it — see above.
    reflect_config = config
    if config.reflect.model:
        # Shallow override of config.model for the generate fn only
        import copy

        reflect_config = copy.copy(config)
        reflect_config.model = config.reflect.model
    if generate_fn is None:
        try:
            generate_fn = _make_generate_fn(reflect_config)
        except Exception as e:
            log.exception(
                "reflect_on_session: could not build generate_fn (session=%s)", session_id
            )
            result["errors"] += 1
            _record_reflect_marker(
                config, outcome="error", detail=f"could not build generate_fn: {_why(e)}"
            )
            return result

    from techne.core.thread_orchestrator import persisted_thread_id

    obs_by_thread = _bucket_observations(threads, observations)

    #: First LLM failure reason, kept so the marker can say WHY rather than just that
    #: the count was non-zero. `result["errors"]` was already being incremented before
    #: techne#555; a number nobody reads is what let this rail fail for two months.
    first_error = ""

    for t in threads:
        t_obs = obs_by_thread.get(t.thread_num, [])
        if len(t_obs) < config.reflect.min_observations_per_thread:
            continue

        prompt = _build_prompt(t, t_obs, config.db_path, config)
        prompt_hash = _prompt_hash(prompt)

        try:
            raw = generate_fn(prompt)
        except Exception as e:
            log.exception(
                "reflect_on_session: LLM call failed (session=%s thread_num=%s)",
                session_id,
                t.thread_num,
            )
            result["errors"] += 1
            if not first_error:
                first_error = _why(e)
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

    # RECORD WHAT ACTUALLY HAPPENED (techne#555). Before this, an automatic reflect
    # pass that reached zero threads and one that failed on every LLM call were
    # indistinguishable from outside: both returned, both logged, both left
    # `check_maintenance_rails` reporting GREEN off candidates the RESIDENT route had
    # written. Measured on the operator's store: this rail produced 0 candidates in its
    # entire history while reading healthy.
    if result["errors"] and result["threads_reflected"] == 0:
        _record_reflect_marker(
            config,
            outcome="error",
            detail=f"{result['errors']} error(s), 0 threads reflected: {first_error or 'see log'}",
        )
    elif result["errors"]:
        _record_reflect_marker(
            config,
            outcome="partial",
            detail=f"{result['threads_reflected']} thread(s) reflected, "
            f"{result['errors']} error(s): {first_error or 'see log'}",
        )
    elif result["threads_reflected"]:
        _record_reflect_marker(
            config, outcome="ok", detail=f"{result['threads_reflected']} thread(s) reflected"
        )
    else:
        _record_reflect_marker(config, outcome="noop:no-eligible-threads")

    return result


def _why(e: Exception) -> str:
    """One-line reason for a marker. Type plus message, truncated.

    The TYPE matters as much as the text: `AuthenticationError` tells the operator this
    is a credential problem in one word, which is the difference between "the rail is
    broken" and "the rail needs a key it was never given".
    """
    msg = str(e).strip().splitlines()[0] if str(e).strip() else ""
    return f"{type(e).__name__}: {msg}"[:300]


def _clear_reflect_marker(config) -> None:
    """Drop this rail's marker when the automatic pass is unbound. Never raises."""
    try:
        from techne.core.custodian import clear_rail_marker

        clear_rail_marker(config, "reflect")
    except Exception:  # noqa: BLE001
        log.warning("reflect: could not clear liveness marker", exc_info=True)


def _record_reflect_marker(config, *, outcome: str, detail: str = "") -> None:
    """Record the automatic rail's outcome. Best-effort; never raises into teardown."""
    try:
        from techne.core.custodian import record_rail_marker

        record_rail_marker(config, "reflect", outcome=outcome, detail=detail)
    except Exception:  # noqa: BLE001
        log.warning("reflect: could not record liveness marker", exc_info=True)


def _is_real_session(session_id: str | None) -> bool:
    """False for the placeholder the session-less passes file candidates under.

    Consolidate merges, dream adds and every conflict supersede are written with
    `SYSTEM_SLEEP_SESSION`, a sentinel that satisfies the candidates FK. Stamping it
    into `knowledge.origin_session` would record a merged fact, or a team store's
    fact, as originating in a local session (Prudence #961 r1). Those rows stay NULL:
    unattributed rather than guessed.
    """
    from techne.core.sleepdream import SYSTEM_SLEEP_SESSION

    return bool(session_id) and session_id != SYSTEM_SLEEP_SESSION


def _stamp_origin_session(conn, knowledge_id: int, session_id: str) -> None:
    """Record which session a promoted knowledge row came from (techne#949).

    Before this, promotion left `knowledge.origin_session` NULL and the session was
    reachable only through `knowledge_candidates.promoted_to_id` or by parsing
    `source`. Never overwrites a value already set. Runs on the caller's connection,
    inside promote's transaction, so the row cannot commit without it.
    """
    conn.execute(
        "UPDATE knowledge SET origin_session = ? WHERE id = ? AND origin_session IS NULL",
        (session_id, knowledge_id),
    )


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
            # last_confirmed_at is set HERE (techne#938). This is promote_candidate's
            # "add" dispatch -- the main way curated knowledge grows -- and the
            # CREATE TABLE no longer carries a default to fall back on.
            """INSERT INTO knowledge
                 (type, content, source, tags, confidence, confidence_numeric,
                  last_confirmed_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
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


#: Actions whose applier mints a NEW knowledge row, so the row originated in the
#: candidate's session (techne#949). `retire` returns an existing survivor, which did
#: not, and must not be stamped.
_ROW_MINTING_ACTIONS = frozenset({"add", "supersede", "merge"})

#: Actions a reviewer may revise with `--content` (techne#952). Add-only: a merge or
#: supersede carries a faithfulness verdict computed on its original content, and
#: revising it would leave that verdict describing text that is no longer applied.
#: Resetting the verdict instead would make revision a way around the gate.
_REVISABLE_ACTIONS = frozenset({"add"})


def promote_candidate(
    db_path: Path,
    candidate_id: int,
    *,
    decided_by: str | None = None,
    decision_reason: str | None = None,
    force: bool = False,
    revised_content: str | None = None,
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

    `revised_content` (techne#952) promotes a reviewer's edit instead of the proposed
    text: for a durable claim wrapped in details that will go stale, where discard
    loses the claim and promote keeps the wrapper. Stored on the candidate's
    `revised_content`; `content` is left as proposed. Add-only, and requires a
    `decision_reason` — an unexplained edit to memory cannot be audited.

    Row-minting actions stamp `knowledge.origin_session` with the candidate's
    session in the same transaction (techne#949).
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

        if revised_content is not None:
            if action not in _REVISABLE_ACTIONS:
                raise ValueError(
                    f"Candidate {candidate_id} is a {action!r}; only 'add' candidates can "
                    "be revised (techne#952). Promote or discard it as proposed."
                )
            if not revised_content.strip():
                raise ValueError(f"Candidate {candidate_id}: revised content is empty")
            if not (decision_reason or "").strip():
                raise ValueError(
                    f"Candidate {candidate_id}: a revision needs a decision_reason "
                    "saying what was changed and why"
                )

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
                      decided_by = ?, decision_reason = ?, revised_content = ?
                WHERE id = ? AND status = 'pending'""",
            (
                decided_by,
                decision_reason,
                # Redacted like every other text written to this table (Prudence #961
                # r1); `reflect review` echoes it back.
                redact_credentials(revised_content) if revised_content is not None else None,
                candidate_id,
            ),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"Candidate {candidate_id} already {row['status']} — cannot re-promote"
            )

        # Appliers read row["content"]; hand them the revision in its place so no
        # applier needs to know revision exists.
        applied = row
        if revised_content is not None:
            applied = dict(row)
            applied["content"] = revised_content
            log.debug("promote_candidate: candidate %s promoted with a revision", candidate_id)

        # Apply on the SAME connection so a failure here rolls back the status flip.
        promoted_to_id = applier(conn, applied, db_path)

        conn.execute(
            "UPDATE knowledge_candidates SET promoted_to_id = ? WHERE id = ?",
            (promoted_to_id, candidate_id),
        )
        if (
            promoted_to_id is not None
            and row["scope"] == "knowledge"
            and action in _ROW_MINTING_ACTIONS
            and _is_real_session(row["source_session_id"])
        ):
            _stamp_origin_session(conn, promoted_to_id, row["source_session_id"])
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
        # Promoted with a reviewer's edit (techne#952). The revise rate is the
        # measure for prompt changes like #950: fewer revisions means fewer
        # candidates arriving with details that go stale.
        revised = conn.execute(
            "SELECT COUNT(*) FROM knowledge_candidates "
            "WHERE status = 'promoted' AND revised_content IS NOT NULL"
        ).fetchone()[0]
        return {
            "pending": counts.get("pending", 0),
            "promoted": counts.get("promoted", 0),
            "revised": revised,
            "discarded": counts.get("discarded", 0),
            "by_scope": {f"{s[0]}.{s[1]}": n for s, n in by_scope.items()},
        }
    finally:
        conn.close()
