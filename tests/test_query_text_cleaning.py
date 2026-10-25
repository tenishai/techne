"""A NUL byte or a lone surrogate in query text must not raise out of any read
path (techne#1249).

Before the fix, ``search(db, "hel\\x00lo world")`` raised ``OperationalError:
unterminated string`` (SQLite reads the NUL as the end of the FTS5 phrase
literal) and ``search(db, "hello\\ud800")`` raised ``UnicodeEncodeError`` (the
surrogate cannot be bound as UTF-8). Every caller below reaches one of those.
"""

from __future__ import annotations

import io
import json
import sqlite3

import pytest

from techne.core.search import (
    _sanitize_fts_query,
    build_or_fts_query,
    clean_query_text,
    has_direct_match,
    prepare_fts_query,
    search,
    search_fts,
    search_vec,
)

NUL = "hel\x00lo world"
LONE = "hello\ud800"
MIXED = "deploy\x00 \x1b[31mhello\ud800 world\x7f"
HOSTILE = [NUL, LONE, MIXED]


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    monkeypatch.setenv("TECHNE_HOOK_TIMEOUT", "0")
    from techne.config import Config
    from techne.core.database import init_db
    from techne.core.knowledge import store

    config = Config.load()
    init_db(config.db_path).close()
    store(config.db_path, "hello world pipelines deploy notes", type="fact")
    return config


# ---- the cleaner itself ------------------------------------------------------


def test_clean_maps_controls_to_spaces():
    assert clean_query_text("a\x00b\x1fc\x7fd") == "a b c d"


def test_clean_keeps_tab_and_line_breaks():
    assert clean_query_text("a\tb\nc\r\nd") == "a\tb\nc\r\nd"


def test_clean_replaces_lone_surrogates_and_joins_pairs():
    assert clean_query_text("x\ud800y\udfffz") == "x�y�z"
    # A high/low pair held as two code points is one real character.
    assert clean_query_text("smile 😀") == "smile \U0001f600"


def test_clean_leaves_ordinary_text_alone():
    s = 'naïve "quoted" text — with 日本語 and \U0001f600'
    assert clean_query_text(s) == s


@pytest.mark.parametrize("q", HOSTILE)
def test_fts_builders_output_is_encodable_and_nul_free(q):
    assert prepare_fts_query(q)
    # The OR form is "" for a single-term query (LONE), which is its contract.
    for built in (_sanitize_fts_query(q), prepare_fts_query(q), build_or_fts_query(q)):
        assert "\x00" not in built
        built.encode("utf-8")  # raises on a surrogate


def test_nul_separates_terms_rather_than_gluing_them():
    assert _sanitize_fts_query(NUL) == '"hel" "lo" "world"'


# ---- every caller path --------------------------------------------------------


@pytest.mark.parametrize("q", HOSTILE)
def test_search_fts_only(home, q):
    rows = search(home.db_path, q, use_vectors=False)
    assert any("hello world" in r["content"] for r in rows) or q == NUL


@pytest.mark.parametrize("q", HOSTILE)
def test_search_hybrid(home, q):
    assert isinstance(search(home.db_path, q), list)


@pytest.mark.parametrize("q", HOSTILE)
def test_search_fts_direct(home, q):
    assert isinstance(search_fts(home.db_path, q), list)


@pytest.mark.parametrize("q", HOSTILE)
def test_search_vec_direct(home, q):
    assert isinstance(search_vec(home.db_path, q), list)


# AND semantics: the NUL splits "hello" into "hel lo", and the escape glues
# "31m" onto "hello", so only LONE names terms the stored row has.
@pytest.mark.parametrize("q,expected", [(NUL, False), (LONE, True), (MIXED, False)])
def test_has_direct_match(home, q, expected):
    assert has_direct_match(home.db_path, q, table="knowledge") is expected


@pytest.mark.parametrize("q", HOSTILE)
def test_techne_search_tool(home, q):
    from techne import mcp_server

    out = mcp_server._techne_search({"query": q}, home)
    assert out["count"] >= 1


@pytest.mark.parametrize("q", HOSTILE)
def test_techne_recall_tool(home, q):
    from techne import mcp_server

    out = mcp_server._techne_recall({"topic": q}, home)
    assert out["search_results"]


@pytest.mark.parametrize("q", HOSTILE)
def test_cli_search(home, q):
    from click.testing import CliRunner

    from techne.cli import main

    r = CliRunner().invoke(main, ["search", q])
    assert r.exit_code == 0, r.output
    assert r.exception is None
    assert "hello world pipelines" in r.output


@pytest.mark.parametrize("q", HOSTILE)
def test_per_turn_recall_hook_injects_and_logs(home, monkeypatch, capsys, q):
    """The hook used to swallow the exception as `degraded`, so the turn got no
    recall. The logged trigger is the cleaned text: a raw surrogate there made
    the best-effort log drop the row silently."""
    from techne.hooks import user_prompt_submit

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": q, "session_id": "s"})))
    with pytest.raises(SystemExit) as exc:
        user_prompt_submit.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "per-turn recall degraded" not in captured.err
    ctx = json.loads(captured.out)["hookSpecificOutput"]["additionalContext"]
    assert "hello world pipelines" in ctx

    conn = sqlite3.connect(home.db_path)
    try:
        triggers = [
            r[0]
            for r in conn.execute(
                "SELECT trigger_snippet FROM context_injections WHERE source='per_turn_recall'"
            )
        ]
    finally:
        conn.close()
    assert triggers == [clean_query_text(q)]
