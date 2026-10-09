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


def test_lazy_logger_forwards_attribute_assignment():
    """`log.propagate = False` must reach the real logger, as it would with a plain
    module logger. __slots__ alone makes it an AttributeError (Prudence, PR 1216)."""
    import logging

    from techne._lazylog import get_logger

    name = "techne.test_lazylog_setattr_probe"
    log = get_logger(name)
    log.propagate = False
    log.disabled = True
    real = logging.getLogger(name)
    try:
        assert real.propagate is False and real.disabled is True
    finally:
        real.propagate, real.disabled = True, False


def test_recall_path_does_not_import_numpy(tmp_path):
    """Issue 1217: per-turn recall embeds the prompt without numpy or the
    sqlite_vec package (whose __init__ imports numpy.typing). Runs the real
    user_prompt_submit.main() in a subprocess against a store with knowledge
    vectors, with a tiny potion-layout model planted in a private HF cache under
    the default model's name, so the AUTO choice resolves to it offline."""
    pytest.importorskip("numpy")
    pytest.importorskip("tokenizers")
    pytest.importorskip("safetensors")
    pytest.importorskip("sqlite_vec")
    from techne.core import embeddings

    if embeddings._select_backend(None)[0] != "model2vec":
        pytest.skip("AUTO picks fastembed here; the numpy-free path is model2vec-only")

    import json
    import os

    from tests.test_query_embed import write_model

    hub = tmp_path / "hub"
    name = embeddings._DEFAULT_MODEL2VEC_MODEL
    snap = hub / f"models--{name.replace('/', '--')}" / "snapshots" / "fixture"
    write_model(snap, dim=32, seed=3)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "TECHNE_HOME": str(home), "HF_HUB_CACHE": str(hub)}

    seed = (
        "from techne.core.database import init_db\n"
        "from techne.core.knowledge import store\n"
        "from techne.core.search import backfill_vectors\n"
        "from pathlib import Path\n"
        f"db = Path({str(home / 'techne.db')!r})\n"
        "init_db(db).close()\n"
        "for i, t in enumerate(['alpha beta gamma', 'search hook recall', 'vector model']):\n"
        "    store(db, f'{t} fact {i}', type='fact')\n"
        "print(backfill_vectors(db, allow_download=False))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", seed], capture_output=True, text=True, env=env, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert "'knowledge': 3" in out.stdout, out.stdout  # the vector channel has rows

    report = tmp_path / "report.json"
    probe = (
        "import json, sys\n"
        "from techne.hooks import user_prompt_submit\n"
        "try:\n"
        "    user_prompt_submit.main()\n"
        "except SystemExit:\n"
        "    pass\n"
        "from techne.core import query_embed\n"
        f"open({str(report)!r}, 'w').write(json.dumps({{\n"
        "    'numpy': 'numpy' in sys.modules,\n"
        "    'sqlite_vec': 'sqlite_vec' in sys.modules,\n"
        "    'fast_path_models': len(query_embed._MODELS),\n"
        "}))\n"
    )
    payload = json.dumps(
        {"session_id": "s-1217", "prompt": "alpha search hook", "cwd": str(tmp_path)}
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    got = json.loads(report.read_text())
    assert got["fast_path_models"] == 1, (got, out.stderr)
    assert got == {"numpy": False, "sqlite_vec": False, "fast_path_models": 1}, (
        "the per-turn recall path imported numpy or sqlite_vec again; "
        f"stderr: {out.stderr[-2000:]}"
    )


#: The Stop hook's gate path (issue 1218): everything `session_end` imports
#: before it learns there is nothing new to detect, plus `techne.core.threads`,
#: which `run_session_end` imports first thing.
SESSION_END_PATH_MODULES = (
    "techne.hooks.session_end",
    "techne.hooks._liveness",
    "techne.hooks._timeout",
    "techne.config",
    "techne.lifecycle",
    "techne.core.state",
    "techne.core.threads",
    "techne.core.extension_state",
    "techne.platforms.claude_code.transcript_import",
    "techne.sync.adapter",
    "techne.sync.outbox",
    "techne.sync.sinks",
)


@pytest.mark.parametrize("deferred", ["structlog", "asyncio"])
def test_importing_the_session_end_path_does_not_load(deferred):
    """structlog costs ~55 ms and drags in asyncio; the Stop hook paid it every
    turn through techne.core.threads -> thread_orchestrator -> techne._log."""
    probe = (
        f"import sys\nimport {', '.join(SESSION_END_PATH_MODULES)}\n"
        f"print({deferred!r} in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", (
        f"importing the session_end hook path loads {deferred!r} again. Use "
        "techne._log.get_logger (lazy) and import it inside the function that needs it."
    )


def test_lazy_struct_logger_writes_to_stderr_not_stdout():
    """First use configures structlog to stderr: stdout is the hook/MCP protocol."""
    probe = (
        "from techne._log import get_logger\n"
        "log = get_logger('probe')\n"
        "import sys\nassert 'structlog' not in sys.modules\n"
        "log.warning('lazy_probe_event', k=1)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == ""
    assert "lazy_probe_event" in out.stderr


def test_lazy_struct_logger_is_seen_by_capture_logs():
    from structlog.testing import capture_logs

    from techne._log import get_logger

    log = get_logger("techne.test_lazy_struct_probe")
    with capture_logs() as logs:
        log.error("probe_event", n=2)
    assert any(e.get("event") == "probe_event" and e.get("n") == 2 for e in logs)
