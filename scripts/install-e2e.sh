#!/usr/bin/env bash
# End-to-end test of the README install paths, run as a user runs them
# (techne#1319): the README's commands, `techne init`, then hooks, the MCP
# server, a store and a recall. Fresh $HOME per path; uv removed from PATH.
#
#   scripts/install-e2e.sh                 # both paths, this checkout's HEAD
#   scripts/install-e2e.sh --path source   # source | pipx
#   scripts/install-e2e.sh --ref v0.18.2   # a published tag over https
#
# Commit mode installs HEAD from git, not uncommitted edits. --ref needs GitHub
# access while the repo is private (GH_TOKEN or a logged-in gh); the token goes
# to git via GIT_CONFIG_* env vars only. Run by CI's `install-e2e` job.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
PATHS="source pipx"
REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --path) PATHS="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "install-e2e: unknown argument $1" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m── %s\033[0m\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# techne's permission checks behave differently for root, and no README reader
# installs as root. Refuse rather than test something nobody runs.
[ "$(id -u)" -ne 0 ] || fail "run as a non-root user (root bypasses the permission checks a user hits)"
command -v git >/dev/null || fail "git not found (the README requires it)"
command -v python3 >/dev/null || fail "python3 not found"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# A PATH without uv: drop every entry that holds a uv binary. The CI runner and
# the build image both have one; a user following the README may not.
clean_path=""
IFS=: read -ra entries <<<"$PATH"
for d in "${entries[@]}"; do
  [ -n "$d" ] && [ ! -x "$d/uv" ] && clean_path="${clean_path:+$clean_path:}$d"
done
export PATH="$clean_path"
! command -v uv >/dev/null || fail "uv still on PATH after cleaning: $(command -v uv)"

# --- where the install comes from ------------------------------------------
# Commit mode: a bare repo holding HEAD as `main`, so a clone from it has
# origin/main exactly as a clone from GitHub does (doctor's runtime:revision
# reads it). A checkout with no .git (plain files copied into a container) is
# committed as-is first.
if [ -n "$REF" ]; then
  ORIGIN="https://github.com/tenishai/techne.git"
  PIN="$REF"
  token="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
  [ -n "$token" ] || token="$(gh auth token 2>/dev/null || true)"
  if [ -n "$token" ]; then
    export GIT_CONFIG_COUNT=1
    export GIT_CONFIG_KEY_0="url.https://x-access-token:${token}@github.com/.insteadOf"
    export GIT_CONFIG_VALUE_0="https://github.com/"
  fi
  unset token
else
  src="$REPO_ROOT"
  if ! git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    src="$WORKDIR/snapshot"
    cp -a "$REPO_ROOT" "$src"
    git -C "$src" init -q
    git -C "$src" add -A
    git -C "$src" -c user.name=install-e2e -c user.email=install-e2e@localhost commit -q -m snapshot
  fi
  ORIGIN="$WORKDIR/origin.git"
  git init -q --bare -b main "$ORIGIN"
  git -C "$ORIGIN" config receive.shallowUpdate true   # CI checks out at depth 1
  git -C "$src" push -q "$ORIGIN" HEAD:refs/heads/main
  PIN="$(git -C "$src" rev-parse HEAD)"
fi
echo "installing from $ORIGIN at $PIN"

# --- the MCP round trip --------------------------------------------------------
# Launches the server with the exact command `init` wrote to ~/.claude.json and
# speaks newline-delimited JSON-RPC to it, as Claude Code does.
cat >"$WORKDIR/mcp_probe.py" <<'PY'
import json, os, subprocess, sys

home = os.environ["HOME"]
entry = json.load(open(os.path.join(home, ".claude.json")))["mcpServers"]["techne"]
cmd = [entry["command"], *entry.get("args", [])]
env = {**os.environ, **entry.get("env", {})}
proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
FACT = "install-e2e probe fact: the plover nests on gravel"

