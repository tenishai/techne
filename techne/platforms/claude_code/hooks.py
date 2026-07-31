"""Claude Code platform adapter — hook registration and MCP setup."""

import json
import os
import shlex
import sys
from pathlib import Path

from techne.config import Config
from techne.platforms.claude_code.paths import claude_config_dir, claude_json_path

# Import-time snapshots, honoring CLAUDE_CONFIG_DIR set before launch. The hook
# installer runs as a fresh `techne` process per invocation, so the env is fixed
# by then; tests monkeypatch these module attributes directly.
SETTINGS_PATH = claude_config_dir() / "settings.json"
MCP_CONFIG_PATH = claude_json_path()

# The standalone editable-install guard, shipped alongside this adapter. Wired as
# the first SessionStart hook; see preflight_guard.py for why it must be invoked
# by absolute path (it heals the very pointer that `-m techne...` depends on).
PREFLIGHT_GUARD = Path(__file__).resolve().parent / "preflight_guard.py"


def _install_root() -> Path:
    """Repo root of the running techne install — the editable pointer's target.

    ``<repo>/techne/platforms/claude_code/hooks.py`` → ``parents[3]`` is ``<repo>``.
    """
    return Path(__file__).resolve().parents[3]


def _is_worktree_path(p: Path) -> bool:
    """True if ``p`` is a git worktree — ASK GIT, don't guess from the path.

    KEEP IN SYNC with the identical copy in ``preflight_guard._is_worktree_path``.
    The duplication is deliberate: preflight_guard runs as a bare script *before*
    techne is known to be importable, so it cannot import this module.
    ``tests/test_worktree_guard.py`` fails if the two drift.

    A linked worktree writes ``.git`` as a FILE containing ``gitdir: <path>``, where
    a normal clone has a ``.git`` DIRECTORY. The test is then whether that git-dir
    contains a ``commondir`` file — git's own marker for "this git-dir is not the
    common one", i.e. exactly the ``git_dir != common_dir`` invariant already used
    in ``coord.py:397``, which reads it via ``git rev-parse`` and whose comment
    likewise warns that string-matching the path is fragile.

    Do NOT match ``/.git/worktrees/`` in the marker text: that silently assumes the
    git-dir is named ``.git``. A worktree of a BARE repo — the mainstream workflow —
    has ``gitdir: /srv/proj.git/worktrees/<id>`` and would be missed, as would
    worktrees of a ``--separate-git-dir`` clone or of a submodule. Verified against
    real git: the string form returned False for a bare-repo worktree
    (Prudence, PR #435).

    This previously matched only paths under ``.claude/worktrees/`` — the harness's
    own convention — so a worktree created anywhere else sailed straight through the
    guard in ``register_hooks``. That is not hypothetical: on 2026-07-31 a manual
    ``git worktree add ~/working/tenishai/techne-wt-428`` was registered, baked into
    ``~/.claude/settings.json`` and the launcher, and took every hook on the machine
    down when the worktree was pruned (techne#434, third occurrence of #578's class).

    A git SUBMODULE also uses a ``.git`` file, but points at ``.git/modules/<name>``.
    The ``worktrees`` path segment is what distinguishes them, and a submodule
    checkout is stable — it must NOT be refused.

    No subprocess: this runs on the preflight hot path and must still answer when
    git is absent, or the marker is unreadable.
    """
    p = p.resolve()
    marker = p / ".git"
    try:
        if marker.is_file():
            head = marker.read_text(encoding="utf-8", errors="replace")[:4096]
            for line in head.splitlines():
                line = line.strip()
                if not line.startswith("gitdir:"):
                    continue
                gitdir = Path(line.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = (p / gitdir).resolve()
                # `commondir` exists ONLY inside a linked worktree's admin dir —
                # git's own marker for "this git-dir is not the common one". A
                # primary checkout's `.git/` never has one, so walking UP is safe.
                #
                # The walk (rather than a single check) covers a SUBMODULE inside a
                # worktree: its git-dir is `<admin>/worktrees/<id>/modules/<name>`,
                # which has no `commondir` of its own but lives under one. That
                # nesting must still count — pruning the worktree takes the
                # submodule's git-dir with it.
                for d in (gitdir, *gitdir.parents):
                    if (d / "commondir").exists():
                        return True
                return False
    except OSError:
        pass  # unreadable marker: fall through to the path heuristic below

    # Retained as a belt-and-braces fallback: catches the harness's worktree dir
    # even if the marker is missing or unreadable.
    parts = p.parts
    return any(
        parts[i] == ".claude" and parts[i + 1] == "worktrees"
        for i in range(len(parts) - 1)
    )


def _launcher_path() -> Path:
    """Path to the generated POSIX launcher in the claude config dir (techne#276).

    Derived from SETTINGS_PATH so test monkeypatching of SETTINGS_PATH
    automatically redirects the launcher to the fake home too.
    """
    return SETTINGS_PATH.parent / "techne-hook.sh"


def _interpreter() -> Path:
    """The interpreter register_hooks is about to bake in (launcher `PY=`, MCP
    command, heal). A seam, not indirection for its own sake: tests must be able
    to pin it without monkeypatching the global ``sys.executable``, which would
    break every test that spawns a subprocess.
    """
    return Path(sys.executable)


def _under_worktree(p: Path) -> Path | None:
    """The worktree ancestor of *p*, or None. Checks *p* and every parent.

    `_is_worktree_path` answers "is this dir a worktree root"; register_hooks bakes
    in paths that merely LIVE inside one. The gap that matters (Prudence, PR #435):
    a plain (non-editable) install into `<worktree>/.venv` puts `_install_root()`
    in site-packages — no `.git`, guard passes — while `sys.executable` is still
    `<worktree>/.venv/bin/python`, so a prunable path lands in machine-global
    config anyway. Walking ancestors closes that.
    """
    try:
        p = p.resolve()
    except OSError:
        return None
    for d in (p, *p.parents):
        if _is_worktree_path(d):
            return d
    return None


def _heal_command() -> str:
    """Install-method-aware heal command baked into the launcher (techne#276).

    Three cases, mirroring preflight_guard._pip_install_editable's venv detection:

    - **pipx** (sys.executable contains ``pipx/venvs/``):
      ``pipx reinstall --python "$(command -v python3)" techne``.
      Explicitly passes ``--python`` to force the PATH-resolved ``python3`` rather than
      pipx's default (``sys.executable`` on non-Windows — i.e. the Python that pipx
      itself runs under; verified against pipx source at pypa/pipx main, 2026-07-07).
      By naming ``python3`` explicitly we survive the common case of a minor-version
      removal (e.g. 3.11→3.12 Homebrew upgrade) as long as ``python3`` and ``pipx`` are
      on PATH at heal time. MEDIUM confidence. Best-effort path.

    - **system/non-venv** (sys.prefix == sys.base_prefix): attempt ``pip install -e``
      under whatever ``python3`` PATH resolves to at heal time, with
      ``--break-system-packages`` (same flag logic as preflight_guard), then reassign
      ``PY="$(command -v python3)"`` so the ``exec "$PY" "$@"`` that follows targets the
      freshly-resolved interpreter rather than the original dead one. Works if ``python3``
      is on PATH at heal time; ``command -v`` returning empty still fails loudly. Best-effort.

    - **plain venv, non-pipx**: safe automatic rebuild is impossible once the base
      interpreter is gone — print a clear remediation message to stderr and exit 1.
      The message includes pipx/uv alternatives for users who may have installed via
      those tools but whose interpreter path did not match the ``pipx/venvs/`` pattern.
      The user must recreate and reinstall manually (techne#276).
    """
    exe = sys.executable
    in_venv = sys.prefix != sys.base_prefix
    is_pipx = "pipx/venvs/" in exe
    repo = _install_root()

    if is_pipx:
        # Use --python to avoid relying on pipx's DEFAULT_PYTHON (= its own sys.executable).
        return 'pipx reinstall --python "$(command -v python3)" techne'

    if not in_venv:
        # system install: fresh editable install under PATH's python3, then re-point PY
        # so the exec that follows targets the new interpreter, not the dead one.
        return (
            f"python3 -m pip install -e {shlex.quote(str(repo))} --break-system-packages"
            '; PY="$(command -v python3)"'
        )

    # plain venv, non-pipx: safe automatic rebuild is impossible once the interpreter is
    # gone — emit a clear remediation message (with pipx/uv alternatives) and exit 1.
    msg = (
        f"[techne] interpreter dead ({exe}) — "
        f"recreate the venv and reinstall: "
        f"python3 -m venv {sys.prefix} && "
        f"{sys.prefix}/bin/pip install -e {repo}"
        f"; or if installed via pipx/uv: "
        f"pipx reinstall techne / uv tool install --force techne"
    )
    return f"echo {shlex.quote(msg)} >&2; exit 1"


def _write_launcher() -> Path:
    """Generate (or regenerate) the POSIX launcher; called by register_hooks (techne#276).

    The launcher is an executable ``/bin/sh`` script that:

    1. Tests whether the baked-in interpreter (``$PY``) is still executable — a single
       ``-x`` stat. A dangling symlink tests as non-executable, so the happy path costs
       one stat and no subprocess.
    2. Fires the install-method-aware heal command only when the interpreter is dead.
    3. ``exec``'s ``$PY`` with all original arguments.

    Regenerated on every ``register_hooks`` call so interpreter and heal stay in sync.

    Written atomically (write to a sibling ``.tmp.<pid>`` then ``os.replace``): a hook
    firing mid-write would otherwise read an empty/partial script and silently not fire.
    Mirrors ``_save_json``'s atomic-write pattern.

    POSIX only (macOS + Linux). Windows ``.cmd``/``.ps1`` is a follow-up.
    """
    launcher = _launcher_path()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "#!/bin/sh\n"
        f"PY={shlex.quote(sys.executable)}\n"
        '[ -x "$PY" ] || { ' + _heal_command() + "; }\n"
        'exec "$PY" "$@"\n'
    )
    tmp = launcher.with_name(f"{launcher.name}.tmp.{os.getpid()}")
    tmp.write_text(content)
    tmp.chmod(0o755)
    os.replace(tmp, launcher)
    return launcher


