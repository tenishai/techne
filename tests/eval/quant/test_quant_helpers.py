"""Unit tests for the issue #1341 quantization scripts. Not eval-marked: they check the
sign test, int8 rounding and a per-format KNN round trip, not run_table's bookkeeping.
The file skips without numpy (the vectors extra), and the round trip without
sqlite-vec."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).parent))
from longmemeval_quant import sign_test  # noqa: E402
from neighbors import FORMATS, _to_int8, build, query  # noqa: E402


def test_sign_test_is_two_sided_exact() -> None:
    assert sign_test(0, 0) == 1.0
    assert sign_test(3, 3) == 1.0
    # 0 of 2: P(X <= 0) = 1/4, doubled.
    assert sign_test(0, 2) == pytest.approx(0.5)
    assert sign_test(2, 0) == pytest.approx(0.5)
    # 19 vs 59, as in the binary row: far below 0.001.
    assert sign_test(19, 59) < 1e-3


def test_to_int8_rounds_and_clips() -> None:
    # 0.006 * 254 = 1.524 rounds to 2, where floor or truncation would give 1.
    v = np.array([[0.5, -0.5, 0.006, 0.9]], dtype=np.float32)
    assert _to_int8(v, 254.0).tolist() == [[127, -127, 2, 127]]


def test_every_format_finds_the_exact_neighbor(tmp_path: Path) -> None:
    import sqlite3

    from techne.core.search import _ensure_sqlite_vec

    if not _ensure_sqlite_vec(sqlite3.connect(":memory:")):
        pytest.skip("sqlite-vec does not load here")
    rng = np.random.default_rng(0)
    vecs = rng.normal(size=(50, 256)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    ids = np.arange(1, 51, dtype=np.int64)
    scale = 127.0 / float(np.abs(vecs).max())
    for fmt in FORMATS:
        conn = build(tmp_path / f"{fmt}.db", fmt, ids, vecs, scale)
        hits = query(conn, fmt, vecs[6], scale, k=5)
        conn.close()
        assert hits[0][0] == 7, fmt
