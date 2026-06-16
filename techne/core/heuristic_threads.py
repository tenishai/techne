"""Heuristic thread detection — structural signals, no LLM required.

The four signals: temporal gap, tool family, repo, entity overlap. Pairwise
additive scoring against a threshold (default 0.7), then a merge pass that
collapses micro-threads. See `heuristic-thread-detection.md` for the design.

Public entry point: ``detect_threads(observations, ...) -> (segments, scores)``.
Pure functions. No DB. No LLM. Filesystem check (for repo detection) is
overridable via ``fs_check`` for testability.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# --- Tool families ---------------------------------------------------------

# Built-in Claude Code tools. User-specific MCP servers (Slack, Datadog, Jira,
# etc.) can be added or overridden via ``user_families`` passed to detection.
DEFAULT_FAMILIES: dict[str, str] = {
    # filesystem
    "Edit": "filesystem",
    "Read": "filesystem",
    "Write": "filesystem",
    "Grep": "filesystem",
    "Glob": "filesystem",
    "NotebookEdit": "filesystem",
    # bash — git detected from content
    "Bash": "bash",
    # web
    "WebFetch": "web",
    "WebSearch": "web",
    # user-prompt — synthetic family for backfilled user messages
    # (see transcript_import). Not meta — high-signal, not transparent.
    "UserPrompt": "user_prompt",
    # meta — transparent: adopts neighbor family for change-detection
    "Agent": "meta",
    "ToolSearch": "meta",
    "Skill": "meta",
    "TaskCreate": "meta",
    "TaskUpdate": "meta",
    "TaskList": "meta",
    "TaskGet": "meta",
    "TaskOutput": "meta",
    "TaskStop": "meta",
    "AskUserQuestion": "meta",
    "EnterPlanMode": "meta",
    "ExitPlanMode": "meta",
    "EnterWorktree": "meta",
    "ExitWorktree": "meta",
    "ScheduleWakeup": "meta",
    "Workflow": "meta",
    "Monitor": "meta",
    "PushNotification": "meta",
    "RemoteTrigger": "meta",
    "CronCreate": "meta",
    "CronList": "meta",
    "CronDelete": "meta",
}

META_FAMILY = "meta"


def resolve_tool_family(
    tool_name: str,
    content: str = "",
    user_families: dict[str, str] | None = None,
) -> str:
    """Resolve a tool name to a family.

    Precedence: user override → built-in → MCP naming convention → ``other``.
    ``Bash`` with a ``git`` invocation in the leading content is reclassified
    as ``git`` so that "Bash → Edit → Bash" inside one repo doesn't look like
    a family change every time.
    """
    if user_families and tool_name in user_families:
        return user_families[tool_name]

    if tool_name in DEFAULT_FAMILIES:
        family = DEFAULT_FAMILIES[tool_name]
        if family == "bash" and re.search(r"\bgit\s", content[:200]):
            return "git"
        return family

    # MCP convention: mcp__<server>__<tool>
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        if len(parts) >= 2 and parts[1]:
            return f"mcp:{parts[1].lower()}"

    return "other"


# --- Entities, paths, repos ------------------------------------------------

# Default entity patterns. Universally-useful only: URL hostnames and file
# basenames. Slack/Datadog/etc. patterns are user-supplied.
DEFAULT_ENTITY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"https?://([a-zA-Z0-9.-]+)"),
]

# Repo-root markers — checked when walking up from a file path.
REPO_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")

# Unix-style absolute path. Liberal in what it accepts.
FILE_PATH_RE = re.compile(r"(/(?:[\w.~-]+/)+[\w.~-]+)")


def extract_file_paths(content: str) -> tuple[set[str], list[str]]:
    """Return (basenames, full_paths) found in content."""
    full = FILE_PATH_RE.findall(content)
    basenames = {p.rsplit("/", 1)[-1] for p in full if "/" in p}
    return basenames, full


def find_repo(
    path: str,
    fs_check: Callable[[Path, str], bool] | None = None,
) -> str | None:
    """Walk up from ``path`` until a repo-root marker is found.

    ``fs_check(directory, marker_name) -> bool`` is the filesystem probe;
    pass a stub in tests. Default uses the real filesystem.
    """
    if fs_check is None:
        def fs_check(p: Path, m: str) -> bool:
            return (p / m).exists()

    p = Path(path)
    if not p.is_absolute():
        return None
    for parent in p.parents:
        for marker in REPO_MARKERS:
            if fs_check(parent, marker):
                return parent.name
    return None


def extract_entities(
    content: str,
    extra_patterns: list[re.Pattern[str]] | None = None,
) -> set[str]:
    """Apply default + extra regex patterns to extract entity strings."""
    entities: set[str] = set()
    patterns = list(DEFAULT_ENTITY_PATTERNS)
    if extra_patterns:
        patterns.extend(extra_patterns)
    for pat in patterns:
        for m in pat.findall(content):
            entities.add(m if isinstance(m, str) else m[0])
    return entities


# --- Feature extraction ----------------------------------------------------

@dataclass
class ObsFeatures:
    """Structural features of one observation."""

    index: int
    timestamp: datetime
    tool_family: str
    repos: set[str] = field(default_factory=set)
    file_paths: set[str] = field(default_factory=set)
    entities: set[str] = field(default_factory=set)
    is_meta: bool = False


def _parse_timestamp(ts_str: str) -> datetime:
    """Best-effort ISO 8601 parse. Always returns a naive datetime (UTC if
    the input was tz-aware) so downstream comparisons against ``datetime.now()``
    don't crash on naive/aware mismatches. Falls back to ``datetime.min`` for
    unparseable input — gives a (near-)zero gap rather than an unbounded one
    that would falsely trigger ``gap_hard``."""
    if not ts_str:
        return datetime.min
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.min
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def extract_features(
    index: int,
    obs: dict,
    user_families: dict[str, str] | None = None,
    extra_entity_patterns: list[re.Pattern[str]] | None = None,
    fs_check: Callable[[Path, str], bool] | None = None,
) -> ObsFeatures:
    """Build the feature vector for one observation."""
    tool_name = obs.get("tool_name", "") or ""
    content = obs.get("content", "") or ""
    ts = _parse_timestamp(obs.get("timestamp", "") or "")

    family = resolve_tool_family(tool_name, content, user_families)
    basenames, full_paths = extract_file_paths(content)

    repos: set[str] = set()
    for full_path in full_paths:
        repo = find_repo(full_path, fs_check=fs_check)
        if repo:
            repos.add(repo)

    entities = extract_entities(content, extra_entity_patterns)
    # File basenames are useful as entities for overlap checks too.
    entities |= basenames

    return ObsFeatures(
        index=index,
        timestamp=ts,
        tool_family=family,
        repos=repos,
        file_paths=basenames,
        entities=entities,
        is_meta=(family == META_FAMILY),
    )


# --- Boundary scoring ------------------------------------------------------

# Gap thresholds in seconds and the additive score each contributes.
GAP_HARD = 3600     # ≥ 60 min — almost always a hard break
GAP_MEDIUM = 1800   # ≥ 30 min — usually a break
GAP_SOFT = 600      # ≥ 10 min — mild signal

FAMILY_CHANGE_SCORE = 0.4
REPO_CHANGE_SCORE = 0.5
ZERO_ENTITY_OVERLAP_SCORE = 0.3
ZERO_FILE_OVERLAP_SCORE = 0.2
# User prompt = candidate thread anchor. Doesn't break alone, but amplifies any
# real signal next to it (gap_medium, repo_change, etc.) so the prompt becomes
# the clean boundary line instead of a nearby tool call.
USER_PROMPT_SCORE = 0.3
USER_PROMPT_FAMILY = "user_prompt"

BOUNDARY_THRESHOLD = 0.7
MIN_SEGMENT_SIZE = 3

# A gap up to this duration can still represent one continuing investigation
# if the same dominant tool family + shared entities span both sides (Rule 4).
CONTINUING_INVESTIGATION_GAP = 3 * GAP_HARD  # 3 hours


@dataclass
class BoundaryScore:
    """Signal-by-signal breakdown for one consecutive observation pair."""

    a_index: int
    b_index: int
    gap_seconds: float
    family_a: str
    family_b: str
    signals: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return sum(self.signals.values())


def _nearest_non_meta(
    features: list[ObsFeatures], start: int, direction: int
) -> ObsFeatures | None:
    """Walk ``features`` in ``direction`` (+1 forward, -1 back) from index
    ``start`` until a non-meta observation is found, or fall off the end."""
    i = start + direction
    while 0 <= i < len(features):
        if not features[i].is_meta:
            return features[i]
        i += direction
    return None


def _score_signals(
    a: ObsFeatures,
    b: ObsFeatures,
    gap: float,
    user_prompt_score: float = USER_PROMPT_SCORE,
) -> dict[str, float]:
    """Compute non-family signals — these don't care about meta substitution."""
    signals: dict[str, float] = {}

    if gap >= GAP_HARD:
        signals["gap_hard"] = 1.0
    elif gap >= GAP_MEDIUM:
        signals["gap_medium"] = 0.6
    elif gap >= GAP_SOFT:
        signals["gap_soft"] = 0.3

    if a.repos and b.repos and not (a.repos & b.repos):
        signals["repo_change"] = REPO_CHANGE_SCORE

    if a.entities and b.entities and not (a.entities & b.entities):
        signals["zero_entity_overlap"] = ZERO_ENTITY_OVERLAP_SCORE

    if a.file_paths and b.file_paths and not (a.file_paths & b.file_paths):
        signals["zero_file_overlap"] = ZERO_FILE_OVERLAP_SCORE

    if b.tool_family == USER_PROMPT_FAMILY:
        signals["user_prompt"] = user_prompt_score

    return signals


