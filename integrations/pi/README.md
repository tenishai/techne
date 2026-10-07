# techne ↔ pi integration

Run techne memory under [pi](https://github.com/badlogic/pi-mono) — the same
`~/.techne/techne.db` the Claude Code and opencode clients read. This is the pi
validation gate for [#57](https://github.com/tenishai/techne/issues/57)
(memory portability across AI clients).

There are two independent pieces. The MCP server gives the **model** access to
memory (search/recall/store/observe); the extension gives **automatic** capture
of your work without the model having to ask.

## 1. MCP server (model-facing memory)

pi reads MCP servers through the standard `.mcp.json` files (via the
pi-mcp-adapter package). Add techne's MCP server to `~/.config/mcp/mcp.json`
(global) or a project's `.mcp.json`:

```json
{
  "mcpServers": {
    "techne": {
      "command": "/ABS/PATH/techne/.venv/bin/python",
      "args": ["-m", "techne.mcp_server"]
    }
  }
}
```

Verify:

```sh
/mcp   # in a pi session → techne ✓ connected
```

This exposes `techne_search`, `techne_recall`, `techne_store`, `techne_observe`,
`techne_status`, `techne_threads` to the model (as `mcp__techne__techne_*`). No
code, no schema changes — the server reads `~/.techne` via `Config.load()`, with
no client path assumptions.

## 2. Capture extension (automatic memory)

pi extensions are in-process TypeScript with real lifecycle events, so this is
a direct event bridge — the analogue of Claude Code's hooks, no shell gymnastics
beyond shelling out to the `techne` CLI per event (the same
`techne observe` path the opencode plugin uses, so all clients funnel through
one capture surface).

Add the extension to `~/.pi/agent/settings.json`:

```json
{
  "extensions": ["/ABS/PATH/techne/integrations/pi/techne.ts"]
}
```

(Or drop/symlink the file into `~/.pi/agent/extensions/`, or test with
`pi -e /ABS/PATH/techne/integrations/pi/techne.ts`.)

The extension shells out to `techne` on PATH by default. That works as long as
the installed `techne` has the `observe` command — i.e. it's current with the
repo. Install techne editable so the CLI tracks HEAD and never drifts:

```sh
pipx install --force --editable /ABS/PATH/techne   # techne CLI = repo HEAD
```

If your `techne` on PATH is pinned/stale and you can't make it editable,
override the binary instead (must be a single executable, not
`python -m techne.cli`):

```sh
export TECHNE_BIN="/ABS/PATH/techne/.venv/bin/techne"
```

### What it captures

- **`tool_execution_end`** → one observation per tool run (`tool_name` = the pi
  tool, content = the tool result, capped at 4000 chars). Only techne's own
  plumbing is skipped — the `techne` CLI, `mcp`/`mcpScript` gateway calls that
  target techne, and `mcp__techne__*` tools; calls to other MCP servers are
  real work and get captured.
- **`message_end`** (user role) → a `UserPrompt` observation per user prompt (so
  memory records what was asked, not just what tools ran).
- **`agent_settled`** (run fully done — no retry/compaction/follow-up left) →
  runs `techne session-end`, which is the final pass (Claude Code runs it on
  SessionEnd): detects threads + summaries + resume points for the pi session,
  so it surfaces in `techne_resume` / `techne_threads` / the login banner.
  Firing per settled run is safe (content-addressed thread IDs), but each run
  with new work records the session as ended and, with a sync adapter, sends
  its threads again.
- The first observation for a session creates the session row stamped
  `platform: pi`.

The session id is the UUID in pi's session file name, so a resumed pi session
keeps its techne session across restarts.

And it **injects** memory:

- **`before_agent_start`** → appends a techne memory block (active threads +
  durable facts for the project, via `techne context`) to pi's system prompt —
  the read-side analogue of the Claude Code SessionStart banner. So the model is
  memory-aware by default, not only when it calls a recall tool. Fetched once
  per session (cached) to avoid a per-turn subprocess.

### Verify capture end-to-end

```sh
pi -p "list the files in this directory"
# then, from the techne repo:
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('$HOME/.techne/techne.db'); \
  print(c.execute(\"select session_id, tool_name from observations \
  where session_id in (select id from sessions where platform='pi') \
  order by id desc limit 5\").fetchall())"
```

You should see fresh `platform='pi'` observations in the shared db — proving
memory captured under pi is readable by the Claude Code and opencode clients.

## 3. Verify memory portability (the differential-canary probe)

Once the MCP server + extension are wired, prove that techne memory actually
reaches the pi model — the per-runtime acceptance test for egress (techne#94):

```sh
techne probe --runtime pi --mode off-test
```

This seeds a unique codeword into techne only, spawns a fresh
`pi --print` session in a throwaway workspace with **all tools disabled**
(`--no-tools`) so the only path left is the extension's `before_agent_start`
injection, then asks for the codeword. Surfacing it proves the **push** path
carries memory into pi. `--no-tools` closes the whole class of pull channels
(built-ins, extension tools, the pi-mcp-adapter gateway) at once instead of
enumerating a denylist against a moving target; extensions still load, so the
injection still fires.
Defaults to the model pi is configured with (override with `--model`); exit 0
= pass. The canary is deleted afterward.

(pi has no native auto-memory, so `--mode differential` has no negative control
there — `off-test` already isolates the only techne path.)
