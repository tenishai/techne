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

With any root handler present `lastResort` never fires, so under pytest only
the `print` ever reached `capsys` and the duplicate was structurally invisible.
Any in-process test of this behaviour is blind for the same reason.

Hence the subprocess: it is the only way to observe what the operator observes.
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
    """Guards the other direction: silencing the duplicate must not silence the warning.

    Deleting `print` outright would also pass the test above, and would leave an
    operator whose app configures logging to a file with no visible warning.
    """
    proc = _run(relocated_home, "status")
    assert "outside TECHNE_HOME" in proc.stderr
    assert "techne#947" in proc.stderr, "the warning must keep its issue pointer"


def test_nothing_leaks_to_stdout(relocated_home):
    """stdout is the MCP JSON-RPC stream and a hook's protocol channel (techne#107)."""
    proc = _run(relocated_home, "status")
    assert "outside TECHNE_HOME" not in proc.stdout
