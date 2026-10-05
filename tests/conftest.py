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

Scope note — the attr pin covers ``~/.claude``; the ENV pin below covers
``~/.techne``. This note used to end "we touch NO environment variable… neither is
needed: ``~/.claude`` is the only real-write path not already isolated". That was
wrong, and measurably so (techne#477).

THE DIRECTION IS THE OPPOSITE OF THE OBVIOUS ONE, and I got it backwards once
before measuring. ``TECHNE_HOME`` OUTRANKS ``HOME``: ``techne_home()`` returns
``$TECHNE_HOME`` and only falls back to ``$HOME/.techne``. The subprocess hook
tests override ``HOME`` to a tmp dir and were correctly isolated *when
TECHNE_HOME was unset* — and wrote straight into the operator's LIVE store
whenever it was SET, which is the normal state for anyone actually running techne
(Prudence's launcher exports it). Measured on this branch: with ``TECHNE_HOME``
pointing at the real store, one run of ``test_self_healing_hooks.py`` added THREE
session rows to it; with this fixture, zero.

Historically that put 322 junk session rows in the live store going back to
2026-06-09, with ``context_injections`` logged against them and ``hook:*:last_run``
liveness markers stamped there — so ``techne doctor`` reported the test suite's
last run as the machine's health, which is the one thing techne#468 exists not to
do. It is the same root cause as the read-side failures found in PR #469 review 7,
where five tests graded the developer's real store instead of their fixture.

Redirecting ``HOME`` is still refused — it breaks the embedding/model cache (keyed
under ~/.cache) — so the two vars are pinned directly instead.

The original objection to pinning them ("overrides and leaks into the many tests
that relocate via their own env, including subprocess ``env_overrides``") does not
apply to a BASELINE: an autouse fixture runs BEFORE the test body, so a test's own
``monkeypatch.setenv`` and any subprocess ``env_overrides`` both still win. What
the baseline removes is the case nobody intended — a test silently inheriting the
developer's ambient value.

PRECONDITION, load-bearing and easy to break silently: that holds for
FUNCTION-SCOPED-OR-LATER setters only. A SESSION- or module-scoped fixture setting
``TECHNE_HOME`` would be clobbered by this one, because higher-scoped fixtures
instantiate FIRST. It is safe today only because ``tests/`` contains no
non-function-scoped fixture that sets these — if you add one, it must set the env
itself at the point of use, not rely on scope ordering. (Prudence, PR #478.)

There is also a class this fixture CANNOT reach: anything binding a path at IMPORT
time, before any fixture runs. ``Config``'s ``db_path``/``data_dir`` were exactly
that and are now ``default_factory`` (techne#477); ``PROJECTS_DIR`` in
transcript_import.py still is, which is why the test asserting on it derives the
root from the resolver rather than hardcoding ``.claude``.

Subprocess tests re-import the module under their own env, so their frozen values
are independent of the attr pin. Tests with their own redirection (``fake_home``,
``isolated_config``) run after this fixture and override the same attrs.

PERFORMANCE — why these carve subdirs out of ``tmp_path`` instead of calling
``tmp_path_factory.mktemp()`` (techne#466). ``mktemp(numbered=True)`` scans its
base temp directory to find the next free number, so its cost grows with how
many entries are already there: measured 411us/call over the first 200 dirs
against 5555us/call once ~3800 exist — a 13.5x climb within a single run. These
fixtures are AUTOUSE, so every test paid it twice. Serially that is ~7,600
entries in one directory at 2312 tests: quadratic cost that *looks* uniform once
averaged, and it is ``getdents`` time. It accounts for ~90% of the EXCESS of
``sys`` over ``user`` (4.9s -> 0.4s), though only ~28% of ``sys`` outright.

WHAT THIS DOES AND DOES NOT FIX. It reduces numbered-dir creations from ~3.30 to
~1.92 per test — not 3 to 1, because ~0.9/test come from tests minting their own
via ``tmp_path_factory``, which this does not touch. Growth remains Theta(N^2)
with the constant down ~2.9x; ~8.7s of an ~81s serial run is still numbered-dir
scanning. Note the tradeoff: pre-change ``tmp_path`` was minted only for tests
that asked for it, and now every test mints one.

UNDER xdist THIS MATTERS MUCH LESS, and the reason is worth recording because an
earlier draft of this note got it exactly backwards. ``getbasetemp()`` is
PER-WORKER (``pytest-N/popen-gwK``), and that per-worker directory is what
``make_numbered_dir`` scans — so the quadratic term is already divided by
parallelism (~1/W^2 per worker) before this change lands. Measured deltas:
-24.5% serial, -7.7% at ``-n 4`` locally, ~-1% on the 2-core CI runner. Do not
probe this with ``tmp_path.parent.parent`` — that resolves to the SHARED
``pytest-N`` root and makes every worker look identical.

Isolation is unchanged: each test still gets a private, non-worktree directory,
so techne#288 and techne#434 stay closed. But note the invariant changed shape —
the carve-outs are now SIBLINGS of the test's own ``tmp_path`` contents rather
than disjoint roots. The suite is insensitive to sibling *directories*, but
sensitive to *files* beneath ``tmp_path``: a test that walks ``tmp_path``
recursively would see anything created here. Keep these carve-outs empty.
"""

import pytest


def pytest_sessionstart(session):  # noqa: ARG001 — pytest hook signature
    """Refuse to run under an interpreter that has no techne installed (#893).

    THE TRAP THIS CLOSES. `python3 -m pytest` from the repo root resolves to the
    system interpreter on a dev box, where techne is not installed. The suite
    then reports **36 failures** that read as a packaging regression —
    `StopIteration` and `PackageNotFoundError` raised deep inside
    `importlib.metadata`, in tests named `test_litellm_not_in_core_deps` and
    `test_an_installed_build_still_trusts_dist_info`. Nothing in that output
    says "wrong interpreter".

    THE PART THAT MAKES IT EXPENSIVE, and the reason this is a hard exit rather
    than a warning: the natural check — *does it fail on main too?* — comes back
    YES, because the interpreter is wrong on both sides. So the check that
    should catch the mistake instead certifies it, and the failures get written
    off as pre-existing breakage. That happened on 2026-09-06 while verifying
    #892; 36 phantom failures were nearly reported as pre-existing on main.

    One line beats 36 misleading ones. `pytest.exit` rather than a fixture
    because this must fire before collection, and collection is where the
    misleading errors are raised.
    """
    import importlib.metadata
    import sys

    try:
        importlib.metadata.version("techne")
    except importlib.metadata.PackageNotFoundError:
        pytest.exit(
            f"techne is not installed in this interpreter ({sys.executable}).\n"
            "Running the suite here produces failures that look like a packaging "
            "regression and reproduce on main, because the interpreter is wrong on "
            "both sides (techne#893).\n"
            "Run it with the repo venv instead:  .venv/bin/python -m pytest",
            returncode=4,
        )


@pytest.fixture(autouse=True)
def _isolate_store_env(tmp_path, monkeypatch):
    """Pin TECHNE_HOME / CLAUDE_CONFIG_DIR so no test can reach the real stores.

    A BASELINE, not an override: this runs before the test body, so a test that
    sets its own value — directly, or via a subprocess ``env_overrides`` — still
    wins. What it removes is the unintended case, where a test inherits whatever
    the developer happened to have exported.

    Both symptoms are the same root cause (techne#477), and both fire when the
    vars are SET — the normal state for anyone running techne. In-process tests
    read the developer's real store and fail on a healthy machine; subprocess
    hook tests, which override only HOME, WRITE to it, because TECHNE_HOME
    outranks HOME. A test suite must not be able to tell you your hooks are fine.

    Deliberately NOT created on disk: an absent dir is a normal, handled state
    (``Config.load`` falls back to defaults rather than creating a store), and
    creating one here would hide tests that depend on the store already existing.
    """
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / "_techne_home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "_claude_config"))


@pytest.fixture(autouse=True)
def _isolate_process_table(monkeypatch):
    """Pin the host's process list away from `runtime:loaded` (techne#491).

    That check asks whether any LONG-LIVED techne process started before the
    deployed revision was committed, which it answers by shelling out to `ps`
    against the real machine. Unpinned, the suite grades the developer's running
    MCP servers: four `test_cli_doctor` tests went red on a healthy laptop that
    happened to have two servers older than the last deploy, while CI — where no
    techne process is running — stayed green. A suite whose verdict depends on
    what is running on the host is the same host-dependence
    `_isolate_host_config` and `_isolate_install_root` exist to remove, and
    `docs/arch/liveness.md` already lists it as a fail-mode.

    Pinned to `[]` — "looked, nothing long-lived running" — which is the DORMANT
    baseline. Deliberately not `None`, which means "could not look" and maps to
    UNKNOWN. Tests that exercise the check override it (see
    `tests/test_runtime_loaded.py`).
    """
    from techne.core import checks as checks_mod

    monkeypatch.setattr(checks_mod, "_running_techne_processes", lambda: [])


@pytest.fixture(autouse=True)
def _isolate_operator_layer(tmp_path, monkeypatch):
    """Pin the operator-config resolution away from the host (techne#393).

    ``resolve_operator_config`` checks ``/etc/techne/config.operator.yaml``
    FIRST and unconditionally — deliberately, because an env-relocatable path
    would let an agent select the permissive state by editing its own
    environment. That same property makes it a host input the suite must not
    read: on a machine where an operator HAS deployed a layer, every test that
    calls ``Config.load`` would silently resolve egress through it, and the
    suite's verdict would depend on the developer's deployment.

    Pointed at a path under ``tmp_path`` that is never created, so the baseline
    is the ``absent`` state. Tests that want a layer set their own (see
    ``tests/test_config_trust.py``), exactly as with the fixtures around it.
    """
    from techne.security import config_trust

    monkeypatch.setattr(
        config_trust, "SYSTEM_OPERATOR_CONFIG", tmp_path / "_etc" / "config.operator.yaml"
    )
    monkeypatch.delenv(config_trust.OPERATOR_CONFIG_ENV, raising=False)


@pytest.fixture(autouse=True)
def _isolate_host_config(tmp_path, monkeypatch):
    from techne.platforms import claude_code
    from techne.platforms.claude_code import hooks as hooks_mod

    # Carved out of the per-test `tmp_path`, NOT minted with
    # `tmp_path_factory.mktemp()` — see the module docstring's PERFORMANCE note.
    # No `exist_ok`: a collision would mean pytest handed back a REUSED tmp_path,
    # which is precisely the isolation failure this fixture exists to prevent, so
    # it must be loud rather than silently absorbed.
    root = tmp_path / "_host"
    root.mkdir()
    settings_path = root / ".claude" / "settings.json"
    mcp_path = root / ".claude.json"
    for target in (hooks_mod, claude_code):
        monkeypatch.setattr(target, "SETTINGS_PATH", settings_path, raising=False)
        monkeypatch.setattr(target, "MCP_CONFIG_PATH", mcp_path, raising=False)


@pytest.fixture(autouse=True)
def _pin_claude_on_path(monkeypatch):
    """Pin "is `claude` on PATH" to yes (#1123).

    `techne init` changes its output when Claude Code is absent, and whether the
    `claude` binary is on PATH is a host detail: a developer laptop has it, CI does
    not. The baseline is the installed case, which every init test written before
    #1123 assumed. Tests of the absent case override it
    (see ``tests/test_init_without_claude_code.py``).
    """
    from techne.platforms.claude_code import paths as paths_mod

    # raising=False: eval-vec's base leg runs these tests against main's library.
    monkeypatch.setattr(paths_mod, "_claude_on_path", lambda: True, raising=False)


@pytest.fixture(autouse=True)
def _pin_install_context(request, monkeypatch):
    """Pin how techne looks installed to a pipx install (#1124).

    The embedding-extra hint follows the install kind, and the suite's own install
    is the host's: editable on a developer checkout, a uv venv in CI. The baseline
    is the pipx install the README documents, which every hint test written before
    #1124 assumed. ``tests/test_install_command.py`` covers each kind.

    Tests marked ``real_install_context`` (techne#1128) run the real
    ``_install_context()``/``_direct_url()`` instead, against a faked
    ``importlib.metadata.distribution`` — see the ``real_install_context``-marked
    tests in ``tests/test_install_command.py``. Without that marker, this fixture
    pins the SEAM one layer up, so a typo in the ``direct_url.json`` filename
    inside ``_direct_url()`` passes the whole suite silently.
    """
    if request.node.get_closest_marker("real_install_context"):
        return
    from techne.core import capabilities as caps

    monkeypatch.setattr(
        caps,
        "_install_context",
        lambda: {
            "direct_url": None,
            "prefix": "/home/u/.local/share/pipx/venvs/techne",
            "base_prefix": "/usr",
            "executable": "/home/u/.local/share/pipx/venvs/techne/bin/python",
        },
        raising=False,  # eval-vec's base leg runs these tests against main's library
    )


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

    Do NOT "simplify" this to a shared session-scoped root with a counter and no
    ``mkdir`` — an earlier attempt did exactly that and broke
    ``test_probe_env_strips_a_relative_entry_resolving_into_the_root``, because a
    relative PYTHONPATH entry must resolve against a root that EXISTS. That is
    the only test that depends on it, and no other property of a mktemp-created
    directory matters here: mktemp yields mode 0o700 and this ``mkdir`` yields
    0o755, but the parent ``tmp_path`` is itself 0o700, so traversal stays
    owner-only and nothing in the suite asserts on a directory mode.
    """
    from techne.platforms.claude_code import hooks as hooks_mod

    # Same carve-out as above; must genuinely EXIST (see the docstring), and no
    # `exist_ok` for the same reason as `_isolate_host_config`.
    canonical = tmp_path / "_install_root"
    canonical.mkdir()
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


def fastembed_is_the_live_backend() -> bool:
    """True when the ACTIVE embedding backend is fastembed (techne#536).

    Used to scope xfails for the float32-contract violation, which is
    backend-specific: `EmbeddingModel.embed()` casts on the model2vec path
    (`EmbeddingBackend.embed` in embeddings.py) and passes fastembed's output
    through unchanged, so the documented float32 contract holds on py>=3.14 and
    breaks below it.

    CONDITIONED, NOT BLANKET, and that matters for `strict=True`: an unconditional
    xfail would FAIL on the 3.14/model2vec leg, where these tests legitimately
    pass. Scoping it to the failing backend keeps strict meaningful — the moment
    #536 is fixed, the xfail turns into an unexpected PASS and the build goes red,
    which is what stops a deferral from quietly becoming permanent.

    Returns False when no backend is installed at all; the tests then skip on
    their own `backend_available()` guard, and a skip beats an xfail.
    """
    try:
        from techne.core import embeddings

        if not embeddings.backend_available():
            return False
        backend, _ = embeddings._select_backend(None)
        return backend == "fastembed"
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _no_model_download_on_init(request, monkeypatch):
    """`techne init` downloads the embedding model (techne#1021). No test may do that
    by accident: stub it here. Tests marked `real_warm_vectors`
    (tests/test_init_vectors.py) run the real helper with the model loader stubbed.

    raising=False: CI's eval-vec base leg runs THESE tests against main's library,
    which has no _warm_vectors until this lands."""
    if request.node.get_closest_marker("real_warm_vectors"):
        return
    monkeypatch.setattr("techne.cli._warm_vectors", lambda config: None, raising=False)


@pytest.fixture(autouse=True)
def _no_installed_recall_sources(monkeypatch):
    """`techne_recall` and doctor discover `techne.recall_sources` entry points
    (techne#1170), so a source package installed on the developer's box would change
    what the suite sees. Pin the group empty; discovery tests install their fakes."""
    import techne.core.recall_sources as rs_mod

    monkeypatch.setattr(rs_mod, "entry_points", lambda group: [])