def _preflight_command() -> str:
    # Route through the launcher (#276) so a dead interpreter triggers the
    # install-method heal before the guard runs. The guard is still invoked by
    # absolute path — it cannot ``import techne``, so ``-m`` would be a
    # chicken-and-egg trap; see preflight_guard.py for the full rationale.
    return (
        f"/bin/sh {shlex.quote(str(_launcher_path()))} "
        f"{shlex.quote(str(PREFLIGHT_GUARD))}"
    )


def _hook_command(module: str) -> str:
    # Route through the launcher (#276) so a dead Homebrew interpreter triggers
    # auto-heal before the module is imported. Same interpreter pin as before —
    # the launcher bakes in sys.executable at register time; the happy-path overhead
    # is a single -x stat then exec.
    return f"/bin/sh {shlex.quote(str(_launcher_path()))} -m {module}"


def _group_contains_techne(group: dict) -> bool:
    """True if a settings.json hook-group entry references techne anywhere.

    Handles both the wrapped shape ``{matcher, hooks: [{type, command}, ...]}``
    and the legacy flat shape ``{type, command}`` that earlier techne versions
    (pre-0.4.1) wrote — we still need to recognize and replace those so a
    re-init cleans up an old install.

    Also recognizes the new launcher-based shape (techne#276): commands contain
    ``techne`` via both the launcher filename (``techne-hook.sh``) and the
    ``-m techne.hooks.X`` module argument, so this check requires no change.
    """
    if "command" in group and "techne" in (group.get("command") or ""):
        return True
    for h in group.get("hooks") or []:
        if "techne" in (h.get("command") or ""):
            return True
    return False


