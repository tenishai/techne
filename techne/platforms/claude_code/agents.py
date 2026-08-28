"""Install techne's read-only research subagent into Claude Code (techne#754).

WHY THIS SHIPS AND THE PROCEDURES DO NOT WORK WITHOUT IT. `techne guide investigating`
tells an agent to do its fetching inside a subagent restricted to web-only tools, and
that instruction is unenforceable if the operator has no such subagent defined. They
would reach for a general research agent instead — one that has a shell — which is the
exact configuration the sandbox exists to prevent: an agent that can both read
attacker-controlled text and write to the pattern store is one injection away from
writing whatever that text says, and afterwards nobody can separate agent-authored
content from injection-authored content.

A DEFINITION CANNOT BE A STUB, and this is the one place the guide-stub trick does not
apply. A procedure can say "run `techne guide <name>` and follow it", because an agent
reads it at run time. An agent definition is configuration the HARNESS parses before any
agent exists — the security property is the literal `tools: WebFetch, WebSearch` line in
the frontmatter. Point it at a guide and there is nothing to enforce. So this renders a
real file, and the ownership discipline below is what keeps that honest.

VENDOR NEUTRALITY IS PRESERVED, not violated (techne#499), on exactly the argument
`skills.py` makes: techne owns the CONSTRAINT as portable markdown in `techne/agents/`;
this module renders it into the one layout Claude Code reads. A second platform gets its
own renderer, not a second copy of the constraint.

OWNERSHIP IS NOT ASSUMED. Like the skills installer and unlike `register_hooks`, this
never overwrites a file the operator has edited: the sha of what techne last wrote is
recorded, and a file that no longer matches it is left alone and reported. Silently
restoring our own version of someone's tuned subagent is the quiet destruction techne
exists to make visible.

BUT THE REPORT IS LOUDER HERE THAN FOR A SKILL, because a diverged copy of THIS file can
be diverged in the one direction that matters — a `tools:` line someone widened. A
procedure that drifts gives worse research; a sandbox that drifts gives an agent with a
shell and a fetch tool. `agents_status` says so rather than reporting a bare
"kept-user-copy".
"""

from dataclasses import dataclass
from pathlib import Path

from techne.config import Config
from techne.platforms.claude_code.paths import claude_config_dir
from techne.platforms.claude_code.skills import _load_manifest, _save_manifest, _sha

#: Package data, shipped the way techne/skills/*.md is.
_AGENT_DIR = Path(__file__).parent.parent.parent / "agents"
AGENT_NAMES = ("research-readonly",)

#: extension_state key holding {name: sha256} of what techne last wrote. Separate from
#: the skills key so uninstalling one never reasons about the other's files.
STATE_KEY = "agents:claude-code:installed"

#: The frontmatter line the sandbox IS. Checked rather than assumed — see
#: `agents_status` and tests/test_agent_definitions.py.
REQUIRED_TOOLS_LINE = "tools: WebFetch, WebSearch"


def _sandbox_intact(text: str) -> bool:
    """Does this file still grant EXACTLY the two web tools?

    SUBSTRING MATCHING IS WRONG HERE AND FAILS OPEN, which is the worst direction for a
    security check. `REQUIRED_TOOLS_LINE in text` is True for a file whose line reads
    `tools: WebFetch, WebSearch, Bash` — the widening is a SUFFIX, so the check passes
    on exactly the edit it exists to catch. Caught by its own test, which is the only
    reason it is not still in here.

    So: find the `tools:` line and compare it whole, after stripping. A file with no
    `tools:` line at all is not intact either — the harness would fall back to whatever
    its default grant is, which is not two web tools.
    """
    for line in text.splitlines():
        if line.strip().startswith("tools:"):
            return line.strip() == REQUIRED_TOOLS_LINE
    return False


@dataclass
class AgentResult:
    """One definition's outcome: installed | updated | unchanged | kept-user-copy."""

    name: str
    action: str
    path: Path
    detail: str = ""


def agents_dir() -> Path:
    """Target directory, derived from claude_config_dir() so CLAUDE_CONFIG_DIR and test
    redirection flow through one place — the same reason skills_dir() does."""
    return claude_config_dir() / "agents"


