"""The TECHNE_HOME mismatch warning reaches the operator once per key (techne#964).

Grip saw FOUR long lines before every command from a config setting both
`db_path` and `data_dir`. Two per key.

ROOT CAUSE, and it is neither hypothesis in the issue: the memo in
`_warn_if_outside_home` is correct and config is loaded once. `config.log` is
STDLIB logging, unconfigured in a CLI run, so `logging.lastResort` -- a
WARNING-level `_StderrHandler` -- already writes the record to stderr. The
explicit `print(file=sys.stderr)` then wrote the same text again.

WHY THE EXISTING TEST MISSED IT. `test_the_warning_is_not_repeated_in_one_process`
asserts `count(...) == 1` and passed all along, because pytest's logging plugin
installs handlers on the ROOT logger:

    root handlers: [_LiveLoggingNullHandler, _FileHandler /dev/null,
                    LogCaptureHandler, LogCaptureHandler]

With any root handler present `lastResort` never fires, so under pytest only the
`print` ever reached `capsys` and the duplicate was invisible by DEFAULT.

NOT "invisible to any in-process test", which an earlier draft of this docstring
claimed. `_StderrHandler.stream` reads `sys.stderr` dynamically, so an in-process
test that CLEARS the root handlers can observe lastResort perfectly well -- and
`test_the_three_silent_configurations_still_warn` below does exactly that. The
subprocess is preferred here because it also exercises the real CLI wiring, not
because it is the only route.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
import yaml


@pytest.fixture
def relocated_home(tmp_path):
    """A TECHNE_HOME whose config.yaml points BOTH paths outside it."""
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / "config.yaml").write_text(
        yaml.dump({"db_path": str(elsewhere / "techne.db"), "data_dir": str(elsewhere)})
    )
    return home


def _run(home, *args):
    return subprocess.run(
        [sys.executable, "-m", "techne.cli", *args],
        capture_output=True,
        text=True,
        env={"TECHNE_HOME": str(home), "PATH": "/usr/bin:/bin", "HOME": str(home.parent)},
    )


def test_each_key_warns_exactly_once_in_a_real_run(relocated_home):
    """THE REGRESSION. Two keys outside the home must produce two lines, not four."""
    proc = _run(relocated_home, "status")
    warnings = [ln for ln in proc.stderr.splitlines() if "outside TECHNE_HOME" in ln]

    assert len(warnings) == 2, f"expected one warning per key, got {len(warnings)}:\n" + "\n".join(
        warnings
    )
    assert sum("sets db_path" in ln for ln in warnings) == 1
    assert sum("sets data_dir" in ln for ln in warnings) == 1


def test_the_warning_still_reaches_stderr_at_all(relocated_home):
    """The warning must survive at all, in the ordinary CLI configuration.

    SCOPE, STATED BECAUSE AN EARLIER DRAFT OVERSTATED IT: this does NOT guard
    against `print` being deleted. In this scenario the record also arrives via
    `lastResort`, so the assertions below still pass with the print removed --
    measured. The arm that would actually be lost is covered by
    `test_the_three_silent_configurations_still_warn`, which neutralises the
    logging fallback so the print is the only possible source.
    """
    proc = _run(relocated_home, "status")
    assert "outside TECHNE_HOME" in proc.stderr
    assert "techne#947" in proc.stderr, "the warning must keep its issue pointer"


def test_nothing_leaks_to_stdout(relocated_home):
    """stdout is the MCP JSON-RPC stream and a hook's protocol channel (techne#107)."""
    proc = _run(relocated_home, "status")
    assert "outside TECHNE_HOME" not in proc.stdout


