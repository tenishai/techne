"""Claude Code transcript adapter — native JSONL → the canonical Transcript shape.

The ONLY Claude-Code-specific piece of transcript indexing. It knows two things:
where CC stores transcripts (``~/.claude/projects/<slug>/*.jsonl``) and how to read
them (the Anthropic Messages content-block shape). It yields canonical
`Transcript`s; the neutral core (techne.core.transcripts) does the rest — render,
redact, dedup, store, recall. A new client adds its own such adapter; nothing in
core changes (techne#91/#57).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from techne.core.transcripts import Transcript, TranscriptTurn
from techne.platforms.claude_code.transcript_import import (
    default_transcripts_dir,
    iter_transcript_files,
    session_id_for,
)
from techne.sync.jsonl_records import content_blocks, extract_text, iter_jsonl_records

log = logging.getLogger(__name__)


class ClaudeCodeTranscriptSource:
    """Yields Claude Code JSONL transcripts as canonical Transcripts."""

    platform = "claude-code"

    def __init__(self, transcripts_dir: Path | None = None):
        # Defaults to this cwd's CC project dir; pass a dir to index another project.
        self.transcripts_dir = (
            Path(transcripts_dir) if transcripts_dir else default_transcripts_dir()
        )
        # Files iter_transcripts could not read, as (path, error). A caller measuring
        # index coverage can ask what was passed over instead of inferring it from a
        # count that silently came up short.
        self.unreadable: list[tuple[Path, str]] = []

    def iter_transcripts(self) -> Iterator[Transcript]:
        for jsonl in iter_transcript_files(self.transcripts_dir):
            turns: list[TranscriptTurn] = []
            try:
                records = list(iter_jsonl_records(jsonl))
            except (OSError, EOFError) as e:
                # gzip.BadGzipFile subclasses OSError, but a TRUNCATED archive raises
                # EOFError ("Compressed file ended before the end-of-stream marker"),
                # which does not — both spellings have to be caught.
                # An unreadable file (a truncated .gz, a permission error) must not take
                # the whole indexing run down — but it must not pass QUIETLY either, or
                # the next coverage measurement is a confident wrong answer. Count it.
                self.unreadable.append((jsonl, str(e)))
                log.warning("transcript_unreadable path=%s error=%s", jsonl, e)
                continue
            for rec in records:
                msg = rec.get("message") or {}
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = extract_text(content_blocks(rec)).strip()
                if not text:  # pure tool_use/tool_result — plumbing, not dialogue
                    continue
                turns.append(TranscriptTurn(role=role, text=text))
            yield Transcript(
                # NOT jsonl.stem — that leaves the ".jsonl" on an archived
                # "<session>.jsonl.gz" and this id becomes the Librarian source_url,
                # so the same session would index twice under two identities (#621).
                id=session_id_for(jsonl),
                platform=self.platform,
                turns=turns,
                uri=str(jsonl),
            )
