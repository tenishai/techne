"""Thread detection and management.

Ported from thread_summarizer.py. Detects distinct topic threads within a
session's observations and generates per-thread summaries. Threads are the
unit of continuity — they link across sessions to form a chain of work.
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from techne.config import Config
from techne.core.redaction import redact_credentials

MAX_SAMPLED_OBSERVATIONS = 60


@dataclass
class Thread:
    thread_num: int
    label: str
    summary: str
    resume_points: str = ""
    observation_indices: list[int] = field(default_factory=list)


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
            print(
                f"techne: skipping bad entity pattern {pattern!r}: {e}",
                file=sys.stderr,
            )

    user_families = config.threads.tool_families or None
    extras = extra_patterns or None

    segments, scores = ht.detect_threads(
        observations,
        user_families=user_families,
        extra_entity_patterns=extras,
        boundary_threshold=config.heuristic.boundary_threshold,
        user_prompt_score=config.heuristic.user_prompt_score,
    )

    # Build features once and reuse for label / fallback summary.
    features = [
        ht.extract_features(i, obs, user_families=user_families, extra_entity_patterns=extras)
        for i, obs in enumerate(observations)
    ]

    use_llm_summary = config.threads.summarizer == "llm"
    if use_llm_summary and generate_fn is None:
        generate_fn = _make_generate_fn(config)

    threads: list[Thread] = []
    for thread_num, segment in enumerate(segments, 1):
        seg_observations = [observations[i] for i in segment]
        seg_features = [features[i] for i in segment]
        label = ht.generate_label(segment, features)
        resume = ht.build_resume_point(seg_observations)

        summary = ht.fallback_summary(seg_observations, seg_features)
        if use_llm_summary:
            try:
                summary = _summarize_thread_llm(seg_observations, label, generate_fn)
            except Exception as e:
                print(
                    f"techne: per-thread LLM summary failed "
                    f"({type(e).__name__}: {e}); using heuristic fallback",
                    file=sys.stderr,
                )

        threads.append(
            Thread(
                thread_num=thread_num,
                label=label,
                summary=summary,
                resume_points=resume,
                observation_indices=segment,
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
A "thread" is a major investigation, project, or piece of work — NOT individual tool calls or sub-tasks.
Group related refactors, edits, and fixes into the SAME thread if they serve the same goal.
Most sessions have 1-4 threads. If you find more than 5, you are over-segmenting — merge related threads.

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
        print(
            f"techne: thread detection failed ({type(e).__name__}: {e}); "
            f"falling back to single summary",
            file=sys.stderr,
        )

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
        thread_id = f"{session_id}-{t.thread_num}"
        parent_id = None

        results = search_fts(
            db_path,
            t.label + " " + t.summary[:200],
            table="threads",
            limit=5,
        )

        for r in results:
            candidate_id = r.get("id", "")
            if candidate_id.startswith(session_id):
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


def save_threads(
    threads: list[Thread],
    session_id: str,
    db_path: Path,
    parent_links: list[tuple[str, str | None]] | None = None,
    conn=None,
):
    """Persist threads to the database.

    If `conn` is provided the caller owns the transaction (no commit/close
    here). Pass a connection to make DELETE + insert atomic in redetection.
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
            thread_id = f"{session_id}-{t.thread_num}"
            parent_id = link_map.get(thread_id)
            conn.execute(
                """INSERT OR REPLACE INTO threads
                   (id, session_id, thread_num, label, summary, resume_points,
                    status, parent_thread_id, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, CURRENT_TIMESTAMP)""",
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
        # Active → completed: no resume points and no activity in complete_after_hours
        cur = conn.execute(
            """UPDATE threads SET status = 'completed', updated_at = CURRENT_TIMESTAMP
               WHERE status = 'active'
               AND (resume_points IS NULL OR resume_points = '')
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
        import litellm

        kwargs: dict = {
            "model": config.model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        if config.llm_api_base:
            kwargs["api_base"] = config.llm_api_base
        if config.llm_api_key:
            kwargs["api_key"] = config.llm_api_key
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
        print(
            f"techne: single summary failed ({type(e).__name__}: {e}); "
            f"falling back to bare summary",
            file=sys.stderr,
        )

    tools: dict[str, int] = {}
    for obs in observations:
        t = obs.get("tool_name", "unknown")
        tools[t] = tools.get(t, 0) + 1
    return (
        f"Session with {len(observations)} observations. "
        f"Tools: {', '.join(f'{k}({v})' for k, v in tools.items())}"
    )
