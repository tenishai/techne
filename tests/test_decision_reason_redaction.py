"""techne#1245: decision_reason is redacted at every write site.

conflict.py already redacted it; the two sleepdream inserts and reflect's
promote and discard stored it raw (found by Temperance on PR 1272).
"""

from techne.core import sleepdream
from techne.core.database import connect, init_db
from techne.core.reflect import discard_candidate, promote_candidate

SECRET = "hunter2hunter2x"
LEAK = f"operator pasted DB_PASSWORD={SECRET}"


def _db(tmp_path):
    p = tmp_path / "t.db"
    init_db(p).close()
    conn = connect(p)
    conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id) VALUES (?)", (sleepdream.SYSTEM_SLEEP_SESSION,)
    )
    conn.commit()
    conn.close()
    return p


def _candidate(p):
    conn = connect(p)
    cid = conn.execute(
        "INSERT INTO knowledge_candidates (scope, type, content, source_session_id,"
        " model_used, prompt_hash) VALUES ('knowledge', 'fact', 'clean', 's1', 'm', 'h')"
        " RETURNING id"
    ).fetchone()[0]
    conn.commit()
    conn.close()
    return cid


def _reasons(p):
    conn = connect(p)
    rows = [r[0] for r in conn.execute("SELECT decision_reason FROM knowledge_candidates")]
    conn.close()
    return rows


def test_promote_redacts_decision_reason(tmp_path):
    p = _db(tmp_path)
    promote_candidate(p, _candidate(p), decision_reason=LEAK)
    (reason,) = _reasons(p)
    assert SECRET not in reason and "<redacted:" in reason


def test_discard_redacts_decision_reason(tmp_path):
    p = _db(tmp_path)
    discard_candidate(p, _candidate(p), decision_reason=LEAK)
    (reason,) = _reasons(p)
    assert SECRET not in reason and "<redacted:" in reason


def test_sleepdream_inserts_redact_decision_reason(tmp_path):
    p = _db(tmp_path)
    conn = connect(p)
    common = dict(
        content="merged",
        ktype="fact",
        confidence="medium",
        status="pending",
        decided_by="test",
        decision_reason=LEAK,
        raw=None,
        model_used="m",
    )
    sleepdream._insert_consolidation_candidate(conn, target_ids=[1, 2], cluster_hash="c", **common)
    sleepdream._insert_dream_candidate(conn, group_ids=[3, 4], group_hash="g", **common)
    conn.commit()
    conn.close()
    reasons = _reasons(p)
    assert len(reasons) == 2
    assert all(SECRET not in r and "<redacted:" in r for r in reasons)
