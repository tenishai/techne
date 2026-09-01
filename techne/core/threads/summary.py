"""Summary helpers for heuristic thread detection.

fallback_summary, format_combined_summary, build_resume_point and helpers.
"""

from __future__ import annotations

import re
from datetime import datetime

from techne.core.threads.features import ObsFeatures, extract_features, extract_file_paths

# --- Resume points ---------------------------------------------------------

# Patterns that signal an error / unresolved issue in observation content.
ERROR_LINE_RE = re.compile(
    r"^(?:Traceback|Error|Exception|AssertionError|FAILED|"
    r"\s*File \".+\", line \d+|.*Error:.+|.*: error:.+|"
    r"FAIL\s|FAILED\s|FATAL\s)",
    re.MULTILINE,
)

# File ops that indicate "this file is open in the thread"
FILE_OP_TOOLS = {"Edit", "Read", "Write", "NotebookEdit", "Grep", "Glob"}


def _humanize_relative(now: datetime, then: datetime) -> str:
    """Compact relative duration string (e.g. "4m ago", "2h ago")."""
    delta = (now - then).total_seconds()
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _short(text: str, limit: int = 120) -> str:
    """Trim and single-line whitespace-collapse a content snippet."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _last_error_snippet(observations: list[dict]) -> str | None:
    """Find the most recent error-like line in any observation content."""
    for obs in reversed(observations):
        content = obs.get("content", "") or ""
        m = ERROR_LINE_RE.search(content)
        if m:
            line = m.group(0).strip()
            return _short(line, 200)
    return None


def _recent_files(observations: list[dict], limit: int = 5) -> list[str]:
    """Distinct file basenames from the most recent file-op observations."""
    seen: list[str] = []
    for obs in reversed(observations):
        if obs.get("tool_name") not in FILE_OP_TOOLS:
            continue
        basenames, _ = extract_file_paths(obs.get("content", "") or "")
        # SORTED FOR THE SAME REASON AS fallback_summary (Prudence, #873 r1):
        # `basenames` is a set and this loop RETURNS EARLY at `limit`, so hash
        # order decided which files appeared in `Open files:` — 12 distinct lines
        # across 12 seeds. It lands in `resume_points`, which is FTS-indexed.
        for b in sorted(basenames):
            if b not in seen:
                seen.append(b)
                if len(seen) >= limit:
                    return seen
    return seen


def build_resume_point(
    observations: list[dict],
    now: datetime | None = None,
) -> str:
    """Build a structured resume_point string for one thread's observations.

    Plain text, multi-line, ready to drop into ``Thread.resume_points``.
    Returns "" for empty input.

    The fields ("Last activity", "Last action", "Open files", etc.) are stable
    so downstream tooling can parse them if needed.
    """
    from techne.core.threads.features import _parse_timestamp

    if not observations:
        return ""

    now = now or datetime.now()

    last = observations[-1]
    last_ts = _parse_timestamp(last.get("timestamp", "") or "")
    last_tool = last.get("tool_name", "?")
    last_content = _short(last.get("content", "") or "", 100)

    files = _recent_files(observations)
    error = _last_error_snippet(observations)

    lines: list[str] = []
    if last_ts != datetime.min:
        lines.append(f"Last activity: {_humanize_relative(now, last_ts)}")
    lines.append(f"Last action: {last_tool} {last_content}".rstrip())

    if files:
        lines.append(f"Open files: {', '.join(files)}")

    if error:
        lines.append(f"Last error: {error}")

    # Last 2 prior observations (excluding the one already shown as Last action)
    tail = observations[-3:-1]
    if tail:
        lines.append("Recent:")
        for obs in tail:
            tool = obs.get("tool_name", "?")
            content = _short(obs.get("content", "") or "", 80)
            lines.append(f"  - {tool}: {content}")

    return "\n".join(lines)


# --- No-LLM fallback summary ----------------------------------------------


def fallback_summary(
    observations: list[dict],
    features: list[ObsFeatures] | None = None,
    max_files: int = 5,
    max_entities: int = 5,
) -> str:
    """Build a structured summary string from observation features alone.

    Output shape (single line, semicolon-delimited):
        "<N> observations; tools: filesystem(12), bash(3); files: a.py, b.py; entities: papi, capig"

    Sections omitted when empty so the line stays compact.
    """
    if not observations:
        return ""

    if features is None:
        features = [extract_features(i, obs) for i, obs in enumerate(observations)]

    fam_counts: dict[str, int] = {}
    files: list[str] = []
    seen_files: set[str] = set()
    entities: list[str] = []
    seen_entities: set[str] = set()

    for f in features:
        if f.tool_family:
            fam_counts[f.tool_family] = fam_counts.get(f.tool_family, 0) + 1
        # SORTED BECAUSE THE OUTPUT IS TRUNCATED (Prudence, #873 r1). `file_paths`
        # and `entities` are SETS, and these lists are later sliced to
        # `max_files` / `max_entities` — so hash order decided not the display
        # order but WHICH FIVE ARE SHOWN. Measured: 12 distinct summaries from 12
        # seeds. `summary` is FTS-indexed and `link_threads` searches
        # `label + summary[:200]` to choose a parent_thread_id, so a per-process
        # summary can select a different parent and persist it as an FK.
        for b in sorted(f.file_paths):
            if b not in seen_files:
                seen_files.add(b)
                files.append(b)
        for e in sorted(f.entities):
            if e in seen_files:
                # file basenames already shown in `files:` — skip in entities
                continue
            if e not in seen_entities:
                seen_entities.add(e)
                entities.append(e)

    parts = [f"{len(observations)} observations"]

    if fam_counts:
        top = sorted(fam_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]
        parts.append("tools: " + ", ".join(f"{k}({v})" for k, v in top))

    if files:
        shown = files[:max_files]
        more = len(files) - len(shown)
        f_part = "files: " + ", ".join(shown)
        if more > 0:
            f_part += f" (+{more} more)"
        parts.append(f_part)

    if entities:
        shown = entities[:max_entities]
        more = len(entities) - len(shown)
        e_part = "entities: " + ", ".join(shown)
        if more > 0:
            e_part += f" (+{more} more)"
        parts.append(e_part)

    return "; ".join(parts)


def format_combined_summary(threads) -> str:
    """Format all threads into a single combined summary string.

    ``threads`` is a list of Thread-like objects with ``.summary``,
    ``.resume_points``, ``.thread_num``, and ``.label`` attributes.
    Imported here from thread_orchestrator to avoid a circular import —
    Thread lives in thread_orchestrator but format_combined_summary is
    spec'd to live in summary.py per issue #6.
    """
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
