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


#: The tool grant that IS the sandbox, as a set — compared against what a YAML parser
#: resolves, not against a string that resembles it.
REQUIRED_TOOLS = frozenset({"WebFetch", "WebSearch"})


def _frontmatter(text: str) -> dict | None:
    """The YAML block between the first two `---` fences, parsed. None if absent/invalid.

    SLICING IS HALF THE FIX. A checker that scans the WHOLE FILE counts a `tools:` line
    that appears in the markdown body, or inside a `description: |` block scalar, as
    though it were the grant — and both of those are things an operator writes on
    purpose, so neither needs an adversary to happen.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                import yaml

                block = yaml.safe_load("\n".join(lines[1:i]))
            except Exception:
                return None
            return block if isinstance(block, dict) else None
    return None


def _declared_tools(text: str) -> frozenset[str] | None:
    """The tool names this definition grants, or None if it grants nothing parseable.

    Accepts BOTH spellings a human writes — the flow scalar `tools: A, B` and the YAML
    sequence `tools:\n  - A\n  - B` — because they mean the same thing to the harness
    and a checker that understands only one reports the other as tampered.
    """
    fm = _frontmatter(text)
    if fm is None or "tools" not in fm:
        return None
    raw = fm["tools"]
    if isinstance(raw, str):
        return frozenset(t.strip() for t in raw.split(",") if t.strip())
    if isinstance(raw, (list, tuple)):
        return frozenset(str(t).strip() for t in raw if str(t).strip())
    return None


def _sandbox_intact(text: str) -> bool:
    """Does this file grant EXACTLY the two web tools?

    THIS FUNCTION HAS FAILED OPEN TWICE, and both times in a way that read as correct.
    First it was `REQUIRED_TOOLS_LINE in text`, which is True for `tools: WebFetch,
    WebSearch, Bash` because the widening is a SUFFIX. The replacement compared the
    stripped `tools:` line, and Prudence broke it four more ways (#807 r1 I1), all from
    one root cause — it scanned lines instead of parsing the document:

      - `strip()` discards indentation, so a `tools:` nested under `metadata:` counted;
      - there was no frontmatter boundary, so a `tools:` line in the BODY counted;
      - it returned the FIRST match while YAML resolves a duplicate key to the LAST;
      - it understood only the flow spelling, so the sequence form read as widened.

    The first two need no adversary — an operator restructuring their own frontmatter, or
    documenting the tool line in prose, got `sandbox intact` printed over a definition
    granting a shell. So: parse the frontmatter and compare a SET against what a YAML
    parser resolves, which is what the harness reads. A file that grants nothing is not
    intact either — the harness would fall back to its own default, which is not two web
    tools.
    """
    return _declared_tools(text) == REQUIRED_TOOLS


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
        elif _sha(on_disk) == manifest.get(name):
            # TECHNE SHIPPED A NEW VERSION; THIS INSTALL HAS NOT TAKEN IT. The file is
            # byte-identical to what techne last wrote, so nobody edited it — and
            # reporting that as "edited" told every operator who upgraded without
            # re-running `agents install` that they had changed a file they never
            # touched, on the command that exists to say whether the sandbox is
            # trustworthy. `register_agents` already made this comparison; status
            # loaded the manifest and then ignored it (Prudence, #807 r1 I2).
            out.append(
                AgentResult(
                    name,
                    "outdated",
                    path,
                    detail=(
                        "techne ships a newer version and this copy is the one it wrote "
                        "— not an edit of yours. `techne agents install` takes it."
                    ),
                )
            )
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
