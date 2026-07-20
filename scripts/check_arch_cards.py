#!/usr/bin/env python3
"""Architecture-card staleness gate (techne pilot).

Each architecture card (ARCHITECTURE.md, docs/arch/*.md) declares the seam file(s)
it describes via `anchor:` lines in its YAML frontmatter, each pinned to the git blob
sha of that file when the card was last written/reviewed:

    ---
    card: arch-card:techne-domain-stores
    anchor: techne/core/stores.py sha=fe749946756940d5109ac45a8d7d8d2b795caf59
    ---

This checker recomputes each anchor's current blob sha (`git hash-object`, so it sees
the working tree) and FAILS if a seam file has drifted from what its card records —
i.e. someone changed the code but not the card. Re-bless a card by updating its body
and bumping the recorded sha in the same change.

Deterministic, stdlib-only, no LLM. Exit 0 = all cards fresh, 1 = stale/malformed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CARD_GLOBS = ("ARCHITECTURE.md", "docs/arch/*.md")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.S)
ANCHOR_RE = re.compile(r"^anchor:\s*(\S+)\s+sha=([0-9a-f]{40})\s*$", re.M)


def blob_sha(rel_path: str) -> str | None:
    p = REPO / rel_path
    if not p.exists():
        return None
    out = subprocess.run(
        ["git", "hash-object", rel_path],
        cwd=REPO, capture_output=True, text=True,
    )
    return out.stdout.strip() if out.returncode == 0 else None


def find_cards() -> list[Path]:
    cards: list[Path] = []
    for g in CARD_GLOBS:
        cards.extend(sorted(REPO.glob(g)))
    return cards


def main() -> int:
    cards = find_cards()
    if not cards:
        print("no architecture cards found", file=sys.stderr)
        return 1

    stale: list[tuple[Path, str]] = []
    n_anchors = 0
    for card in cards:
        fm = FRONTMATTER_RE.search(card.read_text())
        if not fm:
            stale.append((card, "no frontmatter block"))
            continue
        anchors = ANCHOR_RE.findall(fm.group(1))
        if not anchors:
            stale.append((card, "no `anchor:` lines in frontmatter"))
            continue
        for path, recorded in anchors:
            n_anchors += 1
            current = blob_sha(path)
            if current is None:
                stale.append((card, f"anchor seam file missing: {path}"))
            elif current != recorded:
                stale.append((
                    card,
                    f"seam drifted: {path}\n"
                    f"        recorded {recorded}\n"
                    f"        current  {current}",
                ))

    print(f"checked {len(cards)} card(s), {n_anchors} anchor(s)")
    if stale:
        print(f"\nSTALE ({len(stale)}):")
        for card, why in stale:
            print(f"  ✗ {card.relative_to(REPO)}: {why}")
        print(
            "\nA seam changed without its card. Update the card body and bump its "
            "recorded `anchor:` sha in the same change."
        )
        return 1
    print("all architecture cards fresh ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
