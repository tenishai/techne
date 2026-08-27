"""Thread detection and management.

Ported from thread_summarizer.py. Detects distinct topic threads within a
session's observations and generates per-thread summaries. Threads are the
unit of continuity — they link across sessions to form a chain of work.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from techne._log import get_logger
from techne.config import Config
from techne.core.redaction import redact_credentials

log = get_logger(__name__)

MAX_SAMPLED_OBSERVATIONS = 60


@dataclass
class Thread:
    thread_num: int
    label: str
    summary: str
    resume_points: str = ""
    observation_indices: list[int] = field(default_factory=list)
    # Set by heuristic detector.  When present, save_threads derives the row ID
    # from bucket_hash(session_id, first_obs_id, boundary_signal).  None means
    # fall back to the legacy "{session_id}-{thread_num}" format.
    #
    # THE VERB NO LONGER DEPENDS ON THIS FIELD (techne#797).  save_threads used
    # INSERT OR IGNORE here and INSERT OR REPLACE for the legacy form; both were
    # only correct while the caller deleted the row first.  It is now one upsert
    # for both, so replay stays idempotent AND a re-detected label/summary
    # actually lands.
    boundary_signal: str | None = None


def detect_and_summarize(
    observations: list[dict],
    config: Config,
    generate_fn=None,
) -> list[Thread]:
    """Detect topic threads in observations and summarize each.

    Args:
        observations: List of observation dicts with tool_name, content, timestamp.
        config: techne config (for min_observations threshold and model).
        generate_fn: Optional override for LLM generation (for testing).
                     Signature: fn(prompt: str) -> str

    Returns list of Thread objects. Single-topic sessions return one Thread.

    This is the backward-compatible entry point. Callers that also want the
    heuristic detector's per-pair boundary scores (e.g. to persist them) should
    call :func:`detect_summarize_with_metadata` instead.
    """
    threads, _scores = detect_summarize_with_metadata(observations, config, generate_fn)
    return threads


def detect_summarize_with_metadata(
    observations: list[dict],
    config: Config,
    generate_fn=None,
):
    """Detect threads and also return the heuristic detector's boundary scores.

    Returns ``(threads, scores)``. ``scores`` is empty when the LLM detector
    is used. Callers that want to persist score breakdowns (lifecycle hook,
    backfill jobs, audit tooling) should branch on a non-empty ``scores`` list.
    """
    if not observations:
        return [], []

    if config.threads.detector == "heuristic":
        return _detect_summarize_heuristic(observations, config, generate_fn)
    return _detect_summarize_llm(observations, config, generate_fn), []


def _boundary_signal_from_scores(
    first_obs_idx: int,
    scores_by_a: dict[int, object],
    thread_num: int,
) -> str:
    """Derive a stable boundary_signal string for a segment.

    The first segment in a session always gets ``"new_session"``.  For later
    segments the signal is built from the dominant key(s) in the BoundaryScore
    that triggered the split (the score whose ``a_index`` equals the *last*
    observation index of the preceding segment, i.e. ``first_obs_idx - 1``).

    The string is deterministic: same inputs → same string.
    """
    if thread_num == 1:
        return "new_session"

    boundary_score = scores_by_a.get(first_obs_idx - 1)
    if boundary_score is None:
        return f"thread:{thread_num}"

    sigs = boundary_score.signals  # type: ignore[attr-defined]
    if not sigs:
        return f"thread:{thread_num}"

    # Sort for stability, then take the dominant signal name(s).
    ranked = sorted(sigs.items(), key=lambda kv: kv[1], reverse=True)
    top_score = ranked[0][1]
    # Include all signals tied at the top.
    top_keys = sorted(k for k, v in ranked if v == top_score)
    gap_s = boundary_score.gap_seconds  # type: ignore[attr-defined]

    # Produce a human-readable, stable signal string.
    signal_key = top_keys[0]
    if signal_key.startswith("gap_"):
        secs = int(round(gap_s / 60) * 60)  # round to nearest minute
        return f"long_gap:{secs}s"
    if signal_key == "repo_change":
        return "repo_change"
    if signal_key == "family_change":
        return "family_change"
    return signal_key


def _detect_summarize_heuristic(
    observations: list[dict],
    config: Config,
    generate_fn=None,
):
    """Heuristic detector + heuristic (or LLM) summarizer. Always returns
    boundary scores alongside threads."""
    from techne.core import heuristic_threads as ht

    extra_patterns = []
    for pattern in config.threads.entity_patterns:
        try:
            extra_patterns.append(re.compile(pattern))
        except re.error as e:
            log.warning(
                "entity_pattern_invalid",
                pattern=pattern,
                error=str(e),
                error_type=type(e).__name__,
            )

    user_families = config.threads.tool_families or None
    extras = extra_patterns or None

    # Build features ONCE and reuse them for detection, labels, and fallback
    # summaries. find_repo's filesystem walks dominate extraction, so a shared
    # per-pass repo_cache collapses the many repeated path probes within a
    # session; passing features into detect_threads avoids a second full pass
    # (previously features were extracted twice per observation per run).
    repo_cache: dict[str, str | None] = {}
    features = [
        ht.extract_features(
            i,
            obs,
            user_families=user_families,
            extra_entity_patterns=extras,
            repo_cache=repo_cache,
        )
        for i, obs in enumerate(observations)
    ]

    segments, scores = ht.detect_threads(
        observations,
        user_families=user_families,
        extra_entity_patterns=extras,
        boundary_threshold=config.heuristic.boundary_threshold,
        user_prompt_score=config.heuristic.user_prompt_score,
        features=features,
    )

    use_llm_summary = config.threads.summarizer == "llm"
    if use_llm_summary and generate_fn is None:
        generate_fn = _make_generate_fn(config)

    # Index boundary scores by a_index for O(1) lookup in signal derivation.
    scores_by_a = {s.a_index: s for s in scores}

    threads: list[Thread] = []
    for thread_num, segment in enumerate(segments, 1):
        seg_observations = [observations[i] for i in segment]
        seg_features = [features[i] for i in segment]
        label = ht.generate_label(segment, features)
        resume = ht.build_resume_point(seg_observations)

        boundary_signal = _boundary_signal_from_scores(segment[0], scores_by_a, thread_num)

        summary = ht.fallback_summary(seg_observations, seg_features)
        if use_llm_summary:
            try:
                summary = _summarize_thread_llm(seg_observations, label, generate_fn)
            except Exception as e:
                log.warning("thread_summary_llm_failed", error=str(e), error_type=type(e).__name__)

        threads.append(
            Thread(
                thread_num=thread_num,
                label=label,
                summary=summary,
                resume_points=resume,
                observation_indices=segment,
                boundary_signal=boundary_signal,
            )
        )

    return threads, scores


def _summarize_thread_llm(observations: list[dict], label: str, generate_fn) -> str:
    """One LLM call to summarize a single already-detected thread."""
    lines = []
    for i, obs in enumerate(observations[:50]):
        tool = obs.get("tool_name", "?")
        content = (obs.get("content", "") or "")[:200]
        lines.append(f"[{i}] [{tool}] {content}")
    context = "\n".join(lines)
    prompt = (
        f"Summarize this thread in 2-3 sentences:\n"
        f"- What was accomplished or concluded\n"
        f"- Key files/resources involved\n"
        f'- Any unfinished work (or "Complete." if nothing pending)\n\n'
        f"Thread label: {label}\n"
        f"Observations ({len(observations)}):\n{context}\n"
    )
    return (generate_fn(prompt) or "").strip()


def _detect_summarize_llm(
    observations: list[dict],
    config: Config,
    generate_fn=None,
) -> list[Thread]:
    """Original LLM-driven detection + summarization. Unchanged behavior."""
    if generate_fn is None:
        generate_fn = _make_generate_fn(config)

    if len(observations) < config.threads.min_observations:
        summary = _summarize_single(observations, generate_fn)
        return [Thread(thread_num=1, label="session", summary=summary)]

    if len(observations) > MAX_SAMPLED_OBSERVATIONS:
        step = len(observations) / MAX_SAMPLED_OBSERVATIONS
        indices = [int(i * step) for i in range(MAX_SAMPLED_OBSERVATIONS)]
        sampled = [(i, observations[i]) for i in indices]
    else:
        sampled = list(enumerate(observations))

    context_lines = []
    for idx, obs in sampled:
        tool = obs.get("tool_name", "unknown")
        content = obs.get("content", "")[:250]
        ts = obs.get("timestamp", "")[:19]
        context_lines.append(f"[{idx}] [{ts}] [{tool}] {content}")

    context = "\n\n".join(context_lines)

    prompt = f"""Analyze these session observations and identify distinct HIGH-LEVEL topic threads.
