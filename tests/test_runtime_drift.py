"""A server that notices the code moved underneath it (techne#696)."""

from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path

import pytest

from techne.core import runtime_drift as rd


@pytest.fixture
def marker(tmp_path, monkeypatch):
    """A deployment marker to watch, at the path `begin` looks for."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    p = tmp_path / "runtime" / ".git" / "logs" / "HEAD"
    p.parent.mkdir(parents=True)
    p.write_text("initial")
    return p


def _change(watch, name: str, path: Path) -> None:
    """Make `path` differ from the baseline `watch` recorded for it.

    Sets an explicit mtime relative to the RECORDED value rather than calling
    `os.utime(path, None)`: this feature is about mtimes, so a test that relies on
    wall-clock advancing far enough to be visible at filesystem granularity is a test
    that can pass or fail for reasons unrelated to the code.
    """
    os.utime(path, (0, watch.baseline[name] + 10))


def _move(path: Path) -> None:
    """A pull: the ref moves, so the reflog's mtime changes."""
    time.sleep(0.01)
    path.write_text(path.read_text() + "x")


@pytest.fixture
def tearable_module(tmp_path):
    """A synthetic 'already loaded' module, backed by its own file under `tmp_path`.

    NOT `techne.core.runtime_drift` itself (techne#1044). That file is on-disk once,
    shared by every xdist worker process running this test file, and `_module_mtimes`
    walks EVERY loaded `techne.*` module including it — so it is also in the baseline
    of any OTHER test in this file that calls `begin()`, whether or not that test ever
    names it. Four tests here used to `os.utime` it directly to simulate a tear. Under
    `-n 4` a worker's call landed between a DIFFERENT worker's `begin()` and `poll()` on
    the identical path, so that worker's own (untouched) test saw the real module torn
    and failed — reproduced locally: pairing just `test_a_module_changing_under_us_is_a_tear`
    with `test_a_deployment_that_touches_nothing_we_loaded_is_silent` under `-n 2` failed
    about 13/20 runs. A synthetic module's file lives only under this test's own
    `tmp_path`, so no other process can reach it.
    """
    name = "techne_test_tearable"
    path = tmp_path / "tearable.py"
    path.write_text("x = 1")
    os.utime(path, (0, 1_000_000.0))
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    sys.modules[name] = mod
    try:
        yield name, path
    finally:
        del sys.modules[name]


