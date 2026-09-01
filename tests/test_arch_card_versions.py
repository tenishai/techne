"""An arch card that names a schema version must name the CURRENT one.

`docs/arch/codex.md`'s SEAM line said `CODEX_VERSION=3` from the v2->v3 era until
techne#872 — through the whole of #679, which SHIPPED v4 and whose own paragraph
sits six lines below that line in the same card. The card contradicted itself and
the code, and nothing noticed for weeks.

`scripts/check_arch_cards.py` cannot catch this class and is not meant to: it
verifies that a card's recorded anchor sha matches the seam file's current bytes,
which is a claim about FRESHNESS, not about whether the prose is true. It passed
clean on the exact tree carrying the wrong version.

So this checks the one kind of prose claim that IS mechanically checkable — a
constant the card spells out and the code also defines. Found by Constance on
#872 r1 while checking whether the card argued with itself.
"""

import re
from pathlib import Path

import pytest

from techne.core.codex import CODEX_VERSION
from techne.core.database import SCHEMA_VERSION

_LIVE = {"CODEX_VERSION": CODEX_VERSION, "SCHEMA_VERSION": SCHEMA_VERSION}


@pytest.mark.parametrize("card", sorted(Path("docs/arch").glob("*.md")), ids=lambda p: p.name)
def test_a_card_naming_a_schema_version_names_the_current_one(card):
    text = card.read_text()
    stale = []
    for name, live in _LIVE.items():
        # `NAME = N` or `NAME=N`, the form the cards actually use in prose.
        for m in re.finditer(rf"\b{name}\s*=\s*(\d+)", text):
            found = int(m.group(1))
            if found != live:
                line = text[: m.start()].count("\n") + 1
                stale.append(f"{card.name}:{line} says {name}={found}, code says {live}")
    assert not stale, (
        "an architecture card names a schema version that no longer exists:\n  "
        + "\n  ".join(stale)
        + "\nBump it in the same change that bumps the version. check_arch_cards.py "
        "cannot see this — it compares anchor shas, not prose."
    )
