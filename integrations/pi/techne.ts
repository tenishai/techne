/**
 * techne capture extension for pi.
 *
 * Bridges pi's lifecycle events to techne's platform-agnostic capture CLI
 * (`techne observe`), so memory captured while working in pi lands in the same
 * ~/.techne/techne.db that the Claude Code and opencode clients read. This is
 * the pi half of techne#57 — memory travels with the operator, not the client.
 *
 * pi is a better host than opencode: extensions are in-process TypeScript with
 * real lifecycle events, so this is a direct event bridge (no per-event
 * subprocess juggling beyond the techne CLI itself). The CLI shell-out is
 * deliberate: the same `techne observe` path the opencode plugin uses, so all
 * three clients funnel through one capture surface.
 *
 * The first observe for a session creates its row stamped `platform: pi`.
 *
 * Captures: tool executions (tool_execution_end), user prompts (message_end,
 * user role), and end-of-run thread detection/summaries (agent_settled →
 * techne session-end, the Stop-hook equivalent). Also INJECTS memory into pi's
 * system prompt (before_agent_start → techne context), the read-side analogue
 * of the Claude Code SessionStart banner — so pi is memory-aware by default,
 * not only when the model calls a recall tool.
 *
 * Setup — see integrations/pi/README.md. In short, add to ~/.pi/agent/settings.json:
 *   "extensions": ["/ABS/PATH/techne/integrations/pi/techne.ts"]
 * and point TECHNE_BIN at a techne with the `observe` command (the editable
 * venv, to avoid pipx version drift):
 *   export TECHNE_BIN="/ABS/PATH/techne/.venv/bin/techne"
 */

import { spawn } from "node:child_process"
import { basename } from "node:path"

// A single executable path. Default assumes `techne` on PATH; point at the
// editable venv to dodge the pipx CLI version-drift (where `observe` may be
// missing). Must be one binary — not "python -m techne.cli".
const TECHNE_BIN = process.env.TECHNE_BIN ?? "techne"

// Cap content so a large tool output can't bloat a single observation. Mirrors
// techne's own transcript-ingest truncation (and the opencode plugin's cap).
const MAX_CONTENT = 4000

// A hung techne CLI must not hold a turn — kill after this. The default
// detector is heuristic (no LLM), so healthy runs finish in well under this.
const RUN_TIMEOUT_MS = 10_000

// True when the tool call is techne's own plumbing (this extension's CLI
// shell-outs or techne MCP tools) — capturing it would record the plumbing,
// not the work. Non-techne MCP servers are real work and get captured.
function isTechnePlumbing(toolName: string, args: any): boolean {
  if (toolName === "techne") return true
  if (toolName === "mcp" || toolName === "mcpScript") {
    const target = `${args?.server ?? ""} ${args?.tool ?? ""} ${toolName === "mcpScript" ? args?.code ?? "" : ""}`
    return target.includes("techne")
  }
  // Directly-registered MCP tools carry the server in the name
  // (e.g. mcp__techne__techne_search).
  return toolName.includes("techne")
}

// Warn once per process, so a broken TECHNE_BIN is visible instead of
// silently dropping every observation — without spamming per tool run.
let warned = false
function warn(msg: string): void {
  if (warned) return
  warned = true
  console.warn(`[techne] ${msg}`)
}

/** Run a command, resolve with stdout. Never throws — capture is best-effort. */
function run(args: string[], stdin?: string): Promise<string> {
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(TECHNE_BIN, args, { stdio: ["pipe", "pipe", "ignore"] })
    } catch {
      resolve("")
      return
    }
    let out = ""
    let done = false
    const finish = (value: string) => {
      if (done) return
      done = true
      clearTimeout(timer)
      resolve(value)
    }
    const timer = setTimeout(() => {
      child.kill("SIGKILL")
      finish("")
    }, RUN_TIMEOUT_MS)
    child.stdout?.on("data", (d: Buffer) => (out += d.toString()))
    // A fast-exiting child (e.g. a stale pipx techne printing a usage error)
    // never drains stdin → the queued write EPIPEs. Without this listener the
    // error is uncaught and takes down the host pi process.
    child.stdin?.on("error", () => {})
    child.on("error", (err) => {
      warn(
        `capture disabled: cannot run ${TECHNE_BIN} (${err.message}) — ` +
          `set TECHNE_BIN to a techne with the observe command, e.g. ` +
          `export TECHNE_BIN="/ABS/PATH/techne/.venv/bin/techne"`,
      )
      finish("")
    })
    child.on("close", (code) => {
      if (code !== 0) {
        warn(
          `capture degraded: ${TECHNE_BIN} ${args[0]} exited ${code} — ` +
            `stale install? try: pipx install --force --editable <techne repo>`,
        )
      }
      finish(out)
    })
    if (stdin !== undefined) {
      child.stdin?.write(stdin)
    }
    child.stdin?.end()
  })
}

