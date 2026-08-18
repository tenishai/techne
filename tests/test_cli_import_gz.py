"""CLI-level tests for `techne import` against archived transcripts (#621).

The directory branch has always had a per-file net (`import_directory`); the
single-file branch had none, so the SAME corrupt file gave a `skip … reason:` line
through one entry point and a raw traceback through the other — and for a truncated
archive, whose EOFError Click reads as an abort, a bare `Aborted!` with no filename
and no reason. These lock the two entry points to the same contract.
"""

from __future__ import annotations

import gzip
import json

import pytest
from click.testing import CliRunner

from techne.cli import main


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    from techne.config import Config
    from techne.core.database import init_db

    db = tmp_path / "techne.db"
    init_db(db).close()
    cfg = Config(db_path=db, data_dir=tmp_path)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    return cfg


_RECORD = {
    "type": "assistant",
    "timestamp": "2026-06-01T10:00:01.000Z",
    "message": {
        "content": [
            {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "pytest"}}
        ]
    },
}


def _corrupt_payload(p):
    """Valid gzip header, corrupt payload -> zlib.error."""
    p.write_bytes(b"\x1f\x8b\x08 not actually gzip")


def _truncated(p):
    """Cut mid-stream -> EOFError, which Click renders as `Aborted!`."""
    p.write_bytes(gzip.compress(json.dumps(_RECORD).encode())[:10])


@pytest.mark.parametrize("make_broken", [_corrupt_payload, _truncated])
def test_single_file_import_of_a_broken_archive_is_reported_not_fatal(
    cli_db, tmp_path, make_broken
):
    """ "Not fatal" means REPORTED, never a traceback or a bare `Aborted!` — that is
    what #622 bought and it is unchanged.

    The exit code is no longer 0 (techne#630): a single file that failed is not a
    partial success, and reporting it as one broke "no silent failures" at the exit
    code while the stdout was already honest. The OUTPUT contract below is the part
    #622 actually established, and every line of it still holds.
    """
    broken = tmp_path / "sess-broken.jsonl.gz"
    make_broken(broken)

    result = CliRunner().invoke(main, ["import", str(broken)])

    assert result.exit_code != 0, "a single file that failed must not report success"
    assert "Aborted!" not in result.output, "never a bare abort — the #622 contract"
    assert "sess-broken.jsonl.gz" in result.output  # the filename, not a bare abort
    assert "error" in result.output.lower()


@pytest.mark.parametrize("make_broken", [_corrupt_payload, _truncated])
def test_both_entry_points_agree_on_the_same_broken_file(cli_db, tmp_path, make_broken):
    """They agree on what they SAY. They deliberately differ on what they RETURN.

    #622 established this test to stop one entry point raising a traceback while
    the other reported cleanly, and that agreement is intact: same filename, same
    reason, no bare `Aborted!` from either.

    The exit codes now differ ON PURPOSE (techne#630). A DIRECTORY with one bad
    file among many can still be a successful run — partial success is real there.
    A single named file that failed has no partial anything; exit 0 would be the
    command reporting success for work it did not do.
    """
    d = tmp_path / "proj"
    d.mkdir()
    make_broken(d / "sess-broken.jsonl.gz")

    as_dir = CliRunner().invoke(main, ["import", str(d)])
    as_file = CliRunner().invoke(main, ["import", str(d / "sess-broken.jsonl.gz")])

    for r in (as_dir, as_file):
        assert "error" in r.output.lower(), "both must give a REASON"
        assert "Aborted!" not in r.output, "neither may bare-abort"
        assert "Traceback" not in r.output, "neither may raise raw"
        assert "sess-bro" in r.output, "both must identify WHICH file"

    # ASYMMETRY, PRE-EXISTING AND MEASURED HERE rather than assumed away: only the
    # single-file path prints the full `sess-broken.jsonl.gz`. The directory path
    # identifies by session id, which renders truncated to 8 chars — `sess-bro`.
    # #622's own comment explains why the single-file branch puts the name in the
    # REASON instead ("that field renders truncated to 8 chars, which would clip a
    # name to 'sess-bro'"). Worth knowing before anyone asserts stronger agreement
    # than the two paths actually offer; not in scope for techne#630.
    assert "sess-broken.jsonl.gz" in as_file.output
    assert "sess-broken.jsonl.gz" not in as_dir.output

    assert as_dir.exit_code == 0, "a directory run that did partial work is a success"
    assert as_file.exit_code != 0, "a single named file that failed is not"


def test_single_file_import_of_a_non_transcript_name_is_reported_not_fatal(cli_db, tmp_path):
    """session_id_for now RAISES rather than falling back to .stem (that fallback was
    the Critical). The raise is correct; arriving as a traceback was not."""
    odd = tmp_path / "sess-abc.jsonl.bak"  # >=1 record, no sessionId, unknown suffix
    odd.write_text(json.dumps(_RECORD) + "\n")

    result = CliRunner().invoke(main, ["import", str(odd)])

    # Non-zero since techne#630 — the raise was always correct, ARRIVING AS A
    # TRACEBACK was the defect. It still arrives as a named, reasoned message.
    assert result.exit_code != 0, result.output
    assert "sess-abc.jsonl.bak" in result.output
    assert "Traceback" not in result.output


def test_a_good_archive_still_imports_through_the_single_file_path(cli_db, tmp_path):
    good = tmp_path / "sess-good.jsonl.gz"
    with gzip.open(good, "wt", encoding="utf-8") as f:
        f.write(json.dumps(_RECORD) + "\n")

    result = CliRunner().invoke(main, ["import", str(good)])

    assert result.exit_code == 0, result.output
    assert "error" not in result.output.lower()
