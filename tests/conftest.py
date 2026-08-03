"""Global test-isolation guard (techne#288).

Autouse, function-scoped: **no test may write the developer's REAL Claude Code
config** (~/.claude/settings.json, ~/.claude.json, and the generated
~/.claude/techne-hook.sh launcher). Before this guard, a full-suite run silently
stripped the developer's opt-in techne hooks from their real settings.json — see
techne#288 for the order-dependent mechanism.

Root cause: ``hooks.SETTINGS_PATH`` / ``MCP_CONFIG_PATH`` are import-time snapshots,
frozen at *first import* of the module — to the real home if that import ran under
the real ``HOME`` (e.g. at collection time). ``register_hooks`` / ``register_mcp``
then write those exact paths, and the launcher path derives from
``SETTINGS_PATH.parent`` — so a test that runs ``init --platform claude-code``
without redirecting them writes the developer's real config. Pinning the module
attrs (and the package re-exports) into a per-test tmp closes every one of those
write paths regardless of import/run order.

Scope note — we pin ONLY these frozen attrs and touch NO environment variable.
Redirecting ``HOME`` breaks the embedding/model cache (keyed under ~/.cache);
redirecting ``CLAUDE_CONFIG_DIR`` / ``TECHNE_HOME`` overrides and leaks into the many
tests that relocate via their own env (including subprocess ``env_overrides``).
Neither is needed: ``~/.claude`` is the only real-write path not already isolated by
existing per-test fixtures, and it is reached exclusively through these two attrs.
Subprocess tests re-import the module under their own env, so their frozen values
are independent of this pin. Tests with their own redirection (``fake_home``,
``isolated_config``) run after this fixture and override the same attrs.

PERFORMANCE — why these carve subdirs out of ``tmp_path`` instead of calling
``tmp_path_factory.mktemp()`` (techne#466). ``mktemp(numbered=True)`` scans the
base temp directory to find the next free number, so its cost grows with how
many entries are already there: measured 411us/call over the first 200 dirs
against 5555us/call once ~3800 exist — a 13.5x climb within a single run. These
fixtures are AUTOUSE, so every test paid it TWICE, and the suite generates
~3.3 numbered dirs per test (~7,600 in one flat directory at 2312 tests). That
is quadratic cost which *looks* uniform once averaged, and it is ``getdents``
time — the bulk of the ``sys > user`` profile behind techne#463.

Carving out of the per-test ``tmp_path`` costs ONE numbered scan (pytest mints
``tmp_path`` anyway) plus two O(1) mkdirs into a fresh directory, instead of
three numbered scans. Isolation is unchanged: each test still gets a private,
non-worktree directory, so techne#288 and techne#434 stay closed.

Do NOT "simplify" these back to a shared session-scoped root with a counter —
an earlier attempt did exactly that and broke
``test_probe_env_strips_a_relative_entry_resolving_into_the_root``, because the
install root must genuinely exist for a relative PYTHONPATH entry to resolve
against it.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_host_config(tmp_path, monkeypatch):
    from techne.platforms import claude_code
    from techne.platforms.claude_code import hooks as hooks_mod

    # Carved out of the per-test `tmp_path`, NOT minted with
    # `tmp_path_factory.mktemp()` — see the module docstring's PERFORMANCE note.
    root = tmp_path / "_host"
    root.mkdir(exist_ok=True)
    settings_path = root / ".claude" / "settings.json"
    mcp_path = root / ".claude.json"
    for target in (hooks_mod, claude_code):
        monkeypatch.setattr(target, "SETTINGS_PATH", settings_path, raising=False)
        monkeypatch.setattr(target, "MCP_CONFIG_PATH", mcp_path, raising=False)


@pytest.fixture(autouse=True)
def _isolate_install_root(tmp_path, monkeypatch):
    """Pin ``_install_root`` so tests never inherit the checkout's real layout.

    Same principle as ``_isolate_host_config`` above, for the other host-dependent
    input into ``register_hooks``. Without it, running the suite from a git
    worktree makes ``_install_root()`` return that worktree — and once the worktree
    guard could actually SEE worktrees (techne#434), the guard correctly refused
    and 35 hook tests failed. The tests were not wrong and the guard was not wrong;
    the test harness was reading a host detail it never meant to depend on.

    Pinned to a plain temp dir, which is not a worktree, so registration-shaped
    tests behave identically whether the developer works in a clone or a worktree.
    Tests that care about the guard override this with their own ``_install_root``
    (see ``tests/test_worktree_guard.py``), exactly as ``fake_home`` overrides the
    fixture above.
    """
    from techne.platforms.claude_code import hooks as hooks_mod

    # Same carve-out as above; must genuinely EXIST, because the probe tests
    # resolve relative PYTHONPATH entries against it.
    canonical = tmp_path / "_install_root"
    canonical.mkdir(exist_ok=True)
    monkeypatch.setattr(hooks_mod, "_install_root", lambda: canonical, raising=False)
    # register_hooks guards THREE prunable inputs, not just the install root: the
    # interpreter (baked into the launcher's `PY=`, the MCP command and the heal)
    # and the preflight guard's absolute path. Both derive from the host —
    # sys.executable and `__file__` — so a suite run from a worktree trips the guard
    # on them too. Pinned for the same reason SETTINGS_PATH is.
    #
    # Note `_interpreter` exists as a seam precisely so this does NOT monkeypatch
    # the global `sys.executable`, which would break every subprocess-spawning test.
    monkeypatch.setattr(
        hooks_mod, "_interpreter", lambda: canonical / "bin" / "python", raising=False
    )
    monkeypatch.setattr(
        hooks_mod, "PREFLIGHT_GUARD", canonical / "preflight_guard.py", raising=False
    )
