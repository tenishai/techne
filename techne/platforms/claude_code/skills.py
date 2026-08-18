"""Install techne's OPERATIONAL procedures into Claude Code (techne#641).

WHY THESE SHIP WITH THE TOOL AND THE RESEARCH SKILLS DO NOT. `self-care` and
`techne-promote` drive techne's own CLI and know this version's store layout —
`self-care` sweeps the outbox, prunes backups/snapshots and VACUUMs; `promote`
drives `reflect emit`/`ingest`. A procedure written against 0.12 can be WRONG for
0.14, so shipping them in a separate marketplace repo guarantees drift from the
tool they operate. `investigate-url` and `survey-field` have no such coupling and
correctly live in tenishai/skills.

VENDOR NEUTRALITY IS PRESERVED, not violated (techne#499): techne owns the
PROCEDURE as a portable ``SKILL.md``-shaped markdown file; this module renders it
into the layout ONE platform expects. A second platform gets its own renderer,
not a second copy of the procedure.

WHERE THEY GO. ``<claude config dir>/commands/`` — established empirically: that
is where the working copies live on a machine that runs both today. Claude Code's
own discovery contract for this directory is not something techne can pin, so the
installer treats it as a target it writes, never as an API it depends on.

OWNERSHIP IS NOT ASSUMED. Unlike ``register_hooks``, which strips and rewrites the
settings block it owns, this NEVER overwrites a file the user has edited: the
content hash of what techne last wrote is recorded, and a file that no longer
matches it is left alone and reported. A hook block is techne's plumbing; a
procedure is a document someone may have tuned, and silently reverting that is
the kind of quiet destruction techne exists to make visible.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from techne.config import Config
from techne.platforms.claude_code.paths import claude_config_dir

#: The bundled procedures, by installed filename. Package data — hatchling ships
#: every file under the package, the same way inference/prototypes.yaml rides along.
_SKILL_DIR = Path(__file__).parent.parent.parent / "skills"
SKILL_NAMES = ("self-care", "techne-promote")

#: extension_state key holding {name: sha256} of what techne last wrote.
STATE_KEY = "skills:claude-code:installed"


@dataclass
class SkillResult:
    """One skill's outcome. ``action`` is installed | updated | unchanged | kept-user-copy."""

    name: str
    action: str
    path: Path
    detail: str = ""


def skills_dir() -> Path:
    """Target directory. Derived from claude_config_dir() so CLAUDE_CONFIG_DIR and
    test redirection both flow through one place, exactly as SETTINGS_PATH does."""
    return claude_config_dir() / "commands"


def bundled_skill_text(name: str) -> str:
    if name not in SKILL_NAMES:
        raise ValueError(f"unknown skill {name!r}; known: {', '.join(SKILL_NAMES)}")
    return (_SKILL_DIR / f"{name}.md").read_text(encoding="utf-8")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_manifest(config: Config) -> dict:
    from techne.core.database import connect
    from techne.core.extension_state import get_state

    if not config.db_path.exists():
        return {}
    conn = connect(config.db_path)
    try:
        return get_state(conn, STATE_KEY) or {}
    finally:
        conn.close()


def _save_manifest(config: Config, manifest: dict) -> None:
    from techne.core.database import connect
    from techne.core.extension_state import set_state

    if not config.db_path.exists():
        return
    conn = connect(config.db_path)
    try:
        set_state(conn, STATE_KEY, manifest)
    finally:
        conn.close()


def register_skills(config: Config, *, force: bool = False) -> list[SkillResult]:
    """Write the bundled procedures into Claude Code's commands directory.

    Returns one SkillResult per skill. A user-modified file is KEPT and reported
    as ``kept-user-copy`` unless *force* — techne would rather tell you your copy
    diverged than silently restore its own.
    """
    target_dir = skills_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(config)
    results: list[SkillResult] = []

    for name in SKILL_NAMES:
        shipped = bundled_skill_text(name)
        path = target_dir / f"{name}.md"
        shipped_sha = _sha(shipped)

        if not path.exists():
            path.write_text(shipped, encoding="utf-8")
            manifest[name] = shipped_sha
            results.append(SkillResult(name, "installed", path))
            continue

        on_disk = path.read_text(encoding="utf-8")
        if _sha(on_disk) == shipped_sha:
            # Already current. Record the hash anyway: a pre-#641 machine has the
            # file with no manifest entry, and without this it would look
            # user-modified forever and never take an upgrade.
            manifest[name] = shipped_sha
            results.append(SkillResult(name, "unchanged", path))
            continue

        last_written = manifest.get(name)
        if force or (last_written is not None and _sha(on_disk) == last_written):
            path.write_text(shipped, encoding="utf-8")
            manifest[name] = shipped_sha
            results.append(SkillResult(name, "updated", path))
        else:
            results.append(
                SkillResult(
                    name,
                    "kept-user-copy",
                    path,
                    detail=(
                        "on-disk copy differs from what techne last wrote — left alone. "
                        "Take the shipped version with `techne skills install --force` "
                        "(or `techne init --force-skills`)."
                    ),
                )
            )

    _save_manifest(config, manifest)
    return results


def unregister_skills(config: Config) -> list[SkillResult]:
    """Remove ONLY the copies techne wrote and still owns. A user-modified file is
    kept: uninstalling techne is not a licence to delete someone's edited document."""
    manifest = _load_manifest(config)
    results: list[SkillResult] = []
    for name in SKILL_NAMES:
        path = skills_dir() / f"{name}.md"
        if not path.exists():
            continue
        if _sha(path.read_text(encoding="utf-8")) == manifest.get(name):
            path.unlink()
            manifest.pop(name, None)
            results.append(SkillResult(name, "removed", path))
        else:
            results.append(
                SkillResult(name, "kept-user-copy", path, detail="modified since install")
            )
    _save_manifest(config, manifest)
    return results


def skills_status(config: Config) -> list[SkillResult]:
    """Report without writing anything — what #638 needs to answer 'is the hygiene
    pass actually present on this install', which a config flag cannot tell you."""
    manifest = _load_manifest(config)
    out: list[SkillResult] = []
    for name in SKILL_NAMES:
        path = skills_dir() / f"{name}.md"
        if not path.exists():
            out.append(SkillResult(name, "absent", path))
            continue
        on_disk_sha = _sha(path.read_text(encoding="utf-8"))
        if on_disk_sha == _sha(bundled_skill_text(name)):
            out.append(SkillResult(name, "current", path))
        elif on_disk_sha == manifest.get(name):
            out.append(SkillResult(name, "outdated", path, detail="techne's copy, older version"))
        else:
            out.append(SkillResult(name, "user-modified", path))
    return out
