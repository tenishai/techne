import type { Plugin } from "@opencode-ai/plugin"

/**
 * techne capture plugin for opencode.
 *
 * Bridges opencode's lifecycle events to techne's platform-agnostic capture CLI
 * (`techne observe`), so memory captured while working in opencode lands in the
 * same ~/.techne/techne.db that the Claude Code client reads. This is the
 * opencode half of techne#57 — memory travels with the operator, not the client.
 *
 * Why a CLI shell-out and not the MCP tool: opencode has no subprocess hooks
 * (plugins are in-process TypeScript only), and an MCP tool is reachable by the
 * model, not by a plugin event handler. So this shells out to `techne observe`
 * on each tool execution — the analogue of Claude Code's PostToolUse hook. The
 * model-facing `techne_observe` MCP tool is the other capture path; this plugin
 * is the automatic, per-tool one. Both funnel through the same
 * core.state.record_observation.
 *
 * The first observe for a session creates its row stamped `platform: opencode`,
 * which is where techne's sessions.platform column gains meaning for a second
 * client (closes #57 coupling #4).
 *
 * Captures: tool executions (tool.execute.after), user prompts (chat.message),
 * and end-of-session thread detection/summaries (session.idle → techne
 * session-end, the Stop-hook equivalent). Deferred:
 *   - retrieved-context injection (experimental.chat.system.transform)
 *
 * Setup — see integrations/opencode/README.md. In short, add to opencode.jsonc:
 *   "plugin": ["/ABS/PATH/techne/integrations/opencode/techne.ts"]
 * and set TECHNE_BIN to a techne that has the `observe` command (the editable
 * venv, to avoid pipx version drift):
 *   export TECHNE_BIN="/ABS/PATH/techne/.venv/bin/techne"
 */

// A single executable path. Default assumes `techne` on PATH; point at the
// editable venv to dodge the pipx CLI version-drift (where `observe` may be
// missing). Must be one binary — not "python -m techne.cli".
const TECHNE_BIN = process.env.TECHNE_BIN ?? "techne"

// Cap content so a large tool output can't blow ARG_MAX or bloat a single
// observation. Mirrors techne's own transcript-ingest truncation.
const MAX_CONTENT = 4000

export const TechneCapture: Plugin = async ({ $, directory }) => {
  // Best-effort capture of one observation. Bun's $ escapes every ${...}
  // interpolation, so multi-line / special content is one safe argument.
  // .nothrow() + try/catch: capture must never interrupt the user's session.
  const observe = async (sessionID: string, tool: string, body: string) => {
    const text = body.trim()
    if (!text) return
    const content = text.length > MAX_CONTENT ? text.slice(0, MAX_CONTENT) : text
    try {
      await $`${TECHNE_BIN} observe --session-id ${sessionID} --platform opencode --project-path ${directory} --tool-name ${tool} --content ${content}`.nothrow()
    } catch {
      // swallow — a memory write failing is not worth surfacing to the user
    }
  }

  return {
    // Each tool execution → one observation (the PostToolUse analogue).
    "tool.execute.after": async (input, output) => {
      // input: { tool, sessionID, callID, args }; output: { title, output, metadata }
      await observe(input.sessionID, input.tool, [output.title, output.output].filter(Boolean).join("\n"))
    },

    // Each user prompt → a UserPrompt observation (so memory records what was
    // asked, not just what tools ran). input: { sessionID, ... };
    // output: { message, parts }. We keep the text parts.
    "chat.message": async (input, output) => {
      const text = (output.parts ?? [])
        .filter((p: any) => p?.type === "text" && p?.text)
        .map((p: any) => p.text as string)
        .join("\n")
      await observe(input.sessionID, "UserPrompt", text)
    },

    // session.idle = turn complete → run techne's end-of-session processing
    // (thread detection + summaries), the analogue of Claude Code's Stop hook.
    // run_session_end is idempotent (content-addressed thread IDs), so firing
    // per turn is safe. Fire-and-forget: the techne subprocess completes on its
    // own even though opencode may not await session.idle handlers (#16879).
    event: async ({ event }) => {
      if (event.type !== "session.idle") return
      const sessionID = event.properties?.sessionID
      if (!sessionID) return
      try {
        await $`${TECHNE_BIN} session-end ${sessionID}`.nothrow()
      } catch {
        // swallow — end-of-session processing is best-effort
      }
    },
  }
}
