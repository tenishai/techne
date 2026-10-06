"""Tests for `techne vectors backfill` / `techne vectors status` (techne#284).

Wires search.backfill_vectors() (the #283 embedding-backend-identity
migration) to a CLI command, plus a read-only status surface
(techne.core.vector_status) that reports whether a re-embed is needed
WITHOUT mutating the vec0 tables. See vector_status.py's module docstring
for why status can't just call search._ensure_vec_backend_identity.
"""

from __future__ import annotations

import json
import struct

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.embeddings import EmbeddingBackend
from techne.core.extension_state import set_state
from techne.core.knowledge import store as store_knowledge
from techne.core.search import _VEC_BACKEND_STATE_NAME, _ensure_vec_table


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A Config pointed at a fresh initialized DB, injected via Config.load()."""
    db_path = tmp_path / "techne.db"
    init_db(db_path).close()
    cfg = Config()
    cfg.db_path = db_path
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    return cfg


def _invoke(args, env=None):
    runner = CliRunner()
    return runner.invoke(main, args, catch_exceptions=False, env=env)


# ---------------------------------------------------------------------------
# vectors backfill
# ---------------------------------------------------------------------------


class TestVectorsBackfill:
    def test_happy_path_embeds_seeded_knowledge_rows(self, cfg):
        """On this py3.14 venv (model2vec + sqlite-vec + numpy installed by
        design), a DB with knowledge rows should actually get embedded —
        exercises the real backfill_vectors() path, not a mock."""
        from techne.core import embeddings as embeddings_mod

        if not embeddings_mod.backend_available():
            pytest.skip("no embedding backend installed in this environment")
        pytest.importorskip("sqlite_vec")

        store_knowledge(cfg.db_path, "The observatory recorded an unusual meteor shower.")
        store_knowledge(
            cfg.db_path, "The archive team migrated records to the new shelving system."
        )

        result = _invoke(["vectors", "backfill"])
        assert result.exit_code == 0, result.output

        counts = json.loads(result.output)
        assert counts["knowledge"] == 2
        assert counts["threads"] == 0
        assert counts["observations"] == 0

    @pytest.mark.parametrize(
        "argv, expected",
        [
            (["vectors", "backfill"], True),
            (["vectors", "backfill", "--no-download"], False),
        ],
    )
    def test_no_download_flag_reaches_allow_download(self, cfg, monkeypatch, argv, expected):
        """THE NEGATION HAS EXACTLY ONE HOME AND NOTHING WAS WATCHING IT
        (PR #815 review r2). `allow_download=not no_download` in cli.py is the
        only place the flag inverts. The hook test pins `--no-download` in the
        shell string and the search test pins `allow_download=False` in the
        Python API, but they sit either side of this line: inverting it to
        `allow_download=no_download` left the whole suite green while the hook
        asked for the guard and got a download — the exact defect the flag
        exists to prevent.

        BOTH ROWS ARE LOAD-BEARING. Asserting only the flagged case would pass
        with the parameter hard-wired to False, which would strand a fresh
        install on FTS forever (techne#496 from the other side).
        """
        import sys

        seen: dict = {}

        def _spy(db_path, batch_size=100, *, allow_download=True):
            seen["allow_download"] = allow_download
            return {"orphans_swept": 0, "sweep_errors": 0}

        # The command exits early when sqlite_vec is absent, which is the state
        # of any venv without the [vectors] extra — without this stub the test
        # would pass vacuously there by never reaching the call it pins.
        monkeypatch.setitem(sys.modules, "sqlite_vec", type("m", (), {})())
        monkeypatch.setattr("techne.core.search.backfill_vectors", _spy)

        res = _invoke(argv)

        assert res.exit_code == 0, res.output
        assert seen.get("allow_download") is expected, (
            f"{' '.join(argv)} produced allow_download={seen.get('allow_download')!r}, "
            f"expected {expected!r}"
        )

    def test_no_backend_available_is_graceful_exit_zero(self, cfg, monkeypatch):
        """When backend_available() is False, the command must print a
        friendly message and exit 0 — never a stack trace/non-zero exit for
        a user who simply didn't install the optional [vectors] extra."""
        from techne.core import embeddings as embeddings_mod

        monkeypatch.setattr(embeddings_mod, "backend_available", lambda: False)

        result = _invoke(["vectors", "backfill"])
        assert result.exit_code == 0, result.output
        assert "vector search unavailable" in result.output
        assert "tenish-techne[vectors]" in result.output

    def test_uninitialized_db_reports_and_exits_zero(self, tmp_path, monkeypatch):
        db_path = tmp_path / "nope" / "techne.db"
        cfg = Config()
        cfg.db_path = db_path
        monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))

        result = _invoke(["vectors", "backfill"])
        assert result.exit_code == 0
        assert "not initialized" in result.output


