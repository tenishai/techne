"""CLI-level coverage for the transcript commands (techne#630, #631).

Both findings are about the CLI LAYER specifically: #622's tests call
`index_transcripts` in-process, and `cli.py` is the only place a user ever sees
the `unreadable` count or the single-file exit code. A function that works and a
command that reports it correctly are different claims.
"""

from __future__ import annotations

import gzip
import json

import pytest
from click.testing import CliRunner

from techne.cli import main


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    """A Config pointed at a fresh tmp db, injected via Config.load()."""
    from techne.config import Config
    from techne.core.database import init_db

    db = tmp_path / "techne.db"
    init_db(db).close()
    cfg = Config()
    cfg.db_path = db
    cfg.data_dir = tmp_path
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    return cfg


def _good_transcript(path, session_id="aaaaaaaa-1111-2222-3333-444444444444"):
    """A minimal Claude Code .jsonl the importer accepts."""
    rows = [
        {
            "type": "user",
            "sessionId": session_id,
            "cwd": "/tmp/proj",
            "timestamp": "2026-08-18T00:00:00Z",
            "message": {"role": "user", "content": "hello there"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "cwd": "/tmp/proj",
            "timestamp": "2026-08-18T00:00:01Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


# ---------------------------------------------------------------------------
# techne#631 — `library index-transcripts` had NO CLI-level test at all, and it
# is the only surface that renders the `unreadable` count added in #622.
# ---------------------------------------------------------------------------


def test_index_transcripts_reports_the_unreadable_count(cli_db, tmp_path):
    """One good transcript and one corrupt archive: the command must report BOTH,
    because a short `total` with no other signal is indistinguishable from a
    smaller corpus — which is exactly why #622 added the field."""
    # iter_transcript_files globs the given directory directly (not recursively),
    # so --dir points at the folder holding the transcripts.
    tdir = tmp_path / "sessions"
    tdir.mkdir()
    _good_transcript(tdir / "aaaaaaaa-1111-2222-3333-444444444444.jsonl")
    # A .gz that is not valid gzip — the corrupt-archive case #622 was written for.
    (tdir / "bbbbbbbb-1111-2222-3333-444444444444.jsonl.gz").write_bytes(b"not gzip at all")

    result = CliRunner().invoke(main, ["library", "index-transcripts", "--dir", str(tdir)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") : result.output.rindex("}") + 1])
    assert set(payload) >= {"total", "stored", "unchanged", "empty", "unreadable"}
    assert payload["unreadable"] == 1, (
        "the corrupt archive must be COUNTED, not silently absent from `total` "
        "(techne#631 — this line had no test)"
    )


def test_index_transcripts_limit_zero_indexes_nothing(cli_db, tmp_path):
    """`--limit 0` used to mean NO LIMIT and iterate the whole directory, because
    the guard was `if limit:`. An explicit 0 is a legitimate "index nothing", and
    the opposite of what the flag says (noted alongside techne#631)."""
    tdir = tmp_path / "sessions"
    tdir.mkdir()
    _good_transcript(tdir / "aaaaaaaa-1111-2222-3333-444444444444.jsonl")

    result = CliRunner().invoke(
        main, ["library", "index-transcripts", "--dir", str(tdir), "--limit", "0"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{") : result.output.rindex("}") + 1])
    assert payload["total"] == 0, "--limit 0 must index nothing, not everything"


# ---------------------------------------------------------------------------
# techne#630 — a single file has no partial success.
# ---------------------------------------------------------------------------


def test_single_file_import_that_imports_nothing_is_reported_not_silent(cli_db, tmp_path):
    """techne#630, resolved as "document the current behaviour as intentional".

    The issue offered two resolutions: exit non-zero when a single-file import
    imported nothing, or record the current contract deliberately. The second is
    correct here, because #622 established in a test named for it that BOTH ENTRY
    POINTS AGREE on the same broken file — and a file behaving differently
    depending on whether you named it or a glob found it is its own surprise.

    What the objection is really about is silence, and there is none: the
    filename, the reason, and the tally all print. Only the exit code is
    generous. This test pins that the REPORTING stays, so a future change cannot
    quietly make it a silent success.
    """
    bad = tmp_path / "sess-broken.jsonl.gz"
    bad.write_bytes(b"not gzip at all")

    result = CliRunner().invoke(main, ["import", str(bad), "--no-prompts"])

    assert result.exit_code == 0, "deliberate — see techne#630 and the comment in cli.py"
    assert "sess-broken" in result.output, "the failure must name the file"
    assert "0 imported, 1 skipped" in result.output, "and report the tally honestly"
    assert "error" in result.output.lower(), "and give a reason"
    assert "Aborted!" not in result.output, "never a bare abort (#622)"


def test_single_file_import_that_works_still_exits_zero(cli_db, tmp_path):
    """The guard must not fire on success — otherwise it is just a broken command."""
    good = _good_transcript(tmp_path / "aaaaaaaa-1111-2222-3333-444444444444.jsonl")
    result = CliRunner().invoke(main, ["import", str(good), "--no-prompts"])
    assert result.exit_code == 0, result.output
    assert "1 imported, 0 skipped" in result.output


def test_directory_import_with_one_bad_file_still_exits_zero(cli_db, tmp_path):
    """The partial-success case #630 explicitly preserves: a directory where some
    files imported and some did not is a run that did useful work."""
    d = tmp_path / "batch"
    d.mkdir()
    _good_transcript(d / "aaaaaaaa-1111-2222-3333-444444444444.jsonl")
    (d / "sess-broken.jsonl.gz").write_bytes(b"not gzip at all")

    result = CliRunner().invoke(main, ["import", str(d), "--no-prompts"])

    assert result.exit_code == 0, result.output
    assert "1 imported" in result.output


def test_a_valid_gzip_transcript_round_trips(cli_db, tmp_path):
    """Guards the fixture itself: the corrupt-archive tests above only mean
    something if a VALID .jsonl.gz would have imported."""
    raw = tmp_path / "aaaaaaaa-1111-2222-3333-444444444444.jsonl"
    _good_transcript(raw)
    gz = tmp_path / "aaaaaaaa-1111-2222-3333-444444444444.jsonl.gz"
    gz.write_bytes(gzip.compress(raw.read_bytes()))
    raw.unlink()

    result = CliRunner().invoke(main, ["import", str(gz), "--no-prompts"])
    assert result.exit_code == 0, result.output
    assert "1 imported, 0 skipped" in result.output
