"""Claude Code transcript adapter — native JSONL → the canonical Transcript shape.

The ONLY Claude-Code-specific piece of transcript indexing. It knows two things:
where CC stores transcripts (``~/.claude/projects/<slug>/*.jsonl``) and how to read
them (the Anthropic Messages content-block shape). It yields canonical
`Transcript`s; the neutral core (techne.core.transcripts) does the rest — render,
redact, dedup, store, recall. A new client adds its own such adapter; nothing in
core changes (techne#91/#57).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from techne.core.transcripts import Transcript, TranscriptTurn
from techne.platforms.claude_code.transcript_import import default_transcripts_dir
from techne.sync.jsonl_records import content_blocks, extract_text, iter_jsonl_records


class ClaudeCodeTranscriptSource:
    """Yields Claude Code JSONL transcripts as canonical Transcripts."""

    platform = "claude-code"

    def __init__(self, transcripts_dir: Path | None = None):
        # Defaults to this cwd's CC project dir; pass a dir to index another project.
        self.transcripts_dir = (
            Path(transcripts_dir) if transcripts_dir else default_transcripts_dir()
        )

    def iter_transcripts(self) -> Iterator[Transcript]:
        for jsonl in sorted(self.transcripts_dir.glob("*.jsonl")):
            turns: list[TranscriptTurn] = []
            for rec in iter_jsonl_records(jsonl):
                msg = rec.get("message") or {}
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = extract_text(content_blocks(rec)).strip()
                if not text:  # pure tool_use/tool_result — plumbing, not dialogue
                    continue
                turns.append(TranscriptTurn(role=role, text=text))
            yield Transcript(
                id=jsonl.stem,
                platform=self.platform,
                turns=turns,
                uri=str(jsonl),
            )
