"""Resolution of host Claude Code config paths, honoring CLAUDE_CONFIG_DIR.

CLAUDE_CONFIG_DIR is **believed** to relocate Claude Code's whole config root —
both the ``~/.claude/`` directory and the ``~/.claude.json`` file, the latter to
``${CLAUDE_CONFIG_DIR}.json`` (a sibling of the dir). This is OBSERVED behavior,
not officially documented (tracked in claude-code GitHub issues #33430 / #25762),
so treat the sibling-``.json`` rule as load-bearing-but-unverified-in-tree: the
MCP register/unregister path in ``hooks.py`` depends on it. Worth confirming
against a live relocated install before relying on it heavily.

techne reads these host files for transcript import, hook installation, and the
native-memory probe; honoring the env var lets a second agent on one OS user
read its own state instead of the default agent's (techne#113).
"""

import json
import os
import re
import unicodedata
from pathlib import Path


def project_slug(cwd: Path | str) -> str:
    """CC's per-project transcript/projects slug for ``~/.claude/projects/<slug>/``.

    Replicates Claude Code's ``kS`` function (verified against the compiled
    ``@anthropic-ai/claude-code`` source, v2.1.195 — techne#167):

        slug = re.sub(r'[^a-zA-Z0-9]', '-', NFC(realpath(cwd)))

    Three things the prior two divergent copies each got wrong:
    1. EVERY non-alphanumeric char is replaced — not just ``/`` (transcript_import)
       and not just ``/`` and ``.`` (this fn previously). ``_``, spaces, ``@`` etc.
       all become ``-``.
    2. The path IS realpath'd first (CC calls ``fs.realpathSync``), so symlinks/
       firmlinks are dereferenced — ``.resolve()`` is correct, ``.absolute()`` was
       not. (On macOS ``/tmp`` → ``/private/tmp``, ``/home`` → a firmlink target.)
    3. NFC-normalize so non-ASCII path components match CC's ``.normalize("NFC")``.

    This is the single source of truth: the native-memory probe, the #107 memory-
    reconciliation discovery, and transcript import all route through it.

    NOTE: CC truncates slugs >200 chars and appends a djb2 hash; that edge is not
    replicated here (real project paths are far shorter), so a >200-char cwd would
    resolve to the wrong dir. Tracked as a known limitation.
    """
    p = Path(cwd)
    try:
        resolved = str(p.resolve())
    except OSError:
        # Mirror CC's realpathSync try/catch: fall back to the unresolved absolute
        # path when the cwd can't be stat'd (e.g. it no longer exists).
        resolved = str(p.absolute())
    return re.sub(r"[^a-zA-Z0-9]", "-", unicodedata.normalize("NFC", resolved))


def claude_config_dir() -> Path:
    """The Claude Code config directory (``~/.claude`` unless relocated)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def claude_json_path() -> Path:
    """The ``~/.claude.json`` equivalent. When CLAUDE_CONFIG_DIR is set, Claude
    Code is believed to store it at ``${CLAUDE_CONFIG_DIR}.json`` (sibling of the
    dir) — see module docstring on why this is observed-but-undocumented."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if not env:
        return Path.home() / ".claude.json"
    # with_name appends '.json' to the final path component, so a trailing slash
    # (normalized away by pathlib) and '~' (expanded above) are both handled.
    p = Path(env).expanduser()
    return p.with_name(p.name + ".json")


def claude_projects_dir() -> Path:
    """The transcripts/projects dir under the (possibly relocated) config dir."""
    return claude_config_dir() / "projects"


def _git_project_root(cwd: Path) -> Path:
    """The git repo root at/above *cwd* (the dir containing ``.git``), else *cwd*.

    CC's auto-memory is keyed on the GIT REPOSITORY, not the raw cwd: per the official
    docs (https://code.claude.com/docs/en/memory.md) "<project> is derived from the git
    repository … all worktrees and subdirectories within the same repo share one"; outside
    a repo the project root (cwd) is used. So a session run from a subdirectory resolves
    the same memory dir as one run from the repo root. (techne#175)
    """
    try:
        cur = cwd.resolve()
    except OSError:
        cur = cwd.absolute()
    for parent in (cur, *cur.parents):
        if (parent / ".git").exists():
            return parent
    return cur


def memory_project_slug(cwd: Path | str | None = None) -> str:
    """The auto-memory project slug: ``project_slug`` of the GIT REPO ROOT (or cwd outside
    a repo). Used both for the on-disk memory dir AND as the internal curated_memory DB key,
    so the two stay consistent regardless of which subdirectory a session ran from.
    (techne#175 — distinct from ``project_slug``, which keys transcripts on the raw cwd.)"""
    cwd = Path(cwd).expanduser() if cwd else Path.cwd()
    return project_slug(_git_project_root(cwd))


def _auto_memory_directory_override(cwd: Path) -> Path | None:
    """The `autoMemoryDirectory` setting, if any, read across CC's settings scopes in
    precedence order (user → project → local; later wins). Returns the expanded Path or
    None. The value must be absolute or start with ``~/`` (per the docs); anything else is
    ignored. Malformed/absent settings files are skipped. (techne#175)"""
    candidates = [
        claude_config_dir() / "settings.json",       # user scope
        cwd / ".claude" / "settings.json",            # project scope
        cwd / ".claude" / "settings.local.json",      # local scope
    ]
    value: str | None = None
    for f in candidates:
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("autoMemoryDirectory"), str):
            value = data["autoMemoryDirectory"]
    if value and (value.startswith("~/") or value.startswith("/")):
        return Path(value).expanduser()
    return None


def auto_memory_dir(cwd: Path | str | None = None) -> Path:
    """Resolve CC's auto-memory directory for a project (techne#175).

    Order (per https://code.claude.com/docs/en/memory.md, CC v2.1.x — verified against the
    installed binary's help string + the on-disk layout):
      1. `autoMemoryDirectory` from settings.json (user/project/local) — used verbatim.
      2. else ``<config>/projects/<memory_project_slug>/memory`` — the default, where the
         slug is the GIT-REPO-ROOT slug (not the raw cwd).

    NOTE: `agent-memory` (`~/.claude/agent-memory/`, the kS/ZUi `ZUi` regex) is a SEPARATE
    feature — subagent memory / the memory-tool beta — NOT this project auto-memory. The
    #175 worry that techne reads the wrong base dir does not hold for the default path.
    """
    cwd = Path(cwd).expanduser() if cwd else Path.cwd()
    override = _auto_memory_directory_override(cwd)
    if override is not None:
        return override
    return claude_projects_dir() / memory_project_slug(cwd) / "memory"
