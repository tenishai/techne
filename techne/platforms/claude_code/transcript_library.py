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
        # Files the LAST full consumption of iter_transcripts could not read, as
        # (path, error). Valid only after the generator is exhausted, and it describes
        # the files ITERATED — a caller that stops early (``index_transcripts(limit=N)``
        # wraps this in islice) has not looked at the rest, so this is not a coverage
        # figure for the directory. Reset at the top of each iteration.
        self.unreadable: list[tuple[Path, str]] = []

    def iter_transcripts(self) -> Iterator[Transcript]:
        self.unreadable = []
        for jsonl in iter_transcript_files(self.transcripts_dir):
            turns: list[TranscriptTurn] = []
            try:
                records = list(iter_jsonl_records(jsonl))
            except Exception as e:
                # Deliberately broad, matching ``import_directory``. Enumerating the
                # gzip failure classes was tried and was WRONG: BadGzipFile subclasses
                # OSError and a truncated archive raises EOFError, but a valid header
                # over a corrupt payload raises ``zlib.error``, which is neither — so
                # an ``(OSError, EOFError)`` handler let the most likely corruption
                # through and took the whole run down. One unreadable file must not do
                # that; nor may it pass QUIETLY. Recorded here and surfaced by
                # ``index_transcripts`` as ``counts["unreadable"]`` — a swallowed error
                # nothing reports would make a short count read as a smaller corpus.
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
