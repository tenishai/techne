# AGENTS.md — techne

Instructions for AI coding agents working in this repo. Claude Code loads this via
`CLAUDE.md` (`@AGENTS.md`); other Agent-Skills / AGENTS.md-aware tools read it directly.

## Architecture cards

This repo keeps **architecture cards** — short, durable descriptions of each
load-bearing subsystem ("seam") and of the repo as a whole:

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — repo-level overview: purpose, state, entry
  points, and the index of seams.
- [`docs/arch/<seam>.md`](docs/arch/) — one card per seam: its interface/protocol
  boundary, the invariants that aren't obvious from the code, and its fail-modes.

**Before you change a seam, read its card.** The card records the load-bearing rules
that a diff won't show you — ordering constraints, fail-closed behavior, "this is
deliberately NOT a portability seam", etc.

**When you change a seam, update its card in the same change** — including the
`anchor:` sha in the card's frontmatter (the git blob sha of the seam file the card
describes). A card whose recorded anchor sha no longer matches the seam file is *stale*.

**Staleness is enforced, not hoped for.** `scripts/check_arch_cards.py` recomputes each
card's anchor sha and fails if a seam drifted from its card. Run it locally before you
push; CI runs it on every PR. Re-bless a card by updating its body and bumping its
recorded sha together.

```
python scripts/check_arch_cards.py
```

Cards are the **source of truth**. On merge they are mirrored into techne
(`document_card`, org-wide recall) — so keeping them current *here* keeps the
org-at-a-glance view current *everywhere*.
