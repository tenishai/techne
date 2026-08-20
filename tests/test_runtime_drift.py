"""A server that notices the code moved underneath it (techne#696)."""

from __future__ import annotations

import os
import sys
import time
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


def _move(path: Path) -> None:
    """A pull: the ref moves, so the reflog's mtime changes."""
    time.sleep(0.01)
    path.write_text(path.read_text() + "x")


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


def test_a_module_changing_under_us_is_a_tear(marker):
    """The dangerous state, and the one only self-introspection can see: we keep serving
    the OLD definition of what we already loaded, while anything imported from here on
    comes from the NEW source. Reachable in practice — mcp_server.py carries 46
    function-local `from techne…` imports."""
    watch = rd.begin(None, ttl=0.0)
    _move(marker)
    os.utime(Path(sys.modules["techne.core.runtime_drift"].__file__), None)

    drift = rd.poll(watch)
    assert drift is not None
    assert "techne.core.runtime_drift" in drift.torn
    assert "techne.core.runtime_drift" in drift.message()


def test_one_warning_per_deployment(marker):
    """This condition does NOT self-clear — it persists for the life of the process. So
    unlike a SessionStart banner, repeating it would repeat FOREVER, on every tool call.
    Same discipline as `capabilities.invitation`: say it once."""
    watch = rd.begin(None, ttl=0.0)
    _move(marker)
    os.utime(Path(sys.modules["techne.core.runtime_drift"].__file__), None)

    assert rd.poll(watch) is not None
    assert rd.poll(watch) is None, "a repeated warning is how a true signal becomes noise"


def test_a_second_pull_warns_again(marker):
    """Suppression is keyed on the marker mtime, NOT a one-shot flag. A second deploy
    during the same process is new information and must not be swallowed by the first
    one's suppression."""
    watch = rd.begin(None, ttl=0.0)
    mod = Path(sys.modules["techne.core.runtime_drift"].__file__)

    _move(marker)
    os.utime(mod, None)
    assert rd.poll(watch) is not None

    _move(marker)
    os.utime(mod, None)
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
        assert "techne_ghost_module" not in rd._torn_modules(0.0)
    finally:
        del sys.modules["techne_ghost_module"]


def test_a_module_without_a_file_is_skipped():
    """Namespace packages and frozen imports have no `__file__`. Absence of a path is
    not evidence of staleness."""
    import types

    ns = types.ModuleType("techne_namespace_like")
    sys.modules["techne_namespace_like"] = ns
    try:
        assert "techne_namespace_like" not in rd._torn_modules(0.0)
    finally:
        del sys.modules["techne_namespace_like"]


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
