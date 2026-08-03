#!/usr/bin/env python3
"""Architecture-card staleness gate.

Each architecture card (ARCHITECTURE.md, docs/arch/*.md) declares the seam file(s)
it describes via `anchor:` lines in its YAML frontmatter, each pinned to the git blob
sha of that file when the card was last written/reviewed:

    ---
    card: arch-card:techne-domain-stores
    anchor: techne/core/stores.py sha=fe749946756940d5109ac45a8d7d8d2b795caf59
    ---

This checker recomputes each anchor's current blob sha (`git hash-object`, so it sees
the working tree) and FAILS if a seam file has drifted from what its card records — i.e.
someone changed the code but not the card. Re-bless a card by updating its body and
bumping the recorded sha in the same change.

A card is any globbed file whose frontmatter declares a `card:` key; other `.md` files
under the globs are skipped (reported, not failed). Any `anchor:` line that does not
parse exactly is a hard failure — a malformed anchor must never be silently ignored,
or a typo'd sha on a multi-anchor card would leave that seam unguarded.

COVERAGE, on top of freshness. `ARCHITECTURE.md` must exist, must itself parse as a
card, and its `SEAMS → card:` block is the MANIFEST of seam cards. It is checked in
both directions:

  * every path the index names must still parse as a card (catches deletion, rename,
    and frontmatter removal), and
  * every card on disk must appear in the index (without this, a card that was NEVER
    indexed could be deleted for exit 0 — regenerating the very false-green this
    check exists to close, and doing it for NEW cards, which are the least likely to
    have been wired in carefully).

The manifest needs its own guard because it is the one file exempt from the reverse
check — it indexes the seam cards, not itself — so nothing else would notice if its
`card:` frontmatter were stripped and its own anchor silently stopped being verified.
Retiring a seam stays green: drop the card AND its index line together.

Deterministic, stdlib-only, no LLM.
Exit 0 = all cards fresh · 1 = stale/malformed/missing · 2 = environment error.

The repo root defaults to this script's repo; override with ARCH_CARDS_ROOT (for tests).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

CARD_GLOBS = ("ARCHITECTURE.md", "docs/arch/*.md")
# The repo card's `SEAMS → card:` block doubles as the MANIFEST of seam cards that
# must exist. Globbing alone cannot see a DELETED card — it simply yields one fewer
# file and still exits 0, so coverage shrinks silently (the same false-green class
# this gate exists to prevent). Cross-checking the glob against the manifest closes
# that: removing a card AND its index line is a deliberate, passing change; deleting
# just the file is caught. Self-maintaining — no hardcoded card count to keep bumping.
MANIFEST_FILE = "ARCHITECTURE.md"
# Only non-nested paths: the card glob is `docs/arch/*.md`, so a nested path could
# never be found as a card and would be reported as deleted forever. Matching the
# glob's shape keeps the two halves consistent (Prudence, PR #451).
MANIFEST_RE = re.compile(r"→\s*(docs/arch/[^/\s)]+\.md)")
# Tolerate an optional BOM and CRLF line endings around the frontmatter fence.
FRONTMATTER_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---", re.S)
CARD_KEY_RE = re.compile(r"^card:\s*\S", re.M)
ANCHOR_LINE_RE = re.compile(r"^anchor:.*$", re.M)          # every anchor line, valid or not
ANCHOR_RE = re.compile(r"^anchor:\s*(?P<path>\S+)\s+sha=(?P<sha>[0-9a-f]{40})\s*$")


def repo_root() -> Path:
    env = os.environ.get("ARCH_CARDS_ROOT")
    return Path(env).resolve() if env else Path(__file__).resolve().parent.parent


def blob_sha(repo: Path, rel_path: str) -> str | None:
    if not (repo / rel_path).exists():
        return None
    out = subprocess.run(
        ["git", "hash-object", rel_path],
        cwd=repo, capture_output=True, text=True,
    )
    return out.stdout.strip() if out.returncode == 0 else None


def find_cards(repo: Path) -> list[Path]:
    cards: list[Path] = []
    for g in CARD_GLOBS:
        cards.extend(sorted(repo.glob(g)))
    return cards


def check(repo: Path) -> tuple[int, list[tuple[Path, str]], int, int, int]:
    """Return (exit_code, problems, n_cards, n_anchors, n_skipped)."""
    # Preflight: git present and repo is a work tree, else a clear env error (exit 2).
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo, capture_output=True, text=True,
        )
    except FileNotFoundError:
        return 2, [(repo, "git executable not found on PATH")], 0, 0, 0
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return 2, [(repo, f"not a git work tree: {repo}")], 0, 0, 0

    problems: list[tuple[Path, str]] = []
    n_cards = n_anchors = n_skipped = 0
    found: set[str] = set()  # repo-relative paths of files that parsed AS cards

    for f in find_cards(repo):
        fm = FRONTMATTER_RE.search(f.read_text(encoding="utf-8"))
        # Only files whose frontmatter declares `card:` are cards; skip stray .md.
        if not fm or not CARD_KEY_RE.search(fm.group(1)):
            n_skipped += 1
            continue
        n_cards += 1
        found.add(f.relative_to(repo).as_posix())
        anchor_lines = ANCHOR_LINE_RE.findall(fm.group(1))
        if not anchor_lines:
            problems.append((f, "card has no `anchor:` line in frontmatter"))
            continue
        for line in anchor_lines:
            n_anchors += 1
            m = ANCHOR_RE.match(line)
            if not m:
                problems.append((
                    f,
                    "malformed anchor (want 'anchor: <path> sha=<40-hex>'): " + line.strip(),
                ))
                continue
            path, recorded = m.group("path"), m.group("sha")
            current = blob_sha(repo, path)
            if current is None:
                problems.append((f, f"anchor seam file missing: {path}"))
            elif current != recorded:
                problems.append((
                    f,
                    f"seam drifted: {path}\n        recorded {recorded}\n        current  {current}",
                ))

    # Manifest cross-check (see MANIFEST_FILE above): every seam card the repo card
    # indexes must still parse as a card. Catches deletion/rename/frontmatter-removal,
    # none of which the glob can see.
    manifest = repo / MANIFEST_FILE
    # The manifest is the ONE file exempt from the reverse check (it indexes the seam
    # cards, not itself) — so nothing was verifying it. Delete it and you got a loud
    # failure; strip four lines from its head and you got a clean bill of health,
    # with its own `anchor:` silently unchecked. Same "deleted, renamed, or its
    # `card:` frontmatter was removed" class the forward check already names, applied
    # to the file exempt from it. Third instance of one pattern in this script: the
    # check has a blind spot exactly where nothing checks the checker.
    # (Prudence, PR #451.)
    if MANIFEST_FILE not in found:
        detail = (
            "is missing"
            if not manifest.exists()
            else "no longer parses as a card (`card:` frontmatter removed)"
        )
        problems.append(
            (repo, f"{MANIFEST_FILE} {detail} — seam-card coverage cannot be checked")
        )
    # Deliberately a second `if`, not an `else`: when only the frontmatter is gone the
    # SEAMS block is still readable and still worth cross-checking.
    if manifest.exists():
        indexed = set(MANIFEST_RE.findall(manifest.read_text(encoding="utf-8")))
        # Forward: everything the index claims must still parse as a card.
        for rel in sorted(indexed):
            if rel not in found:
                problems.append((
                    manifest,
                    f"card indexed in {MANIFEST_FILE} but not found as a card: {rel}\n"
                    "        (deleted, renamed, or its `card:` frontmatter was removed)\n"
                    "        If the removal is deliberate, drop its line from the SEAMS index too.",
                ))
        # Reverse: every card on disk must appear in the index. Without this the
        # check is one-directional, and a card that was NEVER indexed can be deleted
        # for exit 0 — regenerating the false-green precisely for new cards, which
        # are the ones least likely to have been wired in carefully. It also catches
        # an index that drifted out of the matched shape. (Prudence, PR #451.)
        seam_cards = [r for r in sorted(found) if r != MANIFEST_FILE]
        if seam_cards and not indexed:
            # Blame the INDEX, not the cards. A reformat of the SEAMS block (markdown
            # links, backticks, an ASCII `->`) matches nothing, and the reverse loop
            # would then report every card as unindexed: N failures pointing at N
            # innocent files, under a trailer describing a different failure class.
            # When NOTHING matches, the defect is one line of formatting.
            # (Prudence, PR #451.)
            problems.append((
                manifest,
                f"the `SEAMS → card:` index matched NO cards, but {len(seam_cards)} "
                "exist on disk\n"
                "        Usually a formatting change in the index — or the entries\n"
                "        were removed. Either way the cards are not at fault.\n"
                "        Each entry must contain:\n"
                "          → docs/arch/<name>.md      (a literal → then the bare path)",
            ))
            seam_cards = []  # don't also blame each card individually
        for rel in seam_cards:
            if rel not in indexed:
                problems.append((
                    repo / rel,
                    f"card is not indexed in {MANIFEST_FILE}'s `SEAMS → card:` block\n"
                    "        (a card absent from the index is unprotected — deleting it\n"
                    "         later would pass silently). Add a line containing:\n"
                    f"          → {rel}\n"
                    "        If a line for it already exists, check its SHAPE — only a\n"
                    "        literal → followed by the bare path is matched, so a\n"
                    "        markdown link or backticks reads as absent.",
                ))

    if n_cards == 0:
        return 1, [(repo, "no architecture cards found")], 0, 0, n_skipped
    return (1 if problems else 0), problems, n_cards, n_anchors, n_skipped


def main() -> int:
    repo = repo_root()
    code, problems, n_cards, n_anchors, n_skipped = check(repo)

    if code != 2:
        summary = f"checked {n_cards} card(s), {n_anchors} anchor(s)"
        if n_skipped:
            summary += f", skipped {n_skipped} non-card file(s)"
        print(summary)

    if problems:
        header = "ERROR" if code == 2 else f"FAILED ({len(problems)})"
        print(f"\n{header}:")
        for where, why in problems:
            try:
                shown: Path | str = where.relative_to(repo)
            except ValueError:
                shown = where
            print(f"  ✗ {shown}: {why}")
        # Print the anchor-drift trailer ONLY when an anchor problem is actually
        # among the failures. The coverage classes added in PR #451 (unindexed card,
        # index matched nothing, manifest missing/unparsed) each carry their own
        # correct inline remediation, and this footer CONTRADICTED the advice printed
        # directly above them — telling the reader to bump an `anchor:` sha that has
        # nothing to do with the failure. (Prudence, PR #451.)
        anchor_problem = any(
            ("drifted" in why or "malformed" in why or "seam file missing" in why)
            for _where, why in problems
        )
        if code == 1 and anchor_problem:
            print(
                "\nA seam changed (or an anchor is malformed) without its card being "
                "updated.\nFix the card body and bump its recorded `anchor:` sha in the "
                "same change."
            )
        return code

    print("all architecture cards fresh ✓")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