def test_a_packaged_install_has_nothing_to_watch(tmp_path, monkeypatch):
    """No `.git/logs/HEAD` means no revision to compare, which is healthy rather than
    red — the same call `check_runtime_loaded` makes. Must be silent forever, not
    UNKNOWN-and-noisy."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    watch = rd.begin(None, ttl=0.0)
    assert watch.marker_at is None
    assert rd.poll(watch) is None


def test_silent_while_nothing_moves(marker):
    watch = rd.begin(None, ttl=0.0)
    assert rd.poll(watch) is None
    assert rd.poll(watch) is None


def test_a_deployment_that_touches_nothing_we_loaded_is_silent(marker):
    """THE ANTI-NOISE CASE, and the reason tier 2 exists at all.

    Most pulls change files this process never imported — tests, docs, a module it does
    not use. `check_runtime_loaded` cannot tell that apart from a real tear (it is
    "ordering only", by its own docstring) and would warn anyway. Warning on every pull
    regardless of consequence is how a true signal gets trained into noise.
    """
    watch = rd.begin(None, ttl=0.0)
    _move(marker)
    assert rd.poll(watch) is None


def test_a_module_changing_under_us_is_a_tear(marker, tearable_module):
    """The dangerous state, and the one only self-introspection can see: we keep serving
    the OLD definition of what we already loaded, while anything imported from here on
    comes from the NEW source. Reachable in practice — mcp_server.py carries 46
    function-local `from techne…` imports.

    Uses `tearable_module`, not `techne.core.runtime_drift` itself — see that fixture
    (techne#1044) for why mutating the real module's file races other xdist workers.
    """
    name, path = tearable_module
    watch = rd.begin(None, ttl=0.0)
    _move(marker)
    _change(watch, name, path)

    drift = rd.poll(watch)
    assert drift is not None
    assert name in drift.torn
    assert name in drift.message()


def test_one_warning_per_deployment(marker, tearable_module):
    """This condition does NOT self-clear — it persists for the life of the process. So
    unlike a SessionStart banner, repeating it would repeat FOREVER, on every tool call.
    Same discipline as `capabilities.invitation`: say it once.

    Uses `tearable_module`, not the real `techne.core.runtime_drift` file — see that
    fixture (techne#1044) for why.
    """
    name, path = tearable_module
    watch = rd.begin(None, ttl=0.0)
    _move(marker)
    os.utime(path, None)

    assert rd.poll(watch) is not None
    assert rd.poll(watch) is None, "a repeated warning is how a true signal becomes noise"


def test_a_second_pull_warns_again(marker, tearable_module):
    """Suppression is keyed on the marker mtime, NOT a one-shot flag. A second deploy
    during the same process is new information and must not be swallowed by the first
    one's suppression."""
    name, path = tearable_module
    watch = rd.begin(None, ttl=0.0)

    _move(marker)
    os.utime(path, None)
    assert rd.poll(watch) is not None

    _move(marker)
    os.utime(path, None)
    assert rd.poll(watch) is not None, "a second deployment is not the first one repeated"


def test_the_ttl_actually_suppresses_the_stat(marker, monkeypatch):
    """Tier 1 runs per tool call, so it must not stat per tool call. Asserts the
    RESOLVED behaviour (no re-stat inside the window) rather than that a cache field
    was set."""
    watch = rd.begin(None, ttl=3600.0)
    rd.poll(watch)  # primes _checked_at

    calls = []
    real = Path.stat
    monkeypatch.setattr(
        Path, "stat", lambda self, *a, **k: (calls.append(self), real(self, *a, **k))[1]
    )
    _move(marker)
    assert rd.poll(watch) is None
    assert not calls, f"TTL window must not re-stat; got {calls}"


def test_a_deleted_module_file_is_not_reported_as_changed(marker, tmp_path):
    """ "The file is gone" is a different fault from "the file changed", and this
    function claims only the latter. Guessing would put a claim in the message that the
    measurement does not support."""
    import types

    ghost = types.ModuleType("techne_ghost_module")
    ghost.__file__ = str(tmp_path / "vanished.py")
    sys.modules["techne_ghost_module"] = ghost
    try:
        # A baseline that WOULD make it torn if the guard were absent. With `{}` every
        # module is "newly imported" and the function cannot return anything at all, so
        # the assertion held for a reason unrelated to the guard: Prudence mutated out
        # BOTH guards and all 15 tests still passed.
        assert "techne_ghost_module" not in rd._torn_modules({"techne_ghost_module": 0.0})
    finally:
        del sys.modules["techne_ghost_module"]


def test_a_module_without_a_file_is_skipped():
    """Namespace packages and frozen imports have no `__file__`. Absence of a path is
    not evidence of staleness."""
    import types

    ns = types.ModuleType("techne_namespace_like")
    sys.modules["techne_namespace_like"] = ns
    try:
        assert "techne_namespace_like" not in rd._torn_modules({"techne_namespace_like": 0.0})
    finally:
        del sys.modules["techne_namespace_like"]


def test_one_warning_per_deployment_at_the_SHIPPED_ttl(marker, tearable_module):
    """Prudence r1, the finding I did not ask about — and the reason it survived.

    `test_one_warning_per_deployment` passes `ttl=0.0`, which makes the TTL branch
    unreachable. Inside that branch `poll` returned a CACHED `Drift`, so at the shipped
    30s default the notice repeated on EVERY tool call for thirty seconds. The fixture
    argument switched off the very path that broke the property the test names — a
    guard's precondition used as a way to switch the guard off, in my own test.

    So this one runs at `DEFAULT_TTL_SECONDS` and polls again immediately.

    Uses `tearable_module`, not the real `techne.core.runtime_drift` file — see that
    fixture (techne#1044) for why.
    """
    name, path = tearable_module
    watch = rd.begin(None)
    assert watch.ttl == rd.DEFAULT_TTL_SECONDS
    _move(marker)
    _change(watch, name, path)

    watch._checked_at = 0.0  # allow the first real poll
    assert rd.poll(watch) is not None
    assert rd.poll(watch) is None, "a cached Drift would repeat on every call for 30s"
    assert rd.poll(watch) is None


def test_a_module_imported_after_the_snapshot_is_not_torn(marker, tmp_path):
    """Prudence r1, Important — my aim point 3, confirmed and worse than I guessed.

    Under the old `mtime > started_at` rule, a module imported LATE from a file written
    after startup read as torn while being the NEWEST code in the process. She got the
    server to name `techne.core.health` as one it was "still serving the OLD definitions"
    of, milliseconds after loading it from the new source in the same call. It also broke
    the silence property, since one lazy import made a harmless deployment look torn.

    A per-module baseline cannot express that error: a module absent from the baseline
    came from the current disk, by construction.
    """
    import types

    watch = rd.begin(None, ttl=0.0)
    late = tmp_path / "late_module.py"
    late.write_text("x = 1")
    os.utime(late, (0, time.time() + 3600))  # a file newer than any process timestamp

    mod = types.ModuleType("techne_late_import")
    mod.__file__ = str(late)
    sys.modules["techne_late_import"] = mod
    try:
        assert "techne_late_import" not in rd._torn_modules(watch.baseline)
        # ...and now that it IS baselined, a later change to it IS caught.
        os.utime(late, (0, time.time() + 7200))
        assert "techne_late_import" in rd._torn_modules(watch.baseline)
    finally:
        del sys.modules["techne_late_import"]


def test_the_message_does_not_overclaim_about_reimport(marker):
    """Prudence r1 — the sentence was false of precisely the modules `torn` names.

    It said "anything imported from now on comes from the NEW source". Python will not
    re-read a module already in `sys.modules`, so for the torn modules themselves the
    OLD definition persists for the life of the process. Only NOT-yet-imported modules
    load new code.
    """
    drift = rd.Drift(torn=("techne.core.thing",), moved_at=1.0)
    msg = drift.message()
    assert "sys.modules" in msg
    assert "NOT yet imported" in msg


# --- the dispatch seam ------------------------------------------------------


@pytest.fixture
def cfg(tmp_path):
    from techne.config import Config
    from techne.core.database import init_db

    c = Config.load()
    c.db_path = tmp_path / "t.db"
    c.data_dir = tmp_path
    init_db(c.db_path)
    return c


def _call(config, monkeypatch, notice):
    import techne.mcp_server as ms

    monkeypatch.setattr(ms, "_drift_notice", lambda _c: notice)
    return ms.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "techne_status", "arguments": {}},
        },
        config,
    )


