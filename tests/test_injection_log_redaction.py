"""context_injections.trigger_snippet is redacted before it is stored (techne#1238)."""

import sqlite3

from techne.hooks._injection_log import log_context_injection

SECRET = "sk-ant-api03-" + "A" * 40


def _snippets(db):
    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute("SELECT trigger_snippet FROM context_injections")]
    finally:
        conn.close()


def _log(db, trigger):
    log_context_injection(
        db, source="per_turn_recall", session_id="s1", trigger=trigger, rows=[], injected_text=""
    )


def test_a_secret_in_the_trigger_is_not_stored(tmp_path):
    db = tmp_path / "t.db"
    _log(db, f"deploy with {SECRET} please")
    [snippet] = _snippets(db)
    assert SECRET not in snippet
    assert "sk-ant-api03-AAAA" not in snippet
    assert snippet.startswith("deploy with ")


def test_a_secret_straddling_the_200_char_cut_leaves_no_fragment(tmp_path):
    # Cut first and the fragment left at the 200-character mark ("sk-ant-api") no
    # longer matches the pattern, so it would be stored as-is.
    db = tmp_path / "t.db"
    _log(db, "x" * 190 + SECRET)
    [snippet] = _snippets(db)
    assert len(snippet) <= 200
    assert "sk-ant-a" not in snippet  # the old cut kept "sk-ant-api"


def test_a_missing_or_empty_trigger_stays_null(tmp_path):
    db = tmp_path / "t.db"
    _log(db, None)
    _log(db, "")
    assert _snippets(db) == [None, None]
