---
card: arch-card:techne-coord
updated: 2026-08-05
anchor: techne/core/coord.py sha=70a37b543723f10517685cadcb86aa136b8e4a33
---
# coord — inter-session mailbox (techne#376)

WHAT: concurrent Claude Code sessions talk without a human router.
IDENTITY: self-asserted session_id (CLAUDE_CODE_SESSION_ID); NO token — trust boundary = local machine (anything that writes techne.db can already do anything).
ADDRESSING: channel (default 'coord', broadcast) | session_id (direct, delivers even if offline).
PRESENCE: live_sessions() reads ~/.claude/sessions/<pid>.json; keeps a row only if fresh within a 90-min window (_PRESENCE_FRESH_SECONDS) AND its pid is a live process — reap-on-read via os.kill(pid,0) (techne#380; heartbeat freshness alone advertised dead PIDs as routable, missing/foreign pid fails open). Read-only: never writes/deregisters the roster, so a hard SIGKILL still relies on TTL+reap.
DELIVERY: push — post_tool_use hook injects unread into next tool call; session-start banner shows unread + who's live.

INVARIANTS:
- resolve_recipient(to): 'coord' passthrough | live display-name alias→session_id | v4-UUID passthrough; unmatched name → raise ValueError (dead-letter, NEVER silent-store).
- name tie → newest-active session wins.
- **IDENTITY IS TWO DIFFERENT THINGS AND THEY ARE NOT INTERCHANGEABLE (techne#446).** A message's AUTHOR and a reader's RECEIPT both used to key on the session UUID, which Claude Code mints fresh per launch — so a relaunched session had no receipts and did not recognise its own past sends. It re-read the entire backlog every launch and the unread count only ever grew (measured: 97 messages, the bus's whole life, reported unread; 56 of them the reader's own). Both now key on the DURABLE identity — the `-n` display name — via `reader_key()`, falling back to the UUID when unnamed. `coord_reads.reader_session` is namespaced (`name:` / `uuid:`) because the column holds two KINDS of identity and a bare value cannot say which.
- PER-READER FANOUT IS NOT THE BUG AND MUST STAY. `coord` is a broadcast: if one peer marking a message read cleared it for everyone, a slower peer would silently never see it. #446 changed the receipt's IDENTITY, never its cardinality.
- THE DURABLE KEY COSTS TWO THINGS, both deliberate: a recycled name inherits the previous holder's receipts (the name denotes a persistent ROLE, which is what makes it stable enough to key on), and an unnamed session keeps the old re-read-everything behaviour (no `-n`, no durable identity to offer).

API: MCP techne_send / techne_inbox / techne_mark_read / techne_coord_threads; CLI `techne coord`.
CONSUMERS: every live session sharing techne.db; MCP SERVER_INSTRUCTIONS tell agents to announce branch/commit intent on 'coord' (reflex).