def test_the_notice_rides_as_its_own_block(cfg, monkeypatch):
    """NEVER a key inside the tool's payload. The notice is about the SERVER, not about
    what the tool returned — folding it in would change 34 tools' response shapes to say
    something none of them is about, and #695 spent three review rounds establishing how
    expensive a response-shape change is."""
    import json

    content = _call(cfg, monkeypatch, "DRIFT HERE")["result"]["content"]
    assert len(content) == 2
    assert "DRIFT" not in content[0]["text"], "the tool's own payload must be untouched"
    json.loads(content[0]["text"])  # still valid JSON on its own
    assert content[1]["text"] == "DRIFT HERE"


def test_no_notice_leaves_the_response_exactly_as_before(cfg, monkeypatch):
    """The common case is no drift, and it must be byte-identical to the pre-#696 shape:
    a single content block. An extra empty block would be a response-shape change paid on
    every call, for nothing."""
    assert len(_call(cfg, monkeypatch, None)["result"]["content"]) == 1


def test_a_failing_drift_check_cannot_fail_a_tool_call(cfg, monkeypatch):
    """A diagnostic that can break a call is worse than no diagnostic. Asserts the
    RESOLVED behaviour — `_drift_notice` returns None and the tool result survives —
    rather than trusting that the try/except is present."""
    import techne.mcp_server as ms

    def boom(config, **kw):
        raise RuntimeError("stat exploded")

    monkeypatch.setattr(rd, "begin", boom)
    ms._RUNTIME_WATCH = None
    try:
        assert ms._drift_notice(cfg) is None
        resp = ms.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "techne_status", "arguments": {}},
            },
            cfg,
        )
        assert "result" in resp and resp["result"]["content"]
    finally:
        ms._RUNTIME_WATCH = None


