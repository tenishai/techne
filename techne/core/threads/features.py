"""Feature extraction for heuristic thread detection.

ObsFeatures, extract_features, family/repo/entity resolution.
"""

from __future__ import annotations

import re
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
