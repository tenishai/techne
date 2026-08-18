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


def test_single_file_import_that_failed_exits_non_zero(cli_db, tmp_path):
    """techne#630, decided 2026-08-18 (Richard: "we should fix").

    The partial-success argument that justifies exit 0 for a DIRECTORY does not
    apply to one named file: a failure is the whole outcome, and reporting it as
    success breaks "no silent failures" at the exit code while stdout is honest.

    The reporting is unchanged and still asserted — this is about the return
    value, not about going quiet.
    """
    bad = tmp_path / "sess-broken.jsonl.gz"
    bad.write_bytes(b"not gzip at all")

    result = CliRunner().invoke(main, ["import", str(bad), "--no-prompts"])

    assert result.exit_code != 0
    assert "sess-broken" in result.output, "the failure must still name the file"
    assert "0 imported, 1 skipped" in result.output, "and still report the tally"
    assert "error" in result.output.lower(), "and still give a reason"
    assert "Aborted!" not in result.output, "never a bare abort (#622)"


def test_re_importing_an_already_imported_file_still_exits_zero(cli_db, tmp_path):
    """THE CASE A FLAT `not imported` TEST WOULD HAVE BROKEN (techne#630).

    `import_transcript` is idempotent by design — a session that already has
    observations is skipped with reason "already has N observations". That skip is
    the feature working. Gating the exit code on `imported == 0` would turn the
    safest possible habit, re-running an import, into a reported failure.

    So the guard is on FAILURE, not on zero, and this pins the difference.
    """
    good = _good_transcript(tmp_path / "aaaaaaaa-1111-2222-3333-444444444444.jsonl")

    first = CliRunner().invoke(main, ["import", str(good), "--no-prompts"])
    assert first.exit_code == 0 and "1 imported" in first.output

    second = CliRunner().invoke(main, ["import", str(good), "--no-prompts"])

    assert second.exit_code == 0, (
        "re-importing an already-imported file is idempotent SUCCESS, not failure"
    )
    assert "0 imported, 1 skipped" in second.output
    assert "already has" in second.output


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
