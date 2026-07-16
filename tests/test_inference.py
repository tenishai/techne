"""Tests for embedding-based tag + kind inference (Part A of #19)."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from techne.config import Config
from techne.core.database import init_db
from techne.core.inference import (
    InferenceResult,
    _reset_cache,
    infer,
    load_prototypes,
)
from techne.core.state import add_observation, get_observations, start_session


@pytest.fixture(autouse=True)
def clear_inference_cache():
    """Reset module-level embedding cache between tests."""
    _reset_cache()
    yield
    _reset_cache()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = init_db(path)
    conn.close()
    return path


@pytest.fixture
def config(db_path):
    cfg = Config()
    cfg.db_path = db_path
    return cfg


class TestLoadPrototypes:
    def test_bundled_prototypes_load(self):
        protos = load_prototypes()
        assert "decision" in protos
        assert "fact" in protos
        assert "question" in protos
        assert "kind:decision-shaped" in protos
        assert "kind:fact-shaped" in protos

    def test_user_override_wins(self, tmp_path):
        user_proto = tmp_path / "prototypes.yaml"
        user_proto.write_text("decision:\n  - custom decision sentence\n")
        cfg = Config()
        cfg.data_dir = tmp_path
        protos = load_prototypes(cfg)
        assert protos["decision"] == ["custom decision sentence"]

    def test_user_file_extends(self, tmp_path):
        user_proto = tmp_path / "prototypes.yaml"
        user_proto.write_text("my-custom-tag:\n  - custom tag sentence\n")
        cfg = Config()
        cfg.data_dir = tmp_path
        protos = load_prototypes(cfg)
        assert "my-custom-tag" in protos
        assert "decision" in protos  # bundled still present

    def test_prototypes_cached(self):
        p1 = load_prototypes()
        p2 = load_prototypes()
        assert p1 is p2

    def test_config_none_does_not_poison_later_override(self, tmp_path):
        """techne#168: a first call with config=None cached a bundled-only result
        that every later config-bearing call then returned — silently dropping
        operator overrides. The two-slot cache (keyed on override-active) fixes it."""
        # 1st: bundled-only (config=None) — caches under the False slot.
        bundled = load_prototypes(None)
        assert "my-custom-tag" not in bundled

        # 2nd: a real config WITH an override file — must NOT return the poisoned slot.
        user_proto = tmp_path / "prototypes.yaml"
        user_proto.write_text("my-custom-tag:\n  - custom tag sentence\n")
        cfg = Config()
        cfg.data_dir = tmp_path
        merged = load_prototypes(cfg)
        assert "my-custom-tag" in merged  # override applied despite the earlier None call
        assert "decision" in merged       # bundled still present

        # And the bundled-only slot is untouched (still no override).
        assert "my-custom-tag" not in load_prototypes(None)

    def test_override_cache_slot_distinct_from_bundled(self, tmp_path):
        """The two cache slots are independent objects, never aliased."""
        user_proto = tmp_path / "prototypes.yaml"
        user_proto.write_text("my-custom-tag:\n  - s\n")
        cfg = Config()
        cfg.data_dir = tmp_path
        assert load_prototypes(cfg) is not load_prototypes(None)

    def test_get_prototype_embeddings_populated_path_and_two_slots(self):
        """techne#168: exercise the REAL _get_prototype_embeddings body (the populated
        path, normally gated behind an embedding backend + numpy) with injected fakes.
        Guards the `_embed_cache[key] = result` write: an `_embed_cache = result` rebind
        WITHOUT a `global` (as a careless refactor would write) makes the name local and
        the top-of-function `_embed_cache.get(key)` raise UnboundLocalError — so reaching
        the body at all is the regression guard. Also asserts the two embed-cache slots
        are independent.

        techne#601/#602: the availability probe is now `embeddings.backend_available()`
        rather than a bare `from fastembed import TextEmbedding` — faking `sys.modules`
        no longer short-circuits it (backend_available() does a REAL `import model2vec`
        internally, which would import the REAL numpy and collide with the faked numpy
        module below). Patch `backend_available` directly instead — cleaner, and matches
        the new interface.
        """
        import types

        from techne.core import inference

        class _FakeArr:
            def __init__(self, vals):
                self._vals = vals

            def mean(self, axis=0):
                return ("centroid", tuple(self._vals))

        fake_np = types.SimpleNamespace(stack=lambda lst: _FakeArr(lst))

        class _FakeModel:
            def embed(self, sentences):
                return [len(s) for s in sentences]

        _reset_cache()
        with (
            patch.dict(sys.modules, {"numpy": fake_np}),
            patch("techne.core.embeddings.backend_available", return_value=True),
            patch("techne.core.embeddings._get_embedding_model", return_value=_FakeModel()),
        ):
            protos = {"decision": ["a sentence"], "fact": ["b"], "empty": []}
            out_false = inference._get_prototype_embeddings(protos, key=False)
            # Populated (no UnboundLocalError); empty-sentence label skipped.
            assert set(out_false) == {"decision", "fact"}
            # Same key → cached same object.
            assert inference._get_prototype_embeddings(protos, key=False) is out_false
            # Other slot is independent.
            out_true = inference._get_prototype_embeddings({"fact": ["c"]}, key=True)
            assert out_true is not out_false
            assert set(out_true) == {"fact"}


class TestInferNoFastembed:
    def test_graceful_degradation_on_import_error(self):
        with patch.dict(sys.modules, {"fastembed": None, "numpy": None}):
            _reset_cache()
            result = infer("We decided to use PostgreSQL")
        # Should return default, not raise
        assert isinstance(result, InferenceResult)
        assert result.tags == []
        assert result.kind == "observation-shaped"

    def test_import_failure_cached_no_retry(self):
        """_INFER_DEPS sentinel short-circuits subsequent calls after first ImportError.

        The import is attempted ONCE; on later calls with deps absent the sentinel
        short-circuits without re-attempting the known-failing import (techne#56-14).
        """
        from techne.core import inference as _inf_mod

        with patch.dict(sys.modules, {"fastembed": None, "numpy": None}):
            _reset_cache()
            assert _inf_mod._INFER_DEPS is None  # not yet probed
            infer("first call — triggers the ImportError path")
            assert _inf_mod._INFER_DEPS is False  # cached failure

        # With deps "restored" (context manager exited), but sentinel still False,
        # a second call should short-circuit and return the default InferenceResult
        # WITHOUT attempting the import (so no success even though deps are back).
        result = infer("second call — should hit the sentinel fast-path")
        assert _inf_mod._INFER_DEPS is False  # sentinel unchanged
        assert result.tags == []
        assert result.kind == "observation-shaped"

        # After _reset_cache() the sentinel is cleared so imports are retried.
        _reset_cache()
        assert _inf_mod._INFER_DEPS is None


def _scores_from_map(label_scores: dict):
    """Return a _cosine side_effect fn that maps (content_vec, proto_vec) by
    looking up (content_vec, proto_vec) as a (content_idx, label) pairing.

    Simpler approach: side_effect as a call counter keyed to a label→score map.
    We return a factory that, given a label→score map, produces a cosine mock
    whose return values cycle through the map in dict order. This works because
    infer() iterates proto_embs.items() in insertion order and calls _cosine
    once per label.
    """
    scores = list(label_scores.values())
    call_count = [0]

    def _side(a, b):
        val = scores[call_count[0] % len(scores)]
        call_count[0] += 1
        return val

    return _side


class TestInferTagAndKindSelection:
    """Test tag + kind selection logic by mocking _cosine to return fixed scores.

    This avoids numpy in tests entirely — the production code uses numpy arrays;
    here we intercept at the cosine boundary and inject float scores directly.
    """

    def _stub_infer(self, proto_embs: dict, cosine_scores: dict, **infer_kwargs):
        """Run infer() with stubbed embedding + cosine, return InferenceResult.

        proto_embs: label -> anything (values are only passed to _cosine, which is mocked)
        cosine_scores: label -> float similarity score
        infer_kwargs: passed through to infer(); defaults to tag_threshold=0.5 so
            the existing score-based assertions keep their 0.5 cut. Pass
            ``config=...`` to exercise config-driven threshold resolution (techne#352).
        """
        dummy_fe = MagicMock()
        mock_model = MagicMock()
        mock_model.embed.return_value = iter([MagicMock()])  # content vec placeholder

        scores_list = [cosine_scores.get(lbl, 0.0) for lbl in proto_embs]
        call_count = [0]

        def fake_cosine(a, b):
            val = scores_list[call_count[0] % len(scores_list)]
            call_count[0] += 1
            return val

        # Stub numpy so the `import numpy as np` guard in infer() passes,
        # and np.stack()[0] returns a sentinel used as the content vector.
        content_sentinel = object()
        mock_np = MagicMock()
        mock_np.stack.return_value = [content_sentinel]

        with (
            patch.dict(sys.modules, {"fastembed": dummy_fe, "numpy": mock_np}),
            patch("techne.core.inference._get_prototype_embeddings", return_value=proto_embs),
            patch("techne.core.embeddings._get_embedding_model", return_value=mock_model),
            patch("techne.core.inference._cosine", side_effect=fake_cosine),
        ):
            _reset_cache()
            if not infer_kwargs:
                infer_kwargs = {"tag_threshold": 0.5}
            return infer("dummy content", **infer_kwargs)

    def test_highest_sim_tag_attached(self):
        proto_embs = {
            "decision": object(),
            "fact": object(),
            "kind:decision-shaped": object(),
            "kind:observation-shaped": object(),
        }
        scores = {
            "decision": 0.9,
            "fact": 0.3,
            "kind:decision-shaped": 0.9,
            "kind:observation-shaped": 0.1,
        }
        result = self._stub_infer(proto_embs, scores)
        assert "decision" in result.tags
        assert "fact" not in result.tags  # below threshold of 0.5
        assert result.kind == "decision-shaped"

    def test_threshold_filters_low_scores(self):
        proto_embs = {
            "decision": object(),  # 0.9 — above 0.5
            "fact": object(),  # 0.4 — below 0.5
            "kind:observation-shaped": object(),
        }
        scores = {"decision": 0.9, "fact": 0.4, "kind:observation-shaped": 0.5}
        result = self._stub_infer(proto_embs, scores)
        assert "decision" in result.tags
        assert "fact" not in result.tags

    def test_config_tag_threshold_used_when_not_explicit(self):
        """With no explicit tag_threshold, infer() honours config.tag_threshold
        (techne#352) — a 0.45 score is tagged at config.tag_threshold=0.42 but
        NOT at the old hardcoded 0.6."""
        from techne.config import Config

        proto_embs = {"fact": object(), "kind:observation-shaped": object()}
        scores = {"fact": 0.45, "kind:observation-shaped": 0.1}

        cfg_low = Config()
        cfg_low.tag_threshold = 0.42
        assert "fact" in self._stub_infer(proto_embs, scores, config=cfg_low).tags

        cfg_high = Config()
        cfg_high.tag_threshold = 0.6
        assert "fact" not in self._stub_infer(proto_embs, scores, config=cfg_high).tags

    def test_explicit_threshold_overrides_config(self):
        """An explicit tag_threshold wins over config.tag_threshold."""
        from techne.config import Config

        proto_embs = {"fact": object(), "kind:observation-shaped": object()}
        scores = {"fact": 0.45, "kind:observation-shaped": 0.1}

        cfg = Config()
        cfg.tag_threshold = 0.42  # would tag; explicit 0.6 must veto
        result = self._stub_infer(proto_embs, scores, config=cfg, tag_threshold=0.6)
        assert "fact" not in result.tags

    def test_top_k_limits_tags(self):
        proto_embs = {f"tag{i}": object() for i in range(6)}
        proto_embs["kind:observation-shaped"] = object()
        scores = {lbl: 0.9 for lbl in proto_embs}
        result = self._stub_infer(proto_embs, scores)
        # Default top_k=5; 6 tags all above threshold → at most 5 returned
        assert len(result.tags) <= 5

    def test_empty_proto_cache_returns_default(self):
        dummy_fe = MagicMock()
        mock_np = MagicMock()
        with (
            patch.dict(sys.modules, {"fastembed": dummy_fe, "numpy": mock_np}),
            patch("techne.core.inference._get_prototype_embeddings", return_value={}),
            patch("techne.core.embeddings._get_embedding_model"),
        ):
            _reset_cache()
            result = infer("anything")
        assert result.tags == []
        assert result.kind == "observation-shaped"

    def test_kind_scores_populated(self):
        proto_embs = {
            "fact": object(),
            "kind:fact-shaped": object(),
            "kind:decision-shaped": object(),
        }
        scores = {"fact": 0.8, "kind:fact-shaped": 0.85, "kind:decision-shaped": 0.4}
        result = self._stub_infer(proto_embs, scores)
        assert result.kind == "fact-shaped"
        assert "fact-shaped" in result.kind_scores
        assert "decision-shaped" in result.kind_scores


class TestObservationInference:
    """Integration: add_observation writes tags + kind columns."""

    def test_observation_stores_tags_and_kind(self, db_path, config):
        """With inference stubbed out, tags + kind are persisted."""
        session_id = start_session(db_path)

        # Patch at the source module — add_observation imports infer lazily inside body
        with patch("techne.core.inference.infer") as mock_infer:
            mock_infer.return_value = InferenceResult(
                tags=["decision", "architecture"],
                kind="decision-shaped",
            )
            add_observation(db_path, session_id, "Bash", "content", config=config)

        obs = get_observations(db_path, session_id)
        assert len(obs) == 1
        assert obs[0]["tags"] == "decision architecture"
        assert obs[0]["kind"] == "decision-shaped"

    def test_observation_no_tags_stores_null(self, db_path, config):
        """Empty tag list → NULL tags column, not empty string."""
        session_id = start_session(db_path)

        with patch("techne.core.inference.infer") as mock_infer:
            mock_infer.return_value = InferenceResult(tags=[], kind="observation-shaped")
            add_observation(db_path, session_id, "Bash", "content", config=config)

        obs = get_observations(db_path, session_id)
        assert obs[0]["tags"] is None
        assert obs[0]["kind"] == "observation-shaped"

    def test_add_observation_backwards_compat_no_config(self, db_path):
        """add_observation without config= arg still works (inference degrades)."""
        session_id = start_session(db_path)

        with patch("techne.core.inference.infer") as mock_infer:
            mock_infer.return_value = InferenceResult()
            add_observation(db_path, session_id, "Bash", "content")

        obs = get_observations(db_path, session_id)
        assert len(obs) == 1

    def test_schema_migration_adds_columns(self, tmp_path):
        """A v6 database gets tags + kind columns after migration."""
        import sqlite3

        db = tmp_path / "old.db"
        from techne.core.database import SCHEMA_SQL

        # Build a full-schema DB (via SCHEMA_SQL) then downgrade to v6
        # by recreating observations without tags/kind.
        conn = sqlite3.connect(str(db))
        conn.executescript(SCHEMA_SQL)
        conn.executescript("""
            DELETE FROM schema_version WHERE version >= 7;
            INSERT OR IGNORE INTO schema_version (version) VALUES (6);
            CREATE TABLE observations_old AS SELECT
                id, session_id, thread_id, timestamp, tool_name, content, tier
            FROM observations;
            DROP TABLE observations;
            CREATE TABLE observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                thread_id TEXT,
                timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                tool_name TEXT,
                content TEXT NOT NULL,
                tier TEXT NOT NULL DEFAULT 'hot'
            );
            INSERT INTO observations SELECT * FROM observations_old;
            DROP TABLE observations_old;
        """)
        conn.commit()
        conn.close()

        # Open via techne — migration should add tags + kind
        from techne.core.database import connect as techne_connect

        conn2 = techne_connect(db)
        cols = {row[1] for row in conn2.execute("PRAGMA table_info(observations)")}
        conn2.close()
        assert "tags" in cols
        assert "kind" in cols


class TestBackfillTags:
    """inference.backfill_tags — retroactively fill tags/kind on stored rows
    (techne#352: obs stored before the [vectors] backend was installed have
    tags=NULL)."""

    def _seed_untagged(self, db_path, n=3):
        from techne.core.database import connect

        sid = start_session(db_path, session_id="s-backfill")
        conn = connect(db_path)
        try:
            for i in range(n):
                conn.execute(
                    "INSERT INTO observations"
                    "(session_id, tool_name, content, tier, tags, kind, event_at) "
                    "VALUES (?, 'Bash', ?, 'hot', NULL, NULL, '2026-07-15T00:00:00Z')",
                    (sid, f"content number {i}"),
                )
            conn.commit()
        finally:
            conn.close()
        return sid

    def test_backfills_missing_tags(self, db_path, config, monkeypatch):
        from techne.core import inference
        from techne.core.inference import InferenceResult, backfill_tags

        sid = self._seed_untagged(db_path, 3)
        monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: True)
        monkeypatch.setattr(
            inference,
            "infer",
            lambda content, cfg=None, **kw: InferenceResult(
                tags=["alpha", "beta"], kind="decision-shaped"
            ),
        )
        counts = backfill_tags(db_path, config)
        assert counts == {"scanned": 3, "tagged": 3}
        rows = get_observations(db_path, sid)
        assert all(r["tags"] == "alpha beta" for r in rows)
        assert all(r["kind"] == "decision-shaped" for r in rows)

    def test_only_missing_skips_already_tagged(self, db_path, config, monkeypatch):
        from techne.core import inference
        from techne.core.database import connect
        from techne.core.inference import InferenceResult, backfill_tags

        sid = self._seed_untagged(db_path, 2)
        conn = connect(db_path)
        conn.execute(
            "UPDATE observations SET tags='preexisting' "
            "WHERE id=(SELECT MIN(id) FROM observations)"
        )
        conn.commit()
        conn.close()

        def _stub_new(content, cfg=None, **kw):
            return InferenceResult(tags=["new"], kind="observation-shaped")

        monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: True)
        monkeypatch.setattr(inference, "infer", _stub_new)
        counts = backfill_tags(db_path, config, only_missing=True)
        assert counts["scanned"] == 1  # only the untagged row was touched
        rows = get_observations(db_path, sid)
        assert sorted(r["tags"] for r in rows) == ["new", "preexisting"]

    def test_noop_when_backend_unavailable(self, db_path, config, monkeypatch):
        from techne.core.inference import backfill_tags

        sid = self._seed_untagged(db_path, 2)
        monkeypatch.setattr("techne.core.embeddings.backend_available", lambda: False)
        counts = backfill_tags(db_path, config)
        assert counts == {"scanned": 0, "tagged": 0}
        rows = get_observations(db_path, sid)
        assert all(r["tags"] is None for r in rows)  # untouched


def test_default_threshold_matches_config_default():
    """infer()'s no-config fallback (_DEFAULT_TAG_THRESHOLD) must not drift from
    Config.tag_threshold — the two 0.42s are duplicated by design, so pin them
    (Prudence, PR #367; techne#352)."""
    from techne.config import Config
    from techne.core.inference import _DEFAULT_TAG_THRESHOLD

    assert _DEFAULT_TAG_THRESHOLD == Config().tag_threshold
