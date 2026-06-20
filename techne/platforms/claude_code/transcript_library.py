"""Index Claude Code JSONL transcripts into the Librarian documents store (techne#91).

Two lenses on the same transcripts, complementary:
  - transcript_import.py → *observations* (tool calls) → threads/reflect.
  - this module          → *documents* (the conversation prose) → Librarian recall.

The decisions in a session live in the dialogue — operator statements, conclusions
reached in back-and-forth — which are not tool calls, so the observation lens can't
see them. Indexing the prose as documents makes them FTS + vector searchable and
fetch-on-demand, so "what did we decide about X weeks ago" is recallable.

Deferred (Option D card layer, knowledge#13): a per-transcript `document_card` in
knowledge (gist + pointer) so recall returns the gist and fetches full content on
demand. That needs summarization — reuse the agent-assisted path (techne reflect
emit/ingest pattern), since techne can't call an LLM. For now the document content
itself is the index.
"""

from __future__ import annotations

import json
from pathlib import Path

from techne.core.documents import store_document
from techne.core.redaction import redact_credentials
from techne.platforms.claude_code.transcript_import import default_transcripts_dir
from techne.sync.jsonl_records import content_blocks, extract_text, iter_jsonl_records

_ROLE_LABEL = {"user": "User", "assistant": "Assistant"}


def transcript_to_text(jsonl_path: Path) -> tuple[str, int]:
    """Render a transcript as readable User/Assistant prose, skipping records with
    no text (pure tool_use/tool_result plumbing). Returns (text, turn_count)."""
    turns: list[str] = []
    for rec in iter_jsonl_records(jsonl_path):
        msg = rec.get("message") or {}
        label = _ROLE_LABEL.get(msg.get("role"))
        if not label:
            continue
        text = extract_text(content_blocks(rec)).strip()
        if not text:
            continue
        turns.append(f"## {label}\n\n{text}")
    return "\n\n".join(turns), len(turns)


def index_transcript(db_path: Path, jsonl_path: Path) -> tuple[int | None, str]:
    """Index one transcript as a Librarian document. Deduped/superseded by
    source_url (so re-running updates only changed sessions). Returns
    (doc_id|None, status) where status ∈ stored|unchanged|empty."""
    text, turns = transcript_to_text(jsonl_path)
    if not text:
        return None, "empty"

    session_id = jsonl_path.stem
    # Title = the first user line (what the session was about), trimmed.
    title = f"transcript {session_id[:12]}"
    marker = "## User\n\n"
    if marker in text:
        first_user = text.split(marker, 1)[1].split("\n", 1)[0].strip()
        if first_user:
            title = first_user[:80]

    doc_id, stored = store_document(
        db_path,
        source_type="cc-transcript",
        source_url=f"cc-transcript:{session_id}",
        title=title,
        content=redact_credentials(text),  # transcripts can carry tokens/canaries
        meta=json.dumps({"session_id": session_id, "turns": turns, "path": str(jsonl_path)}),
    )
    return doc_id, ("stored" if stored else "unchanged")


def index_transcripts(
    db_path: Path,
    transcripts_dir: Path | None = None,
    *,
    limit: int | None = None,
) -> dict:
    """Index every *.jsonl in a project's transcript dir into the documents store.

    Defaults to the current cwd's Claude Code project dir; pass transcripts_dir to
    index another project's sessions. Returns counts {total, stored, unchanged, empty}.
    """
    d = Path(transcripts_dir) if transcripts_dir else default_transcripts_dir()
    files = sorted(d.glob("*.jsonl"))
    if limit:
        files = files[:limit]
    counts = {"total": len(files), "stored": 0, "unchanged": 0, "empty": 0}
    for f in files:
        _doc_id, status = index_transcript(db_path, f)
        counts[status] = counts.get(status, 0) + 1
    return counts
