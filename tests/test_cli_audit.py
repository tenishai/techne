"""CLI-level tests for `techne audit` — the edit-rail attribution panel (techne#374).

The attribution math is unit-tested in test_utilization.py; these lock the CLI
rendering: the panel appears, handles the empty case, and surfaces the payload-
composition line (the finding that the rail's injected dependents are mostly
module/test noise).
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.core.state import add_observation, ensure_session
from techne.hooks._injection_log import log_context_injection


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    """A Config pointed at a fresh tmp db, injected via Config.load()."""
    from techne.config import Config
    from techne.core.database import init_db

    db = tmp_path / "techne.db"
    init_db(db).close()
    ensure_session(db, "s1")
    cfg = Config()
    cfg.db_path = db
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    return cfg


def test_audit_edit_rail_panel_empty(cli_db):
    result = CliRunner().invoke(main, ["audit"])
    assert result.exit_code == 0, result.output
    assert "EDIT-RAIL ATTRIBUTION" in result.output
    assert "no codex_edit_rail injections logged yet" in result.output


def test_audit_edit_rail_panel_populated(cli_db):
    # One injection whose payload is 1 real / 1 test / 1 module node, plus a
    # following edit to the real dependent's file.
    log_context_injection(
        cli_db.db_path, source="codex_edit_rail", session_id="s1",
        trigger="techne/trig.py::t::function::5",
        rows=[
            {"id": "techne/foo.py::bar::function::10"},    # real
            {"id": "tests/test_foo.py::baz::function::3"},  # test noise
            {"id": "techne/foo.py::<module>::module::0"},   # module noise
        ],
        injected_text="x" * 30,
    )
    add_observation(cli_db.db_path, "s1", "Edit", "Edited: /repo/techne/foo.py\nChange: bar()")

    result = CliRunner().invoke(main, ["audit"])
    assert result.exit_code == 0, result.output
    assert "EDIT-RAIL ATTRIBUTION" in result.output
    assert "N=1 injections" in result.output
    # Payload composition surfaced (1/3 each → 33.3%).
    assert "payload: real 33.3%" in result.output
    assert "module-noise 33.3%" in result.output
    # Proximity caveat is always printed.
    assert "PROXIMITY" in result.output
