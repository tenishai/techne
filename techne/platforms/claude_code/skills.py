"""Install techne's OPERATIONAL procedures into Claude Code (techne#641).

WHY THESE SHIP WITH THE TOOL AND THE RESEARCH SKILLS DO NOT. `self-care` and
`techne-promote` drive techne's own CLI and know this version's store layout —
`self-care` sweeps the outbox, prunes backups/snapshots and VACUUMs; `promote`
drives `reflect emit`/`ingest`. A procedure written against 0.12 can be WRONG for
0.14, so shipping them in a separate marketplace repo guarantees drift from the
tool they operate. `investigate-url` and `survey-field` ship with techne too, but not
through this installer: since 0.16.0 (#754) they are bundled guides read by
`techne guide investigating` / `techne guide surveying`, and a host gets a thin stub
that runs the guide, never a copy (#988). They are not in SKILL_NAMES: the stubs are
installed by `register_guide_stubs` below (#1040), as ``skills/<name>/SKILL.md`` rather
than ``commands/<name>.md``, because the stub loads its guide with a Claude Code
``!`command``` line and that form was verified on the skills path.

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


def _load_manifest(config: Config, key: str = STATE_KEY) -> dict:
    """What techne last wrote, by name -> sha. `key` is a parameter so the agent
    installer (techne#754) reuses this rather than duplicating it: the ownership
    discipline these two functions encode is security-relevant, and two copies of it
    is two places to fix the day it is wrong."""
    from techne.core.database import connect
    from techne.core.extension_state import get_state

    if not config.db_path.exists():
        return {}
    conn = connect(config.db_path)
    try:
        return get_state(conn, key) or {}
    finally:
        conn.close()


def _save_manifest(config: Config, manifest: dict, key: str = STATE_KEY) -> None:
    from techne.core.database import connect
    from techne.core.extension_state import set_state

    if not config.db_path.exists():
        return
    conn = connect(config.db_path)
    try:
        set_state(conn, key, manifest)
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
        path = target_dir / f"{name}.md"
        results.append(_install_owned(name, path, bundled_skill_text(name), manifest, force))

    _save_manifest(config, manifest)
    return results


def _install_owned(
    name: str, path: Path, shipped: str, manifest: dict, force: bool
) -> SkillResult:
    """Write *shipped* to *path* unless the user has edited what techne last wrote.

    One copy of the ownership rule, shared by the procedures and the guide stubs, so
    the day it is wrong there is one place to fix. Updates *manifest* in place."""
    shipped_sha = _sha(shipped)

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(shipped, encoding="utf-8")
        manifest[name] = shipped_sha
        return SkillResult(name, "installed", path)

    on_disk = path.read_text(encoding="utf-8")
    if _sha(on_disk) == shipped_sha:
        # Already current. Record the hash anyway: a pre-#641 machine has the
        # file with no manifest entry, and without this it would look
        # user-modified forever and never take an upgrade.
        manifest[name] = shipped_sha
        return SkillResult(name, "unchanged", path)

    last_written = manifest.get(name)
    if force or (last_written is not None and _sha(on_disk) == last_written):
        path.write_text(shipped, encoding="utf-8")
        manifest[name] = shipped_sha
        return SkillResult(name, "updated", path)
    return SkillResult(
        name,
        "kept-user-copy",
        path,
        detail=(
            "on-disk copy differs from what techne last wrote — left alone. "
            "Take the shipped version with `techne skills install --force` "
            "(or `techne init --force-skills`)."
        ),
    )


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


# --- Research-guide stubs (#1040) -------------------------------------------------
#
# WHY A STUB AND NOT THE GUIDE. `investigate-url` and `survey-field` are the guides
# behind `techne guide investigating|surveying` (#754). A host copy of the procedure
# drifts in both directions and nothing notices — that was #988, which cost a
# by-hand union reconcile of both guides against their host copies. A stub holds
# nothing that can go stale: its body is one injected command that prints the
# installed guide when the skill loads.
#
# WHY INJECTED. Claude Code runs a skill line of the form !`command` at load time and
# puts the output in its place, so the procedure is in context before the model reads
# anything and there is no "remember to run the guide" step to skip. Two rules come
# with it (verified 2026-09-25 under headless `claude -p` outside auto mode): the
# command must be pre-approved in `allowed-tools`, or the skill aborts; and a non-zero
# exit aborts it too, which the `|| echo` turns into a visible instruction instead.
# Injected output is not re-expanded, so a guide that quotes a !` line is safe.
#
# THE DESCRIPTION IS READ FROM THE GUIDE, not restated here: each guide carries the
# stub frontmatter it recommends, and a second spelling of it in this module would be
# exactly the kind of copy this stub exists to remove.

#: stub name -> (guide name, the argument label, extra allowed tools)
GUIDE_STUBS = {
    # NO clone pre-approval, deliberately (Prudence, #1042 r1-r2). A permission rule
    # is a prefix plus a wildcard, and git reads options AFTER the URL, so even
    # `git clone --depth 1 --quiet https://github.com/*` admits a trailing
    # `-c credential.helper=!…` or `--template=…`, both of which run commands. A deny
    # list of git options would be a hand-kept list again. Step 4.5's clone prompts
    # for each clone (one per surviving candidate), which is the moment the operator
    # should see it.
    "investigate-url": ("investigating", "URL and context", ()),
    "survey-field": ("surveying", "Field and context", ()),
}

#: extension_state key for the stubs. Separate from the procedures' key so removing
#: one set can never make techne believe it no longer owns the other.
GUIDE_STUB_STATE_KEY = "guide-stubs:claude-code:installed"

_BASE_TOOLS = (
    "Bash(techne guide *)",
    "Bash(techne architect *)",
    "Bash(techne agents status)",
    "Bash(date *)",
    "Bash(mktemp *)",
    "Read",
    "Write",
    "Edit",
    "Agent",
)


def guide_stubs_dir() -> Path:
    """Claude Code's user skills directory. A function, like skills_dir(), so
    CLAUDE_CONFIG_DIR and test redirection both flow through one place."""
    return claude_config_dir() / "skills"


def _guide_description(guide: str) -> str:
    """The `description:` line from the stub frontmatter the guide itself recommends."""
    from techne.docs import bundled_guide

    text = bundled_guide(guide)
    section = text.split("## Make this a skill on your platform", 1)
    if len(section) != 2:
        raise ValueError(f"guide {guide!r} has no 'Make this a skill' section")
    for line in section[1].splitlines():
        if line.startswith("description: "):
            desc = line[len("description: "):].strip()
            # Written plain when YAML reads it back unchanged, quoted otherwise. The
            # test is a YAML parser rather than a list of risky characters: a trailing
            # colon, a leading dash or a leading `#` each break or empty a plain scalar,
            # and a hand-kept list missed all three (Temperance, #1042). It is PyYAML's
            # reading; Claude Code's frontmatter parser is assumed, not shown, to agree.
            import json

            import yaml

            try:
                plain_ok = yaml.safe_load(f"description: {desc}") == {"description": desc}
            except yaml.YAMLError:
                plain_ok = False
            return desc if plain_ok else json.dumps(desc, ensure_ascii=False)
    raise ValueError(f"guide {guide!r} recommends no stub description")


def render_guide_stub(name: str) -> str:
    """The SKILL.md techne writes for one research guide."""
    if name not in GUIDE_STUBS:
        raise ValueError(f"unknown guide stub {name!r}; known: {', '.join(GUIDE_STUBS)}")
    guide, arg_label, extra_tools = GUIDE_STUBS[name]
    tools = ", ".join(_BASE_TOOLS + extra_tools)
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {_guide_description(guide)}\n"
        f"allowed-tools: {tools}\n"
        "---\n"
        "\n"
        f"Thin stub installed by techne. The procedure below is printed by the installed\n"
        f"techne when this skill loads (`techne guide {guide}`); it is not a copy, so do\n"
        "not paste it here. On a host that does not run the next line, run that command\n"
        "yourself and follow what it prints.\n"
        "\n"
        f"!`techne guide {guide} 2>&1 || echo \"PROCEDURE NOT LOADED: techne guide {guide} "
        "failed. Run it yourself; if it still fails, stop and tell the operator.\"`\n"
        "\n"
        f"{arg_label} from the operator: $ARGUMENTS\n"
    )


def _stub_path(name: str) -> Path:
    return guide_stubs_dir() / name / "SKILL.md"


def _enabled_plugins() -> dict:
    """`enabledPlugins` from Claude Code's settings, or {} when unreadable."""
    import json

    path = claude_config_dir() / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    plugins = data.get("enabledPlugins") if isinstance(data, dict) else None
    return plugins if isinstance(plugins, dict) else {}


def guide_stub_collisions(name: str) -> list[str]:
    """Other things answering to *name* on this host — the #988 failure was two
    copies of one procedure under one name, with nothing saying which one ran.
    Reported, never removed: they may be the user's.

    A floor, not a guarantee: it sees a legacy command file and a user-scope plugin
    whose NAME matches. A differently named plugin that ships a skill of this name,
    or a project-scope `.claude/skills/<name>`, is not detected."""
    found = []
    legacy = skills_dir() / f"{name}.md"
    if legacy.exists():
        found.append(f"command file {legacy}")
    for key, enabled in _enabled_plugins().items():
        if enabled and key.split("@", 1)[0] == name:
            found.append(f"enabled plugin {key}")
    return found


def _with_collisions(result: SkillResult) -> SkillResult:
    clashes = guide_stub_collisions(result.name)
    if clashes:
        note = (
            "also answering to this name: " + "; ".join(clashes)
            + " — retire it, or you have two procedures and no way to tell which ran"
        )
        result.detail = f"{result.detail} {note}".strip()
    return result


def register_guide_stubs(config: Config, *, force: bool = False) -> list[SkillResult]:
    """Write one stub skill per research guide, under the same ownership rule as the
    procedures: an edited stub is kept and reported unless *force*."""
    manifest = _load_manifest(config, GUIDE_STUB_STATE_KEY)
    results = [
        _with_collisions(
            _install_owned(name, _stub_path(name), render_guide_stub(name), manifest, force)
        )
        for name in GUIDE_STUBS
    ]
    _save_manifest(config, manifest, GUIDE_STUB_STATE_KEY)
    return results


def unregister_guide_stubs(config: Config) -> list[SkillResult]:
    """Remove only the stubs techne wrote and still owns, and their directory when
    that leaves it empty."""
    manifest = _load_manifest(config, GUIDE_STUB_STATE_KEY)
    results: list[SkillResult] = []
    for name in GUIDE_STUBS:
        path = _stub_path(name)
        if not path.exists():
            continue
        if _sha(path.read_text(encoding="utf-8")) == manifest.get(name):
            path.unlink()
            if not any(path.parent.iterdir()):
                path.parent.rmdir()
            manifest.pop(name, None)
            results.append(SkillResult(name, "removed", path))
        else:
            results.append(
                SkillResult(name, "kept-user-copy", path, detail="modified since install")
            )
    _save_manifest(config, manifest, GUIDE_STUB_STATE_KEY)
    return results


def guide_stubs_status(config: Config) -> list[SkillResult]:
    """Report each stub as current / outdated / user-modified / absent, plus anything
    else answering to its name. Writes nothing."""
    manifest = _load_manifest(config, GUIDE_STUB_STATE_KEY)
    out: list[SkillResult] = []
    for name in GUIDE_STUBS:
        path = _stub_path(name)
        if not path.exists():
            result = SkillResult(name, "absent", path)
        else:
            on_disk_sha = _sha(path.read_text(encoding="utf-8"))
            if on_disk_sha == _sha(render_guide_stub(name)):
                result = SkillResult(name, "current", path)
            elif on_disk_sha == manifest.get(name):
                result = SkillResult(name, "outdated", path, detail="techne's stub, older version")
            else:
                result = SkillResult(name, "user-modified", path)
        out.append(_with_collisions(result))
    return out
