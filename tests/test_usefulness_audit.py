"""scripts/usefulness_audit.py — the correction audit (techne#995)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "usefulness_audit.py"
_spec = importlib.util.spec_from_file_location("usefulness_audit", _PATH)
assert _spec and _spec.loader
ua = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ua  # dataclasses resolve the module by name
_spec.loader.exec_module(ua)


def test_rare_terms_prefers_low_df_and_drops_numbers_and_stopwords():
    df = Counter({"common": 900, "techne": 800, "zymurgy": 1, "overflow": 5})
    terms = ua.rare_terms("techne common zymurgy overflow 2026 that", df, k=2)
    assert terms == ["zymurgy", "overflow"]


@pytest.mark.parametrize(
    "prompt, hit",
    [
        ("no, use claude update", True),
        ("that's not what I asked", True),
        ("I said the 9B, not the 27B", True),
        ("why did you merge that?", True),
        ("build 1 and 2", False),
        ("nothing else needed", False),
    ],
)
def test_correction_regex(prompt, hit):
    assert bool(ua._CORRECTION.search(prompt)) is hit


def _write_transcript(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _u(ts, content, **kw):
    return {"type": "user", "timestamp": ts, "message": {"content": content}, **kw}


def _a(ts, blocks, **kw):
    return {"type": "assistant", "timestamp": ts, "message": {"content": blocks}, **kw}


def test_load_turns_groups_replies_and_skips_tool_results_and_sidechains(tmp_path):
    f = tmp_path / "p" / "s1.jsonl"
    _write_transcript(
        f,
        [
            _u("2026-09-21T10:00:00Z", "first prompt"),
            _a("2026-09-21T10:00:01Z", [{"type": "text", "text": "reply one"}]),
            _u("2026-09-21T10:00:02Z", [{"type": "tool_result", "content": "zzz"}]),
            _a("2026-09-21T10:00:03Z", [{"type": "tool_use", "input": {"cmd": "ls"}}]),
            _a("2026-09-21T10:00:04Z", [{"type": "text", "text": "SIDE"}], isSidechain=True),
            _u("2026-09-21T10:05:00Z", "<command-name>/clear</command-name>"),
            _u("2026-09-21T10:06:00Z", "second prompt"),
        ],
    )
    turns = ua.load_turns(f)
    assert [t.prompt for t in turns] == ["first prompt", "second prompt"]
    assert "reply one" in turns[0].reply_text and '"cmd": "ls"' in turns[0].reply_text
    assert "SIDE" not in turns[0].reply_text and "zzz" not in turns[0].reply_text


def _make_db(path: Path, rows, injections):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE knowledge "
        "(id INTEGER PRIMARY KEY, content TEXT, source TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE knowledge_fts "
        "USING fts5(content, content=knowledge, content_rowid=id)"
    )
    conn.execute(
        "CREATE TABLE context_injections (id INTEGER PRIMARY KEY, source TEXT, session_id TEXT, "
        "timestamp TEXT, hit_ids TEXT)"
    )
    conn.executemany("INSERT INTO knowledge VALUES (?,?,?,?)", rows)
    conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES ('rebuild')")
    conn.executemany(
        "INSERT INTO context_injections (source, session_id, timestamp, hit_ids) VALUES (?,?,?,?)",
        injections,
    )
    conn.commit()
    conn.close()


def test_corrections_emit_only_offers_rows_written_before_and_flags_injection(tmp_path):
    db = tmp_path / "t.db"
    _make_db(
        db,
        rows=[
            (
                1,
                "dev04 update with claude update never npm install",
                "agent",
                "2026-09-01 00:00:00",
            ),
            (2, "claude update later knowledge npm install", "reflect:null", "2026-09-30 00:00:00"),
        ],
        injections=[("session_start", "s1", "2026-09-21 09:59:00", "[1]")],
    )
    _write_transcript(
        tmp_path / "p" / "proj" / "s1.jsonl",
        [
            _u("2026-09-21T10:00:00Z", "update claude code"),
            _a(
                "2026-09-21T10:00:05Z",
                [{"type": "text", "text": "Running npm install -g for claude"}],
            ),
            _u("2026-09-21T10:01:00Z", "no, use claude update not npm install"),
        ],
    )
    out = tmp_path / "c.jsonl"
    assert ua.run_corrections_emit(db, tmp_path / "p", out) == 1
    rec = json.loads(out.read_text())
    assert [c["id"] for c in rec["candidates"]] == [1]  # row 2 postdates the correction
    assert rec["candidates"][0]["injected_before"] is True


def test_summarize_corrections_splits_failure_kinds():
    emitted = [
        {"key": "a", "candidates": [{"id": 1, "origin": "hand", "injected_before": True}]},
        {"key": "b", "candidates": [{"id": 2, "origin": "reflect", "injected_before": False}]},
        {"key": "c", "candidates": []},
        {"key": "d", "candidates": []},
        {"key": "e", "candidates": []},
    ]
    verdicts = {
        "a": {"verdict": "covered", "answer_id": 1},
        "b": {"verdict": "covered", "answer_id": 2},
        "c": {"verdict": "not_covered"},
        "d": {"verdict": "not_a_correction"},
    }
    s = ua.summarize_corrections(emitted, verdicts)
    assert s["counts"] == {
        "corrections": 3,
        "use_failure": 1,
        "surfacing_failure": 1,
        "coverage_failure": 1,
        "not_a_correction": 1,
        "unjudged": 1,
    }
    assert s["answering_row_origin"] == {"hand": 1, "reflect": 1}


def test_corrections_emit_keeps_earliest_copy_of_a_replayed_prompt(tmp_path):
    db = tmp_path / "t.db"
    _make_db(
        db,
        rows=[(1, "claude update never npm install", "agent", "2026-09-01 00:00:00")],
        injections=[],
    )
    original = [
        _u("2026-09-21T10:00:00Z", "update claude code"),
        _a("2026-09-21T10:00:05Z", [{"type": "text", "text": "Running npm install -g for claude"}]),
        _u("2026-09-21T10:01:00Z", "no, use claude update not npm install"),
    ]
    replay = [dict(r, timestamp=r["timestamp"].replace("09-21", "09-25")) for r in original]
    _write_transcript(tmp_path / "p" / "proj" / "s1.jsonl", original)
    _write_transcript(tmp_path / "p" / "proj" / "s2.jsonl", replay)
    out = tmp_path / "c.jsonl"
    assert ua.run_corrections_emit(db, tmp_path / "p", out) == 1
    assert json.loads(out.read_text())["session"] == "s1"


def test_summarize_accepts_answer_found_outside_candidates_only_with_its_facts():
    emitted = [{"key": "a", "candidates": []}, {"key": "b", "candidates": []}]
    verdicts = {
        "a": {
            "verdict": "covered",
            "answer_id": 9,
            "answer_origin": "hand",
            "injected_before": True,
        },
        "b": {"verdict": "covered", "answer_id": 9},  # no facts recorded: unusable
    }
    s = ua.summarize_corrections(emitted, verdicts)["counts"]
    assert s["use_failure"] == 1 and s["answer_outside_candidates"] == 1 and s["bad_verdict"] == 1
