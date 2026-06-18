# techne ↔ opencode integration

Run techne memory under [opencode](https://opencode.ai) — the same
`~/.techne/techne.db` the Claude Code client reads. This is the opencode
validation gate for [#57](https://github.com/dimartin/techne/issues/57)
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

Add the plugin and point `TECHNE_BIN` at a techne that has the `observe` command:

```jsonc
{
  "plugin": ["/ABS/PATH/techne/integrations/opencode/techne.ts"]
}
```

```sh
# In the shell that launches opencode. Use the editable venv to avoid the pipx
# CLI version-drift (the pipx `techne` may not have `observe` yet).
export TECHNE_BIN="/ABS/PATH/techne/.venv/bin/techne"
```

`TECHNE_BIN` must be a single executable (not `python -m techne.cli`). It
defaults to `techne` on PATH.

### What it captures

- **`tool.execute.after`** → one observation per tool run (`tool_name` = the
  opencode tool, content = the execution title + output, capped at 4000 chars).
- The first observation for a session creates the session row stamped
  `platform: opencode`.

Deferred (MVP cut): user-prompt capture (`chat.message`), retrieved-context
injection (`experimental.chat.system.transform`), and session-end thread
summarization.

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
