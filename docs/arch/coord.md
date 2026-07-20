---
card: arch-card:techne-coord
updated: 2026-07-20
anchor: techne/core/coord.py sha=3f5c6dbdb86342f5453cedb9a4d22eb2689e5401
---
# coord — inter-session mailbox (techne#376)

WHAT: concurrent Claude Code sessions talk without a human router.
IDENTITY: self-asserted session_id (CLAUDE_CODE_SESSION_ID); NO token — trust boundary = local machine (anything that writes techne.db can already do anything).
ADDRESSING: channel (default 'coord', broadcast) | session_id (direct, delivers even if offline).
PRESENCE: live_sessions() reads ~/.claude/sessions/<pid>.json, 90-min window (_PRESENCE_FRESH_SECONDS); read-only.
DELIVERY: push — post_tool_use hook injects unread into next tool call; session-start banner shows unread + who's live.

INVARIANTS:
- resolve_recipient(to): 'coord' passthrough | live display-name alias→session_id | v4-UUID passthrough; unmatched name → raise ValueError (dead-letter, NEVER silent-store).
- name tie → newest-active session wins.

API: MCP techne_send / techne_inbox / techne_mark_read / techne_coord_threads; CLI `techne coord`.
CONSUMERS: every live session sharing techne.db; MCP SERVER_INSTRUCTIONS tell agents to announce branch/commit intent on 'coord' (reflex).
