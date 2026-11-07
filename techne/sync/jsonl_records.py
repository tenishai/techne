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
leak, where a second reader imported these as underscore-privates across the
platform boundary. That reader, ``sync/jsonl_transcript.py``, was never wired
in and was deleted in issue 1262. See #57 (memory portability across clients).
"""

import gzip
import json
from pathlib import Path

from techne.hooks.post_tool_use import SKIP_TOOLS, _bash_is_readonly_noise


def _open_text(path: Path):
    """Open a transcript as UTF-8 text, transparently decompressing ``.gz``.

    Archived transcripts (``<session>.jsonl.gz``) must read exactly like live ones:
    aged transcripts compress ~3.6x (measured over 3 real transcripts, 67.9 MB → 18.9
    MB), and without this the reader raised ``UnicodeDecodeError`` on the gzip magic
    bytes rather than reading the file (#621).
    """
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open(encoding="utf-8")


def iter_jsonl_records(jsonl_path: Path):
    """Yield parsed JSON objects from a .jsonl (or .jsonl.gz) file, skipping
    blank/invalid lines.

    Whole-file iteration from the start. Offset-based reading is
    ``iter_jsonl_records_from`` below, which seeks a raw byte offset and so is
    uncompressed-only. That is safe only while a transcript still being appended to
    is never archived, which is an ASSUMPTION about how the archiver is operated —
    nothing here enforces it.
    """
    with _open_text(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def iter_jsonl_records_from(jsonl_path: Path, offset: int = 0):
    """Yield ``(record, resume_offset)`` for each JSON object at or after byte
    *offset* of an uncompressed .jsonl file (issue 1218).

    *resume_offset* is where a later read should start so as not to see this
    record again: the byte just past its ``\n``. A last line with no ``\n`` yet
    may be a record the writer is still appending, so it never advances the offset:
    if it already parses it is yielded with the offset of its own start, and the
    next read sees it again. Blank and unparseable lines are skipped, as
    ``iter_jsonl_records`` skips them; so is JSON that is not an object, which
    ``iter_jsonl_records`` yields as-is.

    Byte offsets mean nothing inside a compressed file, so ``.gz`` is not
    accepted here; read those whole with ``iter_jsonl_records``.
    """
    if jsonl_path.suffix == ".gz":
        raise ValueError(f"cannot resume a compressed transcript by offset: {jsonl_path}")
    with jsonl_path.open("rb") as f:
        f.seek(offset)
        pos = offset
        for line in f:
            if line.endswith(b"\n"):
                pos += len(line)
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:  # JSONDecodeError and UnicodeDecodeError
                continue
            if isinstance(rec, dict):
                yield rec, pos


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


def should_import(tool_name: str, tool_input: dict | None = None) -> bool:
    """Filter for backfill — looser than the live ``should_capture`` hook.

    Drops the explicit blocklist (``SKIP_TOOLS``) and the memory-MCP tools.
    Historical Bash/Read is worth backfilling (the live hook skips them only for
    runtime overhead) — BUT pure read-only Bash navigation (``ls``/``grep``/``cd``…)
    carries no durable signal and only dilutes the ledger and vector index, so it
    is dropped here too when the command is available. Anything with real signal
    (``git``/``pytest``/…, mutations, redirects) is kept. Mirrors the live-hook
    denylist so the noise policy is identical regardless of capture path.
    """
    if tool_name in SKIP_TOOLS:
        return False
    if tool_name.startswith("mcp__memory__"):
        return False
    if (
        tool_name == "Bash"
        and tool_input is not None
        and _bash_is_readonly_noise(tool_input.get("command", ""))
    ):
        return False
    return True
