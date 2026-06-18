"""Neutral JSONL transcript-record parsing primitives.

Platform-agnostic helpers for reading newline-delimited JSON transcripts whose
message records follow the Anthropic Messages content-block shape: a ``message``
object with a ``content`` that is either a string or a list of typed blocks
(``text`` / ``tool_use`` / ``tool_result``). Claude Code, opencode, and any
client mirroring the Anthropic messages API emit this shape.

Platform-SPECIFIC record fields (Claude Code's ``sessionId`` / ``cwd`` /
``ai-title``, a platform's on-disk path convention) stay in that platform's
module — only the reusable block parsing lives here.

Extracted from ``platforms/claude_code/transcript_import.py`` to close the #21
leak: ``sync/jsonl_transcript.py`` was importing these as underscore-privates
across the platform boundary, which a second SourceAdapter would have forced to
be promoted or duplicated. See #57 (memory portability across clients).
"""

import json
from pathlib import Path

from techne.hooks.post_tool_use import SKIP_TOOLS


def iter_jsonl_records(jsonl_path: Path):
    """Yield parsed JSON objects from a .jsonl file, skipping blank/invalid lines.

    Whole-file iteration from the start. Offset-based tailing (see
    ``jsonl_transcript``) does its own seeking and does not use this.
    """
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def content_blocks(rec: dict) -> list:
    """Return the content blocks of an Anthropic-shaped message record.

    Normalizes ``message.content`` to a list of block dicts: a string content
    becomes a single text block; a missing or unrecognized shape becomes ``[]``.
    """
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def extract_text(blocks: list) -> str:
    """Concatenate the text of every ``type=="text"`` block (assistant or user)."""
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            t = b.get("text") or ""
            if t.strip():
                parts.append(t)
    return "\n".join(parts).strip()


def tool_result_text(result) -> str:
    """Normalize a tool_result ``content`` (str | list-of-blocks | dict) to text."""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        out = []
        for b in result:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text") or "")
            elif isinstance(b, dict):
                out.append(json.dumps(b, default=str))
            else:
                out.append(str(b))
        return "\n".join(out)
    return json.dumps(result, default=str) if result is not None else ""


def should_import(tool_name: str) -> bool:
    """Filter for backfill — looser than the live ``should_capture`` hook.

    Drops only the explicit blocklist (``SKIP_TOOLS``) and the memory-MCP tools.
    Historical memory is more valuable than skipping the bash/read calls the
    live hook bounds for runtime overhead.
    """
    if tool_name in SKIP_TOOLS:
        return False
    if tool_name.startswith("mcp__memory__"):
        return False
    return True