def register_hooks(config: Config):
    """Register techne hooks in Claude Code settings.json.

    Writes the current Claude Code hook shape:
    ``{<Event>: [{"matcher": "", "hooks": [{"type": "command", "command": "..."}]}]}``.
    Empty matcher means "every tool / every fire of this event".
    """
    # Refuse to bake a worktree path into the config. If techne is installed from
    # a git worktree, its editable pointer — and everything we're about to wire —
    # will break the moment that worktree is pruned (the -32000 footgun). Fail
    # loud with the remediation rather than register a self-destructing install.
    #
    # The guard is deliberately STRICT — no sandbox carve-out. Tests are isolated
    # from the host repo layout by conftest's autouse `_isolate_install_root`,
    # mirroring how `_isolate_host_config` (techne#288) already pins SETTINGS_PATH.
    # An earlier attempt at this change instead let the guard stand down whenever
    # SETTINGS_PATH was redirected; that made the suite pass but silently weakened
    # the guard in every sandboxed context, and broke the existing
    # `test_refuses_worktree_install`. Isolating the input is right; loosening the
    # invariant to fit the test harness is not.
    # Check EVERY prunable path this function is about to bake into machine-global
    # config, not just the install root: the launcher's `PY=`, the MCP server
    # command and the heal all carry `sys.executable`, and SessionStart carries the
    # preflight guard's absolute path. A non-editable install into `<worktree>/.venv`
    # leaves _install_root() in site-packages — clean — while sys.executable is
    # still inside the worktree (Prudence, PR #435).
    for label, candidate in (
        ("install root", _install_root()),
        ("interpreter", _interpreter()),
        ("preflight guard", PREFLIGHT_GUARD),
    ):
        wt = _under_worktree(candidate)
        if wt is not None:
            raise RuntimeError(
                f"techne's {label} ({candidate}) is inside a git worktree ({wt}). "
                "Wiring that into ~/.claude would break the MCP server and every "
                "techne hook the moment the worktree is pruned. Reinstall from your "
                "canonical checkout, then re-run `techne init`:\n"
                f"  {sys.executable} -m pip install -e <canonical-techne-checkout>"
            )

    # Generate the interpreter launcher before baking paths into settings.json
    # (techne#276): hooks route through the launcher so a Homebrew upgrade/removal
    # that kills sys.executable triggers auto-heal rather than silently breaking.
    _write_launcher()

    settings = _load_json(SETTINGS_PATH)
    if settings is None:
        settings = {}

    hooks = settings.setdefault("hooks", {})

    # SessionStart chains the preflight guard and session_start into ONE shell
    # command joined by `;`. This is load-bearing: Claude Code runs all hooks
    # matching an event IN PARALLEL with no ordering guarantee — even multiple
    # commands inside a single group's `hooks` array
    # (https://code.claude.com/docs/en/hooks-guide). So two separate hooks would
    # RACE: session_start's `import techne` could fire before the guard heals the
    # pointer. Sequencing them in one shell process is the only way to guarantee
    # the guard runs to completion first; session_start then starts as a fresh
    # process and reads the just-healed `.pth` at its own interpreter startup.
    # `;` (not `&&`) so a guard crash can never suppress session_start — a broken
    # guard must not regress the normal hook when the pointer was fine anyway.
    # Both halves route through the launcher (#276) so interpreter-death heal
    # fires before either the guard or session_start tries to import anything.
    session_start = (
        f"{_preflight_command()} ; {_hook_command('techne.hooks.session_start')}"
    )
    commands_per_event = {
        "SessionStart": session_start,
        "PreCompact": _hook_command("techne.hooks.pre_compact"),
        "PostCompact": _hook_command("techne.hooks.post_compact"),
        "Stop": _hook_command("techne.hooks.session_end"),
    }
    if config.hooks.post_tool_use:
        commands_per_event["PostToolUse"] = _hook_command("techne.hooks.post_tool_use")

    for event, command in commands_per_event.items():
        groups = hooks.setdefault(event, [])
        # Strip any prior techne entry, in either the wrapped or legacy shape.
        # This also migrates a hand-wired preflight hook: its command contains
        # "techne", so it's recognized, removed, and replaced by the packaged one.
        groups[:] = [g for g in groups if not _group_contains_techne(g)]
        groups.append(
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": command}],
            }
        )

    # PreToolUse grep-assist (techne#206) — OPT-IN, experimental. Only registered
    # when hooks.codex_grep_assist is ON. Unlike the always-on hooks above it uses
    # a "Grep" matcher so it fires only on the agent's own content searches, and
    # it's the sole techne PreToolUse entry — so a flipped-off flag on re-init
    # cleanly removes it (the strip below drops any prior techne PreToolUse group).
    pre_groups = hooks.setdefault("PreToolUse", [])
    pre_groups[:] = [g for g in pre_groups if not _group_contains_techne(g)]
    if config.hooks.codex_grep_assist:
        pre_groups.append(
            {
                "matcher": "Grep",
                "hooks": [
                    {"type": "command", "command": _hook_command("techne.hooks.pre_tool_use")}
                ],
            }
        )
    if not pre_groups:
        del hooks["PreToolUse"]

    # UserPromptSubmit per-turn recall (techne#109/#277) — OPT-IN, experimental.
    # The DYNAMIC per-turn complement to the stable SessionStart injection: on each
    # prompt, inject the durable facts most relevant to it. Gated on
    # hooks.per_turn_recall; empty matcher (UserPromptSubmit isn't tool-scoped). It's
    # the sole techne UserPromptSubmit entry, so a flipped-off flag on re-init cleanly
    # removes it (the strip drops any prior techne UserPromptSubmit group).
    ups_groups = hooks.setdefault("UserPromptSubmit", [])
    ups_groups[:] = [g for g in ups_groups if not _group_contains_techne(g)]
    if config.hooks.per_turn_recall:
        ups_groups.append(
            {
                "matcher": "",
                "hooks": [
                    {"type": "command", "command": _hook_command("techne.hooks.user_prompt_submit")}
                ],
            }
        )
    if not ups_groups:
        del hooks["UserPromptSubmit"]

    _save_json(SETTINGS_PATH, settings)