def test_the_baseline_is_taken_at_server_start(monkeypatch, tmp_path):
    """Prudence r2 — she deleted the whole `begin()` block from `main()` and 40 tests
    still passed. The r1 fix had nothing holding it in place.

    It matters because `mcp_server` carries 46 function-local imports: baselining lazily
    on the first tool call means the modules those imports pull in are recorded at
    whatever mtime they have BY THEN, which may already be post-pull.
    """
    import io

    import techne.mcp_server as ms

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    marker = tmp_path / "runtime" / ".git" / "logs" / "HEAD"
    marker.parent.mkdir(parents=True)
    marker.write_text("initial")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # serve nothing, exit at once
    ms._RUNTIME_WATCH = None
    try:
        ms.main()
        assert ms._RUNTIME_WATCH is not None, "main() must baseline before serving"
        assert ms._RUNTIME_WATCH.baseline, "the baseline must be populated, not empty"
    finally:
        ms._RUNTIME_WATCH = None


def test_a_module_imported_after_begin_is_still_caught(marker, tmp_path):
    """Prudence r2, Important 1 — the permanent-silence path, and the common case.

    `_torn_modules` used to run only AFTER the marker gate, so nothing was baselined
    until a pull had already happened: a module imported after `begin()` and changed by
    that first pull was baselined at its POST-change mtime and never torn, for the life
    of the process. She demonstrated it on a real module, really imported and really
    rewritten — drift None, and still None after three more pulls.
    """
    import types

    watch = rd.begin(None, ttl=0.0)
    late = tmp_path / "late.py"
    late.write_text("x = 1")
    os.utime(late, (0, 1000.0))
    mod = types.ModuleType("techne_late_arrival")
    mod.__file__ = str(late)
    sys.modules["techne_late_arrival"] = mod
    try:
        # A poll BEFORE any pull must baseline the late arrival.
        assert rd.poll(watch) is None
        assert "techne_late_arrival" in watch.baseline

        # Now the pull changes it — that is a tear, and must be reported.
        _move(marker)
        os.utime(late, (0, 2000.0))
        drift = rd.poll(watch)
        assert drift is not None, "a module imported after begin() must still be watched"
        assert "techne_late_arrival" in drift.torn
    finally:
        del sys.modules["techne_late_arrival"]


