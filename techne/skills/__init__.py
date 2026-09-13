"""The procedures techne bundles, and how to read them.

NEUTRAL BY CONSTRUCTION. This module knows which procedures ship and how to load
their text; it knows nothing about where any host wants them written. Platform
renderers depend on this, not the reverse — the same rule ``core/transcripts.py``
states for the transcript core.

That mattered the moment there was a second renderer. ``SKILL_NAMES`` and
``bundled_skill_text`` used to live in ``platforms/claude_code/skills.py``, which
was fine while Claude Code was the only host. Adding ``platforms/agent_skills``
would have made one platform import another to learn what techne ships — a
dependency with no justification beyond where the code happened to be first.

WHAT A PROCEDURE IS. A portable ``SKILL.md``-shaped markdown file: YAML
frontmatter (``description`` at minimum) followed by the procedure itself. Each
renderer projects that one file into the layout its host expects — Claude Code
takes a flat ``commands/<name>.md``, the open Agent Skills standard takes a
``<name>/SKILL.md`` directory. A second host gets a renderer, never a second copy
of the procedure.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from techne.config import Config

#: Package data — hatchling ships every file under the package, the same way
#: inference/prototypes.yaml rides along.
_SKILL_DIR = Path(__file__).parent

#: The bundled procedures, by stem. All four drive techne's own CLI and know this
#: version's store layout, which is why they ship with the tool rather than from a
#: separate repo that would drift from it.
SKILL_NAMES = ("self-care", "techne-promote", "investigate-url", "survey-field")


def bundled_skill_text(name: str) -> str:
    """The shipped text of one procedure. Raises on an unknown name rather than
    returning empty, so a typo in a renderer's loop fails loudly."""
    if name not in SKILL_NAMES:
        raise ValueError(f"unknown skill {name!r}; known: {', '.join(SKILL_NAMES)}")
    return (_SKILL_DIR / f"{name}.md").read_text(encoding="utf-8")


def sha(text: str) -> str:
    """Content hash of a procedure, used by every renderer's ownership manifest to
    tell 'techne wrote this' from 'someone edited it'."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class SkillResult:
    """One procedure's outcome for one host. ``action`` is renderer-defined —
    installed | updated | unchanged | kept-user-copy | removed | missing | modified |
    stale — because what a render can do to a file is the same question everywhere."""

    name: str
    action: str
    path: Path
    detail: str = ""


def load_manifest(config: "Config", key: str) -> dict:
    """What techne last wrote for one host, by name -> sha.

    *key* is required rather than defaulted: every renderer owns a distinct key, and
    a shared default is how two hosts would end up overwriting each other's record of
    what they own. The ownership discipline this encodes is security-relevant, so it
    lives in ONE place — two copies is two places to fix the day it is wrong.
    """
    from techne.core.database import connect
    from techne.core.extension_state import get_state

    if not config.db_path.exists():
        return {}
    conn = connect(config.db_path)
    try:
        return get_state(conn, key) or {}
    finally:
        conn.close()


def save_manifest(config: "Config", manifest: dict, key: str) -> None:
    from techne.core.database import connect
    from techne.core.extension_state import set_state

    if not config.db_path.exists():
        return
    conn = connect(config.db_path)
    try:
        set_state(conn, key, manifest)
    finally:
        conn.close()
