"""The guides ship, and nothing can silently stop shipping one (techne#754).

WHY THIS FILE EXISTS. Before 0.16.0 the wheel carried code, two skills and two
tree-sitter grammars — no markdown. Every `Agent: raise this unprompted` block, and
the whole `AGENTS.md` trigger index, reached agents working ON techne and reached an
installed user's agent never. `techne guide` is the reader; these tests are what stop
the shipping half from rotting away under it.

THE FAILURE THESE GUARD IS SILENT IN BOTH DIRECTIONS. A guide dropped from
pyproject's force-include still WORKS in a source checkout, because `_search_dirs`
falls back to the repo — so a developer sees green while every installed user gets a
KeyError. And a name added to `_MANIFEST` but not to the wheel table looks fine until
someone installs. Only comparing the two tables catches either.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _force_included() -> dict[str, str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return (
        data.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("force-include", {})
    )


def test_the_wheel_actually_force_includes_markdown():
    """A precondition. Every assertion below passes vacuously if this table is empty,
    which is how a structural test quietly stops testing."""
    assert _force_included(), "no force-include table — the docs stopped shipping"


def test_every_manifest_name_is_in_the_wheel():
    """`techne guide <name>` on an INSTALLED techne can only find what the wheel carries.
    A name here that the build does not include works in a checkout and fails for users.

    TWO WAYS TO SHIP, and both count: `docs/*.md` ride in via force-include, while the two
    skill guides are already package data under `techne/skills/` (techne#641) and need no
    force-include entry. Requiring an entry for them would be wrong, not stricter."""
    from techne.docs import _MANIFEST, _SKILL_GUIDES

    shipped = {Path(dst).stem for dst in _force_included().values()} | set(_SKILL_GUIDES)
    missing = sorted(set(_MANIFEST) - shipped)
    assert not missing, f"in _MANIFEST but not shipped in the wheel: {missing}"


def test_every_shipped_doc_is_in_the_manifest():
    """The other direction: a file built into the wheel that `_MANIFEST` does not name is
    unreachable — `guide_path` rejects anything not in the tuple, so it is dead payload."""
    from techne.docs import _MANIFEST

    shipped = {Path(dst).stem for dst in _force_included().values()}
    orphans = sorted(shipped - set(_MANIFEST))
    assert not orphans, f"shipped but unreachable via `techne guide`: {orphans}"


@pytest.mark.parametrize("name", sorted(_force_included().values()))
def test_every_force_included_source_file_exists(name):
    """An anchor that resolves to nothing builds a wheel with a missing doc and no error."""
    src = [s for s, d in _force_included().items() if d == name][0]
    assert (REPO / src).is_file(), f"force-include names {src}, which does not exist"


def test_the_skill_guides_are_actually_package_data():
    """`_SKILL_GUIDES` claims these ship WITHOUT a force-include entry, because hatchling
    takes everything under the package. If that ever stops being true they vanish from an
    installed techne while a checkout keeps working — the silent half of this file's
    premise, asserted rather than assumed."""
    from techne.docs import _SKILL_GUIDES

    for name in _SKILL_GUIDES:
        assert (REPO / "techne" / "skills" / f"{name}.md").is_file(), name


def test_the_two_procedures_this_release_absorbed_are_reachable():
    """techne#754. `investigating` and `surveying` are the procedures that FILL the
    architect — the gap that made `architect add` a writer with no way to produce what it
    writes. If either stops shipping, the feeder has no feeder again."""
    from techne.docs import _MANIFEST, bundled_guide

    for name in ("investigating", "surveying"):
        assert name in _MANIFEST
        text = bundled_guide(name)
        # each must teach the store commands, or it is a procedure with no write step
        assert "techne architect dedup" in text, f"{name} does not teach the dedup check"
        assert "techne architect add" in text or "techne architect" in text


def test_the_guides_that_carry_triggers_all_ship():
    """The POINT of shipping docs. A trigger block whose document is repo-only is a trigger
    that cannot fire for the population it was written for — the same defect as an
    unindexed one (AGENTS.md's own rule), reached from the other side.

    Derived from the documents rather than hardcoded, so a new trigger-carrying guide is
    caught the moment it is added rather than the next time someone remembers this list.
    """
    from techne.docs import _MANIFEST

    marker = "**Agent: raise this unprompted.**"
    carriers = {
        p.stem
        for p in (REPO / "docs").glob("*.md")
        if marker in p.read_text(encoding="utf-8")
    }
    assert carriers, "no trigger blocks found — has the marker changed?"

    unshipped = sorted(carriers - set(_MANIFEST))
    assert not unshipped, f"these carry agent triggers but do not ship: {unshipped}"


def test_a_guide_name_cannot_escape_the_guide_directory():
    """`techne guide` passes a CLI argument into a path join. Membership in a fixed tuple
    is checked BEFORE the filesystem, so traversal is unrepresentable rather than filtered."""
    from techne.docs import guide_path

    for hostile in ("../../../etc/passwd", "/etc/passwd", "../pyproject", "..", ""):
        with pytest.raises(KeyError):
            guide_path(hostile)


def test_every_manifest_entry_is_readable_right_now():
    """Reads through the real accessor, so a checkout with a broken fallback fails here."""
    from techne.docs import _MANIFEST, bundled_guide

    for name in _MANIFEST:
        assert len(bundled_guide(name)) > 500, f"{name} resolved to something implausibly short"


def test_the_mcp_instructions_point_at_the_reader():
    """Shipping the docs without telling the agent they exist is dead payload. The MCP
    instructions field is the one surface that reaches every connected agent."""
    from techne.mcp_server import SERVER_INSTRUCTIONS

    assert "techne guide" in SERVER_INSTRUCTIONS