def bundled_agent_text(name: str) -> str:
    if name not in AGENT_NAMES:
        raise ValueError(f"unknown agent {name!r}; known: {', '.join(AGENT_NAMES)}")
    return (_AGENT_DIR / f"{name}.md").read_text(encoding="utf-8")


def register_agents(config: Config, *, force: bool = False) -> list[AgentResult]:
    """Write the bundled subagent definitions into Claude Code's agents directory."""
    target_dir = agents_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(config, STATE_KEY)
    results: list[AgentResult] = []

    for name in AGENT_NAMES:
        shipped = bundled_agent_text(name)
        path = target_dir / f"{name}.md"
        shipped_sha = _sha(shipped)

        if not path.exists():
            path.write_text(shipped, encoding="utf-8")
            manifest[name] = shipped_sha
            results.append(AgentResult(name, "installed", path))
            continue

        on_disk = path.read_text(encoding="utf-8")
        if _sha(on_disk) == shipped_sha:
            # Record the hash even when unchanged: a machine that had this file before
            # techne shipped it has no manifest entry, and without this it would read as
            # user-modified forever and never take an upgrade.
            manifest[name] = shipped_sha
            results.append(AgentResult(name, "unchanged", path))
            continue

        last_written = manifest.get(name)
        if force or (last_written is not None and _sha(on_disk) == last_written):
            path.write_text(shipped, encoding="utf-8")
            manifest[name] = shipped_sha
            results.append(AgentResult(name, "updated", path))
        else:
            widened = not _sandbox_intact(on_disk)
            detail = (
                "on-disk copy differs from what techne last wrote — left alone. "
                "Take the shipped version with `techne agents install --force`."
            )
            if widened:
                # The one divergence worth shouting about. A skill that drifts gives
                # worse research; a sandbox that drifts can give an agent a shell.
                detail = (
                    "ON-DISK COPY NO LONGER CARRIES `" + REQUIRED_TOOLS_LINE + "`. This "
                    "file IS the sandbox `techne guide investigating` relies on — if its "
                    "tool list was widened, the research subagent can do more than fetch, "
                    "and a page it reads can reach whatever those tools touch. Review it, "
                    "then `techne agents install --force` to restore."
                )
            results.append(AgentResult(name, "kept-user-copy", path, detail=detail))

    _save_manifest(config, manifest, STATE_KEY)
    return results


def unregister_agents(config: Config) -> list[AgentResult]:
    """Remove only the copies techne wrote and still owns. A modified file is kept:
    uninstalling techne is not a licence to delete someone's edited definition."""
    manifest = _load_manifest(config, STATE_KEY)
    results: list[AgentResult] = []
    for name in AGENT_NAMES:
        path = agents_dir() / f"{name}.md"
        if not path.exists():
            continue
        if _sha(path.read_text(encoding="utf-8")) == manifest.get(name):
            path.unlink()
            manifest.pop(name, None)
            results.append(AgentResult(name, "removed", path))
        else:
            results.append(
                AgentResult(name, "kept-user-copy", path, detail="modified since install")
            )
    _save_manifest(config, manifest, STATE_KEY)
    return results


def agents_status(config: Config) -> list[AgentResult]:
    """Report without writing. Answers the question a config flag cannot: is the sandbox
    the investigating guide depends on actually present on this machine, and does it
    still say what it has to say?"""
    manifest = _load_manifest(config, STATE_KEY)
    out: list[AgentResult] = []
    for name in AGENT_NAMES:
        path = agents_dir() / f"{name}.md"
        if not path.exists():
            out.append(
                AgentResult(
                    name,
                    "absent",
                    path,
                    detail=(
                        "`techne guide investigating` tells an agent to research inside "
                        "this sandbox, and it is not installed — so that instruction "
                        "cannot be followed. `techne agents install`."
                    ),
                )
            )
            continue
        on_disk = path.read_text(encoding="utf-8")
        if _sha(on_disk) == _sha(bundled_agent_text(name)):
            out.append(AgentResult(name, "unchanged", path))
        elif not _sandbox_intact(on_disk):
            out.append(
                AgentResult(
                    name,
                    "widened",
                    path,
                    detail=(
                        "the tool list no longer matches what techne shipped — this file "
                        "IS the sandbox, so review it before running an investigation."
                    ),
                )
            )
        else:
            out.append(AgentResult(name, "kept-user-copy", path, detail="edited, sandbox intact"))
    return out
