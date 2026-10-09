"""Importing the post_tool_use hook path loads neither `logging` nor `tempfile`
(issue 1192).

On Python 3.14, `logging` costs ~21 ms (it imports `traceback`, which imports
`_colorize`) and `tempfile` ~10 ms (it imports `shutil`). Every hook process paid
both because seven modules on the path imported them at module level, while a
typical tool call never logs and never writes a temp file. They now use
`techne._lazylog.get_logger` and function-level `import tempfile` / `shutil`.

A NAMED check rather than an allowlist budget like test_fastconfig's: these
modules legitimately import yaml, sqlite3, dataclasses and json, so a module
count would be a number nobody can reason about. This pins the three modules
this change removed, by name, so putting one back fails with that name.

A subprocess, because pytest itself has already imported logging and tempfile.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

HOOK_PATH_MODULES = (
    "techne.hooks.post_tool_use",
    "techne.lifecycle",
    "techne.config",
    "techne.config_migration",
    "techne.core.state",
    "techne.core.inference",
    "techne.core.extension_state",
    "techne.platforms.claude_code.hooks",
    "techne.platforms.claude_code.transcript_import",
)


@pytest.mark.parametrize("deferred", ["logging", "tempfile", "shutil"])
def test_importing_the_hook_path_does_not_load(deferred):
    probe = f"import sys\nimport {', '.join(HOOK_PATH_MODULES)}\nprint({deferred!r} in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", (
        f"importing the post_tool_use hook path loads {deferred!r} again. Import it "
        "inside the function that needs it, or use techne._lazylog.get_logger."
    )


def test_lazy_logger_is_the_real_logger_once_used(caplog):
    import logging

    from techne._lazylog import get_logger

    log = get_logger("techne.test_lazylog_probe", null_handler=True)
    with caplog.at_level(logging.WARNING, logger="techne.test_lazylog_probe"):
        log.warning("hello %s", "there")
    assert "hello there" in caplog.text
    real = logging.getLogger("techne.test_lazylog_probe")
    assert log.handlers is real.handlers
    assert sum(isinstance(h, logging.NullHandler) for h in real.handlers) == 1
