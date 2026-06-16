"""Persistence helpers for heuristic thread detection.

save_boundaries, load_boundaries, boundary_indices_from_segments, bucket_hash.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from techne.core.threads.scoring import BoundaryScore


def bucket_hash(session_id: str, first_obs_id: int, boundary_signal: str) -> str:
    """Deterministic thread ID from (session_id, first_obs_id, boundary_signal).

    Returns a ``t-<16-char-hex>`` string — the ``t-`` prefix makes it
    visually distinct from legacy UUID-format thread IDs.

    Same inputs always produce the same ID.  Different ``boundary_signal``
    values produce different IDs, so replay is idempotent and two threads in
    the same session can't collide even when ``first_obs_id`` is equal.
    """
    key = f"{session_id}\x00{first_obs_id}\x00{boundary_signal}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return f"t-{digest}"


def boundary_indices_from_segments(segments: list[list[int]]) -> set[int]:
    """Map final segmentation back to the set of pair-A indices that count
    as actual thread boundaries (i.e. the last index of each non-final segment).

    Boundary scores whose ``a_index`` is in this set survived the merge pass.
    Scores whose ``a_index`` is *not* in the set either never crossed
    threshold OR were collapsed by a merge rule.
    """
    if not segments:
        return set()
    boundaries: set[int] = set()
    for seg in segments[:-1]:
        if seg:
            boundaries.add(seg[-1])
    return boundaries


def save_boundaries(
    conn: sqlite3.Connection,
    session_id: str,
    scores: list[BoundaryScore],
    boundary_a_indices: set[int],
    *,
    commit: bool = True,
) -> None:
    """Persist every per-pair score plus its final boundary state.

    ``boundary_a_indices`` is the set of ``a_index`` values that became
    real thread boundaries — typically from
    ``boundary_indices_from_segments(segments)``.
    """
    if not scores:
        return
    rows = [
        (
            session_id,
            s.a_index,
            s.b_index,
            s.gap_seconds,
            s.family_a,
            s.family_b,
            json.dumps(s.signals, sort_keys=True),
            s.total,
            1 if s.a_index in boundary_a_indices else 0,
        )
        for s in scores
    ]
    conn.executemany(
        """INSERT INTO thread_boundaries
              (session_id, obs_index_a, obs_index_b, gap_seconds,
               family_a, family_b, signals_json, total_score, is_boundary)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    if commit:
        conn.commit()


def load_boundaries(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """Load persisted boundary rows for a session, ordered by obs_index_a.

    Each row is returned as a dict with ``signals`` parsed back from JSON.
    Convenience for the audit CLI.
    """
    rows = conn.execute(
        """SELECT obs_index_a, obs_index_b, gap_seconds, family_a, family_b,
                  signals_json, total_score, is_boundary
             FROM thread_boundaries
            WHERE session_id = ?
            ORDER BY obs_index_a""",
        (session_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "obs_index_a": r[0],
                "obs_index_b": r[1],
                "gap_seconds": r[2],
                "family_a": r[3],
                "family_b": r[4],
                "signals": json.loads(r[5]),
                "total_score": r[6],
                "is_boundary": bool(r[7]),
            }
        )
    return out