/** Best-effort capture of one observation. Content goes over stdin — no argv
 *  size limits, no shell-quoting hazards, multi-line safe. */
async function observe(sessionId: string, projectPath: string, tool: string, body: string): Promise<void> {
  const text = body.trim()
  if (!text) return
  const content = text.length > MAX_CONTENT ? text.slice(0, MAX_CONTENT) : text
  await run(
    ["observe", "--session-id", sessionId, "--platform", "pi", "--project-path", projectPath, "--tool-name", tool],
    content,
  )
}

/** Flatten a tool result (string or content-part array) to text. */
function resultText(result: unknown): string {
  if (typeof result === "string") return result
  if (Array.isArray(result)) {
    return result
      .map((p) => (p && typeof p === "object" && typeof (p as { text?: unknown }).text === "string" ? (p as { text: string }).text : ""))
      .filter(Boolean)
      .join("\n")
  }
  if (result && typeof result === "object") {
    try {
      return JSON.stringify(result)
    } catch {
      return ""
    }
  }
  return ""
}

/** Flatten a user message's content (string or parts) to text. */
function messageText(content: unknown): string {
  if (typeof content === "string") return content
  if (Array.isArray(content)) {
    return content
      .map((p) => (p && typeof p === "object" && typeof (p as { text?: unknown }).text === "string" ? (p as { text: string }).text : ""))
      .filter(Boolean)
      .join("\n")
  }
  return ""
}

/** Stable session id: the UUID in pi's session file name, so a resumed pi
 *  session keeps its techne session across restarts. Falls back to the raw id. */
function techneSessionId(ctx: { sessionManager: { getSessionFile(): string | null; getSessionId(): string } }): string {
  const file = ctx.sessionManager.getSessionFile()
  const m = file ? basename(file).match(/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/) : null
  return m ? m[1] : ctx.sessionManager.getSessionId()
}

export default function (pi: {
  on(event: string, handler: (event: any, ctx: any) => Promise<any> | any): void
}) {
  // Per-session cache of the injected memory block. before_agent_start fires on
  // every prompt; fetching context is a subprocess, so we run it once per
  // session (an empty string is cached too → no re-fetch). Resume context is
  // "prior context for this work", so a per-session snapshot is the right grain.
  const contextCache = new Map<string, string>()

  // tool_execution_end doesn't carry args, so remember them at start — needed
  // to tell techne's own MCP plumbing apart from other servers' real work.
  const toolArgs = new Map<string, any>()

  pi.on("tool_execution_start", (event) => {
    toolArgs.set(event.toolCallId, event.args)
  })

  // Each tool execution → one observation (the PostToolUse analogue).
  pi.on("tool_execution_end", async (event, ctx) => {
    const args = toolArgs.get(event.toolCallId)
    toolArgs.delete(event.toolCallId)
    if (isTechnePlumbing(event.toolName, args)) return
    await observe(techneSessionId(ctx), ctx.cwd, event.toolName, resultText(event.result))
  })

  // Each user prompt → a UserPrompt observation (so memory records what was
  // asked, not just what tools ran). Guard: user-role messages only —
  // assistant/toolResult turns must not be mislabeled as user prompts.
  pi.on("message_end", async (event, ctx) => {
    if (event.message?.role !== "user") return
    await observe(techneSessionId(ctx), ctx.cwd, "UserPrompt", messageText(event.message.content))
  })

  // Inject relevant techne memory into pi's system prompt — the read-side
  // analogue of the Claude Code SessionStart banner. So pi is memory-aware by
  // default, instead of only when the model calls a tool.
  pi.on("before_agent_start", async (event, ctx) => {
    const sid = techneSessionId(ctx)
    try {
      let block = contextCache.get(sid)
      if (block === undefined) {
        block = (await run(["context", "--project-path", ctx.cwd])).trim()
        contextCache.set(sid, block)
      }
      if (block) return { systemPrompt: event.systemPrompt + "\n\n" + block }
    } catch {
      // swallow — memory injection is best-effort, never blocks a turn
    }
  })

  // agent_settled = the run is done (no retry/compaction/follow-up left) → run
  // techne's end-of-session processing (thread detection + summaries), the
  // analogue of Claude Code's Stop hook. run_session_end is idempotent
  // (content-addressed thread IDs), so firing per settled run is safe.
  // Fire-and-forget: don't hold the event open waiting on the subprocess.
  pi.on("agent_settled", async (_event, ctx) => {
    const sid = techneSessionId(ctx)
    void run(["session-end", sid])
  })
}
