"""Tests for the Claude Code transcript adapter (native JSONL → canonical Transcript)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from techne.core.transcripts import Transcript, derive_title
from techne.platforms.claude_code.transcript_library import (
    ClaudeCodeTranscriptSource,
    dialogue_text,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def test_cc_source_yields_canonical_transcripts(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    _write_jsonl(
        d / "sess-abc.jsonl",
        [
            {"message": {"role": "user", "content": "what is the plan"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "the plan is X"}],
                }
            },
            # pure tool_use → no text → dropped (plumbing, not dialogue)
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                }
            },
        ],
    )
    source = ClaudeCodeTranscriptSource(d)
    assert source.platform == "claude-code"
    transcripts = list(source.iter_transcripts())
    assert len(transcripts) == 1
    t = transcripts[0]
    assert isinstance(t, Transcript)
    assert t.id == "sess-abc"
    assert t.platform == "claude-code"
    assert [(turn.role, turn.text) for turn in t.turns] == [
        ("user", "what is the plan"),
        ("assistant", "the plan is X"),
    ]
    assert t.uri.endswith("sess-abc.jsonl")


def test_cc_source_satisfies_protocol(tmp_path):
    from techne.core.transcripts import TranscriptSource

    d = tmp_path / "t"
    d.mkdir()
    assert isinstance(ClaudeCodeTranscriptSource(d), TranscriptSource)


# --- archived (.gz) transcripts (#621) -------------------------------------


def _write_jsonl_gz(path: Path, records: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in records))


_TURNS = [
    {"message": {"role": "user", "content": "what is the plan"}},
    {"message": {"role": "assistant", "content": [{"type": "text", "text": "the plan is X"}]}},
]


def test_archived_transcript_is_indexed_with_the_right_session_id(tmp_path):
    """The id becomes the Librarian's source_url, so a wrong one indexes the same
    session twice. Path.stem on "sess-abc.jsonl.gz" yields "sess-abc.jsonl"."""
    d = tmp_path / "transcripts"
    d.mkdir()
    _write_jsonl_gz(d / "sess-abc.jsonl.gz", _TURNS)

    transcripts = list(ClaudeCodeTranscriptSource(d).iter_transcripts())

    assert len(transcripts) == 1
    assert transcripts[0].id == "sess-abc"
    assert [(t.role, t.text) for t in transcripts[0].turns] == [
        ("user", "what is the plan"),
        ("assistant", "the plan is X"),
    ]


def test_archived_and_plain_are_read_identically(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    _write_jsonl(d / "plain.jsonl", _TURNS)
    _write_jsonl_gz(d / "archived.jsonl.gz", _TURNS)

    by_id = {t.id: t for t in ClaudeCodeTranscriptSource(d).iter_transcripts()}

    assert set(by_id) == {"plain", "archived"}
    assert [(t.role, t.text) for t in by_id["archived"].turns] == [
        (t.role, t.text) for t in by_id["plain"].turns
    ]


def test_mid_archive_session_is_yielded_once_preferring_the_plain_file(tmp_path):
    """An archive run that wrote the .gz but has not yet unlinked the .jsonl must not
    yield the session twice — that would double-import append-only observations."""
    d = tmp_path / "transcripts"
    d.mkdir()
    _write_jsonl(d / "sess-abc.jsonl", _TURNS)
    _write_jsonl_gz(d / "sess-abc.jsonl.gz", _TURNS)

    transcripts = list(ClaudeCodeTranscriptSource(d).iter_transcripts())

    assert [t.id for t in transcripts] == ["sess-abc"]
    assert transcripts[0].uri.endswith("sess-abc.jsonl")


def test_an_unreadable_archive_is_recorded_not_fatal(tmp_path):
    """One corrupt .gz must not end the indexing run, and must not pass silently —
    a skip nobody can count turns the next coverage measurement into a wrong answer."""
    d = tmp_path / "transcripts"
    d.mkdir()
    (d / "truncated.jsonl.gz").write_bytes(gzip.compress(b'{"message": {}}')[:10])
    _write_jsonl(d / "good.jsonl", _TURNS)

    source = ClaudeCodeTranscriptSource(d)
    transcripts = list(source.iter_transcripts())

    assert [t.id for t in transcripts] == ["good"]
    assert [p.name for p, _ in source.unreadable] == ["truncated.jsonl.gz"]


def test_a_corrupt_payload_is_recorded_not_fatal(tmp_path):
    """A valid gzip header over a corrupt payload raises zlib.error, which is NEITHER
    OSError NOR EOFError — the taxonomy an enumerated handler got wrong, and the most
    likely real corruption."""
    import zlib

    d = tmp_path / "transcripts"
    d.mkdir()
    (d / "corrupt.jsonl.gz").write_bytes(b"\x1f\x8b\x08 not actually gzip")
    _write_jsonl(d / "good.jsonl", _TURNS)

    # pin the premise: this fixture really does raise the class neither name covers
    with pytest.raises(zlib.error):
        with gzip.open(d / "corrupt.jsonl.gz", "rt", encoding="utf-8") as f:
            f.read()

    source = ClaudeCodeTranscriptSource(d)
    transcripts = list(source.iter_transcripts())

    assert [t.id for t in transcripts] == ["good"]
    assert [p.name for p, _ in source.unreadable] == ["corrupt.jsonl.gz"]


def test_unreadable_resets_between_iterations(tmp_path):
    """It describes the LAST consumption, not a running total."""
    d = tmp_path / "transcripts"
    d.mkdir()
    (d / "corrupt.jsonl.gz").write_bytes(b"\x1f\x8b\x08 not actually gzip")

    source = ClaudeCodeTranscriptSource(d)
    for _ in range(3):
        list(source.iter_transcripts())

    assert len(source.unreadable) == 1


@pytest.mark.skipif(
    not (Path.home() / ".claude" / "projects").exists(),
    reason="needs real Claude Code transcripts; hermetic in CI",
)
def test_real_transcripts_survive_the_round_trip(tmp_path):
    """The only check exercising the adapter against records nobody constructed —
    the property the constructed fixtures above cannot have."""
    src = sorted(
        (Path.home() / ".claude" / "projects").glob("*/*.jsonl"),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )[:2]
    if not src:
        pytest.skip("no transcripts on disk")

    plain_d, gz_d = tmp_path / "plain", tmp_path / "gz"
    plain_d.mkdir()
    gz_d.mkdir()
    for f in src:
        raw = f.read_bytes()
        (plain_d / f.name).write_bytes(raw)
        (gz_d / (f.name + ".gz")).write_bytes(gzip.compress(raw))

    plain = {t.id: t for t in ClaudeCodeTranscriptSource(plain_d).iter_transcripts()}
    source = ClaudeCodeTranscriptSource(gz_d)
    archived = {t.id: t for t in source.iter_transcripts()}

    assert source.unreadable == []
    assert set(archived) == set(plain)
    for sid in plain:
        assert [(t.role, t.text) for t in archived[sid].turns] == [
            (t.role, t.text) for t in plain[sid].turns
        ]


def test_index_transcripts_surfaces_an_unreadable_archive_end_to_end(tmp_path):
    """The real adapter through the real indexer: two files on disk, one indexed, and
    the count says so rather than leaving the caller to infer it."""
    from techne.config import Config
    from techne.core.database import init_db
    from techne.core.transcripts import index_transcripts

    d = tmp_path / "transcripts"
    d.mkdir()
    (d / "corrupt.jsonl.gz").write_bytes(b"\x1f\x8b\x08 not actually gzip")
    _write_jsonl(d / "good.jsonl", _TURNS)

    db = tmp_path / "t.db"
    init_db(db).close()
    config = Config(db_path=db, data_dir=tmp_path)

    counts = index_transcripts(config, ClaudeCodeTranscriptSource(d), make_cards=False)

    assert counts["total"] == 1
    assert counts["unreadable"] == 1


# --- dialogue_text: which user turns are the client's plumbing (techne#2806) -------


@pytest.mark.parametrize(
    "raw",
    [
        "<local-command-caveat>Caveat: The messages below were generated by the user",
        "<local-command-stdout>Goodbye!</local-command-stdout>",
        "<command-name>/exit</command-name>\n<command-args></command-args>",
        "<command-message>investigate-url</command-message>\n<command-name>/x</command-name>",
        "<bash-input> sudo scutil --set HostName dev04.lan</bash-input>",
        "<bash-stdout></bash-stdout><bash-stderr>sudo: a terminal is required</bash-stderr>",
        "<task-notification>\n<task-id>abc</task-id>\n<status>completed</status>",
        "<system-reminder>remember the thing</system-reminder>",
        "<fork-boilerplate>\nYou are a worker fork.",
        "---BEGIN USER MESSAGES---\nsay hi\n---END USER MESSAGES---\n\nGenerate a title.",
    ],
)
def test_dialogue_text_rejects_harness_plumbing(raw):
    assert dialogue_text(raw) is None


def test_dialogue_text_strips_turn_context_but_keeps_the_message():
    """`<turn-context>` is a PREFIX on a real message, not a turn of its own — dropping
    the turn would discard what the user actually asked."""
    raw = (
        "<turn-context>\n<current-time>2026-07-13 17:20:00</current-time>\n"
        "<working-directory>/Users/dimartin</working-directory>\n</turn-context>"
        "Reply with exactly one line: ACP link works, and name the model you are."
    )
    assert dialogue_text(raw) == (
        "Reply with exactly one line: ACP link works, and name the model you are."
    )


def test_dialogue_text_keeps_ordinary_dialogue():
    assert dialogue_text("design the store\nmore") == "design the store\nmore"


def test_dialogue_text_keeps_a_marker_quoted_inside_a_real_message():
    """Prefix match, not substring: asking ABOUT a marker is still dialogue."""
    raw = "why does <command-name> keep showing up in my transcripts?"
    assert dialogue_text(raw) == raw


def test_cc_source_titles_from_dialogue_not_the_slash_command_wrapper(tmp_path):
    """The regression itself: a session opened with a slash command must not be titled
    with the caveat wrapper the client injects ahead of the first human turn."""
    d = tmp_path / "transcripts"
    d.mkdir()
    _write_jsonl(
        d / "sess-slash.jsonl",
        [
            {
                "message": {
                    "role": "user",
                    "content": "<local-command-caveat>Caveat: The messages below were "
                    "generated by the user while running local commands.",
                },
                "isMeta": True,
            },
            {"message": {"role": "user", "content": "<command-name>/model</command-name>"}},
            {"message": {"role": "user", "content": "why is recall empty\nsecond line"}},
        ],
    )
    (t,) = list(ClaudeCodeTranscriptSource(d).iter_transcripts())
    hook = ClaudeCodeTranscriptSource.dialogue_text
    assert derive_title(t.turns, dialogue_text=hook) == "why is recall empty"
    # Every turn is still STORED — the hook narrows titles, not the record.
    assert len(t.turns) == 3


def test_index_transcripts_titles_from_dialogue_through_a_real_source_instance(tmp_path):
    """techne#820. Production reads the hook off an INSTANCE
    (``getattr(source, "dialogue_text", None)`` in ``index_transcripts``), and the
    ``staticmethod(...)`` binding is what keeps that call from receiving ``self``
    as the turn text. Every test #817 added reads the hook off the CLASS instead,
    so deleting the binding left them all green. One OLDER test does red on it --
    ``test_index_transcripts_surfaces_an_unreadable_archive_end_to_end`` (#629),
    which drives the real adapter through the real indexer for unrelated reasons
    and takes the ``TypeError`` on the way past. That is the "covered only by
    accident" the issue names, recorded here so a bisect landing on two red tests
    is not a surprise.

    This drives the real adapter through the real indexer, which is the path
    production takes — drop the ``staticmethod`` and this goes red.
    """
    from techne.config import Config
    from techne.core.database import init_db
    from techne.core.documents import list_documents
    from techne.core.librarian import librarian_db
    from techne.core.transcripts import index_transcripts

    d = tmp_path / "transcripts"
    d.mkdir()
    _write_jsonl(
        d / "sess-slash.jsonl",
        [
            {"message": {"role": "user", "content": "<command-name>/model</command-name>"}},
            {"message": {"role": "user", "content": "why is recall empty"}},
        ],
    )

    db = tmp_path / "t.db"
    init_db(db).close()
    config = Config(db_path=db, data_dir=tmp_path)

    index_transcripts(config, ClaudeCodeTranscriptSource(d), make_cards=False)

    titles = {doc["title"] for doc in list_documents(librarian_db(config))}
    assert titles == {"why is recall empty"}
