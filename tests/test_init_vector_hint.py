"""techne#944 — `init` never told a new user that search was keyword-only.

A missing or mistyped extra (`techne[vec]` instead of `[vectors]`) is only a pip
warning, so the install succeeds with no embedding backend and every search runs
FTS-only. `capabilities` says so, but init's next steps never pointed there.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from techne.cli import main


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / ".techne"))
    return tmp_path


def _init(*args):
    result = CliRunner().invoke(main, ["init", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_standalone_init_says_search_is_keyword_only_without_a_backend(isolated, monkeypatch):
    monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: False)
    out = _init("--platform", "standalone")
    assert "keyword-only" in out, out
    assert "tenish-techne[recommended] @ git+https://github.com/tenishai/techne.git@" in out, (
        "the hint must give the git install form: PyPI `techne` is an unrelated package"
    )
    assert "techne vectors backfill" in out


def test_standalone_init_points_at_backfill_when_nothing_is_embedded(isolated, monkeypatch):
    monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: True)
    out = _init("--platform", "standalone")
    assert "keyword-only" not in out
    assert "nothing embedded yet" in out, out
    assert "techne vectors backfill" in out


def test_no_hint_when_vectors_exist(isolated, monkeypatch):
    monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: True)
    monkeypatch.setattr(
        "techne.core.vector_status.vector_row_counts",
        lambda db: {"threads_vec": None, "observations_vec": 0, "knowledge_vec": 12},
    )
    out = _init("--platform", "standalone")
    assert "nothing embedded yet" not in out
    assert "keyword-only" not in out


def test_standalone_init_names_capabilities(isolated):
    assert "techne capabilities" in _init("--platform", "standalone")


def test_claude_code_init_also_warns_without_a_backend(isolated, monkeypatch):
    monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: False)
    out = _init("--platform", "claude-code")
    assert "techne initialized" in out
    assert "keyword-only" in out, out


def test_the_install_hint_never_prints_a_non_release_tag(isolated, monkeypatch):
    monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: False)
    monkeypatch.setattr("techne.cli.__version__", "0.0.0+unknown")
    out = _init("--platform", "standalone")
    assert "techne.git@<tag>" in out
    assert "0.0.0+unknown" not in out


def test_the_next_steps_check_never_loads_the_model_even_with_a_backend_stamp(
    isolated, monkeypatch
):
    """A second `init` on a store that was backfilled has an identity stamp, and
    vector_channel_status builds the backend (a model load) whenever one exists.
    The next-steps check must not. Since #1021 init DOES load the model by default,
    to warm it; `--no-vectors` isolates the check this test is about."""
    from techne.core.database import connect, init_db
    from techne.core.extension_state import set_state

    _init("--platform", "standalone")  # creates the store
    from techne.config import Config

    db = Config.load().db_path
    init_db(db).close()
    conn = connect(db)
    try:
        set_state(conn, "embedding_backend", {"name": "fastembed", "dim": 384})
    finally:
        conn.close()

    calls = []

    def spy(*a, **kw):
        calls.append(1)
        raise AssertionError("init must not build the embedding backend")

    monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: True)
    monkeypatch.setattr("techne.core.embeddings._get_embedding_model", spy)
    out = _init("--platform", "standalone", "--no-vectors")
    assert not calls, "the next-steps check built the embedding backend (a model load)"
    assert "nothing embedded yet" in out