def _expected_registrations(config: Config) -> list[tuple[str, str]]:
    """The ``(event, matcher)`` techne registrations ``register_hooks`` writes for
    this config — the single source of truth for "correctly registered" (techne#287).

    MUST stay in lockstep with ``register_hooks`` above: any hook whose registration
    is gated on a config flag there needs its gate mirrored here, or ``check_hook_drift``
    will diverge from what init actually writes.
    """
    expected = [
        ("SessionStart", ""),
        ("PreCompact", ""),
        ("PostCompact", ""),
        ("Stop", ""),
    ]
    if config.hooks.post_tool_use:
        expected.append(("PostToolUse", ""))
    if config.hooks.codex_grep_assist:
        expected.append(("PreToolUse", "Grep"))
    if config.hooks.per_turn_recall:
        expected.append(("UserPromptSubmit", ""))
    return expected


def check_hook_drift(config: Config) -> list[str]:
    """Report drift between config-enabled techne hooks and what's actually wired
    in settings.json. Returns human-readable warnings; empty list == in sync (techne#287).

    Catches the silent-no-op failure mode where a ``hooks.*`` flag is flipped in
    ``config.yaml`` but ``techne init`` was never re-run, so the hook is enabled in
    config yet absent from settings.json and never fires — with no other diagnostic.

    Comparison is COARSE: by ``(event, matcher)`` presence of a techne-referencing
    group (``_group_contains_techne``), NOT exact command match. Interpreter and
    launcher-path differences (a re-pinned shim, a moved home) must not false-positive;
    only genuinely missing or orphaned registrations are reported.
    """
    try:
        settings = _load_json(SETTINGS_PATH)
    except RuntimeError:
        # settings.json exists but is unparseable — register/unregister already
        # surface that loudly with remediation; don't double-report it here.
        return []
    if settings is None:
        settings = {}
    hooks = settings.get("hooks", {})

    def _has_techne_group(event: str, matcher: str) -> bool:
        for g in hooks.get(event, []):
            if (g.get("matcher") or "") == matcher and _group_contains_techne(g):
                return True
        return False

    expected = _expected_registrations(config)
    expected_set = set(expected)
    warnings: list[str] = []

    def _label(event: str, matcher: str) -> str:
        return f"{event}/{matcher}" if matcher else event

    # Missing: config enables it, settings.json has no techne group for it. This is
    # the reported incident — grep-assist ON in config, PreToolUse absent (techne#287).
    for event, matcher in expected:
        if not _has_techne_group(event, matcher):
            warnings.append(
                f"{_label(event, matcher)} hook enabled in config but NOT "
                f"registered in settings.json"
            )

    # Orphaned: a techne group wired for an (event, matcher) config no longer enables
    # (e.g. a flag flipped off without a re-init) — it will keep firing until re-init.
    for event, groups in hooks.items():
        for g in groups:
            matcher = g.get("matcher") or ""
            if _group_contains_techne(g) and (event, matcher) not in expected_set:
                warnings.append(
                    f"{_label(event, matcher)} techne hook registered in "
                    f"settings.json but not enabled in config"
                )

    return warnings