def score_boundaries(
    features: list[ObsFeatures],
    user_prompt_score: float = USER_PROMPT_SCORE,
) -> list[BoundaryScore]:
    """Score every consecutive pair.

    Time gap and other non-family signals are computed on the actual pair.
    The *family-change* signal is computed using each side's nearest non-meta
    neighbor — so a ToolSearch (meta) sandwiched between two Datadog calls
    doesn't generate a spurious family-change.
    """
    scores: list[BoundaryScore] = []
    for i in range(len(features) - 1):
        a, b = features[i], features[i + 1]
        gap = max(0.0, (b.timestamp - a.timestamp).total_seconds())

        signals = _score_signals(a, b, gap, user_prompt_score=user_prompt_score)

        a_eff = a if not a.is_meta else (_nearest_non_meta(features, i, -1) or a)
        b_eff = b if not b.is_meta else (_nearest_non_meta(features, i + 1, +1) or b)
        # Suppress family_change when crossing into a UserPrompt — the prompt's
        # synthetic family would otherwise double-count with the user_prompt
        # signal (a tool → prompt transition always changes family), pushing
        # every mid-flow prompt to the boundary threshold.
        if (
            not a_eff.is_meta
            and not b_eff.is_meta
            and a_eff.tool_family != b_eff.tool_family
            and b_eff.tool_family != USER_PROMPT_FAMILY
        ):
            signals["family_change"] = FAMILY_CHANGE_SCORE

        scores.append(
            BoundaryScore(
                a_index=a.index,
                b_index=b.index,
                gap_seconds=gap,
                family_a=a.tool_family,
                family_b=b.tool_family,
                signals=signals,
            )
        )
    return scores