def test_the_single_ttl_gate_residual_is_real_and_deliberate(marker, tmp_path):
    """Prudence r3 — PINS A KNOWN LIMITATION so it cannot be lost or quietly widened.

    THE RESIDUAL, precisely: both tiers sit behind ONE TTL gate, so a poll that lands
    inside an open window does no walk at all. A module imported before that suppressed
    poll and changed after it is first seen only on the next poll AFTER the window — by
    which time it is baselined at its POST-change mtime, and stays silent for that
    module, permanently.

    (An earlier version of this test modelled "no poll at all between import and pull".
    That is not the residual and no fix can address it — nothing observed the module
    before it changed. It also passed under both variants, so it discriminated nothing.
    The discriminator is a poll that HAPPENS and is suppressed.)

    THE TRADE, taken deliberately: the full fix is to walk ahead of the TTL gate too, so
    even a suppressed poll baselines. That moves the walk from once-per-window to
    once-per-TOOL-CALL — 0.47 ms, 3% of a `techne_status` and 21% of a `techne_search` —
    and it breaks `test_the_ttl_actually_suppresses_the_stat`. A diagnostic that taxes
    every call to close its own last corner is worse than one that says where its edge is.

    This asserts the CURRENT behaviour, so it FAILS if someone applies the full fix —
    forcing them to re-read the trade rather than meet the cost in production.
    """
    import types

    watch = rd.begin(None)  # shipped TTL; the window is what creates the residual
    assert watch.ttl == rd.DEFAULT_TTL_SECONDS

    late = tmp_path / "inside_window.py"
    late.write_text("x = 1")
    os.utime(late, (0, 1000.0))

    rd.poll(watch, now=1000.0)  # opens the window

    mod = types.ModuleType("techne_inside_window")
    mod.__file__ = str(late)
    sys.modules["techne_inside_window"] = mod
    try:
        # A real poll, INSIDE the window: suppressed, so no walk, so no baseline.
        # This intermediate assert IS the mechanism — without it the test could pass
        # because the marker never moved, rather than because the walk was skipped.
        assert rd.poll(watch, now=1005.0) is None
        assert "techne_inside_window" not in watch.baseline, (
            "the TTL-suppressed poll must do no walk — if it baselined here, the walk "
            "has been moved ahead of the TTL gate (the full fix); see this test's "
            "docstring for the 0.47 ms/call trade before keeping that change"
        )

        # Now it changes, and the deployment moves.
        os.utime(late, (0, 2000.0))
        _move(marker)

        drift = rd.poll(watch, now=1100.0)  # window expired; first walk sees it changed
        # ...and the mechanism, asserted rather than implied: it is recorded at the
        # POST-change mtime, which is precisely why it can never read as torn.
        assert watch.baseline["techne_inside_window"] == 2000.0
        torn = drift.torn if drift else ()
        assert "techne_inside_window" not in torn, (
            "KNOWN RESIDUAL (Prudence r3): a TTL-suppressed poll does no walk, so a "
            "module imported before it and changed after is baselined post-change. If "
            "this now FAILS, someone moved the walk ahead of the TTL gate — the full "
            "fix, costing 0.47 ms on EVERY tool call. Re-read the trade in the module "
            "docstring before keeping it."
        )
    finally:
        del sys.modules["techne_inside_window"]


def test_a_quiet_deployment_stays_re_examinable(marker, tmp_path):
    """Prudence r4, Important — the behaviour change shipped with nothing holding it.

    She restored the one-line `_reported.add` on the not-torn branch and got 18 passed,
    then 144 on a widened selection. THE PATTERN IS THE FINDING: this is the fourth fix
    on this PR to ship unguarded, after the `ttl=0.0` fixture, the deleted `begin()`
    block, and the vacuous pin.

    What it protects: a deployment whose ref moves before its files settle reads as
    "nothing differs". Recording that marker value suppressed it permanently, so the tear
    was never found once the files landed. Not recording it costs nothing — the walk runs
    before both the marker stat and the `_reported` check, so it was never conditional on
    the add (measured: 20 stats per poll either way).
    """
    import types

    watch = rd.begin(None, ttl=0.0)
    late = tmp_path / "settles_late.py"
    late.write_text("x = 1")
    os.utime(late, (0, 1000.0))
    mod = types.ModuleType("techne_settles_late")
    mod.__file__ = str(late)
    sys.modules["techne_settles_late"] = mod
    try:
        assert rd.poll(watch) is None  # baseline it

        # The ref moves, but the file has not settled yet: reads as "nothing differs".
        _move(marker)
        assert rd.poll(watch) is None
        assert not watch._reported, (
            "a marker value we did NOT report on must stay re-examinable; recording it "
            "here permanently suppresses a deployment whose files settle afterwards"
        )

        # The file settles. The SAME marker value must still be able to speak.
        os.utime(late, (0, 2000.0))
        drift = rd.poll(watch)
        assert drift is not None and "techne_settles_late" in drift.torn
    finally:
        del sys.modules["techne_settles_late"]
