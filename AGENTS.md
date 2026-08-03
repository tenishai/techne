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

**When you ADD a card, add its line to the `SEAMS → card:` index in
[`ARCHITECTURE.md`](ARCHITECTURE.md)** — an unindexed card is unprotected, because
deleting it later would pass silently. The gate enforces this in both directions, so
an unindexed card is a hard CI failure. Retiring a seam is fine: drop the card *and*
its index line together. The index entry must contain a literal `→` followed by the
bare path (`→ docs/arch/<seam>.md`); a markdown link or backticks reads as absent.

**Staleness is checked, not hoped for.** `scripts/check_arch_cards.py` recomputes each
card's anchor sha and fails if a seam drifted from its card (or an anchor is malformed).
Run it locally before you push. It also runs in CI on every push/PR — the `arch-cards`
job in `.github/workflows/test.yml` (#383). CAVEAT, do not over-trust it: this repo's
plan has no branch protection (`gh api repos/tenishai/techne/branches/main/protection`
→ 403 "Upgrade to GitHub Pro"), so NO check here is a required status check. The job
REPORTS drift; it cannot BLOCK a merge. Treat a red `arch-cards` as a stop signal by
convention, not by enforcement. Re-bless a card by updating its body and bumping its
recorded sha together.

```
python scripts/check_arch_cards.py
```

Cards are the **source of truth**. On merge they are mirrored into techne
(`document_card`, org-wide recall) — so keeping them current *here* keeps the
org-at-a-glance view current *everywhere*.