def call(i, method, params=None):
    msg = {"jsonrpc": "2.0", "id": i, "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            sys.exit(f"MCP server exited before answering {method}")
        reply = json.loads(line)
        if reply.get("id") == i:
            if "error" in reply:
                sys.exit(f"{method}: {reply['error']}")
            return reply["result"]

def tool(i, name, args):
    result = call(i, "tools/call", {"name": name, "arguments": args})
    if result.get("isError"):
        sys.exit(f"{name}: {result}")
    return json.loads(result["content"][0]["text"])

call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "install-e2e", "version": "0"}})
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
names = {t["name"] for t in call(2, "tools/list")["tools"]}
missing = {"techne_store", "techne_recall"} - names
if missing:
    sys.exit(f"tools/list lacks {sorted(missing)}")
print(f"    -> {len(names)} tools")
stored = tool(3, "techne_store", {"content": FACT, "knowledge_type": "fact"})
if not stored.get("stored"):
    sys.exit(f"techne_store did not store: {stored}")
found = tool(4, "techne_recall", {"topic": "plover gravel"})
if FACT not in json.dumps(found):
    sys.exit("techne_recall did not return the row just stored")
print(f"    -> stored knowledge {stored['id']} and recalled it")
proc.stdin.close()
proc.wait(timeout=30)
PY

# --- doctor on a fresh install -------------------------------------------------
# PINNED, NOT ASSERTED CLEAN (techne#1318). On a fresh install, before any
# session, doctor exits 1: registered hooks that have never fired and rails with
# no input yet count as "not confirmed working". Until 1318 decides how a fresh
# install should read, this pins TODAY's set by name, so a check that newly
# fails, or one that stops failing, breaks this test instead of slipping by.
# runtime:loaded joins it for a git checkout without `ps` (it reads the process
# table); a packaged install reports it dormant.
EXPECTED_UNCONFIRMED="hook:codex_grep_assist hook:post_tool_use hook:session_end hook:user_prompt_submit rail:consolidate rail:dream"

check_install() {
  local label="$1"
  echo "  + techne init"
  techne init >"$WORKDIR/init-$label.log" 2>&1 || { cat "$WORKDIR/init-$label.log"; fail "$label: techne init failed"; }

  echo "  + every hook event in settings.json runs through techne-hook.sh"
  python3 - <<'PY' || fail "$label: hook registration"
import json, os
s = json.load(open(os.path.expanduser("~/.claude/settings.json")))
want = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact",
        "PostCompact", "Stop", "SessionEnd"}
hooks = s.get("hooks", {})
missing = want - set(hooks)
assert not missing, f"events not registered: {sorted(missing)}"
for event in want:
    cmds = [h["command"] for group in hooks[event] for h in group.get("hooks", [])]
    assert cmds and all("techne-hook.sh" in c for c in cmds), f"{event}: {cmds}"
print(f"    -> {len(want)} events")
PY

  echo "  + MCP: initialize, tools/list, techne_store, techne_recall"
  python3 "$WORKDIR/mcp_probe.py" || fail "$label: MCP round trip"

  echo "  + SessionStart hook, the exact registered command"
  local cmd
  cmd="$(python3 -c 'import json,os; s=json.load(open(os.path.expanduser("~/.claude/settings.json"))); print(s["hooks"]["SessionStart"][0]["hooks"][0]["command"])')"
  printf '%s' '{"session_id":"install-e2e-1","hook_event_name":"SessionStart","source":"startup","cwd":"'"$HOME"'"}' \
    | sh -c "$cmd" >"$WORKDIR/ss-$label.out" 2>"$WORKDIR/ss-$label.err" \
    || { cat "$WORKDIR/ss-$label.err"; fail "$label: SessionStart hook exited non-zero"; }
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$WORKDIR/ss-$label.out" \
    || { head -c 400 "$WORKDIR/ss-$label.out"; fail "$label: SessionStart hook output is not one JSON object"; }

  echo "  + techne doctor"
  local rc=0
  techne doctor >"$WORKDIR/doctor-$label.log" 2>&1 || rc=$?
  local got
  got="$(sed -nE 's/^  [?✗] ([a-z_:-]+) .*/\1/p' "$WORKDIR/doctor-$label.log" | sort | tr '\n' ' ' | sed 's/ $//')"
  echo "    -> exit $rc; not confirmed: ${got:-none}"
  if [ -z "$REF" ]; then
    local want="$EXPECTED_UNCONFIRMED"
    [ "$label" != source ] || command -v ps >/dev/null || want="$want runtime:loaded"
    want="$(tr ' ' '\n' <<<"$want" | sort | tr '\n' ' ' | sed 's/ $//')"
    if [ "$got" != "$want" ]; then
      cat "$WORKDIR/doctor-$label.log"
      fail "$label: doctor's unconfirmed set changed (techne#1318). expected: $want; got: ${got:-none}"
    fi
    [ "$rc" -ne 0 ] || fail "$label: doctor exited 0 — if techne#1318 landed, drop the pin above"
  fi
}

