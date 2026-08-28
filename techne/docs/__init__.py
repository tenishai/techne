"""The operator-facing guides that ship inside the wheel (techne#754).

WHY THE PACKAGE CARRIES DOCS AT ALL. techne's agent education is markdown: the
`Agent: raise this unprompted` blocks in these files are how an agent learns to
name a silent failure *before* the operator concludes something from it. None of
it shipped before 0.16.0 — the wheel held code, two skills and two tree-sitter
grammars — so every trigger reached agents working ON techne and reached an
installed user's agent never. An `AGENTS.md` row pointing at `docs/reflect-guide.md`
resolves to nothing on a machine that has the package and not the repo.

THERE IS ONE COPY OF EACH FILE, AND IT LIVES IN THE REPO. `pyproject.toml`'s
force-include maps `docs/*.md` and `AGENTS.md` into `techne/docs/` at BUILD time,
so an installed wheel finds them beside this module. A source checkout has no such
copy — the files are still up at the repo root — so `_search_dirs` falls back to
the checkout rather than vendoring a second copy into the package tree. Two
spellings of one document is the techne#429 hazard, and a guide is exactly the
kind of file that would drift silently: nothing fails when the stale copy is the
one that gets read.

WHY `guide_names()` READS THE DIRECTORY instead of restating a list: a hardcoded
list drifts from the force-include table the first time someone adds a file to one
and not the other — the unindexed-trigger failure this repo already gates against,
one layer down.
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent


def _search_dirs() -> list[Path]:
    """Where a guide may live: the installed package first, then a source checkout.

    Installed order matters. A developer running from a checkout that ALSO has a
    built copy beside the module should read the packaged one, because that is what
    their users will get — a guide that renders correctly only from the repo is a
    guide whose defects ship invisibly.
    """
    dirs = [_DIR, _DIR.parent / "skills"]
    repo = _DIR.parent.parent  # techne/docs -> techne -> repo root, in a checkout
    if (repo / "pyproject.toml").is_file():
        dirs.extend([repo / "docs", repo])
    return dirs


def guide_names() -> list[str]:
    """Every bundled guide, by the name `techne guide <name>` accepts."""
    from techne.docs import _MANIFEST

    names = set()
    for d in _search_dirs():
        for stem in _MANIFEST:
            if (d / f"{stem}.md").is_file():
                names.add(stem)
    return sorted(names)


#: The guides that ship. Mirrors pyproject.toml's force-include table, and
#: `tests/test_bundled_docs.py` fails if the two disagree — so adding a doc to the
#: wheel without adding it here (or the reverse) cannot pass silently.
_MANIFEST = (
    "AGENTS",
    "db-per-domain",
    "deployment",
    "four-functions",
    "investigating",
    "mcp-tools",
    "reflect-guide",
    "self-care",
    "surveying",
    "techne-promote",
    "welcome",
)

#: Guides that live in `techne/skills/` rather than `docs/` because they are ALSO
#: installed as platform skills (techne#641). Exposing them here as well is not a
#: second copy — it is the same file, read through a second door.
#:
#: THE DOOR MATTERS FOR THESE TWO MORE THAN FOR THE OTHERS. #641 bundles them
#: because they are version-coupled: `self-care` sweeps the outbox and VACUUMs,
#: `techne-promote` drives reflect emit/ingest, and a procedure written against
#: 0.12 can be WRONG for 0.14. But `skills install` COPIES them into the host's
#: command directory, where the copy goes stale the moment techne upgrades — the
#: drift check reports that, which is detection, not freshness. `techne guide
#: self-care` reads the installed version every time, so it cannot drift at all.
_SKILL_GUIDES = ("self-care", "techne-promote")


def guide_path(name: str) -> Path:
    """Resolve a guide name to its file, or raise KeyError."""
    # Check the manifest BEFORE touching the filesystem: `name` is a CLI argument,
    # and a path built by joining user input is how `../../etc/passwd` gets read.
    # Membership in a fixed tuple makes traversal unrepresentable rather than
    # merely filtered.
    if name not in _MANIFEST:
        raise KeyError(name)
    for d in _search_dirs():
        candidate = d / f"{name}.md"
        if candidate.is_file():
            return candidate
    raise KeyError(name)


def bundled_guide(name: str) -> str:
    """The text of one bundled guide."""
    return guide_path(name).read_text(encoding="utf-8")