def test_the_three_silent_configurations_still_warn(monkeypatch, capsys, tmp_path):
    """THE REGRESSION THE FIRST FIX INTRODUCED (techne#964, caught in pre-pass).

    The first attempt made the print conditional on
    `log.hasHandlers() or not log.isEnabledFor(WARNING)` and claimed to over-warn
    but never under-warn. Three handler-less configurations silenced it entirely:

        logging.lastResort = None            (a real idiom for muting libraries)
        logging.lastResort.setLevel(ERROR)
        a Filter on the logger rejecting records

    In each, hasHandlers() is False and WARNING is enabled, so the print arm was
    skipped while lastResort emitted nothing and the operator saw NOTHING.

    This runs IN-PROCESS on purpose: it has to neutralise the logging fallback,
    which is exactly what a subprocess cannot be made to do from outside.
    """
    import logging

    from techne.config import _WARNED_OUTSIDE_HOME, _warn_if_outside_home

    home = tmp_path / "home"
    outside = tmp_path / "elsewhere" / "techne.db"
    monkeypatch.setenv("TECHNE_HOME", str(home))

    _ORIGINAL_LAST_RESORT = logging.lastResort
    logger = logging.getLogger("techne.config")
    rejecting = logging.Filter()
    rejecting.filter = lambda _record: False

    scenarios = {
        # EXPLICIT restore, not monkeypatch: monkeypatch unwinds at TEST end, so
        # setting lastResort=None here left it None for the next scenario, whose
        # setup then died on AttributeError. Each scenario must restore its own.
        "lastResort=None": (
            lambda: setattr(logging, "lastResort", None),
            lambda: setattr(logging, "lastResort", _ORIGINAL_LAST_RESORT),
        ),
        "lastResort level=ERROR": (
            lambda: logging.lastResort.setLevel(logging.ERROR),
            lambda: logging.lastResort.setLevel(logging.WARNING),
        ),
        "filter rejects records": (
            lambda: logger.addFilter(rejecting),
            lambda: logger.removeFilter(rejecting),
        ),
    }

    for label, (setup, teardown) in scenarios.items():
        _WARNED_OUTSIDE_HOME.clear()
        setup()
        try:
            _warn_if_outside_home("db_path", outside, home)
            err = capsys.readouterr().err
        finally:
            teardown()
        assert err.count("outside TECHNE_HOME") == 1, (
            f"{label}: expected exactly one operator-visible warning, got "
            f"{err.count('outside TECHNE_HOME')}. Under-warning here means a silent "
            f"write to the wrong store. stderr was: {err!r}"
        )


def test_the_null_handler_is_what_makes_the_print_unconditional():
    """Pins the mechanism, so removing the NullHandler fails loudly here.

    Without it, stdlib logging falls back to lastResort (a WARNING-level stderr
    handler) and the unconditional print becomes a duplicate again.
    """
    import logging

    import techne.config  # noqa: F401 — imported for its handler side effect

    handlers = logging.getLogger("techne.config").handlers
    assert any(isinstance(h, logging.NullHandler) for h in handlers), (
        "techne.config lost its NullHandler; lastResort can fire again and the "
        "unconditional print in _warn_if_outside_home becomes a duplicate"
    )


def test_the_null_handler_does_not_silence_the_rest_of_techne():
    """THE ROUND-2 REGRESSION: the first fix muted 52 warnings to stop one duplicate.

    The NullHandler was attached to the "techne" TREE, which made lastResort
    unreachable for EVERY techne logger the moment this module was imported --
    and techne.config is imported by nearly everything. The other 52 stdlib
    log.warning/error/exception calls in the package stopped reaching the
    operator entirely.

    It belongs on THIS module's logger alone: techne.config stops reaching
    lastResort, every sibling logger still resolves through an empty chain to it.
    """
    import logging

    import techne.config  # noqa: F401 — imported for its handler side effect

    assert not any(
        isinstance(h, logging.NullHandler) for h in logging.getLogger("techne").handlers
    ), "the NullHandler is on the techne TREE again; it silences the whole package"

    # A SUBPROCESS, FOR THE REASON THIS MODULE ALREADY DOCUMENTS: pytest installs
    # root handlers, so lastResort never fires in-process and an in-process probe
    # of "does a sibling logger reach stderr" is blind either way.
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging, techne.config\n"
            "logging.getLogger('techne.somewhere_else').warning('SIBLING-WARNING')\n",
        ],
        capture_output=True,
        text=True,
    )
    assert "SIBLING-WARNING" in proc.stderr, (
        "a sibling techne logger no longer reaches the operator; the NullHandler "
        f"is scoped too widely. stderr was: {proc.stderr!r}"
    )