def register_mcp(config: Config):
    """Register the techne MCP server in Claude's MCP config.

    NOTE: MCP config is command+args (not a shell line) with different restart
    semantics — the interpreter launcher from register_hooks deliberately does not
    cover MCP yet (follow-up, techne#276).
    """
    mcp_config = _load_json(MCP_CONFIG_PATH)
    if mcp_config is None:
        mcp_config = {}

    servers = mcp_config.setdefault("mcpServers", {})
    servers["techne"] = {
        "command": sys.executable,
        "args": ["-m", "techne.mcp_server"],
        "env": {},
    }

    _save_json(MCP_CONFIG_PATH, mcp_config)


def unregister(config: Config):
    """Remove techne hooks and MCP registration."""
    # Remove hooks (handle both wrapped and legacy shapes). Uninstall must be
    # resilient: a corrupt settings.json shouldn't strand the user — _load_json
    # raises on it (correct for register, which must not clobber), but here we
    # warn and press on so the MCP-config cleanup below still runs (techne#166).
    try:
        settings = _load_json(SETTINGS_PATH)
    except RuntimeError as e:
        print(f"techne: skipping hook cleanup: {e}", file=sys.stderr)
        settings = None
    if settings:
        hooks = settings.get("hooks", {})
        for event in list(hooks.keys()):
            hooks[event] = [g for g in hooks[event] if not _group_contains_techne(g)]
            if not hooks[event]:
                del hooks[event]
        _save_json(SETTINGS_PATH, settings)

    # Remove the generated launcher so it doesn't orphan after uninstall.
    _launcher_path().unlink(missing_ok=True)

    # Remove MCP server
    try:
        mcp_config = _load_json(MCP_CONFIG_PATH)
    except RuntimeError as e:
        print(f"techne: skipping MCP cleanup: {e}", file=sys.stderr)
        mcp_config = None
    if mcp_config:
        servers = mcp_config.get("mcpServers", {})
        servers.pop("techne", None)
        _save_json(MCP_CONFIG_PATH, mcp_config)


def _load_json(path: Path) -> dict | None:
    """Load a JSON config. Returns None ONLY when the file genuinely does not exist
    (caller starts fresh). An EXISTING-but-unparseable file RAISES — returning None
    there would make register_hooks treat it as empty and OVERWRITE the user's real
    Claude Code settings with just ours (techne#166). Fail loud, lose nothing."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(
            f"{path} exists but could not be read/parsed ({e}); refusing to overwrite it. "
            "Fix or remove the file, then re-run."
        ) from e


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: a crash mid-write must never leave the file truncated/empty — the
    # next _load_json would then (now) raise, or pre-#166 silently wipe the user's
    # settings. Write a sibling temp then os.replace (POSIX-atomic rename). The pid
    # suffix keeps two racing writers (e.g. concurrent `techne init`) from sharing
    # one temp and interleaving bytes.
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
