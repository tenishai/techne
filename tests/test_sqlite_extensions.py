"""techne#1321: a Python whose sqlite3 cannot load extensions gets no semantic recall.

python.org and Apple's system builds lack `sqlite3.Connection.enable_load_extension`,
so sqlite-vec never loads. `init` said "Semantic recall ready: embedded 0", `vectors
backfill` returned {} and exited 0, and `vectors:coverage` said to run that backfill.
The interpreter's capability is faked here; nothing else is.
"""

from __future__ import annotations

import sqlite3

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.config import Config
from techne.core import embeddings as E
from techne.core import search as search_mod
from techne.core.database import init_db


@pytest.fixture
def no_ext(monkeypatch):
    monkeypatch.setattr(search_mod, "sqlite_loads_extensions", lambda: False)


def test_the_probe_reads_the_connection_attribute(monkeypatch):
    class Built_Without_It:  # noqa: N801 — a stand-in for the C type
        pass

    assert search_mod.sqlite_loads_extensions() is hasattr(
        sqlite3.Connection, "enable_load_extension"
    )
    monkeypatch.setattr(search_mod.sqlite3, "Connection", Built_Without_It)
    assert search_mod.sqlite_loads_extensions() is False


def test_the_message_names_the_interpreter_and_both_fixes():
    lines = search_mod.no_extension_loading_lines()
    text = "\n".join(lines)
    assert __import__("sys").executable in lines[0]
    assert "uv tool install --force --managed-python" in text
    assert "pipx install --force --python <path-to-python>" in text


class TestInit:
    @pytest.fixture
    def isolated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path / ".techne"))
        monkeypatch.setattr(E, "backend_available", lambda: True)
        monkeypatch.setattr(E, "_select_backend", lambda c: ("model2vec", "stub/model"))
        monkeypatch.setattr(E, "model_cached", lambda config=None: False)
        loads: list[bool] = []

        def load(config=None, *, allow_download=True):
            loads.append(allow_download)
            return object()

        monkeypatch.setattr(E, "_get_embedding_model", load)
        return loads

    pytestmark = pytest.mark.real_warm_vectors

    def _init(self):
        result = CliRunner().invoke(main, ["init", "--platform", "standalone"])
        assert result.exit_code == 0, result.output
        return result.output

    def test_no_extension_loading_skips_the_download_and_says_why(self, isolated, no_ext):
        out = self._init()
        assert isolated == [], "weights this interpreter cannot use must not be fetched"
        assert "Downloading" not in out
        assert "Semantic recall ready" not in out
        assert "Semantic recall is NOT ready" in out
        assert "cannot load extensions" in out

    def test_a_result_without_the_sweep_key_is_not_ready(self, isolated, monkeypatch):
        """`orphans_swept` is #408's discriminator: absent, backfill_vectors returned
        before it could load sqlite-vec, so nothing was embedded."""
        monkeypatch.setattr(search_mod, "backfill_vectors", lambda db, **kw: {})
        out = self._init()
        assert "Semantic recall ready" not in out
        assert "Semantic recall is NOT ready" in out

    def test_the_ready_count_is_rows_embedded_not_orphans_swept(self, isolated, monkeypatch):
        counts = {"orphans_swept": 5, "sweep_errors": 0, "threads": 1, "observations": 0}
        monkeypatch.setattr(
            search_mod, "backfill_vectors", lambda db, **kw: counts | {"knowledge": 2}
        )
        assert "Semantic recall ready: embedded 3 stored row(s)." in self._init()


class TestVectorsBackfill:
    @pytest.fixture
    def cfg(self, tmp_path, monkeypatch):
        pytest.importorskip("sqlite_vec")
        cfg = Config()
        cfg.db_path = tmp_path / "techne.db"
        init_db(cfg.db_path).close()
        monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
        monkeypatch.setattr(
            search_mod, "backfill_vectors", lambda *a, **k: pytest.fail("must not run")
        )
        return cfg

    def test_exits_non_zero_with_the_reason_on_stderr(self, cfg, no_ext, monkeypatch):
        monkeypatch.setattr(E, "backend_available", lambda: False)
        result = CliRunner().invoke(main, ["vectors", "backfill"])
        assert result.exit_code == 1, result.output
        assert "cannot load extensions" in result.stderr
        assert result.stdout == ""

    def test_pending_tags_are_still_inferred_and_stdout_stays_json(self, cfg, no_ext, monkeypatch):
        import json

        calls = []
        monkeypatch.setattr(E, "backend_available", lambda: True)
        monkeypatch.setattr(
            "techne.core.inference.backfill_tags",
            lambda db, config, **kw: calls.append(kw) or {"scanned": 4, "tagged": 3},
        )
        result = CliRunner().invoke(main, ["vectors", "backfill", "--no-download"])
        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout) == {"tags": {"scanned": 4, "tagged": 3}}
        assert calls == [{"pending_only": True}]
        assert "cannot load extensions" in result.stderr


class TestVectorsCoverage:
    def test_names_the_cause_and_the_reinstall_not_the_backfill(
        self, tmp_path, monkeypatch, no_ext
    ):
        from techne.core.checks import check_vector_coverage

        cfg = Config()
        cfg.db_path = tmp_path / "t.db"
        init_db(cfg.db_path).close()
        conn = sqlite3.connect(cfg.db_path)
        conn.execute("DROP TABLE IF EXISTS knowledge_vec_rowids")
        conn.commit()
        conn.close()
        monkeypatch.setattr(E, "backend_available", lambda: True)
        monkeypatch.setattr(E, "model_cached", lambda c=None: True)

        res = check_vector_coverage(cfg)

        assert "cannot load extensions" in res.detail
        assert res.remedy.startswith("uv tool install --force --managed-python")
        assert res.remedy != "techne vectors backfill"


class TestEverySurfaceDropsTheBackfillHint:
    """PR 1324 review, Important 1: `capabilities` and `init --no-vectors` still
    said `techne vectors backfill`, which exits 1 on this interpreter."""

    def test_capabilities_lists_neither_vectors_entry(self, no_ext, monkeypatch):
        from techne.core.capabilities import _probe_vectors_backend, _probe_vectors_model

        monkeypatch.setattr(E, "backend_available", lambda: True)
        monkeypatch.setattr(E, "model_cached", lambda config=None: False)

        assert _probe_vectors_model(Config()) is None
        assert _probe_vectors_backend(Config()) is None

    def test_init_without_the_warm_step_names_the_cause(self, tmp_path, no_ext, monkeypatch):
        from techne.cli import _vector_next_steps

        monkeypatch.setattr(E, "backend_available", lambda: True)
        out = "\n".join(_vector_next_steps(tmp_path / "techne.db"))

        assert "cannot load extensions" in out
        assert "techne vectors backfill" not in out
