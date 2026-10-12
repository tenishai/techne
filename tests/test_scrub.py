"""techne#1245: re-redacting rows stored before the pattern set widened."""

import hashlib
import json

from click.testing import CliRunner

from techne.core.database import connect, init_db
from techne.core.scrub import scrub

SECRET = "hunter2hunter2x"


def _db(tmp_path):
    p = tmp_path / "t.db"
    init_db(p).close()
    conn = connect(p)
    conn.execute("INSERT INTO sessions (id) VALUES ('s1')")
    # Raw INSERTs bypass write-time redaction, standing in for rows stored
    # before the patterns caught these shapes.
    conn.execute(
        "INSERT INTO observations (session_id, tool_name, content) VALUES (?, 'Bash', ?)",
        ("s1", f"Ran: export DB_PASSWORD={SECRET}"),
    )
    conn.execute(
        "INSERT INTO observations (session_id, tool_name, content) VALUES (?, 'Bash', ?)",
        ("s1", "Ran: ls"),
    )
    conn.execute(
        "INSERT INTO checkpoints (session_id, trigger, data_json) VALUES (?, 'auto', ?)",
        ("s1", json.dumps({"recent_commands": [f"curl -u admin:{SECRET} x"], "n": 3})),
    )
    conn.commit()
    conn.close()
    return p


def test_dry_run_counts_and_changes_nothing(tmp_path):
    p = _db(tmp_path)
    counts = scrub(p)
    assert counts["observations.content"] == 1
    assert counts["checkpoints.data_json"] == 1
    assert counts["knowledge.content"] == 0  # scanned, clean
    conn = connect(p)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM observations WHERE content LIKE ?", (f"%{SECRET}%",)
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_apply_redacts_keeps_json_parseable_and_hashes_the_raw_text(tmp_path):
    p = _db(tmp_path)
    scrub(p, apply=True)
    conn = connect(p)
    rows = conn.execute("SELECT content, content_hash FROM observations ORDER BY id").fetchall()
    assert SECRET not in rows[0][0] and "<redacted:" in rows[0][0]
    raw = f"Ran: export DB_PASSWORD={SECRET}"
    assert rows[0][1] == hashlib.sha256(raw.encode()).hexdigest()
    assert tuple(rows[1]) == ("Ran: ls", None)  # untouched row keeps a NULL hash
    data = json.loads(conn.execute("SELECT data_json FROM checkpoints").fetchone()[0])
    assert SECRET not in data["recent_commands"][0] and data["n"] == 3
    # FTS follows the UPDATE trigger: the secret is no longer searchable
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM observations_fts WHERE observations_fts MATCH ?", (SECRET,)
        ).fetchone()[0]
        == 0
    )
    conn.close()
    assert sum(scrub(p).values()) == 0  # idempotent


def test_cli_dry_run_by_default_then_apply(tmp_path):
    from techne.cli import main
    from techne.config import Config

    cfg = Config()
    cfg.db_path = _db(tmp_path)
    cfg.data_dir = tmp_path
    res = CliRunner().invoke(main, ["scrub-credentials", "--json"], obj={"config": cfg})
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["applied"] is False and out["total"] == 2
    assert SECRET not in res.output  # counts only, never the matched text

    res = CliRunner().invoke(main, ["scrub-credentials", "--apply"], obj={"config": cfg})
    assert res.exit_code == 0 and "redacted 2 row(s)" in res.output
    assert sum(scrub(cfg.db_path).values()) == 0
