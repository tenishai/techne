"""The agent-trigger convention: indexed so it fires, tested so it does not rot (#756/#757).

techne's failures are overwhelmingly silent, which is the class a human never thinks to ask
about — so parts of the docs are written as TRIGGERS: conditions under which an agent should
speak up unprompted. Two things were missing when the convention shipped in #755, both found
by Prudence reviewing it:

  #756  A trigger that lives only in a document fires only if that document was already
        open — the failure the documents were meant to fix, one level up. It needs an index
        on an always-loaded surface. `AGENTS.md` is that surface, and this file enforces the
        index BOTH DIRECTIONS, the same rule `check_arch_cards.py` applies to cards.

  #757  Architecture cards have `anchor:` shas; trigger blocks had nothing. A bullet saying
        "`techne doctor` does not check this" keeps firing, confidently, after that stops
        being true. What is mechanically checkable is checked here; what is not is MARKED,
        so the set does not read as uniformly verified.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AGENTS = REPO / "AGENTS.md"
TRIGGER_MARKER = "Agent: raise this unprompted"


def _slug(heading: str) -> str:
    """GitHub's heading-to-anchor slug, which is what the index links against."""
    text = heading.strip().lstrip("#").strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)  # drop backticks, punctuation
    return re.sub(r"\s", "-", text)


def _index_rows() -> list[tuple[str, str]]:
    """(doc path, anchor) for every row in the AGENTS.md trigger table."""
    section = AGENTS.read_text().split("## Agent triggers")[1].split("## Architecture cards")[0]
    return re.findall(r"\]\((docs/[^)#]+)#([^)]+)\)", section)


def _docs_with_trigger_blocks() -> set[str]:
    return {
        str(p.relative_to(REPO))
        for p in (REPO / "docs").rglob("*.md")
        if TRIGGER_MARKER in p.read_text()
    }


# --------------------------------------------------------------- #756: the index


def test_the_index_is_not_empty():
    """A guard that silently matches nothing is worse than no guard — every assertion below
    would pass vacuously if the table parse broke."""
    assert len(_index_rows()) >= 8, f"parsed {len(_index_rows())} trigger rows from AGENTS.md"


@pytest.mark.parametrize("doc,anchor", _index_rows())
def test_every_indexed_trigger_points_at_a_real_section(doc, anchor):
    """A row whose link does not resolve sends the agent nowhere, which is indistinguishable
    from having no row at all — except that it looks covered."""
    path = REPO / doc
    assert path.is_file(), f"{doc} does not exist"
    slugs = {_slug(line) for line in path.read_text().splitlines() if line.startswith("#")}
    assert anchor in slugs, f"{doc}#{anchor} resolves to no heading"


def test_every_document_with_a_trigger_block_is_indexed():
    """THE DIRECTION THAT MATTERS MOST (#756). An unindexed trigger is one that never fires,
    exactly as an unindexed arch card is one that is never protected — and both fail the same
    way: silently, looking finished."""
    indexed = {doc for doc, _ in _index_rows()}
    missing = _docs_with_trigger_blocks() - indexed

    assert not missing, f"trigger blocks with no row in AGENTS.md: {sorted(missing)}"


# --------------------------------------------------- #757: the claims that can rot

def test_the_command_triggers_tell_agents_to_offer_still_exists():
    """"offer `techne backup` instead" is a claim about this codebase, and it is the
    remedy every WAL trigger points at."""
    from click.testing import CliRunner

    from techne.cli import main

    result = CliRunner().invoke(main, ["backup", "--help"])

    assert result.exit_code == 0, "the triggers offer a command that no longer exists"
    assert "--keep" in result.output, "the scheduling trigger's `--keep` advice is stale"


def test_the_absence_the_backup_trigger_asserts_is_still_absent():
    """"`techne doctor` does not check this" is the sharpest kind of claim to let rot: if
    doctor GAINS the check, the trigger keeps telling operators it has not got it, and the
    agent talks them out of trusting a signal that now works."""
    from techne.core.checks import _REGISTRY

    # THE REGISTRY, NOT `--help` TEXT. The first version of this grepped `doctor --help` for
    # "schedul" and failed on the phrase "a scheduled audit" — a word matching for a reason
    # unrelated to the claim. That is the same defect #755 spent nine rounds on, so it is
    # worth naming here: a substring in prose is not evidence about behaviour. `_REGISTRY`
    # is what `run_checks` actually iterates, so it is the thing the claim is about.
    concerned = [
        fn.__name__
        for fn in _REGISTRY
        if any(word in fn.__name__ for word in ("backup", "schedul", "retention"))
    ]

    assert not concerned, (
        f"`techne doctor` may now check scheduling ({concerned}) — the trigger in "
        "deployment.md tells operators it does not. Re-read it."
    )


def test_the_absence_the_migration_trigger_asserts_is_still_absent():
    """"nothing archives transcripts" — an absence, which no amount of reading the code
    reminds you to re-check."""
    from click.testing import CliRunner

    from techne.cli import main

    result = CliRunner().invoke(main, ["--help"])

    assert "archive" not in result.output.lower(), (
        "a transcript-archive command may now exist — re-read the trigger in deployment.md"
    )


def test_the_restore_trigger_is_still_all_or_nothing():
    """"a single-db restore desyncs cross-db pointers" — the trigger's advice depends on
    `restore` having no per-domain escape hatch."""
    from click.testing import CliRunner

    from techne.cli import main

    result = CliRunner().invoke(main, ["restore", "--help"])

    assert "--domain" not in result.output, (
        "restore may now take a single domain — the all-or-nothing trigger is stale"
    )


# ------------------------------------------------- #757: what is NOT checked, said aloud

JUDGEMENT_BULLETS = 4  # the four WAL bullets in docs/db-per-domain.md


def test_the_unchecked_bullets_are_accounted_for():
    """WHAT IS NOT VERIFIED MUST NOT READ AS VERIFIED (#757). Four bullets rest on SQLite's
    own semantics — what a `-wal` file is, that copying a live db loses recent writes. They
    are not claims about this codebase, so nothing here can check them and they are not
    expected to rot. Stating the count keeps the set honest: adding a fifth unverifiable
    bullet has to be a deliberate act, not a silent one."""
    block = (REPO / "docs" / "db-per-domain.md").read_text()
    trigger_block = block.split(TRIGGER_MARKER)[1].split("\n\n")[0]
    bullets = [ln for ln in trigger_block.splitlines() if ln.startswith("> - ")]

    assert len(bullets) == JUDGEMENT_BULLETS, (
        f"{len(bullets)} WAL bullets, {JUDGEMENT_BULLETS} accounted for as judgement — "
        "if you added one, decide whether it is checkable and update this count"
    )