A "thread" is a major investigation, project, or piece of work — NOT individual \
tool calls or sub-tasks.
Group related refactors, edits, and fixes into the SAME thread if they serve the same goal.
Most sessions have 1-4 threads. If you find more than 5, you are over-segmenting — \
merge related threads.

For each thread, provide:
1. A short label (3-8 words)
2. Which observation indices belong to it (use only the shown index numbers, comma-separated)
3. A 2-3 sentence summary of what was accomplished
4. Resume points (only if there's unfinished work)

Format your response EXACTLY like this (one section per thread):

## Thread 1: <label>
Observations: 0,1,2,3,5,6
Summary: <2-3 sentences>
Resume: <bullet points, or "None" if thread is complete>

## Thread 2: <label>
Observations: 4,7,8,9,10
Summary: <2-3 sentences>
Resume: <bullet points, or "None">

Rules:
- Every observation must belong to exactly one thread
- Order threads chronologically by when they start
- If there's genuinely only one topic, return a single Thread 1
- Focus on WHAT was accomplished and concluded, not the tool mechanics
- Be specific: include file paths, resource names, service names from the observations
- If a topic was investigated and resolved, state the conclusion — don't reframe it as open work

Session observations ({len(observations)} total, {len(sampled)} shown):
{context}"""

    try:
        response = generate_fn(prompt)
        if response:
            return _parse_threads(response)
    except Exception as e:
        log.warning("thread_inference_llm_failed", error=str(e), error_type=type(e).__name__)

    summary = _summarize_single(observations, generate_fn)
    return [Thread(thread_num=1, label="session", summary=summary)]


def link_threads(
    new_threads: list[Thread],
    session_id: str,
    db_path: Path,
    config: Config,
) -> list[tuple[str, str | None]]:
    """Link new threads to semantically similar threads from recent sessions.

    Returns list of (thread_id, parent_thread_id) tuples.
    """
    from techne.core.search import search_fts

    links = []
    for t in new_threads:
        # Key the link by the SAME id save_threads writes/looks up (thread_row_id):
        # heuristic threads are stored as `t-<hash>`, so the legacy `{session}-{num}`
        # key silently missed in save_threads' link_map → parent linkage was lost for
        # every heuristic (default-detector) session (techne#165).
        thread_id = thread_row_id(session_id, t)
        parent_id = None

        results = search_fts(
            db_path,
            t.label + " " + t.summary[:200],
            table="threads",
            limit=5,
        )

        for r in results:
            candidate_id = r.get("id", "")
            # Skip same-session candidates by session_id, NOT an id-prefix check: a
            # heuristic id is `t-<hash>` and does NOT start with the session id, so the
            # old prefix guard let a thread link to its OWN prior row (e.g. redetect_threads
            # links before the DELETE) — a self-referential parent. Unmasked now that the
            # link keys agree (techne#165). The exact session match covers both id forms.
            if r.get("session_id") == session_id:
                continue
            candidate_status = r.get("status", "")
            if candidate_status == "stale":
                continue
            # BM25 is "lower is better"; convert to a 0-1 similarity score.
            # rank=0 means "no score computed" (unindexed row), not a perfect
            # match — treat as zero similarity so it never auto-links.
            bm25 = r.get("rank", 0)
            similarity = 1.0 / (1.0 + abs(bm25)) if bm25 else 0.0
            if similarity < config.threads.link_threshold:
                continue  # not similar enough to link
            parent_id = candidate_id
            break

        links.append((thread_id, parent_id))

    return links


def thread_row_id(session_id: str, t: Thread) -> str:
    """The threads.id for a Thread — the SINGLE source of truth.

    Any code referencing a thread by FK (knowledge_candidates.source_thread_id,
    reflect's emit/ingest) must derive the id through here, or it drifts from
    what save_threads actually wrote. Heuristic-detected threads (carrying a
    boundary_signal) are content-addressed via bucket_hash; others use the
    legacy "{session_id}-{thread_num}" format.
    """
    from techne.core.threads.persistence import bucket_hash

    if t.boundary_signal is not None and t.observation_indices:
        return bucket_hash(session_id, t.observation_indices[0], t.boundary_signal)
    return f"{session_id}-{t.thread_num}"


def persisted_thread_id(db_path, session_id: str, t: Thread, *, conn=None) -> str | None:
    """Resolve the ACTUAL `threads.id` for a Thread, preferring the persisted row
    over a recomputed hash (techne#106).

    `thread_row_id` is a pure function of the Thread's *current* fields, so it
    drifts when a thread is re-detected at reflection time with a different
    `boundary_signal` than it was saved with (legacy/pre-heuristic sessions were
    saved as `{session}-{num}` but reload as a content-addressed `t-<hash>`). That
    drift made the candidates FK reject the row. Here we trust the database: use
    the computed id only if it actually exists; else fall back to the natural key
    `(session_id, thread_num)`; return None if no row is persisted (the caller
    then stores a NULL FK rather than losing the candidate).

    Takes an optional open *conn*, the same `own_conn` idiom `save_threads` and
    `enqueue_reflection_debt` already use. Without it this was the ONE link in
    that chain that always opened its own connection, so a caller mid-transaction
    could not see its own uncommitted rows and every thread read as unpersisted —
    which is what forced `redetect_threads` to enqueue after its commit (#527 r5).
    """
    from techne.core.database import connect

    computed = thread_row_id(session_id, t)
    own_conn = conn is None
    c = connect(db_path) if own_conn else conn
    try:
        if c.execute("SELECT 1 FROM threads WHERE id = ?", (computed,)).fetchone():
            return computed
        row = c.execute(
            "SELECT id FROM threads WHERE session_id = ? AND thread_num = ?",
            (session_id, t.thread_num),
        ).fetchone()
        return row[0] if row else None
    finally:
        if own_conn:
            c.close()


def save_threads(
    threads: list[Thread],
    session_id: str,
    db_path: Path,
    parent_links: list[tuple[str, str | None]] | None = None,
    conn=None,
):
    """Persist threads to the database.

    If `conn` is provided the caller owns the transaction (no commit/close
    here). Pass a connection to make the prune + insert atomic when
    `persist_detection` replaces a detection.

    When a Thread carries a ``boundary_signal`` (set by the heuristic
    detector) the row ID is derived from
    ``bucket_hash(session_id, first_obs_id, boundary_signal)``.  Threads without
    one fall back to the legacy ``"{session_id}-{thread_num}"`` format.

    THE VERB DOES NOT DEPEND ON THAT FIELD (techne#797).  This used to say the
    boundary_signal path wrote ``INSERT OR IGNORE`` and the legacy path
    ``INSERT OR REPLACE``; both are retired.  It is one
    ``ON CONFLICT(id) DO UPDATE`` upsert for both, so replaying a session is
    still a no-op for CONTENT that has not changed, while a re-detected
    label/summary actually lands.  See the comment on the statement itself for
    why each half of that matters.
    """
    from techne.core.database import connect

    link_map = {}
    if parent_links:
        link_map = {tid: pid for tid, pid in parent_links}

    owned = conn is None
    if owned:
        conn = connect(db_path)
    try:
        for t in threads:
            thread_id = thread_row_id(session_id, t)
            # UPSERT, NOT IGNORE-OR-REPLACE (techne#797). Both old forms were only
            # correct while the caller deleted the row first, which persist_detection
            # no longer does — it spares threads that are still current.
            #
            # `INSERT OR IGNORE` then no-ops on a stable id, so `label`, `summary` and
            # `resume_points` froze at whatever the FIRST detection wrote. A thread's id
            # is bucket_hash(session_id, observation_indices[0], boundary_signal), so it
            # is stable exactly when the partition is stable — every turn after the first
            # for a single-thread session, which is 440 of 538 sessions on the operator's
            # store. Nothing else refreshes these columns (the only other UPDATE on
            # threads sets `status`), so `techne_resume` and `threads_fts` would have
            # served the opening minutes of a session forever, silently.
            #
            # `INSERT OR REPLACE` had the mirror defect: it is DELETE+INSERT, and with
            # `recursive_triggers` off the implicit delete does NOT fire `threads_ad`, so
            # the FTS shadow kept indexing the old label. DO UPDATE fires `threads_au`,
            # which deletes and re-inserts the FTS row properly, and leaves the rowid
            # stable so nothing else has to move.
            #
            # parent_thread_id IS COALESCED, NOT OVERWRITTEN (Prudence, #798 r2). It comes
            # from `link_map.get(thread_id)`, which is None whenever `link_threads` did not
            # re-derive a parent this turn — and a bare `excluded.parent_thread_id` then
            # writes that None over a link already in the row. `INSERT OR IGNORE` could
            # not do this, because it never updated. The trigger is a CLOCK, not a race:
            # `link_threads` skips candidates with status='stale', and `age_threads` (which
            # run_session_end calls every turn) walks active -> completed -> stale, so ~32
            # days after a parent's session GOES QUIET it stops being re-derivable and
            # the child's link is silently erased. (Not 32 days after the link is
            # written: this upsert refreshes updated_at on every re-save, so a parent
            # in a still-live session stays `active` and the erasure is deferred —
            # Prudence, #798 r3 N3. The common case is a parent in an ended session,
            # where the arithmetic holds.) There is no integrity reason to ever clear this
            # column: it has an FK, and persist_detection's guard 1 is what protects it.
            sql = (
                "INSERT INTO threads"
                " (id, session_id, thread_num, label, summary, resume_points,"
                "  status, parent_thread_id, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, CURRENT_TIMESTAMP)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   thread_num = excluded.thread_num,"
                "   label = excluded.label,"
                "   summary = excluded.summary,"
                "   resume_points = excluded.resume_points,"
                "   parent_thread_id = COALESCE("
                "     excluded.parent_thread_id, threads.parent_thread_id),"
                "   updated_at = CURRENT_TIMESTAMP"
            )
            parent_id = link_map.get(thread_id)
            conn.execute(
                sql,
                (
                    thread_id,
                    session_id,
                    t.thread_num,
                    redact_credentials(t.label),
                    redact_credentials(t.summary),
                    redact_credentials(t.resume_points) if t.resume_points else None,
                    parent_id,
                ),
            )
        if owned:
            conn.commit()
    finally:
        if owned:
            conn.close()


def age_threads(db_path: Path, config: Config) -> dict[str, int]:
    """Age threads based on activity. Returns counts of status transitions."""
    from techne.core.database import connect

    conn = connect(db_path)
    counts = {"completed": 0, "stale": 0}
    try:
        # Active → completed: no activity for complete_after_hours.
        # (#72) The old `resume_points IS NULL OR = ''` gate never fired — the
        # heuristic detector populates resume_points on every thread, so it
        # pinned everything to 'active' forever and starved the stale step too.
        # Completing only flips status; the resume_points text is preserved and
        # the thread stays searchable — it just leaves the active working set.
        cur = conn.execute(
            """UPDATE threads SET status = 'completed', updated_at = CURRENT_TIMESTAMP
               WHERE status = 'active'
               AND updated_at < datetime('now', ? || ' hours')""",
            (f"-{config.threads.complete_after_hours}",),
        )
        counts["completed"] = cur.rowcount

        # Completed → stale: no activity in stale_after_days
        cur = conn.execute(
            """UPDATE threads SET status = 'stale', updated_at = CURRENT_TIMESTAMP
               WHERE status = 'completed'
               AND updated_at < datetime('now', ? || ' days')""",
            (f"-{config.threads.stale_after_days}",),
        )
        counts["stale"] = cur.rowcount

        conn.commit()
    finally:
        conn.close()

    return counts


def format_combined_summary(threads: list[Thread]) -> str:
    """Format all threads into a single combined summary string."""
    if len(threads) == 1:
        parts = [threads[0].summary]
        if threads[0].resume_points:
            parts.append(f"\n## Resume Points\n\n{threads[0].resume_points}")
        return "\n".join(parts)

    parts = []
    all_resume = []
    for t in threads:
        parts.append(f"**Thread {t.thread_num}: {t.label}** — {t.summary}")
        if t.resume_points:
            all_resume.append(f"### {t.label}\n{t.resume_points}")

    combined = "\n\n".join(parts)
    if all_resume:
        combined += "\n\n## Resume Points\n\n" + "\n\n".join(all_resume)
    return combined


def _make_generate_fn(config: Config):
    """Create an LLM generation function from config.

    Uses LiteLLM, which provides a unified interface to 140+ providers.
    The config.model field can be any LiteLLM model identifier:
      - "claude-haiku-4-5-20251001" (Anthropic direct, needs ANTHROPIC_API_KEY env)
      - "openai/gpt-4" (OpenAI, needs OPENAI_API_KEY env)
      - "ollama/llama3" (local via Ollama)
      - etc. See LiteLLM docs for the full provider list.

    When `config.llm_api_base` is set, calls are routed through that endpoint
    (e.g., a LiteLLM proxy server providing centralized budget enforcement
    and observability). When unset, LiteLLM uses its standard env-var-based
    provider routing.
    """

    def generate(prompt: str) -> str:
        try:
            import litellm
        except ImportError as exc:
            raise RuntimeError(
                "techne's operator-model path requires litellm. Install it with: "
                "pip install 'techne[llm]'  (the default resident emit/ingest path needs no extra)."
            ) from exc

        kwargs: dict = {
            "model": config.model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        if config.llm_api_base:
            kwargs["api_base"] = config.llm_api_base
        if config.llm_api_key:
            kwargs["api_key"] = config.llm_api_key
        # OPTIONAL PER-CALL TIMEOUT (techne#555). Defaults to unset, so every existing
        # caller behaves exactly as before. It exists because an UNATTENDED caller has
        # no operator to notice a call that never returns: measured on this box, a
        # 1,748-char conflict prompt against a local llama3.2 had not returned after
        # 240s, while a 20-char prompt answered in 1.3s. Interactive callers can wait;
        # a scheduled custodian would simply never finish, and would never say so.
        if getattr(config, "llm_timeout", 0):
            kwargs["timeout"] = config.llm_timeout
        resp = litellm.completion(**kwargs)
        return resp.choices[0].message.content.strip()

    return generate


def _parse_threads(raw: str) -> list[Thread]:
    """Parse the LLM's thread-detection output into Thread objects."""
    threads = []
    sections = re.split(r"## Thread (\d+):\s*(.+)", raw)

    i = 1
    while i + 2 < len(sections):
        thread_num = int(sections[i])
        label = sections[i + 1].strip()
        body = sections[i + 2].strip()

        summary = ""
        resume = ""
        obs_indices = []

        for line in body.split("\n"):
            line = line.strip()
            if line.lower().startswith("observations:"):
                idx_str = line.split(":", 1)[1].strip()
                obs_indices = [int(x.strip()) for x in idx_str.split(",") if x.strip().isdigit()]

        summary_match = re.search(
            r"Summary:\s*(.+?)(?=\nResume:|\nObservations:|\Z)", body, re.DOTALL
        )
        if summary_match:
            summary = summary_match.group(1).strip()

        resume_match = re.search(r"Resume:\s*(.+?)(?=\n## Thread|\Z)", body, re.DOTALL)
        if resume_match:
            resume_text = resume_match.group(1).strip()
            if resume_text.lower() != "none":
                resume = resume_text

        if summary:
            threads.append(
                Thread(
                    thread_num=thread_num,
                    label=label,
                    summary=summary,
                    resume_points=resume,
                    observation_indices=obs_indices,
                )
            )

        i += 3

    if not threads:
        return [Thread(thread_num=1, label="session", summary=raw[:2000])]

    return threads


def _summarize_single(observations: list[dict], generate_fn) -> str:
    """Generate a single summary for a small/simple session."""
    context = "\n\n".join(
        [f"[{obs['tool_name']}] {obs['content'][:500]}" for obs in observations[-20:]]
    )

    prompt = f"""Summarize this session in 2-3 sentences. Focus on:
- What was accomplished (decisions made, conclusions reached)
- Key files/resources modified
- Any errors encountered and how they were resolved

Then add a "## Resume Points" section with 2-3 bullet points for next session.

IMPORTANT:
- If a topic was investigated and concluded, state the conclusion.
- Only reference file names that appear explicitly in the observations.
- Distinguish "decided not to do X" from "still need to evaluate X".

Session observations:
{context}

Summary:"""

    try:
        response = generate_fn(prompt)
        if response:
            return response
    except Exception as e:
        log.warning("thread_single_summary_llm_failed", error=str(e), error_type=type(e).__name__)

    tools: dict[str, int] = {}
    for obs in observations:
        t = obs.get("tool_name", "unknown")
        tools[t] = tools.get(t, 0) + 1
    return (
        f"Session with {len(observations)} observations. "
        f"Tools: {', '.join(f'{k}({v})' for k, v in tools.items())}"
    )
