"""Claude Code platform adapter — hook registration and MCP setup."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from techne.config import CLAUDE_CODE, Config
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


# Per-path memo for the import probe. register_hooks runs it for at most two
# candidates, but the test suite calls register_hooks dozens of times; without this
# each call would spawn a fresh interpreter.
# Every module the registered commands invoke under the baked-in interpreter — the
# UNION the probe must satisfy. Previously the probe asserted only
# `techne.hooks.session_start`, which Prudence measured as tied for the WEAKEST of
# the eight: techne/__init__.py is lazy (PEP 562) and techne/hooks/__init__.py is
# empty, so it proves three files exist. Two shapes passed it while hooks died — a
# partial install, and a complete source tree with PyYAML missing (which kills
# pre_compact/post_compact/post_tool_use and techne.mcp_server). mcp_server matters
# most: register_mcp bakes the same interpreter and the old probe did not represent
# it at all. Kept as ONE tuple because this list was duplicated across four call
# sites, and drift between them is how this class of bug keeps recurring.
# (Prudence, PR #460.)
_BAKED_IN_MODULES = (
    "techne.hooks.session_start",
    "techne.hooks.session_end",
    "techne.hooks.pre_compact",
    "techne.hooks.post_compact",
    "techne.hooks.pre_tool_use",
    "techne.hooks.post_tool_use",
    "techne.hooks.user_prompt_submit",
    "techne.mcp_server",
)

_IMPORT_PROBE: dict[str, bool] = {}
# Generous on purpose: a cold interpreter on a slow/networked filesystem can take
# seconds just to start. This bounds a hung probe rather than policing performance —
# and a TIMEOUT IS NOT THE SAME ANSWER AS "cannot import", so it warns on stderr
# instead of being folded silently into a False (Prudence, PR #460).
_PROBE_TIMEOUT = 15
# (is_venv, sys.prefix) per interpreter — see _venv_state.
_VENV_PROBE: dict[str, tuple[bool, str]] = {}


def _probe_env() -> dict[str, str]:
    """Environment for the import probe, with ``PYTHONPATH`` entries that resolve
    INTO the install root stripped out.

    Neutralizing ``cwd`` is not enough. ``PYTHONPATH`` is the same masking channel
    wearing a different coat: measured, ``_can_import_techne(/usr/bin/python3)`` is
    False bare and True under ``PYTHONPATH=<checkout>``, so
    ``PYTHONPATH=. python3 -m techne.cli init`` would reproduce techne#458 straight
    through this guard. (Prudence, PR #460.)

    A blanket ``-E`` is deliberately NOT used: it would also drop ``PYTHONHOME``,
    virtualenv plumbing and proxy/CA settings that a legitimate install may need, so
    it could fail a genuinely good interpreter. Strip only the masking entries.
    """
    env = dict(os.environ)
    raw = env.get("PYTHONPATH")
    if not raw:
        return env
    try:
        root = _install_root().resolve()
    except OSError:
        return env
    kept: list[str] = []
    for entry in raw.split(os.pathsep):
        if not entry:
            continue
        try:
            # Relative entries (".", "") resolve against the CURRENT cwd — which is
            # exactly the checkout in the incident shape, so this catches them too.
            resolved = Path(entry).resolve()
        except OSError:
            kept.append(entry)  # unresolvable: can't prove it masks, so keep it
            continue
        if resolved == root or root in resolved.parents:
            continue
        # Strip anything that SUPPLIES techne, not merely anything under our root.
        # The root is literally named `techne`, so its PARENT is a namespace-package
        # source; a second checkout or worktree is another. Both were measured
        # masking a broken interpreter while sitting nowhere near this root.
        # (Prudence, PR #460.)
        # `.is_dir()` alone: an `__init__.py` check would be dead code, since its
        # existence implies the parent is a directory (Prudence, PR #460).
        if (resolved / "techne").is_dir():
            continue
        kept.append(entry)
    if kept:
        env["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        env.pop("PYTHONPATH", None)
    return env


def _venv_state(exe: Path) -> tuple[bool, str]:
    """``(is_venv, sys.prefix)`` OF THE GIVEN INTERPRETER — asked of it, not of us.

    Before techne#458 the interpreter baked into the launcher was always the one
    running init, so reading our own ``sys.prefix``/``sys.base_prefix`` was correct
    by construction. Now that ``_interpreter()`` can return a DIFFERENT interpreter,
    mixing the two produces actively wrong remediation — naming one venv as dead
    while telling the operator to rebuild another, or routing a venv install down
    the ``--break-system-packages`` branch. (Prudence, PR #460.)
    """
    key = str(exe)
    if key in _VENV_PROBE:
        return _VENV_PROBE[key]
    if key == sys.executable:
        state = (sys.prefix != sys.base_prefix, sys.prefix)
    else:
        state = (False, "")
        try:
            proc = subprocess.run(
                [key, "-c", "import sys;print(int(sys.prefix!=sys.base_prefix));print(sys.prefix)"],
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT,
            )
            lines = proc.stdout.splitlines()
            if proc.returncode == 0 and len(lines) >= 2:
                state = (lines[0].strip() == "1", lines[1].strip())
        except subprocess.TimeoutExpired:
            # Loud, for the same reason _can_import_techne's is: folding a timeout
            # into (False, "") silently routes a VENV install down the
            # --break-system-packages branch — the exact harm this function exists
            # to prevent, forty lines from where the sibling timeout is already
            # made loud. (Prudence, PR #460.)
            print(
                f"techne: venv probe timed out after {_PROBE_TIMEOUT}s: {key}\n"
                "        heal command may name the wrong install method.",
                file=sys.stderr,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    _VENV_PROBE[key] = state
    return state


def _can_import_techne(candidate: Path) -> bool:
    """Can *candidate* ``import techne`` from a NEUTRAL working directory?

    The neutral cwd is the whole point, not defensive dressing. In the 2026-08-02
    incident the poisoning interpreter imported techne **successfully** — because
    the command ran inside the repo checkout, where ``techne/`` is importable from
    cwd. An in-place probe would have passed and pinned a Homebrew python with no
    techne installed, which is precisely what happened. Probing somewhere that
    cannot mask a missing install is what makes this a real check.

    Never raises: any failure to probe is reported as "cannot import".
    """
    key = str(candidate)
    if key in _IMPORT_PROBE:
        return _IMPORT_PROBE[key]
    ok = False
    try:
        with tempfile.TemporaryDirectory() as neutral:
            proc = subprocess.run(
                # Probe EVERY module that gets baked in, not a representative one.
                # Under PEP 420 `import techne` can succeed from a namespace package
                # supplying no submodules, and any single hook module proves only its
                # own import closure — a missing third-party dep (PyYAML) kills four
                # of the eight while the other four still import.
                [key, "-c", "import " + ",".join(_BAKED_IN_MODULES)],
                cwd=neutral,
                env=_probe_env(),  # cwd alone is not enough — see _probe_env
                capture_output=True,
                timeout=_PROBE_TIMEOUT,
            )
            ok = proc.returncode == 0
    except subprocess.TimeoutExpired:
        # Distinct from "cannot import": we did not get an answer. Still refuse to
        # pin it (an interpreter we can't verify must not reach ~/.claude), but say
        # so, because a silent False here looks identical to a real failure.
        print(
            f"techne: interpreter probe timed out after {_PROBE_TIMEOUT}s: {key}\n"
            "        treating it as unusable — re-run if this was a transient stall.",
            file=sys.stderr,
        )
        ok = False
    except (OSError, subprocess.SubprocessError):
        ok = False
    _IMPORT_PROBE[key] = ok
    return ok


def _imports_only_via_pythonpath(candidate: Path) -> bool:
    """True if *candidate* imports the baked-in modules ONLY with PYTHONPATH intact.

    Distinguishes two shapes that the strict probe reports identically, because they
    ARE identical to a prober: an interpreter with no techne being MASKED by
    PYTHONPATH, and a legitimate `pip install --target=DIR` + exported PYTHONPATH.

    We still refuse both — and that is deliberate, not an oversight. The hooks run
    from Claude Code's environment, not from the one that ran `techne init`, so an
    install reachable only through an exported PYTHONPATH is fragile in exactly the
    way the masked one is broken: same fragility, different intent. What we owe the
    operator is not acceptance but an accurate diagnosis, since a bare "cannot
    determine a stable interpreter" is baffling when `python -c "import techne"`
    plainly works in their shell. (Prudence measured the false-negative on PR #460.)
    """
    try:
        with tempfile.TemporaryDirectory() as neutral:
            proc = subprocess.run(
                [str(candidate), "-c", "import " + ",".join(_BAKED_IN_MODULES)],
                cwd=neutral,
                env=dict(os.environ),  # UNSTRIPPED, deliberately
                capture_output=True,
                timeout=_PROBE_TIMEOUT,
            )
            return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _console_script_interpreter() -> Path | None:
    """The interpreter behind the installed ``techne`` console script, read from its
    shebang — the stable, discoverable interpreter techne#576 asked for.

    This is the fallback that repairs the failure mode rather than merely detecting
    it: when the running interpreter can't import techne, the installed entry point
    almost always can (pipx venv, uv tool, plain venv), because that is what the
    operator actually installed. Returns None when there's no console script or its
    shebang isn't parseable — callers treat that as "no candidate".
    """
    script = shutil.which("techne")
    if not script:
        return None
    try:
        with open(script, encoding="utf-8", errors="replace") as fh:
            first = fh.readline().strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None  # not a POSIX text script (e.g. a Windows launcher .exe)
    try:
        parts = shlex.split(first[2:])
    except ValueError:
        return None
    return Path(parts[0]) if parts else None


def _interpreter() -> Path:
    """The interpreter register_hooks is about to bake in (launcher ``PY=``, MCP
    command, heal). A seam, not indirection for its own sake: tests must be able
    to pin it without monkeypatching the global ``sys.executable``, which would
    break every test that spawns a subprocess.

    VALIDATED, not assumed (techne#458). Baking in a bare ``sys.executable`` has
    poisoned this machine's hooks three times (#574/#576/#584/#587): running
    ``techne`` under any interpreter that can't import techne kills EVERY hook with
    ``ModuleNotFoundError``, and the launcher's own liveness guard can't catch it
    because it tests ``[ -x "$PY" ]`` — the poisoned interpreter exists and is
    executable, it just has no techne. techne#576 named the fix on 2026-07-06:

        techne should pin a stable, discoverable interpreter (the install's own
        venv) rather than whatever ``sys.executable`` happens to be.

    Order: the running interpreter first (correct in the overwhelmingly common
    case), then the installed console script's interpreter. Each must prove it can
    import techne from a neutral cwd before it is written anywhere.

    Raises RuntimeError when nothing validates. That is deliberate: a failed
    ``techne init`` is visible and fixable in one command, whereas silently pinning
    a broken interpreter takes down every hook with no diagnostic and has, three
    times now, cost hours to trace back.
    """
    candidates = [Path(sys.executable)]
    console = _console_script_interpreter()
    if console is not None and console != candidates[0]:
        candidates.append(console)

    for candidate in candidates:
        if _can_import_techne(candidate):
            if candidate != candidates[0]:
                # SAY SO. Substituting a different install is the right repair, but
                # doing it silently means the operator's ~/.claude quietly points at
                # an interpreter they did not choose and cannot see (Prudence, #460).
                print(
                    f"techne: {candidates[0]} cannot import techne; wiring hooks to "
                    f"{candidate} (from the installed `techne` console script) instead.",
                    file=sys.stderr,
                )
            return candidate

    if any(_imports_only_via_pythonpath(c) for c in candidates):
        raise RuntimeError(
            "techne is importable only via PYTHONPATH, not from the interpreter "
            "itself.\n"
            "Hooks run in Claude Code's environment, not the one running this "
            "command, so a PYTHONPATH-only install would leave every hook dead with "
            "ModuleNotFoundError (techne#458).\n"
            "Install it INTO an interpreter instead:\n"
            "  pipx install techne          # or: pipx install -e <checkout>\n"
            "  <python> -m pip install -e <canonical-techne-checkout>\n"
            "A --target layout cannot be made to work here: the probe strips exactly "
            "the PYTHONPATH entry that supplies techne, so re-running init after "
            "exporting it produces this same message."
        )
    tried = "\n".join(f"  - {c}" for c in candidates)
    raise RuntimeError(
        "techne cannot determine a stable interpreter to wire into ~/.claude.\n"
        "None of these could `import techne` from a neutral working directory:\n"
        f"{tried}\n"
        "Writing one of them anyway would break every techne hook with "
        "ModuleNotFoundError (techne#458).\n"
        "Install techne so its entry point resolves, then re-run `techne init`:\n"
        "  pipx install techne        # or: pipx install -e <checkout>\n"
        f"  {sys.executable} -m pip install -e <canonical-techne-checkout>\n"
        "NOTE: running `techne init` from inside the source checkout is NOT enough "
        "— the repo cwd makes techne importable there but not from where hooks run."
    )


def _under_worktree(p: Path) -> Path | None:
    """The worktree ancestor of *p*, or None. Checks *p* and every parent.

    `_is_worktree_path` answers "is this dir a worktree root"; register_hooks bakes
    in paths that merely LIVE inside one. The gap that matters (Prudence, PR #435):
    a plain (non-editable) install into `<worktree>/.venv` puts `_install_root()`
    in site-packages — no `.git`, guard passes — while `sys.executable` is still
    `<worktree>/.venv/bin/python`, so a prunable path lands in machine-global
    config anyway. Walking ancestors closes that.

    Checks the LITERAL path AND its resolved form, and this is load-bearing in both
    directions:

    * Resolving FIRST is wrong, which is what the first version did. A venv's
      ``bin/python`` is a symlink to the base interpreter, so ``resolve()`` walks
      straight OUT of the worktree and the check goes inert for the mainstream
      install shape. Measured on this machine: ``<worktree>/.venv/bin/python`` →
      ``~/.local/share/uv/python/.../bin/python3.13``, no worktree ancestor. And
      the literal — not the resolved — path is what ``_write_launcher`` bakes into
      ``PY=`` and ``register_mcp`` writes to ``~/.claude.json``.
    * The resolved form still matters: a system interpreter symlinked INTO a
      worktree venv dangles just the same when the worktree is pruned.
    """
    candidates = [p]
    try:
        r = p.resolve()
        if r != p:
            candidates.append(r)
    except OSError:
        pass  # unresolvable (dangling symlink): the literal form still counts

    for cand in candidates:
        for d in (cand, *cand.parents):
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
    # The validated interpreter (techne#458), so install-method detection describes
    # the interpreter actually being baked in rather than whichever one happened to
    # run init — otherwise a poisoned run could emit a heal for the wrong install.
    exe_path = _interpreter()
    exe = str(exe_path)
    # Both read from the interpreter being BAKED IN, never from the one running init
    # — those can now differ, and mixing them emits remediation for the wrong venv.
    in_venv, venv_prefix = _venv_state(exe_path)
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
        f"python3 -m venv {venv_prefix} && "
        f"{venv_prefix}/bin/pip install -e {repo}"
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
        f"PY={shlex.quote(str(_interpreter()))}\n"
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


def _matcher_of(group: dict) -> str:
    """A group's `matcher` as a STRING, whatever settings.json actually holds.

    Third crash level found on this PR, below two rounds of validator work. `matcher`
    is a leaf that _settings_shape_error never inspects and _group_contains_techne
    never touches, and it flows into `(event, matcher) not in expected_set` — a HASH.
    A truthy list or dict there raises `TypeError: unhashable type`, which
    `techne status` surfaces as a traceback and the banner swallows into the green
    tick. The old `g.get("matcher") or ""` HID it: falsy containers ([] and {})
    collapse to "" and hashable scalars (5, true) survive, so only truthy containers
    trip it — which is why it took a third look. (Prudence, PR #457.)
    """
    m = group.get("matcher")
    return m if isinstance(m, str) else ""


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
    # isinstance at the DEREFERENCE, not another clause in the shape validator.
    # A non-string `command` (a JSON number/bool) makes `"techne" in 5` raise
    # TypeError — measured: `techne status` exits 1 with a traceback, and at session
    # start render_drift_block raises into session_start's `except Exception`, giving
    # the unqualified green tick #452 exists to kill, through a narrower door.
    # Guarding here closes BOTH call sites at once and cannot drift out of sync with
    # _settings_shape_error the way a parallel validator clause would.
    # (Prudence, PR #457.)
    def _mentions_techne(value: object) -> bool:
        return isinstance(value, str) and "techne" in value

    if _mentions_techne(group.get("command")):
        return True
    inner = group.get("hooks")
    for h in inner if isinstance(inner, list) else []:
        if isinstance(h, dict) and _mentions_techne(h.get("command")):
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


_UNVERIFIABLE = " — hook registration cannot be verified; every techne hook may be unregistered"


def _settings_shape_error(settings: object) -> str | None:
    """Why *settings* cannot be traversed to verify hook registration, or None.

    VALIDATES THE SHAPE THE TRAVERSAL ACTUALLY WALKS — five levels, not two. The
    first cut checked only that `settings` and `hooks` were objects; Prudence then
    measured 10 of 14 malformed shapes STILL raising AttributeError further down,
    including ``{"hooks": {"SessionStart": {...}}}`` — an object where an array
    belongs, i.e. the likeliest hand-edit slip. Those exceptions land in the
    session-start hook's ``except Exception`` and render a GREEN TICK with every
    hook dead, which is the exact failure techne#452 exists to kill.

    Rule of thumb this encodes: validate every level you are about to walk, not the
    first one that occurred to you. (Prudence, PR #457.)
    """
    if settings is None:
        return None  # absent file is a normal state; drift logic handles it
    if not isinstance(settings, dict):
        return (
            "settings.json is valid JSON but not an object "
            f"(got {type(settings).__name__}){_UNVERIFIABLE}"
        )
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return (
            "settings.json's `hooks` key is not an object "
            f"(got {type(hooks).__name__}){_UNVERIFIABLE}"
        )
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            return (
                f"settings.json's `hooks.{event}` is not an array (got "
                f"{type(groups).__name__}){_UNVERIFIABLE}"
            )
        for group in groups:
            if not isinstance(group, dict):
                return (
                    f"settings.json has a non-object entry in `hooks.{event}` (got "
                    f"{type(group).__name__}){_UNVERIFIABLE}"
                )
            inner = group.get("hooks")
            if inner is None:
                continue  # legacy flat shape; _group_contains_techne handles it
            if not isinstance(inner, list):
                return (
                    f"settings.json has a non-array `hooks` inside a `hooks.{event}` "
                    f"group (got {type(inner).__name__}){_UNVERIFIABLE}"
                )
            for entry in inner:
                if not isinstance(entry, dict):
                    return (
                        f"settings.json has a non-object hook in `hooks.{event}` (got "
                        f"{type(entry).__name__}){_UNVERIFIABLE}"
                    )
    return None


def settings_unusable_reason(config: Config) -> str | None:
    """The single source of truth for "settings.json cannot be verified", or None.

    Exists so callers branch on the CONDITION rather than grepping prose. The banner
    previously decided its heading with ``any("cannot be verified" in w ...)``, which
    reaches across a module boundary into message wording: Prudence flipped it two
    ways — rewording a message here left the suite 56/56 green while the banner
    reverted to the mislabel, and because a drift warning embeds the `matcher` read
    from settings.json, a hook group literally named "cannot be verified" filed six
    genuine drift warnings under "settings.json unusable". User data must not steer
    control flow. (Prudence, PR #457.)
    """
    if config.platform != CLAUDE_CODE:
        return None
    try:
        settings = _load_json(SETTINGS_PATH)
    except RuntimeError as exc:
        return f"settings.json is unreadable ({exc}){_UNVERIFIABLE}"
    return _settings_shape_error(settings)


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
    except RuntimeError as exc:
        # Report it HERE rather than deferring to register/unregister. The old
        # comment reasoned that those "already surface it loudly with remediation",
        # which is true and beside the point: they run only at `techne init`, never
        # at session start and never from `techne status`. Returning [] meant a
        # corrupt settings.json rendered a CLEAN bill of health while every hook
        # was in an unknown state — a worse silence than the drift this function
        # exists to report. (techne#452; Prudence, PR #457.)
        return [
            f"settings.json is unreadable ({exc}) — hook registration cannot be "
            "verified; every techne hook may be unregistered"
        ]
    shape = _settings_shape_error(settings)
    if shape:
        return [shape]
    if settings is None:
        settings = {}
    hooks = settings.get("hooks", {})

    def _has_techne_group(event: str, matcher: str) -> bool:
        for g in hooks.get(event, []):
            if _matcher_of(g) == matcher and _group_contains_techne(g):
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
            matcher = _matcher_of(g)
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
        # Validated interpreter, not a bare sys.executable (techne#458) — the MCP
        # server dies with -32000 under a poisoned pin exactly as the hooks do
        # (techne#578), and #370 tracks that it has no self-heal launcher yet.
        "command": str(_interpreter()),
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
