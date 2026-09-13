"""Install techne's OPERATIONAL procedures into Claude Code (techne#641).

WHY THESE SHIP WITH THE TOOL. Every procedure here drives techne's own CLI and
knows this version's store layout — `self-care` sweeps the outbox, prunes
backups/snapshots and VACUUMs; `promote` drives `reflect emit`/`ingest`;
`investigate-url` and `survey-field` drive `architect ingest`/`tag`/`topic`/
`reverdict`/`dedup` and read architect.db's schema directly. A procedure written
against 0.12 can be WRONG for 0.14, so shipping any of them in a separate
marketplace repo guarantees drift from the tool they operate.

THE RESEARCH SKILLS MOVED HERE 2026-09-13, and the reason is worth keeping
because this docstring previously argued the opposite. It claimed
`investigate-url` and `survey-field` "have no such coupling and correctly live
in tenishai/skills". That was true of the version published there, which reaches
the store through a pluggable `skill-store` CLI and names no backend. It was
never true of the versions actually in use: measured before the move,
`investigate-url` carried 13 `techne architect` invocations, 4 hardcoded
`~/.techne/architect.db` paths and 6 raw `sqlite3` calls; `survey-field` carried
4 and 4. Both failed this docstring's own test comprehensively.

The abstraction was the thing to drop, not the coupling. These procedures encode
how we want the architect store to be USED — the dedup query that knows
`source_url` is not in the FTS index, the verdict tiers, the supersede
discipline — and a backend-agnostic wrapper cannot express any of it without
either leaking the schema anyway or flattening it to a lowest common
denominator. So they belong to the tool whose store they describe.

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

from dataclasses import dataclass
from pathlib import Path

from techne.config import Config
from techne.skills import SKILL_NAMES as _SKILL_NAMES
from techne.skills import bundled_skill_text as _bundled_skill_text
from techne.skills import SkillResult as _SkillResult
from techne.skills import load_manifest as _neutral_load
from techne.skills import save_manifest as _neutral_save
from techne.skills import sha as _neutral_sha
from techne.platforms.claude_code.paths import claude_config_dir

#: Re-exported from techne.skills so existing importers keep working. The names and
#: the text live there because they are neutral — see that module's docstring.
SKILL_NAMES = _SKILL_NAMES

#: extension_state key holding {name: sha256} of what techne last wrote.
STATE_KEY = "skills:claude-code:installed"


#: Re-exported; the dataclass is neutral and lives in techne.skills.
SkillResult = _SkillResult


def skills_dir() -> Path:
    """Target directory. Derived from claude_config_dir() so CLAUDE_CONFIG_DIR and
    test redirection both flow through one place, exactly as SETTINGS_PATH does."""
    return claude_config_dir() / "commands"


def bundled_skill_text(name: str) -> str:
    """Thin alias over the neutral loader, kept because callers and tests import it
    from here."""
    return _bundled_skill_text(name)


def _sha(text: str) -> str:
    return _neutral_sha(text)


def _load_manifest(config: Config, key: str = STATE_KEY) -> dict:
    """Thin alias over the neutral loader, defaulting to THIS platform's key. Kept
    because the agent installer (techne#754) imports it from here."""
    return _neutral_load(config, key)


def _save_manifest(config: Config, manifest: dict, key: str = STATE_KEY) -> None:
    _neutral_save(config, manifest, key)


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