# --- Segmentation + merge --------------------------------------------------

def split_at_boundaries(
    n_observations: int,
    scores: list[BoundaryScore],
    threshold: float = BOUNDARY_THRESHOLD,
) -> list[list[int]]:
    """Split ``n_observations`` into segments using any score ≥ threshold."""
    if n_observations == 0:
        return []

    boundaries = {s.a_index for s in scores if s.total >= threshold}
    segments: list[list[int]] = []
    current: list[int] = []
    for i in range(n_observations):
        current.append(i)
        if i in boundaries:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return segments


def _segment_dominant_repo(
    segment: list[int], features: list[ObsFeatures]
) -> str | None:
    counts: dict[str, int] = {}
    for i in segment:
        for r in features[i].repos:
            counts[r] = counts.get(r, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _segment_dominant_family(
    segment: list[int], features: list[ObsFeatures]
) -> str | None:
    """Most-common non-meta family in a segment, or None if it's all-meta."""
    counts: dict[str, int] = {}
    for i in segment:
        f = features[i]
        if f.is_meta:
            continue
        counts[f.tool_family] = counts.get(f.tool_family, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _entity_overlap(
    seg_a: list[int], seg_b: list[int], features: list[ObsFeatures]
) -> int:
    ents_a: set[str] = set()
    ents_b: set[str] = set()
    for i in seg_a:
        ents_a.update(features[i].entities)
    for i in seg_b:
        ents_b.update(features[i].entities)
    return len(ents_a & ents_b)


def _inter_segment_gap(
    seg_a: list[int], seg_b: list[int], features: list[ObsFeatures]
) -> float:
    """Gap from the end of seg_a to the start of seg_b."""
    if not seg_a or not seg_b:
        return 0.0
    return max(
        0.0, (features[seg_b[0]].timestamp - features[seg_a[-1]].timestamp).total_seconds()
    )


def merge_pass(
    segments: list[list[int]],
    features: list[ObsFeatures],
    min_size: int = MIN_SEGMENT_SIZE,
) -> list[list[int]]:
    """Apply merge rules until stable.

    Rule 1 — Tiny segments (< ``min_size``) merge into the neighbor with
    higher entity overlap. Ties merge forward (small segment is setup for
    what follows). Tail tiny segment with no following neighbor merges back.

    Rule 2 — Meta-only segments are preamble and merge into the following
    segment. Trailing meta-only segment merges back.

    Rule 3 — Same-repo adjacent segments merge if no time gap ≥ GAP_HARD
    between them. Same-repo means same *dominant* repo on both sides.

    Rule 4 — Same dominant tool family + nonzero entity overlap → merge,
    tolerating gaps up to ``CONTINUING_INVESTIGATION_GAP`` (3h). Captures
    the "long break in the middle of one investigation" pattern that the
    same-repo rule can't catch when the work is over services/dashboards
    rather than code.
    """
    if len(segments) <= 1:
        return [list(s) for s in segments]

    segments = [list(s) for s in segments]
    max_passes = len(segments) * 4 + 4  # safe upper bound; each pass eliminates ≥1 segment

    for _ in range(max_passes):
        changed = False

        # Rule 1: tiny segments
        i = 0
        while i < len(segments):
            seg = segments[i]
            if len(seg) < min_size and len(segments) > 1:
                prev_overlap = (
                    _entity_overlap(segments[i - 1], seg, features) if i > 0 else -1
                )
                next_overlap = (
                    _entity_overlap(seg, segments[i + 1], features)
                    if i + 1 < len(segments)
                    else -1
                )

                if next_overlap >= prev_overlap and i + 1 < len(segments):
                    segments[i + 1] = seg + segments[i + 1]
                    del segments[i]
                    changed = True
                    continue
                if i > 0:
                    segments[i - 1] = segments[i - 1] + seg
                    del segments[i]
                    changed = True
                    continue
            i += 1

        # Rule 2: meta-only segments
        i = 0
        while i < len(segments):
            seg = segments[i]
            if all(features[idx].is_meta for idx in seg):
                if i + 1 < len(segments):
                    segments[i + 1] = seg + segments[i + 1]
                    del segments[i]
                    changed = True
                    continue
                if i > 0:
                    segments[i - 1] = segments[i - 1] + seg
                    del segments[i]
                    changed = True
                    continue
            i += 1

        # Rule 3: same-repo adjacent
        i = 0
        while i + 1 < len(segments):
            repo_a = _segment_dominant_repo(segments[i], features)
            repo_b = _segment_dominant_repo(segments[i + 1], features)
            gap = _inter_segment_gap(segments[i], segments[i + 1], features)
            if repo_a and repo_a == repo_b and gap < GAP_HARD:
                segments[i] = segments[i] + segments[i + 1]
                del segments[i + 1]
                changed = True
                continue
            i += 1

        # Rule 4: continuing investigation (same family + entity overlap)
        i = 0
        while i + 1 < len(segments):
            fam_a = _segment_dominant_family(segments[i], features)
            fam_b = _segment_dominant_family(segments[i + 1], features)
            overlap = _entity_overlap(segments[i], segments[i + 1], features)
            gap = _inter_segment_gap(segments[i], segments[i + 1], features)
            if (
                fam_a
                and fam_a == fam_b
                and overlap > 0
                and gap < CONTINUING_INVESTIGATION_GAP
            ):
                segments[i] = segments[i] + segments[i + 1]
                del segments[i + 1]
                changed = True
                continue
            i += 1

        if not changed:
            break

    return segments


# --- Label generation ------------------------------------------------------

# Families that don't generate informative "<Family> investigation" labels
# on their own — we only use them when there's nothing else. user_prompt is
# structural (a user turn happened) not semantic (what the work was about),
# so it never wants to drive a label either.
GENERIC_FAMILIES = {"filesystem", "bash", "meta", "other", "user_prompt"}


def _segment_dominant_entity(
    segment: list[int], features: list[ObsFeatures]
) -> str | None:
    """Most-common non-trivial entity in a segment, or None."""
    counts: dict[str, int] = {}
    for i in segment:
        for e in features[i].entities:
            counts[e] = counts.get(e, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _humanize_family(family: str) -> str:
    """Render a family name for display."""
    if family.startswith("mcp:"):
        return family[4:].replace("_", " ").title()
    return family.replace("_", " ").title()


def generate_label(segment: list[int], features: list[ObsFeatures]) -> str:
    """Build a short, informative label for a segment.

    Priority:
    1. Dominant non-generic family → "<Family> investigation" (e.g.,
       "Datadog investigation"). Generic families = filesystem/bash/meta/other.
    2. Dominant repo → "Work on <repo>".
    3. Append top entity if present → ": <entity>".
    4. Fallback: "Session work" + the two most-used families.
    """
    if not segment:
        return "Session work"

    fam = _segment_dominant_family(segment, features)
    repo = _segment_dominant_repo(segment, features)
    entity = _segment_dominant_entity(segment, features)

    if fam and fam not in GENERIC_FAMILIES:
        base = f"{_humanize_family(fam)} investigation"
        if entity:
            return f"{base}: {entity}"
        return base

    if repo:
        base = f"Work on {repo}"
        if entity:
            return f"{base}: {entity}"
        return base

    # Fallback: top families by count. Skip purely-structural markers
    # (user_prompt, meta) — they carry no semantic information about the work
    # itself, only that user turns / orchestration calls happened. "Session
    # work (Filesystem, User Prompt)" is noisier than "Session work
    # (Filesystem)" without adding signal.
    fam_counts: dict[str, int] = {}
    for i in segment:
        f = features[i]
        if f.tool_family and f.tool_family not in {USER_PROMPT_FAMILY, META_FAMILY}:
            fam_counts[f.tool_family] = fam_counts.get(f.tool_family, 0) + 1
    top = sorted(fam_counts.items(), key=lambda kv: kv[1], reverse=True)[:2]
    if top:
        names = ", ".join(_humanize_family(k) for k, _ in top)
        return f"Session work ({names})"
    return "Session work"


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
        for b in f.file_paths:
            if b not in seen_files:
                seen_files.add(b)
                files.append(b)
        for e in f.entities:
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
        for b in basenames:
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


# --- Public entry point ----------------------------------------------------

def detect_threads(
    observations: list[dict],
    user_families: dict[str, str] | None = None,
    extra_entity_patterns: list[re.Pattern[str]] | None = None,
    boundary_threshold: float = BOUNDARY_THRESHOLD,
    user_prompt_score: float = USER_PROMPT_SCORE,
    min_segment_size: int = MIN_SEGMENT_SIZE,
    fs_check: Callable[[Path, str], bool] | None = None,
) -> tuple[list[list[int]], list[BoundaryScore]]:
    """Detect thread boundaries from a session's observations.

    Returns ``(segments, scores)``. Segments are lists of observation
    indices; scores are returned alongside so callers can persist them for
    the audit surface.
    """
    if not observations:
        return [], []

    features = [
        extract_features(i, obs, user_families, extra_entity_patterns, fs_check)
        for i, obs in enumerate(observations)
    ]
    scores = score_boundaries(features, user_prompt_score=user_prompt_score)
    raw_segments = split_at_boundaries(
        len(observations), scores, threshold=boundary_threshold
    )
    segments = merge_pass(raw_segments, features, min_size=min_segment_size)
    return segments, scores


# --- Persistence -----------------------------------------------------------

def boundary_indices_from_segments(segments: list[list[int]]) -> set[int]:
    """Map final segmentation back to the set of pair-A indices that count
    as actual thread boundaries (i.e. the last index of each non-final segment).

    Boundary scores whose ``a_index`` is in this set survived the merge pass.
    Scores whose ``a_index`` is *not* in the set either never crossed
    threshold OR were collapsed by a merge rule.
    """
    if not segments:
        return set()
    boundaries: set[int] = set()
    for seg in segments[:-1]:
        if seg:
            boundaries.add(seg[-1])
    return boundaries


def save_boundaries(
    conn: sqlite3.Connection,
    session_id: str,
    scores: list[BoundaryScore],
    boundary_a_indices: set[int],
    *,
    commit: bool = True,
) -> None:
    """Persist every per-pair score plus its final boundary state.

    ``boundary_a_indices`` is the set of ``a_index`` values that became
    real thread boundaries — typically from
    ``boundary_indices_from_segments(segments)``.
    """
    if not scores:
        return
    rows = [
        (
            session_id,
            s.a_index,
            s.b_index,
            s.gap_seconds,
            s.family_a,
            s.family_b,
            json.dumps(s.signals, sort_keys=True),
            s.total,
            1 if s.a_index in boundary_a_indices else 0,
        )
        for s in scores
    ]
    conn.executemany(
        """INSERT INTO thread_boundaries
              (session_id, obs_index_a, obs_index_b, gap_seconds,
               family_a, family_b, signals_json, total_score, is_boundary)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    if commit:
        conn.commit()


def load_boundaries(
    conn: sqlite3.Connection, session_id: str
) -> list[dict]:
    """Load persisted boundary rows for a session, ordered by obs_index_a.

    Each row is returned as a dict with ``signals`` parsed back from JSON.
    Convenience for the audit CLI.
    """
    rows = conn.execute(
        """SELECT obs_index_a, obs_index_b, gap_seconds, family_a, family_b,
                  signals_json, total_score, is_boundary
             FROM thread_boundaries
            WHERE session_id = ?
            ORDER BY obs_index_a""",
        (session_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "obs_index_a": r[0],
                "obs_index_b": r[1],
                "gap_seconds": r[2],
                "family_a": r[3],
                "family_b": r[4],
                "signals": json.loads(r[5]),
                "total_score": r[6],
                "is_boundary": bool(r[7]),
            }
        )
    return out
