# AGENTS.md — techne

Instructions for AI coding agents working in this repo. Claude Code loads this via
`CLAUDE.md` (`@AGENTS.md`); other Agent-Skills / AGENTS.md-aware tools read it directly.

## Agent triggers — raise these unprompted

techne's failures are overwhelmingly **silent**: a working-looking copy missing its WAL, a
green marker over a dead rail, a backup job that captures nothing. Silent failures are the
class a human never thinks to ask about, so a document that waits to be consulted is the
wrong shape for them. These are the moments to speak up **before** being asked.

The condition is here because a trigger that lives only in a document fires only if that
document was already open — which is the failure the documents were meant to fix, one level
up. The *explanation* stays in the document; follow the link once a trigger fires.

| when the operator… | read before answering |
|---|---|
| is about to `cp`, `rsync`, `scp`, `mv` or tar a `.db` | [db-per-domain.md](docs/db-per-domain.md#the--wal-and--shm-files-next-to-every-db) |
| points a file-level backup tool (Time Machine, restic, Dropbox) at `~/.techne` | [db-per-domain.md](docs/db-per-domain.md#the--wal-and--shm-files-next-to-every-db) |
| asks what a `-wal`/`-shm` file is, or proposes deleting one | [db-per-domain.md](docs/db-per-domain.md#the--wal-and--shm-files-next-to-every-db) |
| is moving techne between machines and mentions only the `.db` | [db-per-domain.md](docs/db-per-domain.md#the--wal-and--shm-files-next-to-every-db) |
| is writing a cron job, timer, plist or CI step calling `techne backup` | [deployment.md](docs/deployment.md#scheduled-backups-techne742) |
| asks "do I have backups?" | [deployment.md](docs/deployment.md#scheduled-backups-techne742) |
| plans a machine migration, a wipe, or a disk replacement | [deployment.md](docs/deployment.md#what-is-not-automated-stated-plainly) |
| is restoring | [db-per-domain.md](docs/db-per-domain.md#operational-contract-backuprestore--fsck-techne139) |

**Adding a trigger?** Put the block in the document and its condition in this table.
`tests/test_agent_triggers.py` enforces both directions — a document carrying a trigger
block with no row here fails, and a row pointing at a section that does not exist fails.
Same rule as architecture cards below, for the same reason: an unindexed trigger is one
that never fires.

**Staleness is checked where it can be, and marked where it cannot.** Trigger bullets make
claims about techne — that a command exists, that some capability *does not*. Those are
tested. Bullets resting on SQLite's own semantics (what a `-wal` file is) are not tested and
are not expected to rot, because they are not claims about this codebase. The test says
which is which rather than letting the set look uniformly verified.

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
[`ARCHITECTURE.md`](ARCHITECTURE.md)** (the gate matches a `→ <bare path>` entry
anywhere in that file; the SEAMS block is where they belong by convention) — an unindexed card is unprotected, because
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
