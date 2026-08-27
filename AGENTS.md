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
| gets **zero rows** from the archivist, librarian or architect | [four-functions.md](docs/four-functions.md#getting-content-in) |
| is about to rely on `librarian_search` for coverage | [four-functions.md](docs/four-functions.md#nothing-in-techne-indexes-transcripts-for-you-and-that-matters-more-than-it-sounds) |
| asks why the architect is empty on a fresh install | [four-functions.md](docs/four-functions.md#getting-content-in) |
| proposes sweeping/deleting `reflection_queue` rows, or asks why reflection debt never reaches zero | [reflect-guide.md](docs/reflect-guide.md#the-debt-number-is-two-populations) |
| sets `reflect.enabled: false` to quiet a rail that looks dead | [reflect-guide.md](docs/reflect-guide.md#the-two-switches-and-why-they-are-not-one-switch) |
| asks whether reflection is running, or why it produced nothing | [reflect-guide.md](docs/reflect-guide.md#the-two-switches-and-why-they-are-not-one-switch) |
| edits `~/.techne/config.yaml` while a techne MCP server is live | [reflect-guide.md](docs/reflect-guide.md#where-the-answers-land-write_to) |
| is about to drain a large session through `techne_reflect_emit` | [reflect-guide.md](docs/reflect-guide.md#draining-a-backlog) |

**Adding a trigger?** Put the block in the document and its condition in this table.
`tests/test_agent_triggers.py` enforces all three directions — a document carrying a
trigger block with no row here fails, a row pointing at a section that does not exist
fails, and a row pointing at a document that no longer carries a block fails.
Same rule as architecture cards below, for the same reason: an unindexed trigger is one
that never fires.

**Staleness is checked, not marked.** Every bullet in both current blocks is a claim about
techne and all of them are tested — including the `-wal` ones, which look like statements
about SQLite and are not: they are true *because* techne opens its databases in WAL mode,
one line in `core/database.py`. Flip it to `DELETE` and all four become confidently wrong
advice. Absences ("`techne doctor` does not check this", "nothing archives transcripts")
cannot be proved by grep, so the relevant SURFACE is pinned instead — the registered check
names, the command tree — and any change trips the test and forces a re-read.

Each bullet **names the test that witnesses it**, and both directions are enforced: a
bullet with no named witness fails, and a witness named but not defined fails. Counting
bullets was not enough — a count says a number is right, not that any particular claim is
still checked, so deleting the one test behind four bullets left the accounting looking
complete.

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