run_source() {
  step "Path: source (git clone, venv, pip install -e ., techne init)"
  export HOME="$WORKDIR/home-source"
  mkdir -p "$HOME"
  (
    cd "$HOME"
    git clone -q "$ORIGIN" techne
    cd techne
    [ -z "$REF" ] || git -c advice.detachedHead=false checkout -q "$PIN"
    python3 -m venv .venv
    # shellcheck disable=SC1091
    . .venv/bin/activate
    pip install -q -e . >"$WORKDIR/pip-source.log" 2>&1 || { tail -20 "$WORKDIR/pip-source.log"; fail "source: pip install -e . failed"; }
    cd "$HOME"
    check_install source
  )
  echo "  source: OK"
}

run_pipx() {
  step "Path: pipx (pipx install [recommended] @ git, ensurepath, new shell, techne init)"
  export HOME="$WORKDIR/home-pipx"
  mkdir -p "$HOME"
  (
    cd "$HOME"
    local pipx=pipx
    if ! command -v pipx >/dev/null; then
      # The README says `apt install pipx` / `brew install pipx`. CI can't use the
      # package manager, so pipx comes from its own venv instead; the install it
      # then performs is the README's.
      python3 -m venv "$WORKDIR/pipx-venv"
      "$WORKDIR/pipx-venv/bin/pip" install -q pipx
      pipx="$WORKDIR/pipx-venv/bin/pipx"
      echo "  (pipx not installed; using pipx from a venv)"
    fi
    local url="$ORIGIN"
    [ -n "$REF" ] || url="file://$ORIGIN"
    "$pipx" install "tenish-techne[recommended] @ git+$url@$PIN" >"$WORKDIR/pipx.log" 2>&1 \
      || { tail -20 "$WORKDIR/pipx.log"; fail "pipx: install failed"; }
    "$pipx" ensurepath >/dev/null 2>&1 || true
    # "Now open a new shell": what ensurepath adds, ~/.local/bin, is on PATH there.
    export PATH="$HOME/.local/bin:$PATH"
    [ "$(command -v techne)" = "$HOME/.local/bin/techne" ] || fail "pipx: techne on PATH is $(command -v techne)"
    check_install pipx

    echo "  + techne vectors backfill (downloads the model, as a user's first run does)"
    techne vectors backfill >"$WORKDIR/backfill.log" 2>&1 || { tail -20 "$WORKDIR/backfill.log"; fail "pipx: vectors backfill failed"; }
    tail -3 "$WORKDIR/backfill.log" | sed 's/^/    /'
    techne vectors status >"$WORKDIR/vstatus.log" 2>&1 || { cat "$WORKDIR/vstatus.log"; fail "pipx: vectors status failed"; }
    sed 's/^/    /' "$WORKDIR/vstatus.log"
    grep -qE 'knowledge_vec: [1-9][0-9]* row' "$WORKDIR/vstatus.log" || fail "pipx: no knowledge row embedded"
  )
  echo "  pipx: OK"
}

for p in $PATHS; do
  case "$p" in
    source) run_source ;;
    pipx) run_pipx ;;
    *) fail "unknown path $p (source or pipx)" ;;
  esac
done
step "install-e2e: all paths OK"
