"""Tests for MMR diversity in the search reranker (issue #41)."""

import sys
from contextlib import contextmanager
from types import ModuleType
from unittest.mock import MagicMock, patch

from techne.core.search import _mmr_reorder, _rerank

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(id: str, combined: float, content: str) -> dict:
    """Build a minimal candidate dict with a pre-computed _combined score."""
    return {
        "type": "knowledge",
        "id": id,
        "rank": -1.0,  # placeholder — _rerank normalises this
        "content": content,
        "updated_at": None,
        "_combined": combined,
    }


def _make_numpy_stub():
    """Build a minimal numpy stub sufficient for _mmr_reorder / _cosine_similarity.

    Uses real float arithmetic on Python lists so no C extension is needed.
    """
    import math

    np_stub = ModuleType("numpy")

    class _Array(list):
        """List subclass that satisfies the small numpy API surface used here."""

        dtype = "float32"

        def __init__(self, data, dtype=None):
            super().__init__(float(x) for x in data)

    def array(data, dtype=None):
        return _Array(data)

    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))

    class _LinAlg:
        @staticmethod
        def norm(v):
            return math.sqrt(sum(x * x for x in v))

    np_stub.array = array
    np_stub.dot = dot  # must be a module-level attribute
    np_stub.linalg = _LinAlg()

    return np_stub


# ---------------------------------------------------------------------------
# Context manager: swap numpy in sys.modules for the duration of a test
# ---------------------------------------------------------------------------


@contextmanager
def _numpy_stub_ctx():
    """Replace numpy in sys.modules with the stub for the duration of the with-block."""
    stub = _make_numpy_stub()
    original = sys.modules.get("numpy", None)
    sys.modules["numpy"] = stub
    try:
        yield stub
    finally:
        if original is None:
            sys.modules.pop("numpy", None)
        else:
            sys.modules["numpy"] = original


def _mock_model_with_vecs(vecs):
    """Return a mock fastembed model that yields vecs (as _Array objects)."""
    stub = _make_numpy_stub()
    model = MagicMock()
    model.embed.return_value = iter(stub.array(v) for v in vecs)
    return model


# ---------------------------------------------------------------------------
# _mmr_reorder unit tests
# ---------------------------------------------------------------------------


