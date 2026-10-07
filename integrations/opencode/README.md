# techne ↔ opencode integration

Run techne memory under [opencode](https://opencode.ai) — the same
`~/.techne/techne.db` the Claude Code client reads. This is the opencode
validation gate for [#57](https://github.com/tenishai/techne/issues/57)
(memory portability across AI clients).

There are two independent pieces. The MCP server gives the **model** access to
memory (search/recall/store/observe); the plugin gives **automatic** capture of
your work without the model having to ask.

## 1. MCP server (model-facing memory)

Add techne's MCP server to `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "techne": {
      "type": "local",
      "command": [
        "/ABS/PATH/techne/.venv/bin/python",
        "-m",
        "techne.mcp_server"
      ],
      "enabled": true
    }
  }
}
```

Verify:

```sh
opencode mcp list      # → techne ✓ connected
```

This exposes `techne_search`, `techne_recall`, `techne_store`, `techne_observe`,
`techne_status`, `techne_threads` to the model. No code, no schema changes — the
server reads `~/.techne` via `Config.load()`, with no Claude-Code path
assumptions.

## 2. Capture plugin (automatic memory)

opencode has no subprocess hooks (plugins are in-process TypeScript), so
automatic capture shells out to the `techne observe` CLI on each tool
execution — the analogue of Claude Code's PostToolUse hook.

Add the plugin to `opencode.jsonc`:

```jsonc
{
  "plugin": ["/ABS/PATH/techne/integrations/opencode/techne.ts"]
}
```

The plugin shells out to `techne` on PATH by default. That works as long as the
installed `techne` has the `observe` command — i.e. it's current with the repo.
Install techne editable so the CLI tracks HEAD and never drifts:

```sh
pipx install --force --editable /ABS/PATH/techne   # techne CLI = repo HEAD
```

If your `techne` on PATH is pinned/stale and you can't make it editable, override
the binary instead (must be a single executable, not `python -m techne.cli`):

```sh
export TECHNE_BIN="/ABS/PATH/techne/.venv/bin/techne"
```

### What it captures

- **`tool.execute.after`** → one observation per tool run (`tool_name` = the
  opencode tool, content = title + output, capped at 4000 chars).
- **`chat.message`** → a `UserPrompt` observation per user prompt (so memory
  records what was asked, not just what tools ran).
- **`session.idle`** (turn complete) → runs `techne session-end`, which is the
  final pass (Claude Code runs it on SessionEnd): detects threads + summaries +
  resume points for the opencode session, so it surfaces in `techne_resume` /
  `techne_threads` / the login banner. Firing per turn is safe
  (content-addressed thread IDs), but each turn with new work records the
  session as ended and, with a sync adapter, sends its threads again.
- The first observation for a session creates the session row stamped
  `platform: opencode`.

And it **injects** memory:

- **`experimental.chat.system.transform`** → prepends a `<techne-memory>` block
  (active threads + durable facts for the project, via `techne context`) to
  opencode's system prompt — the read-side analogue of the Claude Code
  SessionStart banner. So the model is memory-aware by default, not only when it
  calls a recall tool. Fetched once per session (cached) to avoid a per-turn
  subprocess.

### Verify capture end-to-end

Needs an LLM provider configured in opencode (so a turn actually runs a tool):

```sh
opencode run "list the files in this directory"
# then, from the techne repo:
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('$HOME/.techne/techne.db'); \
  print(c.execute(\"select session_id, tool_name from observations \
  where session_id in (select id from sessions where platform='opencode') \
  order by id desc limit 5\").fetchall())"
```

You should see fresh `platform='opencode'` observations in the shared db —
proving memory captured under opencode is readable by the Claude Code client.

## 3. Verify memory portability (the differential-canary probe)

Once the MCP server + plugin are wired, prove that techne memory actually reaches
the opencode model — the per-runtime acceptance test for egress (techne#94):

```sh
techne probe --runtime opencode --mode off-test
```

This seeds a unique codeword into techne only, spawns a fresh `opencode run`
session with the techne **MCP tools gated off** (a throwaway project-local
`techne-readonly` agent with `tools: { techne_*: false }`) so the only path left
is the plugin's `experimental.chat.system.transform` injection, then asks for the
codeword. Surfacing it proves the **push** path carries memory into opencode.
Defaults to the keyless `opencode/big-pickle` model; exit 0 = pass. The canary is
deleted afterward.

(opencode has no native auto-memory, so `--mode differential` has no negative
control there — `off-test` already isolates the only techne path.)
