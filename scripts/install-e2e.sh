#!/usr/bin/env bash
# End-to-end test of the README install paths, run as a user runs them
# (techne#1319): the README's commands, `techne init`, then hooks, the MCP
# server, a store and a recall. Fresh $HOME per path; uv removed from PATH.
#
#   scripts/install-e2e.sh                 # both paths, this checkout's HEAD
#   scripts/install-e2e.sh --path source   # source | pipx
#   scripts/install-e2e.sh --ref v0.18.2   # a published tag over https
#
# Commit mode installs HEAD, not uncommitted edits. --ref needs GH_TOKEN or a
# logged-in gh while the repo is private; a credential helper hands it only to
# the clone and the pipx install. Run by CI's `install-e2e` job.
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

# Only $HOME is replaced per path, so every variable that points techne, Claude
# Code or pip at a real install must go first: an agent seat's TECHNE_HOME and
# CLAUDE_CONFIG_DIR made `techne init` rewrite that seat (PR 1320, round 1).
for v in $(compgen -e); do
  case "$v" in
    TECHNE_*|CLAUDE_*|CLAUDECODE|XDG_*|PIPX_*|PIP_*|VIRTUAL_ENV|PYTHONPATH|PYTHONHOME) unset "$v" ;;
  esac
done

step() { printf '\n\033[1m── %s\033[0m\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# techne's permission checks behave differently for root, and no README reader
# installs as root. Refuse rather than test something nobody runs.
[ "$(id -u)" -ne 0 ] || fail "run as a non-root user (root bypasses the permission checks a user hits)"

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
command -v git >/dev/null || fail "git not on PATH after removing uv's directories (the README requires git)"
command -v python3 >/dev/null || fail "python3 not on PATH after removing uv's directories"

# --- where the install comes from ------------------------------------------
# Commit mode: a bare repo holding HEAD as `main`, so a clone from it has
# origin/main exactly as a clone from GitHub does (doctor's runtime:revision
# reads it). A checkout with no .git (plain files copied into a container) is
# committed as-is first.
TOKEN=""
if [ -n "$REF" ]; then
  ORIGIN="https://github.com/tenishai/techne.git"
  PIN="$REF"
  TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
  [ -n "$TOKEN" ] || TOKEN="$(gh auth token 2>/dev/null || true)"
  unset GH_TOKEN GITHUB_TOKEN
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

# Runs one fetching command with the token, if any, kept out of argv and of
# every later process: a credential helper reads it from a variable exported
# only in this subshell.
with_git_auth() (
  if [ -n "$TOKEN" ]; then
    export E2E_GIT_TOKEN="$TOKEN" GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=credential.https://github.com.helper
    export GIT_CONFIG_VALUE_0='!f() { echo username=x-access-token; echo "password=$E2E_GIT_TOKEN"; }; f'
  fi
  "$@"
)

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
stored = tool(3, "techne_store", {"content": FACT, "type": "fact"})
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
# A fresh install must pass doctor (techne#1318): hooks that have not fired yet
# and rails not yet due read dormant, not failing. runtime:loaded is the one
# exception: it reads the HOST's process table, so it reports whatever techne
# already runs on the machine, not this install (PR 1320 r3). It may fail, and
# then doctor's exit 1 is accepted, only when it is the sole failing line.

check_install() {
  local label="$1"
  echo "  + techne init"
  ! compgen -e | grep -E '^(TECHNE_|CLAUDE_CONFIG_DIR$)' >/dev/null || fail "$label: a TECHNE_/CLAUDE_CONFIG_DIR variable survived; init would write outside \$HOME"
  techne init >"$WORKDIR/init-$label.log" 2>&1 || { cat "$WORKDIR/init-$label.log"; fail "$label: techne init failed"; }
  [ -f "$HOME/.claude/settings.json" ] && grep -qF "Config saved: $HOME/.techne/config.yaml" "$WORKDIR/init-$label.log" \
    || { cat "$WORKDIR/init-$label.log"; fail "$label: techne init wrote somewhere other than the fresh \$HOME"; }

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
  local all got
  all="$(sed -nE 's/^  (\?|✗|!) ([a-z_:-]+) .*/\2/p' "$WORKDIR/doctor-$label.log" | sort | tr '\n' ' ' | sed 's/ $//')"
  got="$(tr ' ' '\n' <<<"$all" | { grep -vx -e 'runtime:loaded' -e '' || true; } | tr '\n' ' ' | sed 's/ $//')"
  echo "    -> exit $rc; not confirmed: ${all:-none}"
  if [ -z "$REF" ]; then
    if [ -n "$got" ] || { [ "$rc" -ne 0 ] && [ "$all" != "runtime:loaded" ]; }; then
      cat "$WORKDIR/doctor-$label.log"
      fail "$label: doctor failed on a fresh install (techne#1318): exit $rc; not confirmed: ${got:-none}"
    fi
  fi
}

run_source() {
  step "Path: source (git clone, venv, pip install -e ., techne init)"
  export HOME="$WORKDIR/home-source"
  mkdir -p "$HOME"
  (
    cd "$HOME"
    with_git_auth git clone -q "$ORIGIN" techne
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
    with_git_auth "$pipx" install "tenish-techne[recommended] @ git+$url@$PIN" >"$WORKDIR/pipx.log" 2>&1 \
      || { tail -20 "$WORKDIR/pipx.log"; fail "pipx: install failed"; }
    "$pipx" ensurepath >/dev/null 2>&1 || true
    # "Now open a new shell": what ensurepath adds, ~/.local/bin, is on PATH there.
    export PATH="$HOME/.local/bin:$PATH"
    [ "$(command -v techne)" = "$HOME/.local/bin/techne" ] || fail "pipx: techne on PATH is $(command -v techne)"
    check_install pipx

    # init already downloaded the model cold (it defaults to --vectors, and never
    # fails on a failed download); backfill is the step that proves it worked.
    echo "  + techne vectors backfill"
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