class TestMmrReorder:
    def test_lambda_zero_returns_relevance_order(self):
        """λ=0: pure relevance — MMR short-circuits before embedding."""
        rows = [
            _row("high", 0.9, "aaa"),
            _row("mid", 0.5, "bbb"),
            _row("low", 0.1, "ccc"),
        ]
        # λ=0 exits before any embedding call — no numpy needed.
        result = _mmr_reorder(rows, limit=3, mmr_lambda=0.0)
        ids = [r["id"] for r in result]
        assert ids == ["high", "mid", "low"]

    def test_similar_duplicate_pushed_down(self):
        """λ=1: near-duplicate second candidate is demoted in favour of diverse one."""
        # A and B are very similar (cos~0.99); C is orthogonal to A.
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [0.99, 0.141, 0.0]  # very similar to A
        vec_c = [0.0, 0.0, 1.0]  # orthogonal to A

        rows = [
            _row("A", 0.9, "topic about X"),
            _row("B", 0.8, "also about X"),
            _row("C", 0.7, "completely different"),
        ]
        with _numpy_stub_ctx():
            model = MagicMock()
            stub = _make_numpy_stub()
            model.embed.return_value = iter([stub.array(v) for v in [vec_a, vec_b, vec_c]])
            with patch("techne.core.search._get_embedding_model", return_value=model):
                result = _mmr_reorder(rows, limit=2, mmr_lambda=1.0)

        ids = [r["id"] for r in result]
        assert ids[0] == "A"
        assert ids[1] == "C"  # C is diverse; B is near-clone

    def test_unrelated_candidates_order_unchanged(self):
        """Orthogonal candidates produce zero penalty — relevance order is preserved."""
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [0.0, 1.0, 0.0]
        vec_c = [0.0, 0.0, 1.0]

        rows = [
            _row("A", 0.9, "alpha topic"),
            _row("B", 0.6, "beta topic"),
            _row("C", 0.3, "gamma topic"),
        ]
        with _numpy_stub_ctx():
            stub = _make_numpy_stub()
            model = MagicMock()
            model.embed.return_value = iter([stub.array(v) for v in [vec_a, vec_b, vec_c]])
            with patch("techne.core.search._get_embedding_model", return_value=model):
                result = _mmr_reorder(rows, limit=3, mmr_lambda=0.5)

        ids = [r["id"] for r in result]
        assert ids == ["A", "B", "C"]

    def test_lambda_one_maximises_diversity(self):
        """λ=1: second pick is the most-distant from first (antipodal), not second-most-relevant."""
        vec_a = [1.0, 0.0]
        vec_b = [0.9, 0.44]  # close to A
        vec_c = [-1.0, 0.0]  # antipodal to A — maximum diversity

        rows = [
            _row("A", 0.9, "a"),
            _row("B", 0.8, "b"),
            _row("C", 0.7, "c"),
        ]
        with _numpy_stub_ctx():
            stub = _make_numpy_stub()
            model = MagicMock()
            model.embed.return_value = iter([stub.array(v) for v in [vec_a, vec_b, vec_c]])
            with patch("techne.core.search._get_embedding_model", return_value=model):
                result = _mmr_reorder(rows, limit=2, mmr_lambda=1.0)

        ids = [r["id"] for r in result]
        assert ids[0] == "A"
        assert ids[1] == "C"

    def test_fastembed_absent_is_noop(self):
        """When fastembed/model is unavailable, MMR silently returns relevance order."""
        rows = [
            _row("high", 0.9, "text1"),
            _row("low", 0.1, "text2"),
        ]
        with patch(
            "techne.core.search._get_embedding_model",
            side_effect=ImportError("no fastembed"),
        ):
            result = _mmr_reorder(rows, limit=2, mmr_lambda=0.5)

        ids = [r["id"] for r in result]
        assert ids == ["high", "low"]

    def test_scratch_keys_cleaned(self):
        """_mmr_reorder must not leak _emb onto returned rows."""
        vec = [1.0, 0.0]
        rows = [_row("x", 0.5, "content")]
        with _numpy_stub_ctx():
            stub = _make_numpy_stub()
            model = MagicMock()
            model.embed.return_value = iter([stub.array(vec)])
            with patch("techne.core.search._get_embedding_model", return_value=model):
                result = _mmr_reorder(rows, limit=1, mmr_lambda=0.3)

        assert "_emb" not in result[0]


# ---------------------------------------------------------------------------
# _rerank integration: mmr_lambda param plumbed correctly
# ---------------------------------------------------------------------------


class TestRerankMmrParam:
    def _make_row(self, rank: float, content: str = "text", ts=None) -> dict:
        return {
            "type": "knowledge",
            "id": f"k{rank}",
            "rank": rank,
            "content": content,
            "updated_at": ts,
        }

    def test_lambda_zero_same_as_no_mmr(self):
        """_rerank with mmr_lambda=0 must equal legacy relevance-only order."""
        rows_a = [self._make_row(float(-i), f"text{i}") for i in range(1, 6)]
        rows_b = [self._make_row(float(-i), f"text{i}") for i in range(1, 6)]

        result_a = _rerank(rows_a, limit=3, recency_weight=0.0, bm25_weight=1.0, mmr_lambda=0.0)
        result_b = _rerank(rows_b, limit=3, recency_weight=0.0, bm25_weight=1.0)

        assert [r["id"] for r in result_a] == [r["id"] for r in result_b]

    def test_mmr_lambda_accepted_without_error(self):
        """Passing mmr_lambda to _rerank does not raise even without fastembed."""
        rows = [self._make_row(float(-i)) for i in range(1, 4)]
        with patch(
            "techne.core.search._get_embedding_model",
            side_effect=ImportError("no fastembed"),
        ):
            result = _rerank(rows, limit=2, recency_weight=0.3, bm25_weight=0.7, mmr_lambda=0.5)
        assert len(result) == 2
