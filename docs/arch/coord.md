---
card: arch-card:techne-coord
updated: 2026-07-25
anchor: techne/core/coord.py sha=1f6533efd744b8c75f33478860361fa4723753e5
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

API: MCP techne_send / techne_inbox / techne_mark_read / techne_coord_threads; CLI `techne coord`.
CONSUMERS: every live session sharing techne.db; MCP SERVER_INSTRUCTIONS tell agents to announce branch/commit intent on 'coord' (reflex).
