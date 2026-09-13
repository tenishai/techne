"""Install techne's procedures for hosts reading the open Agent Skills standard.

WHY A SECOND RENDERER RATHER THAN A SECOND COPY. ``platforms/claude_code/skills.py``
writes a flat ``commands/<name>.md``; the Agent Skills standard wants a directory
per skill holding ``SKILL.md``. Same procedure, two layouts. The text comes from
``techne.skills`` in both cases, so a fix to a procedure reaches every host at once
and neither renderer can drift from the other.

This ports what ``tenishai/skills/install/install-elsewhere.sh`` did before these
procedures moved into the wheel. Two things deliberately did NOT come with it: the
``--copy`` vs symlink choice (techne always writes real files, because a symlink
into a wheel breaks the moment the wheel is upgraded or removed) and the
``skill-store`` shim it installed (these procedures drive ``techne architect``
directly, so the tool they need is already present by definition).

WHERE THEY GO, and why this is a guess we make explicit. There is no registry of
where each host reads skills from; the paths below were taken from that installer,
which took them from each project's docs. A host that changes its mind will silently
stop seeing the procedures, which is why ``skills_dir`` takes an explicit target and
``--target`` exists on the CLI. Treat these as defaults, never as a contract:

    opencode   ~/.config/opencode/skills
    crush      ~/.agents/skills
    goose      ~/.config/agents/skills
    generic    ~/.agents/skills      (the cross-agent path crush and goose both read)

OWNERSHIP IS NOT ASSUMED, exactly as in the Claude Code renderer: techne records the
content hash of what it last wrote, and a file that no longer matches is left alone
and reported rather than overwritten. The manifest key differs per platform, so the
same procedure installed to two hosts is tracked independently — editing your
opencode copy must not make techne think the Claude Code copy diverged.
"""

from dataclasses import dataclass
from pathlib import Path

from techne.config import Config
from techne.skills import (
    SKILL_NAMES,
    SkillResult,
    bundled_skill_text,
    load_manifest,
    save_manifest,
    sha,
)

#: extension_state key holding {name: sha256} of what techne last wrote HERE. Distinct
#: from the Claude Code key so the two installs cannot be confused for one another.
STATE_KEY = "skills:agent-skills:installed"

#: Default target per host. Defaults, not a contract — see the module docstring.
HOST_DIRS: dict[str, str] = {
    "opencode": "~/.config/opencode/skills",
    "crush": "~/.agents/skills",
    "goose": "~/.config/agents/skills",
    "generic": "~/.agents/skills",
}

DEFAULT_HOST = "generic"


@dataclass(frozen=True)
class Target:
    """Where a render lands, and which host's convention produced it."""

    host: str
    root: Path


def skills_dir(host: str = DEFAULT_HOST, target: Path | None = None) -> Target:
    """Resolve the directory to write into.

    An explicit *target* always wins — the host names are conveniences, and a user
    whose host reads somewhere else needs a way to say so that does not require us
    to have anticipated their host.
    """
    if target is not None:
        return Target(host, Path(target).expanduser())
    if host not in HOST_DIRS:
        raise ValueError(f"unknown host {host!r}; known: {', '.join(sorted(HOST_DIRS))}")
    return Target(host, Path(HOST_DIRS[host]).expanduser())


def _skill_path(root: Path, name: str) -> Path:
    """``<root>/<name>/SKILL.md`` — the Agent Skills layout."""
    return root / name / "SKILL.md"


def register_skills(
    config: Config,
    *,
    host: str = DEFAULT_HOST,
    target: Path | None = None,
    force: bool = False,
) -> list[SkillResult]:
    """Write every bundled procedure into an Agent Skills tree.

    Returns one SkillResult per procedure. A user-modified file is KEPT and reported
    as ``kept-user-copy`` unless *force* — the same discipline the Claude Code
    renderer applies, for the same reason: a procedure is a document someone may have
    tuned, and silently reverting that is the kind of quiet destruction techne exists
    to make visible.
    """
    dest = skills_dir(host, target)
    manifest = load_manifest(config, STATE_KEY)
    results: list[SkillResult] = []

    for name in SKILL_NAMES:
        shipped = bundled_skill_text(name)
        path = _skill_path(dest.root, name)
        shipped_sha = sha(shipped)

        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(shipped, encoding="utf-8")
            manifest[name] = shipped_sha
            results.append(SkillResult(name, "installed", path))
            continue

        on_disk = path.read_text(encoding="utf-8")
        if sha(on_disk) == shipped_sha:
            # Already current. Record the hash anyway — a tree installed by the old
            # shell script has the file with no manifest entry, and without this it
            # would read as user-modified forever and never take an upgrade.
            manifest[name] = shipped_sha
            results.append(SkillResult(name, "unchanged", path))
            continue

        last_written = manifest.get(name)
        if force or (last_written is not None and sha(on_disk) == last_written):
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
                        "Take the shipped version with "
                        "`techne skills install --platform agent-skills --force`."
                    ),
                )
            )

    save_manifest(config, manifest, STATE_KEY)
    return results


def unregister_skills(
    config: Config, *, host: str = DEFAULT_HOST, target: Path | None = None
) -> list[SkillResult]:
    """Remove ONLY the copies techne wrote and still owns.

    A user-modified file is kept: uninstalling techne is not a licence to delete
    someone's edited document. An emptied skill directory is removed too, since an
    empty ``<name>/`` left behind reads to a host as a broken skill.
    """
    dest = skills_dir(host, target)
    manifest = load_manifest(config, STATE_KEY)
    results: list[SkillResult] = []

    for name in list(manifest):
        path = _skill_path(dest.root, name)
        if not path.exists():
            manifest.pop(name, None)
            continue
        if sha(path.read_text(encoding="utf-8")) == manifest[name]:
            path.unlink()
            if path.parent.is_dir() and not any(path.parent.iterdir()):
                path.parent.rmdir()
            manifest.pop(name, None)
            results.append(SkillResult(name, "removed", path))
        else:
            results.append(
                SkillResult(
                    name,
                    "kept-user-copy",
                    path,
                    detail="on-disk copy differs from what techne wrote — left in place.",
                )
            )

    save_manifest(config, manifest, STATE_KEY)
    return results


def skills_status(
    config: Config, *, host: str = DEFAULT_HOST, target: Path | None = None
) -> list[SkillResult]:
    """Report without writing anything.

    Uses the SAME vocabulary as the Claude Code renderer — current | outdated |
    user-modified | absent — because `SkillResult` is now shared and two renderers
    describing the same state with different words is a reporting bug waiting to
    happen in whatever aggregates them."""
    dest = skills_dir(host, target)
    manifest = load_manifest(config, STATE_KEY)
    results: list[SkillResult] = []

    for name in SKILL_NAMES:
        path = _skill_path(dest.root, name)
        shipped_sha = sha(bundled_skill_text(name))
        if not path.exists():
            results.append(SkillResult(name, "absent", path))
            continue
        on_disk_sha = sha(path.read_text(encoding="utf-8"))
        if on_disk_sha == shipped_sha:
            results.append(SkillResult(name, "current", path))
        elif manifest.get(name) == on_disk_sha:
            results.append(
                SkillResult(name, "outdated", path, detail="techne ships a newer version")
            )
        else:
            results.append(
                SkillResult(name, "user-modified", path, detail="edited since techne wrote it")
            )
    return results
