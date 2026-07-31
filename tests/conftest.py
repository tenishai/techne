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
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_host_config(tmp_path_factory, monkeypatch):
    from techne.platforms import claude_code
    from techne.platforms.claude_code import hooks as hooks_mod

    root = tmp_path_factory.mktemp("host")
    settings_path = root / ".claude" / "settings.json"
    mcp_path = root / ".claude.json"
    for target in (hooks_mod, claude_code):
        monkeypatch.setattr(target, "SETTINGS_PATH", settings_path, raising=False)
        monkeypatch.setattr(target, "MCP_CONFIG_PATH", mcp_path, raising=False)


@pytest.fixture(autouse=True)
def _isolate_install_root(tmp_path_factory, monkeypatch):
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

    canonical = tmp_path_factory.mktemp("install_root")
    monkeypatch.setattr(hooks_mod, "_install_root", lambda: canonical, raising=False)
