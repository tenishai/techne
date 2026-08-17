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
    broken = tmp_path / "sess-broken.jsonl.gz"
    make_broken(broken)

    result = CliRunner().invoke(main, ["import", str(broken)])

    assert result.exit_code == 0, result.output
    assert "Aborted!" not in result.output
    assert "sess-broken.jsonl.gz" in result.output  # the filename, not a bare abort
    assert "error" in result.output.lower()


@pytest.mark.parametrize("make_broken", [_corrupt_payload, _truncated])
def test_both_entry_points_agree_on_the_same_broken_file(cli_db, tmp_path, make_broken):
    """The contract this PR asserts for a directory must hold for one file."""
    d = tmp_path / "proj"
    d.mkdir()
    make_broken(d / "sess-broken.jsonl.gz")

    as_dir = CliRunner().invoke(main, ["import", str(d)])
    as_file = CliRunner().invoke(main, ["import", str(d / "sess-broken.jsonl.gz")])

    assert as_dir.exit_code == as_file.exit_code == 0, (as_dir.output, as_file.output)


def test_single_file_import_of_a_non_transcript_name_is_reported_not_fatal(cli_db, tmp_path):
    """session_id_for now RAISES rather than falling back to .stem (that fallback was
    the Critical). The raise is correct; arriving as a traceback was not."""
    odd = tmp_path / "sess-abc.jsonl.bak"  # >=1 record, no sessionId, unknown suffix
    odd.write_text(json.dumps(_RECORD) + "\n")

    result = CliRunner().invoke(main, ["import", str(odd)])

    assert result.exit_code == 0, result.output
    assert "sess-abc.jsonl.bak" in result.output


def test_a_good_archive_still_imports_through_the_single_file_path(cli_db, tmp_path):
    good = tmp_path / "sess-good.jsonl.gz"
    with gzip.open(good, "wt", encoding="utf-8") as f:
        f.write(json.dumps(_RECORD) + "\n")

    result = CliRunner().invoke(main, ["import", str(good)])

    assert result.exit_code == 0, result.output
    assert "error" not in result.output.lower()
