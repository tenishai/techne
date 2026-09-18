"""A resumed session must not import its parent's turns a second time (techne#962).

Reported by Grip during a reflect backfill: sessions 439f291b and 70be4597
produced 4 identical prompt_hash values across a batch, and 54 thread items
deduped to 50.

THE MECHANISM, reproduced locally 2026-09-18. Resuming writes a NEW transcript
that REPLAYS the parent's messages. The replayed records carry the new
`sessionId` but keep their ORIGINAL `uuid`. Import idempotency is per session
("already has N observations", keyed on the session id), so the replayed turns
imported again under the second id.

Measured on this machine before the fix: across 373 non-agent transcripts, 52
file pairs share message uuids; of the 2 pairs where BOTH halves had been
imported, 44 observation rows were duplicated. Small here only because our
corpus rarely imports both halves -- Grip's is 1,496 session ids from 182
transcripts, so their exposure is structurally higher.

SCOPE, AND IT IS NARROWER THAN THE FIRST DRAFT OF THIS FILE IMPLIED. Every
row written before schema v27 has `source_uuid` NULL, and NULL never matches
`= ?`, so the skip cannot see it. A resume imported AFTER the upgrade whose
parent was imported BEFORE it still duplicates the parent in full -- the issue's
own scenario on any store that is not fresh.
`test_a_parent_imported_before_v27_is_not_protected` pins that, so the limit is
documented behaviour rather than a surprise.

WHY THE KEY IS `uuid` AND NOT A CONTENT HASH. Grip's corpus is six concurrent
sessions ("facets") sharing one store, one of them an aggregator that reads
across its siblings. Two facets can legitimately describe the same event from
different vantage points -- different transcripts, DIFFERENT message uuids. A
content-keyed dedup would collapse that into one, discarding a real second
observation. The uuid is exact and is an identity the transcript already
carries, so it is equal only when the record literally IS the same record.
`test_two_facets_describing_one_event_are_both_kept` pins that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from techne.core.database import connect, init_db
from techne.platforms.claude_code.transcript_import import import_transcript


@pytest.fixture
def db_path(tmp_path):
    conn = init_db(tmp_path / "resume.db")
    conn.close()
    return tmp_path / "resume.db"


def _prompt(uuid: str, text: str, ts: str, session_id: str) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": ts,
        "cwd": "/some/project",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _write(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _obs(db, session_id=None):
    conn = connect(db)
    try:
        if session_id:
            rows = conn.execute(
                "SELECT content FROM observations WHERE session_id=?", (session_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT content FROM observations").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def test_a_resume_does_not_reimport_the_parents_turns(db_path, tmp_path):
    """THE REGRESSION. The resume replays two turns and adds one of its own."""
    parent_id = "aaaaaaaa-0000-0000-0000-000000000001"
    resume_id = "bbbbbbbb-0000-0000-0000-000000000002"

    shared = [
        _prompt("uuid-shared-1", "first question", "2026-09-17T10:00:00Z", parent_id),
        _prompt("uuid-shared-2", "second question", "2026-09-17T10:01:00Z", parent_id),
    ]
    parent = tmp_path / f"{parent_id}.jsonl"
    _write(parent, shared)

    # The resume REPLAYS both, under its own sessionId, keeping the uuids.
    replayed = [dict(r, sessionId=resume_id) for r in shared]
    resume = tmp_path / f"{resume_id}.jsonl"
    _write(
        resume,
        [*replayed, _prompt("uuid-new-3", "third question", "2026-09-17T10:05:00Z", resume_id)],
    )

    import_transcript(db_path, parent)
    result = import_transcript(db_path, resume)

    assert _obs(db_path, parent_id) == ["first question", "second question"]
    assert _obs(db_path, resume_id) == ["third question"], (
        "the resume must contribute only its own turn"
    )
    assert sorted(_obs(db_path)) == ["first question", "second question", "third question"]
    assert result["replayed_skipped"] == 2, "the skip must be reported, not silent"


def test_two_facets_describing_one_event_are_both_kept(db_path, tmp_path):
    """The cross-facet case a content hash would wrongly collapse.

    Same text, same timestamp, different transcripts, DIFFERENT uuids: two real
    observations from two vantage points. Both must survive.
    """
    a_id = "cccccccc-0000-0000-0000-000000000003"
    b_id = "dddddddd-0000-0000-0000-000000000004"
    a = tmp_path / f"{a_id}.jsonl"
    b = tmp_path / f"{b_id}.jsonl"
    _write(a, [_prompt("uuid-facet-a", "deploy finished", "2026-09-17T11:00:00Z", a_id)])
    _write(b, [_prompt("uuid-facet-b", "deploy finished", "2026-09-17T11:00:00Z", b_id)])

    import_transcript(db_path, a)
    import_transcript(db_path, b)

    assert _obs(db_path) == ["deploy finished", "deploy finished"], (
        "identical content from different records is two observations, not one"
    )


def test_force_still_reimports(db_path, tmp_path):
    """`force` is the documented escape hatch and must keep overriding the guard."""
    sid = "eeeeeeee-0000-0000-0000-000000000005"
    path = tmp_path / f"{sid}.jsonl"
    _write(path, [_prompt("uuid-forced", "only turn", "2026-09-17T12:00:00Z", sid)])

    import_transcript(db_path, path)
    import_transcript(db_path, path, force=True)

    assert len(_obs(db_path)) == 2, "force appends, as documented"


def test_records_without_a_uuid_are_unaffected(db_path, tmp_path):
    """Not every record carries a uuid; those must import exactly as before."""
    sid = "ffffffff-0000-0000-0000-000000000006"
    path = tmp_path / f"{sid}.jsonl"
    rec = _prompt("x", "no uuid here", "2026-09-17T13:00:00Z", sid)
    del rec["uuid"]
    _write(path, [rec])

    import_transcript(db_path, path)
    assert _obs(db_path) == ["no uuid here"]


def test_the_uuid_is_recorded_on_the_row(db_path, tmp_path):
    """Without this column the skip has nothing to key on."""
    sid = "99999999-0000-0000-0000-000000000007"
    path = tmp_path / f"{sid}.jsonl"
    _write(path, [_prompt("uuid-recorded", "a turn", "2026-09-17T14:00:00Z", sid)])

    import_transcript(db_path, path)
    conn = connect(db_path)
    try:
        assert conn.execute("SELECT source_uuid FROM observations").fetchone()[0] == (
            "uuid-recorded"
        )
    finally:
        conn.close()


def test_a_parent_imported_before_v27_is_not_protected(db_path, tmp_path):
    """PINS THE LIMIT the pre-pass found (techne#962).

    The skip keys on `source_uuid`, which every pre-v27 row has as NULL, and NULL
    never matches `= ?`. So the guard protects a resume only when its PARENT was
    also imported at v27 or later. Closing it needs a backfill keyed on each
    session's own transcript, which invents nothing but is a separate change, filed as techne#978.

    This test exists so that limit is a recorded decision, not a gap someone
    rediscovers in production.
    """
    import sqlite3

    parent_id = "11111111-0000-0000-0000-00000000000a"
    resume_id = "22222222-0000-0000-0000-00000000000b"
    shared = [
        _prompt("uuid-old-1", "first question", "2026-09-17T10:00:00Z", parent_id),
        _prompt("uuid-old-2", "second question", "2026-09-17T10:01:00Z", parent_id),
    ]
    parent = tmp_path / f"{parent_id}.jsonl"
    _write(parent, shared)
    import_transcript(db_path, parent)

    # Simulate rows written before the column existed.
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE observations SET source_uuid = NULL")
    conn.commit()
    conn.close()

    resume = tmp_path / f"{resume_id}.jsonl"
    _write(resume, [dict(r, sessionId=resume_id) for r in shared])
    result = import_transcript(db_path, resume)

    assert result["replayed_skipped"] == 0, (
        "premise changed: the skip now sees pre-v27 rows, so the backfill gap may "
        "be closed and this test should be replaced by one asserting it IS protected"
    )
    assert len(_obs(db_path)) == 4, (
        "the parent's turns are duplicated, which is the documented limit"
    )


def _assistant_text(uuid: str, text: str, ts: str, session_id: str) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": ts,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _tool_use(uuid: str, tool_id: str, cmd: str, ts: str, session_id: str) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": ts,
        "message": {
            "content": [
                {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": cmd}}
            ]
        },
    }


def _tool_result(uuid: str, tool_id: str, out: str, ts: str, session_id: str) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": ts,
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": out}]},
    }


def test_all_three_insert_paths_are_guarded(db_path, tmp_path):
    """THE REGRESSION A FUTURE AUTHOR WOULD PLAUSIBLY INTRODUCE (Prudence, #972).

    Every other record in this file is built by `_prompt`, so only the UserPrompt
    INSERT was ever exercised. Measured by Prudence: removing `source_uuid` from
    the AssistantText and tool-result INSERTs left the WHOLE SUITE green while the
    guard silently stopped working for both -- and those are precisely the two
    paths where the original 44 duplicates were observed, since they were
    (tool_name, content) rows.

    The three statements are near-identical column lists in one function: what a
    formatter reflows, a refactor splits, or a copy-paste extends for a fourth
    observation type.

    THIS ALSO EXERCISES THE ONE STRUCTURAL SUBTLETY, which was previously correct
    by reasoning rather than by test. Skipping a replayed ASSISTANT record means
    its `tool_use` never enters `pending`. The tool ROW is stamped with the
    TOOL_RESULT record's uuid, so the pair is skipped or kept together and no
    orphan row is written -- see the replayed_skipped assertion below, where 3 of
    4 replayed records skip and the tool_use is the one that does not.
    """
    parent_id = "aaaa1111-0000-0000-0000-00000000000a"
    resume_id = "bbbb2222-0000-0000-0000-00000000000b"

    shared = [
        _prompt("u-prompt", "do the thing", "2026-09-17T10:00:00Z", parent_id),
        _assistant_text("u-assist", "here is my plan", "2026-09-17T10:00:01Z", parent_id),
        _tool_use("u-use", "tool-1", "pytest -q", "2026-09-17T10:00:02Z", parent_id),
        _tool_result("u-result", "tool-1", "3 passed", "2026-09-17T10:00:03Z", parent_id),
    ]
    parent = tmp_path / f"{parent_id}.jsonl"
    _write(parent, shared)
    import_transcript(db_path, parent)

    before = _obs(db_path)
    assert any("plan" in c for c in before), "precondition: assistant text was imported"
    assert any("pytest" in c for c in before), (
        "precondition: the tool row was imported. `should_import` drops read-only "
        "Bash navigation, so the command has to be one with real signal."
    )

    resume = tmp_path / f"{resume_id}.jsonl"
    _write(
        resume,
        [dict(r, sessionId=resume_id) for r in shared]
        + [_prompt("u-new", "and now this", "2026-09-17T10:05:00Z", resume_id)],
    )
    result = import_transcript(db_path, resume)

    assert _obs(db_path, resume_id) == ["and now this"], (
        "the resume contributed more than its own turn; one of the three INSERT "
        f"paths is unguarded. got: {_obs(db_path, resume_id)}"
    )
    # THREE OF FOUR, AND THE MISSING ONE IS THE POINT. The `tool_use` record's
    # uuid never reaches `observations`: a tool_use only populates `pending`, and
    # the row is written under the TOOL_RESULT record's uuid. So the tool_use is
    # re-processed on the resume -- harmlessly, because all it does is repopulate
    # `pending`, and its paired result IS skipped, so no row is written.
    #
    # That is the structural subtlety in this change, now MEASURED rather than
    # hand-traced: skipping a replayed assistant record means its tool_use never
    # enters `pending`, and the pair is still kept or skipped together.
    assert result["replayed_skipped"] == 3, result
    assert sorted(_obs(db_path)) == sorted(before + ["and now this"])


def test_the_sync_adapter_also_guards_a_resume(tmp_path):
    """THE SECOND DOOR (Prudence, #972). A different writer, the same duplication.

    `techne/sync/jsonl_transcript.py` writes into `observations` with the same
    pairing and filtering as transcript_import, but its idempotency is a per-file
    BYTE-OFFSET cursor -- and a resumed session is a NEW file, so start_byte=0 and
    every replayed parent record is emitted again under the new session id.

    Measured by Prudence on a fresh v27 store: 5 rows where 3 are correct, with
    source_uuid NULL throughout. The NULL stamping was the worse half: those rows
    could not protect a later `techne import` either, so the documented "pre-v27
    only" limit reappeared on a store that had never seen v26, from rows written
    AFTER the upgrade.
    """
    import sqlite3

    from techne.core.database import init_db
    from techne.sync.jsonl_transcript import _ingest_records_from_offset

    db = tmp_path / "sync.db"
    init_db(db)

    parent_id = "cccc3333-0000-0000-0000-00000000000c"
    resume_id = "dddd4444-0000-0000-0000-00000000000d"
    shared = [
        _prompt("s-1", "first question", "2026-09-17T10:00:00Z", parent_id),
        _prompt("s-2", "second question", "2026-09-17T10:01:00Z", parent_id),
    ]
    parent = tmp_path / f"{parent_id}.jsonl"
    resume = tmp_path / f"{resume_id}.jsonl"
    _write(parent, shared)
    _write(
        resume,
        [dict(r, sessionId=resume_id) for r in shared]
        + [_prompt("s-3", "third question", "2026-09-17T10:05:00Z", resume_id)],
    )

    for i, path in enumerate((parent, resume)):
        _ingest_records_from_offset(db, path, 0, "test", cursor_key=path.stem, inode=i, head="h")

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT session_id, source_uuid FROM observations ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 3, (
        f"the adapter duplicated the parent's turns: {len(rows)} rows where 3 are correct"
    )
    assert all(su for _sid, su in rows), (
        "the adapter left source_uuid NULL, so its rows cannot protect a later "
        f"`techne import` either: {rows}"
    )