# ---------------------------------------------------------------------------
# vectors status
# ---------------------------------------------------------------------------


class TestVectorsStatus:
    def test_fresh_db_reports_sensibly(self, cfg):
        """No stamp, no vec tables yet — status should report that plainly
        and NOT claim a re-embed is needed (there's nothing to re-embed;
        it's simply un-backfilled, not stale)."""
        result = _invoke(["vectors", "status"])
        assert result.exit_code == 0, result.output
        assert "stored identity stamp: none" in result.output
        assert "threads_vec: absent" in result.output
        assert "observations_vec: absent" in result.output
        assert "knowledge_vec: absent" in result.output
        assert "re-embed needed: False" in result.output

    def test_stale_after_backend_swap_reports_reembed_needed(self, cfg, monkeypatch):
        """Model the scenario from TestEnsureVecBackendIdentity: stamp a
        384-d fastembed identity + an existing 384-d table, then make the
        CURRENT active backend a 256-d model2vec model. Status must report
        reembed_needed=True and print the hint — all without dropping the
        table (this is the non-mutating read-only check)."""
        sqlite_vec = pytest.importorskip("sqlite_vec")

        conn = connect(cfg.db_path)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)

            _ensure_vec_table(conn, "threads_vec", 384)
            emb = struct.pack("384f", *([0.1] * 384))
            conn.execute("INSERT INTO threads_vec(rowid, embedding) VALUES (1, ?)", (emb,))
            conn.commit()

            set_state(
                conn,
                _VEC_BACKEND_STATE_NAME,
                {"name": "fastembed:sentence-transformers/all-MiniLM-L6-v2", "dim": 384},
            )
        finally:
            conn.close()

        # Force the active backend to a 256-d model2vec identity regardless
        # of what's actually importable in this environment — status only
        # needs `_get_embedding_model()` to return something with .name/.dim.
        fake_backend = EmbeddingBackend("model2vec", "minishlab/potion-base-8M", impl=None, dim=256)
        monkeypatch.setattr(
            "techne.core.embeddings._get_embedding_model", lambda *a, **k: fake_backend
        )

        result = _invoke(["vectors", "status"])
        assert result.exit_code == 0, result.output
        assert (
            "active embedding backend: model2vec:minishlab/potion-base-8M (dim=256)"
            in result.output
        )
        assert (
            "stored identity stamp: fastembed:sentence-transformers/all-MiniLM-L6-v2 (dim=384)"
            in result.output
        )
        assert "threads_vec: 1 row(s) (dim=384)" in result.output
        assert "re-embed needed: True" in result.output
        assert "run `techne vectors backfill` to restore it" in result.output

        # Non-mutating: the table must still exist with its original row.
        conn = connect(cfg.db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='threads_vec'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()

    def test_uninitialized_db_reports_and_exits_zero(self, tmp_path, monkeypatch):
        db_path = tmp_path / "nope" / "techne.db"
        cfg = Config()
        cfg.db_path = db_path
        monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))

        result = _invoke(["vectors", "status"])
        assert result.exit_code == 0
        assert "not initialized" in result.output
