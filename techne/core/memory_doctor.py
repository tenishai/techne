"""Ephemeral memory reconciliation — `techne memory doctor` (techne#107 PR1).

The spool→drain→reconcile thesis: the client (Claude Code) authors its native
`~/.claude/projects/<slug>/memory/*.md` (the curated SPOOL); techne holds the raw
tool-call LEDGER (the observations table). The value-add is diffing the two — the only
store with both the curated summary AND the raw deeds can check memory against reality.

This is the *ephemeral* first cut: walk the memory files from disk, query the deed-log,
diff at FILE-REFERENCE grain, return a report. No persistence, no schema change, no
posture — a read-only diagnostic that proves the moat before any plumbing is built.

Scope (deliberate, techne#107): file-reference reconciliation only. The deed-log content
is truncated to ~500 chars (`post_tool_use` summaries), so file PATHS are reliably
recoverable but claim-level facts are not — claim-level corroboration is PR3 (#105).
Matching is by BASENAME (a path-aware refinement is deferred); collisions are possible.
"""

from __future__ import annotations

import re
from pathlib import Path

from techne.core.state import deed_log
from techne.core.threads.features import extract_file_paths

# Cap on deeds pulled per project-path candidate. Surfaced via report["deed_log_capped"]
# rather than silently dropping the oldest deeds (which would read as false `unbacked`).
DEED_LIMIT = 2000

# Bare filename tokens with a known source/config extension — catches `a.py` mentioned
# in prose, which the absolute-path FILE_PATH_RE (reused below) misses. Non-capturing
# group so findall returns the whole token.
_FILE_TOKEN_RE = re.compile(
    r"[\w./-]*\b[\w-]+\.(?:py|js|ts|tsx|jsx|md|ya?ml|json|toml|go|rs|sh|sql|txt|cfg|"
    r"ini|rb|java|cpp|hpp|[ch])\b"
)

# Deed observations whose summary names the file actually touched. post_tool_use renders
# Write as "Wrote: <fp>\n…" and Edit as "Edited: <fp>\n…" (first-line prefix);
# MultiEdit/NotebookEdit fall through to "Tool: …\nInput: {json}", so their path is read
# from the json's "file_path" instead (see _deed_touched_files).
_TOUCH_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_TOUCH_PREFIXES = ("Wrote:", "Edited:")
_FILE_PATH_JSON_RE = re.compile(r'"file_path"\s*:\s*"([^"]+)"')


def find_curated_memory_files(cwd: Path | None = None) -> list[Path]:
    """Resolve CC's curated memory files for a project. The directory is resolved by
    ``auto_memory_dir`` — the default ``~/.claude/projects/<git-root-slug>/memory/`` or a
    user-set ``autoMemoryDirectory`` override (techne#175). Empty list when absent."""
    from techne.platforms.claude_code.paths import auto_memory_dir

    cwd = Path(cwd) if cwd else Path.cwd()
    mem_dir = auto_memory_dir(cwd)
    if not mem_dir.is_dir():
        return []
    return sorted(mem_dir.glob("*.md"))


def _file_basenames(text: str) -> set[str]:
    """All file references in *text*, normalized to basenames. Unions the reused
    absolute-path extractor with the bare-filename token regex."""
    basenames, _full = extract_file_paths(text)  # techne#107 reuse (threads/features)
    for tok in _FILE_TOKEN_RE.findall(text):
        basenames.add(tok.rsplit("/", 1)[-1])
    return basenames


def _deed_touched_files(deeds: list[dict]) -> set[str]:
    """Basenames of files actually WRITTEN/EDITED in the deed-log (the authoritative
    'touched' signal). For Write/Edit the path is the first-line prefix; for
    MultiEdit/NotebookEdit (no prefix) it's the `file_path` in the rendered json. Reads
    the target path, not the truncated body, so a file merely mentioned isn't counted."""
    touched: set[str] = set()
    for d in deeds:
        if d.get("tool_name") not in _TOUCH_TOOLS:
            continue
        content = d.get("content") or ""
        path: str | None = None
        first = content.splitlines()[0] if content else ""
        for pre in _TOUCH_PREFIXES:
            if first.startswith(pre):
                path = first[len(pre):].strip()
                break
        if path is None:  # MultiEdit/NotebookEdit render "Tool: …\nInput: {json}"
            m = _FILE_PATH_JSON_RE.search(content)
            if m:
                path = m.group(1)
        if path:
            touched.add(path.rsplit("/", 1)[-1])
    return touched


def reconcile(db_path: Path, *, cwd: Path | None = None, since_days: int | None = None) -> dict:
    """Diff curated memory against the deed-log at file-reference grain (techne#107).

    Returns a report dict:
      memory_present  — were any curated memory files found
      memory_files    — the discovered .md paths
      deed_count      — observations considered (windowed)
      window_days     — the deed window (None = all time)
      corroborated    — files both named in memory AND touched in deeds
      omissions       — files touched in deeds but NOT named in memory
      unbacked        — files named in memory but NOT touched in the deed window
    """
    cwd = Path(cwd) if cwd else Path.cwd()
    files = find_curated_memory_files(cwd)
    memory_text = "\n".join(_safe_read(f) for f in files)
    memory_refs = _file_basenames(memory_text)

    # Scope deeds to this project; match both the resolved and raw cwd, since the
    # capture hook may store either form in sessions.project_path.
    candidates = {str(cwd), str(cwd.resolve())}
    deeds: dict[int, dict] = {}
    capped = False
    for proj in candidates:
        batch = deed_log(db_path, project_path=proj, since_days=since_days, limit=DEED_LIMIT)
        if len(batch) >= DEED_LIMIT:
            capped = True  # oldest deeds dropped → omissions/unbacked may be incomplete
        for d in batch:
            deeds[d["id"]] = d
    deed_list = list(deeds.values())
    touched = _deed_touched_files(deed_list)

    return {
        "memory_present": bool(files),
        "memory_files": [str(f) for f in files],
        "deed_count": len(deed_list),
        "window_days": since_days,
        "deed_log_capped": capped,
        "corroborated": sorted(memory_refs & touched),
        "omissions": sorted(touched - memory_refs),
        "unbacked": sorted(memory_refs - touched),
    }


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
